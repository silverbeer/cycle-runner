import json
import os
import urllib.request
from datetime import date

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
    "description": None,
    "startsAt": "2030-01-06T05:00:00.000Z",
    "endsAt": "2030-01-13T05:00:00.000Z",
    "progress": 0.5,
}
def fake_issue(identifier, title, state, state_type, estimate=1, priority="No priority",
               created="2030-01-01", labels=(), blocked_by=()):
    """One issue node as the cycle query returns it. blocked_by: (id, state_type) pairs."""
    return {
        "identifier": identifier,
        "title": title,
        "estimate": estimate,
        "priorityLabel": priority,
        "createdAt": f"{created}T12:00:00.000Z",
        "state": {"name": state, "type": state_type},
        "labels": {"nodes": [{"name": name} for name in labels]},
        "inverseRelations": {
            "nodes": [
                {"type": "blocks", "issue": {"identifier": blocker, "state": {"type": blocker_type}}}
                for blocker, blocker_type in blocked_by
            ]
        },
    }


FAKE_ISSUES = [
    fake_issue("TEST-1", "Ship the widget", "Done", "completed", estimate=3),
    fake_issue("TEST-2", "Fix the flaky login", "In Progress", "started", estimate=2, priority="High"),
    fake_issue("TEST-3", "Write the release notes", "Todo", "unstarted", priority="Medium"),
    fake_issue("TEST-4", "Old idea", "Canceled", "canceled", estimate=None),
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
    "inverseRelations": {"nodes": []},
}


class FakeLinear:
    """Stands in for LinearClient: answers the two queries the tools send."""

    def __init__(self, cycle=FAKE_CYCLE, issues=FAKE_ISSUES, issue=FAKE_ISSUE, page_size=50):
        self.cycle, self.issues, self.issue, self.page_size = cycle, issues, issue, page_size
        self.calls = []  # variables of each request
        self.documents = []  # GraphQL text of each request

    async def query(self, document, variables=None):
        self.calls.append(variables)
        self.documents.append(document)
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


@pytest.fixture(autouse=True)
def work_request_db(tmp_path, monkeypatch):
    """Every test gets its own work-request database, never the developer's."""
    path = tmp_path / "cycle-runner.db"
    monkeypatch.setenv("CYCLE_RUNNER_DB", str(path))
    return path


@pytest.fixture(autouse=True)
def fixed_today(monkeypatch):
    """Pin "today" so ages and days remaining are deterministic: 2030-01-10."""
    monkeypatch.setattr(linear_tools, "_today", lambda: date(2030, 1, 10))


@pytest.fixture
def fake_linear(monkeypatch):
    """Replace the tools' Linear client with FakeLinear and return it."""
    fake = FakeLinear()
    monkeypatch.setattr(linear_tools, "_client", lambda: fake)
    return fake


# --- a scripted model, for deterministic end-to-end ADK tests -----------------

from google.adk.models import BaseLlm, LlmResponse  # noqa: E402
from google.genai import types  # noqa: E402


class ScriptedLlm(BaseLlm):
    """A BaseLlm that replies from a fixed script, in order, and records every request.

    Root agent and recommender share one instance, so the script lists every
    model call of a turn in the order ADK makes them. Running out of script
    fails the test: an unexpected extra model call is a bug.
    """

    model: str = "scripted"
    script: list = []
    requests: list = []

    async def generate_content_async(self, llm_request, stream=False):
        self.requests.append(llm_request)
        if not self.script:
            raise AssertionError("unexpected model call: the script is empty")
        yield self.script.pop(0)


def say(text):
    return LlmResponse(content=types.Content(role="model", parts=[types.Part(text=text)]))


def call(name, **args):
    return LlmResponse(
        content=types.Content(role="model", parts=[types.Part(function_call=types.FunctionCall(name=name, args=args))])
    )
