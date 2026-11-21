"""Parse Reddit-shaped JSON (both reddit.com and Arctic Shift use it) into models."""

from __future__ import annotations

from typing import Any

from .models import Comment, More, Node, Post, Subreddit

REDDIT = "https://www.reddit.com"

# Comment sorts that can be applied locally on an already-fetched tree.
LOCAL_SORTS = ("top", "new", "old")


def _int(value: Any) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def _float(value: Any) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def _opt_int(value: Any) -> int | None:
    return None if value is None else _int(value)


def _abs_permalink(path: Any) -> str:
    if not path:
        return ""
    path = str(path)
    return path if path.startswith("http") else REDDIT + path


def unwrap(thing: Any) -> dict[str, Any]:
    """Return the inner ``data`` dict of a ``{"kind": ..., "data": ...}`` thing."""
    if isinstance(thing, dict) and "kind" in thing and isinstance(thing.get("data"), dict):
        return thing["data"]
    return thing if isinstance(thing, dict) else {}


def listing_children(obj: Any) -> list[Any]:
    """Children of a Listing; tolerate a bare list and Reddit's "" for no replies."""
    if isinstance(obj, list):
        return obj
    if isinstance(obj, dict):
        data = obj.get("data")
        if isinstance(data, dict) and isinstance(data.get("children"), list):
            return data["children"]
        if isinstance(obj.get("children"), list):
            return obj["children"]
    return []


def parse_post(thing: Any) -> Post:
    d = unwrap(thing)
    return Post(
        id=str(d.get("id") or ""),
        subreddit=d.get("subreddit") or "",
        author=d.get("author") or "[deleted]",
        title=d.get("title") or "",
        score=_int(d.get("score")),
        num_comments=_int(d.get("num_comments")),
        created_utc=_float(d.get("created_utc")),
        permalink=_abs_permalink(d.get("permalink")),
        # Crossposts and some media posts carry a site-relative url.
        url=_abs_permalink(d.get("url")) if str(d.get("url") or "").startswith("/") else (d.get("url") or ""),
        is_self=bool(d.get("is_self", True)),
        selftext=d.get("selftext") or "",
        flair=d.get("link_flair_text") or None,
        over_18=bool(d.get("over_18")),
        spoiler=bool(d.get("spoiler")),
        locked=bool(d.get("locked")),
        stickied=bool(d.get("stickied")),
        upvote_ratio=d.get("upvote_ratio"),
        removed_by=d.get("removed_by_category") or None,
    )


def parse_comment(thing: Any, replies: list[Node] | None = None) -> Comment:
    d = unwrap(thing)
    return Comment(
        id=str(d.get("id") or ""),
        author=d.get("author") or "[deleted]",
        body=d.get("body") or "",
        score=_int(d.get("score")),
        created_utc=_float(d.get("created_utc")),
        depth=_int(d.get("depth")),
        parent_id=d.get("parent_id") or "",
        link_id=d.get("link_id") or "",
        permalink=_abs_permalink(d.get("permalink")),
        subreddit=d.get("subreddit") or "",
        link_title=d.get("link_title") or None,
        is_submitter=bool(d.get("is_submitter")),
        stickied=bool(d.get("stickied")),
        distinguished=d.get("distinguished") or None,
        score_hidden=bool(d.get("score_hidden")),
        replies=replies or [],
    )


def parse_more(thing: Any) -> More:
    d = unwrap(thing)
    children = [str(c) for c in (d.get("children") or [])]
    return More(
        parent_id=d.get("parent_id") or "",
        depth=_int(d.get("depth")),
        count=_int(d.get("count")) or len(children),
        children=children,
    )


def _is_comment(thing: Any) -> bool:
    if isinstance(thing, dict) and thing.get("kind") == "t1":
        return True
    d = unwrap(thing)
    return "body" in d and "id" in d


def count_comments(children: list[Any]) -> int:
    """Number of comments (including collapsed ones) under a raw children list."""
    total = 0
    for child in children:
        if isinstance(child, dict) and child.get("kind") == "more":
            total += parse_more(child).count
        elif _is_comment(child):
            total += 1 + count_comments(listing_children(unwrap(child).get("replies")))
    return total


def parse_tree(children: list[Any], max_depth: int, level: int = 0) -> list[Node]:
    """Parse a raw comment Listing, cutting everything below ``max_depth`` levels.

    ``level`` 0 is the first level shown. Comments at the last allowed level keep
    a ``More`` placeholder (with empty ``children``) instead of their replies, so
    the caller can see that the thread continues and how many replies were cut.
    """
    nodes: list[Node] = []
    for child in children:
        if isinstance(child, dict) and child.get("kind") == "more":
            nodes.append(parse_more(child))
            continue
        if not _is_comment(child):
            continue
        data = unwrap(child)
        raw_replies = listing_children(data.get("replies"))
        if not raw_replies:
            replies: list[Node] = []
        elif level + 1 >= max_depth:
            replies = [
                More(
                    parent_id=f"t1_{data.get('id')}",
                    depth=_int(data.get("depth")) + 1,
                    count=count_comments(raw_replies),
                )
            ]
        else:
            replies = parse_tree(raw_replies, max_depth, level + 1)
        nodes.append(parse_comment(child, replies))
    return nodes


def sort_tree(nodes: list[Node], how: str) -> list[Node]:
    """Sort comments locally at every level; placeholders stay at the end."""
    comments = [n for n in nodes if isinstance(n, Comment)]
    rest = [n for n in nodes if not isinstance(n, Comment)]
    if how == "top":
        comments.sort(key=lambda c: (not c.stickied, -c.score, c.created_utc))
    elif how == "new":
        comments.sort(key=lambda c: (not c.stickied, -c.created_utc))
    elif how == "old":
        comments.sort(key=lambda c: (not c.stickied, c.created_utc))
    for c in comments:
        c.replies = sort_tree(c.replies, how)
    return [*comments, *rest]


def parse_subreddit(thing: Any) -> Subreddit:
    d = unwrap(thing)
    return Subreddit(
        name=d.get("display_name") or "",
        title=d.get("title") or "",
        public_description=d.get("public_description") or "",
        description=d.get("description") or "",
        subscribers=_opt_int(d.get("subscribers")),
        active_users=_opt_int(d.get("active_user_count") or d.get("accounts_active")),
        created_utc=d.get("created_utc"),
        over_18=bool(d.get("over18")),
        subreddit_type=d.get("subreddit_type") or None,
        as_of=d.get("retrieved_on"),
    )
