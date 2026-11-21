# reddit-mcp

A Reddit MCP server that keeps working after Reddit closed self-service API
keys (Nov 2025) and blocked anonymous `.json` requests (2026). Reads work with
no account at all; with your own logged-in session it also gives live data and,
if you opt in, acts on your account.

**One instance per person.** This is a self-hosted server: you deploy your own
copy, with your own Reddit session and your own access token, and only you
use it. Do not run one instance for several people. It would hold everyone's
Reddit login (anyone with the token can read and post as the account behind
it), all requests would share one IP that Reddit can block, and the operator
would answer for every user's account. There is no hosted public instance.

## How it gets data

Two interchangeable sources sit behind one interface. With `source="auto"`
(the default) a request goes to live first when a session is configured and
falls back to the archive when live is refused or cannot do the request.
Every answer starts with the source that produced it, and with notes on
anything approximated or fallen back, so a model never mistakes archive data
for live data.

| Source | Needs | Good at | Limits |
|---|---|---|---|
| **archive**: [Arctic Shift](https://github.com/ArthurHeitmann/arctic_shift) | nothing | history back to 2005, date ranges, posts deleted since | time order only; keyword search needs a subreddit or author and fails (HTTP 422) on very active ones; scores frozen for ~36 h after posting |
| **live**: reddit.com through your logged-in session | your browser's Cookie header | hot/top/rising, relevance search, live scores, writes | uses your account (see the warning below) |

### The live source on a server

From a cloud server, Reddit refuses plain HTTP clients outright and serves
"403 Blocked" pages per endpoint. What finally works, verified from Railway on
2026-11-21:

* **Browser client** (`REDDIT_LIVE_CLIENT=browser`): every reddit.com request
  is a `fetch()` inside a headless Chromium page, after the page has opened
  reddit.com once so the site can set its own cookies. The full Cookie header
  is needed; `reddit_session` alone is not recognized as a login.
* **Two front doors.** `oauth.reddit.com` is called with the session's
  `token_v2` as bearer token, exactly as reddit.com's own web app does;
  `www.reddit.com` is called with the cookie (plus the modhash for writes).
  Reddit blocks different endpoints on each, so every endpoint tries one door,
  then the other, and remembers which worked. An endpoint blocked on both is
  skipped for 10 minutes so callers fall back to the archive at once.
* **Search** comes from old.reddit.com's server-rendered search page, because
  the JSON search endpoints are blocked on both doors.
* **token_v2 lasts 24 h.** Reddit only issues a new one to a browser that has
  none: a visit leaves a still-valid token alone, even minutes before it
  expires. So a renewal opens reddit.com in a scratch browser context that has
  every cookie except token_v2, and the site issues a fresh 24 h token there
  from the long-lived `reddit_session` cookie. Only a new token replaces the
  old one, in one step, so a renewal that fails never leaves the server
  without a working token. The session keeper renews when under 12 h are
  left; a request renews when the token is minutes from expiring or Reddit
  refuses it; a failed renewal waits 30 s before the next try, so requests
  never pile up behind one. You do not have to refresh anything.
* **The session survives restarts.** With `REDDIT_SESSION_STORE` pointing at
  a file on a Railway volume, the browser's cookies are saved after every
  token refresh and every keeper run, and a restart continues from them
  instead of from the cookie you configured once. A newly set `REDDIT_COOKIE`
  always wins: the saved session remembers (as a hash) which configured
  cookie it grew from. Every `REDDIT_SESSION_REFRESH_HOURS` (default 6) the
  session keeper opens reddit.com, confirms the login and saves the session.
* **`reddit_session` itself is Reddit's to renew** (it lasts about six
  months). The keeper logs its remaining days before and after each visit, so
  the logs show whether Reddit extends it. When it is 14 days from expiring,
  or the login stops working (two failed checks in a row, or a 401), every
  answer carries a note asking for a fresh cookie.

| Tool | Live path from the server |
|---|---|
| `browse_subreddit` | oauth.reddit.com; `sort=new` from old.reddit.com's listing page (`/r/<sub>/new` is blocked on both doors and `sort=new` is ignored elsewhere; the page has no post bodies) |
| `get_subreddit_info`, `find_subreddits` | oauth.reddit.com |
| `get_post`, `get_comment_thread` | www.reddit.com (a comment's post is looked up in the archive when not given) |
| `get_user_activity` | oauth.reddit.com |
| `search_posts` | old.reddit.com search page |
| `get_comments_by_id` | with `post`: www.reddit.com, one request per comment (10 per call); without: the archive, since `/api/info` and `/api/morechildren` are blocked on every door |
| all write tools | oauth.reddit.com (www.reddit.com + modhash if oauth refuses an endpoint) |

Request URLs (search terms, usernames) are kept out of the logs: httpx's
per-request log lines are turned off.

## Tools

All read tools return compact markdown. Collapsed comments are never dropped:
they render as the exact follow-up call, for example
`get_comment_thread(comment_id='abc', post='xyz')` or
`get_comments_by_id(comment_ids=[...])`. Paging uses `next_cursor`, which is
tied to the source that produced it.

| Tool | What it does |
|---|---|
| `browse_subreddit` | posts in a subreddit (hot/new/top/rising/controversial) with paging |
| `search_posts` | keywords, subreddit, author; date range (`after`/`before`) on the archive |
| `get_post` | a post plus its comment tree; `comment_depth` is enforced exactly |
| `get_comment_thread` | one comment and the replies under it |
| `get_comments_by_id` | expand "N more replies collapsed" placeholders |
| `get_user_activity` | a user's posts and/or comments, newest first |
| `get_subreddit_info` | description, sidebar, subscribers, creation date |
| `find_subreddits` | by topic (live) or name prefix (archive) |
| `diagnose_live` | how reddit.com answers this server right now |

### Write tools (opt-in)

Registered only when `REDDIT_ENABLE_WRITES=1` and a session is configured.

| Tool | What it does |
|---|---|
| `get_my_account` | which account the write tools act as |
| `submit_post` | text or link post, optional flair, NSFW, spoiler |
| `reply` | reply to a post (`t3_...`) or comment (`t1_...`) |
| `edit_content` | replace the text of your own post or comment |
| `delete_content` | delete your own post or comment |
| `vote` | up, down, or clear |
| `save_content` | save or unsave |

Targets must be a fullname (`t3_`/`t1_`) or a URL, because posts and comments
share one id space. Guard rails, enforced in the server, not left to the client:

* writes run one at a time, so parallel tool calls cannot race past the checks;
* at least `REDDIT_WRITE_INTERVAL` seconds between writes (default 5) and at
  most `REDDIT_WRITE_DAILY_LIMIT` per rolling 24 h (default 100);
* the same text to the same place (same parent, same subreddit) is refused
  within an hour;
* `delete_content` says plainly that Reddit answers success even for content
  the account did not write;
* a write is never resent blindly. The only retries are on refusals that prove
  nothing was processed (a 403 from one door, a stale modhash). A timeout or
  5xx is reported as "may have gone through" and still counts as a duplicate.
* Reddit's own errors (rate limit, CAPTCHA, banned, locked) come back verbatim.

Write tools are annotated as not read-only, and edit/delete as destructive.
Clients can use that to ask before running them; whether they do depends on
the client and its permission mode.

## Run locally

```bash
uv sync
uv run pytest
claude mcp add reddit -- uv --directory /path/to/reddit-mcp run reddit-mcp
```

For Claude Desktop and other clients:

```json
{
  "mcpServers": {
    "reddit": {
      "command": "uv",
      "args": ["--directory", "/path/to/reddit-mcp", "run", "reddit-mcp"],
      "env": { "REDDIT_COOKIE_FILE": "/path/to/reddit-cookie.txt" }
    }
  }
}
```

`reddit-mcp --transport streamable-http --port 8000` serves HTTP on 127.0.0.1
instead of stdio.

## Deploy on Railway

The `Dockerfile` is based on Playwright's image (Chromium included) and serves
stateless Streamable HTTP on `$PORT`, with `/health` for the platform. Build,
health check and restart policy are set on the Railway service itself (there
is no `railway.json`, which Railway deprecates on 2026-12-01).

| Variable | Required | Notes |
|---|---|---|
| `MCP_AUTH_TOKEN` | yes | 24+ random characters; the server refuses to start publicly without it |
| `REDDIT_COOKIE` | for live | your browser's whole `cookie:` request header for www.reddit.com |
| `REDDIT_LIVE_CLIENT` | for live | `browser` |
| `REDDIT_ENABLE_WRITES` | no | `1` adds the write tools |
| `REDDIT_SESSION_STORE` | recommended | `/data/reddit-session.json`, with a volume mounted at `/data` |

The container starts as root only long enough to hand the session directory
to Playwright's unprivileged `pwuser` (Railway mounts volumes owned by root),
then runs the server and Chromium as that user. With a volume attached,
Railway stops the old deployment before starting the new one, so a redeploy
has a short gap.

To set the cookie without pasting it anywhere: log in to reddit.com, open
developer tools, Network, reload, click the `www.reddit.com` request, copy the
value of the `cookie:` request header, then run

```bash
bash scripts/set-railway-cookie.sh
```

(macOS; the first time, run `railway login` and `railway link` in the repo to
pick your project and service, or set `RAILWAY_PROJECT_ID`,
`RAILWAY_SERVICE_ID` and `RAILWAY_ENVIRONMENT`.) It reads the clipboard, checks that it is a reddit.com Cookie header (it
refuses Google cookies, the Application-panel table, or a header without
`reddit_session` and `token_v2`), stores it through the Railway CLI from stdin,
prints only cookie names, and clears the clipboard.

Connect a client with the token in a header:

```bash
claude mcp add --transport http reddit https://<app>.up.railway.app/mcp --header "Authorization: Bearer <token>"
```

Clients that only take a URL can use `https://<app>.up.railway.app/mcp/<token>`.
Treat that URL as a password: Railway's edge records request paths in the
project's HTTP logs. With writes enabled the token lets anyone post as you, so
prefer the header form and rotate the token if the project is shared.

### Startup checks and probes

Every start logs, without ever printing a cookie or token (all lines go
through a redaction filter):

* `live-check:` how each reddit.com endpoint answers (block page or not, login
  recognized or not);
* `token-check:` (browser mode) renews `token_v2` through the same path the
  keeper uses and confirms the new one is issued and accepted;
* `probe:` read-only requests listed in `REDDIT_MCP_PROBES`, for finding out
  what Reddit serves without redeploying code. Example value:
  `[{"url": "https://old.reddit.com/r/Python/search?q=x&restrict_sr=on", "find": ["search-result-link"], "around": "search-title", "chars": 600}]`.
  Only GET to `https://*.reddit.com` is allowed.
* `write-check:` only with `REDDIT_MCP_WRITE_SELFTEST=1` (save and unsave the
  post named in `REDDIT_MCP_WRITE_SELFTEST_TARGET`, e.g. `t3_abc123`) or `full` (also a submit to a subreddit name that cannot exist, an
  empty reply, an edit of id 0, and clearing a vote: each reaches its endpoint
  and is refused, so nothing is ever posted). Switch it off afterwards.

While running, `session-keeper:` lines (browser mode) report each keeper
visit: the account, where the session came from (`store` or `config`), cookie
counts, whether it was saved, and the days left on `reddit_session` before and
after the visit. Only names and expiry dates, never values.

## Configuration

| Variable | Default | Meaning |
|---|---|---|
| `REDDIT_SOURCE` | `auto` | `auto`, `live` or `archive` |
| `REDDIT_COOKIE` / `REDDIT_COOKIE_FILE` | unset | enables the live source |
| `REDDIT_LIVE_CLIENT` | `http` | `browser` sends reddit.com requests through headless Chromium (needs the `browser` extra) |
| `REDDIT_USER_AGENT` | `reddit-mcp/0.1 ...` | user agent in `http` mode (browser mode uses Chromium's own) |
| `REDDIT_ENABLE_WRITES` | unset | `1` registers the write tools |
| `REDDIT_WRITE_INTERVAL` | `5` | minimum seconds between writes |
| `REDDIT_WRITE_DAILY_LIMIT` | `100` | maximum writes per rolling 24 h, `0` disables the cap |
| `REDDIT_SESSION_STORE` | unset | browser mode: file that keeps the session across restarts (on a persistent volume) |
| `REDDIT_SESSION_REFRESH_HOURS` | `6` | browser mode: hours between session keeper visits (0.25 to 168) |
| `REDDIT_MCP_CACHE_TTL` | `120` | seconds to cache identical reads, `0` disables |
| `REDDIT_MCP_LIVE_INTERVAL` | `1.0` | minimum seconds between live requests |
| `REDDIT_MCP_ARCHIVE_INTERVAL` | `0.5` | minimum seconds between archive requests |
| `REDDIT_MCP_TIMEOUT` | `20` | request timeout in seconds |
| `REDDIT_MCP_LIVE_CHECK` | `1` | `0` skips the startup checks |
| `REDDIT_MCP_PROBES` | unset | JSON list of read-only probes, see above |
| `REDDIT_MCP_WRITE_SELFTEST` | unset | `1` or `full`, see above |
| `MCP_AUTH_TOKEN` | unset | required when listening on anything but localhost |
| `REDDIT_ARCHIVE_URL` | Arctic Shift | archive base URL |

## Layout

```
src/reddit_mcp/
  server.py          MCP tools (FastMCP)
  router.py          source choice, fallback, caching, cursors
  sources/archive.py Arctic Shift backend
  sources/live.py    reddit.com backend: doors, block detection, diagnosis
  browser.py         httpx transport that fetches from inside Chromium; token_v2 refresh, keepalive
  session_store.py   the browser's session on the volume (atomic, mode 600)
  oldsearch.py       old.reddit.com search page parser
  writer.py          write actions, doors, guard rails
  probe.py           startup probes, self-tests, log redaction
  http_app.py        HTTP deployment: auth gate, /health, startup checks, session keeper
  parse.py, format.py, ids.py, models.py, config.py
docker-entrypoint.sh            hands the volume to pwuser, then drops root
scripts/set-railway-cookie.sh   set REDDIT_COOKIE from the clipboard
scripts/smoke.py                one real request per source, run locally
```

## Warning

Reddit's rules prohibit automated access without an agreement. Using a session
this way can get the account suspended; use an account you can afford to lose.
The cookie and token are only sent to reddit.com and are never logged, printed,
or included in error messages.

## Not included

Private messages, image and video uploads, subreddit rules, moderation actions,
multiple accounts, and impersonating Reddit's official apps.
