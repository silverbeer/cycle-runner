# cycle-runner

A learning project for Google's [Agent Development Kit](https://adk.dev) (ADK).

One agent, `cycle_runner`, talks through a local model served by Ollama. It
acts as Product Owner / Scrum Master for a weekly engineering cycle.

- **V0.1**: conversation only.
- **V0.2**: one tool, `get_cycle_status`, which returns hard-coded cycle data.
  The model decides for itself when to call it. Nothing talks to Linear yet.
- **V0.3**: Telegram as the human interface, behind a small generic gateway, so
  the Telegram layer carries no Cycle Runner logic. See [V0.3](#v03).
- **V0.4**: cycle state in SQLite, surviving restarts. (Superseded by V0.5.)
- **V0.5**: read-only Linear. `get_cycle_status` and `get_issue` read the SB
  team's real cycle and issues; the SQLite demo layer is gone. See [V0.5](#v05).
- **V0.6**: "What should we work on next?" gets a read-only recommendation,
  built from Linear facts and ending with a question. See [V0.6](#v06).
- **V0.7**: recommendations are structured (Pydantic) and can be explicitly
  approved. Approval is recorded in the session; nothing is executed yet. See
  [V0.7](#v07).
- **V0.8**: an approval creates a durable work request (`WR-000001`, pending)
  in SQLite that survives restarts. Still nothing executes. See [V0.8](#v08).
- **V0.9**: a separate executor claims pending work requests atomically and
  runs them through a deterministic `FakeExecutor` to `completed`. No real work
  yet. See [V0.9](#v09).
- **V1.0**: `ClaudeCodeExecutor`, a real Claude coding agent (Claude Agent SDK)
  confined to a disposable workspace, returning a structured result. See
  [V1.0](#v10).
- **V1.1**: projects. A work request knows its project (Linear's repo label),
  `projects.toml` maps it to a repository and test command, and the runner
  resolves a fresh clone as the executor's workspace. See [V1.1](#v11).

```
Python 3.14 → uv → google-adk → LiteLLM → Ollama → gemma4:12b
```

## Prerequisites

- Python 3.14 and [uv](https://docs.astral.sh/uv/)
- [Ollama](https://ollama.com), running, with the model pulled:

```bash
brew install ollama
brew services start ollama
ollama pull gemma4:12b      # ~7.6 GB
```

## Install

```bash
uv sync
```

Secrets live in 1Password; `.env` holds `op://` references to them (see
[V0.5](#v05)), so everything that needs Linear runs under `op run`.

## Run the agent

Interactive terminal chat:

```bash
uv run adk run src/cycle_runner
```

Browser dev UI (chat plus an event/trace inspector), at http://localhost:8000:

```bash
uv run adk web src
```

`adk web` takes the *parent* of the agent package and lists every agent it
finds there; pick `cycle_runner` in the dropdown.

## Test

```bash
uv run pytest                          # everything; live tests skip if unavailable
uv run pytest -m "not ollama and not telegram and not linear"   # offline only
uv run pytest -m ollama                # live round-trips through the local model
op run --env-file .env -- uv run pytest -m telegram   # real Telegram API
op run --env-file .env -- uv run pytest -m linear     # real Linear API, read-only
CLAUDE_CODE_OAUTH_TOKEN=$(op read op://agents/cycle-runner-claude/token) uv run pytest -m claude   # real coding agent
CLAUDE_CODE_OAUTH_TOKEN=$(op read op://agents/cycle-runner-claude/token) \
    op run --env-file .env -- uv run pytest tests/test_mt_live.py -s   # agent on a MissingTable clone (V1.2)
```

CI (`.github/workflows/ci.yml`) runs the offline set on every PR and every push
to `main`. Unless a test is marked `linear`, `tests/conftest.py` removes the
Linear credentials from its environment, so ordinary tests can't reach Linear.
The live Ollama tests use `FakeLinear`: a real model with fake Linear data.

Normal `pytest` never needs Telegram credentials. The `telegram` test skips
unless `TELEGRAM_BOT_TOKEN` is set.

The live tests (`-m ollama`) skip themselves when Ollama isn't reachable or the
model isn't pulled. One of them asks "What is the status of my cycle?" and
checks that the model called `get_cycle_status` exactly once and used its
result. Before adding it, gemma4:12b was probed with 10 status questions (10/10
called the tool) and 5 general ones (0/5 did).

## Configuration

| Variable | Default | Purpose |
|---|---|---|
| `CYCLE_RUNNER_MODEL` | `ollama_chat/gemma4:12b` | LiteLLM model string |
| `OLLAMA_API_BASE` | `http://localhost:11434` | Where LiteLLM finds Ollama |
| `LINEAR_CLIENT_ID` | none (required) | Linear app client id, as an `op://` reference in `.env` |
| `LINEAR_CLIENT_SECRET` | none (required) | Linear app client secret, as an `op://` reference in `.env` |
| `LINEAR_TEAM_KEY` | `SB` | Team whose active cycle `get_cycle_status` reads |
| `CYCLE_RUNNER_DB` | `data/cycle-runner.db` | SQLite file holding work requests (gitignored) |
| `CYCLE_RUNNER_CODING_MODEL` | `claude-sonnet-5` | Model for `ClaudeCodeExecutor` |
| `CLAUDE_CODE_OAUTH_TOKEN` | none | Claude credential for the coding agent; pass it only to runs that need it |
| `CYCLE_RUNNER_PROJECTS` | `projects.toml` | Project workspace configuration |
| `TELEGRAM_BOT_TOKEN` | none (required for Telegram) | Bot token from @BotFather |
| `TELEGRAM_ALLOWED_USER_IDS` | empty, so nobody is allowed | Comma-separated Telegram user ids allowed to chat |

Use the `ollama_chat/` prefix, not `ollama/`. ADK's docs warn the latter can
cause infinite tool-call loops.

Thinking is switched off (`reasoning_effort="none"` on `LiteLlm`). gemma4
reasons by default, which adds latency, and `adk run` prints the whole
trace before the answer.

To try another model: `ollama pull qwen3:8b`, then
`CYCLE_RUNNER_MODEL=ollama_chat/qwen3:8b uv run adk run src/cycle_runner`.

## Checking that Ollama is really being used

`adk run` prints a LiteLLM line naming the provider on every model call. A
turn that uses the tool shows two, one before the tool runs and one after:

```
LiteLLM completion() model= gemma4:12b; provider = ollama_chat
```

From another terminal:

```bash
ollama ps                                   # model listed as loaded while you chat
curl -s localhost:11434/api/ps              # same, as JSON
tail -f /opt/homebrew/var/log/ollama.log    # a POST /api/chat line per turn
```

No Google API key is configured anywhere, so there is no cloud model to fall
back to. Stop Ollama (`brew services stop ollama`) and the agent fails with a
connection error.

## ADK concepts used here

- **Agent (`LlmAgent`)**: `Agent` is an alias for `LlmAgent`, an agent whose
  behaviour comes from a model plus an `instruction` (its system prompt). A
  `name` identifies it in events and multi-agent setups; `description` is what
  *other* agents read when deciding whether to hand work to it.
- **`root_agent` convention**: `adk run` and `adk web` import
  `<package>.agent` and look for a variable called `root_agent`. That's why
  `__init__.py` used to do `from . import agent`; since V0.9 it's empty,
  because ADK imports `cycle_runner.agent` itself and the import made every
  module load ADK. (`__main__.py` is only for the Telegram bot; `adk run`
  doesn't use it.)
- **Model adapter (`LiteLlm`)**: ADK speaks Gemini natively. Anything else goes
  through a model adapter; `LiteLlm` wraps [LiteLLM](https://docs.litellm.ai),
  which knows how to talk to Ollama.
- **Runner, Session, Event**: a `Runner` executes an agent for one user turn.
  It loads a `Session` (conversation history and state) from a session service,
  runs the agent, and yields `Event`s: model output, tool calls, state changes.
  `adk run`/`adk web` create a runner for you; the live test uses
  `InMemoryRunner`, which keeps sessions in memory and loses them on exit.
- **Tool (`FunctionTool`)**: put a plain function in `Agent(tools=[...])` and
  ADK wraps it in a `FunctionTool`. The model never sees the Python. It sees a
  declaration built from the function's **name**, **docstring** and **typed
  parameters**, so the docstring is effectively prompt text: it's how the model
  knows when the tool is worth calling.
- **A tool turn**: the model replies with a function call instead of text. ADK
  runs the function and hands the result back as a function response, then
  calls the model again to write the answer. One user message, two model
  calls, three events:

  ```
  1. author=cycle_runner  role=model  function_call      get_cycle_status()
  2. author=cycle_runner  role=user   function_response  {cycle, issues, current_focus}
  3. author=cycle_runner  role=model  text               "The current cycle is Week 39…"
  ```

  `adk web` shows these events in its trace panel.

## V0.3

Telegram becomes the human interface. The main lesson is how a chat app maps
onto ADK's Runner, SessionService, `user_id`, `session_id` and Events. The
second lesson is keeping Telegram code free of Cycle Runner logic, so the same
Telegram layer could later front a different agent.

### Architecture

```
 Telegram app (your phone)
        │  Bot API, long polling
        ▼
 telegram_adapter.py   TelegramAdapter      Telegram only: token, allowlist,
        │                                   identity mapping, typing, formatting,
        │                                   4096-char limit
        │  handle_message(user_id, session_id, text) -> str
        ▼
 gateway.py            AgentGateway         ADK only: session get-or-create,
        │                                   Runner.run_async, pick final reply
        ▼
 ADK Runner  ◄──►  InMemorySessionService   (sessions keyed by app, user, session)
        │
        ▼
 agent.py              root_agent           Cycle Runner only: instruction,
        │                                   get_cycle_status tool
        ▼
 LiteLlm → Ollama → gemma4:12b
```

Models write Markdown, which Telegram shows as raw `**` and `*`. The adapter
converts the common parts (bold, italic, code, bullets, headings) to Telegram's
HTML subset, and resends as plain text if Telegram ever rejects the HTML. That's
a Telegram concern, so it lives in the adapter, not in the agent's instruction.

`__main__.py` is the only module that imports both sides. It builds the Runner
around `root_agent` and passes the gateway to the Telegram adapter.

### The reusable boundary

- `telegram_adapter.py` imports neither ADK nor anything from `cycle_runner`.
  It needs one object with `async handle_message(user_id, session_id, message) -> str`.
- `gateway.py` imports ADK but not `cycle_runner.agent`. It works with any
  `Runner`, whatever agent that runner wraps.
- `agent.py` doesn't know Telegram exists.

`tests/test_boundaries.py` enforces this by reading the source of both generic
modules. It fails if they import `cycle_runner` or mention `get_cycle_status`,
`root_agent`, `Scrum`, `Product Owner` or `Linear`, and if the adapter imports
`google.*`. To put Telegram in front of a different agent, pass that agent's
Runner in `__main__.py`; the other two modules stay as they are.

### ADK pieces, as used here

- **Runner**: executes one turn: `runner.run_async(user_id=..., session_id=...,
  new_message=Content)`. It loads the session, appends the user message, runs
  the agent (model calls, tool calls), appends every resulting event to the
  session, and yields each one as it happens. It raises if the session doesn't
  exist, so the gateway gets or creates it first. `Runner(auto_create_session=True)`
  does the same, but less visibly.
- **SessionService**: stores sessions. `InMemorySessionService` keeps them in a
  dict inside the process: fast, nothing to set up, and gone on restart.
- **Session**: one conversation. It holds `events` (the full history, which ADK
  replays to the model on every turn) and `state` (unused so far). It's keyed by
  **(app_name, user_id, session_id)**.
- **user_id**: who is talking. **session_id**: which conversation this is.
- **Event**: one step in a turn: model text, a `function_call`, a
  `function_response`. `event.is_final_response()` marks the one worth showing
  a human. The gateway logs every event and returns only that final text, with
  any model reasoning (`part.thought`) removed. Telegram never sees tool calls.

### Telegram identity → ADK identity

| ADK | Value | Example |
|---|---|---|
| `app_name` | fixed | `cycle_runner` |
| `user_id` | `telegram:<update.effective_user.id>` | `telegram:123456789` |
| `session_id` | `telegram:<update.effective_chat.id>` | `telegram:123456789` |

- In a private chat, Telegram's chat id equals the user id, so the two values
  match.
- The same chat always maps to the same `session_id`, so every message in it
  continues one ADK session for as long as the process runs.
- A group chat gets its own `session_id`. Because the session key includes
  `user_id`, each member of the group still has their own session.
- The `telegram:` prefix keeps these ids from colliding with ids from other
  interfaces later.
- Restarting the bot empties `InMemorySessionService`, so the next message
  starts a new session. Persistence is a later milestone.

### One Telegram message, end to end

```
you: "How is the cycle going?"
 → TelegramAdapter.on_message      allowlist check, typing…, map ids
 → AgentGateway.handle_message     get_session → exists (reused)
 → Runner.run_async
     Event author=cycle_runner function_call ['get_cycle_status']
     Event author=cycle_runner function_response ['get_cycle_status']
     Event author=cycle_runner text (525 chars) [final]
 → reply text back to TelegramAdapter
 → message.reply_text(...)         Markdown → Telegram HTML, split at 4096 chars
```

The bot logs those `Event` lines (`cycle_runner.gateway`) for each message.

### Configure the bot

1. In Telegram, message **@BotFather**, send `/newbot`, and copy the token.
   Use a bot of its own: Telegram allows only one poller per bot, so sharing
   a token with another running program makes them fight over messages.
2. Create `.env` in the repo root. It's gitignored; never commit it.

   ```bash
   TELEGRAM_BOT_TOKEN=123456:ABC...
   TELEGRAM_ALLOWED_USER_IDS=123456789
   ```

   Don't know your user id? Start the bot with the allowlist empty and send it
   anything. It replies with your id.

### Run Cycle Runner through Telegram

```bash
op run --env-file .env -- uv run python -m cycle_runner
```

(Before V0.5 this was `uv run --env-file .env ...`. The bot now needs Linear
credentials, which `op run` resolves from 1Password.)

Then, in Telegram, open your bot, send `/start`, and chat.

### Manual Telegram check

The automated tests mock Telegram. To check the real thing end to end:

1. `op run --env-file .env -- uv run pytest -m telegram` confirms Telegram accepts the token.
2. Start the bot, then send these three messages in the same chat:
   1. `What is my role?`: says it's PO/Scrum Master and you're the decision maker.
   2. `What are we working on?`: answers about the Week 39 cycle.
   3. `How is the cycle going?`: answers from the tool.
3. In the bot's log:
   - `session=telegram:<chat id> created` appears exactly once, for the first
     message.
   - Message 3 shows `function_call ['get_cycle_status']` followed by
     `function_response`.

## V0.4

> **Superseded in V0.5.** The SQLite store, bootstrap and demo cycle described
> below were removed once Linear became the source of truth for cycles and
> issues. The lesson still stands; the commands in this section no longer exist.

The main question: **where does the agent's conversation end and the
application's actual state begin?**

### Two kinds of state

| | ADK session state | Cycle Runner application state |
|---|---|---|
| What | The conversation: messages, tool calls and replies | The cycle: name, goal, dates, issues, statuses |
| Owner | ADK (`Runner` + `SessionService`) | Cycle Runner (`CycleStore`) |
| Storage | `InMemorySessionService`, a dict in the process | SQLite, `data/cycle-runner.db` |
| Survives restart | **No** | **Yes** |
| Written by | ADK, automatically, on every turn | `bootstrap.py` today; later, whatever syncs from Linear |
| The model sees it | Replayed as chat history every turn | Only through tool results |
| Trust | What was said, which can be stale or wrong | The source of truth |

In short: the session is the agent's short-term memory of a chat, and the
store is what's actually true about the cycle. If something from the
conversation disagrees with the store, the store wins. That's why the
instruction asks for a fresh tool call on every cycle question instead of
trusting an earlier tool result still sitting in the session history.

### Architecture

```
Telegram → telegram_adapter.py → gateway.py → ADK Runner ──► SessionService ──► conversation
                                                   │          (in memory, lost on restart)
                                                   ▼
                                          agent.py  root_agent
                                                   │  tools=[get_cycle_status]
                                                   ▼
                                          tools.py  domain tools (plain dicts, no storage words)
                                                   │
                                                   ▼
                                          store.py  CycleStore (only module that imports sqlite3)
                                                   │
                                                   ▼
                                          data/cycle-runner.db ──► application state
                                                                   (on disk, survives restart)
```

### Schema

```sql
CREATE TABLE cycles (
    id         TEXT PRIMARY KEY,
    name       TEXT NOT NULL,
    goal       TEXT NOT NULL,
    status     TEXT NOT NULL CHECK (status IN ('active', 'completed')),
    start_date TEXT NOT NULL,   -- YYYY-MM-DD
    end_date   TEXT NOT NULL
);
CREATE UNIQUE INDEX one_active_cycle ON cycles (status) WHERE status = 'active';

CREATE TABLE issues (
    id       TEXT PRIMARY KEY,
    title    TEXT NOT NULL,
    status   TEXT NOT NULL CHECK (status IN ('todo', 'in_progress', 'done', 'blocked')),
    cycle_id TEXT NOT NULL REFERENCES cycles (id)
);
```

The database itself enforces the rules, not just the code: at most one active
cycle (so "the current cycle" is never ambiguous), only known statuses, and no
issue without a real cycle. "Current focus" isn't stored. It's derived from the
`in_progress` issues.

### How the tools reach application state

- The model gets **domain tools**, not database access. It can ask "what's
  the current cycle?" but it can't run a query. There is no `run_sql`.
- Tool names and docstrings never mention storage. The model doesn't know
  SQLite exists. `tests/test_tools.py` and `tests/test_boundaries.py` check
  both of these.
- Each tool call opens the store fresh, so an answer always reflects the file
  as it is at that moment.
- `store.py` is the only module that imports `sqlite3`. Moving to Postgres or
  Linear later means replacing that one file.

### One cycle-level tool

The agent has a single domain tool, `get_cycle_status`. It returns the cycle's
details (name, goal, dates) together with its issues and progress, and its
docstring covers every kind of question about the current cycle.

The first V0.4 draft also had `get_current_cycle`, which returned the cycle
details only. The model told the two apart perfectly (progress questions went
to `get_cycle_status` 9/9, goal and date questions to `get_current_cycle`
6/6), but that isn't a reason to keep both: `get_cycle_status` already
contained everything the smaller tool returned. It was removed so that each
tool exposes a distinct capability. Overlapping tools make the model choose
between near-duplicates and give you two things to keep consistent.

With the single tool, measured with gemma4:12b, 3 runs per question:

| Question | Tool chosen |
|---|---|
| How is the cycle going? | `get_cycle_status` 3/3 |
| What is the goal of this cycle? / When does the cycle end? | `get_cycle_status` 6/6 |
| What is a retrospective? | none 3/3 |

### Restart check (as verified in V0.4)

After a bot restart the agent had forgotten the conversation (the ADK
session was new and empty) but still reported the same cycle, because the
tool read it from SQLite. Conversation state is short-term; application state
lives outside the process.

## V0.5

Read-only access to the team's real work in Linear. The agent now answers
from the SB team's actual cycle and issues.

### Architecture

```
Telegram → telegram_adapter → gateway → ADK Runner ──► InMemorySessionService (conversation)
                                            │
                                        root_agent   tools=[get_cycle_status, get_issue]
                                            │
                                        linear_tools.py   what Cycle Runner asks for, shaped small
                                            │
                                        linear_client.py  how Linear is reached: URL, OAuth, HTTP
                                            │
                                        Linear (SB team) ──► source of truth for cycles and issues
```

- **`linear_client.py`** is generic, like `telegram_adapter.py`. It holds the
  API URL, the OAuth client-credentials token, HTTP, and errors, and it never
  imports the rest of the package. It's the one file to move out if a second
  agent ever needs Linear.
- **`linear_tools.py`** is Cycle Runner's view: which fields matter, how to
  summarise a cycle, how much to return. The GraphQL text lives here, because
  deciding *what to ask for* is a Cycle Runner decision. The model never sees
  it: tool names and docstrings contain no API words, and
  `tests/test_linear_tools.py` checks that.
- **`tests/test_boundaries.py`** enforces the split. Only `linear_client.py`
  imports `httpx` or mentions the API host, OAuth or auth headers, and the
  generic modules contain no Cycle Runner terms.

### Tools

| Tool | Returns | Notes |
|---|---|---|
| `get_cycle_status()` | cycle number, dates, progress %, counts per status, **open** issues (id, title, status, estimate), in-progress ids | Follows pagination. Done and canceled issues are counted but not listed. |
| `get_issue(issue_id)` | one issue: title, status, estimate, priority, assignee, labels, cycle, description (trimmed to 1500 chars) | The first tool with an argument. ADK builds its parameter schema from `issue_id: str`. |

Both tools are `async def`. ADK awaits async tools, but calls plain `def`
tools directly on the event loop (`FunctionTool._invoke_callable`), so a
synchronous network call would freeze the Telegram bot while it waited.

### Identity and read-only access

Cycle Runner talks to Linear as its **own app identity** ("Cycle Runner"),
not as you. That's a Linear OAuth app using the client-credentials grant: no
browser, no billable seat, and actions are attributed to the app.

Read-only is enforced in three layers:

1. **Linear:** the token is requested with scope `read`. A write attempt gets
   `Invalid scope: write or issues:create required` (`FORBIDDEN`), and the
   `linear` live test proves it.
2. **Client:** `query()` refuses any document that isn't a query (mutations
   and subscriptions, even mixed in with a query) before it touches the
   network. It also refuses a token that comes back with more than `read`.
3. **Agent:** there are no write tools, and the instruction says so.

### Credentials

The secret never sits in the repo, in plain text on disk, or in front of the
model.

- The client id and secret live in 1Password (`agents` vault,
  `cycle-runner-linear-app`).
- `.env` holds `op://` references, not values. `op run --env-file .env -- …`
  resolves them into the process environment and masks them in its output.
- `LinearClient` keeps them out of `repr()` and out of every error message.
  HTTP failures report the status code only. `httpx` stays at WARNING, so no
  request URLs or headers are logged.
- Tools never take or return credentials, and no tool description mentions
  auth.

### Linear vs SQLite

- **Linear** is the source of truth for engineering work: cycles, issues,
  statuses. The tools read it live on every call. There is no sync and no
  cache.
- **SQLite** is gone for now. The V0.4 demo cycle duplicated what Linear owns,
  so it was removed. Cycle Runner's own state (decisions, configuration, run
  history) will come back as its own store when there's something to keep.
- If Linear can't be reached, the tool returns an error and the agent says the
  information is unavailable. It never falls back to anything else.

### Context window

Two measurements shaped this milestone:

- **Tool output size.** The real SB cycle had 66 issues. Listing them all is
  about 16k characters; listing only the 39 open ones is 5.4k. Hence
  "open issues only".
- **Ollama's 4096-token default.** Ollama started gemma4 with
  `-c 4096 --context-shift --keep 4`. A four-question Telegram-style
  conversation reached 3,902 prompt tokens, and Ollama began silently dropping
  the *start* of the prompt, where the instruction lives. The next answer
  skipped the tool and answered from stale history. `agent.py` now asks for
  `num_ctx=16384` (about +0.2 GB). The same conversation then grew to 5,897
  tokens intact, with fresh tool calls.

A firmer instruction ("every time the user asks about the cycle or an issue,
call the tool again before answering") took follow-up questions from
sometimes stale to 12 of 12 fresh tool calls across three runs.

### Run it

```bash
op run --env-file .env -- uv run python -m cycle_runner   # Telegram bot
op run --env-file .env -- uv run adk run src/cycle_runner # terminal chat
```

`.env` needs these lines in addition to the Telegram ones:

```bash
LINEAR_CLIENT_ID=op://agents/cycle-runner-linear-app/client_id
LINEAR_CLIENT_SECRET=op://agents/cycle-runner-linear-app/client_secret
LINEAR_TEAM_KEY=SB
```

The bot checks the Linear credentials at startup and refuses to start without
them. Run without `op run`, the references stay unresolved and `LinearClient`
says so.

## V0.6

Cycle Runner can now answer "What should we work on next?" with a
**recommendation**, not an action. It reads the cycle, weighs the open work,
offers a few candidates, keeps Linear's facts apart from its own reasoning,
and asks before anything happens. Nothing is written to Linear.

### No new tool

The existing tools weren't enough. `get_cycle_status` returned only id, title,
status and estimate, and `get_issue` would have needed one call per issue.
Instead of adding a tool (which would repeat the issue list, the overlap
V0.4 removed), `get_cycle_status` now carries the evidence:

| Field | Source | Why it matters |
|---|---|---|
| `priority`, `labels` | Linear | urgency and area |
| `blocked_by` | Linear "blocks" relations, **open blockers only** | a finished blocker no longer blocks; in Cycle 9, 5 of the 9 issues with blocking relations are actually blocked |
| `age_days` | derived from `createdAt` | the model doesn't know today's date |
| `cycle.today`, `cycle.days_remaining` | derived | whether a 3-point issue fits the time left |
| `cycle.goal` | Linear cycle description | `null` for Cycle 9, which makes "no goal set" a fact instead of a guess |

`get_issue` gains `blocked_by` too. Linear has no due dates on these issues,
and every issue has the same assignee, so neither is reported. The tool still
does no ranking.

### Where the judgment lives

In the instruction, as factors to weigh rather than a formula: finish
in-progress work, don't pick work blocked by open issues (consider the
blocker), fit priority and estimate to the days remaining, and use age and
labels as supporting evidence. No single factor decides. The reply must:

1. offer two or three candidates, each with **"Linear facts"** (tool values
   only) and **"Why"** (reasoning);
2. name exactly one issue under **"My recommendation"**;
3. say what Linear doesn't record (for example, no cycle goal);
4. end by asking whether to proceed, without ever claiming to have started,
   assigned or changed anything.

Tested on the real cycle: the first wording was followed less often with a
~4.4k-token prompt of real data (the missing goal was mentioned in 1 of 3
runs, and a single pick named in 0 of 3). The concrete wording ("exactly one
issue", "if the cycle's goal is null…") took both to 3 of 3. A real
recommendation takes about 40 seconds on gemma4:12b.

### Why no structured output (yet)

ADK 2.10 supports `output_schema` together with tools, but it forces *every*
final reply of the agent into that schema. "What is my role?" would come back
as JSON too, and Telegram would need a rendering layer. Doing it properly
means a separate recommendation agent, which is multi-agent scope. For now:

- the **facts** are structured: tool output, tested deterministically in
  `tests/test_linear_tools.py`;
- the **judgment** is prose, tested structurally in
  `tests/test_recommendation.py` (live model, fixed fake-Linear scenarios).

Structured output becomes worth it when something *consumes* the
recommendation, such as an approval step that acts on the chosen issue.

### Tests

- **Deterministic** (`test_linear_tools.py`, in CI): open-vs-closed blockers,
  non-blocking relations ignored, in-progress-but-blocked reported as both,
  priority, labels and age, a cycle where everything is blocked, a cycle with
  no open work, the goal when present, and every request being a read query.
- **Live** (`test_recommendation.py`, `ollama` marker, not in CI): fresh tool
  call, "Linear facts" present, a separate recommendation, the blocked
  issue never picked, "nothing actionable" recognised, the missing goal named,
  a question at the end, and no mutation sent. Each was run 3 times, all
  passing.

## V0.7

The recommendation becomes data, and "yes" becomes a recorded, checked
approval of that data. Nothing is executed: V0.7 ends at
*recommendation → explicit approval → recorded approval*.

### Architecture

```
Telegram → gateway → Runner → root_agent (conversational)
                                │ before_agent_callback  approval_gate: decides approval in code, before the model
                                │ tools: get_cycle_status, get_issue, AgentTool(recommend_next_work)
                                │
                                │ before_tool_callback   read the cycle (get_cycle_status) into session state
                                ▼
                    recommend_next_work (LlmAgent, no tools)
                        instruction has {recommender_evidence}  ← filled from session state
                        output_schema=RecommenderOutput         ← enforced by Ollama while decoding
                        output_key=recommender_output
                                │
                                │ after_tool_callback    validate against the same cycle data → Recommendation
                                │ before_model_callback  show the stored Recommendation as text (no model call)
                                ▼
                         session state: pending_recommendation → approved_work_item
```

New modules: `recommendation.py` (models, recommender, validation, rendering)
and `approval.py` (reply classification, approval gate). Telegram, the gateway
(apart from one fix) and the Linear modules are untouched.

### Why AgentTool, not a sub-agent

ADK's runner gives the next turn to whichever agent replied last. With a
sub-agent, the recommender (and its schema) would answer "Yes, proceed" as
JSON. `AgentTool` runs the recommender as a tool call and returns its
validated output, so the root agent keeps the conversation. It also forwards
the child's state changes (its `output_key`) into the caller's session.

### Why the recommender has no tools

With tools, ADK 2.10 can only enforce `output_schema` through an extra
`set_model_response` tool call, because LiteLLM reports
`output_schema_and_tools=False`. That was measured with gemma4:12b first:

| Recommender | Valid output | Time |
|---|---|---|
| with `get_cycle_status` as a tool (`set_model_response`) | 6/10 first try, 2/10 after a retry, 2/10 empty; Ollama also failed with HTTP 500 when it couldn't parse nested tool-call JSON | 8–17s |
| **no tools, cycle passed through state, native schema** | **10/10** | **4–13s** |

So code reads the cycle and hands it over through ADK's `{state_key}`
instruction templating, and the schema becomes a response format that Ollama
enforces while decoding.

### Facts versus judgment, by construction

```python
class RecommenderOutput(BaseModel):     # all the model writes
    candidates: list[CandidateChoice]   # issue_id + rationale, up to 3
    recommended_issue_id: str | None
    unknowns: list[str]

class Recommendation(BaseModel):        # what is stored, shown and approved
    cycle_number: int
    candidates: list[Candidate]         # issue_id, title*, facts*, rationale
    recommended_issue_id: str | None
    blocked: dict[str, list[str]]       # *
    corrections: list[str]              # where code overruled the model
    unknowns: list[str]                 # plus "no goal set" when Linear has none*
    approval_question: str
```

Fields marked * are filled by code from the same cycle data, never by the
model. A pick that isn't an open, unblocked issue of the cycle is never
passed on. It becomes "no pick" with a visible correction; with every issue
blocked, gemma picked a blocked issue in 3 of 3 runs, and the correction
caught it every time.

### What you see is what gets approved

The stored `Recommendation` is rendered by code (`render()`), and a
`before_model_callback` returns that text as the reply, so the root model
never paraphrases it. That's also why V0.7 is faster: on real Cycle 9 the
recommendation path went from about 42s (V0.6) to about 20s.

### Approval rules

`approval_gate` runs before the model on every message:

| Situation | Reply | State |
|---|---|---|
| Explicit reply right after a recommendation ("yes", "yes, proceed", "go ahead", "do it", "approve", "approve SB-123") | "Approved. I have recorded your approval for SB-123 … No action has been taken yet." | `approved_work_item` set, pending cleared |
| "approve SB-999" when SB-123 is pending | "Not approved: the pending recommendation is SB-123…" | nothing recorded |
| Soft reply right after ("sounds good", "ok", "makes sense") | asks for "approve" or "yes, proceed" | still pending for the next reply |
| Anything else ("tell me more", "interesting", a new question) | answered by the model as usual | the recommendation is stale from the next message |
| Explicit reply after the conversation moved on | "Not approved: the recommendation for SB-123 is no longer pending…" | nothing recorded, stale item cleared |
| Explicit reply with nothing pending | answered by the model, which can't approve anything | nothing recorded |
| A new recommendation | replaces the pending one, even if it fails | |

"Pending" means: shown on turn *n*, and valid only for turn *n+1*. The model
never decides approval, and approval never touches Linear or starts work;
`tests/test_boundaries.py` checks that neither module can reach Linear or
launch processes.

Session state holds `turn`, `recommender_evidence`, `recommender_output`,
`pending_recommendation` and `approved_work_item`. It's in memory: a restart
forgets pending and approved items, which is fine while approval causes no
action.

### Tests

- **Deterministic, with a scripted model** (`tests/test_approval.py`):
  `ScriptedLlm` is a `BaseLlm` that replies from a script, so the real
  Runner, AgentTool, callbacks, templating and session state run with no
  Ollama at all. Covered: the recommendation stored in state, the shown text
  equal to the stored model, the recommender receiving the cycle with no tools
  and a schema, a blocked pick corrected, a recommender that ignores the
  schema, an unreadable cycle, replacement by a new recommendation, explicit,
  mismatched, soft, stale and no-pending approvals, approval making no model,
  tool or Linear call, and normal questions unaffected.
- **Deterministic, pure** (`tests/test_recommender.py`): model validity,
  facts from Linear rather than the model, blocked picks and candidates,
  unknown ids, the missing goal, nothing actionable, ADK's `exclude_none`,
  failed runs not mistaken for "nothing actionable", and rendering.
- **Live** (`tests/test_recommendation.py`, `ollama` marker): structure,
  blocked work not picked, nothing actionable, approving a live
  recommendation, soft replies, and normal questions after a recommendation.


### Open concerns for V0.8

- **Blocked-issue list is long.** Every recommendation lists all open issues
  blocked by open work. On real Cycle 10 that's five lines before the unknowns
  and the question. It's accurate, but noisy in Telegram. Left as is in V0.7;
  a V0.8 usability decision (for example, count them and show only the ones
  that block a candidate).
- **Approvals live in session memory.** A restart forgets pending and
  approved items. That's acceptable while approval executes nothing. It has
  to change before an approved item can trigger work. *(Resolved in V0.8: an
  approval creates a durable work request.)*

## V0.8

Approving a recommendation now creates a **durable work request**: Cycle
Runner's record that a human approved an issue for agent execution. It
survives process, Telegram and session restarts. Nothing consumes it yet.

```
"Yes, proceed"
   → approval_gate (deterministic, before the model)
   → validate the pending recommendation (next message, matching id)
   → WorkRequestStore.create_for_approval(...)      ← the only new side effect
   → "Approved SB-1234. Work request WR-000001 created. No work has been started."
```

### Two sources of truth

| | Linear | Work-request store |
|---|---|---|
| Owns | engineering work: the issue, title, status, priority, labels, cycle | Cycle Runner's execution intent: "a human approved SB-1234 for agent execution" |
| Written by | people (Cycle Runner only reads it) | `approval.py`, and nothing else |
| Storage | Linear | SQLite, `data/cycle-runner.db` (gitignored) |

The store does **not** copy the Linear issue. V0.9 will re-read the issue from
Linear when it picks a request up.

### The WorkRequest

| Field | Why |
|---|---|
| `work_request_id` | `WR-000001`: an AUTOINCREMENT row, never reused, not the Linear id. One issue can have several requests over its lifetime. |
| `recommendation_id` | a UUID given to each recommendation; **UNIQUE**, which makes approval idempotent |
| `issue_id` | the Linear issue to work on |
| `status` | `pending`, the only value the database accepts (`CHECK`), because there's no executor yet |
| `approved_by` | the ADK `user_id` (`telegram:<user id>`) |
| `approved_at` | UTC |
| `cycle_number`, `title_at_approval`, `rationale` | a snapshot of what the human saw when approving |

Deliberately not stored: Linear's current status, priority, estimate, labels,
description or blockers (Linear is authoritative), the other candidates, the
conversation, and a separate `created_at` (it would equal `approved_at`).

### Why SQLite (again)

V0.5 removed SQLite because it was duplicating Linear. This is different data
that Linear doesn't hold, and it's operational state: small, local,
single-writer, and needing transactions and a uniqueness constraint. SQLite
provides exactly that with no new dependency. `work_requests.py` is the only
module that imports `sqlite3`, and it imports nothing from ADK, Telegram,
Linear or the rest of the app.

### Idempotency

"The same approval processed twice" can mean a double-tapped "yes", a
retried Telegram update, or a crash after the write but before the session
update. All of them hit the same `recommendation_id`:

- **The database guarantees it:** `UNIQUE(recommendation_id)` with
  `INSERT … ON CONFLICT DO NOTHING`, then read back the one row.
- **The conversation says so:** a repeat "yes" right after an approval gets
  "Already approved: SB-1234 is work request WR-000001, still pending. No new
  work request was created."
- **If the write fails**, nothing is approved and the recommendation stays
  pending for one retry.

A *new* recommendation that picks the same issue is a new approval and a new
request (`WR-000002`). Whether that should be refused while an earlier request
for the issue is still pending is a V0.9 decision, once "pending" can end.

### Restart

The conversation is still in memory, so pending recommendations are forgotten
on restart. Work requests are not. The tests restart with a new Runner, a new
session service and a new store object on the same file, and check from a
separate OS process that `WR-000001` is still there and still pending. A "yes"
after a restart finds nothing pending and creates nothing.

### Boundaries (checked by tests)

```
approval.py ──► work_requests.py ──► SQLite        the only new side effect
approval.py ──╳─► model, ADK agents, Linear, Telegram, subprocess, coding agents
agent / recommender / tools / gateway / Telegram ──╳─► work_requests.py
```

### Open concerns for V0.9

- **Blocked-issue list is long** (carried over from V0.7).
- **Several pending requests for one issue** are possible (see Idempotency).
- **Nothing reads work requests yet**, including the user: there's no "what's
  approved?" question. That's deliberate until something acts on them.
- **A bare "yes" with nothing pending goes to the model.** After a restart,
  "Yes, proceed" was answered with a cycle summary. It's harmless, since
  nothing was created, but confusing. V0.7 deliberately lets the model handle
  "yes" when nothing is pending, so that "yes" can still answer the model's
  own questions. Worth revisiting.

## V0.9

The first executor. It runs outside the Telegram app and knows nothing about
ADK, Telegram, Linear or the model. It takes durable work requests through
their whole lifecycle using a `FakeExecutor` that does no real work.

```
WorkRequest(pending) ─claim (atomic)─► claimed ─start─► running ─FakeExecutor─► completed
```

### The lifecycle

```
pending ──claim──► claimed ──start──► running ──finish──► completed
   ▲                  │                   └────finish──► failed
   └────release───────┘ (manual)          running ──abandon──► failed (manual)
```

- **`claimed` and `running` are separate** because they're the only way to
  tell a crash *before* the executor ran (nothing happened, so it's safe to
  retry) from one *during* it (work may have partly happened, so it isn't).
- **`failed` exists now** because an executor can fail. Without it, a failure
  would be indistinguishable from a crash (stuck in `running`).
- `completed` and `failed` are final.
- `TRANSITIONS` in `work_requests.py` is the only definition. A database
  trigger generated from it rejects any other status change, from any
  writer, including the `sqlite3` shell. Another trigger keeps new rows
  `pending`.

### Executor interface

```python
class ExecutionResult(BaseModel):
    outcome: Literal["completed", "failed"]
    message: str

class Executor(Protocol):
    name: str
    def execute(self, request: WorkRequest) -> ExecutionResult: ...
```

Claiming, statuses and timestamps belong to the runner (`run_next`,
`run_request`) and the store, not the executor. A future coding-agent
executor implements `execute()` and nothing else changes.
`FakeExecutor` returns "Fake execution completed for WR-000001 (SB-640). No
real work was done.", imports only the interface and the model, and does no
I/O: tests forbid sockets, subprocesses and file access around it.

### The atomic claim

Every transition is one compare-and-set statement:
`UPDATE … SET status = 'claimed' … WHERE id = ? AND status = 'pending' RETURNING *`.
`claim_next` finds and claims the oldest pending row in a single statement.
SQLite runs each under its write lock, and the loser of a race waits for the
lock (`timeout=10`), then matches no rows: "already claimed". Tested with real
OS processes: 8 racing for one request gives exactly one winner; 8 racing for
3 requests gives each claimed exactly once. The race tests passed 10 of 10
runs.

### Crashes and recovery

| Crash | State left | What happens next |
|---|---|---|
| before claim | `pending` | the next run picks it up |
| after claim, before the executor ran | `claimed` | runs skip it; `executor release WR-…` puts it back to `pending` (safe: nothing ran) |
| while running | `running` | runs skip it and never retry it; `executor abandon WR-… --reason …` marks it `failed` for a human |
| executor raises | `failed` | recorded with the exception; never retried |

There are no leases. A stranded request needs a human, which is acceptable
while one person runs one executor by hand. The crash tests kill real
processes (`os._exit`) at each point and check the state a fresh process
sees.

### Idempotency

`run_request` on anything that isn't `pending` executes nothing and reports
what it is: running `WR-000001` twice gives "WR-000001 is already completed;
nothing executed."

### Run it

```bash
uv run python -m cycle_runner.executor            # run the next pending request, then exit
uv run python -m cycle_runner.executor run --request WR-000002
uv run python -m cycle_runner.executor list
uv run python -m cycle_runner.executor release WR-000002
uv run python -m cycle_runner.executor abandon WR-000002 --reason "executor died"
```

No `op run` is needed: the executor uses no credentials. It exits after one
request; there's no daemon.

> **Careful with real approvals.** The executor claims the *oldest pending*
> request. On your development database that's a real approval (for example
> `WR-000001`, SB-640), and `FakeExecutor` would mark it `completed` with a
> fake result. Its `claimed_by` (`fake@host:pid`) and message make that
> visible, but the approval is consumed. Point `CYCLE_RUNNER_DB` at a scratch
> file to experiment.

### Migration

Opening a V0.8 database migrates it to schema version 2 in one transaction.
SQLite can't alter a `CHECK`, so the table is rebuilt, keeping every row, id
and the never-reuse counter. Verified on a copy of a real database: its
approval came through unchanged.

### Open concerns for V0.10

- **No leases:** stranded `claimed` or `running` requests need manual
  recovery.
- **The real executor's inputs:** the request holds the issue id and a
  snapshot of what was approved, but a coding agent will need the current
  issue (description, repo). Where that's read from, and whether the executor
  may read Linear, is the next design question.
- **Several pending requests per issue** are still possible (from V0.8).
- **Blocked-issue list length** and a **bare "yes" after a restart** are
  still open (from V0.8).

## V1.0

The first real executor. `ClaudeCodeExecutor` hands an approved work request
to a Claude coding agent (the Claude Agent SDK, which runs Claude Code), in a
**disposable local repository**, and turns the agent's report into an
`ExecutionResult`. No real project repository, GitHub, Linear or Telegram is
involved.

```
executor.run_request / run_next      owns the lifecycle: claim, start, finish
        │ WorkRequest (running)
        ▼
ClaudeCodeExecutor.execute           builds the task, runs the agent, maps the result
        │ prompt + ClaudeAgentOptions (confined to one Workspace)
        ▼
Claude Agent SDK ─► Claude Code CLI  Read / Glob / Grep / Edit / Write / Bash(tests only)
        │ StructuredOutput: {outcome, summary, tests_passed, tests_command}
        ▼
ExecutionResult(outcome, message, details={files_changed, cost_usd, turns, denials, ...})
```

### How it works

- **Workspace:** `Workspace(path, test_command, readable)` says where to work
  and how to check the work. It's passed in; the executor contains no
  project-specific code. It's the seed of a per-project configuration layer
  (see "Next").
- **The task** comes from the `WorkRequest` alone: its id, the issue id, the
  title as approved, and the rationale. The executor doesn't read Linear.
- **The result:** the agent must answer in a JSON schema (SDK
  `output_format`, delivered through a `StructuredOutput` tool call). The
  result is `completed` only if the SDK run succeeded, the agent reports
  passing tests, **and** files actually changed. Changed files come from
  hashing the workspace before and after, not from the agent's say-so and not
  from running git. `details` carries the files changed, cost, turns and
  permission denials; it's returned, not yet persisted.
- **Failures:** an SDK error, a turn or budget limit (`ResultError`, reported
  from its `ResultMessage`), an invalid report, failing tests or no changes
  all become `failed` with a readable reason. The runner records it, as with
  any executor.
- The lifecycle is untouched: the runner claims, starts and finishes. The
  executor never opens the store (a boundary test checks this).

### Security boundary

Six layers. None of them relies on the prompt.

| Layer | What it does |
|---|---|
| `setting_sources=[]` | none of your `~/.claude` settings, hooks, CLAUDE.md, skills or permission rules load (the SDK default loads all of them) |
| `tools` | only Read, Glob, Grep, Edit, Write, Bash exist: no web, subagents or MCP |
| `permission_mode="dontAsk"` | allow rules scoped to the workspace and the exact test command; anything else that needs approval is denied |
| `PreToolUse` hook (`check_tool_call`) | runs before every tool call. Paths must resolve inside the workspace (symlinks followed, `~` expanded), `.git` is read-only, Bash may only run the test command, with no shell operators. Tested with 36 cases. |
| OS sandbox (Seatbelt) | every Bash command **and its children**, including tests the agent wrote: writes only in the workspace; reads denied from `/` except the workspace, the test interpreter and system directories; no network; no unsandboxed fallback; fails if unavailable |
| Credentials | credential-looking variables are blanked for the agent, and all of them (including its own login) are unset inside sandboxed commands |

Measured with a probe test that the agent ran as ordinary work, from inside
the sandbox:

| Attempt | Result |
|---|---|
| read the workspace | allowed |
| list `~`, `~/.ssh`; read `~/.zshenv`, another repo's `.env` | blocked |
| read a neighbouring directory, `/etc/hosts` | blocked |
| write a neighbouring file or `~/…` | blocked (and verified unchanged afterwards) |
| network | blocked |
| the executor's credentials (Claude token, 1Password service-account token, Linear key) | not visible |

Things the live runs found, and fixes:

- The first read restriction (`denyRead: ["~/"]`) left paths outside the
  home directory readable. It's now `denyRead: ["/"]` plus an allowlist.
- The hook first treated `~/.ssh/…` as a relative path, and let `~/…` Glob
  patterns through. Both are closed and tested.
- It first denied the SDK's own `StructuredOutput` tool. It's now allowed.
- The model itself refused an obviously probing prompt, which is welcome,
  but it's why the isolation test puts the probe in *test code* instead.

**Caveats**
- The system allowlist is **macOS-specific**; Linux (bubblewrap) needs its
  own.
- Git doesn't run inside this sandbox (macOS's git is an `xcode-select`
  shim), so the agent isn't offered it.
- The sandbox sets its own proxy variables (`CLOUDSDK_PROXY_*`,
  `GIT_CONFIG_*`) pointing at its local proxy; they grant nothing.
- The task text (issue title, rationale) is untrusted input to the agent.
  The layers above bound what it could do.

### Run it

```bash
CLAUDE_CODE_OAUTH_TOKEN=$(op read op://agents/cycle-runner-claude/token) uv run pytest -m claude
```

Three live tests run in pytest temp directories:

1. Claude adds `greet()` to a tiny repo through the real runner lifecycle.
   The test verifies the work independently: the function works, the tests
   pass, nothing was committed.
2. A one-turn limit gives a clean `failed`.
3. The isolation probe above.

About $0.05–0.10 per test in model usage, estimated by the SDK. With the
OAuth token this comes out of your Claude subscription's limits. The token is
passed only to these runs, not put in `.env`, so the Telegram bot never has
it. The deterministic tests (`tests/test_claude_executor.py`) replace the
SDK's `query()` with a fake and run in CI.

The executor CLI still runs only `FakeExecutor`: V1.0 doesn't let the CLI
point a real agent at a real directory.

### Deliberately not in V1.0

Real project repositories, GitHub push or PRs, Linear writes, Telegram
commands, K3s, leases, retries, A2A, or multi-agent orchestration.

### Next

- **Project/workspace configuration:** map a work request to a workspace
  (repository, how to check it out, test command, readable paths) without
  project-specific code in the executor. Linear labels such as `MT` or `TRD`
  are the likely key.
- **What the agent is told:** the approved title and rationale are thin. The
  issue description lives in Linear, and whether the runner (not the
  executor) should fetch it is the open question.
- **Persist `details`**, and give the result a place for a diff or branch.
- **The CLI:** a way to run `ClaudeCodeExecutor` on a configured workspace,
  once workspaces exist.

## V1.1

Cycle Runner works across several projects (MT, TRD, BET, JT, MTA, QB, …)
without the executor knowing any of them. The question V1.1 answers: *given
an approved work request for project X, where should the coding agent work,
and how is the work checked?*

```
Linear issue ──repo label──► WorkRequest.project_id ("MT")        set at approval
                                     │
executor.run_request / run_next      │  the runner assembles the context
        │ claim                      ▼
        ├──────────► WorkspaceResolver ──► projects.toml [projects.MT]
        │                  │  repository + test_command
        │                  ▼
        │            fresh clone: workspace_root/WR-000002 (origin removed)
        │                  │
        │ start            ▼ ExecutionWorkspace(path, test_command, readable)
        └──────────► Executor.execute(request, workspace)   Fake or Claude; project-agnostic
```

### How a work request knows its project

Every SB issue has exactly one label in Linear's **`repo` label group** (MT,
TRD, …); in the current cycle that's 40 of 40. It's a Linear fact:

1. `get_cycle_status` / `get_issue` report it as `project`.
2. The recommendation's Linear facts carry it.
3. Approval copies it into `WorkRequest.project_id` (schema version 3, a
   nullable column).

The approval side needs no configuration. An issue with no repo label, or
several, gives `project_id = None`.

### Project configuration

`projects.toml` at the repository root (override with
`CYCLE_RUNNER_PROJECTS`), read with the stdlib `tomllib`, so there's no new
dependency:

```toml
workspace_root = "~/.local/share/cycle-runner/workspaces"

[projects.MT]
repository   = "~/gitrepos/missing-table"
test_command = "uv run --directory backend pytest -q"
```

It's validated when loaded (Pydantic; unknown keys are refused):

- absolute paths (`~` allowed);
- a dedicated `workspace_root`: not `/`, not your home directory or above
  it, and not inside a repository (or vice versa);
- uppercase project ids;
- a single `test_command` with no shell operators. It's the only command the
  agent may run, so `cd x && …` could never work anyway.

The committed file lists what was verified on this machine: MT, MTA, TRD,
JT. BET and QB aren't cloned here yet.

### Workspaces: always a fresh clone

`WorkspaceResolver.resolve(request)`:

1. Checks the request has a project and that the project is configured.
2. Checks the repository is a git checkout.
3. Makes `workspace_root/WR-xxxxxx` (never reusing one).
4. Clones into it with `git clone --no-hardlinks`, then removes the clone's
   `origin` remote.

The real checkout is **only read**. The executor gets an `ExecutionWorkspace`
pointing at the clone, and V1.0's sandbox confines the agent to it. The
sandbox itself is unchanged.

### Who owns what

| Component | Owns |
|---|---|
| Runner (`executor.py`) | the lifecycle (claim, start, finish) and assembling the context: resolve a workspace, then execute |
| `WorkspaceResolver` (`projects.py`) | project id → configuration → a validated, fresh workspace. Knows nothing of Claude, ADK, Telegram or Linear. |
| Executor (`ClaudeCodeExecutor`, `FakeExecutor`) | doing the work in the workspace it's given. Knows nothing of projects or configuration. |

`Executor.execute` now takes `(request, workspace)`: the runner hands the
workspace over instead of the executor being built around one. A request
that can't be resolved (no project, unknown project, clone failure) goes
`claimed → failed` ("Not started: …"), a new transition. It never ran, so it
isn't `running`, and releasing it would only fail again. The executor, and so
Claude, is never invoked.

### Adding a project

Add a `[projects.X]` table where X is the issue's repo label in Linear.
There are no code changes. `tests/test_boundaries.py` fails if a string
constant equal to a project id appears in the code, and
`test_adding_a_project_is_only_configuration` runs a never-seen project
end to end from configuration alone.

### Tests

- **`tests/test_projects.py`** (CI):
  - loading, and 13 kinds of invalid configuration;
  - MT, TRD and BET each resolving to a clone of their own repository, with
    the origin byte-for-byte untouched and no remote;
  - no project, unknown project, not a checkout, or an existing workspace;
  - the runner handing over the resolved workspace;
  - Claude never invoked for unresolvable requests;
  - a new project by configuration alone.

  All repositories are disposable. No test touches a real repository or your
  database.
- **`tests/test_claude_live.py`** (`-m claude`): a `DEMO` work request goes
  from its project id through configuration, the resolver and a fresh clone,
  to Claude, passing tests and `completed`. The configured origin is verified
  unchanged.

### ⚠️ Existing approvals

`WR-000001` (SB-640) was approved before V1.1, so its `project_id` is `NULL`.
If the executor CLI claims it, it fails to start ("has no project"). Linear
says SB-640 is `MT`, but nothing sets that automatically.

### Not in V1.1

- **Running real projects' tests.** The sandbox denies network and reads
  under `~`, so `uv run pytest` can't reach its caches or install
  dependencies. The configuration describes real projects; executing them
  needs dependency setup, which comes next.
- Also not here: branches, commits, push or PRs; Linear writes; fetching the
  full issue description; a CLI option to run `ClaudeCodeExecutor` (the CLI
  still runs `FakeExecutor`).

### Deferred to V1.2

- **Issue context:** the runner (not the executor) fetches the issue
  description from Linear for the task.
- **Workspace setup:** dependencies inside the sandbox, and per-project
  readable paths such as uv caches.
- **Cleaning up old workspaces.**
- **What to do with pre-V1.1 requests** that have no project.

## V1.2

Real project execution: an approved MissingTable (MT) work request runs end
to end. Cycle Runner reads the whole Linear issue, clones MT fresh and
installs its dependencies. A Claude agent then works in the clone inside the
sandbox, and MT's own unit tests run there too. Nothing is pushed, committed
or written to Linear, and the MT checkout is never changed.

```
WR-000001 ──claim──► TaskSource (issue_context.py) ──► Linear, read-only: SB-640's full description
             │        WorkspaceResolver (projects.py) ─► git clone --branch main ─► setup: uv sync
             │                                                   (outside the sandbox, with network)
             ▼
          ExecutionTask + ExecutionWorkspace ──► ClaudeCodeExecutor (sandboxed, no network)
                                                     └─ MT unit tests, observed in the SDK stream
             ▼
          completed / failed  +  WR-000001.json and WR-000001.claude/ beside the workspace
```

If the Linear read, the clone or the setup fails, the request goes from
`claimed` to `failed` ("Not started: …") and Claude is never invoked.

### Run it

```bash
CLAUDE_CODE_OAUTH_TOKEN=$(op read op://agents/cycle-runner-claude/token) \
  op run --env-file .env -- uv run python -m cycle_runner.executor run \
    --request WR-000001 --executor claude --max-turns 40 --max-budget-usd 3
```

`--executor claude` only runs a request named with `--request`, and checks
the Linear credentials before it claims anything.

### Task context: the runner reads Linear, the executor doesn't

`LinearIssueSource` (issue_context.py) uses the existing read-only
`LinearClient` to fetch the identifier, title, description and labels. It
builds a task only when:
- the identifier matches;
- the issue's `repo` label equals the project recorded at approval;
- the description isn't empty.

Descriptions are capped at 20k characters. Executors receive an
`ExecutionTask` (issue, project, title, description, rationale) instead of
the `WorkRequest`, and they import neither Linear nor the store (checked by
`test_boundaries.py`).

The description is untrusted text. The prompt fences it as `<issue>` source
material and asks the agent to list anything that isn't a code change (for
example rotating a production password) as left for a human. Security
doesn't depend on that wording: what the agent can do is fixed by the
sandbox, the tool policy and the credential stripping from V1.0.

### Setup: outside the sandbox, so restricted

```toml
[projects.MT]
branch = "main"                  # the checkout itself is on a feature branch
setup_command = "env UV_PYTHON_INSTALL_DIR=.python UV_PYTHON_PREFERENCE=only-managed uv sync --directory backend --frozen"
setup_produces = ["backend/.venv/bin/python"]
test_command = "/usr/bin/env -C backend PATH=/usr/bin:/bin .venv/bin/python -m pytest tests/unit -o addopts= -o log_cli=false -n auto -q --capture=no"
test_success_pattern = '^\d+ passed(, \d+ (skipped|deselected|xfailed|xpassed|warnings?))* in [\d.]+s'
```

Installing needs network and caches, and the agent gets neither, so setup
runs before the agent and outside its sandbox. That makes it privileged, so
it's constrained:
- **One install step:** `uv sync`, `npm ci`, `pnpm install`, `yarn install`,
  `poetry install` or `bundle install`, optionally behind `env NAME=value`.
  `uv run` and anything else is refused when the config loads.
- **No shell:** no operators or quoting; it's split with `shlex` and run with
  `shell=False`.
- **Paths stay in the clone:** no absolute paths, no `..`, no `~`. The
  working directory is the clone.
- **No credentials:** the environment has every credential-looking variable
  removed.
- **A 10-minute timeout.**
- **Verified:** every `setup_produces` path must exist and resolve inside the
  clone.

Dependencies stay inside the workspace. `UV_PYTHON_INSTALL_DIR=.python` puts
the interpreter in the clone. uv's default links the venv to a Python under
`~`, which the sandbox can't read, and `setup_produces` catches exactly that.
The sandbox can already read the workspace, so no `readable` paths under `~`
are needed. The setup takes about 5 s with a warm uv cache.

Why the test command looks like that (each part was found by running MT's
tests in the sandbox):
- `env -C backend`: MT's `conftest.py` loads `../.env.test`.
- `--capture=no`: pytest's capture dies in the sandbox (exit 120).
- `PATH=/usr/bin:/bin`: one MT test shells out to 1Password `op`, which the
  sandbox can't run.
- `-o addopts=`: drops the verbose and coverage options.

Result: 1,324 passed and 12 skipped in the sandbox, the same as outside it
apart from the skips.

### Not trusting the agent's word

The executor watches the SDK stream (tool calls and the output Claude Code
captured), not just the agent's report. It records `completed` only if all of
these hold:
- the run ended cleanly and the report says the tests passed;
- the report names the command that actually ran (the live WR-000001 run
  produced a placeholder report, `summary: "test"`);
- the exact test command ran after the last edit, and its output matches
  `test_success_pattern`;
- the agent changed files. A change counts only if it touches a file that
  existed before or one the agent wrote. Test byproducts such as logs and
  pytest's temp directories don't count; they once made a no-op run look
  like a change.

The sandbox makes every command report exit code 1, even on success, so the
exit code is ignored. Why: V1.2 denies writes to Claude Code's shared temp
area, and its shell wrapper can't write its cwd file there. The output
pattern decides instead.

When the agent changes nothing, the result is `failed` with "No change
made." and its summary. That may be correct (the work may already be done),
but nothing was delivered, so a human decides.

### Workspaces: kept, and nothing hidden

The runner leaves three things under `workspace_root`:
- `WR-000001/`: the clone, with its origin remote removed;
- `WR-000001.json`: the result, turns, cost, test runs observed and the tail
  of the last test output;
- `WR-000001.claude/`: the agent's Claude Code state, including its session
  transcript. Without this, Claude Code wrote every agent transcript to
  `~/.claude/projects/<workspace path>`. The login token isn't written there
  (checked).

The store's result message ends with `[workspace: …]`. Nothing is deleted
automatically.

### Sandbox changes (found while doing this)

- **Claude Code's shared temp area was writable.** `/tmp/claude-<uid>`,
  which holds every Claude Code session's scratch files, could be written by
  sandboxed code. It's now `denyWrite`, and each run gets its own scratch
  directory. A `workspace_root` inside that area is refused.
- **`.git` in the clone is read-only to sandboxed code.** The runner later
  runs git there, outside the sandbox, and `.git/config` can name programs.
- **Background Bash is refused.** Its output landed outside the workspace,
  where the agent couldn't read it.
- **Parent-session variables are hidden.** When the executor is started from
  inside a Claude Code session, that session's variables
  (`CLAUDE_CODE_SESSION_ID`, `CLAUDE_TMPDIR`, …) are removed for the run.
  `SHELL` is pinned to bash.

The live isolation test (`-m claude`) checks the temp area and `.git` too.

### WR-000001

It was approved in V0.8, before projects were recorded. Its `project_id` was
set to `MT` by hand, on request, with a database backup taken first.
`backfill-project` now covers this case narrowly:

```bash
uv run python -m cycle_runner.executor backfill-project WR-000001 MT
```

It changes only a pending request that has no project, never overwrites one,
and is a no-op if the value is already set. The run cross-checks the project
against the issue's `repo` label in Linear. There's no second approval and
no Linear change.

The deliberate run (2026-09-28) ended `failed`: "No change made."
- SB-640's code items (rate limiting and a password policy) were already on
  MT `main` (MT #612).
- The agent ran MT's unit tests (1,324 passed, 12 skipped) and changed
  nothing.
- Its report was the placeholder described above; the check for that was
  added after this run.
- The MT checkout was unchanged: its refs, status and stash hashed the same
  before and after.

### Tests

- **Offline** (in CI): setup validation and execution with a stand-in `uv`,
  branch cloning, issue context, the backfill, observed-test verification,
  change attribution, report consistency, and the boundaries.
- **`tests/test_mt_live.py`** (markers `claude` and `linear`, with a
  temporary database and workspace root):
  - SB-640 on a fresh MT clone;
  - a small real change to MT whose unit tests must pass in the sandbox.

  About $0.40 per agent run.

### Not in V1.2

- Branches, commits, push, PRs and Linear writes. The work stays in the clone.
- Cleaning up old workspaces.
- An outcome for "already done", distinct from `failed`.
- An independent re-run of the tests by the runner. Re-running them outside
  the sandbox would execute agent-written code unconfined, so the evidence is
  the observed in-sandbox run.
- Setup for the other projects (MTA, TRD, JT).


## V1.3

Local Git delivery. A verified change becomes a local branch and a commit
in the disposable clone, made by Cycle Runner and never by the agent.
Nothing is pushed, no PR is created and nothing is written to Linear. The
commit waits there for a human.

```
claim ─► task (Linear, read-only) ─► workspace (fresh clone + setup) ─► Claude (sandboxed)
      ─► ExecutionResult: changed | no_change | failed
             │ changed only
             ▼
         LocalGitDelivery (outside the sandbox): audit .git ─► select files ─► branch cycle-runner/WR-xxxxxx
             ─► stage exactly those files ─► check the staged diff ─► commit ─► audit again (still no remote)
             ▼
         store: completed (changed), branch, commit_sha   +   WR-xxxxxx.json (diff summary, review: pending)
```

### Outcomes

The lifecycle `status` is unchanged. A new `outcome` column (schema v4)
says what came of a run:

| outcome     | status      | meaning                                                                       |
|-------------|-------------|-------------------------------------------------------------------------------|
| `changed`   | `completed` | Work done, tests observed passing after the last edit, files changed          |
| `no_change` | `completed` | Tests observed passing and nothing needed changing (V1.2's SB-640). A success |
| `failed`    | `failed`    | Anything else, including an untrustworthy report or a refused delivery        |

The files decide between `changed` and `no_change`, not the agent. Only
files the executor saw the agent change count, not test byproducts. An
agent that says `no_change` while files changed fails. A database trigger
keeps `outcome` consistent with `status`.

"Needs a human" isn't a separate state. The agent is asked to list
anything it couldn't do in its summary, and every outcome already waits for
a person.

### Meaningless reports

A report fails the run when:
- its summary is a placeholder (`test`, `TODO`, `done`, `n/a`, …) or too
  short (under 40 characters or 6 words);
- its summary contains tool-call markup;
- it names a test command other than the one that ran.

The evidence is still checked independently: observed test runs, attributed
file changes and the git diff.

Root cause, found in the agent's transcripts: the model sometimes wrote
tool-call markup (`</summary><parameter name="tests_passed">`) into the long
summary string. That swallowed the fields after it; after retries it gave up
or sent placeholders, which is where V1.2's `summary: "test"` came from.
`summary` is now the last field in the schema, and the prompt asks for plain
prose. In the live runs since, the report was accepted on the first attempt.

### Branch and commit

- **Branch:** `cycle-runner/WR-000123`, built only from a validated work
  request id (`^WR-\d{6,}$`), then checked with `git check-ref-format`. An
  existing branch is never reused.
- **Commit:** author and committer are `Cycle Runner
  <cycle-runner@localhost.invalid>`, made with `--no-verify` and no signing.
- **Message:** the issue id plus the title approved at approval time. Control
  characters are removed, whitespace is collapsed, and the subject is capped
  at 72 characters. The issue description never goes in. An unusual issue id
  becomes `Cycle Runner: …`. The message goes through stdin, never a shell.
- **Base:** exactly one commit on top of the clone's HEAD, checked after
  committing.

### Which files get committed

A file is committed only if **git** reports it changed **and** the
executor saw the agent change it. Everything else stays in the workspace,
uncommitted, and is listed with a reason. Test byproducts (for MT, about
120 pytest temp files) are left this way.

Never committed, whatever `.gitignore` says:
- **Dependency, cache and tool directories:** `.venv`, `venv`, `.python`,
  `node_modules`, `site-packages`, `__pycache__`, `.pytest_cache`,
  `.mypy_cache`, `.ruff_cache`, `.tox`, `.cache`, `.claude`, `.gradle`, IDE
  directories.
- **Environment and credential files:** `.env*`, keys and certificates
  (`*.pem`, `*.key`, `id_*`, …), `.netrc`, `.npmrc`, `.pypirc`, and names
  containing `credential`, `secret` or `token`.
- **Generated data:** logs, databases, coverage files.
- **Cycle Runner's own records:** `WR-*.json`.
- **Unsafe entries:** symlinks, binaries, and files over 1 MB.

One more rule: if an included file looks like it holds a credential
(private keys; GitHub, Anthropic, OpenAI, AWS, Slack, Linear or 1Password
token shapes; or the value of one of the runner's own credential
variables), the whole delivery is refused. The run fails and nothing is
committed.

`WR-xxxxxx.json` and `WR-xxxxxx.claude/` sit beside the clone, not inside
it, so git never sees them.

### Git safety

- **A small, fixed set of git commands:** `rev-parse`, `config --list`,
  `remote`, `status`, `check-ref-format`, `switch -c`, `add`, `diff
  --cached`, `commit`, `diff-tree`, plus `reset`, `switch` and `branch -D`
  to undo a refused delivery. `test_boundaries.py` checks this, and that no
  `push`, `fetch`, `pull` or `clone` appears.
- **Hardened git calls.** Every call runs with system and global config off
  (`GIT_CONFIG_NOSYSTEM`, `GIT_CONFIG_GLOBAL=/dev/null`) and with hooks,
  fsmonitor, signing, pagers and external diff off. `protocol.allow=never`
  means git can't reach any network. Pathspecs are literal.
- **Config audited before and after.** The clone's `.git/config` is checked
  both times. Any remote, url rewrite, include, filter, hooks path or other
  unexpected key refuses the delivery.
- **The agent can't have touched `.git`:** the sandbox denies writes there
  (V1.2).
- **Tripwires:** a symlinked `.git`, or an index staged by anything else,
  refuses the delivery.
- **Found in an independent review, and fixed.** Git read
  `$HOME/.config/git/attributes` and `ignore` even with global config off,
  and `HOME` was the workspace. An agent could therefore:
  - re-encode a secret with `working-tree-encoding`, so the on-disk scan saw
    nothing, and it was committed;
  - or hide its own files from `git status`.

  `HOME` is now an empty temp directory, attributes and excludes files are
  off, and the secret, binary and size checks run on the **staged blob**,
  the bytes git will actually commit.
- **Any error during delivery refuses it** (a git timeout, a missing git,
  an unreadable file), including checks after the commit. The undo removes
  the commit and the branch and leaves the agent's work unstaged in the
  working tree. The runner records the run as `failed` instead of leaving it
  `running`.
- **File names are compared NFC-normalized**, because macOS git reports
  precomposed names.

### The human review boundary

```bash
uv run python -m cycle_runner.executor review WR-000123
```

It shows the outcome, workspace, run record, tests observed, branch and
commit, the diff summary (added, changed and deleted files, insertions and
deletions) and what was left uncommitted. The record says `review: pending`.
Nothing is approved or pushed automatically, and the workspace is kept.

### No change, failure

- **`no_change` and `failed` never touch git:** no branch and no commit.
  The clone stays exactly as the agent left it.
- **A refused delivery** is `failed`, with "Local delivery refused, nothing
  committed: …". Anything already staged or branched is undone.

### Existing records

WR-000001 keeps its V1.2 record (`failed`, "No change made…", outcome
`NULL`). A terminal record isn't rewritten, and the `failed → completed`
transition doesn't exist. The same case is now covered as `no_change` by a
regression test.

### Tests

- **Offline** (568 in CI): outcomes and the SB-640 regression; meaningless
  and garbled reports; store outcomes, the trigger and the migration;
  delivery. The delivery tests cover:
  - the branch, commit and author;
  - the diff summary from git;
  - untracked and unattributed files;
  - about 20 sensitive and generated paths;
  - secret shapes and the runner's own secrets;
  - symlinks, binaries and huge files;
  - `.gitignore`d files;
  - a remote or hostile config;
  - hooks, both the clone's and global ones;
  - an existing branch, a staged index, a symlinked `.git`;
  - message and branch sanitizing;
  - no network protocol;
  - the runner paths and `review`.
- **Live** (`-m claude`, `tests/test_mt_live.py`): the disposable greet task
  and a fresh MT clone each end in one Cycle Runner commit, with byproducts
  left, no remote and the MT checkout unchanged. SB-640 on a temporary
  database: when it's `no_change`, no branch is created.

### Not in V1.3

Push, PRs, the GitHub API, Linear writes, running from Telegram, retries,
CI feedback, approving a delivery, and cleaning up workspaces.

## V1.4

Human-approved GitHub delivery. It's Cycle Runner's first GitHub write. A
local commit (V1.3) reaches GitHub only after a person approves that exact
commit on the command line. Then it's pushed as one branch and opened as a
**draft** PR. Nothing is approved automatically, and nothing is merged.

```
 WorkRequest (completed, changed)           DeliveryApproval (separate record, same database)
 branch cycle-runner/WR-7, commit abc…
        │
        ▼  review WR-7               local only: re-verify the commit, show files, diff, tests, state
 review_pending
        │
        ▼  approve WR-7 --commit abc1234     re-verify ─► record: full SHA, branch, base, repository,
 approved (APR-3, abc…) ◄───────────────────              evidence, who, when. Immutable.
        │
        ▼  deliver WR-7   (the only process with CYCLE_RUNNER_GITHUB_TOKEN)
        │   1. re-verify the local commit == abc…   ─ mismatch ─► APR-3 invalid, nothing pushed
        │   2. repository = projects.toml = APR-3's; exists, not archived, token may push
        │   3. base already on GitHub's main (so exactly one commit goes up)
        │   4. git push <url> abc…:refs/heads/cycle-runner/WR-7   (no remote added, no force, no tags)
        │   5. GitHub's cycle-runner/WR-7 == abc…
 pushed ─────────────────────────────────── 6. find or create one draft PR for the branch at abc…
        ▼
 pr_created (PR #n)
```

### Approval: bound to one SHA

`approve WR-x --commit <sha>` is the only way to approve. The human names
the commit they reviewed (at least 7 characters of its SHA). A mismatch
refuses the approval, and a conversation can't approve anything.

Before recording anything, the local commit is verified again:
- the workspace exists;
- `.git` is a plain directory with no remote and no unusual configuration;
- the branch exists, HEAD is on it, and both are at the recorded commit;
- the commit exists, has exactly one parent, and that parent is the clone's
  `main`, so there's exactly one commit on top;
- Cycle Runner authored and committed it;
- its files and line counts match the V1.3 record;
- no protected files, secrets, binaries or oversized files are in it.

The approval record keeps the full SHA, branch, base SHA, project, target
repository (from `projects.toml`), and who approved and when. It also keeps
the evidence the human saw: message, tree, diff and observed tests.

The database enforces the rules itself:
- none of those fields can ever change;
- approvals are never deleted;
- only a completed, `changed` request's own recorded commit can be approved;
- each request has at most one live approval.

Delivery states: `approved → pushed → pr_created`. `invalid` and `rejected`
are final. The work request itself is untouched: a failed push is a
delivery failure (`last_error` on the approval), not failed engineering.

If the commit changes after approval (amended, moved, a new commit, a
changed record), the next `deliver` marks the approval `invalid` and pushes
nothing. The approval never moves to another commit, and a new commit can't
be approved against the old request. A new explicit approval is needed.

### The GitHub credential

- **A fine-grained personal access token**, limited to the delivery
  repositories: Contents read/write and Pull requests read/write. It's stored
  in 1Password (`op://agents/cycle-runner-github/credential`) and referenced only by
  `.env.github`.
- **Only `deliver` gets it** (`op run --env-file .env.github -- …`). The bot's
  `.env` doesn't contain it.
- **The coding agent never gets it.** `run --executor claude` refuses to start
  if `CYCLE_RUNNER_GITHUB_TOKEN` is in its environment. The agent's sandbox
  would blank and deny it anyway, like every `*TOKEN*` variable.
- **git receives it as an in-memory `http.extraHeader`** via `GIT_CONFIG_*`
  environment variables. It's never in argv, a file, a remote or the
  workspace. The API receives it in a header. It's scrubbed from any error
  text and never logged.

### Push

The push is exactly one refspec, `git push --porcelain --no-verify
--no-follow-tags <https://github.com/owner/name.git> <sha>:refs/heads/<branch>`,
with the same hardened git as V1.3. `protocol.https` is allowed for that one
call only. There's no remote, no force, no tags and no other refs.

The target is `github = "owner/name"` in `projects.toml`, never the clone.
If it has changed since approval, delivery stops.

The approved commit's base must already be on the GitHub base branch.
Otherwise the push would publish the local checkout's unpushed commits too,
so delivery stops.

After pushing, GitHub is asked where the branch is. Anything but the
approved SHA is a delivery failure.

### Draft PR

- **Always a draft.** A PR created as ready for review is treated as a
  failure.
- **Title:** the commit subject.
- **Body:**
  - a Cycle Runner banner;
  - the issue id and approved title, project, work request, commit and base,
    and who approved it and when;
  - changed files with line counts;
  - tests, stated only from evidence ("ran N times; last result: …", or "No
    test run was observed. Treat this change as untested.");
  - the agent's summary, labelled as its own unverified words.
- **Neutralized:** `@mentions` and issue-closing keywords from agent or
  issue text. The issue description isn't copied.

### Found in an independent review, and fixed

- **A rejected commit could be approved later.** A rejection is now final:
  the store and a database trigger both refuse it.
- **Agent-chosen file names and test output could break out of the PR's
  Markdown.** For example, a backtick or newline in a path could add live
  `@mentions`, issue-closing keywords or HTML. Such names are now never
  committed and refuse approval. Test output is flattened, and closing
  keywords with full URLs are neutralized too.
- **Replace refs:** `refs/replace/*` let the human review one tree while
  another was pushed. git now runs with `GIT_NO_REPLACE_OBJECTS=1`, and replace
  refs, grafts, alternates or a shallow file refuse the delivery.
- **The push re-read the workspace's `.git/config` after it was audited.** A
  `pushInsteadOf` could redirect the push, or a proxy could see the token.
  Now the approved branch is fetched into a fresh bare repository Cycle Runner
  owns, and its SHA, tree and parent are checked again. The push happens from
  there, so the workspace's configuration takes no part.
- **An existing PR was reused whatever its state.** Now it's reused only if
  it's open, a draft, and against the configured base. A closed, merged,
  retargeted or ready PR stops delivery, including one GitHub created as
  ready.

### Idempotency and recovery

| Situation                                   | `deliver` does                                           |
|---------------------------------------------|----------------------------------------------------------|
| Branch already on GitHub at the approved SHA | Doesn't push again                                      |
| Branch on GitHub at any other commit         | Stops; nothing pushed                                   |
| A PR for the branch at that SHA exists       | Reuses it (no duplicate)                                |
| Pushed, PR creation failed                   | State `pushed` + error; running again creates only the PR |
| Auth/permission failure                      | Error recorded; approval stays `approved`; nothing pushed |
| Push failure                                 | Error recorded; no PR; no new commit                    |
| Local commit changed                         | Approval `invalid`; nothing pushed; approve again        |
| Already `pr_created`                         | Returns it                                              |

### Security model, in one place

- **The agent:** no git, no network, no GitHub token, and no write access to
  `.git`. It never runs in the delivery process.
- **Local commit:** made by Cycle Runner, with protected files and secrets
  kept out (V1.3), and verified again at approval and at delivery.
- **Approval:** a deliberate CLI act naming the SHA, recorded immutably.
- **GitHub writes:** only in `github_delivery.py` (checked by
  `test_boundaries.py`): five REST calls plus one `git push`, against the
  configured repository only.

### How to review and approve

```bash
uv run python -m cycle_runner.executor review WR-000007     # verified: ok / the problem; delivery: review_pending
git -C ~/.local/share/cycle-runner/workspaces/WR-000007 show  # look at the change itself
uv run python -m cycle_runner.executor approve WR-000007 --commit 1a2b3c4d5e6f
op run --env-file .env.github -- uv run python -m cycle_runner.executor deliver WR-000007
uv run python -m cycle_runner.executor reject WR-000007 --commit 1a2b3c4d5e6f --reason "wrong approach"
```

### Tests

- **Offline** (in CI): the approval store (immutability, transitions,
  eligibility, one live approval) and review/approve/reject. They cover:
  - approval refusal for a missing workspace, a missing branch, a wrong HEAD,
    an extra or amended commit, a remote, hostile config, a changed record,
    protected files, secrets, a missing commit and a foreign author;
  - delivery against a stand-in GitHub: a local bare repo plus a mocked REST
    API;
  - idempotency, push-then-PR failure and retry, auth, permission and push
    failures;
  - a remote branch at another commit, a base missing on GitHub, and a
    changed configuration;
  - the token never written or in argv, and the agent never given it;
  - the CLI.
- **Live** (`-m github`, `tests/test_github_live.py`): a real push and draft
  PR on `silverbeer/cycle-runner-sandbox`, using the CLI end to end and
  delivering twice.

### Live result (2026-09-29)

`tests/test_github_live.py` ran against `silverbeer/cycle-runner-sandbox`:
- local commit, review, `approve --commit`, `deliver`;
- draft PR #1, open, with head `de0983b85ae5` (the approved SHA, one commit
  by Cycle Runner, one file);
- a second `deliver` returned the same PR, with no new push and no duplicate;
- the repo holds `main` plus that branch, and no tags.

Draft PRs work on this private repository.

Limitation: the repository API's `permissions.push` reflects the account's
role, not the fine-grained token's scope. So "the token may push" is really
checked by the push itself. A token without write access fails there,
safely and recorded, before any PR.

### Not in V1.4

Approving from Telegram, Telegram execution, CI feedback, merging, marking
PRs ready, Linear writes, retries, GitHub Apps, and delivering to MT.
Delivery to MT needs `github = "silverbeer/missing-table"` in
`projects.toml` and a token scoped to it. Do that deliberately, after the
sandbox.
