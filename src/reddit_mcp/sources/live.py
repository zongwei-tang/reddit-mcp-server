"""Live reddit.com through the user's own logged-in browser session cookie.

The cookie is only ever placed in the request header. It is never logged,
never included in an error message, and never part of this object's repr.
"""

from __future__ import annotations

import re
import time as _time
from typing import Any
from urllib.parse import parse_qsl

import httpx

from .. import oldsearch
from ..config import Settings
from ..errors import InvalidRequest, NotFound, NotSupported, Unavailable
from ..http import Pacer, send
from ..models import Comment, Page, Post, SubredditInfo, Thread
from ..parse import (
    listing_children,
    parse_comment,
    parse_post,
    parse_subreddit,
    parse_tree,
    unwrap,
)

COMMENT_SORT_PARAM = {
    "best": "confidence",
    "top": "top",
    "new": "new",
    "old": "old",
    "controversial": "controversial",
    "qa": "qa",
}
ACTIVITY_PATH = {"overview": "overview", "posts": "submitted", "comments": "comments"}
MAX_IDS = 100
BLOCK_MARKERS = ("blocked by network security", "whoa there, pardner", "your request has been blocked")


OLD_WEB = "https://old.reddit.com"
DEAD_DOOR_SECONDS = 600.0
LIVE_ID_BATCH = 10  # comments fetched one request each; keep a call under ~15 s


class Blocked(Unavailable):
    """Reddit's edge refused this endpoint ("blocked by network security")."""


def endpoint_kind(path: str) -> str:
    """Group API paths by kind, e.g. /r/x/hot.json -> /r/*/hot, /comments/a/_/b.json -> /comments."""
    p = path.split("?")[0].removesuffix(".json")
    p = re.sub(r"^/comments/.*", "/comments", p)
    p = re.sub(r"^/r/[^/]+", "/r/*", p)
    p = re.sub(r"^/user/[^/]+", "/user/*", p)
    return p


def is_block_page(response: httpx.Response) -> bool:
    # Reddit's block page answers "403 Blocked" and puts its message after a
    # large inline stylesheet, so look at the reason phrase and the whole body.
    # Only a 403 can be one: a normal page (say, search results that quote the
    # phrase) is never mistaken for a block.
    if response.status_code != 403:
        return False
    if response.reason_phrase.lower() == "blocked":
        return True
    text = response.text.lower() if response.content else ""
    return any(marker in text for marker in BLOCK_MARKERS)


def is_login_path(path: str) -> bool:
    """/login, /login/..., /account/login: not /r/loginproblems."""
    p = path.lower().rstrip("/")
    return p in ("/login", "/account/login") or p.startswith(("/login/", "/account/login/"))


def redirected_elsewhere(response: httpx.Response) -> str | None:
    """Where a followed redirect ended, if it left the requested resource.

    The browser's fetch() follows redirects itself (the transport reports the
    final URL), so this is the browser-mode counterpart of a 3xx: a request for
    a subreddit that does not exist ends on Reddit's subreddit search, and a
    logged-out session ends on the login page.
    """
    final = response.headers.get("x-final-url")
    if not final:
        return None
    path = httpx.URL(final).path
    if is_login_path(path):
        return path
    requested = response.request.url.path.lower()
    if path.lower().startswith("/subreddits/search") and not requested.startswith("/subreddits/search"):
        return path
    return None


def make_transport(settings: Settings, *, read_only_store: bool = False) -> httpx.AsyncBaseTransport | None:
    """The transport for reddit.com requests: default httpx, or a real browser.

    With REDDIT_SESSION_STORE set, the browser starts from the session saved
    there. Only the server's own browser saves to it; other browsers (the
    startup checks) pass ``read_only_store`` so they use it without changing it.
    """
    if settings.live_client != "browser":
        return None
    from ..browser import BrowserTransport
    from ..session_store import SessionStore

    store = SessionStore(settings.session_store, read_only=read_only_store) if settings.session_store else None
    return BrowserTransport(settings.cookie, store=store)


DIAG_CHECKS = (
    ("www.reddit.com", "/api/me.json", False),
    ("www.reddit.com", "/r/popular/hot.json?limit=1", False),
    ("old.reddit.com", "/api/me.json", False),
    ("old.reddit.com", "/r/popular/hot.json?limit=1", False),
    # Read paths that might not be treated as listing scrapes:
    ("www.reddit.com", "/api/info.json?id=t5_2qh0y", False),
    ("www.reddit.com", "/r/Python/about.json", False),
    ("www.reddit.com", "/comments/1y1lfmh.json?limit=1", False),
    # The reddit.com web app reads through oauth.reddit.com with the session's
    # own token_v2 as bearer token; probe the same thing.
    ("oauth.reddit.com", "/r/popular/hot?limit=1", True),
    ("oauth.reddit.com", "/api/v1/me", True),
    ("oauth.reddit.com", "/r/Python/search?q=asyncio&restrict_sr=1&limit=1", True),
    # Server-rendered search page, a possible fallback when the JSON search is blocked.
    ("old.reddit.com", "/r/Python/search?q=asyncio&restrict_sr=on", False),
)


def _cookie_value(cookie: str | None, name: str) -> str | None:
    for part in (cookie or "").split(";"):
        key, sep, value = part.strip().partition("=")
        if sep and key == name:
            return value
    return None


async def diagnose(settings: Settings, transport: httpx.AsyncBaseTransport | None = None) -> list[str]:
    """Probe reddit.com the way the live source does and describe each answer.

    Reports status, content type, whether it is Reddit's block page, and
    whether Reddit recognized a logged-in user. Never echoes the cookie.
    """
    lines = [f"client: {settings.live_client}"]
    own_transport = transport is None and settings.live_client == "browser"
    if own_transport:
        transport = make_transport(settings, read_only_store=True)
    async with httpx.AsyncClient(
        timeout=max(settings.timeout, 60.0) if settings.live_client == "browser" else settings.timeout,
        follow_redirects=False,
        transport=transport,
        headers={
            "User-Agent": settings.user_agent,
            "Accept": "application/json",
            "Cookie": settings.cookie or "",
        },
    ) as client:
        # In browser mode the transport adds the browser's *current* token_v2
        # to oauth requests; the one in the configured cookie may be a day old.
        bearer = None if settings.live_client == "browser" else _cookie_value(settings.cookie, "token_v2")
        for host, path, use_bearer in DIAG_CHECKS:
            label = f"{host}{path.split('?')[0]}"
            headers = {}
            if use_bearer and settings.live_client != "browser":
                if not bearer:
                    lines.append(f"{label}: skipped (no token_v2 cookie)")
                    continue
                headers["Authorization"] = f"Bearer {bearer}"
            # httpx replaces a URL's query string with `params`, so keep the
            # probe's own query (q=..., limit=...) by merging it in.
            bare, _, query = path.partition("?")
            params = dict(parse_qsl(query))
            params["raw_json"] = "1"
            try:
                r = await client.get(f"https://{host}{bare}", params=params, headers=headers)
            except httpx.HTTPError as exc:
                lines.append(f"{label}: network error {type(exc).__name__}")
                continue
            kind = r.headers.get("content-type", "?").split(";")[0]
            bits = [f"HTTP {r.status_code}", kind]
            if is_block_page(r):
                bits.append("BLOCK PAGE")
            if r.status_code >= 400:
                snippet = " ".join(r.text[:200].split())
                bits.append(f"body: {snippet!r}")
            elif "html" in kind:
                posts, _ = oldsearch.parse_search_page(r.text)
                bits.append(f"html, {len(posts)} search result(s) parsed")
            if 300 <= r.status_code < 400:
                bits.append(f"redirect -> {httpx.URL(r.headers.get('location', '')).path or '?'}")
            if "json" in kind:
                try:
                    data = r.json()
                except ValueError:
                    data = None
                if path.startswith("/api/v1/me"):
                    bits.append(f"logged in as u/{data['name']}" if isinstance(data, dict) and data.get("name") else "not logged in")
                elif path.startswith("/api/me"):
                    inner = data.get("data") if isinstance(data, dict) else None
                    if isinstance(inner, dict) and inner.get("name"):
                        bits.append(f"logged in as u/{inner['name']}")
                        bits.append("modhash present" if inner.get("modhash") else "no modhash")
                    else:
                        bits.append("not logged in")
                elif r.status_code == 200:
                    bits.append("data OK")
            lines.append(f"{label}: " + ", ".join(bits))
    names = sorted(
        {p.split("=", 1)[0].strip() for p in (settings.cookie or "").split(";") if "=" in p}
    )
    known = [n for n in ("reddit_session", "token_v2", "loid", "csrf_token", "edgebucket", "session_tracker") if n in names]
    lines.append(
        f"user agent: {'browser-like' if 'Mozilla/' in settings.user_agent else 'tool default'}; "
        f"cookie: {'set' if settings.cookie else 'missing'}"
        + (f" ({len(names)} cookie(s); includes: {', '.join(known) or 'none of the usual ones'})" if settings.cookie else "")
    )
    return lines


class LiveSource:
    name = "live"
    label = "reddit.com (logged-in session)"

    def __init__(
        self,
        settings: Settings,
        client: httpx.AsyncClient | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        if not settings.cookie:
            raise ValueError("LiveSource needs a session cookie")
        self._oauth = settings.uses_oauth_api
        self.transport = transport  # shared with the writer and diagnose_live
        self._www = settings.live_url
        self._door: dict[str, tuple[str, ...]] = {}
        self._dead: dict[str, tuple[float, str]] = {}
        self._client = client or httpx.AsyncClient(
            base_url=settings.live_api_url,
            timeout=settings.timeout,
            follow_redirects=False,
            transport=transport,
            headers={
                "User-Agent": settings.user_agent,
                "Accept": "application/json",
                "Cookie": settings.cookie,
            },
        )
        self._pacer = Pacer(settings.live_interval)

    def __repr__(self) -> str:
        return "LiveSource(<session cookie hidden>)"

    async def aclose(self) -> None:
        await self._client.aclose()

    # ------------------------------------------------------------------ http

    async def _get(self, path: str, params: dict[str, Any] | None = None) -> Any:
        """GET a reddit.com API path (written with the .json suffix).

        In browser mode there are two front doors: oauth.reddit.com (bearer
        token) and www.reddit.com (cookie). Reddit blocks different endpoints
        on each, so try both and remember per endpoint kind which one works.
        A block page means the request was not processed, so retrying is safe.
        """
        if not self._oauth:
            return await self._get_at(path, params)
        kind = endpoint_kind(path)
        dead = self._dead.get(kind)
        if dead and dead[0] > _time.monotonic():
            raise Blocked(f"{dead[1]} (remembered for a few minutes)")
        order = self._door.get(kind) or ("oauth", "www")
        blocked: Blocked | None = None
        for door in order:
            target = path[: -len(".json")] if door == "oauth" and path.endswith(".json") else path
            if door == "www":
                target = self._www + target
            try:
                body = await self._get_at(target, params)
            except Blocked as exc:
                blocked = exc
                continue
            self._door[kind] = (door, *[d for d in order if d != door])
            self._dead.pop(kind, None)
            return body
        blocked = blocked or Blocked("reddit.com blocked the request")
        # Both doors refused this kind of endpoint: skip it for a while so
        # callers fall back to the archive at once instead of paying two
        # blocked round trips every time.
        self._dead[kind] = (_time.monotonic() + DEAD_DOOR_SECONDS, str(blocked))
        raise blocked

    async def _get_at(self, path: str, params: dict[str, Any] | None = None) -> Any:
        clean = {k: v for k, v in (params or {}).items() if v is not None and v != ""}
        clean["raw_json"] = 1
        response = await send(
            lambda: self._client.get(path, params=clean), self._pacer, label="reddit.com"
        )
        status = response.status_code
        if 300 <= status < 400:
            location = response.headers.get("location", "")
            target = httpx.URL(location) if location else None
            if target is not None and (
                is_login_path(target.path)
                or (target.host and not target.host.endswith("reddit.com"))
            ):
                raise Unavailable(
                    "reddit.com redirected to a login page; "
                    "the session cookie is probably missing or expired"
                )
            raise NotFound(
                f"reddit.com redirected this request (HTTP {status}); "
                "the subreddit or user may not exist or may be renamed"
            )
        # Checked before anything else, so a redirect to a page that happens
        # to be blocked is reported as what it is, not as a blocked endpoint.
        ended = redirected_elsewhere(response)
        if ended is not None:
            if is_login_path(ended):
                raise Unavailable(
                    "reddit.com sent the request to its login page; "
                    "the session cookie is probably missing or expired"
                )
            raise NotFound(
                f"reddit.com redirected this request to {ended}: "
                "the subreddit probably does not exist (or is banned or renamed)"
            )
        body: Any = None
        is_json = "json" in response.headers.get("content-type", "")
        if is_json:
            try:
                body = response.json()
            except ValueError:
                body = None
        if status in (401, 403):
            reason = body.get("reason") if isinstance(body, dict) else None
            if reason:
                raise NotFound(f"reddit.com refused access: {reason}")
            if is_block_page(response):
                raise Blocked(
                    f"reddit.com 'blocked by network security' page (HTTP {status}) on "
                    f"{response.request.url.host}: Reddit refused this endpoint from this server"
                )
            raise Unavailable(
                f"reddit.com refused the request (HTTP {status}); "
                "the session cookie may be expired or the request was blocked"
            )
        if status == 404:
            raise NotFound("not found on reddit.com")
        if status >= 400:
            raise Unavailable(f"reddit.com returned HTTP {status}")
        if not is_json or body is None:
            raise Unavailable(
                "reddit.com returned a web page instead of JSON (likely a block or login page)"
            )
        return body

    @staticmethod
    def _listing(body: Any) -> tuple[list[Any], str | None]:
        if not isinstance(body, dict):
            return [], None
        data = body.get("data") or {}
        return listing_children(body), data.get("after")

    @staticmethod
    def _mixed(children: list[Any]) -> list[Post | Comment]:
        items: list[Post | Comment] = []
        for child in children:
            kind = child.get("kind") if isinstance(child, dict) else None
            if kind == "t3":
                items.append(parse_post(child))
            elif kind == "t1":
                items.append(parse_comment(child))
        return items

    # ------------------------------------------------------------------- api

    async def browse(self, subreddit, sort, time, limit, cursor) -> Page[Post]:
        sort = sort or "hot"
        if sort == "new" and self._oauth:
            if time and time != "all":
                raise NotSupported("time filter only applies to sort=top or controversial on reddit.com")
            # Browser mode: /r/<sub>/new is blocked on both API doors and
            # sort=new is ignored elsewhere; old.reddit.com's page answers.
            return await self._new_page(subreddit, limit, cursor)
        params = {"limit": limit, "after": cursor}
        if sort in ("top", "controversial"):
            params["t"] = time or "day"
        elif time and time != "all":
            raise NotSupported(f"time filter only applies to sort=top or controversial on reddit.com")
        body = await self._get(f"/r/{subreddit}/{sort}.json", params)
        children, after = self._listing(body)
        return Page(
            # Only posts: a listing of anything else (say, subreddits) is not an answer.
            items=[parse_post(c) for c in children if isinstance(c, dict) and c.get("kind") == "t3"],
            next_cursor=after,
            source=self.label,
            applied={"sort": sort},
        )

    async def search(
        self, query, subreddit, author, sort, time, after, before, limit, cursor
    ) -> Page[Post]:
        if after or before:
            raise NotSupported("reddit.com search has no date-range filter")
        sort = sort or "relevance"
        if sort == "old":
            raise NotSupported("reddit.com search cannot sort oldest-first")
        terms = [t for t in (query, f"author:{author}" if author else None) if t]
        if not terms:
            if subreddit:
                # Not a bad request: the archive can list a subreddit's posts by time.
                raise NotSupported("reddit.com search needs a query or an author (or use browse_subreddit)")
            raise InvalidRequest("give at least one of query, subreddit or author")
        params = {
            "q": " ".join(terms),
            "sort": sort,
            "t": time,
            "limit": limit,
            "after": cursor,
            "type": "link",
        }
        if self._oauth:
            # Browser mode: Reddit blocks the JSON search endpoints on both
            # doors for this server, but old.reddit.com's search page answers.
            try:
                return await self._search_page(" ".join(terms), subreddit, sort, time, limit, cursor)
            except Blocked:
                pass  # try the JSON endpoints after all
        json_cursor, _ = oldsearch.split_cursor(cursor)
        params["after"] = json_cursor
        if subreddit:
            params["restrict_sr"] = 1
            path = f"/r/{subreddit}/search.json"
        else:
            path = "/search.json"
        body = await self._get(path, params)
        children, next_after = self._listing(body)
        return Page(
            items=[parse_post(c) for c in children],
            next_cursor=next_after,
            source=self.label,
            applied={"sort": sort},
        )

    async def _search_page(self, q, subreddit, sort, time, limit, cursor) -> Page[Post]:
        after, count = oldsearch.split_cursor(cursor)
        params = {
            "q": q,
            "sort": sort,
            "t": time,
            "limit": limit,
            "after": after,
            "count": count,
            "restrict_sr": "on" if subreddit else None,
            "type": "link",
        }
        url = f"{OLD_WEB}/r/{subreddit}/search" if subreddit else f"{OLD_WEB}/search"
        text = await self._old_page(url, params, "search", subreddit)
        posts, next_cursor = oldsearch.parse_search_page(text)
        if not posts and 'data-fullname="t3_' in text:
            # Results are on the page but the parser found none: the markup changed.
            raise Unavailable("could not read old.reddit.com's search results (page layout changed)")
        return Page(
            items=posts[:limit],
            next_cursor=next_cursor if len(posts) >= limit else None,
            source=self.label,
            applied={"sort": sort},
            notes=["search results read from old.reddit.com's search page"],
        )

    async def _old_page(self, url, params, what, subreddit) -> str:
        """One server-rendered old.reddit.com page, with the same error mapping
        as the API: block page, login redirect, missing subreddit."""
        clean = {k: v for k, v in params.items() if v is not None and v != ""}
        response = await send(lambda: self._client.get(url, params=clean), self._pacer, label="reddit.com")
        if is_block_page(response):
            raise Blocked(f"old.reddit.com {what} page: 'blocked by network security'")
        ended = redirected_elsewhere(response)
        if ended is not None:
            if is_login_path(ended):
                raise Unavailable(f"old.reddit.com sent the {what} page to its login page; the session cookie may be expired")
            raise NotFound(f"r/{subreddit} was redirected to {ended}: it probably does not exist")
        if response.status_code == 404:
            raise NotFound(f"r/{subreddit} not found on reddit.com")
        if response.status_code >= 400:
            raise Unavailable(f"old.reddit.com {what} page returned HTTP {response.status_code}")
        if "html" not in response.headers.get("content-type", ""):
            raise Unavailable(f"old.reddit.com {what} page did not return a web page")
        return response.text

    async def _new_page(self, subreddit, limit, cursor) -> Page[Post]:
        after, count = oldsearch.split_cursor(cursor)
        params = {"limit": limit, "after": after, "count": count}
        text = await self._old_page(f"{OLD_WEB}/r/{subreddit}/new/", params, "listing", subreddit)
        posts, next_cursor = oldsearch.parse_listing_page(text)
        if not posts and "thing" in text and 'data-fullname="t3_' in text:
            raise Unavailable("could not read old.reddit.com's listing (page layout changed)")
        return Page(
            items=posts[:limit],
            next_cursor=next_cursor if len(posts) >= limit else None,
            source=self.label,
            applied={"sort": "new"},
            notes=["newest posts read from old.reddit.com's listing page (no post bodies there)"],
        )

    async def get_post(self, post_id, comment_limit, comment_depth, comment_sort) -> Thread:
        body = await self._get(
            f"/comments/{post_id}.json",
            {"limit": comment_limit, "depth": comment_depth, "sort": COMMENT_SORT_PARAM[comment_sort]},
        )
        if not isinstance(body, list) or len(body) < 2:
            raise Unavailable("unexpected response shape from reddit.com")
        posts = listing_children(body[0])
        if not posts:
            raise NotFound(f"post {post_id} not found on reddit.com")
        comments = parse_tree(listing_children(body[1]), comment_depth) if comment_limit else []
        return Thread(post=parse_post(posts[0]), comments=comments, source=self.label)

    async def get_thread(self, post_id, comment_id, depth, limit, sort) -> Thread:
        if not post_id:
            info = await self._get("/api/info.json", {"id": f"t1_{comment_id}"})
            found = listing_children(info)
            if not found:
                raise NotFound(f"comment {comment_id} not found on reddit.com")
            post_id = str(unwrap(found[0]).get("link_id") or "").removeprefix("t3_")
        body = await self._get(
            f"/comments/{post_id}/_/{comment_id}.json",
            # depth + 1 because reddit counts the focus comment as the first level.
            {"limit": limit, "depth": depth + 1, "sort": COMMENT_SORT_PARAM[sort]},
        )
        if not isinstance(body, list) or len(body) < 2:
            raise Unavailable("unexpected response shape from reddit.com")
        posts = listing_children(body[0])
        nodes = parse_tree(listing_children(body[1]), depth + 1)
        focus = [n for n in nodes if isinstance(n, Comment) and n.id == comment_id]
        if not focus:
            raise NotFound(f"comment {comment_id} not found on reddit.com")
        return Thread(
            post=parse_post(posts[0]) if posts else None,
            comments=focus,
            focus_comment_id=comment_id,
            source=self.label,
        )

    async def get_comments(self, comment_ids, post_id=None) -> Page[Comment]:
        ids = list(comment_ids)[:MAX_IDS]
        if post_id and self._oauth:
            # /api/info and /api/morechildren are refused from the server, but a
            # single comment's own thread page answers: one request per comment.
            return await self._comments_one_by_one(post_id, ids)
        body = await self._get("/api/info.json", {"id": ",".join(f"t1_{i}" for i in ids)})
        items = [parse_comment(c) for c in listing_children(body)]
        notes = []
        missing = set(ids) - {c.id for c in items}
        if missing:
            notes.append(f"{len(missing)} id(s) not returned: {', '.join(sorted(missing))}")
        return Page(items=items, source=self.label, notes=notes)

    async def _comments_one_by_one(self, post_id: str, ids: list[str]) -> Page[Comment]:
        batch, rest = ids[:LIVE_ID_BATCH], ids[LIVE_ID_BATCH:]
        items: list[Comment] = []
        missing: list[str] = []
        for cid in batch:
            try:
                body = await self._get(f"/comments/{post_id}/_/{cid}.json", {"limit": 1, "depth": 1})
            except NotFound:
                missing.append(cid)
                continue
            nodes = parse_tree(listing_children(body[1]), 1) if isinstance(body, list) and len(body) > 1 else []
            found = next((n for n in nodes if isinstance(n, Comment) and n.id == cid), None)
            if found is None:
                missing.append(cid)
            else:
                items.append(found)
        notes = []
        if missing:
            notes.append(f"{len(missing)} id(s) not found on reddit.com: {', '.join(missing)}")
        if rest:
            notes.append(
                f"fetched the first {LIVE_ID_BATCH} live (one request each); call again for the other "
                f"{len(rest)}: get_comments_by_id(comment_ids={rest}, post='{post_id}')"
            )
        return Page(items=items, source=self.label, notes=notes)

    async def user_activity(self, username, kind, limit, cursor) -> Page[Post | Comment]:
        body = await self._get(
            f"/user/{username}/{ACTIVITY_PATH[kind]}.json",
            {"limit": limit, "after": cursor, "sort": "new"},
        )
        children, after = self._listing(body)
        return Page(items=self._mixed(children), next_cursor=after, source=self.label)

    async def subreddit_info(self, name) -> SubredditInfo:
        body = await self._get(f"/r/{name}/about.json")
        data = unwrap(body)
        if not data.get("display_name"):
            raise NotFound(f"r/{name} not found on reddit.com")
        return SubredditInfo(subreddit=parse_subreddit(data), source=self.label)

    async def find_subreddits(self, query, limit) -> Page:
        body = await self._get("/subreddits/search.json", {"q": query, "limit": limit})
        children, _ = self._listing(body)
        return Page(items=[parse_subreddit(c) for c in children], source=self.label)
