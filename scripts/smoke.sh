#!/usr/bin/env bash
# CLI smoke test inside a running stack (run on the host with Docker).
set -euo pipefail

cd "$(dirname "$0")/.."

echo "== health"
docker compose exec -T executor curl -fsS http://127.0.0.1:8080/health
echo

echo "== cli.status"
docker compose exec -T opencode python -m cli.status

echo "== cli.balance"
docker compose exec -T opencode python -m cli.balance

echo "== cli.market_snapshot"
docker compose exec -T opencode python -m cli.market_snapshot --pairs BTC-USDT --no-coingecko
