"""Project workspace configuration: where a project's work happens, and how it's checked.

    WorkRequest.project_id ("MT") ─► projects.toml [projects.MT] ─► WorkspaceResolver
        ─► a fresh clone of the project's repository ─► ExecutionWorkspace ─► any Executor

A project is identified by its Linear repo label (MT, TRD, ...), recorded on
the work request at approval time. Everything project-specific is in the
configuration file; there is no project name anywhere in the code, so adding
a project means adding a [projects.X] table.

The workspace is always a fresh clone of the configured repository (its
configured branch), made for one work request, with its origin remote
removed. The real checkout is only read, never changed.

Setup (optional setup_command) prepares the clone's dependencies before the
coding agent starts, e.g. `uv sync`. It runs here, outside the agent's
sandbox and with network access, because installing needs both; the agent
itself never gets either. That makes it privileged, so it's constrained:
- one package-manager command (SETUP_PROGRAMS), optionally behind `env
  NAME=value`; no shell, no operators, no absolute or parent paths, no ~;
- run with the clone as its working directory, without a shell, with
  credential-looking variables removed, and with a timeout;
- then checked: every setup_produces path must exist and resolve inside the
  clone (so the agent's sandboxed tests can use it without reading ~).
A failed or unverified setup fails the request before the agent runs.

This module knows projects, paths and git. It knows nothing about Claude,
ADK, Telegram or Linear.
"""

import logging
import os
import re
import shlex
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
GITHUB_REPOSITORY = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9-]{0,38})/[A-Za-z0-9._-]{1,100}$")
BRANCH = re.compile(r"^[A-Za-z0-9._/][A-Za-z0-9._/-]*$")
ENV_ASSIGNMENT = re.compile(r"^[A-Z_][A-Z0-9_]*=")
# What a setup command may run: a package manager's install step, which puts
# a project's dependencies in its own directory. Only these subcommands: e.g.
# `uv run` would run anything.
SETUP_PROGRAMS = {
    "uv": {"sync"}, "npm": {"ci"}, "pnpm": {"install"}, "yarn": {"install"},
    "poetry": {"install"}, "bundle": {"install"},
}
SETUP_TIMEOUT_SECONDS = 600
# Removed from setup's environment: it needs the network, not our credentials.
CREDENTIAL_NAME = re.compile(r"TOKEN|SECRET|KEY|PASSWORD|CREDENTIAL|AUTH|CLIENT_ID", re.IGNORECASE)

log = logging.getLogger(__name__)


class ProjectConfigError(Exception):
    """The configuration file is missing or invalid."""


def _expanded(value: Any) -> Path:
    return Path(os.path.expanduser(str(value)))


class ProjectConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    repository: Path  # a local git checkout; only ever read (cloned)
    test_command: str  # run inside the clone; the only command the coding agent may run
    readable: tuple[Path, ...] = ()  # extra paths sandboxed tests may read (e.g. an interpreter)
    branch: str | None = None  # the branch to clone; default: whatever the checkout has checked out
    setup_command: str | None = None  # prepares dependencies in the clone, before the agent runs
    setup_produces: tuple[str, ...] = ()  # clone-relative paths setup must create, inside the clone
    test_success_pattern: str | None = None  # regex the agent's last test run output must match
    github: str | None = None  # owner/name: where an approved delivery may be pushed (V1.4); needs branch

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

    @field_validator("branch")
    @classmethod
    def _branch(cls, value: str | None) -> str | None:
        if value is not None and (not BRANCH.match(value) or ".." in value):
            raise ValueError("must be a plain branch name")
        return value

    @field_validator("setup_command")
    @classmethod
    def _setup_command(cls, value: str | None) -> str | None:
        if value is None:
            return None
        value = " ".join(value.split())
        if SHELL_OPERATORS.search(value) or any(c in value for c in "\"'"):
            raise ValueError("must be a single command without shell operators or quoting")
        words = value.split()
        program, subcommand = _setup_program(words)
        allowed = [f"{name} {sub}" for name, subs in sorted(SETUP_PROGRAMS.items()) for sub in sorted(subs)]
        if subcommand not in SETUP_PROGRAMS.get(program, ()):
            raise ValueError(
                f"must be one of {allowed} (optionally behind env NAME=value), not {f'{program} {subcommand}'.strip()!r}"
            )
        for word in words:
            argument = word.split("=", 1)[1] if ENV_ASSIGNMENT.match(word) else word
            if argument.startswith(("/", "~")) or ".." in Path(argument).parts:
                raise ValueError(f"must stay inside the clone: {word!r} names a path outside it")
        return value

    @field_validator("setup_produces")
    @classmethod
    def _setup_produces(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        for path in value:
            if path.startswith(("/", "~")) or ".." in Path(path).parts:
                raise ValueError(f"must be paths inside the clone, not {path!r}")
        return value

    @field_validator("test_success_pattern")
    @classmethod
    def _test_success_pattern(cls, value: str | None) -> str | None:
        if value is not None:
            try:
                re.compile(value)
            except re.error as exc:
                raise ValueError(f"not a valid regular expression: {exc}") from None
        return value

    @field_validator("github")
    @classmethod
    def _github(cls, value: str | None) -> str | None:
        if value is not None and not GITHUB_REPOSITORY.match(value):
            raise ValueError("must be a GitHub owner/name, like silverbeer/missing-table")
        return value

    @model_validator(mode="after")
    def _setup_consistent(self) -> "ProjectConfig":
        if self.setup_produces and not self.setup_command:
            raise ValueError("setup_produces needs a setup_command")
        if self.github and not self.branch:
            raise ValueError("github needs branch: the base a delivery's PR targets")
        return self


def _setup_program(words: list[str]) -> tuple[str, str]:
    """The program a setup command runs and its subcommand, after any `env NAME=value ...`."""
    rest = words[1:] if words and words[0] == "env" else words
    while rest and ENV_ASSIGNMENT.match(rest[0]):
        rest = rest[1:]
    return (rest[0] if rest else ""), (rest[1] if len(rest) > 1 else "")


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
        claude_temp = {Path(f"/tmp/claude-{os.getuid()}"), Path(f"/private/tmp/claude-{os.getuid()}")}
        if any(path == area or area in path.parents for area in claude_temp):
            # The agent's sandbox denies writes there, so the agent couldn't work in it.
            raise ValueError("must not be inside Claude Code's temp area (/tmp/claude-<uid>)")
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
        branch = ["--branch", project.branch] if project.branch else []
        self._git("clone", "--quiet", "--no-hardlinks", *branch, str(repository), str(workspace))
        self._git("-C", str(workspace), "remote", "remove", "origin")  # nowhere to push to
        if project.setup_command:
            self._setup(project, workspace)

        return ExecutionWorkspace(
            path=workspace, test_command=project.test_command, readable=project.readable,
            test_success_pattern=project.test_success_pattern,
        )

    @staticmethod
    def _setup(project: ProjectConfig, workspace: Path) -> None:
        """Run the setup command in the clone (no shell, no credentials), then check what it made.

        The clone is left in place whatever happens, for inspection.
        """
        environ = {name: value for name, value in os.environ.items() if not CREDENTIAL_NAME.search(name)}
        log.info("setup in %s: %s", workspace, project.setup_command)
        try:
            result = subprocess.run(
                shlex.split(project.setup_command), cwd=workspace, env=environ,
                capture_output=True, text=True, timeout=SETUP_TIMEOUT_SECONDS,
            )
        except subprocess.TimeoutExpired:
            raise WorkspaceError(f"setup failed: timed out after {SETUP_TIMEOUT_SECONDS}s") from None
        except OSError as exc:
            raise WorkspaceError(f"setup failed: {exc.strerror or exc}") from None
        if result.returncode != 0:
            detail = ((result.stderr or result.stdout).strip().splitlines() or ["no output"])[-1]
            raise WorkspaceError(f"setup failed (exit {result.returncode}): {detail}")
        root = workspace.resolve()
        for produced in project.setup_produces:
            path = (workspace / produced).resolve()
            if not path.exists():
                raise WorkspaceError(f"setup failed: it didn't create {produced}")
            if path != root and root not in path.parents:
                # e.g. a venv whose interpreter is a symlink into ~: the sandbox can't read it
                raise WorkspaceError(f"setup failed: {produced} resolves outside the workspace ({path})")

    @staticmethod
    def _git(*args: str) -> None:
        result = subprocess.run(
            ["git", *args], capture_output=True, text=True, env={**os.environ, "GIT_TERMINAL_PROMPT": "0"}
        )
        if result.returncode != 0:
            detail = (result.stderr.strip().splitlines() or ["unknown error"])[-1]
            raise WorkspaceError(f"git {args[0]} failed: {detail}")
