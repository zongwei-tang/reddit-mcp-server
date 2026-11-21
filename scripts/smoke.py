"""Hit the real sources once and report what works. Run it yourself:

    uv run python scripts/smoke.py

It reads the same environment variables as the server. It prints only status,
counts and one post title per check; it never prints the cookie.
"""

from __future__ import annotations

import asyncio
import sys

from reddit_mcp.config import Settings
from reddit_mcp.errors import SourceError
from reddit_mcp.models import Comment
from reddit_mcp.router import Router


def notes(result) -> None:
    for note in result.notes:
        print(f"    note: {note}")


async def step(name: str, coro) -> object | None:
    try:
        result = await coro
    except SourceError as exc:
        print(f"  FAIL {name}: {type(exc).__name__}: {exc}")
        return None
    print(f"  ok   {name}")
    return result


async def check(router: Router, source: str) -> bool:
    print(f"\n== {source} ==")
    page = await step(
        "browse r/Python (default sort)",
        router.run("browse", source, subreddit="Python", sort=None, time=None, limit=3),
    )
    if page is None:
        return False
    print(f"    {len(page.items)} posts, sort={page.applied.get('sort')}")
    notes(page)
    ok = True
    search = await step(
        "search 'asyncio' in r/Python",
        router.run(
            "search", source, query="asyncio", subreddit="Python", author=None, sort=None,
            time=None, after=None, before=None, limit=3,
        ),
    )
    ok &= search is not None
    if search is not None:
        print(f"    {len(search.items)} posts, sort={search.applied.get('sort')}")

    # Use a post that has comments, so the thread checks have something to read.
    candidates = [p for p in page.items + (search.items if search else []) if p.num_comments > 0]
    if not candidates:
        print("  skip get_post: no post with comments in the sample")
        return ok
    post = candidates[0]
    print(f"    sample post: {post.title[:70]!r}")
    thread = await step(
        f"get_post {post.id}",
        router.run("get_post", source, post_id=post.id, comment_limit=20, comment_depth=2, comment_sort="top"),
    )
    ok &= thread is not None
    if thread is None:
        return ok
    comments = [c for c in thread.comments if isinstance(c, Comment)]
    print(f"    {len(thread.comments)} top-level nodes")
    notes(thread)
    if comments:
        sub = await step(
            f"get_comment_thread {comments[0].id}",
            router.run(
                "get_thread", source, post_id=post.id, comment_id=comments[0].id,
                depth=2, limit=20, sort="top",
            ),
        )
        ok &= sub is not None
        if sub is not None:
            root = sub.comments[0]
            dup = any(isinstance(r, Comment) and r.id == root.id for r in root.replies)
            print(f"    focus has {len(root.replies)} reply nodes, duplicate-of-self: {dup}")
            ok &= not dup
    return ok


async def main() -> int:
    settings = Settings.from_env()
    router = Router.from_settings(settings)
    try:
        results = [await check(router, "archive")]
        if settings.has_live:
            results.append(await check(router, "live"))
        else:
            print("\n== live ==\n  skip: no REDDIT_COOKIE / REDDIT_COOKIE_FILE set")
    finally:
        await router.aclose()
    print("\nALL OK" if all(results) else "\nSOME CHECKS FAILED")
    return 0 if all(results) else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
