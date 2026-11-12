#!/bin/sh
# OpenCode container entrypoint: waits for a healthy executor, then starts the TUI.
# It has no access to OKX secrets — hence REQUIRE_SECRETS=0.
set -eu

EXECUTOR_URL="${EXECUTOR_URL:-http://executor:8080}"

echo "opencode: waiting for $EXECUTOR_URL/health ..."
i=0
while [ "$i" -lt 30 ]; do
  if curl -fsS "$EXECUTOR_URL/health" >/dev/null 2>&1; then
    echo "opencode: executor is ready"
    break
  fi
  i=$((i + 1))
  sleep 2
done

if [ "$i" -ge 30 ]; then
  echo "FATAL: executor did not respond after 60 s" >&2
  exit 1
fi

# OpenCode reads permissions and the model from /app/opencode.jsonc. There is no flag
# that bypasses permissions — access is decided by the project configuration.
exec env REQUIRE_SECRETS=0 /app/docker/entrypoint-base.sh opencode
