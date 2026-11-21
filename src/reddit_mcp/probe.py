"""Operator checks that run on the deployed server and report through its logs.

Everything here exists so the live source can be debugged from the platform's
logs alone, without shell access to the container and without exposing the
session: every line that leaves this module goes through :func:`redact`.

* :func:`run_probes` asks reddit.com arbitrary read-only questions configured
  in ``REDDIT_MCP_PROBES`` (a JSON list, see :func:`parse_probes`).
* :func:`token_selftest` proves the browser renews token_v2 by itself.
* :func:`write_selftest` exercises the write endpoints.
"""

from __future__ import annotations

import dataclasses
import json
import re
from collections.abc import Iterable
from dataclasses import dataclass, field
from typing import Any

import httpx

from .errors import SourceError

PROBE_HOST = re.compile(r"^([a-z0-9-]+\.)*reddit\.com$")
MAX_PROBES = 12
MAX_CHARS = 1500

# Keys whose values must never be logged.
_SECRET_KEYS = (
    r"modhash|csrf[_-]?token|token[_-]?v2|access[_-]?token|refresh[_-]?token|id[_-]?token|"
    r"reddit[_-]?session|session[_-]?tracker|loid|authorization|bearer|password|secret|uh"
)
_KEY_VALUE = re.compile(
    rf"""(?ix)
    (["']?(?:{_SECRET_KEYS})["']?\s*[:=]\s*["']?)   # key, separator, optional quote
    ([^"'\s,;&<>]{{4,}})                            # the value
    """
)
_JWT = re.compile(r"eyJ[A-Za-z0-9_-]{6,}\.[A-Za-z0-9_-]{6,}\.[A-Za-z0-9_-]{6,}")
_BEARER = re.compile(r"(?i)\b(bearer)\s+[A-Za-z0-9._~+/-]+=*")
_LONG_OPAQUE = re.compile(r"(?<![A-Za-z0-9/_.-])[A-Za-z0-9_-]{48,}(?![A-Za-z0-9/_.-])")
# HTML form fields: <input name="uh" value="..."> and <input value="..." name="uh">
_ATTR_NAME_VALUE = re.compile(
    rf"""(?ix)(\bname\s*=\s*["']?(?:{_SECRET_KEYS})["']?[^>]*?\bvalue\s*=\s*["']?)([^"'\s>]+)"""
)
_ATTR_VALUE_NAME = re.compile(
    rf"""(?ix)(\bvalue\s*=\s*["']?)([^"'\s>]+)(["']?[^>]*?\bname\s*=\s*["']?(?:{_SECRET_KEYS})\b)"""
)


def redact(text: str) -> str:
    """Remove anything that looks like a credential from a log line.

    Escaped JSON (``\\"modhash\\": \\"..\\"``) and entity-encoded attributes
    (``&quot;modhash&quot;:&quot;..&quot;``) are unescaped first, so the same
    key/value rules catch them.
    """
    text = text.replace("&quot;", '"').replace("&#34;", '"').replace('\\"', '"')
    text = _JWT.sub("<jwt>", text)
    text = _BEARER.sub(lambda m: m.group(1) + " <redacted>", text)
    text = _ATTR_NAME_VALUE.sub(lambda m: m.group(1) + "<redacted>", text)
    text = _ATTR_VALUE_NAME.sub(lambda m: m.group(1) + "<redacted>" + m.group(3), text)
    text = _KEY_VALUE.sub(lambda m: m.group(1) + "<redacted>", text)
    return _LONG_OPAQUE.sub("<opaque>", text)


@dataclass
class Probe:
    url: str
    find: list[str] = field(default_factory=list)
    around: str | None = None
    nth: int = 1
    chars: int = 500
    regex: bool = False


def parse_probes(raw: str | None) -> list[Probe]:
    """Parse ``REDDIT_MCP_PROBES``.

    Example::

        [{"url": "https://old.reddit.com/r/Python/search?q=asyncio&restrict_sr=on",
          "find": ["data-fullname=\\"t3_", "search-result"],
          "around": "search-result", "chars": 800}]

    Only GET requests to reddit.com hosts are allowed: probes are for looking,
    not for doing.
    """
    probes, errors = parse_probes_lenient(raw)
    if errors:
        raise ValueError("; ".join(errors))
    return probes


def parse_probes_lenient(raw: str | None) -> tuple[list[Probe], list[str]]:
    """Like :func:`parse_probes`, but a bad entry is reported and skipped
    instead of costing every other probe (and the checks after them)."""
    if not raw or not raw.strip():
        return [], []
    try:
        data = json.loads(raw)
    except ValueError as exc:
        return [], [f"REDDIT_MCP_PROBES is not valid JSON: {exc}"]
    if not isinstance(data, list):
        return [], ["REDDIT_MCP_PROBES must be a JSON list"]
    probes: list[Probe] = []
    errors: list[str] = []
    for n, item in enumerate(data[:MAX_PROBES], 1):
        try:
            if isinstance(item, str):
                item = {"url": item}
            url = httpx.URL(str(item["url"]))
            if url.scheme != "https" or not PROBE_HOST.match(url.host or ""):
                raise ValueError(f"URL must be https://*.reddit.com, got {url.host!r}")
            probes.append(
                Probe(
                    url=str(url),
                    find=[str(f) for f in item.get("find", [])][:10],
                    around=item.get("around"),
                    nth=max(1, int(item.get("nth", 1))),
                    chars=max(80, min(MAX_CHARS, int(item.get("chars", 500)))),
                    regex=bool(item.get("regex", False)),
                )
            )
        except Exception as exc:  # any malformed entry: report it, keep the rest
            errors.append(f"probe {n} skipped: {type(exc).__name__}: {str(exc)[:120]}")
    if len(data) > MAX_PROBES:
        errors.append(f"only the first {MAX_PROBES} probes run; {len(data) - MAX_PROBES} ignored")
    return probes, errors


def _matches(probe: Probe, text: str, needle: str) -> list[int]:
    if probe.regex:
        return [m.start() for m in re.finditer(needle, text)]
    out, start = [], 0
    while (i := text.find(needle, start)) != -1:
        out.append(i)
        start = i + 1
    return out


def describe(probe: Probe, response: httpx.Response) -> list[str]:
    text = response.text
    url = httpx.URL(probe.url)
    head = (
        f"GET {url.host}{url.raw_path.decode()} -> HTTP {response.status_code} "
        f"{response.headers.get('content-type', '?').split(';')[0]} {len(text)} chars"
    )
    final = response.headers.get("x-final-url")
    if final and final != probe.url:
        head += f" (ended at {httpx.URL(final).path})"
    lines = [head]
    if probe.find:
        counts = ", ".join(f"{f!r}={len(_matches(probe, text, f))}" for f in probe.find)
        lines.append(f"  find: {counts}")
    if probe.around:
        hits = _matches(probe, text, probe.around)
        if len(hits) >= probe.nth:
            at = hits[probe.nth - 1]
            half = probe.chars // 2
            snippet = " ".join(text[max(0, at - half) : at + half].split())
            lines.append(f"  around {probe.around!r} #{probe.nth}: {snippet}")
        else:
            lines.append(f"  around {probe.around!r}: only {len(hits)} match(es)")
    elif not probe.find:
        lines.append(f"  start: {' '.join(text[: probe.chars].split())}")
    return [redact(line) for line in lines]


async def run_probes(client: httpx.AsyncClient, probes: Iterable[Probe]) -> list[str]:
    lines = []
    for i, probe in enumerate(probes, 1):
        try:
            response = await client.get(probe.url)
        except httpx.HTTPError as exc:
            lines.append(f"[{i}] {probe.url}: {type(exc).__name__}")
            continue
        lines += [f"[{i}] {line}" if n == 0 else line for n, line in enumerate(describe(probe, response))]
    return lines


async def token_selftest(transport: Any, client: httpx.AsyncClient) -> list[str]:
    """Renew token_v2 on purpose, the way the keeper and expiring requests do,
    and check the new one works.

    The token the user pasted lasts about a day; this proves, at every start,
    that the server gets the next one by itself from the long-lived
    reddit_session cookie (through a scratch visit to the front page).
    """
    from .browser import WEB_ORIGIN, jwt_exp

    await transport.bearer()  # make sure the browser is up
    before = await transport._token_from_context()
    after = await transport.renew_token()
    if after and after != before:
        outcome = "new token issued, different from before"
    elif after:
        outcome = "NO new token (the old one is kept)"
    else:
        outcome = "NO token"
    lines = [f"web token present before: {'yes' if before else 'no'}; renewal through {WEB_ORIGIN}: {outcome}"]
    exp = jwt_exp(after) if after else None
    if exp:
        import time

        label = "new token" if after != before else "token"
        lines.append(f"{label} expires in {(exp - time.time()) / 3600:.1f} h")
    try:
        r = await client.get("https://oauth.reddit.com/api/v1/me")
        name = r.json().get("name") if "json" in r.headers.get("content-type", "") else None
        lines.append(f"oauth /api/v1/me with the new token: HTTP {r.status_code}" + (f", u/{name}" if name else ""))
    except (httpx.HTTPError, ValueError) as exc:
        lines.append(f"oauth /api/v1/me with the new token: {type(exc).__name__}")
    return lines


async def lifecycle_selftest(settings: Any, transport: Any, *, stamp: str, pause: float = 2.0) -> list[str]:
    """Every write tool for real, on the account's own profile, then cleaned up.

    submit -> read back -> reply -> edit -> read back -> vote up/clear ->
    save/unsave -> delete reply and post -> read back. The post lives on the
    user's own profile (r/test if the profile refuses posts) for about a
    minute. Cleanup runs whatever happens; if the new post's id cannot be read,
    it is looked up among the account's own posts by its unique title.
    """
    import asyncio

    from .sources.live import LiveSource
    from .writer import RedditWriter

    quick = dataclasses.replace(settings, write_interval=0)
    writer = RedditWriter(quick, transport=transport)
    live = LiveSource(quick, transport=transport)
    title = f"reddit-mcp self-test {stamp} (deleted automatically)"
    lines: list[str] = []
    post = reply = None
    name = ""

    async def find_own_post() -> str | None:
        page = await live.user_activity(name, "posts", 10, None)
        match = next((p for p in page.items if getattr(p, "title", None) == title), None)
        return f"t3_{match.id}" if match else None

    try:
        name, _ = await writer.me()
        lines.append(f"account u/{name}")
        for sr in (f"u_{name}", "test"):
            try:
                result = await writer.submit(
                    sr, title, "Automated end-to-end test of a reddit-mcp server; deleted within a minute.",
                    None, None, False, False,
                )
            except SourceError as exc:
                lines.append(f"submit to {sr} refused: {type(exc).__name__}: {str(exc)[:200]}")
                continue
            post = result.fullname or None
            lines.append(f"OK submit to {sr} -> {post or '(no id in the answer)'} (via {writer.last_door})")
            break
        if post is None and any(line.startswith("OK submit") for line in lines):
            await asyncio.sleep(pause)
            post = await find_own_post()
            lines.append(f"looked the new post up by title: {post or 'NOT FOUND, check the profile'}")
        if post is None:
            return lines
        pid = post.removeprefix("t3_")

        await asyncio.sleep(pause)
        thread = await live.get_post(pid, 10, 2, "new")
        ok = thread.post is not None and thread.post.title == title
        lines.append(f"{'OK' if ok else 'FAILED'} read back the post: title {'matches' if ok else 'differs'}")

        result = await writer.reply(post, f"self-test reply {stamp}")
        reply = result.fullname or None
        lines.append(f"OK reply -> {reply} (via {writer.last_door})")
        if reply:
            edited = f"self-test reply {stamp} (edited)"
            await writer.edit(reply, edited)
            lines.append(f"OK edit {reply} (via {writer.last_door})")
            await asyncio.sleep(pause)
            seen = await live.get_thread(pid, reply.removeprefix("t1_"), 1, 5, "new")
            body = seen.comments[0].body if seen.comments else ""
            lines.append(f"{'OK' if body == edited else 'FAILED'} read back the edit: {body[:60]!r}")

        for direction in (1, 0):
            result = await writer.vote(post, direction)
            lines.append(f"OK {result.action} (via {writer.last_door})")
        for saved in (True, False):
            result = await writer.save(post, saved)
            lines.append(f"OK {result.action} (via {writer.last_door})")
    except SourceError as exc:
        lines.append(f"FAILED: {type(exc).__name__}: {str(exc)[:240]}")
    finally:
        for thing in (reply, post):
            if not thing:
                continue
            try:
                await writer.delete(thing)
                lines.append(f"OK deleted {thing} (via {writer.last_door})")
            except SourceError as exc:
                lines.append(f"FAILED to delete {thing}: {type(exc).__name__}: {str(exc)[:200]} -- delete it by hand")
        if post:
            try:
                await asyncio.sleep(pause)
                gone = await live.get_post(post.removeprefix("t3_"), 10, 1, "new")
                p = gone.post
                deleted = p is not None and (p.author == "[deleted]" or p.selftext in ("[deleted]", "[removed]"))
                lines.append(f"{'OK' if deleted else 'CHECK'} read back after delete: author={p.author if p else '?'}")
            except SourceError as exc:
                lines.append(f"read back after delete: {type(exc).__name__} (a deleted post may 404)")
        await writer.aclose()
        await live.aclose()
    return [redact(line) for line in lines]


# Requests Reddit must refuse, so they reach each write endpoint without ever
# creating anything: subreddit names are at most 21 characters, an empty
# comment is always refused ("NO_TEXT"), and id 0 is never a live thing.
IMPOSSIBLE_SUBREDDIT = "mcp_selftest_name_too_long_to_exist"
IMPOSSIBLE_COMMENT = "t1_0"


async def write_selftest(settings: Any, transport: Any, target: str, full: bool) -> list[str]:
    """Exercise the write endpoints on the user's account.

    Basic: save and unsave ``target`` (private, undone at once).
    Full, additionally:
      * submit to a subreddit name that cannot exist (refused: SUBREDDIT_NOEXIST)
      * reply to ``target`` with empty text (refused: NO_TEXT)
      * edit a thing that cannot exist (refused)
      * clear the vote on ``target`` (dir 0: a no-op unless the account had voted)
    Any unexpected success of a check that should be refused is called out.
    """
    from .writer import RedditWriter

    writer = RedditWriter(dataclasses.replace(settings, write_interval=0), transport=transport)
    lines = []
    try:
        name, _ = await writer.me()
        lines.append(f"account u/{name}")
        for saved in (True, False):
            try:
                result = await writer.save(target, saved)
                lines.append(f"OK {result.action} (via {writer.last_door})")
            except SourceError as exc:
                lines.append(f"FAILED {'save' if saved else 'unsave'} {target}: {type(exc).__name__}: {exc}")
        if full:
            refused = [
                ("submit", lambda: writer.submit(IMPOSSIBLE_SUBREDDIT, "mcp self-test", "x", None, None, False, False)),
                # Bypass the client-side empty-text check on purpose: Reddit must answer NO_TEXT.
                ("reply", lambda: writer._post("/api/comment", {"thing_id": target, "text": ""}, text=None)),
                ("edit", lambda: writer.edit(IMPOSSIBLE_COMMENT, "mcp self-test edit")),
            ]
            for label, call in refused:
                try:
                    await call()
                    lines.append(f"UNEXPECTED OK {label} (via {writer.last_door}): check the account")
                except SourceError as exc:
                    lines.append(
                        f"{label} reached Reddit and was refused as expected (via {writer.last_door}): "
                        f"{type(exc).__name__}: {str(exc)[:240]}"
                    )
            try:
                result = await writer.vote(target, 0)
                lines.append(f"OK {result.action} (via {writer.last_door})")
            except SourceError as exc:
                lines.append(f"FAILED vote clear on {target} (via {writer.last_door}): {type(exc).__name__}: {str(exc)[:240]}")
    except SourceError as exc:
        lines.append(f"FAILED {type(exc).__name__}: {str(exc)[:300]}")
    finally:
        await writer.aclose()
    return [redact(line) for line in lines]
