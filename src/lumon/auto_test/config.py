"""Machine-local Auto Test settings and approved QA targets."""

from __future__ import annotations

from pathlib import PurePosixPath
from typing import Annotated, Literal
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field, StrictBool, field_validator

Platform = Literal["web", "ios"]
Identifier = Annotated[str, Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$")]
Command = Annotated[tuple[str, ...], Field(min_length=1, max_length=64)]


class SettingsModel(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid", str_strip_whitespace=True)


class TargetSetup(SettingsModel):
    """A trusted login or setup step performed before recording begins."""

    action: Literal["click", "fill", "wait"]
    selector: str = Field(min_length=1, max_length=500)
    environment_variable: str = Field(default="", pattern=r"^([A-Z][A-Z0-9_]{0,79})?$")

    @field_validator("environment_variable")
    @classmethod
    def validate_account_variable(cls, value: str) -> str:
        if value and not value.startswith("LUMON_QA_"):
            raise ValueError("QA account variables must start with LUMON_QA_.")
        return value


class BuildTarget(SettingsModel):
    repository: Identifier
    build: tuple[Command, ...] = Field(default=(), max_length=12)
    setup: tuple[TargetSetup, ...] = Field(default=(), max_length=16)

    @field_validator("build")
    @classmethod
    def validate_build(cls, commands: tuple[Command, ...]) -> tuple[Command, ...]:
        for command in commands:
            if not command[0] or any("\0" in arg or len(arg) > 2000 for arg in command):
                raise ValueError("Build commands must be nonempty argument arrays.")
        return commands


class WebTarget(BuildTarget):
    directory: str = "."
    output: str
    base_path: str = "/"
    start_path: str = "/"

    @field_validator("directory", "output")
    @classmethod
    def validate_output(cls, value: str) -> str:
        return value if value == "." else relative_path(value)

    @field_validator("base_path", "start_path")
    @classmethod
    def validate_start_path(cls, value: str) -> str:
        if (
            not value.startswith("/")
            or value.startswith("//")
            or "\\" in value
            or any(part == ".." for part in value.split("/"))
        ):
            raise ValueError("The Web start path must be on the local test server.")
        return value


class IosTarget(BuildTarget):
    project: str
    scheme: str = Field(min_length=1, max_length=120)
    configuration: str = Field(default="Debug", min_length=1, max_length=120)
    app: str = Field(max_length=160)
    bundle_id: Identifier
    device_type: Identifier
    runtime: Identifier

    @field_validator("project")
    @classmethod
    def validate_project(cls, value: str) -> str:
        if not value.endswith((".xcworkspace", ".xcodeproj")):
            raise ValueError("Specify an Xcode project or workspace.")
        return relative_path(value)

    @field_validator("app")
    @classmethod
    def validate_app(cls, value: str) -> str:
        if not value.endswith(".app") or "/" in value or "\\" in value:
            raise ValueError("Specify the simulator .app product name.")
        return value


class QABackend(SettingsModel):
    repository: Identifier
    version_url: str

    @field_validator("version_url")
    @classmethod
    def validate_version_url(cls, value: str) -> str:
        https_origin(value)
        if urlsplit(value).query:
            raise ValueError("QA version URLs must not contain query parameters.")
        return value


JIRA_PROJECT_KEY_PATTERN = r"[A-Z][A-Z0-9_]{1,63}"


class AutoTestControls(SettingsModel):
    enabled: StrictBool = False
    schedule_expression: str = "*/5 * * * *"
    jira_site: str = Field(default="", pattern=r"^([a-z0-9][a-z0-9-]*\.atlassian\.net)?$")
    jira_project: str = Field(default="", pattern=rf"^({JIRA_PROJECT_KEY_PATTERN})?$")
    trigger_jql: str = Field(default="", max_length=8000)
    model: Identifier = "gpt-6.1-sol"


class AutoTestSettings(AutoTestControls):
    """Keep operator-approved targets and services in the local Workspace profile."""

    qa_origins: tuple[str, ...] = Field(default=(), max_length=32)
    web: WebTarget | None = None
    ios: IosTarget | None = None
    backends: tuple[QABackend, ...] = Field(default=(), max_length=16)

    @field_validator("qa_origins")
    @classmethod
    def validate_origins(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        for value in values:
            if https_origin(value) != value:
                raise ValueError("List HTTPS QA origins without paths or query parameters.")
        return values


def relative_path(value: str) -> str:
    path = PurePosixPath(value)
    if path.is_absolute() or ".." in path.parts or not path.parts or "\\" in value or "\0" in value:
        raise ValueError("Specify a relative path inside the source snapshot.")
    return value


def https_origin(value: str) -> str:
    parsed = urlsplit(value)
    if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password:
        raise ValueError("Specify a credential-free HTTPS URL.")
    port = f":{parsed.port}" if parsed.port is not None else ""
    return f"https://{parsed.hostname}{port}"
