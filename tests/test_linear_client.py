import asyncio
import json
import logging
import os
import uuid

import httpx
import pytest

from cycle_runner.linear_client import API_URL, TOKEN_URL, LinearClient, LinearError

SECRET = "s3cret-" + "x" * 25
TOKEN = "tok-" + "y" * 40


class FakeLinearServer:
    """An httpx transport that plays Linear's token and GraphQL endpoints."""

    def __init__(self, scope="read", graphql=None, token_status=200, graphql_status=200):
        self.scope = scope
        self.graphql = graphql or {"data": {"viewer": {"name": "Cycle Runner"}}}
        self.token_status = token_status
        self.graphql_status = graphql_status
        self.requests: list[httpx.Request] = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if str(request.url) == TOKEN_URL:
            if self.token_status != 200:
                return httpx.Response(self.token_status, json={"error": "invalid_client"})
            return httpx.Response(
                200, json={"access_token": TOKEN, "token_type": "Bearer", "expires_in": 2591999, "scope": self.scope}
            )
        assert str(request.url) == API_URL
        return httpx.Response(self.graphql_status, json=self.graphql)

    def client(self) -> LinearClient:
        return LinearClient("client-id", SECRET, transport=httpx.MockTransport(self.handler))


# --- configuration -----------------------------------------------------------


def test_from_env_reads_credentials():
    client = LinearClient.from_env({"LINEAR_CLIENT_ID": "id", "LINEAR_CLIENT_SECRET": "secret"})
    assert (client.client_id, client.client_secret) == ("id", "secret")


@pytest.mark.parametrize("env", [{}, {"LINEAR_CLIENT_ID": "id"}, {"LINEAR_CLIENT_SECRET": "s"}])
def test_from_env_requires_both_credentials(env):
    with pytest.raises(LinearError, match="must be set"):
        LinearClient.from_env(env)


def test_from_env_explains_unresolved_1password_references():
    env = {"LINEAR_CLIENT_ID": "op://agents/x/id", "LINEAR_CLIENT_SECRET": "op://agents/x/secret"}
    with pytest.raises(LinearError, match="op run"):
        LinearClient.from_env(env)


def test_repr_hides_credentials():
    text = repr(LinearClient("client-id", SECRET))
    assert SECRET not in text and "client-id" not in text


# --- read-only ---------------------------------------------------------------


@pytest.mark.parametrize(
    "document",
    [
        "mutation { issueUpdate(id: \"x\", input: {title: \"y\"}) { success } }",
        "subscription { issueCreated { id } }",
        "query A { viewer { id } } mutation B { issueDelete(id: \"x\") { success } }",
        "# looks harmless\nmutation { x }",
    ],
)
def test_refuses_anything_but_queries_before_touching_the_network(document):
    server = FakeLinearServer()

    with pytest.raises(LinearError, match="only GraphQL queries"):
        asyncio.run(server.client().query(document))

    assert server.requests == []


@pytest.mark.parametrize("document", ["{ viewer { name } }", "query ($id: String!) { issue(id: $id) { title } }"])
def test_allows_queries(document):
    assert asyncio.run(FakeLinearServer().client().query(document)) == {"viewer": {"name": "Cycle Runner"}}


def test_requests_a_read_only_client_credentials_token():
    server = FakeLinearServer()

    asyncio.run(server.client().query("{ viewer { name } }"))

    token_request = server.requests[0]
    assert dict(httpx.QueryParams(token_request.content.decode())) == {
        "grant_type": "client_credentials",
        "scope": "read",
    }
    assert token_request.headers["authorization"].startswith("Basic ")
    assert server.requests[1].headers["authorization"] == f"Bearer {TOKEN}"


def test_refuses_a_token_with_more_than_read_scope():
    with pytest.raises(LinearError, match="expected 'read'"):
        asyncio.run(FakeLinearServer(scope="read,write").client().query("{ viewer { name } }"))


def test_reuses_the_token_across_queries():
    server = FakeLinearServer()
    client = server.client()

    async def two_queries():
        await client.query("{ viewer { name } }")
        await client.query("{ viewer { name } }")

    asyncio.run(two_queries())

    token_calls = [r for r in server.requests if str(r.url) == TOKEN_URL]
    assert len(token_calls) == 1


# --- errors never leak credentials -------------------------------------------


def _error_text(server: FakeLinearServer) -> str:
    with pytest.raises(LinearError) as info:
        asyncio.run(server.client().query("{ viewer { name } }"))
    return str(info.value)


def test_rejected_credentials_give_a_clear_error():
    text = _error_text(FakeLinearServer(token_status=401))
    assert "refused the credentials" in text
    assert SECRET not in text


def test_graphql_errors_are_reported_without_credentials():
    server = FakeLinearServer(
        graphql={"errors": [{"message": "x", "extensions": {"userPresentableMessage": "Entity not found"}}]}
    )
    text = _error_text(server)
    assert text == "Entity not found"
    assert TOKEN not in text and SECRET not in text


def test_http_failures_report_only_the_status():
    def handler(request):
        if str(request.url) == TOKEN_URL:
            return httpx.Response(200, json={"access_token": TOKEN, "expires_in": 100, "scope": "read"})
        return httpx.Response(502, text=f"<html>bad gateway {TOKEN}</html>")

    client = LinearClient("id", SECRET, transport=httpx.MockTransport(handler))
    with pytest.raises(LinearError) as info:
        asyncio.run(client.query("{ viewer { name } }"))
    assert str(info.value) == "Linear returned HTTP 502"


def test_network_failures_become_linear_errors():
    def handler(request):
        raise httpx.ConnectError("boom")

    client = LinearClient("id", SECRET, transport=httpx.MockTransport(handler))
    with pytest.raises(LinearError, match="could not reach Linear"):
        asyncio.run(client.query("{ viewer { name } }"))


def test_nothing_secret_is_logged(caplog):
    caplog.set_level(logging.DEBUG)
    asyncio.run(FakeLinearServer().client().query("{ viewer { name } }"))
    assert SECRET not in caplog.text and TOKEN not in caplog.text


# --- real Linear (network) ---------------------------------------------------


def _real_credentials() -> bool:
    value = os.environ.get("LINEAR_CLIENT_ID", "")
    return bool(value) and not value.startswith("op://")


needs_linear = pytest.mark.skipif(
    not _real_credentials(), reason="run under `op run --env-file .env` with Linear credentials"
)


@pytest.mark.linear
@needs_linear
def test_real_linear_grants_only_read_and_refuses_writes():
    client = LinearClient.from_env()

    async def attempt_write():
        token = await client._access_token()
        # Bypasses our query-only guard on purpose, to prove Linear's own refusal.
        # The id doesn't exist, so nothing could change even if writes were allowed.
        response = await client._post(
            API_URL,
            json={"query": f'mutation {{ issueUpdate(id: "{uuid.uuid4()}", input: {{title: "x"}}) {{ success }} }}'},
            headers={"Authorization": f"Bearer {token}"},
        )
        return response.json()

    body = asyncio.run(attempt_write())
    assert body.get("data") is None
    assert body["errors"][0]["extensions"]["code"] == "FORBIDDEN"
    assert "scope" in json.dumps(body["errors"][0]).lower()


@pytest.mark.linear
@needs_linear
def test_real_linear_identity_is_the_app():
    data = asyncio.run(LinearClient.from_env().query("{ viewer { name } }"))
    assert data["viewer"]["name"] == "Cycle Runner"
