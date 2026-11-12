"""CLI: account balance (via the executor). `python -m cli.balance [--ccy USDT]`."""

from __future__ import annotations

import argparse

import cli._http as http
from cli._output import emit, fail


def build_parser() -> argparse.ArgumentParser:
    """CLI arguments."""
    parser = argparse.ArgumentParser(description="OKX account balance (via the executor)")
    parser.add_argument("--ccy", default="USDT", help="currency (default USDT)")
    return parser


def main(argv: list[str] | None = None) -> int:
    """Fetches the account state and extracts the balances."""
    args = build_parser().parse_args(argv)
    try:
        response = http.get("/state")
    except http.ExecutorUnavailable as exc:
        return fail(str(exc), error_type="executor_unavailable")
    balances = response.get("balances", {})
    if args.ccy and args.ccy not in balances:
        return emit({"ok": True, "ccy": args.ccy, "available": 0.0, "balances": balances})
    return emit({"ok": True, "ccy": args.ccy, "available": balances.get(args.ccy, 0.0), "balances": balances})


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
