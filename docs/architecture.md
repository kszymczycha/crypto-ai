# Architecture

## Purpose

Deliver a decision signal (from OpenCode) to an order on OKX spot in a
repeatable, auditable way bounded by risk policy. OpenCode does **not** have a
connection to the exchange; `executor` is the sole owner of the keys.

## Components

### opencode (container)

- Based on `python:3.12-slim` + Node 22 (image `docker/opencode.Dockerfile`).
- Contains the OpenCode CLI (`@opencode/cli`) and the `okxbot` + `cli` package.
- Variables: `EXECUTOR_URL`, `OPENCODE_BASE_URL`, `OPENCODE_API_KEY`,
  `OPENCODE_MODEL_ID`.
- Configuration: `opencode.jsonc` (model, provider, permissions) and `AGENTS.md`.
- Mounts: `src/` (RW — strategy), `config/` (RO), `docs/` (RO), `tests/` (RO),
  `data/` (RO), `logs/` (RW), `.opencode/` (RO). **No `secrets/`**.
- Has egress to the model provider; it has no OKX keys, so it cannot place
  an order bypassing the executor.

### executor (container)

- FastAPI + uvicorn on port 8080, non-root USER.
- Holds the keys: `secrets/okx_demo.toml` (demo) or `secrets/okx_live.toml` (live),
  read-only mount.
- Responsible for: `POST /signal`, `GET /health`, `GET /state`, `POST /cancel`.
- Runs `risk_engine.validate()` → strategy gate → `OrderRouter.execute()`
  → write to `state.db`.
- Does not call OpenCode (no reverse dependency).

### collector (container)

- Asynchronous loop: fetches public OKX tickers and CoinGecko data,
  computes indicators (RSI, MA), writes snapshots to `state.db`.
- **No `secrets/`**, no right to trade (public endpoints only).
- Writes history to `state.db`; the `cli.market_snapshot` CLI fetches current data
  directly from OKX (without secrets), so it does not depend on the collector.

> Invariant: `OkxClient` has a **split** SDK initialisation. `_init_public_sdk()`
> requires no keys (tickers, candles), `_init_private_sdk()` **always** requires them —
> also when SDK clients are injected. This way the collector and `cli.market_snapshot`
> work without secrets, and no trading path can skip them.

## Data flow

1. `collector` → OKX public `/api/v5/market/tickers` + CoinGecko `/simple/price`
   → `state.db` (`market_snapshots` table, cache in `data/cache/`).
2. OpenCode reads the snapshot: `python -m cli.market_snapshot --pairs BTC-USDT,ETH-USDT`
   → JSON (1–3 lines).
3. OpenCode sends the signal: `python -m cli.signal --pair BTC-USDT --side buy ...`
   → `POST http://executor:8080/signal`.
4. `executor`: `risk_engine.validate()` (limits, balance, daily P&L, number of
   positions) → strategy gate `evaluate_entry()` for a buy (rule from
   `strategy/rules.py`, fresh data from closed candles)
   → if OK: `OrderRouter.execute()` on OKX → write to `orders` and `signals`
   in `state.db`.
   → if `needs_confirmation`: returns the status without placing an order.
5. OpenCode checks the result: `python -m cli.status` → balance, positions, daily P&L.

## Threat model and trust boundaries

| Boundary | Principle |
|---|---|
| API keys | Only in `secrets/` in the executor container; read-only mount |
| Key permissions | Spot trade; **no withdraw**; IP whitelist |
| Public data | Tickers/candles without a signature and without keys — the collector and CLI read them without secrets |
| Network | `botnet` (internal: true) for opencode↔executor↔collector; a separate `egress` network provides internet (OKX/CoinGecko/model provider) |
| OpenCode → OKX | Impossible: no keys and no client in the opencode container |
| Executor → OpenCode | Forbidden: no callbacks |
| Secrets in git | `.gitignore` + `.dockerignore`; only `.example` and `.toml.example` |
| Amounts | `max_order_usdt` 50, `require_confirmation_above_usdt` 100 — validation on the executor side |

Docker: `botnet` is `internal: true` — containers talk over it internally
(`http://executor:8080`), but this network has no exit to the outside world. Each of the three
services is additionally on the `egress` network (a regular bridge), which provides internet:
executor → OKX, collector → CoinGecko, opencode → model provider. Port 8080 of the
executor **is not published** to the host, so nobody from outside can reach the bot's
API.


## Data and state

- `data/state.db` — SQLite, WAL. Tables: `signals`, `orders`, `daily_stats`,
  `market_snapshots`, `kv`.
- `data/cache/` — HTTP cache (TTL 60 s for prices, 300 s for metadata).
- `logs/` — JSON logs (rotation in the application).

## DEMO / LIVE modes

- `flag='1'` (demo): header `x-simulated-trading: 1`, OKX endpoints the same as live.
- `flag='0'` (live): no simulation header.
- Switching = changing the mounted TOML file + `OKX_FLAG` in the compose override.
  Both modes use **the same** REST URLs.

## CLI ↔ executor communication

- Signal contract: JSON, see `src/okxbot/executor/service.py` and `AGENTS.md`.
- Response: short JSON, `{"ok": true|false, ...}`.
- Every CLI: argparse, no interactivity, one JSON on stdout, exit 0/1,
  logs to `logs/`.

## Out of scope

- Automatic signal generation. The rules in `strategy/rules.py` are
  enforced by the executor's gate, but the decision is made by the OpenCode agent.
- Backtesting is external (`scripts/backtest.py`), it is not part of the executor.
- Websockets (REST for now), UI, alerts.
