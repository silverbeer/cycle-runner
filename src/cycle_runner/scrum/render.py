"""Render a Report as Markdown, JSON or a Rich terminal view. Rendering adds nothing the report doesn't hold.

Every section can be rendered alone (the `cycle`, `aging`, `epics`, `prs` and
`branches` commands). A partial report says so at the top, whatever the format.
"""

from collections import Counter, defaultdict
from datetime import datetime
from zoneinfo import ZoneInfo
from typing import Literal

from cycle_runner.scrum.models import Finding, Report

Section = Literal["all", "cycle", "aging", "epics", "prs", "branches"]
SECTIONS: tuple[Section, ...] = ("all", "cycle", "aging", "epics", "prs", "branches")
ICON = {"critical": "🔴", "high": "🟠", "medium": "🟡", "low": "⚪"}
ACTIVE_STATES = {"started", "unstarted", "triage"}


def _local(report: Report, value: datetime) -> str:
    return value.astimezone(ZoneInfo(report.timezone)).strftime("%Y-%m-%d %H:%M %Z")


def branch_groups(findings: list[Finding]) -> list[tuple[str, list[Finding], list[Finding], list[str]]]:
    """Per repository (and checkout): candidates, items needing review, and combined commands for the candidates."""
    groups: dict[str, list[Finding]] = defaultdict(list)
    for f in findings:
        groups[f.group or f.subject].append(f)
    out = []
    for name in sorted(groups):
        items = groups[name]
        candidates = [f for f in items if f.severity_reason == "cleanup candidate"]
        review = [f for f in items if f.severity_reason != "cleanup candidate"]
        commands = []
        local = [f.subject.rsplit(":", 1)[1] for f in candidates if f.rule == "BRANCH-LOCAL-MERGED"]
        if local:
            path = name.split("(local ", 1)[1].rstrip(")")
            commands.append(f"git -C {path} branch -d {' '.join(local)}")
        remote = [f.subject.split(":", 1)[1] for f in candidates
                  if f.rule in {"BRANCH-REMOTE-MERGED", "BRANCH-REMOTE-CONTAINED"}]
        if remote:
            commands.append(f"git push origin --delete {' '.join(remote)}   # from a checkout of {name}")
        out.append((name, candidates, review, commands))
    return out


def to_json(report: Report) -> str:
    return report.model_dump_json(indent=2) + "\n"


def _of(report: Report, section: str) -> list[Finding]:
    return [f for f in report.findings if f.section == section]


def _md_link(text: str, url: str | None) -> str:
    text = text.replace("|", "\\|")
    return f"[{text}]({url})" if url else f"`{text}`"


def _md_cell(text: str) -> str:
    return text.replace("|", "\\|").replace("\n", " ")


def _md_finding(f: Finding) -> list[str]:
    priority = f" · Linear: {f.source_priority}" if f.source_priority else ""
    return [
        f"- {ICON[f.severity]} **{f.severity}** {_md_link(f.subject, f.url)} {_md_cell(f.title)}",
        f"  - rule `{f.rule}`: {f.severity_reason}{priority}",
        f"  - evidence: {'; '.join(f.evidence)}",
        f"  - next: {f.action}",
    ]


def to_markdown(report: Report, section: Section = "all") -> str:
    out = [f"# Scrum Master report · {report.scope.team}", ""]
    out += [f"Generated {_local(report, report.generated_at)} · data collected {_local(report, report.collected_at)}", ""]
    if not report.complete:
        out += ["> ⚠️ **PARTIAL REPORT.** Some data is missing or incomplete; see *Data quality* below.", ""]
    out += [f"Scope: Linear team `{report.scope.team}`; repositories: "
            f"{', '.join(f'`{r}`' for r in report.scope.repos) or 'none'}", ""]

    if section in ("all", "cycle"):
        out += _md_cycle(report)
    if section == "all":
        out += ["## Top actions today", ""]
        if report.top_actions:
            for n, f in enumerate(report.top_actions, 1):
                out += [f"{n}. {ICON[f.severity]} {_md_link(f.subject, f.url)} {_md_cell(f.title)} — {f.action}",
                        f"   *{f.rule}: {f.severity_reason}*"]
        else:
            out.append("Nothing above low severity. Pick from the backlog.")
        out.append("")
    if section in ("all", "aging"):
        out += _md_aging(report)
    if section == "all":
        out += ["## Blockers and overdue work", ""]
        out += [line for f in _of(report, "blockers") for line in _md_finding(f)] or ["None."]
        out.append("")
    if section in ("all", "epics"):
        out += _md_epics(report)
    if section in ("all", "prs"):
        out += _md_prs(report)
    if section in ("all", "branches"):
        out += _md_branches(report)
    out += _md_quality(report)
    return "\n".join(out).rstrip() + "\n"


def _md_cycle(report: Report) -> list[str]:
    c = report.cycle
    if c is None:
        return ["## Cycle", "", "No cycle in scope.", ""]
    points = (f"{c.points_completed:g} of {c.points_total:g} points"
              if c.points_total is not None else f"not reported ({c.estimates_present} of {c.total} estimated)")
    rows = [
        ("Dates", f"{c.starts_on} → {c.ends_on}"),
        ("Days", f"{c.days_elapsed} elapsed, {c.days_remaining} remaining (today included)"),
        ("Issues", f"{c.completed} of {c.total} done · **{c.completion_by_count}% by count**"
                   + (f" ({c.excluded} canceled/duplicate excluded)" if c.excluded else "")),
        ("Points", points),
        ("In progress / not started", f"{c.in_progress} / {c.unstarted}"),
        ("Blocked", str(c.blocked)),
        ("Aging or inactive", str(c.aging_or_inactive)),
        ("Added after start", f"{len(c.added_after_start)}" + (f": {', '.join(c.added_after_start)}"
                                                               if c.added_after_start else "")),
        ("Carried in", "unknown" if c.carried_in is None else f"{len(c.carried_in)}"),
        ("Carryover risk", f"{len(c.carryover_risk)} open" if c.carryover_risk else "not yet (outside the risk window)"),
    ]
    out = [f"## Cycle {c.number}" + (f" · {c.name}" if c.name else ""), "", "| | |", "|---|---|"]
    out += [f"| {k} | {_md_cell(v)} |" for k, v in rows]
    out += ["", "Completion by count and by points are different measures; neither is a forecast.", ""]
    found = _of(report, "cycle")
    if found:
        out += [line for f in found for line in _md_finding(f)] + [""]
    return out


def _md_aging(report: Report) -> list[str]:
    out = ["## Aging and inactive issues", ""]
    out += [line for f in _of(report, "aging") for line in _md_finding(f)] or ["No aging findings."]
    active = [r for r in report.aging if r.in_cycle or r.state_type == "started"]
    out += ["", "In the cycle or in progress:", "",
            "| Issue | State | Priority | Owner | Age | Inactive | In progress |", "|---|---|---|---|---|---|---|"]
    out += [f"| {_md_link(r.identifier, r.url)} {_md_cell(r.title)} | {r.state_name} | {r.priority_label} | "
            f"{r.assignee or '—'} | {r.age_days}d | {r.inactive_days}d | "
            f"{'—' if r.in_progress_days is None else f'{r.in_progress_days}d'} |" for r in active]
    rest = [r for r in report.aging if not (r.in_cycle or r.state_type == "started")]
    if rest:
        buckets = Counter(r.age_bucket for r in rest)
        parts = [f"{n} aged {b}+ days" if b is not None else f"{n} newer" for b, n in
                 sorted(buckets.items(), key=lambda kv: -1 if kv[0] is None else kv[0])]
        out += ["", f"Rest of the open backlog ({len(rest)}) by age: {', '.join(parts)}. Full list in the JSON report."]
    return out + [""]


def _md_epics(report: Report) -> list[str]:
    out = ["## Epics and parent issues", ""]
    out += [line for f in _of(report, "epics") for line in _md_finding(f)]
    out += ["", "| Epic | Kind | Status | Owner | Done / total | Open | Unassigned | Blocked | Aged | Outside cycle |",
            "|---|---|---|---|---|---|---|---|---|---|"]
    for e in report.epics:
        if not e.complete_data:
            counts = "partial data | — | — | — | — | —"
        else:
            counts = (f"{e.done} / {e.children} | {e.open} | {len(e.unassigned)} | {len(e.blocked)} | "
                      f"{len(e.aged)} | {len(e.outside_cycle)}")
        out.append(f"| {_md_link(e.subject if e.kind == 'parent' else e.title, e.url)}"
                   f"{' ' + _md_cell(e.title) if e.kind == 'parent' else ''} | {e.kind} | {e.status} | "
                   f"{e.owner or '—'} | {counts} |")
    return out + [""]


def _md_prs(report: Report) -> list[str]:
    out = ["## Open pull requests", ""]
    if not report.pull_requests:
        return out + ["None open." if _source_ok(report, "github") else "Not available (GitHub not collected).", ""]
    out += ["| PR | Author | Age | Inactive | Draft | Review | Checks | Mergeable | Behind | Linear |",
            "|---|---|---|---|---|---|---|---|---|---|"]
    out += [f"| {_md_link(p.subject, p.url)} {_md_cell(p.title)} | {p.author or '—'} | {p.age_days}d | "
            f"{p.inactive_days}d | {'yes' if p.is_draft else 'no'} | {p.review} | {p.checks} | {p.mergeable} | "
            f"{'?' if p.behind_by is None else p.behind_by} | {', '.join(p.linear_ids) or '—'} |"
            for p in report.pull_requests]
    out += [""] + [line for f in _of(report, "prs") for line in _md_finding(f)]
    return out + [""]


def _md_branches(report: Report) -> list[str]:
    out = ["## Branch cleanup candidates", "",
           "Recommendations only. Nothing is deleted; check the evidence, then run the commands yourself.", ""]
    groups = branch_groups(_of(report, "branches"))
    for name, candidates, review, commands in groups:
        out += [f"### {name}", ""]
        if candidates:
            out.append(f"Candidates ({len(candidates)}): "
                       + ", ".join(f"{_md_link(f.subject.rsplit(':', 1)[1], f.url)} ({f.title})" for f in candidates))
            out += ["", "```", *commands, "```", ""] if commands else [""]
        if review:
            out += [f"Needs human review ({len(review)}):", ""]
            out += [f"- {_md_link(f.subject.rsplit(':', 1)[1], f.url)} {_md_cell(f.title)} (`{f.rule}`): "
                    f"{'; '.join(f.evidence)}. Next: {f.action}" for f in review] + [""]
    if not groups:
        out += ["None.", ""]
    return out


def _md_quality(report: Report) -> list[str]:
    out = ["## Data quality", "", "| Source | Status | Notes |", "|---|---|---|"]
    for s in report.sources:
        status = "failed" if not s.ok else ("complete" if s.complete else "partial")
        out.append(f"| {s.name} | {status} | {_md_cell(s.error or '; '.join(s.notes) or '—')} |")
    if report.warnings:
        out += ["", *[f"- {w}" for w in report.warnings]]
    return out + [""]


def _source_ok(report: Report, name: str) -> bool:
    return any(s.name == name and s.ok for s in report.sources)


# --- terminal ----------------------------------------------------------------------------


def to_terminal(report: Report, section: Section = "all", *, console=None) -> None:
    """Print the report with Rich. Imported lazily: Rich is in the optional `scrum` dependency group."""
    from rich.console import Console
    from rich.panel import Panel
    from rich.table import Table

    console = console or Console()
    style = {"critical": "bold red", "high": "dark_orange", "medium": "yellow", "low": "dim"}

    def link(text: str, url: str | None) -> str:
        safe = text.replace("[", "\\[")
        return f"[link={url}]{safe}[/link]" if url else safe

    compact = section == "all"  # the full report: one line per finding; a section command shows the evidence

    def findings(items: list[Finding], *, empty: str = "None.") -> None:
        if not items:
            console.print(f"  [dim]{empty}[/dim]")
        for f in items:
            if compact:
                console.print(f"  [{style[f.severity]}]{f.severity:<8}[/] {link(f.subject, f.url)} {_esc(f.title)}"
                              f" [dim]({f.rule})[/dim]")
                continue
            prio = f" · Linear {f.source_priority}" if f.source_priority else ""
            console.print(f"  [{style[f.severity]}]{f.severity:<8}[/] {link(f.subject, f.url)} {_esc(f.title)}")
            console.print(f"           [dim]{f.rule}: {_esc(f.severity_reason)}{_esc(prio)}[/dim]")
            console.print(f"           [dim]{_esc('; '.join(f.evidence))}[/dim]")
            console.print(f"           → {_esc(f.action)}")

    head = (f"[bold]Scrum Master · {report.scope.team}[/bold]   generated {_local(report, report.generated_at)}"
            f" · data {_local(report, report.collected_at)}\n"
            f"repos: {', '.join(report.scope.repos) or 'none'}")
    if not report.complete:
        head += "\n[bold red]PARTIAL REPORT[/bold red]: some data is missing; see Data quality."
    console.print(Panel(head, expand=False))
    if compact:
        console.print("[dim]One line per finding; run a section command (cycle, aging, epics, prs, branches) "
                      "or use --format markdown for evidence and next steps.[/dim]")

    if section in ("all", "cycle"):
        c = report.cycle
        if c is None:
            console.print("[bold]Cycle[/bold]: none in scope")
        else:
            table = Table(title=f"Cycle {c.number}  {c.starts_on} → {c.ends_on}", show_header=False, title_justify="left")
            points = (f"{c.points_completed:g} / {c.points_total:g}" if c.points_total is not None
                      else f"not reported ({c.estimates_present}/{c.total} estimated)")
            for key, value in [
                ("days", f"{c.days_elapsed} elapsed · {c.days_remaining} left"),
                ("done (by count)", f"{c.completed} / {c.total} · {c.completion_by_count}%"),
                ("points", points),
                ("in progress · not started", f"{c.in_progress} · {c.unstarted}"),
                ("blocked · aging", f"{c.blocked} · {c.aging_or_inactive}"),
                ("added after start", str(len(c.added_after_start))),
                ("carried in", "unknown" if c.carried_in is None else str(len(c.carried_in))),
                ("carryover risk", str(len(c.carryover_risk)) if c.carryover_risk else "—"),
            ]:
                table.add_row(key, value)
            console.print(table)
            findings(_of(report, "cycle"), empty="No cycle findings.")

    if section == "all":
        console.rule("Top actions today")
        for n, f in enumerate(report.top_actions, 1):
            console.print(f" {n}. [{style[f.severity]}]{f.severity}[/] {link(f.subject, f.url)} {_esc(f.title)}")
            console.print(f"    → {_esc(f.action)}  [dim]({f.rule}: {_esc(f.severity_reason)})[/dim]")
        if not report.top_actions:
            console.print("  Nothing above low severity.")

    if section in ("all", "aging"):
        console.rule("Aging and inactive")
        findings(_of(report, "aging"))
        table = Table("issue", "state", "prio", "age", "idle", "in prog", title="In the cycle or in progress",
                      title_justify="left")
        for r in report.aging:
            if r.in_cycle or r.state_type == "started":
                table.add_row(link(r.identifier, r.url) + " " + _esc(r.title[:60]), r.state_name, r.priority_label,
                              f"{r.age_days}d", f"{r.inactive_days}d",
                              "—" if r.in_progress_days is None else f"{r.in_progress_days}d")
        console.print(table)

    if section == "all":
        console.rule("Blockers and overdue")
        findings(_of(report, "blockers"))

    if section in ("all", "epics"):
        console.rule("Epics and parent issues")
        findings(_of(report, "epics"), empty="No epic findings.")
        table = Table("epic", "kind", "status", "done/total", "open", "blocked", "outside cycle")
        for e in report.epics:
            counts = (["partial", "—", "—", "—"] if not e.complete_data else
                      [f"{e.done}/{e.children}", str(e.open), str(len(e.blocked)), str(len(e.outside_cycle))])
            name = e.title if e.kind == "project" else f"{e.subject} {e.title}"
            table.add_row(link(name[:60], e.url), e.kind, e.status, *counts)
        console.print(table)

    if section in ("all", "prs"):
        console.rule("Open pull requests")
        table = Table("PR", "age", "idle", "review", "checks", "merge", "behind", "linear")
        for p in report.pull_requests:
            table.add_row(link(p.subject, p.url) + " " + _esc(p.title[:50]) + (" [dim](draft)[/dim]" if p.is_draft else ""),
                          f"{p.age_days}d", f"{p.inactive_days}d", p.review, p.checks, p.mergeable,
                          "?" if p.behind_by is None else str(p.behind_by), ", ".join(p.linear_ids) or "—")
        if report.pull_requests:
            console.print(table)
        findings(_of(report, "prs"))

    if section in ("all", "branches"):
        console.rule("Branch cleanup (recommendations only; nothing is deleted)")
        groups = branch_groups(_of(report, "branches"))
        if not groups:
            console.print("  [dim]None.[/dim]")
        for name, candidates, review, commands in groups:
            console.print(f"  [bold]{_esc(name)}[/bold]: {len(candidates)} candidates, {len(review)} to review")
            for command in commands:
                console.print(f"    [cyan]$ {_esc(command)}[/cyan]")
            for f in review:
                console.print(f"    [yellow]review[/yellow] {link(f.subject.rsplit(':', 1)[1], f.url)} {_esc(f.title)}"
                              f" → {_esc(f.action)}")

    console.rule("Data quality")
    for s in report.sources:
        status = "[red]failed[/red]" if not s.ok else ("[green]complete[/green]" if s.complete else "[yellow]partial[/yellow]")
        console.print(f"  {s.name:<7} {status} {_esc(s.error or '')}")
    for w in report.warnings:
        console.print(f"  [yellow]•[/yellow] {_esc(w)}")


def _esc(text: str) -> str:
    return text.replace("[", "\\[")
