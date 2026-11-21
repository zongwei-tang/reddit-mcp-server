import pytest

from reddit_mcp import ids
from reddit_mcp.config import Settings, normalize_cookie
from reddit_mcp.errors import InvalidRequest


@pytest.mark.parametrize(
    "value,expected",
    [
        ("abc123", "abc123"),
        ("t3_ABC123", "abc123"),
        ("https://www.reddit.com/r/Python/comments/1y1f78s/some_title/", "1y1f78s"),
        ("https://old.reddit.com/r/x/comments/abc/t/def456/?context=3", "abc"),
        ("https://redd.it/xyz9", "xyz9"),
    ],
)
def test_post_id(value, expected):
    assert ids.post_id(value) == expected


def test_post_id_rejects_share_links_and_garbage():
    with pytest.raises(InvalidRequest, match="share links"):
        ids.post_id("https://www.reddit.com/r/running/s/3TzXiyxaMD")
    with pytest.raises(InvalidRequest):
        ids.post_id("not an id!")


@pytest.mark.parametrize(
    "value",
    ["https://i.redd.it/abc123.jpg", "https://v.redd.it/xyz789", "https://preview.redd.it/abc.png?width=640"],
)
def test_media_links_are_not_posts(value):
    with pytest.raises(InvalidRequest, match="media file"):
        ids.post_id(value)


def test_gallery_links_and_bare_hosts():
    assert ids.post_id("https://www.reddit.com/gallery/1abc2d3") == "1abc2d3"
    assert ids.post_id("reddit.com/r/Python/comments/1y1lfmh/x/") == "1y1lfmh"
    assert ids.post_id("old.reddit.com/r/Python/comments/1y1lfmh") == "1y1lfmh"
    assert ids.post_id("https://www.reddit.com/user/spez/comments/1xiupm0/about/") == "1xiupm0"


def test_slug_named_s_is_not_a_share_link():
    assert ids.post_id("https://www.reddit.com/r/x/comments/abc123/s/") == "abc123"
    with pytest.raises(InvalidRequest, match="share links"):
        ids.post_id("https://www.reddit.com/s/3TzXiyxaMD")


def test_other_sites_are_refused():
    with pytest.raises(InvalidRequest, match="not a Reddit URL"):
        ids.post_id("https://example.com/r/x/comments/abc123/title/")


def test_post_of_comment_url():
    assert ids.post_of_comment_url("https://www.reddit.com/r/x/comments/abc/title/def456/") == "abc"
    assert ids.post_of_comment_url("https://www.reddit.com/r/x/comments/abc/title/") is None
    assert ids.post_of_comment_url("t1_def456") is None
    with pytest.raises(InvalidRequest, match="links to a post"):
        ids.comment_id("https://www.reddit.com/r/x/comments/abc/title/")


def test_comment_id_from_url_and_prefix():
    assert ids.comment_id("t1_19sxtv3lgpatd") == "19sxtv3lgpatd"
    assert ids.comment_id("https://www.reddit.com/r/x/comments/abc/title/def456/") == "def456"


def test_names():
    assert ids.subreddit_name("/r/Python/") == "Python"
    assert ids.subreddit_name("r/AskReddit") == "AskReddit"
    assert ids.username("/u/spez") == "spez"
    assert ids.username("u/some-user_1") == "some-user_1"
    with pytest.raises(InvalidRequest):
        ids.subreddit_name("bad name")


def test_normalize_cookie():
    assert normalize_cookie("Cookie: a=1; b=2\n") == "a=1; b=2"
    assert normalize_cookie("eyJhbGciOi") == "reddit_session=eyJhbGciOi"
    assert normalize_cookie("   ") is None


def test_cookie_bom_is_dropped_and_non_ascii_refused(tmp_path):
    f = tmp_path / "cookie.txt"
    f.write_text("﻿reddit_session=abc; token_v2=x\n", encoding="utf-8")
    assert Settings.from_env({"REDDIT_COOKIE_FILE": str(f)}).cookie == "reddit_session=abc; token_v2=x"
    with pytest.raises(ValueError, match="non-ASCII"):
        normalize_cookie("reddit_session=abc…")


def test_settings_from_env(tmp_path):
    cookie_file = tmp_path / "cookie.txt"
    cookie_file.write_text("reddit_session=secret-value\n")
    s = Settings.from_env({"REDDIT_COOKIE_FILE": str(cookie_file), "REDDIT_SOURCE": "LIVE"})
    assert s.source == "live"
    assert s.has_live
    assert "secret-value" not in repr(s)


def test_settings_rejects_bad_source():
    with pytest.raises(ValueError):
        Settings.from_env({"REDDIT_SOURCE": "somewhere"})


def test_missing_cookie_file_does_not_crash_with_contents(tmp_path):
    with pytest.raises(ValueError, match="cannot read REDDIT_COOKIE_FILE"):
        Settings.from_env({"REDDIT_COOKIE_FILE": str(tmp_path / "missing")})
