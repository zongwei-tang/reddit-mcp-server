import httpx
import pytest
import respx

from reddit_mcp.config import ARCHIVE_URL, Settings
from reddit_mcp.errors import InvalidRequest, NotFound, NotSupported, Unavailable
from reddit_mcp.models import Comment, More
from reddit_mcp.sources import ArchiveSource

from .fixtures import ARCHIVE_TREE, NOW, archive_post


@pytest.fixture
def archive():
    src = ArchiveSource(Settings(archive_interval=0))
    src.now = lambda: NOW
    return src


@pytest.fixture
def api():
    with respx.mock(base_url=ARCHIVE_URL, assert_all_called=False) as mock:
        yield mock


async def test_browse_new_sends_desc_and_cursor(archive, api):
    route = api.get("/api/posts/search").respond(
        json={"data": [archive_post("a", created=NOW - 10 * 86400), archive_post("b", created=NOW - 11 * 86400)]}
    )
    page = await archive.browse("Python", "new", None, 2, "1790000000")
    params = route.calls.last.request.url.params
    assert params["subreddit"] == "Python"
    assert params["sort"] == "desc"
    assert params["before"] == "1790000000"
    assert [p.id for p in page.items] == ["a", "b"]
    assert page.next_cursor == f"{int(NOW - 11 * 86400)}:b"
    assert page.notes == []


async def test_items_sharing_the_boundary_second_are_not_lost(archive, api):
    route = api.get("/api/posts/search").mock(
        side_effect=[
            httpx.Response(200, json={"data": [archive_post("a", created=200), archive_post("b", created=100)]}),
            # Asked again for second 100 (before=101) with room for the shown b:
            # b is dropped, c (same second) and d are new.
            httpx.Response(200, json={"data": [archive_post("b", created=100), archive_post("c", created=100), archive_post("d", created=90)]}),
        ]
    )
    first = await archive.browse("Python", "new", None, 2, None)
    assert first.next_cursor == "100:b"
    second = await archive.browse("Python", "new", None, 2, first.next_cursor)
    params = route.calls.last.request.url.params
    assert params["before"] == "101" and params["limit"] == "3"
    assert [p.id for p in second.items] == ["c", "d"]  # a full page despite the repeat
    assert second.next_cursor == "90:d"


async def test_short_answer_after_the_boundary_means_no_more(archive, api):
    api.get("/api/posts/search").respond(json={"data": [archive_post("b", created=100), archive_post("c", created=100)]})
    page = await archive.browse("Python", "new", None, 2, "100:b")
    assert [p.id for p in page.items] == ["c"] and page.next_cursor is None


async def test_more_than_a_page_in_one_second_does_not_loop(archive, api):
    same_second = [archive_post(f"s{i}", created=100) for i in range(100)]
    route = api.get("/api/posts/search").mock(
        side_effect=[
            httpx.Response(200, json={"data": same_second}),
            httpx.Response(200, json={"data": [archive_post("d", created=99)]}),
        ]
    )
    cursor = "100:" + ",".join(f"s{i}" for i in range(100))
    page = await archive.browse("Python", "new", None, 100, cursor)
    assert [c.request.url.params["before"] for c in route.calls] == ["101", "100"]
    assert [p.id for p in page.items] == ["d"] and page.next_cursor is None


async def test_bad_cursor_is_invalid():
    from reddit_mcp.sources.archive import decode_cursor

    assert decode_cursor("1790000000") == (1790000000, set())
    assert decode_cursor("100:a,b") == (100, {"a", "b"})
    with pytest.raises(InvalidRequest):
        decode_cursor("t3_abc")


async def test_search_time_is_reported_when_after_wins(archive, api):
    api.get("/api/posts/search").respond(json={"data": []})
    page = await archive.search("x", "Python", None, "new", "week", "2024-01-01", None, 5, None)
    assert any("time=week ignored because after=2024-01-01" in n for n in page.notes)


async def test_truncated_tree_says_sort_is_partial(archive, api):
    api.get("/api/posts/ids").respond(json={"data": [archive_post()]})
    api.get("/api/comments/tree").respond(json=ARCHIVE_TREE)  # contains a "more" placeholder
    thread = await archive.get_post("1y1f78s", 50, 3, "new")
    assert any("orders only the comments shown" in n for n in thread.notes)


async def test_browse_refuses_rankings_it_cannot_do(archive, api):
    with pytest.raises(NotSupported, match="only sort=new"):
        await archive.browse("Python", "hot", None, 10, None)
    assert not api.calls


async def test_browse_time_filter_becomes_offset_with_note(archive, api):
    route = api.get("/api/posts/search").respond(json={"data": []})
    page = await archive.browse("Python", "new", "week", 10, None)
    assert route.calls.last.request.url.params["after"] == "7d"
    assert any("7d" in n for n in page.notes)
    assert page.next_cursor is None


async def test_fresh_items_get_a_score_warning(archive, api):
    api.get("/api/posts/search").respond(json={"data": [archive_post("a", created=NOW - 3600)]})
    page = await archive.browse("Python", "new", None, 10, None)
    assert any("under 36h" in n for n in page.notes)


async def test_search_validation(archive, api):
    with pytest.raises(InvalidRequest):
        await archive.search(None, None, None, "new", None, None, None, 10, None)
    with pytest.raises(NotSupported, match="needs a subreddit or author"):
        await archive.search("hello", None, None, "new", None, None, None, 10, None)
    with pytest.raises(NotSupported, match="relevance"):
        await archive.search("hello", "Python", None, "relevance", None, None, None, 10, None)
    assert not api.calls


async def test_search_old_pages_forward(archive, api):
    route = api.get("/api/posts/search").respond(
        json={"data": [archive_post("a", created=1000), archive_post("b", created=2000)]}
    )
    page = await archive.search("x", "Python", None, "old", None, "2020-01-01", None, 2, None)
    params = route.calls.last.request.url.params
    assert params["sort"] == "asc" and params["after"] == "2020-01-01" and params["query"] == "x"
    assert page.next_cursor == "2000:b"


async def test_get_post_builds_tree_honoring_depth(archive, api):
    api.get("/api/posts/ids").respond(json={"data": [archive_post()]})
    tree = api.get("/api/comments/tree").respond(json=ARCHIVE_TREE)
    thread = await archive.get_post("1y1f78s", 50, 1, "top")
    params = tree.calls.last.request.url.params
    assert params["link_id"] == "t3_1y1f78s"
    assert params["start_depth"] == "1"
    assert thread.post.id == "1y1f78s"
    assert [c.score for c in thread.comments] == [11, 1, 0]
    cut = thread.comments[0].replies[0]
    assert isinstance(cut, More) and cut.count == 3


async def test_get_post_best_sort_is_reported_not_silent(archive, api):
    api.get("/api/posts/ids").respond(json={"data": [archive_post()]})
    api.get("/api/comments/tree").respond(json=ARCHIVE_TREE)
    thread = await archive.get_post("1y1f78s", 50, 3, "best")
    assert any("'best'" in n for n in thread.notes)


async def test_fresh_comment_on_old_post_gets_the_score_warning(archive, api):
    import copy

    tree = copy.deepcopy(ARCHIVE_TREE)
    tree["data"][1]["data"]["created_utc"] = NOW - 3600  # a reply from an hour ago
    api.get("/api/posts/ids").respond(json={"data": [archive_post(created=NOW - 5 * 86400)]})
    api.get("/api/comments/tree").respond(json=tree)
    thread = await archive.get_post("1y1f78s", 50, 3, "top")
    assert any("1 item(s) are under 36h" in n for n in thread.notes)


async def test_get_thread_freshness_covers_focus_and_replies(archive, api):
    focus = dict(ARCHIVE_TREE["data"][0]["data"], replies="", created_utc=NOW - 600)
    api.get("/api/comments/ids").respond(json={"data": [focus]})
    api.get("/api/comments/tree").respond(json={"data": []})
    api.get("/api/posts/ids").respond(json={"data": [archive_post(created=NOW - 5 * 86400)]})
    thread = await archive.get_thread("1y1f78s", "19sxtv3lgpatd", 2, 10, "top")
    assert any("under 36h" in n for n in thread.notes)


async def test_get_post_missing(archive, api):
    api.get("/api/posts/ids").respond(json={"data": []})
    with pytest.raises(NotFound):
        await archive.get_post("nope", 10, 2, "top")


async def test_get_thread_finds_post_from_comment(archive, api):
    focus = dict(ARCHIVE_TREE["data"][0]["data"], replies="")
    api.get("/api/comments/ids").respond(json={"data": [focus]})
    tree = api.get("/api/comments/tree").respond(
        json={"data": ARCHIVE_TREE["data"][0]["data"]["replies"]["data"]["children"]}
    )
    api.get("/api/posts/ids").respond(json={"data": [archive_post()]})
    thread = await archive.get_thread(None, "19sxtv3lgpatd", 3, 20, "top")
    params = tree.calls.last.request.url.params
    assert params["link_id"] == "t3_1y1f78s" and params["parent_id"] == "t1_19sxtv3lgpatd"
    (root,) = thread.comments
    assert root.id == "19sxtv3lgpatd"
    assert isinstance(root.replies[0], Comment)
    assert thread.focus_comment_id == "19sxtv3lgpatd"


async def test_get_comments_reports_missing(archive, api):
    focus = dict(ARCHIVE_TREE["data"][1]["data"])
    api.get("/api/comments/ids").respond(json={"data": [focus]})
    page = await archive.get_comments(("19sxxue068ver", "zzz"))
    assert [c.id for c in page.items] == ["19sxxue068ver"]
    assert any("zzz" in n for n in page.notes)


async def test_overview_merges_newest_first(archive, api):
    api.get("/api/posts/search").respond(json={"data": [archive_post("p", created=NOW - 5 * 86400)]})
    c = dict(ARCHIVE_TREE["data"][1]["data"], created_utc=NOW - 4 * 86400)
    api.get("/api/comments/search").respond(json={"data": [c]})
    page = await archive.user_activity("someone", "overview", 10, None)
    assert [type(i).__name__ for i in page.items] == ["Comment", "Post"]


async def test_http_errors_map_to_source_errors(archive, api):
    api.get("/api/posts/search").respond(400, json={"error": "title requires subreddit"})
    with pytest.raises(NotSupported, match="title requires subreddit"):
        await archive.browse("Python", "new", None, 10, None)


async def test_timeout_error_body_is_unavailable(archive, api):
    api.get("/api/posts/search").respond(json={"error": "Query timed out"})
    with pytest.raises(Unavailable, match="timed out"):
        await archive.browse("Python", "new", None, 10, None)


async def test_429_is_retried_with_header_wait(archive, api, no_sleep):
    api.get("/api/posts/search").mock(
        side_effect=[
            httpx.Response(429, headers={"X-RateLimit-Reset": "3"}),
            httpx.Response(200, json={"data": []}),
        ]
    )
    await archive.browse("Python", "new", None, 10, None)
    assert 3.0 in no_sleep


async def test_network_failure_is_unavailable(archive, api):
    api.get("/api/posts/search").mock(side_effect=httpx.ConnectError("boom"))
    with pytest.raises(Unavailable, match="unreachable"):
        await archive.browse("Python", "new", None, 10, None)


async def test_subreddit_info_and_find(archive, api):
    sub = {"display_name": "Python", "subscribers": 1325561, "created_utc": 1201230879,
           "retrieved_on": 1739562419, "public_description": "News about Python", "over18": False}
    api.get("/api/subreddits/search").respond(json={"data": [sub]})
    info = await archive.subreddit_info("Python")
    assert info.subreddit.subscribers == 1325561 and info.subreddit.as_of == 1739562419
    page = await archive.find_subreddits("pyth", 5)
    assert page.items[0].name == "Python"
    assert any("prefix" in n for n in page.notes)


async def test_get_thread_when_tree_includes_focus_comment(archive, api):
    focus_thing = ARCHIVE_TREE["data"][0]
    api.get("/api/comments/ids").respond(json={"data": [dict(focus_thing["data"], replies="")]})
    api.get("/api/comments/tree").respond(json={"data": [focus_thing]})
    api.get("/api/posts/ids").respond(json={"data": [archive_post()]})
    thread = await archive.get_thread("1y1f78s", "19sxtv3lgpatd", 3, 20, "top")
    (root,) = thread.comments
    assert root.replies[0].id == "19sxu4okgmlmq"  # not a duplicate of the focus comment


async def test_get_post_with_zero_comments_skips_tree(archive, api):
    api.get("/api/posts/ids").respond(json={"data": [archive_post()]})
    tree = api.get("/api/comments/tree").respond(json=ARCHIVE_TREE)
    thread = await archive.get_post("1y1f78s", 0, 3, "top")
    assert thread.comments == [] and not tree.called


async def test_default_sort_is_new(archive, api):
    api.get("/api/posts/search").respond(json={"data": []})
    page = await archive.browse("Python", None, None, 5, None)
    assert page.applied == {"sort": "new"}
