"""CLI: account status and daily P&L from the executor. `python -m cli.status`."""

from __future__ import annotations

import cli._http as http
from cli._output import emit, fail


def main(argv: list[str] | None = None) -> int:
    """Fetches /state from the executor and prints JSON."""
    del argv
    try:
        response = http.get("/state")
    except http.ExecutorUnavailable as exc:
        return fail(str(exc), error_type="executor_unavailable")
    return emit(response)


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
