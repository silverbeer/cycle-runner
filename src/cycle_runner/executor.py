"""Executing approved work requests: the executor interface, the runner, and a CLI.

    uv run python -m cycle_runner.executor            # run the next pending request, then exit
    uv run python -m cycle_runner.executor list
    uv run python -m cycle_runner.executor release WR-000001
    uv run python -m cycle_runner.executor abandon WR-000001 --reason "..."
    uv run python -m cycle_runner.executor backfill-project WR-000001 MT
    uv run python -m cycle_runner.executor review WR-000002    # what a run left for a human to inspect

    # a real coding agent, on one named request (reads the issue from Linear):
    op run --env-file .env -- uv run python -m cycle_runner.executor run \
        --request WR-000001 --executor claude --max-turns 40 --max-budget-usd 3

An Executor does the work for one request, inside a workspace it is given,
and says how it went. It knows nothing about claiming, statuses, timestamps
or projects. The runner owns the lifecycle and assembles the context: it
claims a request, has a TaskSource assemble the task (for the Claude
executor, the full issue from Linear: issue_context.py) and a
WorkspaceResolver turn it into an ExecutionWorkspace (projects.py: a fresh
clone, set up), and only then calls the executor. If either fails, the
request fails to start and the executor never runs. Executors: FakeExecutor
and ClaudeCodeExecutor. The workspace is kept, and its path recorded in the
result, for inspection.

This module knows work requests and executors only: no ADK, Telegram, Linear
or model. The database is the source of truth for execution state.

Crashes (no leases in V0.9, by design):
- before claim: the request stays pending, and the next run picks it up.
- after claim, before the executor ran: it stays claimed and runs skip it.
  `release` puts it back to pending; that's safe because nothing ran.
- while running: it stays running and runs skip it. It's never retried
  automatically, because work may have partly happened. `abandon` marks it
  failed for a human to look at.
An exception from the executor isn't a crash: it's recorded as failed.
"""

import argparse
import json
import logging
import os
import socket
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal, Protocol

from pydantic import BaseModel, Field

from cycle_runner.work_requests import InvalidTransition, Outcome, WorkRequest, WorkRequestStore, open_store

log = logging.getLogger(__name__)


class ExecutionResult(BaseModel):
    """What an execution produced.

    changed:   the work was done and verified, and files changed.
    no_change: the work was checked and verified, and nothing needed changing
               (e.g. it was already done). A success, not a failure.
    failed:    anything else, including a report that can't be trusted.
    """

    outcome: Outcome
    message: str  # human-readable; this is what the store records
    # Workspace-relative files the executor itself saw change (not the agent's word).
    files_changed: list[str] = Field(default_factory=list)
    # Anything structured the executor wants to report (cost, tests, ...); written
    # to WR-xxxxxx.json beside the workspace.
    details: dict[str, Any] = Field(default_factory=dict)


@dataclass(frozen=True)
class Delivery:
    """A changed result, delivered locally: a branch and a commit in the workspace."""

    branch: str
    commit_sha: str
    diff: dict[str, Any]  # files changed/added/deleted, insertions, deletions
    left_uncommitted: tuple[str, ...] = ()  # what was in the workspace but not committed, and why


class DeliveryError(Exception):
    """A changed result couldn't be delivered safely; nothing was committed."""


class Deliverer(Protocol):
    def deliver(self, request: WorkRequest, workspace: ExecutionWorkspace, result: ExecutionResult) -> Delivery: ...


@dataclass(frozen=True)
class ExecutionWorkspace:
    """Where an executor works and how the work is checked; resolved by the runner.

    path: the only directory the work may read and change.
    test_command: the exact command that checks the work.
    readable: extra paths the checks may read (e.g. the test interpreter).
    test_success_pattern: optional regex the test output must match for the
        work to count as passing (e.g. pytest's "N passed" summary line).
    """

    path: Path
    test_command: str
    readable: tuple[Path, ...] = ()
    test_success_pattern: str | None = None


@dataclass(frozen=True)
class ExecutionTask:
    """What to do, assembled by the runner: the approval plus the issue it's for.

    The executor gets this instead of the WorkRequest, so it never needs the
    store, Linear or anything else upstream. description is the issue text as
    written, and it's untrusted input: it informs the work, it doesn't grant
    anything.
    """

    work_request_id: str
    issue_id: str
    project_id: str | None
    title: str
    description: str
    rationale: str


class WorkspaceError(Exception):
    """No workspace can be made for a request (no project, unknown project, clone or setup failed)."""


class TaskContextError(Exception):
    """The task's context (e.g. the issue from Linear) can't be assembled."""


class WorkspaceResolver(Protocol):
    def resolve(self, request: WorkRequest) -> ExecutionWorkspace: ...


class TaskSource(Protocol):
    def task_for(self, request: WorkRequest) -> ExecutionTask: ...


def task_from_approval(request: WorkRequest) -> ExecutionTask:
    """A task from what was approved alone (no issue description). For executors that don't need one."""
    return ExecutionTask(
        work_request_id=request.work_request_id,
        issue_id=request.issue_id,
        project_id=request.project_id,
        title=request.title_at_approval,
        description="",
        rationale=request.rationale,
    )


class ApprovalOnly:
    """A TaskSource that uses only what was approved; no Linear."""

    def task_for(self, request: WorkRequest) -> ExecutionTask:
        return task_from_approval(request)


class Executor(Protocol):
    name: str

    def execute(self, task: ExecutionTask, workspace: ExecutionWorkspace) -> ExecutionResult:
        """Do the task inside the given workspace (the request is claimed and running)."""
        ...


def worker_id(executor: Executor) -> str:
    """Who holds a claim: which executor, on which host, in which process."""
    return f"{executor.name}@{socket.gethostname()}:{os.getpid()}"


def run_next(
    store: WorkRequestStore, executor: Executor, resolver: WorkspaceResolver, source: TaskSource | None = None,
    deliverer: Deliverer | None = None,
) -> WorkRequest | None:
    """Claim the oldest pending request and run it. None if nothing is pending.

    source assembles the task; the default uses only what was approved.
    deliverer turns a changed result into a local branch and commit; without
    one, a changed result stays uncommitted in the workspace.
    """
    claimed = store.claim_next(worker_id(executor))
    if claimed is None:
        log.info("no pending work requests")
        return None
    return _run_claimed(store, executor, resolver, source or ApprovalOnly(), claimed, deliverer)


def run_request(
    store: WorkRequestStore, executor: Executor, resolver: WorkspaceResolver, work_request_id: str,
    source: TaskSource | None = None, deliverer: Deliverer | None = None,
) -> tuple[WorkRequest, bool]:
    """Run one specific request, if it's pending.

    Returns the request as it now is, and whether this call executed it.
    Anything that isn't pending (already claimed, running, completed or
    failed) is returned untouched: a request never executes twice.
    """
    claimed = store.claim(work_request_id, worker_id(executor))
    if claimed is None:
        current = store.get(work_request_id)
        if current is None:
            raise InvalidTransition(f"{work_request_id} does not exist")
        log.info("%s is %s; not executing it", work_request_id, current.status)
        return current, False
    return _run_claimed(store, executor, resolver, source or ApprovalOnly(), claimed, deliverer), True


def _run_claimed(
    store: WorkRequestStore, executor: Executor, resolver: WorkspaceResolver, source: TaskSource,
    claimed: WorkRequest, deliverer: Deliverer | None = None,
) -> WorkRequest:
    wr = claimed.work_request_id
    log.info("%s claimed by %s (issue %s, project %s)", wr, claimed.claimed_by, claimed.issue_id, claimed.project_id)
    # The runner assembles the context: the task (the issue, from upstream) and a
    # ready workspace. If either can't be had, the executor never runs.
    try:
        task = source.task_for(claimed)
        workspace = resolver.resolve(claimed)
    except (TaskContextError, WorkspaceError) as exc:
        failed = store.fail_to_start(wr, str(exc))
        log.warning("%s not started: %s", wr, exc)
        return failed
    log.info("%s workspace %s", wr, workspace.path)
    running = store.start(wr)
    try:
        result = executor.execute(task, workspace)
    except Exception as exc:  # the executor failed; a crash (SystemExit, a killed process) isn't caught
        log.exception("%s: executor %s raised", wr, executor.name)
        result = ExecutionResult(outcome="failed", message=f"{executor.name} raised {type(exc).__name__}: {exc}")
    # Only a changed result is delivered: no_change and failed never touch git.
    delivery = None
    if result.outcome == "changed" and deliverer is not None:
        try:
            delivery = deliverer.deliver(claimed, workspace, result)
        except DeliveryError as exc:
            log.warning("%s: local delivery refused: %s", wr, exc)
            result = result.model_copy(update={
                "outcome": "failed",
                "message": f"Local delivery refused, nothing committed: {exc}. The agent's work: {result.message}",
                "details": result.details | {"delivery_error": str(exc)},
            })
        else:
            result = result.model_copy(update={
                "message": f"{result.message} Committed {delivery.commit_sha[:12]} on local branch {delivery.branch} "
                           f"({_diff_line(delivery.diff)}); not pushed, awaiting human review.",
            })
    # Record where the work is, so it can be inspected (it's never deleted here),
    # and the details in a file beside it (the store keeps the message).
    _write_details(workspace, wr, task, result, delivery)
    finished = store.finish(
        wr, result.outcome, f"{result.message} [workspace: {workspace.path}]",
        branch=delivery.branch if delivery else None, commit_sha=delivery.commit_sha if delivery else None,
    )
    log.info("%s %s (%s): %s", wr, finished.status, finished.outcome, finished.result_message)
    return finished


def _diff_line(diff: dict[str, Any]) -> str:
    return (f"{len(diff.get('files', []))} files, +{diff.get('insertions', 0)} "
            f"-{diff.get('deletions', 0)}")


def details_path(workspace: ExecutionWorkspace) -> Path:
    """Where a run's details go: beside the workspace (WR-000001 -> WR-000001.json), not inside it."""
    return workspace.path.with_name(workspace.path.name + ".json")


def _write_details(workspace: ExecutionWorkspace, wr: str, task: ExecutionTask, result: ExecutionResult,
                   delivery: Delivery | None = None) -> None:
    record = {"work_request_id": wr, "issue_id": task.issue_id, "outcome": result.outcome,
              "message": result.message, "files_changed": result.files_changed, "details": result.details,
              "workspace": str(workspace.path),
              "delivery": {
                  "branch": delivery.branch, "commit": delivery.commit_sha, "diff": delivery.diff,
                  "left_uncommitted": list(delivery.left_uncommitted),
                  "review": "pending: inspect the workspace; nothing has been pushed",
              } if delivery else None}
    try:
        details_path(workspace).write_text(json.dumps(record, indent=2, default=str) + "\n")
    except OSError as exc:  # the outcome is still recorded in the store
        log.warning("%s: couldn't write details: %s", wr, exc)


# --- command line -------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
    parser = argparse.ArgumentParser(prog="python -m cycle_runner.executor")
    commands = parser.add_subparsers(dest="command")
    run = commands.add_parser("run", help="run the next pending request (the default), then exit")
    run.add_argument("--request", help="run this request instead of the next pending one")
    run.add_argument("--executor", choices=["fake", "claude"], default="fake",
                     help="fake (default) or claude: a real coding agent; needs --request, Linear and Claude credentials")
    run.add_argument("--max-turns", type=int, default=30)
    run.add_argument("--max-budget-usd", type=float, default=1.0)
    commands.add_parser("list", help="show every work request and its state")
    release = commands.add_parser("release", help="put a stranded claimed request back to pending")
    release.add_argument("work_request_id")
    abandon = commands.add_parser("abandon", help="mark a stranded running request failed")
    abandon.add_argument("work_request_id")
    abandon.add_argument("--reason", default="stranded while running")
    backfill = commands.add_parser(
        "backfill-project",
        help="record the project of a pending request approved before projects were recorded "
             "(checked against Linear when it runs)",
    )
    backfill.add_argument("work_request_id")
    backfill.add_argument("project_id")
    review = commands.add_parser("review", help="show what a finished run left for human review")
    review.add_argument("work_request_id")
    args = parser.parse_args(argv)

    store = open_store()
    try:
        if args.command in (None, "run"):
            return _run(
                store, getattr(args, "request", None), getattr(args, "executor", "fake"),
                getattr(args, "max_turns", 30), getattr(args, "max_budget_usd", 1.0),
            )
        if args.command == "list":
            for request in store.list_all():
                print(_describe(request))
            return 0
        if args.command == "release":
            print(_describe(store.release(args.work_request_id)))
            return 0
        if args.command == "review":
            return _review(store, args.work_request_id)
        if args.command == "backfill-project":
            request = store.backfill_project(args.work_request_id, args.project_id)
            print(f"{_describe(request)} (project {request.project_id})")
            return 0
        print(_describe(store.abandon(args.work_request_id, args.reason)))
        return 0
    except InvalidTransition as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2


def _run(store: WorkRequestStore, work_request_id: str | None, kind: str, max_turns: int,
         max_budget_usd: float) -> int:
    from cycle_runner.projects import ProjectConfigError, WorkspaceResolver, load_projects

    try:
        resolver = WorkspaceResolver(load_projects())
    except ProjectConfigError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    if kind == "claude":
        # A real coding agent only ever runs on a request named deliberately.
        if not work_request_id:
            print("error: --executor claude needs --request WR-…", file=sys.stderr)
            return 2
        from cycle_runner.claude_executor import ClaudeCodeExecutor
        from cycle_runner.issue_context import LinearIssueSource

        try:
            source = LinearIssueSource.from_env()  # before claiming: missing credentials change nothing
        except TaskContextError as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 2
        from cycle_runner.git_delivery import LocalGitDelivery

        executor = ClaudeCodeExecutor(max_turns=max_turns, max_budget_usd=max_budget_usd)
        deliverer: Deliverer | None = LocalGitDelivery()  # a changed result becomes a local commit
    else:
        from cycle_runner.fake_executor import FakeExecutor

        source, executor, deliverer = ApprovalOnly(), FakeExecutor(), None
    if work_request_id:
        request, executed = run_request(store, executor, resolver, work_request_id, source, deliverer)
        if not executed:
            print(f"{request.work_request_id} is already {request.status}; nothing executed.")
            return 0
    else:
        request = run_next(store, executor, resolver, source, deliverer)
        if request is None:
            print("No pending work requests.")
            return 0
    print(_describe(request))
    return 0 if request.status == "completed" else 1


def _review(store: WorkRequestStore, work_request_id: str) -> int:
    """The human review point: everything a finished run left, from the store and its record."""
    from cycle_runner.projects import ProjectConfigError, load_projects

    request = store.get(work_request_id)
    if request is None:
        raise InvalidTransition(f"{work_request_id} does not exist")
    print(_describe(request))
    if request.status not in ("completed", "failed"):
        return 0
    try:
        workspace = load_projects().workspace_root / request.work_request_id
    except ProjectConfigError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    record_path = details_path(ExecutionWorkspace(path=workspace, test_command=""))
    try:
        record = json.loads(record_path.read_text())
    except (OSError, ValueError):
        print(f"workspace: {workspace} (no run record at {record_path})")
        return 0
    details, delivery = record.get("details", {}), record.get("delivery")
    print(f"outcome:   {record.get('outcome')}")
    print(f"workspace: {workspace}")
    print(f"record:    {record_path}")
    tail = (details.get("tests_output_tail") or "").strip().splitlines()
    tests = next((line for line in reversed(tail) if " passed" in line or " failed" in line or line == "OK"), None)
    print(f"tests:     {details.get('tests_observed', 0)} observed run(s); last: {tests or 'n/a'}")
    if delivery:
        diff = delivery["diff"]
        print(f"branch:    {delivery['branch']} (local only, no remote)")
        print(f"commit:    {delivery['commit']}")
        print(f"diff:      {_diff_line(diff)}; added {diff['files_added']}, "
              f"changed {diff['files_changed']}, deleted {diff['files_deleted']}")
        for item in delivery.get("left_uncommitted", []):
            print(f"  not committed: {item}")
        print(f"inspect:   git -C {workspace} show --stat {delivery['commit'][:12]}")
        print("review:    pending. Nothing has been pushed; no PR exists.")
    return 0


def _describe(request: WorkRequest) -> str:
    parts = [request.work_request_id, request.issue_id,
             request.status + (f" ({request.outcome})" if request.status == "completed" and request.outcome else "")]
    if request.claimed_by:
        parts.append(f"by {request.claimed_by}")
    if request.result_message:
        parts.append(f"- {request.result_message}")
    return " ".join(parts)


if __name__ == "__main__":
    sys.exit(main())
