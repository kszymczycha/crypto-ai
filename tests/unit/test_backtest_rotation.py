"""Portfolio backtest of the Regime Rotation hypothesis (`scripts/backtest.py`).

The simulator is the measuring instrument for B1, so the tests pin the things
that would silently flatter the result: shared entry bars, costs charged on both
legs, and no lookahead (a signal on bar N trades at bar N's close, never before).
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from typing import Any

import pytest

from okxbot.strategy.rules import REGIME_ROTATION_MA_PERIOD

_SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "backtest.py"


def _load_backtest() -> Any:
    spec = importlib.util.spec_from_file_location("backtest_script", _SCRIPT)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    # dataclasses ask `sys.modules[cls.__module__]` while defining the class;
    # without the registration below that lookup returns None and import fails.
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


backtest = _load_backtest()
PAIRS = ("BTC-USDT", "ETH-USDT", "SOL-USDT")
BAR_MS = backtest.BAR_MS


def series(prices: list[float], *, start: int = 1_600_000_000_000) -> list[dict[str, Any]]:
    """One 4H candle per price, close == open == high == low."""
    return [
        {
            "ts": str(start + index * BAR_MS),
            "open": price,
            "high": price,
            "low": price,
            "close": price,
            "vol": 1.0,
        }
        for index, price in enumerate(prices)
    ]


def basket(
    btc: list[float], eth: list[float] | None = None, sol: list[float] | None = None
) -> dict:
    return {
        "BTC-USDT": series(btc),
        "ETH-USDT": series(eth if eth is not None else btc),
        "SOL-USDT": series(sol if sol is not None else btc),
    }


def test_requires_btc_market_filter() -> None:
    with pytest.raises(ValueError):
        backtest.simulate_rotation({"ETH-USDT": series([100.0] * 20)})


def test_flat_regime_band_does_not_trade() -> None:
    """Price inside the dead band is unknown — the simulator must hold cash."""
    result = backtest.simulate_rotation(basket([100.0] * 120), dead_band=0.01)
    assert result["trades"] == []
    assert result["final_usdt"] == pytest.approx(backtest.PORTFOLIO_USDT)


def test_risk_on_then_off_opens_and_closes_three_legs() -> None:
    n = REGIME_ROTATION_MA_PERIOD + 20
    btc = [100.0] * n + [200.0] * 10 + [50.0] * 10
    result = backtest.simulate_rotation(basket(btc))
    assert {t["pair"] for t in result["trades"]} == set(PAIRS)
    assert len(result["trades"]) == 3
    assert all(t["exit_reason"] == "regime_off" for t in result["trades"])
    # Entered while rising, exited after the drop: the basket must lose money.
    assert result["final_usdt"] < backtest.PORTFOLIO_USDT


def test_costs_are_charged_on_entry_and_exit() -> None:
    """Each leg's P&L is exactly net of 0.15% on entry and 0.15% on exit."""
    n = REGIME_ROTATION_MA_PERIOD + 20
    btc = [100.0] * n + [200.0] * 10 + [100.0] * 10
    result = backtest.simulate_rotation(basket(btc))
    side = backtest.ROTATION_COST_PER_SIDE
    for trade in result["trades"]:
        qty = trade["invest_usdt"] / trade["entry_price"]
        expected = qty * trade["exit_price"] * (1 - side) - trade["invest_usdt"] * (1 + side)
        assert trade["pnl_usdt"] == pytest.approx(expected)


def test_costs_reduce_equity() -> None:
    n = REGIME_ROTATION_MA_PERIOD + 20
    btc = [100.0] * n + [200.0] * 10 + [150.0] * 10
    with_costs = backtest.simulate_rotation(basket(btc))
    without_costs = backtest.simulate_rotation(basket(btc), cost_per_side=0.0)
    assert with_costs["final_usdt"] < without_costs["final_usdt"]


def test_basket_legs_get_equal_notional() -> None:
    """1/3 per pair: fees are taken from the share, not off the last leg."""
    n = REGIME_ROTATION_MA_PERIOD + 20
    btc = [100.0] * n + [200.0] * 10 + [150.0] * 10
    result = backtest.simulate_rotation(basket(btc))
    notionals = {round(t["invest_usdt"], 6) for t in result["trades"]}
    assert len(notionals) == 1
    # The 1/3 share covers notional plus its own fee.
    share = backtest.PORTFOLIO_USDT / 3
    assert notionals.pop() == pytest.approx(share / (1 + backtest.ROTATION_COST_PER_SIDE), rel=1e-6)


def test_no_lookahead_entry_happens_on_signal_close() -> None:
    """A regime flip is only visible at a close; the fill uses that same close."""
    n = REGIME_ROTATION_MA_PERIOD + 5
    btc = [100.0] * n + [300.0]
    timestamps, _ = backtest._aligned_series(basket(btc))
    result = backtest.simulate_rotation(basket(btc))
    assert len(result["trades"]) == 3
    expected_entry = backtest._date(timestamps[-1])
    assert all(t["entry_ts"] == expected_entry for t in result["trades"])


def test_open_basket_is_closed_at_end_of_data() -> None:
    n = REGIME_ROTATION_MA_PERIOD + 20
    result = backtest.simulate_rotation(basket([100.0] * n + [200.0] * 5))
    assert len(result["trades"]) == 3
    assert all(t["exit_reason"] == "end_of_data" for t in result["trades"])


def test_equity_curve_covers_every_bar() -> None:
    btc = [100.0] * (REGIME_ROTATION_MA_PERIOD + 15)
    result = backtest.simulate_rotation(basket(btc))
    assert len(result["equity_curve"]) == len(btc)


def test_alignment_uses_only_common_timestamps() -> None:
    btc = series([100.0] * 30)
    eth = series([100.0] * 25)  # shorter history: 5 earliest bars dropped
    eth = [{**c, "ts": str(int(c["ts"]) + 5 * BAR_MS)} for c in eth]  # and shifted
    timestamps, _ = backtest._aligned_series({"BTC-USDT": btc, "ETH-USDT": eth})
    assert timestamps
    assert all(ts in {c["ts"] for c in eth} for ts in timestamps)


def test_window_limits_trading_but_keeps_sma_warmup() -> None:
    """A window start must not shorten the SMA history: the first bar trades on a full filter."""
    n = REGIME_ROTATION_MA_PERIOD + 20
    btc = [100.0] * n + [200.0] * 100
    candles = basket(btc)
    timestamps, _ = backtest._aligned_series(candles)
    # Start inside the Risk-On stretch; the warm-up bars still feed SMA(100).
    start = int(timestamps[REGIME_ROTATION_MA_PERIOD + 5])
    result = backtest.simulate_rotation(candles, start_ts=start)
    assert result["trades"], "no basket opened: the window start starved the SMA"
    assert int(result["equity_curve"][0][0]) == start


def test_window_end_closes_open_basket() -> None:
    n = REGIME_ROTATION_MA_PERIOD + 20
    btc = [100.0] * n + [200.0] * 100
    candles = basket(btc)
    timestamps, _ = backtest._aligned_series(candles)
    end = int(timestamps[REGIME_ROTATION_MA_PERIOD + 30])
    result = backtest.simulate_rotation(candles, end_ts=end)
    assert len(result["trades"]) == 3
    assert all(t["exit_reason"] == "end_of_data" for t in result["trades"])
    assert result["equity_curve"][-1][0] == timestamps[REGIME_ROTATION_MA_PERIOD + 30]


def test_walk_forward_windows_are_yearly_and_shift_by_quarter() -> None:
    windows = backtest.walk_forward_windows("2022-07", years=1, step_months=3, last_start="2022-10")
    # `last_start` is inclusive: 2022-10 is the last window that may start.
    assert [start for start, _ in windows] == ["2022-07-01", "2022-10-01"]
    first_start, first_end = windows[0]
    # 12 months minus one day: the next window starts where this one ends.
    assert first_start == "2022-07-01"
    assert first_end == "2023-06-30"


def test_walk_forward_derives_the_last_window_from_the_data() -> None:
    """Without `--wfa-to` the grid must run to the data, not stop at one window."""
    bars_per_year = 365 * 24 // 4
    n = REGIME_ROTATION_MA_PERIOD + 20
    prices = [100.0] * (n + 3 * bars_per_year)
    rows = backtest.walk_forward_report(
        basket(prices),
        ma_period=REGIME_ROTATION_MA_PERIOD,
        dead_bands=(0.0,),
        first_start="2020-01",
    )
    # Three years of data -> about 12 windows (3 years, step 3 months, 1-year each).
    assert len(rows) >= 8


def test_walk_forward_drops_windows_past_the_end_of_data() -> None:
    """A clipped window would compare a partial year against a full one."""
    n = REGIME_ROTATION_MA_PERIOD + 20
    candles = basket([100.0] * n)  # starts 2020-09, ends a few weeks later
    rows = backtest.walk_forward_report(
        candles,
        ma_period=REGIME_ROTATION_MA_PERIOD,
        dead_bands=(0.0, 0.005),
        first_start="2020-01",
        last_start="2030-01",
    )
    # No 12-month window fits in ~70 bars, so nothing is measured rather than
    # reporting a clipped 2-month "year".
    assert rows == []


def test_walk_forward_variants_share_each_window() -> None:
    """Every band is measured on the same bars and against the same B&H."""
    n = REGIME_ROTATION_MA_PERIOD + 20
    # Two years of flat history: long enough for one full window.
    bars_per_year = 365 * 24 // 4
    prices = [100.0] * (n + 2 * bars_per_year)
    rows = backtest.walk_forward_report(
        basket(prices),
        ma_period=REGIME_ROTATION_MA_PERIOD,
        dead_bands=(0.0, 0.005),
        first_start="2020-01",
        last_start="2020-01",
    )
    assert len(rows) == 1
    row = rows[0]
    assert set(row) >= {0.0, 0.005, "bh"}
    # Flat prices: B&H loses exactly the round-trip costs.
    expected_bh = -2 * backtest.ROTATION_COST_PER_SIDE * 100.0
    assert row["bh"]["total_pct"] == pytest.approx(expected_bh, rel=1e-6)
    assert row[0.0]["total_pct"] == pytest.approx(0.0, abs=1e-9)


def test_rotation_stats_reports_portfolio_metrics() -> None:
    n = REGIME_ROTATION_MA_PERIOD + 20
    result = backtest.simulate_rotation(basket([100.0] * n + [200.0] * 10 + [50.0] * 10))
    stats = backtest.rotation_stats(result)
    assert stats["n"] == 3
    assert stats["entries"] == 1
    assert stats["exits"] == 1
    assert stats["win_rate"] == 0.0
    assert stats["max_dd_pct"] > 0
    assert stats["total_pct"] < 0
