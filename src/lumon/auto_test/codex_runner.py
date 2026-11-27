"""Run one bounded Auto Test turn through the signed-in Codex CLI."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import cast

from pydantic import ValidationError

from lumon.agents.agent.config import AgentConfig
from lumon.agents.agent.runner import agent_codex_environment
from lumon.auto_test.process import build_environment
from lumon.errors import PreflightError
from lumon.tools.codex import CodexTool
from lumon.tools.codex_rpc import CodexRpc, CodexRpcError
from lumon.version import __version__

# Jira and screenshots enter the model as data. Only the host tool may act on them.
_CONFIG = (
    "features.shell_tool=false",
    "features.apps=false",
    "features.plugins=false",
    "features.multi_agent=false",
    "features.multi_agent_v2=false",
    "features.image_generation=false",
    "features.code_mode=false",
    "features.skill_mcp_dependency_install=false",
    'web_search="disabled"',
)


@dataclass(frozen=True, slots=True)
class AutoTestTool:
    name: str
    description: str
    input_schema: dict[str, object]
    handle: Callable[[object], Awaitable[dict[str, str]]]
    max_calls: int


class CodexTestRunner:
    def __init__(self, cli: CodexTool) -> None:
        self.cli = cli

    async def require_ready(self) -> None:
        if not self.cli.is_available():
            raise PreflightError("Install the Codex CLI on the Auto Test worker.")
        if not await asyncio.to_thread(self.cli.is_authenticated):
            raise PreflightError("Sign in to the Lumon Agent with `lumon agent codex-login`.")

    async def turn(
        self,
        prompt: str,
        tool: AutoTestTool,
        output_schema: dict[str, object],
        *,
        instructions: str,
        timeout: float,
        screenshot: bytes | None = None,
    ) -> str:
        rpc = CodexRpc(allow_tool_calls=True)
        with TemporaryDirectory(prefix="lumon-auto-test-codex-") as directory:
            scratch = Path(directory).resolve()
            inputs: list[dict[str, str]] = [{"type": "text", "text": prompt}]
            if screenshot is not None:
                image = scratch / "screen.png"
                image.write_bytes(screenshot)
                inputs.append({"type": "localImage", "path": str(image)})
            try:
                async with asyncio.timeout(timeout):
                    await rpc.start(self.cli.binary, scratch, self.cli.environment, config=_CONFIG)
                    thread_id = await self._start_thread(rpc, scratch, tool, instructions)
                    started = await rpc.request(
                        "turn/start",
                        {
                            "threadId": thread_id,
                            "input": inputs,
                            "effort": self.cli.reasoning_effort,
                            "outputSchema": _strict_output_schema(output_schema),
                        },
                    )
                    turn_id = _id(started.get("turn"))
                    return await _finish_turn(rpc, thread_id, turn_id, tool)
            except TimeoutError as exc:
                raise PreflightError("Codex timed out during Auto Test.") from exc
            except ValidationError as exc:
                raise PreflightError("Codex returned invalid Auto Test tool input.") from exc
            except (CodexRpcError, OSError) as exc:
                raise PreflightError(
                    "Codex CLI could not complete Auto Test. Check the Agent login and CLI version."
                ) from exc
            finally:
                await rpc.close()

    async def _start_thread(
        self, rpc: CodexRpc, scratch: Path, tool: AutoTestTool, instructions: str
    ) -> str:
        await rpc.request(
            "initialize",
            {
                "clientInfo": {"name": "lumon", "title": "Lumon", "version": __version__},
                "capabilities": {"experimentalApi": True},
            },
        )
        await rpc.send({"method": "initialized", "params": {}})
        settings = await rpc.request("config/read", {"cwd": str(scratch)})
        config = _fields(settings.get("config"))
        if not config:
            raise CodexRpcError()
        # Codex merges config tables, so each inherited server must be disabled by name.
        servers = _fields(config.get("mcp_servers"))
        started = await rpc.request(
            "thread/start",
            {
                "cwd": str(scratch),
                "model": self.cli.model,
                "approvalPolicy": "never",
                "sandbox": "read-only",
                "developerInstructions": instructions,
                "config": {"mcp_servers": {name: {"enabled": False} for name in servers}},
                "environments": [],
                "ephemeral": True,
                "dynamicTools": [
                    {
                        "type": "function",
                        "name": tool.name,
                        "description": tool.description,
                        "inputSchema": tool.input_schema,
                    }
                ],
            },
        )
        return _id(started.get("thread"))


def agent_test_runner(config: AgentConfig, codex_home: Path) -> CodexTestRunner:
    agent_environment = agent_codex_environment(codex_home)
    environment = build_environment()
    for key in ("CODEX_HOME", "CODEX_SQLITE_HOME"):
        environment[key] = agent_environment[key]
    return CodexTestRunner(
        CodexTool(
            environment=environment,
            model=config.agent_model,
            reasoning_effort=config.agent_reasoning_effort,
        )
    )


async def _finish_turn(rpc: CodexRpc, thread_id: str, turn_id: str, tool: AutoTestTool) -> str:
    calls: set[str] = set()
    final_text = ""
    while True:
        message = await rpc.notifications.get()
        method = message.get("method")
        if method == "lumon/closed":
            raise CodexRpcError()
        params = _fields(message.get("params"))
        if method == "item/tool/call":
            await _handle_call(rpc, message.get("id"), params, thread_id, turn_id, tool, calls)
            continue
        if params.get("threadId") != thread_id:
            continue
        if method == "item/completed" and params.get("turnId") == turn_id:
            item = _fields(params.get("item"))
            text = item.get("text")
            if item.get("type") == "agentMessage" and isinstance(text, str):
                final_text = text
        if method == "turn/completed":
            turn = _fields(params.get("turn"))
            if turn.get("id") != turn_id:
                continue
            if turn.get("status") != "completed" or not final_text or len(final_text) > 1024 * 1024:
                raise PreflightError("Codex did not return a complete Auto Test result.")
            return final_text


async def _handle_call(
    rpc: CodexRpc,
    request_id: object,
    params: dict[str, object],
    thread_id: str,
    turn_id: str,
    tool: AutoTestTool,
    calls: set[str],
) -> None:
    call_id = params.get("callId")
    if (
        not isinstance(request_id, (str, int))
        or not isinstance(call_id, str)
        or not call_id
        or call_id in calls
        or params.get("threadId") != thread_id
        or params.get("turnId") != turn_id
        or params.get("tool") != tool.name
        or params.get("namespace") is not None
    ):
        raise PreflightError("Codex sent an unexpected Auto Test tool request.")
    if len(calls) >= tool.max_calls:
        raise PreflightError("Codex exceeded the Auto Test tool call limit.")
    calls.add(call_id)
    reply = await tool.handle(params.get("arguments"))
    await rpc.send({"id": request_id, "result": {"success": True, "contentItems": [reply]}})


def _fields(value: object) -> dict[str, object]:
    return cast(dict[str, object], value) if isinstance(value, dict) else {}


def _strict_output_schema(schema: dict[str, object]) -> dict[str, object]:
    """Require every generated field while keeping defaults for stored records."""

    strict = dict(schema)
    strict.pop("default", None)
    for key in ("properties", "$defs"):
        if key in schema:
            children = _fields(schema[key])
            strict[key] = {
                name: _strict_output_schema(_fields(child)) for name, child in children.items()
            }
            if key == "properties":
                strict["required"] = list(children)
    for key in ("anyOf", "oneOf", "allOf", "prefixItems"):
        variants = schema.get(key)
        if isinstance(variants, list):
            strict[key] = [
                _strict_output_schema(_fields(item)) for item in cast(list[object], variants)
            ]
    if isinstance(schema.get("items"), dict):
        strict["items"] = _strict_output_schema(_fields(schema["items"]))
    return strict


def _id(value: object) -> str:
    identifier = _fields(value).get("id")
    if not isinstance(identifier, str) or not identifier:
        raise CodexRpcError()
    return identifier
