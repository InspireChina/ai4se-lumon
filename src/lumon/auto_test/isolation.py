"""Keep builds and the browser off the worker's network."""

from __future__ import annotations

import shlex
import shutil
import sys
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from importlib.metadata import version
from pathlib import Path
from uuid import uuid4

from playwright.async_api import Browser, BrowserType
from playwright.async_api import Error as PlaywrightError

from lumon.auto_test.process import build_environment, command
from lumon.errors import PreflightError

_OFFLINE_PROFILE = "(version 1) (allow default) (deny network*)"

# Pass the Playwright debugging pipe through Docker stdin and stdout. Chromium
# uses a writable profile inside the container and cannot read the worker's files.
_CHROMIUM = """
set -eu
browsers=(/ms-playwright/chromium-*/chrome-linux*/chrome)
test "${#browsers[@]}" = 1 && test -x "${browsers[0]}" || exit 1
args=()
for arg in "$@"; do
    case "$arg" in
        --user-data-dir=*) args+=(--user-data-dir=/tmp/chromium) ;;
        *) args+=("$arg") ;;
    esac
done
exec "${browsers[0]}" "${args[@]}" 3<&0 4>&1 </dev/null >&2
"""


def offline_build(arguments: tuple[str, ...]) -> tuple[str, ...]:
    if sys.platform == "darwin" and Path("/usr/bin/sandbox-exec").is_file():
        return ("/usr/bin/sandbox-exec", "-p", _OFFLINE_PROFILE, *arguments)
    if sys.platform == "linux" and (unshare := shutil.which("unshare")):
        return (unshare, "--user", "--map-current-user", "--net", "--", *arguments)
    raise PreflightError("This worker cannot disable network access for Auto Test builds.")


def chromium_image() -> str:
    major, minor, _ = version("playwright").split(".", maxsplit=2)
    return f"mcr.microsoft.com/playwright:v{major}.{minor}.0-noble"


@asynccontextmanager
async def isolated_chromium(chromium: BrowserType, directory: Path) -> AsyncGenerator[Browser]:
    docker = shutil.which("docker")
    if docker is None:
        raise PreflightError("Install Docker to run Auto Test Chromium with networking disabled.")
    image = chromium_image()
    try:
        await command((docker, "image", "inspect", image), label="Check Auto Test Chromium image")
    except PreflightError as exc:
        raise PreflightError(
            f"Start Docker and prepare the local Auto Test image {image}."
        ) from exc

    name = f"lumon-auto-test-{uuid4().hex}"
    arguments = (
        docker,
        "run",
        "--name",
        name,
        "--rm",
        "--interactive",
        "--pull=never",
        "--network=none",
        "--read-only",
        "--tmpfs",
        "/tmp:rw,nosuid,nodev",
        "--cap-drop=ALL",
        "--security-opt=no-new-privileges",
        "--user=pwuser",
        "--env=HOME=/tmp",
        "--entrypoint=/bin/bash",
        image,
        "-c",
        _CHROMIUM,
        "chromium",
    )
    launcher = directory / "isolated-chromium"
    launcher.write_text(f'#!/bin/sh\nexec {shlex.join(arguments)} "$@" 0<&3 1>&4\n')
    launcher.chmod(0o700)
    environment: dict[str, str | float | bool] = {**build_environment()}
    try:
        try:
            # The container supplies the process and network boundaries on macOS and Linux.
            browser = await chromium.launch(
                executable_path=launcher, env=environment, chromium_sandbox=False
            )
        except PlaywrightError as exc:
            raise PreflightError(
                "Could not start Auto Test Chromium in the isolated container."
            ) from exc
        async with browser:
            yield browser
    finally:
        # Also clean up if the Docker client exited before the container.
        try:
            await command((docker, "rm", "--force", name), label="Clean up Auto Test Chromium")
        except PreflightError:
            pass
