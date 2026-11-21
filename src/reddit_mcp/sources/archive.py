"""Arctic Shift archive (https://github.com/ArthurHeitmann/arctic_shift).

No account needed. Limits worth knowing, all surfaced to the caller as notes:
  * only chronological order (no hot/top/rising/relevance);
  * full-text search needs a subreddit or author;
  * scores and comment counts are frozen at ingest for roughly the first 36 hours.
"""

from __future__ import annotations

import time as _time
from typing import Any

import httpx

from ..config import TOOL_USER_AGENT, Settings
from ..errors import InvalidRequest, NotFound, NotSupported, Unavailable
from ..http import Pacer, send
from ..models import Comment, More, Page, Post, SubredditInfo, Thread
from ..parse import (
    LOCAL_SORTS,
    listing_children,
    parse_comment,
    parse_post,
    parse_subreddit,
    parse_tree,
    sort_tree,
    unwrap,
)

FRESH_SECONDS = 36 * 3600
MAX_SEARCH_LIMIT = 100  # Arctic Shift's cap for posts/comments search


def encode_cursor(items: list, newest_first: bool = True) -> str:
    """'<boundary second>:<ids shown at that second>' for the next page."""
    stamps = [int(i.created_utc) for i in items]
    edge = min(stamps) if newest_first else max(stamps)
    shown = [i.id for i in items if int(i.created_utc) == edge]
    return f"{edge}:{','.join(shown)}"


def decode_cursor(cursor: str | None) -> tuple[int | None, set[str]]:
    """Accepts the current form and a bare timestamp (older cursors)."""
    if not cursor:
        return None, set()
    stamp, _, ids = cursor.partition(":")
    try:
        edge = int(float(stamp))
    except ValueError:
        raise InvalidRequest(f"invalid archive cursor {cursor!r}; pass next_cursor back unchanged") from None
    return edge, {i for i in ids.split(",") if i}
TIME_TO_OFFSET = {"hour": "1hour", "day": "1d", "week": "7d", "month": "30d", "year": "1year"}
MAX_IDS = 100


def _error_text(response: httpx.Response) -> str:
    try:
        body = response.json()
    except ValueError:
        body = response.text
    if isinstance(body, dict):
        body = body.get("error") or body.get("message") or body
    text = str(body).strip()
    return text[:300] if text else f"HTTP {response.status_code}"


class ArchiveSource:
    name = "archive"
    label = "Arctic Shift archive"

    def __init__(self, settings: Settings, client: httpx.AsyncClient | None = None) -> None:
        self._client = client or httpx.AsyncClient(
            base_url=settings.archive_url,
            timeout=settings.timeout,
            headers={"User-Agent": TOOL_USER_AGENT, "Accept": "application/json"},
        )
        self._pacer = Pacer(settings.archive_interval)
        self.now = _time.time

    async def aclose(self) -> None:
        await self._client.aclose()

    # ------------------------------------------------------------------ http

    async def _get(self, path: str, params: dict[str, Any]) -> list[Any]:
        clean = {k: v for k, v in params.items() if v is not None and v != ""}
        response = await send(
            lambda: self._client.get(path, params=clean), self._pacer, label=self.label
        )
        if response.status_code == 400:
            raise NotSupported(f"archive rejected the query: {_error_text(response)}")
        if response.status_code == 404:
            raise NotFound("not found in the archive")
        if response.status_code == 422:
            raise NotSupported(
                "the archive refused this query (HTTP 422): keyword search on a very active "
                "subreddit or user needs a bounded window; pass both after and before "
                "(a few months apart) and page through with next_cursor"
            )
        if response.status_code >= 400:
            raise Unavailable(f"archive returned HTTP {response.status_code}")
        try:
            body = response.json()
        except ValueError:
            raise Unavailable("archive returned a non-JSON response") from None
        if isinstance(body, dict) and body.get("error"):
            message = str(body["error"])[:300]
            if "timed out" in message.lower():
                raise Unavailable(f"archive query timed out: {message}")
            raise NotSupported(f"archive rejected the query: {message}")
        data = body.get("data") if isinstance(body, dict) else body
        return data if isinstance(data, list) else []

    # --------------------------------------------------------------- helpers

    def _fresh_note(self, created: list[float]) -> list[str]:
        cutoff = self.now() - FRESH_SECONDS
        fresh = sum(1 for ts in created if ts > cutoff)
        if not fresh:
            return []
        return [
            f"{fresh} item(s) are under 36h old; the archive stores their score (and a "
            "post's comment count) from ingest time, so those numbers are likely 0 or 1."
        ]

    @staticmethod
    def _tree_times(nodes: list) -> list[float]:
        """created_utc of every comment in a tree (collapsed placeholders skipped)."""
        times: list[float] = []
        for node in nodes:
            if isinstance(node, Comment):
                times.append(node.created_utc)
                times += ArchiveSource._tree_times(node.replies)
        return times

    @staticmethod
    def _time_offset(time: str | None) -> str | None:
        return TIME_TO_OFFSET.get(time or "all")

    def _page(
        self, items: list, limit: int, notes: list[str] | None = None, *, full: bool | None = None,
        newest_first: bool = True,
    ) -> Page:
        created = [i.created_utc for i in items]
        more = (len(items) >= limit) if full is None else full
        return Page(
            items=items,
            next_cursor=encode_cursor(items, newest_first) if more and items else None,
            source=self.label,
            notes=[*(notes or []), *self._fresh_note(created)],
        )

    async def _fetch(self, path: str, params: dict, limit: int, cursor: str | None, *, newest_first: bool, parse):
        """One page of a time-ordered search, continuing after ``cursor``.

        Arctic Shift's before/after are exclusive and timestamps have whole
        seconds, so a plain timestamp cursor would drop the items that share the
        boundary second. The cursor therefore also carries the ids already shown
        at that second: the next page asks for the boundary second again and
        leaves those out. Returns (items, whether the source had a full page).
        """
        edge, seen = decode_cursor(cursor)
        # Ask for the already-shown boundary items too, so the page still has
        # `limit` new items after they are dropped.
        want = min(MAX_SEARCH_LIMIT, limit + len(seen))
        query = dict(params, limit=want, sort="desc" if newest_first else "asc")
        bound = "before" if newest_first else "after"
        if edge is not None:
            query[bound] = (edge + 1 if newest_first else edge - 1) if seen else edge
        rows = await self._get(path, query)
        full = len(rows) >= want
        items = [parse(r) for r in rows]
        if seen:
            items = [i for i in items if not (int(i.created_utc) == edge and i.id in seen)]
            if not items and full:
                # More than a page of items share that one second; skip past it.
                rows = await self._get(path, dict(query, limit=limit, **{bound: edge}))
                items = [parse(r) for r in rows]
                full = len(rows) >= limit
        if len(items) > limit:
            items, full = items[:limit], True
        return items, full

    async def _post_by_id(self, post_id: str) -> Post | None:
        rows = await self._get("/api/posts/ids", {"ids": post_id})
        return parse_post(rows[0]) if rows else None

    # ------------------------------------------------------------------- api

    async def browse(self, subreddit, sort, time, limit, cursor) -> Page[Post]:
        sort = sort or "new"
        if sort != "new":
            raise NotSupported(f"the archive has no '{sort}' ranking, only sort=new")
        notes = []
        after = self._time_offset(time)
        if after:
            notes.append(f"time={time} applied as 'posted within the last {after}'")
        items, full = await self._fetch(
            "/api/posts/search", {"subreddit": subreddit, "after": after}, limit, cursor,
            newest_first=True, parse=parse_post,
        )
        page = self._page(items, limit, notes, full=full)
        page.applied = {"sort": sort}
        return page

    async def search(
        self, query, subreddit, author, sort, time, after, before, limit, cursor
    ) -> Page[Post]:
        if not (query or subreddit or author):
            raise InvalidRequest("give at least one of query, subreddit or author")
        sort = sort or "new"
        if sort not in ("new", "old"):
            raise NotSupported(f"the archive cannot sort by '{sort}', only new or old")
        if query and not (subreddit or author):
            raise NotSupported("archive full-text search needs a subreddit or author")
        notes = []
        offset = self._time_offset(time)
        if offset and after:
            notes.append(f"time={time} ignored because after={after} was given")
        elif offset:
            after = offset
            notes.append(f"time={time} applied as 'posted within the last {after}'")
        newest_first = sort == "new"
        params = {"query": query, "subreddit": subreddit, "author": author, "after": after, "before": before}
        items, full = await self._fetch(
            "/api/posts/search", params, limit, cursor, newest_first=newest_first, parse=parse_post
        )
        page = self._page(items, limit, notes, full=full, newest_first=newest_first)
        page.applied = {"sort": sort}
        return page

    def _sort_note(self, sort: str) -> tuple[str, list[str]]:
        if sort in LOCAL_SORTS:
            return sort, []
        return "top", [f"the archive cannot rank comments by '{sort}'; sorted by score instead"]

    @staticmethod
    def _truncated(nodes: list) -> bool:
        for node in nodes:
            if isinstance(node, More) or (isinstance(node, Comment) and ArchiveSource._truncated(node.replies)):
                return True
        return False

    def _partial_sort_note(self, nodes: list, how: str) -> list[str]:
        """The archive picks which comments to return; sorting happens after."""
        if not self._truncated(nodes):
            return []
        return [
            f"the archive returned part of the thread; sort={how} orders only the comments shown, "
            "collapsed ones may rank higher (expand them, or raise comment_limit)"
        ]

    async def get_post(self, post_id, comment_limit, comment_depth, comment_sort) -> Thread:
        post = await self._post_by_id(post_id)
        if post is None:
            raise NotFound(f"post {post_id} not found in the archive")
        if comment_limit == 0:
            return Thread(post=post, comments=[], source=self.label, notes=self._fresh_note([post.created_utc]))
        rows = await self._get(
            "/api/comments/tree",
            {
                "link_id": f"t3_{post_id}",
                "limit": comment_limit,
                "start_depth": comment_depth,
                "start_breadth": comment_limit,
            },
        )
        how, notes = self._sort_note(comment_sort)
        comments = sort_tree(parse_tree(rows, comment_depth), how)
        notes += self._partial_sort_note(comments, how)
        notes += self._fresh_note([post.created_utc, *self._tree_times(comments)])
        return Thread(post=post, comments=comments, source=self.label, notes=notes)

    async def get_thread(self, post_id, comment_id, depth, limit, sort) -> Thread:
        found = await self._get("/api/comments/ids", {"ids": comment_id})
        if not found:
            raise NotFound(f"comment {comment_id} not found in the archive")
        focus_raw = found[0]
        link_id = str(focus_raw.get("link_id") or "")
        post_id = post_id or link_id.removeprefix("t3_")
        if not post_id:
            raise NotFound(f"cannot tell which post comment {comment_id} belongs to")
        rows = await self._get(
            "/api/comments/tree",
            {
                "link_id": f"t3_{post_id}",
                "parent_id": f"t1_{comment_id}",
                "limit": limit,
                "start_depth": depth,
                "start_breadth": limit,
            },
        )
        # The tree may come back as the focus comment's replies, or as a subtree
        # rooted at the focus comment itself. Handle both.
        for row in rows:
            if str(unwrap(row).get("id")) == comment_id:
                rows = listing_children(unwrap(row).get("replies"))
                break
        how, notes = self._sort_note(sort)
        focus = parse_comment(focus_raw, sort_tree(parse_tree(rows, depth), how))
        notes += self._partial_sort_note(focus.replies, how)
        post = await self._post_by_id(post_id)
        times = [post.created_utc] if post is not None else []
        notes += self._fresh_note([*times, *self._tree_times([focus])])
        return Thread(
            post=post, comments=[focus], focus_comment_id=comment_id, source=self.label, notes=notes
        )

    async def get_comments(self, comment_ids, post_id=None) -> Page[Comment]:  # post_id not needed here
        ids = list(comment_ids)[:MAX_IDS]
        rows = await self._get("/api/comments/ids", {"ids": ",".join(ids)})
        items = [parse_comment(r) for r in rows]
        notes = []
        missing = set(ids) - {c.id for c in items}
        if missing:
            notes.append(f"{len(missing)} id(s) not in the archive: {', '.join(sorted(missing))}")
        return Page(
            items=items,
            source=self.label,
            notes=[*notes, *self._fresh_note([c.created_utc for c in items])],
        )

    async def user_activity(self, username, kind, limit, cursor) -> Page[Post | Comment]:
        params = {"author": username}
        items: list[Post | Comment] = []
        more = False
        if kind in ("posts", "overview"):
            posts, full = await self._fetch("/api/posts/search", params, limit, cursor, newest_first=True, parse=parse_post)
            items += posts
            more = more or full
        if kind in ("comments", "overview"):
            comments, full = await self._fetch(
                "/api/comments/search", params, limit, cursor, newest_first=True, parse=parse_comment
            )
            items += comments
            more = more or full
        items.sort(key=lambda i: i.created_utc, reverse=True)
        # Overview merges two streams: what is cut here comes back on the next
        # page, because the cursor is built from what was actually shown.
        return self._page(items[:limit], limit, full=more or len(items) > limit)

    async def subreddit_info(self, name) -> SubredditInfo:
        rows = await self._get("/api/subreddits/search", {"subreddit": name, "limit": 1})
        if not rows:
            raise NotFound(f"r/{name} not found in the archive")
        sub = parse_subreddit(rows[0])
        notes = ["subscriber count is from the archive's last snapshot, see 'as of'"]
        return SubredditInfo(subreddit=sub, source=self.label, notes=notes)

    async def find_subreddits(self, query, limit) -> Page:
        rows = await self._get(
            "/api/subreddits/search",
            {"subreddit_prefix": query, "limit": limit, "sort_type": "subscribers", "sort": "desc"},
        )
        return Page(
            items=[parse_subreddit(r) for r in rows],
            source=self.label,
            notes=["the archive matches subreddit names by prefix only, not by topic"],
        )
