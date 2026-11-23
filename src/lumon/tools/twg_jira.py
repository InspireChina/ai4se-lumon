"""Bounded TWG reads and a check for one Jira project."""

from __future__ import annotations

import asyncio
import os
from collections.abc import Mapping
from dataclasses import dataclass

from pydantic import BaseModel, Field, ValidationError

from lumon.workspace.settings import JiraSettings

_AUTH_ERRORS = {"AUTH_INVALID", "AUTH_REQUIRED", "AUTH_MISSING", "AUTH_EXPIRED"}


@dataclass(frozen=True, slots=True)
class JiraConnectionResult:
    success: bool
    detail: str


class _Project(BaseModel):
    key: str


class _Error(BaseModel):
    code: str = ""


class _ProjectReply(BaseModel):
    data: list[_Project] | None = None
    error: _Error | None = None
    errors: list[object] = Field(default_factory=list[object])
    ok: bool | None = None


async def verify_jira_connection(settings: JiraSettings) -> JiraConnectionResult:
    try:
        async with asyncio.timeout(15):
            code, payload = await read_twg_jira(
                "twg",
                "space",
                "get",
                settings.project,
                site=settings.site,
                environment=dict(os.environ, TWG_AGENT_DEFAULTS="1"),
            )
    except FileNotFoundError:
        return JiraConnectionResult(False, "TWG is not available on the Dashboard PATH.")
    except TimeoutError:
        return JiraConnectionResult(False, "The Jira connection check timed out.")
    except (OSError, ValueError):
        return JiraConnectionResult(False, "TWG could not complete the Jira connection check.")
    try:
        reply = _ProjectReply.model_validate_json(payload)
    except ValidationError:
        return JiraConnectionResult(False, "TWG returned an invalid Jira response.")
    if reply.error and reply.error.code in _AUTH_ERRORS:
        return JiraConnectionResult(
            False, f"TWG Jira authentication needs attention ({reply.error.code})."
        )
    if (
        code
        or reply.error
        or reply.errors
        or reply.ok is False
        or [item.key for item in reply.data or ()] != [settings.project]
    ):
        return JiraConnectionResult(
            False,
            f"Cannot read Jira project {settings.project} on {settings.site}. "
            "Check the project key and TWG access.",
        )
    return JiraConnectionResult(
        True, f"Connected to Jira project {settings.project} on {settings.site}."
    )


async def read_twg_jira(
    binary: str,
    namespace: str,
    *arguments: str,
    site: str,
    environment: Mapping[str, str] | None = None,
) -> tuple[int, bytes]:
    process = await asyncio.create_subprocess_exec(
        binary,
        "jira",
        namespace,
        *arguments,
        "--site",
        site,
        "--output",
        "json",
        "--output-summary",
        "none",
        stdin=asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.DEVNULL,
        env=environment,
    )
    try:
        assert process.stdout is not None
        payload = bytearray()
        while chunk := await process.stdout.read(64 * 1024):
            payload.extend(chunk)
            if len(payload) > 1024 * 1024:
                raise ValueError("Jira response is too large.")
        return await process.wait(), bytes(payload)
    finally:
        if process.returncode is None:
            try:
                process.kill()
            except ProcessLookupError:
                pass
        await process.wait()
