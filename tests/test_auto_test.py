"""Auto Test contracts with local Git, a fixture UI, and a fake Jira site."""

from __future__ import annotations

import asyncio
import base64
import json
import re
import struct
import subprocess
import sys
import time
from collections.abc import AsyncGenerator
from contextlib import AsyncExitStack, asynccontextmanager
from dataclasses import replace
from pathlib import Path
from typing import Literal
from uuid import UUID, uuid4
from zipfile import ZipFile

import httpx
import httpx2
import pytest
from fastapi.testclient import TestClient
from openai import AsyncOpenAI
from openai.lib.computer import ComputerActionInput
from openai.types.responses.computer_action import Click

from lumon.auto_test import service as auto_test
from lumon.auto_test.config import AutoTestSettings, IosTarget, Platform, WebTarget
from lumon.auto_test.drivers import IosScreen, Screen
from lumon.auto_test.jira import JiraError, JiraIssue
from lumon.auto_test.jira import TestJira as Jira
from lumon.auto_test.model import AcceptanceCriterion, Case, CasePlan, Check, Step
from lumon.auto_test.planning import CasePlanner, PlannedTest, validate_plan
from lumon.auto_test.process import owned_process
from lumon.auto_test.runtime import Recording
from lumon.auto_test.runtime import TestRuntime as Runtime
from lumon.auto_test.sources import Sources
from lumon.auto_test.targets import IosChoice, WebChoice, web_target
from lumon.dashboard.routes import create_app
from lumon.dashboard.service import DashboardService
from lumon.errors import PreflightError
from lumon.workspace.config import WorkspaceConfig
from lumon.workspace.manifest import WorkspaceManifest
from lumon.workspace.model import RepositoryRecord
from lumon.workspace.registry import WorkspaceRegistry

_REQUIREMENT = "A signed-in QA user can save a draft."
_JIRA = "qa-example.atlassian.net"
_KEY = "QA-7"
_QA_JQL = (
    'project = QA\nAND sprint in openSprints()\nAND status = "Ready For QA"\n'
    "AND Flagged = Impediment\nORDER BY Rank ASC"
)
_PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+jN1sAAAAASUVORK5CYII="
)


class FakeJira(Jira):
    def __init__(self) -> None:
        super().__init__(_JIRA)
        self.eligible = [_KEY]
        self.queries: list[str] = []
        self.files: list[dict[str, object]] = []
        self.posts: dict[str, str] = {}
        self.comment_writes: list[str] = []
        self.uploads: list[str] = []
        self.upload_failure: Literal["rejected", "unconfirmed"] | None = None

    async def _call(self, *arguments: str, timeout: float = 30) -> dict[str, object]:
        del timeout
        match arguments:
            case ("query", "--jql", query, "--fields", "summary", "--first", limit):
                self.queries.append(query)
                retest = re.search(r"AND key = (\w+-\d+)", query)
                exclude = re.search(r"AND key NOT IN \(([^)]+)\)", query)
                excluded = exclude.group(1).split(", ") if exclude else []
                issues = [
                    {"key": key}
                    for key in self.eligible
                    if key not in excluded and (retest is None or retest.group(1) == key)
                ]
                return {"data": {"issues": issues[: int(limit)]}}
            case ("get", key, "--full"):
                return {
                    "data": [{"key": key, "summary": "Save draft", "description": _REQUIREMENT}]
                }
            case ("attachment", "query", "--issue-id", "QA-7"):
                return {"data": list(self.files)}
            case ("attachment", "upload", "--issue-id", "QA-7", "--file", raw_path):
                path = Path(raw_path)
                self.uploads.append(path.name)
                failure = self.upload_failure if path.suffix == ".webm" else None
                if failure:
                    self.upload_failure = None
                if failure == "rejected":
                    raise JiraError(
                        "Jira rejected the fixture attachment (HTTP 413).", rejected=True
                    )
                self.files.append(
                    {
                        "id": str(len(self.files) + 1),
                        "filename": path.name,
                        "size": path.stat().st_size,
                    }
                )
                if failure == "unconfirmed":
                    raise JiraError("The fixture connection closed before returning the response.")
                return {}
            case ("comment", "query", *_):
                comments = [{"id": key, "body": body} for key, body in self.posts.items()]
                return {"data": comments}
            case (
                "comment",
                "create",
                "--issue-id",
                "QA-7",
                "--body-format",
                "markdown",
                "--body",
                body,
            ):
                self.comment_writes.append("create")
                self.posts[str(len(self.posts) + 100)] = body
                return {}
            case (
                "comment",
                "update",
                "--id",
                comment_id,
                "--issue-id",
                "QA-7",
                "--body-format",
                "markdown",
                "--body",
                body,
            ):
                self.comment_writes.append(comment_id)
                self.posts[comment_id] = body
                return {}
            case _:
                raise AssertionError(arguments)


class FixtureScreen(Screen):
    async def require_target(self) -> None:
        pass

    async def capture(self) -> bytes:
        return _PNG

    async def apply(self, action: ComputerActionInput) -> None:
        raise AssertionError(action)

    async def element(self, selector: str, *, read_text: bool) -> tuple[bool, str]:
        return True, {"#saved": "Saved", "draft-status": "Draft", "#absent": ""}[selector]

    async def setup_step(self, step: object, value: str) -> None:
        raise AssertionError((step, value))


def _git(directory: Path, *arguments: str) -> str:
    return subprocess.run(
        ("git", "-C", str(directory), *arguments), capture_output=True, text=True, check=True
    ).stdout.strip()


@pytest.fixture
def workspace(tmp_path: Path) -> tuple[WorkspaceRegistry, UUID, Path, Path, str]:
    root, remote = tmp_path / "workspace", tmp_path / "upstream"
    (root / "lumon").mkdir(parents=True)
    (root / "repos").mkdir()
    remote.mkdir()
    _git(remote, "init", "-q", "-b", "main")
    _git(remote, "config", "user.name", "Fixture")
    _git(remote, "config", "user.email", "fixture@example.invalid")
    (remote / "index.html").write_text("installed revision")
    _git(remote, "add", "index.html")
    _git(remote, "commit", "-qm", "initial")
    _git(root, "clone", "-q", remote.as_uri(), "repos/app")
    record = RepositoryRecord("app", remote.as_uri(), "repos/app", "main")
    manifest = WorkspaceManifest.create("test", "1.0.0")
    manifest.write(root / "lumon" / "manifest.json")
    WorkspaceConfig("test", (record,)).write(root / "lumon" / "workspace.toml")
    registry = WorkspaceRegistry(tmp_path / "profile")
    registry.register(root)
    (remote / "index.html").write_text("latest registered branch")
    _git(remote, "commit", "-qam", "advance")
    return registry, manifest.workspace_id, root, remote, _git(remote, "rev-parse", "HEAD")


def _settings(*, enabled: bool = True) -> AutoTestSettings:
    return AutoTestSettings(
        enabled=enabled,
        jira_site=_JIRA,
        jira_project="QA",
        trigger_jql=_QA_JQL,
        web=WebTarget(repository="app", output="dist"),
        ios=IosTarget(
            repository="app",
            project="App.xcodeproj",
            scheme="Fixture",
            app="Fixture.app",
            bundle_id="com.example.fixture",
            device_type="com.apple.CoreSimulator.SimDeviceType.iPhone-18",
            runtime="com.apple.CoreSimulator.SimRuntime.iOS-27-0",
        ),
    )


def _case(
    title: str,
    platform: Platform = "web",
    *,
    selector: str = "#saved",
    checked: bool = True,
    backends: tuple[str, ...] = (),
) -> Case:
    assertion = Check(
        platform=platform, selector=selector, operator="text_equals", expected="Saved"
    )
    return Case(
        title=title,
        ac_refs=("AC-1",),
        primary_platform=platform,
        path="Happy",
        preconditions="Signed in with a QA account.",
        steps=(Step(platform=platform, action="Save the QA draft."),),
        expected="Saved status is visible.",
        checks=(assertion,) if checked else (),
        backend_repositories=backends,
    )


def _plan(*cases: Case) -> CasePlan:
    return CasePlan(
        criteria=(AcceptanceCriterion(id="AC-1", quote=_REQUIREMENT, gap=None),),
        cases=cases,
        gaps=(),
    )


def _service(
    registry: WorkspaceRegistry, workspace_id: UUID, jira: FakeJira
) -> auto_test.AutoTestService:
    service = auto_test.AutoTestService(
        registry, jira=lambda site: jira, openai=lambda: AsyncOpenAI(api_key="fixture")
    )
    settings = service.settings_store.load(workspace_id)
    service.settings_store.save(replace(settings, auto_test=_settings()))
    return service


def _fixture_builds(monkeypatch: pytest.MonkeyPatch, plan: CasePlan) -> list[tuple[Platform, str]]:
    builds: list[tuple[Platform, str]] = []

    async def generate(self: CasePlanner, *args: object) -> PlannedTest:
        return PlannedTest(plan=plan, web=None, ios=None)

    async def build(
        self: Runtime, platform: Platform, *, choice: WebChoice | IosChoice | None = None
    ) -> str:
        source = await self.sources.pin("app")
        copy = await source.copy_for_build(self.directory / platform)
        builds.append((platform, (copy / "index.html").read_text()))
        return source.revision.sha

    monkeypatch.setattr(CasePlanner, "generate", generate)
    monkeypatch.setattr(Runtime, "build", build)
    return builds


def test_agent_discovers_web_build_from_pinned_source_without_a_profile(
    workspace: tuple[WorkspaceRegistry, UUID, Path, Path, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    registry, workspace_id, root, remote, _ = workspace
    (remote / "package.json").write_text(json.dumps({"scripts": {"build:uat": "fixture-build"}}))
    (remote / "yarn.lock").write_text("# yarn lockfile v1\n")
    (remote / ".nvmrc").write_text("16\n")
    (remote / ".env").write_text("SYNTHETIC_TEST_VALUE=never-send")
    _git(remote, "add", ".")
    _git(remote, "commit", "-qm", "register UAT build")
    head = _git(remote, "rev-parse", "HEAD")

    choice = WebChoice(
        repository="app",
        directory=".",
        script="build:uat",
        output="dist",
        base_path="/uat",
        start_path="/uat/",
    )
    response_plan = PlannedTest(plan=_plan(_case("Check QA draft")), web=choice, ios=None)
    model_requests = 0

    def model_response(request: httpx2.Request) -> httpx2.Response:
        nonlocal model_requests
        model_requests += 1
        body = request.content.decode()
        assert "registered_repositories" in body and "main" in body
        assert "never-send" not in body
        output: list[dict[str, object]]
        if model_requests == 1:
            output = [
                {
                    "type": "function_call",
                    "id": "fc_build",
                    "call_id": "call_build",
                    "name": "source_context",
                    "status": "completed",
                    "arguments": json.dumps(
                        {"repository": "app", "action": "read", "query": "package.json"}
                    ),
                },
                {
                    "type": "function_call",
                    "id": "fc_manifest",
                    "call_id": "call_manifest",
                    "name": "source_context",
                    "status": "completed",
                    "arguments": json.dumps(
                        {"repository": "app", "action": "read", "query": ".env"}
                    ),
                },
            ]
        else:
            assert "fixture-build" in body
            assert "outside the allowed repository files" in body
            output = [
                {
                    "id": "msg_build",
                    "type": "message",
                    "role": "assistant",
                    "status": "completed",
                    "content": [
                        {
                            "type": "output_text",
                            "text": response_plan.model_dump_json(),
                            "annotations": [],
                        }
                    ],
                }
            ]
        return httpx2.Response(200, json={"id": f"resp_{model_requests}", "output": output})

    commands: list[tuple[str, ...]] = []
    yarn = ("npx", "--yes", "--package=node@16", "--package=yarn@1.22.22", "--", "yarn")

    async def build_step(arguments: tuple[str, ...], *, cwd: Path, **options: object) -> bytes:
        del options
        assert cwd != remote and cwd != root / "repos/app"
        commands.append(arguments)
        if arguments == (*yarn, "run", "build:uat"):
            (cwd / "dist").mkdir()
            (cwd / "dist/index.html").write_text("built from pinned source")
        return b""

    async def build_web(self: Runtime, target: WebTarget, checkout: Path, sha: str) -> str:
        assert target.start_path == "/uat/"
        assert (checkout / target.output / "index.html").is_file()
        return sha

    async def operate(*args: object) -> None:
        pass

    monkeypatch.setattr("lumon.auto_test.runtime.command", build_step)
    monkeypatch.setattr(Runtime, "_build_web", build_web)
    monkeypatch.setattr(Runtime, "record", _record)
    monkeypatch.setattr(auto_test, "_operate", operate)
    jira = FakeJira()
    service = auto_test.AutoTestService(
        registry,
        jira=lambda site: jira,
        openai=lambda: AsyncOpenAI(
            api_key="fixture",
            http_client=httpx2.AsyncClient(transport=httpx2.MockTransport(model_response)),
        ),
    )
    saved = service.settings_store.load(workspace_id)
    settings = _settings().model_copy(update={"web": None, "ios": None})
    service.settings_store.save(replace(saved, auto_test=settings))
    polled = asyncio.run(service.poll(workspace_id))
    assert polled.run_id is not None
    run = service.store(workspace_id).load(polled.run_id)
    assert run.results[0].status == "passed"
    assert run.builds == {"web": head} and run.revisions[0].sha == head
    assert commands == [(*yarn, "install", "--frozen-lockfile"), (*yarn, "run", "build:uat")]
    assert model_requests == 2
    assert not (root / "repos/app/dist").exists()


def test_ios_build_uses_shared_qa_scheme_and_worker_simulator(
    workspace: tuple[WorkspaceRegistry, UUID, Path, Path, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _, _, root, remote, _ = workspace
    ios = remote / "ios"
    scheme = ios / "App.xcodeproj/xcshareddata/xcschemes/FixtureQA.xcscheme"
    scheme.parent.mkdir(parents=True)
    scheme.write_text("<Scheme/>")
    (ios / "Podfile").touch()
    (ios / "Podfile.lock").touch()
    (remote / "config.uat.js").write_text("QA config")
    _git(remote, "add", ".")
    _git(remote, "commit", "-qm", "add shared QA scheme")
    commands: list[tuple[str, ...]] = []

    async def simulators(*args: object, **options: object) -> bytes:
        del args, options
        iphone = "com.apple.CoreSimulator.SimDeviceType.iPhone-18"
        return json.dumps(
            {
                "devices": {
                    "com.apple.CoreSimulator.SimRuntime.iOS-27-0": [
                        {
                            "isAvailable": True,
                            "deviceTypeIdentifier": iphone,
                        },
                    ]
                }
            }
        ).encode()

    async def build_step(arguments: tuple[str, ...], **options: object) -> bytes:
        del options
        commands.append(arguments)
        return b""

    async def build_ios(self: Runtime, target: IosTarget, checkout: Path, sha: str) -> str:
        assert target.scheme == "FixtureQA" and (checkout / target.project).is_dir()
        assert target.device_type.endswith("iPhone-18") and target.runtime.endswith("iOS-27-0")
        return sha

    monkeypatch.setattr("lumon.auto_test.targets.command", simulators)
    monkeypatch.setattr("lumon.auto_test.runtime.command", build_step)
    monkeypatch.setattr(Runtime, "_build_ios", build_ios)
    choice = IosChoice(
        repository="app",
        project="ios/App.xcodeproj",
        scheme="FixtureQA",
        configuration="Debug",
        app="Fixture.app",
        bundle_id="com.example.fixture",
        qa_config_file="config.uat.js",
    )

    async def run() -> str:
        sources = Sources(root, "test-ios-discovery")
        try:
            async with AsyncExitStack() as stack:
                runtime = Runtime(AutoTestSettings(), sources, root / "builds", stack)
                return await runtime.build("ios", choice=choice)
        finally:
            await sources.close()

    assert asyncio.run(run()) == _git(remote, "rev-parse", "HEAD")
    assert commands == [
        ("cp", "./config.uat.js", "./config.js"),
        ("pod", "install", "--deployment", "--project-directory=ios"),
    ]


@pytest.mark.parametrize("script", ["deploy:uat", "build:publish", "sh -c arbitrary"])
def test_agent_cannot_select_non_build_commands(tmp_path: Path, script: str) -> None:
    (tmp_path / "package.json").write_text(json.dumps({"scripts": {script: "unsafe-command"}}))
    choice = WebChoice(
        repository="app",
        directory=".",
        script=script,
        output="dist",
        base_path="/",
        start_path="/",
    )
    with pytest.raises(PreflightError, match="declared Web build script"):
        web_target(choice, tmp_path)


@asynccontextmanager
async def _record(self: Runtime, platform: Platform, path: Path) -> AsyncGenerator[Recording]:
    try:
        yield Recording(FixtureScreen(), path, time.monotonic())
    finally:
        path.write_bytes(b"fixture recording" * 20)


def test_poll_pins_latest_branch_and_tests_once_until_an_eligible_retest(
    workspace: tuple[WorkspaceRegistry, UUID, Path, Path, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    registry, workspace_id, root, _, head = workspace
    jira = FakeJira()
    service = _service(registry, workspace_id, jira)
    builds = _fixture_builds(
        monkeypatch,
        _plan(
            _case("=1+1"),
            _case("Repeatable iOS issue", "ios", selector="draft-status"),
            _case("Visual outcome", checked=False),
            _case("Needs a deployment probe", backends=("backend",)),
        ),
    )
    monkeypatch.setattr(Runtime, "record", _record)

    async def operate(*args: object) -> None:
        pass

    monkeypatch.setattr(auto_test, "_operate", operate)

    async def run() -> None:
        first = await service.poll(workspace_id)
        assert first.status == "ran" and first.run_id is not None
        result = service.store(workspace_id).load(first.run_id)
        assert result.revisions[0].sha == head
        assert result.revisions[0].branch == "main"
        assert (root / "repos" / "app" / "index.html").read_text() == "installed revision"
        assert builds == [("web", "latest registered branch"), ("ios", "latest registered branch")]
        assert [case.status for case in result.results] == ["passed", "failed", "review", "blocked"]
        assert [(file.kind, file.attempt) for file in result.results[1].artifacts] == [
            ("video", 1),
            ("screenshot", 1),
            ("video", 2),
            ("screenshot", 2),
        ]
        assert not result.results[3].artifacts
        assert result.jira.status == "published"
        assert all(not name.endswith(".png") for name in jira.uploads)
        assert jira.comment_writes == ["create"]
        assert "Repeatable iOS issue" in jira.posts["100"] and "Saved" in jira.posts["100"]
        assert result.plan is not None
        assert result.workbook is not None
        _, path = service.store(workspace_id).artifact(result.run_id, result.workbook.filename)
        with ZipFile(path) as book:
            assert b"A1:O5" in book.read("xl/worksheets/sheet1.xml")
            assert b"<f>" not in book.read("xl/worksheets/sheet1.xml")
            assert b"=1+1" in book.read("xl/sharedStrings.xml")
            assert b"Evidence" in book.read("xl/workbook.xml")
            assert head.encode() in book.read("xl/sharedStrings.xml")
        dashboard = DashboardService(registry=registry, state_root=registry.layout.root)
        prefix = f"/api/workspaces/{workspace_id}/auto-test/runs/{result.run_id}/artifacts"
        with TestClient(create_app(dashboard)) as client:
            workbook = client.get(f"{prefix}/{result.workbook.filename}/preview")
            assert workbook.status_code == 200
            cases, evidence = workbook.json()
            assert cases["name"] == "Test Cases" and evidence["name"] == "Evidence"
            assert cases["rows"][1][5:8] == ["=1+1", result.plan.cases[0].preconditions, ""]
            assert head in evidence["rows"][1][4]

            video, screenshot = result.results[0].artifacts
            png = client.get(f"{prefix}/{screenshot.filename}?inline=true")
            assert png.status_code == 200 and png.content == _PNG
            assert png.headers["content-type"] == "image/png"
            assert png.headers["content-disposition"].startswith("inline")
            download = client.get(f"{prefix}/{screenshot.filename}")
            assert download.headers["content-disposition"].startswith("attachment")
            segment = client.get(
                f"{prefix}/{video.filename}?inline=true", headers={"Range": "bytes=0-19"}
            )
            assert segment.status_code == 206 and len(segment.content) == 20
            assert segment.headers["content-type"] == "video/webm"
            assert client.get(f"{prefix}/{video.filename}/preview").status_code == 409
            assert client.get(f"{prefix}/unregistered.png?inline=true").status_code == 409
        assert (await service.poll(workspace_id)).status == "idle"
        assert (await service.poll(workspace_id, manual=True)).status == "idle"
        jira.eligible = []
        assert (
            await service.poll(workspace_id, manual=True, retest=result.run_id)
        ).status == "idle"
        jira.eligible = [_KEY]
        retry = await service.poll(workspace_id, manual=True, retest=result.run_id)
        assert retry.status == "ran" and retry.run_id != result.run_id
        assert len(jira.posts) == 2
        assert all(
            'status = "Ready For QA"' in q and "Flagged = Impediment" in q for q in jira.queries
        )
        assert jira.queries[-1].endswith("AND key = QA-7 ORDER BY Rank ASC")

    asyncio.run(run())


@pytest.mark.parametrize("failure", ["rejected", "unconfirmed"])
def test_retry_jira_publication_reuses_the_comment_and_reconciles_uploads(
    workspace: tuple[WorkspaceRegistry, UUID, Path, Path, str],
    monkeypatch: pytest.MonkeyPatch,
    failure: Literal["rejected", "unconfirmed"],
) -> None:
    registry, workspace_id, _, _, _ = workspace
    jira = FakeJira()
    jira.upload_failure = failure
    service = _service(registry, workspace_id, jira)
    _fixture_builds(monkeypatch, _plan(_case("Save draft")))
    monkeypatch.setattr(Runtime, "record", _record)

    async def operate(*args: object) -> None:
        pass

    monkeypatch.setattr(auto_test, "_operate", operate)

    async def run() -> None:
        poll = await service.poll(workspace_id)
        assert poll.run_id is not None
        first = service.store(workspace_id).load(poll.run_id)
        assert first.jira.status == "partial" and first.jira.comment_id == "100"
        assert "available in Lumon" in jira.posts["100"]
        retried = await service.retry_publication(workspace_id, first.run_id)
        assert retried.jira.status == "published"
        assert retried.jira.comment_id == "100"
        assert jira.comment_writes == ["create", "100"]
        assert len(jira.files) == 2
        assert len(jira.uploads) == (3 if failure == "rejected" else 2)
        assert "/attachment/content/" in jira.posts["100"]
        await service.retry_publication(workspace_id, first.run_id)
        assert jira.comment_writes == ["create", "100"]

    asyncio.run(run())


def test_stop_finalizes_the_current_recording_and_cleans_owned_processes(
    workspace: tuple[WorkspaceRegistry, UUID, Path, Path, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    registry, workspace_id, root, _, _ = workspace
    jira = FakeJira()
    service = _service(registry, workspace_id, jira)
    builds = _fixture_builds(monkeypatch, _plan(_case("Stop during the Web case")))
    reached = asyncio.Event()
    children: list[asyncio.subprocess.Process] = []

    @asynccontextmanager
    async def record(self: Runtime, platform: Platform, path: Path) -> AsyncGenerator[Recording]:
        async with _record(self, platform, path) as recording:
            async with owned_process((sys.executable, "-c", "import time; time.sleep(60)")) as proc:
                children.append(proc)
                reached.set()
                yield recording

    async def operate(*args: object) -> None:
        await asyncio.sleep(60)

    monkeypatch.setattr(Runtime, "record", record)
    monkeypatch.setattr(auto_test, "_operate", operate)

    async def run() -> None:
        pending = asyncio.create_task(service.poll(workspace_id))
        await asyncio.wait_for(reached.wait(), 10)
        store = service.store(workspace_id)
        active = store.list()[0]
        assert active.status == "running"
        assert (await service.poll(workspace_id, manual=True)).status == "busy"
        store.stop(active.run_id)
        await asyncio.wait_for(pending, 10)
        completed = store.load(active.run_id)
        assert completed.status == "stopped" and completed.results[0].status == "not_run"
        assert {item.kind for item in completed.artifacts} == {"workbook", "video"}
        assert builds == [("web", "latest registered branch")]
        assert not jira.posts
        assert not tuple(store.directory(active.run_id).glob("build-*"))
        assert len(_git(root / "repos" / "app", "worktree", "list").splitlines()) == 1

    asyncio.run(run())
    assert len(children) == 1 and children[0].returncode is not None


def test_queue_excludes_only_previously_claimed_candidates_in_the_active_query() -> None:
    jira = FakeJira()
    claimed = frozenset(f"QA-{index}" for index in range(10_000, 11_000))
    jira.eligible = [*sorted(claimed)[:50], _KEY]
    assert asyncio.run(jira.candidate(_settings(), claimed)) == _KEY
    assert len(jira.queries) == 2
    assert "key NOT IN" not in jira.queries[0]
    assert sorted(claimed)[0] in jira.queries[1]
    assert sorted(claimed)[-1] not in jira.queries[1]
    assert all("openSprints()" in query and "ORDER BY Rank ASC" in query for query in jira.queries)


def test_case_plan_requires_jira_criteria_and_a_nonempty_text_check() -> None:
    plan = _plan(_case("Save draft"))
    with pytest.raises(PreflightError, match="unsupported by Jira"):
        validate_plan(plan, JiraIssue(_KEY, "Save draft", "Unrelated text"))
    empty = plan.cases[0].checks[0].model_copy(update={"expected": ""})
    invalid = plan.cases[0].model_copy(update={"checks": (empty,)})
    with pytest.raises(PreflightError, match="expected value"):
        validate_plan(_plan(invalid), JiraIssue(_KEY, "Save draft", _REQUIREMENT))


def test_ios_assertion_and_pixel_mapping_use_the_appium_session() -> None:
    command_log: list[tuple[str, object]] = []
    png = b"\x89PNG\r\n\x1a\n" + bytes(8) + struct.pack(">II", 900, 1800)
    response_values: dict[str, object] = {
        "/execute/sync": {"bundleId": "com.example.fixture"},
        "/screenshot": base64.b64encode(png).decode(),
        "/window/rect": {"width": 300, "height": 600},
        "/elements": [{"element-6066-11e4-a52e-4f735466cecf": "status-id"}],
        "/element/status-id/displayed": True,
        "/element/status-id/text": "Saved",
    }

    def respond(request: httpx.Request) -> httpx.Response:
        path = request.url.path.removeprefix("/session/fixture")
        payload: object = json.loads(request.content) if request.content else None
        command_log.append((path, payload))
        return httpx.Response(200, json={"value": response_values.get(path)})

    async def run() -> None:
        async with httpx.AsyncClient(
            base_url="http://127.0.0.1:11000", transport=httpx.MockTransport(respond)
        ) as client:
            screen = IosScreen(client, "fixture", "com.example.fixture")
            await screen.screenshot()
            await screen.perform(Click(type="click", button="left", x=450, y=1200))
            check = _case("Save draft", "ios", selector="draft-status").checks[0]
            assert await screen.observe(check) == (True, True, "Saved")
            response_values["/execute/sync"] = {"bundleId": "other.app"}
            with pytest.raises(PreflightError, match="foreground"):
                await screen.perform(Click(type="click", button="left", x=450, y=1200))

    asyncio.run(run())
    actions = [payload for path, payload in command_log if path == "/actions"]
    assert len(actions) == 1
    assert '"x": 150, "y": 400' in json.dumps(actions[0])


def test_dashboard_settings_are_workspace_scoped_and_poll_is_off(
    workspace: tuple[WorkspaceRegistry, UUID, Path, Path, str],
) -> None:
    registry, workspace_id, _, _, _ = workspace
    dashboard = DashboardService(registry=registry, state_root=registry.layout.root)
    client = TestClient(create_app(dashboard))
    prefix = f"/api/workspaces/{workspace_id}/auto-test"
    initial = client.get(prefix + "/settings")
    assert initial.status_code == 200
    assert initial.json()["enabled"] is False
    assert initial.json()["schedule_expression"] == "*/5 * * * *"
    body = _settings(enabled=False).model_dump(mode="json")
    body["qa_origins"] = ["https://qa.example.test"]
    external = client.put(
        prefix + "/settings", headers={"Origin": "https://example.net"}, json=body
    )
    assert external.status_code == 403
    updated = client.put(prefix + "/settings", json=body)
    assert updated.status_code == 200 and updated.json()["trigger_jql"] == _QA_JQL
    for internal in ("web", "ios", "qa_origins", "backends"):
        assert internal not in updated.json()
    assert client.put(prefix + "/settings", json={"model": "example-model"}).status_code == 200
    jira = {
        "jira_site": "qa-example.atlassian.net",
        "jira_project": "QA",
        "trigger_jql": "project = QA",
    }
    jira_saved = client.put(prefix + "/settings", json=jira)
    assert jira_saved.status_code == 200
    assert jira_saved.json() == {**updated.json(), "model": "example-model", **jira}
    stored = auto_test.AutoTestService(registry).settings_store.load(workspace_id).auto_test
    assert stored.trigger_jql == "project = QA" and stored.model == "example-model"
    assert stored.web == _settings().web and stored.ios == _settings().ios
    assert stored.qa_origins == ("https://qa.example.test",)
    assert client.get(f"/api/workspaces/{uuid4()}/auto-test/settings").status_code == 404
    assert client.get(prefix + "/runs").json() == {"runs": [], "poll": None, "starting": False}
    assert asyncio.run(auto_test.AutoTestService(registry).poll(workspace_id)).status == "disabled"
