"""Backtest of the strategy rule on public OKX history (without API keys).

External tool relative to the application: it imports indicators from
`okxbot.data.indicators` read-only and is never called by the executor.

Stages:
- `--frequency` (default): how many candles satisfy the entry rule, per pair/year.
- `--full`: full B1 mean-reversion backtest (costs, IS/OOS, metrics).
- `--regime-rotation`: portfolio backtest of the Regime Rotation hypothesis (B1):
  a single BTC vs SMA(100) filter decides about buying/selling a 1/3+1/3+1/3 basket.
- `--walk-forward`: WFA B1.5 for dead-band variants (12-month windows,
  3-month step), each variant + BTC B&H on the same window.

Mean-reversion rule (verbatim from `okxbot.data.indicators.signal_hint`, thresholds
are not parameterised here in `--frequency`):
    RSI(14) <= 35 AND regime != "down" AND ADX(14) < 25 AND vol_ratio >= 1.0

Regime Rotation rule (`strategy.rules.evaluate_regime_rotation`):
    BTC close > SMA(100) → buy the basket; BTC close < SMA(100) → sell 100%.

Running (no local pip — the okxbot:local image has the OKX SDK):
    docker run --rm -v "$PWD/scripts:/app/scripts:ro" -v "$PWD/src:/app/src:ro" \
        -w /app -e PYTHONPATH=/app/src okxbot:local \
        python scripts/backtest.py --frequency --pairs BTC-USDT,ETH-USDT,SOL-USDT

Market data: `--market-flag` (default "0"). Demo (`"1"`) has a distorted candle
history — single bars with prices two orders of magnitude higher than their
neighbours (BTC 29 445 → 1 112 220 → 178 852 in three consecutive 4H candles).
Public history (`"0"`) is the only source fit for measurement; it is a public
endpoint, without API keys and without orders.
"""

from __future__ import annotations

import argparse
import sys
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

import okx.MarketData as MarketData

from okxbot.data.indicators import compute_snapshot, signal_hint
from okxbot.strategy.rules import (
    REGIME_ROTATION_MA_PERIOD,
    RegimeRotationConfig,
    StrategyConfig,
    evaluate_regime_rotation,
    exits_from_atr,
)

DEFAULT_PAIRS = "BTC-USDT,ETH-USDT,SOL-USDT"
BAR = "4H"
BAR_MS = 4 * 60 * 60 * 1000
# The indicator window must match production: MarketService fetches
# `settings.candle_limit` (300) candles and computes indicators over the whole
# series. A backtest computing over the entire history would yield a different
# MA200 than the bot with the same data.
WINDOW = 300


def fetch_history(pair: str, *, max_candles: int, api: Any) -> list[dict[str, Any]]:
    """Fetches candles from oldest to newest, paginating backwards.

    OKX returns from the newest; `after` moves the window into the past
    (OSI pagination parameter: candles older than the given ts).
    """
    collected: list[dict[str, Any]] = []
    after: str | None = None
    while len(collected) < max_candles:
        kwargs: dict[str, str] = {"instId": pair, "bar": BAR, "limit": "100"}
        if after:
            kwargs["after"] = after
        response = api.get_history_candlesticks(**kwargs)
        if str(response.get("code")) != "0":
            raise RuntimeError(f"OKX {pair}: {response.get('code')} {response.get('msg')}")
        rows = response.get("data") or []
        if not rows:
            break
        collected.extend(
            {
                "ts": row[0],
                "open": float(row[1]),
                "high": float(row[2]),
                "low": float(row[3]),
                "close": float(row[4]),
                "vol": float(row[5]),
            }
            for row in rows
        )
        after = rows[-1][0]
    collected.reverse()  # oldest first
    # Pagination goes into the past, so when the limit is exceeded we cut off
    # the oldest candles — the newer ones are what we want.
    return collected[-max_candles:] if max_candles else collected


def closed_candles(candles: list[dict[str, Any]], *, now_ms: int) -> list[dict[str, Any]]:
    """Drops the last candle if the current moment falls within its interval."""
    if not candles:
        return candles
    if int(candles[-1]["ts"]) + BAR_MS > now_ms:
        return candles[:-1]
    return candles


def window_starts(candles: list[dict[str, Any]], min_index: int) -> list[int]:
    """Indexes from which indicators can be computed (keeping `min_index`)."""
    return list(range(min_index, len(candles)))


def is_entry(snapshot: dict[str, Any]) -> bool:
    """Entry rule 1:1 with the production code — no own interpretation.

    `hint` is gated exactly like `rules.evaluate` — all indicators must be
    computable (unknown blocks).
    """
    return snapshot.get("hint") == "consider_buy"


def evaluate_series(
    candles: list[dict[str, Any]], *, min_index: int, rsi_entry: float = 35.0
) -> list[dict[str, Any]]:
    """Computes a snapshot for every possible decision candle.

    Indicators are computed only from data available **up to** and including the
    given candle — no lookahead. The window is the last `WINDOW` candles, as in
    production; a shorter series (start of history) uses everything available.
    """
    config = StrategyConfig()
    results: list[dict[str, Any]] = []
    for index in window_starts(candles, min_index):
        history = candles[max(0, index + 1 - WINDOW) : index + 1]
        snapshot = compute_snapshot(history)
        snapshot["hint"] = signal_hint(snapshot, rsi_entry=rsi_entry)
        # TP/SL from ATR — the same values that `strategy.signals.build_signal` will send.
        tp_pct, sl_pct = exits_from_atr(snapshot, config)
        snapshot["suggested_tp_pct"] = tp_pct
        snapshot["suggested_sl_pct"] = sl_pct
        results.append({"index": index, "ts": candles[index]["ts"], "snapshot": snapshot})
    return results


def frequency_report(
    candles_by_pair: dict[str, list[dict[str, Any]]], *, min_index: int
) -> list[dict[str, Any]]:
    """Collects the number of setups per pair and per year."""
    rows: list[dict[str, Any]] = []
    for pair, candles in candles_by_pair.items():
        setups = [item for item in evaluate_series(candles, min_index=min_index) if is_entry(item["snapshot"])]
        per_year: dict[int, int] = {}
        for item in setups:
            year = datetime.fromtimestamp(int(item["ts"]) / 1000, UTC).year
            per_year[year] = per_year.get(year, 0) + 1
        rows.append(
            {
                "pair": pair,
                "candles": len(candles),
                "from": _date(candles[min_index]["ts"]) if len(candles) > min_index else None,
                "to": _date(candles[-1]["ts"]),
                "setups": len(setups),
                "per_year": dict(sorted(per_year.items())),
                "setups_detail": [
                    {
                        "ts": _date(item["ts"]),
                        "rsi": item["snapshot"].get("rsi"),
                        "adx": item["snapshot"].get("adx"),
                        "vol_ratio": item["snapshot"].get("vol_ratio"),
                        "regime": item["snapshot"].get("regime"),
                    }
                    for item in setups
                ],
            }
        )
    return rows


def _date(ts: str) -> str:
    return datetime.fromtimestamp(int(ts) / 1000, UTC).strftime("%Y-%m-%d %H:%M")


# --------------------------------------------------------------------- B1 full
#
# Structural diagnosis, not an edge measurement: with ~44 episodes the confidence
# interval for win rate is ±15 p.p. The goal is to answer "is the rule
# structurally broken", not "does it make money".

# Costs per transaction side (fraction of notional). OKX spot taker 0.10%,
# spread and slippage vary with pair liquidity.
COSTS: dict[str, dict[str, float]] = {
    "BTC-USDT": {"taker": 0.0010, "spread": 0.0002, "slippage": 0.0005},
    "ETH-USDT": {"taker": 0.0010, "spread": 0.0002, "slippage": 0.0005},
    "SOL-USDT": {"taker": 0.0010, "spread": 0.0005, "slippage": 0.0005},
}
POSITION_USDT = 15.0  # 5% of 300 USDT capital — the binding limit from risk_limits.yaml

IS_END = "2024-05-01"
ROTATION_START = "2022-07-01"


def _cluster_episodes(setups: list[dict[str, Any]], *, gap_hours: int = 48) -> list[dict[str, Any]]:
    """Groups setups into episodes: the next signal <= gap_hours after the previous one is the same move."""
    episodes: list[list[dict[str, Any]]] = []
    for item in setups:
        ts = datetime.fromtimestamp(int(item["ts"]) / 1000, UTC)
        if episodes:
            last_ts = datetime.fromtimestamp(int(episodes[-1][-1]["ts"]) / 1000, UTC)
            if ts - last_ts <= timedelta(hours=gap_hours):
                episodes[-1].append(item)
                continue
        episodes.append([item])
    return [{"entry_signal": ep[0], "signals_in_episode": len(ep)} for ep in episodes]


def simulate_trade(
    candles: list[dict[str, Any]],
    *,
    signal_index: int,
    tp_pct: float,
    sl_pct: float,
    costs: dict[str, float],
) -> dict[str, Any] | None:
    """Simulates one position: entry at open after the signal, exit at TP/SL.

    - Entry: `open` of the candle following the signal (the signal is visible
      only after the close).
    - TP and SL in the same candle -> SL (order within the bar is unknown).
    - No exit by the end of data -> close at the last close.
    """
    entry_index = signal_index + 1
    if entry_index >= len(candles):
        return None
    entry_price = float(candles[entry_index]["open"])
    tp_price = entry_price * (1 + tp_pct / 100.0)
    sl_price = entry_price * (1 - sl_pct / 100.0)

    exit_price: float | None = None
    exit_index = len(candles) - 1
    reason = "end_of_data"
    ambiguous = False
    for index in range(entry_index, len(candles)):
        high = float(candles[index]["high"])
        low = float(candles[index]["low"])
        hit_tp = high >= tp_price
        hit_sl = low <= sl_price
        if hit_tp and hit_sl:
            # Without tick data there is no way to know which came first. We flag
            # the case so the report can show how sensitive the result is.
            ambiguous = True
        if hit_sl:  # conservatively: SL takes precedence
            exit_price, exit_index, reason = sl_price, index, "sl"
            break
        if hit_tp:
            exit_price, exit_index, reason = tp_price, index, "tp"
            break
    if exit_price is None:
        exit_price = float(candles[-1]["close"])

    gross_pct = (exit_price - entry_price) / entry_price * 100.0
    cost_pct = 2 * (costs["taker"] + costs["spread"] + costs["slippage"]) * 100.0
    net_pct = gross_pct - cost_pct
    return {
        "entry_ts": _date(candles[entry_index]["ts"]),
        "exit_ts": _date(candles[exit_index]["ts"]),
        "sl_pct": sl_pct,
        "gross_pct": gross_pct,
        "net_pct": net_pct,
        "gross_usdt": gross_pct / 100.0 * POSITION_USDT,
        "net_usdt": net_pct / 100.0 * POSITION_USDT,
        "reason": reason,
        "ambiguous": ambiguous,
        "bars_held": exit_index - entry_index + 1,
    }


def _stats(trades: list[dict[str, Any]], key: str) -> dict[str, float]:
    """Aggregated metrics based on the chosen profile (`net_pct`/`gross_pct`)."""
    if not trades:
        return {
            "n": 0,
            "win_rate": 0.0,
            "avg_win": 0.0,
            "avg_loss": 0.0,
            "profit_factor": 0.0,
            "total_pct": 0.0,
            "max_dd_pct": 0.0,
            "sharpe": 0.0,
        }
    wins = [t[key] for t in trades if t[key] > 0]
    losses = [t[key] for t in trades if t[key] <= 0]
    gross_win = sum(wins)
    gross_loss = abs(sum(losses))
    equity = 0.0
    peak = 0.0
    max_dd = 0.0
    for trade in trades:
        equity += trade[key]
        peak = max(peak, equity)
        max_dd = max(max_dd, peak - equity)
    returns = [t[key] for t in trades]
    mean = sum(returns) / len(returns)
    variance = sum((r - mean) ** 2 for r in returns) / len(returns) if len(returns) > 1 else 0.0
    stdev = variance**0.5
    # Annualised Sharpe: trades are rare, so we scale by the number of trades/year.
    years = 4.35
    trades_per_year = len(trades) / years
    sharpe = (mean / stdev * (trades_per_year**0.5)) if stdev else 0.0
    return {
        "n": len(trades),
        "win_rate": len(wins) / len(trades) * 100.0,
        "avg_win": sum(wins) / len(wins) if wins else 0.0,
        "avg_loss": sum(losses) / len(losses) if losses else 0.0,
        "profit_factor": (gross_win / gross_loss) if gross_loss else float("inf"),
        "total_pct": sum(returns),
        "max_dd_pct": max_dd,
        "sharpe": sharpe,
    }


def buy_and_hold(candles: list[dict[str, Any]], *, start_ts: str | None, costs: dict[str, float]) -> float:
    """Buy-and-hold return in %, after entry and exit costs."""
    subset = [c for c in candles if start_ts is None or c["ts"] >= start_ts]
    if len(subset) < 2:
        return 0.0
    first = float(subset[0]["open"])
    last = float(subset[-1]["close"])
    gross = (last - first) / first * 100.0
    cost = 2 * (costs["taker"] + costs["spread"] + costs["slippage"]) * 100.0
    return gross - cost


def run_full(
    candles_by_pair: dict[str, list[dict[str, Any]]], *, min_index: int, rsi_entry: float, aggressive: bool
) -> dict[str, Any]:
    """Full run for a single RSI threshold: episodes, trades, metrics."""
    per_pair: list[dict[str, Any]] = []
    all_trades: list[dict[str, Any]] = []
    for pair, candles in candles_by_pair.items():
        costs = COSTS[pair]
        series = evaluate_series(candles, min_index=min_index, rsi_entry=rsi_entry)
        setups = [item for item in series if is_entry(item["snapshot"])]
        episodes = _cluster_episodes(setups)
        trades: list[dict[str, Any]] = []
        if aggressive:
            # "Production-like" variant: every signal is a separate entry
            # (the 10 s cooldown and max_open_positions=0 do not block later ones).
            for item in setups:
                trade = simulate_trade(
                    candles,
                    signal_index=item["index"],
                    tp_pct=item["snapshot"]["suggested_tp_pct"],
                    sl_pct=item["snapshot"]["suggested_sl_pct"],
                    costs=costs,
                )
                if trade:
                    trade["pair"] = pair
                    trades.append(trade)
        else:
            for episode in episodes:
                item = episode["entry_signal"]
                trade = simulate_trade(
                    candles,
                    signal_index=item["index"],
                    tp_pct=item["snapshot"]["suggested_tp_pct"],
                    sl_pct=item["snapshot"]["suggested_sl_pct"],
                    costs=costs,
                )
                if trade:
                    trade["pair"] = pair
                    trade["signals_in_episode"] = episode["signals_in_episode"]
                    trades.append(trade)
        all_trades.extend(trades)
        per_pair.append(
            {
                "pair": pair,
                "setups": len(setups),
                "episodes": len(episodes),
                "trades": trades,
                "net": _stats(trades, "net_pct"),
                "gross": _stats(trades, "gross_pct"),
                "buy_hold_pct": buy_and_hold(candles, start_ts=None, costs=costs),
            }
        )
    return {
        "per_pair": per_pair,
        "all_trades": all_trades,
        "net": _stats(all_trades, "net_pct"),
        "gross": _stats(all_trades, "gross_pct"),
    }


def print_frequency(rows: list[dict[str, Any]]) -> None:
    print(f"{'pair':10} {'candles':>7} {'from':10} {'to':10} {'setups':>7}  per year")
    for row in rows:
        years = ", ".join(f"{year}:{count}" for year, count in row["per_year"].items())
        print(
            f"{row['pair']:10} {row['candles']:>7} {str(row['from'])[:10]:10} "
            f"{str(row['to'])[:10]:10} {row['setups']:>7}  {years}"
        )
    print()
    for row in rows:
        print(f"--- {row['pair']}: {row['setups']} setups")
        for item in row["setups_detail"]:
            print(
                f"  {item['ts']}  RSI={item['rsi']:<6} ADX={item['adx']:<6} "
                f"vol={item['vol_ratio']:<7} regime={item['regime']}"
            )


def print_full(title: str, result: dict[str, Any]) -> None:
    """B1 report: per pair, with vs. without costs, R distribution, IS/OOS."""
    net, gross = result["net"], result["gross"]
    print(f"\n{'=' * 78}\n{title}\n{'=' * 78}")
    print(
        f"{'pair':10} {'setups':>7} {'epis.':>6} {'trades':>7} {'WR%':>6} {'PF':>6} "
        f"{'tot%':>8} {'maxDD%':>7} {'B&H%':>9}"
    )
    for row in result["per_pair"]:
        s = row["net"]
        print(
            f"{row['pair']:10} {row['setups']:>7} {row['episodes']:>6} {s['n']:>7} "
            f"{s['win_rate']:>6.1f} {s['profit_factor']:>6.2f} {s['total_pct']:>8.2f} "
            f"{s['max_dd_pct']:>7.2f} {row['buy_hold_pct']:>9.2f}"
        )
    print(
        f"{'TOTAL':10} {'':>7} {'':>6} {net['n']:>7} {net['win_rate']:>6.1f} "
        f"{net['profit_factor']:>6.2f} {net['total_pct']:>8.2f} {net['max_dd_pct']:>7.2f}"
    )
    print(
        f"\ncosts vs. no costs: PF without = {gross['profit_factor']:.2f}, "
        f"PF with = {net['profit_factor']:.2f}, difference = {gross['profit_factor'] - net['profit_factor']:.2f}"
    )
    print(
        f"  tot% without costs = {gross['total_pct']:.2f} | with costs = {net['total_pct']:.2f} "
        f"| Sharpe (net) = {net['sharpe']:.2f}"
    )
    print(f"  avg win = {net['avg_win']:.2f}% | avg loss = {net['avg_loss']:.2f}%")

    # R = net_pct / sl_pct of the trade (risk defined per trade by ATR).
    buckets: dict[str, int] = {}
    for trade in result["all_trades"]:
        risk = trade.get("sl_pct") or 0.0
        r = trade["net_pct"] / risk if risk else 0.0
        label = (
            ">=+2R" if r >= 2 else "+1..2R" if r >= 1 else "0..+1R" if r > 0
            else "-1R" if r > -1.01 else "-1..-2R" if r > -2 else "<=-2R"
        )
        buckets[label] = buckets.get(label, 0) + 1
    order = [">=+2R", "+1..2R", "0..+1R", "-1R", "-1..-2R", "<=-2R"]
    print("  net distribution in R (R = net% / sl%): " + ", ".join(f"{k}:{buckets.get(k, 0)}" for k in order))

    reasons: dict[str, int] = {}
    for trade in result["all_trades"]:
        reasons[trade["reason"]] = reasons.get(trade["reason"], 0) + 1
    print(f"  exits: {reasons}")

    # How many trades touched TP and SL in the same candle? A high share means
    # the result depends on the ordering assumption, not on the strategy.
    ambiguous = [t for t in result["all_trades"] if t.get("ambiguous")]
    if result["all_trades"]:
        share = len(ambiguous) / len(result["all_trades"]) * 100
        print(
            f"  ambiguous (TP and SL in the same candle): {len(ambiguous)}/"
            f"{len(result['all_trades'])} = {share:.0f}%"
        )
    if ambiguous:
        # Optimistic variant for comparison only: the same trades
        # closed at TP (TP = 2x SL, ATR multiplier 3.0/1.5) instead of at SL.
        base = result["net"]["total_pct"]
        adjusted = base + sum(t["sl_pct"] * 2 - t["net_pct"] for t in ambiguous)
        print(f"    if all had closed at TP: tot% from {base:.1f} to {adjusted:.1f}")


# ------------------------------------------------------------ regime rotation B1
#
# Portfolio backtest: one market filter, three legs bought and sold together.
# Unlike the mean-reversion run, capital is shared — equity is compounded
# through realised P&L, so costs and drawdown are measured at portfolio level,
# not summed per pair.

PORTFOLIO_USDT = 300.0  # starting equity, same scale as risk_limits.yaml
BASKET_WEIGHT = 1.0 / 3.0  # capital share per pair at entry
# Spec of the hypothesis: 0.10% taker and 0.05% slippage per side. Spread is
# left at zero so the result reflects exactly the stated model.
ROTATION_COSTS: dict[str, float] = {"taker": 0.0010, "slippage": 0.0005, "spread": 0.0}
ROTATION_COST_PER_SIDE = sum(ROTATION_COSTS.values())


@dataclass
class Holding:
    """One basket leg: capital committed and quantity held."""

    pair: str
    invest_usdt: float
    entry_price: float
    qty: float
    entry_ts: str


def _aligned_series(
    candles_by_pair: dict[str, list[dict[str, Any]]],
) -> tuple[list[str], dict[str, dict[str, float]]]:
    """Aligns candles on timestamps shared by every pair.

    Returns (timestamps, {pair: {ts: close}}). Only bars present in all pairs
    are used, so a basket entry always happens on the same bar for every leg.
    """
    per_pair: dict[str, dict[str, float]] = {}
    for pair, candles in candles_by_pair.items():
        per_pair[pair] = {str(c["ts"]): float(c["close"]) for c in candles}
    common = set.intersection(*(set(series) for series in per_pair.values()))
    timestamps = sorted(common, key=int)
    return timestamps, per_pair


def simulate_rotation(
    candles_by_pair: dict[str, list[dict[str, Any]]],
    *,
    ma_period: int = REGIME_ROTATION_MA_PERIOD,
    dead_band: float = 0.0,
    initial_usdt: float = PORTFOLIO_USDT,
    cost_per_side: float = ROTATION_COST_PER_SIDE,
    start_ts: int | None = None,
    end_ts: int | None = None,
) -> dict[str, Any]:
    """Buy the basket when BTC turns Risk-On, sell 100% when it turns Risk-Off.

    Order within the candle: exit first, then entry. The candle on which the
    regime changes is the signal; the trade executes at its `close` — the signal
    is visible only after the candle closes, so the result does not assume
    foresight (production would do at most the same at that close).

    `start_ts`/`end_ts` limit **trading and measurement**, but not the SMA
    warm-up: history before `start_ts` still feeds the average, so the first
    measured bar has the full filter. Without that, the IS/OOS result would start
    from nonsense. The regime before the window is unknown beyond the data, so
    `prev_regime` starts as None and the first verdict in the window opens a
    position according to the regime — an assumption "we enter when the window
    starts in Risk-On", not a historical signal.

    Capital: 1/3 share per pair, unallocated capital (e.g. after Risk-Off) stays
    in cash. P&L is realised on exit; equity valuation between entry and exit is
    computed at current closes.
    """
    if "BTC-USDT" not in candles_by_pair:
        raise ValueError("portfolio backtest requires BTC-USDT (the market filter)")
    pairs = sorted(candles_by_pair)
    timestamps, closes = _aligned_series(candles_by_pair)
    btc_closes = [closes["BTC-USDT"][ts] for ts in timestamps]

    cash = initial_usdt
    holdings: dict[str, Holding] = {}
    trades: list[dict[str, Any]] = []
    equity_curve: list[tuple[str, float]] = []
    prev_regime: str | None = None

    def close_basket(ts: str, reason: str) -> None:
        nonlocal cash
        if not holdings:
            return
        for pair, holding in holdings.items():
            exit_price = closes[pair][ts]
            gross = holding.qty * exit_price
            fee = gross * cost_per_side
            net = gross - fee
            cost_basis = holding.invest_usdt * (1.0 + cost_per_side)
            pnl = net - cost_basis
            trades.append(
                {
                    "pair": pair,
                    "entry_ts": holding.entry_ts,
                    "exit_ts": _date(ts),
                    "entry_price": holding.entry_price,
                    "exit_price": exit_price,
                    "invest_usdt": holding.invest_usdt,
                    "pnl_usdt": pnl,
                    "gross_pct": (exit_price / holding.entry_price - 1.0) * 100.0,
                    "net_pct": pnl / cost_basis * 100.0,
                    "exit_reason": reason,
                }
            )
            cash += net
        holdings.clear()

    def open_basket(ts: str) -> None:
        nonlocal cash
        # Equal capital shares per pair. The fee is taken out of the share, so
        # every leg gets the same notional instead of the first legs starving
        # the last one by a rounding of their fees.
        per_leg = cash * BASKET_WEIGHT
        for pair in pairs:
            price = closes[pair][ts]
            spend = min(per_leg, cash) / (1.0 + cost_per_side)
            fee = spend * cost_per_side
            cash -= spend + fee
            holdings[pair] = Holding(
                pair=pair,
                invest_usdt=spend,
                entry_price=price,
                qty=spend / price,
                entry_ts=_date(ts),
            )

    for index, ts in enumerate(timestamps):
        if start_ts is not None and int(ts) < start_ts:
            continue  # warm-up: feeds the SMA, does not trade or measure
        # At least ma_period closes are needed for the SMA; evaluate on the
        # history up to and including this bar.
        if index + 1 < ma_period:
            # No filter yet: measure the untouched starting capital. (Only
            # reachable without `start_ts`, which normally covers the warm-up.)
            equity_curve.append((ts, cash))
            continue
        if end_ts is not None and int(ts) > end_ts:
            break
        window = btc_closes[: index + 1]
        verdict = evaluate_regime_rotation(
            window, RegimeRotationConfig(ma_period=ma_period, dead_band=dead_band)
        )
        regime = verdict["regime"]
        if regime is not None and regime != prev_regime:
            if regime == "off":
                close_basket(ts, "regime_off")
            elif regime == "on":
                open_basket(ts)
            prev_regime = regime
        held = sum(h.qty * closes[h.pair][ts] for h in holdings.values())
        equity_curve.append((ts, cash + held))

    # Close any open basket at the last measured bar so metrics include
    # unrealised P&L — including when the window ended before the data did.
    if holdings and equity_curve:
        last_ts = equity_curve[-1][0]
        close_basket(last_ts, "end_of_data")
        equity_curve[-1] = (last_ts, cash)
    return {
        "trades": trades,
        "equity_curve": equity_curve,
        "initial_usdt": initial_usdt,
        "final_usdt": cash,
    }


def _drawdown_from_curve(curve: list[tuple[str, float]]) -> tuple[float, float]:
    """Returns (max drawdown %, max drawdown USDT) of an equity curve."""
    peak = curve[0][1] if curve else 0.0
    max_dd = 0.0
    for _, value in curve:
        peak = max(peak, value)
        max_dd = max(max_dd, peak - value)
    start_peak = max(value for _, value in curve) if curve else 0.0
    max_dd_pct = (max_dd / start_peak * 100.0) if start_peak else 0.0
    return max_dd_pct, max_dd


def rotation_stats(result: dict[str, Any]) -> dict[str, Any]:
    """Portfolio-level metrics for one rotation run."""
    trades = result["trades"]
    curve = result["equity_curve"]
    initial = result["initial_usdt"]
    final = result["final_usdt"]
    wins = [t for t in trades if t["pnl_usdt"] > 0]
    losses = [t for t in trades if t["pnl_usdt"] <= 0]
    gross_win = sum(t["pnl_usdt"] for t in wins)
    gross_loss = abs(sum(t["pnl_usdt"] for t in losses))
    # "gross" profile: same trades without costs, to match the report fields of
    # the mean-reversion run.
    gross_win_nc = sum(
        t["invest_usdt"] * (t["exit_price"] / t["entry_price"] - 1.0)
        for t in trades
        if t["exit_price"] > t["entry_price"]
    )
    gross_loss_nc = abs(
        sum(
            t["invest_usdt"] * (t["exit_price"] / t["entry_price"] - 1.0)
            for t in trades
            if t["exit_price"] <= t["entry_price"]
        )
    )
    max_dd_pct, max_dd_usdt = _drawdown_from_curve(curve)
    return {
        "n": len(trades),
        "entries": len({t["entry_ts"] for t in trades}),
        "exits": len({t["exit_ts"] for t in trades}),
        "win_rate": len(wins) / len(trades) * 100.0 if trades else 0.0,
        "profit_factor": (gross_win / gross_loss) if gross_loss else float("inf"),
        "profit_factor_gross": (gross_win_nc / gross_loss_nc) if gross_loss_nc else float("inf"),
        "final_usdt": final,
        "total_pct": (final / initial - 1.0) * 100.0,
        "max_dd_pct": max_dd_pct,
        "max_dd_usdt": max_dd_usdt,
    }


def print_rotation(title: str, result: dict[str, Any]) -> dict[str, Any]:
    stats = rotation_stats(result)
    print(f"\n{'=' * 78}\n{title}\n{'=' * 78}")
    print(
        f"capital {result['initial_usdt']:.2f} → {result['final_usdt']:.2f} USDT "
        f"({stats['total_pct']:+.2f}%)"
    )
    print(
        f"trades (legs): {stats['n']}  basket entries: {stats['entries']}  "
        f"basket exits: {stats['exits']}"
    )
    print(
        f"win rate: {stats['win_rate']:.1f}%  PF net: {stats['profit_factor']:.2f}  "
        f"PF gross: {stats['profit_factor_gross']:.2f}"
    )
    print(
        f"max DD: {stats['max_dd_pct']:.2f}% ({stats['max_dd_usdt']:.2f} USDT)"
    )
    for trade in result["trades"]:
        print(
            f"  {trade['pair']:9} {trade['entry_ts']} → {trade['exit_ts']}  "
            f"{trade['net_pct']:+7.2f}%  {trade['pnl_usdt']:+8.2f} USDT  "
            f"({trade['exit_reason']})"
        )
    return stats


def _ms(date: str) -> int:
    return int(datetime.strptime(date, "%Y-%m-%d").replace(tzinfo=UTC).timestamp() * 1000)


def rotation_report(
    candles_by_pair: dict[str, list[dict[str, Any]]], *, ma_period: int
) -> dict[str, Any]:
    """Full window + IS/OOS split, all three on the same start/end dates.

    The windows share their boundaries with the reported dates: full is
    START → end of data, IS is START → CUT, OOS is CUT → end of data. Every
    run gets the pre-START history as SMA warm-up, so no window starts blind.
    """
    start = _ms(ROTATION_START)
    cut = _ms(IS_END)
    last = int(candles_by_pair["BTC-USDT"][-1]["ts"])
    windows = [
        (f"REGIME ROTATION — FULL PERIOD {ROTATION_START} → {_date(str(last))[:10]}", start, last),
        (f"IN-SAMPLE {ROTATION_START} → {IS_END}", start, cut - 1),
        (f"OUT-OF-SAMPLE {IS_END} → {_date(str(last))[:10]}", cut, last),
    ]
    result: dict[str, Any] = {}
    for title, window_start, window_end in windows:
        result[title] = print_rotation(
            title,
            simulate_rotation(
                candles_by_pair, ma_period=ma_period, start_ts=window_start, end_ts=window_end
            ),
        )

    # Buy & hold BTC over exactly the measured window, same costs.
    btc = [c for c in candles_by_pair.get("BTC-USDT", []) if int(c["ts"]) >= start]
    bh = buy_and_hold(btc, start_ts=None, costs=ROTATION_COSTS)
    print(
        f"\nBuy & Hold BTC ({ROTATION_START} → end of data, same costs): {bh:+.2f}%  "
        f"→ {PORTFOLIO_USDT * (1 + bh / 100.0):.2f} USDT from {PORTFOLIO_USDT:.2f}"
    )
    return result


# ------------------------------------------------------- walk-forward (B1.5)
#
# Dead band 0.5% was noticed, not derived: it was spotted as the best value on
# the 2022-07 -> 2026-11 window it would then be judged on. A single-window
# result cannot separate an edge from a lucky period, so the band is tested on
# rolling windows instead. Each variant gets the same warm-up history and the
# same window; only the band differs.

def _shift_months(date: datetime, months: int) -> datetime:
    month_index = date.month - 1 + months
    year = date.year + month_index // 12
    month = month_index % 12 + 1
    return date.replace(year=year, month=month, day=1)


def walk_forward_windows(
    first_start: str, *, years: int = 1, step_months: int = 3, last_start: str | None = None
) -> list[tuple[str, str]]:
    """Rolling (start, end) windows shifted by `step_months` over `years` each.

    A window always starts on the 1st, so `2022-07` means 2022-07-01. The last
    window starts on or before `last_start`; a later start cannot exist.
    """
    cursor = datetime.strptime(first_start, "%Y-%m").replace(tzinfo=UTC)
    limit = (
        datetime.strptime(last_start, "%Y-%m").replace(tzinfo=UTC)
        if last_start
        else cursor
    )
    windows: list[tuple[str, str]] = []
    while cursor <= limit:
        end = _shift_months(cursor, years * 12) - timedelta(days=1)
        windows.append((cursor.strftime("%Y-%m-%d"), end.strftime("%Y-%m-%d")))
        cursor = _shift_months(cursor, step_months)
    return windows


def _window_stats(
    candles_by_pair: dict[str, list[dict[str, Any]]],
    *,
    start: str,
    end: str,
    ma_period: int,
    dead_bands: tuple[float, ...],
) -> dict[Any, dict[str, Any]]:
    """Runs every band variant over the same window; plus BTC buy-and-hold."""
    start_ms, end_ms = _ms(start), _ms(end)
    out: dict[float, dict[str, Any]] = {}
    for band in dead_bands:
        result = simulate_rotation(
            candles_by_pair, ma_period=ma_period, dead_band=band,
            start_ts=start_ms, end_ts=end_ms,
        )
        out[band] = rotation_stats(result)
    btc = [c for c in candles_by_pair.get("BTC-USDT", []) if start_ms <= int(c["ts"]) <= end_ms]
    out["bh"] = {"total_pct": buy_and_hold(btc, start_ts=None, costs=ROTATION_COSTS)}
    return out


def walk_forward_report(
    candles_by_pair: dict[str, list[dict[str, Any]]],
    *,
    ma_period: int,
    dead_bands: tuple[float, ...] = (0.0, 0.0025, 0.005, 0.01),
    first_start: str = "2022-07",
    last_start: str | None = None,
) -> list[dict[str, Any]]:
    """Year-by-year walk-forward: does a band help in every window, or one?

    Only fully covered 12-month windows are used: a window clipped by the end
    of data would compare a partial period against a full one.
    """
    last_ts = int(candles_by_pair["BTC-USDT"][-1]["ts"])
    last_date = datetime.fromtimestamp(last_ts / 1000, UTC)
    if last_start is None:
        # Latest window that still fits a full year before the data ends,
        # snapped down to a month so the 3-month grid keeps its alignment.
        last_start = _shift_months(last_date.replace(day=1), -12).strftime("%Y-%m")
    windows = walk_forward_windows(first_start, years=1, step_months=3, last_start=last_start)
    windows = [
        (start, end)
        for start, end in windows
        if datetime.strptime(end, "%Y-%m-%d").replace(tzinfo=UTC) <= last_date
    ]
    if not windows:
        print("\nWALK-FORWARD: no full window in the data — not enough history.", file=sys.stderr)
        return []
    rows: list[dict[str, Any]] = []
    for start, end in windows:
        stats = _window_stats(
            candles_by_pair, start=start, end=end, ma_period=ma_period, dead_bands=dead_bands
        )
        rows.append({"start": start, "end": end, **stats})

    labels = ", ".join(f"{band * 100:.2f}%" for band in dead_bands)
    print(f"\n{'=' * 100}\nWALK-FORWARD — 12-month windows, 3-month step\n{'=' * 100}")
    print(f"N=1 window: {len(rows)}. Variants (dead band): {labels} + BTC B&H. Result = net return %.")
    header = f"{'from':10} {'to':10} {'B&H':>8}"
    for band in dead_bands:
        header += f" {'band ' + f'{band * 100:.2f}%':>12} {'PF':>5} {'DD%':>6} {'#ent':>4}"
    print(header)
    for row in rows:
        line = f"{row['start']:10} {row['end']:10} {row['bh']['total_pct']:>8.1f}"
        for band in dead_bands:
            s = row[band]
            line += (
                f" {s['total_pct']:>12.1f} {s['profit_factor']:>5.2f}"
                f" {s['max_dd_pct']:>6.1f} {s['entries']:>4}"
            )
        print(line)

    if rows:
        print_dominance(rows, dead_bands)
    return rows


def print_dominance(rows: list[dict[str, Any]], dead_bands: tuple[float, ...]) -> None:
    """Counts how often each variant beats the others and buy-and-hold."""
    print("\nSummary (how many windows won):")
    print(f"{'variant':12} {'> B&H':>7} {'> band 0':>9} {'mean return':>13} {'median':>9} {'min':>8} {'max':>8}")
    for band in dead_bands:
        returns = sorted(row[band]["total_pct"] for row in rows)
        beats_bh = sum(1 for row in rows if row[band]["total_pct"] > row["bh"]["total_pct"])
        beats_zero = (
            sum(1 for row in rows if row[band]["total_pct"] > row[0.0]["total_pct"])
            if band != 0.0
            else len(rows)
        )
        median = returns[len(returns) // 2]
        label = f"{band * 100:.2f}%"
        print(
            f"{label:12} {beats_bh:>4}/{len(rows):<2} {beats_zero:>6}/{len(rows):<2}"
            f" {sum(returns) / len(returns):>12.1f}% {median:>8.1f}%"
            f" {min(returns):>7.1f}% {max(returns):>7.1f}%"
        )
    bh = sorted(row["bh"]["total_pct"] for row in rows)
    beats = sum(1 for row in rows if row[0.0]["total_pct"] > row["bh"]["total_pct"])
    print(
        f"{'B&H BTC':12} {'—':>7} {'—':>9} {sum(bh) / len(bh):>12.1f}%"
        f" {bh[len(bh) // 2]:>8.1f}% {min(bh):>7.1f}% {max(bh):>7.1f}%"
        f"   (baseline band 0 beats B&H in {beats}/{len(rows)} windows)"
    )


def print_split(candles_by_pair: dict[str, list[dict[str, Any]]], *, min_index: int, rsi_entry: float) -> None:
    """IS/OOS — the same rule, two disjoint periods (parameters not optimised)."""
    cut_ms = int(datetime.strptime(IS_END, "%Y-%m-%d").replace(tzinfo=UTC).timestamp() * 1000)

    def at_cut(pair: str, before: bool) -> list[dict[str, Any]]:
        # `ts` is milliseconds as a string — the comparison must be numeric.
        return [c for c in candles_by_pair[pair] if (int(c["ts"]) < cut_ms) == before]

    for label, before in (("IN-SAMPLE 2022-07 → 2024-05", True), ("OUT-OF-SAMPLE 2024-05 → 2026-11", False)):
        subset = {pair: at_cut(pair, before) for pair in candles_by_pair}
        subset = {pair: cs for pair, cs in subset.items() if len(cs) > min_index + 5}
        if not subset:
            print(f"\n{label}: not enough data")
            continue
        result = run_full(subset, min_index=min_index, rsi_entry=rsi_entry, aggressive=False)
        print_full(label, result)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Backtest of the strategy rule on OKX history")
    parser.add_argument("--pairs", default=DEFAULT_PAIRS, help="comma-separated pairs")
    parser.add_argument("--max-candles", type=int, default=10_000, help="history limit per pair")
    parser.add_argument(
        "--market-flag",
        default="0",
        choices=("0", "1"),
        help="Candle source: 0 = public history (real), 1 = demo (distorted)",
    )
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--frequency", action="store_true", help="setup frequency only")
    mode.add_argument("--full", action="store_true", help="full B1 backtest + RSI<=40 diagnostic")
    mode.add_argument(
        "--regime-rotation",
        action="store_true",
        help="portfolio backtest of the Regime Rotation hypothesis (BTC vs SMA(100), 1/3 basket)",
    )
    mode.add_argument(
        "--walk-forward",
        action="store_true",
        help="WFA B1.5: 12-month windows with a 3-month step, dead-band variants vs B&H",
    )
    parser.add_argument(
        "--wfa-from",
        default="2022-07",
        help="first WFA window (YYYY-MM); earlier data is SMA warm-up only",
    )
    parser.add_argument(
        "--wfa-to",
        default=None,
        help="last WFA window (YYYY-MM); defaults to as far as the data covers",
    )
    parser.add_argument(
        "--dead-bands",
        default="0,0.0025,0.005,0.01",
        help="dead-band variants (fraction of price), comma-separated",
    )
    args = parser.parse_args(argv)

    # Regime Rotation needs only the SMA(100) history; the mean-reversion paths
    # still require MA200 and keep their own threshold.
    portfolio_mode = args.regime_rotation or args.walk_forward
    min_history = REGIME_ROTATION_MA_PERIOD + 10 if portfolio_mode else 210

    api = MarketData.MarketAPI(flag=args.market_flag)
    now_ms = int(datetime.now(UTC).timestamp() * 1000)
    pairs = [pair.strip().upper() for pair in args.pairs.split(",") if pair.strip()]
    candles_by_pair: dict[str, list[dict[str, Any]]] = {}
    for pair in pairs:
        candles = fetch_history(pair, max_candles=args.max_candles, api=api)
        closed = closed_candles(candles, now_ms=now_ms)
        if len(closed) <= min_history:
            print(f"{pair}: not enough history ({len(closed)} candles) — skipping", file=sys.stderr)
            continue
        candles_by_pair[pair] = closed
        print(f"{pair}: fetched {len(closed)} candles", file=sys.stderr)

    if portfolio_mode:
        if "BTC-USDT" not in candles_by_pair:
            print("Regime Rotation requires BTC-USDT (the market filter).", file=sys.stderr)
            return 1
    if args.regime_rotation:
        rotation_report(candles_by_pair, ma_period=REGIME_ROTATION_MA_PERIOD)
        return 0
    if args.walk_forward:
        bands = tuple(float(part) for part in args.dead_bands.split(",") if part.strip())
        walk_forward_report(
            candles_by_pair,
            ma_period=REGIME_ROTATION_MA_PERIOD,
            dead_bands=bands,
            first_start=args.wfa_from,
            last_start=args.wfa_to,
        )
        return 0

    if not args.full:
        rows = frequency_report(candles_by_pair, min_index=210)
        print_frequency(rows)
        return 0

    print(
        "\nB1 — STRUCTURAL DIAGNOSIS (not an edge measurement; with n≈44 the confidence "
        "interval for win rate is ±15 p.p.)"
    )
    baseline = run_full(candles_by_pair, min_index=210, rsi_entry=35.0, aggressive=False)
    print_full("BASELINE RSI<=35 (1 entry per episode)", baseline)
    print_split(candles_by_pair, min_index=210, rsi_entry=35.0)

    aggressive = run_full(candles_by_pair, min_index=210, rsi_entry=35.0, aggressive=True)
    print_full("PRODUCT-LIKE VARIANT: entry on EVERY signal (10 s cooldown does not block)", aggressive)

    diagnostic = run_full(candles_by_pair, min_index=210, rsi_entry=40.0, aggressive=False)
    print_full("RSI<=40 THRESHOLD DIAGNOSTIC (probe, NOT a deployment candidate)", diagnostic)
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
