"""Order layer facade: `Orders` = risk validation + routing to OKX.

A thin layer over `RiskEngine` and `OrderRouter`, so the executor (HTTP) and
integration tests have one entry point independent of transport.
"""

from __future__ import annotations

from typing import Any

from okxbot.executor.order_router import OrderRouter
from okxbot.executor.risk_engine import RiskEngine, Signal, Verdict
from okxbot.logging import setup_logging


class Orders:
    """Combines the risk engine with the order router."""

    def __init__(self, risk: RiskEngine, router: OrderRouter) -> None:
        """Injects the risk engine and the router."""
        self.risk = risk
        self.router = router
        self._log = setup_logging("orders")

    def submit(self, signal: Signal) -> dict[str, Any]:
        """Runs the full flow: validation → (optionally) order.

        Returns a dict matching the `/signal` response shape — without raising
        exceptions on rejection (rejection is a valid business result).
        """
        verdict: Verdict = self.risk.validate(signal)
        if verdict.status == "rejected":
            self._log.warning("submit_rejected", pair=signal.pair, reason=verdict.reason)
            return {"ok": False, "status": "rejected", "order_id": "", "reason": verdict.reason}
        if verdict.status == "needs_confirmation":
            self._log.info("submit_needs_confirmation", pair=signal.pair, amount=signal.amount_usdt)
            return {
                "ok": True,
                "status": "needs_confirmation",
                "order_id": "",
                "reason": verdict.reason,
            }
        result = self.router.execute(signal, signal_id=0)
        return {
            "ok": True,
            "status": "placed",
            "order_id": result["order_id"],
            "state": result["state"],
            "filled": result["filled"],
            "avg_price": result["avg_price"],
            "tp_price": result["tp_price"],
            "sl_price": result["sl_price"],
            "reason": "",
        }
