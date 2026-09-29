"""The human review point: inspect a work request's local commit, and approve or reject it.

    changed request + local commit ──► review ──► approve --commit <sha> ──► DeliveryApproval (store)
                                                 reject                       bound to that exact SHA

Approval is a deliberate command, never something inferred from a
conversation. The human names the commit they reviewed (at least 7
characters of its SHA); the approval records the full SHA, the branch, its
base, the target repository from projects.toml, who and when, and the
evidence they saw (files, diff, message, tests).

Before anything is recorded, the local commit is verified again
(git_delivery.CommitVerifier): the workspace, branch, HEAD, parent, exactly
one commit, author, files matching the record, no protected files or
secrets. Any mismatch refuses the approval; nothing is recorded.

This module knows the store, the project configuration and the local
workspace. It knows nothing about GitHub, Claude, Linear or Telegram.
"""

import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from cycle_runner.executor import DeliveryError, ExecutionWorkspace, details_path
from cycle_runner.git_delivery import CommitVerifier, VerifiedCommit
from cycle_runner.projects import ProjectConfig, ProjectsConfig
from cycle_runner.work_requests import DeliveryApproval, InvalidTransition, WorkRequest, WorkRequestStore

MIN_SHA_PREFIX = 7
SUMMARY_LIMIT = 2000


class ApprovalRefused(Exception):
    """The local delivery can't be approved (or rejected) as it stands. Nothing was recorded."""


@dataclass
class Review:
    """Everything a human needs to decide, and whether the local commit still checks out."""

    request: WorkRequest
    project: ProjectConfig | None
    workspace: Path
    record: dict[str, Any]
    verified: VerifiedCommit | None
    problem: str | None
    approvals: list[DeliveryApproval]

    @property
    def state(self) -> str:
        return delivery_state(self.request, self.approvals)


def delivery_state(request: WorkRequest, approvals: list[DeliveryApproval]) -> str:
    """Where a request's delivery is. review_pending: a local commit nobody has decided on yet."""
    if request.outcome != "changed" or not request.commit_sha:
        return "not_deliverable"
    current = [a for a in approvals if a.commit_sha == request.commit_sha]
    if not current:
        return "review_pending"
    latest = current[-1]
    return "review_pending" if latest.status == "invalid" else latest.status


def review(store: WorkRequestStore, config: ProjectsConfig, work_request_id: str,
           verifier: CommitVerifier | None = None) -> Review:
    request = store.get(work_request_id)
    if request is None:
        raise ApprovalRefused(f"{work_request_id} does not exist")
    project = config.projects.get(request.project_id or "")
    workspace = config.workspace_root / work_request_id
    try:
        record = json.loads(details_path(ExecutionWorkspace(path=workspace, test_command="")).read_text())
    except (OSError, ValueError):
        record = {}
    approvals = store.approvals_for(work_request_id)
    verified, problem = None, _eligibility_problem(request, project)
    if problem is None:
        recorded_diff = (record.get("delivery") or {}).get("diff")
        try:
            verified = (verifier or CommitVerifier()).verify(
                workspace, work_request_id=work_request_id, commit_sha=request.commit_sha,
                base_branch=project.branch, recorded_diff=recorded_diff,
            )
        except DeliveryError as exc:
            problem = str(exc)
    return Review(request=request, project=project, workspace=workspace, record=record, verified=verified,
                  problem=problem, approvals=approvals)


def approve(store: WorkRequestStore, config: ProjectsConfig, work_request_id: str, *, commit: str,
            approved_by: str, verifier: CommitVerifier | None = None) -> DeliveryApproval:
    """Record approval of exactly the commit the human named, after verifying it again."""
    current = review(store, config, work_request_id, verifier)
    if current.problem:
        raise ApprovalRefused(f"approval refused: {current.problem}")
    verified = current.verified
    if len(commit) < MIN_SHA_PREFIX or not re.fullmatch(r"[0-9a-fA-F]+", commit) \
            or not verified.commit_sha.startswith(commit.lower()):
        raise ApprovalRefused(
            f"approval refused: you named {commit!r}, but {work_request_id}'s commit is {verified.commit_sha}. "
            f"Review it and approve with --commit <at least {MIN_SHA_PREFIX} characters of that SHA>."
        )
    try:
        return store.approve_delivery(
            work_request_id, commit_sha=verified.commit_sha, base_sha=verified.base_sha,
            repository=current.project.github, evidence=_evidence(current), approved_by=approved_by,
        )
    except InvalidTransition as exc:
        raise ApprovalRefused(f"approval refused: {exc}") from None


def reject(store: WorkRequestStore, work_request_id: str, *, commit: str, rejected_by: str,
           reason: str) -> DeliveryApproval:
    request = store.get(work_request_id)
    if request is None or not request.commit_sha or len(commit) < MIN_SHA_PREFIX \
            or not request.commit_sha.startswith(commit.lower()):
        raise ApprovalRefused(f"{work_request_id} has no commit starting {commit!r}")
    try:
        return store.reject_delivery(work_request_id, commit_sha=request.commit_sha, rejected_by=rejected_by,
                                     reason=reason)
    except InvalidTransition as exc:
        raise ApprovalRefused(str(exc)) from None


def _eligibility_problem(request: WorkRequest, project: ProjectConfig | None) -> str | None:
    if request.status != "completed" or request.outcome != "changed":
        return f"{request.work_request_id} is {request.status} ({request.outcome}); only a changed request is delivered"
    if not request.commit_sha or not request.branch:
        return f"{request.work_request_id} has no local commit"
    if project is None:
        return f"project {request.project_id!r} is not configured"
    if not project.github:
        return f"project {request.project_id} has no GitHub repository configured"
    return None


def _evidence(current: Review) -> dict[str, Any]:
    """What the human saw when approving, kept with the approval (and used for the PR)."""
    details = current.record.get("details", {})
    return {
        "issue_id": current.request.issue_id,
        "title": current.request.title_at_approval,
        "project_id": current.request.project_id,
        "base_branch": current.project.branch,
        "message": current.verified.message,
        "tree_sha": current.verified.tree_sha,
        "diff": current.verified.diff,
        "tests": test_evidence(details),
        "summary": str(details.get("summary", ""))[:SUMMARY_LIMIT],
    }


def test_evidence(details: dict[str, Any]) -> dict[str, Any]:
    """Only what was observed: how many runs of the test command, and the last run's summary line."""
    tail = (details.get("tests_output_tail") or "").strip().splitlines()
    last = next((line.strip() for line in reversed(tail)
                 if " passed" in line or " failed" in line or line.strip() == "OK" or line.startswith("Ran ")), None)
    return {"observed_runs": int(details.get("tests_observed") or 0), "command": details.get("tests_command"),
            "last_result": last}
