#!/bin/sh
# Start as root only long enough to hand the session directory to the
# unprivileged user (Railway mounts volumes owned by root), then run the
# server, and the browser it starts, as that user. If anything is off (a
# store path that would hand a system directory to that user, or no way to
# drop root), the container stops: it never falls through to running as root.
set -eu

# The root phase runs only system tools, never anything from the app's PATH.
app_path=$PATH
PATH=/usr/sbin:/usr/bin:/sbin:/bin
export PATH

fail() {
  echo "docker-entrypoint: $*" >&2
  exit 1
}

if [ "$(id -u)" = "0" ]; then
  # The same trimming the server applies, so both agree on the path.
  store=$(printf '%s' "${REDDIT_SESSION_STORE:-}" | sed 's/^[[:space:]]*//; s/[[:space:]]*$//')
  if [ -n "$store" ]; then
    case "$store" in
      /*) ;;
      *) fail "REDDIT_SESSION_STORE must be an absolute file path, e.g. /data/reddit-session.json" ;;
    esac
    case "$store" in
      */) fail "REDDIT_SESSION_STORE must name a file, not a directory" ;;
    esac
    # Lexical only (-s): never follow a symlink someone left on the volume.
    store=$(realpath -s -m -- "$store")
    dir=$(dirname -- "$store")
    case "$dir" in
      / | /app | /app/* | /bin | /bin/* | /boot | /boot/* | /dev | /dev/* | /etc | /etc/* | /home | \
      /lib | /lib/* | /lib64 | /lib64/* | /ms-playwright | /ms-playwright/* | /opt | /proc | /proc/* | \
      /opt/* | /root | /root/* | /run | /run/* | /sbin | /sbin/* | /srv | /sys | /sys/* | /tmp | /tmp/* | \
      /usr | /usr/* | /var | /var/*)
        fail "refusing to give $dir to pwuser; put the store on its own volume, e.g. /data/reddit-session.json" ;;
    esac
    export REDDIT_SESSION_STORE="$store"
    mkdir -p "$dir"
    chown pwuser:pwuser "$dir"
    chmod 700 "$dir"
  fi
  export HOME=/home/pwuser USER=pwuser LOGNAME=pwuser
  if command -v setpriv >/dev/null 2>&1; then
    exec setpriv --reuid=pwuser --regid=pwuser --init-groups -- env PATH="$app_path" "$@"
  fi
  if command -v runuser >/dev/null 2>&1; then
    exec runuser -u pwuser -- env PATH="$app_path" "$@"
  fi
  fail "cannot drop root (no setpriv or runuser); refusing to run as root"
fi

PATH=$app_path
exec "$@"
