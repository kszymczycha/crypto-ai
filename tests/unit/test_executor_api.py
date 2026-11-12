"""Executor endpoint tests through the FastAPI TestClient (no Docker, no network)."""

from __future__ import annotations

import uuid
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from okxbot.config import RiskLimits, get_settings, get_symbols
from okxbot.executor.risk_engine import RiskEngine
from okxbot.storage.db import StateStore
from okxbot.strategy.rules import StrategyConfig


class FakeClient:
    """Fake OKX: configurable balance, optional errors and a queue of fills.

    `fills` is the queue of `place_order` responses — the portfolio tests drive
    them to simulate a completed buy and sell at a given price. The balance
    changes as on an exchange: a buy takes USDT and adds the base asset, a sell
    does the opposite.
    """

    def __init__(
        self,
        *,
        fail: bool = False,
        usdt: float = 1000.0,
        fail_balance: bool = False,
        fills: list[dict] | None = None,
    ) -> None:
        self.fail = fail
        self.usdt = usdt
        self.fail_balance = fail_balance
        self.fills = list(fills or [])
        self.holdings: dict[str, float] = {}

    def get_balance(self, ccy=None):  # noqa: ARG002
        if self.fail_balance:
            from okxbot.client.okx_client import OkxApiError

            raise OkxApiError("50011", "timeout", endpoint="private:get_balance")
        balances = {"USDT": self.usdt}
        balances.update(self.holdings)
        return balances

    def get_pending_orders(self, pair=None):  # noqa: ARG002
        return []

    def get_tickers(self, pairs):
        return {pair: {"ask": 100.0, "bid": 99.0, "price": 99.5} for pair in pairs}

    def place_order(self, **kwargs):
        if self.fail:
            from okxbot.client.okx_client import OkxApiError

            raise OkxApiError("50013", "limit", endpoint="private:place_order")
        fill = (
            self.fills.pop(0)
            if self.fills
            else {"state": "live", "filled": 0.0, "avg_price": 0.0}
        )
        base = str(kwargs.get("pair", "")).split("-")[0]
        filled = float(fill.get("filled") or 0.0)
        avg_price = float(fill.get("avg_price") or 0.0)
        if filled > 0:
            if kwargs.get("side") == "buy":
                self.usdt -= float(kwargs.get("amount_usdt") or 0.0)
                self.holdings[base] = self.holdings.get(base, 0.0) + filled
            else:
                self.usdt += filled * avg_price
                self.holdings[base] = max(0.0, self.holdings.get(base, 0.0) - filled)
        return {
            "order_id": "ord-api-1",
            "client_order_id": kwargs.get("clientOrderId", ""),
            **fill,
        }

    def cancel_order(self, order_id, pair):  # noqa: ARG002
        return {"order_id": order_id, "state": "canceled", "side": "buy"}


class FakeMarket:
    """Fake MarketService: a snapshot that passes the buy rule by default."""

    def __init__(self, **overrides) -> None:
        self.snapshot_data = {
            "rsi": 28.0,
            "regime": "up",
            "vol_ratio": 1.5,
            "atr_pct": 1.0,
            "candles_count": 250,
            "price": 100.0,
            "adx": 18.0,
        }
        self.snapshot_data.update(overrides)
        self.calls = 0

    def snapshot(self, pairs):
        self.calls += 1
        return {pairs[0]: dict(self.snapshot_data)}


class BrokenMarket:
    """Fake MarketService that cannot fetch data."""

    def snapshot(self, pairs):  # noqa: ARG002
        raise RuntimeError("OKX down")


def risk_limits(**overrides):
    """Risk limits for the API tests (equity 1000 USDT, confirmation threshold 100)."""
    base = {
        "max_position_pct": 100.0,
        "max_daily_loss_pct": 3.0,
        "max_portfolio_drawdown_pct": 15.0,
        "max_open_positions": 3,
        "max_order_usdt": 50.0,
        "min_order_usdt": 5.0,
        "require_confirmation_above_usdt": 100.0,
        "allowed_sides": ("buy", "sell"),
        "allowed_order_types": ("market", "limit"),
        "cooldown_seconds_between_signals": 10,
        "max_signals_per_day": 40,
    }
    base.update(overrides)
    return RiskLimits(**base)


@pytest.fixture()
def api_factory(config_dir: Path, store: StateStore, monkeypatch: pytest.MonkeyPatch):  # noqa: ARG001
    """Returns a factory of TestClients with the given risk limits."""
    from okxbot.executor import service

    clients: list[TestClient] = []

    def build(limits: RiskLimits | None = None, market=None, client=None) -> TestClient:
        from okxbot.executor.order_router import OrderRouter

        fake_client = client or FakeClient()
        state = service.ExecutorState.__new__(service.ExecutorState)
        state.settings = get_settings()
        state.store = store
        state.credentials = type("C", (), {"flag": "1"})()
        state.client = fake_client
        state.symbols = get_symbols()
        state.risk = RiskEngine(limits or risk_limits(), store, symbols=get_symbols(), equity_usdt=1000.0)
        state.router = OrderRouter(fake_client, store)
        state.market = market or FakeMarket()
        state.strategy = StrategyConfig()
        monkeypatch.setattr(service, "_state", state)
        client = TestClient(service.app)
        client.__enter__()
        clients.append(client)
        return client

    yield build
    for client in clients:
        client.__exit__(None, None, None)


@pytest.fixture()
def api(api_factory) -> TestClient:
    """Default TestClient (limits from `risk_limits()`)."""
    return api_factory()


def signal_payload(**overrides) -> dict:
    payload = {
        "pair": "BTC-USDT",
        "side": "buy",
        "amount_usdt": 20.0,
        "reason": "RSI 28 and price above MA20",
        "idempotency_key": str(uuid.uuid4()),
        "tp_pct": 2.0,
        "sl_pct": 1.0,
    }
    payload.update(overrides)
    return payload


def test_health(api: TestClient) -> None:
    response = api.get("/health")
    assert response.status_code == 200
    assert response.json()["ok"] is True
    assert response.json()["mode"] == "demo"


def test_signal_placed(api: TestClient) -> None:
    response = api.post("/signal", json=signal_payload())
    body = response.json()
    assert response.status_code == 200
    assert body["ok"] is True
    assert body["status"] == "placed"
    assert body["order_id"] == "ord-api-1"
    assert body["tp_price"] == pytest.approx(102.0)


def test_signal_duplicate_idempotency(api: TestClient) -> None:
    payload = signal_payload(idempotency_key="same-key-123")
    first = api.post("/signal", json=payload).json()
    second = api.post("/signal", json=payload).json()
    assert first["status"] == "placed"
    assert second["status"] == "duplicate"
    assert "already accepted" in second["reason"]


def test_signal_rejected_by_risk(api: TestClient) -> None:
    response = api.post("/signal", json=signal_payload(amount_usdt=1.0))
    body = response.json()
    assert body["ok"] is False
    assert body["status"] == "rejected"


def test_signal_needs_confirmation(api_factory) -> None:
    # The amount must fit the pair limit from config/symbols.yaml (BTC-USDT = 50 USDT)
    # and at the same time exceed the confirmation threshold — hence threshold 30 and amount 40.
    client = api_factory(risk_limits(max_order_usdt=50.0, require_confirmation_above_usdt=30.0))
    body = client.post("/signal", json=signal_payload(amount_usdt=40.0)).json()
    assert body["ok"] is True
    assert body["status"] == "needs_confirmation"
    assert "confirmation threshold" in body["reason"]


def test_buy_rejected_when_strategy_says_watch(api_factory) -> None:
    """The executor does not trust the signal reason: it evaluates the entry itself with the strategy rule."""
    client = api_factory(market=FakeMarket(rsi=55.0))
    body = client.post("/signal", json=signal_payload()).json()
    assert body["ok"] is False
    assert body["status"] == "rejected"
    assert body["reason"].startswith("strategy_rejected")


def test_buy_rejected_when_strategy_data_unavailable(api_factory) -> None:
    """No market data = no entry (fail-closed), not a pass-through."""
    client = api_factory(market=BrokenMarket())
    body = client.post("/signal", json=signal_payload()).json()
    assert body["ok"] is False
    assert body["status"] == "rejected"
    assert body["reason"].startswith("strategy_unavailable")


def test_sell_is_not_gated_by_strategy(api_factory) -> None:
    """A sell is an emergency exit — the strategy gate does not block it."""
    client = api_factory(market=FakeMarket(rsi=55.0))
    body = client.post("/signal", json=signal_payload(side="sell")).json()
    assert body["ok"] is True
    assert body["status"] == "placed"


def test_signal_invalid_side(api: TestClient) -> None:
    response = api.post("/signal", json=signal_payload(side="hold"))
    assert response.status_code == 422


def test_signal_missing_reason(api: TestClient) -> None:
    response = api.post("/signal", json=signal_payload(reason="x"))
    assert response.status_code == 422


def test_state_endpoint(api: TestClient) -> None:
    body = api.get("/state").json()
    assert body["ok"] is True
    assert body["balances"]["USDT"] == 1000.0
    assert body["pending_orders"] == []
    assert body["daily"]["max_open_positions"] == 3


def test_cancel_endpoint(api: TestClient) -> None:
    body = api.post("/cancel", json={"order_id": "ord-api-1", "pair": "BTC-USDT"}).json()
    assert body["ok"] is True
    assert body["order"]["state"] == "canceled"


# ------------------------------------- portfolio stop wiring (Stage D, integration)


def test_buy_rejected_after_equity_drops_20_percent(api_factory) -> None:
    """The balance drops by 20% while the bot runs → the next buy is rejected."""
    client = FakeClient(usdt=1000.0)
    api = api_factory(client=client)

    first = api.post("/signal", json=signal_payload()).json()
    assert first["status"] == "placed"

    client.usdt = 800.0  # peak 1000, drawdown 20% > limit 15%
    second = api.post(
        "/signal", json=signal_payload(pair="ETH-USDT", amount_usdt=20.0)
    ).json()
    assert second["status"] == "rejected"
    assert second["reason"].startswith("portfolio_drawdown_limit")


def test_sell_stays_allowed_after_drawdown(api_factory) -> None:
    """The circuit must not trap a position before the emergency exit."""
    client = FakeClient(usdt=1000.0)
    api = api_factory(client=client)
    api.post("/signal", json=signal_payload()).json()

    client.usdt = 500.0  # 50% drawdown
    sell = api.post(
        "/signal", json=signal_payload(pair="ETH-USDT", side="sell", amount_usdt=20.0)
    ).json()
    assert sell["status"] == "placed"


def test_losing_close_lowers_realized_pnl_and_equity(api_factory) -> None:
    """A losing sell: `realized_pnl` negative and equity lower than before."""
    client = FakeClient(
        usdt=1000.0,
        fills=[
            {"state": "filled", "filled": 0.2, "avg_price": 100.0},  # buy
            {"state": "filled", "filled": 0.2, "avg_price": 80.0},   # sell at a loss
        ],
    )
    # Same pair in one test: a 10 s cooldown would block the sell.
    api = api_factory(limits=risk_limits(cooldown_seconds_between_signals=0), client=client)

    assert api.post("/signal", json=signal_payload(amount_usdt=20.0)).json()["status"] == "placed"
    before = api.get("/state").json()["daily"]["equity_usdt"]

    sold = api.post(
        "/signal", json=signal_payload(side="sell", amount_usdt=20.0)
    ).json()
    assert sold["status"] == "placed"

    from okxbot.executor import service

    state = service._state
    assert state is not None
    equity_after = state._refresh_equity()
    daily = state.risk.daily_summary()
    # 0.2 BTC × (80 − 100) = −4 USDT, no fees (local approximation).
    assert daily["realized_pnl_usdt"] == pytest.approx(-4.0)
    assert equity_after < before


def test_equity_api_error_blocks_buy_but_not_sell(api_factory) -> None:
    """Balance failure: drawdown cannot be measured → buy fail-closed."""
    client = FakeClient(fail_balance=True)
    api = api_factory(client=client)

    buy = api.post("/signal", json=signal_payload()).json()
    assert buy["status"] == "rejected"
    assert "equity unknown" in buy["reason"]

    sell = api.post(
        "/signal", json=signal_payload(pair="ETH-USDT", side="sell", amount_usdt=20.0)
    ).json()
    assert sell["status"] == "placed"


def test_equity_includes_held_assets(api_factory) -> None:
    """A buy must not look like a loss: USDT goes out, the coin stays."""
    client = FakeClient(usdt=1000.0)
    client.holdings["BTC"] = 1.0  # 1 BTC × bid 99 = 99 USDT
    api = api_factory(client=client)
    api.post("/signal", json=signal_payload())  # forces a refresh

    from okxbot.executor import service

    state = service._state
    assert state is not None
    assert state._refresh_equity() == pytest.approx(1099.0)
    assert state.risk.portfolio_drawdown_pct() == 0.0
