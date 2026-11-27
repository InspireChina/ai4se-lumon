"""Bind UI actions to the test page or simulator app owned by this run."""

from __future__ import annotations

import asyncio
import base64
import os
import struct
from abc import ABC, abstractmethod
from typing import Literal, cast
from urllib.parse import urlsplit

import httpx
from playwright.async_api import Page
from pydantic import Field, StrictInt

from lumon.auto_test.config import TargetSetup
from lumon.auto_test.model import Check, Record
from lumon.errors import PreflightError
from lumon.tools.safety import sanitize_output

Viewport = tuple[int, int]
Point = tuple[int, int]
_KEYS = {
    "ENTER": "Enter",
    "TAB": "Tab",
    "ESC": "Escape",
    "ESCAPE": "Escape",
    "BACKSPACE": "Backspace",
    "DELETE": "Delete",
    "SPACE": "Space",
    "ARROWUP": "ArrowUp",
    "ARROWDOWN": "ArrowDown",
    "ARROWLEFT": "ArrowLeft",
    "ARROWRIGHT": "ArrowRight",
    "UP": "ArrowUp",
    "DOWN": "ArrowDown",
    "LEFT": "ArrowLeft",
    "RIGHT": "ArrowRight",
}
_ELEMENT = "element-6066-11e4-a52e-4f735466cecf"


class ScreenPoint(Record):
    x: StrictInt
    y: StrictInt


class PointerAction(ScreenPoint):
    type: Literal["click", "double_click", "move"]


class ScrollAction(ScreenPoint):
    type: Literal["scroll"]
    scroll_x: StrictInt
    scroll_y: StrictInt


class DragAction(Record):
    type: Literal["drag"]
    path: tuple[ScreenPoint, ...] = Field(min_length=2, max_length=32)


class TypeAction(Record):
    type: Literal["type"]
    text: str = Field(max_length=2000)


class KeyAction(Record):
    type: Literal["keypress"]
    keys: list[str] = Field(min_length=1, max_length=1)


class WaitAction(Record):
    type: Literal["wait"]


class ScreenshotAction(Record):
    type: Literal["screenshot"]


UiAction = PointerAction | ScrollAction | DragAction | TypeAction | KeyAction | WaitAction


class ScreenRequest(Record):
    action: UiAction | ScreenshotAction


class Screen(ABC):
    def __init__(self) -> None:
        self.image_size: Viewport | None = None
        self.actions = 0

    async def perform(self, action: UiAction) -> None:
        await self.require_target()
        self.actions += 1
        if self.actions > 80:
            raise PreflightError("The Auto Test case exceeded the UI action limit.")
        await self.apply(action)

    async def screenshot(self) -> bytes:
        image = await self.capture_png()
        self.image_size = struct.unpack(">II", image[16:24])
        return image

    async def capture_png(self) -> bytes:
        await self.require_target()
        image = await self.capture()
        if not 24 <= len(image) <= 8 * 1024 * 1024 or not image.startswith(b"\x89PNG\r\n\x1a\n"):
            raise PreflightError("Auto Test could not obtain a valid PNG screenshot.")
        return image

    def point(self, x: int, y: int, viewport: Viewport) -> Point:
        width, height = self.image_size or viewport
        if min(width, height) <= 0 or not 0 <= x < width or not 0 <= y < height:
            raise PreflightError("Auto Test UI coordinates are outside the recorded screen.")
        return x * viewport[0] // width, y * viewport[1] // height

    async def setup(self, steps: tuple[TargetSetup, ...]) -> None:
        for step in steps:
            await self.require_target()
            value = os.environ.get(step.environment_variable) if step.environment_variable else None
            if step.action == "fill" and not value:
                raise PreflightError("A configured QA account value is unavailable.")
            await self.setup_step(step, value or "")

    async def observe(self, check: Check) -> tuple[bool, bool, str]:
        await self.require_target()
        visible, text = await self.element(
            check.selector, read_text=check.operator.startswith("text")
        )
        actual = sanitize_output(text or ("visible" if visible else "not visible"))[:500]
        match = {
            "visible": visible,
            "not_visible": not visible,
            "text_equals": visible and text == check.expected,
            "text_contains": visible and check.expected in text,
        }[check.operator]
        return match, visible, actual

    @abstractmethod
    async def require_target(self) -> None: ...

    @abstractmethod
    async def capture(self) -> bytes: ...

    @abstractmethod
    async def apply(self, action: UiAction) -> None: ...

    @abstractmethod
    async def element(self, selector: str, *, read_text: bool) -> tuple[bool, str]: ...

    @abstractmethod
    async def setup_step(self, step: TargetSetup, value: str) -> None: ...


def origin(url: str) -> str:
    parsed = urlsplit(url)
    if parsed.scheme not in {"http", "https", "ws", "wss"} or parsed.username or parsed.password:
        return ""
    scheme = {"ws": "http", "wss": "https"}.get(parsed.scheme, parsed.scheme)
    return f"{scheme}://{parsed.netloc}"


class WebScreen(Screen):
    def __init__(self, page: Page, origins: frozenset[str]) -> None:
        super().__init__()
        self.page = page
        self.origins = origins
        self.network_error = ""

    async def require_target(self) -> None:
        if self.network_error:
            raise PreflightError(self.network_error)
        if origin(self.page.url) not in self.origins:
            raise PreflightError("The browser left the configured QA origins.")

    async def capture(self) -> bytes:
        return await self.page.screenshot(type="png", scale="css", timeout=10000)

    async def apply(self, action: UiAction) -> None:
        viewport = self.page.viewport_size
        if viewport is None:
            raise PreflightError("Auto Test needs a fixed browser viewport.")
        bounds = (viewport["width"], viewport["height"])
        mouse = self.page.mouse
        if (
            action.type == "click"
            or action.type == "double_click"
            or action.type == "move"
            or action.type == "scroll"
        ):
            x, y = self.point(action.x, action.y, bounds)
            if action.type == "click":
                await mouse.click(x, y)
            elif action.type == "double_click":
                await mouse.dblclick(x, y)
            elif action.type == "move":
                await mouse.move(x, y)
            elif action.type == "scroll":
                await mouse.move(x, y)
                await mouse.wheel(action.scroll_x, action.scroll_y)
        elif action.type == "drag":
            points = [self.point(point.x, point.y, bounds) for point in action.path]
            await mouse.move(*points[0])
            await mouse.down()
            try:
                for point in points[1:]:
                    await mouse.move(*point)
            finally:
                await mouse.up()
        elif action.type == "type":
            await self.page.keyboard.insert_text(_typed_value(action.text))
        elif action.type == "keypress":
            await self.page.keyboard.press(_key(action.keys))
        else:
            await _wait()

    async def element(self, selector: str, *, read_text: bool) -> tuple[bool, str]:
        locator = self.page.locator(selector)
        if await locator.count() > 1:
            raise PreflightError("The Web assertion selector matches more than one element.")
        visible = await locator.is_visible()
        return visible, (
            await locator.inner_text(timeout=5000)
        ).strip() if visible and read_text else ""

    async def setup_step(self, step: TargetSetup, value: str) -> None:
        locator = self.page.locator(step.selector)
        if step.action == "fill":
            await locator.fill(value, timeout=15000)
        elif step.action == "click":
            await locator.click(timeout=15000)
        else:
            await locator.wait_for(state="visible", timeout=15000)


class IosScreen(Screen):
    def __init__(self, client: httpx.AsyncClient, session: str, bundle_id: str) -> None:
        super().__init__()
        self.client = client
        self.session = session
        self.bundle_id = bundle_id

    async def request(self, method: str, path: str, body: object = None) -> object:
        response = await self.client.request(method, f"/session/{self.session}{path}", json=body)
        if response.is_error:
            raise PreflightError("Appium could not complete the iOS UI operation.")
        return _mapping(response.json()).get("value")

    async def require_target(self) -> None:
        app = await self.request(
            "POST", "/execute/sync", {"script": "mobile: activeAppInfo", "args": []}
        )
        if _mapping(app).get("bundleId") != self.bundle_id:
            raise PreflightError("The configured iOS QA app is no longer in the foreground.")

    async def capture(self) -> bytes:
        image = await self.request("GET", "/screenshot")
        if not isinstance(image, str):
            raise PreflightError("Appium returned an invalid screenshot.")
        return base64.b64decode(image, validate=True)

    async def apply(self, action: UiAction) -> None:
        if action.type in {"click", "double_click", "drag", "scroll"}:
            await self._touch(action)
        elif action.type == "type":
            active = _element_id(await self.request("GET", "/element/active"))
            await self._send_keys(active, _typed_value(action.text))
        elif action.type == "keypress":
            key = _key(action.keys)
            escaped = {"Enter": "\ue007", "Backspace": "\ue003", "Tab": "\ue004"}.get(key)
            if escaped is None:
                raise PreflightError("This key is unavailable on the iOS test keyboard.")
            active = _element_id(await self.request("GET", "/element/active"))
            await self._send_keys(active, escaped)
        elif action.type == "move":
            raise PreflightError("Hover input is unavailable on the iOS simulator.")
        else:
            await _wait()

    async def _touch(self, action: UiAction) -> None:
        rect = _mapping(await self.request("GET", "/window/rect"))
        width, height = rect.get("width"), rect.get("height")
        if not isinstance(width, int) or not isinstance(height, int) or min(width, height) <= 0:
            raise PreflightError("Appium returned an invalid viewport.")
        bounds = width, height
        if action.type == "drag":
            points = [self.point(point.x, point.y, bounds) for point in action.path]
        elif action.type == "click" or action.type == "double_click" or action.type == "scroll":
            points = [self.point(action.x, action.y, bounds)]
            if action.type == "scroll":
                x, y = points[0]
                points.append(
                    (
                        max(0, min(width - 1, x - action.scroll_x)),
                        max(0, min(height - 1, y - action.scroll_y)),
                    )
                )
        else:
            raise PreflightError("The iOS driver received an unsupported touch action.")
        x, y = points[0]
        actions: list[dict[str, object]] = [
            {"type": "pointerMove", "duration": 0, "x": x, "y": y},
            {"type": "pointerDown", "button": 0},
            {"type": "pause", "duration": 70},
        ]
        actions.extend(
            {"type": "pointerMove", "duration": 140, "x": x, "y": y} for x, y in points[1:]
        )
        actions.append({"type": "pointerUp", "button": 0})
        if action.type == "double_click":
            actions.extend(
                (
                    {"type": "pause", "duration": 70},
                    {"type": "pointerDown", "button": 0},
                    {"type": "pointerUp", "button": 0},
                )
            )
        await self.request(
            "POST",
            "/actions",
            {
                "actions": [
                    {
                        "type": "pointer",
                        "id": "finger",
                        "parameters": {"pointerType": "touch"},
                        "actions": actions,
                    }
                ]
            },
        )

    async def element(self, selector: str, *, read_text: bool) -> tuple[bool, str]:
        element = await self._find(selector)
        if element is None:
            return False, ""
        visible = await self.request("GET", f"/element/{element}/displayed") is True
        value = (
            await self.request("GET", f"/element/{element}/text") if read_text and visible else ""
        )
        return visible, value.strip() if isinstance(value, str) else ""

    async def _find(self, selector: str) -> str | None:
        found = await self.request(
            "POST", "/elements", {"using": "accessibility id", "value": selector}
        )
        if not isinstance(found, list):
            raise PreflightError("Appium did not return a valid element list.")
        elements = cast(list[object], found)
        if len(elements) > 1:
            raise PreflightError("The iOS assertion selector matches more than one element.")
        return _element_id(elements[0]) if elements else None

    async def setup_step(self, step: TargetSetup, value: str) -> None:
        async with asyncio.timeout(15):
            element = await self._find(step.selector)
            while element is None:
                await asyncio.sleep(0.25)
                element = await self._find(step.selector)
        if step.action == "click":
            await self.request("POST", f"/element/{element}/click", {})
        elif step.action == "fill":
            await self.request("POST", f"/element/{element}/clear", {})
            await self._send_keys(element, value)

    async def _send_keys(self, element: str, value: str) -> None:
        await self.request(
            "POST", f"/element/{element}/value", {"text": value, "value": list(value)}
        )


def _mapping(value: object) -> dict[str, object]:
    if not isinstance(value, dict):
        raise PreflightError("The iOS driver returned invalid Appium data.")
    return cast(dict[str, object], value)


def _element_id(value: object) -> str:
    element = _mapping(value).get(_ELEMENT)
    if not isinstance(element, str) or not element or not element.replace("-", "").isalnum():
        raise PreflightError("The iOS driver returned an invalid element identifier.")
    return element


def _typed_value(text: str) -> str:
    if len(text) > 2000 or "\0" in text:
        raise PreflightError("The requested UI input exceeds the Auto Test text limit.")
    return text


def _key(keys: list[str]) -> str:
    if len(keys) != 1 or keys[0].upper() not in _KEYS:
        raise PreflightError("The requested key is unavailable in Auto Test.")
    return _KEYS[keys[0].upper()]


async def _wait() -> None:
    await asyncio.sleep(1)
