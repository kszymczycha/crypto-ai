#!/bin/sh
# Collector entrypoint: no secrets (public data only), but it still drops
# privileges to the "bot" user through the shared entrypoint.
set -eu

echo "collector: start (interval ${COLLECTOR_INTERVAL_SECONDS:-60}s)"
exec /app/docker/entrypoint-base.sh python -m okxbot.data.collector
