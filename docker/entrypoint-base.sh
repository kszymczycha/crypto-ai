#!/bin/sh
# Shared entrypoint: prepare secrets + drop privileges to the bot user.
#
# Why a plain bind-mount is not enough:
#   Compose mounts the secret file 1:1 from the host, so it preserves its permissions
#   (chmod 600 => owner uid 1000). The process in the container runs as uid 10001
#   ("bot") and cannot read such a file. Loose permissions (644) on the host
#   would solve the problem, but expose the API keys to the whole system — a bad trade-off.
#
# What we do: start as root, copy the secret to /run/okx-secrets (outside the
# bind-mount), set the owner to "bot" and mode 0400, then drop
# privileges via setpriv. This way ONLY this process reads the keys.
#
# Variables:
#   SECRETS_FILE      — source of secrets (default /run/secrets/okx.toml)
#   REQUIRE_SECRETS   — "0" for processes without exchange access (collector, opencode)
set -eu

APP_USER="bot"
APP_HOME="/home/${APP_USER}"
SECRETS_FILE="${SECRETS_FILE:-/run/secrets/okx.toml}"
SECRET_DIR="/run/okx-secrets"
SAFE_SECRET="${SECRET_DIR}/okx.toml"
REQUIRE_SECRETS="${REQUIRE_SECRETS:-1}"

if [ "$REQUIRE_SECRETS" = "1" ]; then
  if [ ! -f "$SECRETS_FILE" ]; then
    echo "FATAL: secrets file $SECRETS_FILE is missing" >&2
    echo "       start the stack with a mode override (make demo / make live)" >&2
    exit 1
  fi

  if grep -q 'CHANGE_ME' "$SECRETS_FILE" 2>/dev/null; then
    echo "FATAL: secrets contain CHANGE_ME — fill in the OKX API keys" >&2
    exit 1
  fi

  mkdir -p "$SECRET_DIR"
  # The directory must belong to the process user — otherwise, after dropping
  # privileges, there is no execute (x) permission and reading the file fails with PermissionError.
  chown "$APP_USER:$APP_USER" "$SECRET_DIR"
  chmod 700 "$SECRET_DIR"
  cp "$SECRETS_FILE" "$SAFE_SECRET"
  chown "$APP_USER:$APP_USER" "$SAFE_SECRET"
  chmod 400 "$SAFE_SECRET"
  export SECRETS_FILE="$SAFE_SECRET"
fi

# Runtime directories must belong to the user we drop privileges to.
for dir in /app/data /app/logs; do
  [ -d "$dir" ] && chown "$APP_USER:$APP_USER" "$dir" 2>/dev/null || true
done

export HOME="$APP_HOME"

if [ "$(id -u)" = "0" ]; then
  exec setpriv --reuid="$APP_USER" --regid="$APP_USER" --init-groups "$@"
fi
exec "$@"
