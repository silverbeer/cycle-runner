"""scrum-master: the command line. Read-only, manual, no server, no container, no scheduler.

    op run --env-file .env --env-file .env.scrum -- uv run scrum-master report

Commands that build a report read live data unless --offline names a saved
snapshot. collect, analyze and render run the three stages one at a time, so
each can be checked (or wrapped) on its own.

Exit codes: 0 done; 1 couldn't run (Linear unavailable, bad config or input);
2 the report is partial and --strict was given; 3 doctor found a failing check.
"""

import asyncio
import json
import logging
import sys
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from typing import Annotated

import typer
from pydantic import ValidationError

from cycle_runner.linear_client import LinearClient, LinearError
from cycle_runner.scrum import analyze as analysis
from cycle_runner.scrum import collect as collection
from cycle_runner.scrum import config as configuration
from cycle_runner.scrum import doctor as checks
from cycle_runner.scrum import render
from cycle_runner.scrum.models import Report, Snapshot

app = typer.Typer(no_args_is_help=True, add_completion=False, help=__doc__.splitlines()[0])


class Format(StrEnum):
    terminal = "terminal"
    markdown = "markdown"
    json = "json"


ConfigOpt = Annotated[Path | None, typer.Option("--config", help="scrum.toml path (default: repo root, or $SCRUM_CONFIG)")]
TeamOpt = Annotated[str | None, typer.Option("--team", help="Linear team key (overrides scrum.toml)")]
RepoOpt = Annotated[list[str] | None, typer.Option("--repo", help="owner/name; repeat to scope GitHub to these")]
CycleOpt = Annotated[int | None, typer.Option("--cycle", help="cycle number (default: the active cycle)")]
FormatOpt = Annotated[Format, typer.Option("--format", "-f")]
OutputOpt = Annotated[Path | None, typer.Option("--output", "-o", help="write to this file instead of stdout")]
OfflineOpt = Annotated[Path | None, typer.Option("--offline", help="analyze a saved snapshot instead of live data")]
StrictOpt = Annotated[bool, typer.Option("--strict", help="exit 2 if the report is partial")]


class JsonLogFormatter(logging.Formatter):
    """One JSON object per line on stderr. Only fields passed in `extra` and the message; never request data."""

    _skip = set(logging.makeLogRecord({}).__dict__) | {"message", "asctime"}

    def format(self, record: logging.LogRecord) -> str:
        data = {"ts": datetime.fromtimestamp(record.created, UTC).isoformat(timespec="seconds"),
                "level": record.levelname.lower(), "logger": record.name, "msg": record.getMessage()}
        data.update({k: v for k, v in record.__dict__.items() if k not in self._skip})
        return json.dumps(data, default=str)


@app.callback()
def _setup(log_level: Annotated[str, typer.Option("--log-level", help="debug, info, warning")] = "warning") -> None:
    handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(JsonLogFormatter())
    logging.basicConfig(level=log_level.upper(), handlers=[handler], force=True)
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)


def _fail(message: str, code: int = 1) -> typer.Exit:
    typer.echo(f"scrum-master: {message}", err=True)
    return typer.Exit(code)


def _config(path: Path | None, team: str | None = None, repos: list[str] | None = None) -> configuration.Config:
    try:
        return configuration.load(path).scoped(team=team, repos=repos)
    except (ValidationError, ValueError, OSError) as exc:
        raise _fail(f"bad configuration: {exc}") from None


def _snapshot(cfg: configuration.Config, cycle: int | None, offline: Path | None) -> Snapshot:
    if offline is not None:
        try:
            return Snapshot.model_validate_json(offline.read_text())
        except (OSError, ValidationError) as exc:
            raise _fail(f"can't read snapshot {offline}: {exc}") from None
    try:
        linear = LinearClient.from_env()
        github, why = collection.github_client(cfg)
        return asyncio.run(collection.collect(cfg, linear_query=linear.query, github=github, github_error=why,
                                              cycle=cycle))
    except (LinearError, LookupError) as exc:
        raise _fail(f"Linear is unavailable, so there is no report: {exc}") from None


def _emit(text: str, output: Path | None) -> None:
    if output is None:
        sys.stdout.write(text)
    else:
        output.write_text(text)
        typer.echo(f"wrote {output}", err=True)


def _show(report: Report, fmt: Format, output: Path | None, section: render.Section, strict: bool) -> None:
    if fmt is Format.json:
        _emit(render.to_json(report), output)
    elif fmt is Format.markdown:
        _emit(render.to_markdown(report, section), output)
    elif output is None:
        render.to_terminal(report, section)
    else:
        from rich.console import Console

        with output.open("w") as handle:
            render.to_terminal(report, section, console=Console(file=handle, width=140, color_system=None))
        typer.echo(f"wrote {output}", err=True)
    if strict and not report.complete:
        raise _fail("the report is partial (--strict)", 2)


def _section_command(section: render.Section, doc: str):
    def command(fmt: FormatOpt = Format.terminal, output: OutputOpt = None, offline: OfflineOpt = None,
                team: TeamOpt = None, repo: RepoOpt = None, cycle: CycleOpt = None, strict: StrictOpt = False,
                config: ConfigOpt = None) -> None:
        cfg = _config(config, team, repo)
        snapshot = _snapshot(cfg, cycle, offline)
        _show(analysis.analyze(snapshot, cfg, now=datetime.now(UTC)), fmt, output, section, strict)

    command.__doc__ = doc
    return command


app.command("report")(_section_command("all", "The whole report: cycle, top actions, aging, blockers, epics, PRs, branches."))
app.command("cycle")(_section_command("cycle", "Cycle health for the active (or --cycle) cycle."))
app.command("aging")(_section_command("aging", "Aging and inactive issues."))
app.command("epics")(_section_command("epics", "Incomplete epics (Linear projects) and parent issues."))
app.command("prs")(_section_command("prs", "Open pull requests: age, activity, review, checks."))
app.command("branches")(_section_command("branches", "Branch and worktree cleanup candidates (recommendations only)."))


@app.command("collect")
def collect_command(output: OutputOpt = None, team: TeamOpt = None, repo: RepoOpt = None, cycle: CycleOpt = None,
                    config: ConfigOpt = None) -> None:
    """Stage 1: read Linear, GitHub and git into a snapshot (JSON)."""
    cfg = _config(config, team, repo)
    _emit(_snapshot(cfg, cycle, None).model_dump_json(indent=2) + "\n", output)


@app.command("analyze")
def analyze_command(snapshot: Annotated[Path, typer.Argument(help="snapshot JSON from `collect`")],
                    output: OutputOpt = None, config: ConfigOpt = None,
                    now: Annotated[str | None, typer.Option(help="ISO time to analyse as of (default: now)")] = None
                    ) -> None:
    """Stage 2: apply the rules to a snapshot; writes the report as JSON."""
    cfg = _config(config)
    data = _snapshot(cfg, None, snapshot)
    try:
        when = datetime.fromisoformat(now) if now else datetime.now(UTC)
    except ValueError:
        raise _fail(f"--now {now!r} is not an ISO date-time") from None
    when = when if when.tzinfo else when.replace(tzinfo=UTC)
    _emit(render.to_json(analysis.analyze(data, cfg, now=when)), output)


@app.command("render")
def render_command(report: Annotated[Path, typer.Argument(help="report JSON from `analyze`")],
                   fmt: FormatOpt = Format.terminal, output: OutputOpt = None,
                   section: Annotated[str, typer.Option(help="all, cycle, aging, epics, prs, branches")] = "all",
                   strict: StrictOpt = False) -> None:
    """Stage 3: render a report JSON as terminal, Markdown or JSON."""
    if section not in render.SECTIONS:
        raise _fail(f"unknown section {section!r}")
    try:
        data = Report.model_validate_json(report.read_text())
    except (OSError, ValidationError) as exc:
        raise _fail(f"can't read report {report}: {exc}") from None
    _show(data, fmt, output, section, strict)


@app.command("config")
def config_command(config: ConfigOpt = None, team: TeamOpt = None, repo: RepoOpt = None) -> None:
    """Show the effective configuration and scope. Contains no secrets, only where they're read from."""
    typer.echo(json.dumps(_config(config, team, repo).public(), indent=2))


MARK = {"ok": "✓", "warn": "!", "fail": "✗"}


@app.command("doctor")
def doctor_command(config: ConfigOpt = None, team: TeamOpt = None, repo: RepoOpt = None) -> None:
    """Check configuration, credentials, connectivity and permissions. Read-only; prints no secret values."""
    cfg = _config(config, team, repo)
    results = asyncio.run(checks.run(cfg))
    for result in results:
        typer.echo(f"{MARK[result.status]} {result.name}: {result.detail}")
    if any(r.status == "fail" for r in results):
        raise typer.Exit(3)


def main() -> None:
    app()
