# Scrum Master report · TEST

Generated 2030-01-10 10:00 EST · data collected 2030-01-10 10:00 EST

Scope: Linear team `TEST`; repositories: `acme/app`

## Cycle 5

| | |
|---|---|
| Dates | 2030-01-06 → 2030-01-12 |
| Days | 4 elapsed, 3 remaining (today included) |
| Issues | 1 of 5 done · **20% by count** |
| Points | not reported (4 of 5 estimated) |
| In progress / not started | 2 / 2 |
| Blocked | 1 |
| Aging or inactive | 1 |
| Added after start | 1: TEST-5 |
| Carried in | 1 |
| Carryover risk | not yet (outside the risk window) |

Completion by count and by points are different measures; neither is a forecast.

- 🟡 **medium** [TEST-4](https://linear.app/t/issue/TEST-4) Payments API credentials — unassigned
  - rule `ISSUE-UNASSIGNED`: cycle work with no owner · Linear: Medium
  - evidence: state: Todo; in cycle 5
  - next: Assign an owner, or take it out of the cycle.
- 🟡 **medium** [TEST-4](https://linear.app/t/issue/TEST-4) Payments API credentials — not started, untouched 12 days
  - rule `CYCLE-UNTOUCHED`: planned work with no change in 7+ days · Linear: Medium
  - evidence: state: Todo; in cycle 5; last update 2029-12-29
  - next: Start it or move it out of the cycle.
- ⚪ **low** `cycle 5 carry-in` 1 of 1 issues carried in are still open
  - rule `CYCLE-CARRIED-IN`: informational: repeated carryover is a planning signal
  - evidence: carried in from cycle 4: TEST-2
  - next: At planning, size the next cycle for this carryover or move these back to the backlog.

## Top actions today

1. 🔴 [TEST-3](https://linear.app/t/issue/TEST-3) Wire payments to the new API — blocked — Start TEST-4 first.
   *ISSUE-BLOCKED: in-progress work stalled, Linear priority High*
2. 🔴 [acme/app#12](https://github.com/acme/app/pull/12) Flaky test fix — checks failure — Fix the build; nothing merges until it's green.
   *PR-CHECKS-FAILED: failing 48h on a High-priority issue*
3. 🟠 [TEST-2](https://linear.app/t/issue/TEST-2) Fix the flaky checkout test — in progress, untouched 9 days — Resume it today, or move it back to Todo so in-progress means in progress.
   *ISSUE-INACTIVE: in cycle 5*

## Aging and inactive issues

- 🟠 **high** [TEST-2](https://linear.app/t/issue/TEST-2) Fix the flaky checkout test — in progress, untouched 9 days
  - rule `ISSUE-INACTIVE`: in cycle 5 · Linear: High
  - evidence: state: In Progress; last update 2030-01-01 (9 days); in progress 20 days; in cycle 5
  - next: Resume it today, or move it back to Todo so in-progress means in progress.
- ⚪ **low** `backlog` 2 open issues untouched 30+ days
  - rule `BACKLOG-STALE`: backlog hygiene
  - evidence: oldest: TEST-6 (60d), TEST-8 (50d)
  - next: Groom: cancel what you won't do, so the backlog stays a list of real options.

In the cycle or in progress:

| Issue | State | Priority | Owner | Age | Inactive | In progress |
|---|---|---|---|---|---|---|
| [TEST-2](https://linear.app/t/issue/TEST-2) Fix the flaky checkout test | In Progress | High | me | 20d | 9d | 20d |
| [TEST-3](https://linear.app/t/issue/TEST-3) Wire payments to the new API | In Progress | High | me | 1d | 1d | 1d |
| [TEST-4](https://linear.app/t/issue/TEST-4) Payments API credentials | Todo | Medium | — | 12d | 12d | — |
| [TEST-5](https://linear.app/t/issue/TEST-5) Release notes | Todo | Medium | me | 2d | 2d | — |

Rest of the open backlog (2) by age: 2 aged 30+ days. Full list in the JSON report.

## Blockers and overdue work

- 🔴 **critical** [TEST-3](https://linear.app/t/issue/TEST-3) Wire payments to the new API — blocked
  - rule `ISSUE-BLOCKED`: in-progress work stalled, Linear priority High · Linear: High
  - evidence: state: In Progress; in cycle 5; blocked by TEST-4 (Todo)
  - next: Start TEST-4 first.
- 🟠 **high** [TEST-5](https://linear.app/t/issue/TEST-5) Release notes — overdue 1 days
  - rule `ISSUE-OVERDUE`: in cycle 5 · Linear: Medium
  - evidence: state: Todo; due 2030-01-09; in cycle 5
  - next: Finish it, or move the due date to an honest one.

## Epics and parent issues

- 🟡 **medium** [TEST-8](https://linear.app/t/issue/TEST-8) Search — all 1 sub-issues done, parent open
  - rule `EPIC-PARENT-OPEN-CHILDREN-DONE`: unclear epic completion · Linear: Medium
  - evidence: state: Todo; 1 done
  - next: Close the parent, or add what's left.

| Epic | Kind | Status | Owner | Done / total | Open | Unassigned | Blocked | Aged | Outside cycle |
|---|---|---|---|---|---|---|---|---|---|
| [Epic p1](https://linear.app/t/project/p1) | project | Started | me | 1 / 2 | 1 | 0 | 1 | 0 | 0 |
| [Epic p2](https://linear.app/t/project/p2) | project | Planned | me | 1 / 3 | 2 | 0 | 0 | 2 | 2 |
| [TEST-8](https://linear.app/t/issue/TEST-8) Search | parent | Todo | me | 1 / 1 | 0 | 0 | 0 | 0 | 0 |

## Open pull requests

| PR | Author | Age | Inactive | Draft | Review | Checks | Mergeable | Behind | Linear |
|---|---|---|---|---|---|---|---|---|---|
| [acme/app#11](https://github.com/acme/app/pull/11) Payments client | me | 16d | 1d | no | none | success | unknown | ? | TEST-3 |
| [acme/app#12](https://github.com/acme/app/pull/12) Flaky test fix | me | 3d | 2d | no | none | failure | unknown | ? | TEST-2 |
| [acme/app#13](https://github.com/acme/app/pull/13) Spike: GraphQL | me | 30d | 20d | yes | none | none | unknown | ? | — |

- 🔴 **critical** [acme/app#12](https://github.com/acme/app/pull/12) Flaky test fix — checks failure
  - rule `PR-CHECKS-FAILED`: failing 48h on a High-priority issue · Linear: High (TEST-2)
  - evidence: last commit 48h before collection; linked: TEST-2
  - next: Fix the build; nothing merges until it's green.
- 🟡 **medium** [acme/app#11](https://github.com/acme/app/pull/11) Payments client — open 16 days
  - rule `PR-OLD`: open 14+ days, still active · Linear: High (TEST-3)
  - evidence: last activity 1 days ago; linked: TEST-3
  - next: Old but moving: check what's holding the merge.
- ⚪ **low** [acme/app#13](https://github.com/acme/app/pull/13) Spike: GraphQL — draft, untouched 20 days
  - rule `PR-DRAFT-STALE`: draft untouched 14+ days
  - evidence: open 30 days; linked: none
  - next: Finish it or close it; a parked draft hides its branch from cleanup.
- ⚪ **low** [acme/app#13](https://github.com/acme/app/pull/13) Spike: GraphQL — no Linear issue
  - rule `PR-UNLINKED`: no TEST-N in branch, title or body
  - evidence: branch feature-13; linked: none
  - next: Add `Fixes TEST-N` to the PR body so Linear tracks it.

## Branch cleanup candidates

Recommendations only. Nothing is deleted; check the evidence, then run the commands yourself.

### acme/app

Candidates (1): [old-feature](https://github.com/acme/app/tree/old-feature) (merged via #9)

```
git push origin --delete old-feature   # from a checkout of acme/app
```

Needs human review (1):

- [experiment](https://github.com/acme/app/tree/experiment) no PR, 4 commits ahead (`BRANCH-REMOTE-NO-PR`): remote branch; last commit 30 days ago; no open PR; 4 commits not in main. Next: Open a PR for it, or confirm the work is abandoned before deleting.

### acme/app (local /src/app)

Candidates (1): `old-feature` (merged into origin/main)

```
git -C /src/app branch -d old-feature
```

Needs human review (1):

- `hotfix` upstream deleted, not merged (`BRANCH-LOCAL-GONE`): local branch in /src/app; last commit 30 days ago; not checked out; upstream origin/hotfix is gone; not an ancestor of the default branch (squash merge?). Next: Check `git -C /src/app log origin/main..hotfix`; if it landed, `git branch -D` it.

## Data quality

| Source | Status | Notes |
|---|---|---|
| linear | complete | — |
| github | complete | — |
| git | complete | — |

- linear: only 4 of 5 cycle issues have estimates; points are not reported
