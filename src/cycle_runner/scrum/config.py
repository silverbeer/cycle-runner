"""Scrum Master configuration: scrum.toml, with no secrets in it.

The scope is explicit: one Linear team and an allowlist of GitHub repositories,
each optionally with a local checkout to inspect. Nothing outside that list is
read. Thresholds and severity overrides live here too, so a rule can be tuned
without a code change.

Credentials are never in this file. Linear uses LINEAR_CLIENT_ID and
LINEAR_CLIENT_SECRET (the read-only Cycle Runner app, from .env); GitHub uses
the variable named by github.token_env (SCRUM_GITHUB_TOKEN, from .env.scrum).
"""

import os
import tomllib
from pathlib import Path
from zoneinfo import ZoneInfo

from pydantic import BaseModel, ConfigDict, Field, field_validator

from cycle_runner.scrum.models import Severity

DEFAULT_PATH = Path(__file__).resolve().parents[3] / "scrum.toml"


class _Config(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class Thresholds(_Config):
    age_buckets: list[int] = [3, 7, 14, 30]  # issue age, in days, reported as the largest bucket reached
    inactive_days: int = 7  # an in-progress issue untouched this long is inactive
    in_progress_days: int = 14  # in progress this long, even if active, is worth a look
    backlog_inactive_days: int = 30  # backlog hygiene: open, not started, untouched this long
    pr_old_days: list[int] = [7, 14]
    pr_inactive_days: int = 7
    draft_inactive_days: int = 14
    branch_inactive_days: int = 14  # a cleanup candidate's last commit must be at least this old
    ci_persistent_hours: int = 24  # failing checks on a commit this old count as persistent
    cycle_risk_days: int = 2  # with this many days left, open cycle work is carryover risk
    estimate_coverage: float = 0.9  # points are summed only when this share of issues has estimates
    added_after_start_grace_hours: int = 1  # Linear adds planned issues just after a cycle starts

    @field_validator("age_buckets", "pr_old_days")
    @classmethod
    def _sorted(cls, value: list[int]) -> list[int]:
        return sorted(set(value))


class RepoConfig(_Config):
    github: str  # owner/name
    local: str | None = None  # a local checkout to inspect for branches and worktrees

    @field_validator("github")
    @classmethod
    def _owner_name(cls, value: str) -> str:
        if value.count("/") != 1 or not all(value.split("/")):
            raise ValueError(f"expected owner/name, got {value!r}")
        return value

    @property
    def local_path(self) -> Path | None:
        return Path(self.local).expanduser() if self.local else None


class GitHubConfig(_Config):
    token_env: str = "SCRUM_GITHUB_TOKEN"


class Config(_Config):
    team: str = "SB"
    timezone: str = "America/New_York"
    thresholds: Thresholds = Thresholds()
    severity: dict[str, Severity] = Field(default_factory=dict)  # rule id -> severity override
    github: GitHubConfig = GitHubConfig()
    repos: list[RepoConfig] = Field(default_factory=list)

    @field_validator("timezone")
    @classmethod
    def _known_zone(cls, value: str) -> str:
        ZoneInfo(value)  # raises for an unknown zone
        return value

    @property
    def tz(self) -> ZoneInfo:
        return ZoneInfo(self.timezone)

    def scoped(self, *, team: str | None = None, repos: list[str] | None = None) -> "Config":
        """This config narrowed by command-line options. A --repo outside the allowlist is added, GitHub only."""
        update: dict = {}
        if team:
            update["team"] = team
        if repos:
            known = {r.github: r for r in self.repos}
            update["repos"] = [known.get(name) or RepoConfig(github=name) for name in repos]
        return self.model_copy(update=update)

    def public(self) -> dict:
        """Everything here is non-secret by construction; say where credentials come from, not what they are."""
        data = self.model_dump(mode="json")
        data["credentials"] = {
            "linear": ["LINEAR_CLIENT_ID", "LINEAR_CLIENT_SECRET"],
            "github": [self.github.token_env],
        }
        return data


def load(path: Path | None = None) -> Config:
    """scrum.toml, plus scrum.local.toml beside it if present (gitignored: e.g. private repositories).

    The local file's [[repos]] are added to the allowlist; its other keys replace the shared ones.
    """
    path = path or Path(os.environ.get("SCRUM_CONFIG") or DEFAULT_PATH)
    data: dict = {}
    if path.exists():
        with path.open("rb") as handle:
            data = tomllib.load(handle)
    local = path.with_name(f"{path.stem}.local{path.suffix}")
    if local.exists():
        with local.open("rb") as handle:
            extra = tomllib.load(handle)
        repos = data.get("repos", []) + extra.pop("repos", [])
        data = {**data, **extra, "repos": repos}
    return Config.model_validate(data)
