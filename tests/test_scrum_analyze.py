"""The Scrum Master's rules: definitions, severities and their explanations, on hand-built snapshots."""

from datetime import UTC, datetime

import pytest
from scrum_factory import (
    CONFIG,
    NOW,
    ago,
    branch,
    cycle,
    day,
    issue,
    local_branch,
    local_repo,
    ok,
    pr,
    project,
    snapshot,
)

from cycle_runner.scrum.analyze import analyze
from cycle_runner.scrum.config import Config, RepoConfig, Thresholds
from cycle_runner.scrum.models import SourceStatus, Worktree


def run(snap, config=CONFIG, now=NOW):
    return analyze(snap, config, now=now)


def rules(report, subject=None):
    return [(f.rule, f.severity.value) for f in report.findings if subject is None or f.subject == subject]


def one(report, rule):
    found = [f for f in report.findings if f.rule == rule]
    assert len(found) == 1, found
    return found[0]


# --- age and inactivity ----------------------------------------------------------------


def test_age_and_inactivity_are_separate():
    old_but_active = issue(1, "started", created=40, updated=0, started=3)
    report = run(snapshot(issues=[old_but_active]))
    row = report.aging[0]
    assert (row.age_days, row.inactive_days, row.in_progress_days, row.age_bucket) == (40, 0, 3, 30)
    assert rules(report) == []  # old, but progressing: nothing to flag


def test_age_buckets_use_the_largest_threshold_reached():
    report = run(snapshot(issues=[issue(n, created=d) for n, d in [(1, 2), (2, 3), (3, 13), (4, 14), (5, 90)]]))
    assert {r.identifier: r.age_bucket for r in report.aging} == {
        "TEST-1": None, "TEST-2": 3, "TEST-3": 7, "TEST-4": 14, "TEST-5": 30}


def test_days_are_counted_between_local_dates():
    # 04:30 UTC on Jan 9 is 23:30 on Jan 8 in New York: two local days before Jan 10, not one.
    late_evening = issue(1, created=0).model_copy(update={"created_at": datetime(2030, 1, 9, 4, 30, tzinfo=UTC),
                                                          "updated_at": datetime(2030, 1, 9, 4, 30, tzinfo=UTC)})
    report = run(snapshot(issues=[late_evening]))
    assert report.aging[0].age_days == 2
    utc = run(snapshot(issues=[late_evening]), Config(team="TEST", timezone="UTC"))
    assert utc.aging[0].age_days == 1


def test_inactive_in_progress_issue_in_the_cycle_is_high():
    stale = issue(1, "started", created=20, updated=8)
    report = run(snapshot(issues=[stale], cycle_=cycle(["TEST-1"])))
    found = one(report, "ISSUE-INACTIVE")
    assert found.severity == "high" and found.severity_reason == "in cycle 5"
    assert "last update 2030-01-02 (8 days)" in found.evidence


def test_inactivity_threshold_is_configurable():
    stale = issue(1, "started", created=20, started=6, updated=5)
    assert rules(run(snapshot(issues=[stale]))) == []
    tight = Config(team="TEST", thresholds=Thresholds(inactive_days=5))
    assert rules(run(snapshot(issues=[stale]), tight)) == [("ISSUE-INACTIVE", "medium")]


def test_long_running_but_active_is_not_called_inactive():
    report = run(snapshot(issues=[issue(1, "started", created=30, started=20, updated=1)]))
    assert rules(report) == [("ISSUE-LONG-RUNNING", "medium")]


def test_completed_and_canceled_issues_are_not_active_work():
    done = issue(1, "completed", created=90, updated=60, due_date=day(30))
    canceled = issue(2, "canceled", created=90, updated=60)
    duplicate = issue(3, "duplicate", created=90, updated=60)
    report = run(snapshot(issues=[done, canceled, duplicate]))
    assert rules(report) == [] and report.aging == []


def test_missing_started_at_is_not_invented():
    report = run(snapshot(issues=[issue(1, "unstarted", created=5)]))
    assert report.aging[0].in_progress_days is None


# --- overdue and blocked --------------------------------------------------------------------


def test_overdue_needs_a_past_due_date_on_an_open_issue():
    report = run(snapshot(issues=[
        issue(1, due_date=day(3)), issue(2, due_date=day(0)), issue(3, priority=2, due_date=day(1)),
    ]))
    assert rules(report) == [("ISSUE-OVERDUE", "high"), ("ISSUE-OVERDUE", "medium")]
    assert one(run(snapshot(issues=[issue(1, due_date=day(3))])), "ISSUE-OVERDUE").title.endswith("overdue 3 days")


def test_stalled_in_progress_high_priority_cycle_work_is_critical():
    blocked = issue(1, "started", priority=2, blocked_by=["TEST-2"])
    report = run(snapshot(issues=[blocked, issue(2)], cycle_=cycle(["TEST-1"])))
    found = one(report, "ISSUE-BLOCKED")
    assert found.severity == "critical"
    assert found.severity_reason == "in-progress work stalled, Linear priority High"
    assert found.source_priority == "High"  # Linear's own priority is shown, never replaced
    assert found.action == "Start TEST-2 first."


def test_waiting_on_unstarted_cycle_work_is_sequencing_not_a_crisis():
    chain = [issue(1, blocked_by=["TEST-2"], priority=1), issue(2, blocked_by=["TEST-3"]), issue(3)]
    report = run(snapshot(issues=chain, cycle_=cycle(["TEST-1", "TEST-2", "TEST-3"])))
    first = [f for f in report.findings if f.subject == "TEST-1"][0]
    assert first.severity == "medium"
    assert "root of the chain: TEST-3" in first.evidence
    assert first.action == "Start TEST-3, the root of the chain."


def test_cycle_work_waiting_outside_the_cycle_is_high():
    report = run(snapshot(issues=[issue(1, blocked_by=["TEST-9"]), issue(9, "backlog")], cycle_=cycle(["TEST-1"])))
    found = one(report, "ISSUE-BLOCKED")
    assert (found.severity, found.severity_reason) == ("high", "waits on TEST-9 outside the cycle")


def test_blocked_becomes_critical_when_the_cycle_is_ending():
    blocked = issue(1, "started", blocked_by=["TEST-2"])
    ending = cycle(["TEST-1"], starts=5)  # last day tomorrow: 2 days left, today included
    found = one(run(snapshot(issues=[blocked, issue(2)], cycle_=ending)), "ISSUE-BLOCKED")
    assert found.severity == "critical" and "2 day(s) left" in found.severity_reason


def test_backlog_waiting_on_backlog_is_not_reported():
    report = run(snapshot(issues=[issue(1, "backlog", blocked_by=["TEST-2"]), issue(2, "backlog")]))
    assert "ISSUE-BLOCKED" not in [r for r, _ in rules(report)]


def test_blocked_state_without_a_relation_is_still_blocked():
    report = run(snapshot(issues=[issue(1, "started", state_name="Blocked")], cycle_=cycle(["TEST-1"])))
    found = one(report, "ISSUE-BLOCKED")
    assert "marked blocked (state or label), no blocking issue linked" in found.evidence


# --- cycle -------------------------------------------------------------------------------


def test_cycle_health_counts_by_issue_and_excludes_canceled():
    issues = [issue(1, "completed", estimate=3), issue(2, "started", estimate=2), issue(3, estimate=1),
              issue(4, "canceled", estimate=8)]
    health = run(snapshot(issues=issues, cycle_=cycle([i.identifier for i in issues]))).cycle
    assert (health.total, health.excluded, health.completed, health.completion_by_count) == (3, 1, 1, 33)
    assert (health.in_progress, health.unstarted) == (1, 1)
    assert (health.points_completed, health.points_total) == (3, 6)
    assert (health.starts_on, health.ends_on) == (day(4), day(-2))
    assert (health.days_elapsed, health.days_remaining) == (4, 3)


def test_points_are_hidden_when_estimates_are_inconsistent():
    issues = [issue(1, "completed", estimate=3), issue(2, estimate=None)]
    report = run(snapshot(issues=issues, cycle_=cycle(["TEST-1", "TEST-2"])))
    assert report.cycle.points_total is None and report.cycle.estimates_present == 1
    assert any("only 1 of 2 cycle issues have estimates" in w for w in report.warnings)


def test_added_after_start_allows_a_grace_period():
    c = cycle(["TEST-1", "TEST-2"])
    planned = issue(1, added_to_cycle_at=c.starts_at.replace(minute=10))
    late = issue(2, added_to_cycle_at=ago(1))
    assert run(snapshot(issues=[planned, late], cycle_=c)).cycle.added_after_start == ["TEST-2"]


def test_carryover_risk_is_high_on_the_last_day():
    report = run(snapshot(issues=[issue(1, "started", updated=0)], cycle_=cycle(["TEST-1"], starts=6)))
    assert report.cycle.days_remaining == 1
    assert rules(report, "cycle 5") == [("CYCLE-CARRYOVER-RISK", "high")]


def test_carry_in_and_carryover_risk():
    c = cycle(["TEST-1", "TEST-2"], starts=5, carried_in=["TEST-1", "TEST-7"])
    report = run(snapshot(issues=[issue(1, "started", updated=0), issue(2, "completed")], cycle_=c))
    assert report.cycle.carried_in == ["TEST-1"]
    assert report.cycle.carryover_risk == ["TEST-1"]
    assert rules(report, "cycle 5") == [("CYCLE-CARRYOVER-RISK", "medium")]
    assert rules(report, "cycle 5 carry-in") == [("CYCLE-CARRIED-IN", "low")]


def test_unknown_carry_in_stays_unknown():
    report = run(snapshot(issues=[issue(1)], cycle_=cycle(["TEST-1"], carried_in=None)))
    assert report.cycle.carried_in is None


def test_cycle_issue_missing_from_the_snapshot_makes_the_report_partial():
    report = run(snapshot(issues=[issue(1)], cycle_=cycle(["TEST-1", "TEST-2"])))
    assert not report.complete
    assert any("1 cycle issues weren't read: TEST-2" in w for w in report.warnings)


def test_unassigned_and_untouched_cycle_work():
    report = run(snapshot(issues=[issue(1, assignee=None, created=10)], cycle_=cycle(["TEST-1"])))
    assert sorted(rules(report)) == [("CYCLE-UNTOUCHED", "medium"), ("ISSUE-UNASSIGNED", "medium")]


def test_backlog_staleness_is_one_aggregate_finding():
    report = run(snapshot(issues=[issue(n, "backlog", created=40 + n) for n in range(1, 8)]))
    found = one(report, "BACKLOG-STALE")
    assert found.title == "7 open issues untouched 30+ days"
    assert found.evidence == ["oldest: TEST-7 (47d), TEST-6 (46d), TEST-5 (45d), TEST-4 (44d), TEST-3 (43d)"]


# --- epics ---------------------------------------------------------------------------------


def test_project_children_are_counted_when_complete():
    issues = [issue(1, "completed"), issue(2, "started", assignee=None), issue(3, blocked_by=["TEST-2"], created=40)]
    report = run(snapshot(issues=issues, projects=[project("p1", ["TEST-1", "TEST-2", "TEST-3"])],
                          cycle_=cycle(["TEST-2"])))
    row = report.epics[0]
    assert (row.children, row.done, row.open, row.in_progress) == (3, 1, 2, 1)
    assert (row.unassigned, row.blocked, row.aged, row.outside_cycle) == (["TEST-2"], ["TEST-3"], ["TEST-3"], ["TEST-3"])


def test_partial_child_data_hides_counts():
    report = run(snapshot(issues=[issue(1)], projects=[project("p1", ["TEST-1", "TEST-99"]), project("p2", None)]))
    assert [(r.complete_data, r.children) for r in report.epics] == [(False, None), (False, None)]
    assert not any(f.section == "epics" for f in report.findings)


def test_epic_all_done_but_open_and_target_passed():
    done = run(snapshot(issues=[issue(1, "completed")], projects=[project("p1", ["TEST-1"])]))
    assert rules(done) == [("EPIC-ALL-DONE-OPEN", "medium")]
    late = run(snapshot(issues=[issue(1, "started", updated=0)],
                        projects=[project("p1", ["TEST-1"], target=day(5))]))
    assert ("EPIC-TARGET-PASSED", "high") in rules(late)


def test_started_epic_with_no_active_work_is_stalled():
    report = run(snapshot(issues=[issue(1), issue(2, "completed")], projects=[project("p1", ["TEST-1", "TEST-2"])],
                          cycle_=cycle([])))
    assert ("EPIC-STALLED", "medium") in rules(report)


def test_parent_issue_completion_mismatches():
    parent_open = issue(1, children=["TEST-2", "TEST-3"])
    parent_done = issue(4, "completed", children=["TEST-5"])
    kids = [issue(2, "completed", parent="TEST-1"), issue(3, "canceled", parent="TEST-1"), issue(5, parent="TEST-4")]
    report = run(snapshot(issues=[parent_open, *kids, parent_done]))
    assert rules(report, "TEST-1") == [("EPIC-PARENT-OPEN-CHILDREN-DONE", "medium")]
    assert rules(report, "TEST-4") == [("EPIC-PARENT-DONE-CHILDREN-OPEN", "medium")]
    rows = {r.subject: r for r in report.epics if r.kind == "parent"}
    assert (rows["TEST-1"].children, rows["TEST-1"].done) == (1, 1)  # canceled children don't count


# --- pull requests --------------------------------------------------------------------------


def test_old_is_not_stale():
    report = run(snapshot(prs=[pr(1, created=15, updated=1, linear_ids=["TEST-1"])]))
    assert rules(report) == [("PR-OLD", "medium")]
    report = run(snapshot(prs=[pr(1, created=8, updated=1, linear_ids=["TEST-1"])]))
    assert rules(report) == [("PR-OLD", "low")]
    report = run(snapshot(prs=[pr(1, created=15, updated=9, linear_ids=["TEST-1"])]))
    assert rules(report) == [("PR-STALE", "medium")]


def test_a_recent_commit_counts_as_activity():
    report = run(snapshot(prs=[pr(1, created=10, updated=9, commit=1, linear_ids=["TEST-1"])]))
    assert report.pull_requests[0].inactive_days == 1


def test_persistent_ci_failure_on_a_high_priority_pr_is_critical():
    linked = issue(1, priority=2)
    failing = pr(1, created=3, updated=2, commit=2, checks="FAILURE", linear_ids=["TEST-1"])
    found = one(run(snapshot(issues=[linked], prs=[failing])), "PR-CHECKS-FAILED")
    assert found.severity == "critical" and found.source_priority == "High (TEST-1)"
    fresh = pr(1, created=3, updated=0, commit=0, checks="FAILURE", linear_ids=["TEST-1"])
    assert one(run(snapshot(issues=[linked], prs=[fresh])), "PR-CHECKS-FAILED").severity == "high"


def test_approved_unmerged_draft_stale_and_unlinked():
    report = run(snapshot(prs=[
        pr(1, review_decision="APPROVED", checks="SUCCESS", linear_ids=["TEST-1"]),
        pr(2, is_draft=True, created=20, linear_ids=["TEST-2"]),
        pr(3),
    ]))
    assert one(report, "PR-APPROVED-UNMERGED").action == "Merge it."
    assert one(report, "PR-DRAFT-STALE").subject == "acme/app#2"
    assert one(report, "PR-UNLINKED").subject == "acme/app#3"


def test_conflicts_are_flagged():
    assert rules(run(snapshot(prs=[pr(1, mergeable="CONFLICTING", linear_ids=["TEST-1"])]))) == [
        ("PR-CONFLICTS", "medium")]


# --- branches -------------------------------------------------------------------------------


def test_remote_branch_candidates_and_reviews():
    report = run(snapshot(branches=[
        branch("main", idle=100),
        branch("merged", prs=[(1, "MERGED")]),
        branch("closed", prs=[(2, "CLOSED")]),
        branch("contained", ahead=0),
        branch("unmerged", ahead=3),
        branch("open-pr", prs=[(3, "OPEN"), (4, "MERGED")]),
        branch("recent", idle=2, prs=[(5, "MERGED")]),
    ]))
    got = {f.subject.split(":")[1]: (f.rule, f.severity_reason) for f in report.findings}
    assert got == {
        "merged": ("BRANCH-REMOTE-MERGED", "cleanup candidate"),
        "closed": ("BRANCH-REMOTE-CLOSED-UNMERGED", "needs human review"),
        "contained": ("BRANCH-REMOTE-CONTAINED", "cleanup candidate"),
        "unmerged": ("BRANCH-REMOTE-NO-PR", "needs human review"),
    }
    assert all(f.severity == "low" for f in report.findings)


def test_local_branches_and_worktrees():
    repo = local_repo(
        [local_branch("main", merged=True), local_branch("done", merged=True),
         local_branch("squashed", gone=True), local_branch("wip"), local_branch("in-tree", merged=True),
         local_branch("dirty-tree", merged=True), local_branch("fresh", idle=1, merged=True)],
        [Worktree(path="/src/app", branch="main", is_main=True, dirty=False),
         Worktree(path="/src/app-wt", branch="in-tree", is_main=False, dirty=False),
         Worktree(path="/src/app-dirty", branch="dirty-tree", is_main=False, dirty=True),
         Worktree(path="/src/gone", branch=None, is_main=False, prunable=True)],
    )
    report = run(snapshot(local=[repo]))
    got = sorted((f.rule, f.subject.rsplit(":", 1)[1]) for f in report.findings)
    assert got == [
        ("BRANCH-LOCAL-GONE", "squashed"),
        ("BRANCH-LOCAL-MERGED", "done"),
        ("WORKTREE-MERGED", "/src/app-wt"),
        ("WORKTREE-PRUNABLE", "/src/gone"),
        ("WORKTREE-REVIEW", "/src/app-dirty"),
    ]
    merged = one(report, "BRANCH-LOCAL-MERGED")
    assert "merged into origin/main (last fetch 1 days ago)" in merged.evidence
    assert merged.action == "`git -C /src/app branch -d done` (refuses if not merged)."


# --- ranking, overrides, completeness ----------------------------------------------------------


def test_top_actions_are_the_three_most_severe_distinct_subjects_above_low():
    issues = [
        issue(1, "started", priority=2, blocked_by=["TEST-9"]),  # critical
        issue(2, "started", updated=10, priority=4),  # high (in cycle)
        issue(3, "started", updated=10, priority=1),  # high, urgent: ranks before TEST-2
        issue(4, due_date=day(1)),  # medium overdue... and in the cycle: high
        issue(9),
    ]
    report = run(snapshot(issues=issues, cycle_=cycle(["TEST-1", "TEST-2", "TEST-3", "TEST-4"])))
    assert [f.subject for f in report.top_actions] == ["TEST-1", "TEST-3", "TEST-4"]
    assert len({f.subject for f in report.top_actions}) == 3


def test_low_findings_never_reach_the_top_actions():
    report = run(snapshot(prs=[pr(1)]))
    assert rules(report) == [("PR-UNLINKED", "low")] and report.top_actions == []


def test_severity_override_is_applied_and_explained():
    config = Config(team="TEST", severity={"PR-UNLINKED": "high"})
    found = one(run(snapshot(prs=[pr(1)]), config), "PR-UNLINKED")
    assert found.severity == "high"
    assert found.severity_reason.startswith("set to high in scrum.toml (rule default low:")


def test_unknown_severity_in_config_is_rejected():
    with pytest.raises(ValueError):
        Config(severity={"PR-UNLINKED": "urgent"})


def test_a_failed_source_makes_the_report_partial():
    sources = [*ok("linear", "git"), SourceStatus(name="github", ok=False, complete=False, error="token missing")]
    report = run(snapshot(sources=sources))
    assert not report.complete
    assert report.warnings == ["github: collection failed (token missing); its sections are missing"]


def test_a_partial_source_makes_the_report_partial():
    sources = [*ok("github", "git"), SourceStatus(name="linear", ok=True, complete=False, notes=["cap reached"])]
    assert not run(snapshot(sources=sources)).complete


def test_analysis_is_deterministic():
    snap = snapshot(issues=[issue(n, "started", updated=n) for n in range(1, 20)], prs=[pr(1), pr(2, created=30)],
                    cycle_=cycle([f"TEST-{n}" for n in range(1, 20)]))
    assert run(snap).model_dump_json() == run(snap).model_dump_json()
    shuffled = snap.model_copy(update={"issues": list(reversed(snap.issues))})
    assert [f.subject for f in run(shuffled).findings] == [f.subject for f in run(snap).findings]


def test_repo_config_requires_owner_and_name():
    with pytest.raises(ValueError):
        RepoConfig(github="just-a-name")
