#!/bin/sh
# Executor entrypoint: secrets + privilege drop (entrypoint-base.sh),
# then start FastAPI.
set -eu

echo "executor: start (OKX_FLAG=${OKX_FLAG:-1})"
exec /app/docker/entrypoint-base.sh python -m okxbot.executor.service
