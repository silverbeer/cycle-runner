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


def test_only_the_linear_client_and_github_delivery_make_http_calls():
    users = [path.name for path in _modules() if "httpx" in _imported_modules(path)]
    assert users == ["github_delivery.py", "linear_client.py"]


def test_only_the_linear_client_knows_how_linear_is_reached():
    leaks = {
        path.name: [term for term in LINEAR_TRANSPORT_TERMS if term.lower() in path.read_text().lower()]
        for path in _modules()
        if path.name != "linear_client.py"
    }
    leaks["github_delivery.py"] = [t for t in leaks.get("github_delivery.py", []) if t != "Authorization"]  # GitHub's
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
    assert sorted(users) == ["approval.py", "delivery_approval.py", "executor.py", "git_delivery.py",
                             "github_delivery.py", "issue_context.py", "projects.py"]


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


@pytest.mark.parametrize(
    "module", ["executor.py", "fake_executor.py", "work_requests.py", "claude_executor.py", "projects.py",
               "git_delivery.py"]
)
def test_execution_modules_cannot_reach_adk_telegram_linear_or_the_conversation(module):
    imports = _imported_modules(PACKAGE / module)
    forbidden = (
        "google", "telegram", "httpx", "litellm",
        "cycle_runner.agent", "cycle_runner.gateway", "cycle_runner.telegram_adapter",
        "cycle_runner.linear_client", "cycle_runner.linear_tools",
        "cycle_runner.recommendation", "cycle_runner.approval",
    )
    assert not [name for name in imports if name.startswith(forbidden)], (module, imports)


def test_the_fake_executor_depends_only_on_the_executor_interface():
    imports = _imported_modules(PACKAGE / "fake_executor.py")
    assert imports == {"cycle_runner.executor"}


def test_only_the_executor_cli_knows_which_executor_exists():
    users = [path.name for path in _modules() if "cycle_runner.fake_executor" in _imported_modules(path)]
    assert users == ["executor.py"]


def test_the_conversation_side_cannot_start_execution():
    users = [path.name for path in _modules() if "cycle_runner.executor" in _imported_modules(path)]
    # The executors implement the interface and the resolver produces its
    # workspaces; nothing in the Telegram/ADK path imports it.
    assert sorted(users) == ["claude_executor.py", "delivery_approval.py", "fake_executor.py", "git_delivery.py",
                             "github_delivery.py", "issue_context.py", "projects.py"]


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


def _runner_library_imports() -> set[str]:
    """What the runner itself imports; the command line below it picks the implementations."""
    source = (PACKAGE / "executor.py").read_text()
    library, _, _ = source.partition("# --- command line")
    return _imported_modules_in(library)


def _imported_modules_in(source: str) -> set[str]:
    names = set()
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Import):
            names.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            names.add(node.module or "")
    return names


def test_the_runner_does_not_know_about_claude_or_linear():
    imports = _runner_library_imports()
    forbidden = ("claude_agent_sdk", "cycle_runner.claude_executor", "cycle_runner.issue_context",
                 "cycle_runner.linear_client", "cycle_runner.git_delivery", "subprocess")
    assert not [n for n in imports if n.startswith(forbidden)], imports


def test_executors_get_the_task_not_linear():
    for module in ("claude_executor.py", "fake_executor.py"):
        imports = _imported_modules(PACKAGE / module)
        assert not [n for n in imports if n.startswith(
            ("cycle_runner.issue_context", "cycle_runner.linear_client", "cycle_runner.work_requests", "httpx")
        )], (module, imports)


def test_the_issue_context_only_reads():
    imports = _imported_modules(PACKAGE / "issue_context.py")
    assert imports == {"asyncio", "cycle_runner.executor", "cycle_runner.linear_client", "cycle_runner.work_requests"}
    source = (PACKAGE / "issue_context.py").read_text()
    for forbidden in ("mutation", "open_store", "WorkRequestStore", ".claim(", ".finish("):
        assert forbidden not in source, forbidden


# --- V1.1: projects live in configuration, not code ---------------------------


PROJECT_IDS = {"MT", "TRD", "BET", "JT", "MTA", "QB", "DEMO"}


def test_no_project_is_named_in_the_code():
    # Adding a project must be a configuration change. Any string constant that
    # is exactly a project id (e.g. `if project == "MT"`) would mean project
    # logic in Python. Prose in docstrings doesn't count.
    found = {
        path.name: sorted(
            node.value for node in ast.walk(ast.parse(path.read_text()))
            if isinstance(node, ast.Constant) and node.value in PROJECT_IDS
        )
        for path in _modules()
    }
    assert {name: ids for name, ids in found.items() if ids} == {}


def test_the_claude_executor_knows_nothing_about_project_configuration():
    imports = _imported_modules(PACKAGE / "claude_executor.py")
    assert "cycle_runner.projects" not in imports and "tomllib" not in imports
    source = (PACKAGE / "claude_executor.py").read_text()
    for word in ("projects.toml", "load_projects", "WorkspaceResolver", "project_id"):
        assert word not in source, word


def test_the_runner_resolves_workspaces_through_the_interface_only():
    # executor.py's library code depends on the WorkspaceResolver protocol; only
    # its CLI entry point picks the configuration-backed resolver.
    source = (PACKAGE / "executor.py").read_text()
    library, _, cli = source.partition("# --- command line")
    assert "cycle_runner.projects" not in library and "cycle_runner.projects" in cli


def test_only_the_resolver_reads_project_configuration():
    users = [path.name for path in _modules() if "tomllib" in _imported_modules(path)]
    assert users == ["projects.py"]



# --- V1.3: local git delivery ------------------------------------------------------

GIT_OPERATIONS = {"rev-parse", "config", "remote", "status", "check-ref-format", "switch", "add", "diff",
                  "commit", "diff-tree", "reset", "branch", "cat-file", "symbolic-ref", "rev-list", "log"}


def _git_operations(module: str) -> set[str]:
    """The first argument after the workspace of every self._git(root, ...) call."""
    ops = set()
    for node in ast.walk(ast.parse((PACKAGE / module).read_text())):
        if (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr == "_git"
                and len(node.args) >= 2):
            first = node.args[1]
            assert isinstance(first, (ast.Constant, ast.Starred)), ast.dump(first)
            if isinstance(first, ast.Constant):
                ops.add(first.value)
    return ops


def test_delivery_uses_only_a_fixed_set_of_git_operations():
    assert _git_operations("git_delivery.py") <= GIT_OPERATIONS
    constants = {node.value for node in ast.walk(ast.parse((PACKAGE / "git_delivery.py").read_text()))
                 if isinstance(node, ast.Constant) and isinstance(node.value, str)}
    assert not constants & {"push", "fetch", "pull", "clone", "--force", "--mirror", "ls-remote", "send-email"}


def test_only_projects_and_delivery_start_processes():
    users = [path.name for path in _modules() if "subprocess" in _imported_modules(path)]
    assert sorted(users) == ["git_delivery.py", "projects.py"]


def test_delivery_knows_nothing_about_claude_linear_or_the_conversation():
    imports = _imported_modules(PACKAGE / "git_delivery.py")
    assert imports == {"logging", "os", "re", "subprocess", "tempfile", "unicodedata", "pathlib", "typing", "contextlib", "dataclasses", "cycle_runner.executor",
                       "cycle_runner.work_requests"}


def test_the_claude_executor_cannot_deliver():
    imports = _imported_modules(PACKAGE / "claude_executor.py")
    assert "cycle_runner.git_delivery" not in imports and "subprocess" not in imports


# --- V1.4: human-approved GitHub delivery --------------------------------------------------


def test_only_github_delivery_can_push_or_talk_to_github():
    for path in _modules():
        source = path.read_text()
        if path.name == "github_delivery.py":
            continue
        assert "api.github.com" not in source and "github.com/{" not in source, path.name
        assert '"push"' not in source, path.name
    assert _git_operations("github_delivery.py") == {"push"}


def test_github_delivery_knows_nothing_about_claude_linear_or_the_conversation():
    imports = _imported_modules(PACKAGE / "github_delivery.py")
    assert imports == {"base64", "logging", "re", "dataclasses", "typing", "httpx", "cycle_runner.executor",
                       "cycle_runner.git_delivery", "cycle_runner.projects", "cycle_runner.work_requests"}


def test_nothing_that_runs_or_commits_the_agents_work_can_reach_github():
    for module in ("claude_executor.py", "git_delivery.py", "issue_context.py", "projects.py", "fake_executor.py",
                   "delivery_approval.py", "approval.py", "agent.py", "telegram_adapter.py", "gateway.py"):
        imports = _imported_modules(PACKAGE / module)
        assert "cycle_runner.github_delivery" not in imports, module
    source = (PACKAGE / "executor.py").read_text()
    library, _, cli = source.partition("# --- command line")
    assert "github_delivery" not in library and "github_delivery" in cli


def test_approval_is_local_only():
    imports = _imported_modules(PACKAGE / "delivery_approval.py")
    assert not [n for n in imports if n.startswith(("httpx", "cycle_runner.github_delivery", "claude_agent_sdk",
                                                    "cycle_runner.linear", "telegram", "subprocess"))], imports
