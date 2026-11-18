"""CLI contract tests for the Agent lifecycle commands."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from typer.testing import CliRunner

from lumon.agents.agent.config import AgentConfig, AgentConfigStore, ObservabilityConfig
from lumon.agents.agent.runner import AgentRunner, CodexAgentRunner
from lumon.cli.app import app
from lumon.cli.commands import agent as agent_commands
from lumon.tools.codex import CodexTool


def test_agent_configure_collects_secret_without_printing_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("LUMON_HOME", str(tmp_path / "lumon"))
    result = CliRunner().invoke(app, ["agent", "configure"], input="cli_test\nsecret-value\n\n")

    assert result.exit_code == 0, result.stdout
    assert "secret-value" not in result.stdout
    config = AgentConfigStore(tmp_path / "lumon").load()
    assert config.feishu_app_id == "cli_test"
    assert config.feishu_app_secret == "secret-value"


def test_agent_doctor_json_is_safe_when_not_configured(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("LUMON_HOME", str(tmp_path / "lumon"))

    result = CliRunner().invoke(app, ["agent", "doctor", "--json"])

    assert result.exit_code == 3
    payload = json.loads(result.stdout)
    assert payload["ok"] is False
    assert "secret" not in result.stdout.casefold()


def test_agent_doctor_reports_the_configured_model_and_effort(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    state_root = tmp_path / "lumon"
    monkeypatch.setenv("LUMON_HOME", str(state_root))
    AgentConfigStore(state_root).save(
        AgentConfig(
            enabled=True,
            agent_model="gpt-5.6-terra",
            agent_reasoning_effort="high",
            feishu_app_id="cli_test",
            feishu_app_secret="secret-value",
        )
    )

    def unavailable_runner(_config: AgentConfig | None, *, codex_home: Path) -> AgentRunner:
        assert codex_home == state_root / "codex-agent"
        return CodexAgentRunner(tool=CodexTool(binary=str(tmp_path / "missing-codex")))

    monkeypatch.setattr(agent_commands, "create_agent_runner", unavailable_runner)
    result = CliRunner().invoke(app, ["agent", "doctor", "--json"])

    payload = json.loads(result.stdout)
    model_check = next(check for check in payload["checks"] if check["name"] == "agent_model")
    assert model_check["detail"] == ("Codex model: gpt-5.6-terra (reasoning effort: high)")


def test_agent_doctor_reports_langfuse_cloud_without_exposing_credentials(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state_root = tmp_path / "lumon"
    monkeypatch.setenv("LUMON_HOME", str(state_root))
    monkeypatch.setenv("LANGFUSE_PUBLIC_KEY", "pk-lf-test")
    monkeypatch.setenv("LANGFUSE_SECRET_KEY", "sk-lf-test")
    AgentConfigStore(state_root).save(
        AgentConfig(
            enabled=True,
            feishu_app_id="cli_test",
            feishu_app_secret="secret-value",
            observability=ObservabilityConfig(enabled=True),
        )
    )

    def unavailable_runner(_config: AgentConfig | None, *, codex_home: Path) -> AgentRunner:
        assert codex_home == state_root / "codex-agent"
        return CodexAgentRunner(tool=CodexTool(binary=str(tmp_path / "missing-codex")))

    monkeypatch.setattr(agent_commands, "create_agent_runner", unavailable_runner)
    result = CliRunner().invoke(app, ["agent", "doctor", "--json"])

    payload = json.loads(result.stdout)
    observability_check = next(
        check for check in payload["checks"] if check["name"] == "observability"
    )
    credentials_check = next(
        check for check in payload["checks"] if check["name"] == "observability_credentials"
    )
    assert "https://cloud.langfuse.com" in observability_check["detail"]
    assert credentials_check["ok"] is True
    assert "pk-lf-test" not in result.stdout
    assert "sk-lf-test" not in result.stdout


def test_agent_status_json_reports_stopped_without_creating_a_process(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("LUMON_HOME", str(tmp_path / "lumon"))

    result = CliRunner().invoke(app, ["agent", "status", "--json"])

    assert result.exit_code == 0
    assert json.loads(result.stdout)["status"] == "stopped"


def test_agent_requires_sign_in_to_its_own_codex_home(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    state_root = tmp_path / "lumon"
    desktop_home = tmp_path / "desktop"
    desktop_home.mkdir()
    (desktop_home / "logged-in").touch()
    monkeypatch.setenv("LUMON_HOME", str(state_root))
    monkeypatch.setenv("CODEX_HOME", str(desktop_home))
    monkeypatch.setenv("CODEX_SQLITE_HOME", str(desktop_home))
    store = AgentConfigStore(state_root)
    store.save(AgentConfig(enabled=True, feishu_app_id="cli_test", feishu_app_secret="test"))
    binary = tmp_path / "fake-codex"
    binary.write_text(
        "#!/bin/sh\n"
        'test "$CODEX_HOME" = "$CODEX_SQLITE_HOME" || exit 2\n'
        'if test "$2" = status; then test -f "$CODEX_HOME/logged-in"; exit; fi\n'
        'touch "$CODEX_HOME/logged-in"\n'
    )
    binary.chmod(0o700)
    monkeypatch.setattr(agent_commands, "resolve_codex_binary", lambda: str(binary))
    monkeypatch.setattr("lumon.tools.codex.resolve_codex_binary", lambda: str(binary))
    cli = CliRunner()
    start = cli.invoke(app, ["agent", "start", "--background"])
    assert start.exit_code == 3
    assert "lumon agent codex-login" in start.output

    login = cli.invoke(app, ["agent", "codex-login"])

    assert login.exit_code == 0, login.output
    assert (store.codex_home / "logged-in").is_file()
    assert store.codex_home.stat().st_mode & 0o777 == 0o700
    report = cli.invoke(app, ["agent", "doctor", "--json"])
    check = next(c for c in json.loads(report.stdout)["checks"] if c["name"] == "agent_login")
    assert check["ok"] is True
    assert str(store.codex_home) in check["detail"]
    assert set(desktop_home.iterdir()) == {desktop_home / "logged-in"}


def test_agent_status_reads_legacy_runtime_pid_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    state_root = tmp_path / "lumon"
    state_root.mkdir()
    monkeypatch.setenv("LUMON_HOME", str(state_root))
    (state_root / "mark-agent.pid").write_text("1234\n", encoding="utf-8")

    def pid_is_running(_pid: int) -> bool:
        return True

    monkeypatch.setattr(agent_commands, "_pid_is_running", pid_is_running)

    result = CliRunner().invoke(app, ["agent", "status", "--json"])

    assert result.exit_code == 0
    assert json.loads(result.stdout) == {
        "pid": 1234,
        "state_file": str(state_root / "agent.status.json"),
        "status": "running",
    }
