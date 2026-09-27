"""Cycle Runner: a Google ADK agent backed by a local Ollama model.

Deliberately empty. `adk run` / `adk web` import cycle_runner.agent directly,
so the package doesn't import it here: that keeps parts that must not depend
on ADK (the work-request store, the executor) from loading it as a side effect.
"""
