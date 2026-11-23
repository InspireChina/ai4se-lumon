"""Run test-owned subprocesses with bounded output and cancellation."""

from __future__ import annotations

import asyncio
import os
import signal
from collections.abc import AsyncGenerator, Mapping, Sequence
from contextlib import asynccontextmanager
from pathlib import Path

from lumon.errors import PreflightError


def build_environment() -> dict[str, str]:
    # Builds may execute repository scripts. Pass the toolchain environment, not API credentials.
    keys = {
        "PATH",
        "HOME",
        "USER",
        "LOGNAME",
        "LANG",
        "LC_ALL",
        "TMPDIR",
        "DEVELOPER_DIR",
        "JAVA_HOME",
        "NVM_BIN",
        "SDKROOT",
        "SYSTEMROOT",
    }
    return {key: value for key, value in os.environ.items() if key in keys}


async def terminate(process: asyncio.subprocess.Process, sig: signal.Signals) -> None:
    try:
        os.killpg(process.pid, sig)
    except ProcessLookupError:
        return
    try:
        await asyncio.wait_for(process.wait(), timeout=5)
    except TimeoutError:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        await process.wait()


@asynccontextmanager
async def owned_process(
    argv: Sequence[str],
    *,
    cwd: Path | None = None,
    read: bool = False,
    stop_signal: signal.Signals = signal.SIGTERM,
    environment: Mapping[str, str] | None = None,
) -> AsyncGenerator[asyncio.subprocess.Process]:
    try:
        process = await asyncio.create_subprocess_exec(
            *argv,
            cwd=cwd,
            env=environment,
            start_new_session=True,
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE if read else asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
        )
    except OSError as exc:
        raise PreflightError("Could not start a configured Auto Test tool.") from exc
    try:
        yield process
    finally:
        await terminate(process, stop_signal)


async def command(
    argv: Sequence[str],
    *,
    label: str,
    cwd: Path | None = None,
    read: bool = False,
    timeout: float = 60,
    max_output: int = 1024 * 1024,
) -> bytes:
    try:
        async with (
            asyncio.timeout(timeout),
            owned_process(
                argv,
                cwd=cwd,
                read=read,
                environment=build_environment(),
            ) as process,
        ):
            output = bytearray()
            if process.stdout is not None:
                while chunk := await process.stdout.read(65536):
                    output.extend(chunk)
                    if len(output) > max_output:
                        raise PreflightError(f"{label} returned too much output.")
            code = await process.wait()
            if code != 0:
                raise PreflightError(f"{label} failed (exit {code}).")
            return bytes(output)
    except TimeoutError as exc:
        raise PreflightError(f"{label} timed out.") from exc
