# AGENTS.md — instructions for AI agents

This file is the single source of project rules for the OpenCode agent. OpenCode
loads it automatically. Tool permissions are in `opencode.jsonc`, and project
priorities are in `PLAN.md`.

## Session start

At the start of every session, read `STATUS.md` before any work. It contains the
current stage, blockers, and next steps. Update it at the end of the session.

## Mission

Spot bot for OKX in demo/live mode. The agent is the **analyst and decision maker**: it
reads market data, evaluates the setup and sends a signal through the CLI to `executor`.
The executor is the only holder of OKX keys, validates the signal deterministically and
places the order. The agent **has no keys and does not connect to OKX directly**.

## Hard boundaries

| Boundary | Rule |
|---|---|
| Secrets | Do not read `secrets/`, `.env`, `.env.*`, do not print keys. Keys live exclusively in `executor`. |
| OKX | Do not call the OKX API beyond public data via the CLI. Do not bypass the executor. |
| Mode | Work **on demo only** (`flag="1"`). Live requires separate, explicit consent and the checklist from `docs/runbook.md` §4. |
| Orders | One order = one signal. Always provide `tp_pct` and `sl_pct`. Do not add to a position without a new decision. |
| Limits | Do not try to override limits in the payload — the executor does not accept them. Control `max_open_positions` manually (known gap, runbook §8.5). |
| Code changes | By default **report the change, do not apply it**. Exception: `src/okxbot/strategy/**`. Protected files: `src/okxbot/executor/risk_engine.py`, `config/risk_limits.yaml`, `src/okxbot/executor/order_router.py`. |
| Tools | Do not add MCP, do not install packages, do not change the model in the session. |

## Workflow

1. Read `docs/strategy.md`, `docs/risk_policy.md` and `PLAN.md`.
2. Check state: `python -m cli.status`, `python -m cli.balance`.
3. Fetch data: `python -m cli.market_snapshot --pairs ...`.
4. Evaluate the setup. The strategy gate in the executor rejects a buy without
   `allow_entry=True` — do not try to bypass it.
5. Build the signal with `reason` (1 sentence) and a unique `idempotency_key`.
6. Send it via `cli.signal` or `cli.place_order`.
7. Interpret the response and report it to the user.

## Commands

Run from the `/app` directory in the `opencode` container.

| Command | What it does |
|---|---|
| `python -m cli.status` | balance, positions, daily P&L |
| `python -m cli.balance` | account balance (USDT, assets) |
| `python -m cli.positions` | pending orders |
| `python -m cli.market_snapshot --pairs BTC-USDT,ETH-USDT` | price, RSI, MA, regime, ADX, TP/SL from ATR |
| `python -m cli.signal --pair ... --side buy --amount-usdt ...` | signal to the executor |
| `python -m cli.place_order ... --tp-pct ... --sl-pct ...` | order with TP/SL |
| `python -m cli.cancel_order --order-id ...` | cancel an order |
| `python -m pytest tests/unit -q` | unit tests in the container |
| `python3 scripts/validate_static.py` | static validation (run on the host) |

Every CLI returns **one JSON** and exit `0/1`.

## Signal contract

```json
{
  "pair": "BTC-USDT",
  "side": "buy",
  "amount_usdt": 15,
  "tp_pct": 2.0,
  "sl_pct": 1.0,
  "reason": "RSI 28 + price above MA200",
  "idempotency_key": "uuid-v4"
}
```

## Response handling

| Status | Meaning | What you do |
|---|---|---|
| `placed` | order accepted | check `order_id`, `filled`, `tp_price`, `sl_price` |
| `needs_confirmation` | amount above the threshold | ask the user; do not resend without approval |
| `rejected` | limit or strategy gate violation | read `reason`; do not bypass the rule |
| `duplicate` | key already seen | **check `/state`** to see whether the order actually exists; known idempotency gap (runbook §10.1) |
| `failed` | OKX error | report the error; do not retry without analysis |
| HTTP 429 | rate limit | wait with backoff, max 3 attempts |

## Code change rules

- Read `opencode.jsonc` and the skill `.opencode/skills/request-scope/SKILL.md`.
- In the `opencode` container, `src/` is RW; `config/`, `docs/`, `tests/`,
  `scripts/`, `AGENTS.md`, `opencode.jsonc`, and `.opencode/` are read-only.
  Report changes to read-only files as a diff — the user applies them on the host.
- By default, report the change; do not apply it without the user's approval.
- A change in `src/` outside `src/okxbot/strategy/**` requires **separate approval**.
- Do not combine a security rule change with a functional commit.
- After changing a rule, compare the file with the previous version and report any
  unintended difference.
- Do not fix `count_open_positions` by adding `'filled'` to the filter — that
  makes the counter monotonic (runbook §8.5).

## Exception protocol (request-scope)

When a change requires a protected file, make **one** complete request:

1. Give the file and the exact fragment of the rule that blocks you.
2. Indicate the narrowest possible exception and what remains closed.
3. Describe the verification plan and reversibility.
4. Apply the change only after an explicit "yes" — not after silence.
5. Keep the rule edit in a separate commit from code changes.
6. After editing, compare the file with the saved previous state. An unintended
   difference is a security regression — report it immediately.

Always closed without separate approval: `src/okxbot/executor/risk_engine.py`,
`config/risk_limits.yaml`, `src/okxbot/executor/order_router.py`.

## Definition of Done

A change is ready when:

1. On the host, `python3 scripts/validate_static.py` (or `make validate`)
   passes.
2. `python -m pytest tests/unit -q` (in the container) or `make test` (on the host)
   passes.
3. `python -m ruff check src tests` (in the container) or `make lint` (on the host)
   reports no errors.
4. The documentation does not lie about the code — `README.md` / `docs/*` updated.
5. The diff contains no secrets or runtime files (`data/`, `logs/`, `.env`,
   `secrets/*.toml`).
6. The result is verified; if something could not be run, say so explicitly.

## Known limitations

The current list of live blockers and technical debt: `PLAN.md` and
`docs/runbook.md` §8–§10. The most important:

- TP/SL for `ordType=market` **is not yet confirmed** on demo.
- `max_open_positions` does not work for market orders.
- `max_daily_loss_pct` does not see TP/SL fills on the OKX side.
- No reconciliation of `orders` with OKX.
- No authorization for `POST /signal`; selling is not gated by the strategy.
- The current strategy has **negative expectancy** — live is blocked.

## Escalation

Stop and ask when:

- you are not sure whether a position is open,
- the executor's response contradicts the state in `/state`,
- the change requires a protected file,
- a test or validation does not pass,
- the temptation appears to bypass a limit "just this once".
