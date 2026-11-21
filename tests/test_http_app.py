import json
from contextlib import asynccontextmanager

import httpx
import pytest
import respx

from reddit_mcp.config import ARCHIVE_URL
from reddit_mcp.http_app import AuthGate, check_auth_config

from .fixtures import archive_post

TOKEN = "t" * 32
HEADERS = {"accept": "application/json, text/event-stream", "content-type": "application/json"}


def rpc(method, params=None, id=1):
    return {"jsonrpc": "2.0", "id": id, "method": method, "params": params or {}}


INIT = rpc(
    "initialize",
    {"protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "t", "version": "0"}},
)


@pytest.fixture(autouse=True)
def archive_only_env(monkeypatch):
    monkeypatch.setenv("REDDIT_MCP_ARCHIVE_INTERVAL", "0")
    monkeypatch.delenv("REDDIT_COOKIE", raising=False)
    monkeypatch.delenv("REDDIT_COOKIE_FILE", raising=False)


@asynccontextmanager
async def make_client():
    # A context manager, not an async fixture: the MCP lifespan must be entered
    # and exited in the same task, which pytest-asyncio fixtures do not guarantee.
    from reddit_mcp.server import build_server

    server = build_server(host="0.0.0.0", shared_router=True, stateless_http=True)
    inner = server.streamable_http_app()
    app = AuthGate(inner, TOKEN)
    async with inner.router.lifespan_context(inner):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://example.up.railway.app") as c:
            yield c


async def test_health_needs_no_token():
    async with make_client() as client:
        await _test_health_needs_no_token(client)


async def _test_health_needs_no_token(client):
    r = await client.get("/health")
    assert r.status_code == 200 and r.json() == {"status": "ok"}


async def test_mcp_requires_token():
    async with make_client() as client:
        await _test_mcp_requires_token(client)


async def _test_mcp_requires_token(client):
    r = await client.post("/mcp", json=INIT, headers=HEADERS)
    assert r.status_code == 401
    r = await client.post("/mcp", json=INIT, headers={**HEADERS, "authorization": "Bearer wrong"})
    assert r.status_code == 401
    r = await client.post("/mcp/wrong-token-wrong-token-xx", json=INIT, headers=HEADERS)
    assert r.status_code == 404


@pytest.mark.parametrize("mode", ["header", "path"])
async def test_tool_call_over_http(mode):
    async with make_client() as client:
        await _test_tool_call_over_http(client, mode)


async def _test_tool_call_over_http(client, mode):
    headers = dict(HEADERS)
    path = "/mcp"
    if mode == "header":
        headers["authorization"] = f"Bearer {TOKEN}"
    else:
        path = f"/mcp/{TOKEN}"
    r = await client.post(path, json=INIT, headers=headers)
    assert r.status_code == 200, r.text
    assert r.json()["result"]["serverInfo"]["name"] == "reddit"

    with respx.mock(base_url=ARCHIVE_URL, assert_all_called=False) as api:
        api.get("/api/posts/search").respond(json={"data": [archive_post()]})
        call = rpc("tools/call", {"name": "browse_subreddit", "arguments": {"subreddit": "Python"}}, id=2)
        r = await client.post(path, json=call, headers=headers)
    assert r.status_code == 200, r.text
    result = r.json()["result"]
    assert not result.get("isError"), json.dumps(result)
    assert "r/Python · new" in result["content"][0]["text"]


def test_refuses_public_bind_without_token():
    assert check_auth_config("0.0.0.0", None, False)
    assert check_auth_config("0.0.0.0", None, True) is None
    assert check_auth_config("127.0.0.1", None, False) is None
    assert "at least" in check_auth_config("0.0.0.0", "short", False)
    assert check_auth_config("0.0.0.0", TOKEN, False) is None
