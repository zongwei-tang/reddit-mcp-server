"""Parse old.reddit.com search result pages.

Reddit blocks the JSON search endpoints (oauth.reddit.com/search and
www.reddit.com/search.json) for this server, but the server-rendered search
page on old.reddit.com answers. Its markup is plain and has been stable for
years: one ``<div class="search-result search-result-link" data-fullname=...>``
per post, with the title, score, comment count, time, author, subreddit,
flair and the post body inside, and a "next" link carrying ``after``/``count``.
"""

from __future__ import annotations

import html as _html
import re
from datetime import datetime
from urllib.parse import parse_qs, urlsplit

from .models import Post

WEB = "https://www.reddit.com"

_RESULT = re.compile(
    r'<div class="[^"]*\bsearch-result-link\b[^"]*"[^>]*\bdata-fullname="t3_([a-z0-9]+)"', re.I
)
_TITLE = re.compile(r'<a href="([^"]*)"[^>]*class="[^"]*\bsearch-title\b[^"]*"[^>]*>(.*?)</a>', re.S | re.I)
_SCORE = re.compile(r'class="search-score"[^>]*>\s*(-?[\d,.]+)\s*point', re.I)
_COMMENTS = re.compile(r'class="[^"]*\bsearch-comments\b[^"]*"[^>]*>\s*([\d,.]+)\s*comment', re.I)
_TIME = re.compile(r'<time[^>]*\bdatetime="([^"]+)"', re.I)
_AUTHOR = re.compile(r'<a[^>]*class="[^"]*\bauthor\b[^"]*"[^>]*>([^<]+)</a>', re.I)
# Posts on a user profile show "u/name" here; Reddit calls that subreddit "u_name".
_SUBREDDIT = re.compile(r'class="[^"]*\bsearch-subreddit-link\b[^"]*"[^>]*>\s*/?(r|u)/([^<\s]+)\s*</a>', re.I)
_FLAIR = re.compile(r'<span class="[^"]*\blinkflairlabel\b[^"]*"[^>]*title="([^"]*)"', re.I)
_LINK = re.compile(r'<a href="([^"]+)"[^>]*class="[^"]*\bsearch-link\b[^"]*"', re.I)
_BODY = re.compile(r'class="search-result-body"[^>]*>(.*?)</div>\s*</div>', re.S | re.I)
_NEXT = re.compile(r'class="nextprev"[^>]*>.*?<a href="([^"]+)"[^>]*\brel="[^"]*\bnext\b', re.S | re.I)
_TAG = re.compile(r"<[^>]+>")
_PERMALINK = re.compile(r"/(?:r|u|user)/[^/]+/comments/[a-z0-9]+/[^\"?#]*", re.I)


def _text(fragment: str) -> str:
    return " ".join(_html.unescape(_TAG.sub(" ", fragment)).split())


def _number(match: re.Match | None) -> int:
    if not match:
        return 0
    try:
        return int(float(match.group(1).replace(",", "")))
    except ValueError:
        return 0


def _timestamp(value: str) -> float:
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return 0.0


def _web_url(href: str) -> str:
    href = _html.unescape(href)
    m = _PERMALINK.search(href)
    return WEB + m.group(0) if m else href


def parse_search_page(page: str) -> tuple[list[Post], str | None]:
    """Posts on one result page, and the cursor for the next page ("t3_x:25") or None."""
    starts = list(_RESULT.finditer(page))
    end_of_results = page.find("<footer", starts[-1].end()) if starts else -1
    posts: list[Post] = []
    for i, m in enumerate(starts):
        stop = starts[i + 1].start() if i + 1 < len(starts) else (end_of_results if end_of_results > 0 else len(page))
        block = page[m.end() : stop]
        title = _TITLE.search(block)
        link = _LINK.search(block)
        body = _BODY.search(block)
        author = _AUTHOR.search(block)
        subreddit = _SUBREDDIT.search(block)
        flair = _FLAIR.search(block)
        when = _TIME.search(block)
        url = _html.unescape(link.group(1)) if link else ""
        if subreddit:
            name = _html.unescape(subreddit.group(2))
            sub = name if subreddit.group(1).lower() == "r" else f"u_{name}"
        else:
            sub = ""
        posts.append(
            Post(
                id=m.group(1).lower(),
                subreddit=sub,
                author=_text(author.group(1)) if author else "[deleted]",
                title=_text(title.group(2)) if title else "",
                score=_number(_SCORE.search(block)),
                num_comments=_number(_COMMENTS.search(block)),
                created_utc=_timestamp(when.group(1)) if when else 0.0,
                permalink=_web_url(title.group(1)) if title else "",
                url=url,
                is_self=not url,
                selftext=_text(body.group(1)) if body else "",
                flair=_html.unescape(flair.group(1)) or None if flair else None,
                over_18="nsfw-stamp" in block or 'class="nsfw' in block,
            )
        )
    cursor = None
    nxt = _NEXT.search(page)
    if nxt:
        query = parse_qs(urlsplit(_html.unescape(nxt.group(1))).query)
        after = (query.get("after") or [None])[0]
        count = (query.get("count") or ["0"])[0]
        if after:
            cursor = f"{after}:{count}"
    return posts, cursor


def split_cursor(cursor: str | None) -> tuple[str | None, str | None]:
    """'t3_x:25' -> ('t3_x', '25')."""
    if not cursor:
        return None, None
    after, _, count = cursor.partition(":")
    return after or None, count or None


# ------------------------------------------------------------ listing pages
#
# Reddit blocks /r/<sub>/new on both API doors for this server (and quietly
# ignores sort=new on /r/<sub>), but old.reddit.com's /r/<sub>/new/ page
# answers. Each post is a <div class="thing ..."> whose data-* attributes
# carry nearly everything (verified from Railway, 2026-11-21).

_THING = re.compile(r'<div class="([^"]*\bthing\b[^"]*)"([^>]*\bdata-fullname="t3_[a-z0-9]+"[^>]*)>', re.I)
_ATTR = re.compile(r'\bdata-([a-z-]+)="([^"]*)"', re.I)
_LISTING_TITLE = re.compile(r'<a class="[^"]*\btitle\b[^"]*"[^>]*>(.*?)</a>', re.S | re.I)


def _int(value: str | None) -> int:
    try:
        return int(float((value or "0").replace(",", "")))
    except ValueError:
        return 0


def parse_listing_page(page: str) -> tuple[list[Post], str | None]:
    """Posts on one old.reddit.com listing page (ads skipped), and the cursor
    for the next page ("t3_x:25") or None."""
    starts = list(_THING.finditer(page))
    posts: list[Post] = []
    for i, m in enumerate(starts):
        attrs = {k.lower(): _html.unescape(v) for k, v in _ATTR.findall(m.group(2))}
        if attrs.get("promoted") == "true" or attrs.get("type", "link") != "link":
            continue
        classes = m.group(1).split()
        stop = starts[i + 1].start() if i + 1 < len(starts) else len(page)
        block = page[m.end() : stop]
        title = _LISTING_TITLE.search(block)
        flair = _FLAIR.search(block)
        permalink = attrs.get("permalink", "")
        url = attrs.get("url", "")
        is_self = "self" in classes or url == permalink or url.startswith("/r/")
        posts.append(
            Post(
                id=attrs["fullname"].removeprefix("t3_").lower(),
                subreddit=attrs.get("subreddit", ""),
                author=attrs.get("author") or "[deleted]",
                title=_text(title.group(1)) if title else "",
                score=_int(attrs.get("score")),
                num_comments=_int(attrs.get("comments-count")),
                created_utc=_int(attrs.get("timestamp")) / 1000.0,
                permalink=WEB + permalink if permalink.startswith("/") else permalink,
                url="" if is_self else url,
                is_self=is_self,
                flair=(_html.unescape(flair.group(1)).strip() or None) if flair else None,
                over_18=attrs.get("nsfw") == "true",
                spoiler=attrs.get("spoiler") == "true",
                locked="locked" in classes,
                stickied="stickied" in classes,
            )
        )
    cursor = None
    nxt = _NEXT.search(page)
    if nxt:
        query = parse_qs(urlsplit(_html.unescape(nxt.group(1))).query)
        after = (query.get("after") or [None])[0]
        count = (query.get("count") or ["0"])[0]
        if after:
            cursor = f"{after}:{count}"
    return posts, cursor
