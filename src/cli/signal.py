"""CLI: sends a signal to the executor. `python -m cli.signal --pair ... --side buy ...`."""

from __future__ import annotations

import argparse
import uuid

import cli._http as http
from cli._output import emit, fail


def build_parser() -> argparse.ArgumentParser:
    """Defines the CLI arguments."""
    parser = argparse.ArgumentParser(description="Send a signal to the executor (risk validation on the bot side)")
    parser.add_argument("--pair", required=True, help="pair, e.g. BTC-USDT")
    parser.add_argument("--side", required=True, choices=["buy", "sell"], help="direction")
    parser.add_argument("--amount-usdt", required=True, type=float, help="amount in USDT")
    parser.add_argument("--reason", required=True, help="justification (1 sentence)")
    parser.add_argument("--tp-pct", type=float, default=None, help="take profit in %% from entry")
    parser.add_argument("--sl-pct", type=float, default=None, help="stop loss in %% from entry")
    parser.add_argument("--idempotency-key", default=None, help="idempotency key (uuid4 by default)")
    return parser


def main(argv: list[str] | None = None) -> int:
    """Sends the signal and prints the executor's response as JSON."""
    args = build_parser().parse_args(argv)
    payload = {
        "pair": args.pair.upper(),
        "side": args.side,
        "amount_usdt": args.amount_usdt,
        "reason": args.reason,
        "idempotency_key": args.idempotency_key or str(uuid.uuid4()),
    }
    if args.tp_pct is not None:
        payload["tp_pct"] = args.tp_pct
    if args.sl_pct is not None:
        payload["sl_pct"] = args.sl_pct
    try:
        response = http.post("/signal", payload)
    except http.ExecutorUnavailable as exc:
        return fail(str(exc), error_type="executor_unavailable", pair=args.pair.upper())
    return emit(response)


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
