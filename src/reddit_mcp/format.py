"""Render results as compact markdown for an LLM to read."""

from __future__ import annotations

import time as _time
from datetime import UTC, datetime

from .models import Comment, More, Node, Page, Post, Result, Subreddit, SubredditInfo, Thread

LISTING_BODY = 280
POST_BODY = 4000
COMMENT_BODY = 1200
MAX_MORE_IDS = 100  # get_comments_by_id takes at most 100 ids per call


def clip(text: str, limit: int) -> str:
    text = text.strip()
    if len(text) <= limit:
        return text
    return text[:limit].rstrip() + f"… [+{len(text) - limit} chars]"


def age(ts: float | None, now: float) -> str:
    if not ts:
        return "unknown time"
    delta = max(0, int(now - ts))
    if delta < 3600:
        rel = f"{delta // 60}m ago"
    elif delta < 86400:
        rel = f"{delta // 3600}h ago"
    elif delta < 86400 * 60:
        rel = f"{delta // 86400}d ago"
    else:
        rel = datetime.fromtimestamp(ts, UTC).strftime("%Y-%m-%d")
    return rel


def stamp(ts: float | None) -> str:
    if not ts:
        return "unknown"
    return datetime.fromtimestamp(ts, UTC).strftime("%Y-%m-%d %H:%M UTC")


def community(subreddit: str) -> str:
    """'Python' -> 'r/Python'; a profile's 'u_spez' -> 'u/spez', as Reddit shows them."""
    if subreddit.startswith("u_"):
        return "u/" + subreddit[2:]
    return f"r/{subreddit}" if subreddit else "r/?"


def header(result: Result, title: str) -> list[str]:
    lines = [f"## {title}", f"_source: {result.source}_"]
    lines += [f"> note: {n}" for n in result.notes]
    return lines


def _post_meta(p: Post, now: float) -> str:
    bits = [community(p.subreddit), f"u/{p.author}", f"{p.score} pts"]
    if p.upvote_ratio is not None:
        bits.append(f"{round(p.upvote_ratio * 100)}% up")
    bits += [f"{p.num_comments} comments", age(p.created_utc, now), f"id `{p.id}`"]
    flags = [
        f"flair: {p.flair}" if p.flair else None,
        "NSFW" if p.over_18 else None,
        "spoiler" if p.spoiler else None,
        "locked" if p.locked else None,
        "pinned" if p.stickied else None,
        f"removed by {p.removed_by}" if p.removed_by else None,
    ]
    bits += [f for f in flags if f]
    return " · ".join(bits)


def render_post_line(i: int, p: Post, now: float) -> list[str]:
    lines = [f"{i}. **{p.title}**", f"   {_post_meta(p, now)}", f"   {p.permalink}"]
    if not p.is_self and p.url:
        lines.append(f"   link: {p.url}")
    if p.selftext:
        lines.append("   > " + clip(p.selftext, LISTING_BODY).replace("\n", " "))
    return lines


def render_post_full(p: Post, now: float) -> list[str]:
    lines = [f"### {p.title}", _post_meta(p, now), f"posted {stamp(p.created_utc)}", p.permalink]
    if not p.is_self and p.url:
        lines.append(f"link: {p.url}")
    if p.selftext:
        lines += ["", clip(p.selftext, POST_BODY)]
    return lines


def _more_line(m: More, pad: str, post_id: str | None = None) -> str:
    # Passing post= lets the follow-up call skip a lookup Reddit may refuse.
    post_arg = f", post='{post_id}'" if post_id else ""
    if m.children:
        ids = m.children[:MAX_MORE_IDS]
        rest = len(m.children) - len(ids)
        extra = (
            f" ({rest} more ids not listed: reload the post with a larger comment_limit)" if rest > 0 else ""
        )
        noun = "reply" if m.count == 1 else "replies"
        return (
            f"{pad}- _[{m.count} more {noun} collapsed: get_comments_by_id("
            f"comment_ids={ids}{post_arg}){extra}]_"
        )
    count = f"{m.count} " if m.count else ""
    parent = m.parent_id.removeprefix("t1_")
    if m.parent_id.startswith("t3_") or not parent:
        return f"{pad}- _[{count}more replies not loaded]_"
    return f"{pad}- _[{count}deeper replies: get_comment_thread(comment_id='{parent}'{post_arg})]_"


def _comment_head(c: Comment, now: float) -> str:
    score = "score hidden" if c.score_hidden else f"{c.score} pts"
    tags = [
        "OP" if c.is_submitter else None,
        c.distinguished if c.distinguished else None,
        "pinned" if c.stickied else None,
    ]
    tag = "".join(f" [{t}]" for t in tags if t)
    return f"**u/{c.author}**{tag} · {score} · {age(c.created_utc, now)} · id `{c.id}`"


def render_tree(nodes: list[Node], now: float, level: int = 0, post_id: str | None = None) -> list[str]:
    lines: list[str] = []
    pad = "  " * level
    for node in nodes:
        if isinstance(node, More):
            lines.append(_more_line(node, pad, post_id))
            continue
        body = clip(node.body, COMMENT_BODY).replace("\n", f"\n{pad}  ")
        lines.append(f"{pad}- {_comment_head(node, now)}")
        lines.append(f"{pad}  {body}")
        lines += render_tree(node.replies, now, level + 1, post_id or node.link_id.removeprefix("t3_") or None)
    return lines


def render_flat_comment(i: int, c: Comment, now: float) -> list[str]:
    where = f"{community(c.subreddit)} · " if c.subreddit else ""
    on = f" on “{c.link_title}”" if c.link_title else ""
    lines = [f"{i}. {_comment_head(c, now)}", f"   {where}post `{c.link_id.removeprefix('t3_')}`{on}"]
    if c.permalink:
        lines.append(f"   {c.permalink}")
    lines.append("   > " + clip(c.body, COMMENT_BODY // 2).replace("\n", " "))
    return lines


def _cursor_line(page: Page) -> list[str]:
    if page.next_cursor:
        return ["", f"next_cursor: `{page.next_cursor}`"]
    return ["", "(no more results)"] if page.items else []


def render_posts(page: Page, title: str, now: float | None = None) -> str:
    now = _time.time() if now is None else now
    lines = header(page, title)
    if not page.items:
        lines.append("No posts found.")
    for i, p in enumerate(page.items, 1):
        lines += [""] + render_post_line(i, p, now)
    return "\n".join(lines + _cursor_line(page))


def render_activity(page: Page, title: str, now: float | None = None) -> str:
    now = _time.time() if now is None else now
    lines = header(page, title)
    if not page.items:
        lines.append("No activity found.")
    for i, item in enumerate(page.items, 1):
        lines.append("")
        if isinstance(item, Post):
            lines += render_post_line(i, item, now)
        else:
            lines += render_flat_comment(i, item, now)
    return "\n".join(lines + _cursor_line(page))


def render_comments(page: Page, title: str, now: float | None = None) -> str:
    now = _time.time() if now is None else now
    lines = header(page, title)
    if not page.items:
        lines.append("No comments found.")
    for i, c in enumerate(page.items, 1):
        lines += [""] + render_flat_comment(i, c, now)
    lines.append("")
    lines.append(
        "_Replies are not included; use get_comment_thread(comment_id=..., post=...) for any of these._"
    )
    return "\n".join(lines)


def render_thread(thread: Thread, title: str, now: float | None = None) -> str:
    now = _time.time() if now is None else now
    lines = header(thread, title)
    if thread.post is not None:
        lines += [""] + render_post_full(thread.post, now)
    lines += ["", "#### Comments" if thread.focus_comment_id is None else "#### Thread"]
    if not thread.comments:
        lines.append("No comments loaded.")
    lines += render_tree(thread.comments, now, post_id=thread.post.id if thread.post else None)
    return "\n".join(lines)


def render_subreddit(sub: Subreddit, now: float) -> list[str]:
    bits = [f"r/{sub.name}"]
    if sub.subscribers is not None:
        bits.append(f"{sub.subscribers:,} subscribers")
    if sub.active_users:
        bits.append(f"{sub.active_users:,} online")
    if sub.created_utc:
        bits.append(f"created {stamp(sub.created_utc)[:10]}")
    if sub.over_18:
        bits.append("NSFW")
    if sub.subreddit_type and sub.subreddit_type != "public":
        bits.append(sub.subreddit_type)
    return [" · ".join(bits)] + ([sub.title] if sub.title else [])


def render_subreddit_info(info: SubredditInfo, now: float | None = None) -> str:
    now = _time.time() if now is None else now
    sub = info.subreddit
    lines = header(info, f"r/{sub.name}") + [""] + render_subreddit(sub, now)
    if sub.as_of:
        lines.append(f"as of {stamp(sub.as_of)}")
    if sub.public_description:
        lines += ["", clip(sub.public_description, 600)]
    if sub.description:
        lines += ["", "**Sidebar**", clip(sub.description, 2500)]
    return "\n".join(lines)


def render_subreddit_list(page: Page, title: str, now: float | None = None) -> str:
    now = _time.time() if now is None else now
    lines = header(page, title)
    if not page.items:
        lines.append("No subreddits found.")
    for i, sub in enumerate(page.items, 1):
        head, *rest = render_subreddit(sub, now)
        lines.append(f"{i}. {head}")
        desc = sub.public_description or (rest[0] if rest else "")
        if desc:
            lines.append("   " + clip(desc, 200).replace("\n", " "))
    return "\n".join(lines)
