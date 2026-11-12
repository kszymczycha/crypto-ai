"""CLI: market snapshot (price, RSI, MA20, hint). `python -m cli.market_snapshot --pairs ...`.

Note: this CLI reads public data directly from OKX (without secrets), so it can
run in the `opencode` container — it does not need the executor.
"""

from __future__ import annotations

import argparse

from cli._output import emit, fail
from okxbot.data.market import MarketService
from okxbot.strategy.rules import evaluate


def build_parser() -> argparse.ArgumentParser:
    """CLI arguments."""
    parser = argparse.ArgumentParser(description="OKX market snapshot + indicators")
    parser.add_argument(
        "--pairs",
        default="BTC-USDT,ETH-USDT",
        help="comma-separated list of pairs",
    )
    parser.add_argument("--no-coingecko", action="store_true", help="skip the CoinGecko context")
    parser.add_argument(
        "--bar",
        default=None,
        help="candle horizon (e.g. 1m, 15m, 1H, 4H, 1D); by default from config/settings.yaml",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    """Computes the snapshot and appends the strategy evaluation (hint)."""
    args = build_parser().parse_args(argv)
    pairs = [pair.strip().upper() for pair in args.pairs.split(",") if pair.strip()]
    service = MarketService()
    try:
        if args.no_coingecko:
            snap = service.snapshot(pairs, bar=args.bar)
        else:
            snap = service.snapshot_with_context(pairs, bar=args.bar)
    except Exception as exc:
        return fail(str(exc), error_type="market_error", pairs=pairs)
    for pair, data in snap.items():
        data["evaluation"] = evaluate(data)
    return emit({"ok": True, "data": snap})


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
