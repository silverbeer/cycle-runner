"""A deterministic stand-in for the future coding agent.

It does no work and touches nothing: no shell, no network, no files, no
Linear, no GitHub, no model. It only proves that a work request can travel
the whole lifecycle. The issue id is just an identifier here.
"""

from cycle_runner.executor import ExecutionResult
from cycle_runner.work_requests import WorkRequest


class FakeExecutor:
    name = "fake"

    def execute(self, request: WorkRequest) -> ExecutionResult:
        return ExecutionResult(
            outcome="completed",
            message=(
                f"Fake execution completed for {request.work_request_id} "
                f"({request.issue_id}). No real work was done."
            ),
        )
