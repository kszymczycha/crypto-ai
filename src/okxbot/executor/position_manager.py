"""Position management: state reading, valuation, and closing.

The scope is deliberately minimal: positions are calculated from buy orders in
`state.db` and valued at the current OKX price. There is no automatic scaling or
"average down" — the risk policy forbids it (see docs/risk_policy.md).
"""

from __future__ import annotations

from typing import Any

from okxbot.client.okx_client import OkxApiError, OkxClient
from okxbot.config import Symbol
from okxbot.logging import setup_logging
from okxbot.storage.db import StateStore, utc_now_iso


class PositionManager:
    """Builds the position list, calculates exposure and unrealized P&L."""

    def __init__(
        self,
        store: StateStore,
        client: OkxClient | None = None,
        *,
        symbols: dict[str, Symbol] | None = None,
    ) -> None:
        """`client` is optional — without it, valuation uses the last database snapshot."""
        self.store = store
        self.client = client
        self.symbols = symbols or {}
        self._log = setup_logging("positions")

    def _last_price(self, pair: str, snapshots: dict[str, float]) -> float:
        if pair in snapshots:
            return snapshots[pair]
        row = self.store.latest_snapshot(pair)
        return float(row["price"]) if row else 0.0

    def open_positions(self) -> list[dict[str, Any]]:
        """Positions resulting from unfinished buy orders."""
        orders = self.store.open_orders()
        prices: dict[str, float] = {}
        if self.client is not None:
            pairs = sorted({order["pair"] for order in orders})
            if pairs:
                try:
                    prices = {
                        pair: float(data.get("price") or 0.0)
                        for pair, data in self.client.get_tickers(pairs).items()
                    }
                except OkxApiError as exc:
                    self._log.warning("position_pricing_failed", error=str(exc))

        grouped: dict[str, dict[str, Any]] = {}
        for order in orders:
            if order.get("side") != "buy":
                continue
            pair = str(order["pair"])
            entry = float(order.get("avg_price") or order.get("price") or 0.0)
            amount = float(order.get("amount_base") or 0.0)
            invested = float(order.get("amount_usdt") or 0.0)
            bucket = grouped.setdefault(
                pair,
                {
                    "pair": pair,
                    "base_amount": 0.0,
                    "invested_usdt": 0.0,
                    "opened_at": order.get("created_at") or utc_now_iso(),
                },
            )
            bucket["base_amount"] += amount
            bucket["invested_usdt"] += invested
            bucket["_entry_notional"] = bucket.get("_entry_notional", 0.0) + entry * amount

        result: list[dict[str, Any]] = []
        for pair, bucket in grouped.items():
            base_amount = float(bucket["base_amount"])
            entry_notional = float(bucket.pop("_entry_notional", 0.0))
            avg_entry = entry_notional / base_amount if base_amount else 0.0
            price = self._last_price(pair, prices)
            bucket["avg_entry_price"] = round(avg_entry, 8)
            bucket["price"] = price
            bucket["unrealized_pnl_usdt"] = round((price - avg_entry) * base_amount, 4) if price else 0.0
            result.append(bucket)
        return result

    def summary(self) -> dict[str, Any]:
        """Short summary for `/state`: position count, exposure, unrealized P&L."""
        positions = self.open_positions()
        return {
            "count": len(positions),
            "exposure_usdt": round(sum(float(p["invested_usdt"]) for p in positions), 4),
            "unrealized_pnl_usdt": round(sum(float(p["unrealized_pnl_usdt"]) for p in positions), 4),
            "positions": positions,
        }
