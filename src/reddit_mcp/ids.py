"""Normalize user input: URLs, prefixed ids, r/ and u/ names.

URLs are parsed, not pattern-matched anywhere in the string, so the host
decides what a link means: ``i.redd.it/abc.jpg`` is a media file, not post
``abc``, and ``example.com/comments/abc`` is not a Reddit post at all.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from urllib.parse import urlsplit

from .errors import InvalidRequest

_ID = re.compile(r"^[a-z0-9]{1,20}$")
_SUB = re.compile(r"^[A-Za-z0-9_]{2,50}$")
_USER = re.compile(r"^[A-Za-z0-9_-]{2,30}$")
# /r/<sub>/comments/<post>/<slug>/<comment>, also under /user/<name>/ and without a slug.
_COMMENTS_PATH = re.compile(
    r"^(?:/(?:r|u|user)/[^/]+)?/comments/(?P<post>[a-z0-9]{1,20})(?:/(?P<slug>[^/]*)(?:/(?P<comment>[a-z0-9]{1,20}))?)?/?$",
    re.I,
)
_GALLERY_PATH = re.compile(r"^/gallery/(?P<post>[a-z0-9]{1,20})/?$", re.I)
_SHARE_PATH = re.compile(r"^/(?:r/[^/]+/)?s/[A-Za-z0-9]+/?$")
_SHORT_PATH = re.compile(r"^/(?P<post>[a-z0-9]{1,20})/?$", re.I)
MEDIA_HOSTS = ("i.redd.it", "v.redd.it", "preview.redd.it", "external-preview.redd.it")


@dataclass(frozen=True)
class RedditLink:
    post: str
    comment: str | None = None


def _strip_prefix(value: str, prefix: str) -> str:
    return value[len(prefix) :] if value.lower().startswith(prefix) else value


def _as_url(value: str):
    """A SplitResult if ``value`` looks like a URL, else None."""
    v = value.strip()
    if "://" not in v:
        if not re.match(r"^(?:[a-z0-9-]+\.)*(?:reddit\.com|redd\.it)/", v, re.I):
            return None
        v = "https://" + v
    return urlsplit(v)


def parse_link(value: str) -> RedditLink | None:
    """Post (and comment) ids from a Reddit URL; None if ``value`` is not a URL.

    Raises InvalidRequest for URLs that point somewhere else: share links
    (they need a browser to resolve), media files, other sites.
    """
    url = _as_url(value)
    if url is None:
        return None
    host = (url.hostname or "").lower()
    path = url.path or "/"
    if host in MEDIA_HOSTS:
        raise InvalidRequest(f"{value!r} is a media file link, not a post; give the post's URL")
    if host == "redd.it":
        m = _SHORT_PATH.match(path)
        if m:
            return RedditLink(post=m.group("post").lower())
        raise InvalidRequest(f"not a redd.it post link: {value!r}")
    if host == "reddit.com" or host.endswith(".reddit.com"):
        if _SHARE_PATH.match(path):
            raise InvalidRequest(
                "share links (/s/...) cannot be resolved here; open it in a browser and paste the full URL"
            )
        m = _COMMENTS_PATH.match(path) or _GALLERY_PATH.match(path)
        if m:
            comment = m.groupdict().get("comment")
            return RedditLink(post=m.group("post").lower(), comment=comment.lower() if comment else None)
        raise InvalidRequest(f"not a Reddit post or comment URL: {value!r}")
    raise InvalidRequest(f"not a Reddit URL: {value!r}")


def post_id(value: str) -> str:
    link = parse_link(value)
    if link is not None:
        return link.post
    v = _strip_prefix(value.strip().lower(), "t3_")
    if not _ID.match(v):
        raise InvalidRequest(f"not a post id or post URL: {value!r}")
    return v


def comment_id(value: str) -> str:
    link = parse_link(value)
    if link is not None:
        if link.comment is None:
            raise InvalidRequest(f"{value!r} links to a post, not a comment")
        return link.comment
    v = _strip_prefix(value.strip().lower(), "t1_")
    if not _ID.match(v):
        raise InvalidRequest(f"not a comment id or comment URL: {value!r}")
    return v


def post_of_comment_url(value: str) -> str | None:
    """The post id carried by a comment URL, if ``value`` is one."""
    try:
        link = parse_link(value)
    except InvalidRequest:
        return None
    return link.post if link is not None and link.comment else None


def fullname(value: str) -> str:
    """A post or comment as a fullname (t3_... or t1_...), from a fullname or URL.

    Bare ids are rejected: posts and comments share the id space, so the
    prefix is the only way to know which one is meant.
    """
    low = value.strip().lower()
    if low.startswith(("t1_", "t3_")) and _ID.match(low[3:]):
        return low
    link = parse_link(value)
    if link is not None:
        return f"t1_{link.comment}" if link.comment else f"t3_{link.post}"
    raise InvalidRequest(
        f"{value!r} is ambiguous: pass t3_<id> for a post, t1_<id> for a comment, or a URL"
    )


def subreddit_name(value: str) -> str:
    v = value.strip().strip("/")
    v = _strip_prefix(v, "r/")
    if not _SUB.match(v):
        raise InvalidRequest(f"not a subreddit name: {value!r}")
    return v


def username(value: str) -> str:
    v = value.strip().strip("/").lstrip("@")
    for prefix in ("user/", "u/"):
        v = _strip_prefix(v, prefix)
    if not _USER.match(v):
        raise InvalidRequest(f"not a username: {value!r}")
    return v
