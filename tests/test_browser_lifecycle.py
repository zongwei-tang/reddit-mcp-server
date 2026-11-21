"""BrowserTransport lifecycle with a fake Playwright: crash recovery, cleanup
on failed starts, per-request timeouts and token refresh coalescing."""

import asyncio
import sys
import types

import httpx
import pytest

from reddit_mcp import browser as browser_mod
from reddit_mcp.browser import BrowserTransport

OK = {"status": 200, "statusText": "", "url": "https://www.reddit.com/x", "headers": {"content-type": "application/json"}, "body": "{}"}


class FakePage:
    def __init__(self, world, origin=None):
        self.world = world
        self.closed = False
        self.origin = origin

    async def goto(self, url, **kw):
        self.world.gotos.append(url)
        if self.world.goto_fails:
            raise RuntimeError("navigation failed")

    async def wait_for_load_state(self, *a, **kw):
        pass

    async def evaluate(self, script, arg):
        self.world.evaluations.append(arg["url"])
        if self.world.evaluate_hook:
            return await self.world.evaluate_hook(self, arg)
        return dict(OK, url=arg["url"])

    def is_closed(self):
        return self.closed

    async def close(self):
        self.closed = True
        self.world.closed_pages += 1


class FakeContext:
    def __init__(self, world):
        self.world = world
        self.token = None

    async def new_page(self):
        page = FakePage(self.world)
        self.world.pages.append(page)
        return page

    async def add_cookies(self, cookies):
        if self.world.add_cookies_fails:
            raise RuntimeError("bad cookie")
        for c in cookies:
            if c["name"] == "token_v2":
                self.token = c["value"]

    async def cookies(self, url):
        return [{"name": "token_v2", "value": self.token}] if self.token else []

    async def clear_cookies(self, name=None):
        self.token = None

    async def close(self):
        self.world.closed_contexts += 1


class FakeBrowser:
    version = "136.0.7103.25"

    def __init__(self, world):
        self.world = world
        self.connected = True

    def is_connected(self):
        return self.connected

    async def new_context(self, **kw):
        self.world.context = FakeContext(self.world)
        return self.world.context

    async def close(self):
        self.connected = False
        self.world.closed_browsers += 1


class World:
    def __init__(self):
        self.launches = 0
        self.stops = 0
        self.gotos, self.evaluations, self.pages = [], [], []
        self.closed_pages = self.closed_contexts = self.closed_browsers = 0
        self.goto_fails = False
        self.add_cookies_fails = False
        self.evaluate_hook = None
        self.browser = None
        self.context = None

    def module(self):
        world = self

        class Chromium:
            async def launch(self, **kw):
                world.launches += 1
                world.browser = FakeBrowser(world)
                return world.browser

        class PW:
            chromium = Chromium()

            async def stop(self):
                world.stops += 1

        class Starter:
            async def start(self):
                return PW()

        mod = types.ModuleType("playwright.async_api")
        mod.async_playwright = lambda: Starter()
        return mod


@pytest.fixture
def world(monkeypatch):
    w = World()
    monkeypatch.setitem(sys.modules, "playwright.async_api", w.module())
    return w


async def get(transport, url="https://www.reddit.com/api/me.json", timeout=5.0):
    async with httpx.AsyncClient(transport=browser_mod_keep(transport), timeout=timeout) as client:
        return await client.get(url)


def browser_mod_keep(t):
    from reddit_mcp.http import KeepOpen

    return KeepOpen(t)


async def test_relaunches_after_browser_crash(world):
    t = BrowserTransport("reddit_session=x")
    assert (await get(t)).status_code == 200
    assert world.launches == 1
    world.browser.connected = False  # Chromium died
    assert (await get(t)).status_code == 200
    assert world.launches == 2  # recovered by itself


async def test_crashed_page_is_dropped_and_reopened(world):
    t = BrowserTransport("reddit_session=x")
    await get(t)

    async def crash_once(page, arg):
        world.evaluate_hook = None
        raise RuntimeError("Target crashed")

    world.evaluate_hook = crash_once
    with pytest.raises(httpx.ReadError):
        await get(t)
    pages_before = len(world.pages)
    assert (await get(t)).status_code == 200
    assert len(world.pages) == pages_before + 1  # a fresh page, same browser
    assert world.launches == 1


async def test_failed_start_cleans_up_and_next_request_retries(world):
    world.add_cookies_fails = True
    t = BrowserTransport("reddit_session=x")
    with pytest.raises(httpx.ConnectError):
        await get(t)
    assert world.closed_browsers == 1 and world.stops == 1  # nothing leaked
    world.add_cookies_fails = False
    assert (await get(t)).status_code == 200
    assert world.launches == 2


async def test_failed_page_load_closes_the_page(world):
    t = BrowserTransport("reddit_session=x")
    world.goto_fails = True
    with pytest.raises(httpx.ConnectError):
        await get(t)
    assert world.pages and all(p.closed for p in world.pages)
    world.goto_fails = False
    assert (await get(t)).status_code == 200


async def test_request_timeout_is_honoured(world):
    t = BrowserTransport("reddit_session=x")
    await get(t)

    async def hang(page, arg):
        await asyncio.sleep(10)

    world.evaluate_hook = hang
    with pytest.raises(httpx.ReadTimeout):
        await get(t, timeout=0.2)


async def test_slow_origin_does_not_block_another_origin(world):
    t = BrowserTransport("reddit_session=x")
    await get(t)  # browser up, www page open
    gate = asyncio.Event()
    original_goto = FakePage.goto

    async def slow_goto(self, url, **kw):
        if "old.reddit.com" in url:
            await gate.wait()
        await original_goto(self, url, **kw)

    FakePage.goto = slow_goto
    try:
        slow = asyncio.create_task(get(t, "https://old.reddit.com/r/x/search", timeout=5))
        await asyncio.sleep(0.05)
        fast = await asyncio.wait_for(get(t, "https://www.reddit.com/r/x/about.json"), timeout=1)
        assert fast.status_code == 200
        gate.set()
        assert (await slow).status_code == 200
    finally:
        FakePage.goto = original_goto


async def test_forced_refreshes_are_coalesced(world):
    import base64
    import json
    import time

    body = base64.urlsafe_b64encode(json.dumps({"exp": time.time() + 86400}).encode()).decode().rstrip("=")
    t = BrowserTransport(f"reddit_session=x; token_v2=eyJ0.{body}.sig")
    token = await t.bearer()
    opened = len(world.pages)
    # Three requests got a 401 for the same token at once: one renewal attempt.
    await asyncio.gather(*(t.bearer(rejected=token) for _ in range(3)))
    assert len(world.pages) == opened + 1  # one throwaway page, not three
    assert all(p.closed for p in world.pages[opened:])  # and it was closed
