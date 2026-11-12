"""Executor HTTP server (FastAPI).

The endpoint implementation lives in `okxbot.executor.service` (to avoid
defining the application twice and circular imports), and this module is the
stable entry point named in the project specification:

    python -m okxbot.executor.http_server

Exports `app` (the FastAPI instance) and `run()` to start the server.
"""

from __future__ import annotations

from okxbot.executor.service import app, main

__all__ = ["app", "main", "run"]


def run() -> None:
    """Starts the uvicorn server (alias of `main()` from `service`)."""
    main()


if __name__ == "__main__":  # pragma: no cover
    run()
