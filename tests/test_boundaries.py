"""Architecture rules, checked by reading the source.

- gateway.py, telegram_adapter.py and linear_client.py are meant to be reusable
  with other agents, so no Cycle Runner business logic may leak into them.
- Only linear_client.py knows how Linear is reached (URL, auth, HTTP).
- The agent gets domain tools, never a way to run arbitrary requests.
"""

import ast
from pathlib import Path

import pytest

import cycle_runner

PACKAGE = Path(cycle_runner.__file__).parent
CORE_BUSINESS_TERMS = ["get_cycle_status", "get_issue", "root_agent", "Scrum", "Product Owner"]
GENERIC_MODULES = {
    "gateway.py": CORE_BUSINESS_TERMS + ["Linear"],
    "telegram_adapter.py": CORE_BUSINESS_TERMS + ["Linear"],
    "linear_client.py": CORE_BUSINESS_TERMS + ["SB", "cycle"],
}
LINEAR_TRANSPORT_TERMS = ["api.linear.app", "oauth/token", "Authorization", "client_secret"]


def _imported_modules(path: Path) -> set[str]:
    tree = ast.parse(path.read_text())
    names = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            names.add(node.module or "")
    return names


def _modules():
    return sorted(PACKAGE.glob("*.py"))


@pytest.mark.parametrize("module", GENERIC_MODULES)
def test_generic_modules_do_not_import_cycle_runner(module):
    imports = _imported_modules(PACKAGE / module)
    assert not any(name.startswith("cycle_runner") for name in imports), imports


@pytest.mark.parametrize(("module", "terms"), GENERIC_MODULES.items())
def test_generic_modules_do_not_mention_cycle_runner_business_logic(module, terms):
    source = (PACKAGE / module).read_text()
    assert [term for term in terms if term in source] == []


def test_telegram_adapter_does_not_depend_on_adk():
    imports = _imported_modules(PACKAGE / "telegram_adapter.py")
    assert not any(name.startswith("google") for name in imports), imports


def test_only_the_linear_client_makes_http_calls():
    users = [path.name for path in _modules() if "httpx" in _imported_modules(path)]
    assert users == ["linear_client.py"]


def test_only_the_linear_client_knows_how_linear_is_reached():
    leaks = {
        path.name: [term for term in LINEAR_TRANSPORT_TERMS if term.lower() in path.read_text().lower()]
        for path in _modules()
        if path.name != "linear_client.py"
    }
    assert {name: terms for name, terms in leaks.items() if terms} == {}


def test_agent_gets_domain_tools_not_raw_request_access():
    from cycle_runner.agent import root_agent

    for tool in root_agent.tools:
        name = getattr(tool, "__name__", None) or tool.name
        for word in ["sql", "graphql", "query", "execute", "request", "http"]:
            assert word not in name.lower()


def test_approval_and_recommendation_cannot_reach_linear_or_start_processes():
    # V0.7 records approvals only. Nothing in these modules may talk to Linear
    # directly or launch anything.
    for module in ["approval.py", "recommendation.py"]:
        imports = _imported_modules(PACKAGE / module)
        forbidden = {"httpx", "subprocess", "cycle_runner.linear_client", "os", "multiprocessing"}
        assert not imports & forbidden, (module, imports & forbidden)


# --- V0.8: approval -> work-request store, and nothing else ------------------


def test_only_the_work_request_store_knows_about_sqlite():
    users = [path.name for path in _modules() if "sqlite3" in _imported_modules(path)]
    assert users == ["work_requests.py"]


def test_the_store_knows_nothing_about_adk_telegram_linear_or_the_rest_of_the_app():
    imports = _imported_modules(PACKAGE / "work_requests.py")
    assert not any(
        name.startswith(("google", "telegram", "httpx", "litellm", "cycle_runner")) for name in imports
    ), imports


def test_only_approval_and_the_executor_can_reach_the_work_request_store():
    # The agent, the recommender, the tools, the gateway and Telegram can't
    # create, read or change work requests. Approval creates them (V0.8); the
    # executor claims and runs them (V0.9).
    users = [
        path.name
        for path in _modules()
        if path.name != "work_requests.py"
        and any(name.startswith("cycle_runner.work_requests") for name in _imported_modules(path))
    ]
    assert sorted(users) == ["approval.py", "claude_executor.py", "executor.py", "fake_executor.py"]


@pytest.mark.parametrize(
    ("forbidden", "why"),
    [
        (("google.adk",), "the model and agents"),
        (("cycle_runner.linear_client", "cycle_runner.linear_tools", "httpx"), "Linear"),
        (("telegram", "cycle_runner.telegram_adapter", "cycle_runner.gateway"), "Telegram"),
        (("subprocess", "multiprocessing", "claude_agent_sdk", "kubernetes"), "a coding agent"),
    ],
)
def test_approval_cannot_reach(forbidden, why):
    imports = _imported_modules(PACKAGE / "approval.py")
    assert not [name for name in imports if name.startswith(forbidden)], f"approval.py reaches {why}"


def test_the_agent_has_no_tool_that_touches_work_requests():
    from cycle_runner.agent import root_agent

    names = [getattr(tool, "__name__", None) or tool.name for tool in root_agent.tools]
    assert names == ["get_cycle_status", "get_issue", "recommend_next_work"]


# --- V0.9: the executor stands alone ------------------------------------------


@pytest.mark.parametrize("module", ["executor.py", "fake_executor.py", "work_requests.py", "claude_executor.py"])
def test_execution_modules_cannot_reach_adk_telegram_linear_or_the_conversation(module):
    imports = _imported_modules(PACKAGE / module)
    forbidden = (
        "google", "telegram", "httpx", "litellm",
        "cycle_runner.agent", "cycle_runner.gateway", "cycle_runner.telegram_adapter",
        "cycle_runner.linear_client", "cycle_runner.linear_tools",
        "cycle_runner.recommendation", "cycle_runner.approval",
    )
    assert not [name for name in imports if name.startswith(forbidden)], (module, imports)


def test_the_fake_executor_depends_only_on_the_executor_interface_and_the_work_request_model():
    imports = _imported_modules(PACKAGE / "fake_executor.py")
    assert imports == {"cycle_runner.executor", "cycle_runner.work_requests"}


def test_only_the_executor_cli_knows_which_executor_exists():
    users = [path.name for path in _modules() if "cycle_runner.fake_executor" in _imported_modules(path)]
    assert users == ["executor.py"]


def test_the_conversation_side_cannot_start_execution():
    users = [path.name for path in _modules() if "cycle_runner.executor" in _imported_modules(path)]
    # The executors implement the interface; nothing in the Telegram/ADK path imports it.
    assert sorted(users) == ["claude_executor.py", "fake_executor.py"]


def test_importing_the_package_does_not_load_adk():
    # cycle_runner/__init__.py must stay empty, or every "independent" module
    # would drag ADK in with it.
    assert (PACKAGE / "__init__.py").read_text().strip().startswith('"""')
    assert _imported_modules(PACKAGE / "__init__.py") == set()


# --- V1.0: the Claude executor ------------------------------------------------


def test_only_the_claude_executor_uses_the_agent_sdk():
    users = [path.name for path in _modules() if any(n.startswith("claude_agent_sdk") for n in _imported_modules(path))]
    assert users == ["claude_executor.py"]


def test_the_claude_executor_never_touches_lifecycle_state_or_starts_processes():
    # It executes; the runner claims and records. It doesn't open the store,
    # talk to SQLite, or shell out itself (the agent's Bash runs in the sandbox).
    imports = _imported_modules(PACKAGE / "claude_executor.py")
    assert "sqlite3" not in imports and "subprocess" not in imports
    source = (PACKAGE / "claude_executor.py").read_text()
    for forbidden in ["open_store", "WorkRequestStore", ".claim(", ".start(", ".finish(", "bypassPermissions"]:
        assert forbidden not in source, forbidden


def test_the_runner_does_not_know_about_claude():
    imports = _imported_modules(PACKAGE / "executor.py")
    assert not [n for n in imports if n.startswith(("claude_agent_sdk", "cycle_runner.claude_executor"))]

