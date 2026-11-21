import httpx
import pytest

from reddit_mcp.browser import BrowserTransport, parse_cookie_header
from reddit_mcp.config import Settings
from reddit_mcp.sources.live import LiveSource, is_block_page, make_transport


class FakePage:
    def __init__(self, result):
        self.result = result
        self.calls = []

    async def evaluate(self, script, arg):
        self.calls.append(arg)
        if isinstance(self.result, Exception):
            raise self.result
        return self.result


def transport_with(page):
    t = BrowserTransport("reddit_session=SECRET; other=1")

    async def page_for(origin):
        page.origin = origin
        return page

    t._page_for = page_for
    return t


def test_parse_cookie_header():
    cookies = parse_cookie_header("reddit_session=abc; token_v2=x=y; junk")
    assert [(c["name"], c["value"]) for c in cookies] == [("reddit_session", "abc"), ("token_v2", "x=y")]
    assert all(c["domain"] == ".reddit.com" and c["secure"] for c in cookies)


def test_repr_hides_cookie():
    assert "SECRET" not in repr(BrowserTransport("reddit_session=SECRET"))


async def test_get_goes_through_page_fetch_without_cookie_header():
    page = FakePage(
        {"status": 200, "statusText": "OK", "url": "u", "headers": {"content-type": "application/json"},
         "body": '{"kind": "Listing", "data": {"children": [], "after": null}}'}
    )
    t = transport_with(page)
    live = LiveSource(Settings(cookie="reddit_session=SECRET", live_interval=0), transport=t)
    result = await live.browse("Python", None, None, 5, None)
    assert result.items == []
    sent = page.calls[0]
    assert page.origin == "https://www.reddit.com"
    assert sent["method"] == "GET" and "/r/Python/hot.json" in sent["url"]
    assert "cookie" not in {k.lower() for k in sent["headers"]}
    assert "user-agent" not in {k.lower() for k in sent["headers"]}


async def test_post_body_is_forwarded():
    page = FakePage({"status": 200, "statusText": "OK", "url": "u", "headers": {"content-type": "application/json"}, "body": "{}"})
    t = transport_with(page)
    async with httpx.AsyncClient(transport=t, base_url="https://www.reddit.com") as client:
        r = await client.post("/api/vote", data={"id": "t1_x", "dir": "1"})
    assert r.status_code == 200
    assert page.calls[0]["method"] == "POST" and page.calls[0]["body"] == "id=t1_x&dir=1"


async def test_block_reason_phrase_is_kept():
    page = FakePage({"status": 403, "statusText": "Blocked", "url": "u", "headers": {"content-type": "text/html"}, "body": "<style>...</style>"})
    async with httpx.AsyncClient(transport=transport_with(page)) as client:
        r = await client.get("https://www.reddit.com/api/me.json")
    assert is_block_page(r)


async def test_fetch_errors_become_httpx_errors():
    page = FakePage({"error": "TypeError: Failed to fetch"})
    async with httpx.AsyncClient(transport=transport_with(page)) as client:
        with pytest.raises(httpx.ReadError):
            await client.get("https://www.reddit.com/x")
    page = FakePage(RuntimeError("target closed"))
    async with httpx.AsyncClient(transport=transport_with(page)) as client:
        with pytest.raises(httpx.ReadError):
            await client.get("https://www.reddit.com/x")


def test_block_page_detected_by_reason_phrase_or_late_marker():
    r = httpx.Response(403, extensions={"reason_phrase": b"Blocked"}, text="<style>" + "x" * 9000)
    assert is_block_page(r)
    r = httpx.Response(403, text="<style>" + "x" * 9000 + "</style> blocked by network security")
    assert is_block_page(r)
    assert not is_block_page(httpx.Response(403, text="forbidden"))


def _jwt(exp):
    import base64
    import json

    part = base64.urlsafe_b64encode(json.dumps({"exp": exp}).encode()).decode().rstrip("=")
    return f"h.{part}.s"


def test_jwt_exp():
    from reddit_mcp.browser import jwt_exp

    assert jwt_exp(_jwt(1795259373.5)) == 1795259373.5
    assert jwt_exp("not-a-jwt") is None


class SeqPage(FakePage):
    def __init__(self, results):
        super().__init__(None)
        self.results = list(results)

    async def evaluate(self, script, arg):
        self.calls.append(arg)
        return self.results.pop(0)


async def test_oauth_requests_get_bearer_and_refresh_on_401():
    ok = {"status": 200, "statusText": "", "url": "u", "headers": {"content-type": "application/json"}, "body": "{}"}
    expired = dict(ok, status=401, body='{"message": "Unauthorized"}')
    page = SeqPage([expired, ok])
    t = transport_with(page)
    asked = []

    async def bearer(rejected=None):
        asked.append(rejected)
        return "new-token" if rejected == "old-token" else "old-token"

    t.bearer = bearer
    async with httpx.AsyncClient(transport=t) as client:
        r = await client.get("https://oauth.reddit.com/r/popular/hot")
    assert r.status_code == 200
    assert page.calls[0]["headers"]["Authorization"] == "Bearer old-token"
    assert page.calls[1]["headers"]["Authorization"] == "Bearer new-token"
    assert asked == [None, "old-token"]  # the 401 named the token it refused


async def test_a_401_is_not_resent_with_the_same_token():
    expired = {"status": 401, "statusText": "", "url": "u", "headers": {"content-type": "application/json"}, "body": "{}"}
    page = SeqPage([expired])
    t = transport_with(page)

    async def bearer(rejected=None):
        return "only-token"  # no renewal was possible (say, during the cooldown)

    t.bearer = bearer
    async with httpx.AsyncClient(transport=t) as client:
        r = await client.get("https://oauth.reddit.com/api/v1/me")
    assert r.status_code == 401 and len(page.calls) == 1


async def test_www_requests_get_no_bearer():
    ok = {"status": 200, "statusText": "", "url": "u", "headers": {"content-type": "application/json"}, "body": "{}"}
    page = FakePage(ok)
    t = transport_with(page)

    async def bearer(rejected=None):
        raise AssertionError("no bearer for www")

    t.bearer = bearer
    async with httpx.AsyncClient(transport=t) as client:
        await client.get("https://www.reddit.com/api/me.json")
    assert "Authorization" not in page.calls[0]["headers"]


async def test_live_source_uses_oauth_paths_in_browser_mode():
    seen = []

    def handler(request):
        seen.append(str(request.url))
        return httpx.Response(200, json={"kind": "Listing", "data": {"children": [], "after": None}})

    s = Settings(cookie="reddit_session=x", live_client="browser", live_interval=0)
    live = LiveSource(s, transport=httpx.MockTransport(handler))
    await live.browse("Python", "top", "week", 5, None)
    assert seen[0].startswith("https://oauth.reddit.com/r/Python/top?")


async def test_writer_on_oauth_uses_v1_me_and_no_modhash():
    from urllib.parse import parse_qs

    from reddit_mcp.writer import RedditWriter

    posts = []

    def handler(request):
        if request.url.path == "/api/v1/me":
            return httpx.Response(200, json={"name": "me_user"})
        posts.append(request)
        return httpx.Response(200, json={"json": {"errors": [], "data": {"things": [{"data": {"name": "t1_n", "permalink": "/r/x/comments/a/b/n/"}}]}}})

    s = Settings(cookie="reddit_session=x", live_client="browser", live_interval=0, write_interval=0)
    w = RedditWriter(s, transport=httpx.MockTransport(handler))
    result = await w.reply("t3_abc", "hi")
    assert result.account == "me_user" and result.fullname == "t1_n"
    sent = parse_qs(posts[0].content.decode())
    assert "uh" not in sent and "x-modhash" not in posts[0].headers
    assert str(posts[0].url).startswith("https://oauth.reddit.com/api/comment")


BLOCK = dict(status_code=403, text="<style>...</style>", extensions={"reason_phrase": b"Blocked"}, headers={"content-type": "text/html"})


async def test_live_falls_back_to_www_when_oauth_blocks_and_remembers():
    from .fixtures import live_comments_listing, live_post_listing

    seen = []

    def handler(request):
        seen.append(f"{request.url.host}{request.url.path}")
        if request.url.host == "oauth.reddit.com" and request.url.path.startswith("/comments"):
            return httpx.Response(**BLOCK)
        return httpx.Response(200, json=[live_post_listing(), live_comments_listing()])

    s = Settings(cookie="reddit_session=x", live_client="browser", live_interval=0)
    live = LiveSource(s, transport=httpx.MockTransport(handler))
    thread = await live.get_post("abc123", 10, 2, "best")
    assert thread.post.id == "abc123"
    assert seen == ["oauth.reddit.com/comments/abc123", "www.reddit.com/comments/abc123.json"]
    await live.get_post("abc123", 10, 2, "top")
    assert seen[-1] == "www.reddit.com/comments/abc123.json" and len(seen) == 3  # oauth skipped


async def test_live_reports_block_when_both_doors_block():
    from reddit_mcp.errors import Unavailable

    s = Settings(cookie="reddit_session=x", live_client="browser", live_interval=0)
    live = LiveSource(s, transport=httpx.MockTransport(lambda r: httpx.Response(**BLOCK)))
    with pytest.raises(Unavailable, match="blocked by network security"):
        await live.search("x", None, None, None, None, None, None, 5, None)


async def test_writer_switches_to_www_when_oauth_blocks_writes():
    from urllib.parse import parse_qs

    from reddit_mcp.writer import RedditWriter

    posts = []

    def handler(request):
        if request.url.path == "/api/v1/me":
            return httpx.Response(200, json={"name": "me_user"})
        if request.url.path == "/api/me.json":
            return httpx.Response(200, json={"data": {"name": "me_user", "modhash": "mh"}})
        posts.append(request)
        if request.url.host == "oauth.reddit.com":
            return httpx.Response(**BLOCK)
        return httpx.Response(200, json={"json": {"errors": [], "data": {"things": [{"data": {"name": "t1_n"}}]}}})

    s = Settings(cookie="reddit_session=x", live_client="browser", live_interval=0, write_interval=0)
    w = RedditWriter(s, transport=httpx.MockTransport(handler))
    result = await w.reply("t3_abc", "hi")
    assert result.fullname == "t1_n"
    assert [p.url.host for p in posts] == ["oauth.reddit.com", "www.reddit.com"]
    assert parse_qs(posts[1].content.decode())["uh"] == ["mh"]
    assert w.last_door == "www"
    # The endpoint remembers its door: the next reply skips oauth entirely.
    await w.reply("t3_abc", "second reply")
    assert [p.url.host for p in posts] == ["oauth.reddit.com", "www.reddit.com", "www.reddit.com"]


async def test_writer_tries_www_after_plain_oauth_403_and_keeps_oauth_for_other_endpoints():
    from reddit_mcp.writer import RedditWriter

    posts = []

    def handler(request):
        if request.url.path == "/api/v1/me":
            return httpx.Response(200, json={"name": "me_user"})
        if request.url.path == "/api/me.json":
            return httpx.Response(200, json={"data": {"name": "me_user", "modhash": "mh"}})
        posts.append(f"{request.url.host}{request.url.path}")
        if request.url.host == "oauth.reddit.com" and request.url.path == "/api/comment":
            return httpx.Response(403, json={"message": "Forbidden", "error": 403})
        if request.url.path == "/api/comment":
            return httpx.Response(200, json={"json": {"errors": [], "data": {"things": [{"data": {"name": "t1_n"}}]}}})
        return httpx.Response(200, json={})

    s = Settings(cookie="reddit_session=x", live_client="browser", live_interval=0, write_interval=0)
    w = RedditWriter(s, transport=httpx.MockTransport(handler))
    await w.reply("t3_abc", "hi")
    await w.save("t3_abc", True)
    assert posts == ["oauth.reddit.com/api/comment", "www.reddit.com/api/comment", "oauth.reddit.com/api/save"]


async def test_writer_refusal_message_explains_without_secrets():
    from reddit_mcp.errors import Unavailable
    from reddit_mcp.writer import RedditWriter

    def handler(request):
        if request.url.path == "/api/me.json":
            return httpx.Response(200, json={"data": {"name": "me_user", "modhash": "mh"}})
        return httpx.Response(403, json={"message": "Forbidden: modhash=SECRETMODHASH123456"})

    w = RedditWriter(Settings(cookie="reddit_session=x", live_interval=0, write_interval=0), transport=httpx.MockTransport(handler))
    with pytest.raises(Unavailable) as exc:
        await w.save("t3_abc", True)
    assert "HTTP 403: Forbidden" in str(exc.value) and "SECRETMODHASH123456" not in str(exc.value)


def test_endpoint_kind():
    from reddit_mcp.sources.live import endpoint_kind

    assert endpoint_kind("/r/Python/hot.json") == "/r/*/hot"
    assert endpoint_kind("/comments/abc/_/def.json") == "/comments"
    assert endpoint_kind("/user/bob/submitted.json") == "/user/*/submitted"
    assert endpoint_kind("/search.json") == "/search"


def test_make_transport_modes():
    assert make_transport(Settings(cookie="a=b")) is None
    assert isinstance(make_transport(Settings(cookie="a=b", live_client="browser")), BrowserTransport)
    with pytest.raises(ValueError):
        Settings.from_env({"REDDIT_LIVE_CLIENT": "curl"})
