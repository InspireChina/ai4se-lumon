# pyright: reportUnusedFunction=false
"""Dashboard controls for the Workspace Auto Test worker and its evidence."""

from __future__ import annotations

import asyncio
import os
import sys
from collections.abc import Callable
from dataclasses import replace
from datetime import datetime
from uuid import UUID

from fastapi import APIRouter, Request, Response
from fastapi.responses import FileResponse

from lumon.auto_test.config import AutoTestControls, AutoTestSettings
from lumon.auto_test.model import (
    Artifact,
    CaseStatus,
    JiraReceipt,
    PollResult,
    Record,
    RunStatus,
    TestRun,
)
from lumon.auto_test.scheduler import AutoTestScheduler
from lumon.auto_test.service import AutoTestService
from lumon.auto_test.workbook import WorkbookSheet, read_workbook_preview
from lumon.errors import InvalidInputError, LumonError, PreflightError, WorkspaceNotFoundError
from lumon.workspace.settings import validate_auto_test_settings


class AutoTestLaunch(Record):
    retest: UUID | None = None


class RunSummary(Record):
    run_id: UUID
    issue_key: str
    issue_title: str
    jira_site: str
    started_at: datetime
    status: RunStatus
    phase: str
    detail: str
    counts: dict[CaseStatus, int]
    artifacts: tuple[Artifact, ...]
    jira: JiraReceipt
    stopping: bool
    publishable: bool


class AutoTestHistory(Record):
    runs: tuple[RunSummary, ...]
    poll: PollResult | None
    starting: bool


def add_auto_test_routes(
    router: APIRouter,
    service: AutoTestService,
    check_origin: Callable[[Request], None],
) -> None:
    path = "/workspaces/{workspace_id}/auto-test"
    scheduler = AutoTestScheduler(service.registry.layout.root)
    children: set[UUID] = set()
    waits: set[asyncio.Task[int]] = set()

    @router.get(path + "/settings", response_model=AutoTestControls)
    def settings(workspace_id: UUID) -> AutoTestControls:
        service.store(workspace_id)
        return service.settings_store.load(workspace_id).auto_test

    @router.put(path + "/settings", response_model=AutoTestControls)
    async def update_settings(
        request: Request, workspace_id: UUID, payload: AutoTestSettings
    ) -> AutoTestControls:
        check_origin(request)
        registration = service.registry.find(workspace_id)
        if registration is None:
            raise WorkspaceNotFoundError("The Auto Test Workspace is not registered.")
        current = service.settings_store.load(workspace_id)
        changes = {field: getattr(payload, field) for field in payload.model_fields_set}
        effective = current.auto_test.model_copy(update=changes)
        if current.jira is not None and (
            effective.jira_site != current.jira.site
            or effective.jira_project != current.jira.project
        ):
            raise InvalidInputError("Change the shared Jira site and project in the Jira panel.")
        validate_auto_test_settings(effective)
        if effective.enabled and any(
            getattr(current.auto_test, field) != getattr(effective, field)
            for field in ("enabled", "jira_site", "jira_project", "trigger_jql")
        ):
            await service.jira(effective.jira_site).candidate(effective, frozenset())
            latest = service.settings_store.load(workspace_id)
            if latest.auto_test != current.auto_test:
                raise PreflightError("Auto Test settings changed. Reload before saving again.")
            current = latest
        snapshot = service.settings_store.raw_snapshot(workspace_id)
        try:
            service.settings_store.save(replace(current, auto_test=effective))
            if (
                current.auto_test.enabled != effective.enabled
                or current.auto_test.schedule_expression != effective.schedule_expression
            ):
                scheduler.apply(workspace_id, registration.path, effective)
        except LumonError:
            service.settings_store.restore_raw(workspace_id, snapshot)
            raise
        return effective

    @router.get(path + "/runs")
    def history(workspace_id: UUID) -> AutoTestHistory:
        store = service.store(workspace_id)
        return AutoTestHistory(
            runs=tuple(
                _summary(run, store.stopping(run.run_id))
                for run in service.history(workspace_id)[:30]
            ),
            poll=store.latest_poll(),
            starting=workspace_id in children,
        )

    @router.post(path + "/runs", status_code=202)
    async def launch(
        request: Request, workspace_id: UUID, payload: AutoTestLaunch
    ) -> dict[str, bool]:
        check_origin(request)
        store = service.store(workspace_id)
        if workspace_id in children:
            raise PreflightError("An Auto Test process is already running for this Workspace.")
        if payload.retest is not None:
            store.load(payload.retest)
        args = (
            sys.executable,
            "-m",
            "lumon",
            "test",
            "poll",
            "--workspace-id",
            str(workspace_id),
            "--manual",
            "--json",
        )
        if payload.retest is not None:
            args = (*args, "--retest", str(payload.retest))
        children.add(workspace_id)
        try:
            process = await asyncio.create_subprocess_exec(
                *args,
                env=dict(os.environ, LUMON_HOME=str(service.registry.layout.root)),
                start_new_session=True,
                stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
            )
        except OSError as exc:
            children.discard(workspace_id)
            raise PreflightError("Could not start the local Auto Test worker.") from exc
        except asyncio.CancelledError:
            children.discard(workspace_id)
            raise
        task = asyncio.create_task(process.wait())
        waits.add(task)

        def retire(done: asyncio.Task[int]) -> None:
            children.discard(workspace_id)
            waits.discard(done)

        task.add_done_callback(retire)
        return {"accepted": True}

    @router.post(path + "/runs/{run_id}/stop", status_code=202)
    def stop(request: Request, workspace_id: UUID, run_id: UUID) -> dict[str, bool]:
        check_origin(request)
        service.store(workspace_id).stop(run_id)
        return {"accepted": True}

    @router.post(path + "/runs/{run_id}/publish")
    async def publish(request: Request, workspace_id: UUID, run_id: UUID) -> JiraReceipt:
        check_origin(request)
        run = await service.retry_publication(workspace_id, run_id)
        return run.jira

    evidence = path + "/runs/{run_id}/artifacts/{filename}"

    @router.get(evidence)
    def artifact(
        workspace_id: UUID, run_id: UUID, filename: str, inline: bool = False
    ) -> FileResponse:
        file, location = service.store(workspace_id).artifact(run_id, filename)
        if file.kind == "workbook":
            media = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
        elif file.kind == "screenshot":
            media = "image/png"
        else:
            media = "video/webm" if location.suffix == ".webm" else "video/mp4"
        return FileResponse(
            location,
            filename=file.filename,
            media_type=media,
            content_disposition_type="inline"
            if inline and file.kind != "workbook"
            else "attachment",
            headers={"Cache-Control": "no-store", "X-Content-Type-Options": "nosniff"},
        )

    @router.get(evidence + "/preview")
    def preview(
        workspace_id: UUID, run_id: UUID, filename: str, response: Response
    ) -> tuple[WorkbookSheet, ...]:
        file, location = service.store(workspace_id).artifact(run_id, filename)
        if file.kind != "workbook":
            raise PreflightError("The evidence file is not an Auto Test workbook.")
        response.headers["Cache-Control"] = "no-store"
        return read_workbook_preview(location)


def _summary(run: TestRun, stopping: bool) -> RunSummary:
    counts: dict[CaseStatus, int] = {}
    for result in run.results:
        counts[result.status] = counts.get(result.status, 0) + 1
    return RunSummary(
        run_id=run.run_id,
        issue_key=run.issue_key,
        issue_title=run.issue_title,
        jira_site=run.jira_site,
        started_at=run.started_at,
        status=run.status,
        phase=run.phase,
        detail=run.detail,
        counts=counts,
        artifacts=run.artifacts,
        jira=run.jira,
        stopping=stopping,
        publishable=run.status in {"completed", "blocked"},
    )
