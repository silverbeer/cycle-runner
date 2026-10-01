"""LinearIssueSource: the full issue, read-only, as an ExecutionTask. Linear is faked."""

from datetime import UTC, datetime

import pytest

from cycle_runner.executor import TaskContextError, run_request
from cycle_runner.issue_context import DESCRIPTION_LIMIT, ISSUE, LinearIssueSource
from cycle_runner.linear_client import LinearError, _require_read_only
from cycle_runner.work_requests import WorkRequestStore

DESCRIPTION = "## Problem\n\nSignups accept 3-character passwords.\n\n## Fix\n\n1. Rotate the admin password.\n2. Enforce a policy."


def _issue(identifier="DEMO-640", description=DESCRIPTION, project="DEMO", extra_labels=()):
    labels = [{"name": name, "parent": None} for name in extra_labels]
    if project:
        labels.append({"name": project, "parent": {"name": "repo"}})
    return {"identifier": identifier, "title": "Weak admin password and no rate limiting",
            "description": description, "labels": {"nodes": labels}}


class StubLinear:
    def __init__(self, issue=None, error=None):
        self.issue, self.error = issue, error
        self.documents, self.calls = [], []

    async def query(self, document, variables=None):
        self.documents.append(document)
        self.calls.append(variables)
        if self.error:
            raise self.error
        return {"issue": self.issue}


@pytest.fixture
def store(work_request_db):
    return WorkRequestStore(work_request_db)


def _request(store, project_id="DEMO", issue_id="DEMO-640"):
    request, _ = store.create_for_approval(
        recommendation_id="rec-1", issue_id=issue_id, approved_by="telegram:1", approved_at=datetime.now(UTC),
        cycle_number=10, title_at_approval="Weak admin password", rationale="Security.", project_id=project_id,
    )
    return request


def test_the_task_carries_the_whole_description(store):
    linear = StubLinear(_issue())
    request = _request(store)

    task = LinearIssueSource(linear).task_for(request)

    assert linear.calls == [{"id": "DEMO-640"}]
    assert (task.work_request_id, task.issue_id, task.project_id) == (request.work_request_id, "DEMO-640", "DEMO")
    assert task.title == "Weak admin password and no rate limiting"  # as it is now, from Linear
    assert task.description == DESCRIPTION
    assert task.rationale == "Security."


def test_the_query_is_read_only():
    _require_read_only(ISSUE)  # the client refuses anything else
    assert "mutation" not in ISSUE.lower()


def test_a_huge_description_is_capped(store):
    task = LinearIssueSource(StubLinear(_issue(description="x" * (DESCRIPTION_LIMIT + 500)))).task_for(_request(store))
    assert len(task.description) == DESCRIPTION_LIMIT


@pytest.mark.parametrize(
    ("linear", "problem"),
    [
        (StubLinear(error=LinearError("Linear is unreachable")), "couldn't read DEMO-640 from Linear: Linear is unreachable"),
        (StubLinear(None), "different issue"),
        (StubLinear(_issue(identifier="DEMO-641")), "different issue"),
        (StubLinear(_issue(project="OTHER")), "labelled 'OTHER' in Linear, but was approved for 'DEMO'"),
        (StubLinear(_issue(project=None)), "labelled None"),
        (StubLinear(_issue(description="   ")), "no description"),
        (StubLinear(_issue(description=None)), "no description"),
    ],
)
def test_no_task_unless_the_issue_matches_the_approval(store, linear, problem):
    with pytest.raises(TaskContextError, match=problem):
        LinearIssueSource(linear).task_for(_request(store))


def test_missing_credentials_are_a_task_context_error(monkeypatch):
    # conftest removes the Linear credentials for unmarked tests.
    with pytest.raises(TaskContextError, match="LINEAR_CLIENT_ID and LINEAR_CLIENT_SECRET must be set"):
        LinearIssueSource.from_env()


def test_a_linear_failure_fails_the_request_before_the_executor_runs(store, fixed_workspace):
    request = _request(store)

    class MustNotRun:
        name = "must-not-run"

        def execute(self, task, workspace):
            raise AssertionError("the executor ran without its task")

    source = LinearIssueSource(StubLinear(error=LinearError("Linear is unreachable")))
    done, executed = run_request(store, MustNotRun(), fixed_workspace, request.work_request_id, source)

    assert executed  # it was claimed ...
    assert done.status == "failed" and done.started_at is None  # ... and never started
    assert done.result_message == "Not started: couldn't read DEMO-640 from Linear: Linear is unreachable"


def test_the_executor_gets_the_description(store, fixed_workspace):
    request = _request(store)
    received = []

    class Recording:
        name = "recording"

        def execute(self, task, workspace):
            from cycle_runner.executor import ExecutionResult

            received.append(task)
            return ExecutionResult(outcome="changed", message="ok")

    done, _ = run_request(store, Recording(), fixed_workspace, request.work_request_id,
                          LinearIssueSource(StubLinear(_issue())))

    assert done.status == "completed"
    assert received[0].description == DESCRIPTION



def test_describe_reads_title_project_and_cycle(store):
    issue = {**_issue(), "cycle": {"number": 10}}
    summary = LinearIssueSource(StubLinear(issue)).describe("DEMO-640")
    assert (summary.issue_id, summary.title, summary.project_id, summary.cycle_number) == \
        ("DEMO-640", "Weak admin password and no rate limiting", "DEMO", 10)


@pytest.mark.parametrize("linear", [StubLinear(error=LinearError("down")), StubLinear(None),
                                    StubLinear(_issue(identifier="DEMO-1"))])
def test_describe_fails_safely(linear):
    with pytest.raises(TaskContextError):
        LinearIssueSource(linear).describe("DEMO-640")
