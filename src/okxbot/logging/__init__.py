"""Structured (JSON) logging to logs/.

Public API: `setup_logging(name, stdout=False) -> StructuredLogger`, whose
methods take structural fields as kwargs (`log.info("event", pair="BTC-USDT")`).
"""

from okxbot.logging.setup import StructuredLogger, setup_logging

__all__ = ["StructuredLogger", "setup_logging"]
