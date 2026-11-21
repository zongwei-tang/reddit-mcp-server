from urllib.parse import parse_qs

import httpx
import pytest
import respx
from mcp.shared.memory import create_connected_server_and_client_session

from reddit_mcp import ids
from reddit_mcp.config import LIVE_URL, Settings
from reddit_mcp.errors import InvalidRequest, Unavailable
from reddit_mcp.router import Router
from reddit_mcp.server import build_server
from reddit_mcp.sources import ArchiveSource
from reddit_mcp.writer import Guard, RedditWriter, WriteRejected, WriteUncertain

SECRET = "reddit_session=SECRET-TOKEN-123"
ME = {"kind": "t2", "data": {"name": "me_user", "modhash": "mh123"}}


def settings(**over):
    base = dict(cookie=SECRET, live_interval=0, write_interval=0, write_daily_limit=100, enable_writes=True)
    base.update(over)
    return Settings(**base)


@pytest.fixture
def api():
    with respx.mock(base_url=LIVE_URL, assert_all_called=False) as mock:
        mock.get("/api/me.json").respond(json=ME)
        yield mock


def form(call):
    return {k: v[0] for k, v in parse_qs(call.request.content.decode()).items()}


async def test_submit_self_post_sends_modhash_and_returns_link(api):
    submit = api.post("/api/submit").respond(
        json={"json": {"errors": [], "data": {"url": "https://www.reddit.com/r/test/comments/abc/hi/", "id": "abc", "name": "t3_abc"}}}
    )
    result = await RedditWriter(settings()).submit("test", "Hi", "body", None, None, False, False)
    sent = form(submit.calls.last)
    assert sent["sr"] == "test" and sent["kind"] == "self" and sent["text"] == "body"
    assert sent["uh"] == "mh123" and sent["api_type"] == "json"
    assert submit.calls.last.request.headers["x-modhash"] == "mh123"
    assert submit.calls.last.request.headers["cookie"] == SECRET
    assert result.fullname == "t3_abc" and result.permalink.endswith("/comments/abc/hi/")
    assert result.account == "me_user"


async def test_link_post_and_validation(api):
    submit = api.post("/api/submit").respond(json={"json": {"errors": [], "data": {"name": "t3_x", "url": "u"}}})
    w = RedditWriter(settings())
    await w.submit("test", "Link", None, "https://example.com", None, True, False)
    sent = form(submit.calls.last)
    assert sent["kind"] == "link" and sent["url"] == "https://example.com" and sent["nsfw"] == "true"
    assert "text" not in sent
    with pytest.raises(InvalidRequest):
        await w.submit("test", "Both", "t", "https://x", None, False, False)


async def test_reply_parses_comment(api):
    api.post("/api/comment").respond(
        json={"json": {"errors": [], "data": {"things": [{"kind": "t1", "data": {"name": "t1_new", "permalink": "/r/t/comments/abc/x/new/"}}]}}}
    )
    result = await RedditWriter(settings()).reply("t3_abc", "hello")
    assert result.fullname == "t1_new"
    assert result.permalink == "https://www.reddit.com/r/t/comments/abc/x/new/"


async def test_reddit_form_errors_are_surfaced_not_retried(api):
    comment = api.post("/api/comment").respond(
        json={"json": {"errors": [["RATELIMIT", "you are doing that too much. try again in 9 minutes.", "ratelimit"]]}}
    )
    with pytest.raises(WriteRejected, match="RATELIMIT: you are doing that too much"):
        await RedditWriter(settings()).reply("t3_abc", "hello")
    assert comment.call_count == 1


async def test_captcha_is_reported_not_bypassed(api):
    api.post("/api/submit").respond(json={"json": {"errors": [["BAD_CAPTCHA", "care to try these again?", "captcha"]]}})
    with pytest.raises(WriteRejected, match="BAD_CAPTCHA"):
        await RedditWriter(settings()).submit("test", "t", "b", None, None, False, False)


async def test_stale_modhash_is_refreshed_once(api):
    api.post("/api/vote").mock(
        side_effect=[httpx.Response(403, json={}), httpx.Response(200, json={})]
    )
    result = await RedditWriter(settings()).vote("t1_abc", 1)
    assert result.action == "upvoted t1_abc"


async def test_server_error_is_not_resent_and_counts_as_maybe_sent(api):
    comment = api.post("/api/comment").respond(502)
    w = RedditWriter(settings())
    with pytest.raises(WriteUncertain, match="may have gone through"):
        await w.reply("t3_abc", "hello")
    assert comment.call_count == 1
    # The model retrying the same text is stopped by the duplicate guard.
    with pytest.raises(WriteRejected, match="already posted"):
        await w.reply("t3_abc", "hello")
    assert comment.call_count == 1


async def test_timeout_after_send_is_uncertain_and_recorded(api):
    api.post("/api/comment").mock(side_effect=httpx.ReadTimeout("slow"))
    w = RedditWriter(settings())
    with pytest.raises(WriteUncertain):
        await w.reply("t3_abc", "hello")
    with pytest.raises(WriteRejected, match="already posted"):
        await w.reply("t3_abc", "hello")


async def test_connect_error_is_not_recorded(api):
    comment = api.post("/api/comment").mock(
        side_effect=[httpx.ConnectError("down"), httpx.Response(200, json={"json": {"errors": [], "data": {"things": []}}})]
    )
    w = RedditWriter(settings())
    with pytest.raises(Unavailable, match="nothing was sent"):
        await w.reply("t3_abc", "hello")
    await w.reply("t3_abc", "hello")  # allowed: the first one never left
    assert comment.call_count == 2


async def test_vote_with_empty_body_is_success(api):
    api.post("/api/vote").respond(200, text="", headers={"content-type": "text/html"})
    result = await RedditWriter(settings()).vote("t3_abc", -1)
    assert result.action == "downvoted t3_abc"


async def test_parallel_identical_replies_post_once(api):
    import asyncio

    comment = api.post("/api/comment").respond(
        json={"json": {"errors": [], "data": {"things": [{"data": {"name": "t1_n", "permalink": "/x"}}]}}}
    )
    w = RedditWriter(settings())
    results = await asyncio.gather(
        w.reply("t3_abc", "same text"), w.reply("t3_abc", "same text"), return_exceptions=True
    )
    assert comment.call_count == 1
    assert sum(isinstance(r, WriteRejected) for r in results) == 1


async def test_same_text_under_different_parents_is_allowed(api):
    comment = api.post("/api/comment").respond(
        json={"json": {"errors": [], "data": {"things": [{"data": {"name": "t1_n", "permalink": "/x"}}]}}}
    )
    w = RedditWriter(settings())
    await w.reply("t3_aaa", "thanks!")
    await w.reply("t1_bbb", "thanks!")
    assert comment.call_count == 2
    with pytest.raises(WriteRejected, match="already posted"):
        await w.reply("t3_aaa", "thanks!")


async def test_delete_says_reddit_may_have_ignored_it(api):
    api.post("/api/del").respond(json={})
    result = await RedditWriter(settings()).delete("t3_abc")
    assert result.action == "asked reddit.com to delete t3_abc"
    assert any("did not write" in n for n in result.notes)


async def test_429_is_rejected_not_recorded(api):
    api.post("/api/comment").mock(
        side_effect=[httpx.Response(429), httpx.Response(200, json={"json": {"errors": [], "data": {}}})]
    )
    w = RedditWriter(settings())
    with pytest.raises(WriteRejected, match="429"):
        await w.reply("t3_abc", "hello")
    await w.reply("t3_abc", "hello")


async def test_logged_out_session():
    with respx.mock(base_url=LIVE_URL) as mock:
        mock.get("/api/me.json").respond(json={})
        with pytest.raises(Unavailable, match="logged-in user"):
            await RedditWriter(settings()).vote("t1_abc", 1)


def test_guard_interval_daily_cap_and_duplicates():
    g = Guard(min_interval=10, daily_limit=2)
    t = [1000.0]
    g.now = lambda: t[0]
    g.check("hello")
    g.record("hello")
    t[0] += 5
    with pytest.raises(WriteRejected, match="spaced"):
        g.check(None)
    t[0] += 10
    with pytest.raises(WriteRejected, match="already posted"):
        g.check(" hello ")
    g.check("other")
    g.record("other")
    t[0] += 20
    with pytest.raises(WriteRejected, match="daily write limit"):
        g.check("third")
    t[0] += 86400
    g.check("third")


def test_fullname():
    assert ids.fullname("t3_ABC") == "t3_abc"
    assert ids.fullname("https://www.reddit.com/r/x/comments/abc/title/def/") == "t1_def"
    assert ids.fullname("https://www.reddit.com/r/x/comments/abc/title/") == "t3_abc"
    with pytest.raises(InvalidRequest, match="ambiguous"):
        ids.fullname("abc")


def test_writer_repr_hides_cookie():
    assert "SECRET" not in repr(RedditWriter(settings()))


async def test_write_tools_hidden_unless_enabled():
    s = Settings()
    server = build_server(lambda: Router(s, archive=ArchiveSource(s)), enable_writes=False)
    async with create_connected_server_and_client_session(server) as client:
        names = {t.name for t in (await client.list_tools()).tools}
    assert "submit_post" not in names and len(names) == 9


async def test_write_tools_over_mcp(api):
    s = settings()
    api.post("/api/vote").respond(json={})
    server = build_server(
        lambda: Router(s, archive=ArchiveSource(s), writer=RedditWriter(s)), enable_writes=True
    )
    async with create_connected_server_and_client_session(server) as client:
        tools = {t.name: t for t in (await client.list_tools()).tools}
        assert {"submit_post", "reply", "edit_content", "delete_content", "vote", "save_content", "get_my_account"} <= set(tools)
        assert tools["delete_content"].annotations.destructiveHint is True
        assert tools["vote"].annotations.readOnlyHint is False

        res = await client.call_tool("get_my_account", {})
        assert "u/me_user" in res.content[0].text
        res = await client.call_tool("vote", {"target": "t1_abc", "direction": "up"})
        assert not res.isError and "upvoted t1_abc as u/me_user" in res.content[0].text
        res = await client.call_tool("vote", {"target": "abc", "direction": "up"})
        assert res.isError and "ambiguous" in res.content[0].text


async def test_write_tools_without_cookie_explain():
    s = Settings()
    server = build_server(lambda: Router(s, archive=ArchiveSource(s)), enable_writes=True)
    async with create_connected_server_and_client_session(server) as client:
        res = await client.call_tool("reply", {"parent": "t3_abc", "text": "hi"})
    assert res.isError and "REDDIT_ENABLE_WRITES=1 and a session cookie" in res.content[0].text
