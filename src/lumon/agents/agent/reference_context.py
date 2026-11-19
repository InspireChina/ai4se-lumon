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
from typing import cast

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
_MAX_SOURCE_LENGTH = 160_000
_READ_TIMEOUT = 12

CliRead = Callable[..., Mapping[str, object]]
AsyncCliRead = Callable[..., Awaitable[Mapping[str, object]]]


@dataclass(frozen=True, slots=True)
class MailPreview:
    subject: str
    sender: str
    date: datetime | None


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


async def read_reference_context(message: InboundMessage, *, read: CliRead | None = None) -> str:
    """Fetch the original sources without altering the stored inbound message."""

    previews, documents = _references(message.text)
    if not previews and not documents:
        return ""
    if message.is_group or message.sender_type != "user":
        return _render([], "Full-source lookup is available in the matching user's direct chat.")

    async def read_source(*args: str) -> Mapping[str, object]:
        return await asyncio.to_thread(read or _read_cli, *args)

    try:
        async with asyncio.timeout(30):
            return await _read_sources(message, previews, documents, read_source)
    except TimeoutError:
        return _render([], "Full-source lookup timed out.")


async def _read_sources(
    message: InboundMessage,
    previews: tuple[MailPreview, ...],
    documents: tuple[str, ...],
    read: AsyncCliRead,
) -> str:
    profile = await read("contact", "+get-user")
    identity = _object(profile.get("user"))
    if not _same_user(message, identity):
        return _render([], "The local Feishu login could not be matched to the requester.")

    sources: list[dict[str, object]] = []
    seen_threads: set[str] = set()
    for preview in previews:
        thread = await _resolve_thread(preview, read)
        if thread is None:
            sources.append(_unavailable("email_thread", preview.subject))
            continue
        if thread[0] not in seen_threads:
            seen_threads.add(thread[0])
            sources.append(await _read_thread(preview.subject, *thread, read))
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
    return _render(sources)


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
    address = parseaddr(sender[1])[1].casefold()
    if not address or "@" not in address:
        return None
    return MailPreview(plain.splitlines()[0].lstrip("# ").strip(), address, _date(date[1]))


def _date(value: str) -> datetime | None:
    value = value.strip()
    if re.fullmatch(r"[0-9]{10}(?:[0-9]{3})?", value):
        timestamp = int(value) / (1000 if len(value) == 13 else 1)
        return datetime.fromtimestamp(timestamp, UTC)
    if ":" not in value:
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
) -> dict[str, object]:
    thread = await read("mail", "+thread", "--thread-id=" + thread_id, "--html=false")
    messages = _objects(thread.get("messages"))
    if (
        not messages
        or len(messages) != thread.get("message_count")
        or thread.get("has_more") is True
        or not any(item.get("message_id") == message_id for item in messages)
    ):
        return _unavailable("email_thread", subject)
    delivered = [
        item
        for item in messages
        if item.get("message_state_text") != "draft" and item.get("message_state") not in (3, "3")
    ]
    if not delivered or any(not _has_plain_body(item) for item in delivered):
        return _unavailable("email_thread", subject)
    return {
        "kind": "email_thread",
        "reference": subject,
        "status": "complete",
        "messages": [
            {
                key: item[key]
                for key in (
                    "subject",
                    "head_from",
                    "to",
                    "cc",
                    "date",
                    "date_formatted",
                    "body_plain_text",
                )
                if key in item
            }
            for item in delivered
        ],
    }


def _has_plain_body(message: Mapping[str, object]) -> bool:
    body = message.get("body_plain_text")
    return isinstance(body, str) and bool(body.strip() or not message.get("body_preview"))


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


def _render(sources: list[dict[str, object]], issue: str = "") -> str:
    records: list[dict[str, object]] = []
    used = 0
    for source in sources:
        size = len(json.dumps(source, ensure_ascii=False))
        if used + size > _MAX_SOURCE_LENGTH:
            records.append(
                {
                    **_unavailable(_string(source["kind"]), _string(source["reference"])),
                    "reason": "Source exceeds the context limit.",
                }
            )
        else:
            records.append(source)
            used += size
    content = json.dumps({"sources": records, "issue": issue}, ensure_ascii=False)
    return (
        "<feishu-reference-context>\n"
        + content.replace("<", "\\u003c")
        + "\n</feishu-reference-context>"
    )


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
