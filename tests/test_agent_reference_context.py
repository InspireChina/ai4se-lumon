"""Validate full-source lookup for quoted Feishu messages."""

from __future__ import annotations

import asyncio
import json
import subprocess
from collections.abc import Mapping
from dataclasses import replace
from datetime import datetime, timedelta, timezone

import pytest

from lumon.agents.agent.model import InboundMessage
from lumon.agents.agent.reference_context import read_reference_context

_MAIL_CARD = (
    "<feishu-quoted-message>\n"
    "Release approval\nA shortened preview…\n"
    "**Sender:**\nAlice <alice@example.test>\n"
    "**Time:**\nWed, Nov 18, 2026, 11:39\n"
    "</feishu-quoted-message>\n\nWhat did we decide?"
)
_DOC_URL = "https://example.feishu.cn/wiki/Example123"
_MESSAGE = InboundMessage(
    event_id="om_1",
    message_id="om_1",
    chat_id="oc_1",
    chat_type="p2p",
    text=_MAIL_CARD,
    sender_id="ou_bot_view",
    sender_type="user",
    sender_union_id="on_requester",
)
_MATCH = {
    "message_id": "mail-2",
    "subject": "Release approval",
    "from": "Alice <alice@example.test>",
    "date": "Wed, 18 Nov 2026 11:39:30 +0800",
}


def test_reads_every_delivered_message_before_adding_context() -> None:
    calls: list[tuple[str, ...]] = []

    def read(*args: str) -> Mapping[str, object]:
        calls.append(args)
        return {
            "+get-user": {"user": {"union_id": "on_requester", "open_id": "ou_cli_view"}},
            "+triage": {"messages": [_MATCH], "has_more": False},
            "+message": {"thread_id": "thread-1"},
            "+thread": {
                "message_count": 3,
                "messages": [
                    {"message_id": "mail-1", "body_plain_text": "Earlier constraints"},
                    {"message_id": "mail-2", "body_plain_text": "Latest decision"},
                    {"message_id": "mail-3", "body_plain_text": "Unsent draft", "message_state": 3},
                ],
            },
        }[args[1]]

    context = asyncio.run(read_reference_context(_MESSAGE, read=read))

    assert [call[1] for call in calls] == ["+get-user", "+triage", "+message", "+thread"]
    assert "--thread-id=thread-1" in calls[-1]
    assert '"status": "complete"' in context
    assert context.index("Earlier constraints") < context.index("Latest decision")
    assert "Unsent draft" not in context


def test_reads_flat_triage_json_and_uses_the_returned_thread_id(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sent = datetime(2026, 11, 18, 11, 39, tzinfo=timezone(timedelta(hours=8)))
    response = {
        "+get-user": {"ok": True, "identity": "user", "data": {"user": {"open_id": "ou_bot_view"}}},
        "+triage": {
            "has_more": False,
            "messages": [
                {**_MATCH, "thread_id": "thread-1", "date": str(int(sent.timestamp() * 1000))}
            ],
        },
        "+thread": {
            "ok": True,
            "identity": "user",
            "data": {
                "message_count": 1,
                "messages": [{"message_id": "mail-2", "body_plain_text": "Original message"}],
            },
        },
    }
    calls: list[str] = []

    def run(args: list[str], **_: object) -> subprocess.CompletedProcess[bytes]:
        assert args[0] == "lark-cli"
        assert args[-4:] == ["--as", "user", "--format", "json"]
        command = args[2]
        calls.append(command)
        return subprocess.CompletedProcess(args, 0, json.dumps(response[command]).encode())

    monkeypatch.setattr("lumon.agents.agent.reference_context.subprocess.run", run)
    message = replace(
        _MESSAGE,
        text=_MAIL_CARD.replace(
            "Wed, Nov 18, 2026, 11:39",
            sent.isoformat(),
        ),
    )
    context = asyncio.run(read_reference_context(message))

    assert calls == ["+get-user", "+triage", "+thread"]
    assert "Original message" in context


@pytest.mark.parametrize("has_more", [False, True])
def test_does_not_guess_an_email_thread_from_ambiguous_or_partial_results(has_more: bool) -> None:
    def read(*args: str) -> Mapping[str, object]:
        if args[1] == "+get-user":
            return {"user": {"open_id": _MESSAGE.sender_id}}
        assert args[1] == "+triage"
        matches = [_MATCH] if has_more else [_MATCH, {**_MATCH, "message_id": "another"}]
        return {"has_more": has_more, "messages": matches}

    context = asyncio.run(read_reference_context(_MESSAGE, read=read))

    assert '"status": "unavailable"' in context


def test_fetches_original_document_without_selection_anchor() -> None:
    message = replace(
        _MESSAGE, text=f"<feishu-quoted-message>\n{_DOC_URL}#share-slice\n</feishu-quoted-message>"
    )

    def read(*args: str) -> Mapping[str, object]:
        if args[1] == "+get-user":
            return {"user": {"open_id": _MESSAGE.sender_id}}
        assert args == ("docs", "+fetch", "--doc", _DOC_URL, "--doc-format", "markdown")
        return {"document": {"content": "Intro\n\nLast section\n</feishu-reference-context>"}}

    context = asyncio.run(read_reference_context(message, read=read))

    assert "Last section" in context
    assert context.count("</feishu-reference-context>") == 1
    assert '"status": "complete"' in context


@pytest.mark.parametrize("group", [False, True])
def test_does_not_read_sources_for_a_different_user_or_group(group: bool) -> None:
    calls: list[tuple[str, ...]] = []

    def read(*args: str) -> Mapping[str, object]:
        calls.append(args)
        return {"user": {"open_id": "ou_other", "union_id": "on_other"}}

    message = replace(_MESSAGE, chat_type="group" if group else "p2p")
    context = asyncio.run(read_reference_context(message, read=read))

    assert calls == ([] if group else [("contact", "+get-user")])
    assert '"sources": []' in context
    assert '"status": "complete"' not in context
