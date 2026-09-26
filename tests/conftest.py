import json
import os
import urllib.request

import pytest

from cycle_runner.agent import MODEL
from cycle_runner.bootstrap import seed_demo_data
from cycle_runner.store import CycleStore


def _ollama_has_model() -> bool:
    base = os.environ.get("OLLAMA_API_BASE", "http://localhost:11434")
    try:
        with urllib.request.urlopen(f"{base}/api/tags", timeout=2) as resp:
            names = {m["name"] for m in json.load(resp)["models"]}
    except OSError:
        return False
    return MODEL.removeprefix("ollama_chat/") in names


@pytest.fixture(scope="session")
def ollama():
    """Skip the test unless the local Ollama server has the agent's model."""
    if not _ollama_has_model():
        pytest.skip(f"Ollama not serving {MODEL}")


@pytest.fixture(autouse=True)
def cycle_db(tmp_path, monkeypatch):
    """Point every test at its own seeded SQLite file, never the developer's database."""
    path = tmp_path / "cycle-runner.db"
    monkeypatch.setenv("CYCLE_RUNNER_DB", str(path))
    seed_demo_data(CycleStore(path))
    return path
