"""Build and run isolated Chromium and iOS acceptance-test targets."""

from __future__ import annotations

import asyncio
import plistlib
import shutil
import signal
import socket
import time
from collections.abc import AsyncGenerator
from contextlib import AsyncExitStack, asynccontextmanager
from dataclasses import dataclass
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from threading import Thread
from typing import cast
from urllib.parse import urljoin, urlsplit
from uuid import UUID

import httpx
from playwright.async_api import Browser, Page, Route, WebSocketRoute, async_playwright
from playwright.async_api import Error as PlaywrightError

from lumon.auto_test.config import (
    AutoTestSettings,
    IosTarget,
    Platform,
    TestEnvironment,
    WebTarget,
    https_origin,
)
from lumon.auto_test.drivers import IosScreen, Screen, WebScreen, origin
from lumon.auto_test.isolation import isolated_chromium, offline_build
from lumon.auto_test.process import build_environment, command, owned_process
from lumon.auto_test.sources import Sources
from lumon.auto_test.targets import IosChoice, WebChoice, ios_target, web_environment, web_target
from lumon.errors import PreflightError


@dataclass(frozen=True, slots=True)
class Recording:
    screen: Screen
    path: Path
    started: float


@dataclass(frozen=True, slots=True)
class WebBuild:
    profile: WebTarget
    browser: Browser
    origin: str
    approved_origins: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class IosBuild:
    profile: IosTarget
    app: Path
    udid: str
    appium: httpx.AsyncClient


class TestRuntime:
    def __init__(
        self, settings: AutoTestSettings, sources: Sources, directory: Path, stack: AsyncExitStack
    ) -> None:
        self.settings = settings
        self.sources = sources
        self.directory = directory
        self.stack = stack
        self.web: WebBuild | None = None
        self.ios: IosBuild | None = None

    async def build(
        self, platform: Platform, *, choice: WebChoice | IosChoice | None = None
    ) -> str:
        _require_isolated_platform(platform)
        target = self.settings.web if platform == "web" else self.settings.ios
        selection = target or choice
        if selection is None:
            raise PreflightError(f"The Agent did not find a supported {platform} build target.")
        source = await self.sources.pin(selection.repository)
        if target is None and platform == "web" and isinstance(choice, WebChoice):
            target = web_target(choice, source.path)
        if target is None and platform == "ios" and isinstance(choice, IosChoice):
            target = await ios_target(choice, source.path)
        if target is None:
            raise PreflightError(f"The selected build target does not match {platform}.")
        if isinstance(target, WebTarget):
            environment = web_environment(target)
            approved = self.settings.qa_environments.get(environment, ())
            if environment in {"staging", "uat"} and not approved:
                raise PreflightError(
                    f"No HTTPS origins are approved for the {environment} environment."
                )
            target = target.model_copy(update={"environment": environment})
        checkout = await source.copy_for_build(self.directory / platform)
        directory = (
            _inside(checkout, target.directory) if isinstance(target, WebTarget) else checkout
        )
        for number, arguments in enumerate(target.build, 1):
            await command(
                offline_build(arguments),
                label=f"{platform} offline build step {number}",
                cwd=directory,
                timeout=1800,
            )
        if isinstance(target, WebTarget):
            return await self._build_web(target, directory, source.revision.sha)
        return await self._build_ios(target, checkout, source.revision.sha)

    async def _build_web(self, target: WebTarget, checkout: Path, sha: str) -> str:
        environment: TestEnvironment = web_environment(target)
        output = _inside(checkout, target.output)
        if not (output / "index.html").is_file():
            raise PreflightError("The Web build did not produce an index.html file.")
        address = await self.stack.enter_async_context(_serve(output, target.base_path))
        playwright = await self.stack.enter_async_context(async_playwright())
        browser = await self.stack.enter_async_context(
            isolated_chromium(playwright.chromium, self.directory)
        )
        approved = self.settings.qa_environments.get(environment, ())
        self.web = WebBuild(target, browser, address, approved)
        return f"{target.repository}@{sha} · {environment} · Chromium {browser.version}"

    async def _build_ios(self, target: IosTarget, checkout: Path, sha: str) -> str:
        if shutil.which("appium") is None:
            raise PreflightError("Install Appium and its XCUITest driver on the Auto Test worker.")
        project = _inside(checkout, target.project)
        if not project.is_dir():
            raise PreflightError("The configured Xcode project is unavailable after preparation.")
        udid = await self.stack.enter_async_context(_simulator(target))
        products = self.directory / "derived-data"
        project_flag = "-workspace" if project.suffix == ".xcworkspace" else "-project"
        await command(
            (
                "xcodebuild",
                project_flag,
                str(project),
                "-scheme",
                target.scheme,
                "-configuration",
                target.configuration,
                "-sdk",
                "iphonesimulator",
                "-destination",
                f"platform=iOS Simulator,id={udid}",
                "-derivedDataPath",
                str(products),
                "CODE_SIGNING_ALLOWED=NO",
                "build",
            ),
            label="Xcode simulator build",
            cwd=checkout,
            timeout=1800,
        )
        app = _inside(
            products, f"Build/Products/{target.configuration}-iphonesimulator/{target.app}"
        )
        version = _verify_app(app, target.bundle_id)
        appium = await self.stack.enter_async_context(_appium())
        self.ios = IosBuild(target, app, udid, appium)
        return f"{target.repository}@{sha} · {target.bundle_id} {version} · {target.runtime}"

    async def verify_backends(self, repositories: tuple[str, ...]) -> None:
        if not repositories:
            return
        approved = self.web.approved_origins if self.web is not None else ()
        probes = {backend.repository: backend.version_url for backend in self.settings.backends}
        async with httpx.AsyncClient(timeout=10, follow_redirects=False, trust_env=False) as client:
            for repository in dict.fromkeys(repositories):
                url = probes.get(repository)
                if url is None or https_origin(url) not in approved:
                    raise PreflightError(
                        f"Use a version probe for {repository} in the selected test environment."
                    )
                source = await self.sources.pin(repository)
                try:
                    response = await client.get(url)
                    response.raise_for_status()
                    value: object = response.json()
                except (httpx.HTTPError, ValueError) as exc:
                    raise PreflightError(
                        f"Unable to verify the QA deployment for {repository}."
                    ) from exc
                if (
                    not isinstance(value, dict)
                    or cast(dict[str, object], value).get("revision") != source.revision.sha
                ):
                    raise PreflightError(
                        f"The QA deployment for {repository} does not match the pinned revision."
                    )

    @asynccontextmanager
    async def record(self, platform: Platform, path: Path) -> AsyncGenerator[Recording]:
        _require_isolated_platform(platform)
        if platform == "web" and self.web is not None:
            async with _web_recording(self.web, path) as recording:
                yield recording
        elif platform == "ios" and self.ios is not None:
            async with _ios_recording(self.ios, path) as recording:
                yield recording
        else:
            raise PreflightError(f"The {platform} QA build is unavailable.")


def _require_isolated_platform(platform: Platform) -> None:
    if platform == "ios":
        raise PreflightError("iOS Auto Test requires a network-isolated simulator.")


async def forward_web_request(route: Route, origins: frozenset[str]) -> bool:
    url = route.request.url
    if origin(url) not in origins:
        return False
    # Forward each redirect only after checking its target against the run's scope.
    response = await route.fetch(max_redirects=0, timeout=20000)
    try:
        location = response.headers.get("location")
        if (
            location
            and 300 <= response.status < 400
            and origin(urljoin(url, location)) not in origins
        ):
            return False
        await route.fulfill(response=response)
        return True
    finally:
        await response.dispose()


@asynccontextmanager
async def _web_recording(build: WebBuild, path: Path) -> AsyncGenerator[Recording]:
    origins = frozenset((build.origin, *build.approved_origins))
    context = await build.browser.new_context(
        viewport={"width": 1280, "height": 800},
        device_scale_factor=1,
        service_workers="block",
        accept_downloads=False,
    )

    async def route_request(route: Route) -> None:
        if await forward_web_request(route, origins):
            return
        screen.network_error = (
            "The Web app requested a service outside the selected test environment."
        )
        await route.abort()

    async def route_socket(route: WebSocketRoute) -> None:
        screen.network_error = "WebSockets are unavailable in the isolated Auto Test browser."
        await route.close()

    async def close_popup(popup: Page) -> None:
        await popup.close()

    try:
        page = await context.new_page()
        screen = WebScreen(page, origins)
        await context.route("**/*", route_request)
        await context.route_web_socket("**/*", route_socket)
        page.on("popup", close_popup)
        try:
            await page.goto(
                build.origin + build.profile.start_path,
                wait_until="domcontentloaded",
                timeout=25000,
            )
        except PlaywrightError as exc:
            if screen.network_error:
                raise PreflightError(screen.network_error) from exc
            raise
        await screen.require_target()
        await screen.setup(build.profile.setup)
        await screen.require_target()
        await page.screencast.start(path=path, size={"width": 1280, "height": 800})
        try:
            yield Recording(screen, path, time.monotonic())
        finally:
            await page.screencast.stop()
        await screen.require_target()
    finally:
        await context.close()


@asynccontextmanager
async def _ios_recording(build: IosBuild, path: Path) -> AsyncGenerator[Recording]:
    capabilities = {
        "platformName": "iOS",
        "appium:automationName": "XCUITest",
        "appium:udid": build.udid,
        "appium:app": str(build.app),
        "appium:bundleId": build.profile.bundle_id,
        "appium:fullReset": True,
        "appium:wdaLocalPort": _local_port(),
        "appium:newCommandTimeout": 180,
        "appium:showXcodeLog": False,
    }
    response = await build.appium.post(
        "/session", json={"capabilities": {"alwaysMatch": capabilities}}, timeout=240
    )
    if response.is_error:
        raise PreflightError("Appium could not start the configured iOS test app.")
    value: object = response.json()
    data = cast(dict[str, object], value).get("value") if isinstance(value, dict) else None
    session = cast(dict[str, object], data).get("sessionId") if isinstance(data, dict) else None
    if not isinstance(session, str) or not session or not session.replace("-", "").isalnum():
        raise PreflightError("Appium did not return a valid iOS session.")
    screen = IosScreen(build.appium, session, build.profile.bundle_id)
    try:
        await screen.setup(build.profile.setup)
        await screen.require_target()
        async with owned_process(
            (
                "xcrun",
                "simctl",
                "io",
                build.udid,
                "recordVideo",
                "--codec=h264",
                str(path),
            ),
            stop_signal=signal.SIGINT,
            environment=build_environment(),
        ) as recorder:
            await asyncio.sleep(1)
            if recorder.returncode is not None:
                raise PreflightError("The iOS screen recorder could not start.")
            yield Recording(screen, path, time.monotonic())
    finally:
        try:
            await build.appium.delete(f"/session/{session}", timeout=15)
        except httpx.HTTPError:
            pass


@asynccontextmanager
async def _simulator(profile: IosTarget) -> AsyncGenerator[str]:
    raw = await command(
        ("xcrun", "simctl", "create", "Lumon Auto Test", profile.device_type, profile.runtime),
        label="Create the Auto Test simulator",
        read=True,
    )
    try:
        udid = str(UUID(raw.decode().strip()))
    except ValueError as exc:
        raise PreflightError("Xcode did not return the Auto Test simulator ID.") from exc
    try:
        await command(
            ("xcrun", "simctl", "bootstatus", udid, "-b"),
            label="Boot the Auto Test simulator",
            timeout=240,
        )
        yield udid
    finally:
        for action in ("shutdown", "delete"):
            try:
                await command(
                    ("xcrun", "simctl", action, udid), label="Clean up the Auto Test simulator"
                )
            except PreflightError:
                continue


@asynccontextmanager
async def _appium() -> AsyncGenerator[httpx.AsyncClient]:
    port = _local_port()
    async with owned_process(
        ("appium", "--address", "127.0.0.1", "--port", str(port), "--log-level", "error"),
        environment=build_environment(),
    ) as process:
        async with httpx.AsyncClient(
            base_url=f"http://127.0.0.1:{port}", timeout=25, trust_env=False
        ) as client:
            await _wait_for_appium(client, process)
            yield client


async def _wait_for_appium(client: httpx.AsyncClient, process: asyncio.subprocess.Process) -> None:
    for _ in range(60):
        if process.returncode is not None:
            break
        try:
            response = await client.get("/status", timeout=0.5)
            response.raise_for_status()
            return
        except httpx.HTTPError:
            await asyncio.sleep(0.25)
    raise PreflightError("Appium did not become ready on its local port.")


class _WebHandler(SimpleHTTPRequestHandler):
    base_path = ""

    def translate_path(self, path: str) -> str:
        root = Path(self.directory).resolve()
        request_path = urlsplit(path).path
        if self.base_path and not (
            request_path == self.base_path or request_path.startswith(self.base_path + "/")
        ):
            return str(root / ".unavailable")
        relative = request_path.removeprefix(self.base_path)
        candidate = Path(super().translate_path(relative))
        resolved = candidate.resolve()
        if not resolved.is_relative_to(root) or any(
            part.startswith(".") for part in resolved.relative_to(root).parts
        ):
            return str(root / ".unavailable")
        if candidate.is_dir() or (not candidate.exists() and not Path(relative).suffix):
            return str(root / "index.html")
        return str(resolved)

    def log_message(self, format: str, *args: object) -> None:
        pass


@asynccontextmanager
async def _serve(directory: Path, base: str = "/") -> AsyncGenerator[str]:
    class MountedHandler(_WebHandler):
        base_path = base.rstrip("/")

    with ThreadingHTTPServer(
        ("127.0.0.1", 0), partial(MountedHandler, directory=str(directory))
    ) as server:
        thread = Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            yield f"http://127.0.0.1:{server.server_port}"
        finally:
            await asyncio.to_thread(server.shutdown)
            thread.join()


def _inside(root: Path, relative: str) -> Path:
    candidate = (root / relative).resolve()
    if not candidate.is_relative_to(root.resolve()):
        raise PreflightError("An Auto Test build path escaped the run directory.")
    return candidate


def _verify_app(app: Path, bundle_id: str) -> str:
    try:
        with (app / "Info.plist").open("rb") as stream:
            info = cast(dict[str, object], plistlib.load(stream))
    except (ValueError, OSError, plistlib.InvalidFileException) as exc:
        raise PreflightError("The Xcode build did not produce the configured iOS app.") from exc
    if (
        info.get("CFBundleIdentifier") != bundle_id
        or info.get("DTPlatformName") != "iphonesimulator"
    ):
        raise PreflightError("The built app does not match the configured simulator bundle.")
    return f"{info.get('CFBundleShortVersionString', '?')} ({info.get('CFBundleVersion', '?')})"


def _local_port() -> int:
    with socket.socket() as server:
        server.bind(("127.0.0.1", 0))
        return int(server.getsockname()[1])
