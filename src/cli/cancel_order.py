"""CLI: cancel an order. `python -m cli.cancel_order --order-id ... --pair ...`."""

from __future__ import annotations

import argparse

import cli._http as http
from cli._output import emit, fail


def build_parser() -> argparse.ArgumentParser:
    """CLI arguments."""
    parser = argparse.ArgumentParser(description="Cancel a spot order (via the executor)")
    parser.add_argument("--order-id", required=True, help="ordId from OKX")
    parser.add_argument("--pair", required=True, help="order pair, e.g. BTC-USDT")
    return parser


def main(argv: list[str] | None = None) -> int:
    """Sends the cancellation to the executor."""
    args = build_parser().parse_args(argv)
    try:
        response = http.post("/cancel", {"order_id": args.order_id, "pair": args.pair.upper()})
    except http.ExecutorUnavailable as exc:
        return fail(str(exc), error_type="executor_unavailable", order_id=args.order_id)
    return emit(response)


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
