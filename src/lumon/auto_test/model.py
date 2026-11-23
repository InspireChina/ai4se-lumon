"""Validated test plans, observed results, and durable run receipts."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Annotated, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from lumon.auto_test.config import Platform

ISSUE_PATTERN = r"^[A-Z][A-Z0-9_]*-[1-9][0-9]*$"
CaseStatus = Literal["passed", "failed", "review", "blocked", "not_run"]
RunStatus = Literal["running", "completed", "blocked", "stopped", "interrupted"]
Text = Annotated[str, Field(min_length=1, max_length=1200)]


class Record(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class AcceptanceCriterion(Record):
    id: Annotated[str, Field(pattern=r"^AC-[1-9][0-9]*$")]
    quote: Text
    gap: str | None


class Step(Record):
    platform: Platform
    action: Text


class Check(Record):
    platform: Platform
    selector: Annotated[str, Field(min_length=1, max_length=500)]
    operator: Literal["visible", "not_visible", "text_equals", "text_contains"]
    expected: str


class Case(Record):
    title: Text
    ac_refs: tuple[str, ...] = Field(min_length=1, max_length=12)
    primary_platform: Platform
    path: Literal["Happy", "Alternative", "Sad", "Sad(Edge)"]
    preconditions: Text
    steps: tuple[Step, ...] = Field(min_length=1, max_length=12)
    expected: Text
    checks: tuple[Check, ...] = Field(max_length=12)
    backend_repositories: tuple[str, ...] = Field(max_length=12)

    @property
    def platforms(self) -> tuple[Platform, ...]:
        return tuple(dict.fromkeys(step.platform for step in self.steps))


class CasePlan(Record):
    criteria: tuple[AcceptanceCriterion, ...] = Field(max_length=100)
    cases: tuple[Case, ...] = Field(max_length=80)
    gaps: tuple[Text, ...] = Field(max_length=180)


class Revision(Record):
    repository: str
    branch: str
    sha: Annotated[str, Field(pattern=r"^([0-9a-f]{40}|[0-9a-f]{64})$")]


class Artifact(Record):
    filename: Annotated[str, Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,160}$")]
    kind: Literal["workbook", "screenshot", "video"]
    size: int = Field(gt=0)
    case_id: str | None = None
    platform: Platform | None = None
    attempt: int | None = None


class Observation(Record):
    platform: Platform
    expected: str
    actual: str
    matched: bool
    visible: bool
    video_seconds: float
    attempt: int
    recording: str


class CaseResult(Record):
    case_id: str
    status: CaseStatus
    detail: str
    observations: tuple[Observation, ...] = ()
    artifacts: tuple[Artifact, ...] = ()


class JiraReceipt(Record):
    status: Literal["pending", "published", "partial", "failed"] = "pending"
    comment_id: str | None = None
    attachments: dict[str, str] = Field(default_factory=dict)
    uncertain_writes: tuple[str, ...] = ()
    detail: str = ""


class TestRun(Record):
    run_id: UUID
    workspace_id: UUID
    jira_site: str
    issue_key: Annotated[str, Field(pattern=ISSUE_PATTERN)]
    issue_title: str
    model_name: str = ""
    started_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    finished_at: datetime | None = None
    status: RunStatus = "running"
    phase: str = "planning"
    detail: str = ""
    revisions: tuple[Revision, ...] = ()
    builds: dict[str, str] = Field(default_factory=dict)
    plan: CasePlan | None = None
    results: tuple[CaseResult, ...] = ()
    workbook: Artifact | None = None
    jira: JiraReceipt = JiraReceipt()

    @property
    def artifacts(self) -> tuple[Artifact, ...]:
        workbook = (self.workbook,) if self.workbook else ()
        return (*workbook, *(file for case in self.results for file in case.artifacts))


class PollResult(Record):
    status: Literal["disabled", "idle", "busy", "failed", "ran"]
    detail: str = ""
    run_id: UUID | None = None
    checked_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
