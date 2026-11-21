"""The interface every data source implements.

Arguments arrive already normalized (bare ids, names without r/ or u/).
A source raises ``NotSupported`` for anything it cannot honor exactly, rather
than quietly answering a different question.
"""

from __future__ import annotations

from typing import Protocol

from ..models import Comment, Page, Post, SubredditInfo, Thread

BROWSE_SORTS = ("hot", "new", "top", "rising", "controversial")
SEARCH_SORTS = ("relevance", "hot", "top", "new", "old", "comments")
COMMENT_SORTS = ("best", "top", "new", "old", "controversial", "qa")
TIME_FILTERS = ("hour", "day", "week", "month", "year", "all")
ACTIVITY_KINDS = ("overview", "posts", "comments")


class DataSource(Protocol):
    name: str
    label: str

    async def browse(
        self, subreddit: str, sort: str, time: str | None, limit: int, cursor: str | None
    ) -> Page[Post]: ...

    async def search(
        self,
        query: str | None,
        subreddit: str | None,
        author: str | None,
        sort: str,
        time: str | None,
        after: str | None,
        before: str | None,
        limit: int,
        cursor: str | None,
    ) -> Page[Post]: ...

    async def get_post(
        self, post_id: str, comment_limit: int, comment_depth: int, comment_sort: str
    ) -> Thread: ...

    async def get_thread(
        self, post_id: str | None, comment_id: str, depth: int, limit: int, sort: str
    ) -> Thread: ...

    async def get_comments(
        self, comment_ids: tuple[str, ...], post_id: str | None = None
    ) -> Page[Comment]: ...

    async def user_activity(
        self, username: str, kind: str, limit: int, cursor: str | None
    ) -> Page[Post | Comment]: ...

    async def subreddit_info(self, name: str) -> SubredditInfo: ...

    async def find_subreddits(self, query: str, limit: int) -> Page: ...

    async def aclose(self) -> None: ...
