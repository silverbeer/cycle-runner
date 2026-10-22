"""The two documents the stages pass between them: Snapshot (facts) and Report (findings).

Both are versioned. A Snapshot holds only what the sources said, plus how
complete each source's answer was; it never holds a judgment. A Report holds
findings derived from one Snapshot, each with its rule id, evidence and the
reason for its severity. docs/scrum-master.md documents both schemas.
"""

from datetime import date, datetime
from enum import StrEnum
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

SNAPSHOT_SCHEMA = 1
REPORT_SCHEMA = 1

CLOSED_STATE_TYPES = frozenset({"completed", "canceled", "duplicate"})


class Model(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


# --- snapshot ----------------------------------------------------------------------


class SourceStatus(Model):
    """How one source's collection went. complete=False means some of its data is missing."""

    name: Literal["linear", "github", "git"]
    ok: bool  # False: the source failed outright; nothing from it is in the snapshot
    complete: bool
    error: str | None = None
    notes: list[str] = Field(default_factory=list)  # what is partial or unavailable, and why


class Issue(Model):
    identifier: str
    title: str
    url: str
    state_name: str
    state_type: str  # triage, backlog, unstarted, started, completed, canceled, duplicate
    priority: int  # Linear: 0 none, 1 urgent, 2 high, 3 medium, 4 low
    priority_label: str
    estimate: float | None = None
    assignee: str | None = None
    labels: list[str] = Field(default_factory=list)
    created_at: datetime
    updated_at: datetime  # Linear's "any change" timestamp: the only activity signal available
    started_at: datetime | None = None
    completed_at: datetime | None = None
    canceled_at: datetime | None = None
    due_date: date | None = None
    added_to_cycle_at: datetime | None = None
    cycle_number: int | None = None
    project_id: str | None = None
    parent: str | None = None  # identifier
    children: list[str] | None = Field(default_factory=list)  # None: truncated, so counts can't be trusted
    blocked_by: list[str] = Field(default_factory=list)  # open issues with a "blocks" relation to this one

    @property
    def is_open(self) -> bool:
        return self.state_type not in CLOSED_STATE_TYPES

    @property
    def is_done(self) -> bool:
        return self.state_type == "completed"


class Cycle(Model):
    number: int
    name: str | None = None
    starts_at: datetime
    ends_at: datetime
    issues: list[str]  # identifiers, every state
    carried_in: list[str] | None = None  # the previous cycle's uncompleted-on-close; None if unknown
    previous_number: int | None = None


class Project(Model):
    """A Linear project. In this workspace, projects are the epics."""

    id: str
    name: str
    url: str
    status_name: str
    status_type: str  # backlog, planned, started, paused, completed, canceled
    lead: str | None = None
    target_date: date | None = None
    issues: list[str] | None = None  # identifiers, every state; None if not complete


class PullRequest(Model):
    repo: str
    number: int
    title: str
    url: str
    author: str | None = None
    created_at: datetime
    updated_at: datetime
    is_draft: bool
    review_decision: str | None = None  # APPROVED, CHANGES_REQUESTED, REVIEW_REQUIRED, or None
    review_requests: list[str] = Field(default_factory=list)
    mergeable: str | None = None  # MERGEABLE, CONFLICTING, UNKNOWN
    checks: str | None = None  # SUCCESS, FAILURE, ERROR, PENDING, EXPECTED, or None (no checks)
    last_commit_at: datetime | None = None
    head_ref: str
    base_ref: str
    behind_by: int | None = None  # commits on base not in head; None if it couldn't be established
    linear_ids: list[str] = Field(default_factory=list)


class AssociatedPR(Model):
    number: int
    state: str  # OPEN, CLOSED, MERGED
    url: str
    merged_at: datetime | None = None


class RemoteBranch(Model):
    repo: str
    name: str
    last_commit_at: datetime | None = None
    prs: list[AssociatedPR] = Field(default_factory=list)
    ahead_of_default: int | None = None  # only looked up for branches without a PR


class Repo(Model):
    name: str  # owner/name
    default_branch: str | None = None


class LocalBranch(Model):
    name: str
    last_commit_at: datetime | None = None
    upstream: str | None = None
    upstream_gone: bool = False
    merged: bool | None = None  # into origin/<default> as of the last fetch


class Worktree(Model):
    path: str
    branch: str | None = None  # None: detached HEAD
    is_main: bool
    dirty: bool | None = None  # None: couldn't be checked
    prunable: bool = False


class LocalRepo(Model):
    repo: str
    path: str
    default_branch: str | None = None
    last_fetched_at: datetime | None = None
    branches: list[LocalBranch] = Field(default_factory=list)
    worktrees: list[Worktree] = Field(default_factory=list)


class Scope(Model):
    team: str
    repos: list[str]
    cycle: int | None = None  # None: the active cycle


class Snapshot(Model):
    schema_version: int = SNAPSHOT_SCHEMA
    collected_at: datetime
    scope: Scope
    sources: list[SourceStatus]
    team_name: str | None = None
    timezone: str
    cycle: Cycle | None = None
    issues: list[Issue] = Field(default_factory=list)
    projects: list[Project] = Field(default_factory=list)
    repos: list[Repo] = Field(default_factory=list)
    pull_requests: list[PullRequest] = Field(default_factory=list)
    remote_branches: list[RemoteBranch] = Field(default_factory=list)
    local_repos: list[LocalRepo] = Field(default_factory=list)


# --- report ------------------------------------------------------------------------


class Severity(StrEnum):
    critical = "critical"
    high = "high"
    medium = "medium"
    low = "low"


SEVERITY_RANK = {Severity.critical: 0, Severity.high: 1, Severity.medium: 2, Severity.low: 3}


class Finding(Model):
    rule: str  # e.g. ISSUE-BLOCKED; documented in docs/scrum-master.md
    severity: Severity
    severity_reason: str  # why this severity, in words
    section: Literal["cycle", "aging", "blockers", "epics", "prs", "branches"]
    subject: str  # SB-123, silverbeer/repo#12, silverbeer/repo:branch, ...
    title: str
    url: str | None = None
    source_priority: str | None = None  # Linear's own priority, shown beside ours, never replaced
    evidence: list[str]
    action: str
    group: str | None = None  # branches: the repository (and checkout) they belong to
    # Ordering only: Linear priority (1 urgent .. 4 low, 5 none) and the subject's age in days.
    rank_priority: int = 5
    rank_age: int = 0


class CycleHealth(Model):
    number: int
    name: str | None
    starts_on: date
    ends_on: date  # the last day of the cycle
    days_elapsed: int
    days_remaining: int
    total: int  # canceled and duplicate excluded
    excluded: int
    completed: int
    completion_by_count: int  # percent
    in_progress: int
    unstarted: int
    blocked: int
    aging_or_inactive: int
    estimates_present: int
    points_total: float | None  # None when estimates aren't consistent enough to sum honestly
    points_completed: float | None
    added_after_start: list[str]
    carried_in: list[str] | None
    carryover_risk: list[str]


class AgingRow(Model):
    identifier: str
    title: str
    url: str
    state_name: str
    state_type: str
    priority_label: str
    assignee: str | None
    age_days: int
    age_bucket: int | None  # the largest threshold reached, e.g. 14 for 14+ days
    inactive_days: int
    in_progress_days: int | None
    in_cycle: bool


class EpicRow(Model):
    kind: Literal["project", "parent"]
    subject: str  # project name, or parent identifier
    title: str
    url: str
    status: str
    owner: str | None
    complete_data: bool  # False: the child list may be incomplete, so counts are hidden
    children: int | None
    done: int | None
    open: int | None
    unassigned: list[str]
    blocked: list[str]
    aged: list[str]
    outside_cycle: list[str]
    in_progress: int | None


class PRRow(Model):
    subject: str
    title: str
    url: str
    author: str | None
    age_days: int
    inactive_days: int
    is_draft: bool
    review: str
    checks: str
    mergeable: str
    behind_by: int | None
    linear_ids: list[str]


class Report(Model):
    schema_version: int = REPORT_SCHEMA
    generated_at: datetime
    collected_at: datetime
    timezone: str
    scope: Scope
    complete: bool  # False: some source failed or returned partial data; see warnings
    sources: list[SourceStatus]
    cycle: CycleHealth | None
    top_actions: list[Finding]
    findings: list[Finding]
    aging: list[AgingRow]
    epics: list[EpicRow]
    pull_requests: list[PRRow]
    warnings: list[str]
