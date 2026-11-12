"""Signal building tests: from snapshot evaluation to the `POST /signal` payload.

The module had no coverage until now — which is why the unreachable
`consider_sell` branch survived the removal of the sell rule from the strategy.
"""

from __future__ import annotations

import pytest

from okxbot.config import RiskLimits
from okxbot.strategy.signals import build_signal, default_amount_usdt

LIMITS = RiskLimits(
    max_position_pct=5.0,
    max_daily_loss_pct=3.0,
    max_portfolio_drawdown_pct=15.0,
    max_open_positions=3,
    max_order_usdt=50.0,
    min_order_usdt=5.0,
    require_confirmation_above_usdt=100.0,
    allowed_sides=("buy", "sell"),
    allowed_order_types=("market", "limit"),
    cooldown_seconds_between_signals=10,
    max_signals_per_day=40,
)


def buy_snapshot() -> dict:
    """Snapshot meeting all entry conditions."""
    return {
        "rsi": 25.0,
        "regime": "up",
        "vol_ratio": 1.5,
        "atr_pct": 1.0,
        "candles_count": 250,
        "price": 100.0,
        "adx": 18.0,
    }


# ------------------------------------------------------------------ build_signal


def test_build_signal_produces_buy_payload() -> None:
    payload = build_signal("BTC-USDT", buy_snapshot(), amount_usdt=15.0)
    assert payload["pair"] == "BTC-USDT"
    assert payload["side"] == "buy"
    assert payload["amount_usdt"] == 15.0
    assert payload["reason"]
    assert payload["idempotency_key"]


def test_tp_sl_come_from_atr_not_from_config() -> None:
    """TP/SL must come from the evaluation (ATR), not from config constants.

    Regression: `build_signal` took `cfg.default_tp_pct` (2.0), ignoring ATR.
    With `atr_pct=1.0` the correct TP is 3.0% (3× ATR), not 2.0%.
    """
    payload = build_signal("BTC-USDT", buy_snapshot(), amount_usdt=15.0)
    assert payload["tp_pct"] == 3.0
    assert payload["sl_pct"] == 1.5


def test_build_signal_rejects_unknown_hint() -> None:
    """No buy hint → ValueError, not a blind signal."""
    flat = {"rsi": 50.0, "regime": "up", "vol_ratio": 1.5, "candles_count": 250}
    with pytest.raises(ValueError, match="no buy hint"):
        build_signal("BTC-USDT", flat, amount_usdt=15.0)


def test_build_signal_rejects_downtrend() -> None:
    snap = dict(buy_snapshot(), regime="down")
    with pytest.raises(ValueError):
        build_signal("BTC-USDT", snap, amount_usdt=15.0)


def test_build_signal_rejects_insufficient_candles() -> None:
    snap = dict(buy_snapshot(), candles_count=10)
    with pytest.raises(ValueError):
        build_signal("BTC-USDT", snap, amount_usdt=15.0)


def test_idempotency_key_is_preserved_when_given() -> None:
    payload = build_signal(
        "BTC-USDT", buy_snapshot(), amount_usdt=15.0, idempotency_key="moj-klucz"
    )
    assert payload["idempotency_key"] == "moj-klucz"


def test_reason_fits_signal_schema_limit() -> None:
    """`reason` has a 280-character limit in the signal schema — the payload must fit."""
    payload = build_signal("BTC-USDT", buy_snapshot(), amount_usdt=15.0)
    assert len(payload["reason"]) <= 280


def test_payload_has_only_signal_schema_fields() -> None:
    """The payload must not carry limit fields — the executor does not accept them."""
    payload = build_signal("BTC-USDT", buy_snapshot(), amount_usdt=15.0)
    allowed = {"pair", "side", "amount_usdt", "tp_pct", "sl_pct", "reason", "idempotency_key"}
    assert set(payload) <= allowed


# ----------------------------------------------------------- default_amount_usdt


def test_default_amount_respects_position_pct_cap() -> None:
    """With 300 USDT, `max_position_pct` binds, not `max_order_usdt`."""
    assert default_amount_usdt(300.0, LIMITS, symbol_max=50.0) == 15.0


def test_default_amount_respects_symbol_max() -> None:
    """The pair limit (e.g. SOL 25) is lower than 5% of equity on a larger account."""
    assert default_amount_usdt(10_000.0, LIMITS, symbol_max=25.0) == 25.0


def test_default_amount_never_below_minimum() -> None:
    """With very little equity the amount does not drop below `min_order_usdt`."""
    assert default_amount_usdt(10.0, LIMITS, symbol_max=50.0) == LIMITS.min_order_usdt


def test_default_amount_handles_zero_equity() -> None:
    assert default_amount_usdt(0.0, LIMITS, symbol_max=50.0) == LIMITS.min_order_usdt
