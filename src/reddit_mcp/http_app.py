"""Remote deployment: Streamable HTTP behind a shared-secret gate.

Two ways to present the secret, so it works with every MCP client:
  * ``Authorization: Bearer <token>`` on requests to ``/mcp`` (preferred), or
  * the secret as a path segment: ``/mcp/<token>``, for clients that can only
    be given a URL (e.g. custom connectors in the Claude apps).

``/health`` answers without the token so the platform can probe it.
Access logging is off, because a path-token URL would otherwise end up in logs.
"""

from __future__ import annotations

import hmac
import json
import os
import sys
from collections.abc import Awaitable, Callable, MutableMapping
from typing import Any

import httpx

from .http import KeepOpen

__all__ = ["AuthGate", "KeepOpen", "build_app", "check_auth_config", "run_checks", "serve"]

Scope = MutableMapping[str, Any]
Receive = Callable[[], Awaitable[MutableMapping[str, Any]]]
Send = Callable[[MutableMapping[str, Any]], Awaitable[None]]
ASGIApp = Callable[[Scope, Receive, Send], Awaitable[None]]

MCP_PATH = "/mcp"
HEALTH_PATHS = ("/health", "/healthz")
MIN_TOKEN_LENGTH = 24


async def _respond(send: Send, status: int, body: dict, headers: list | None = None) -> None:
    payload = json.dumps(body).encode()
    await send(
        {
            "type": "http.response.start",
            "status": status,
            "headers": [
                (b"content-type", b"application/json"),
                (b"content-length", str(len(payload)).encode()),
                *(headers or []),
            ],
        }
    )
    await send({"type": "http.response.body", "body": payload})


class AuthGate:
    def __init__(self, app: ASGIApp, token: str | None, mcp_path: str = MCP_PATH) -> None:
        self.app = app
        self.token = token.encode() if token else None
        self.mcp_path = mcp_path

    def _matches(self, candidate: str) -> bool:
        return self.token is not None and hmac.compare_digest(candidate.encode(), self.token)

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)  # lifespan events pass straight through
            return
        path: str = scope.get("path", "")
        if path in HEALTH_PATHS:
            await _respond(send, 200, {"status": "ok"})
            return
        if self.token is None:
            await self.app(scope, receive, send)
            return

        prefix = self.mcp_path + "/"
        if path.startswith(prefix) and path.rstrip("/") != self.mcp_path:
            if self._matches(path[len(prefix) :].rstrip("/")):
                inner = dict(scope, path=self.mcp_path, raw_path=self.mcp_path.encode())
                await self.app(inner, receive, send)
            else:
                await _respond(send, 404, {"error": "not found"})
            return

        header = dict(scope.get("headers") or []).get(b"authorization", b"").decode("latin-1")
        scheme, _, value = header.partition(" ")
        if scheme.lower() == "bearer" and self._matches(value.strip()):
            await self.app(scope, receive, send)
            return
        await _respond(
            send,
            401,
            {"error": "missing or invalid bearer token"},
            [(b"www-authenticate", b'Bearer realm="reddit-mcp"')],
        )


def check_auth_config(host: str, token: str | None, allow_no_auth: bool) -> str | None:
    """Return an error message if this configuration must not start."""
    from .server import LOOPBACK

    if token is not None and len(token) < MIN_TOKEN_LENGTH:
        return f"MCP_AUTH_TOKEN must be at least {MIN_TOKEN_LENGTH} characters"
    if token is None and host not in LOOPBACK and not allow_no_auth:
        return (
            f"refusing to listen on {host} without MCP_AUTH_TOKEN: anyone could use this "
            "server (and your Reddit session, if configured). Set MCP_AUTH_TOKEN, or "
            "MCP_ALLOW_NO_AUTH=1 if you really mean it."
        )
    return None


KEEPER_FIRST_DELAY = 90.0  # seconds; lets the startup checks finish first
KEEPALIVE_TIMEOUT = 300.0
SHUTDOWN_SAVE_TIMEOUT = 5.0


def describe_keepalive(report: dict[str, Any]) -> list[str]:
    """Log lines for one keeper run: account, cookie names' expiry, counts."""
    before, after = report.get("before") or {}, report.get("after") or {}
    account = report.get("account")
    lines = [
        f"login: {'u/' + account if account else 'NOT confirmed'} "
        f"(answers {report.get('status')}; failed checks in a row: {report.get('failures')})",
        f"session from: {after.get('seeded_from')}; cookies {before.get('cookies')} -> "
        f"{after.get('cookies')}; saved {report.get('saved')}",
        f"reddit_session days left: {before.get('session_days_left')} -> {after.get('session_days_left')}"
        f"; token_v2 hours left: {before.get('token_hours_left')} -> {after.get('token_hours_left')}"
        + (" (renewed)" if report.get("renewed") else ""),
    ]
    old, new = before.get("session_days_left"), after.get("session_days_left")
    if isinstance(old, int | float) and isinstance(new, int | float) and new > old + 0.5:
        lines.append(f"Reddit renewed the login cookie (+{new - old:.1f} days)")
    if after.get("save_error"):
        lines.append(f"could not save the session: {after['save_error']}")
    if report.get("visit_error"):
        lines.append(f"the visit to reddit.com failed ({report['visit_error']}); the old token is kept")
    if after.get("readable") is False:
        lines.append("could not read the browser's cookies; nothing concluded from this check")
    return lines


async def session_keeper(
    transport: Any,
    hours: float,
    *,
    first_delay: float = KEEPER_FIRST_DELAY,
    sleep: Callable[[float], Awaitable[None]] | None = None,
) -> None:
    """Every few hours, let the browser visit reddit.com, check the login and
    save the session to the volume. Runs for the life of the server."""
    import asyncio

    sleep = sleep or asyncio.sleep
    await sleep(first_delay)
    while True:
        try:
            report = await asyncio.wait_for(transport.keepalive(), KEEPALIVE_TIMEOUT)
            _log("session-keeper", describe_keepalive(report))
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # the keeper must never take the server down
            _log("session-keeper", [f"failed: {type(exc).__name__}: {str(exc)[:200]}"])
        await sleep(hours * 3600)


class Lifespan:
    """Runs background work for as long as the server runs.

    Watches the ASGI lifespan messages on their way to the wrapped app: the
    tasks start at startup and stop at shutdown, where ``on_shutdown`` runs
    too. Everything else passes through untouched."""

    def __init__(
        self,
        app: ASGIApp,
        tasks: Callable[[], list[Awaitable[None]]],
        on_shutdown: Callable[[], Awaitable[None]] | None = None,
    ) -> None:
        self.app = app
        self.tasks = tasks
        self.on_shutdown = on_shutdown

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "lifespan":
            await self.app(scope, receive, send)
            return
        import asyncio

        running: list[asyncio.Task] = []
        stopped = False

        async def stop() -> None:
            nonlocal stopped
            if stopped:
                return
            stopped = True
            for task in running:
                task.cancel()
            await asyncio.gather(*running, return_exceptions=True)
            if self.on_shutdown is not None:
                try:
                    await self.on_shutdown()
                except Exception:
                    pass

        async def watched_receive() -> MutableMapping[str, Any]:
            message = await receive()
            if message.get("type") == "lifespan.startup" and not running:
                running.extend(asyncio.ensure_future(c) for c in self.tasks())
            elif message.get("type") == "lifespan.shutdown":
                await stop()
            return message

        try:
            await self.app(scope, watched_receive, send)
        finally:
            await stop()


def _shared_router() -> Any:
    """The one Router of this process, built up front so the session keeper
    and the tools share one browser. A bad configuration is left for the
    tools to report, as before."""
    from .config import Settings
    from .router import Router

    try:
        return Router.from_settings(Settings.from_env())
    except ValueError as exc:
        print(f"reddit-mcp: config error: {exc}", file=sys.stderr, flush=True)
        return None


def build_app(host: str, token: str | None) -> ASGIApp:
    import asyncio

    from .server import build_server

    router = _shared_router()
    factory = (lambda: router) if router is not None else None
    server = build_server(factory, host=host, shared_router=True, stateless_http=True)
    app: ASGIApp = server.streamable_http_app()
    transport = getattr(router, "transport", None)
    if transport is not None and hasattr(transport, "keepalive"):
        hours = router.settings.session_refresh_hours

        async def save_on_shutdown() -> None:
            await asyncio.wait_for(transport.save_session(), SHUTDOWN_SAVE_TIMEOUT)

        app = Lifespan(app, lambda: [session_keeper(transport, hours)], save_on_shutdown)
    return AuthGate(app, token)


def _log(prefix: str, lines: list[str]) -> None:
    from .probe import redact

    for line in lines:
        print(f"{prefix}: {redact(line)}", file=sys.stderr, flush=True)


async def run_checks(settings: Any, env: Any) -> None:
    """Startup checks, all through one browser. Each part fails on its own."""
    from .probe import lifecycle_selftest, parse_probes_lenient, run_probes, token_selftest, write_selftest
    from .sources.live import diagnose, make_transport

    # Reads the saved session (so the checks see what the tools use) but never
    # writes it: only the server's own browser keeps the saved session.
    inner = make_transport(settings, read_only_store=True)
    transport = KeepOpen(inner) if inner is not None else None
    try:
        try:
            _log("live-check", await diagnose(settings, transport=transport))
        except Exception as exc:  # diagnostics must never take the server down
            _log("live-check", [f"failed: {type(exc).__name__}"])

        if inner is not None and settings.live_client == "browser":
            async with httpx.AsyncClient(transport=transport, timeout=90) as client:
                try:
                    _log("token-check", await token_selftest(transport, client))
                except Exception as exc:
                    _log("token-check", [f"failed: {type(exc).__name__}: {str(exc)[:200]}"])

        probes, problems = parse_probes_lenient(env.get("REDDIT_MCP_PROBES"))
        if problems:
            _log("probe", problems)
        if probes:
            headers = {"User-Agent": settings.user_agent, "Cookie": settings.cookie or ""}
            async with httpx.AsyncClient(
                transport=transport, timeout=90, headers=headers, follow_redirects=False
            ) as client:
                try:
                    _log("probe", await run_probes(client, probes))
                except Exception as exc:
                    _log("probe", [f"failed: {type(exc).__name__}: {str(exc)[:200]}"])

        mode = (env.get("REDDIT_MCP_WRITE_SELFTEST") or "").strip().lower()
        if mode in ("1", "true", "yes", "full", "lifecycle"):
            target = (env.get("REDDIT_MCP_WRITE_SELFTEST_TARGET") or "").strip()
            try:
                if not target:
                    _log("write-check", ["skipped: set REDDIT_MCP_WRITE_SELFTEST_TARGET to a post id (t3_...) to save and unsave"])
                else:
                    _log("write-check", await write_selftest(settings, transport, target, full=mode != "1"))
            except Exception as exc:
                _log("write-check", [f"failed: {type(exc).__name__}: {str(exc)[:200]}"])
        if mode == "lifecycle":
            import time as _t

            try:
                stamp = _t.strftime("%Y%m%d-%H%M%S", _t.gmtime())
                _log("lifecycle", await lifecycle_selftest(settings, transport, stamp=stamp))
            except Exception as exc:
                _log("lifecycle", [f"failed: {type(exc).__name__}: {str(exc)[:200]}"])
    finally:
        if inner is not None:
            await inner.aclose()


def start_live_check() -> None:
    """Run the startup checks once in the background and log the result.

    Lets the operator see from the platform's logs whether the live source
    works from this server, without exposing the cookie.
    """
    import asyncio
    import threading

    from .config import Settings

    try:
        settings = Settings.from_env()
    except ValueError as exc:
        print(f"live-check: config error: {exc}", file=sys.stderr, flush=True)
        return
    if not settings.has_live:
        print("live-check: no session cookie configured; live source disabled", file=sys.stderr, flush=True)
        return
    env = dict(os.environ)

    def run() -> None:
        try:
            asyncio.run(run_checks(settings, env))
        except Exception as exc:
            print(f"live-check: failed: {type(exc).__name__}", file=sys.stderr, flush=True)

    threading.Thread(target=run, name="live-check", daemon=True).start()


def serve(host: str, port: int) -> None:
    import uvicorn

    token = (os.environ.get("MCP_AUTH_TOKEN") or "").strip() or None
    allow = os.environ.get("MCP_ALLOW_NO_AUTH", "").strip().lower() in ("1", "true", "yes")
    problem = check_auth_config(host, token, allow)
    if problem:
        print(f"reddit-mcp: {problem}", file=sys.stderr)
        sys.exit(2)
    if os.environ.get("REDDIT_MCP_LIVE_CHECK", "1").strip().lower() not in ("0", "false", "no"):
        start_live_check()
    uvicorn.run(
        build_app(host, token),
        host=host,
        port=port,
        access_log=False,
        proxy_headers=True,
        forwarded_allow_ips="*",
        log_level="info",
    )
