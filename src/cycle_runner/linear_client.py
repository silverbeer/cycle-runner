"""Read-only Linear API client.

Everything about talking to Linear lives here: the API URL, OAuth
client-credentials auth, GraphQL, and HTTP errors. Callers pass a GraphQL query
and get back plain data. Nothing here knows about Cycle Runner, and nothing
here imports the rest of the package, so it could move out on its own.

Read-only in three layers:
1. The token is requested with scope "read", so Linear itself refuses writes.
2. query() rejects any GraphQL document that isn't a query.
3. Nothing above this module is given a way to send anything else.

Credentials come from LINEAR_CLIENT_ID and LINEAR_CLIENT_SECRET. They are never
logged, never included in exception messages, and hidden from repr().
"""

import os
import re
import time
from collections.abc import Mapping
from dataclasses import dataclass, field

import httpx

API_URL = "https://api.linear.app/graphql"
TOKEN_URL = "https://api.linear.app/oauth/token"
SCOPE = "read"


class LinearError(Exception):
    """Linear could not be reached or refused the request. Safe to show: no credentials."""


@dataclass
class LinearClient:
    client_id: str = field(repr=False)
    client_secret: str = field(repr=False)
    # Tests pass an httpx.MockTransport; in real use httpx picks its default.
    transport: httpx.AsyncBaseTransport | None = field(default=None, repr=False)
    _token: str | None = field(default=None, init=False, repr=False)
    _token_expires_at: float = field(default=0.0, init=False, repr=False)

    @classmethod
    def from_env(cls, environ: Mapping[str, str] = os.environ) -> "LinearClient":
        client_id = environ.get("LINEAR_CLIENT_ID", "").strip()
        client_secret = environ.get("LINEAR_CLIENT_SECRET", "").strip()
        if not client_id or not client_secret:
            raise LinearError("LINEAR_CLIENT_ID and LINEAR_CLIENT_SECRET must be set")
        if client_id.startswith("op://") or client_secret.startswith("op://"):
            raise LinearError(
                "Linear credentials are unresolved 1Password references; "
                "run under `op run --env-file .env -- ...`"
            )
        return cls(client_id=client_id, client_secret=client_secret)

    async def query(self, document: str, variables: dict | None = None) -> dict:
        """Run a read-only GraphQL query and return its "data"."""
        _require_read_only(document)
        token = await self._access_token()
        response = await self._post(
            API_URL,
            json={"query": document, "variables": variables or {}},
            headers={"Authorization": f"Bearer {token}"},
        )
        body = response.json()
        if body.get("errors"):
            raise LinearError(_first_error_message(body["errors"]))
        return body["data"]

    async def _access_token(self) -> str:
        # Client-credentials tokens last 30 days; fetch one per process and reuse it.
        if self._token and time.monotonic() < self._token_expires_at:
            return self._token
        response = await self._post(
            TOKEN_URL,
            data={"grant_type": "client_credentials", "scope": SCOPE},
            auth=(self.client_id, self.client_secret),
        )
        body = response.json()
        if "access_token" not in body:
            raise LinearError(f"Linear refused the credentials ({body.get('error', 'unknown error')})")
        if body.get("scope") != SCOPE:
            raise LinearError(f"Linear granted scope {body.get('scope')!r}, expected {SCOPE!r}")
        self._token = body["access_token"]
        self._token_expires_at = time.monotonic() + body.get("expires_in", 0) - 60
        return self._token

    async def _post(self, url: str, **kwargs) -> httpx.Response:
        try:
            async with httpx.AsyncClient(transport=self.transport, timeout=15) as http:
                response = await http.post(url, **kwargs)
        except httpx.HTTPError as exc:
            raise LinearError(f"could not reach Linear ({type(exc).__name__})") from None
        # Linear reports GraphQL errors with a 200 or a 400 and a JSON body; anything
        # else (401, 5xx, HTML) is an HTTP-level failure. Only the status is reported.
        if response.status_code >= 400 and not _is_json(response):
            raise LinearError(f"Linear returned HTTP {response.status_code}")
        return response


_OPERATION = re.compile(r"\b(mutation|subscription)\b")


def _require_read_only(document: str) -> None:
    without_comments = re.sub(r"#[^\n]*", "", document).strip()
    if not (without_comments.startswith("{") or without_comments.startswith("query")):
        raise LinearError("only GraphQL queries are allowed")
    if _OPERATION.search(without_comments):
        raise LinearError("only GraphQL queries are allowed")


def _first_error_message(errors: list[dict]) -> str:
    first = errors[0]
    extensions = first.get("extensions") or {}
    return extensions.get("userPresentableMessage") or first.get("message") or "Linear error"


def _is_json(response: httpx.Response) -> bool:
    return response.headers.get("content-type", "").startswith("application/json")
