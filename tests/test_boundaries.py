"""The gateway and Telegram adapter are meant to be reusable with other agents.

These tests read their source and fail if Cycle Runner business logic leaks in.
"""

import ast
from pathlib import Path

import pytest

import cycle_runner

PACKAGE = Path(cycle_runner.__file__).parent
GENERIC_MODULES = ["gateway.py", "telegram_adapter.py"]
BUSINESS_TERMS = ["get_cycle_status", "root_agent", "Scrum", "Product Owner", "Linear"]


def _imported_modules(path: Path) -> set[str]:
    tree = ast.parse(path.read_text())
    names = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            names.add(node.module or "")
    return names


@pytest.mark.parametrize("module", GENERIC_MODULES)
def test_generic_modules_do_not_import_the_agent(module):
    imports = _imported_modules(PACKAGE / module)
    assert not any(name.startswith("cycle_runner") for name in imports), imports


@pytest.mark.parametrize("module", GENERIC_MODULES)
def test_generic_modules_do_not_mention_cycle_runner_business_logic(module):
    source = (PACKAGE / module).read_text()
    assert [term for term in BUSINESS_TERMS if term in source] == []


def test_telegram_adapter_does_not_depend_on_adk():
    imports = _imported_modules(PACKAGE / "telegram_adapter.py")
    assert not any(name.startswith("google") for name in imports), imports
