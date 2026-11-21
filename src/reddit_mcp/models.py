"""Source-independent data model. Both backends parse into these."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Generic, TypeVar, Union


@dataclass(kw_only=True)
class Post:
    id: str
    subreddit: str
    author: str
    title: str
    score: int
    num_comments: int
    created_utc: float
    permalink: str
    url: str = ""
    is_self: bool = True
    selftext: str = ""
    flair: str | None = None
    over_18: bool = False
    spoiler: bool = False
    locked: bool = False
    stickied: bool = False
    upvote_ratio: float | None = None
    removed_by: str | None = None


@dataclass(kw_only=True)
class More:
    """Collapsed comments.

    ``children`` non-empty: sibling comments that were cut off (expand by id).
    ``children`` empty: the thread continues deeper below ``parent_id``.
    """

    parent_id: str
    depth: int
    count: int
    children: list[str] = field(default_factory=list)


@dataclass(kw_only=True)
class Comment:
    id: str
    author: str
    body: str
    score: int
    created_utc: float
    depth: int
    parent_id: str
    link_id: str
    permalink: str = ""
    subreddit: str = ""
    link_title: str | None = None
    is_submitter: bool = False
    stickied: bool = False
    distinguished: str | None = None
    score_hidden: bool = False
    replies: list[Node] = field(default_factory=list)


Node = Union[Comment, More]


@dataclass(kw_only=True)
class Subreddit:
    name: str
    title: str = ""
    public_description: str = ""
    description: str = ""
    subscribers: int | None = None
    active_users: int | None = None
    created_utc: float | None = None
    over_18: bool = False
    subreddit_type: str | None = None
    as_of: float | None = None


T = TypeVar("T")


@dataclass(kw_only=True)
class Result:
    source: str = ""
    notes: list[str] = field(default_factory=list)
    # Parameters as the source actually applied them, e.g. {"sort": "new"}.
    applied: dict[str, str] = field(default_factory=dict)


@dataclass(kw_only=True)
class Page(Result, Generic[T]):
    items: list[T]
    next_cursor: str | None = None


@dataclass(kw_only=True)
class Thread(Result):
    post: Post | None
    comments: list[Node]
    focus_comment_id: str | None = None


@dataclass(kw_only=True)
class SubredditInfo(Result):
    subreddit: Subreddit
