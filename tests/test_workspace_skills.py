"""Workspace Skills use Codex discovery without changing the user's Codex settings."""

from __future__ import annotations

import asyncio
import os
import shutil
import tomllib
from collections.abc import Mapping, Sequence
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import cast
from uuid import uuid4

import pytest

from lumon.errors import AgentRuntimeError, PreflightError
from lumon.skills.manager import CodexSkillManager, workspace_skill_config
from lumon.skills.settings import WorkspaceSkillSettings
from lumon.tools.codex_rpc import CodexRpc, CodexRpcError
from lumon.workspace.manifest import WorkspaceManifest
from lumon.workspace.settings import WorkspaceSettingsStore


class FakeSkillRpc(CodexRpc):
    def __init__(self, workspace: Path) -> None:
        super().__init__()
        self.workspace = workspace
        self.calls: list[str] = []
        self.fail = False
        self.invalid = False
        self.empty = False
        self.skills: list[dict[str, object]] = [
            {
                "name": "same",
                "description": "User skill",
                "path": "/user/one/SKILL.md",
                "scope": "user",
                "enabled": True,
                "pluginId": "example@plugins",
            },
            {
                "name": "same",
                "description": "Project skill",
                "path": "/repo/two/SKILL.md",
                "scope": "repo",
                "enabled": True,
            },
            {
                "name": "blocked",
                "description": "Disabled in Codex",
                "path": "/user/blocked/SKILL.md",
                "scope": "user",
                "enabled": False,
            },
        ]

    async def start(
        self,
        binary: str,
        workspace: Path,
        environment: Mapping[str, str] | None,
        *,
        config: Sequence[str] = (),
    ) -> None:
        del binary, workspace, environment, config

    async def send(self, payload: dict[str, object]) -> None:
        del payload

    async def request(
        self, method: str, params: dict[str, object], *, timeout: float = 30
    ) -> dict[str, object]:
        del params, timeout
        self.calls.append(method)
        if self.fail:
            raise CodexRpcError()
        if method == "initialize":
            return {}
        if method == "skills/list":
            return {"data": [{"cwd": str(self.workspace), "skills": self.skills, "errors": [{}]}]}
        if method == "config/read":
            if self.invalid:
                return {"config": {"skills": {"config": [{"unexpected": True}]}}}
            if self.empty:
                return {"config": {"skills": None}}
            return {"config": {"skills": {"config": [{"name": "blocked", "enabled": False}]}}}
        raise AssertionError(method)

    async def close(self, *, kill_tools: bool = True) -> None:
        del kill_tools


def test_discovery_and_switches_are_isolated_by_workspace(tmp_path: Path) -> None:
    first, second = uuid4(), uuid4()
    rpc = FakeSkillRpc(tmp_path)
    settings = WorkspaceSkillSettings(tmp_path / "state")
    manager = CodexSkillManager(binary="codex", settings=settings, rpc_factory=lambda: rpc)

    async def check() -> None:
        original = await manager.inventory(tmp_path, first)
        assert original.error_count == 1
        assert [item.source for item in original.skills] == ["plugin", "repo", "user"]
        assert original.skills[0].id != original.skills[1].id
        assert [item.enabled for item in original.skills] == [True, True, False]
        await manager.set_enabled(tmp_path, first, original.skills[0].id, False)
        after = await manager.inventory(tmp_path, first)
        other = await manager.inventory(tmp_path, second)
        assert [item.enabled for item in after.skills] == [False, True, False]
        assert [item.enabled for item in other.skills] == [True, True, False]
        with pytest.raises(PreflightError, match="disabled in Codex"):
            await manager.set_enabled(tmp_path, first, original.skills[2].id, True)
        rpc.skills = rpc.skills[1:]
        with pytest.raises(PreflightError, match="no longer available"):
            await manager.set_enabled(tmp_path, first, original.skills[0].id, True)

    asyncio.run(check())
    assert settings.load(first) == frozenset({"/user/one/SKILL.md"})
    assert settings.load(second) == frozenset()
    assert settings.path_for(first).stat().st_mode & 0o777 == 0o600


def test_runtime_overrides_preserve_codex_rules_and_fail_closed(tmp_path: Path) -> None:
    rpc = FakeSkillRpc(tmp_path)
    manager = CodexSkillManager(binary="codex", rpc_factory=lambda: rpc)

    async def check() -> None:
        assert await manager.execution_config(tmp_path, frozenset()) == ()
        assert not rpc.calls
        config = await manager.execution_config(tmp_path, frozenset({'/tmp/skill "one"/SKILL.md'}))
        parsed = tomllib.loads(config[0])
        rules = cast(list[dict[str, object]], cast(dict[str, object], parsed["skills"])["config"])
        assert rules == [
            {"name": "blocked", "enabled": False},
            {"path": '/tmp/skill "one"/SKILL.md', "enabled": False},
        ]
        rpc.empty = True
        empty_config = await manager.execution_config(tmp_path, frozenset({"/tmp/a/SKILL.md"}))
        assert tomllib.loads(empty_config[0])["skills"]["config"] == [
            {"path": "/tmp/a/SKILL.md", "enabled": False}
        ]
        rpc.empty = False
        rpc.invalid = True
        with pytest.raises(AgentRuntimeError, match="Skill settings"):
            await manager.execution_config(tmp_path, frozenset({"/tmp/a/SKILL.md"}))
        rpc.fail = True
        with pytest.raises(AgentRuntimeError, match="Codex Skills"):
            await manager.inventory(tmp_path, uuid4())

    asyncio.run(check())


def test_skill_settings_preserve_concurrent_updates_and_reject_bad_data(tmp_path: Path) -> None:
    workspace_id = uuid4()
    settings = WorkspaceSkillSettings(tmp_path)
    paths = [f"/tmp/skill-{index}/SKILL.md" for index in range(10)]

    def disable(path: str) -> None:
        settings.set_enabled(workspace_id, path, False)

    with ThreadPoolExecutor(max_workers=5) as executor:
        list(executor.map(disable, paths))
    assert settings.load(workspace_id) == frozenset(paths)
    settings.path_for(workspace_id).write_text('{"invalid": true}')
    with pytest.raises(PreflightError, match="Skill settings"):
        settings.load(workspace_id)


def test_unregister_removes_only_the_workspace_skill_profile(tmp_path: Path) -> None:
    workspace_id = uuid4()
    skills = WorkspaceSkillSettings(tmp_path)
    skills.set_enabled(workspace_id, "/tmp/example/SKILL.md", False)
    WorkspaceSettingsStore(tmp_path).remove(workspace_id)
    assert not skills.path_for(workspace_id).exists()


@pytest.mark.skipif(shutil.which("codex") is None, reason="Codex CLI is not installed")
def test_native_codex_discovery_preserves_global_and_workspace_restrictions(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = tmp_path / "home"
    codex_home = home / ".codex"
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "lumon").mkdir()
    manifest = WorkspaceManifest.create("test", "1.0.0")
    manifest.write(workspace / "lumon" / "manifest.json")
    codex_home.mkdir(parents=True)
    paths = [home / ".agents" / "skills" / name / "SKILL.md" for name in ("first", "second")]
    for path in paths:
        path.parent.mkdir(parents=True)
        path.write_text(f"---\nname: {path.parent.name}\ndescription: Test skill\n---\n\nTest\n")
    (codex_home / "config.toml").write_text(
        f'[[skills.config]]\npath = "{paths[1]}"\nenabled = false\n'
    )
    original_config = (codex_home / "config.toml").read_bytes()
    environment = {"HOME": str(home), "CODEX_HOME": str(codex_home), "PATH": os.environ["PATH"]}
    settings = WorkspaceSkillSettings(tmp_path / "state")
    monkeypatch.setenv("LUMON_HOME", str(tmp_path / "state"))
    manager = CodexSkillManager(
        binary=shutil.which("codex"), environment=environment, settings=settings
    )
    workspace_id = manifest.workspace_id

    async def check() -> None:
        inventory = await manager.inventory(workspace, workspace_id)
        found = {item.name: item for item in inventory.skills}
        assert found["first"].enabled
        assert not found["second"].codex_enabled
        await manager.set_enabled(workspace, workspace_id, found["first"].id, False)
        config = await workspace_skill_config(workspace, manager.binary, environment)
        rpc = CodexRpc()
        try:
            await rpc.start(manager.binary, workspace, environment, config=config)
            await rpc.request("initialize", {"clientInfo": {"name": "lumon-test", "version": "1"}})
            await rpc.send({"method": "initialized"})
            response = await rpc.request(
                "skills/list", {"cwds": [str(workspace)], "forceReload": True}
            )
            groups = cast(list[dict[str, object]], response["data"])
            items = cast(list[dict[str, object]], groups[0]["skills"])
            states = {str(item["name"]): item["enabled"] for item in items}
            assert states["first"] is False
            assert states["second"] is False
        finally:
            await rpc.close()
        assert (codex_home / "config.toml").read_bytes() == original_config
        (codex_home / "config.toml").write_text("")
        empty_native_config = await workspace_skill_config(workspace, manager.binary, environment)
        assert tomllib.loads(empty_native_config[0])["skills"]["config"] == [
            {"path": str(paths[0]), "enabled": False}
        ]

    asyncio.run(check())
