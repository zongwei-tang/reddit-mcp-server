"""Choose a source per request, fall back when it makes sense, cache results."""

from __future__ import annotations

import dataclasses
from typing import Any

from .config import Settings
from .errors import InvalidRequest, SourceError
from .http import TTLCache
from .models import Result
from .sources import ArchiveSource, DataSource, LiveSource


def split_cursor(cursor: str | None) -> tuple[str | None, str | None]:
    """'archive:1795...' -> ('archive', '1795...'). Cursors are tied to a source."""
    if not cursor:
        return None, None
    name, sep, raw = cursor.partition(":")
    if not sep or name not in ("live", "archive") or not raw:
        raise InvalidRequest(f"invalid cursor {cursor!r}; pass next_cursor back unchanged")
    return name, raw


def _freeze(value: Any) -> Any:
    if isinstance(value, list | tuple):
        return tuple(_freeze(v) for v in value)
    return value


class Router:
    def __init__(
        self,
        settings: Settings,
        live: DataSource | None = None,
        archive: DataSource | None = None,
        writer: Any = None,
        transport: Any = None,
    ) -> None:
        self.settings = settings
        self.live = live
        self.archive = archive
        self.writer = writer
        # The shared reddit.com transport (the browser, in browser mode); the
        # session keeper renews its session.
        self.transport = transport
        self.cache = TTLCache(settings.cache_ttl)

    @classmethod
    def from_settings(cls, settings: Settings) -> Router:
        from .sources.live import make_transport
        from .writer import RedditWriter

        # One transport (one browser, in browser mode) shared by reads and writes.
        transport = make_transport(settings) if settings.has_live else None
        live = LiveSource(settings, transport=transport) if settings.has_live else None
        writer = (
            RedditWriter(settings, transport=transport)
            if settings.has_live and settings.enable_writes
            else None
        )
        return cls(settings, live=live, archive=ArchiveSource(settings), writer=writer, transport=transport)

    def session_notes(self) -> list[str]:
        """A warning about the server's Reddit login, when there is one."""
        note = getattr(self.transport, "session_note", None)
        return [note] if note else []

    def require_writer(self):
        if self.writer is None:
            raise InvalidRequest(
                "writing needs REDDIT_ENABLE_WRITES=1 and a session cookie "
                "(REDDIT_COOKIE or REDDIT_COOKIE_FILE)"
            )
        return self.writer

    async def aclose(self) -> None:
        for src in (self.live, self.archive, self.writer):
            if src is not None:
                await src.aclose()

    def order(self, requested: str | None) -> list[DataSource]:
        choice = (requested or "auto").lower()
        if choice == "auto":
            choice = self.settings.source
        if choice == "live":
            if self.live is None:
                raise InvalidRequest(
                    "source=live needs a session cookie: set REDDIT_COOKIE or REDDIT_COOKIE_FILE"
                )
            return [self.live]
        if choice == "archive":
            if self.archive is None:
                raise InvalidRequest("the archive source is not configured")
            return [self.archive]
        if choice != "auto":
            raise InvalidRequest(f"source must be auto, live or archive, got {requested!r}")
        return [s for s in (self.live, self.archive) if s is not None]

    async def run(self, op: str, source: str | None, *, cursor: str | None = None, **kwargs: Any) -> Any:
        cursor_source, raw_cursor = split_cursor(cursor)
        if cursor_source:
            if source not in (None, "auto", cursor_source):
                raise InvalidRequest(
                    f"this cursor belongs to source={cursor_source}, not source={source}"
                )
            source = cursor_source
            kwargs["cursor"] = raw_cursor
        elif "cursor" in _CURSOR_OPS.get(op, ()):
            kwargs["cursor"] = None

        sources = self.order(source)
        notes: list[str] = []
        for index, src in enumerate(sources):
            key = (src.name, op, tuple(sorted((k, _freeze(v)) for k, v in kwargs.items())))
            result = self.cache.get(key)
            if result is None:
                try:
                    result = await getattr(src, op)(**kwargs)
                except SourceError as exc:
                    last = index == len(sources) - 1
                    if not exc.fallback or last:
                        message = "; ".join([*notes, f"{src.label}: {exc}", *self.session_notes()])
                        raise type(exc)(message) from None
                    notes.append(f"{src.label} could not answer ({exc}); fell back")
                    continue
                if getattr(result, "next_cursor", None):
                    result.next_cursor = f"{src.name}:{result.next_cursor}"
                self.cache.set(key, result)
            if isinstance(result, Result):
                result = dataclasses.replace(result, notes=[*notes, *result.notes, *self.session_notes()])
            return result
        raise InvalidRequest("no data source is configured")


_CURSOR_OPS = {
    "browse": ("cursor",),
    "search": ("cursor",),
    "user_activity": ("cursor",),
}
