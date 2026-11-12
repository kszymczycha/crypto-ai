"""Translates a validated signal into an OKX call and stores the result.

Separated from FastAPI so it can be tested without an HTTP server.
TP/SL: for a buy, calculated from the entry price (ask) and passed as trigger prices.
"""

from __future__ import annotations

from typing import Any

from okxbot.client.okx_client import OkxApiError, OkxClient
from okxbot.executor.risk_engine import Signal
from okxbot.logging import setup_logging
from okxbot.storage.db import StateStore, utc_now_iso

# Entry price per pair, in `kv` in state.db. Needed to calculate P&L on a sell —
# the `orders` table has no query for the last buy, and there is one position per
# pair (the strategy does not pyramid). The value is cleared after a sell.
ENTRY_PRICE_KEY = "entry_price:{pair}"


class OrderRouter:
    """Executes signals through OkxClient and persists state in SQLite."""

    def __init__(self, client: OkxClient, store: StateStore) -> None:
        """Injects the OKX client and the state store."""
        self.client = client
        self.store = store
        self._log = setup_logging("order_router")

    def _entry_price(self, pair: str, side: str) -> float:
        tickers = self.client.get_tickers([pair])
        ticker = tickers.get(pair, {})
        return float(ticker.get("ask" if side == "buy" else "bid") or ticker.get("price") or 0.0)

    def execute(self, signal: Signal, *, signal_id: int) -> dict[str, Any]:
        """Places a market order with TP/SL. Returns a normalized result."""
        entry = self._entry_price(signal.pair, signal.side)
        tp_price = sl_price = None
        if signal.side == "buy":
            if signal.tp_pct:
                tp_price = entry * (1 + signal.tp_pct / 100.0)
            if signal.sl_pct:
                sl_price = entry * (1 - signal.sl_pct / 100.0)
        else:
            if signal.tp_pct:
                tp_price = entry * (1 - signal.tp_pct / 100.0)
            if signal.sl_pct:
                sl_price = entry * (1 + signal.sl_pct / 100.0)

        amount_base = None
        if signal.side == "sell" and entry > 0:
            amount_base = signal.amount_usdt / entry

        try:
            order = self.client.place_order(
                pair=signal.pair,
                side=signal.side,
                amount_usdt=signal.amount_usdt if signal.side == "buy" else None,
                amount_base=amount_base,
                order_type="market",
                tp_price=round(tp_price, 8) if tp_price else None,
                sl_price=round(sl_price, 8) if sl_price else None,
                client_order_id=signal.idempotency_key,
            )
        except OkxApiError as exc:
            self.store.insert_order(
                {
                    "signal_id": signal_id,
                    "pair": signal.pair,
                    "side": signal.side,
                    "order_type": "market",
                    "amount_usdt": signal.amount_usdt,
                    "state": "failed",
                    "error": str(exc),
                }
            )
            self._log.error("order_failed", pair=signal.pair, code=exc.code, error=str(exc))
            raise

        self.store.insert_order(
            {
                "signal_id": signal_id,
                "order_id": order.get("order_id"),
                "client_order_id": order.get("client_order_id"),
                "pair": signal.pair,
                "side": signal.side,
                "order_type": "market",
                "amount_usdt": signal.amount_usdt,
                "amount_base": amount_base,
                "tp_price": tp_price,
                "sl_price": sl_price,
                "state": order.get("state", "live"),
                "filled": order.get("filled", 0.0),
                "avg_price": order.get("avg_price", 0.0),
            }
        )
        self._record_fill(order, signal)
        self._log.info(
            "order_placed",
            pair=signal.pair,
            side=signal.side,
            order_id=order.get("order_id"),
            state=order.get("state"),
            ts=utc_now_iso(),
        )
        return {
            "order_id": order.get("order_id", ""),
            "state": order.get("state", ""),
            "filled": order.get("filled", 0.0),
            "avg_price": order.get("avg_price", 0.0),
            "tp_price": tp_price,
            "sl_price": sl_price,
        }

    def _record_fill(self, order: dict[str, Any], signal: Signal) -> None:
        """Stores the entry price (buy) or realized P&L (sell).

        Runs only for the actually filled part (`filled > 0`). OKX usually
        returns `state="filled"` for market orders, but `filled` is the source
        of truth: a partially filled order also contributed P&L.

        **Approximation until reconciliation:** sell P&L is calculated locally as
        `filled_base × (exit − entry)`, without fees, without FX differences, and
        without checking on OKX whether exactly this position was sold. The entry
        price comes from a recorded buy of the same pair; no entry price means the
        record is skipped (we do not guess), not zero.
        """
        filled = float(order.get("filled") or 0.0)
        avg_price = float(order.get("avg_price") or 0.0)
        if filled <= 0 or avg_price <= 0:
            if order.get("state") == "filled":
                self._log.warning(
                    "fill_without_quantity", pair=signal.pair, side=signal.side
                )
            return

        key = ENTRY_PRICE_KEY.format(pair=signal.pair)
        if signal.side == "buy":
            # One position per pair: overwriting is correct. Pyramiding would
            # require a weighted average — the strategy does not use it.
            self.store.set_kv(key, repr(avg_price))
            return

        raw = self.store.get_kv(key)
        try:
            entry_price = float(raw) if raw else 0.0
        except ValueError:
            entry_price = 0.0
        if entry_price <= 0:
            self._log.warning(
                "realized_pnl_skipped", pair=signal.pair, reason="missing entry price"
            )
            return

        pnl_usdt = filled * (avg_price - entry_price)
        total = self.store.add_realized_pnl(pnl_usdt)
        self.store.set_kv(key, "")
        self._log.info(
            "realized_pnl",
            pair=signal.pair,
            pnl_usdt=round(pnl_usdt, 8),
            day_total=round(total, 8),
        )

    def cancel(self, order_id: str, pair: str) -> dict[str, Any]:
        """Cancels an order and returns its state."""
        order = self.client.cancel_order(order_id, pair)
        self.store.insert_order(
            {
                "order_id": order.get("order_id", order_id),
                "pair": pair,
                "side": order.get("side", ""),
                "order_type": "cancel",
                "state": "canceled",
            }
        )
        return order
