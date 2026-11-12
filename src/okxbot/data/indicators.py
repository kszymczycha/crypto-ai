"""Technical indicators computed locally (no external dependencies).

RSI is computed with Wilder's method (as in TradingView), MA as a moving average.
Input: a list of candles from oldest to newest.
"""

from __future__ import annotations

from typing import Any


def _closes(candles: list[dict[str, Any]]) -> list[float]:
    return [float(candle["close"]) for candle in candles]


def sma(values: list[float], period: int) -> float | None:
    """Moving average of the last `period` values; None when there is too little data."""
    if period <= 0:
        raise ValueError("period must be > 0")
    if len(values) < period:
        return None
    window = values[-period:]
    return sum(window) / period


def sma_regime(
    closes: list[float], period: int = 100, *, dead_band: float = 0.0
) -> str | None:
    """Classify the market against a long SMA: "on" above, "off" below, None unknown.

    Used by the regime-rotation hypothesis (`strategy.rules.evaluate_regime_rotation`),
    where the filter is BTC close vs SMA(100) on 4H. The SMA itself comes from
    `sma()`; this function only turns it into a binary decision.

    `dead_band` is a fraction of the average (0.0 by default, i.e. a strict
    comparison). A price inside the band is ambiguous and returns None — the
    same fail-closed rule as everywhere else: an unclassifiable bar must never
    be read as "on", because that would open a position on no signal. An SMA of
    zero or None returns None as well, for the same reason.
    """
    if period <= 0:
        raise ValueError("period must be > 0")
    if not closes:
        return None
    average = sma(closes, period)
    if average is None or average <= 0:
        return None
    price = closes[-1]
    if price > average * (1.0 + dead_band):
        return "on"
    if price < average * (1.0 - dead_band):
        return "off"
    return None


def rsi(closes: list[float], period: int = 14) -> float | None:
    """Wilder's RSI. Returns None when there are fewer than period + 1 data points."""
    if period <= 0:
        raise ValueError("period must be > 0")
    if len(closes) < period + 1:
        return None
    gains = 0.0
    losses = 0.0
    for previous, current in zip(closes[: period + 1], closes[1 : period + 1], strict=False):
        delta = current - previous
        if delta >= 0:
            gains += delta
        else:
            losses -= delta
    avg_gain = gains / period
    avg_loss = losses / period
    for current, previous in zip(closes[period + 1 :], closes[period:-1], strict=False):
        delta = current - previous
        gain = max(delta, 0.0)
        loss = max(-delta, 0.0)
        avg_gain = (avg_gain * (period - 1) + gain) / period
        avg_loss = (avg_loss * (period - 1) + loss) / period
    if avg_loss == 0:
        return 100.0
    rs = avg_gain / avg_loss
    return 100.0 - (100.0 / (1.0 + rs))


def true_ranges(candles: list[dict[str, Any]]) -> list[float]:
    """Return the True Range for every candle (the first has no prior close)."""
    ranges: list[float] = []
    for index, candle in enumerate(candles):
        high = float(candle["high"])
        low = float(candle["low"])
        if index == 0:
            ranges.append(high - low)
            continue
        previous_close = float(candles[index - 1]["close"])
        ranges.append(max(high - low, abs(high - previous_close), abs(low - previous_close)))
    return ranges


def atr(candles: list[dict[str, Any]], period: int = 14) -> float | None:
    """Average True Range using Wilder's smoothing (as in TradingView).

    Returns None when there are fewer than period + 1 candles: the first candle
    yields no True Range, so we need one extra bar before smoothing starts.
    """
    if period <= 0:
        raise ValueError("period must be > 0")
    if len(candles) < period + 1:
        return None
    ranges = true_ranges(candles)
    value = sum(ranges[1 : period + 1]) / period
    for index in range(period + 1, len(ranges)):
        value = (value * (period - 1) + ranges[index]) / period
    return value


def adx(candles: list[dict[str, Any]], period: int = 14) -> float | None:
    """Average Directional Index (Wilder), as in TradingView.

    It measures the *strength* of a trend, not its direction: a high ADX means a
    trend, a low one means consolidation. The entry rule uses it as a
    mean-reversion filter (see `docs/strategy.md`).

    Returns None when there are fewer than 2 * period + 1 candles: Wilder's
    smoothing needs `period` diffs to seed and another `period` DX values to
    average.
    """
    if period <= 0:
        raise ValueError("period must be > 0")
    if len(candles) < 2 * period + 1:
        return None

    plus_dm: list[float] = []
    minus_dm: list[float] = []
    true_ranges: list[float] = []
    for previous, current in zip(candles, candles[1:], strict=False):
        high = float(current["high"])
        low = float(current["low"])
        previous_high = float(previous["high"])
        previous_low = float(previous["low"])
        previous_close = float(previous["close"])
        up_move = high - previous_high
        down_move = previous_low - low
        plus_dm.append(up_move if up_move > down_move and up_move > 0 else 0.0)
        minus_dm.append(down_move if down_move > up_move and down_move > 0 else 0.0)
        true_ranges.append(
            max(high - low, abs(high - previous_close), abs(low - previous_close))
        )

    smoothed_tr = sum(true_ranges[:period])
    smoothed_plus = sum(plus_dm[:period])
    smoothed_minus = sum(minus_dm[:period])

    dx_values: list[float] = []
    for index in range(period, len(true_ranges)):
        smoothed_tr = smoothed_tr - smoothed_tr / period + true_ranges[index]
        smoothed_plus = smoothed_plus - smoothed_plus / period + plus_dm[index]
        smoothed_minus = smoothed_minus - smoothed_minus / period + minus_dm[index]
        if smoothed_tr == 0:
            dx_values.append(0.0)
            continue
        plus_di = 100.0 * smoothed_plus / smoothed_tr
        minus_di = 100.0 * smoothed_minus / smoothed_tr
        total = plus_di + minus_di
        dx_values.append(100.0 * abs(plus_di - minus_di) / total if total else 0.0)

    if len(dx_values) < period:
        return None
    value = sum(dx_values[:period]) / period
    for dx in dx_values[period:]:
        value = (value * (period - 1) + dx) / period
    return value


def compute_snapshot(
    candles: list[dict[str, Any]],
    *,
    rsi_period: int = 14,
    ma_period: int = 20,
    trend_ma_period: int = 200,
    atr_period: int = 14,
    vol_period: int = 20,
    adx_period: int = 14,
) -> dict[str, Any]:
    """Compute the indicator set for a candle series.

    Returned keys:
    - price, rsi, ma20, trend ("up"/"down"/"flat") — trend against the short MA
    - ma200, regime — trend against the long MA (market-regime filter)
    - atr, atr_pct — absolute volatility and volatility as a share of price
    - adx — trend strength (Wilder); None when there is too little history
    - vol_sma, vol_ratio — average volume and current volume relative to it
    - candles_count
    """
    empty = {
        "price": 0.0,
        "rsi": None,
        "ma20": None,
        # None means "unknown", never "flat". Collapsing the two would let a
        # missing indicator pass as a computed one, and every consumer that
        # filters on this value would then approve entries on no data at all.
        "trend": None,
        "ma200": None,
        "regime": None,
        "atr": None,
        "atr_pct": None,
        "adx": None,
        "vol_sma": None,
        "vol_ratio": None,
        "candles_count": 0,
    }
    if not candles:
        return empty
    closes = _closes(candles)
    price = closes[-1]
    rsi_value = rsi(closes, rsi_period)
    ma_value = sma(closes, ma_period)
    trend_ma_value = sma(closes, trend_ma_period)
    atr_value = atr(candles, atr_period)
    adx_value = adx(candles, adx_period)
    volumes = [float(candle.get("vol") or 0.0) for candle in candles]
    vol_sma_value = sma(volumes, vol_period)
    current_vol = volumes[-1]

    # trend/regime are None when the average itself could not be computed —
    # "unknown", not "flat". "flat" is reserved for a computed average that the
    # price genuinely sits on top of (within the 0.1% dead band).
    trend = None
    if ma_value:
        if price > ma_value * 1.001:
            trend = "up"
        elif price < ma_value * 0.999:
            trend = "down"
        else:
            trend = "flat"

    regime = None
    if trend_ma_value:
        if price > trend_ma_value * 1.001:
            regime = "up"
        elif price < trend_ma_value * 0.999:
            regime = "down"
        else:
            regime = "flat"

    vol_ratio = None
    if vol_sma_value:
        vol_ratio = current_vol / vol_sma_value

    return {
        "price": round(price, 8),
        "rsi": None if rsi_value is None else round(rsi_value, 2),
        "ma20": None if ma_value is None else round(ma_value, 8),
        "trend": trend,
        "ma200": None if trend_ma_value is None else round(trend_ma_value, 8),
        "regime": regime,
        "atr": None if atr_value is None else round(atr_value, 8),
        "atr_pct": None if not atr_value or not price else round(atr_value / price * 100.0, 4),
        "adx": None if adx_value is None else round(adx_value, 2),
        "vol_sma": None if vol_sma_value is None else round(vol_sma_value, 8),
        "vol_ratio": None if vol_ratio is None else round(vol_ratio, 4),
        "candles_count": len(closes),
    }


def signal_hint(
    snapshot: dict[str, Any],
    *,
    rsi_entry: float = 35.0,
    min_vol_ratio: float = 1.0,
    adx_max: float = 25.0,
) -> str:
    """Suggest an entry from RSI, regime, trend strength and volume.

    The hint is also enforced by the executor (the strategy gate in
    `executor/service.py`) — that is the only place where the entry rule has
    binding force. Exits are handled by TP/SL, see `docs/strategy.md`.

    Entry rule (buy only):
        RSI(14) <= rsi_entry AND regime is known and not "down"
        AND ADX(14) < adx_max AND vol_ratio >= min_vol_ratio

    Fail-closed on unknown data: a missing regime or ADX blocks the entry. Only
    a *computed* "flat" passes, because that is a result, not an absence. This
    is the distinction the whole function rests on — unknown is not flat.

    Snapshot keys are read with `.get()`, but a missing key must never be
    silently promoted to a permissive default; that is why there is no
    `snapshot.get("regime", "flat")` anywhere below.

    Returns one of: "consider_buy", "watch", "no_data".
    """
    rsi_value = snapshot.get("rsi")
    if rsi_value is None:
        return "no_data"
    regime = snapshot.get("regime")
    if regime is None or regime == "down":
        # Unknown regime fails closed, exactly like a known downtrend.
        return "watch"
    adx_value = snapshot.get("adx")
    if adx_value is None or float(adx_value) >= adx_max:
        # A strong trend (or missing trend-strength data) blocks mean-reversion:
        # buying the dip in a fast-moving trend is a trend-following strategy,
        # not this rule.
        return "watch"
    vol_ratio = snapshot.get("vol_ratio")
    if vol_ratio is None or vol_ratio < min_vol_ratio:
        return "watch"
    if rsi_value <= rsi_entry:
        return "consider_buy"
    return "watch"
