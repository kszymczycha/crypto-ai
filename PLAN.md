# PLAN.md — project plan

**Last updated:** 2026-11-12
**Status:** DEMO ONLY. Live is blocked until all P0 items are closed.

## Context

The infrastructure is mature: separated secrets, a deterministic executor,
a strategy gate, unit tests and audited documentation. The problem is no longer
"can an order be placed", but "do the risk limits and position protections actually
work" and "does the strategy have an edge".

Our own backtest B1 produced a negative result (PF 0.38 net, WR 22.7%, OOS worse than IS).
That means there is **no basis for live** regardless of infrastructure quality.
First we close P0, then P1, and we change the strategy only in P2.

## Baseline state

| Area | State | Live blocker? |
|---|---|---|
| Secrets and network isolation | implemented | no |
| Deterministic strategy gate | implemented | no |
| Portfolio drawdown circuit breaker (HWM) | implemented in code, tests exist | no |
| TP/SL for market orders | **not verified on demo** | **YES** |
| `max_open_positions` | not enforced for market | **YES** |
| `max_daily_loss_pct` | does not see TP/SL fills from OKX | **YES** |
| Reconciliation with OKX | missing | **YES** |
| Idempotency | a rejected signal blocks a retry | **YES** |
| Authorization for `POST /signal` | missing | **YES** |
| Selling | no exact position size | **YES** |
| Strategy | negative expectancy (B1) | **YES** |

## Plan rules

1. **Live only after P0.** Not "almost after P0", not "with a small amount".
2. **We do not loosen limits** to let something through. A limit that gets in the way
   is a matter for a decision, not for a workaround.
3. **One change = one commit.** Security rule changes separate from features.
4. **The document must describe the code, not intentions.** Drift is a bug to fix.
5. **No verification = no proof.** If a test was not run, that must be
   stated explicitly.
6. **We do not optimize the strategy before fixing the measurement.** Data and costs
   first, variants afterwards.

---

## P0 — live blockers

### P0.1 — Confirm TP/SL on demo

**Problem:** `src/okxbot/client/okx_client.py` sends `tpTriggerPx`/`slTriggerPx`
as top-level fields for `ordType=market`. In OKX v5, attached TP/SL may
require `attachAlgoOrds`. If the hypothesis is confirmed, positions are unprotected.

**Task:**

- Place a test market order on demo with TP/SL.
- Check in the OKX panel whether algo TP/SL orders are visible next to the market order.
- If not: switch to `attachAlgoOrds` and add payload tests.
- Update `docs/runbook.md` §11.5 and `docs/strategy.md`.

**Acceptance criterion:** market `filled` + visible algo TP/SL; a unit test
checks the correct payload shape; the runbook describes the result.

### P0.2 — Reconciliation with OKX and real position/P&L limits

**Problem:** `orders` is never updated after placement. `count_open_positions()`
counts `live/partially_filled` states, while a market order is immediately `filled`, so the
`max_open_positions` limit always returns 0. `realized_pnl_usdt` is only written when
the bot places a sell; TP/SL fills on the OKX side do not reach
`daily_stats`, so `max_daily_loss_pct` is sometimes blind.

**Task:**

- Read `GET /account/balance`, `/account/positions`, `/account/fills`.
- Update `orders` (`UPDATE`) based on fills.
- Compute the **net position per pair**: buys minus sells, realized only.
- Recompute `realized_pnl_usdt` from fills, including fees.
- Wire the correct counter into `RiskEngine`.
- Add tests: buy → sell → TP/SL → restart → limits still correct.

**Acceptance criterion:** after a series of buy/sell/TP/SL, `count_open_positions()` and
`realized_pnl_usdt` match OKX; the limit of 3 positions actually blocks the fourth;
the daily loss limit reacts to a loss from TP/SL.

### P0.3 — Fix idempotency

**Problem:** `_reject()` and `needs_confirmation` store the signal with a unique
`idempotency_key`. After fixing an error, a retry comes back as `duplicate`
with `ok:true` and **does not place the order**. `idempotency_window_seconds` from the config is not
used. There is also a check-then-insert race.

**Task:**

- Block a retry only for signals that were actually accepted/executed.
- Decide: a 300 s window or a permanent key — and implement one semantics.
- On a concurrent conflict, return a controlled error (e.g. 409), not 500.
- Add tests: rejected → fix → placed; needs_confirmation → placed;
  two concurrent requests with the same key.

**Acceptance criterion:** a rejected signal does not "eat" the key; a duplicate applies
only to an actually accepted order; no unhandled `IntegrityError`.

### P0.4 — Authorization, selling and mandatory TP/SL

**Problem:** `POST /signal` has no authentication. `opencode` permissions are
in `opencode.jsonc`, but the HTTP itself has no authorization. Selling is not subject to
the strategy gate. `require_confirmation_above_usdt: 100` with
`max_order_usdt: 50` is unreachable. A signal without `tp_pct`/`sl_pct` passes
validation.

**Task:**

- Add a token/HMAC between `opencode` and `executor` (key from secrets, not from the repo).
- Selling: reduce-only / up to the real position balance only, not "blind".
- Keep `opencode.jsonc` as the single source of permissions; do not add flags
  that bypass permissions.
- Add `require_tp_sl: true` to `config/risk_limits.yaml` and to validation.
- Set the confirmation threshold below `max_order_usdt` or remove the dead branch.
- Update `AGENTS.md`, `opencode.jsonc`, `docs/risk_policy.md`,
  `docs/strategy.md`.

**Acceptance criterion:** unauthorized request → 401; a sell does not exceed
the amount held; a signal without TP/SL → rejected; the `needs_confirmation` branch
is reachable and tested.

### P0.5 — Clean up documentation and dead code

**Problem:** `README.md` claims there is no backtest and that the executor does not execute
rules — both are untrue. `docs/architecture.md` describes `executor/models.py`,
`orders.place()`, and the `positions` and `daily_pnl` tables, which do not exist. The collector writes
snapshots that nobody reads in the decision path. `orders.py` and
`position_manager.py` are dead code. The `Makefile` masks linter errors (`|| true`)
and compose errors (`|| echo`).

**Task:**

- Update `README.md` and `docs/architecture.md` to reflect the actual state.
- Decide: the collector is needed (wire it into decisions/backtest) or remove it.
- Remove or use `orders.py` and `position_manager.py`.
- Fix `make lint` and `make check-config` so that an error is not a success.

**Acceptance criterion:** documentation matches the code; no dead modules
in `src/`; `make lint` returns an error on error; `make validate` passes.

---

## P1 — quality and safety

### P1.1 — Exact sell size

Add `amount_base` to the signal or wire `PositionManager` into `/state` so that
"sell 100% of the position" does not rely on `amount_usdt / bid`. Criterion:
sell exactly the amount held, also on a shared account.

### P1.2 — Normalize file permissions

Most existing files are tracked as `100755` (including `README.md`,
configs, tests). Set `755` only for `.sh` scripts and entrypoints, the rest to
`644`.
Separate commit, no content changes.

### P1.3 — CI and quality gates

Add CI running: `scripts/validate_static.py`, `pytest`, `ruff` (without
`|| true`), eventually `mypy` and a coverage threshold. Criterion: a PR without green CI is
not merged.

### P1.4 — Tests for known gaps

Add tests that reproduce production, not a synthetic state:

- a `filled` market order must not yield `count_open_positions() == 0`,
- TP/SL filled on the OKX side must reach the daily P&L,
- rejected/needs_confirmation must not block a retry,
- a signal without TP/SL must be rejected.

### P1.5 — Deeper healthcheck

`/health` currently only checks whether the process is alive. Add checks for access to
SQLite and (optionally) OKX, so that the healthcheck is not green when a dependency
fails.

---

## P2 — strategy

### P2.1 — New hypothesis after B1

B1 (RSI ≤ 35 + ADX < 25 + regime + volume) has negative expectancy gross and net.
This direction is closed. A new rule = a new cycle:

1. B1 on the new hypothesis (costs, IS/OOS, TP=SL→SL).
2. B2 — variants (risk-based sizing, partial profit, multi-timeframe).
3. Comparison report.
4. Decision: deploy or reject.

**Live criterion:** positive net expectancy after costs, stable in OOS and
walk-forward. Without that, live stays off.

### P2.2 — Realistic measurement

Account for fees, slippage, spread, entry latency and limited
liquidity. Do not report results from demo (`flag="1"`) — demo history is
distorted (runbook §12).

### P2.3 — Monitoring and alerts

Add alerts: drawdown, loss of connection to OKX, limit rejections, state drift
versus the exchange. An alert does not replace reconciliation — it only shortens the reaction time.

---

## Order of execution

1. **P0.1** — TP/SL on demo. Without it the rest is pointless.
2. **P0.2** — reconciliation and real limits.
3. **P0.3** — idempotency.
4. **P0.4** — authorization, selling, mandatory TP/SL.
5. **P0.5** — documentation and dead code.
6. **P1** — quality, CI, exact selling, permissions.
7. **P2** — new strategy and only then the decision about live.

## Definition of Done

- `python3 scripts/validate_static.py` — OK.
- `make test` / `make test-docker` — OK.
- `make lint` — OK, without masking errors.
- Documentation updated and consistent with the code.
- No secrets or runtime files in the diff.
- Result verified on demo; any lack of verification stated explicitly.
- The commit has a narrow scope and a clear description.

## Non-goals

- Live before P0 is closed.
- Loosening limits "for testing".
- Optimizing the strategy on demo data.
- Automatic trading without human confirmation.
- Adding MCP, packages and new dependencies without approval.
- Hiding gaps in the documentation.

## Sources

- `AGENTS.md` — agent working rules (loaded automatically by OpenCode).
- `opencode.jsonc` — OpenCode model, provider and permissions.
- `docs/strategy.md` — strategy and B1 results.
- `docs/risk_policy.md` — limits and order of checks.
- `docs/runbook.md` §8–§12 — known risks, audit and backtests.
- `docs/architecture.md` — architecture (to be updated in P0.5).
