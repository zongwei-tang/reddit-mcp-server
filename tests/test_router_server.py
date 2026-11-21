import pytest
import respx
from mcp.shared.memory import create_connected_server_and_client_session

from reddit_mcp.config import ARCHIVE_URL, Settings
from reddit_mcp.errors import InvalidRequest, NotSupported, Unavailable
from reddit_mcp.models import Page
from reddit_mcp.router import Router, split_cursor
from reddit_mcp.server import build_server
from reddit_mcp.sources import ArchiveSource

from .fixtures import ARCHIVE_TREE, NOW, archive_post


class Fake:
    def __init__(self, name, error=None):
        self.name = name
        self.label = f"{name}-label"
        self.error = error
        self.calls = 0

    async def browse(self, **kwargs):
        self.calls += 1
        if self.error:
            raise self.error
        return Page(items=[], next_cursor="42", source=self.label, notes=[f"{self.name} note"])

    async def aclose(self):
        pass


def router(live=None, archive=None, **settings):
    return Router(Settings(cookie="x" if live else None, **settings), live=live, archive=archive)


async def test_falls_back_and_explains():
    live, archive = Fake("live", Unavailable("HTTP 403")), Fake("archive")
    page = await router(live, archive).run(
        "browse", "auto", subreddit="a", sort="new", time=None, limit=5
    )
    assert page.source == "archive-label"
    assert page.notes[0].startswith("live-label could not answer (HTTP 403)")
    assert page.next_cursor == "archive:42"


async def test_invalid_request_does_not_fall_back():
    live, archive = Fake("live", InvalidRequest("bad")), Fake("archive")
    with pytest.raises(InvalidRequest):
        await router(live, archive).run("browse", "auto", subreddit="a", sort="new", time=None, limit=5)
    assert archive.calls == 0


async def test_all_failing_combines_reasons():
    live, archive = Fake("live", Unavailable("403")), Fake("archive", NotSupported("no hot"))
    with pytest.raises(NotSupported) as exc:
        await router(live, archive).run("browse", "auto", subreddit="a", sort="hot", time=None, limit=5)
    assert "403" in str(exc.value) and "no hot" in str(exc.value)


async def test_cursor_pins_source_and_cache_hits():
    live, archive = Fake("live"), Fake("archive")
    r = router(live, archive)
    for _ in range(2):
        page = await r.run("browse", "auto", cursor="archive:99", subreddit="a", sort="new", time=None, limit=5)
    assert live.calls == 0 and archive.calls == 1
    assert page.notes == ["archive note"]  # cached result does not accumulate notes


def test_split_cursor():
    assert split_cursor("live:t3_abc") == ("live", "t3_abc")
    with pytest.raises(InvalidRequest):
        split_cursor("garbage")


async def test_live_requested_without_cookie():
    with pytest.raises(InvalidRequest, match="REDDIT_COOKIE"):
        await router(None, Fake("archive")).run("browse", "live", subreddit="a", sort="new", time=None, limit=5)


async def test_end_to_end_over_mcp_with_archive_only():
    settings = Settings(archive_interval=0)

    def factory():
        archive = ArchiveSource(settings)
        archive.now = lambda: NOW
        return Router(settings, archive=archive)

    server = build_server(factory)
    with respx.mock(base_url=ARCHIVE_URL, assert_all_called=False) as api:
        api.get("/api/posts/search").respond(json={"data": [archive_post()]})
        api.get("/api/posts/ids").respond(json={"data": [archive_post()]})
        api.get("/api/comments/tree").respond(json=ARCHIVE_TREE)
        async with create_connected_server_and_client_session(server) as client:
            tools = await client.list_tools()
            assert len(tools.tools) == 9

            res = await client.call_tool("browse_subreddit", {"subreddit": "r/Python"})
            text = res.content[0].text
            assert not res.isError
            assert "r/Python · new" in text and "Arctic Shift archive" in text

            res = await client.call_tool(
                "get_post",
                {"post": "https://www.reddit.com/r/Python/comments/1y1f78s/x/", "comment_depth": 1},
            )
            assert "deeper replies: get_comment_thread" in res.content[0].text

            res = await client.call_tool("browse_subreddit", {"subreddit": "Python", "sort": "hot"})
            assert res.isError and "only sort=new" in res.content[0].text

            res = await client.call_tool("browse_subreddit", {"subreddit": "Python", "source": "live"})
            assert res.isError and "REDDIT_COOKIE" in res.content[0].text


async def test_comment_thread_resolves_post_via_archive_before_live():
    from reddit_mcp.models import Comment, Thread

    calls = []

    class LiveFake(Fake):
        async def get_thread(self, post_id, comment_id, depth, limit, sort):
            calls.append(("live", post_id, comment_id))
            focus = Comment(id=comment_id, author="a", body="b", score=1, created_utc=NOW, depth=0,
                            parent_id="t3_p1", link_id="t3_p1")
            return Thread(post=None, comments=[focus], focus_comment_id=comment_id, source=self.label)

    class ArchiveFake(Fake):
        async def get_comments(self, comment_ids):
            calls.append(("archive", comment_ids))
            c = Comment(id=comment_ids[0], author="a", body="b", score=1, created_utc=NOW, depth=0,
                        parent_id="t3_p1", link_id="t3_p1")
            return Page(items=[c], source=self.label)

    settings = Settings(cookie="x")
    server = build_server(lambda: Router(settings, live=LiveFake("live"), archive=ArchiveFake("archive")))
    async with create_connected_server_and_client_session(server) as client:
        res = await client.call_tool("get_comment_thread", {"comment_id": "t1_c1"})
    assert not res.isError, res.content[0].text
    assert calls == [("archive", ("c1",)), ("live", "p1", "c1")]


async def test_default_sort_falls_back_from_dead_live_to_archive_over_mcp():
    """Expired cookie + no explicit sort must still answer from the archive."""
    from reddit_mcp.config import LIVE_URL
    from reddit_mcp.sources import LiveSource

    settings = Settings(cookie="reddit_session=x", archive_interval=0, live_interval=0)

    def factory():
        archive = ArchiveSource(settings)
        archive.now = lambda: NOW
        return Router(settings, live=LiveSource(settings), archive=archive)

    server = build_server(factory)
    with respx.mock(assert_all_called=False) as api:
        api.get(f"{LIVE_URL}/r/Python/hot.json").respond(403, text="blocked")
        api.get(f"{LIVE_URL}/r/Python/search.json").respond(403, text="blocked")
        api.get(f"{ARCHIVE_URL}/api/posts/search").respond(json={"data": [archive_post()]})
        async with create_connected_server_and_client_session(server) as client:
            res = await client.call_tool("browse_subreddit", {"subreddit": "Python"})
            text = res.content[0].text
            assert not res.isError, text
            assert "r/Python · new" in text
            assert "fell back" in text and "HTTP 403" in text

            res = await client.call_tool("search_posts", {"query": "asyncio", "subreddit": "Python"})
            assert not res.isError, res.content[0].text
            assert "· new" in res.content[0].text

            # An explicit ranking the archive cannot do still fails honestly.
            res = await client.call_tool("browse_subreddit", {"subreddit": "Python", "sort": "hot"})
            assert res.isError and "only sort=new" in res.content[0].text
