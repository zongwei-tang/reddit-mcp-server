import httpx
import pytest

from reddit_mcp.config import Settings
from reddit_mcp.http_app import KeepOpen, run_checks
from reddit_mcp.probe import (
    IMPOSSIBLE_SUBREDDIT,
    describe,
    parse_probes,
    redact,
    run_probes,
    write_selftest,
)

JWT = "eyJhbGciOiJSUzI1NiJ9.eyJzdWIiOiJ0Ml8yIn0.c2lnbmF0dXJlc2lnbmF0dXJl"


@pytest.mark.parametrize(
    "line",
    [
        f'"token_v2": "{JWT}"',
        'r.config = {"modhash": "abc123def456ghi789"}',
        "reddit_session=SECRETSECRETSECRET; loid=000000002nflturgnp.2.17",
        "Authorization: Bearer abcdefghijklmnopqrstuvwxyz0123",
        "uh=zzzzzzzzzzzzzzzz&api_type=json",
        "csrf_token=ec753b8572f934502007033978701443",
        "x" + "A" * 60 + " trailing",
    ],
)
def test_redact_removes_secrets(line):
    out = redact(line)
    for secret in (JWT, "abc123def456ghi789", "SECRETSECRETSECRET", "abcdefghijklmnopqrstuvwxyz0123",
                   "zzzzzzzzzzzzzzzz", "ec753b8572f934502007033978701443", "A" * 60):
        assert secret not in out


@pytest.mark.parametrize(
    "line",
    [
        '<input type="hidden" name="uh" value="SECRETUH12345">',
        "<input value='SECRETUH12345' type='hidden' name='uh'>",
        '<input name="csrf_token" type="hidden" value="SECRETUH12345"/>',
        '{\\"modhash\\": \\"SECRETUH12345\\", \\"x\\": 1}',
        'data-config="{&quot;modhash&quot;:&quot;SECRETUH12345&quot;}"',
    ],
)
def test_redact_html_and_escaped_forms(line):
    assert "SECRETUH12345" not in redact(line)


def test_parse_probes_lenient_keeps_good_entries():
    from reddit_mcp.probe import parse_probes_lenient

    probes, errors = parse_probes_lenient(
        '[{"url": "https://www.reddit.com/a"}, {"url": "https://[::1"}, {"nope": 1}, "https://evil.com/x", "https://old.reddit.com/b"]'
    )
    assert [p.url for p in probes] == ["https://www.reddit.com/a", "https://old.reddit.com/b"]
    assert len(errors) == 3 and all("skipped" in e for e in errors)
    assert parse_probes_lenient("{not json")[1][0].startswith("REDDIT_MCP_PROBES is not valid JSON")


def test_http_request_logs_are_quiet():
    import logging

    from reddit_mcp.server import build_server

    build_server(enable_writes=False)
    assert logging.getLogger("httpx").getEffectiveLevel() >= logging.WARNING
    assert logging.getLogger("httpcore").getEffectiveLevel() >= logging.WARNING


def test_redact_keeps_ordinary_text():
    text = 'class="search-result search-result-link" data-fullname="t3_1y1lfmh" href="/r/Python/comments/1y1lfmh/x/"'
    assert redact(text) == text


def test_parse_probes_validates_hosts_and_limits():
    probes = parse_probes('[{"url": "https://old.reddit.com/r/x/search?q=a", "find": ["a"], "chars": 99999}, "https://www.reddit.com/"]')
    assert [p.url for p in probes] == ["https://old.reddit.com/r/x/search?q=a", "https://www.reddit.com/"]
    assert probes[0].chars == 1500
    assert parse_probes("") == []
    for bad in ('[{"url": "https://evil.example.com/"}]', '[{"url": "http://www.reddit.com/"}]',
                '[{"url": "https://reddit.com.evil.io/"}]', '{"url": "x"}'):
        with pytest.raises(ValueError):
            parse_probes(bad)


def test_describe_counts_and_snippets():
    (probe,) = parse_probes('[{"url": "https://old.reddit.com/s", "find": ["search-result", "t3_"], "around": "search-result", "nth": 2, "chars": 120}]')
    body = '<div class="search-result" data-fullname="t3_a">A</div> <div class="search-result" data-fullname="t3_b">B</div> "modhash": "SECRETMODHASH123"'
    lines = describe(probe, httpx.Response(200, text=body, headers={"content-type": "text/html"}))
    assert lines[0].startswith("GET old.reddit.com/s -> HTTP 200 text/html")
    assert "'search-result'=2" in lines[1] and "'t3_'=2" in lines[1]
    assert "t3_b" in lines[2] and "SECRETMODHASH123" not in "\n".join(lines)


async def test_run_probes_through_a_transport():
    def handler(request):
        return httpx.Response(200, text="<shreddit-post id='t3_x'></shreddit-post>", headers={"content-type": "text/html"})

    probes = parse_probes('[{"url": "https://www.reddit.com/search/?q=a", "find": ["<shreddit-post"]}]')
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        lines = await run_probes(client, probes)
    assert lines[0].startswith("[1] GET www.reddit.com/search/?q=a -> HTTP 200")
    assert "'<shreddit-post'=1" in lines[1]


async def test_full_write_selftest_creates_nothing_and_reports_rejections():
    seen = []

    def handler(request):
        seen.append((request.method, request.url.path, request.content.decode()))
        if request.url.path == "/api/me.json":
            return httpx.Response(200, json={"data": {"name": "me_user", "modhash": "mh"}})
        if request.url.path in ("/api/save", "/api/unsave"):
            return httpx.Response(200, json={})
        if request.url.path == "/api/submit":
            return httpx.Response(200, json={"json": {"errors": [["SUBREDDIT_NOEXIST", "that subreddit doesn't exist", "sr"]]}})
        if request.url.path == "/api/comment":
            return httpx.Response(200, json={"json": {"errors": [["NO_TEXT", "we need something here", "text"]]}})
        if request.url.path == "/api/vote":
            return httpx.Response(200, json={})
        return httpx.Response(200, json={"json": {"errors": [["NO_THING_ID", "that thing does not exist", "parent"]]}})

    s = Settings(cookie="reddit_session=x", live_interval=0)
    lines = await write_selftest(s, httpx.MockTransport(handler), "t3_abc", full=True)
    text = "\n".join(lines)
    assert "account u/me_user" in text
    assert "OK saved t3_abc (via www)" in text and "OK unsaved t3_abc" in text
    assert "submit reached Reddit and was refused as expected" in text and "SUBREDDIT_NOEXIST" in text
    assert "reply reached Reddit and was refused as expected" in text and "NO_TEXT" in text
    assert "edit reached Reddit and was refused as expected" in text
    assert "OK cleared vote on t3_abc" in text
    assert "UNEXPECTED" not in text
    submitted = [body for method, path, body in seen if path == "/api/submit"]
    assert IMPOSSIBLE_SUBREDDIT in submitted[0]
    replies = [body for method, path, body in seen if path == "/api/comment"]
    assert "text=&" in replies[0] + "&" and "thing_id=t3_abc" in replies[0]


async def test_write_selftest_flags_unexpected_success():
    def handler(request):
        if request.url.path == "/api/me.json":
            return httpx.Response(200, json={"data": {"name": "me_user", "modhash": "mh"}})
        return httpx.Response(200, json={"json": {"errors": [], "data": {"name": "t3_new", "url": "https://x"}}})

    lines = await write_selftest(Settings(cookie="reddit_session=x", live_interval=0), httpx.MockTransport(handler), "t3_abc", full=True)
    assert any(line.startswith("UNEXPECTED OK submit") for line in lines)


async def test_run_checks_logs_each_part_and_closes(capsys):
    calls = []

    def handler(request):
        calls.append(f"{request.url.host}{request.url.path}")
        if request.url.path == "/api/me.json":
            return httpx.Response(200, json={"data": {"name": "me_user", "modhash": "mh"}})
        return httpx.Response(200, json={})

    class Closing(httpx.MockTransport):
        closed = False

        async def aclose(self):
            Closing.closed = True

    transport = Closing(handler)
    from reddit_mcp.sources import live

    original = live.make_transport
    live.make_transport = lambda settings, **kw: transport
    try:
        env = {
            "REDDIT_MCP_PROBES": '[{"url": "https://www.reddit.com/r/x/search?q=a", "find": ["t3_"]}]',
            "REDDIT_MCP_WRITE_SELFTEST": "1",
            "REDDIT_MCP_WRITE_SELFTEST_TARGET": "t3_1y1lfmh",
        }
        await run_checks(Settings(cookie="reddit_session=SECRET", live_interval=0), env)
        await run_checks(Settings(cookie="reddit_session=SECRET", live_interval=0), {"REDDIT_MCP_WRITE_SELFTEST": "1"})
    finally:
        live.make_transport = original
    err = capsys.readouterr().err
    assert "live-check:" in err and "probe: [1] GET www.reddit.com/r/x/search?q=a" in err
    assert "write-check: OK saved t3_1y1lfmh" in err
    assert "write-check: skipped: set REDDIT_MCP_WRITE_SELFTEST_TARGET" in err
    assert "SECRET" not in err
    assert Closing.closed


async def test_token_selftest_reports_reissue_without_leaking():
    import base64
    import json
    import time

    from reddit_mcp.probe import token_selftest

    def jwt(exp):
        body = base64.urlsafe_b64encode(json.dumps({"exp": exp}).encode()).decode().rstrip("=")
        return f"eyJhbGciOiJub25lIn0.{body}.signaturesignature"

    old, new = jwt(time.time() + 100), jwt(time.time() + 86400)

    class FakeTransport:
        def __init__(self):
            self.token = old

        async def _token_from_context(self):
            return self.token

        async def bearer(self, rejected=None):
            return self.token

        async def renew_token(self):
            self.token = new
            return self.token

    def handler(request):
        return httpx.Response(200, json={"name": "me_user"})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        lines = await token_selftest(FakeTransport(), client)
    text = "\n".join(lines)
    assert "new token issued, different from before" in text
    assert "expires in 24.0 h" in text
    assert "HTTP 200, u/me_user" in text
    assert old not in text and new not in text


class FakeReddit:
    """Just enough of reddit.com's write and read endpoints for the lifecycle test."""

    def __init__(self, fail_edit=False, profile_refuses=False):
        self.title = None
        self.reply_body = None
        self.deleted = set()
        self.calls = []
        self.fail_edit = fail_edit
        self.profile_refuses = profile_refuses

    def post_listing(self):
        author = "[deleted]" if "t3_new" in self.deleted else "me_user"
        return {"kind": "Listing", "data": {"children": [{"kind": "t3", "data": {
            "id": "new", "title": self.title, "author": author, "subreddit": "u_me_user",
            "selftext": "[deleted]" if "t3_new" in self.deleted else "body", "permalink": "/user/me_user/comments/new/x/",
            "created_utc": 1, "num_comments": 1, "score": 1}}]}}

    def __call__(self, request):
        from urllib.parse import parse_qs

        path = request.url.path
        form = {k: v[0] for k, v in parse_qs(request.content.decode()).items()} if request.method == "POST" else {}
        self.calls.append((request.method, path, form.get("sr") or form.get("thing_id") or form.get("id") or ""))
        if path == "/api/me.json":
            return httpx.Response(200, json={"data": {"name": "me_user", "modhash": "mh"}})
        if path == "/api/submit":
            if self.profile_refuses and form["sr"].startswith("u_"):
                return httpx.Response(200, json={"json": {"errors": [["SUBREDDIT_NOTALLOWED", "not allowed", "sr"]]}})
            self.title = form["title"]
            return httpx.Response(200, json={"json": {"errors": [], "data": {"name": "t3_new", "url": "https://www.reddit.com/user/me_user/comments/new/x/"}}})
        if path == "/api/comment":
            self.reply_body = form["text"]
            return httpx.Response(200, json={"json": {"errors": [], "data": {"things": [{"data": {"name": "t1_rep", "permalink": "/x/rep/"}}]}}})
        if path == "/api/editusertext":
            if self.fail_edit:
                return httpx.Response(200, json={"json": {"errors": [["TOO_LONG", "too long", "text"]]}})
            self.reply_body = form["text"]
            return httpx.Response(200, json={"json": {"errors": [], "data": {"things": [{"data": {"name": "t1_rep"}}]}}})
        if path == "/api/del":
            self.deleted.add(form["id"])
            return httpx.Response(200, json={})
        if path in ("/api/vote", "/api/save", "/api/unsave"):
            return httpx.Response(200, json={})
        if path == "/comments/new.json":
            return httpx.Response(200, json=[self.post_listing(), {"kind": "Listing", "data": {"children": []}}])
        if path == "/comments/new/_/rep.json":
            comment = {"kind": "t1", "data": {"id": "rep", "body": self.reply_body, "author": "me_user", "parent_id": "t3_new",
                                              "link_id": "t3_new", "created_utc": 1, "score": 1, "depth": 0, "replies": ""}}
            return httpx.Response(200, json=[self.post_listing(), {"kind": "Listing", "data": {"children": [comment]}}])
        return httpx.Response(404, json={"error": 404})


async def test_lifecycle_runs_every_write_and_cleans_up():
    from reddit_mcp.probe import lifecycle_selftest

    reddit = FakeReddit()
    lines = await lifecycle_selftest(Settings(cookie="reddit_session=x", live_interval=0), httpx.MockTransport(reddit), stamp="T1", pause=0)
    text = "\n".join(lines)
    for expected in ("OK submit to u_me_user -> t3_new", "OK read back the post: title matches", "OK reply -> t1_rep",
                     "OK edit t1_rep", "OK read back the edit: 'self-test reply T1 (edited)'", "OK upvoted t3_new",
                     "OK cleared vote on t3_new", "OK saved t3_new", "OK unsaved t3_new", "OK deleted t1_rep",
                     "OK deleted t3_new", "OK read back after delete: author=[deleted]"):
        assert expected in text, (expected, text)
    assert reddit.deleted == {"t1_rep", "t3_new"}


async def test_lifecycle_falls_back_to_r_test_and_cleans_up_after_a_failure():
    from reddit_mcp.probe import lifecycle_selftest

    reddit = FakeReddit(fail_edit=True, profile_refuses=True)
    lines = await lifecycle_selftest(Settings(cookie="reddit_session=x", live_interval=0), httpx.MockTransport(reddit), stamp="T2", pause=0)
    text = "\n".join(lines)
    assert "submit to u_me_user refused" in text and "OK submit to test -> t3_new" in text
    assert "FAILED: WriteRejected" in text and "TOO_LONG" in text
    assert reddit.deleted == {"t1_rep", "t3_new"}  # cleanup ran anyway


def test_keep_open_does_not_close_inner():
    class Inner(httpx.MockTransport):
        closed = False

        async def aclose(self):
            Inner.closed = True

    import asyncio

    async def go():
        async with httpx.AsyncClient(transport=KeepOpen(Inner(lambda r: httpx.Response(200)))) as c:
            await c.get("https://www.reddit.com/")

    asyncio.run(go())
    assert not Inner.closed
