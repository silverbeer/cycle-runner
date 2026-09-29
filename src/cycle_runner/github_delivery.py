"""GitHub delivery: push one human-approved commit and open a draft PR. Nothing else.

    DeliveryApproval (approved, commit abc…) ──► verify the local commit again (exact SHA)
        ──► check the repository (from projects.toml, not the clone) and its base on GitHub
        ──► git push <url> abc…:refs/heads/cycle-runner/WR-… (no remote is added)
        ──► confirm the remote branch is at abc… ──► draft PR ──► approval: pushed, pr_created

The only code in Cycle Runner that can write to GitHub. It runs from the
`deliver` command, which alone is given the GitHub token
(CYCLE_RUNNER_GITHUB_TOKEN); the coding agent never runs in that process and
its sandbox strips every *TOKEN* variable anyway.

What it will push is fixed by the approval, not by the workspace:
- the approval's exact SHA, re-verified locally right before pushing. If the
  branch, HEAD, files or anything else moved, the approval is invalidated
  (a new one is needed) and nothing is pushed;
- to the repository named in projects.toml, which must equal the one
  recorded in the approval;
- as one ref: <sha>:refs/heads/<approved branch>. No tags, no other refs,
  never forced. The approved commit's base must already be on the GitHub base
  branch, so the push adds exactly one commit.

Retrying is safe: a branch already at the approved SHA isn't pushed again,
and an existing PR for the branch at that SHA is reused. A branch at any
other commit, or a PR for another commit, stops delivery.

A failed push or PR is a delivery failure recorded on the approval
(last_error); the work request stays completed.

The token goes to git as an in-memory http.extraHeader through the
GIT_CONFIG_* environment (not argv, not a file, not a remote), and to the
API in a header. It is never logged and is scrubbed from error text.
"""

import base64
import logging
import re
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx

from cycle_runner.executor import DeliveryError
from cycle_runner.git_delivery import CommitVerifier
from cycle_runner.projects import ProjectsConfig
from cycle_runner.work_requests import DeliveryApproval, InvalidTransition, WorkRequestStore

log = logging.getLogger(__name__)

API_URL = "https://api.github.com"
TOKEN_VARIABLE = "CYCLE_RUNNER_GITHUB_TOKEN"
TIMEOUT_SECONDS = 30
BODY_SUMMARY_LIMIT = 1500


class GitHubDeliveryError(Exception):
    """Delivery stopped. step says where: repository, remote, push, pr or local."""

    def __init__(self, step: str, message: str):
        super().__init__(f"{step}: {message}")
        self.step = step


@dataclass(frozen=True)
class PullRequest:
    number: int
    url: str
    head_sha: str
    draft: bool
    state: str = "open"
    base: str = ""


class GitHubApi:
    """The five GitHub REST calls delivery needs, and nothing else."""

    def __init__(self, repository: str, token: str, transport: httpx.BaseTransport | None = None):
        self.repository = repository
        self._token = token
        self._client = httpx.Client(
            base_url=API_URL, transport=transport, timeout=TIMEOUT_SECONDS,
            headers={"Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json",
                     "X-GitHub-Api-Version": "2022-11-28", "User-Agent": "cycle-runner"},
        )

    def __repr__(self) -> str:
        return f"GitHubApi({self.repository!r})"

    def check_repository(self) -> None:
        """The repository exists, is the one configured, isn't archived, and this token may push."""
        body = self._get(f"/repos/{self.repository}", step="repository")
        if str(body.get("full_name", "")).lower() != self.repository.lower():
            raise GitHubDeliveryError("repository", f"GitHub answered for {body.get('full_name')!r}")
        if body.get("archived"):
            raise GitHubDeliveryError("repository", f"{self.repository} is archived")
        if not (body.get("permissions") or {}).get("push"):
            raise GitHubDeliveryError("repository", f"the token can't push to {self.repository}")

    def branch_sha(self, branch: str) -> str | None:
        """Where the branch points on GitHub, or None if it doesn't exist."""
        body = self._get(f"/repos/{self.repository}/git/ref/heads/{branch}", step="remote", missing_ok=True)
        if body is None:
            return None
        if not isinstance(body, dict) or (body.get("object") or {}).get("type") != "commit":
            raise GitHubDeliveryError("remote", f"refs/heads/{branch} isn't a single branch on GitHub")
        return body["object"]["sha"]

    def contains(self, base_branch: str, sha: str) -> bool:
        """Whether sha is already on base_branch (so pushing a child of it adds only that child)."""
        body = self._get(f"/repos/{self.repository}/compare/{base_branch}...{sha}", step="remote", missing_ok=True)
        return body is not None and body.get("status") in ("behind", "identical")

    def find_pull(self, branch: str) -> PullRequest | None:
        owner = self.repository.split("/")[0]
        pulls = self._get(f"/repos/{self.repository}/pulls", step="pr",
                          params={"head": f"{owner}:{branch}", "state": "all", "per_page": 10})
        if len(pulls) > 1:
            raise GitHubDeliveryError("pr", f"{len(pulls)} pull requests already use {branch}")
        return _pull(pulls[0]) if pulls else None

    def create_draft_pull(self, *, branch: str, base: str, title: str, body: str) -> PullRequest:
        response = self._request("POST", f"/repos/{self.repository}/pulls", step="pr", json={
            "title": title, "head": branch, "base": base, "body": body, "draft": True,
            "maintainer_can_modify": False,
        })
        return _pull(response)

    def _get(self, path: str, *, step: str, missing_ok: bool = False, params: dict | None = None) -> Any:
        return self._request("GET", path, step=step, missing_ok=missing_ok, params=params)

    def _request(self, method: str, path: str, *, step: str, missing_ok: bool = False, **kwargs) -> Any:
        try:
            response = self._client.request(method, path, **kwargs)
        except httpx.HTTPError as exc:
            raise GitHubDeliveryError(step, f"GitHub unreachable: {type(exc).__name__}") from None
        if response.status_code == 404 and missing_ok:
            return None
        if response.status_code in (401, 403):
            raise GitHubDeliveryError(step, f"GitHub refused the credential ({response.status_code}: "
                                            f"{_scrub(_message(response), self._token)})")
        if response.status_code >= 400:
            raise GitHubDeliveryError(step, f"GitHub answered {response.status_code} to {method} {path}: "
                                            f"{_scrub(_message(response), self._token)}")
        return response.json()


class GitHubDelivery:
    """Delivers one work request's live approval: push (once), then a draft PR (once)."""

    def __init__(self, store: WorkRequestStore, config: ProjectsConfig, *, token: str,
                 api: GitHubApi | None = None, verifier: CommitVerifier | None = None,
                 push_url: str | None = None, push_protocol: str = "https"):
        if not token:
            raise GitHubDeliveryError("repository", f"{TOKEN_VARIABLE} isn't set")
        self.store, self.config = store, config
        self._token = token
        self._api = api
        self._pusher = _Pusher(verifier)
        # Tests point these at a local bare repository; in use they're GitHub's.
        self._push_url, self._push_protocol = push_url, push_protocol

    def deliver(self, work_request_id: str) -> DeliveryApproval:
        approval = self.store.live_approval(work_request_id)
        if approval is None:
            raise GitHubDeliveryError("local", f"{work_request_id} has no approval to deliver; review and approve it")
        if approval.status == "pr_created":
            return approval  # already delivered
        try:
            return self._deliver(approval)
        except GitHubDeliveryError as exc:
            if exc.step != "local":  # a local mismatch already invalidated the approval
                self.store.record_delivery_error(approval.approval_id, _scrub(str(exc), self._token))
            raise

    def _deliver(self, approval: DeliveryApproval) -> DeliveryApproval:
        project = self.config.projects.get(approval.project_id)
        if project is None or project.github != approval.repository or project.branch != approval.evidence.get(
                "base_branch"):
            raise GitHubDeliveryError("repository", f"projects.toml no longer delivers {approval.project_id} to "
                                                    f"{approval.repository}; nothing pushed")
        self._verify_local(approval, project.branch)
        api = self._api or GitHubApi(approval.repository, self._token)
        api.check_repository()
        branch, sha = approval.branch, approval.commit_sha

        if approval.status == "approved":
            remote = api.branch_sha(branch)
            if remote is None:
                if not api.contains(project.branch, approval.base_sha):
                    raise GitHubDeliveryError("remote", f"the approved commit's base {approval.base_sha[:12]} isn't on "
                                                        f"{approval.repository}'s {project.branch}; nothing pushed")
                self._pusher.push(self.config.workspace_root / approval.work_request_id, url=self._url(approval),
                                  sha=sha, branch=branch, token=self._token, protocol=self._push_protocol,
                                  tree_sha=approval.evidence.get("tree_sha", ""), base_sha=approval.base_sha)
            elif remote != sha:
                raise GitHubDeliveryError("remote", f"{branch} already exists on GitHub at {remote[:12]}, not the "
                                                    f"approved {sha[:12]}; nothing pushed")
            confirmed = api.branch_sha(branch)
            if confirmed != sha:
                raise GitHubDeliveryError("push", f"after pushing, GitHub has {branch} at "
                                                  f"{(confirmed or 'nothing')[:12]}, not {sha[:12]}")
            approval = self.store.mark_pushed(approval.approval_id)
            log.info("%s pushed %s to %s:%s", approval.approval_id, sha[:12], approval.repository, branch)
        else:  # pushed earlier; the PR step failed. Never push again, but check GitHub still agrees.
            remote = api.branch_sha(branch)
            if remote != sha:
                raise GitHubDeliveryError("remote", f"{branch} on GitHub is at {(remote or 'nothing')[:12]}, not "
                                                    f"the approved {sha[:12]}")

        pull = api.find_pull(branch)
        if pull is not None and (pull.state != "open" or pull.base != project.branch or not pull.draft):
            # Found in review: a closed, merged, retargeted or ready PR must not count as delivered.
            raise GitHubDeliveryError("pr", f"PR #{pull.number} for {branch} is {pull.state}, "
                                            f"{'a draft' if pull.draft else 'not a draft'}, against {pull.base}; "
                                            "not reusing it")
        if pull is None:
            try:
                pull = api.create_draft_pull(branch=branch, base=project.branch, title=pr_title(approval),
                                             body=pr_body(approval))
            except GitHubDeliveryError as exc:
                raise GitHubDeliveryError("pr", f"pushed, but creating the draft PR failed ({exc}); "
                                                "run deliver again to retry the PR only") from None
            if not pull.draft:
                raise GitHubDeliveryError("pr", f"GitHub created PR #{pull.number} as ready for review, not a draft")
        if pull.head_sha != sha:
            raise GitHubDeliveryError("pr", f"PR #{pull.number} for {branch} is at {pull.head_sha[:12]}, not {sha[:12]}")
        approval = self.store.mark_pr_created(approval.approval_id, pr_number=pull.number, pr_url=pull.url)
        log.info("%s draft PR %s", approval.approval_id, pull.url)
        return approval

    def _verify_local(self, approval: DeliveryApproval, base_branch: str) -> None:
        """The approved SHA must still be exactly what's in the workspace. If not, the approval dies."""
        try:
            verified = self._pusher.verify(
                self.config.workspace_root / approval.work_request_id, work_request_id=approval.work_request_id,
                commit_sha=approval.commit_sha, base_branch=base_branch,
                recorded_diff=approval.evidence.get("diff"),
            )
            if (verified.base_sha, verified.tree_sha) != (approval.base_sha, approval.evidence.get("tree_sha")):
                raise DeliveryError("the commit's base or content no longer matches the approval")
        except DeliveryError as exc:
            try:
                self.store.invalidate_approval(approval.approval_id, f"at delivery: {exc}")
            except InvalidTransition:
                pass
            raise GitHubDeliveryError("local", f"{exc}. {approval.approval_id} is now invalid; nothing pushed. "
                                               "Review and approve again.") from None

    def _url(self, approval: DeliveryApproval) -> str:
        return self._push_url or f"https://github.com/{approval.repository}.git"


class _Pusher(CommitVerifier):
    """The one push: the approved SHA to the approved branch, by URL, with an in-memory credential.

    Found in review: pushing from the workspace re-read its .git/config after
    the audit (a pushInsteadOf could redirect the push, a proxy could see the
    token). So the approved commit is first fetched into a fresh bare
    repository Cycle Runner owns, checked there again (SHA, tree, parent), and
    pushed from there. The workspace's configuration never takes part.
    """

    def __init__(self, verifier: CommitVerifier | None):
        super().__init__()
        if verifier is not None:
            self.__dict__.update(verifier.__dict__)

    def push(self, workspace, *, url: str, sha: str, branch: str, token: str, protocol: str,
             tree_sha: str, base_sha: str) -> None:
        if not re.fullmatch(r"[0-9a-f]{40}", sha) or not branch.startswith("cycle-runner/WR-"):
            raise GitHubDeliveryError("push", "refusing an unapproved ref")
        credential = base64.b64encode(f"x-access-token:{token}".encode()).decode()
        env = {"GIT_CONFIG_COUNT": "1", "GIT_CONFIG_KEY_0": "http.https://github.com/.extraheader",
               "GIT_CONFIG_VALUE_0": f"AUTHORIZATION: basic {credential}"}
        try:
            with self._session(), tempfile.TemporaryDirectory(prefix="cycle-runner-push-") as scratch:
                clean = Path(scratch) / "approved.git"
                self._git(Path(scratch), "init", "--quiet", "--bare", str(clean))
                self._git(clean, "fetch", "--quiet", "--no-tags", "--no-write-fetch-head", str(workspace.resolve()),
                          f"refs/heads/{branch}:refs/heads/{branch}", extra_config=("protocol.file.allow=always",))
                fetched = self._git(clean, "rev-parse", f"refs/heads/{branch}").stdout.strip()
                tree = self._git(clean, "rev-parse", f"{sha}^{{tree}}", check=False).stdout.strip()
                parents = self._git(clean, "rev-list", "--parents", "-n", "1", sha, check=False).stdout.split()[1:]
                if (fetched, tree, parents) != (sha, tree_sha, [base_sha]):
                    raise DeliveryError(f"the fetched commit ({fetched[:12]}) isn't the approved one")
                self._git(clean, "push", "--porcelain", "--no-verify", "--no-follow-tags", url,
                          f"{sha}:refs/heads/{branch}", extra_env=env,
                          extra_config=(f"protocol.{protocol}.allow=always",))
        except DeliveryError as exc:
            raise GitHubDeliveryError("push", _scrub(str(exc), token, credential)) from None


def pr_title(approval: DeliveryApproval) -> str:
    return (approval.evidence.get("message") or approval.branch).splitlines()[0][:120]


def pr_body(approval: DeliveryApproval) -> str:
    """For a human reviewer: what, why, which tests were seen, and where it came from. No claims beyond evidence."""
    evidence = approval.evidence
    diff = evidence.get("diff") or {}
    tests = evidence.get("tests") or {}
    if tests.get("observed_runs"):
        last = _plain(tests.get("last_result") or "not captured")
        tests_line = (f"The test command ran {tests['observed_runs']} time(s) inside Cycle Runner's sandbox; the last "
                      f"run's result: `{last}`.")
    else:
        tests_line = "No test run was observed. Treat this change as untested."
    files = "\n".join(f"- `{_plain(f['path'])}` ({f['status']}, +{f['insertions']} -{f['deletions']})"
                      for f in diff.get("files", [])) or "- (none)"
    summary = _neutral(evidence.get("summary") or "(no summary)")[:BODY_SUMMARY_LIMIT]
    return (
        "> [!NOTE]\n"
        "> Draft opened by **Cycle Runner** after a human approved this exact commit. Review before marking ready.\n\n"
        f"**Issue:** {_neutral(evidence.get('issue_id', ''))} ({_neutral(evidence.get('title', ''))})\n"
        f"**Project:** {_neutral(evidence.get('project_id', ''))}\n"
        f"**Work request:** {approval.work_request_id}\n"
        f"**Commit:** `{approval.commit_sha}` on top of `{approval.base_sha[:12]}`\n"
        f"**Approved:** {approval.approval_id} by {_neutral(approval.approved_by)} at {approval.approved_at:%Y-%m-%d %H:%M} UTC\n\n"
        "### What changed\n\n"
        f"{files}\n\n+{diff.get('insertions', 0)} / -{diff.get('deletions', 0)} lines.\n\n"
        "### Tests\n\n"
        f"{tests_line}\n\n"
        "### The coding agent's summary (its own words, not verified)\n\n"
        + "\n".join(f"> {line}" for line in summary.splitlines())
        + "\n"
    )


def _neutral(text: str) -> str:
    """No @mentions and no issue-closing references from agent or issue text."""
    text = re.sub(r"@(?=\w)", "@\u200b", str(text))
    return re.sub(r"(?i)\b(close[sd]?|fix(e[sd])?|resolve[sd]?)(\s*:?\s*)(#|\w[\w.-]*/[\w.-]+#|https?://)",
                  lambda m: f"{m[1]}{m[3]}\u200b{m[4]}", text)


def _plain(text: str) -> str:
    """For inside a code span: one line, no backticks or angle brackets (found in review)."""
    text = "".join(ch if ch.isprintable() else " " for ch in str(text))
    return _neutral(text.replace("`", "'").replace("<", "\u2039").replace(">", "\u203a"))[:300]


def _pull(body: dict[str, Any]) -> PullRequest:
    return PullRequest(number=int(body["number"]), url=str(body["html_url"]),
                       head_sha=str((body.get("head") or {}).get("sha", "")), draft=bool(body.get("draft")),
                       state="merged" if body.get("merged_at") else str(body.get("state", "")),
                       base=str((body.get("base") or {}).get("ref", "")))


def _message(response: httpx.Response) -> str:
    try:
        return str(response.json().get("message", ""))[:300]
    except ValueError:
        return response.text[:300]


def _scrub(text: str, *secrets: str) -> str:
    for secret in secrets:
        if secret:
            text = text.replace(secret, "***")
    return text
