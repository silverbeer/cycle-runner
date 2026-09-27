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
  `__init__.py` does `from . import agent`. (`__main__.py` is only for the
  Telegram bot; `adk run` doesn't use it.)
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

