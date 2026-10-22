"""Turn a Snapshot into a Report: cycle health, findings with explained severities, and the top three actions.

Pure: the same snapshot, config and `now` always give the same report. No I/O,
no clock (now is passed in), no model. Every finding names its rule, the
evidence it rests on and why it got its severity; Linear's own priority is
shown beside it and never replaced.

Definitions (all in days, counted between local calendar dates in the
configured time zone):
- age: today minus the creation date.
- inactivity: today minus the latest activity timestamp available (an issue's
  updatedAt; a PR's last update or last commit, whichever is later). Linear's
  updatedAt moves on any change, including automation, so inactivity can be
  understated, never overstated.
- in progress for: today minus startedAt.
- overdue: a due date before today on an open issue.
- blocked: an open "blocks" relation from another open issue, a state named
  "Blocked", or a "blocked" label.
"""

from collections.abc import Iterable, Iterator
from datetime import date, datetime, timedelta

from cycle_runner.scrum.collect_linear import _issue_order as issue_order
from cycle_runner.scrum.config import Config
from cycle_runner.scrum.models import (
    SEVERITY_RANK,
    AgingRow,
    CycleHealth,
    EpicRow,
    Finding,
    Issue,
    LocalRepo,
    PRRow,
    PullRequest,
    Report,
    Severity,
    Snapshot,
)

C, H, M, L = Severity.critical, Severity.high, Severity.medium, Severity.low
HIGH_PRIORITY = {1, 2}  # Linear urgent and high

# Rule order breaks ties between equal severities; it is also the documented rule list.
RULES = [
    "ISSUE-BLOCKED", "PR-CHECKS-FAILED", "ISSUE-OVERDUE", "CYCLE-CARRYOVER-RISK", "ISSUE-INACTIVE",
    "EPIC-TARGET-PASSED", "PR-CONFLICTS", "PR-APPROVED-UNMERGED", "PR-STALE", "ISSUE-UNASSIGNED",
    "CYCLE-UNTOUCHED", "ISSUE-LONG-RUNNING", "EPIC-ALL-DONE-OPEN", "EPIC-PARENT-OPEN-CHILDREN-DONE",
    "EPIC-PARENT-DONE-CHILDREN-OPEN", "EPIC-STALLED", "PR-OLD", "PR-DRAFT-STALE", "PR-UNLINKED",
    "CYCLE-CARRIED-IN", "BACKLOG-STALE", "BRANCH-REMOTE-MERGED", "BRANCH-REMOTE-CONTAINED",
    "BRANCH-REMOTE-CLOSED-UNMERGED", "BRANCH-REMOTE-NO-PR", "BRANCH-LOCAL-MERGED", "BRANCH-LOCAL-GONE",
    "WORKTREE-MERGED", "WORKTREE-REVIEW", "WORKTREE-PRUNABLE",
]


class _Context:
    def __init__(self, snapshot: Snapshot, config: Config, now: datetime) -> None:
        self.s, self.cfg, self.t = snapshot, config, config.thresholds
        self.tz = config.tz
        self.today = now.astimezone(self.tz).date()
        self.issues = {i.identifier: i for i in snapshot.issues}
        self.in_cycle = set(snapshot.cycle.issues) if snapshot.cycle else set()
        self.days_remaining: int | None = None

    def local(self, value: datetime) -> date:
        return value.astimezone(self.tz).date()

    def days(self, value: datetime | date | None) -> int | None:
        if value is None:
            return None
        day = value if isinstance(value, date) and not isinstance(value, datetime) else self.local(value)
        return (self.today - day).days

    def age(self, issue: Issue) -> int:
        return self.days(issue.created_at)

    def inactive(self, issue: Issue) -> int:
        return self.days(issue.updated_at)

    def in_progress(self, issue: Issue) -> int | None:
        return self.days(issue.started_at) if issue.state_type == "started" else None


def is_blocked(issue: Issue) -> bool:
    return issue.is_open and (
        bool(issue.blocked_by) or issue.state_name.strip().lower() == "blocked"
        or any(label.lower() == "blocked" for label in issue.labels)
    )


def analyze(snapshot: Snapshot, config: Config, *, now: datetime) -> Report:
    ctx = _Context(snapshot, config, now)
    warnings = _source_warnings(snapshot)
    cycle = _cycle_health(ctx, warnings)

    findings = [
        *_issue_findings(ctx), *_cycle_findings(ctx, cycle), *_epic_findings(ctx), *_pr_findings(ctx),
        *_branch_findings(ctx),
    ]
    findings = sorted((_override(f, config) for f in findings), key=_order)
    top, seen = [], set()
    for finding in findings:
        if finding.subject not in seen and finding.severity != L:
            top.append(finding)
            seen.add(finding.subject)
        if len(top) == 3:
            break

    return Report(
        generated_at=now, collected_at=snapshot.collected_at, timezone=config.timezone, scope=snapshot.scope,
        complete=all(s.ok and s.complete for s in snapshot.sources) and not any(w.startswith("!") for w in warnings),
        sources=snapshot.sources, cycle=cycle, top_actions=top, findings=findings,
        aging=_aging_rows(ctx), epics=_epic_rows(ctx), pull_requests=_pr_rows(ctx),
        warnings=[w.removeprefix("!") for w in warnings],
    )


def _order(f: Finding) -> tuple:
    return (SEVERITY_RANK[f.severity], f.rank_priority, RULES.index(f.rule), -f.rank_age, f.subject)


def _override(finding: Finding, config: Config) -> Finding:
    severity = config.severity.get(finding.rule)
    if severity is None or severity == finding.severity:
        return finding
    return finding.model_copy(update={
        "severity": severity,
        "severity_reason": f"set to {severity} in scrum.toml (rule default {finding.severity}: "
                           f"{finding.severity_reason})",
    })


def _source_warnings(snapshot: Snapshot) -> list[str]:
    """Warnings starting with "!" make the report incomplete; the "!" is dropped when shown."""
    warnings = []
    for source in snapshot.sources:
        if not source.ok:
            warnings.append(f"!{source.name}: collection failed ({source.error}); its sections are missing")
        warnings += [f"!{source.name}: {note}" for note in source.notes if source.ok and not source.complete]
        if source.ok and source.complete:
            warnings += [f"{source.name}: {note}" for note in source.notes]
    return warnings


# --- cycle -----------------------------------------------------------------------------


def _cycle_health(ctx: _Context, warnings: list[str]) -> CycleHealth | None:
    cycle = ctx.s.cycle
    if cycle is None:
        return None
    starts_on = ctx.local(cycle.starts_at)
    ends_on = ctx.local(cycle.ends_at - timedelta(microseconds=1))  # Linear ends a cycle at the next midnight
    length = (ends_on - starts_on).days + 1
    elapsed = min(max((ctx.today - starts_on).days, 0), length)
    remaining = min(max((ends_on - ctx.today).days + 1, 0), length)
    ctx.days_remaining = remaining

    missing = [i for i in cycle.issues if i not in ctx.issues]
    if missing:
        warnings.append(f"!linear: {len(missing)} cycle issues weren't read: {', '.join(missing[:5])}")
    issues = [ctx.issues[i] for i in cycle.issues if i in ctx.issues]
    counted = [i for i in issues if i.state_type not in {"canceled", "duplicate"}]
    open_ = [i for i in counted if i.is_open]
    with_estimate = [i for i in counted if i.estimate is not None]
    consistent = bool(counted) and len(with_estimate) / len(counted) >= ctx.t.estimate_coverage
    if counted and not consistent:
        warnings.append(f"linear: only {len(with_estimate)} of {len(counted)} cycle issues have estimates; "
                        "points are not reported")
    grace = cycle.starts_at + timedelta(hours=ctx.t.added_after_start_grace_hours)

    return CycleHealth(
        number=cycle.number, name=cycle.name, starts_on=starts_on, ends_on=ends_on, days_elapsed=elapsed,
        days_remaining=remaining, total=len(counted), excluded=len(issues) - len(counted),
        completed=sum(i.is_done for i in counted),
        completion_by_count=round(100 * sum(i.is_done for i in counted) / len(counted)) if counted else 0,
        in_progress=sum(i.state_type == "started" for i in counted),
        unstarted=sum(i.is_open and i.state_type != "started" for i in counted),
        blocked=sum(is_blocked(i) for i in counted),
        aging_or_inactive=sum(_stale_in_progress(ctx, i) or _long_running(ctx, i) for i in open_),
        estimates_present=len(with_estimate),
        points_total=sum(i.estimate for i in with_estimate) if consistent else None,
        points_completed=sum(i.estimate for i in with_estimate if i.is_done) if consistent else None,
        added_after_start=[i.identifier for i in issues if i.added_to_cycle_at and i.added_to_cycle_at > grace],
        carried_in=None if cycle.carried_in is None else [i for i in cycle.issues if i in set(cycle.carried_in)],
        carryover_risk=[i.identifier for i in open_] if remaining <= ctx.t.cycle_risk_days else [],
    )


def _cycle_findings(ctx: _Context, health: CycleHealth | None) -> Iterator[Finding]:
    if health is None:
        return
    if health.carryover_risk:
        started = [i for i in health.carryover_risk if ctx.issues[i].state_type == "started"]
        sev = H if health.days_remaining <= 1 else M
        yield Finding(
            rule="CYCLE-CARRYOVER-RISK", severity=sev, section="cycle", subject=f"cycle {health.number}",
            title=f"{len(health.carryover_risk)} open issues with {health.days_remaining} days left",
            severity_reason=f"{health.days_remaining} day(s) left (high at 1 or fewer)",
            evidence=[f"{len(started)} in progress, {len(health.carryover_risk) - len(started)} not started",
                      f"open: {', '.join(health.carryover_risk[:10])}"],
            action="Decide now what moves to the next cycle; finish or descope the in-progress ones first.",
        )
    if health.carried_in:
        still_open = [i for i in health.carried_in if ctx.issues.get(i) and ctx.issues[i].is_open]
        if still_open:
            yield Finding(
                rule="CYCLE-CARRIED-IN", severity=L, section="cycle", subject=f"cycle {health.number} carry-in",
                title=f"{len(still_open)} of {len(health.carried_in)} issues carried in are still open",
                severity_reason="informational: repeated carryover is a planning signal",
                evidence=[f"carried in from cycle {ctx.s.cycle.previous_number}: {', '.join(still_open[:10])}"],
                action="At planning, size the next cycle for this carryover or move these back to the backlog.",
            )


# --- issues ----------------------------------------------------------------------------


def _stale_in_progress(ctx: _Context, issue: Issue) -> bool:
    return issue.state_type == "started" and ctx.inactive(issue) >= ctx.t.inactive_days


def _long_running(ctx: _Context, issue: Issue) -> bool:
    days = ctx.in_progress(issue)
    return days is not None and days >= ctx.t.in_progress_days and not _stale_in_progress(ctx, issue)


def _issue_finding(ctx: _Context, issue: Issue, *, rule: str, severity: Severity, reason: str, section: str,
                   title: str, evidence: list[str], action: str) -> Finding:
    return Finding(
        rule=rule, severity=severity, severity_reason=reason, section=section, subject=issue.identifier,
        title=f"{issue.title} — {title}", url=issue.url, source_priority=issue.priority_label,
        evidence=[f"state: {issue.state_name}", *evidence], action=action,
        rank_priority=issue.priority or 5, rank_age=ctx.age(issue),
    )


def _issue_findings(ctx: _Context) -> Iterator[Finding]:
    backlog_stale: list[Issue] = []
    for issue in ctx.s.issues:
        if not issue.is_open:
            continue
        in_cycle = issue.identifier in ctx.in_cycle
        high = issue.priority in HIGH_PRIORITY
        where = f"in cycle {ctx.s.cycle.number}" if in_cycle else "not in the cycle"

        if is_blocked(issue):
            finding = _blocked(ctx, issue, in_cycle=in_cycle, high=high, where=where)
            if finding:
                yield finding

        if issue.due_date and issue.due_date < ctx.today:
            late = (ctx.today - issue.due_date).days
            sev, why = (H, f"Linear priority {issue.priority_label}" if high else where) if high or in_cycle \
                else (M, "overdue, not high priority, not in the cycle")
            yield _issue_finding(ctx, issue, rule="ISSUE-OVERDUE", severity=sev, reason=why, section="blockers",
                                 title=f"overdue {late} days", evidence=[f"due {issue.due_date}", where],
                                 action="Finish it, or move the due date to an honest one.")

        if _stale_in_progress(ctx, issue):
            sev, why = (H, where if in_cycle else f"Linear priority {issue.priority_label}") if in_cycle or high \
                else (M, "in progress outside the cycle")
            yield _issue_finding(
                ctx, issue, rule="ISSUE-INACTIVE", severity=sev, reason=why, section="aging",
                title=f"in progress, untouched {ctx.inactive(issue)} days",
                evidence=[f"last update {ctx.local(issue.updated_at)} ({ctx.inactive(issue)} days)",
                          f"in progress {ctx.in_progress(issue)} days", where],
                action="Resume it today, or move it back to Todo so in-progress means in progress.",
            )
        elif _long_running(ctx, issue):
            yield _issue_finding(
                ctx, issue, rule="ISSUE-LONG-RUNNING", severity=M, section="aging",
                reason=f"in progress {ctx.t.in_progress_days}+ days, though still active",
                title=f"in progress {ctx.in_progress(issue)} days",
                evidence=[f"started {ctx.local(issue.started_at)}", f"last update {ctx.inactive(issue)} days ago",
                          where],
                action="Split it, or name what's left to finish it.",
            )

        if in_cycle and not issue.assignee:
            yield _issue_finding(ctx, issue, rule="ISSUE-UNASSIGNED", severity=M, reason="cycle work with no owner",
                                 section="cycle", title="unassigned", evidence=[where],
                                 action="Assign an owner, or take it out of the cycle.")

        if in_cycle and issue.state_type != "started" and ctx.inactive(issue) >= ctx.t.inactive_days:
            yield _issue_finding(
                ctx, issue, rule="CYCLE-UNTOUCHED", severity=M, section="cycle",
                reason=f"planned work with no change in {ctx.t.inactive_days}+ days",
                title=f"not started, untouched {ctx.inactive(issue)} days",
                evidence=[where, f"last update {ctx.local(issue.updated_at)}"],
                action="Start it or move it out of the cycle.",
            )

        if not in_cycle and issue.state_type != "started" and ctx.inactive(issue) >= ctx.t.backlog_inactive_days:
            backlog_stale.append(issue)

    if backlog_stale:
        oldest = sorted(backlog_stale, key=lambda i: (i.updated_at, i.identifier))
        yield Finding(
            rule="BACKLOG-STALE", severity=L, section="aging", subject="backlog",
            title=f"{len(oldest)} open issues untouched {ctx.t.backlog_inactive_days}+ days",
            severity_reason="backlog hygiene",
            evidence=[f"oldest: {', '.join(f'{i.identifier} ({ctx.inactive(i)}d)' for i in oldest[:5])}"],
            action="Groom: cancel what you won't do, so the backlog stays a list of real options.",
            rank_age=ctx.inactive(oldest[0]),
        )


def _root_blockers(ctx: _Context, issue: Issue) -> list[str]:
    """Follow open blockers to the issues that block without being blocked themselves."""
    roots, seen, todo = set(), {issue.identifier}, list(issue.blocked_by)
    while todo:
        current = todo.pop()
        if current in seen:
            continue
        seen.add(current)
        upstream = ctx.issues[current].blocked_by if current in ctx.issues else []
        if upstream:
            todo += upstream
        else:
            roots.add(current)
    return sorted(roots, key=issue_order)


def _blocked(ctx: _Context, issue: Issue, *, in_cycle: bool, high: bool, where: str) -> Finding | None:
    """Blocked work, told apart from work that is merely sequenced behind other planned work.

    Waiting for an issue that is itself in the cycle and not started yet is a
    dependency, not a crisis: the action is to start the root of the chain.
    It becomes critical when work already in progress has stalled on a
    blocker, or a cycle issue waits on something outside the cycle, while it
    is high priority or the cycle is ending. Not-started backlog work waiting
    on other backlog work is ordinary ordering and isn't reported.
    """
    started = issue.state_type == "started"
    if not in_cycle and not started:
        return None
    blockers = [ctx.issues[b] for b in issue.blocked_by if b in ctx.issues]
    external = [b.identifier for b in blockers if b.identifier not in ctx.in_cycle]
    roots = _root_blockers(ctx, issue)
    ending = ctx.days_remaining is not None and ctx.days_remaining <= ctx.t.cycle_risk_days
    if not in_cycle:
        sev, why = M, "in progress outside the cycle, stalled on a blocker"
    elif (started or external) and (high or ending):
        sev = C
        why = ("in-progress work stalled" if started else f"waits on {', '.join(external)} outside the cycle") + (
            f", Linear priority {issue.priority_label}" if high else f", {ctx.days_remaining} day(s) left")
    elif started or external:
        sev, why = H, "in-progress work stalled" if started else f"waits on {', '.join(external)} outside the cycle"
    else:
        sev, why = M, "sequenced behind other cycle work that hasn't started"
    evidence = [where] + [f"blocked by {b} ({ctx.issues[b].state_name})" if b in ctx.issues else f"blocked by {b}"
                          for b in issue.blocked_by]
    if roots and roots != issue.blocked_by:
        evidence.append(f"root of the chain: {', '.join(roots)}")
    if not issue.blocked_by:
        evidence.append("marked blocked (state or label), no blocking issue linked")
    if not issue.blocked_by:
        action = "Record what it's waiting on, or unblock it."
    else:
        root = roots[0] if roots else issue.blocked_by[0]
        verb = "Finish" if root in ctx.issues and ctx.issues[root].state_type == "started" else "Start"
        action = f"{verb} {root}" + (", the root of the chain." if root not in issue.blocked_by else " first.")
    return _issue_finding(ctx, issue, rule="ISSUE-BLOCKED", severity=sev, reason=why, section="blockers",
                          title="blocked", evidence=evidence, action=action)


def _aging_rows(ctx: _Context) -> list[AgingRow]:
    rows = []
    for issue in ctx.s.issues:
        if not issue.is_open:
            continue
        age = ctx.age(issue)
        reached = [b for b in ctx.t.age_buckets if age >= b]
        rows.append(AgingRow(
            identifier=issue.identifier, title=issue.title, url=issue.url, state_name=issue.state_name,
            state_type=issue.state_type, priority_label=issue.priority_label, assignee=issue.assignee,
            age_days=age, age_bucket=reached[-1] if reached else None, inactive_days=ctx.inactive(issue),
            in_progress_days=ctx.in_progress(issue), in_cycle=issue.identifier in ctx.in_cycle,
        ))
    started_first = {"started": 0, "unstarted": 1, "triage": 2, "backlog": 3}
    return sorted(rows, key=lambda r: (not r.in_cycle, started_first.get(r.state_type, 4), -r.inactive_days,
                                       -r.age_days, r.identifier))


# --- epics -----------------------------------------------------------------------------


def _children_summary(ctx: _Context, identifiers: list[str] | None) -> dict:
    if identifiers is None or any(i not in ctx.issues for i in identifiers):
        return {"complete_data": False, "children": None, "done": None, "open": None, "in_progress": None,
                "unassigned": [], "blocked": [], "aged": [], "outside_cycle": []}
    kids = [ctx.issues[i] for i in identifiers if ctx.issues[i].state_type not in {"canceled", "duplicate"}]
    open_ = [k for k in kids if k.is_open]
    oldest = ctx.t.age_buckets[-1] if ctx.t.age_buckets else 30
    return {
        "complete_data": True, "children": len(kids), "done": sum(k.is_done for k in kids), "open": len(open_),
        "in_progress": sum(k.state_type == "started" for k in open_),
        "unassigned": [k.identifier for k in open_ if not k.assignee],
        "blocked": [k.identifier for k in open_ if is_blocked(k)],
        "aged": [k.identifier for k in open_ if ctx.age(k) >= oldest],
        "outside_cycle": [k.identifier for k in open_ if ctx.s.cycle and k.identifier not in ctx.in_cycle],
    }


def _parents(ctx: _Context) -> Iterable[Issue]:
    for issue in ctx.s.issues:
        if issue.children == [] or issue.state_type in {"canceled", "duplicate"}:
            continue
        if issue.children is None:
            if issue.is_open:
                yield issue  # shown, with its counts marked unavailable
            continue
        open_children = any(ctx.issues.get(c) is None or ctx.issues[c].is_open for c in issue.children)
        if issue.is_open or open_children:
            yield issue


def _epic_rows(ctx: _Context) -> list[EpicRow]:
    rows = [
        EpicRow(kind="project", subject=p.name, title=p.name, url=p.url, status=p.status_name, owner=p.lead,
                **_children_summary(ctx, p.issues))
        for p in ctx.s.projects
    ]
    rows += [
        EpicRow(kind="parent", subject=i.identifier, title=i.title, url=i.url, status=i.state_name, owner=i.assignee,
                **_children_summary(ctx, i.children))
        for i in _parents(ctx)
    ]
    status_order = {"started": 0, "paused": 1, "planned": 2, "backlog": 3}
    projects = {p.name: p for p in ctx.s.projects}
    return sorted(rows, key=lambda r: (
        r.kind != "project",
        status_order.get(projects[r.subject].status_type, 4) if r.kind == "project" else 0,
        -(r.open or 0), r.subject,
    ))


def _epic_findings(ctx: _Context) -> Iterator[Finding]:
    for project in ctx.s.projects:
        summary = _children_summary(ctx, project.issues)
        if not summary["complete_data"]:
            continue
        base = dict(section="epics", subject=project.name, url=project.url, source_priority=None)
        if summary["children"] and summary["open"] == 0:
            yield Finding(rule="EPIC-ALL-DONE-OPEN", severity=M, severity_reason="unclear epic completion",
                          title=f"all {summary['children']} issues done, project still {project.status_name}",
                          evidence=[f"{summary['done']} done, 0 open"],
                          action="Mark the project completed in Linear, or add the work that's missing.", **base)
        if project.target_date and project.target_date < ctx.today and summary["open"]:
            yield Finding(rule="EPIC-TARGET-PASSED", severity=H, severity_reason="epic past its target date",
                          title=f"target {project.target_date} passed with {summary['open']} open",
                          evidence=[f"{summary['done']} of {summary['children']} done",
                                    f"{(ctx.today - project.target_date).days} days past target"],
                          action="Re-plan: cut scope or set a target date you believe.", **base)
        if project.status_type == "started" and summary["open"] and not summary["in_progress"] and \
                not any(i in ctx.in_cycle for i in project.issues):
            yield Finding(rule="EPIC-STALLED", severity=M, severity_reason="started epic with no active work",
                          title=f"started, {summary['open']} open, none in progress or in the cycle",
                          evidence=[f"{summary['done']} of {summary['children']} done"],
                          action="Pull its next issue into the cycle, or pause the project.", **base)

    for parent in _parents(ctx):
        summary = _children_summary(ctx, parent.children)
        if not summary["complete_data"] or not summary["children"]:
            continue
        if parent.is_open and summary["open"] == 0:
            yield _issue_finding(ctx, parent, rule="EPIC-PARENT-OPEN-CHILDREN-DONE", severity=M,
                                 reason="unclear epic completion", section="epics",
                                 title=f"all {summary['children']} sub-issues done, parent open",
                                 evidence=[f"{summary['done']} done"], action="Close the parent, or add what's left.")
        if parent.is_done and summary["open"]:
            yield _issue_finding(ctx, parent, rule="EPIC-PARENT-DONE-CHILDREN-OPEN", severity=M,
                                 reason="parent looks complete while children remain", section="epics",
                                 title=f"done, but {summary['open']} sub-issues open",
                                 evidence=[f"open: {', '.join(i for i in parent.children if ctx.issues[i].is_open)}"],
                                 action="Finish or cancel the open sub-issues, or reopen the parent.")


# --- pull requests ---------------------------------------------------------------------


def _pr_activity(pr: PullRequest) -> datetime:
    return max(filter(None, [pr.updated_at, pr.last_commit_at]))


def _pr_priority(ctx: _Context, pr: PullRequest) -> Issue | None:
    linked = [ctx.issues[i] for i in pr.linear_ids if i in ctx.issues and ctx.issues[i].priority]
    return min(linked, key=lambda i: i.priority) if linked else None


def _pr_findings(ctx: _Context) -> Iterator[Finding]:
    for pr in ctx.s.pull_requests:
        age, inactive = ctx.days(pr.created_at), ctx.days(_pr_activity(pr))
        top = _pr_priority(ctx, pr)
        subject = f"{pr.repo}#{pr.number}"

        def finding(rule, severity, reason, title, evidence, action, pr=pr, top=top, age=age, subject=subject):
            return Finding(rule=rule, severity=severity, severity_reason=reason, section="prs", subject=subject,
                           title=f"{pr.title} — {title}", url=pr.url,
                           source_priority=f"{top.priority_label} ({top.identifier})" if top else None,
                           evidence=[*evidence, f"linked: {', '.join(pr.linear_ids) or 'none'}"], action=action,
                           rank_priority=top.priority if top else 5, rank_age=age)

        if pr.checks in {"FAILURE", "ERROR"}:
            hours = (ctx.s.collected_at - pr.last_commit_at).total_seconds() / 3600 if pr.last_commit_at else 0
            persistent = hours >= ctx.t.ci_persistent_hours
            high = top is not None and top.priority in HIGH_PRIORITY
            sev = C if persistent and high else H
            why = (f"failing {hours:.0f}h on a {top.priority_label}-priority issue" if sev == C
                   else "failing checks" + (" (persistent)" if persistent else " on the latest commit"))
            yield finding("PR-CHECKS-FAILED", sev, why, f"checks {pr.checks.lower()}",
                          [f"last commit {hours:.0f}h before collection"], "Fix the build; nothing merges until it's green.")
        if pr.mergeable == "CONFLICTING":
            yield finding("PR-CONFLICTS", M, "can't merge until conflicts are resolved", "merge conflicts",
                          [f"base {pr.base_ref}" + (f", {pr.behind_by} commits behind" if pr.behind_by else "")],
                          f"Rebase on {pr.base_ref} and resolve conflicts.")
        if pr.review_decision == "APPROVED" and not pr.is_draft:
            green = pr.checks in {None, "SUCCESS"}
            yield finding("PR-APPROVED-UNMERGED", M, "approved work not yet delivered", "approved, not merged",
                          [f"checks: {pr.checks or 'none'}"], "Merge it." if green else "Get checks green, then merge.")
        if not pr.is_draft and inactive >= ctx.t.pr_inactive_days:
            yield finding("PR-STALE", M, f"no activity {ctx.t.pr_inactive_days}+ days", f"no activity {inactive} days",
                          [f"open {age} days", f"last activity {ctx.local(_pr_activity(pr))}"],
                          "Push it over the line, or close it with a note.")
        elif not pr.is_draft and ctx.t.pr_old_days and age >= ctx.t.pr_old_days[0]:
            biggest = max(d for d in ctx.t.pr_old_days if age >= d)
            yield finding("PR-OLD", M if biggest == ctx.t.pr_old_days[-1] else L, f"open {biggest}+ days, still active",
                          f"open {age} days", [f"last activity {inactive} days ago"],
                          "Old but moving: check what's holding the merge.")
        if pr.is_draft and inactive >= ctx.t.draft_inactive_days:
            yield finding("PR-DRAFT-STALE", L, f"draft untouched {ctx.t.draft_inactive_days}+ days",
                          f"draft, untouched {inactive} days", [f"open {age} days"],
                          "Finish it or close it; a parked draft hides its branch from cleanup.")
        if not pr.linear_ids:
            yield finding("PR-UNLINKED", L, f"no {ctx.cfg.team}-N in branch, title or body",
                          "no Linear issue", [f"branch {pr.head_ref}"],
                          f"Add `Fixes {ctx.cfg.team}-N` to the PR body so Linear tracks it.")


def _pr_rows(ctx: _Context) -> list[PRRow]:
    return [
        PRRow(subject=f"{pr.repo}#{pr.number}", title=pr.title, url=pr.url, author=pr.author,
              age_days=ctx.days(pr.created_at), inactive_days=ctx.days(_pr_activity(pr)), is_draft=pr.is_draft,
              review=(pr.review_decision or "none").lower().replace("_", " ")
              + (f", requested: {', '.join(pr.review_requests)}" if pr.review_requests else ""),
              checks=(pr.checks or "none").lower(), mergeable=(pr.mergeable or "unknown").lower(),
              behind_by=pr.behind_by, linear_ids=pr.linear_ids)
        for pr in ctx.s.pull_requests
    ]


# --- branches --------------------------------------------------------------------------

_CANDIDATE = "cleanup candidate"
_REVIEW = "needs human review"


def _branch_findings(ctx: _Context) -> Iterator[Finding]:
    defaults = {r.name: r.default_branch for r in ctx.s.repos}
    thr = ctx.t.branch_inactive_days
    for branch in ctx.s.remote_branches:
        idle = ctx.days(branch.last_commit_at)
        if branch.name == defaults.get(branch.repo) or any(p.state == "OPEN" for p in branch.prs):
            continue
        if idle is None or idle < thr:
            continue
        merged = [p for p in branch.prs if p.state == "MERGED"]
        base = dict(severity=L, section="branches", subject=f"{branch.repo}:{branch.name}", group=branch.repo,
                    url=f"https://github.com/{branch.repo}/tree/{branch.name}", rank_age=idle)
        common = ["remote branch", f"last commit {idle} days ago", "no open PR"]
        delete = f"After checking, delete on GitHub or `git push origin --delete {branch.name}`."
        if merged:
            yield Finding(rule="BRANCH-REMOTE-MERGED", severity_reason=_CANDIDATE, title=f"merged via #{merged[0].number}",
                          evidence=[*common, f"PR #{merged[0].number} merged {ctx.local(merged[0].merged_at)}"
                                    if merged[0].merged_at else f"PR #{merged[0].number} merged"],
                          action=delete, **base)
        elif branch.prs:
            yield Finding(rule="BRANCH-REMOTE-CLOSED-UNMERGED", severity_reason=_REVIEW,
                          title=f"PR #{branch.prs[-1].number} closed without merging",
                          evidence=[*common, "its commits may exist nowhere else"],
                          action="Review the commits; delete only if the work is abandoned or landed another way.",
                          **base)
        elif branch.ahead_of_default == 0:
            yield Finding(rule="BRANCH-REMOTE-CONTAINED", severity_reason=_CANDIDATE,
                          title=f"no PR, fully contained in {defaults.get(branch.repo)}",
                          evidence=[*common, f"0 commits ahead of {defaults.get(branch.repo)}"], action=delete, **base)
        else:
            ahead = "unknown" if branch.ahead_of_default is None else str(branch.ahead_of_default)
            yield Finding(rule="BRANCH-REMOTE-NO-PR", severity_reason=_REVIEW, title=f"no PR, {ahead} commits ahead",
                          evidence=[*common, f"{ahead} commits not in {defaults.get(branch.repo)}"],
                          action="Open a PR for it, or confirm the work is abandoned before deleting.", **base)

    for repo in ctx.s.local_repos:
        yield from _local_findings(ctx, repo)


def _local_findings(ctx: _Context, repo: LocalRepo) -> Iterator[Finding]:
    thr = ctx.t.branch_inactive_days
    fetched = ctx.days(repo.last_fetched_at)
    freshness = f"last fetch {fetched} days ago" if fetched is not None else "last fetch unknown"
    checked_out = {w.branch for w in repo.worktrees if w.branch}
    for branch in repo.branches:
        idle = ctx.days(branch.last_commit_at)
        if branch.name == repo.default_branch or branch.name in checked_out or idle is None or idle < thr:
            continue
        base = dict(severity=L, section="branches", subject=f"{repo.repo}:local:{branch.name}", rank_age=idle,
                    url=None, group=f"{repo.repo} (local {repo.path})")
        common = [f"local branch in {repo.path}", f"last commit {idle} days ago", "not checked out"]
        if branch.merged:
            yield Finding(rule="BRANCH-LOCAL-MERGED", severity_reason=_CANDIDATE,
                          title=f"merged into origin/{repo.default_branch}",
                          evidence=[*common, f"merged into origin/{repo.default_branch} ({freshness})"],
                          action=f"`git -C {repo.path} branch -d {branch.name}` (refuses if not merged).", **base)
        elif branch.upstream_gone:
            yield Finding(rule="BRANCH-LOCAL-GONE", severity_reason=_REVIEW, title="upstream deleted, not merged",
                          evidence=[*common, f"upstream {branch.upstream} is gone",
                                    "not an ancestor of the default branch (squash merge?)"],
                          action=f"Check `git -C {repo.path} log origin/{repo.default_branch}..{branch.name}`; "
                                 "if it landed, `git branch -D` it.", **base)

    by_name = {b.name: b for b in repo.branches}
    for tree in repo.worktrees:
        if tree.is_main:
            continue
        base = dict(severity=L, section="branches", subject=f"{repo.repo}:worktree:{tree.path}", url=None,
                    group=f"{repo.repo} (local {repo.path})")
        if tree.prunable:
            yield Finding(rule="WORKTREE-PRUNABLE", severity_reason=_CANDIDATE, title="worktree directory is gone",
                          evidence=[f"git marks {tree.path} prunable"],
                          action=f"`git -C {repo.path} worktree prune` after confirming the path is gone.", **base)
            continue
        branch = by_name.get(tree.branch) if tree.branch else None
        if branch is None or not (branch.merged or branch.upstream_gone):
            continue
        state = "merged" if branch.merged else "upstream gone"
        if tree.dirty is False and branch.merged:
            yield Finding(rule="WORKTREE-MERGED", severity_reason=_CANDIDATE, title=f"worktree on {state} branch",
                          evidence=[f"branch {tree.branch} {state} ({freshness})", "no uncommitted changes"],
                          action=f"`git -C {repo.path} worktree remove {tree.path}`.", **base)
        else:
            dirty = {True: "has uncommitted changes", False: "clean", None: "uncommitted changes unknown"}[tree.dirty]
            yield Finding(rule="WORKTREE-REVIEW", severity_reason=_REVIEW, title=f"worktree on {state} branch",
                          evidence=[f"branch {tree.branch} {state} ({freshness})", dirty],
                          action=f"Look in {tree.path} before removing it.", **base)
