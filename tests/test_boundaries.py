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
        for word in ["sql", "graphql", "query", "execute", "request", "http"]:
            assert word not in tool.__name__.lower()
