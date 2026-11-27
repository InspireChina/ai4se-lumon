"""Bounded JSON-RPC transport to a private local Codex app-server process."""

from __future__ import annotations

import asyncio
import json
import os
import re
import signal
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import cast

from lumon.tools.codex import is_missing_session_error


class CodexRpcError(Exception):
    """Provider error; deliberately excludes arbitrary server text and credentials."""

    def __init__(
        self,
        code: int | None = None,
        *,
        thread_busy: bool = False,
        session_unavailable: bool = False,
    ) -> None:
        super().__init__("Codex app-server request failed.")
        self.code = code
        self.thread_busy = thread_busy
        self.session_unavailable = session_unavailable
        self.method: str | None = None


class CodexRpc:
    def __init__(self, *, allow_tool_calls: bool = False) -> None:
        self.notifications: asyncio.Queue[dict[str, object]] = asyncio.Queue(maxsize=512)
        self._allow_tool_calls = allow_tool_calls
        self.process: asyncio.subprocess.Process | None = None
        self._reader: asyncio.Task[None] | None = None
        self._stderr: asyncio.Task[None] | None = None
        self._pending: dict[int, tuple[asyncio.Future[dict[str, object]], str | None]] = {}
        self._sequence = 0

    async def start(
        self,
        binary: str,
        workspace: Path,
        environment: Mapping[str, str] | None,
        *,
        config: Sequence[str] = (),
    ) -> None:
        self.process = await asyncio.create_subprocess_exec(
            binary,
            "app-server",
            *(part for item in config for part in ("--config", item)),
            "--listen",
            "stdio://",
            cwd=workspace,
            env=environment,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            limit=16 * 1024 * 1024,
            start_new_session=os.name == "posix",
        )
        self._reader = asyncio.create_task(self._read())
        self._stderr = asyncio.create_task(self._drain_stderr())

    async def send(self, payload: dict[str, object]) -> None:
        process = self.process
        if process is None or process.stdin is None or process.returncode is not None:
            raise CodexRpcError()
        process.stdin.write((json.dumps(payload) + "\n").encode())
        await process.stdin.drain()

    async def request(
        self, method: str, params: dict[str, object], *, timeout: float = 30
    ) -> dict[str, object]:
        self._sequence += 1
        request_id = self._sequence
        future: asyncio.Future[dict[str, object]] = asyncio.get_running_loop().create_future()
        resume_thread = params.get("threadId") if method == "thread/resume" else None
        self._pending[request_id] = (
            future,
            resume_thread if isinstance(resume_thread, str) else None,
        )
        try:
            await self.send({"id": request_id, "method": method, "params": params})
            return await asyncio.wait_for(future, timeout)
        except CodexRpcError as exc:
            exc.method = method
            raise
        finally:
            self._pending.pop(request_id, None)

    async def _read(self) -> None:
        assert self.process is not None and self.process.stdout is not None
        try:
            while line := await self.process.stdout.readline():
                payload: object = json.loads(line)
                if not isinstance(payload, dict):
                    raise CodexRpcError()
                message = cast(dict[str, object], payload)
                if "method" in message:
                    await self._receive_method(message)
                else:
                    self._receive_reply(message)
        except (ValueError, OSError, CodexRpcError):
            pass  # The process boundary reports a safe failure, never raw protocol text.
        finally:
            for future, _ in self._pending.values():
                if not future.done():
                    future.set_exception(CodexRpcError())
            # Wake the event consumer even when the provider closes unexpectedly.
            if self.notifications.full():
                self.notifications.get_nowait()
            self.notifications.put_nowait({"method": "lumon/closed"})

    async def _receive_method(self, message: dict[str, object]) -> None:
        tool_call = self._allow_tool_calls and message.get("method") == "item/tool/call"
        if "id" in message and not tool_call:
            # Full-access operation needs no approvals. Never auto-approve new RPC types.
            await self.send(
                {
                    "id": message["id"],
                    "error": {
                        "code": -32601,
                        "message": "Unsupported client request; not approved.",
                    },
                }
            )
            return
        try:
            self.notifications.put_nowait(message)
        except asyncio.QueueFull as exc:
            raise CodexRpcError() from exc

    def _receive_reply(self, message: dict[str, object]) -> None:
        request_id = message.get("id")
        pending = self._pending.get(request_id) if isinstance(request_id, int) else None
        if pending is None:
            return
        future, resume_thread = pending
        if future.done():
            return
        result = message.get("result")
        if "error" in message or not isinstance(result, dict):
            error = message.get("error")
            fields = cast(dict[str, object], error) if isinstance(error, dict) else {}
            code, detail = fields.get("code"), fields.get("message")
            thread_busy = (
                code == -32600
                and isinstance(detail, str)
                and re.fullmatch(
                    r"thread [A-Za-z0-9_-]{1,128} already has an active writer", detail
                )
                is not None
            )
            future.set_exception(
                CodexRpcError(
                    code if isinstance(code, int) else None,
                    thread_busy=thread_busy,
                    session_unavailable=(
                        resume_thread is not None
                        and code == -32600
                        and isinstance(detail, str)
                        and is_missing_session_error(detail, resume_thread)
                    ),
                )
            )
        else:
            future.set_result(cast(dict[str, object], result))

    async def _drain_stderr(self) -> None:
        assert self.process is not None and self.process.stderr is not None
        while await self.process.stderr.read(65536):
            pass

    async def close(self, *, kill_tools: bool = True) -> None:
        process = self.process
        if process is not None:
            # A fresh process group belongs exclusively to this execution, including tools.
            try:
                if kill_tools and os.name == "posix":
                    os.killpg(process.pid, signal.SIGKILL)
                elif process.returncode is None:
                    process.kill()
            except ProcessLookupError:
                pass
            await process.wait()
        tasks = [task for task in (self._reader, self._stderr) if task is not None]
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
