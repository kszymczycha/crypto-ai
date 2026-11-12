"""CLI: order via the executor (with TP/SL). `python -m cli.place_order ...`.

A thin wrapper over `POST /signal` — all risk policy lives on the executor side.
For a sell, `--amount-usdt` is an indicative value, and the size in the base
currency is computed by the executor.
"""

from __future__ import annotations

import argparse
import uuid

import cli._http as http
from cli._output import emit, fail


def build_parser() -> argparse.ArgumentParser:
    """CLI arguments."""
    parser = argparse.ArgumentParser(description="Place a spot market order via the executor")
    parser.add_argument("--pair", required=True, help="pair, e.g. BTC-USDT")
    parser.add_argument("--side", required=True, choices=["buy", "sell"], help="direction")
    parser.add_argument("--amount-usdt", required=True, type=float, help="amount in USDT")
    parser.add_argument("--tp-pct", type=float, default=2.0, help="take profit %% (default 2)")
    parser.add_argument("--sl-pct", type=float, default=1.0, help="stop loss %% (default 1)")
    parser.add_argument("--reason", required=True, help="justification (1 sentence)")
    parser.add_argument("--idempotency-key", default=None, help="idempotency key (uuid4 by default)")
    return parser


def main(argv: list[str] | None = None) -> int:
    """Sends the order request and prints the executor's response."""
    args = build_parser().parse_args(argv)
    payload = {
        "pair": args.pair.upper(),
        "side": args.side,
        "amount_usdt": args.amount_usdt,
        "tp_pct": args.tp_pct,
        "sl_pct": args.sl_pct,
        "reason": args.reason,
        "idempotency_key": args.idempotency_key or str(uuid.uuid4()),
    }
    try:
        response = http.post("/signal", payload)
    except http.ExecutorUnavailable as exc:
        return fail(str(exc), error_type="executor_unavailable", pair=args.pair.upper())
    return emit(response)


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
