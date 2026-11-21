"""Error types shared by all data sources.

``fallback`` tells the router whether trying the next source could help.
"""

from __future__ import annotations


class SourceError(Exception):
    """Base class for anything a data source can fail with."""

    fallback: bool = False


class Unavailable(SourceError):
    """The source could not be reached or refused us (network, 403, 5xx, 429)."""

    fallback = True


class NotSupported(SourceError):
    """The source cannot answer this kind of request (e.g. archive has no 'hot')."""

    fallback = True


class NotFound(SourceError):
    """The object does not exist in this source. Another source may still have it."""

    fallback = True


class InvalidRequest(SourceError):
    """The request itself is malformed; no source can answer it."""

    fallback = False
