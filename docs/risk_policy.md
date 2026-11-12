# Risk policy

Threshold values come **exclusively** from `config/risk_limits.yaml` and are
enforced by `src/okxbot/executor/risk_engine.py` on the executor side.
The model (OpenCode) cannot override them in the signal payload — limit fields are not
accepted in `POST /signal`.

## Limits

| Limit | Value | Meaning |
|---|---|---|
| `max_position_pct` | 5 | max % of capital (USDT balance) per order |
| `max_daily_loss_pct` | 3 | once exceeded: new signals blocked until the end of the UTC day |
| `max_open_positions` | 3 | max number of open buy orders |
| `max_order_usdt` | 50 | absolute ceiling on the order amount |
| `min_order_usdt` | 5 | minimum (fee viability); OKX has its own pair minimums |
| `require_confirmation_above_usdt` | 100 | above this: `needs_confirmation`, without execution |
| `cooldown_seconds_between_signals` | 10 | interval between signals for the same pair |
| `max_signals_per_day` | 40 | hard daily counter of accepted signals |

Per-pair limits in `config/symbols.yaml` (`max_order_usdt` for SOL-USDT = 25) cannot
exceed the global limit — this is guarded by the test `test_symbols_limits_not_above_global`.

## Order of checks in `validate()`

1. `side` allowed (buy/sell),
2. `pair` supported,
3. `reason` present (5–280 characters),
4. `idempotency_key` present + no duplicate within a 300 s window,
5. amount: `min_order_usdt` ≤ amount ≤ `max_order_usdt` and ≤ pair limit,
6. amount ≤ `max_position_pct` of capital,
7. number of open positions < `max_open_positions`,
8. daily signal counter < `max_signals_per_day`,
9. daily P&L > −`max_daily_loss_pct` of the day's starting capital,
10. pair cooldown,
11. amount > `require_confirmation_above_usdt` → `needs_confirmation`.

Verdict: `allowed` | `needs_confirmation` | `rejected` (+ a readable `reason`).

## Non-negotiable rules

- **One order = one signal.** No scaling, no "topping up" without a new signal.
- **Always TP and SL** (default 2% / 1%) — an order without protection is a process error.
- **Capital 300–500 USD**: a typical position is 20–50 USDT, i.e. 4–17% of capital.
  The 5% limit means that at 400 USDT a single order is at most 20 USDT.
- **Bot stop**: on a 3% daily loss or 2 consecutive losing orders,
  stop trading and analyse the logs (`logs/executor.log`).
- **Changing limits** = restarting the executor (config read at startup).

## Starting capital of the day

`equity_start_usdt` is recorded on the first successful balance read on a given
UTC day and does not change afterwards. This way the loss limit is calculated from the capital
at the start of the day, not from the current (changing) balance.

## What the policy does NOT cover

- Slippage and pair liquidity (we assume high-turnover pairs: BTC/ETH/SOL-USDT).
- Taxes and accounting costs.
- Protection against a "flash crash" — that is the role of SL, not the bot.
