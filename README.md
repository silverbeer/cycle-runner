# cycle-runner

A learning project for Google's [Agent Development Kit](https://adk.dev) (ADK).

One agent, `cycle_runner`, talks through a local model served by Ollama. It
acts as Product Owner / Scrum Master for a weekly engineering cycle.

- **V0.1**: conversation only.
- **V0.2**: one tool, `get_cycle_status`, which returns hard-coded cycle data.
  The model decides for itself when to call it. Nothing talks to Linear yet.
- **V0.3**: Telegram as the human interface, behind a small generic gateway, so
  the Telegram layer carries no Cycle Runner logic. See [V0.3](#v03).
- **V0.4**: the cycle and its issues live in SQLite and survive restarts. Two
  domain tools read them. See [V0.4](#v04).

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
uv run python -m cycle_runner.bootstrap   # create data/cycle-runner.db with the demo cycle
```

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
uv run pytest -m "not ollama and not telegram"   # offline only, no Ollama, no network
uv run pytest -m ollama                # live round-trips through the local model
uv run --env-file .env pytest -m telegram        # real Telegram API (needs a token)
```

CI (`.github/workflows/ci.yml`) runs the offline set on every PR and every push
to `main`. Every test uses its own temporary SQLite file (`tests/conftest.py`),
so tests never read or change your `data/cycle-runner.db`.

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
| `CYCLE_RUNNER_DB` | `data/cycle-runner.db` | SQLite file holding the cycle (relative to the working directory) |
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
uv run --env-file .env python -m cycle_runner
```

Then, in Telegram, open your bot, send `/start`, and chat.

### Manual Telegram check

The automated tests mock Telegram. To check the real thing end to end:

1. `uv run --env-file .env pytest -m telegram` confirms Telegram accepts the token.
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
                                                   │  tools=[get_current_cycle, get_cycle_status]
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

### Why two tools

`get_cycle_status` returns everything `get_current_cycle` returns, plus the
issues. The overlap is deliberate, to see how the model picks between tools
with neighbouring descriptions. Measured with gemma4:12b, 3 runs per question,
18 runs in total:

| Question | Tool chosen |
|---|---|
| What is the status of my cycle? / How is the cycle going? / What are we working on? | `get_cycle_status` 9/9 |
| What is the goal of this cycle? / When does the cycle end? | `get_current_cycle` 6/6 |
| What is a retrospective? | none 3/3 |

The docstrings do the routing: each one says what the tool is for and points
at the other for everything else. For a cycle this small, `get_cycle_status`
alone would answer every question. The small tool earns its place once the
issue list gets long enough that returning it for "when does the cycle end?"
wastes context.

### Run it

```bash
uv run python -m cycle_runner.bootstrap           # once; safe to repeat
uv run --env-file .env python -m cycle_runner     # Telegram bot
uv run adk run src/cycle_runner                   # or the terminal chat
```

`bootstrap` creates the demo cycle (Week 39, `DEMO-1`..`DEMO-4`) only if it
isn't there, and never overwrites existing records. Delete
`data/cycle-runner.db` to start over. `data/` and `*.db` are gitignored.

### Restart check

1. Start the bot and ask "How is the cycle going?". It calls
   `get_cycle_status` and lists `DEMO-1`..`DEMO-4`.
2. Stop the bot (Ctrl-C) and start it again.
3. Ask "What did I just ask you?". The session is new, so it doesn't know.
   That's the conversation state that was lost.
4. Ask "How is the cycle going?" again. It gives the same cycle and issues,
   read from SQLite. That's the application state that survived.

