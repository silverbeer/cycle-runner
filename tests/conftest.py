import json
import os
import urllib.request

import pytest

from cycle_runner import linear_tools
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


@pytest.fixture(autouse=True)
def no_real_linear(request, monkeypatch):
    """Keep every test off the real Linear API unless it's marked `linear`."""
    real_client = linear_tools._client  # a test may swap it for a fake
    real_client.cache_clear()
    if request.node.get_closest_marker("linear") is None:
        for name in ("LINEAR_CLIENT_ID", "LINEAR_CLIENT_SECRET"):
            monkeypatch.delenv(name, raising=False)
    yield
    real_client.cache_clear()


FAKE_CYCLE = {
    "number": 42.0,
    "name": None,
    "startsAt": "2030-01-06T05:00:00.000Z",
    "endsAt": "2030-01-13T05:00:00.000Z",
    "progress": 0.5,
}
FAKE_ISSUES = [
    {"identifier": "TEST-1", "title": "Ship the widget", "estimate": 3, "state": {"name": "Done", "type": "completed"}},
    {"identifier": "TEST-2", "title": "Fix the flaky login", "estimate": 2, "state": {"name": "In Progress", "type": "started"}},
    {"identifier": "TEST-3", "title": "Write the release notes", "estimate": 1, "state": {"name": "Todo", "type": "unstarted"}},
    {"identifier": "TEST-4", "title": "Old idea", "estimate": None, "state": {"name": "Canceled", "type": "canceled"}},
]
FAKE_ISSUE = {
    "identifier": "TEST-2",
    "title": "Fix the flaky login",
    "description": "Login fails about 1 in 20 runs on CI.",
    "estimate": 2,
    "priorityLabel": "High",
    "state": {"name": "In Progress", "type": "started"},
    "assignee": {"name": "Pat Example"},
    "labels": {"nodes": [{"name": "bug"}]},
    "cycle": {"number": 42.0},
}


class FakeLinear:
    """Stands in for LinearClient: answers the two queries the tools send."""

    def __init__(self, cycle=FAKE_CYCLE, issues=FAKE_ISSUES, issue=FAKE_ISSUE, page_size=50):
        self.cycle, self.issues, self.issue, self.page_size = cycle, issues, issue, page_size
        self.calls = []

    async def query(self, document, variables=None):
        self.calls.append(variables)
        if "activeCycle" in document:
            if self.cycle is None:
                return {"teams": {"nodes": [{"activeCycle": None}]}}
            start = int(variables.get("after") or 0)
            page = self.issues[start : start + self.page_size]
            end = start + len(page)
            return {
                "teams": {
                    "nodes": [
                        {
                            "activeCycle": {
                                **self.cycle,
                                "issues": {
                                    "pageInfo": {"hasNextPage": end < len(self.issues), "endCursor": str(end)},
                                    "nodes": page,
                                },
                            }
                        }
                    ]
                }
            }
        return {"issue": self.issue}


@pytest.fixture
def fake_linear(monkeypatch):
    """Replace the tools' Linear client with FakeLinear and return it."""
    fake = FakeLinear()
    monkeypatch.setattr(linear_tools, "_client", lambda: fake)
    return fake
