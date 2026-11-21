#!/usr/bin/env bash
# Read a Reddit Cookie header from the clipboard and store it as REDDIT_COOKIE
# on the Railway service. Prints cookie names only, never values.
#
# One-time setup (macOS: uses pbpaste/pbcopy):
#   brew install railway
#   railway login
#   railway link        # in this repo: pick your project and the reddit-mcp service
# or set RAILWAY_PROJECT_ID, RAILWAY_SERVICE_ID (and RAILWAY_ENVIRONMENT) instead.
#
# Then, after copying the `cookie:` request-header value from the browser's
# Network panel (a request to www.reddit.com):
#   bash scripts/set-railway-cookie.sh
set -euo pipefail

# Which service to update: the one this directory is linked to, unless set here.
target=()
[ -n "${RAILWAY_PROJECT_ID:-}" ] && target+=(--project "$RAILWAY_PROJECT_ID")
[ -n "${RAILWAY_SERVICE_ID:-}" ] && target+=(--service "$RAILWAY_SERVICE_ID")
[ -n "${RAILWAY_ENVIRONMENT:-}" ] && target+=(--environment "$RAILWAY_ENVIRONMENT")

die() { echo "error: $*" >&2; exit 1; }

command -v railway >/dev/null || die "Railway CLI not found. Run: brew install railway && railway login"
railway whoami >/dev/null 2>&1 || die "not logged in to Railway. Run: railway login"

raw="$(pbpaste)"
[ -n "$raw" ] || die "clipboard is empty"

# Whatever happens next (success or any error), do not leave something that
# looks like a session cookie sitting in the clipboard.
case "$raw" in
  *=*) trap 'printf "" | pbcopy; echo "(clipboard cleared)" >&2' EXIT ;;
esac

# Normalize: drop a leading 'cookie:' label and all line breaks / outer spaces.
cookie="$(printf '%s' "$raw" | tr -d '\r\n' | sed -E 's/^[[:space:]]*[Cc]ookie:[[:space:]]*//; s/[[:space:]]+$//')"

# Reject things that are not a Cookie request header.
case "$cookie" in
  *$'\t'*) die "clipboard looks like the Application > Cookies table (has tabs). Copy the 'cookie:' request header from the Network panel instead." ;;
esac
for bad in "SID=" "__Secure-1PSID=" "SAPISID=" "APISID="; do
  case "; $cookie" in
    *"; $bad"*) die "clipboard contains Google cookies ($bad...). Copy the header of a request to www.reddit.com only." ;;
  esac
done
for need in reddit_session token_v2; do
  case "; $cookie" in
    *"; $need="*) ;;
    *) die "missing $need. Are you logged in to reddit.com in that browser, and did you copy the whole header?" ;;
  esac
done

names="$(printf '%s' "$cookie" | tr ';' '\n' | sed -E 's/^[[:space:]]+//; s/=.*//' | grep -v '^$' | tr '\n' ' ')"
echo "cookie OK: ${#cookie} characters"
echo "names: $names"

echo "setting REDDIT_COOKIE on Railway (service will redeploy)..."
# The value goes through stdin, so it never appears in the process list.
printf '%s' "$cookie" | railway variable set REDDIT_COOKIE --stdin ${target[@]+"${target[@]}"} >/dev/null \
  || die "railway variable set failed (see message above; run 'railway link' first?)"

echo "done. Railway is redeploying."
