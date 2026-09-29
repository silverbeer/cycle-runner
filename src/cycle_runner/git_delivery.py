"""Local Git delivery: a verified change becomes a branch and a commit in the workspace clone.

    changed ExecutionResult ─► LocalGitDelivery.deliver ─► cycle-runner/WR-000123 + one commit
                                                              (local only: the clone has no remote)

Cycle Runner makes the branch and the commit, never the coding agent: the
agent has no git, and its sandbox can't write .git. These git commands run
outside the sandbox, so they are privileged and kept to a small fixed set
(rev-parse, config --list, remote, status, check-ref-format, switch -c, add,
diff --cached, cat-file, commit, diff-tree, and reset/switch/branch -D to
undo a refused delivery). Nothing here takes a git command from configuration or issue text.

Every git call runs with the user's and the system's git configuration,
attributes and excludes off (and an empty temporary HOME), hooks off,
fsmonitor off, signing off and literal pathspecs. The clone's own
.git/config is audited first: anything but basic core and branch settings
(a remote, a url rewrite, an include, a filter, ...) refuses the delivery.

What gets committed is decided here, not by the agent's report:
- only files git reports as changed that the executor also saw the agent
  change (result.files_changed);
- never dependency, cache or tool directories, env files, keys or
  credentials, logs, databases, or Cycle Runner's own records, whatever
  .gitignore says;
- never symlinks, binaries or very large files;
- and nothing at all if an included file looks like it contains a secret,
  checked both on disk and as the blob git staged.
Everything else stays in the workspace, uncommitted, and is listed.

This module knows git and paths. It knows nothing about Claude, ADK,
Telegram or Linear, and never pushes: there is no remote to push to, and it
checks that before and after.
"""

import logging
import os
import re
import subprocess
import tempfile
import unicodedata
from pathlib import Path, PurePosixPath
from typing import Any

from cycle_runner.executor import Delivery, DeliveryError, ExecutionResult, ExecutionWorkspace
from cycle_runner.work_requests import WorkRequest

log = logging.getLogger(__name__)

BRANCH_PREFIX = "cycle-runner/"
WORK_REQUEST_ID = re.compile(r"^WR-\d{6,}$")
ISSUE_ID = re.compile(r"^[A-Z][A-Z0-9]*-\d+$")
AUTHOR_NAME, AUTHOR_EMAIL = "Cycle Runner", "cycle-runner@localhost.invalid"
TITLE_LIMIT = 72
MAX_FILE_BYTES = 1_000_000
GIT_TIMEOUT_SECONDS = 60

# Settings git reads on every call that could run a program or change what a
# command does. Forced off, whatever any configuration says.
HARDENING = (
    "core.hooksPath=/dev/null", "core.fsmonitor=false", "core.pager=cat", "core.quotePath=false",
    "core.attributesFile=/dev/null", "core.excludesFile=/dev/null",
    "commit.gpgSign=false", "tag.gpgSign=false", "diff.external=", "protocol.allow=never", "advice.detachedHead=false",
)
# The only .git/config keys a fresh clone (with origin removed) should have.
ALLOWED_CONFIG = re.compile(
    r"^(core\.(repositoryformatversion|filemode|bare|logallrefupdates|ignorecase|precomposeunicode|symlinks)"
    r"|branch\.[^.]+\.(merge|remote)|user\.(name|email)|extensions\.objectformat|init\.defaultbranch)$"
)

# Never committed, whatever .gitignore says (checked per path component).
PROTECTED_DIRS = {
    ".git", ".venv", "venv", ".python", "node_modules", "site-packages", "__pycache__", ".pytest_cache",
    ".mypy_cache", ".ruff_cache", ".tox", ".nox", ".cache", ".claude", ".gradle", ".idea", ".vscode",
}
PROTECTED_NAMES = re.compile(
    r"""(?ix)^(
        \.env(\..*)?                                      # .env, .env.local, .env.prod, ...
      | .*\.(pem|key|p12|pfx|jks|keystore|kdbx|gpg|asc)
      | id_(rsa|dsa|ecdsa|ed25519)(\.pub)?
      | \.netrc | \.npmrc | \.pypirc | \.pgpass | \.git-credentials | \.htpasswd
      | .*credential.* | .*secret.* | .*token.*\.(txt|json)
      | .*\.(log|pyc|pyo|sqlite|sqlite3|db|coverage) | \.coverage | \.DS_Store
      | WR-\d+\.json                                      # Cycle Runner's own records
    )$""",
)
# Looks like a live credential. One match in an included file refuses the delivery.
SECRET_CONTENT = re.compile(
    rb"-----BEGIN [A-Z ]*PRIVATE KEY-----"
    rb"|\bgh[pousr]_[A-Za-z0-9]{30,}|\bgithub_pat_[A-Za-z0-9_]{40,}"
    rb"|\bsk-ant-[A-Za-z0-9_-]{20,}|\bsk-[A-Za-z0-9]{32,}"
    rb"|\bAKIA[0-9A-Z]{16}\b|\bxox[abpr]-[A-Za-z0-9-]{10,}"
    rb"|\blin_(api|oauth)_[A-Za-z0-9]{20,}|\bops_[A-Za-z0-9_-]{40,}"
)
CREDENTIAL_NAME = re.compile(r"TOKEN|SECRET|KEY|PASSWORD|CREDENTIAL|AUTH|CLIENT_ID", re.IGNORECASE)


def branch_name(work_request_id: str) -> str:
    if not WORK_REQUEST_ID.match(work_request_id):
        raise DeliveryError(f"{work_request_id!r} is not a work request id")
    return BRANCH_PREFIX + work_request_id


def commit_message(request: WorkRequest) -> str:
    """From what was approved, sanitized and bounded. Never the issue description."""
    title = "".join(ch if ch.isprintable() else " " for ch in request.title_at_approval)
    title = " ".join(title.split()).lstrip("-#> ")
    subject_prefix = f"{request.issue_id}: " if ISSUE_ID.match(request.issue_id) else "Cycle Runner: "
    room = TITLE_LIMIT - len(subject_prefix)
    if len(title) > room:
        title = title[: room - 1].rstrip() + "…"
    subject = subject_prefix + (title or request.work_request_id)
    return (
        f"{subject}\n\n"
        f"Work request {request.work_request_id}, delivered by Cycle Runner.\n"
        "Local only and not yet reviewed: nothing has been pushed.\n"
    )


def _nfc(path: str) -> str:
    """One spelling per name: macOS git reports precomposed (NFC) names; files may be stored decomposed."""
    return unicodedata.normalize("NFC", path)


def protected_reason(path: str) -> str | None:
    """Why a workspace-relative path must never be committed, or None."""
    parts = PurePosixPath(path).parts
    if not parts or path.startswith("/") or ".." in parts:
        return "outside the workspace"
    for part in parts[:-1]:
        if part.lower() in PROTECTED_DIRS:
            return f"inside {part}/"
    if parts[-1].lower() in PROTECTED_DIRS:
        return f"{parts[-1]} is a protected directory"
    if PROTECTED_NAMES.match(parts[-1]):
        return "a protected file (secrets, environment, logs, caches or Cycle Runner records)"
    return None


class LocalGitDelivery:
    """Commits a changed result on a new local branch in the workspace clone."""

    def __init__(self, environ: dict[str, str] | None = None):
        # The runner's own credentials: an included file containing one refuses the delivery.
        environ = dict(os.environ if environ is None else environ)
        self._secrets = [value.encode() for name, value in environ.items()
                         if CREDENTIAL_NAME.search(name) and len(value) >= 12]
        self._path = environ.get("PATH", "/usr/bin:/bin")
        self._home: str | None = None

    def deliver(self, request: WorkRequest, workspace: ExecutionWorkspace, result: ExecutionResult) -> Delivery:
        """Any failure, of any kind, is a DeliveryError with the workspace put back as it was."""
        with tempfile.TemporaryDirectory(prefix="cycle-runner-git-") as home:
            # An empty HOME and XDG_CONFIG_HOME: found in review, git reads
            # $HOME/.config/git/attributes and .../ignore even with the global
            # config off, and HOME must not be anywhere the agent could write.
            self._home = home
            try:
                return self._deliver(request, workspace, result)
            except DeliveryError:
                raise
            except Exception as exc:  # a timeout, git missing, an unreadable file, ...
                raise DeliveryError(f"{type(exc).__name__}: {exc}") from None
            finally:
                self._home = None

    def _deliver(self, request: WorkRequest, workspace: ExecutionWorkspace, result: ExecutionResult) -> Delivery:
        root = workspace.path.resolve()
        branch = branch_name(request.work_request_id)
        git_dir = workspace.path / ".git"
        if git_dir.is_symlink() or not git_dir.is_dir():
            raise DeliveryError("the workspace's .git is not a plain directory")
        self._audit(root)
        if self._git(root, "check-ref-format", "--branch", branch, check=False).returncode != 0:
            raise DeliveryError(f"{branch!r} is not a valid branch name")
        if self._git(root, "rev-parse", "--verify", "--quiet", f"refs/heads/{branch}", check=False).returncode == 0:
            raise DeliveryError(f"branch {branch} already exists")
        base = self._git(root, "rev-parse", "HEAD").stdout.strip()
        original = self._git(root, "rev-parse", "--abbrev-ref", "HEAD").stdout.strip()

        include, left = self._select(root, {_nfc(path) for path in result.files_changed})
        if not include:
            raise DeliveryError("nothing committable: " + ("; ".join(left) or "no changed files"))

        self._git(root, "switch", "--quiet", "--create", branch)
        try:
            self._git(root, "add", "--all", "--", *sorted(include))
            diff = self._staged_diff(root)
            staged = {_nfc(entry["path"]) for entry in diff["files"]}
            if staged != {_nfc(path) for path in include}:
                raise DeliveryError(f"staged files {sorted(staged)} differ from the selected {sorted(include)}")
            # What git will commit, not what is on disk: found in review, attributes
            # (working-tree-encoding, filters, eol, ident) can transform content on add.
            for entry in diff["files"]:
                if entry["status"] != "deleted":
                    self._check_blob(root, entry["path"])
            self._git(root, "commit", "--quiet", "--no-verify", "--no-gpg-sign", "--file", "-",
                      input=commit_message(request))
            commit = self._git(root, "rev-parse", "HEAD").stdout.strip()
            committed = {_nfc(path) for path in self._git(
                root, "diff-tree", "--no-commit-id", "--name-only", "-r", "-z", "--no-renames", "HEAD",
            ).stdout.split("\0") if path}
            if self._git(root, "rev-parse", "HEAD^").stdout.strip() != base or committed != {_nfc(p) for p in include}:
                raise DeliveryError(f"the commit {commit[:12]} isn't exactly the selected change")
            self._audit(root)  # still no remote
        except BaseException:
            self._undo(root, base, original, branch)
            raise
        log.info("%s committed %s on %s", request.work_request_id, commit[:12], branch)
        return Delivery(branch=branch, commit_sha=commit, diff=diff, left_uncommitted=tuple(left))

    # --- checks -------------------------------------------------------------------

    def _audit(self, root: Path) -> None:
        """No remote, and nothing in .git/config beyond what a plain clone has."""
        if self._git(root, "remote").stdout.strip():
            raise DeliveryError("the workspace has a remote")
        listed = self._git(root, "config", "--local", "--name-only", "--list", "-z").stdout
        unexpected = sorted(key for key in listed.split("\0") if key and not ALLOWED_CONFIG.match(key.lower()))
        if unexpected:
            raise DeliveryError(f"unexpected git configuration in the workspace: {unexpected}")

    def _select(self, root: Path, attributed: set[str]) -> tuple[set[str], list[str]]:
        """Which of git's changed paths to commit, and why each other one isn't."""
        entries = self._git(root, "status", "--porcelain=v1", "-z", "--untracked-files=all", "--no-renames").stdout
        include: set[str] = set()
        left: list[str] = []
        seen: set[str] = set()
        for entry in filter(None, entries.split("\0")):
            code, path = entry[:2], entry[3:]  # git's own spelling, for git and the filesystem
            seen.add(_nfc(path))
            if code[0] not in " ?":
                raise DeliveryError(f"the index was changed outside Cycle Runner ({path})")
            reason = self._excluded(root, path, deleted=code[1] == "D", attributed=attributed)
            if reason:
                left.append(f"{path}: {reason}")
            else:
                include.add(path)
        for path in sorted(attributed - seen):  # both NFC
            left.append(f"{path}: ignored by the project's .gitignore")
        return include, left

    def _excluded(self, root: Path, path: str, *, deleted: bool, attributed: set[str]) -> str | None:
        reason = protected_reason(path)
        if reason:
            return reason
        if _nfc(path) not in attributed:
            return "not a change the executor saw the agent make (e.g. a test byproduct)"
        if deleted:
            return None
        file = root / path
        if file.is_symlink():
            return "a symlink"
        resolved = file.resolve()
        if root not in resolved.parents:
            return "outside the workspace"
        if not resolved.is_file():
            return "not a regular file"
        if resolved.stat().st_size > MAX_FILE_BYTES:
            return f"larger than {MAX_FILE_BYTES} bytes"
        content = resolved.read_bytes()
        if b"\0" in content[:8192]:
            return "a binary file"
        self._check_content(path, content)
        return None

    def _check_content(self, path: str, content: bytes) -> None:
        if SECRET_CONTENT.search(content) or any(secret in content for secret in self._secrets):
            raise DeliveryError(f"{path} looks like it contains a credential; nothing was committed")

    def _check_blob(self, root: Path, path: str) -> None:
        """The staged content of path: no secrets, not binary, not huge."""
        blob = self._git(root, "cat-file", "blob", f":{path}", binary=True).stdout
        if len(blob) > MAX_FILE_BYTES or b"\0" in blob[:8192]:
            raise DeliveryError(f"{path} is staged as binary or oversized content; nothing was committed")
        self._check_content(path, blob)

    def _staged_diff(self, root: Path) -> dict[str, Any]:
        """Machine-readable summary of what is about to be committed, from git itself."""
        statuses = self._git(root, "diff", "--cached", "--name-status", "-z", "--no-renames").stdout.split("\0")
        kinds = dict(zip(statuses[1::2], statuses[0::2]))
        files = []
        for line in filter(None, self._git(root, "diff", "--cached", "--numstat", "-z", "--no-renames").stdout.split("\0")):
            insertions, deletions, path = line.split("\t", 2)
            files.append({
                "path": path,
                "status": {"A": "added", "D": "deleted"}.get(kinds.get(path, "M"), "modified"),
                "insertions": int(insertions) if insertions.isdigit() else 0,
                "deletions": int(deletions) if deletions.isdigit() else 0,
            })
        return {
            "files": files,
            "files_changed": sorted(f["path"] for f in files if f["status"] == "modified"),
            "files_added": sorted(f["path"] for f in files if f["status"] == "added"),
            "files_deleted": sorted(f["path"] for f in files if f["status"] == "deleted"),
            "insertions": sum(f["insertions"] for f in files),
            "deletions": sum(f["deletions"] for f in files),
        }

    def _undo(self, root: Path, base: str, original: str, branch: str) -> None:
        """Back to where delivery started: no commit, nothing staged, the original branch, no
        new branch. The agent's changes stay in the working tree, for inspection."""
        for args in (("reset", "--quiet", "--soft", base), ("reset", "--quiet"), ("switch", "--quiet", original),
                     ("branch", "--quiet", "-D", branch)):
            try:
                self._git(root, *args, check=False)
            except Exception:  # best effort; the run is recorded as failed either way
                log.exception("undo step git %s failed", args[0])

    # --- the one way git is run ---------------------------------------------------

    def _git(self, root: Path, *args: str, input: str | None = None, check: bool = True,
             binary: bool = False) -> subprocess.CompletedProcess:
        if self._home is None:
            raise DeliveryError("git runs only inside deliver()")
        env = {
            "PATH": self._path, "HOME": self._home, "XDG_CONFIG_HOME": self._home, "LC_ALL": "C",
            "GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": os.devnull, "GIT_TERMINAL_PROMPT": "0",
            "GIT_LITERAL_PATHSPECS": "1", "GIT_OPTIONAL_LOCKS": "0", "GIT_EDITOR": "true",
            "GIT_AUTHOR_NAME": AUTHOR_NAME, "GIT_AUTHOR_EMAIL": AUTHOR_EMAIL,
            "GIT_COMMITTER_NAME": AUTHOR_NAME, "GIT_COMMITTER_EMAIL": AUTHOR_EMAIL,
        }
        hardening = [item for setting in HARDENING for item in ("-c", setting)]
        result = subprocess.run(
            ["git", *hardening, "-C", str(root), *args], env=env, input=input,
            capture_output=True, text=not binary, timeout=GIT_TIMEOUT_SECONDS,
        )
        if check and result.returncode != 0:
            stderr = result.stderr.decode(errors="replace") if binary else result.stderr
            detail = (stderr.strip().splitlines() or ["no output"])[-1]
            raise DeliveryError(f"git {args[0]} failed: {detail}")
        return result
