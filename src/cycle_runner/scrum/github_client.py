"""Read-only GitHub API client for the Scrum Master report.

Read-only in three layers:
1. The token is meant to be a fine-grained PAT with read permissions only
   (`scrum-master doctor` warns about a classic token with write scopes).
   Without one, the token `gh` is logged in with is used, which usually can
   write; layers 2 and 3 are then the guarantee.
2. The only methods are get() (HTTP GET) and query() (GraphQL), and query()
   rejects any document that isn't a query.
3. Nothing above this module is given a way to send anything else.

This is deliberately not the delivery token (CYCLE_RUNNER_GITHUB_TOKEN), which
can push. The token is never logged, never in an exception message, and hidden
from repr(). Transient failures (connection errors, 5xx, 429 and secondary rate
limits) are retried a bounded number of times.
"""

import asyncio
import os
import re
import shutil
import subprocess
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field

import httpx

API_URL = "https://api.github.com"
GRAPHQL_URL = f"{API_URL}/graphql"
ATTEMPTS = 3
BACKOFF_SECONDS = 2.0
MAX_WAIT_SECONDS = 60.0
WRITE_SCOPES = {"repo", "public_repo", "write:org", "admin:org", "workflow", "delete_repo"}


class GitHubError(Exception):
    """GitHub could not be reached or refused the request. Safe to show: no credentials."""


@dataclass
class GitHubClient:
    token: str = field(repr=False)
    source: str = "token"  # where the token came from, for messages; never the token itself
    transport: httpx.AsyncBaseTransport | None = field(default=None, repr=False)
    sleep: Callable[[float], Awaitable[None]] = field(default=asyncio.sleep, repr=False)
    requests_made: int = field(default=0, init=False)

    @classmethod
    def from_env(cls, name: str, environ: Mapping[str, str] = os.environ) -> "GitHubClient":
        """The token in `name`, or else the one `gh` is logged in with."""
        token = environ.get(name, "").strip()
        if token.startswith("op://"):
            raise GitHubError(f"{name} is an unresolved 1Password reference; run under `op run --env-file .env.scrum`")
        if token:
            return cls(token=token, source=name)
        token = gh_cli_token()
        if not token:
            raise GitHubError(f"{name} is not set and `gh auth token` gave none")
        return cls(token=token, source="`gh auth token`")

    async def get(self, path: str, params: dict | None = None) -> httpx.Response:
        """GET a REST path such as /repos/o/r. Returns the response; 404 is the caller's to interpret."""
        if not path.startswith("/"):
            raise GitHubError("GitHub paths must start with /")
        response = await self._send("GET", f"{API_URL}{path}", params=params)
        if response.status_code >= 400 and response.status_code != 404:
            raise GitHubError(f"GitHub returned HTTP {response.status_code} for GET {_safe_path(path)}")
        return response

    async def query(self, document: str, variables: dict | None = None) -> dict:
        """Run a read-only GraphQL query and return its "data"."""
        _require_read_only(document)
        # GraphQL reads are POSTs by protocol; this is the only non-GET request this client sends.
        response = await self._send("POST", GRAPHQL_URL, json={"query": document, "variables": variables or {}})
        if response.status_code >= 400:
            raise GitHubError(f"GitHub GraphQL returned HTTP {response.status_code}")
        body = response.json()
        if body.get("errors"):
            raise GitHubError(body["errors"][0].get("message") or "GitHub GraphQL error")
        return body["data"]

    async def _send(self, method: str, url: str, **kwargs) -> httpx.Response:
        headers = {
            "Authorization": f"Bearer {self.token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        }
        for attempt in range(1, ATTEMPTS + 1):
            last = attempt == ATTEMPTS
            self.requests_made += 1
            try:
                async with httpx.AsyncClient(transport=self.transport, timeout=20) as http:
                    response = await http.request(method, url, headers=headers, **kwargs)
            except httpx.HTTPError as exc:
                if last:
                    raise GitHubError(f"could not reach GitHub ({type(exc).__name__})") from None
                await self.sleep(_backoff(attempt, None))
                continue
            if response.status_code == 401:
                raise GitHubError("GitHub rejected the token (HTTP 401)")
            if _is_transient(response) and not last:
                await self.sleep(_backoff(attempt, response))
                continue
            return response
        raise AssertionError("unreachable")


_OPERATION = re.compile(r"\b(mutation|subscription)\b")


def _require_read_only(document: str) -> None:
    without_comments = re.sub(r"#[^\n]*", "", document).strip()
    if not (without_comments.startswith("{") or without_comments.startswith("query")):
        raise GitHubError("only GraphQL queries are allowed")
    if _OPERATION.search(without_comments):
        raise GitHubError("only GraphQL queries are allowed")


def _is_transient(response: httpx.Response) -> bool:
    if response.status_code == 429 or response.status_code >= 500:
        return True
    # Secondary rate limits are a 403 with Retry-After, or with the primary limit exhausted.
    return response.status_code == 403 and (
        "retry-after" in response.headers or response.headers.get("x-ratelimit-remaining") == "0"
    )


def _backoff(attempt: int, response: httpx.Response | None) -> float:
    retry_after = response.headers.get("retry-after") if response is not None else None
    if retry_after and retry_after.isdigit():
        return min(float(retry_after), MAX_WAIT_SECONDS)
    return min(BACKOFF_SECONDS * 2 ** (attempt - 1), MAX_WAIT_SECONDS)


def _safe_path(path: str) -> str:
    return path.split("?", 1)[0]


def gh_cli_token() -> str | None:
    """The token the GitHub CLI is logged in with, or None. Read-only: `gh auth token` only prints it."""
    if shutil.which("gh") is None:
        return None
    try:
        result = subprocess.run(["gh", "auth", "token"], capture_output=True, text=True, timeout=10, check=False)
    except (OSError, subprocess.TimeoutExpired):
        return None
    token = result.stdout.strip()
    return token if result.returncode == 0 and token else None


def write_scopes(response: httpx.Response) -> set[str]:
    """Write-capable scopes a classic token carries. Fine-grained tokens send no scopes header."""
    granted = {s.strip() for s in response.headers.get("x-oauth-scopes", "").split(",") if s.strip()}
    return granted & WRITE_SCOPES
