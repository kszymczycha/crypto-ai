"""Strategy rules — entry and exit hints (not automated trading).

Two hypotheses coexist, both long-only, on a 4H horizon:

1. **Mean reversion** (`evaluate_mean_reversion`) — the direction was closed after
   B1 (negative expectancy, see `docs/strategy.md`). It stays in the code as a
   reference point; the executor still enforces this gate (`executor/service.py`).
2. **Regime Rotation** (`evaluate_regime_rotation`) — the new B1 hypothesis.
   A whole-basket signal from one filter: BTC close vs SMA(100). The executor
   does not handle it (it requires `tp_pct`/`sl_pct`) — deployment is a separate
   stage C.

Mean reversion: a buy passes only when `evaluate()` returns
`allow_entry=True`; a sell is not gated (manual emergency exit).
Entry rule: RSI(14) <= rsi_entry AND regime != "down"
AND ADX(14) < adx_max AND vol_ratio >= min_vol_ratio; exits are ATR(14)
multiples.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from okxbot.data.indicators import sma, sma_regime, signal_hint

# Bounds for suggested TP/SL. The executor enforces its own limits
# (`config/risk_limits.yaml`), but these bounds protect against absurd values
# when ATR is distorted (e.g. by a single candle with an extreme range).
MIN_TP_SL_PCT = 0.5
MAX_TP_SL_PCT = 20.0


@dataclass(frozen=True)
class StrategyConfig:
    """Strategy thresholds: entry from RSI + regime + volume, exits from ATR."""

    rsi_entry: float = 35.0
    min_vol_ratio: float = 1.0
    # ADX measures trend strength. Mean reversion buys in consolidation; at ADX
    # >= 25 the market is moving and "oversold" is often a continuation of the drop.
    adx_max: float = 25.0
    # MA200 needs 200 candles, RSI and ATR need a dozen or so — 210 is the
    # threshold below which the regime is unreliable, not merely "slightly worse".
    min_candles: int = 210
    tp_atr_multiple: float = 3.0
    sl_atr_multiple: float = 1.5
    # Fallback values when ATR is unavailable (too little candle history).
    default_tp_pct: float = 2.0
    default_sl_pct: float = 1.0


def _clamp_pct(value: float) -> float:
    """Clamps a percentage to [MIN_TP_SL_PCT, MAX_TP_SL_PCT] and rounds it."""
    return round(min(max(value, MIN_TP_SL_PCT), MAX_TP_SL_PCT), 2)


def _regime(snapshot: dict[str, Any]) -> str | None:
    """Market regime.

    Returns None when the regime cannot be determined — it never substitutes
    "flat". Missing data and a computed flat are two different states (see
    `indicators.compute_snapshot`), and the rule is fail-closed: an unknown
    regime blocks entry.
    """
    regime = snapshot.get("regime")
    return None if regime is None else str(regime)


def exits_from_atr(
    snapshot: dict[str, Any], config: StrategyConfig | None = None
) -> tuple[float, float]:
    """Returns (tp_pct, sl_pct) calculated from ATR.

    When ATR is missing (too few candles), returns the configured fallback
    values — the signal must still carry TP/SL, because an order without them is
    unprotected (see `docs/runbook.md` §8.1).
    """
    cfg = config or StrategyConfig()
    atr_pct = snapshot.get("atr_pct")
    if atr_pct is None:
        return cfg.default_tp_pct, cfg.default_sl_pct
    atr_pct = float(atr_pct)
    if atr_pct <= 0:
        return cfg.default_tp_pct, cfg.default_sl_pct
    return (
        _clamp_pct(atr_pct * cfg.tp_atr_multiple),
        _clamp_pct(atr_pct * cfg.sl_atr_multiple),
    )


def evaluate(snapshot: dict[str, Any], config: StrategyConfig | None = None) -> dict[str, Any]:
    """Evaluates a snapshot and returns a hint plus suggested TP/SL (mean reversion).

    Result: {hint, allow_entry, confidence, suggested_tp_pct, suggested_sl_pct,
    reasons[]}. `allow_entry` is the only binding verdict for the executor gate.
    `confidence` describes how many entry conditions are met — it is a measure of
    setup completeness, not the probability of profit.
    """
    cfg = config or StrategyConfig()
    tp_pct, sl_pct = exits_from_atr(snapshot, cfg)

    if snapshot.get("candles_count", 0) < cfg.min_candles:
        return {
            "hint": "no_data",
            "allow_entry": False,
            "confidence": 0.0,
            "suggested_tp_pct": tp_pct,
            "suggested_sl_pct": sl_pct,
            "reasons": [
                f"not enough candles ({snapshot.get('candles_count', 0)} < {cfg.min_candles})"
            ],
        }

    hint = signal_hint(
        snapshot,
        rsi_entry=cfg.rsi_entry,
        min_vol_ratio=cfg.min_vol_ratio,
        adx_max=cfg.adx_max,
    )
    reasons: list[str] = []
    confidence = 0.0

    rsi_value = snapshot.get("rsi")
    if rsi_value is None:
        reasons.append("missing RSI")
    else:
        reasons.append(f"RSI={rsi_value}")
        if rsi_value <= cfg.rsi_entry:
            confidence += 0.4

    regime = _regime(snapshot)
    if regime == "up":
        reasons.append("regime: price above MA200")
        confidence += 0.3
    elif regime == "down":
        reasons.append("regime: price below MA200 — entry rejected")
    elif regime == "flat":
        reasons.append("regime: price near MA200")
        confidence += 0.1
    else:
        reasons.append("missing regime (not enough candles for MA200) — entry rejected")

    adx_value = snapshot.get("adx")
    if adx_value is None:
        reasons.append("missing ADX — entry rejected")
    elif adx_value < cfg.adx_max:
        reasons.append(f"ADX={adx_value} < {cfg.adx_max} (consolidation)")
    else:
        reasons.append(f"ADX={adx_value} >= {cfg.adx_max} (trend) — entry rejected")

    vol_ratio = snapshot.get("vol_ratio")
    if vol_ratio is None:
        reasons.append("missing volume data")
    elif vol_ratio >= cfg.min_vol_ratio:
        reasons.append(f"volume {vol_ratio}x the average")
        confidence += 0.3
    else:
        reasons.append(f"volume {vol_ratio}x below the average")

    reasons.append(f"TP/SL from ATR: {tp_pct}%/{sl_pct}%")

    return {
        "hint": hint,
        "allow_entry": hint == "consider_buy",
        "confidence": round(min(confidence, 1.0), 2),
        "suggested_tp_pct": tp_pct,
        "suggested_sl_pct": sl_pct,
        "reasons": reasons,
    }


def evaluate_mean_reversion(
    snapshot: dict[str, Any], config: StrategyConfig | None = None
) -> dict[str, Any]:
    """Alias describing the hypothesis, kept for callers that name it explicitly.

    The body lives in `evaluate`, which the executor gate and `strategy.signals`
    already import. Renaming that function is an `executor/` change, so this
    name exists only to document the split in `docs/strategy.md`.
    """
    return evaluate(snapshot, config)


# --------------------------------------------------------------- regime rotation
#
# Long-only rotation on a basket: one market-level filter (BTC vs SMA(100))
# decides both sides. No per-pair indicator, no ATR exits — the regime flip is
# the only exit, so the position has no TP/SL of its own.
#
# The filter uses BTC only, deliberately: correlations across the three pairs
# are high, and a shared filter makes the basket one bet rather than three.

REGIME_ROTATION_MA_PERIOD = 100


@dataclass(frozen=True)
class RegimeRotationConfig:
    """Regime Rotation hypothesis thresholds.

    `ma_period` is the SMA length on 4H candles (SMA(100) ≈ 16.7 days).
    `dead_band` is a dead zone around the SMA as a fraction of price; 0.0 means a
    strict comparison. A non-zero value dampens regime flipping on noise — at the
    cost of a later entry and exit.
    """

    ma_period: int = REGIME_ROTATION_MA_PERIOD
    dead_band: float = 0.0


def evaluate_regime_rotation(
    market_closes: list[float] | None, config: RegimeRotationConfig | None = None
) -> dict[str, Any]:
    """Evaluates the market filter and returns the basket verdict.

    The argument is the closed BTC-USDT closes (4H), oldest to newest; the
    decision is calculated from the last close.

    Returns {regime, allow_entry, allow_exit, reasons[]}:
    - `allow_entry` — buy the basket: BTC close > SMA(ma_period).
    - `allow_exit`  — sell 100% of the basket: BTC close < SMA(ma_period).

    Both flags are never true at once. Unknown (too little history, empty
    series) returns both False and `regime=None`: the filter fails closed in
    both directions, because acting on missing data is worse than holding an
    existing position. `allow_exit` is a *regime* verdict, not a gate over an
    open position — no position state is tracked here.
    """
    cfg = config or RegimeRotationConfig()
    regime = sma_regime(market_closes or [], cfg.ma_period, dead_band=cfg.dead_band)
    if regime is None:
        reasons = [
            "no regime: not enough BTC history for the SMA "
            f"({cfg.ma_period}) or price inside the dead band — no action"
        ]
        return {"regime": None, "allow_entry": False, "allow_exit": False, "reasons": reasons}

    average = sma(market_closes or [], cfg.ma_period)
    price = market_closes[-1]
    if regime == "on":
        reasons = [
            f"risk-on: BTC {price:.2f} > SMA{cfg.ma_period} {average:.2f} — buy the basket",
        ]
    else:
        reasons = [
            f"risk-off: BTC {price:.2f} < SMA{cfg.ma_period} {average:.2f} — sell the basket",
        ]
    return {
        "regime": regime,
        "allow_entry": regime == "on",
        "allow_exit": regime == "off",
        "reasons": reasons,
    }
