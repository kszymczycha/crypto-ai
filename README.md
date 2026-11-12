# okx-bot — OKX trading bot infrastructure (demo/live)

Spot bot for OKX with separated responsibilities: **OpenCode** analyzes and sends
signals, the **deterministic executor** validates them and places orders on the
exchange. Project scope: infrastructure + strategy rules as guidance for the agent.
Decision rules are implemented in `src/okxbot/strategy/rules.py` (described in
`docs/strategy.md`). The executor enforces the entry gate but **does not generate
signals** — OpenCode makes the decision and sends a ready signal. Backtesting is an
external tool (`scripts/backtest.py`); there is no automatic trading.

> ⚠️ Start with DEMO. Enable live only after passing the checklist in `docs/runbook.md`.

## Architecture (overview)

```
┌────────────────┐   HTTP POST /signal   ┌──────────────┐   OKX REST v5   ┌─────────┐
│    opencode    │ ────────────────────▶ │   executor   │ ──────────────▶ │   OKX   │
│ (OpenCode CLI) │ ◀──── GET /state ──── │  :8080       │ ◀────────────── │ sandbox │
└────────────────┘                       └──────┬───────┘                 └─────────┘
                                             │
                                      ┌──────▼───────┐
                                      │  state.db    │  (SQLite, volume)
                                      └──────▲───────┘
                                             │
                                      ┌──────┴───────┐   CoinGecko + OKX
                                      │  collector   │ ──────────────────▶  (market data)
                                      └──────────────┘
```

- `opencode` — **has no API keys and does not touch OKX**. It calls thin CLI wrappers
  that talk to `executor` over the `botnet` network.
- `executor` — the only holder of keys (read-only `secrets/` mount), validates the
  signal through `risk_engine`, places orders, keeps state in `data/state.db`.
- `collector` — fetches market data (OKX public + CoinGecko) and writes it to `state.db`.
  It has **no** access to `secrets/`.

## Quick start (DEMO)

```bash
make bootstrap          # .env, directories, secrets template + YAML validation
cp secrets/okx_demo.toml.example secrets/okx_demo.toml   # paste demo keys
make build              # okxbot:local + okxbot-opencode:local
make demo               # docker compose -f compose.yaml -f compose.demo.yaml up -d (3 services)
make logs               # view logs
make shell              # shell in the opencode container
python -m cli.status    # {"ok":true,...} — run INSIDE the opencode container
make down
```

Shutdown and the full verification scenario: `make status`, `make health`, `scripts/smoke.sh`.

Full step-by-step instructions: `docs/runbook.md`.
Risk rules: `docs/risk_policy.md`. Technical design: `docs/architecture.md`.

## OpenCode (agent)

The `opencode` container runs OpenCode V2 and has no OKX secrets. Project
configuration lives in `opencode.jsonc` (model, provider, permissions) and `AGENTS.md`
(working rules). The model provider is controlled by these variables:

```bash
OPENCODE_BASE_URL=https://.../anthropic   # endpoint compatible with the Anthropic API
OPENCODE_API_KEY=...                      # provider key (do not commit)
OPENCODE_MODEL_ID=deepseek-v4.1-flash    # model identifier
```

For an OpenAI-compatible endpoint, change `package` in `opencode.jsonc` to
`@opencode/ai/providers/openai-compatible`. Permissions: `shell` and `edit`
ask by default, `src/okxbot/strategy/**` is allowed without asking, and reading
`secrets/` and `.env` is blocked.

## Structure

```
compose.yaml            executor + collector + opencode (botnet/egress networks)
compose.demo.yaml       DEMO override  (flag=1, secrets/okx_demo.toml)
compose.live.yaml       LIVE override  (flag=0, secrets/okx_live.toml)
Dockerfile              base image for executor/collector (python:3.12-slim, non-root user)
docker/opencode.Dockerfile  opencode image (Node 22 + OpenCode CLI, no OKX secrets)
opencode.jsonc          OpenCode model, provider and permissions
AGENTS.md               agent working rules (loaded automatically by OpenCode)
.opencode/skills/       OpenCode skills (e.g. request-scope)
config/                 settings.yaml, risk_limits.yaml, symbols.yaml, env/
src/okxbot/             config, client/, data/, storage/, strategy/, executor/, logging/
src/cli/                status, balance, market_snapshot, signal, place_order, cancel_order, positions
docker/                 executor.Dockerfile, collector.Dockerfile, opencode.Dockerfile, entrypoint-*.sh
scripts/                bootstrap.sh, smoke.sh, validate_static.py, entrypoint-*.sh
tests/                  unit/, integration/
docs/                   architecture.md, risk_policy.md, runbook.md
```

## Security — hard rules

1. OKX keys only in `secrets/okx_live.toml` / `secrets/okx_demo.toml` (TOML, read-only mount).
2. Key permissions: **spot trade**, no withdraw, IP whitelist.
3. `.gitignore` covers `secrets/`, `.env`, `data/`, `logs/`, `*.db`.
4. A signal never carries an amount > `max_order_usdt`; the executor rejects limit violations.
5. `opencode` → `executor` only over the internal Docker network.

## Requirements

- Docker + Docker Compose v2
- Python 3.11+ (only for local CLI tests)
- OKX keys: demo (sandbox) to start; live only after the checklist

## Tests

```bash
make test         # pytest locally (unit always; integration only when OKX_FLAG=1)
make test-docker  # pytest in the container — no local pip (requires make build)
make lint         # ruff + compileall
make validate     # project static validation (no Docker)
```
