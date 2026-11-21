from reddit_mcp import format as fmt
from reddit_mcp.models import Comment, More, Page, Thread
from reddit_mcp.parse import count_comments, parse_post, parse_tree, sort_tree

from .fixtures import ARCHIVE_TREE, NOW, archive_post


def test_parse_post_from_archive_shape():
    p = parse_post(archive_post(removed_by_category="moderator", selftext="[removed]"))
    assert p.id == "1y1f78s"
    assert p.permalink.startswith("https://www.reddit.com/r/Python/comments/")
    assert p.removed_by == "moderator"
    assert p.flair == "Discussion"


def test_crosspost_relative_url_becomes_absolute():
    p = parse_post(archive_post(is_self=False, url="/r/pics/comments/abc/original/"))
    assert p.url == "https://www.reddit.com/r/pics/comments/abc/original/"
    p = parse_post(archive_post(is_self=False, url="https://example.com/x"))
    assert p.url == "https://example.com/x"


def test_tree_keeps_more_placeholders_and_empty_replies():
    nodes = parse_tree(ARCHIVE_TREE["data"], max_depth=10)
    assert [type(n) for n in nodes] == [Comment, Comment, Comment]
    first = nodes[0]
    assert first.score == 11
    reply, placeholder = first.replies
    assert isinstance(reply, Comment) and reply.id == "19sxu4okgmlmq"
    assert isinstance(placeholder, More)
    assert placeholder.children == ["19sxv1x9ye1vm"]
    assert nodes[1].replies == []  # "replies": "" means none


def test_depth_is_enforced_exactly():
    nodes = parse_tree(ARCHIVE_TREE["data"], max_depth=1)
    first = nodes[0]
    assert len(first.replies) == 1
    cut = first.replies[0]
    assert isinstance(cut, More) and cut.children == []
    assert cut.parent_id == "t1_19sxtv3lgpatd"
    # 1 reply + 1 nested reply + 1 collapsed sibling
    assert cut.count == 3

    nodes = parse_tree(ARCHIVE_TREE["data"], max_depth=2)
    reply = nodes[0].replies[0]
    assert isinstance(reply.replies[0], More) and reply.replies[0].count == 1


def test_count_comments():
    assert count_comments(ARCHIVE_TREE["data"]) == 6


def test_sort_tree_top_keeps_placeholders_last():
    nodes = sort_tree(parse_tree(ARCHIVE_TREE["data"], 10), "top")
    assert [n.score for n in nodes] == [11, 1, 0]
    assert isinstance(nodes[0].replies[-1], More)
    nodes = sort_tree(parse_tree(ARCHIVE_TREE["data"], 10), "old")
    assert [n.id for n in nodes][0] == "19sxtv3lgpatd"


def test_render_thread_shows_followup_calls():
    thread = Thread(
        post=parse_post(archive_post()),
        comments=parse_tree(ARCHIVE_TREE["data"], max_depth=2),
        source="Arctic Shift archive",
        notes=["a note"],
    )
    text = fmt.render_thread(thread, "post 1y1f78s", now=NOW)
    assert "_source: Arctic Shift archive_" in text
    assert "> note: a note" in text
    assert "get_comments_by_id(comment_ids=['19sxv1x9ye1vm'], post='1y1f78s')" in text
    # The follow-up carries the post id, so live mode needs no (blocked) id lookup.
    assert "get_comment_thread(comment_id='19sxu4okgmlmq', post='1y1f78s')" in text
    assert "**u/user_19sx** · 11 pts" in text


def test_large_more_lists_up_to_the_tool_limit():
    many = More(parent_id="t3_p", depth=0, count=150, children=[f"c{i}" for i in range(150)])
    line = fmt._more_line(many, "", "p")
    assert "'c99'" in line and "'c100'" not in line
    assert "50 more ids not listed" in line and "post='p'" in line


def test_community_names():
    assert fmt.community("Python") == "r/Python"
    assert fmt.community("u_spez") == "u/spez"
    assert fmt.community("") == "r/?"


def test_render_posts_clips_and_shows_cursor():
    long = archive_post(selftext="x" * 1000)
    page = Page(items=[parse_post(long)], next_cursor="archive:123", source="Arctic Shift archive")
    text = fmt.render_posts(page, "r/Python · new", now=NOW)
    assert "[+720 chars]" in text
    assert "next_cursor: `archive:123`" in text
    assert "5d ago" in text
