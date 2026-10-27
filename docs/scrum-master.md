# Scrum Master

A read-only engineering health report built from Linear, GitHub and local git
(SB-1385). You run it by hand, read the result, and decide what to do. It
writes nothing anywhere, sends nothing, and uses no model: the same snapshot
always gives the same report.

```
collect ─► snapshot.json ─► analyze ─► report.json ─► render (terminal | markdown | json)
 Linear      facts only      rules      findings,      presentation only
 GitHub      + completeness  (pure)     evidence,
 git                                    severity reasons
```

Each stage is a plain function over a versioned Pydantic document, and each
can be run on its own (`scrum-master collect | analyze | render`). That keeps
them easy to wrap later (a k3s CronJob, Telegram) without
changing them. None of that is in this version.

## Install and set up

```bash
uv sync                     # installs the `scrum` group (Typer, Rich) with the rest
gh auth status              # GitHub: the gh CLI's login is used unless SCRUM_GITHUB_TOKEN is set
```

**Linear** uses the existing read-only Cycle Runner app (`LINEAR_CLIENT_ID`,
`LINEAR_CLIENT_SECRET` in `.env`). Its token is requested with scope `read`,
so Linear refuses writes, and the client also rejects any GraphQL document
that isn't a query.

**GitHub** reads `SCRUM_GITHUB_TOKEN` if it is set, and otherwise the token
the GitHub CLI is logged in with (`gh auth token`). That token can usually
write, so `doctor` warns about it; the client still only reads (see
*Read-only guarantees*). For a token that can't write, which anything running
unattended should use, make a fine-grained personal access token limited to
the repositories in `scrum.toml`, with **read-only** permissions: Contents,
Pull requests and Commit statuses (Metadata is implied). Store it as
`op://agents/cycle-runner-github-read/credential`,
`cp .env.scrum.example .env.scrum`, and add `--env-file .env.scrum` to the
commands below. Don't reuse `CYCLE_RUNNER_GITHUB_TOKEN`: that one can push.

**Local git** needs nothing. Checkouts listed under `local` in `scrum.toml`
are inspected with read-only commands only (see *Read-only guarantees*).

## Run it

Every live command runs under `op run`, for the Linear credentials:

```bash
alias sm='op run --env-file .env -- uv run scrum-master'

sm doctor                         # config, credentials, access; prints no secrets
sm report                         # the whole report in the terminal
sm report -f markdown -o report.md
sm report -f json -o report.json --strict    # exit 2 if anything is partial
sm cycle                          # one section, with evidence for every finding
sm aging | sm epics | sm prs | sm branches
sm cycle --cycle 12               # a past cycle
sm prs --repo silverbeer/missing-table      # scope GitHub to one repo
uv run scrum-master config        # effective config; no credentials needed
```

Snapshots make a run reproducible and let you tune rules without re-reading
the APIs:

```bash
sm collect -o snap.json                                   # stage 1 (the only stage that reads the network)
uv run scrum-master report --offline snap.json            # any command, from the snapshot
uv run scrum-master analyze snap.json -o report.json      # stage 2
uv run scrum-master render report.json -f markdown        # stage 3
```

A full live collection takes about 45 seconds (roughly 60 Linear queries and
3 GitHub calls per repository).

Options shared by the report commands: `--format terminal|markdown|json`,
`--output PATH`, `--offline SNAPSHOT`, `--team KEY`, `--repo OWNER/NAME`
(repeatable), `--cycle N`, `--strict`, `--config PATH`. Global:
`--log-level info` prints structured JSON logs on stderr.

Exit codes: `0` done, `1` couldn't run (Linear unavailable, bad config or
input), `2` partial report with `--strict`, `3` doctor found a failing check.

## Sample output

From a synthetic workspace (the test fixture), not real data:
[Markdown](scrum-master/sample-report.md) ·
[JSON](scrum-master/sample-report.json) ·
[terminal](scrum-master/sample-terminal.txt) ·
[snapshot](scrum-master/sample-snapshot.json).

The terminal `report` shows one line per finding so that the top actions stay
visible; the section commands and Markdown show the evidence and next step
for each.

## Configuration

`scrum.toml` at the repository root (or `--config`, or `$SCRUM_CONFIG`). It
holds no secrets; `scrum-master config` prints the effective values. This
repository is public, so private repositories go in `scrum.local.toml` beside
it (gitignored, same format): its `[[repos]]` are added to the allowlist and
its other keys override.

| Key | Default | Meaning |
|---|---|---|
| `team` | `SB` | The one Linear team in scope |
| `timezone` | `America/New_York` | Days are counted between local calendar dates here |
| `thresholds.age_buckets` | `[3, 7, 14, 30]` | Issue age buckets, days |
| `thresholds.inactive_days` | `7` | In progress with no update this long is inactive |
| `thresholds.in_progress_days` | `14` | In progress this long, even if active |
| `thresholds.backlog_inactive_days` | `30` | Backlog hygiene |
| `thresholds.pr_old_days` | `[7, 14]` | PR age: low, then medium |
| `thresholds.pr_inactive_days` | `7` | PR with no activity this long is stale |
| `thresholds.draft_inactive_days` | `14` | Parked drafts |
| `thresholds.branch_inactive_days` | `14` | Cleanup candidates must be at least this idle |
| `thresholds.ci_persistent_hours` | `24` | Failing checks on a commit this old are persistent |
| `thresholds.cycle_risk_days` | `2` | With this many days left, open cycle work is carryover risk |
| `thresholds.estimate_coverage` | `0.9` | Points are summed only if this share of cycle issues is estimated |
| `thresholds.added_after_start_grace_hours` | `1` | Linear adds planned issues just after a cycle starts |
| `severity.<RULE>` | — | Override a rule's severity; the report says it was overridden |
| `github.token_env` | `SCRUM_GITHUB_TOKEN` | Where the GitHub token is read from |
| `[[repos]] github`, `local` | — | The allowlist; `local` adds branch and worktree checks |

## Definitions

All day counts are between local calendar dates in the configured time zone.

- **Age**: today minus the creation date.
- **Inactivity**: today minus the latest activity timestamp available. For
  an issue that is Linear's `updatedAt`, which moves on any change, including
  automation, so inactivity can be understated but never overstated. Linear's
  own automatic cycle carryover does *not* move it. For a PR it is the later
  of `updatedAt` and the last commit.
- **In progress for**: today minus `startedAt`.
- **Overdue**: a due date before today on an open issue.
- **Open**: not completed, canceled or duplicate.
- **Blocked**: an open `blocks` relation from another open issue, a state
  named "Blocked", or a `blocked` label.
- **Carried in**: Linear's `uncompletedIssuesUponClose` for the previous
  cycle, intersected with this cycle's issues.
- **Added after start**: `addedToCycleAt` later than the cycle start plus the
  grace period. Linear keeps only the latest add, so an issue removed and
  re-added counts from the re-add.
- **Carryover risk**: open cycle issues once `cycle_risk_days` or fewer remain.
- **Cycle days**: Linear ends a cycle at the next midnight, so the last day is
  the day before `endsAt`. *Elapsed* is days before today; *remaining*
  includes today.
- **Completion** is reported by issue count, and separately by points only
  when estimates are consistent. Neither is a forecast, and no delivery date is
  predicted.
- **Epics**: in this workspace epics are Linear **projects** (as the
  linear-crud skill defines them). Parent issues with sub-issues are reported
  alongside them, labelled `parent`. Both are read from the actual
  relationships, never inferred from names.

## Rules

Severity: **critical** threatens cycle delivery now; **high** needs attention
today; **medium** is hygiene that is costing you; **low** is cleanup. Every
finding records the reason for its severity and shows Linear's own priority
next to it, never in place of it. Ties are broken by Linear priority, then the
order below, then age. The top three actions are the first three distinct
subjects above low.

| Rule | Section | Severity | When |
|---|---|---|---|
| `ISSUE-BLOCKED` | blockers | critical: in-progress cycle work stalled, or cycle work waiting outside the cycle, when high priority or the cycle is ending; high: the same otherwise; medium: in progress outside the cycle, or waiting on unstarted cycle work (sequencing) | blocked, in the cycle or in progress. Backlog waiting on backlog is ordinary ordering and isn't reported. The root of a chain of blockers is named |
| `PR-CHECKS-FAILED` | prs | critical: failing ≥ 24h and linked to an urgent/high issue; else high | last commit's check rollup is FAILURE or ERROR |
| `ISSUE-OVERDUE` | blockers | high: urgent/high priority or in the cycle; else medium | past due date |
| `CYCLE-CARRYOVER-RISK` | cycle | high with ≤ 1 day left; else medium | open cycle work inside the risk window |
| `ISSUE-INACTIVE` | aging | high: in the cycle or urgent/high; else medium | in progress, no update for `inactive_days` |
| `EPIC-TARGET-PASSED` | epics | high | project target date passed with open issues |
| `PR-CONFLICTS` | prs | medium | mergeable is CONFLICTING |
| `PR-APPROVED-UNMERGED` | prs | medium | approved, not a draft |
| `PR-STALE` | prs | medium | not a draft, no activity for `pr_inactive_days` |
| `ISSUE-UNASSIGNED` | cycle | medium | open cycle issue with no assignee |
| `CYCLE-UNTOUCHED` | cycle | medium | cycle issue not started and not updated for `inactive_days` |
| `ISSUE-LONG-RUNNING` | aging | medium | in progress `in_progress_days`+, but still active |
| `EPIC-ALL-DONE-OPEN` | epics | medium | every project issue closed, project still open |
| `EPIC-PARENT-OPEN-CHILDREN-DONE` | epics | medium | every sub-issue closed, parent open |
| `EPIC-PARENT-DONE-CHILDREN-OPEN` | epics | medium | parent done, sub-issues open |
| `EPIC-STALLED` | epics | medium | started project, open issues, none in progress or in the cycle |
| `PR-OLD` | prs | low at 7+ days, medium at 14+ | open a long time but still active (old, not stale) |
| `PR-DRAFT-STALE` | prs | low | draft with no activity for `draft_inactive_days` |
| `PR-UNLINKED` | prs | low | no `SB-N` in the branch, title or body |
| `CYCLE-CARRIED-IN` | cycle | low | issues carried in from the previous cycle still open |
| `BACKLOG-STALE` | aging | low | one aggregate: open, not started, outside the cycle, untouched `backlog_inactive_days` |
| `BRANCH-REMOTE-MERGED` | branches | low, candidate | no open PR, a merged PR, idle |
| `BRANCH-REMOTE-CONTAINED` | branches | low, candidate | no PR, 0 commits ahead of the default branch, idle |
| `BRANCH-REMOTE-CLOSED-UNMERGED` | branches | low, review | PR closed without merging; commits may exist nowhere else |
| `BRANCH-REMOTE-NO-PR` | branches | low, review | no PR, commits ahead (or unknown), idle |
| `BRANCH-LOCAL-MERGED` | branches | low, candidate | merged into `origin/<default>` as of the last fetch, not checked out, idle |
| `BRANCH-LOCAL-GONE` | branches | low, review | upstream deleted but not an ancestor of the default branch (often a squash merge) |
| `WORKTREE-MERGED` | branches | low, candidate | worktree on a merged branch, no uncommitted changes |
| `WORKTREE-REVIEW` | branches | low, review | worktree on a merged/gone branch with changes, or unknown |
| `WORKTREE-PRUNABLE` | branches | low, candidate | git marks the worktree prunable |

Branch findings are recommendations only. "Candidate" means the evidence
shows the work landed; "review" means the tool can't establish that, so a
person must look. Neither is called safe to delete. The report groups them by
repository and prints the commands for you to run; `git branch -d` is chosen
because it refuses to delete unmerged work.

## Data quality and partial reports

Linear is required: if it can't be read there is no report (exit 1). GitHub
and local git are optional: a failure is recorded in the snapshot's
`sources`, the report is marked **PARTIAL** at the top in every format and
`complete: false` in JSON, and the missing sections say why. `--strict` turns
a partial report into exit code 2. Pagination caps, truncated child lists,
unreadable related issues, and failed compares are all recorded as notes and
also make the report partial.

Transient failures (connection errors, HTTP 429 and 5xx, Linear's
`RATELIMITED`, GitHub's secondary rate limits) are retried twice with backoff
(2s, 4s, or `Retry-After`), with 15–20s request timeouts.

## Read-only guarantees

- **Linear**: a `read`-scoped app token; the client rejects mutations and
  subscriptions; tests assert every document sent is a query.
- **GitHub**: the client has only `get()` (HTTP GET) and `query()` (GraphQL,
  mutations rejected); tests assert every request is a GET or a GraphQL query.
  This holds even with the `gh` CLI's token, which can write; `gh` itself is
  only ever run as `gh auth token`.
- **git**: an allowlist (`for-each-ref`, `branch --merged`,
  `worktree list --porcelain`, `status --porcelain`, `symbolic-ref`,
  `rev-parse --git-common-dir`); delete, move and force flags are refused;
  `GIT_OPTIONAL_LOCKS=0` so `status` doesn't touch the index; no fetch. A test
  checks that refs, worktrees and the index are unchanged after collection.
- **Secrets**: never logged, never in errors, hidden from `repr()`; `config`
  and `doctor` name the variables and never print values. Tests check each.

## JSON schemas

Both documents carry `schema_version` (currently 1). Generated schemas:
[report.schema.json](scrum-master/report.schema.json),
[snapshot.schema.json](scrum-master/snapshot.schema.json). Tests fail if the
models change without regenerating them (`SCRUM_UPDATE_GOLDEN=1 uv run pytest
tests/test_scrum_report.py`).

Each finding has: `rule`, `severity`, `severity_reason`, `section`,
`subject`, `title`, `url`, `source_priority` (Linear's), `evidence` (list),
`action`, and `group` for branches. `rank_priority` and `rank_age` are kept
for ordering.

## Tests

```bash
uv run pytest tests/test_scrum_*.py          # offline: mocked APIs, temp git repos
op run --env-file .env -- uv run pytest -m linear tests/test_scrum_live.py
op run --env-file .env -- uv run pytest -m "linear and github_read" tests/test_scrum_live.py
```

The live tests are read-only and assert shape, not workspace contents. CI
runs only the offline set.

## What the APIs don't give us (known limitations)

- **Issue activity** is Linear's `updatedAt` only. Comments, attachments and
  history events are not read separately, and `updatedAt` also moves on
  automated changes. Issue history (`history`) is available but not used yet.
- **Carryover beyond one cycle** isn't tracked: only the previous cycle's
  uncompleted-on-close list is read, so "carried over three times" isn't known.
- **Added after start** uses `addedToCycleAt`, which keeps only the most
  recent add.
- **Project priority** isn't read, so "critical-path epic" isn't a rule; a
  project past its target date is the high-severity epic signal.
- **Child lists** are read 50 per issue; a larger one is marked partial.
- **CI status with a fine-grained token is unverified.** GitHub documents
  calling the Checks API as a known limitation of fine-grained tokens, and
  `statusCheckRollup` includes check runs. If PRs that clearly have CI show
  checks `none` with the new token, that's why; it was verified here only
  with a classic token. Sources: [GitHub: managing personal access
  tokens](https://docs.github.com/en/authentication/keeping-your-account-and-data-secure/managing-your-personal-access-tokens).
- **GitHub mergeability** is computed lazily by GitHub, so it often reads
  `UNKNOWN` for PRs nobody has opened recently.
- **"Behind the default branch"** comes from one REST compare per open PR,
  against the PR's base; it's `?` when GitHub can't compare (e.g. a deleted
  fork).
- **Persistent CI failure** means the latest commit has been failing for 24h;
  check-run history isn't read.
- **Local "merged"** is as of the last fetch (the report shows how old it is),
  because the tool never fetches. Squash-merged branches show as "upstream
  gone, not merged" and go to review.
- **Review activity** isn't separate from a PR's `updatedAt`.
- **No forecast**: there's no throughput history yet, so no delivery date is
  predicted.

## Not in this version

No container, no Kubernetes, no schedule, no Telegram, no notification
history, no Swamp, no LLM summary. The stage boundaries are where those
attach later. A Swamp wrapper was tried and dropped (SB-1386, SB-1442); see
[swamp-spike.md](swamp-spike.md).
