"""Small HTTP helpers: pacing, retry on 429/5xx, TTL cache."""

from __future__ import annotations

import asyncio
import time
from collections import OrderedDict
from collections.abc import Awaitable, Callable
from typing import Any

import httpx

from .errors import Unavailable

MAX_WAIT = 30.0

# Indirection so tests can skip real sleeping.
sleep: Callable[[float], Awaitable[None]] = asyncio.sleep
monotonic: Callable[[], float] = time.monotonic


class Pacer:
    """Keep at least ``interval`` seconds between requests to one host."""

    def __init__(self, interval: float) -> None:
        self.interval = max(0.0, interval)
        self._next = 0.0
        self._lock = asyncio.Lock()

    async def wait(self) -> None:
        async with self._lock:
            now = monotonic()
            if now < self._next:
                await sleep(self._next - now)
                now = monotonic()
            self._next = now + self.interval


def retry_after(response: httpx.Response) -> float:
    """Seconds to wait after a 429, from whichever header the server sent."""
    headers = response.headers
    for name in ("retry-after", "x-ratelimit-reset"):
        raw = headers.get(name)
        if raw:
            try:
                return min(MAX_WAIT, max(0.0, float(raw)))
            except ValueError:
                pass
    raw = headers.get("x-ratelimit-reset-at")
    if raw:
        try:
            return min(MAX_WAIT, max(0.0, float(raw) - time.time()))
        except ValueError:
            pass
    return 5.0


async def send(
    request: Callable[[], Awaitable[httpx.Response]],
    pacer: Pacer,
    *,
    label: str,
    retries: int = 2,
) -> httpx.Response:
    """Send with pacing; retry 429, 5xx and transport errors a few times."""
    attempt = 0
    while True:
        await pacer.wait()
        try:
            response = await request()
        except httpx.TimeoutException:
            if attempt >= retries:
                raise Unavailable(f"{label} timed out") from None
            await sleep(2.0 * (attempt + 1))
        except httpx.HTTPError as exc:
            if attempt >= retries:
                raise Unavailable(f"{label} unreachable ({type(exc).__name__})") from None
            await sleep(2.0 * (attempt + 1))
        else:
            if response.status_code == 429:
                if attempt >= retries:
                    raise Unavailable(f"{label} rate limit hit (HTTP 429); try again later")
                await sleep(retry_after(response))
            elif response.status_code >= 500:
                if attempt >= retries:
                    raise Unavailable(f"{label} server error (HTTP {response.status_code})")
                await sleep(2.0 * (attempt + 1))
            else:
                return response
        attempt += 1


def quiet_http_logs() -> None:
    """httpx logs every request URL at INFO, and FastMCP turns INFO on. Those
    URLs carry what the user searched for and whom they looked up, so they
    stay out of the platform's logs."""
    import logging

    for name in ("httpx", "httpcore"):
        logging.getLogger(name).setLevel(logging.WARNING)


class KeepOpen(httpx.AsyncBaseTransport):
    """Lend a transport (e.g. one browser) to a short-lived client without
    letting that client close it."""

    def __init__(self, inner: Any) -> None:
        self.inner = inner

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        return await self.inner.handle_async_request(request)

    async def aclose(self) -> None:
        pass

    def __getattr__(self, name: str) -> Any:  # bearer(), _context, ... of the browser
        return getattr(self.inner, name)


class TTLCache:
    def __init__(self, ttl: float, maxsize: int = 512) -> None:
        self.ttl = ttl
        self.maxsize = maxsize
        self._data: OrderedDict[Any, tuple[float, Any]] = OrderedDict()

    def get(self, key: Any) -> Any | None:
        if self.ttl <= 0:
            return None
        hit = self._data.get(key)
        if hit is None:
            return None
        expires, value = hit
        if monotonic() >= expires:
            del self._data[key]
            return None
        self._data.move_to_end(key)
        return value

    def clear(self) -> None:
        self._data.clear()

    def set(self, key: Any, value: Any) -> None:
        if self.ttl <= 0:
            return
        self._data[key] = (monotonic() + self.ttl, value)
        self._data.move_to_end(key)
        while len(self._data) > self.maxsize:
            self._data.popitem(last=False)
