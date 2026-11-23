"""Translate Agent-selected project settings into supported local build commands."""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Annotated, cast

from pydantic import BaseModel, Field, TypeAdapter, ValidationError

from lumon.auto_test.config import (
    Command,
    Identifier,
    IosTarget,
    SettingsModel,
    WebTarget,
    relative_path,
)
from lumon.auto_test.process import command
from lumon.errors import PreflightError

_BUILD_SCRIPT = re.compile(r"build(?:[:.-][A-Za-z0-9_-]+)*")
_PUBLISH = re.compile(r"[:.-](?:deploy|publish|release)(?=$|[:.-])", re.IGNORECASE)
_QA_VARIANT = re.compile(r"\.(uat|qa|staging|local)(?=\.[^.]+$)", re.IGNORECASE)
_IOS_NAME = Annotated[str, Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9 ._-]{0,119}$")]
_RUNTIME = re.compile(r"com\.apple\.CoreSimulator\.SimRuntime\.iOS-[0-9-]+")
_NODE_VERSION = re.compile(r"v?([0-9]+(?:\.[0-9]+){0,2})")


class WebChoice(SettingsModel):
    repository: Identifier
    directory: str
    script: str | None
    output: str
    base_path: str
    start_path: str


class IosChoice(SettingsModel):
    repository: Identifier
    project: str
    scheme: _IOS_NAME
    configuration: _IOS_NAME
    app: str
    bundle_id: Identifier
    qa_config_file: str | None


class _SimulatorDevice(BaseModel):
    isAvailable: bool
    deviceTypeIdentifier: str | None = None


def web_target(choice: WebChoice, source: Path) -> WebTarget:
    """Resolve a tracked Node build script or an existing static site."""

    target = WebTarget(
        repository=choice.repository,
        directory=choice.directory,
        output=choice.output,
        base_path=choice.base_path,
        start_path=choice.start_path,
    )
    directory = _source_path(source, target.directory)
    _source_path(directory, target.output)
    if not directory.is_dir():
        raise PreflightError("The selected Web project directory is missing.")

    if choice.script is None:
        if not _source_path(directory, target.output, "index.html").is_file():
            raise PreflightError("The selected static Web target has no index.html.")
        return target

    package = _package(directory)
    scripts = package.get("scripts")
    script = choice.script
    if (
        not _BUILD_SCRIPT.fullmatch(script)
        or _PUBLISH.search(script)
        or not isinstance(scripts, dict)
        or not isinstance(cast(dict[str, object], scripts).get(script), str)
    ):
        raise PreflightError("The Agent must select a declared Web build script.")
    install, runner = _node_commands(directory, package)
    return target.model_copy(update={"build": (install, (*runner, "run", script))})


async def ios_target(choice: IosChoice, source: Path) -> IosTarget:
    """Resolve a shared Xcode scheme, locked dependencies and an available simulator."""

    if not choice.project.endswith((".xcworkspace", ".xcodeproj")):
        raise PreflightError("The Agent must select an Xcode project or workspace.")
    project = _source_path(source, relative_path(choice.project))
    if not project.is_dir() or not _has_scheme(source, project, choice.scheme):
        raise PreflightError("The selected iOS project or shared scheme is missing.")

    steps: list[Command] = []
    if _source_path(source, "package.json").is_file():
        install, _ = _node_commands(source, _package(source))
        steps.append(install)
    if choice.qa_config_file:
        steps.append(_qa_config_step(source, choice.qa_config_file))
    elif not re.search(r"uat|staging|qa$|local$", choice.scheme, re.IGNORECASE):
        raise PreflightError("Select a local or QA iOS scheme or a checked-in QA configuration.")
    podfile = _source_path(source, project.parent.relative_to(source).as_posix(), "Podfile")
    if podfile.is_file():
        parent = podfile.parent.relative_to(source).as_posix()
        if not podfile.with_name("Podfile.lock").is_file():
            raise PreflightError("The iOS Podfile requires a checked-in Podfile.lock.")
        steps.append(("pod", "install", "--deployment", f"--project-directory={parent}"))

    device_type, runtime = await _available_iphone()
    return IosTarget(
        repository=choice.repository,
        build=tuple(steps),
        project=choice.project,
        scheme=choice.scheme,
        configuration=choice.configuration,
        app=choice.app,
        bundle_id=choice.bundle_id,
        device_type=device_type,
        runtime=runtime,
    )


def _source_path(source: Path, *parts: str) -> Path:
    path = source.joinpath(*parts)
    resolved = path.resolve()
    if resolved != path or not resolved.is_relative_to(source):
        raise PreflightError("The Agent selected a path outside the pinned project or a symlink.")
    return resolved


def _package(directory: Path) -> dict[str, object]:
    try:
        package: object = json.loads(_source_path(directory, "package.json").read_bytes())
    except (OSError, ValueError) as exc:
        raise PreflightError("The selected project has no readable package.json.") from exc
    if not isinstance(package, dict):
        raise PreflightError("The selected project's package.json is invalid.")
    return cast(dict[str, object], package)


def _node_commands(directory: Path, package: dict[str, object]) -> tuple[Command, Command]:
    """Use the pinned project's lockfile and Node version, when declared."""

    declared = package.get("packageManager")
    manager, install = _package_manager(directory, declared)
    runner = (manager,)
    packages: list[str] = []
    if manager == "yarn" and install[-1] == "--frozen-lockfile":
        # A host Yarn shim can require a newer Node than the repository selects.
        classic = (
            re.match(r"yarn@(1\.[0-9]+\.[0-9]+)(?:\+|$)", declared)
            if isinstance(declared, str)
            else None
        )
        packages.append(f"--package=yarn@{classic[1] if classic else '1.22.22'}")
    elif manager != "npm" and isinstance(declared, str):
        runner = ("corepack", manager)
    for filename in (".nvmrc", ".node-version"):
        version_file = _source_path(directory, filename)
        if version_file.is_file():
            version = _NODE_VERSION.fullmatch(version_file.read_text().strip())
            if version is None:
                raise PreflightError(
                    "The project must declare a numeric Node version for Auto Test."
                )
            packages.insert(0, f"--package=node@{version[1]}")
            break
    if packages:
        runner = ("npx", "--yes", *packages, "--", *runner)
    return (*runner, *install), runner


def _package_manager(directory: Path, declaration: object) -> tuple[str, tuple[str, ...]]:
    if isinstance(declaration, str):
        manager, separator, version = declaration.partition("@")
        if not separator or manager not in {"npm", "yarn", "pnpm"} or not version:
            raise PreflightError("Auto Test cannot use the declared package manager.")
    else:
        manager = next(
            (
                name
                for name, lock in (("pnpm", "pnpm-lock.yaml"), ("yarn", "yarn.lock"))
                if _source_path(directory, lock).is_file()
            ),
            "npm",
        )
        version = ""
    lock = {"npm": "package-lock.json", "yarn": "yarn.lock", "pnpm": "pnpm-lock.yaml"}[manager]
    if not _source_path(directory, lock).is_file():
        raise PreflightError("Auto Test requires the project's package manager lockfile.")
    if manager == "npm":
        return manager, ("ci",)
    if manager == "yarn" and version and not version.startswith("1."):
        return manager, ("install", "--immutable")
    return manager, ("install", "--frozen-lockfile")


def _has_scheme(source: Path, project: Path, scheme: str) -> bool:
    suffix = f"xcshareddata/xcschemes/{scheme}.xcscheme"
    candidates = (project, *project.parent.glob("*.xcodeproj"))
    return any(
        _source_path(source, candidate.relative_to(source).as_posix(), suffix).is_file()
        for candidate in candidates
    )


def _qa_config_step(source: Path, filename: str) -> Command:
    variant = _source_path(source, relative_path(filename))
    derived = variant.with_name(_QA_VARIANT.sub("", variant.name))
    stable = _source_path(source, derived.relative_to(source).as_posix())
    # Some projects generate the active configuration from a tracked variant.
    if variant == stable or not variant.is_file() or stable.is_dir():
        raise PreflightError("The selected iOS QA configuration is unavailable.")
    return ("cp", f"./{variant.relative_to(source)}", f"./{stable.relative_to(source)}")


async def _available_iphone() -> tuple[str, str]:
    raw = await command(
        ("xcrun", "simctl", "list", "devices", "available", "--json"),
        label="List iOS simulators",
        read=True,
        max_output=2 * 1024 * 1024,
    )
    try:
        devices = TypeAdapter(dict[str, list[_SimulatorDevice]]).validate_python(
            json.loads(raw)["devices"]
        )
    except (KeyError, TypeError, ValueError, ValidationError) as exc:
        raise PreflightError("Xcode did not return a valid simulator inventory.") from exc
    for runtime, entries in devices.items():
        if not _RUNTIME.fullmatch(runtime):
            continue
        for entry in entries:
            device = entry.deviceTypeIdentifier
            if (
                entry.isAvailable
                and device
                and device.startswith("com.apple.CoreSimulator.SimDeviceType.iPhone")
            ):
                return device, runtime
    raise PreflightError("Install an iOS runtime with an available iPhone simulator.")
