import httpx
import pytest
import respx

from reddit_mcp.config import LIVE_URL, Settings
from reddit_mcp.errors import InvalidRequest, NotFound, NotSupported, Unavailable
from reddit_mcp.models import Comment, More
from reddit_mcp.sources import LiveSource

from .fixtures import live_comments_listing, live_post_listing

SECRET = "reddit_session=SECRET-TOKEN-123"


@pytest.fixture
def live():
    return LiveSource(Settings(cookie=SECRET, live_interval=0))


@pytest.fixture
def api():
    with respx.mock(base_url=LIVE_URL, assert_all_called=False) as mock:
        yield mock


def test_repr_hides_cookie(live):
    assert "SECRET" not in repr(live)


async def test_browse_sends_cookie_and_raw_json(live, api):
    route = api.get("/r/pics/top.json").respond(json=live_post_listing())
    page = await live.browse("pics", "top", "week", 5, None)
    req = route.calls.last.request
    assert req.headers["cookie"] == SECRET
    assert req.url.params["raw_json"] == "1" and req.url.params["t"] == "week"
    assert page.items[0].subreddit == "pics"


async def test_browse_time_only_for_top(live, api):
    with pytest.raises(NotSupported):
        await live.browse("pics", "hot", "week", 5, None)


@pytest.mark.parametrize(
    "response,error",
    [
        (httpx.Response(403, text="<html>blocked</html>", headers={"content-type": "text/html"}), Unavailable),
        (httpx.Response(302, headers={"location": "https://www.reddit.com/login"}), Unavailable),
        (httpx.Response(200, text="<html>hi</html>", headers={"content-type": "text/html"}), Unavailable),
        (httpx.Response(403, json={"reason": "private"}), NotFound),
        (httpx.Response(404, json={"error": 404}), NotFound),
    ],
)
async def test_failures_map_and_never_leak_cookie(live, api, response, error):
    api.get("/r/pics/hot.json").mock(return_value=response)
    with pytest.raises(error) as exc:
        await live.browse("pics", "hot", None, 5, None)
    assert "SECRET" not in str(exc.value)


async def test_search_rules(live, api):
    with pytest.raises(NotSupported, match="date-range"):
        await live.search("x", None, None, "new", None, "2024-01-01", None, 5, None)
    # Subreddit only: not answerable by reddit.com search, but the archive can
    # (so the router may fall back); nothing at all is a bad request.
    with pytest.raises(NotSupported):
        await live.search(None, "pics", None, "new", None, None, None, 5, None)
    with pytest.raises(InvalidRequest):
        await live.search(None, None, None, "new", None, None, None, 5, None)
    route = api.get("/r/pics/search.json").respond(json=live_post_listing())
    await live.search("cats", "pics", "bob", "top", "year", None, None, 5, None)
    params = route.calls.last.request.url.params
    assert params["q"] == "cats author:bob" and params["restrict_sr"] == "1"


async def test_get_post_parses_post_and_tree(live, api):
    route = api.get("/comments/abc123.json").respond(json=[live_post_listing(), live_comments_listing()])
    thread = await live.get_post("abc123", 50, 4, "best")
    assert route.calls.last.request.url.params["sort"] == "confidence"
    assert thread.post.id == "abc123"
    top, cont = thread.comments
    assert isinstance(top, Comment) and top.replies[0].id == "c2"
    assert isinstance(cont, More) and cont.children == []


async def test_get_thread_looks_up_post_when_missing(live, api):
    api.get("/api/info.json").respond(
        json={"kind": "Listing", "data": {"children": [{"kind": "t1", "data": {"id": "c1", "link_id": "t3_abc123", "body": "x"}}]}}
    )
    route = api.get("/comments/abc123/_/c1.json").respond(json=[live_post_listing(), live_comments_listing()])
    thread = await live.get_thread(None, "c1", 2, 20, "top")
    assert route.calls.last.request.url.params["depth"] == "3"
    assert thread.comments[0].id == "c1"


async def test_non_login_redirect_is_not_reported_as_expired_cookie(live, api):
    api.get("/r/oldname/hot.json").respond(302, headers={"location": "/r/newname/hot.json"})
    with pytest.raises(NotFound, match="redirected"):
        await live.browse("oldname", None, None, 5, None)


async def test_block_page_is_named(live, api):
    page = "<html><body>You've been blocked by network security. To continue, log in</body></html>"
    api.get("/r/pics/hot.json").respond(403, text=page, headers={"content-type": "text/html"})
    with pytest.raises(Unavailable, match="blocked by network security"):
        await live.browse("pics", None, None, 5, None)


async def test_diagnose_keeps_each_probe_query():
    from reddit_mcp.sources.live import diagnose

    seen = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.url)
        return httpx.Response(200, json={})

    await diagnose(Settings(cookie=SECRET), transport=httpx.MockTransport(handler))
    search = next(u for u in seen if u.host == "old.reddit.com" and u.path.endswith("/search"))
    assert dict(search.params) == {"q": "asyncio", "restrict_sr": "on", "raw_json": "1"}
    hot = next(u for u in seen if u.path == "/r/popular/hot.json")
    assert hot.params["limit"] == "1"


async def test_diagnose_reports_without_secrets():
    from reddit_mcp.sources.live import diagnose

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers["cookie"] == SECRET
        if request.url.host == "www.reddit.com":
            return httpx.Response(403, text="blocked by network security", headers={"content-type": "text/html"})
        if request.url.path == "/api/me.json":
            return httpx.Response(200, json={"data": {"name": "me_user", "modhash": "m"}})
        return httpx.Response(200, json={"data": {"children": []}})

    lines = await diagnose(Settings(cookie=SECRET), transport=httpx.MockTransport(handler))
    text = "\n".join(lines)
    assert "www.reddit.com/api/me.json: HTTP 403, text/html, BLOCK PAGE" in text
    assert "old.reddit.com/api/me.json: HTTP 200, application/json, logged in as u/me_user, modhash present" in text
    assert "SECRET" not in text


async def test_time_all_is_no_filter_for_hot(live, api):
    hot = api.get("/r/pics/hot.json").respond(json=live_post_listing())
    page = await live.browse("pics", "hot", "all", 5, None)
    assert hot.called and "t" not in hot.calls.last.request.url.params
    assert page.items


def test_block_page_needs_a_403_and_login_paths_are_exact():
    from reddit_mcp.sources.live import is_block_page, is_login_path

    quoting = httpx.Response(200, text="results about 'blocked by network security' errors")
    assert not is_block_page(quoting)
    assert is_block_page(httpx.Response(403, text="... blocked by network security ..."))
    assert is_login_path("/login") and is_login_path("/login/") and is_login_path("/account/login")
    assert not is_login_path("/r/loginproblems/search") and not is_login_path("/user/loginuser")


def _browser_live(handler):
    s = Settings(cookie=SECRET, live_client="browser", live_interval=0)
    return LiveSource(s, transport=httpx.MockTransport(handler))


async def test_redirect_to_subreddit_search_is_not_found_not_fake_posts():
    def handler(request):
        # The browser followed reddit's redirect for a subreddit that does not exist.
        return httpx.Response(
            200,
            json={"kind": "Listing", "data": {"children": [{"kind": "t5", "data": {"display_name": "nosuch", "title": "x"}}]}},
            headers={"x-final-url": "https://www.reddit.com/subreddits/search.json?q=nosuchsubreddit"},
        )

    live = _browser_live(handler)
    with pytest.raises(NotFound, match="does not exist"):
        await live.browse("nosuchsubreddit", "hot", None, 5, None)
    # ...and that did not mark listings as blocked.
    assert not live._dead


async def test_redirect_to_login_is_unavailable():
    live = _browser_live(
        lambda r: httpx.Response(200, text="<html>login</html>", headers={"x-final-url": "https://www.reddit.com/login/?dest=x"})
    )
    with pytest.raises(Unavailable, match="login page"):
        await live.browse("pics", "hot", None, 5, None)


async def test_canonical_redirects_are_followed_normally():
    from .fixtures import live_comments_listing, live_post_listing as posts

    def handler(request):
        return httpx.Response(
            200,
            json=[posts(), live_comments_listing()],
            headers={"x-final-url": "https://www.reddit.com/r/pics/comments/abc123/x/.json"},
        )

    thread = await _browser_live(handler).get_post("abc123", 10, 2, "best")
    assert thread.post.id == "abc123"


async def test_comments_by_id_with_post_fetches_each_live():
    from .fixtures import live_post_listing as posts
    from .fixtures import listing, t1

    seen = []

    def handler(request):
        seen.append(f"{request.url.host}{request.url.path}")
        cid = request.url.path.rstrip("/").split("/")[-1].removesuffix(".json")
        if request.url.host == "oauth.reddit.com":
            return httpx.Response(403, text="<style></style>", extensions={"reason_phrase": b"Blocked"})
        if cid == "gone":
            return httpx.Response(200, json=[posts(), listing([])])
        return httpx.Response(200, json=[posts(), listing([t1(cid, "t3_abc123", 1, f"body {cid}", replies=listing([t1("x" + cid, "t1_" + cid, 2, "r")]))])])

    live = _browser_live(handler)
    ids = tuple(f"c{i}" for i in range(12)) + ("gone",)
    page = await live.get_comments(ids, post_id="abc123")
    assert [c.id for c in page.items] == [f"c{i}" for i in range(10)]
    assert page.items[0].body == "body c0"
    assert any("call again for the other 3" in n and "post='abc123'" in n for n in page.notes)
    assert seen[0] == "oauth.reddit.com/comments/abc123/_/c0"
    assert seen[1] == "www.reddit.com/comments/abc123/_/c0.json"
    assert all(s.startswith("www.") for s in seen[2:])  # the door was remembered


async def test_default_sorts(live, api):
    hot = api.get("/r/pics/hot.json").respond(json=live_post_listing())
    page = await live.browse("pics", None, None, 5, None)
    assert hot.called and page.applied == {"sort": "hot"}
    mocked = api.get("/search.json").respond(json=live_post_listing())
    await live.search("cats", None, None, None, None, None, None, 5, None)
    assert mocked.calls.last.request.url.params["sort"] == "relevance"
