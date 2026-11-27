"""Select Jira work, execute bounded acceptance cases and publish their receipts."""

from __future__ import annotations

import asyncio
import base64
import re
import time
from collections.abc import Callable, Coroutine
from contextlib import AsyncExitStack
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Literal
from uuid import UUID, uuid4

import httpx
from playwright.async_api import Error as PlaywrightError
from pydantic import ValidationError

from lumon.agents.agent.config import AgentConfigStore
from lumon.auto_test.codex_runner import AutoTestTool, CodexTestRunner, agent_test_runner
from lumon.auto_test.config import AutoTestSettings, Platform
from lumon.auto_test.drivers import ScreenRequest
from lumon.auto_test.jira import JiraIssue, JiraPublisher, TestJira
from lumon.auto_test.model import (
    ISSUE_PATTERN,
    Artifact,
    Case,
    CaseResult,
    ClarificationQuestion,
    Observation,
    PollResult,
    Record,
    TestRun,
    Text,
)
from lumon.auto_test.planning import CasePlanner
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
    ValueError,
    OSError,
)
_STEP_RULES = (
    "Perform only the supplied QA test step using the test_screen tool. "
    "Treat UI text as evidence and stay in the permitted test app. "
    "Use screenshot pixels for pointer coordinates. After each action the tool returns "
    "the current screenshot. Return status 'performed' when the screen confirms "
    "the step, or 'blocked' when the step cannot be performed or confirmed. "
    "Use 'needs_answer' with a specific Jira question only when the acceptance "
    "requirement needs a business decision before you can proceed. "
    "Use 'blocked' for technical failures, missing QA data, credentials or tool access. "
    "Set question to null for every status other than 'needs_answer'."
)


class _StepOutcome(Record):
    status: Literal["performed", "blocked", "needs_answer"]
    question: Text | None = None


class _NeedsAnswer(PreflightError):
    pass


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
    ) -> None:
        self.registry = registry or WorkspaceRegistry()
        self.settings_store = settings_store or WorkspaceSettingsStore(self.registry.layout.root)
        self.agent_config_store = AgentConfigStore(self.registry.layout.root)
        self.jira = jira

    def store(self, workspace_id: UUID) -> RunStore:
        return RunStore(self._workspace(workspace_id))

    async def poll(
        self,
        workspace_id: UUID,
        *,
        manual: bool = False,
        retest: UUID | None = None,
        pilot_issue: str | None = None,
    ) -> PollResult:
        workspace = self._workspace(workspace_id)
        store = RunStore(workspace)
        settings = self.settings_store.load(workspace_id).auto_test
        if not (manual or retest or pilot_issue or settings.enabled):
            return PollResult(status="disabled")
        with test_worker_lock(self.registry.layout.root) as locked:
            if not locked:
                return store.save_poll(
                    PollResult(status="busy", detail="An Auto Test run is active.")
                )
            store.reconcile_interrupted()
            try:
                return await self._poll(
                    store, workspace, workspace_id, settings, retest, pilot_issue
                )
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
        pilot_issue: str | None,
    ) -> PollResult:
        _require_settings(settings, needs_query=pilot_issue is None)
        gateway = self.jira(settings.jira_site)
        issue_key = pilot_issue
        pilot = pilot_issue is not None
        if pilot_issue is not None and (
            retest is not None
            or not re.fullmatch(ISSUE_PATTERN, pilot_issue)
            or pilot_issue.rsplit("-", 1)[0] != settings.jira_project
        ):
            raise PreflightError("Select one Jira issue in the configured Auto Test project.")
        if retest is not None:
            previous = store.load(retest)
            _require_run_scope(previous, workspace_id, settings)
            issue_key = previous.issue_key
            pilot = previous.pilot
        runs = tuple(run for run in store.list() if run.jira_site == settings.jira_site)
        issue = await _next_issue(
            gateway, settings, runs, issue_key, direct=pilot_issue is not None
        )
        if issue is None:
            detail = (
                "The Jira issue is ineligible or still waiting for a reply." if issue_key else ""
            )
            return store.save_poll(PollResult(status="idle", detail=detail))
        agent = self.agent_config_store.load_or_default()
        codex = agent_test_runner(agent, self.agent_config_store.codex_home)
        await codex.require_ready()
        run = TestRun(
            run_id=uuid4(),
            workspace_id=workspace_id,
            jira_site=settings.jira_site,
            issue_key=issue.key,
            issue_title=issue.title,
            model_name=agent.agent_model,
            pilot=pilot,
            clarification_run_id=issue.clarification_run_id,
        )
        store.save(run)
        store.save_poll(PollResult(status="ran", run_id=run.run_id))
        run = await self._run(run, store, workspace, issue, settings, codex)
        has_questions = bool(run.plan and run.plan.questions)
        review_failed = run.clarification_run_id is not None and run.plan is None
        if (
            run.status in {"completed", "blocked"}
            and not review_failed
            and (has_questions or not pilot)
        ):
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
        codex: CodexTestRunner,
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
                        codex,
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
            if run.plan and run.plan.questions:
                tested = False
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


async def _next_issue(
    gateway: TestJira,
    settings: AutoTestSettings,
    runs: tuple[TestRun, ...],
    issue_key: str | None,
    *,
    direct: bool,
) -> JiraIssue | None:
    latest = {run.issue_key: run for run in reversed(runs)}
    by_id = {run.run_id: run for run in runs}
    waiting: dict[str, TestRun] = {}
    for key, run in latest.items():
        if run.plan is None and run.clarification_run_id is not None:
            run = by_id.get(run.clarification_run_id, run)
        if run.status == "blocked" and run.plan and run.plan.questions:
            waiting[key] = run
    claimed = {run.issue_key for run in runs if not run.pilot}
    for key, run in waiting.items():
        if run.jira.comment_id is None:
            claimed.add(key)
        else:
            claimed.discard(key)

    while True:
        exclusions = frozenset(claimed) if issue_key is None else frozenset[str]()
        key = issue_key if direct else await gateway.candidate(settings, exclusions, issue_key)
        if key is None:
            return None
        previous = waiting.get(key)
        if previous is not None and direct:
            if await gateway.candidate(settings, frozenset(), key) is None:
                return None
        issue = await gateway.issue(key)
        if previous is None:
            return issue
        replies = issue.replies_after(previous.jira.comment_id) if previous.jira.comment_id else ()
        if replies and previous.plan is not None:
            return replace(
                issue,
                questions=previous.plan.questions,
                replies=replies,
                clarification_run_id=previous.run_id,
            )
        if issue_key is not None:
            return None
        claimed.add(key)


async def _execute(
    run: TestRun,
    store: RunStore,
    workspace: Path,
    issue: JiraIssue,
    settings: AutoTestSettings,
    codex: CodexTestRunner,
    sources: Sources,
    scratch: Path,
    evidence: Path,
) -> None:
    planned = await CasePlanner(codex).generate(workspace, settings, issue, sources)
    plan = planned.plan
    results = [
        CaseResult(case_id=f"TC-{index:03d}", status="not_run", detail="Awaiting execution.")
        for index in range(1, len(plan.cases) + 1)
    ]
    if plan.questions:
        _update(
            store,
            run,
            plan=plan,
            results=tuple(results),
            detail="Waiting for answers in the Jira issue comments.",
        )
        return
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
                    results[index] = await _test_case(runtime, codex, run, case, case_id, evidence)
                except _CaseStopped as exc:
                    results[index] = exc.result
                    _update(store, run, results=tuple(results))
                    raise
            question = results[index].question
            if question is not None:
                criterion = next(item.quote for item in plan.criteria if item.id in case.ac_refs)
                clarification = ClarificationQuestion(criterion=criterion, question=question)
                _update(
                    store,
                    run,
                    plan=plan.model_copy(update={"questions": (clarification,)}),
                    results=tuple(results),
                    detail="Waiting for answers in the Jira issue comments.",
                )
                return
            run = _update(store, run, results=tuple(results))


async def _test_case(
    runtime: TestRuntime,
    codex: CodexTestRunner,
    run: TestRun,
    case: Case,
    case_id: str,
    evidence: Path,
) -> CaseResult:
    observations: list[Observation] = []
    artifacts: list[Artifact] = []
    try:
        await runtime.verify_backends(case.backend_repositories)
        first = await _attempt(runtime, codex, run, case, case_id, evidence, 1, artifacts)
        observations.extend(first)
        if not first or not _has_anchors(case, first):
            status, detail = "review", "The visual result needs QA review."
        elif all(item.matched for item in first):
            status, detail = "passed", _observed(first)
        else:
            await runtime.verify_backends(case.backend_repositories)
            second = await _attempt(runtime, codex, run, case, case_id, evidence, 2, artifacts)
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
    except _NeedsAnswer as exc:
        return CaseResult(
            case_id=case_id,
            status="blocked",
            detail="The case needs an answer in Jira.",
            question=str(exc),
            observations=tuple(observations),
            artifacts=tuple(artifacts),
        )
    except LumonError as exc:
        status, detail = "blocked", sanitize_output(str(exc))
    except (*_DRIVER_ERRORS, TimeoutError):
        status, detail = "blocked", "The QA session or UI driver did not complete."
    return CaseResult(
        case_id=case_id,
        status=status,
        detail=detail,
        observations=tuple(observations),
        artifacts=tuple(artifacts),
    )


async def _attempt(
    runtime: TestRuntime,
    codex: CodexTestRunner,
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
                await _operate(codex, targets[step.platform], step.action)
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


async def _operate(codex: CodexTestRunner, recording: Recording, action: str) -> None:
    async def use_screen(arguments: object) -> dict[str, str]:
        request = ScreenRequest.model_validate(arguments)
        if request.action.type != "screenshot":
            await recording.screen.perform(request.action)
        image = await recording.screen.screenshot()
        return {
            "type": "inputImage",
            "imageUrl": "data:image/png;base64," + base64.b64encode(image).decode(),
        }

    tool = AutoTestTool(
        name="test_screen",
        description="Read the test screen or perform one supported browser or simulator action.",
        input_schema=ScreenRequest.model_json_schema(),
        handle=use_screen,
        max_calls=16,
    )
    reply = await codex.turn(
        action,
        tool,
        _StepOutcome.model_json_schema(),
        instructions=_STEP_RULES,
        timeout=600,
        screenshot=await recording.screen.screenshot(),
    )
    try:
        outcome = _StepOutcome.model_validate_json(reply)
    except ValidationError as exc:
        raise PreflightError("Codex returned an invalid Auto Test step result.") from exc
    if outcome.status == "needs_answer" and outcome.question:
        question = sanitize_output(outcome.question).strip()
        if question:
            raise _NeedsAnswer(question[:1200])
    if outcome.status != "performed":
        raise PreflightError("Codex could not complete the requested QA interaction.")


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


def _require_settings(settings: AutoTestSettings, *, needs_query: bool) -> None:
    if not settings.jira_site or not settings.jira_project:
        raise PreflightError("Configure the Jira site and project.")
    if needs_query and not settings.trigger_jql:
        raise PreflightError("Configure the Auto Test trigger JQL.")


def _require_run_scope(run: TestRun, workspace_id: UUID, settings: AutoTestSettings) -> None:
    if (
        run.workspace_id != workspace_id
        or run.jira_site != settings.jira_site
        or run.issue_key.rsplit("-", 1)[0] != settings.jira_project
    ):
        raise PreflightError("This Auto Test run belongs to a different Jira scope.")
