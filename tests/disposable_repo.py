"""A tiny, throwaway Git repository for the coding-agent tests.

It lives in a pytest temp directory (outside the home directory, removed after
the test) and has nothing to do with any real project.
"""

import subprocess
import textwrap
from pathlib import Path

from cycle_runner.claude_executor import Workspace, python_for_tests

FILES = {
    "README.md": "# hello\n\nA tiny package used to test Cycle Runner's coding agent.\n",
    "src/__init__.py": "",
    "src/hello.py": textwrap.dedent(
        '''
        """A tiny module for the coding agent to extend."""


        def shout(text: str) -> str:
            return text.upper() + "!"
        '''
    ).lstrip(),
    "tests/__init__.py": "",
    "tests/test_hello.py": textwrap.dedent(
        """
        import unittest

        from src.hello import shout


        class ShoutTest(unittest.TestCase):
            def test_shout(self):
                self.assertEqual(shout("hi"), "HI!")
        """
    ).lstrip(),
}


def make_repo(path: Path, extra_files: dict[str, str] | None = None) -> Workspace:
    """Create the repository at `path`, commit it, and describe it as a Workspace."""
    path.mkdir(parents=True)
    for name, content in {**FILES, **(extra_files or {})}.items():
        (path / name).parent.mkdir(parents=True, exist_ok=True)
        (path / name).write_text(content)
    for command in (
        ["git", "init", "-q", "-b", "main"],
        ["git", "config", "user.email", "tests@example.invalid"],
        ["git", "config", "user.name", "Cycle Runner tests"],
        ["git", "add", "-A"],
        ["git", "commit", "-q", "-m", "initial"],
    ):
        subprocess.run(command, cwd=path, check=True, capture_output=True)
    python = python_for_tests()
    return Workspace(
        path=path,
        test_command=f"{python} -m unittest discover -s tests -v",
        readable=(python.parent.parent,),  # the interpreter's install, so sandboxed tests can start
    )


def run_tests(workspace: Workspace) -> subprocess.CompletedProcess:
    """Run the workspace's tests from the test harness (not the agent)."""
    return subprocess.run(workspace.test_command.split(), cwd=workspace.path, capture_output=True, text=True)


def changed_files(workspace: Workspace) -> list[str]:
    result = subprocess.run(
        ["git", "status", "--porcelain", "--untracked-files=all"], cwd=workspace.path,
        capture_output=True, text=True, check=True,
    )
    return sorted(line[3:] for line in result.stdout.splitlines())
