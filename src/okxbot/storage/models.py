"""Data models (pydantic v2) used in the CLI, API, and persistence layer.

Kept in one place so the JSON shape is identical in:
`POST /signal` (executor), the CLI (opencode), and SQLite rows.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field, field_validator

Side = Literal["buy", "sell"]
OrderType = Literal["market", "limit"]
SignalStatus = Literal["accepted", "rejected", "needs_confirmation", "duplicate"]
OrderState = Literal["live", "partially_filled", "filled", "canceled", "failed", "pending", "unknown"]


class SignalModel(BaseModel):
    """Incoming signal: intent, not an order. Limits are enforced by risk_engine."""

    pair: str = Field(..., min_length=5, max_length=20, examples=["BTC-USDT"])
    side: Side
    amount_usdt: float = Field(..., gt=0, description="amount in USDT")
    reason: str = Field(..., min_length=5, max_length=280)
    idempotency_key: str = Field(..., min_length=8, max_length=64)
    tp_pct: float | None = Field(default=None, gt=0, le=20)
    sl_pct: float | None = Field(default=None, gt=0, le=20)

    @field_validator("pair")
    @classmethod
    def _upper_pair(cls, value: str) -> str:
        return value.upper().strip()


class SignalResponse(BaseModel):
    """Executor response to a signal (short, no raw OKX data)."""

    ok: bool
    status: SignalStatus | Literal["placed", "failed"]
    order_id: str = ""
    reason: str = ""
    state: str = ""
    filled: float = 0.0
    avg_price: float = 0.0
    tp_price: float | None = None
    sl_price: float | None = None


class OrderModel(BaseModel):
    """Order in persistent form (what we store and display)."""

    order_id: str = ""
    client_order_id: str = ""
    signal_id: int | None = None
    pair: str
    side: Side
    order_type: OrderType | str = "market"
    amount_usdt: float | None = None
    amount_base: float | None = None
    price: float | None = None
    tp_price: float | None = None
    sl_price: float | None = None
    state: OrderState | str = "unknown"
    filled: float = 0.0
    avg_price: float = 0.0
    error: str | None = None
    created_at: str = ""
    updated_at: str = ""


class PositionModel(BaseModel):
    """Spot position calculated from buy orders (simplified model)."""

    pair: str
    base_amount: float = 0.0
    avg_entry_price: float = 0.0
    invested_usdt: float = 0.0
    opened_at: str = ""


class DailyStatsModel(BaseModel):
    """Daily statistics used by the risk limits."""

    day: str
    realized_pnl_usdt: float = 0.0
    equity_start_usdt: float = 0.0
    signals_count: int = 0
    orders_count: int = 0
    updated_at: str = ""


class MarketSnapshotModel(BaseModel):
    """Market snapshot stored by the collector."""

    pair: str
    price: float
    rsi: float | None = None
    ma20: float | None = None
    change_24h_pct: float | None = None
    market_cap: float | None = None
    ts: str = ""


class ErrorResponse(BaseModel):
    """Common error shape returned by the API."""

    ok: bool = False
    error: str
    message: str = ""
