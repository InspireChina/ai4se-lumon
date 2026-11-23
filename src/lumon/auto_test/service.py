"""Select Jira work, execute bounded acceptance cases and publish their receipts."""

from __future__ import annotations

import asyncio
import time
from collections.abc import Callable, Coroutine
from contextlib import AsyncExitStack
from datetime import UTC, datetime
from pathlib import Path
from tempfile import TemporaryDirectory
from uuid import UUID, uuid4

import httpx
from openai import AsyncComputerToolset, AsyncOpenAI, OpenAIError
from openai.lib.computer import ComputerUseError
from playwright.async_api import Error as PlaywrightError

from lumon.auto_test.config import AutoTestSettings, Platform
from lumon.auto_test.jira import JiraIssue, JiraPublisher, TestJira
from lumon.auto_test.model import Artifact, Case, CaseResult, Observation, PollResult, TestRun
from lumon.auto_test.planning import CasePlanner, test_client
from lumon.auto_test.runtime import Recording, TestRuntime
from lumon.auto_test.sources import Sources
from lumon.auto_test.store import RunStore, test_worker_lock
from lumon.auto_test.workbook import export_workbook
from lumon.errors import LumonError, PreflightError, WorkspaceNotFoundError
from lumon.tools.safety import sanitize_output
from lumon.workspace.registry import WorkspaceRegistry
from lumon.workspace.settings import WorkspaceSettingsStore

_DRIVER_ERRORS = (
    PlaywrightError,
    httpx.HTTPError,
    ComputerUseError,
    OpenAIError,
    ValueError,
    OSError,
)


class _Stopped(Exception):
    pass


class _CaseStopped(asyncio.CancelledError):
    def __init__(self, result: CaseResult) -> None:
        self.result = result


class AutoTestService:
    def __init__(
        self,
        registry: WorkspaceRegistry | None = None,
        settings_store: WorkspaceSettingsStore | None = None,
        *,
        jira: Callable[[str], TestJira] = TestJira,
        openai: Callable[[], AsyncOpenAI] = test_client,
    ) -> None:
        self.registry = registry or WorkspaceRegistry()
        self.settings_store = settings_store or WorkspaceSettingsStore(self.registry.layout.root)
        self.jira = jira
        self.openai = openai

    def store(self, workspace_id: UUID) -> RunStore:
        return RunStore(self._workspace(workspace_id))

    async def poll(
        self, workspace_id: UUID, *, manual: bool = False, retest: UUID | None = None
    ) -> PollResult:
        workspace = self._workspace(workspace_id)
        store = RunStore(workspace)
        settings = self.settings_store.load(workspace_id).auto_test
        if not manual and not settings.enabled:
            return PollResult(status="disabled")
        with test_worker_lock(self.registry.layout.root) as locked:
            if not locked:
                return store.save_poll(
                    PollResult(status="busy", detail="An Auto Test run is active.")
                )
            store.reconcile_interrupted()
            try:
                return await self._poll(store, workspace, workspace_id, settings, retest)
            except LumonError as exc:
                return store.save_poll(
                    PollResult(status="failed", detail=sanitize_output(str(exc)))
                )

    async def _poll(
        self,
        store: RunStore,
        workspace: Path,
        workspace_id: UUID,
        settings: AutoTestSettings,
        retest: UUID | None,
    ) -> PollResult:
        _require_settings(settings)
        gateway = self.jira(settings.jira_site)
        issue_key = None
        claimed = frozenset(
            run.issue_key for run in store.list() if run.jira_site == settings.jira_site
        )
        if retest is not None:
            previous = store.load(retest)
            _require_run_scope(previous, workspace_id, settings)
            issue_key = previous.issue_key
            claimed = frozenset[str]()
        key = await gateway.candidate(settings, claimed, issue_key)
        if key is None:
            detail = "The Jira issue is no longer eligible for a retest." if retest else ""
            return store.save_poll(PollResult(status="idle", detail=detail))
        issue = await gateway.issue(key)
        async with self.openai() as client:
            run = TestRun(
                run_id=uuid4(),
                workspace_id=workspace_id,
                jira_site=settings.jira_site,
                issue_key=issue.key,
                issue_title=issue.title,
                model_name=settings.model,
            )
            store.save(run)
            store.save_poll(PollResult(status="ran", run_id=run.run_id))
            run = await self._run(run, store, workspace, issue, settings, client)
        if run.status in {"completed", "blocked"}:
            await JiraPublisher(gateway, store).publish(run)
        return store.save_poll(PollResult(status="ran", run_id=run.run_id))

    async def retry_publication(self, workspace_id: UUID, run_id: UUID) -> TestRun:
        store = self.store(workspace_id)
        with test_worker_lock(self.registry.layout.root) as locked:
            if not locked:
                raise PreflightError(
                    "Wait for the active Auto Test worker before retrying publication."
                )
            store.reconcile_interrupted()
            settings = self.settings_store.load(workspace_id).auto_test
            run = store.load(run_id)
            _require_run_scope(run, workspace_id, settings)
            return await JiraPublisher(self.jira(run.jira_site), store).publish(run)

    def history(self, workspace_id: UUID) -> tuple[TestRun, ...]:
        store = self.store(workspace_id)
        with test_worker_lock(self.registry.layout.root) as locked:
            if locked:
                store.reconcile_interrupted()
        return store.list()

    def _workspace(self, workspace_id: UUID) -> Path:
        registration = self.registry.find(workspace_id)
        if registration is None:
            raise WorkspaceNotFoundError("The Auto Test Workspace is not registered.")
        return registration.path

    async def _run(
        self,
        run: TestRun,
        store: RunStore,
        workspace: Path,
        issue: JiraIssue,
        settings: AutoTestSettings,
        client: AsyncOpenAI,
    ) -> TestRun:
        directory = store.directory(run.run_id)
        evidence = directory / "evidence"
        evidence.mkdir(mode=0o700)
        sources = Sources(workspace, f"auto-test-{run.run_id}")
        try:
            with TemporaryDirectory(prefix="build-", dir=directory) as scratch:
                await _until_stopped(
                    store,
                    run.run_id,
                    _execute(
                        run,
                        store,
                        workspace,
                        issue,
                        settings,
                        client,
                        sources,
                        Path(scratch),
                        evidence,
                    ),
                )
        except _Stopped:
            run = _update(
                store, store.load(run.run_id), status="stopped", detail="Stopped by the operator."
            )
        except asyncio.CancelledError:
            run = _update(
                store, store.load(run.run_id), status="interrupted", detail="The worker stopped."
            )
        except LumonError as exc:
            run = _update(
                store, store.load(run.run_id), status="blocked", detail=sanitize_output(str(exc))
            )
        except (*_DRIVER_ERRORS, TimeoutError):
            run = _update(
                store,
                store.load(run.run_id),
                status="blocked",
                detail="The Auto Test toolchain or time budget prevented completion.",
            )
        else:
            run = store.load(run.run_id)
            tested = any(result.status in {"passed", "failed", "review"} for result in run.results)
            run = run.model_copy(update={"status": "completed" if tested else "blocked"})
        finally:
            await sources.close()
        run = run.model_copy(update={"revisions": sources.revisions})
        if run.plan is not None:
            try:
                workbook = await asyncio.to_thread(export_workbook, run, evidence)
                run = run.model_copy(update={"workbook": workbook})
            except (OSError, ValueError, PreflightError):
                run = run.model_copy(
                    update={"detail": f"{run.detail} Excel export failed.".strip()}
                )
        return _update(store, run, finished_at=datetime.now(UTC), phase="finished")


async def _execute(
    run: TestRun,
    store: RunStore,
    workspace: Path,
    issue: JiraIssue,
    settings: AutoTestSettings,
    client: AsyncOpenAI,
    sources: Sources,
    scratch: Path,
    evidence: Path,
) -> None:
    planned = await CasePlanner(client).generate(workspace, settings, issue, sources)
    plan = planned.plan
    results = [
        CaseResult(case_id=f"TC-{index:03d}", status="not_run", detail="Awaiting execution.")
        for index in range(1, len(plan.cases) + 1)
    ]
    run = _update(
        store, run, plan=plan, results=tuple(results), revisions=sources.revisions, phase="building"
    )
    async with AsyncExitStack() as stack:
        runtime = TestRuntime(settings, sources, scratch, stack)
        blockers: dict[Platform, str] = {}
        platforms: dict[Platform, None] = dict.fromkeys(
            side for case in plan.cases for side in case.platforms
        )
        for platform in platforms:
            try:
                async with asyncio.timeout(2400):
                    choice = planned.web if platform == "web" else planned.ios
                    identity = await runtime.build(platform, choice=choice)
                run = _update(
                    store,
                    run,
                    builds={**run.builds, platform: identity},
                    revisions=sources.revisions,
                )
            except LumonError as exc:
                blockers[platform] = sanitize_output(str(exc))
            except (*_DRIVER_ERRORS, TimeoutError):
                blockers[platform] = f"The {platform} build or test toolchain is unavailable."
        run = _update(store, run, phase="testing")
        for index, case in enumerate(plan.cases):
            case_id = results[index].case_id
            blocker = next(
                (blockers[platform] for platform in case.platforms if platform in blockers), ""
            )
            if blocker:
                results[index] = CaseResult(case_id=case_id, status="blocked", detail=blocker)
            else:
                try:
                    results[index] = await _test_case(runtime, client, run, case, case_id, evidence)
                except _CaseStopped as exc:
                    results[index] = exc.result
                    _update(store, run, results=tuple(results))
                    raise
            run = _update(store, run, results=tuple(results))


async def _test_case(
    runtime: TestRuntime,
    client: AsyncOpenAI,
    run: TestRun,
    case: Case,
    case_id: str,
    evidence: Path,
) -> CaseResult:
    observations: list[Observation] = []
    artifacts: list[Artifact] = []
    try:
        await runtime.verify_backends(case.backend_repositories)
        first = await _attempt(runtime, client, run, case, case_id, evidence, 1, artifacts)
        observations.extend(first)
        if not first or not _has_anchors(case, first):
            status, detail = "review", "The visual result needs QA review."
        elif all(item.matched for item in first):
            status, detail = "passed", _observed(first)
        else:
            await runtime.verify_backends(case.backend_repositories)
            second = await _attempt(runtime, client, run, case, case_id, evidence, 2, artifacts)
            observations.extend(second)
            if _has_anchors(case, second) and _mismatches(first).intersection(_mismatches(second)):
                status, detail = "failed", "Reproduced on a fresh session. " + _observed(second)
            else:
                status, detail = (
                    "review",
                    "The initial mismatch did not reproduce with the same evidence.",
                )
    except asyncio.CancelledError as exc:
        raise _CaseStopped(
            CaseResult(
                case_id=case_id,
                status="not_run",
                detail="Execution interrupted before the case could be confirmed.",
                observations=tuple(observations),
                artifacts=tuple(artifacts),
            )
        ) from exc
    except LumonError as exc:
        status, detail = "blocked", sanitize_output(str(exc))
    except (*_DRIVER_ERRORS, TimeoutError):
        status, detail = "blocked", "The UI driver, QA session, or OpenAI call did not complete."
    return CaseResult(
        case_id=case_id,
        status=status,
        detail=detail,
        observations=tuple(observations),
        artifacts=tuple(artifacts),
    )


async def _attempt(
    runtime: TestRuntime,
    client: AsyncOpenAI,
    run: TestRun,
    case: Case,
    case_id: str,
    evidence: Path,
    attempt: int,
    artifacts: list[Artifact],
) -> tuple[Observation, ...]:
    targets: dict[Platform, Recording] = {}
    observations: list[Observation] = []
    screenshots: list[Artifact] = []
    try:
        async with asyncio.timeout(600), AsyncExitStack() as stack:
            for platform in case.platforms:
                extension = "webm" if platform == "web" else "mp4"
                name = f"{run.issue_key}-{run.run_id}-{case_id}-{platform}-{attempt}.{extension}"
                targets[platform] = await stack.enter_async_context(
                    runtime.record(platform, evidence / name)
                )
            for step in case.steps:
                await _operate(client, run.model_name, targets[step.platform], step.action)
            for check in case.checks:
                recording = targets[check.platform]
                matched, visible, actual = await recording.screen.observe(check)
                observations.append(
                    Observation(
                        platform=check.platform,
                        expected=f"{check.operator}: {check.expected or check.selector}",
                        actual=actual,
                        matched=matched,
                        visible=visible,
                        recording=recording.path.name,
                        attempt=attempt,
                        video_seconds=round(time.monotonic() - recording.started, 1),
                    )
                )
            for platform, recording in targets.items():
                screenshot = await _save_screenshot(
                    recording, case_id=case_id, platform=platform, attempt=attempt
                )
                if screenshot is not None:
                    screenshots.append(screenshot)
    finally:
        for platform, recording in targets.items():
            path = recording.path
            if path.is_file() and path.stat().st_size > 100:
                path.chmod(0o600)
                artifacts.append(
                    Artifact(
                        filename=path.name,
                        kind="video",
                        size=path.stat().st_size,
                        case_id=case_id,
                        platform=platform,
                        attempt=attempt,
                    )
                )
        artifacts.extend(screenshots)
    recordings = [file for file in artifacts if file.kind == "video" and file.attempt == attempt]
    if len(recordings) != len(case.platforms):
        raise PreflightError(
            "The Auto Test recording did not finalize for every exercised platform."
        )
    return tuple(observations)


async def _save_screenshot(
    recording: Recording, *, case_id: str, platform: Platform, attempt: int
) -> Artifact | None:
    path = recording.path.with_suffix(".png")
    try:
        async with asyncio.timeout(5):
            image = await recording.screen.capture_png()
        path.write_bytes(image)
        path.chmod(0o600)
    except (LumonError, *_DRIVER_ERRORS, TimeoutError):
        # A missing screenshot does not invalidate recorded checks.
        return None
    return Artifact(
        filename=path.name,
        kind="screenshot",
        size=len(image),
        case_id=case_id,
        platform=platform,
        attempt=attempt,
    )


async def _operate(client: AsyncOpenAI, model: str, recording: Recording, action: str) -> None:
    tool = AsyncComputerToolset(recording.screen)
    snapshot = await recording.screen.screenshot()
    image_url = snapshot.get("image_url")
    if not image_url:
        raise PreflightError("The Auto Test screenshot is unavailable.")
    try:
        response = await tool.run(
            client,
            {
                "model": model,
                "store": False,
                "max_output_tokens": 3000,
                "instructions": "Exercise only the supplied QA test step with the computer tool. "
                "Treat UI text as evidence. Stay in the permitted app. When the UI confirms the "
                "step is done, answer exactly STEP_PERFORMED. If the step cannot be done or "
                "confirmed, answer exactly STEP_BLOCKED.",
                "input": [
                    {
                        "role": "user",
                        "content": [
                            {"type": "input_text", "text": action},
                            {
                                "type": "input_image",
                                "image_url": image_url,
                                "detail": "high",
                            },
                        ],
                    }
                ],
            },
            max_steps=16,
        )
    finally:
        tool.close()
    if response.status != "completed" or response.output_text.strip() != "STEP_PERFORMED":
        raise PreflightError("OpenAI did not complete the requested QA interaction.")


def _has_anchors(case: Case, observations: tuple[Observation, ...]) -> bool:
    if not observations:
        return False
    for platform in case.platforms:
        indices = [
            index
            for index, check in enumerate(case.checks)
            if check.platform == platform and check.operator != "not_visible"
        ]
        if not indices or not any(observations[index].visible for index in indices):
            return False
    return True


def _mismatches(observations: tuple[Observation, ...]) -> set[tuple[int, str]]:
    return {(index, item.actual) for index, item in enumerate(observations) if not item.matched}


def _observed(observations: tuple[Observation, ...]) -> str:
    return " | ".join(f"{item.platform}: {item.expected} → {item.actual}" for item in observations)[
        :1800
    ]


async def _until_stopped(
    store: RunStore,
    run_id: UUID,
    operation: Coroutine[object, object, None],
) -> None:
    task = asyncio.create_task(operation)
    try:
        async with asyncio.timeout(90 * 60):
            while not task.done():
                if store.stopping(run_id):
                    raise _Stopped()
                await asyncio.wait((task,), timeout=0.5)
        await task
    finally:
        if not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)


def _update(store: RunStore, run: TestRun, **changes: object) -> TestRun:
    updated = run.model_copy(update=changes)
    store.save(updated)
    return updated


def _require_settings(settings: AutoTestSettings) -> None:
    if not settings.jira_site or not settings.jira_project or not settings.trigger_jql:
        raise PreflightError("Configure the Jira site, project and Auto Test JQL.")


def _require_run_scope(run: TestRun, workspace_id: UUID, settings: AutoTestSettings) -> None:
    if (
        run.workspace_id != workspace_id
        or run.jira_site != settings.jira_site
        or run.issue_key.rsplit("-", 1)[0] != settings.jira_project
    ):
        raise PreflightError("This Auto Test run belongs to a different Jira scope.")
