"""Jira reads and verified publication to one source issue through TWG."""

from __future__ import annotations

import asyncio
import json
import os
import re
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import cast
from uuid import UUID

from lumon.auto_test.config import AutoTestSettings
from lumon.auto_test.jira_reference import MAX_REFERENCE_BYTES, REFERENCE_SUFFIXES, read_reference
from lumon.auto_test.model import ISSUE_PATTERN, Artifact, ClarificationQuestion, TestRun
from lumon.auto_test.store import RunStore
from lumon.errors import PreflightError
from lumon.tools.jira_delivery import split_jql
from lumon.tools.safety import sanitize_output

_OBJECT_ID = re.compile(r"^[a-zA-Z0-9:/_.-]{1,240}$")
_AUTH_ERRORS = {"AUTH_INVALID", "AUTH_REQUIRED", "AUTH_MISSING", "AUTH_EXPIRED"}
_RESULT_MARKER = re.compile(r"^(?:#+\s*)?Lumon Auto Test run [0-9a-f-]{36}\b")


class JiraError(PreflightError):
    def __init__(self, message: str, *, rejected: bool = False) -> None:
        super().__init__(message)
        self.rejected = rejected


@dataclass(frozen=True, slots=True)
class JiraComment:
    id: str
    body: str
    created: datetime | None
    updated: datetime | None


@dataclass(frozen=True, slots=True)
class JiraIssue:
    key: str
    title: str
    evidence: str
    comments: tuple[JiraComment, ...] = ()
    questions: tuple[ClarificationQuestion, ...] = ()
    replies: tuple[str, ...] = ()
    clarification_run_id: UUID | None = None
    references: tuple[str, ...] = ()
    unread_attachments: tuple[str, ...] = ()

    def replies_after(self, comment_id: str) -> tuple[str, ...]:
        asked = next(
            (comment.created for comment in self.comments if comment.id == comment_id), None
        )
        if asked is None:
            return ()
        replies: list[str] = []
        for comment in self.comments:
            activity = comment.updated or comment.created
            if (
                activity
                and activity > asked
                and comment.body
                and not _RESULT_MARKER.match(comment.body)
            ):
                replies.append(comment.body[:6000])
        return tuple(replies[-20:])


class TestJira:
    def __init__(self, site: str, binary: str = "twg") -> None:
        self.site = site
        self.binary = binary

    async def candidate(
        self, settings: AutoTestSettings, claimed: frozenset[str], issue_key: str | None = None
    ) -> str | None:
        excluded: frozenset[str] = frozenset()
        limit = 50 if claimed else 1
        while True:
            query = eligible_query(settings, excluded, issue_key)
            reply = await self._call(
                "query", "--jql", query, "--fields", "summary", "--first", str(limit)
            )
            issues = _records(_object(reply.get("data")).get("issues"))
            keys = tuple(_issue_key(item.get("key")) for item in issues)
            if len(keys) > limit or any(
                key.rsplit("-", 1)[0] != settings.jira_project
                or key in excluded
                or (issue_key is not None and key != issue_key)
                for key in keys
            ):
                raise JiraError(
                    "Jira returned an Auto Test candidate outside the configured scope."
                )
            candidate = next((key for key in keys if key not in claimed), None)
            if candidate is not None or len(keys) < limit:
                return candidate
            # TWG search omits paging metadata. Exclude only matching issues from this poll.
            excluded = excluded.union(keys)

    async def issue(self, key: str) -> JiraIssue:
        item = await self._get(key, "--full")
        title = sanitize_output(_text(item.get("summary")))[:500]
        description = _text(item.get("description"))
        comments = tuple(_jira_comment(row) for row in await self.comments(key))
        bodies = [comment.body for comment in comments if not _RESULT_MARKER.match(comment.body)]
        evidence = sanitize_output("\n\n".join((title, description, *bodies)))[:32000]
        files = item.get("attachment", item.get("attachments", []))
        references, unread = await self._read_references(_records(files) if files else [])
        return JiraIssue(
            key, title or key, evidence, comments, references=references, unread_attachments=unread
        )

    async def _read_references(
        self, attachments: list[dict[str, object]]
    ) -> tuple[tuple[str, ...], tuple[str, ...]]:
        found: list[str] = []
        unread: list[str] = []
        attempts = 0
        for attachment in attachments[:40]:
            name = sanitize_output(str(attachment.get("filename", "Attachment")))[:200]
            suffix = Path(name).suffix.lower()
            identifier, size = str(attachment.get("id", "")), attachment.get("size")
            if (
                suffix not in REFERENCE_SUFFIXES
                or not identifier.isdecimal()
                or not isinstance(size, int)
                or not 0 < size <= MAX_REFERENCE_BYTES
                or attempts >= 4
            ):
                unread.append(name)
                continue
            attempts += 1
            try:
                with TemporaryDirectory(prefix="lumon-jira-reference-") as directory:
                    path = Path(directory) / f"reference{suffix}"
                    await self._call(
                        "attachment",
                        "download",
                        "--attachment-id",
                        identifier,
                        "--output-path",
                        str(path),
                    )
                    if path.stat().st_size != size:
                        raise PreflightError("Jira reference size did not match its metadata.")
                    contents = await asyncio.to_thread(read_reference, path)
            except (PreflightError, OSError, ValueError):
                unread.append(name)
                continue
            if contents.strip():
                found.append(f"{name}\n{sanitize_output(contents)}")
            else:
                unread.append(name)
        return tuple(found), tuple(unread)

    async def _get(self, key: str, *options: str) -> dict[str, object]:
        data = await self._call("get", key, *options)
        values = _records(data.get("data"))
        if len(values) != 1 or values[0].get("key") != key:
            raise JiraError("Unable to verify the selected Jira issue.")
        return values[0]

    async def block(self, key: str) -> None:
        current = _object((await self._get(key, "--fields", "status")).get("status"))
        if _is_block(current.get("name")):
            return
        category = _object(current.get("statusCategory"), allow_none=True)
        if not current.get("name") or str(category.get("key", "")).casefold() == "done":
            raise JiraError("The current Jira status does not allow an Auto Test block.")
        reply = await self._call("transition", "--id", key)
        transitions = _records(_object(reply.get("data")).get("transitions"))
        matches = [item for item in transitions if _is_block(item.get("toName"))]
        transition_id = _matching_id(matches)
        if transition_id is None:
            raise JiraError("Jira did not offer a Block transition for this issue.")
        if matches[0].get("requirements"):
            raise JiraError("The Jira Block transition requires additional inputs.")
        await self._call("transition", "--id", key, "--transition-id", transition_id)
        status = _object((await self._get(key, "--fields", "status")).get("status"))
        if not _is_block(status.get("name")):
            raise JiraError("The Jira Block transition could not be verified.")

    async def attachments(self, key: str) -> list[dict[str, object]]:
        response = await self._call("attachment", "query", "--issue-id", key)
        return _records(response.get("data"))

    async def comments(self, key: str) -> list[dict[str, object]]:
        comments: list[dict[str, object]] = []
        cursor = ""
        for _ in range(20):
            paging = ("--after", cursor) if cursor else ()
            response = await self._call(
                "comment", "query", "--issue-id", key, "--first", "50", *paging
            )
            items = _records(response.get("data"))
            comments.extend(items)
            info = _object(response.get("pageInfo"), allow_none=True)
            if info.get("hasNextPage") is not True:
                if not info and len(items) >= 50:
                    raise JiraError("Jira comment history could not be read completely.")
                return comments
            next_cursor = info.get("nextCursor", info.get("endCursor"))
            if not isinstance(next_cursor, str) or not next_cursor or next_cursor == cursor:
                raise JiraError("Jira comment history returned an invalid page cursor.")
            cursor = next_cursor
        raise JiraError("Jira comment history exceeds the reconciliation limit.")

    async def upload(self, key: str, path: Path) -> None:
        await self._call(
            "attachment", "upload", "--issue-id", key, "--file", str(path), timeout=180
        )

    async def write_comment(self, key: str, body: str, comment_id: str | None) -> None:
        command = ("update", "--id", comment_id) if comment_id else ("create",)
        await self._call(
            "comment", *command, "--issue-id", key, "--body-format", "markdown", "--body", body
        )

    async def _call(self, *arguments: str, timeout: float = 30) -> dict[str, object]:
        process: asyncio.subprocess.Process | None = None
        try:
            async with asyncio.timeout(timeout):
                process = await asyncio.create_subprocess_exec(
                    self.binary,
                    "jira",
                    "workitem",
                    *arguments,
                    "--site",
                    self.site,
                    "--output",
                    "json",
                    "--output-summary",
                    "none",
                    stdin=asyncio.subprocess.DEVNULL,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.DEVNULL,
                    env=dict(os.environ, TWG_AGENT_DEFAULTS="1"),
                )
                assert process.stdout is not None
                return await _read_reply(process, process.stdout)
        except (TimeoutError, ValueError, OSError) as exc:
            raise JiraError("TWG could not complete the Auto Test Jira request.") from exc
        finally:
            if process is not None and process.returncode is None:
                try:
                    process.kill()
                except ProcessLookupError:
                    pass
                await process.wait()


def eligible_query(
    settings: AutoTestSettings, claimed: frozenset[str], issue_key: str | None
) -> str:
    predicate, sort = split_jql(settings.trigger_jql)
    if not predicate or not settings.jira_project:
        raise JiraError("Configure a Jira project and Auto Test JQL.")
    query = f"({predicate}) AND project = {settings.jira_project}"
    if issue_key is not None:
        query += f" AND key = {_issue_key(issue_key)}"
    if claimed:
        query += " AND key NOT IN (" + ", ".join(_issue_key(key) for key in sorted(claimed)) + ")"
    query = f"{query} {sort}".strip()
    if len(query) > 8000:
        raise JiraError("Auto Test JQL and previous claims exceed the query length limit.")
    return query


async def _read_reply(
    process: asyncio.subprocess.Process, stream: asyncio.StreamReader
) -> dict[str, object]:
    payload = bytearray()
    while chunk := await stream.read(65536):
        payload.extend(chunk)
        if len(payload) > 4 * 1024 * 1024:
            raise JiraError("TWG returned a Jira response larger than the Auto Test limit.")
    await process.wait()
    response = _object(json.loads(payload))
    error = _object(response.get("error"), allow_none=True)
    code = error.get("code")
    if isinstance(code, str) and code in _AUTH_ERRORS:
        raise JiraError(f"TWG Jira authentication needs attention ({code}).", rejected=True)
    if process.returncode != 0 or response.get("ok") is False or response.get("errors"):
        status = error.get("status", error.get("statusCode"))
        rejected = str(status) in {"400", "401", "403", "404", "413", "415", "422", "429"}
        hint = f" (HTTP {status})" if rejected else ""
        raise JiraError(
            f"Jira rejected or did not confirm the Auto Test request{hint}.", rejected=rejected
        )
    return response


def _issue_key(value: object) -> str:
    if not isinstance(value, str) or not re.fullmatch(ISSUE_PATTERN, value):
        raise JiraError("Invalid Jira issue key in the Auto Test queue.")
    return value


def _object(value: object, *, allow_none: bool = False) -> dict[str, object]:
    if value is None and allow_none:
        return {}
    if not isinstance(value, dict):
        raise JiraError("TWG returned an invalid Jira response.")
    return cast(dict[str, object], value)


def _records(value: object) -> list[dict[str, object]]:
    if not isinstance(value, list):
        raise JiraError("TWG returned an incomplete Jira result list.")
    return [_object(item) for item in cast(list[object], value)]


def _text(value: object) -> str:
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        return "\n".join(_text(item) for item in cast(list[object], value))
    if isinstance(value, dict):
        item = cast(dict[str, object], value)
        return _text(item.get("text", item.get("body", item.get("content", item.get("comments")))))
    return ""


def _jira_time(raw: object) -> datetime | None:
    if not isinstance(raw, str):
        return None
    try:
        parsed = datetime.fromisoformat(raw)
    except ValueError:
        return None
    return parsed if parsed.tzinfo is not None else None


def _jira_comment(row: dict[str, object]) -> JiraComment:
    return JiraComment(
        id=_matching_id([row]) or "",
        body=sanitize_output(_text(row.get("body"))).strip(),
        created=_jira_time(row.get("created")),
        updated=_jira_time(row.get("updated")),
    )


def _is_block(status: object) -> bool:
    if not isinstance(status, str):
        return False
    return status.strip().casefold().removesuffix(" (migrated)") in {"block", "blocked"}


def _matching_id(items: list[dict[str, object]]) -> str | None:
    if not items:
        return None
    item_id = items[0].get("id")
    if (
        len(items) != 1
        or not isinstance(item_id, (str, int))
        or not _OBJECT_ID.fullmatch(str(item_id))
    ):
        raise JiraError("Jira returned ambiguous Auto Test publication records.")
    return str(item_id)


class JiraPublisher:
    """Persist write intent before each call so an uncertain result is never resent blindly."""

    def __init__(self, gateway: TestJira, store: RunStore) -> None:
        self.gateway = gateway
        self.store = store

    async def publish(self, run: TestRun) -> TestRun:
        if run.status not in {"completed", "blocked"}:
            raise PreflightError("Only a completed or blocked Auto Test run can be published.")
        if self.gateway.site != run.jira_site:
            raise PreflightError("The run belongs to a different Jira site.")
        if run.plan and run.plan.questions:
            return await self._publish_questions(run)
        errors: list[str] = []
        if run.plan is not None and run.workbook is None:
            errors.append("The Excel workbook is unavailable.")
        for artifact in run.artifacts:
            if artifact.kind == "screenshot":
                continue
            try:
                run = await self._attachment(run, artifact)
            except JiraError as exc:
                run = self.store.load(run.run_id)
                errors.append(f"{artifact.filename}: {exc}")
        try:
            run = await self._comment(run)
        except JiraError as exc:
            run = self.store.load(run.run_id)
            errors.append(str(exc))
        status = ("partial" if run.jira.comment_id else "failed") if errors else "published"
        return self._save(run, status=status, detail="\n".join(errors)[:3000])

    async def _publish_questions(self, run: TestRun) -> TestRun:
        try:
            run = await self._comment(run)
            if not run.jira.block_confirmed:
                await self.gateway.block(run.issue_key)
        except JiraError as exc:
            run = self.store.load(run.run_id)
            status = "partial" if run.jira.comment_id else "failed"
            return self._save(run, status=status, detail=str(exc))
        return self._save(run, status="published", detail="", block_confirmed=True)

    async def _attachment(self, run: TestRun, artifact: Artifact) -> TestRun:
        def verify(items: list[dict[str, object]]) -> str | None:
            matches = [item for item in items if item.get("filename") == artifact.filename]
            if matches and any(str(item.get("size")) != str(artifact.size) for item in matches):
                raise JiraError("A Jira attachment has the expected filename but a different size.")
            return _matching_id(matches)

        filename = artifact.filename
        attachment_id = verify(await self.gateway.attachments(run.issue_key))
        if attachment_id is None:
            self._require_first_write(run, filename)
            try:
                _, path = self.store.artifact(run.run_id, filename)
            except PreflightError as exc:
                raise JiraError("The local attachment is unavailable.", rejected=True) from exc
            run = self._save(run, uncertain_writes=(*run.jira.uncertain_writes, filename))
            try:
                await self.gateway.upload(run.issue_key, path)
            except JiraError as exc:
                if exc.rejected:
                    self._clear_intent(run, filename)
                raise
            attachment_id = verify(await self.gateway.attachments(run.issue_key))
            if attachment_id is None:
                raise JiraError("The uploaded attachment is not yet visible in Jira.")
        return self._save(
            run,
            attachments={**run.jira.attachments, filename: attachment_id},
            uncertain_writes=tuple(item for item in run.jira.uncertain_writes if item != filename),
        )

    async def _comment(self, run: TestRun) -> TestRun:
        marker = f"Lumon Auto Test run {run.run_id}"
        body = comment_body(run, marker)
        existing, matched = await self._read_comment(run, marker, body)
        if not matched:
            if existing is None:
                self._require_first_write(run, "comment")
                run = self._save(run, uncertain_writes=(*run.jira.uncertain_writes, "comment"))
            try:
                await self.gateway.write_comment(run.issue_key, body, existing)
            except JiraError as exc:
                if exc.rejected and existing is None:
                    self._clear_intent(run, "comment")
                raise
            existing, matched = await self._read_comment(run, marker, body)
        if existing is None or not matched:
            raise JiraError("The Auto Test comment could not be verified in Jira.")
        return self._save(
            run,
            comment_id=existing,
            uncertain_writes=tuple(item for item in run.jira.uncertain_writes if item != "comment"),
        )

    async def _read_comment(self, run: TestRun, marker: str, body: str) -> tuple[str | None, bool]:
        matches = [
            item
            for item in await self.gateway.comments(run.issue_key)
            if marker in _text(item.get("body"))
        ]
        comment_id = _matching_id(matches)
        if run.jira.comment_id is not None and comment_id != run.jira.comment_id:
            raise JiraError("The previously published Jira comment could not be verified.")
        # Jira renders Markdown as ADF. Compare a version marker that survives rendering.
        return comment_id, bool(matches and body.splitlines()[-1] in _text(matches[0].get("body")))

    def _clear_intent(self, run: TestRun, item: str) -> None:
        self._save(
            run,
            uncertain_writes=tuple(
                pending for pending in run.jira.uncertain_writes if pending != item
            ),
        )

    def _save(self, run: TestRun, **changes: object) -> TestRun:
        updated = run.model_copy(update={"jira": run.jira.model_copy(update=changes)})
        self.store.save(updated)
        return updated

    @staticmethod
    def _require_first_write(run: TestRun, item: str) -> None:
        if item in run.jira.uncertain_writes or item in run.jira.attachments:
            raise JiraError(
                "A prior Jira write is unconfirmed. Check the original issue before retrying."
            )


def comment_body(run: TestRun, marker: str) -> str:
    from hashlib import sha256

    if run.plan and run.plan.questions:
        lines = [f"## {marker}", "", "**Questions blocking Auto Test**", ""]
        lines.extend(
            f"{number}. {item.question}" for number, item in enumerate(run.plan.questions, 1)
        )
        lines.extend(("", "Please answer here, then return the issue to the Auto Test queue."))
        body = sanitize_output("\n".join(lines))
        return f"{body}\n\nAuto Test revision {sha256(body.encode()).hexdigest()[:20]}"
    counts = ", ".join(
        f"{status}: {sum(item.status == status for item in run.results)}"
        for status in ("passed", "failed", "review", "blocked", "not_run")
    )
    lines = [f"## {marker}", "", f"**Results** · {counts}"]
    if run.detail:
        lines.append(f"**Run detail** · {run.detail}")
    lines.extend(("", "**Evidence**"))
    for artifact in run.artifacts:
        if artifact.kind == "screenshot":
            continue
        filename = artifact.filename
        attachment_id = run.jira.attachments.get(filename)
        if attachment_id and attachment_id.isdecimal():
            url = f"https://{run.jira_site}/rest/api/3/attachment/content/{attachment_id}"
            lines.append(f"- [{filename}]({url})")
        elif attachment_id:
            lines.append(f"- {filename} · Jira attachment")
        else:
            lines.append(f"- {filename} · available in Lumon")
    if run.plan:
        if run.plan.gaps:
            lines.extend(("", "**Coverage gaps**", *(f"- {gap}" for gap in run.plan.gaps[:5])))
        for case, result in zip(run.plan.cases, run.results, strict=True):
            if result.status not in {"failed", "blocked", "review"}:
                continue
            lines.extend(
                (
                    "",
                    f"**{result.case_id} · {result.status}** · {case.title}",
                    f"Expected: {case.expected}",
                    f"Observed: {result.detail}",
                )
            )
            if result.status == "failed":
                lines.extend(f"{i}. {step.action}" for i, step in enumerate(case.steps, 1))
    body = sanitize_output("\n".join(lines))
    if len(body) > 24000:
        body = f"{body[:23000]}\n\nSee the workbook for the remaining results and evidence."
    return f"{body}\n\nAuto Test revision {sha256(body.encode()).hexdigest()[:20]}"
