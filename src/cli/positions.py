"""CLI: open orders/positions (via the executor). `python -m cli.positions [--pair ...]`."""

from __future__ import annotations

import argparse

import cli._http as http
from cli._output import emit, fail


def build_parser() -> argparse.ArgumentParser:
    """CLI arguments."""
    parser = argparse.ArgumentParser(description="Open spot orders (via the executor)")
    parser.add_argument("--pair", default=None, help="pair filter, e.g. BTC-USDT")
    return parser


def main(argv: list[str] | None = None) -> int:
    """Filters open orders by pair (if provided)."""
    args = build_parser().parse_args(argv)
    try:
        response = http.get("/state")
    except http.ExecutorUnavailable as exc:
        return fail(str(exc), error_type="executor_unavailable")
    orders = response.get("pending_orders", [])
    if args.pair:
        wanted = args.pair.upper()
        orders = [order for order in orders if str(order.get("pair", "")).upper() == wanted]
    return emit({"ok": True, "count": len(orders), "positions": orders, "daily": response.get("daily", {})})


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
