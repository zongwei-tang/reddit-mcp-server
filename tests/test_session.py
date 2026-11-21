"""Session kept across restarts: the store file, the browser seeding from it,
the periodic keeper, and how a failing login reaches the answers."""

import asyncio
import base64
import json
import os
import stat
import sys
import time
import types

import httpx
import pytest

from reddit_mcp.browser import BrowserTransport
from reddit_mcp.config import Settings
from reddit_mcp.http import KeepOpen
from reddit_mcp.http_app import Lifespan, describe_keepalive, session_keeper
from reddit_mcp.models import Page
from reddit_mcp.router import Router
from reddit_mcp.session_store import SessionStore, fingerprint
from reddit_mcp.sources.live import make_transport

DAY = 86400.0


def jwt(exp: float, tag: str) -> str:
    def part(obj):
        return base64.urlsafe_b64encode(json.dumps(obj).encode()).decode().rstrip("=")

    return f"{part({'alg': 'none'})}.{part({'exp': exp, 'tag': tag})}.sig"


def reddit_cookie(name, value, expires=-1, domain=".reddit.com"):
    return {"name": name, "value": value, "domain": domain, "path": "/", "expires": expires,
            "httpOnly": True, "secure": True, "sameSite": "None"}


# ----------------------------------------------------------- fake Playwright


class FakePage:
    def __init__(self, world, context):
        self.world = world
        self.context = context
        self.closed = False

    async def goto(self, url, **kw):
        self.world.visits += 1
        if self.world.goto_gate is not None:
            await self.world.goto_gate.wait()
        if self.world.goto_fails:
            raise TimeoutError("navigation timed out")
        if self.world.on_visit:
            self.world.on_visit(self.context.jar)

    async def wait_for_load_state(self, *a, **kw):
        pass

    async def evaluate(self, script, arg):
        auth = arg["headers"].get("Authorization", "")
        status, body, ctype = self.world.reply(arg["url"], auth.removeprefix("Bearer ") or None)
        return {"status": status, "statusText": "", "url": arg["url"], "headers": {"content-type": ctype}, "body": body}

    def is_closed(self):
        return self.closed

    async def close(self):
        self.closed = True


class FakeContext:
    """Keeps a cookie jar and answers cookies() the way Playwright does, with
    every field filled in and one Playwright may add that add_cookies refuses."""

    def __init__(self, world):
        self.world = world
        self.jar = {}
        self.closed = False

    async def new_page(self):
        return FakePage(self.world, self)

    async def add_cookies(self, cookies):
        if self.world.refuse and self.world.refuse(cookies):
            raise RuntimeError("Invalid cookie fields")
        for c in cookies:
            self.jar[c["name"]] = dict(c)

    async def cookies(self, urls=None):
        if self.world.cookies_fail:
            raise RuntimeError("Target closed")
        defaults = {"expires": -1, "httpOnly": False, "secure": False, "sameSite": "Lax", "partitionKey": "pk"}
        return [{**defaults, **c} for c in self.jar.values()]

    async def clear_cookies(self, name=None, **kw):
        if self.world.clear_fails:
            raise RuntimeError("Target closed")
        if name is None:
            self.jar.clear()
        else:
            self.jar.pop(name, None)

    async def close(self):
        self.closed = True


class FakeBrowser:
    version = "136.0.7103.25"

    def __init__(self, world):
        self.world = world

    def is_connected(self):
        return True

    async def new_context(self, **kw):
        context = FakeContext(self.world)
        self.world.contexts.append(context)
        return context

    async def close(self):
        pass


ME_OK = (200, json.dumps({"name": "tester"}), "application/json")
WWW_ME_OK = (200, json.dumps({"data": {"name": "tester"}}), "application/json")
LOGGED_OUT = (200, "{}", "application/json")
BLOCKED = (403, "<html>blocked</html>", "text/html")
UNAUTHORIZED = (401, '{"message": "Unauthorized"}', "application/json")


class World:
    def __init__(self):
        self.contexts = []  # every context made; the transport's own is t._context
        self.visits = 0
        self.on_visit = issue_token
        self.refuse = None
        self.goto_fails = False
        self.goto_gate = None
        self.cookies_fail = False
        self.clear_fails = False
        self.me = ME_OK
        self.www_me = WWW_ME_OK
        self.oauth_hook = None  # (url, token) -> reply, for answers that depend on the token

    def reply(self, url, token):
        if url.startswith("https://oauth.reddit.com/") and self.oauth_hook:
            return self.oauth_hook(url, token)
        if url.startswith("https://oauth.reddit.com/"):
            # Like Reddit: no bearer, or an expired one, is refused.
            from reddit_mcp.browser import jwt_exp

            exp = jwt_exp(token) if token else None
            if not token or (exp is not None and exp < time.time()):
                return UNAUTHORIZED
        if url.startswith("https://oauth.reddit.com/api/v1/me"):
            return self.me
        if url.endswith("/api/me.json"):
            return self.www_me
        return (200, "{}", "application/json")

    def module(self):
        world = self

        class Chromium:
            async def launch(self, **kw):
                return FakeBrowser(world)

        class PW:
            chromium = Chromium()

            async def stop(self):
                pass

        class Starter:
            async def start(self):
                return PW()

        mod = types.ModuleType("playwright.async_api")
        mod.async_playwright = lambda: Starter()
        return mod


def issue_token(jar):
    """What a visit to reddit.com does: a fresh token_v2 for the session, but
    only when the browser has none (a valid one is left alone, as seen live)."""
    if "token_v2" not in jar:
        exp = time.time() + DAY
        jar["token_v2"] = reddit_cookie("token_v2", jwt(exp, f"token-{time.monotonic()}"), exp)


@pytest.fixture
def world(monkeypatch):
    w = World()
    monkeypatch.setitem(sys.modules, "playwright.async_api", w.module())
    return w


def session(days: float, tag: str = "s1") -> str:
    return jwt(time.time() + days * DAY, tag)


# -------------------------------------------------------------------- store


def test_store_round_trip_keeps_only_usable_reddit_cookies(tmp_path):
    path = tmp_path / "volume" / "session.json"
    store = SessionStore(path)
    now = time.time()
    cookies = [
        {**reddit_cookie("reddit_session", "v1", now + DAY), "partitionKey": "pk"},
        {"name": "csv", "value": "2", "domain": "www.reddit.com", "path": "/", "expires": -1, "sameSite": "Odd"},
        {"name": "gone", "value": "1", "domain": ".reddit.com", "path": "/", "expires": now - 10},
        {"name": "SID", "value": "g", "domain": ".google.com"},
        {"name": "x", "value": "y", "domain": ".notreddit.com"},
        {"name": "", "value": "z", "domain": ".reddit.com"},
    ]
    assert store.save(cookies, "src") == 3
    assert stat.S_IMODE(os.stat(path).st_mode) == 0o600
    assert os.listdir(path.parent) == ["session.json"]  # no temp file left behind
    loaded = store.load("src")
    assert [c["name"] for c in loaded] == ["reddit_session", "csv"]  # expired one dropped
    assert "partitionKey" not in loaded[0] and "sameSite" not in loaded[1]
    assert store.load("another configured cookie") is None


@pytest.mark.parametrize("content", ["not json", "[]", '{"source": "src", "cookies": "x"}', ""])
def test_store_ignores_broken_files(tmp_path, content):
    path = tmp_path / "s.json"
    path.write_text(content)
    assert SessionStore(path).load("src") is None
    assert SessionStore(tmp_path / "missing.json").load("src") is None


def test_read_only_store_never_writes(tmp_path):
    path = tmp_path / "s.json"
    SessionStore(path).save([reddit_cookie("a", "1")], "src")
    before = path.read_text()
    reader = SessionStore(path).reader()
    assert reader.save([reddit_cookie("b", "2")], "other") == 0
    reader.discard()
    assert path.read_text() == before
    assert reader.load("src")[0]["name"] == "a"


def test_fingerprint_hides_the_cookie():
    fp = fingerprint("reddit_session=SECRET")
    assert "SECRET" not in fp and len(fp) == 16
    assert fp != fingerprint("reddit_session=OTHER")


# ------------------------------------------------- browser seeded from store


async def test_first_start_uses_the_configured_cookie_and_saves_it(world, tmp_path):
    path = tmp_path / "s.json"
    cookie = f"reddit_session={session(100)}; loid=abc"
    t = BrowserTransport(cookie, store=SessionStore(path))
    report = await t.keepalive()
    assert t.seeded_from == "config" and report["account"] == "tester"
    data = json.loads(path.read_text())
    assert data["source"] == fingerprint(cookie)
    assert {c["name"] for c in data["cookies"]} == {"reddit_session", "loid", "token_v2"}
    assert all("partitionKey" not in c for c in data["cookies"])


async def test_restart_continues_from_the_saved_session(world, tmp_path):
    path = tmp_path / "s.json"
    cookie = f"reddit_session={session(100, 'configured')}"
    renewed = session(180, "renewed")

    def renew(jar):
        issue_token(jar)
        jar["reddit_session"] = reddit_cookie("reddit_session", renewed, time.time() + 180 * DAY)

    world.on_visit = renew
    first = BrowserTransport(cookie, store=SessionStore(path))
    report = await first.keepalive()
    lines = describe_keepalive(report)
    assert any("Reddit renewed the login cookie (+80" in line for line in lines)

    world.on_visit = issue_token
    second = BrowserTransport(cookie, store=SessionStore(path))  # same config, after a restart
    await second._ensure_context()
    assert second.seeded_from == "store"
    assert second._context.jar["reddit_session"]["value"] == renewed


async def test_a_newly_configured_cookie_wins_over_the_saved_session(world, tmp_path):
    path = tmp_path / "s.json"
    old = f"reddit_session={session(100, 'old')}"
    await BrowserTransport(old, store=SessionStore(path)).keepalive()
    new_value = session(150, "new")
    t = BrowserTransport(f"reddit_session={new_value}", store=SessionStore(path))
    await t._ensure_context()
    assert t.seeded_from == "config"
    assert t._context.jar["reddit_session"]["value"] == new_value
    await t.save_session()
    assert json.loads(path.read_text())["source"] == fingerprint(f"reddit_session={new_value}")


async def test_a_saved_session_the_browser_refuses_is_set_aside(world, tmp_path):
    path = tmp_path / "s.json"
    cookie = f"reddit_session={session(100)}"
    SessionStore(path).save([reddit_cookie("reddit_session", "saved")], fingerprint(cookie))
    world.refuse = lambda cookies: any("sameSite" in c for c in cookies)  # only saved ones have it
    t = BrowserTransport(cookie, store=SessionStore(path))
    async with httpx.AsyncClient(transport=KeepOpen(t)) as client:
        r = await client.get("https://www.reddit.com/r/x/about.json")
    assert r.status_code == 200  # still works, from the configured cookie
    assert t.seeded_from == "config (saved session refused)"
    assert t._context.jar["reddit_session"]["value"] != "saved"
    assert (tmp_path / "s.json.rejected").exists()


async def test_a_full_disk_never_fails_requests(world, tmp_path):
    class Broken(SessionStore):
        def save(self, cookies, source):
            raise OSError(28, "No space left on device")

    t = BrowserTransport(f"reddit_session={session(100)}", store=Broken(tmp_path / "s.json"))
    report = await t.keepalive()
    assert report["account"] == "tester" and report["saved"] == 0
    assert "No space left" in report["after"]["save_error"]
    assert any("could not save" in line for line in describe_keepalive(report))


# ------------------------------------------------------- keeper and warnings


async def test_warns_before_the_login_runs_out(world):
    t = BrowserTransport(f"reddit_session={session(10.5)}")
    await t.keepalive()
    assert "expires in 10 days" in t.session_note


async def test_a_lost_login_cookie_is_reported_at_once(world):
    world.on_visit = lambda jar: jar.pop("reddit_session", None)  # Reddit logged the session out
    t = BrowserTransport(f"reddit_session={session(100)}")
    await t.keepalive()
    assert "gone or expired" in t.session_note


async def test_one_failed_check_is_logged_two_in_a_row_are_reported(world):
    t = BrowserTransport(f"reddit_session={session(100)}")
    world.me, world.www_me = BLOCKED, LOGGED_OUT
    first = await t.keepalive()
    assert first["account"] is None and first["failures"] == 1 and t.session_note is None
    await t.keepalive()
    assert "no longer works" in t.session_note
    world.me = ME_OK
    third = await t.keepalive()
    assert third["failures"] == 0 and t.session_note is None


async def test_either_door_confirming_the_login_is_enough(world):
    t = BrowserTransport(f"reddit_session={session(100)}")
    world.me = BLOCKED
    report = await t.keepalive()
    assert report["account"] == "tester" and report["status"] == "403/200"


async def test_a_refused_login_is_conclusive(world):
    t = BrowserTransport(f"reddit_session={session(100)}")
    world.me, world.www_me = (401, '{"message": "Unauthorized"}', "application/json"), LOGGED_OUT
    await t.keepalive()
    assert "no longer works" in t.session_note


async def test_keeper_reports_never_contain_cookie_values(world, tmp_path):
    value = session(100, "very-secret")
    t = BrowserTransport(f"reddit_session={value}; loid=SECRETLOID", store=SessionStore(tmp_path / "s.json"))
    report = await t.keepalive()
    text = repr(report) + "\n".join(describe_keepalive(report)) + repr(t) + repr(t._store)
    assert value not in text and "SECRETLOID" not in text
    token = t._context.jar["token_v2"]["value"]
    assert token not in text


async def test_keeper_runs_on_schedule_and_survives_failures(capsys):
    calls = []

    class Transport:
        async def keepalive(self):
            calls.append(1)
            if len(calls) == 2:
                raise RuntimeError("browser went away")
            return {"account": "tester", "status": "200", "failures": 0, "saved": 3,
                    "before": {"session_days_left": 100.0}, "after": {"session_days_left": 100.0, "seeded_from": "store"}}

    delays = []

    async def fake_sleep(seconds):
        delays.append(seconds)
        if len(delays) == 4:
            raise asyncio.CancelledError

    with pytest.raises(asyncio.CancelledError):
        await session_keeper(Transport(), 6, first_delay=90, sleep=fake_sleep)
    assert delays == [90, 6 * 3600, 6 * 3600, 6 * 3600] and len(calls) == 3
    err = capsys.readouterr().err
    assert "session-keeper: login: u/tester" in err
    assert "session-keeper: failed: RuntimeError: browser went away" in err


async def test_lifespan_runs_tasks_between_startup_and_shutdown():
    async def app(scope, receive, send):
        assert (await receive())["type"] == "lifespan.startup"
        await send({"type": "lifespan.startup.complete"})
        assert (await receive())["type"] == "lifespan.shutdown"
        await send({"type": "lifespan.shutdown.complete"})

    started, cancelled, saved = asyncio.Event(), [], []

    async def task():
        started.set()
        try:
            await asyncio.sleep(3600)
        except asyncio.CancelledError:
            cancelled.append(True)
            raise

    async def on_shutdown():
        saved.append(True)

    queue: asyncio.Queue = asyncio.Queue()
    sent = []

    async def send(message):
        sent.append(message["type"])

    runner = asyncio.create_task(Lifespan(app, lambda: [task()], on_shutdown)({"type": "lifespan"}, queue.get, send))
    await queue.put({"type": "lifespan.startup"})
    await asyncio.wait_for(started.wait(), 1)
    assert not cancelled
    await queue.put({"type": "lifespan.shutdown"})
    await asyncio.wait_for(runner, 1)
    assert cancelled == [True] and saved == [True]
    assert sent == ["lifespan.startup.complete", "lifespan.shutdown.complete"]


async def test_lifespan_passes_requests_through():
    seen = []

    async def app(scope, receive, send):
        seen.append(scope["type"])

    await Lifespan(app, lambda: [])({"type": "http"}, None, None)
    assert seen == ["http"]


def test_build_app_runs_the_keeper_only_for_the_browser(monkeypatch, tmp_path):
    from reddit_mcp.http_app import build_app

    monkeypatch.setenv("REDDIT_COOKIE", "reddit_session=x")
    monkeypatch.setenv("REDDIT_SESSION_STORE", str(tmp_path / "s.json"))
    monkeypatch.setenv("REDDIT_LIVE_CLIENT", "browser")
    assert isinstance(build_app("0.0.0.0", "t" * 32).app, Lifespan)
    monkeypatch.setenv("REDDIT_LIVE_CLIENT", "http")
    assert not isinstance(build_app("0.0.0.0", "t" * 32).app, Lifespan)


async def test_login_warning_reaches_every_answer():
    class Archive:
        name, label = "archive", "archive-label"

        async def browse(self, **kwargs):
            return Page(items=[], source=self.label)

        async def aclose(self):
            pass

    class Transport:
        session_note = "the Reddit login of this server expires in 3 days"

    router = Router(Settings(), archive=Archive(), transport=Transport())
    page = await router.run("browse", "archive", subreddit="a", sort="new", time=None, limit=5)
    assert page.notes == ["the Reddit login of this server expires in 3 days"]
    Transport.session_note = None
    page = await router.run("browse", "archive", subreddit="b", sort="new", time=None, limit=5)
    assert page.notes == []


def test_session_settings(tmp_path):
    s = Settings.from_env({"REDDIT_SESSION_STORE": " /data/s.json ", "REDDIT_SESSION_REFRESH_HOURS": "3"})
    assert s.session_store == "/data/s.json" and s.session_refresh_hours == 3
    assert Settings.from_env({}).session_store is None
    with pytest.raises(ValueError, match="REDDIT_SESSION_REFRESH_HOURS"):
        Settings.from_env({"REDDIT_SESSION_REFRESH_HOURS": "0"})
    browser = Settings(cookie="a=b", live_client="browser", session_store=str(tmp_path / "s.json"))
    assert make_transport(browser)._store.read_only is False
    assert make_transport(browser, read_only_store=True)._store.read_only is True
    assert make_transport(Settings(cookie="a=b", live_client="browser"))._store is None


async def test_built_app_serves_tools_while_the_keeper_runs(monkeypatch, tmp_path):
    """The real app, lifespan included: the keeper wrapper must not get in the
    way of the MCP session manager starting and stopping."""
    import respx

    from reddit_mcp.config import ARCHIVE_URL
    from reddit_mcp.http_app import build_app

    from .fixtures import archive_post
    from .test_http_app import HEADERS, INIT, rpc

    for name, value in {
        "REDDIT_COOKIE": "reddit_session=x",
        "REDDIT_LIVE_CLIENT": "browser",
        "REDDIT_SOURCE": "archive",  # no real browser in tests
        "REDDIT_SESSION_STORE": str(tmp_path / "s.json"),
        "REDDIT_MCP_ARCHIVE_INTERVAL": "0",
    }.items():
        monkeypatch.setenv(name, value)
    token = "t" * 32
    app = build_app("0.0.0.0", token)
    inbox: asyncio.Queue = asyncio.Queue()
    outbox: asyncio.Queue = asyncio.Queue()
    lifespan = asyncio.create_task(app({"type": "lifespan", "asgi": {"version": "3.0"}}, inbox.get, outbox.put))
    await inbox.put({"type": "lifespan.startup"})
    assert (await asyncio.wait_for(outbox.get(), 5))["type"] == "lifespan.startup.complete"
    try:
        headers = {**HEADERS, "authorization": f"Bearer {token}"}
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://x.up.railway.app") as c:
            assert (await c.post("/mcp", json=INIT, headers=headers)).status_code == 200
            with respx.mock(base_url=ARCHIVE_URL, assert_all_called=False) as api:
                api.get("/api/posts/search").respond(json={"data": [archive_post()]})
                call = rpc("tools/call", {"name": "browse_subreddit", "arguments": {"subreddit": "Python"}}, id=2)
                r = await c.post("/mcp", json=call, headers=headers)
        assert "r/Python" in r.json()["result"]["content"][0]["text"]
    finally:
        await inbox.put({"type": "lifespan.shutdown"})
        assert (await asyncio.wait_for(outbox.get(), 5))["type"] == "lifespan.shutdown.complete"
        await asyncio.wait_for(lifespan, 5)


def token_cookie(hours: float) -> str:
    return f"token_v2={jwt(time.time() + hours * 3600, 'configured-token')}"


async def test_keeper_renews_a_token_that_is_getting_short(world):
    t = BrowserTransport(f"reddit_session={session(100)}; {token_cookie(0.2)}")
    report = await t.keepalive()
    assert report["before"]["token_hours_left"] == pytest.approx(0.2, abs=0.05)
    assert report["after"]["token_hours_left"] == pytest.approx(24, abs=0.1)


async def test_keeper_leaves_a_fresh_token_alone(world):
    t = BrowserTransport(f"reddit_session={session(100)}; {token_cookie(20)}")
    report = await t.keepalive()
    assert report["after"]["token_hours_left"] == pytest.approx(20, abs=0.1)


async def test_an_expiring_token_is_replaced_not_kept(world):
    t = BrowserTransport(f"reddit_session={session(100)}; {token_cookie(2 / 60)}")
    await t._ensure_context()
    old = t._context.jar["token_v2"]["value"]
    new = await t.bearer()
    assert new and new != old  # a visit alone would have kept the old one
    assert t._context.jar["token_v2"]["value"] == new


# ------------------------------------------- review findings (regressions)


async def test_a_failed_renewal_keeps_the_token_that_still_works(world):
    """Finding 1: a renewal that gets nothing must not cost the working token
    or raise a false alarm."""
    t = BrowserTransport(f"reddit_session={session(100)}; {token_cookie(6)}")
    await t._ensure_context()
    working = t._context.jar["token_v2"]["value"]
    world.on_visit = lambda jar: None  # the front page hands out nothing (say, a block page)
    report = await t.keepalive()
    assert t._context.jar["token_v2"]["value"] == working
    assert report["renewed"] is False and report["account"] == "tester"
    assert t.session_note is None


async def test_a_renewal_that_fails_outright_still_checks_the_login(world):
    t = BrowserTransport(f"reddit_session={session(100)}; {token_cookie(6)}")
    async with httpx.AsyncClient(transport=KeepOpen(t)) as client:
        await client.get("https://oauth.reddit.com/api/v1/me")  # the pages requests use are open
    working = t._context.jar["token_v2"]["value"]
    world.goto_fails = True  # from now on every page load fails, the renewal's too
    report = await t.keepalive()
    assert report["visit_error"] == "TimeoutError"
    assert t._context.jar["token_v2"]["value"] == working
    assert report["account"] == "tester" and t.session_note is None
    assert any("old token is kept" in line for line in describe_keepalive(report))


async def test_failed_renewals_are_cooled_down(world):
    """Finding 2: renewals that fail do not repeat for every request."""
    t = BrowserTransport(f"reddit_session={session(100)}; {token_cookie(20)}")
    await t.bearer()  # pages open, no renewal needed yet
    contexts = len(world.contexts)
    world.on_visit = lambda jar: None  # from now on Reddit hands out nothing
    short = jwt(time.time() + 60, "short")
    await t._context.add_cookies([reddit_cookie("token_v2", short, time.time() + 60)])
    results = await asyncio.gather(*(t.bearer() for _ in range(5)))
    assert len(world.contexts) == contexts + 1  # one scratch context: one attempt for five callers
    assert results == [short] * 5  # and everyone keeps the token that still works
    await t.bearer()
    assert len(world.contexts) == contexts + 1  # within the cooldown: no new attempt


async def test_a_failed_renewal_also_starts_the_cooldown(world):
    t = BrowserTransport(f"reddit_session={session(100)}; {token_cookie(2 / 60)}")
    await t._ensure_context()
    await t._page_for("https://www.reddit.com")
    old = t._context.jar["token_v2"]["value"]
    world.goto_fails = True
    assert await t.bearer() == old  # the renewal failed, but the token still works: no error
    contexts = len(world.contexts)
    await t.bearer()  # within the cooldown: no new attempt
    assert len(world.contexts) == contexts


async def test_the_token_check_renews_even_right_after_a_renewal(world):
    """Findings 3, 4, 9: the startup check asks on purpose and is not cooled down."""
    from reddit_mcp.probe import token_selftest

    t = BrowserTransport(f"reddit_session={session(100)}; {token_cookie(2 / 60)}")
    first = await t.bearer()  # renews: the token was minutes from expiring
    async with httpx.AsyncClient(transport=KeepOpen(t)) as client:
        lines = await token_selftest(KeepOpen(t), client)
    text = "\n".join(lines)
    assert "new token issued, different from before" in text
    assert "new token expires in 24.0 h" in text and "HTTP 200, u/tester" in text
    assert t._context.jar["token_v2"]["value"] != first


async def test_concurrent_401s_share_one_renewal(world):
    t = BrowserTransport(f"reddit_session={session(100)}; {token_cookie(20)}")
    await t._ensure_context()
    refused = t._context.jar["token_v2"]["value"]
    world.oauth_hook = lambda url, token: UNAUTHORIZED if token == refused else ME_OK
    async with httpx.AsyncClient(transport=KeepOpen(t)) as client:
        await client.get("https://www.reddit.com/api/me.json")  # pages up
        contexts = len(world.contexts)
        responses = await asyncio.gather(*(client.get("https://oauth.reddit.com/api/v1/me") for _ in range(3)))
    assert [r.status_code for r in responses] == [200, 200, 200]
    assert len(world.contexts) == contexts + 1


async def test_a_plain_keeper_visit_does_not_hold_up_requests(world):
    """Finding 10: with a fresh token the keeper's visit takes no lock."""
    t = BrowserTransport(f"reddit_session={session(100)}; {token_cookie(20)}")
    async with httpx.AsyncClient(transport=KeepOpen(t)) as client:
        await client.get("https://oauth.reddit.com/api/v1/me")  # www and oauth pages open
        world.goto_gate = asyncio.Event()
        keeper = asyncio.create_task(t.keepalive())
        await asyncio.sleep(0.05)
        r = await asyncio.wait_for(client.get("https://oauth.reddit.com/api/v1/me"), 1)
        assert r.status_code == 200 and not keeper.done()
        world.goto_gate.set()
        await keeper


async def test_a_browser_that_cannot_be_read_concludes_nothing(world):
    """Finding 6: a dead browser during the check is not a lost login."""
    t = BrowserTransport(f"reddit_session={session(100)}; {token_cookie(20)}")
    t.session_note = "an earlier note"
    world.me, world.www_me = BLOCKED, LOGGED_OUT

    def blocked_then_unreadable(url, token):
        world.cookies_fail = True
        return BLOCKED

    world.oauth_hook = blocked_then_unreadable
    report = await t.keepalive()
    assert report["after"]["readable"] is False and report["failures"] == 0
    assert t.session_note == "an earlier note"
    assert any("could not read" in line for line in describe_keepalive(report))


async def test_a_browser_dying_while_seeding_keeps_the_saved_session(world, tmp_path):
    """Finding 5: only a file the healthy browser refuses is set aside."""
    path = tmp_path / "s.json"
    cookie = f"reddit_session={session(100)}"
    SessionStore(path).save([reddit_cookie("reddit_session", "renewed-login")], fingerprint(cookie))
    world.refuse = lambda cookies: True
    world.clear_fails = True  # the browser is gone, not the file
    t = BrowserTransport(cookie, store=SessionStore(path))
    with pytest.raises(RuntimeError):
        await t._ensure_context()
    assert path.exists() and not (tmp_path / "s.json.rejected").exists()
    world.refuse, world.clear_fails = None, False
    await t._ensure_context()
    assert t.seeded_from == "store"
    assert t._context.jar["reddit_session"]["value"] == "renewed-login"


def test_a_jar_without_the_login_never_replaces_a_saved_one(tmp_path):
    """Finding 8, second layer: a jar read at a bad moment cannot wipe the session."""
    path = tmp_path / "s.json"
    store = SessionStore(path)
    store.save([reddit_cookie("reddit_session", "login"), reddit_cookie("loid", "1")], "src")
    with pytest.raises(ValueError, match="not replacing"):
        store.save([reddit_cookie("loid", "2")], "src")
    assert store.load("src")[0]["value"] == "login"
    assert store.save([reddit_cookie("loid", "3")], "a newly configured cookie") == 1


def test_renewal_never_clears_cookies_in_the_main_browser():
    """Finding 8: Playwright's clear_cookies(name=...) empties the whole jar for
    a moment; the transport must not use it on the browser it serves from."""
    import inspect

    from reddit_mcp import browser

    source = inspect.getsource(browser.BrowserTransport)
    assert "clear_cookies(name=" not in source


async def test_the_login_note_also_reaches_failed_answers():
    """Finding 11: the note matters most when things fail."""
    from mcp.shared.memory import create_connected_server_and_client_session

    from reddit_mcp.errors import Unavailable
    from reddit_mcp.server import build_server

    note = "the Reddit login of this server no longer works; set a fresh cookie"

    class Transport:
        session_note = note

    class Live:
        name, label = "live", "reddit.com"

        async def browse(self, **kwargs):
            raise Unavailable("HTTP 401")

        async def aclose(self):
            pass

    class Writer:
        async def me(self):
            return "tester", "modhash"

        async def save(self, *args):
            raise Unavailable("reddit.com refused the write (HTTP 401)")

        async def aclose(self):
            pass

    settings = Settings(cookie="x", enable_writes=True)
    router = Router(settings, live=Live(), writer=Writer(), transport=Transport())
    server = build_server(lambda: router, enable_writes=True)
    async with create_connected_server_and_client_session(server) as client:
        res = await client.call_tool("browse_subreddit", {"subreddit": "Python", "source": "live"})
        assert res.isError and note in res.content[0].text
        res = await client.call_tool("save_content", {"target": "t3_abc"})
        assert res.isError and note in res.content[0].text
        res = await client.call_tool("get_my_account", {})
        assert not res.isError and note in res.content[0].text


async def test_no_token_and_no_answer_from_www_is_not_proof(world):
    t = BrowserTransport(f"reddit_session={session(100)}")
    t.session_note = "earlier"
    world.on_visit = lambda jar: None  # no token is ever issued
    world.www_me = BLOCKED
    report = await t.keepalive()
    assert report["status"].startswith("no token") and report["failures"] == 1
    assert t.session_note == "earlier"


async def test_a_401_for_an_expired_token_is_not_proof(world):
    t = BrowserTransport(f"reddit_session={session(100)}; {token_cookie(-1)}")
    world.on_visit = lambda jar: None
    world.www_me = LOGGED_OUT
    report = await t.keepalive()
    assert report["status"].startswith("401") and report["failures"] == 1
    assert t.session_note is None  # one inconclusive check


async def test_a_late_401_after_the_cooldown_uses_the_renewed_token(world):
    t = BrowserTransport(f"reddit_session={session(100)}; {token_cookie(20)}")
    await t._ensure_context()
    old = t._context.jar["token_v2"]["value"]
    new = await t.bearer(rejected=old)
    assert new != old
    contexts = len(world.contexts)
    t._last_attempt -= 31  # the cooldown is over when a slow request's 401 arrives
    assert await t.bearer(rejected=old) == new
    assert len(world.contexts) == contexts  # no second renewal


async def test_a_check_across_a_browser_restart_concludes_nothing(world):
    t = BrowserTransport(f"reddit_session={session(100)}; {token_cookie(20)}")
    await t._ensure_context()
    t.session_note = "earlier"
    world.me, world.www_me = BLOCKED, LOGGED_OUT

    def crash(url, token):
        t._context = None  # the browser died during the check; the next call relaunches it
        return BLOCKED

    world.oauth_hook = crash
    report = await t.keepalive()
    assert report["failures"] == 0 and t.session_note == "earlier"
