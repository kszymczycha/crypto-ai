"""OrderRouter tests: TP/SL calculation, database write, OKX error handling."""

from __future__ import annotations

import pytest

from okxbot.client.okx_client import OkxApiError
from okxbot.executor.order_router import OrderRouter
from okxbot.executor.risk_engine import Signal
from okxbot.storage.db import StateStore


class FakeClient:
    """Fake OkxClient with a configurable result."""

    def __init__(self, *, fail: bool = False, order_id: str = "ord-9") -> None:
        self.fail = fail
        self.order_id = order_id
        self.last_kwargs: dict = {}

    def get_tickers(self, pairs):
        return {pairs[0]: {"ask": 100.0, "bid": 99.0, "price": 99.5}}

    def place_order(self, **kwargs):
        self.last_kwargs = kwargs
        if self.fail:
            raise OkxApiError("50013", "too many requests", endpoint="private:place_order")
        return {
            "order_id": self.order_id,
            "client_order_id": kwargs.get("clientOrderId", ""),
            "state": "live",
            "filled": 0.0,
            "avg_price": 0.0,
        }

    def cancel_order(self, order_id, pair):  # noqa: ARG002
        return {"order_id": order_id, "state": "canceled", "side": "buy"}


def buy_signal(**overrides) -> Signal:
    base = {
        "pair": "BTC-USDT",
        "side": "buy",
        "amount_usdt": 20.0,
        "reason": "test",
        "idempotency_key": "router-key-1",
        "tp_pct": 2.0,
        "sl_pct": 1.0,
    }
    base.update(overrides)
    return Signal(**base)


def test_buy_tp_sl_computed_from_ask(store: StateStore) -> None:
    router = OrderRouter(FakeClient(), store)
    result = router.execute(buy_signal(), signal_id=1)
    assert result["order_id"] == "ord-9"
    assert result["tp_price"] == pytest.approx(102.0)
    assert result["sl_price"] == pytest.approx(99.0)


def test_sell_tp_sl_computed_from_bid(store: StateStore) -> None:
    client = FakeClient()
    router = OrderRouter(client, store)
    result = router.execute(buy_signal(side="sell", amount_usdt=99.0), signal_id=1)
    assert result["tp_price"] == pytest.approx(97.02)
    assert result["sl_price"] == pytest.approx(99.99)
    # A sell passes the size in the base currency: 99 USDT / 99 (bid) = 1 BTC.
    assert client.last_kwargs["amount_base"] == pytest.approx(1.0)
    assert "amount_usdt" in client.last_kwargs and client.last_kwargs["amount_usdt"] is None


def test_order_saved_to_store(store: StateStore) -> None:
    router = OrderRouter(FakeClient(), store)
    router.execute(buy_signal(), signal_id=7)
    orders = store.open_orders("BTC-USDT")
    assert len(orders) == 1
    assert orders[0]["order_id"] == "ord-9"
    assert orders[0]["signal_id"] == 7


def test_failed_order_saved_with_error(store: StateStore) -> None:
    router = OrderRouter(FakeClient(fail=True), store)
    with pytest.raises(OkxApiError):
        router.execute(buy_signal(), signal_id=1)
    with store._cursor() as cursor:  # noqa: SLF001
        row = cursor.execute("SELECT * FROM orders").fetchone()
    assert row["state"] == "failed"
    assert "50013" in row["error"]


def test_no_tp_sl_when_not_given(store: StateStore) -> None:
    router = OrderRouter(FakeClient(), store)
    result = router.execute(buy_signal(tp_pct=None, sl_pct=None), signal_id=1)
    assert result["tp_price"] is None
    assert result["sl_price"] is None


def test_cancel_returns_order(store: StateStore) -> None:
    router = OrderRouter(FakeClient(), store)
    order = router.cancel("ord-9", "BTC-USDT")
    assert order["state"] == "canceled"
