"""Write actions on reddit.com through the user's logged-in session.

Uses Reddit's classic form endpoints (/api/submit, /api/comment, ...), which
accept a session cookie plus the account's modhash (a per-session CSRF token
read from /api/me.json).

Guard rails, all enforced here rather than trusted to the caller:
  * nothing is registered unless REDDIT_ENABLE_WRITES=1 (see server.py);
  * a minimum gap between writes and a rolling 24h cap;
  * the same text cannot be posted twice within an hour;
  * Reddit's own errors (rate limit, CAPTCHA, banned, ...) are returned as-is,
    never retried around.
"""

from __future__ import annotations

import asyncio
import hashlib
import time as _time
from collections import deque
from dataclasses import dataclass, field
from typing import Any

import httpx

from .config import Settings
from .errors import InvalidRequest, NotFound, SourceError, Unavailable
from .http import Pacer, send
from .probe import redact
from .sources.live import is_block_page

DUPLICATE_WINDOW = 3600.0
DAY = 86400.0
TITLE_MAX = 300
TEXT_MAX = 40000


class WriteRejected(SourceError):
    """Reddit (or a local guard rail) refused the write. Never falls back."""

    fallback = False


class WriteUncertain(SourceError):
    """The request was sent but the outcome is unknown; it may have gone through."""

    fallback = False


UNCERTAIN = "check the account before retrying, the write may have gone through"


@dataclass(kw_only=True)
class WriteResult:
    action: str
    fullname: str = ""
    permalink: str = ""
    account: str = ""
    notes: list[str] = field(default_factory=list)


def _form_errors(body: Any) -> list[str]:
    """Reddit answers 200 with {"json": {"errors": [[code, message, field], ...]}}."""
    if not isinstance(body, dict):
        return []
    errors = (body.get("json") or {}).get("errors") or []
    out = []
    for err in errors:
        if isinstance(err, list | tuple) and err:
            code = err[0]
            message = err[1] if len(err) > 1 else ""
            out.append(f"{code}: {message}".strip(": "))
        else:
            out.append(str(err))
    return out


class Guard:
    """Local limits that apply to every write, regardless of what Reddit allows."""

    def __init__(self, min_interval: float, daily_limit: int) -> None:
        self.min_interval = max(0.0, min_interval)
        self.daily_limit = daily_limit
        self.now = _time.time
        self._writes: deque[float] = deque()
        self._recent_text: dict[str, float] = {}

    def check(self, text: str | None) -> None:
        now = self.now()
        while self._writes and now - self._writes[0] > DAY:
            self._writes.popleft()
        if self.daily_limit and len(self._writes) >= self.daily_limit:
            raise WriteRejected(
                f"daily write limit reached ({self.daily_limit} in 24h); "
                "raise REDDIT_WRITE_DAILY_LIMIT to allow more"
            )
        if self._writes and now - self._writes[-1] < self.min_interval:
            wait = self.min_interval - (now - self._writes[-1])
            raise WriteRejected(f"writes are spaced {self.min_interval:.0f}s apart; try again in {wait:.0f}s")
        if text:
            digest = hashlib.sha256(text.strip().encode()).hexdigest()
            last = self._recent_text.get(digest)
            if last is not None and now - last < DUPLICATE_WINDOW:
                raise WriteRejected("the same text was already posted within the last hour")

    def record(self, text: str | None) -> None:
        now = self.now()
        self._writes.append(now)
        if text:
            self._recent_text[hashlib.sha256(text.strip().encode()).hexdigest()] = now
        self._recent_text = {k: t for k, t in self._recent_text.items() if now - t < DUPLICATE_WINDOW}


class RedditWriter:
    def __init__(
        self,
        settings: Settings,
        client: httpx.AsyncClient | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        if not settings.cookie:
            raise ValueError("RedditWriter needs a session cookie")
        # Two front doors in browser mode: oauth.reddit.com (bearer token, added
        # by the browser transport) and www.reddit.com (cookie + modhash). Reddit
        # refuses some endpoints on one door only, so each endpoint remembers the
        # door that worked. Without the browser there is only www.
        self._has_oauth = settings.uses_oauth_api
        self._www = settings.live_url
        self._doors: dict[str, str] = {}
        self.last_door: str | None = None
        self._client = client or httpx.AsyncClient(
            base_url=settings.live_api_url,
            timeout=settings.timeout,
            follow_redirects=False,
            transport=transport,
            headers={
                "User-Agent": settings.user_agent,
                "Accept": "application/json",
                "Cookie": settings.cookie,
            },
        )
        self._pacer = Pacer(settings.live_interval)
        self.guard = Guard(settings.write_interval, settings.write_daily_limit)
        self._modhash: str | None = None
        self.account: str | None = None
        # One write at a time: parallel tool calls must not race past the guard.
        self._lock = asyncio.Lock()

    def __repr__(self) -> str:
        return "RedditWriter(<session cookie hidden>)"

    async def aclose(self) -> None:
        await self._client.aclose()

    # ------------------------------------------------------------------ http

    @staticmethod
    def _why(response: httpx.Response) -> str:
        """A short, redacted reason for a refusal, from the response body."""
        if is_block_page(response):
            return "Reddit's 'blocked by network security' page"
        reason = ""
        if "json" in response.headers.get("content-type", ""):
            try:
                body = response.json()
            except ValueError:
                body = None
            if isinstance(body, dict):
                reason = str(body.get("message") or body.get("reason") or body.get("explanation") or "")
        if not reason:
            reason = " ".join(response.text[:160].split())
        return redact(reason) or "no details"

    @classmethod
    def _check_status(cls, response: httpx.Response) -> Any:
        status = response.status_code
        if 300 <= status < 400:
            raise Unavailable(
                "reddit.com redirected the request (usually to a login page); "
                "the session cookie is probably missing or expired"
            )
        if status in (401, 403):
            raise Unavailable(
                f"reddit.com refused the write (HTTP {status}: {cls._why(response)}); the session "
                "may be expired, the account suspended, or this endpoint blocked from this server"
            )
        if status == 404:
            raise NotFound("reddit.com: target not found")
        if status >= 400:
            raise Unavailable(f"reddit.com returned HTTP {status}")
        if "json" not in response.headers.get("content-type", ""):
            raise Unavailable("reddit.com returned a web page instead of JSON (likely a block or login page)")
        try:
            return response.json()
        except ValueError:
            raise Unavailable("reddit.com returned malformed JSON") from None

    def _url(self, door: str, path: str) -> str:
        # The client's base URL is oauth.reddit.com in browser mode, else www.
        return self._www + path if door == "www" and self._has_oauth else path

    async def _get_json(self, url: str) -> Any:
        response = await send(
            lambda: self._client.get(url, params={"raw_json": 1}), self._pacer, label="reddit.com"
        )
        return self._check_status(response)

    async def _load_www_me(self) -> None:
        body = await self._get_json(self._url("www", "/api/me.json"))
        data = body.get("data") if isinstance(body, dict) else None
        if not isinstance(data, dict) or not data.get("name"):
            raise Unavailable("reddit.com does not see a logged-in user; the session cookie is invalid or expired")
        if not data.get("modhash"):
            raise Unavailable("reddit.com returned no modhash for this session, so it cannot accept writes")
        self.account, self._modhash = str(data["name"]), str(data["modhash"])

    async def me(self) -> tuple[str, str]:
        """(username, modhash) of the logged-in account. The modhash is only
        needed, and only fetched, for writes that go through www.reddit.com."""
        if self.account is None:
            if self._has_oauth:
                body = await self._get_json("/api/v1/me")
                if not isinstance(body, dict) or not body.get("name"):
                    raise Unavailable(
                        "oauth.reddit.com does not see a logged-in user; the session cookie is invalid or expired"
                    )
                self.account = str(body["name"])
            else:
                await self._load_www_me()
        return self.account, self._modhash or ""

    async def _post(self, path: str, form: dict[str, Any], *, text: str | None, lenient: bool = False) -> Any:
        """Send one write.

        ``lenient``: an empty or non-JSON 2xx body counts as success (vote, save,
        delete answer with little or nothing). Otherwise it is an unknown outcome.

        A 403 from oauth.reddit.com (block page or plain refusal) means the
        request was not processed, so it is safe to try the www door once. A 403
        on www that is not a block page is usually a stale modhash: refresh it
        and try once more. Nothing else is ever resent.
        """
        # The duplicate guard keys on where the text goes as well as the text:
        # "thanks!" under two different comments is two different writes.
        text = None if text is None else f"{path}|{form.get('thing_id') or form.get('sr') or ''}|{text}"
        async with self._lock:
            self.guard.check(text)
            await self.me()
            remembered = self._doors.get(path)
            if not self._has_oauth or remembered == "www":
                doors = ["www"]
            else:
                doors = ["oauth", "www"]
            response: httpx.Response | None = None
            door = doors[0]
            for door in doors:
                response = await self._send_once(door, path, form, text)
                if door == "oauth" and response.status_code == 403:
                    continue
                if door == "www" and response.status_code == 403 and not is_block_page(response):
                    self._modhash = None
                    response = await self._send_once(door, path, form, text)
                break
            self.last_door = door
            body = self._finish(response, text=text, lenient=lenient)
            self._doors[path] = door
            return body

    async def _send_once(self, door: str, path: str, form: dict[str, Any], text: str | None) -> httpx.Response:
        modhash = ""
        if door == "www":
            if not self._modhash:
                await self._load_www_me()
            modhash = self._modhash or ""
        payload = {k: v for k, v in form.items() if v is not None}
        payload["api_type"] = "json"
        headers = {}
        if modhash:
            payload["uh"] = modhash
            headers["X-Modhash"] = modhash
        await self._pacer.wait()
        try:
            return await self._client.post(
                self._url(door, path), data=payload, headers=headers, params={"raw_json": 1}
            )
        except (httpx.ConnectError, httpx.ConnectTimeout):
            raise Unavailable("could not connect to reddit.com; nothing was sent") from None
        except httpx.HTTPError as exc:
            # Sent, but no answer: never resend automatically.
            self.guard.record(text)
            raise WriteUncertain(f"no response from reddit.com ({type(exc).__name__}); {UNCERTAIN}") from None

    def _finish(self, response: httpx.Response, *, text: str | None, lenient: bool) -> Any:
        status = response.status_code
        if status == 429:
            raise WriteRejected("reddit.com rate limit (HTTP 429); wait a few minutes")
        if status >= 500:
            self.guard.record(text)
            raise WriteUncertain(f"reddit.com server error (HTTP {status}); {UNCERTAIN}")
        is_json = "json" in response.headers.get("content-type", "")
        if 200 <= status < 300 and not is_json:
            if lenient:
                self.guard.record(text)
                return {}
            self.guard.record(text)
            raise WriteUncertain(f"reddit.com answered with a web page instead of JSON; {UNCERTAIN}")
        body = self._check_status(response)  # raises for 3xx/401/403/404: not processed
        errors = _form_errors(body)
        if errors:
            raise WriteRejected("reddit.com rejected the write: " + "; ".join(errors))
        self.guard.record(text)
        return body

    @staticmethod
    def _thing(body: Any) -> dict[str, Any]:
        data = (body.get("json") or {}).get("data") or {} if isinstance(body, dict) else {}
        things = data.get("things") or []
        if things and isinstance(things[0], dict):
            return things[0].get("data") or {}
        return data

    @staticmethod
    def _permalink(value: Any) -> str:
        if not value:
            return ""
        value = str(value)
        return value if value.startswith("http") else "https://www.reddit.com" + value

    # ------------------------------------------------------------------- api

    async def submit(
        self, subreddit: str, title: str, text: str | None, url: str | None,
        flair_id: str | None, nsfw: bool, spoiler: bool,
    ) -> WriteResult:
        if not title.strip():
            raise InvalidRequest("title is required")
        if len(title) > TITLE_MAX:
            raise InvalidRequest(f"title is longer than {TITLE_MAX} characters")
        if url and text:
            raise InvalidRequest("give either text (self post) or url (link post), not both")
        if text and len(text) > TEXT_MAX:
            raise InvalidRequest(f"text is longer than {TEXT_MAX} characters")
        body = await self._post(
            "/api/submit",
            {
                "sr": subreddit,
                "kind": "link" if url else "self",
                "title": title,
                "text": text if not url else None,
                "url": url,
                "flair_id": flair_id,
                "nsfw": "true" if nsfw else None,
                "spoiler": "true" if spoiler else None,
                "sendreplies": "true",
                "resubmit": "true",
            },
            text=title + "\n" + (text or url or ""),
        )
        data = self._thing(body)
        return WriteResult(
            action="submitted post",
            fullname=str(data.get("name") or (f"t3_{data['id']}" if data.get("id") else "")),
            permalink=self._permalink(data.get("url")),
            account=self.account or "",
        )

    async def reply(self, parent_fullname: str, text: str) -> WriteResult:
        if not text.strip():
            raise InvalidRequest("reply text is empty")
        if len(text) > TEXT_MAX:
            raise InvalidRequest(f"text is longer than {TEXT_MAX} characters")
        body = await self._post("/api/comment", {"thing_id": parent_fullname, "text": text}, text=text)
        data = self._thing(body)
        return WriteResult(
            action=f"replied to {parent_fullname}",
            fullname=str(data.get("name") or ""),
            permalink=self._permalink(data.get("permalink")),
            account=self.account or "",
        )

    async def edit(self, fullname: str, text: str) -> WriteResult:
        if not text.strip():
            raise InvalidRequest("new text is empty")
        body = await self._post("/api/editusertext", {"thing_id": fullname, "text": text}, text=text)
        data = self._thing(body)
        return WriteResult(
            action=f"edited {fullname}",
            fullname=fullname,
            permalink=self._permalink(data.get("permalink")),
            account=self.account or "",
        )

    async def delete(self, fullname: str) -> WriteResult:
        await self._post("/api/del", {"id": fullname}, text=None, lenient=True)
        return WriteResult(
            action=f"asked reddit.com to delete {fullname}",
            fullname=fullname,
            account=self.account or "",
            notes=[
                "reddit.com answers success even for things this account did not write (and then "
                "does nothing); check get_user_activity to confirm it is gone"
            ],
        )

    async def vote(self, fullname: str, direction: int) -> WriteResult:
        await self._post("/api/vote", {"id": fullname, "dir": str(direction)}, text=None, lenient=True)
        word = {1: "upvoted", -1: "downvoted", 0: "cleared vote on"}[direction]
        return WriteResult(action=f"{word} {fullname}", fullname=fullname, account=self.account or "")

    async def save(self, fullname: str, saved: bool) -> WriteResult:
        await self._post(
            "/api/save" if saved else "/api/unsave", {"id": fullname}, text=None, lenient=True
        )
        return WriteResult(
            action=("saved " if saved else "unsaved ") + fullname, fullname=fullname, account=self.account or ""
        )
