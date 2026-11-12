"""CLI for OpenCode: every command returns exactly one JSON.

Commands: status, balance, positions, market_snapshot, signal, place_order, cancel_order.
Run as `python -m cli.<name>` from the `/app` directory (PYTHONPATH=/app/src).
"""

__all__ = ["balance", "cancel_order", "market_snapshot", "place_order", "positions", "signal", "status"]
