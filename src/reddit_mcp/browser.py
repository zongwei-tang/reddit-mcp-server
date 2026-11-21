"""An httpx transport that sends every request from inside a real Chromium page.

Why: Reddit blocks some clients at the network edge before looking at the
session cookie. A request made with ``fetch()`` inside a Chromium page uses the
browser's own network stack (TLS, HTTP/2, headers), exactly like the user's
browser would. It does not change the server's IP address.

The rest of the code keeps using httpx; only the transport differs, so the
live source, the writer and the diagnosis work unchanged.

Robustness rules:
  * the browser is relaunched if it crashed or disconnected, and a page that
    failed is dropped, so one bad moment never breaks the live source for good;
  * a failed start cleans up everything it created;
  * one lock guards the browser start, another each origin's page, so a slow
    page load never stalls requests to other origins;
  * each request honours its own httpx timeout.

Enable with REDDIT_LIVE_CLIENT=browser (needs the ``browser`` extra and a
Chromium build, which the Playwright Docker image provides).
"""

from __future__ import annotations

import asyncio
import base64
import json
import time
from typing import Any

import httpx

# Runs inside the page. Same-origin fetch with the context's cookies.
FETCH_JS = """
async ({url, method, headers, body}) => {
  const init = {method, headers, credentials: "include", redirect: "follow"};
  if (body !== null) init.body = body;
  try {
    const r = await fetch(url, init);
    const out = {status: r.status, statusText: r.statusText, url: r.url, headers: {}};
    r.headers.forEach((v, k) => { out.headers[k] = v; });
    out.body = await r.text();
    return out;
  } catch (e) {
    return {error: String(e)};
  }
}
"""

DEFAULT_FETCH_TIMEOUT = 45.0
PAGE_LOAD_TIMEOUT_MS = 45_000
IDLE_WAIT_MS = 15_000
OAUTH_HOST = "oauth.reddit.com"
WEB_ORIGIN = "https://www.reddit.com"
TOKEN_COOKIE = "token_v2"
TOKEN_MARGIN = 300.0  # a request renews token_v2 when it has less than this many seconds left
REFRESH_COOLDOWN = 30.0  # after a failed token renewal, wait this long before the next

# Headers the browser must set itself; passing them from httpx would be
# rejected by fetch() or would make the request look unlike a browser's.
DROP_HEADERS = {
    "host", "cookie", "user-agent", "content-length", "connection",
    "accept-encoding", "transfer-encoding",
}


def jwt_exp(token: str) -> float | None:
    """Expiry of a JWT (no signature check; only used to decide when to refresh)."""
    try:
        payload = token.split(".")[1]
        payload += "=" * (-len(payload) % 4)
        return float(json.loads(base64.urlsafe_b64decode(payload))["exp"])
    except Exception:
        return None


def parse_cookie_header(cookie: str, domain: str = ".reddit.com") -> list[dict[str, Any]]:
    cookies = []
    for part in cookie.split(";"):
        name, sep, value = part.strip().partition("=")
        if not sep or not name:
            continue
        cookies.append(
            {"name": name, "value": value, "domain": domain, "path": "/", "secure": True, "httpOnly": True}
        )
    return cookies


def _not_expired(token: str) -> bool:
    exp = jwt_exp(token)
    return exp is None or exp > time.time()


async def _close_quietly(obj: Any) -> None:
    if obj is None:
        return
    try:
        await obj.close()
    except Exception:
        pass


SESSION_COOKIE = "reddit_session"
REDDIT_URLS = ("https://www.reddit.com", "https://old.reddit.com", "https://oauth.reddit.com")
WARN_DAYS = 14.0  # say so in answers when the login itself is about to run out
TOKEN_RENEW_HOURS = 12.0  # the keeper renews token_v2 when less than this is left


class BrowserTransport(httpx.AsyncBaseTransport):
    def __init__(self, cookie: str | None, *, headless: bool = True, store: Any = None) -> None:
        from .session_store import fingerprint

        self._cookie = cookie or ""
        self._source = fingerprint(self._cookie)
        self._store = store
        self.seeded_from = "config"
        self.session_note: str | None = None
        self.last_save_error: str | None = None
        self._login_failures = 0
        self._headless = headless
        self._start_lock = asyncio.Lock()
        self._origin_locks: dict[str, asyncio.Lock] = {}
        self._renew_lock = asyncio.Lock()
        self._last_attempt = float("-inf")  # end of the last token renewal attempt
        self._last_warm_up_failure = float("-inf")
        self._pw: Any = None
        self._browser: Any = None
        self._context: Any = None
        self._pages: dict[str, Any] = {}

    def __repr__(self) -> str:
        return "BrowserTransport(<session cookie hidden>)"

    # ------------------------------------------------------------ lifecycle

    def _alive(self) -> bool:
        if self._context is None or self._browser is None:
            return False
        try:
            return bool(self._browser.is_connected())
        except Exception:
            return False

    async def _reset(self) -> None:
        """Close whatever exists; the next request starts a fresh browser."""
        pages, self._pages = list(self._pages.values()), {}
        for page in pages:
            await _close_quietly(page)
        await _close_quietly(self._context)
        await _close_quietly(self._browser)
        if self._pw is not None:
            try:
                await self._pw.stop()
            except Exception:
                pass
        self._pw = self._browser = self._context = None
        # A new browser may renew its token and open pages at once.
        self._last_attempt = self._last_warm_up_failure = float("-inf")

    def _context_options(self) -> dict[str, Any]:
        # Use the real Chromium version so the UA agrees with the browser's
        # own client hints; only drop the "Headless" marker.
        major = str(self._browser.version).split(".")[0]
        return {
            "locale": "en-US",
            "user_agent": (
                "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                f"(KHTML, like Gecko) Chrome/{major}.0.0.0 Safari/537.36"
            ),
        }

    async def _ensure_context(self) -> Any:
        async with self._start_lock:
            if self._alive():
                return self._context
            await self._reset()  # a crashed or half-started browser is discarded
            try:
                from playwright.async_api import async_playwright
            except ImportError:
                raise httpx.ConnectError(
                    "REDDIT_LIVE_CLIENT=browser needs playwright (install the 'browser' extra)"
                ) from None
            try:
                self._pw = await async_playwright().start()
                self._browser = await self._pw.chromium.launch(
                    headless=self._headless, args=["--disable-dev-shm-usage"]
                )
                context = await self._browser.new_context(**self._context_options())
                await self._seed(context)
                self._context = context
            except Exception:
                await self._reset()
                raise
            return self._context

    async def _seed(self, context: Any) -> None:
        """Give a new browser its cookies. Prefer the session saved on the
        volume (it carries every renewal since the cookie was configured),
        unless the configured cookie changed since: then the new one wins.

        The saved file is blamed only once the same context has proved
        healthy by taking the configured cookie instead: a browser that died
        mid-start keeps the file for the next attempt, while a file the
        browser really refuses is set aside instead of failing every start."""
        stored = None
        if self._store is not None:
            try:
                stored = self._store.load(self._source)
            except Exception:
                stored = None
        if stored:
            try:
                await context.add_cookies(stored)
                self.seeded_from = "store"
                return
            except Exception:
                await context.clear_cookies()  # if these fail, the error goes up and the file stays
                if self._cookie:
                    await context.add_cookies(parse_cookie_header(self._cookie))
                else:
                    await context.cookies()
                self._store.discard()
                self.seeded_from = "config (saved session refused)"
                return
        self.seeded_from = "config"
        if self._cookie:
            await context.add_cookies(parse_cookie_header(self._cookie))

    async def _open(self, context: Any, origin: str) -> Any:
        """A new page that has opened ``origin`` and let its scripts run, as a
        normal visit would: Reddit sets session or verification cookies there
        that later fetch() calls need. A block page still gives a same-origin
        document to fetch from."""
        page = await context.new_page()
        try:
            await page.goto(origin + "/", wait_until="domcontentloaded", timeout=PAGE_LOAD_TIMEOUT_MS)
            try:
                await page.wait_for_load_state("networkidle", timeout=IDLE_WAIT_MS)
            except Exception:
                pass  # busy pages never go idle; the document is enough
        except BaseException:
            await _close_quietly(page)
            raise
        return page

    async def _page_for(self, origin: str) -> Any:
        context = await self._ensure_context()
        lock = self._origin_locks.setdefault(origin, asyncio.Lock())
        async with lock:
            page = self._pages.get(origin)
            if page is not None and not page.is_closed() and self._alive():
                return page
            page = await self._open(context, origin)
            self._pages[origin] = page
            return page

    async def _drop_page(self, origin: str) -> None:
        page = self._pages.pop(origin, None)
        await _close_quietly(page)

    # ---------------------------------------------------------------- token

    async def _token_from_context(self) -> str | None:
        if self._context is None:
            return None
        for cookie in await self._context.cookies(WEB_ORIGIN):
            if cookie.get("name") == TOKEN_COOKIE:
                return cookie.get("value")
        return None

    @staticmethod
    def _needs_renewal(token: str | None, rejected: str | None) -> bool:
        if not token or (rejected is not None and token == rejected):
            return True
        exp = jwt_exp(token)
        return exp is not None and exp - time.time() < TOKEN_MARGIN

    async def _warm_up(self) -> None:
        """Open the shared www page, the visit Reddit expects before API calls
        (and whose own scripts keep the session fresh, as in a real tab).
        Best effort: a request that already holds a working token must not
        fail because the front page did, and a failure is not retried at once."""
        page = self._pages.get(WEB_ORIGIN)
        if page is not None and not page.is_closed() and self._alive():
            return
        if time.monotonic() - self._last_warm_up_failure < REFRESH_COOLDOWN:
            return
        try:
            await self._page_for(WEB_ORIGIN)
        except Exception:
            self._last_warm_up_failure = time.monotonic()

    async def bearer(self, *, rejected: str | None = None) -> str | None:
        """The session's current token_v2, the token reddit.com's own web app
        sends to oauth.reddit.com. Renewed when there is none, when it is
        minutes from expiring, or when Reddit just refused it (``rejected`` is
        the token that got a 401).

        A caller whose token still works never waits for a renewal; callers
        that need one share a single attempt; and an attempt that failed is
        not repeated for REFRESH_COOLDOWN seconds, so a missing token can never
        set off a reload storm."""
        await self._warm_up()
        token = await self._token_from_context()
        if not self._needs_renewal(token, rejected):
            return token
        return await self._renew(rejected=rejected, force=False)

    async def renew_token(self) -> str | None:
        """Get a fresh token_v2 now, cooldown or not: the session keeper and
        the startup token-check. The old token stays until a new one exists."""
        await self._warm_up()
        return await self._renew(rejected=None, force=True)

    async def _renew(self, *, rejected: str | None, force: bool) -> str | None:
        async with self._renew_lock:
            token = await self._token_from_context()
            if not force:
                if not self._needs_renewal(token, rejected):
                    return token  # renewed by someone else while this caller waited
                if time.monotonic() - self._last_attempt < REFRESH_COOLDOWN:
                    return token  # it just failed; asking again at once won't help
            try:
                issued = await self._issue_token()
            except Exception:
                if force:
                    raise  # the keeper and the token-check report it
                # A caller whose token still works must not fail because
                # the renewal did; only one without a usable token sees it.
                token = await self._token_from_context()
                if token and token != rejected and _not_expired(token):
                    return token
                raise
            finally:
                self._last_attempt = time.monotonic()  # failures start the cooldown too
            if issued:
                await self.save_session()
            return await self._token_from_context()

    async def _issue_token(self) -> bool:
        """Have Reddit issue a fresh token_v2 without ever leaving the browser
        without a working one.

        Reddit hands a token only to a browser that has none: a visit leaves a
        still-valid token alone, even minutes before it expires (seen from
        Railway). So the visit happens in a scratch context that holds every
        cookie except token_v2. Only when a new token came back are it, and any
        other cookie the visit renewed, copied into the browser, in one
        add_cookies call; anything that fails earlier leaves the browser as it
        was. Returns whether a new token arrived."""
        from .session_store import clean_cookie

        context = await self._ensure_context()
        current = await context.cookies(list(REDDIT_URLS))
        seed = [c for c in map(clean_cookie, current) if c and c["name"] != TOKEN_COOKIE]
        scratch = await self._browser.new_context(**self._context_options())
        try:
            if seed:
                await scratch.add_cookies(seed)
            page = await self._open(scratch, WEB_ORIGIN)
            await _close_quietly(page)
            issued = [c for c in map(clean_cookie, await scratch.cookies(list(REDDIT_URLS))) if c]
        finally:
            # Shielded: a cancelled renewal still closes its scratch context.
            await asyncio.shield(_close_quietly(scratch))
        fresh = next((c for c in issued if c["name"] == TOKEN_COOKIE and c["value"]), None)
        old = [c for c in current if c.get("name") == TOKEN_COOKIE]
        if fresh is None or any(c.get("value") == fresh["value"] for c in old):
            return False
        known = {(c.get("name"), c.get("domain"), c.get("path")): c.get("value") for c in current}
        updates = {
            (c["name"], c.get("domain"), c.get("path")): c
            for c in issued
            if known.get((c["name"], c.get("domain"), c.get("path"))) != c["value"]
        }
        # The new token also replaces every copy of the old one, wherever it
        # was stored, so no stale copy can shadow it.
        for c in old:
            key = (TOKEN_COOKIE, c.get("domain"), c.get("path"))
            updates[key] = {**fresh, "domain": c.get("domain"), "path": c.get("path") or "/"}
        await context.add_cookies(list(updates.values()))
        return True

    # -------------------------------------------------------------- session

    async def save_session(self) -> int:
        """Write the browser's current Reddit cookies to the store (if any).
        A failing disk never fails a request; the error is kept for reports."""
        if self._store is None or self._context is None:
            return 0
        try:
            count = self._store.save(await self._context.cookies(list(REDDIT_URLS)), self._source)
            self.last_save_error = None
            return count
        except Exception as exc:
            self.last_save_error = f"{type(exc).__name__}: {str(exc)[:120]}"
            return 0

    async def session_report(self) -> dict[str, Any]:
        """Names and expiry of the session cookies; never their values.
        ``readable`` is False when the browser could not be asked at all,
        which says nothing about the login."""
        cookies: list[dict[str, Any]] = []
        readable = False
        if self._context is not None:
            try:
                cookies = await self._context.cookies(list(REDDIT_URLS))
                readable = True
            except Exception:
                cookies = []
        by_name = {c.get("name"): c for c in cookies}
        now = time.time()

        def seconds_left(name: str) -> float | None:
            cookie = by_name.get(name)
            if not cookie:
                return None
            exp = jwt_exp(str(cookie.get("value") or ""))
            expires = cookie.get("expires")
            if exp is None and isinstance(expires, int | float) and expires > 0:
                exp = float(expires)
            return None if exp is None else exp - now

        session_left, token_left = seconds_left(SESSION_COOKIE), seconds_left(TOKEN_COOKIE)
        return {
            "readable": readable,
            "cookies": len(cookies),
            "session_cookie": SESSION_COOKIE in by_name,
            "session_days_left": None if session_left is None else round(session_left / 86400, 2),
            "token_hours_left": None if token_left is None else round(token_left / 3600, 1),
            "seeded_from": self.seeded_from,
            "save_error": self.last_save_error,
        }

    async def _visit(self, context: Any) -> None:
        """A plain front-page load in a throwaway page, as a returning user's."""
        page = await self._open(context, WEB_ORIGIN)
        await _close_quietly(page)

    async def keepalive(self) -> dict[str, Any]:
        """Visit reddit.com like a returning user, so Reddit can renew the
        session cookies, renew token_v2 when it has under TOKEN_RENEW_HOURS
        left, check the login still works and save the session. Used by the
        periodic session keeper. Requests holding a working token never wait
        for any of this.

        The note shown in answers changes only on clear evidence: the login
        cookie is gone or expired, Reddit refused a token it was sent (401),
        or two checks in a row failed. A flaky check, or a browser that could
        not even be asked, only goes to the log."""
        context = await self._ensure_context()
        before = await self.session_report()
        hours = before.get("token_hours_left")
        renewed, visit_error = False, None
        try:
            if hours is None or hours < TOKEN_RENEW_HOURS:
                old = await self._token_from_context()
                new = await self.renew_token()
                renewed = bool(new) and new != old
            else:
                await self._visit(context)
        except Exception as exc:  # the old token stays; the login check still runs
            visit_error = type(exc).__name__
        try:
            await self._ensure_context()  # relaunches (from the store) if the browser died
            token = await self._token_from_context()
        except Exception:
            token = None
        checked_on = self._context
        account, statuses = None, []
        doors: list[tuple[str, Any]] = [(f"{WEB_ORIGIN}/api/me.json", lambda d: (d.get("data") or {}).get("name"))]
        if token:
            doors.insert(0, (f"https://{OAUTH_HOST}/api/v1/me", lambda d: d.get("name")))
        else:
            statuses.append("no token")  # nothing to send to oauth; ask www only
        # Either door confirming the account is enough: one blocked door is
        # not a lost login.
        for url, pick in doors:
            try:
                r = await self.handle_async_request(httpx.Request("GET", url))
                statuses.append(str(r.status_code))
                if r.status_code == 200 and "json" in r.headers.get("content-type", ""):
                    data = json.loads(r.content)
                    account = (pick(data) if isinstance(data, dict) else None) or None
            except Exception as exc:
                statuses.append(type(exc).__name__)
            if account:
                break
        # A 401 proves a refused login only for a token that could still have
        # worked; one for an expired token proves nothing.
        refused = bool(token) and _not_expired(token) and statuses[:1] == ["401"]
        # A check that only met transport errors, or ran across a browser
        # restart, says nothing about the login.
        crashed = self._context is not checked_on or not any(x.isdigit() for x in statuses)
        saved = await self.save_session()
        after = await self.session_report()
        if not after["readable"]:
            try:  # the browser died during the check: look at it once more
                await self._ensure_context()
            except Exception:
                pass
            after = await self.session_report()
        inconclusive = not account and (crashed or not after["readable"])
        if account:
            self._login_failures = 0
        elif not inconclusive:
            self._login_failures += 1
        days = after.get("session_days_left")
        fix = "set a fresh cookie with scripts/set-railway-cookie.sh"
        if not after["readable"]:
            pass  # the browser could not be asked; this says nothing about the login
        elif not after["session_cookie"] or (days is not None and days <= 0):
            self.session_note = f"the Reddit login cookie of this server is gone or expired; {fix}"
        elif inconclusive:
            pass
        elif not account and (refused or self._login_failures >= 2):
            self.session_note = f"the Reddit login of this server no longer works; {fix}"
        elif account and days is not None and days < WARN_DAYS:
            self.session_note = f"the Reddit login of this server expires in {days:.0f} days; {fix} before then"
        elif account:
            self.session_note = None
        # else: one inconclusive check; keep whatever the note said before.
        return {
            "account": account,
            "status": "/".join(statuses),
            "failures": self._login_failures,
            "renewed": renewed,
            "visit_error": visit_error,
            "saved": saved,
            "before": before,
            "after": after,
        }

    # ------------------------------------------------------------- requests

    @staticmethod
    def _timeout(request: httpx.Request) -> float:
        timeouts = request.extensions.get("timeout") or {}
        value = timeouts.get("read") if isinstance(timeouts, dict) else None
        return float(value) if value else DEFAULT_FETCH_TIMEOUT

    async def _token_for(self, request: httpx.Request, *, rejected: str | None = None) -> str | None:
        try:
            return await self.bearer(rejected=rejected)
        except httpx.HTTPError:
            raise
        except Exception as exc:
            await self._reset_if_dead()
            raise httpx.ConnectError(f"browser start failed: {type(exc).__name__}", request=request) from None

    async def _reset_if_dead(self) -> None:
        # Under the start lock, so a failing request never tears down a
        # browser another task is starting right now.
        async with self._start_lock:
            if not self._alive():
                await self._reset()

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        own_token = any(k.lower() == "authorization" for k in request.headers)
        oauth = request.url.host == OAUTH_HOST and not own_token
        token = await self._token_for(request) if oauth else None
        response = await self._send(request, token)
        if oauth and response.status_code == 401:
            # Reddit refused the token: renew once and resend. A 401 was not
            # processed, so resending is safe.
            new = await self._token_for(request, rejected=token)
            if new and new != token:
                response = await self._send(request, new)
        return response

    async def _send(self, request: httpx.Request, token: str | None) -> httpx.Response:
        origin = f"{request.url.scheme}://{request.url.host}"
        headers = {k: v for k, v in request.headers.items() if k.lower() not in DROP_HEADERS}
        if token:
            headers["Authorization"] = f"Bearer {token}"
        body = request.content.decode("utf-8") if request.content else None
        # Playwright errors become httpx errors the callers already handle:
        # failing before the fetch means nothing was sent (ConnectError);
        # failing during the fetch means it may have been sent (ReadError).
        try:
            page = await self._page_for(origin)
        except httpx.HTTPError:
            raise
        except Exception as exc:
            await self._reset_if_dead()
            raise httpx.ConnectError(f"browser start failed: {type(exc).__name__}", request=request) from None
        try:
            result = await asyncio.wait_for(
                page.evaluate(
                    FETCH_JS,
                    {"url": str(request.url), "method": request.method, "headers": headers, "body": body},
                ),
                timeout=self._timeout(request),
            )
        except TimeoutError:
            raise httpx.ReadTimeout("browser fetch timed out", request=request) from None
        except Exception as exc:
            # The page (or the whole browser) died: forget it so the next
            # request opens a fresh one instead of failing forever.
            if self._alive():
                await self._drop_page(origin)
            else:
                await self._reset_if_dead()
            raise httpx.ReadError(f"browser error: {type(exc).__name__}", request=request) from None
        if "error" in result:
            raise httpx.ReadError(f"browser fetch failed: {result['error'][:200]}", request=request)
        response_headers = [
            (k, v) for k, v in result["headers"].items() if k.lower() not in ("content-encoding", "content-length")
        ]
        if result.get("url"):
            # fetch() follows redirects itself; keep where it ended up.
            response_headers.append(("x-final-url", str(result["url"])))
        return httpx.Response(
            status_code=int(result["status"]),
            headers=response_headers,
            content=result["body"].encode("utf-8"),
            request=request,
            extensions={"reason_phrase": (result.get("statusText") or "").encode()},
        )

    async def aclose(self) -> None:
        async with self._start_lock:
            await self._reset()
