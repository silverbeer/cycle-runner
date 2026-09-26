import json
import os
import urllib.request

import pytest

from cycle_runner.agent import MODEL


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
