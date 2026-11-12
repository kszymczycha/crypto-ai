# Runbook

## 0. Requirements

- Docker Desktop / Docker Engine with Compose v2 (`docker compose version`).
- OKX **demo** keys with spot trade permission (no withdraw), IP whitelist.
- For the `opencode` container: `OPENCODE_BASE_URL`, `OPENCODE_API_KEY` and
  `OPENCODE_MODEL_ID` in the host environment (or in `.env`).

## 1. Bootstrap (once)

```bash
make bootstrap                        # creates .env, directories, checks config
cp secrets/okx_demo.toml.example secrets/okx_demo.toml
$EDITOR secrets/okx_demo.toml         # paste api_key / secret_key / passphrase, flag = "1"
chmod 600 secrets/okx_demo.toml
```

Verify that `secrets/` does not end up in the repo:

```bash
git status --porcelain | grep secrets   # should return nothing
```

## 2. Starting DEMO (step by step)

```bash
make build                 # builds okxbot:local (executor/collector) + okxbot-opencode:local
make demo                  # = docker compose -f compose.yaml -f compose.demo.yaml up -d
make status                # executor and collector: Up (healthy); opencode starts interactively
make health                # {"ok":true,"mode":"demo","flag":"1"}
```

`opencode` is a separate image (`docker/opencode.Dockerfile`) — it adds Node 22 and
the OpenCode CLI, which the base image does not have. The first `make build` takes a
few minutes.

**Without `make` (e.g. a WSL host without the `make` package)** — the same steps by hand:

```bash
docker build -t okxbot:local -f Dockerfile .
docker build -t okxbot-opencode:local -f docker/opencode.Dockerfile .
docker compose -f compose.yaml -f compose.demo.yaml up -d
docker compose ps
docker compose exec executor curl -fsS http://127.0.0.1:8080/health
```

**`src/` and the agent files are mounted from the host** — the demo mounts `src/`,
`config/`, `docs/`, `tests/`, `AGENTS.md`, `PLAN.md`, `opencode.jsonc` and
`.opencode/`. Code changes take effect without a rebuild; rebuild the image after
changing `docker/opencode.Dockerfile` or the Python/Node dependencies. Image content
verification: §11.2.

Verification in the `opencode` container:

```bash
docker compose exec opencode python -m cli.status
docker compose exec opencode python -m cli.market_snapshot --pairs BTC-USDT
docker compose exec opencode python -m cli.place_order --pair BTC-USDT --side buy \
  --amount-usdt 10 --tp-pct 2 --sl-pct 1 --reason "test runbook"
```

Expected response shape: `{"ok":true,"status":"placed","order_id":"..."}`. Check in the
OKX demo panel that the order exists.

## 3. Shutdown

```bash
docker compose down          # containers stop; data/ and logs/ remain
docker compose down -v       # also removes the opencode-home volume
```

## 3a. Verification without Docker

Before you start the stack, you can check the project's consistency locally:

```bash
make validate      # required files, YAML, AST, secret boundaries, compose topology
make test          # unit tests locally (requires: pip install -e 'src[dev]')
make test-docker   # the same in a container — works without local pip/pytest
```

If you do not want to install the development dependencies system-wide (or you lack
`pip`), use `make test-docker`: the image `docker/test.Dockerfile` adds `pytest` to
`okxbot:local` and mounts the `tests/` directory read-only. It requires a prior
`make build`.

## 4. Switching to LIVE — safety checklist

Perform this **after** at least a week of running on demo. Tick each item deliberately.

- [ ] API key created with **Trade** permission (spot), **without** Withdraw.
- [ ] IP whitelist set to the address the executor egresses from.
- [ ] `secrets/okx_live.toml` exists, `flag = "0"`, permissions `600`.
- [ ] `config/risk_limits.yaml`: `max_order_usdt` ≤ 5% of capital (e.g. 20 with 400 USDT).
- [ ] `require_confirmation_above_usdt` ≤ 100.
- [ ] LIMITS on the exchange: daily withdrawal limit and alert configured.
- [ ] `docker compose config` passes without errors.
- [ ] `pytest tests/unit` test is green.
- [ ] You know how to stop the bot in 5 seconds (`docker compose stop executor`).
- [ ] You have a plan for a position open at the moment of shutdown (TP/SL stay on the exchange).

Starting live:

```bash
cp config/env/live.env.example config/env/live.env
make live        # = docker compose -f compose.yaml -f compose.live.yaml up -d executor collector
docker compose exec executor curl -fsS http://127.0.0.1:8080/health   # mode: live
```

First 24 h: set `max_order_usdt` to the minimum (5–10 USDT) and watch the logs.

## 5. Error 429 (rate limit)

| Symptom | Cause | Action |
|---|---|---|
| `{"ok":false,"error":"executor_unavailable"}` | executor rejects/limits | wait 2–3 s, retry once |
| `okx_retry` log with `attempt` 0..3 | OKX rate limit, backoff works | do nothing, the client retries on its own |
| `OkxRateLimitError` after 3 attempts | persistent limit (e.g. banned IP) | stop trading, check `logs/executor.log`, wait 60 s, lower the frequency |
| CoinGecko 429 | >100 req/min | the collector throttles itself (bucket 1.6 req/s); cache 60 s |

**Do not** increase `max_retries` "by force" — that deepens the throttling.

## 6. Where to look for logs

| What | Where |
|---|---|
| All services | `make logs` (or `docker compose logs -f executor`) |
| Executor (orders) | `logs/executor.log` — JSON, `msg` field: `order_placed`, `signal_rejected` |
| Collector | `logs/collector.log` — `collector_cycle` |
| OKX client (retry) | `logs/okx_client.log` — `okx_retry` |
| Market (snapshot) | `logs/market.log` — `market_snapshot` |
| State and history | `data/state.db` (SQLite): `signals`, `orders`, `daily_stats` tables |

Quick view of order history:

```bash
docker compose exec executor python -c "
import sqlite3;c=sqlite3.connect('data/state.db');c.row_factory=sqlite3.Row
print([dict(r) for r in c.execute('SELECT order_id,pair,side,state,created_at FROM orders ORDER BY id DESC LIMIT 10')])"
```

## 7. Common problems

| Symptom | Fix |
|---|---|
| `FATAL: secrets file ... is missing` | start with a mode override (`make demo` / `make live`) and check the file in `secrets/` |
| `FATAL: secrets contain CHANGE_ME — fill in the OKX API keys` | fill in the keys in the TOML file |
| `opencode` restarts in a loop | the executor is not passing its healthcheck — `docker compose logs executor` |
| Signal rejected: `amount ... exceeds 5% of equity` | lower `--amount-usdt`; the limit is computed from the USDT balance |
| Signal rejected: `pair cooldown until ...` | wait 10 s or use a different pair |
| All signals `rejected: daily loss limit 3% reached` | the UTC day is locked; analysis in `docs/risk_policy.md` |
| `422` on `/signal` | payload does not match the schema (side/pair/reason/key length) |

## 8. Known risks

### 8.1 An order without TP/SL passes validation

`config/risk_limits.yaml` **does not contain** the `tp_pct`/`sl_pct` fields, and
`src/okxbot/executor/risk_engine.py` does not check that they are present. TP/SL are
passed in the signal payload as `tp_pct`/`sl_pct` and converted to prices only in
`src/okxbot/executor/order_router.py:34-44`. A signal without these fields will be
accepted and will place an order **without protection**.

`docs/risk_policy.md` calls such an order a "process error", but **nothing blocks it
technically**. Until `require_tp_sl: true` is implemented (see below), prevention
depends solely on the agent's discipline — the mandatory rule is written down in
`docs/strategy.md`.

**Target state (Stage 2, out of scope for now):** add `require_tp_sl: true` to
`config/risk_limits.yaml` and a check in `risk_engine.validate()`, so that the executor
rejects a signal without TP/SL. It requires a change in `src/` — report it, do not do it
yourself.

### 8.2 No handling of partial fills

The `partially_filled` type exists (`src/okxbot/storage/models.py:16`) and counts toward
the open position limit (`src/okxbot/storage/db.py:216-230`), but this runbook does not
describe a procedure. There is no agreement on what to do with the rest of the order.

### 8.3 No "cancel-all / flatten" procedure

The executor exposes only `POST /signal`, `GET /health`, `GET /state`, `POST /cancel`
(`docs/architecture.md:26`). `POST /cancel` cancels **one** order by `ordId`. There is no
bulk order. On shutdown (`docker compose stop executor`) open TP/SL stays on the
exchange — that behavior is intentional, but it requires manually closing the position in
the OKX panel.

### 8.4 Selling — the position size cannot be read by any CLI

`POST /signal` accepts only `amount_usdt` (the `SignalRequest` schema in
`src/okxbot/executor/service.py:30-39`). For a sell, the executor computes
`amount_base = amount_usdt / entry` itself, where `entry` is the **current market price**
(bid), not the position's average entry price (`order_router.py:26-29, 46-48`).

Problem: `PositionManager.open_positions()` computes the held amount (`base_amount`,
`src/okxbot/executor/position_manager.py:40-86`), but **the class is dead code** — it is
not imported anywhere, and `/state` returns only `balances`, `pending_orders` and `daily`
(`service.py:214-229`). `cli.positions` shows *pending* orders from OKX, so a completed
buy does not appear there.

The only available source of quantity is the account balance (`cli.balance --ccy <BASE>`),
which covers the **entire** balance of a given currency, not just what the bot acquired. On
a clean demo account these notions coincide; on a shared account they do not.

The rule "sell 100% of the position" (`docs/strategy.md`) is therefore feasible only as an
**approximation, and only on a dedicated account**. Exact selling requires an `amount_base`
field in the signal or wiring `PositionManager` into `/state` — Stage 2, changes in `src/`,
to be reported.

If the bid falls between reading the balance and placing the order, the computed
`amount_base` will exceed the balance and OKX will reject the order (a safe failure —
nothing is sold, the position stays open).

### 8.5 `max_open_positions` is not enforced for market orders

**This is a risk-control bypass, not cosmetics.** The "max 3 open positions" limit from
`AGENTS.md` **does not work** in the current implementation.

Mechanism: `StateStore.count_open_positions()`
(`src/okxbot/storage/db.py:256-262`) counts buy orders in the states
`('live','partially_filled')`. But `order_router` **always** places `market` orders
(`order_router.py:56, 67, 83`; `POST /signal` does not even have an `order_type` field),
and on OKX a market order is immediately `filled`. A `filled` order does not fit the
filter, so the counter **always returns 0**.

Verified empirically: after 5 completed market buys
`count_open_positions()` → **0**; the limit of 3 never activates.

**Do not fix this by adding `'filled'` to the filter — that is worse than the bug.**
Nothing in the code does an `UPDATE`/`DELETE` on `orders`, and a sell neither closes a
position nor writes `realized_pnl`. The counter would become **monotonic**: after 3 buys
*in the entire history of the bot* trading would be permanently blocked, despite
long-closed positions.

The correct direction: count the **net position per pair** (`SUM(amount_base)` of buys
minus sells, grouped by `pair`, completed orders only), not "orders in the live state".
Verified: such a counter drops after a sell and correctly tracks a partially closed
position. It requires changes in `storage/db.py` **and** `executor/risk_engine.py` —
outside `strategy/`, to be reported and separately approved.

Until it is fixed: **the agent controls the number of open positions manually**, via
`cli.balance --ccy <BASE>` for each pair. The limit of 3 is not enforced by the executor
and cannot be relied on.

## 9. Pipelines in scripts — the exit status lies

**Rule:** in scripts and the Makefile **never end a pipeline with `tail`/`head`/`grep`
without `set -o pipefail`.** A pipeline's exit status is the status of the **last**
command, not the one that matters.

```bash
# WRONG — reports success when the build failed (tail returns 0)
docker build -t obraz . | tail -3 && echo "BUILD OK"

# RIGHT
set -o pipefail
docker build -t obraz . | tail -3 && echo "BUILD OK"
```

Verified: `false | tail -3` returns **0** without `pipefail` and **1** with it.

This is the same class of error as `docker compose config` (chapter 6): a "diagnostic"
command reports a state it never checked. In the `Makefile` the problem is already fixed
globally by `.SHELLFLAGS := -eu -o pipefail -c` — there is no need to repeat it in every
recipe. Outside the Makefile you have to remember it yourself.

**Practical consequence for the agent:** do not trust `&& echo "OK"` after a pipeline.
Check `$?` of the command that matters, or use `pipefail`.

### 9a. Secret scanning: do not trust the pattern alone

When checking whether the repo or its history contains keys, **do not rely on string
length.** The pattern `[0-9a-f]{32,}` catches **commit SHA-1 hashes** (40 hex characters)
— that is, git history itself, in which every commit has such a hash. The scan then
returns matches and it looks like a leak, even though it is not one.

Rule: **whitelist 40-character hex or check vendor prefixes**, not length alone:

```bash
# WRONG — commit hashes show up as "keys"
git log --all -p | grep -cE "[0-9a-f]{32,}"

# RIGHT — specific vendor prefixes
git log --all -p | grep -cE "sk-[A-Za-z0-9]{20,}|sk-or-|sk-ant-|okx_[a-z0-9-]{20,}|BEGIN [A-Z ]*PRIVATE KEY"
```

Before you announce a leak, **check what exactly matched** — print the context of the
matches. In this repo a real scan produced 5 hits, all of them SHA-1 hashes plus one
`client_order_id` in a test; real keys numbered **zero**.

It is the same pattern as above: the tool returned output, not an answer.

Additionally: **do not trust build time.** The Docker cache can make
`docker build` take 0.2 s and return success even though the image does not contain the
new code. When the result matters, verify the **image content**, not its metadata:

```bash
docker run --rm --entrypoint sh okxbot-test -c 'grep -n wzorzec /app/src/...'
```

## 10. Fix order after the audit (2026-11-01)

The code audit confirmed that some of the limits from `docs/risk_policy.md` are today
enforced only partially. Below is the established state and the order that **is not open
to negotiation while working** — so that the priorities are not lost in a month.

**Overriding rule: live is off until points 1–4 are closed. Demo — yes, immediately after
the keys are entered.**

### 10.1 State after the audit

| Problem | Where in the code | Status |
|---|---|---|
| TP/SL for `ordType=market` sent as top-level `tpTriggerPx`/`slTriggerPx`; in OKX v5 attached TP/SL goes through `attachAlgoOrds` | `client/okx_client.py:261-264` | **to be verified on demo** — it may mean that no position is protected |
| `max_daily_loss_pct` never activates: `add_realized_pnl()` has no call sites in `src/` | `storage/db.py:293`, read in `executor/risk_engine.py:132-142` | the 3% limit is decoration |
| `max_open_positions` counts `live/partially_filled`, while a market order is `filled` right away; no `UPDATE orders` anywhere in `src/` | §8.5 | risk-control bypass |
| Selling computed from `amount_usdt / entry`, not from the held `amount_base` | §8.4 | the emergency exit may fail |
| The 10 s cooldown does not enforce "one order = one signal" (setups within an episode are 4–8 h apart) | §10.6 | production keeps adding into a decline, losses 1.5× larger |
| A signal without `tp_pct`/`sl_pct` passes validation | §8.1 | depends on the agent's discipline |
| A rejected signal stores `idempotency_key` (`UNIQUE`), so a valid retry comes back as `duplicate` with `ok:true` | `executor/service.py:132-156` | the key swallows a signal that was not executed |
| `docs/architecture.md` describes `executor/models.py`, the `positions` and `daily_pnl` tables — they do not exist | `docs/architecture.md:51,77-78` | the documentation lies about the code |

### 10.2 Order

1. **OKX demo keys** — the user's task, not the agent's. Nothing moves without them.
2. **`make demo` + TP/SL verification in the OKX panel** — zero code, ~an hour.
   The first test order is meant **solely** to check whether TP/SL algo orders are
   visible in the OKX panel after the purchase. If they are not: **stop**,
   switch `okx_client.place_order` to `attachAlgoOrds`, and only then anything
   else. This point decides all the others.
3. **Reconciliation with OKX — a separate sprint (1–2 days), not a "quick fix".**
   Scope: `GET /account/balance`, `/account/positions`, `/account/fills` →
   `UPDATE orders` in `state.db`, recomputing `realized_pnl_usdt` from fills,
   net position per pair. Additionally, fixing `idempotency_key`: a `rejected` or
   `needs_confirmation` signal must not block a retry as
   `duplicate`, because it was not executed. Separate commit, separate tests, separate
   verification on demo. **Reconciliation will not protect a position from a loss if
   TP/SL does not work — that is why point 2 comes first.**
4. **`require_tp_sl: true`** — only after reconciliation. This is a signal contract
   change: it requires updating the tests **and** all documents with signal examples
   (`AGENTS.md`, `docs/risk_policy.md`, `docs/strategy.md`). Do not do it
   as a single line in `risk_engine.py`.
5. **`executor/orders.py`** — remove it or use it in `service.py` (today dead code,
   and the validation flow is duplicated). Cleanup, not a priority.
6. **Backtest — a condition for going live, not a "nice to have".** 30–90 days of data,
   comparison with buy-and-hold, win rate, average R/R. Without it the bot has no basis
   to believe it earns — only that it "does not lose faster than the market".
7. **Only then the strategy.** The infrastructure executes the decision; a decision without
   an edge is a coin flip with commissions.

### 10.3 Rules along the way

- **Do not run live until points 1–4 are closed** — regardless of
  how good the demo looks.
- **Do not fix `count_open_positions` by adding `'filled'` to the filter**
  (§8.5) — that is worse than the bug.
- **A document that lies about the code is worse than no document** —
  fix `docs/architecture.md` along with reconciliation, not separately.
- Any of the above changes in `src/` (outside `strategy/`) requires a report and
  explicit approval — see `AGENTS.md` and the `request-scope` skill.

### 10.6 The cooldown does not enforce "one order = one signal"

`docs/strategy.md` requires: "One order = one signal. No scaling, no
adding without a new signal". **The code does not enforce this.**

`risk_engine.py:144-149` checks `cooldown_seconds_between_signals`
(`config/risk_limits.yaml:13`) — **10 seconds**. Setups within one market move are
4–8 hours apart (one 4H candle), so the cooldown does not block them. The second
mechanism that could catch this — `max_open_positions` — also does not work for
market orders (see §8.5).

**Consequence:** production enters **every** signal of an episode, buying another
15 USDT each time in a row. That is not "scaling after a new decision", it is
adding into a decline.

Measured on history (backtest B1, `scripts/backtest.py --full`):

| Variant | Trades | Sum of returns | MaxDD |
|---|---|---|---|
| 1 entry per episode (as per the docs) | 44 | −73,4% | 78,3% |
| every signal (like production) | 68 | **−110,5%** | **119,9%** |

In terms of 300 USDT of capital and a 15 USDT position: **−16,6 USDT instead of
−11,0 USDT** over 4,35 years.

**To be fixed together with reconciliation (§10.2 item 3)** — together with `max_open_positions`
and the P&L computation. Until then the agent controls the number of entries in a single move:
one signal per pair per episode, the next one only after the position is closed.

## 11. First run after entering the keys (demo)

Procedure for the state in which the demo keys are already in `secrets/okx_demo.toml`.
The order matters — point 2 prevents starting **old code**.

### 11.1 Keys (file, not chat)

```bash
cp secrets/okx_demo.toml.example secrets/okx_demo.toml   # if it does not exist
chmod 600 secrets/okx_demo.toml
# fill in: api_key, secret_key, passphrase, flag = "1"
```

`load_credentials` (`src/okxbot/config.py:230-243`) requires `api_key`,
`secret_key`, `passphrase`; `flag` must be `"1"` (demo) or `"0"` (live).
Do not paste keys into a chat or into the agent prompt.

### 11.2 Rebuild the images after changing the Dockerfile or dependencies

`src/` is mounted from the host to `/app/src` (RW), so a code change works without
a rebuild. Rebuild the image after changing `docker/opencode.Dockerfile`, the
`@opencode/cli` version or the Python dependencies. The files `AGENTS.md`, `opencode.jsonc` and
`.opencode/` are also mounted from the host (read-only), so editing them does not require
a rebuild.

```bash
docker build -t okxbot:local -f Dockerfile .
docker build -t okxbot-opencode:local -f docker/opencode.Dockerfile .
```

Verifying image content (not metadata — see §9a):

```bash
docker run --rm --entrypoint sh okxbot:local -c \
  'grep -c "def adx" /app/src/okxbot/data/indicators.py'
# expected: 1. "0" means an image from before Stage A — do not start it.
```

### 11.3 Start (demo, without `opencode` to begin with)

```bash
docker compose -f compose.yaml -f compose.demo.yaml up -d executor collector
docker compose ps
docker compose exec executor curl -fsS http://127.0.0.1:8080/health
# {"ok":true,"mode":"demo","flag":"1"}  ← mode MUST be demo
```

The `opencode` container uses the permissions from `opencode.jsonc`: `shell` and `edit`
ask by default, `src/okxbot/strategy/**` is allowed, and reading `secrets/` and
`.env` is blocked. **There is no flag that bypasses the permissions.** Do not enable it
until TP/SL is confirmed — an agent with unprotected positions is a bad
combination.

### 11.4 Verifying data and the gate

```bash
docker compose exec executor python -m cli.market_snapshot --pairs BTC-USDT,ETH-USDT,SOL-USDT
```

Check: `adx` and `regime` are numbers (not `null`), `candles_count >= 210`,
`evaluation.allow_entry` is a bool. **`adx: null` = the image was not rebuilt.**

### 11.5 TP/SL test — it decides everything that follows

```bash
docker compose exec executor python -m cli.place_order --pair BTC-USDT --side buy \
  --amount-usdt 10 --tp-pct 3 --sl-pct 1.5 --reason "test TP/SL demo"
```

Then, **in the OKX demo panel**, check whether **TP/SL algo orders** are visible next to
the market order.

| Observation | Meaning | Next step |
|---|---|---|
| market `filled` + algo TP/SL visible | `tpTriggerPx` works for `ordType=market` | the hypothesis from §10.1 is disproved, continue |

## 12. Backtests: public history only (`flag="0"`), never demo

**Rule:** historical analyses may be computed only on the public history endpoint
(`flag="0"`, no keys, no orders). Demo (`flag="1"`) is suitable for
testing signals on current candles, **not for backtests**.

### 12.1 What is broken

`get_history_candlesticks` with `flag="1"` returns distorted OHLCV: individual
candles with prices one or two orders of magnitude higher than their neighbours, while
monotonic time is preserved. Example (BTC-USDT, 4H, `flag="1"`):

| ts | open | high | low | close |
|---|---|---|---|---|
| 2023-07-02 08:00 | 30 562,5 | 30 599,8 | 29 438,5 | **29 445,3** |
| 2023-07-02 12:00 | 29 444,7 | **1 122 986,1** | 29 437,6 | **1 112 220** |
| 2023-07-02 16:00 | 1 112 220 | **36 162 247,1** | 118 001 | 178 852,6 |
| 2023-07-02 20:00 | 178 852,6 | 185 733,2 | 107 986,5 | 133 653,3 |

The same candles with `flag="0"`: close 30 510,4 / 30 499,4 / 30 559,8 / 30 615,6 —
no spikes.

### 12.2 Scale (2022-06 → 2026-11, 4H, comparison by `ts`)

| pair | candles compared | close divergence > 5% | share | years |
|---|---|---|---|---|
| BTC-USDT | 9 729 | 58 | 0,6% | 2022–2023 |
| ETH-USDT | 9 729 | 102 | 1,0% | 2022–2026 |
| SOL-USDT | 9 729 | 39 | 0,4% | 2022–2025 |

The share is small, but **one candle poisons every indicator computed on it
and on the window after it**: SMA(100) on 4H carries a spike for 100 bars ≈ 17 days,
and the candle's max/min enter ATR and ADX. In the backtest, the Regime Rotation run on demo
gave a peak equity of 1 947 USDT from 300 (max DD 95,8%) — an artifact of one bar
with `high = 36 million`; the same run on `flag="0"` gave 981 USDT / DD 48%.

### 12.3 What is NOT broken

`get_candlesticks` (current candles, the last ~300) from demo **agrees**
with the public history to within 0.01% (checked 2026-11-03: close 77 738,7 vs
77 738,4). Current demo signals are therefore based on correct candles —
what is broken is the history endpoint, not the demo market as a whole.

### 12.4 How to check the data before drawing a conclusion

```bash
# 1. Is the result "too good"? Data first, then the strategy.
# 2. Quick spike test on 4H: |close_t / close_{t-1} - 1| > 0.30 is a red flag.
# 3. Always run the backtest with an explicit source:
python scripts/backtest.py --regime-rotation --market-flag 0
```

`--market-flag 1` exists only so that the artifact can be reproduced and
compared. **Do not report results from `--market-flag 1`** (the default `0` is
correct). Exception: comparing both sources, as in §12.2.
| market `filled`, **no** algo | **position without protection** | `attachAlgoOrds` in `okx_client.py` — before anything else |
| `strategy_rejected` at RSI > 35 | gate A1 works correctly | OK |
| `422` on `/signal` | payload does not match the schema | see §7 |

### 11.6 Starting state

`data/state.db` persists between runs: old signals and daily
counter affect the cooldown and `max_signals_per_day`. For a clean start:

```bash
docker compose down
mv data/state.db data/state.db.$(date +%Y%m%d-%H%M%S).bak
```

### 11.7 Shutdown

```bash
docker compose -f compose.yaml -f compose.demo.yaml down
```

Without `-v` (`-v` removes the `opencode-home` volume). `data/` and `logs/` remain.
