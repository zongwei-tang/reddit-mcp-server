"""Settings, read from environment variables."""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path

ARCHIVE_URL = "https://arctic-shift.photon-reddit.com"
LIVE_URL = "https://www.reddit.com"
OAUTH_URL = "https://oauth.reddit.com"
TOOL_USER_AGENT = "reddit-mcp/0.1 (personal research tool)"

SOURCES = ("auto", "live", "archive")


def _float(env: Mapping[str, str], name: str, default: float) -> float:
    raw = env.get(name)
    if raw is None or raw.strip() == "":
        return default
    try:
        return float(raw)
    except ValueError:
        raise ValueError(f"{name} must be a number, got {raw!r}") from None


def normalize_cookie(raw: str) -> str | None:
    """Accept a full Cookie header, 'Cookie: ...', or a bare reddit_session value.

    Header values must be plain ASCII: a byte-order mark from an editor is
    dropped, anything else non-ASCII is refused here with a clear message
    instead of breaking every request later.
    """
    value = "".join(raw.replace("﻿", "").splitlines()).strip()
    if value.lower().startswith("cookie:"):
        value = value[len("cookie:") :].strip()
    if not value:
        return None
    if not value.isascii():
        raise ValueError(
            "the Reddit cookie contains non-ASCII characters; copy the cookie header again "
            "(from the browser's Network panel) without editing it"
        )
    if "=" not in value:
        value = f"reddit_session={value}"
    return value


def load_cookie(env: Mapping[str, str]) -> str | None:
    direct = env.get("REDDIT_COOKIE")
    if direct and direct.strip():
        return normalize_cookie(direct)
    path = env.get("REDDIT_COOKIE_FILE")
    if path and path.strip():
        try:
            text = Path(path).expanduser().read_text(encoding="utf-8")
        except OSError as exc:
            # Never echo file contents; the path is the user's own config.
            raise ValueError(f"cannot read REDDIT_COOKIE_FILE: {exc.strerror}") from None
        return normalize_cookie(text)
    return None


@dataclass(frozen=True)
class Settings:
    source: str = "auto"
    cookie: str | None = field(default=None, repr=False)
    user_agent: str = TOOL_USER_AGENT
    archive_url: str = ARCHIVE_URL
    live_url: str = LIVE_URL
    timeout: float = 20.0
    cache_ttl: float = 120.0
    live_interval: float = 1.0
    archive_interval: float = 0.5
    live_client: str = "http"  # "http" (httpx) or "browser" (Chromium via Playwright)
    enable_writes: bool = False
    write_interval: float = 5.0
    write_daily_limit: int = 100
    # Browser mode: where the browser's session is kept across restarts (a
    # file on a persistent volume), and how often the session keeper visits
    # reddit.com so the session stays fresh.
    session_store: str | None = None
    session_refresh_hours: float = 6.0

    @property
    def has_live(self) -> bool:
        return bool(self.cookie)

    @property
    def uses_oauth_api(self) -> bool:
        """Browser mode talks to oauth.reddit.com, like reddit.com's own web app."""
        return self.live_client == "browser"

    @property
    def live_api_url(self) -> str:
        return OAUTH_URL if self.uses_oauth_api else self.live_url

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> Settings:
        env = os.environ if env is None else env
        source = (env.get("REDDIT_SOURCE") or "auto").strip().lower()
        if source not in SOURCES:
            raise ValueError(f"REDDIT_SOURCE must be one of {SOURCES}, got {source!r}")
        live_client = (env.get("REDDIT_LIVE_CLIENT") or "http").strip().lower()
        if live_client not in ("http", "browser"):
            raise ValueError(f"REDDIT_LIVE_CLIENT must be http or browser, got {live_client!r}")
        return cls(
            source=source,
            live_client=live_client,
            cookie=load_cookie(env),
            user_agent=(env.get("REDDIT_USER_AGENT") or TOOL_USER_AGENT).strip(),
            archive_url=(env.get("REDDIT_ARCHIVE_URL") or ARCHIVE_URL).rstrip("/"),
            live_url=(env.get("REDDIT_LIVE_URL") or LIVE_URL).rstrip("/"),
            timeout=_float(env, "REDDIT_MCP_TIMEOUT", 20.0),
            cache_ttl=_float(env, "REDDIT_MCP_CACHE_TTL", 120.0),
            live_interval=_float(env, "REDDIT_MCP_LIVE_INTERVAL", 1.0),
            archive_interval=_float(env, "REDDIT_MCP_ARCHIVE_INTERVAL", 0.5),
            enable_writes=(env.get("REDDIT_ENABLE_WRITES") or "").strip().lower()
            in ("1", "true", "yes"),
            write_interval=_float(env, "REDDIT_WRITE_INTERVAL", 5.0),
            write_daily_limit=int(_float(env, "REDDIT_WRITE_DAILY_LIMIT", 100)),
            session_store=(env.get("REDDIT_SESSION_STORE") or "").strip() or None,
            session_refresh_hours=_refresh_hours(env),
        )


def _refresh_hours(env: Mapping[str, str]) -> float:
    hours = _float(env, "REDDIT_SESSION_REFRESH_HOURS", 6.0)
    if not 0.25 <= hours <= 24 * 7:
        raise ValueError(f"REDDIT_SESSION_REFRESH_HOURS must be between 0.25 and 168, got {hours}")
    return hours
