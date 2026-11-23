"""Install a Workspace Auto Test poll in the macOS user scheduler."""

from __future__ import annotations

import os
import plistlib
import subprocess
import sys
import tempfile
from pathlib import Path
from uuid import UUID

from lumon.auto_test.config import AutoTestSettings
from lumon.delivery.scheduler import launchd_timing
from lumon.errors import PreflightError


class AutoTestScheduler:
    def __init__(self, state_root: Path) -> None:
        self.state_root = state_root

    def apply(self, workspace_id: UUID, workspace: Path, settings: AutoTestSettings) -> None:
        if not settings.enabled:
            self.remove(workspace_id)
            return
        if sys.platform != "darwin":
            raise PreflightError("Auto Test scheduling requires macOS.")
        timing = launchd_timing(settings.schedule_expression, label="Auto Test")
        logs = self.state_root / "logs"
        logs.mkdir(parents=True, exist_ok=True, mode=0o700)
        path = self.plist(workspace_id)
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        previous = path.read_bytes() if path.is_file() else None
        payload = {
            "Label": path.stem,
            "ProgramArguments": [
                sys.executable,
                "-m",
                "lumon",
                "test",
                "poll",
                "--workspace-id",
                str(workspace_id),
                "--json",
            ],
            "WorkingDirectory": str(workspace),
            "EnvironmentVariables": {
                "PATH": os.environ.get("PATH", os.defpath),
                "LUMON_HOME": str(self.state_root),
            },
            "StandardOutPath": str(logs / f"test-{workspace_id}.log"),
            "StandardErrorPath": str(logs / f"test-{workspace_id}.error.log"),
            "RunAtLoad": False,
            **timing,
        }
        try:
            _replace(path, plistlib.dumps(payload))
            _launchctl("bootout", path, check=False)
            _launchctl("bootstrap", path)
        except (OSError, PreflightError) as exc:
            if previous is None:
                path.unlink(missing_ok=True)
            else:
                _replace(path, previous)
                _launchctl("bootstrap", path, check=False)
            raise PreflightError("Could not install the Auto Test schedule.") from exc

    def remove(self, workspace_id: UUID) -> None:
        path = self.plist(workspace_id)
        if sys.platform == "darwin" and path.exists():
            _launchctl("bootout", path, check=False)
            path.unlink(missing_ok=True)

    @staticmethod
    def plist(workspace_id: UUID) -> Path:
        return Path.home() / "Library" / "LaunchAgents" / f"com.lumon.test.{workspace_id}.plist"


def _replace(path: Path, content: bytes) -> None:
    fd, temporary = tempfile.mkstemp(dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        Path(temporary).unlink(missing_ok=True)


def _launchctl(action: str, path: Path, *, check: bool = True) -> None:
    try:
        subprocess.run(
            ("launchctl", action, f"gui/{os.getuid()}", str(path)),
            check=check,
            capture_output=True,
            timeout=10,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise PreflightError("The macOS Auto Test scheduler could not update the job.") from exc
