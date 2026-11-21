"""MCP server: eight read-only Reddit tools."""

from __future__ import annotations

from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from typing import Annotated, Literal

from mcp.server.fastmcp import Context, FastMCP
from mcp.server.fastmcp.exceptions import ToolError
from mcp.server.transport_security import TransportSecuritySettings
from mcp.types import ToolAnnotations
from pydantic import Field

from . import format as fmt
from . import ids
from .config import Settings
from .errors import SourceError
from .http import quiet_http_logs
from .router import Router

Source = Literal["auto", "live", "archive"]
BrowseSort = Literal["hot", "new", "top", "rising", "controversial"]
SearchSort = Literal["relevance", "hot", "top", "new", "old", "comments"]
CommentSort = Literal["best", "top", "new", "old", "controversial", "qa"]
TimeFilter = Literal["hour", "day", "week", "month", "year", "all"]
ActivityKind = Literal["overview", "posts", "comments"]

READ_ONLY = ToolAnnotations(readOnlyHint=True, idempotentHint=True, openWorldHint=True)

SOURCE_HELP = (
    "Data source. 'auto' tries live reddit.com (if a session cookie is configured) and falls "
    "back to the Arctic Shift archive; 'live' or 'archive' forces one."
)

INSTRUCTIONS = """\
Read-only access to Reddit. Two sources exist: live reddit.com through the user's
logged-in session (only if configured) and the Arctic Shift archive (always available).
Every answer starts with the source that produced it and any notes; read the notes,
they say when a request was approximated or fell back. The archive only orders posts
chronologically and its scores are unreliable for posts under ~36h old.
To page, pass next_cursor back unchanged. Collapsed comments show the exact follow-up
call (get_comments_by_id or get_comment_thread) to expand them."""


def _router(ctx: Context) -> Router:
    return ctx.request_context.lifespan_context


async def _call(ctx: Context, op: str, source: str | None, **kwargs):
    try:
        return await _router(ctx).run(op, source, **kwargs)
    except SourceError as exc:
        raise ToolError(str(exc)) from None


LOOPBACK = ("127.0.0.1", "localhost", "::1")


def build_server(
    router_factory: Callable[[], Router] | None = None,
    *,
    host: str = "127.0.0.1",
    shared_router: bool = False,
    stateless_http: bool = False,
    enable_writes: bool | None = None,
) -> FastMCP:
    """Build the FastMCP server.

    ``shared_router`` keeps one Router (HTTP clients, cache, pacing) for the whole
    process. HTTP mode needs it: the MCP lifespan runs once per HTTP session, or
    once per request when stateless, and a router per request would defeat the
    cache and the rate pacing.
    """
    factory = router_factory or (lambda: Router.from_settings(Settings.from_env()))
    shared: list[Router] = []

    @asynccontextmanager
    async def lifespan(_: FastMCP) -> AsyncIterator[Router]:
        if shared_router:
            if not shared:
                shared.append(factory())
            yield shared[0]
            return
        router = factory()
        try:
            yield router
        finally:
            await router.aclose()

    extra = {}
    if host not in LOOPBACK:
        # DNS-rebinding protection guards servers on localhost against web pages.
        # A public server is protected by the bearer token instead (see http_app).
        extra["transport_security"] = TransportSecuritySettings(
            enable_dns_rebinding_protection=False
        )
    mcp = FastMCP(
        "reddit",
        instructions=INSTRUCTIONS,
        lifespan=lifespan,
        host=host,
        stateless_http=stateless_http,
        json_response=stateless_http,
        **extra,
    )
    quiet_http_logs()  # after FastMCP, which configures logging at INFO

    @mcp.tool(annotations=READ_ONLY, structured_output=False)
    async def browse_subreddit(
        ctx: Context,
        subreddit: Annotated[str, Field(description="Subreddit name, with or without r/")],
        sort: Annotated[
            BrowseSort | None,
            Field(description="Ranking. Default: hot on live, new on archive (archive has only new)."),
        ] = None,
        time: Annotated[
            TimeFilter | None, Field(description="Time window for top/controversial.")
        ] = None,
        limit: Annotated[int, Field(ge=1, le=100)] = 15,
        cursor: Annotated[str | None, Field(description="next_cursor from a previous call")] = None,
        source: Annotated[Source, Field(description=SOURCE_HELP)] = "auto",
    ) -> str:
        """List posts in a subreddit."""
        try:
            name = ids.subreddit_name(subreddit)
        except SourceError as exc:
            raise ToolError(str(exc)) from None
        # sort=None lets each source pick its own default, so a fallback from
        # live to the archive still works when the caller did not ask for 'hot'.
        page = await _call(
            ctx, "browse", source, subreddit=name, sort=sort, time=time, limit=limit, cursor=cursor
        )
        used = page.applied.get("sort", sort or "default")
        return fmt.render_posts(page, f"r/{name} · {used}" + (f" · {time}" if time else ""))

    @mcp.tool(annotations=READ_ONLY, structured_output=False)
    async def search_posts(
        ctx: Context,
        query: Annotated[str | None, Field(description="Keywords. Live supports reddit search syntax.")] = None,
        subreddit: Annotated[str | None, Field(description="Restrict to one subreddit")] = None,
        author: Annotated[str | None, Field(description="Restrict to one author")] = None,
        sort: Annotated[
            SearchSort | None,
            Field(
                description="Default: relevance on live, new on archive. "
                "relevance/hot/top/comments need live; new works on both; old only on the archive."
            ),
        ] = None,
        time: Annotated[TimeFilter | None, Field(description="Time window")] = None,
        after: Annotated[
            str | None,
            Field(description="Archive only: posted after this date (2024-01-31, epoch, or '30d')"),
        ] = None,
        before: Annotated[str | None, Field(description="Archive only: posted before this date")] = None,
        limit: Annotated[int, Field(ge=1, le=100)] = 15,
        cursor: Annotated[str | None, Field(description="next_cursor from a previous call")] = None,
        source: Annotated[Source, Field(description=SOURCE_HELP)] = "auto",
    ) -> str:
        """Search posts by keywords, subreddit, author and date range."""
        try:
            sub = ids.subreddit_name(subreddit) if subreddit else None
            user = ids.username(author) if author else None
        except SourceError as exc:
            raise ToolError(str(exc)) from None
        page = await _call(
            ctx,
            "search",
            source,
            query=(query or "").strip() or None,
            subreddit=sub,
            author=user,
            sort=sort,
            time=time,
            after=after,
            before=before,
            limit=limit,
            cursor=cursor,
        )
        scope = " ".join(x for x in (f"r/{sub}" if sub else "", f"u/{user}" if user else "") if x)
        used = page.applied.get("sort", sort or "default")
        label = f"search “{query.strip()}”" if query and query.strip() else "search"
        title = " ".join(x for x in (label, scope) if x) + f" · {used}"
        return fmt.render_posts(page, title)

    @mcp.tool(annotations=READ_ONLY, structured_output=False)
    async def get_post(
        ctx: Context,
        post: Annotated[str, Field(description="Post id (abc123 or t3_abc123) or post URL")],
        comment_limit: Annotated[int, Field(ge=0, le=500, description="Max comments to load")] = 60,
        comment_depth: Annotated[
            int, Field(ge=1, le=10, description="Reply levels to show; deeper ones are collapsed")
        ] = 4,
        comment_sort: Annotated[CommentSort, Field(description="Comment order")] = "best",
        source: Annotated[Source, Field(description=SOURCE_HELP)] = "auto",
    ) -> str:
        """Get a post with its comment tree."""
        try:
            pid = ids.post_id(post)
        except SourceError as exc:
            raise ToolError(str(exc)) from None
        thread = await _call(
            ctx,
            "get_post",
            source,
            post_id=pid,
            comment_limit=comment_limit,
            comment_depth=comment_depth,
            comment_sort=comment_sort,
        )
        return fmt.render_thread(thread, f"post {pid}")

    @mcp.tool(annotations=READ_ONLY, structured_output=False)
    async def get_comment_thread(
        ctx: Context,
        comment_id: Annotated[str, Field(description="Comment id (t1_ optional) or comment URL")],
        post: Annotated[str | None, Field(description="Post id or URL, if known (saves a request)")] = None,
        depth: Annotated[int, Field(ge=1, le=10, description="Reply levels below the comment")] = 4,
        limit: Annotated[int, Field(ge=1, le=500)] = 60,
        sort: Annotated[CommentSort, Field(description="Reply order")] = "best",
        source: Annotated[Source, Field(description=SOURCE_HELP)] = "auto",
    ) -> str:
        """Get one comment and the replies under it (expands 'deeper replies')."""
        try:
            cid = ids.comment_id(comment_id)
            # A comment URL already names its post.
            pid = ids.post_id(post) if post else ids.post_of_comment_url(comment_id)
        except SourceError as exc:
            raise ToolError(str(exc)) from None
        router = _router(ctx)
        if pid is None and router.live is not None and source != "archive":
            # Live needs the post id, and Reddit refuses the id lookup from the
            # server; the archive knows which post a comment belongs to.
            try:
                found = await router.run("get_comments", "archive", comment_ids=(cid,))
                if found.items and found.items[0].link_id:
                    pid = found.items[0].link_id.removeprefix("t3_")
            except SourceError:
                pass
        thread = await _call(
            ctx, "get_thread", source, post_id=pid, comment_id=cid, depth=depth, limit=limit, sort=sort
        )
        return fmt.render_thread(thread, f"comment {cid}")

    @mcp.tool(annotations=READ_ONLY, structured_output=False)
    async def get_comments_by_id(
        ctx: Context,
        comment_ids: Annotated[
            list[str], Field(min_length=1, max_length=100, description="Comment ids, t1_ optional")
        ],
        post: Annotated[
            str | None,
            Field(description="The post these comments belong to (lets live reddit.com answer)"),
        ] = None,
        source: Annotated[Source, Field(description=SOURCE_HELP)] = "auto",
    ) -> str:
        """Fetch specific comments by id (expands 'more replies collapsed')."""
        try:
            wanted = tuple(dict.fromkeys(ids.comment_id(c) for c in comment_ids))
            pid = ids.post_id(post) if post else None
            if pid is None:
                pid = next((p for p in map(ids.post_of_comment_url, comment_ids) if p), None)
        except SourceError as exc:
            raise ToolError(str(exc)) from None
        page = await _call(ctx, "get_comments", source, comment_ids=wanted, post_id=pid)
        return fmt.render_comments(page, f"{len(wanted)} comment(s)")

    @mcp.tool(annotations=READ_ONLY, structured_output=False)
    async def get_user_activity(
        ctx: Context,
        username: Annotated[str, Field(description="Username, with or without u/")],
        kind: Annotated[ActivityKind, Field(description="posts, comments, or both")] = "overview",
        limit: Annotated[int, Field(ge=1, le=100)] = 20,
        cursor: Annotated[str | None, Field(description="next_cursor from a previous call")] = None,
        source: Annotated[Source, Field(description=SOURCE_HELP)] = "auto",
    ) -> str:
        """A user's recent posts and/or comments, newest first."""
        try:
            name = ids.username(username)
        except SourceError as exc:
            raise ToolError(str(exc)) from None
        page = await _call(
            ctx, "user_activity", source, username=name, kind=kind, limit=limit, cursor=cursor
        )
        return fmt.render_activity(page, f"u/{name} · {kind}")

    @mcp.tool(annotations=READ_ONLY, structured_output=False)
    async def get_subreddit_info(
        ctx: Context,
        subreddit: Annotated[str, Field(description="Subreddit name, with or without r/")],
        source: Annotated[Source, Field(description=SOURCE_HELP)] = "auto",
    ) -> str:
        """Subreddit description, sidebar, subscriber count and creation date."""
        try:
            name = ids.subreddit_name(subreddit)
        except SourceError as exc:
            raise ToolError(str(exc)) from None
        info = await _call(ctx, "subreddit_info", source, name=name)
        return fmt.render_subreddit_info(info)

    @mcp.tool(annotations=READ_ONLY, structured_output=False)
    async def find_subreddits(
        ctx: Context,
        query: Annotated[str, Field(min_length=1, description="Topic (live) or name prefix (archive)")],
        limit: Annotated[int, Field(ge=1, le=100)] = 15,
        source: Annotated[Source, Field(description=SOURCE_HELP)] = "auto",
    ) -> str:
        """Find subreddits by topic (live) or by name prefix (archive)."""
        page = await _call(ctx, "find_subreddits", source, query=query.strip(), limit=limit)
        return fmt.render_subreddit_list(page, f"subreddits matching “{query.strip()}”")

    @mcp.tool(annotations=READ_ONLY, structured_output=False)
    async def diagnose_live(ctx: Context) -> str:
        """Check how reddit.com answers this server: block page, login recognized, modhash."""
        from .http import KeepOpen
        from .probe import redact
        from .sources.live import diagnose

        router = _router(ctx)
        settings = router.settings
        if not settings.has_live:
            return "No session cookie configured (REDDIT_COOKIE / REDDIT_COOKIE_FILE)."
        # Reuse the running browser instead of starting a second one.
        shared = getattr(router.live, "transport", None)
        lines = await diagnose(settings, transport=KeepOpen(shared) if shared is not None else None)
        return "\n".join(["## live source diagnosis", *(f"- {redact(line)}" for line in lines)])

    if enable_writes is None:
        enable_writes = Settings.from_env().enable_writes
    if enable_writes:
        register_write_tools(mcp)
    return mcp


WRITE = ToolAnnotations(readOnlyHint=False, destructiveHint=False, idempotentHint=False, openWorldHint=True)
DESTRUCTIVE = ToolAnnotations(readOnlyHint=False, destructiveHint=True, idempotentHint=False, openWorldHint=True)
TOGGLE = ToolAnnotations(readOnlyHint=False, destructiveHint=False, idempotentHint=True, openWorldHint=True)

WRITE_NOTE = " Acts on the user's real Reddit account and is publicly visible."


async def _write(ctx: Context, action: str, *args) -> str:
    router = _router(ctx)
    try:
        writer = router.require_writer()
        result = await getattr(writer, action)(*args)
    except SourceError as exc:
        raise ToolError("; ".join([str(exc), *router.session_notes()])) from None
    router.cache.clear()  # reads after a write should see it
    lines = [f"Done: {result.action} as u/{result.account}"]
    if result.fullname:
        lines.append(f"id: `{result.fullname}`")
    if result.permalink:
        lines.append(result.permalink)
    return "\n".join(lines + [f"> note: {n}" for n in [*result.notes, *router.session_notes()]])


def _fullname(value: str) -> str:
    try:
        return ids.fullname(value)
    except SourceError as exc:
        raise ToolError(str(exc)) from None


def register_write_tools(mcp: FastMCP) -> None:
    @mcp.tool(annotations=READ_ONLY, structured_output=False)
    async def get_my_account(ctx: Context) -> str:
        """Which Reddit account the write tools act as."""
        router = _router(ctx)
        try:
            name, _ = await router.require_writer().me()
        except SourceError as exc:
            raise ToolError("; ".join([str(exc), *router.session_notes()])) from None
        lines = [f"Logged in as u/{name}. Write tools act on this account."]
        return "\n".join(lines + [f"> note: {n}" for n in router.session_notes()])

    @mcp.tool(annotations=WRITE, structured_output=False, description="Submit a new post." + WRITE_NOTE)
    async def submit_post(
        ctx: Context,
        subreddit: Annotated[str, Field(description="Subreddit name, with or without r/")],
        title: Annotated[str, Field(min_length=1, max_length=300)],
        text: Annotated[str | None, Field(description="Markdown body for a text post")] = None,
        url: Annotated[str | None, Field(description="Link for a link post (instead of text)")] = None,
        flair_id: Annotated[str | None, Field(description="Post flair template id, if required")] = None,
        nsfw: bool = False,
        spoiler: bool = False,
    ) -> str:
        try:
            sub = ids.subreddit_name(subreddit)
        except SourceError as exc:
            raise ToolError(str(exc)) from None
        return await _write(ctx, "submit", sub, title, text, url, flair_id, nsfw, spoiler)

    @mcp.tool(annotations=WRITE, structured_output=False, description="Reply to a post or comment." + WRITE_NOTE)
    async def reply(
        ctx: Context,
        parent: Annotated[str, Field(description="t3_<post id>, t1_<comment id>, or a URL")],
        text: Annotated[str, Field(min_length=1, description="Markdown text")],
    ) -> str:
        return await _write(ctx, "reply", _fullname(parent), text)

    @mcp.tool(
        annotations=DESTRUCTIVE,
        structured_output=False,
        description="Replace the text of your own text post or comment." + WRITE_NOTE,
    )
    async def edit_content(
        ctx: Context,
        target: Annotated[str, Field(description="t3_<post id>, t1_<comment id>, or a URL")],
        text: Annotated[str, Field(min_length=1, description="New markdown text (replaces the old)")],
    ) -> str:
        return await _write(ctx, "edit", _fullname(target), text)

    @mcp.tool(
        annotations=DESTRUCTIVE,
        structured_output=False,
        description="Delete your own post or comment. Cannot be undone." + WRITE_NOTE,
    )
    async def delete_content(
        ctx: Context,
        target: Annotated[str, Field(description="t3_<post id>, t1_<comment id>, or a URL")],
    ) -> str:
        return await _write(ctx, "delete", _fullname(target))

    @mcp.tool(annotations=TOGGLE, structured_output=False, description="Upvote, downvote or clear your vote." + WRITE_NOTE)
    async def vote(
        ctx: Context,
        target: Annotated[str, Field(description="t3_<post id>, t1_<comment id>, or a URL")],
        direction: Annotated[Literal["up", "down", "clear"], Field(description="up, down, or clear")],
    ) -> str:
        value = {"up": 1, "down": -1, "clear": 0}[direction]
        return await _write(ctx, "vote", _fullname(target), value)

    @mcp.tool(annotations=TOGGLE, structured_output=False, description="Save or unsave a post or comment to your account.")
    async def save_content(
        ctx: Context,
        target: Annotated[str, Field(description="t3_<post id>, t1_<comment id>, or a URL")],
        saved: Annotated[bool, Field(description="true to save, false to unsave")] = True,
    ) -> str:
        return await _write(ctx, "save", _fullname(target), saved)
