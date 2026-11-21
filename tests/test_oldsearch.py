"""old.reddit.com search page parsing.

The fixture is stitched together from markup captured on the deployed server
(2026-11-21, `probe:` log lines): one self post with flair and body, one link
post, the "next" pager, and a subreddit result that must be ignored.
"""

import httpx
import pytest

from reddit_mcp import oldsearch
from reddit_mcp.config import Settings
from reddit_mcp.sources import LiveSource

SELF_POST = (
    '<div class=" search-result search-result-link has-thumbnail has-linkflair linkflair-tutorial " data-fullname="t3_1xq2x06">'
    '<a href="/r/Python/comments/1xq2x06/internals_of_asyncio/" class="may-blank thumbnail self"></a><div>'
    '<header class="search-result-header"><span class="linkflairlabel " title="Tutorial">Tutorial</span>'
    '<a href="https://old.reddit.com/r/Python/comments/1xq2x06/internals_of_asyncio/" class="search-title may-blank" >Internals of Asyncio</a></header>'
    '<div class="search-result-meta"><span class="search-result-icon search-result-icon-score"></span><span class="search-score">76 points</span>&#32;'
    '<a href="https://old.reddit.com/r/Python/comments/1xq2x06/internals_of_asyncio/" class="search-comments may-blank" >10 comments</a>&#32;'
    '<span class="search-time">submitted&#32;<time title="Sat Nov 7 10:04:24 2026 UTC" datetime="2026-11-07T10:04:24+00:00">13 days ago</time></span>&#32;'
    '<span class="search-author">by&#32;<a href="https://old.reddit.com/user/example_author" class="author may-blank id-t2_aaaa1111" >example_author</a><span class="userattrs"></span></span>&#32;'
    '<span>to&#32;<a href="https://old.reddit.com/r/Python/" class="search-subreddit-link may-blank" >r/Python</a></span></div>'
    '<div class="search-expando collapsed"><div class="search-result-body"><div class="md"><p>This article is a deep dive into the internals of the asyncio Python standard library &amp; more.</p> '
    '<p>Article: <a href="https://origintracer.app/excerpt/asyncio-internals/">internals of asyncio</a></p> </div> </div></div>'
    '<div class="search-expando-button collapsed"><span class="search-expando-button-label-collapsed">more</span><span class="search-expando-button-label-expanded">less</span></div></div></div>'
)
LINK_POST = (
    '<div class=" search-result search-result-link no-linkflair " data-fullname="t3_1xe7dp8"><div>'
    '<header class="search-result-header"><a href="https://old.reddit.com/r/programming/comments/1xe7dp8/the_move_to_python_3/" class="search-title may-blank" >The move to Python 3 &quot;begins&quot;</a></header>'
    '<div class="search-result-meta"><span class="search-score">1 point</span>&#32;'
    '<a href="https://old.reddit.com/r/programming/comments/1xe7dp8/the_move_to_python_3/" class="search-comments may-blank" >comment</a>&#32;'
    '<span class="search-time">submitted&#32;<time title="x" datetime="2026-11-20T01:00:00+00:00">1 day ago</time></span>&#32;'
    '<span class="search-author">by&#32;<a href="https://old.reddit.com/user/another_author" class="author may-blank id-t2_bbbb2222" >another_author</a><span class="userattrs"></span></span>&#32;'
    '<span>to&#32;<a href="https://old.reddit.com/r/programming/" class="search-subreddit-link may-blank" >r/programming</a></span></div>'
    '<div class="search-result-footer"><span class="search-result-icon search-result-icon-external"></span>'
    '<a href="https://www.eveonline.com/news/view/the-move-to-python-3-begins?a=1&amp;b=2" class="search-link may-blank" >https://www.eveonline.com/news/view/the-move-to-python-3-begins</a></div></div></div>'
)
SUBREDDIT_RESULT = (
    '<div class=" search-result search-result-subreddit " data-fullname="t5_2qh0y"><div>'
    '<a href="https://old.reddit.com/r/Python/" class="search-subreddit-link may-blank" >r/Python</a>'
    '<span class="search-time">a community for 18 years</span><div class="search-result-body">The largest Python community</div></div></div>'
)
PAGER = (
    '<footer><div class="nav-buttons"><span class="nextprev">view more:&#32;'
    '<a href="https://old.reddit.com/r/Python/search?q=asyncio&amp;restrict_sr=on&amp;count=25&amp;after=t3_1clz4dy" rel="nofollow next" >next &rsaquo;</a>'
    "</span></div></footer>"
)
PAGE = (
    '<html><head><title>Python: search results - asyncio</title><script>r.config = {"modhash": "SECRETMODHASH"}</script></head>'
    '<body class="combined-search-page loggedin search-page"><div class="search-result-group">'
    + SUBREDDIT_RESULT
    + '</div><div class="search-result-group"><header class="search-result-group-header"><span class="search-header-label">posts</span></header>'
    + '<div class="contents">' + SELF_POST + LINK_POST + "</div>" + PAGER + "</div>"
    + '<script id="message-report-template" type="text/template"><form data-fullname="<%- fullname %>"></form></script></body></html>'
)


def test_parses_self_and_link_posts_and_ignores_subreddit_results():
    posts, cursor = oldsearch.parse_search_page(PAGE)
    assert [p.id for p in posts] == ["1xq2x06", "1xe7dp8"]
    first, second = posts
    assert first.title == "Internals of Asyncio"
    assert first.score == 76 and first.num_comments == 10
    assert first.author == "example_author" and first.subreddit == "Python"
    assert first.flair == "Tutorial"
    assert first.permalink == "https://www.reddit.com/r/Python/comments/1xq2x06/internals_of_asyncio/"
    assert first.is_self and first.url == ""
    assert first.selftext.startswith("This article is a deep dive") and "&amp;" not in first.selftext
    assert int(first.created_utc) == 1794045864  # 2026-11-07T10:04:24Z
    assert second.title == 'The move to Python 3 "begins"'
    assert second.score == 1 and second.num_comments == 0
    assert not second.is_self
    assert second.url == "https://www.eveonline.com/news/view/the-move-to-python-3-begins?a=1&b=2"
    assert second.flair is None
    assert cursor == "t3_1clz4dy:25"


def test_profile_posts_get_u_subreddit_and_www_permalink():
    block = SELF_POST.replace(
        "https://old.reddit.com/r/Python/comments/1xq2x06/internals_of_asyncio/",
        "https://old.reddit.com/user/spez/comments/1xq2x06/about_that_login_screen/",
    ).replace(
        '<a href="https://old.reddit.com/r/Python/" class="search-subreddit-link may-blank" >r/Python</a>',
        '<a href="https://old.reddit.com/user/spez/" class="search-subreddit-link may-blank" >u/spez</a>',
    )
    (post,), _ = oldsearch.parse_search_page(block)
    assert post.subreddit == "u_spez"
    assert post.permalink == "https://www.reddit.com/user/spez/comments/1xq2x06/about_that_login_screen/"


def test_last_page_has_no_cursor_and_empty_page_is_empty():
    posts, cursor = oldsearch.parse_search_page(PAGE.replace(PAGER, ""))
    assert len(posts) == 2 and cursor is None
    assert oldsearch.parse_search_page("<html><body>nothing here</body></html>") == ([], None)


def test_deleted_author_and_missing_numbers():
    block = SELF_POST.replace(
        '<a href="https://old.reddit.com/user/example_author" class="author may-blank id-t2_aaaa1111" >example_author</a>',
        "<span>[deleted]</span>",
    ).replace('<span class="search-score">76 points</span>', "")
    (post,), _ = oldsearch.parse_search_page(block)
    assert post.author == "[deleted]" and post.score == 0


def test_split_cursor():
    assert oldsearch.split_cursor("t3_abc:50") == ("t3_abc", "50")
    assert oldsearch.split_cursor("t3_abc") == ("t3_abc", None)
    assert oldsearch.split_cursor(None) == (None, None)


def _browser_live(handler):
    s = Settings(cookie="reddit_session=x", live_client="browser", live_interval=0)
    return LiveSource(s, transport=httpx.MockTransport(handler))


async def test_live_search_uses_old_reddit_page_in_browser_mode():
    seen = []

    def handler(request):
        seen.append(request.url)
        return httpx.Response(200, text=PAGE, headers={"content-type": "text/html; charset=UTF-8"})

    live = _browser_live(handler)
    page = await live.search("asyncio", "Python", None, "new", "week", None, None, 2, "t3_prev:25")
    url = seen[0]
    assert url.host == "old.reddit.com" and url.path == "/r/Python/search"
    params = dict(url.params)
    assert params["q"] == "asyncio" and params["restrict_sr"] == "on" and params["sort"] == "new"
    assert params["t"] == "week" and params["limit"] == "2"
    assert params["after"] == "t3_prev" and params["count"] == "25"
    assert [p.id for p in page.items] == ["1xq2x06", "1xe7dp8"]
    assert page.next_cursor == "t3_1clz4dy:25"
    assert any("old.reddit.com" in n for n in page.notes)
    assert "SECRETMODHASH" not in repr(page)


async def test_live_search_author_only_and_global():
    seen = []

    def handler(request):
        seen.append(request.url)
        return httpx.Response(200, text=PAGE, headers={"content-type": "text/html"})

    live = _browser_live(handler)
    await live.search(None, None, "spez", None, None, None, None, 10, None)
    assert seen[0].path == "/search" and dict(seen[0].params)["q"] == "author:spez"
    assert "restrict_sr" not in dict(seen[0].params)


async def test_live_search_reports_layout_change_so_router_falls_back():
    from reddit_mcp.errors import Unavailable

    broken = PAGE.replace("search-result-link", "search-result-item")

    live = _browser_live(lambda r: httpx.Response(200, text=broken, headers={"content-type": "text/html"}))
    with pytest.raises(Unavailable, match="layout changed"):
        await live.search("asyncio", "Python", None, None, None, None, None, 5, None)


async def test_live_search_falls_back_to_json_when_page_is_blocked():
    from .fixtures import live_post_listing

    block = dict(status_code=403, text="<style></style>", extensions={"reason_phrase": b"Blocked"}, headers={"content-type": "text/html"})
    seen = []

    def handler(request):
        seen.append(f"{request.url.host}{request.url.path}")
        if request.url.host == "old.reddit.com":
            return httpx.Response(**block)
        return httpx.Response(200, json=live_post_listing())

    live = _browser_live(handler)
    page = await live.search("x", "pics", None, None, None, None, None, 5, "t3_c:25")
    assert seen == ["old.reddit.com/r/pics/search", "oauth.reddit.com/r/pics/search"]
    assert page.items[0].id == "abc123"


async def test_both_doors_blocked_is_remembered():
    from reddit_mcp.sources.live import Blocked

    block = dict(status_code=403, text="<style></style>", extensions={"reason_phrase": b"Blocked"}, headers={"content-type": "text/html"})
    calls = []

    def handler(request):
        calls.append(request.url.path)
        return httpx.Response(**block)

    live = _browser_live(handler)
    for _ in range(3):
        with pytest.raises(Blocked):
            await live.get_comments(("abc",))
    assert len(calls) == 2  # first call tried both doors; later calls were refused locally


# ------------------------------------------------------------ listing pages

def _thing(pid, ts, *, cls="link self", title="A title", url=None, promoted="false", score=13, comments=1, flair=None):
    url = url or f"/r/Python/comments/{pid}/slug/"
    flair_html = f'<span class="linkflairlabel " title="{flair}">{flair}</span>' if flair else ""
    return (
        f'<div class=" thing id-t3_{pid} odd&#32; {cls}" id="thing_t3_{pid}" onclick="click_thing(this)" '
        f'data-fullname="t3_{pid}" data-type="link" data-author="sam" data-author-fullname="t2_x" '
        f'data-subreddit="Python" data-subreddit-prefixed="r/Python" data-timestamp="{ts}" '
        f'data-url="{url}" data-permalink="/r/Python/comments/{pid}/slug/" data-domain="self.Python" '
        f'data-rank="1" data-comments-count="{comments}" data-score="{score}" data-promoted="{promoted}" '
        f'data-nsfw="false" data-spoiler="false" data-oc="false" data-context="listing" >'
        f'<div class="entry unvoted"><div class="top-matter"><p class="title">{flair_html}'
        f'<a class="title may-blank loggedin " data-event-action="title" href="/r/Python/comments/{pid}/slug/" '
        f'tabindex="1" rel="nofollow" >{title}</a></p></div></div></div>'
    )


LISTING = (
    '<div id="siteTable" class="sitetable linklisting">'
    + _thing("1y1rtih", 1795248668000, flair="News", title="dataviz 1.0: a &amp; b")
    + _thing("ad0001", 1795248000000, promoted="true")
    + _thing("1y1ext1", 1795227065000, cls="link", url="https://example.com/x", score=-2, comments=12)
    + _thing("1y1stk", 1795200000000, cls="link self stickied locked", flair=" Daily Thread")
    + '</div><div class="nav-buttons"><span class="nextprev">view more:&#32;<span class="next-button">'
    '<a href="https://old.reddit.com/r/Python/new/?count=25&amp;after=t3_1xspb09" rel="nofollow next" >next &rsaquo;</a>'
    "</span></span></div>"
)


def test_listing_page_parses_posts_skips_ads_and_pages():
    from reddit_mcp.oldsearch import parse_listing_page

    posts, cursor = parse_listing_page(LISTING)
    assert [p.id for p in posts] == ["1y1rtih", "1y1ext1", "1y1stk"]
    first, link, sticky = posts
    assert first.title == "dataviz 1.0: a & b" and first.flair == "News" and first.author == "sam"
    assert first.created_utc == 1795248668.0 and first.score == 13 and first.num_comments == 1
    assert first.is_self and first.url == "" and first.subreddit == "Python"
    assert first.permalink == "https://www.reddit.com/r/Python/comments/1y1rtih/slug/"
    assert not link.is_self and link.url == "https://example.com/x" and link.score == -2
    assert sticky.stickied and sticky.locked and sticky.flair == "Daily Thread"
    assert cursor == "t3_1xspb09:25"


async def test_live_new_reads_old_reddit_listing_in_browser_mode():
    import httpx

    from reddit_mcp.config import Settings
    from reddit_mcp.sources import LiveSource

    seen = []

    def handler(request):
        seen.append(request.url)
        return httpx.Response(200, text=LISTING, headers={"content-type": "text/html; charset=UTF-8"})

    s = Settings(cookie="reddit_session=x", live_client="browser", live_interval=0)
    live = LiveSource(s, transport=httpx.MockTransport(handler))
    page = await live.browse("Python", "new", None, 3, "t3_abc:25")
    url = seen[0]
    assert url.host == "old.reddit.com" and url.path == "/r/Python/new/"
    assert dict(url.params) == {"limit": "3", "after": "t3_abc", "count": "25"}
    assert [p.id for p in page.items] == ["1y1rtih", "1y1ext1", "1y1stk"]
    assert page.next_cursor == "t3_1xspb09:25" and page.applied == {"sort": "new"}


async def test_live_new_block_page_lets_the_archive_answer():
    import httpx
    import pytest

    from reddit_mcp.config import Settings
    from reddit_mcp.errors import Unavailable
    from reddit_mcp.sources import LiveSource

    def handler(request):
        return httpx.Response(403, text="<html>blocked by network security</html>", headers={"content-type": "text/html"})

    s = Settings(cookie="reddit_session=x", live_client="browser", live_interval=0)
    live = LiveSource(s, transport=httpx.MockTransport(handler))
    with pytest.raises(Unavailable) as exc:
        await live.browse("Python", "new", None, 3, None)
    assert exc.value.fallback
