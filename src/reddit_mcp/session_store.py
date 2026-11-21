"""Keep the browser's Reddit cookies across restarts.

The browser session renews itself while it runs: token_v2 every day, and
Reddit may also hand out fresh session cookies when the site is visited. Those
renewals live only in the browser's memory, so without this module every
restart would fall back to the cookie the user configured once.

The store is a JSON file on a persistent volume. It remembers which configured
cookie it grew from (a hash, never the cookie itself): when the user sets a new
REDDIT_COOKIE, the stored session is discarded and the new one is used.

The file holds the session, so it is written atomically with mode 0600, and
nothing from it is ever logged except cookie names and expiry dates.
"""

from __future__ import annotations

import hashlib
import json
import os
import time
from pathlib import Path
from typing import Any

REDDIT_DOMAINS = ("reddit.com",)
# The fields Playwright's add_cookies() accepts. Anything else that cookies()
# returned (newer Playwright versions add more) is dropped on load, so a saved
# file can always be fed back to the browser.
COOKIE_KEYS = ("name", "value", "domain", "path", "expires", "httpOnly", "secure", "sameSite")
SAME_SITE = ("Strict", "Lax", "None")
SESSION_COOKIE = "reddit_session"


def fingerprint(cookie_header: str | None) -> str:
    """A short hash identifying the configured cookie (not reversible)."""
    return hashlib.sha256((cookie_header or "").encode()).hexdigest()[:16]


def _is_reddit(cookie: dict[str, Any]) -> bool:
    domain = str(cookie.get("domain") or "").lstrip(".").lower()
    return any(domain == d or domain.endswith("." + d) for d in REDDIT_DOMAINS)


def _alive(cookie: dict[str, Any], now: float) -> bool:
    expires = cookie.get("expires")
    # Playwright uses -1 for session cookies.
    return not isinstance(expires, int | float) or expires < 0 or expires > now


def clean_cookie(cookie: dict[str, Any]) -> dict[str, Any] | None:
    """The cookie reduced to what add_cookies() accepts, or None if unusable."""
    if not isinstance(cookie, dict):
        return None
    name, value = cookie.get("name"), cookie.get("value")
    if not isinstance(name, str) or not name or not isinstance(value, str) or not _is_reddit(cookie):
        return None
    out = {k: cookie[k] for k in COOKIE_KEYS if k in cookie}
    out.setdefault("path", "/")
    if not isinstance(out.get("expires"), int | float):
        out.pop("expires", None)
    if out.get("sameSite") not in SAME_SITE:
        out.pop("sameSite", None)
    for flag in ("httpOnly", "secure"):
        if flag in out and not isinstance(out[flag], bool):
            out.pop(flag)
    return out


class SessionStore:
    def __init__(self, path: str | os.PathLike[str], *, read_only: bool = False) -> None:
        self.path = Path(path)
        self.read_only = read_only

    def __repr__(self) -> str:
        mode = ", read_only=True" if self.read_only else ""
        return f"SessionStore({str(self.path)!r}{mode})"

    def reader(self) -> SessionStore:
        """The same file, for a browser that may use it but must not change it."""
        return SessionStore(self.path, read_only=True)

    def load(self, source: str) -> list[dict[str, Any]] | None:
        """Stored cookies grown from ``source``, or None (missing, unreadable,
        or from an older configured cookie)."""
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
        if not isinstance(data, dict) or data.get("source") != source:
            return None
        now = time.time()
        raw = data.get("cookies")
        cookies = [c for c in map(clean_cookie, raw if isinstance(raw, list) else []) if c and _alive(c, now)]
        return cookies or None

    def discard(self) -> None:
        """Set a file the browser refused aside, so the next start uses the
        configured cookie (best effort; the next save replaces it anyway)."""
        if self.read_only:
            return
        try:
            os.replace(self.path, self.path.with_name(self.path.name + ".rejected"))
        except OSError:
            pass

    def _holds_login(self, source: str) -> bool:
        """Whether the file has a login cookie grown from ``source``."""
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
            if data.get("source") != source:
                return False
            return any(c.get("name") == SESSION_COOKIE for c in data.get("cookies", []) if isinstance(c, dict))
        except (OSError, ValueError, AttributeError, TypeError):
            return False

    def save(self, cookies: list[dict[str, Any]], source: str) -> int:
        """Write the Reddit cookies atomically; returns how many were kept.

        A cookie jar without the login cookie never replaces a saved one that
        has it: a jar read at a bad moment must not wipe out the session."""
        if self.read_only:
            return 0
        keep = [c for c in map(clean_cookie, cookies) if c]
        if not any(c["name"] == SESSION_COOKIE for c in keep) and self._holds_login(source):
            raise ValueError("not replacing the saved login with a cookie jar that has none")
        payload = json.dumps({"source": source, "saved_at": int(time.time()), "cookies": keep})
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_name(self.path.name + ".tmp")
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(payload)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, self.path)
        os.chmod(self.path, 0o600)
        return len(keep)
