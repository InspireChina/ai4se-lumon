"""Atomic run receipts and evidence paths shared by the CLI and Dashboard."""

from __future__ import annotations

import fcntl
import os
import tempfile
from collections.abc import Generator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID

from pydantic import BaseModel, ValidationError

from lumon.auto_test.model import Artifact, PollResult, TestRun
from lumon.errors import PreflightError


class RunStore:
    def __init__(self, workspace: Path) -> None:
        self.root = workspace.resolve() / "lumon" / "runs" / "tests"

    def directory(self, run_id: UUID) -> Path:
        directory = self.root / str(run_id)
        if directory.resolve() != directory or directory.is_symlink():
            raise PreflightError("Auto Test run directory is unsafe.")
        return directory

    def save(self, run: TestRun) -> None:
        write_record(self.directory(run.run_id) / "run.json", run)

    def load(self, run_id: UUID) -> TestRun:
        path = self.directory(run_id) / "run.json"
        try:
            if path.is_symlink():
                raise ValueError("Unsafe Auto Test receipt.")
            run = TestRun.model_validate_json(path.read_bytes())
            if run.run_id != run_id:
                raise ValueError("Auto Test run ID mismatch.")
        except (OSError, ValueError, ValidationError) as exc:
            raise PreflightError("Unable to read the Auto Test run receipt.") from exc
        return run

    def list(self) -> tuple[TestRun, ...]:
        if not self.root.is_dir():
            return ()
        runs: list[TestRun] = []
        for path in self.root.iterdir():
            try:
                run_id = UUID(path.name)
            except ValueError:
                continue
            if path.name == str(run_id) and (path / "run.json").is_file():
                runs.append(self.load(run_id))
        return tuple(sorted(runs, key=lambda run: run.started_at, reverse=True))

    def artifact(self, run_id: UUID, filename: str) -> tuple[Artifact, Path]:
        artifact = next(
            (item for item in self.load(run_id).artifacts if item.filename == filename), None
        )
        directory = self.directory(run_id) / "evidence"
        path = directory / filename
        if artifact is None or path.resolve() != path or not path.is_file():
            raise PreflightError("Auto Test evidence is unavailable.")
        if path.stat().st_size != artifact.size:
            raise PreflightError("Auto Test evidence size does not match its receipt.")
        return artifact, path

    def save_poll(self, result: PollResult) -> PollResult:
        write_record(self.root / "poll.json", result)
        return result

    def latest_poll(self) -> PollResult | None:
        path = self.root / "poll.json"
        if not path.exists():
            return None
        try:
            if path.resolve() != path:
                raise ValueError("Unsafe Auto Test poll receipt.")
            return PollResult.model_validate_json(path.read_bytes())
        except (OSError, ValueError) as exc:
            raise PreflightError("Unable to read the Auto Test poll receipt.") from exc

    def stop(self, run_id: UUID) -> None:
        if self.load(run_id).status != "running":
            raise PreflightError("This Auto Test run has already ended.")
        path = self.directory(run_id) / "stop"
        fd = os.open(path, os.O_CREAT | os.O_WRONLY | os.O_NOFOLLOW, 0o600)
        os.close(fd)

    def stopping(self, run_id: UUID) -> bool:
        return (self.directory(run_id) / "stop").exists()

    def reconcile_interrupted(self) -> None:
        """Called only while holding the host worker lock."""
        for run in self.list():
            if run.status == "running":
                self.save(
                    run.model_copy(
                        update={
                            "status": "interrupted",
                            "finished_at": datetime.now(UTC),
                            "detail": "The Auto Test worker exited before completing the run.",
                        }
                    )
                )


@contextmanager
def test_worker_lock(state_root: Path) -> Generator[bool, None, None]:
    folder = state_root / "locks"
    folder.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd = os.open(folder / "auto-test.lock", os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, "a+") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            yield False
            return
        yield True


def write_record(path: Path, record: BaseModel) -> None:
    if path.resolve() != path:
        raise PreflightError("Auto Test receipt path is unsafe.")
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd, temporary = tempfile.mkstemp(dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            stream.write(record.model_dump_json(indent=2))
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        Path(temporary).unlink(missing_ok=True)
