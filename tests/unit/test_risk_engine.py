"""Risk engine tests — each limit separately, with no network calls."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from okxbot.config import RiskLimits, Symbol
from okxbot.executor.risk_engine import RiskEngine, Signal
from okxbot.storage.db import StateStore

SYMBOLS = {
    "BTC-USDT": Symbol("BTC-USDT", "BTC", "USDT", "bitcoin", 150.0),
    "ETH-USDT": Symbol("ETH-USDT", "ETH", "USDT", "ethereum", 150.0),
}

# Max open positions set high in the limit tests so that the position limit
# (growing with every order in state.db) does not mask the condition under test.
NO_POSITION_LIMIT = 99


def make_limits(**overrides) -> RiskLimits:
    base = {
        "max_position_pct": 100.0,  # disabled in tests unless a test sets it
        "max_daily_loss_pct": 3.0,
        "max_portfolio_drawdown_pct": 15.0,
        "max_open_positions": NO_POSITION_LIMIT,
        "max_order_usdt": 1000.0,
        "min_order_usdt": 5.0,
        "require_confirmation_above_usdt": 1000.0,
        "allowed_sides": ("buy", "sell"),
        "allowed_order_types": ("market", "limit"),
        "cooldown_seconds_between_signals": 10,
        "max_signals_per_day": 999,
    }
    base.update(overrides)
    return RiskLimits(**base)


def make_signal(**overrides) -> Signal:
    base = {
        "pair": "BTC-USDT",
        "side": "buy",
        "amount_usdt": 20.0,
        "reason": "RSI 28 and price above MA20",
        "idempotency_key": "test-key-0001",
    }
    base.update(overrides)
    return Signal(**base)


def engine(store: StateStore, equity: float = 1000.0, **limits) -> RiskEngine:
    return RiskEngine(make_limits(**limits), store, symbols=SYMBOLS, equity_usdt=equity)


def test_allowed_signal(store: StateStore) -> None:
    verdict = engine(store).validate(make_signal())
    assert verdict.status == "allowed"
    assert verdict.allowed is True


@pytest.mark.parametrize("side", ["short", "BUY", ""])
def test_rejects_unknown_side(store: StateStore, side: str) -> None:
    verdict = engine(store).validate(make_signal(side=side))
    assert verdict.status == "rejected"
    assert "side" in verdict.reason


def test_rejects_unknown_pair(store: StateStore) -> None:
    verdict = engine(store).validate(make_signal(pair="DOGE-USDT"))
    assert verdict.status == "rejected"
    assert "DOGE-USDT" in verdict.reason


def test_rejects_short_reason(store: StateStore) -> None:
    verdict = engine(store).validate(make_signal(reason="ok"))
    assert verdict.status == "rejected"


def test_rejects_missing_idempotency(store: StateStore) -> None:
    verdict = engine(store).validate(make_signal(idempotency_key=""))
    assert verdict.status == "rejected"


def test_rejects_below_minimum(store: StateStore) -> None:
    verdict = engine(store).validate(make_signal(amount_usdt=1.0))
    assert verdict.status == "rejected"
    assert "min_order_usdt" in verdict.reason


def test_rejects_above_max(store: StateStore) -> None:
    verdict = engine(store, max_order_usdt=50.0).validate(make_signal(amount_usdt=80.0))
    assert verdict.status == "rejected"
    assert "max_order_usdt" in verdict.reason


def test_rejects_above_symbol_limit(store: StateStore) -> None:
    verdict = engine(store, equity=10_000.0, max_order_usdt=1000.0).validate(make_signal(amount_usdt=400.0))
    assert verdict.status == "rejected"
    assert "pair limit" in verdict.reason


def test_rejects_above_position_pct(store: StateStore) -> None:
    # 5% of 200 USDT = 10 USDT, so 20 USDT exceeds the position limit.
    verdict = engine(store, equity=200.0, max_position_pct=5.0).validate(make_signal(amount_usdt=20.0))
    assert verdict.status == "rejected"
    assert "equity" in verdict.reason


def test_rejects_when_max_open_positions(store: StateStore) -> None:
    for index in range(3):
        store.insert_order({"pair": "BTC-USDT", "side": "buy", "state": "live", "order_id": f"o{index}"})
    verdict = engine(store, max_open_positions=3).validate(make_signal())
    assert verdict.status == "rejected"
    assert "max_open_positions" in verdict.reason


def test_rejects_after_daily_signal_limit(store: StateStore) -> None:
    for _ in range(2):
        store.increment_signals()
    verdict = engine(store, max_signals_per_day=2).validate(make_signal())
    assert verdict.status == "rejected"
    assert "max_signals_per_day" in verdict.reason


def test_rejects_after_daily_loss_limit(store: StateStore) -> None:
    store.set_equity_start(1000.0)
    store.add_realized_pnl(-40.0)  # 4% loss > 3%
    verdict = engine(store, equity=960.0, max_daily_loss_pct=3.0).validate(make_signal())
    assert verdict.status == "rejected"
    assert "daily loss limit" in verdict.reason


def test_cooldown_blocks_second_signal(store: StateStore) -> None:
    risk = engine(store)
    risk.mark_signal("BTC-USDT")
    verdict = risk.validate(make_signal(idempotency_key="test-key-0002"))
    assert verdict.status == "rejected"
    assert "cooldown" in verdict.reason


def test_cooldown_expires(store: StateStore) -> None:
    risk = engine(store)
    past = (datetime.now(UTC) - timedelta(seconds=60)).isoformat()
    store.set_kv("last_signal_at:BTC-USDT", past)
    verdict = risk.validate(make_signal())
    assert verdict.status == "allowed"


def test_needs_confirmation_above_threshold(store: StateStore) -> None:
    # The amount 120 USDT fits max_order_usdt=200, the pair limit (150) and 5%
    # of the 3000 equity — but it exceeds the confirmation threshold of 100.
    verdict = engine(
        store,
        equity=3000.0,
        max_order_usdt=200.0,
        require_confirmation_above_usdt=100.0,
    ).validate(make_signal(amount_usdt=120.0))
    assert verdict.status == "needs_confirmation"
    assert "confirmation threshold" in verdict.reason


def test_daily_summary_shape(store: StateStore) -> None:
    summary = engine(store, max_open_positions=3).daily_summary()
    assert summary["max_open_positions"] == 3
    assert summary["realized_pnl_usdt"] == 0.0
    assert summary["day"]


# ------------------------------------------------- portfolio-level drawdown stop


def test_drawdown_within_limit_allows_buy(store: StateStore) -> None:
    """10% drawdown against a 15% limit — the buy passes."""
    risk = engine(store, equity=1000.0)
    risk.equity_usdt = 900.0  # HWM 1000, drawdown 10%
    verdict = risk.validate(make_signal())
    assert verdict.status == "allowed"
    assert "portfolio_drawdown" in verdict.checks


def test_drawdown_beyond_limit_rejects_buy(store: StateStore) -> None:
    """20% drawdown against a 15% limit — the buy is rejected by this rule."""
    risk = engine(store, equity=1000.0)
    risk.equity_usdt = 800.0
    verdict = risk.validate(make_signal())
    assert verdict.status == "rejected"
    assert verdict.reason.startswith("portfolio_drawdown_limit")
    assert "portfolio_drawdown" in verdict.checks


def test_drawdown_at_exact_limit_rejects(store: StateStore) -> None:
    """The boundary is closed: 15.0% against a 15% limit is already a breach."""
    risk = engine(store, equity=1000.0)
    risk.equity_usdt = 850.0
    assert risk.validate(make_signal()).status == "rejected"


def test_drawdown_limit_does_not_block_sell(store: StateStore) -> None:
    """A drawdown must not trap the user in a position: the sell passes."""
    risk = engine(store, equity=1000.0)
    risk.equity_usdt = 500.0
    verdict = risk.validate(make_signal(side="sell"))
    assert verdict.status == "allowed"


def test_unknown_equity_blocks_buy(store: StateStore) -> None:
    """No equity = no measurement = no buy (fail-closed)."""
    verdict = RiskEngine(make_limits(), store, symbols=SYMBOLS, equity_usdt=0.0).validate(
        make_signal()
    )
    assert verdict.status == "rejected"
    assert verdict.reason.startswith("portfolio_drawdown_limit")
    assert "equity unknown" in verdict.reason


def test_unknown_equity_does_not_block_sell(store: StateStore) -> None:
    verdict = RiskEngine(make_limits(), store, symbols=SYMBOLS, equity_usdt=0.0).validate(
        make_signal(side="sell")
    )
    assert verdict.status == "allowed"


def test_hwm_moves_only_up(store: StateStore) -> None:
    risk = engine(store, equity=1000.0)
    risk.equity_usdt = 800.0
    assert risk.portfolio_hwm() == 1000.0
    risk.equity_usdt = 1200.0
    assert risk.portfolio_hwm() == 1200.0
    risk.equity_usdt = 1100.0
    assert risk.portfolio_hwm() == 1200.0


def test_hwm_survives_restart(store: StateStore) -> None:
    """HWM in `kv`, not in memory: a restart after a drawdown does not reset the protection."""
    engine(store, equity=1000.0)
    restarted = engine(store, equity=820.0)
    assert restarted.portfolio_hwm() == 1000.0
    assert restarted.portfolio_drawdown_pct() == pytest.approx(18.0)
    assert restarted.validate(make_signal()).status == "rejected"


def test_unknown_equity_does_not_overwrite_hwm(store: StateStore) -> None:
    """A balance of 0 from the API must not look like equity fallen to zero."""
    risk = engine(store, equity=1000.0)
    risk.equity_usdt = 0.0
    assert risk.portfolio_hwm() == 1000.0
    assert risk.portfolio_drawdown_pct() is None


def test_drawdown_grows_after_new_high(store: StateStore) -> None:
    """HWM is the peak, not the day's start: a new peak raises the bar."""
    risk = engine(store, equity=1000.0)
    risk.equity_usdt = 1400.0
    risk.equity_usdt = 1200.0
    assert risk.portfolio_drawdown_pct() == pytest.approx(100 * 200 / 1400)


def test_daily_summary_reports_drawdown(store: StateStore) -> None:
    summary = engine(store, equity=1000.0).daily_summary()
    assert summary["max_portfolio_drawdown_pct"] == 15.0
    assert summary["portfolio_hwm_usdt"] == 1000.0
    assert summary["portfolio_drawdown_pct"] == 0.0
    assert summary["equity_usdt"] == 1000.0
