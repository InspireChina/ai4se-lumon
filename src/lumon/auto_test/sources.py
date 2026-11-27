"""Capture exact registered branches and expose bounded read-only source context."""

from __future__ import annotations

import asyncio
import os
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from lumon.auto_test.model import Revision
from lumon.auto_test.process import command
from lumon.errors import PreflightError
from lumon.workspace.config import load_workspace_config
from lumon.workspace.layout import WorkspaceLayout
from lumon.workspace.model import RepositoryRecord
from lumon.workspace.repository_worktree import (
    PreparedWorktree,
    prepare_worktree,
    registered_repository,
    remove_read_worktree,
)

_SOURCE_SUFFIXES = {
    ".ts",
    ".tsx",
    ".js",
    ".jsx",
    ".vue",
    ".swift",
    ".kt",
    ".java",
    ".php",
    ".go",
    ".ftl",
    ".py",
    ".rb",
    ".html",
    ".md",
    ".json",
    ".pbxproj",
    ".xcscheme",
    ".xcworkspacedata",
}
_EXCLUDED_PARTS = {".git", "node_modules", "Pods", "vendor", "dist", "build"}
_LOCKFILES = {"package-lock.json", "pnpm-lock.yaml", "yarn.lock", "Podfile.lock"}


@dataclass(frozen=True, slots=True)
class Source:
    repository: RepositoryRecord
    worktree: PreparedWorktree

    @property
    def revision(self) -> Revision:
        return Revision(
            repository=self.repository.name, branch=self.worktree.branch, sha=self.worktree.revision
        )

    @property
    def path(self) -> Path:
        return Path(self.worktree.path)

    async def copy_for_build(self, destination: Path) -> Path:
        # Verify the pinned worktree before permitting a build in the disposable copy.
        head = await command(
            ("git", "rev-parse", "HEAD"), label="Verify build source", cwd=self.path, read=True
        )
        if head.decode().strip() != self.worktree.revision:
            raise PreflightError("The pinned Auto Test source changed before building.")
        status = await command(
            ("git", "status", "--porcelain", "--untracked-files=all"),
            label="Verify build source",
            cwd=self.path,
            read=True,
        )
        if status.strip():
            raise PreflightError("The pinned Auto Test source contains local edits.")
        task = asyncio.create_task(
            asyncio.to_thread(
                shutil.copytree,
                self.path,
                destination,
                symlinks=True,
                ignore=shutil.ignore_patterns(".git"),
            )
        )
        try:
            return await asyncio.shield(task)
        except asyncio.CancelledError:
            await asyncio.gather(task, return_exceptions=True)
            raise


class Sources:
    def __init__(self, workspace: Path, task: str) -> None:
        self.workspace = workspace
        self.task = task
        self.pinned: dict[str, Source] = {}

    def registered(self) -> tuple[RepositoryRecord, ...]:
        return load_workspace_config(
            WorkspaceLayout.from_root(self.workspace).workspace_config
        ).repositories

    @property
    def revisions(self) -> tuple[Revision, ...]:
        return tuple(source.revision for source in self.pinned.values())

    async def pin(self, repository: str) -> Source:
        if repository in self.pinned:
            return self.pinned[repository]
        record = registered_repository(self.workspace, repository)
        task = asyncio.create_task(
            asyncio.to_thread(prepare_worktree, self.workspace, record, self.task, mode="read")
        )
        try:
            prepared = await asyncio.shield(task)
        except asyncio.CancelledError:
            result = (await asyncio.gather(task, return_exceptions=True))[0]
            if isinstance(result, PreparedWorktree):
                self.pinned[repository] = Source(record, result)
            raise
        source = Source(record, prepared)
        if prepared.dirty or prepared.revision != prepared.remote_revision:
            raise PreflightError(f"Unable to pin the latest configured revision of {repository}.")
        self.pinned[repository] = source
        return source

    async def close(self) -> None:
        for source in self.pinned.values():
            try:
                await asyncio.to_thread(
                    remove_read_worktree,
                    self.workspace,
                    source.repository,
                    self.task,
                    source.worktree,
                )
            except PreflightError:
                # The existing helper preserves anything it cannot verify as run-owned.
                continue

    async def context(self, repository: str, action: Literal["list", "read"], query: str) -> str:
        source = self.pinned.get(repository)
        if source is None:
            if not any(record.name == repository for record in self.registered()):
                return "Repository is outside this Workspace."
            try:
                source = await self.pin(repository)
            except PreflightError:
                return "Unable to pin the configured branch of this repository."
        if action == "read":
            return _read_source(source.path, query)
        return _list_sources(source.path, query)


def _source_file(root: Path, name: str, *, listing: bool = False) -> Path | None:
    path = root / name
    if path.resolve() != path or not path.is_file() or root not in path.parents:
        return None
    parts = path.relative_to(root).parts
    if any(part.startswith(".") or part in _EXCLUDED_PARTS for part in parts):
        return None
    if path.name in _LOCKFILES:
        return path if listing else None
    if path.name.casefold() in {"secrets.json", "credentials.json", "config.json"}:
        return None
    if path.suffix not in _SOURCE_SUFFIXES and path.name not in {"Podfile", "Jenkinsfile"}:
        return None
    return path


def _read_source(root: Path, query: str) -> str:
    path = _source_file(root, query)
    if path is None:
        return "Source file is outside the allowed repository files."
    with path.open("rb") as stream:
        return stream.read(12000).decode("utf-8", errors="replace")


def _list_sources(root: Path, query: str) -> str:
    results: list[str] = []
    needle = query.casefold()
    for directory, folders, names in os.walk(root, followlinks=False):
        folders[:] = sorted(
            folder
            for folder in folders
            if not folder.startswith(".") and folder not in _EXCLUDED_PARTS
        )
        for name in sorted(names):
            relative = (Path(directory) / name).relative_to(root).as_posix()
            if (
                needle in relative.casefold()
                and _source_file(root, relative, listing=True) is not None
            ):
                results.append(relative)
                if len(results) == 80:
                    return "\n".join(results)
    return "\n".join(results) or "No matching source paths."
