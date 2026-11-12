"""Strategy rule tests: entry from RSI + regime + volume, exits from ATR."""

from __future__ import annotations

import pytest

from okxbot.strategy.rules import (
    MAX_TP_SL_PCT,
    MIN_TP_SL_PCT,
    RegimeRotationConfig,
    StrategyConfig,
    evaluate,
    evaluate_mean_reversion,
    evaluate_regime_rotation,
    exits_from_atr,
)


def snapshot(
    rsi_value,
    regime="up",
    candles=250,
    *,
    vol_ratio=1.5,
    atr_pct=1.0,
    adx_value=18.0,
    trend=None,
) -> dict:
    """Snapshot with complete data; by default one that passes the buy rule."""
    snap = {
        "rsi": rsi_value,
        "regime": regime,
        "candles_count": candles,
        "price": 100.0,
        "vol_ratio": vol_ratio,
        "atr_pct": atr_pct,
        "adx": adx_value,
    }
    if trend is not None:
        snap["trend"] = trend
    return snap


# ----------------------------------------------------------------------- entry


def test_oversold_in_uptrend_with_volume_suggests_buy() -> None:
    """Full set of conditions: oversold RSI + non-downtrend regime + volume."""
    result = evaluate(snapshot(28.0, "up", vol_ratio=1.5))
    assert result["hint"] == "consider_buy"
    assert result["confidence"] > 0


def test_downtrend_blocks_entry_despite_oversold_rsi() -> None:
    """A downtrend rejects the entry — that is the whole point of the MA200 filter."""
    result = evaluate(snapshot(20.0, "down", vol_ratio=2.0))
    assert result["hint"] == "watch"


def test_low_volume_blocks_entry() -> None:
    """Oversold RSI, but volume below the average — no confirmation."""
    result = evaluate(snapshot(25.0, "up", vol_ratio=0.5))
    assert result["hint"] == "watch"


def test_high_rsi_is_watch_not_sell() -> None:
    """The strategy is long-only: overbought does not generate a sell signal."""
    result = evaluate(snapshot(78.0, "up"))
    assert result["hint"] == "watch"


def test_neutral_is_watch() -> None:
    result = evaluate(snapshot(50.0, "up"))
    assert result["hint"] == "watch"


def test_not_enough_candles() -> None:
    result = evaluate(snapshot(20.0, "up", candles=10))
    assert result["hint"] == "no_data"
    assert result["allow_entry"] is False
    assert result["confidence"] == 0.0


def test_min_candles_reflects_ma200_requirement() -> None:
    """MA200 needs 200 candles — the threshold must account for that, not be cosmetic."""
    assert StrategyConfig().min_candles > 200


def test_missing_rsi() -> None:
    result = evaluate(snapshot(None, "up"))
    assert result["hint"] == "no_data"


def test_unknown_regime_blocks_entry() -> None:
    """An unknown regime blocks the entry — fail-closed, unknown != flat.

    `flat` still passes (see `test_flat_regime_allows_entry`); only missing data
    blocks.
    """
    snap = snapshot(25.0, None)
    assert evaluate(snap)["hint"] == "watch"
    assert any("missing regime" in reason for reason in evaluate(snap)["reasons"])


def test_missing_regime_key_also_blocks_entry() -> None:
    """A missing "regime" key is treated as missing data, not as flat."""
    snap = snapshot(25.0, "up")
    snap.pop("regime")
    snap["trend"] = "flat"
    assert evaluate(snap)["hint"] == "watch"


def test_flat_regime_allows_entry() -> None:
    """flat is a valid computed result — it must not block."""
    assert evaluate(snapshot(25.0, "flat"))["hint"] == "consider_buy"


def test_custom_rsi_entry_threshold() -> None:
    cfg = StrategyConfig(rsi_entry=40.0)
    assert evaluate(snapshot(35.0, "up"), cfg)["hint"] == "consider_buy"


# ----------------------------------------------------------------------- exits


def test_exits_are_atr_multiples() -> None:
    """TP/SL are ATR multiples, not fixed percentages."""
    tp_pct, sl_pct = exits_from_atr(snapshot(30.0, atr_pct=1.0))
    assert tp_pct == 3.0  # 1.0% * 3.0
    assert sl_pct == 1.5  # 1.0% * 1.5


def test_exits_scale_with_volatility() -> None:
    """Higher volatility gives wider exits."""
    tp_low, _ = exits_from_atr(snapshot(30.0, atr_pct=0.5))
    tp_high, _ = exits_from_atr(snapshot(30.0, atr_pct=2.0))
    assert tp_high > tp_low


def test_exits_are_clamped_to_sane_bounds() -> None:
    """An extreme ATR must not generate an absurd TP/SL."""
    tp_pct, sl_pct = exits_from_atr(snapshot(30.0, atr_pct=100.0))
    assert tp_pct == MAX_TP_SL_PCT
    assert sl_pct == MAX_TP_SL_PCT


def test_exits_have_a_floor() -> None:
    tp_pct, sl_pct = exits_from_atr(snapshot(30.0, atr_pct=0.001))
    assert tp_pct == MIN_TP_SL_PCT
    assert sl_pct == MIN_TP_SL_PCT


def test_exits_fall_back_when_atr_missing() -> None:
    """Without ATR we return fallback values — a signal never goes out without TP/SL."""
    tp_pct, sl_pct = exits_from_atr(snapshot(30.0, atr_pct=None))
    assert (tp_pct, sl_pct) == (2.0, 1.0)


def test_zero_atr_falls_back_instead_of_dividing() -> None:
    tp_pct, sl_pct = exits_from_atr(snapshot(30.0, atr_pct=0.0))
    assert (tp_pct, sl_pct) == (2.0, 1.0)


def test_evaluate_suggests_atr_based_tp_sl() -> None:
    result = evaluate(snapshot(25.0, "up", atr_pct=1.0))
    assert result["suggested_tp_pct"] == 3.0
    assert result["suggested_sl_pct"] == 1.5


def test_missing_volume_blocks_entry() -> None:
    """Missing volume does NOT give an entry — the move cannot be confirmed."""
    snap = {"rsi": 20.0, "regime": "up", "candles_count": 250, "adx": 18.0}
    assert evaluate(snap)["hint"] == "watch"


def test_strong_trend_blocks_entry_despite_oversold_rsi() -> None:
    """ADX >= 25 is a trend, not a consolidation — mean-reversion does not buy the dip there."""
    result = evaluate(snapshot(25.0, "up", adx_value=32.0))
    assert result["hint"] == "watch"
    assert result["allow_entry"] is False
    assert any("ADX" in reason and "trend" in reason for reason in result["reasons"])


def test_missing_adx_blocks_entry() -> None:
    """Missing ADX is missing data — fail-closed, just like with the regime."""
    snap = snapshot(25.0, "up")
    snap.pop("adx")
    assert evaluate(snap)["hint"] == "watch"


def test_allow_entry_matches_hint() -> None:
    """The executor gate reads `allow_entry`; it must be consistent with `hint`."""
    assert evaluate(snapshot(25.0, "up"))["allow_entry"] is True
    assert evaluate(snapshot(50.0, "up"))["allow_entry"] is False


def test_every_unknown_field_fails_closed() -> None:
    """Consistent behaviour: every missing input blocks the entry."""
    base = {
        "rsi": 20.0,
        "regime": "up",
        "candles_count": 250,
        "vol_ratio": 2.0,
        "adx": 18.0,
    }
    complete = evaluate(base)
    assert complete["hint"] == "consider_buy"  # baseline: works

    for missing in ("rsi", "regime", "vol_ratio", "adx"):
        snap = {key: value for key, value in base.items() if key != missing}
        assert evaluate(snap)["hint"] != "consider_buy", f"missing {missing} let the entry through"


def test_evaluate_always_returns_tp_and_sl() -> None:
    """Every path returns TP/SL — an order without protection is a process error."""
    for snap in (
        snapshot(20.0, "up", candles=10),
        snapshot(None, "up"),
        snapshot(25.0, "down"),
        snapshot(25.0, "up", atr_pct=None),
    ):
        result = evaluate(snap)
        assert result["suggested_tp_pct"] > 0
        assert result["suggested_sl_pct"] > 0


@pytest.mark.parametrize("regime", ["up", "flat", "down"])
def test_evaluate_returns_reasons_for_every_regime(regime: str) -> None:
    result = evaluate(snapshot(25.0, regime))
    assert result["reasons"]
    assert any("regime" in reason for reason in result["reasons"])


def test_mean_reversion_alias_matches_evaluate() -> None:
    """The alias exists to name the hypothesis; it must not drift from `evaluate`."""
    snap = snapshot(25.0, "up")
    assert evaluate_mean_reversion(snap) == evaluate(snap)


# ------------------------------------------------------------ regime rotation


def test_regime_on_allows_entry() -> None:
    result = evaluate_regime_rotation([100.0] * 99 + [120.0])
    assert result["regime"] == "on"
    assert result["allow_entry"] is True
    assert result["allow_exit"] is False


def test_regime_off_allows_exit() -> None:
    result = evaluate_regime_rotation([100.0] * 99 + [80.0])
    assert result["regime"] == "off"
    assert result["allow_entry"] is False
    assert result["allow_exit"] is True


def test_regime_never_allows_entry_and_exit_together() -> None:
    for closes in ([100.0] * 99 + [120.0], [100.0] * 99 + [80.0]):
        result = evaluate_regime_rotation(closes)
        assert not (result["allow_entry"] and result["allow_exit"])


def test_regime_unknown_blocks_both_directions() -> None:
    """Fail-closed in both directions: no data means no action at all."""
    result = evaluate_regime_rotation([100.0, 101.0])
    assert result["regime"] is None
    assert result["allow_entry"] is False
    assert result["allow_exit"] is False


def test_regime_empty_series_is_unknown() -> None:
    result = evaluate_regime_rotation([])
    assert result["regime"] is None


def test_regime_uses_only_the_last_close() -> None:
    """A long history above the average does not matter if the last close is below."""
    closes = [100.0] * 99 + [200.0] * 20 + [50.0]
    assert evaluate_regime_rotation(closes)["regime"] == "off"


def test_regime_dead_band_blocks_ambiguous_price() -> None:
    result = evaluate_regime_rotation([100.0] * 100, RegimeRotationConfig(dead_band=0.01))
    assert result["regime"] is None
