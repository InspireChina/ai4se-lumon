"""Machine-local Skill restrictions for individual Workspaces."""

from __future__ import annotations

import fcntl
import json
import os
import tempfile
from pathlib import Path
from typing import cast
from uuid import UUID

from lumon.errors import PreflightError
from lumon.workspace.registry import UserStateLayout


class WorkspaceSkillSettings:
    """Store disabled Skill paths without rewriting other Workspace settings."""

    def __init__(self, state_root: Path | None = None) -> None:
        self.layout = UserStateLayout.from_root(state_root)

    def path_for(self, workspace_id: UUID) -> Path:
        return self.layout.profile(workspace_id).with_name("skills.json")

    def load(self, workspace_id: UUID) -> frozenset[str]:
        path = self.path_for(workspace_id)
        try:
            if path.is_symlink():
                raise ValueError("Invalid Skill settings")
            try:
                size = path.stat().st_size
            except FileNotFoundError:
                return frozenset()
            if size > 1_000_000:
                raise ValueError("Invalid Skill settings")
            payload: object = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(payload, list):
                raise ValueError("Invalid Skill settings")
            paths: list[str] = []
            for item in cast(list[object], payload):
                if (
                    not isinstance(item, str)
                    or not Path(item).is_absolute()
                    or Path(item).name != "SKILL.md"
                ):
                    raise ValueError("Invalid Skill settings")
                paths.append(item)
            return frozenset(paths)
        except (OSError, UnicodeError, ValueError) as exc:
            raise PreflightError("Unable to read Workspace Skill settings.") from exc

    def set_enabled(self, workspace_id: UUID, path: str, enabled: bool) -> None:
        """Update one path while preserving concurrent changes to other paths."""

        if not Path(path).is_absolute() or Path(path).name != "SKILL.md":
            raise PreflightError("Invalid Skill path.")
        target = self.path_for(workspace_id)
        temporary: Path | None = None
        try:
            target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            descriptor = os.open(target.parent, os.O_RDONLY)
            try:
                fcntl.flock(descriptor, fcntl.LOCK_EX)
                disabled = set(self.load(workspace_id))
                if enabled:
                    disabled.discard(path)
                else:
                    disabled.add(path)
                with tempfile.NamedTemporaryFile(dir=target.parent, delete=False) as stream:
                    temporary = Path(stream.name)
                    stream.write(json.dumps(sorted(disabled), ensure_ascii=False).encode("utf-8"))
                    stream.flush()
                    os.fsync(stream.fileno())
                os.replace(temporary, target)
                temporary = None
            finally:
                os.close(descriptor)
        except OSError as exc:
            raise PreflightError("Unable to save Workspace Skill settings.") from exc
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)
