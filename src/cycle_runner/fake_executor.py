"""A deterministic stand-in for the future coding agent.

It does no work and touches nothing: no shell, no network, no files, no
Linear, no GitHub, no model. It only proves that a work request can travel
the whole lifecycle. The issue id is just an identifier here.
"""

from cycle_runner.executor import ExecutionResult, ExecutionTask, ExecutionWorkspace


class FakeExecutor:
    name = "fake"

    def execute(self, task: ExecutionTask, workspace: ExecutionWorkspace) -> ExecutionResult:
        return ExecutionResult(
            outcome="no_change",
            message=(
                f"Fake execution completed for {task.work_request_id} "
                f"({task.issue_id}). No real work was done."
            ),
        )
