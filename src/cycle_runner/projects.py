"""Project workspace configuration: where a project's work happens, and how it's checked.

    WorkRequest.project_id ("MT") ─► projects.toml [projects.MT] ─► WorkspaceResolver
        ─► a fresh clone of the project's repository ─► ExecutionWorkspace ─► any Executor

A project is identified by its Linear repo label (MT, TRD, ...), recorded on
the work request at approval time. Everything project-specific is in the
configuration file; there is no project name anywhere in the code, so adding
a project means adding a [projects.X] table.

The workspace is always a fresh clone of the configured repository, made for
one work request, with its origin remote removed. The real checkout is only
read, never changed.

This module knows projects, paths and git. It knows nothing about Claude,
ADK, Telegram or Linear.
"""

import os
import re
import subprocess
import tomllib
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, ValidationError, field_validator, model_validator

from cycle_runner.executor import ExecutionWorkspace, WorkspaceError
from cycle_runner.work_requests import WorkRequest

DEFAULT_CONFIG_PATH = "projects.toml"
PROJECT_ID = re.compile(r"^[A-Z][A-Z0-9]*$")
SHELL_OPERATORS = re.compile(r"[;&|<>`$\\\n]")


class ProjectConfigError(Exception):
    """The configuration file is missing or invalid."""


def _expanded(value: Any) -> Path:
    return Path(os.path.expanduser(str(value)))


class ProjectConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    repository: Path  # a local git checkout; only ever read (cloned)
    test_command: str  # run inside the clone; the only command the coding agent may run
    readable: tuple[Path, ...] = ()  # extra paths sandboxed tests may read (e.g. an interpreter)

    @field_validator("repository", mode="before")
    @classmethod
    def _repository(cls, value: Any) -> Path:
        path = _expanded(value)
        if not path.is_absolute():
            raise ValueError("must be an absolute path (~ is allowed)")
        return path

    @field_validator("readable", mode="before")
    @classmethod
    def _readable(cls, value: Any) -> tuple[Path, ...]:
        paths = tuple(_expanded(v) for v in value)
        if not all(p.is_absolute() for p in paths):
            raise ValueError("readable paths must be absolute")
        return paths

    @field_validator("test_command")
    @classmethod
    def _test_command(cls, value: str) -> str:
        value = " ".join(value.split())
        if not value:
            raise ValueError("must not be empty")
        if SHELL_OPERATORS.search(value):
            raise ValueError("must be a single command without shell operators (the agent may run only it)")
        return value


class ProjectsConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    workspace_root: Path  # where a fresh clone is made for each work request
    projects: dict[str, ProjectConfig]

    @field_validator("workspace_root", mode="before")
    @classmethod
    def _workspace_root(cls, value: Any) -> Path:
        path = _expanded(value)
        if not path.is_absolute():
            raise ValueError("must be an absolute path (~ is allowed)")
        home = Path.home()
        if path == Path("/") or path == home or path in home.parents:
            raise ValueError("must be a dedicated directory, not /, the home directory or above it")
        return path

    @field_validator("projects")
    @classmethod
    def _project_ids(cls, projects: dict[str, ProjectConfig]) -> dict[str, ProjectConfig]:
        bad = [name for name in projects if not PROJECT_ID.match(name)]
        if bad:
            raise ValueError(f"project ids must be uppercase labels like MT or TRD: {bad}")
        return projects

    @model_validator(mode="after")
    def _separate(self) -> "ProjectsConfig":
        root = self.workspace_root.resolve()
        for name, project in self.projects.items():
            repository = project.repository.resolve()
            if root == repository or repository in root.parents or root in repository.parents:
                raise ValueError(f"workspace_root and project {name}'s repository must not contain each other")
        return self


def config_path_from_env() -> Path:
    return Path(os.environ.get("CYCLE_RUNNER_PROJECTS", DEFAULT_CONFIG_PATH))


def load_projects(path: str | Path | None = None) -> ProjectsConfig:
    """Read and validate the configuration file (CYCLE_RUNNER_PROJECTS, default projects.toml)."""
    path = Path(path) if path else config_path_from_env()
    try:
        data = tomllib.loads(path.read_text())
    except FileNotFoundError:
        raise ProjectConfigError(f"{path}: no such configuration file") from None
    except tomllib.TOMLDecodeError as exc:
        raise ProjectConfigError(f"{path}: not valid TOML ({exc})") from None
    try:
        return ProjectsConfig.model_validate(data)
    except ValidationError as exc:
        problems = "; ".join(f"{'.'.join(map(str, e['loc']))}: {e['msg']}" for e in exc.errors())
        raise ProjectConfigError(f"{path}: {problems}") from None


class WorkspaceResolver:
    """Turns a work request into a fresh, validated ExecutionWorkspace for its project."""

    def __init__(self, config: ProjectsConfig):
        self.config = config

    def resolve(self, request: WorkRequest) -> ExecutionWorkspace:
        wr = request.work_request_id
        if not request.project_id:
            raise WorkspaceError(f"{wr} has no project")
        project = self.config.projects.get(request.project_id)
        if project is None:
            raise WorkspaceError(f"{wr}'s project {request.project_id!r} is not configured")
        repository = project.repository.resolve()
        if not (repository / ".git").exists():
            raise WorkspaceError(f"project {request.project_id}'s repository {repository} is not a git checkout")

        root = self.config.workspace_root.resolve()
        workspace = root / wr
        if workspace.exists():
            raise WorkspaceError(f"{wr} already has a workspace at {workspace}")
        root.mkdir(parents=True, exist_ok=True)
        self._git("clone", "--quiet", "--no-hardlinks", str(repository), str(workspace))
        self._git("-C", str(workspace), "remote", "remove", "origin")  # nowhere to push to

        return ExecutionWorkspace(path=workspace, test_command=project.test_command, readable=project.readable)

    @staticmethod
    def _git(*args: str) -> None:
        result = subprocess.run(
            ["git", *args], capture_output=True, text=True, env={**os.environ, "GIT_TERMINAL_PROMPT": "0"}
        )
        if result.returncode != 0:
            detail = (result.stderr.strip().splitlines() or ["unknown error"])[-1]
            raise WorkspaceError(f"git {args[0]} failed: {detail}")
