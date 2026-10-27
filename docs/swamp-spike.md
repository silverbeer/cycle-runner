# Swamp spike: Scrum Master as behaviors (SB-1386)

Can [Swamp](https://swamp-club.com/manual) run the Scrum Master pipeline
better than the plain CLI does? This spike wraps the three `scrum-master`
stages as one Swamp behavior and chains them in a workflow. It is read-only,
like the CLI: nothing is written to Linear, GitHub or git.

**Verdict: don't adopt it for cycle-runner. Dropped 2026-10-27 (see the end of this doc).** The workflow works, and
versioned stage data with an assert gate is nice to have. But the CLI already
has clean stage boundaries, and Swamp adds a hosted account, a second language
and a runtime auth dependency for a three-step linear pipeline. None of that
helps with what matters next, which is good-looking scheduled notifications.

## What was built

All of it lives in `swamp/`, a Swamp repo initialized next to the Python code
so its generated `CLAUDE.md` and `.claude/` don't touch the project root.

| File | What it is |
| --- | --- |
| `extensions/behaviors/scrum_master.ts` | Behavior type `@silverbeer/scrum-master`: methods `collect`, `analyze`, `render`. Each runs one CLI stage and stores its JSON as Swamp data (`snapshot`, `report`, `rendered`) |
| `models/@silverbeer/scrum-master/scrum-master.yaml` | The behavior instance. Linear credentials are `vault.get` references, resolved per step |
| `vaults/@swamp/1password/…yaml` | Vault `op-agents`, reading the 1Password `agents` vault through `op` |
| `workflows/workflow-scrum-report.yaml` | `collect` → assert snapshot has issues → `analyze` → `render` (Markdown) |

Swamp's own repo rules say `command/shell` is only for one-off commands and
never for wrapping a CLI, so this uses a TypeScript extension behavior.

```bash
cd swamp
swamp workflow run scrum-report
swamp data query 'modelName == "scrum-master" && name == "rendered"' --select content
```

## Measured

One run of each on 2026-10-24, same machine, same data:

| | Plain CLI | Swamp workflow |
| --- | --- | --- |
| Command | `op run --env-file .env -- uv run scrum-master report -f markdown` | `swamp workflow run scrum-report` |
| Wall time | 36.0 s | 40.6 s (collect 38.5 s, analyze 1.1 s, render 1.0 s) |
| Markdown output | 138,291 bytes | 137,674 chars |
| Kept after the run | the output file | every stage's JSON, versioned (last 10), plus run history; 3.9 MB in `.swamp/` after three runs |
| Secrets in stored data | n/a | none: the Linear secret and client ID appear in no file under `swamp/` or `~/.config/swamp` |

Collect, which is Linear and GitHub I/O, dominates both. Swamp's overhead
is about 4.6 s per run.

## What Swamp adds

- **Versioned stage data.** Every snapshot and report is kept and queryable
  (`swamp data query`). The CLI throws the snapshot away unless you pass `-o`.
  This is the closest thing to the "notification history" the Scrum Master
  doc lists as missing.
- **Gates between stages.** The assert step stops before analyzing an empty
  snapshot, using CEL over the stored data.
- **Secrets by reference.** `vault.get` resolves per step and is redacted in
  stored output, a cleaner path than `op run` wrapping the whole command.
- **Run history and failure reports** (`swamp run history`,
  `@swamp/workflow-summary`) with no extra code.

## What it costs

- **A hosted account for every command.** Every command except
  help/version/auth needs a swamp-club.com login, and a cached proof lasts 14
  days offline ([account requirement](https://swamp-club.com/manual/reference/swamp-account-requirement)).
  A k3s CronJob would need `SWAMP_API_KEY` plus a sign-in token, and runs
  would fail if swamp-club.com is unreachable without a fresh proof. The plain
  CLI depends only on Linear and GitHub.
- **Usage data on by default.** It's off here three ways: user config
  `telemetry.collection disabled`, `telemetryDisabled: true` in `.swamp.yaml`,
  and `SWAMP_NO_TELEMETRY=1`. Any new machine or pod has to repeat that.
- **Install by `curl | sh`.** The script checks a SHA-256 fetched from the
  same host, with no signature. It also breaks when run as `sh -c "$(curl …)"`
  (it restores `$-`, which then contains `c`). Pipe it into `sh` as the docs
  show.
- **A second language.** A TypeScript/Deno wrapper around a Python CLI. It
  re-implements nothing, but it's one more toolchain to keep working.
- **It writes outside the repo.** `swamp repo init` installed the `swamp` and
  `swamp-getting-started` skills into `~/.claude/skills/`. chezmoi ignores
  that directory, so they exist on this machine only.

## If we revisit

Swamp starts to pay off with many data sources, real DAG fan-out, or when
stage history becomes a requirement. Scheduled workflows exist but weren't
tried here. For the daily notification path, the existing k3s CronJob running
the plain CLI is simpler, and the formatting work belongs in `render` either
way.

## Dropped (SB-1442, 2026-10-27)

After the spike, `scrum-report` ran on a weekday schedule from a local
`swamp serve` launchd agent (SB-1441, #23). We decided not to continue: the
plain-CLI standup on k3s already covers the daily path. `swamp/` was deleted
and swamp uninstalled from this machine. The code is in git history: commit
`cc5ac51` has the spike, and #23 has the schedule.
