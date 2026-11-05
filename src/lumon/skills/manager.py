"""Discover Codex Skills and apply Workspace-local restrictions."""

from __future__ import annotations

import hashlib
import json
from collections.abc import AsyncGenerator, Callable, Mapping
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from lumon.errors import AgentRuntimeError, PreflightError
from lumon.skills.settings import WorkspaceSkillSettings
from lumon.tools.codex import resolve_codex_binary
from lumon.tools.codex_rpc import CodexRpc, CodexRpcError
from lumon.tools.github_mcp_proxy import workspace_git_environment, workspace_mcp_config
from lumon.version import __version__
from lumon.workspace.layout import WorkspaceLayout
from lumon.workspace.manifest import load_manifest


@dataclass(frozen=True, slots=True)
class WorkspaceSkill:
    id: str
    name: str
    description: str
    path: str
    source: str
    enabled: bool
    codex_enabled: bool


@dataclass(frozen=True, slots=True)
class SkillInventory:
    skills: tuple[WorkspaceSkill, ...]
    error_count: int


class _NativeSkill(BaseModel):
    model_config = ConfigDict(extra="ignore", strict=True)
    name: str = Field(min_length=1, max_length=1024)
    description: str = Field(max_length=32768)
    path: str = Field(min_length=1, max_length=4096)
    scope: str = Field(min_length=1, max_length=64)
    enabled: bool
    plugin_id: str | None = Field(default=None, alias="pluginId")

    @model_validator(mode="after")
    def absolute_skill_path(self) -> _NativeSkill:
        if not Path(self.path).is_absolute() or Path(self.path).name != "SKILL.md":
            raise ValueError("Invalid Skill path")
        return self


class _SkillGroup(BaseModel):
    model_config = ConfigDict(extra="ignore", strict=True)
    cwd: str
    skills: list[_NativeSkill] = Field(max_length=10000)
    errors: list[object] = Field(default_factory=lambda: list[object]())


class _SkillList(BaseModel):
    model_config = ConfigDict(extra="ignore", strict=True)
    data: list[_SkillGroup]


class _SkillRule(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    path: str | None = None
    name: str | None = None
    enabled: bool

    @model_validator(mode="after")
    def one_selector(self) -> _SkillRule:
        if bool(self.path) == bool(self.name):
            raise ValueError("A Skill rule must have exactly one selector")
        return self


class _NativeSkillsConfig(BaseModel):
    model_config = ConfigDict(extra="ignore", strict=True)
    config: list[_SkillRule] | None = None


class _NativeConfig(BaseModel):
    model_config = ConfigDict(extra="ignore", strict=True)
    skills: _NativeSkillsConfig | None = None


class _ConfigResponse(BaseModel):
    model_config = ConfigDict(extra="ignore", strict=True)
    config: _NativeConfig


class CodexSkillManager:
    """Read native Skill metadata without loading Skill instructions."""

    def __init__(
        self,
        binary: str | None = None,
        *,
        environment: Mapping[str, str] | None = None,
        settings: WorkspaceSkillSettings | None = None,
        rpc_factory: Callable[[], CodexRpc] = CodexRpc,
    ) -> None:
        self.binary = binary or resolve_codex_binary()
        self.environment = dict(environment) if environment is not None else None
        self.settings = settings or WorkspaceSkillSettings()
        self._rpc_factory = rpc_factory

    async def inventory(self, workspace: Path, workspace_id: UUID) -> SkillInventory:
        async with self._rpc(workspace) as rpc:
            response = await rpc.request(
                "skills/list", {"cwds": [str(workspace)], "forceReload": True}, timeout=15
            )
            try:
                groups = _SkillList.model_validate(response).data
            except ValidationError:
                raise AgentRuntimeError("Codex returned an invalid Skill catalog.") from None
        group = next((item for item in groups if item.cwd == str(workspace)), None)
        if group is None:
            raise AgentRuntimeError("Codex did not return Skills for this Workspace.")
        disabled = self.settings.load(workspace_id)
        skills = tuple(
            WorkspaceSkill(
                _skill_id(item.path),
                item.name,
                item.description,
                item.path,
                "plugin" if item.plugin_id else item.scope,
                item.enabled and item.path not in disabled,
                item.enabled,
            )
            for item in group.skills
        )
        return SkillInventory(skills, len(group.errors))

    async def set_enabled(
        self, workspace: Path, workspace_id: UUID, skill_id: str, enabled: bool
    ) -> None:
        inventory = await self.inventory(workspace, workspace_id)
        skill = next((item for item in inventory.skills if item.id == skill_id), None)
        if skill is None:
            raise PreflightError("Skill is no longer available. Refresh and try again.")
        if enabled and not skill.codex_enabled:
            raise PreflightError("This Skill is disabled in Codex settings.")
        self.settings.set_enabled(workspace_id, skill.path, enabled)

    async def execution_config(self, workspace: Path, disabled: frozenset[str]) -> tuple[str, ...]:
        if not disabled:
            return ()
        async with self._rpc(workspace) as rpc:
            response = await rpc.request(
                "config/read", {"cwd": str(workspace), "includeLayers": False}, timeout=15
            )
            try:
                skills = _ConfigResponse.model_validate(response).config.skills
            except ValidationError:
                raise AgentRuntimeError("Unable to read Codex Skill settings.") from None
        rules: list[_SkillRule] = []
        if skills is not None and skills.config is not None:
            rules = skills.config
        entries = [_rule_toml(rule) for rule in rules]
        entries.extend(
            _rule_toml(_SkillRule(path=path, enabled=False)) for path in sorted(disabled)
        )
        return ("skills.config=[" + ",".join(entries) + "]",)

    @asynccontextmanager
    async def _rpc(self, workspace: Path) -> AsyncGenerator[CodexRpc]:
        rpc = self._rpc_factory()
        try:
            config = workspace_mcp_config(workspace)
            environment = workspace_git_environment(workspace, self.environment)
            await rpc.start(self.binary, workspace, environment, config=config)
            await rpc.request(
                "initialize",
                {"clientInfo": {"name": "lumon", "title": "Lumon", "version": __version__}},
                timeout=15,
            )
            await rpc.send({"method": "initialized"})
            yield rpc
        except (OSError, TimeoutError, CodexRpcError) as exc:
            raise AgentRuntimeError(
                "Unable to read Codex Skills. Check the local Codex CLI."
            ) from exc
        finally:
            await rpc.close()


async def workspace_skill_config(
    workspace: Path, binary: str, environment: Mapping[str, str] | None
) -> tuple[str, ...]:
    """Build process-only overrides for a registered Workspace, when needed."""

    manifest_path = WorkspaceLayout.from_root(workspace).manifest
    try:
        manifest_path.stat()
    except FileNotFoundError:
        return ()
    except OSError as exc:
        raise PreflightError("Unable to read Workspace Skill settings.") from exc
    workspace_id = load_manifest(manifest_path).workspace_id
    settings = WorkspaceSkillSettings()
    disabled = settings.load(workspace_id)
    return await CodexSkillManager(
        binary, environment=environment, settings=settings
    ).execution_config(workspace, disabled)


def _skill_id(path: str) -> str:
    return hashlib.sha256(path.encode("utf-8")).hexdigest()


def _rule_toml(rule: _SkillRule) -> str:
    selector = "path" if rule.path is not None else "name"
    value = rule.path if rule.path is not None else rule.name
    return (
        "{"
        + selector
        + "="
        + json.dumps(value, ensure_ascii=False)
        + ",enabled="
        + ("true" if rule.enabled else "false")
        + "}"
    )
