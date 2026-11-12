"""Indicator tests: SMA and RSI by Wilder's method."""

from __future__ import annotations

import pytest

from okxbot.data.indicators import (
    adx,
    atr,
    compute_snapshot,
    rsi,
    signal_hint,
    sma,
    sma_regime,
    true_ranges,
)
from tests.conftest import make_candles


def test_sma_basic() -> None:
    assert sma([1, 2, 3, 4], 2) == 3.5


def test_sma_too_few_values() -> None:
    assert sma([1, 2], 5) is None


def test_sma_invalid_period() -> None:
    with pytest.raises(ValueError):
        sma([1, 2, 3], 0)


def test_sma_regime_on_when_price_above_average() -> None:
    assert sma_regime([10.0] * 9 + [20.0], 10) == "on"


def test_sma_regime_off_when_price_below_average() -> None:
    assert sma_regime([10.0] * 9 + [1.0], 10) == "off"


def test_sma_regime_unknown_without_enough_history() -> None:
    """Fail-closed: an uncomputable SMA must never read as Risk-On."""
    assert sma_regime([10.0, 20.0], 100) is None


def test_sma_regime_unknown_inside_dead_band() -> None:
    """A price on the average is ambiguous, not "on"."""
    closes = [100.0] * 9 + [100.0]
    assert sma_regime(closes, 10, dead_band=0.001) is None


def test_sma_regime_dead_band_is_fractional() -> None:
    closes = [100.0] * 9 + [100.5]
    assert sma_regime(closes, 10, dead_band=0.01) is None
    assert sma_regime(closes, 10, dead_band=0.001) == "on"


def test_sma_regime_invalid_period() -> None:
    with pytest.raises(ValueError):
        sma_regime([1.0, 2.0], 0)


def test_rsi_all_gains_is_100() -> None:
    closes = [float(value) for value in range(1, 20)]
    assert rsi(closes, 14) == 100.0


def test_rsi_needs_enough_data() -> None:
    assert rsi([1.0, 2.0, 3.0], 14) is None


def test_rsi_known_series() -> None:
    # 14 declines and 1 gain: RSI clearly below 30.
    closes = [50.0 - index for index in range(15)] + [36.0]
    value = rsi(closes, 14)
    assert value is not None
    assert value < 30.0


def test_compute_snapshot_rising(rising_candles) -> None:
    snap = compute_snapshot(rising_candles, rsi_period=14, ma_period=20)
    assert snap["candles_count"] == 30
    assert snap["price"] == rising_candles[-1]["close"]
    assert snap["rsi"] == 100.0
    assert snap["ma20"] is not None
    assert snap["trend"] == "up"


def test_compute_snapshot_empty() -> None:
    snap = compute_snapshot([])
    assert snap["candles_count"] == 0
    assert snap["rsi"] is None


def test_signal_hint_oversold_falling() -> None:
    snap = {"rsi": 25.0, "regime": "down", "vol_ratio": 1.5, "adx": 18.0}
    assert signal_hint(snap) == "watch"


def test_signal_hint_oversold_flat() -> None:
    """A computed flat PASSES — it is a result, not missing data."""
    snap = {"rsi": 25.0, "regime": "flat", "vol_ratio": 1.5, "adx": 18.0}
    assert signal_hint(snap) == "consider_buy"


def test_signal_hint_requires_volume_confirmation() -> None:
    """Oversold alone is not enough — volume must confirm the move."""
    snap = {"rsi": 25.0, "regime": "flat", "vol_ratio": 0.5, "adx": 18.0}
    assert signal_hint(snap) == "watch"


def test_signal_hint_unknown_regime_fails_closed() -> None:
    """An unknown regime BLOCKS — unknown != flat.

    That is the whole difference: `regime=None` (MA200 cannot be computed) must
    behave like a filter that did not pass, not like flat.
    """
    snap = {"rsi": 25.0, "regime": None, "vol_ratio": 1.5, "adx": 18.0}
    assert signal_hint(snap) == "watch"


def test_signal_hint_missing_regime_key_fails_closed() -> None:
    """A missing "regime" key also blocks — no silent default to "flat"."""
    snap = {"rsi": 25.0, "vol_ratio": 1.5, "adx": 18.0}
    assert signal_hint(snap) == "watch"


def test_signal_hint_unknown_volume_fails_closed() -> None:
    snap = {"rsi": 25.0, "regime": "flat", "vol_ratio": None, "adx": 18.0}
    assert signal_hint(snap) == "watch"


def test_signal_hint_strong_trend_blocks_mean_reversion() -> None:
    """ADX >= 25: the market is trending, not consolidating — this is not the setup."""
    snap = {"rsi": 25.0, "regime": "up", "vol_ratio": 1.5, "adx": 32.0}
    assert signal_hint(snap) == "watch"


def test_signal_hint_unknown_adx_fails_closed() -> None:
    snap = {"rsi": 25.0, "regime": "up", "vol_ratio": 1.5}
    assert signal_hint(snap) == "watch"


def test_adx_strict_uptrend_is_max() -> None:
    """A uniform trend: +DM on every candle, -DM zero → DX/ADX = 100."""
    candles = make_candles([100.0 + index for index in range(40)])
    assert adx(candles, 14) == pytest.approx(100.0)


def test_adx_needs_enough_data() -> None:
    candles = make_candles([100.0] * 20)
    assert adx(candles, 14) is None


def test_adx_sideways_market_is_low() -> None:
    """A market without direction has low ADX — that is the consolidation signal."""
    candles = make_candles([100.0 + (1.0 if index % 2 else -1.0) for index in range(60)])
    value = adx(candles, 14)
    assert value is not None
    assert value < 25.0


def test_signal_hint_missing_data() -> None:
    assert signal_hint({"rsi": None}) == "no_data"


def test_snapshot_from_flat_market() -> None:
    candles = make_candles([100.0] * 30)
    snap = compute_snapshot(candles)
    assert snap["trend"] == "flat"


# --------------------------------------------------------------------------- ATR


def test_true_ranges_uses_previous_close_for_gaps() -> None:
    """On a gap, True Range takes the distance from the previous close.

    Candle 2 opens far above the previous close, so high-low alone would
    understate volatility — that is exactly the case TR exists for.
    """
    candles = [
        {"high": 10.0, "low": 9.0, "close": 9.5},
        {"high": 20.0, "low": 19.0, "close": 19.5},
    ]
    ranges = true_ranges(candles)
    assert ranges[0] == 1.0
    # max(20-19, |20-9.5|, |19-9.5|) = max(1.0, 10.5, 9.5) = 10.5
    assert ranges[1] == 10.5


def test_atr_needs_period_plus_one_candles() -> None:
    """The first candle has no TR, so one observation is missing."""
    assert atr(make_candles([100.0] * 14), 14) is None
    assert atr(make_candles([100.0] * 15), 14) is not None


def test_atr_of_constant_range() -> None:
    """A constant range gives an ATR equal to that range (Wilder converges to the mean)."""
    candles = [{"high": 101.0, "low": 99.0, "close": 100.0} for _ in range(50)]
    value = atr(candles, 14)
    assert value is not None
    assert value == pytest.approx(2.0)


def test_atr_invalid_period() -> None:
    with pytest.raises(ValueError):
        atr(make_candles([100.0] * 20), 0)


def test_snapshot_reports_atr_and_atr_pct(rising_candles) -> None:
    snap = compute_snapshot(rising_candles, atr_period=14)
    assert snap["atr"] is not None
    assert snap["atr_pct"] is not None
    # Candles from make_candles have a 2% price range (high 1.01, low 0.99).
    assert snap["atr_pct"] == pytest.approx(2.0, abs=0.2)


# ------------------------------------------------------------------------ regime


def test_snapshot_reports_regime_from_long_ma() -> None:
    """The regime is measured against the long average, not MA20."""
    candles = make_candles([100.0 + index * 0.5 for index in range(250)])
    snap = compute_snapshot(candles, trend_ma_period=200)
    assert snap["ma200"] is not None
    assert snap["regime"] == "up"


def test_snapshot_regime_is_unknown_without_long_history() -> None:
    """Too few candles for MA200 → regime UNDEFINED (None), not guessed.

    Regression: it used to return "flat" here, so a snapshot without MA200 was
    indistinguishable from a real flat and passed the regime filter.
    """
    candles = make_candles([100.0 + index * 0.5 for index in range(30)])
    snap = compute_snapshot(candles, trend_ma_period=200)
    assert snap["ma200"] is None
    assert snap["regime"] is None


def test_snapshot_flat_is_reported_only_when_computed() -> None:
    """flat is returned only when the average exists and the price sits on it.

    The same snapshot with too short a history gives None — comparing both cases
    shows that flat is not a default value.
    """
    candles = make_candles([100.0] * 30)
    computed = compute_snapshot(candles, ma_period=20, trend_ma_period=25)
    assert computed["ma20"] is not None
    assert computed["trend"] == "flat"
    assert computed["regime"] == "flat"

    unknown = compute_snapshot(candles, ma_period=20, trend_ma_period=200)
    assert unknown["ma200"] is None
    assert unknown["regime"] is None


# --------------------------------------------------------------------- volume


def test_snapshot_volume_ratio_reflects_spike() -> None:
    """vol_ratio > 1 when the last candle has above-average volume.

    Note the detail: the spike enters its own baseline average, because SMA
    computes the last `vol_period` candles including the current one. With 19
    candles at 1.0 and a spike of 5.0 the average is 1.2, so the ratio is
    5.0/1.2, not 5.0.
    """
    candles = make_candles([100.0] * 30)
    for candle in candles:
        candle["vol"] = 1.0
    candles[-1]["vol"] = 5.0
    snap = compute_snapshot(candles, vol_period=20)
    assert snap["vol_ratio"] == pytest.approx(5.0 / 1.2, abs=0.01)


def test_snapshot_volume_ratio_none_without_volume() -> None:
    candles = make_candles([100.0] * 30)
    for candle in candles:
        candle["vol"] = 0.0
    snap = compute_snapshot(candles, vol_period=20)
    assert snap["vol_ratio"] is None
