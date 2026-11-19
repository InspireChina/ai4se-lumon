"""Resolve Feishu source material before the Agent answers a referenced question."""

from __future__ import annotations

import asyncio
import json
import re
import subprocess
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from email.utils import parseaddr, parsedate_to_datetime
from html import unescape
from pathlib import Path
from typing import cast

from lumon.agents.agent.mail_attachment import (
    AttachmentKind,
    attachment_evidence,
    attachment_kind,
    download_mail_attachment,
)
from lumon.agents.agent.model import InboundMessage

_DOC_URL = re.compile(
    r"https://(?:[a-z0-9-]+\.)*(?:feishu\.cn|larksuite\.com|doubao\.com)"
    r"/(?:docx|wiki)/[a-z0-9]+(?=$|[\s?#)\]>.,\"'])",
    re.IGNORECASE,
)
_CARD_BLOCK = re.compile(r"<feishu-(quoted-message|card-context)>\n(.*?)\n</feishu-\1>", re.DOTALL)
_MAIL_SENDER = re.compile(
    r"^(?:sender|from|发件人|寄件人|發件人)\s*[:：]\s*(.+)$", re.IGNORECASE | re.MULTILINE
)
_MAIL_TIME = re.compile(r"^(?:time|date|时间|時間)\s*[:：]\s*(.+)$", re.IGNORECASE | re.MULTILINE)
_LOCAL_MAIL_DATE = re.compile(
    r"(\d{4}年\d{1,2}月\d{1,2}日)\s*"
    r"(?:[（(]\s*(?:周|週|星期)[一二三四五六日天]\s*[）)]\s*)?"
    r"(\d{1,2}:\d{2})(?=\s|$)"
)
_MAX_SOURCE_LENGTH = 160_000
_MAX_ATTACHMENT_COUNT = 32
_MAX_ATTACHMENT_BYTES = 8 * 1024 * 1024
_MAX_ATTACHMENT_TOTAL_BYTES = 24 * 1024 * 1024
_READ_TIMEOUT = 12

CliRead = Callable[..., Mapping[str, object]]
AsyncCliRead = Callable[..., Awaitable[Mapping[str, object]]]
AttachmentDownload = Callable[[str, int], bytes | None]


@dataclass(frozen=True, slots=True)
class ReferenceContext:
    text: str = ""
    images: tuple[Path, ...] = ()


@dataclass(frozen=True, slots=True)
class MailPreview:
    subject: str
    sender: str
    date: datetime | None


@dataclass(frozen=True, slots=True)
class _MailAttachment:
    message_id: str
    attachment_id: str
    kind: AttachmentKind
    media_type: str
    source: dict[str, object]


def document_links(text: str) -> tuple[str, ...]:
    """Recognize Feishu document addresses and drop selection anchors and query strings."""

    return tuple(dict.fromkeys(match.group() for match in _DOC_URL.finditer(text)))


def _references(text: str) -> tuple[tuple[MailPreview, ...], tuple[str, ...]]:
    previews: list[MailPreview] = []

    def keep_document_reference(match: re.Match[str]) -> str:
        preview = _mail_preview(match[2])
        if preview is None:
            return match[2]
        previews.append(preview)
        return ""

    document_text = _CARD_BLOCK.sub(keep_document_reference, text)
    return tuple(dict.fromkeys(previews)), document_links(document_text)


async def read_reference_context(
    message: InboundMessage,
    *,
    image_directory: Path,
    read: CliRead | None = None,
    download: AttachmentDownload = download_mail_attachment,
) -> ReferenceContext:
    """Fetch the original sources without altering the stored inbound message."""

    previews, documents = _references(message.text)
    if not previews and not documents:
        return ReferenceContext()
    if message.is_group or message.sender_type != "user":
        return _render(
            [],
            image_directory,
            "Full-source lookup is available in the matching user's direct chat.",
        )

    async def read_source(*args: str) -> Mapping[str, object]:
        return await asyncio.to_thread(read or _read_cli, *args)

    sources: list[dict[str, object]] = []
    attachments: list[_MailAttachment] = []
    issue = ""
    try:
        async with asyncio.timeout(30):
            profile = await read_source("contact", "+get-user")
            if _same_user(message, _object(profile.get("user"))):
                await _read_sources(previews, documents, read_source, sources, attachments)
                await _read_attachments(attachments, read_source, download, image_directory)
            else:
                issue = "The local Feishu login could not be matched to the requester."
    except TimeoutError:
        issue = "Full-source lookup timed out. Some sources may be missing."
    return _render(sources, image_directory, issue)


async def _read_sources(
    previews: tuple[MailPreview, ...],
    documents: tuple[str, ...],
    read: AsyncCliRead,
    sources: list[dict[str, object]],
    attachments: list[_MailAttachment],
) -> None:
    seen_threads: set[str] = set()
    for preview in previews:
        thread = await _resolve_thread(preview, read)
        if thread is None:
            sources.append(_unavailable("email_thread", preview.subject))
            continue
        if thread[0] not in seen_threads:
            seen_threads.add(thread[0])
            source, messages = await _read_thread(preview.subject, *thread, read)
            sources.append(source)
            for index, item in enumerate(messages, start=1):
                attachments.extend(_mail_attachments(preview.subject, index, item, sources))
    for url in documents:
        result = await read("docs", "+fetch", "--doc", url, "--doc-format", "markdown")
        document = _object(result.get("document"))
        content = _string(document.get("content"))
        if not content or document.get("has_more") is True or result.get("has_more") is True:
            sources.append(_unavailable("document", url))
        else:
            sources.append(
                {"kind": "document", "reference": url, "status": "complete", "content": content}
            )


def _same_user(message: InboundMessage, identity: Mapping[str, object]) -> bool:
    if message.sender_id.startswith("ou_") and message.sender_id == identity.get("open_id"):
        return True
    union_id = message.sender_union_id
    return bool(union_id and union_id.startswith("on_") and union_id == identity.get("union_id"))


def _mail_preview(text: str) -> MailPreview | None:
    plain = unescape(text).replace("**", "").replace("__", "").strip()
    sender = _MAIL_SENDER.search(plain)
    date = _MAIL_TIME.search(plain)
    if sender is None or date is None or not plain:
        return None
    address = parseaddr(sender[1].replace("\u00ad", ""))[1].casefold()
    if not address or "@" not in address:
        return None
    local_date = _LOCAL_MAIL_DATE.match(plain, date.start(1))
    timestamp = f"{local_date[1]} {local_date[2]}" if local_date else date[1]
    return MailPreview(plain.splitlines()[0].lstrip("# ").strip(), address, _date(timestamp))


def _date(value: str) -> datetime | None:
    value = value.strip()
    if re.fullmatch(r"[0-9]{10}(?:[0-9]{3})?", value):
        timestamp = int(value) / (1000 if len(value) == 13 else 1)
        return datetime.fromtimestamp(timestamp, UTC)
    if ":" not in value:
        return None
    if "年" in value:
        try:
            return datetime.strptime(value, "%Y年%m月%d日 %H:%M")
        except ValueError:
            return None
    try:
        return parsedate_to_datetime(value)
    except ValueError:
        pass
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        try:
            return datetime.strptime(value, "%a, %b %d, %Y, %H:%M")
        except ValueError:
            return None


def _minute(value: datetime) -> datetime:
    return value.replace(tzinfo=None, second=0, microsecond=0)


def _mail_matches(item: Mapping[str, object], preview: MailPreview, date: datetime) -> bool:
    if _string(item.get("subject")).casefold() != preview.subject.casefold():
        return False
    if parseaddr(_string(item.get("from")))[1].casefold() != preview.sender:
        return False
    sent = _date(_string(item.get("date")))
    if sent is None:
        return False
    if _minute(sent) == _minute(date) and date.tzinfo is None:
        return True
    return _minute(sent.astimezone(date.tzinfo)) == _minute(date)


async def _resolve_thread(preview: MailPreview, read: AsyncCliRead) -> tuple[str, str] | None:
    if not preview.subject or preview.date is None:
        return None
    filters = json.dumps({"subject": preview.subject, "from": [preview.sender]}, ensure_ascii=False)
    listing = await read("mail", "+triage", "--filter", filters, "--max", "100")
    if listing.get("has_more") is not False:
        return None
    matches = [
        item
        for item in _objects(listing.get("messages"))
        if _mail_matches(item, preview, preview.date)
    ]
    if len(matches) != 1:
        return None
    message_id = _string(matches[0].get("message_id"))
    if not message_id:
        return None
    thread_id = _string(matches[0].get("thread_id"))
    if thread_id:
        return thread_id, message_id
    mail = await read("mail", "+message", "--message-id=" + message_id, "--html=false")
    thread_id = _string(mail.get("thread_id"))
    return (thread_id, message_id) if thread_id else None


async def _read_thread(
    subject: str, thread_id: str, message_id: str, read: AsyncCliRead
) -> tuple[dict[str, object], list[Mapping[str, object]]]:
    thread = await read("mail", "+thread", "--thread-id=" + thread_id, "--html=false")
    messages = _objects(thread.get("messages"))
    if (
        not messages
        or len(messages) != thread.get("message_count")
        or thread.get("has_more") is True
        or not any(item.get("message_id") == message_id for item in messages)
    ):
        return _unavailable("email_thread", subject), []
    delivered = [
        item
        for item in messages
        if item.get("message_state_text") != "draft" and item.get("message_state") not in (3, "3")
    ]
    if not delivered:
        return _unavailable("email_thread", subject), []
    message_bodies = _mail_bodies(delivered)
    complete = all("body_plain_text" in body for body in message_bodies)
    source: dict[str, object] = {
        "kind": "email_thread",
        "reference": subject,
        "status": "complete" if complete else "partial",
        "messages": message_bodies,
    }
    if not complete:
        source["reason"] = "One or more messages did not include a readable body."
    return source, delivered


def _mail_attachments(
    subject: str, index: int, message: Mapping[str, object], sources: list[dict[str, object]]
) -> list[_MailAttachment]:
    message_id = _string(message.get("message_id"))
    attachments: list[_MailAttachment] = []
    for item in _objects(message.get("attachments")):
        filename = _string(item.get("filename")) or "Unnamed attachment"
        media_type = _string(item.get("content_type"))
        attachment_id = _string(item.get("id"))
        source = {
            **_unavailable("email_attachment", filename),
            "thread": subject,
            "message_number": index,
            "inline": item.get("is_inline") is True,
        }
        sources.append(source)
        kind = attachment_kind(filename, media_type)
        if kind is None:
            source["reason"] = "Unsupported attachment format."
        elif not message_id or not attachment_id:
            source["reason"] = "Feishu did not provide an attachment identifier."
        else:
            source["reason"] = "Attachment lookup did not finish."
            attachments.append(_MailAttachment(message_id, attachment_id, kind, media_type, source))
    return attachments


async def _read_attachments(
    attachments: list[_MailAttachment],
    read: AsyncCliRead,
    download: AttachmentDownload,
    directory: Path,
) -> None:
    by_message: dict[str, list[_MailAttachment]] = {}
    for index, item in enumerate(attachments):
        if index >= _MAX_ATTACHMENT_COUNT:
            item.source["reason"] = "Attachment count exceeds the limit for this turn."
        else:
            by_message.setdefault(item.message_id, []).append(item)

    remaining = _MAX_ATTACHMENT_TOTAL_BYTES
    for message_id, items in by_message.items():
        urls = await _attachment_urls(message_id, items, read)
        for item in items:
            url = urls.get(item.attachment_id)
            if not url:
                item.source["reason"] = "Feishu did not return a download link."
                continue
            if remaining <= 0:
                item.source["reason"] = "Attachments exceed the size limit for this turn."
                continue
            limit = min(remaining, _MAX_ATTACHMENT_BYTES)
            content = await asyncio.to_thread(download, url, limit)
            if content is None or len(content) > limit:
                item.source["reason"] = "Download failed or exceeded the size limit."
                continue
            remaining -= len(content)
            evidence = attachment_evidence(
                content, kind=item.kind, media_type=item.media_type, directory=directory
            )
            if "reason" not in evidence:
                del item.source["reason"]
                item.source["status"] = "complete"
            item.source.update(evidence)


async def _attachment_urls(
    message_id: str, items: list[_MailAttachment], read: AsyncCliRead
) -> dict[str, str]:
    params = json.dumps(
        {
            "user_mailbox_id": "me",
            "message_id": message_id,
            "attachment_ids": list(dict.fromkeys(item.attachment_id for item in items)),
        }
    )
    result = await read(
        "mail", "user_mailbox.message.attachments", "download_url", "--params", params
    )
    return {
        _string(item.get("attachment_id")): _string(item.get("download_url"))
        for item in _objects(result.get("download_urls"))
    }


def _mail_bodies(messages: list[Mapping[str, object]]) -> list[dict[str, object]]:
    records: list[dict[str, object]] = []
    earlier_bodies: list[tuple[int, str]] = []
    for number, message in enumerate(messages, start=1):
        record = _mail_body(message)
        records.append(record)
        body = record.get("body_plain_text")
        if not isinstance(body, str) or len(body) < 512:
            continue
        # An exact suffix can be restored from the earlier message without sending it twice.
        for prior_number, prior_body in reversed(earlier_bodies):
            if body.endswith(prior_body):
                record["body_plain_text"] = body[: -len(prior_body)]
                record["quoted_suffix_from_message"] = prior_number
                break
        earlier_bodies.append((number, body))
    return records


def _mail_body(message: Mapping[str, object]) -> dict[str, object]:
    evidence = {
        key: message[key]
        for key in ("subject", "head_from", "to", "cc", "date", "date_formatted")
        if key in message
    }
    body = message.get("body_plain_text")
    if isinstance(body, str) and (body.strip() or not message.get("body_preview")):
        evidence["body_plain_text"] = body
    else:
        evidence["body_status"] = "unavailable"
    return evidence


def _read_cli(*args: str) -> Mapping[str, object]:
    try:
        result = subprocess.run(
            ["lark-cli", *args, "--as", "user", "--format", "json"],
            check=True,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            timeout=_READ_TIMEOUT,
        )
        payload = _object(json.loads(result.stdout))
    except (OSError, subprocess.SubprocessError, ValueError):
        return {}
    if payload.get("ok") is True and payload.get("identity") == "user":
        return _object(payload.get("data"))
    # Mail triage prints a plain JSON list envelope even with explicit --as user.
    if args[:2] == ("mail", "+triage") and isinstance(payload.get("has_more"), bool):
        return payload
    return {}


def _render(sources: list[dict[str, object]], directory: Path, issue: str = "") -> ReferenceContext:
    records: list[dict[str, object]] = []
    images: dict[str, int] = {}
    used = 0
    for source in sources:
        size = len(json.dumps(source, ensure_ascii=False).replace("<", "\\u003c"))
        if used + size > _MAX_SOURCE_LENGTH:
            record = {
                key: value
                for key, value in source.items()
                if key not in {"content", "messages", "image"}
            }
            record.update(status="unavailable", reason="Source exceeds the context limit.")
            records.append(record)
            continue
        image = _string(source.get("image"))
        if image:
            source["image_number"] = images.setdefault(image, len(images) + 1)
        records.append(source)
        used += size
    content = json.dumps({"sources": records, "issue": issue}, ensure_ascii=False)
    text = (
        "<feishu-reference-context>\n"
        + content.replace("<", "\\u003c")
        + "\n</feishu-reference-context>"
    )
    return ReferenceContext(text, tuple(directory / image for image in images))


def _unavailable(kind: str, reference: str) -> dict[str, object]:
    return {"kind": kind, "reference": reference, "status": "unavailable"}


def _object(value: object) -> Mapping[str, object]:
    if isinstance(value, Mapping):
        return cast(Mapping[str, object], value)
    return {}


def _objects(value: object) -> tuple[Mapping[str, object], ...]:
    if isinstance(value, list):
        return tuple(_object(item) for item in cast(list[object], value))
    return ()


def _string(value: object) -> str:
    return value.strip() if isinstance(value, str) else ""
