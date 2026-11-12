# Decision strategy

Decision document for the agent (OpenCode) working with this bot.

**The rules are implemented in `src/okxbot/strategy/rules.py`** and in
`src/okxbot/data/indicators.py`. This document describes **the intent and background
of decisions** — the code describes the implementation. On divergence the code
decides; a divergence is a bug to fix, not something to interpret.

Overriding principle: **fail-closed**. Missing data never lets an entry through.

Established 2026-11-01. Updated 2026-11-01 after the rules were implemented in code.

## Operating mode

- Work **exclusively on demo** (`flag = "1"`). Live only with explicit, separate
  user consent — never as part of this strategy.
- The agent **proposes** signals; the user **approves every order**.
  The agent does not place an order without confirmation.
- Loop cadence: every **15 minutes**. The collector gathers data in the background (60 s), so a
  rarer tick does not lose data.
- Pairs: `BTC-USDT`, `ETH-USDT`, `SOL-USDT` (`config/symbols.yaml`).
- Horizon: **4H** candles (`config/settings.yaml: candle_bar: 4H`).

## Entry rule (buy only)

    RSI(14) <= 35
    AND regime != "down"        (regime = price position relative to MA(200))
    AND ADX(14) < 25            (trend strength: consolidation only, not a trend)
    AND vol_ratio >= 1.0        (candle volume vs SMA(20) of volume)

All four conditions must be met simultaneously.

Rationale for each component:

- **RSI(14) <= 35** — the 35 threshold instead of the classic 30 gives more opportunities on
  a 4H horizon. RSI alone is not enough: oversold in a downtrend is
  usually "even cheaper", not an opportunity.
- **regime != "down"** — the most important filter. Price below MA(200) means the
  broader trend is down; no buy signal passes then.
- **ADX(14) < 25** — trend strength. MA(200) tells direction, ADX tells whether a trend
  exists at all. At ADX >= 25 the market is moving and "oversold" tends to be
  a continuation of the decline; this rule buys in consolidation. Missing ADX blocks entry
  (fail-closed).
- **vol_ratio >= 1.0** — volume must confirm the move. A rise on thin
  volume is often a false move.

Candles are computed **only from closed bars** — `MarketService.snapshot`
drops the current, forming candle (`drop_forming_candle`). Otherwise
`vol_ratio` is understated and the hint changes within the same bar.

### Unknown ≠ flat (important)

The rule distinguishes **missing data** from a **computed result**:

| State | Value | Behaviour |
|---|---|---|
| MA(200) cannot be computed | `regime = None` | **blocks** (fail-closed) |
| Computed, price at the average | `regime = "flat"` | **passes** |
| Computed, price above the average | `regime = "up"` | passes |
| Computed, price below the average | `regime = "down"` | blocks |

`compute_snapshot` returns `None` for indicators that cannot be computed — **it never
substitutes `"flat"`**. It used to substitute it, so a snapshot without MA(200) was
indistinguishable from a genuine flat and passed the regime filter. That was a bug.

The same principle applies to `vol_ratio` and `rsi`: a missing value blocks.

## Exits: TP/SL from ATR

    tp_pct = atr_pct × 3.0
    sl_pct = atr_pct × 1.5

where `atr_pct` is ATR(14) expressed as a percentage of price. Exits therefore scale
with volatility: a calm market gets tight TP/SL, a choppy one — wide ones.
Fixed 2%/1% does not have that property.

Safety bounds: `MIN_TP_SL_PCT = 0.5`, `MAX_TP_SL_PCT = 20.0`.
When ATR is unavailable, the fallback values `2.0% / 1.0%` are used.

**There is no sell rule based on RSI.** The previous version (`RSI > 70 →
sell`) was removed: a position is closed by TP/SL, and an RSI exit would duplicate
that role and introduce selling without a holding condition.

### Manual sell / emergency exit

When a position has to be closed outside TP/SL, the following applies:

**We sell 100% of the position in a given pair** — no partial exits. With
capital of 300–500 USD and max 50 USDT per position, partial exits make no
economic sense, and they complicate the logic and the audit.

Procedure (the only available source of quantity is the account balance):

1. `python -m cli.balance --ccy <BASE>` → read `available` (e.g. `--ccy BTC`).
2. `python -m cli.market_snapshot --pairs <pair>` → read `bid`.
3. `amount_usdt = available × bid`.
4. `python -m cli.signal --pair <pair> --side sell --amount-usdt <result> --reason ... --tp-pct ... --sl-pct ...`

`POST /signal` accepts **only `amount_usdt`** (`SignalRequest` in
`src/okxbot/executor/service.py:30-39`) — there is no `amount_base` field. The executor
computes `amount_base = amount_usdt / entry` itself, where `entry` is **the current market
price at processing time** (bid for a sell): `_entry_price()` calls
`get_tickers([pair])` and takes `bid`
(`src/okxbot/executor/order_router.py:26-29, 46-48`). This is **not** the average entry
price of the position.

Consequence: `amount_usdt = quantity × current_price` theoretically yields exactly
100% of the position — a deviation equal to the P&L **does not occur**, because both sides
compute at the same, current price.

**But the quantity cannot currently be read by any CLI.** `PositionManager.open_positions()`
returns `base_amount` (`src/okxbot/executor/position_manager.py:40-86`), but the class
is **dead code** — it is not imported or instantiated anywhere, and the
`/state` endpoint returns only `balances`, `pending_orders` and `daily`
(`src/okxbot/executor/service.py:214-229`). `cli.positions` returns *pending
orders* from OKX, and a completed buy is no longer a pending order — so it will not
show the quantity held.

**Two caveats:**

- **Balance is not position.** `cli.balance --ccy BTC` returns the *entire* BTC balance in
  the account, not the amount acquired by the bot. If the demo account holds funds from another
  source, we would sell more than our position. On a clean demo account
  dedicated to the bot, the two notions coincide. During a manual emergency
  exit, `cli.balance` shows the total balance; if TP/SL has frozen the funds,
  OKX rejects the excess (fail-safe).
- **Price race.** If the bid drops between step 2 and placing the order,
  the computed `amount_base` will exceed the balance and OKX will reject the order. A safe
  failure — nothing is sold, the position stays open.

**Stage 2:** exact selling requires an `amount_base` field in the signal or wiring
`PositionManager` into `/state`. Both are changes in `src/` — to be reported, not
to be done on your own.

## TP/SL: where they are enforced

**Note — TP/SL are not in `config/risk_limits.yaml`** (that file has no such
fields). They are passed in the signal payload as `tp_pct`/`sl_pct` and converted
into prices in `src/okxbot/executor/order_router.py:34-44`.

There is no validation that would enforce their presence — **a signal without
`tp_pct`/`sl_pct` will pass and place an order without protection**
(`docs/runbook.md` §8.1).

**Hard agent rule:** every order must specify `tp_pct` and `sl_pct`.
`docs/risk_policy.md` calls an order without protection a "process error";
nothing blocks it technically, so the restraint is up to the agent.
`rules.evaluate` always returns TP/SL — also on the `no_data` path.

## Limits (enforced by the executor — the agent cannot override them)

They come from `config/risk_limits.yaml` and are validated on the executor side in
`src/okxbot/executor/risk_engine.py`. Limit fields are not accepted in
`POST /signal` (`docs/risk_policy.md:5-6`).

| Limit | Value |
|---|---|
| `max_order_usdt` | 50 |
| `min_order_usdt` | 5 |
| `require_confirmation_above_usdt` | 100 |
| `max_open_positions` | 3 |
| `max_position_pct` | 5 |
| `max_daily_loss_pct` | 3 |
| `max_signals_per_day` | 40 |
| `cooldown_seconds_between_signals` | 10 |

Per pair: SOL-USDT `max_order_usdt: 25`; BTC and ETH 50 each.

**`max_open_positions` does NOT work for market orders** — and the bot places only
market orders. The counter returns 0 because it filters on open states, and a market order is
immediately `filled`. **Until this is fixed, the agent controls the number of positions manually**
via `cli.balance --ccy <BASE>`. Details: `docs/runbook.md` §8.5.

**At 300 USDT the binding limit is `max_position_pct` (5% = 15 USDT), not
`max_order_usdt` (50 USDT).** `max_daily_loss_pct: 3%` (9 USDT) practically never
activates at ~0.23 USDT of risk per trade — the limits are consistent in scale for
an account larger than 300 USDT.

## Stop

- Rule from `docs/risk_policy.md:46-47`: on a **3% daily loss** or **2 consecutive
  losing orders** — stop trading and analyse `logs/executor.log`.
- Emergency stop: `docker compose stop executor` (`docs/runbook.md:82`).
  Open TP/SL stay on the exchange.

## Limitations of this strategy

The rule is deliberately minimal — a starting point for collecting data on demo, not an
investment strategy.

- **The rule is enforced by the executor, but not automatically.** The gate in
  `executor/service.py` (`evaluate_entry`) evaluates every `buy` signal on fresh
  data and rejects it with `strategy_rejected` when `rules.evaluate` does not return
  `allow_entry=True`. The executor does not generate signals itself — the decision and the
  moment of submission are still chosen by the agent. Selling is not subject to the gate
  (emergency exit).
- **Backtest done — negative result.** The thresholds (35 / MA200 / 1.0× volume /
  3×ATR) have no edge: PF 0.38 net / 0.46 gross, WR 22.7%, OOS worse than
  IS. Details in "Strategy development plan → B1".
- **`confidence` is not a probability of profit.** It measures how many entry
  conditions are met — setup completeness, not the chance of profit.
- **No handling of partial fills.** `docs/runbook.md` does not describe such a
  case; the type `partially_filled` exists in `src/okxbot/storage/models.py:16`
  and counts toward the open positions limit (`src/okxbot/storage/db.py:216-230`),
  but there is no procedure.
- **No "cancel-all/flatten" procedure.** The executor has only `POST /signal`,
  `GET /health`, `GET /state`, `POST /cancel` (single order).

## Strategy development plan (after the audit)

Binding order: **quality fixes → backtest → decision → deployment**.
We do not loosen thresholds before a backtest — otherwise we optimise for noise.

### Stage A — quality fixes (before changing the strategy)

| # | Change | File | Status |
|---|---|---|---|
| A1 | Deterministic decision: the executor evaluates the entry itself (`allow_entry`), does not trust the reason in the signal | `executor/service.py` | done 2026-11-01 |
| A2 | Rejecting the unclosed candle; indicators only from closed bars | `data/market.py` | done 2026-11-01 |
| A3 | ADX(14) as a regime filter (`ADX < 25`) | `data/indicators.py`, `strategy/rules.py` | done 2026-11-01 |
| A4 | Shared exposure limit for the crypto basket (BTC+ETH+SOL) | `executor/risk_engine.py` | **blocked — requires a decision** |

A4 requires two things that do not exist: (1) a policy threshold, and `config/risk_limits.yaml`
is a closed file without separate consent — values must not be written into code;
(2) a source of exposure. The `orders` table will not provide it (see `docs/runbook.md`
§8.5: no `UPDATE orders`, the position counter is always 0), so a limit built on
it would be either dead or monotonic. The real source is exchange balances
(`GET /account/balance` for BTC/ETH/SOL × price), but that in itself counts the entire
account balance, not the bot's positions. **To decide: a threshold in `risk_limits.yaml` +
exposure from balances, or reconciliation first (runbook §10 item 3).**

### Stage B — backtest (before aggression)

**B1 — result (structural diagnosis, n=44):** the RSI≤35 + ADX<25 baseline has
negative expectancy gross and net, IS and OOS, all pairs apart from the ETH noise
(n=14). Costs are not the killer (PF 0.46→0.38). The exit structure is broken (34/44 SL,
WR 22.7%, zero trades at break-even). RSI≤40 does not save it (PF 0.62).
**Direction closed.** Infrastructure is ready for a new rule. ETH PF 1.03 at
n=14 — a hypothesis to check, not a result.

Measurement details: `scripts/backtest.py --full`, window 2022-07 → 2026-11,
1 entry per episode (setups within 48 h treated as one move), entry
at the open of the candle after the signal, TP and SL in the same candle → SL
(0/44 cases, the result is robust to this assumption).

- **B1.** ~~Backtest of the current rule on 4H~~ — **done, direction closed.**
- **B2.** Variants on the same data and costs: risk-based sizing (1% of
  capital at risk), partial profit (50% at R:R 2:1, the rest trailing
  1.5×ATR), multi-timeframe (4H trend / 1H entry), RSI ≤ 40, ADX < 20.
  **Cancelled for this rule** — there is nothing to optimise when the baseline loses
  gross. A new rule = a new B1 → B2 cycle.
- **B3.** Comparative report: variant, number of trades, win rate, profit
  factor, max DD, annual net return. — **not applicable**; see B2.

The backtest is **external** (`scripts/backtest.py`) — it does not touch
`src/okxbot/`. The result is meant to show whether it is worth it, not to prove that it is.

### Stage C — deployment of the chosen variant

After the report we choose **one** variant and deploy it separately (a separate commit with
a description: which variant and why). Without a report there is no deployment. **For the current
rule this stage does not happen** — B1 closed the direction.

### What not to do

- **Live only after a backtest with positive net expectancy after costs.**
- **Do not change `config/risk_limits.yaml` without separate consent.**
- **Do not implement the Stage B variants in `src/`** before the report.
- **Do not loosen thresholds "by feel".**
