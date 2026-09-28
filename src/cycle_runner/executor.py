"""Executing approved work requests: the executor interface, the runner, and a CLI.

    uv run python -m cycle_runner.executor            # run the next pending request, then exit
    uv run python -m cycle_runner.executor list
    uv run python -m cycle_runner.executor release WR-000001
    uv run python -m cycle_runner.executor abandon WR-000001 --reason "..."

An Executor does the work for one request, inside a workspace it is given,
and says how it went. It knows nothing about claiming, statuses, timestamps
or projects. The runner owns the lifecycle and assembles the context: it
claims a request, has a WorkspaceResolver turn it into an ExecutionWorkspace
(projects.py), and only then calls the executor. Executors: FakeExecutor and
ClaudeCodeExecutor.

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
import logging
import os
import socket
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal, Protocol

from pydantic import BaseModel, Field

from cycle_runner.work_requests import InvalidTransition, WorkRequest, WorkRequestStore, open_store

log = logging.getLogger(__name__)


class ExecutionResult(BaseModel):
    outcome: Literal["completed", "failed"]
    message: str  # human-readable; this is what the store records
    # Anything structured the executor wants to report (files changed, cost, ...).
    # Returned to the caller, not persisted yet.
    details: dict[str, Any] = Field(default_factory=dict)


@dataclass(frozen=True)
class ExecutionWorkspace:
    """Where an executor works and how the work is checked; resolved by the runner.

    path: the only directory the work may read and change.
    test_command: the exact command that checks the work.
    readable: extra paths the checks may read (e.g. the test interpreter).
    """

    path: Path
    test_command: str
    readable: tuple[Path, ...] = ()


class WorkspaceError(Exception):
    """No workspace can be made for a request (no project, unknown project, clone failed)."""


class WorkspaceResolver(Protocol):
    def resolve(self, request: WorkRequest) -> ExecutionWorkspace: ...


class Executor(Protocol):
    name: str

    def execute(self, request: WorkRequest, workspace: ExecutionWorkspace) -> ExecutionResult:
        """Do the work for one request (claimed and running) inside the given workspace."""
        ...


def worker_id(executor: Executor) -> str:
    """Who holds a claim: which executor, on which host, in which process."""
    return f"{executor.name}@{socket.gethostname()}:{os.getpid()}"


def run_next(store: WorkRequestStore, executor: Executor, resolver: WorkspaceResolver) -> WorkRequest | None:
    """Claim the oldest pending request and run it. None if nothing is pending."""
    claimed = store.claim_next(worker_id(executor))
    if claimed is None:
        log.info("no pending work requests")
        return None
    return _run_claimed(store, executor, resolver, claimed)


def run_request(
    store: WorkRequestStore, executor: Executor, resolver: WorkspaceResolver, work_request_id: str
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
    return _run_claimed(store, executor, resolver, claimed), True


def _run_claimed(
    store: WorkRequestStore, executor: Executor, resolver: WorkspaceResolver, claimed: WorkRequest
) -> WorkRequest:
    wr = claimed.work_request_id
    log.info("%s claimed by %s (issue %s, project %s)", wr, claimed.claimed_by, claimed.issue_id, claimed.project_id)
    # The runner assembles the context: the executor gets a ready workspace or never runs.
    try:
        workspace = resolver.resolve(claimed)
    except WorkspaceError as exc:
        failed = store.fail_to_start(wr, str(exc))
        log.warning("%s not started: %s", wr, exc)
        return failed
    log.info("%s workspace %s", wr, workspace.path)
    running = store.start(wr)
    try:
        result = executor.execute(running, workspace)
    except Exception as exc:  # the executor failed; a crash (SystemExit, a killed process) isn't caught
        log.exception("%s: executor %s raised", wr, executor.name)
        result = ExecutionResult(outcome="failed", message=f"{executor.name} raised {type(exc).__name__}: {exc}")
    finished = store.finish(wr, result.outcome, result.message)
    log.info("%s %s: %s", wr, finished.status, finished.result_message)
    return finished


# --- command line -------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
    parser = argparse.ArgumentParser(prog="python -m cycle_runner.executor")
    commands = parser.add_subparsers(dest="command")
    run = commands.add_parser("run", help="run the next pending request (the default), then exit")
    run.add_argument("--request", help="run this request instead of the next pending one")
    commands.add_parser("list", help="show every work request and its state")
    release = commands.add_parser("release", help="put a stranded claimed request back to pending")
    release.add_argument("work_request_id")
    abandon = commands.add_parser("abandon", help="mark a stranded running request failed")
    abandon.add_argument("work_request_id")
    abandon.add_argument("--reason", default="stranded while running")
    args = parser.parse_args(argv)

    store = open_store()
    try:
        if args.command in (None, "run"):
            return _run(store, getattr(args, "request", None))
        if args.command == "list":
            for request in store.list_all():
                print(_describe(request))
            return 0
        if args.command == "release":
            print(_describe(store.release(args.work_request_id)))
            return 0
        print(_describe(store.abandon(args.work_request_id, args.reason)))
        return 0
    except InvalidTransition as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2


def _run(store: WorkRequestStore, work_request_id: str | None) -> int:
    from cycle_runner.fake_executor import FakeExecutor  # the only executor the CLI runs
    from cycle_runner.projects import ProjectConfigError, WorkspaceResolver, load_projects

    try:
        resolver = WorkspaceResolver(load_projects())
    except ProjectConfigError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    executor = FakeExecutor()
    if work_request_id:
        request, executed = run_request(store, executor, resolver, work_request_id)
        if not executed:
            print(f"{request.work_request_id} is already {request.status}; nothing executed.")
            return 0
    else:
        request = run_next(store, executor, resolver)
        if request is None:
            print("No pending work requests.")
            return 0
    print(_describe(request))
    return 0 if request.status == "completed" else 1


def _describe(request: WorkRequest) -> str:
    parts = [request.work_request_id, request.issue_id, request.status]
    if request.claimed_by:
        parts.append(f"by {request.claimed_by}")
    if request.result_message:
        parts.append(f"- {request.result_message}")
    return " ".join(parts)


if __name__ == "__main__":
    sys.exit(main())
