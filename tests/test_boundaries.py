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
LINEAR_TRANSPORT_TERMS = ["api.linear.app", "oauth", "Authorization", "client_secret"]


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


def test_only_approval_can_reach_the_work_request_store():
    # The agent, the recommender, the tools, the gateway and Telegram can't
    # create (or read) work requests; only the deterministic approval code can.
    users = [
        path.name
        for path in _modules()
        if path.name != "work_requests.py"
        and any(name.startswith("cycle_runner.work_requests") for name in _imported_modules(path))
    ]
    assert users == ["approval.py"]


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
