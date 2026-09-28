"""Executing approved work requests: the executor interface, the runner, and a CLI.

    uv run python -m cycle_runner.executor            # run the next pending request, then exit
    uv run python -m cycle_runner.executor list
    uv run python -m cycle_runner.executor release WR-000001
    uv run python -m cycle_runner.executor abandon WR-000001 --reason "..."

An Executor does the work for one request and says how it went. It knows
nothing about claiming, statuses or timestamps; the runner and the store own
those. V0.9 has only FakeExecutor. A real coding agent will be another
Executor, and nothing else here needs to change.

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


class Executor(Protocol):
    name: str

    def execute(self, request: WorkRequest) -> ExecutionResult:
        """Do the work for one request (it is already claimed and running)."""
        ...


def worker_id(executor: Executor) -> str:
    """Who holds a claim: which executor, on which host, in which process."""
    return f"{executor.name}@{socket.gethostname()}:{os.getpid()}"


def run_next(store: WorkRequestStore, executor: Executor) -> WorkRequest | None:
    """Claim the oldest pending request and run it. None if nothing is pending."""
    claimed = store.claim_next(worker_id(executor))
    if claimed is None:
        log.info("no pending work requests")
        return None
    return _run_claimed(store, executor, claimed)


def run_request(store: WorkRequestStore, executor: Executor, work_request_id: str) -> tuple[WorkRequest, bool]:
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
    return _run_claimed(store, executor, claimed), True


def _run_claimed(store: WorkRequestStore, executor: Executor, claimed: WorkRequest) -> WorkRequest:
    wr = claimed.work_request_id
    log.info("%s claimed by %s (issue %s)", wr, claimed.claimed_by, claimed.issue_id)
    running = store.start(wr)
    try:
        result = executor.execute(running)
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
    from cycle_runner.fake_executor import FakeExecutor  # the only executor V0.9 has

    executor = FakeExecutor()
    if work_request_id:
        request, executed = run_request(store, executor, work_request_id)
        if not executed:
            print(f"{request.work_request_id} is already {request.status}; nothing executed.")
            return 0
    else:
        request = run_next(store, executor)
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
