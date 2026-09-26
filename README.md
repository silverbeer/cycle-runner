# cycle-runner

A learning project for Google's [Agent Development Kit](https://adk.dev) (ADK).

One agent, `cycle_runner`, talks through a local model served by Ollama. It
acts as Product Owner / Scrum Master for a weekly engineering cycle.

- **V0.1**: conversation only.
- **V0.2**: one tool, `get_cycle_status`, which returns hard-coded cycle data.
  The model decides for itself when to call it. Nothing talks to Linear yet.

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
uv run pytest            # config + tool tests, and two live tests through Ollama
uv run pytest -m "not ollama"   # skip the live test
```

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
  `__init__.py` does `from . import agent`, and why there's no `main()`.
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
