"""Model-side signal building: from snapshot evaluation to a JSON payload.

This module does NOT decide about trading — it prepares a candidate signal
(amount, TP/SL, justification) that the user or prompt approves, and the limits
are verified by `risk_engine` in the executor.

The strategy is **long-only** (see `docs/strategy.md`): the position is closed by
TP/SL, not by a sell signal. Therefore this module never builds `side="sell"` —
selling is a manual emergency procedure performed via `cli.signal`.
"""

from __future__ import annotations

import uuid
from typing import Any

from okxbot.config import RiskLimits
from okxbot.strategy.rules import StrategyConfig, evaluate


def default_amount_usdt(equity_usdt: float, limits: RiskLimits, symbol_max: float) -> float:
    """Suggests a conservative amount: min(2% of capital, pair limit, global max)."""
    if equity_usdt <= 0:
        return limits.min_order_usdt
    by_equity = equity_usdt * limits.max_position_pct / 100.0
    proposed = min(by_equity, symbol_max, limits.max_order_usdt)
    return round(max(proposed, limits.min_order_usdt), 2)


def build_signal(
    pair: str,
    snapshot: dict[str, Any],
    *,
    amount_usdt: float,
    config: StrategyConfig | None = None,
    idempotency_key: str | None = None,
) -> dict[str, Any]:
    """Builds a buy signal payload (a dict matching the `POST /signal` schema).

    Raises `ValueError` when the evaluation does not give an unambiguous buy
    hint. There is no `side` parameter: the strategy is long-only, so the only
    valid direction is "buy". An earlier version accepted `side` and mapped a
    sell hint — a hint the strategy no longer produces, so that branch was
    unreachable.

    `amount_usdt` is required: the module does not know the balance or pair
    limits. Use `default_amount_usdt()` to suggest a conservative amount.
    """
    cfg = config or StrategyConfig()
    evaluation = evaluate(snapshot, cfg)
    hint = evaluation["hint"]

    if hint != "consider_buy":
        raise ValueError(f"no buy hint (hint={hint}) — not building a signal")

    reasons = "; ".join(str(reason) for reason in evaluation["reasons"])
    return {
        "pair": pair,
        "side": "buy",
        "amount_usdt": amount_usdt,
        # TP/SL come from the evaluation (ATR multiples), not from cfg — otherwise
        # the signal would carry fixed percentages and ignore market volatility.
        "tp_pct": evaluation["suggested_tp_pct"],
        "sl_pct": evaluation["suggested_sl_pct"],
        "reason": f"{hint} ({reasons})"[:280],
        "idempotency_key": idempotency_key or str(uuid.uuid4()),
    }
