"""OkxClient tests on a fake SDK (no network, no python-okx)."""

from __future__ import annotations

import pytest

from okxbot.client.okx_client import OkxApiError, OkxClient, OkxRateLimitError
from okxbot.config import OkxCredentials
from okxbot.client.rate_limiter import TokenBucket

CREDS = OkxCredentials(api_key="k", secret_key="s", passphrase="p", flag="1")


class FakeClock:
    """Clock for the bucket — no sleeping."""

    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


class FakeMarketApi:
    """Fake MarketData.MarketAPI."""

    def __init__(self, ticker_code: str = "0") -> None:
        self.ticker_code = ticker_code
        self.calls = 0

    def get_ticker(self, instId: str) -> dict:  # noqa: N803 - OKX API
        self.calls += 1
        if self.ticker_code != "0":
            return {"code": self.ticker_code, "msg": "rate limit", "data": []}
        return {
            "code": "0",
            "data": [{"last": "65000", "bidPx": "64990", "askPx": "65010", "vol24h": "1234", "ts": "1"}],
        }

    def get_candlesticks(self, instId: str, bar: str, limit: str) -> dict:  # noqa: N803
        rows = [
            ["3", "102", "103", "101", "102.5", "10"],
            ["2", "101", "102", "100", "101.5", "10"],
            ["1", "100", "101", "99", "100.5", "10"],
        ]
        return {"code": "0", "data": rows}


class FakeAccountApi:
    """Fake Account.AccountAPI."""

    def __init__(self, details: list[dict] | None = None) -> None:
        self.details = details or [{"ccy": "USDT", "availBal": "100.5", "cashBal": "100.5"}]

    def get_account_balance(self, **kwargs) -> dict:  # noqa: ARG002
        return {"code": "0", "data": [{"details": self.details}]}


class FakeTradeApi:
    """Fake Trade.TradeAPI."""

    def __init__(self) -> None:
        self.last_kwargs: dict = {}

    def place_order(self, **kwargs) -> dict:
        self.last_kwargs = kwargs
        return {
            "code": "0",
            "data": [{"ordId": "ord-1", "clOrdId": kwargs.get("clOrdId", ""), "sCode": "0",
                      "state": "live", "sz": kwargs.get("sz", ""), "avgPx": ""}],
        }

    def cancel_order(self, **kwargs) -> dict:  # noqa: ARG002
        return {"code": "0", "data": [{"ordId": "ord-1", "state": "canceled"}]}

    def get_order_list(self, **kwargs) -> dict:  # noqa: ARG002
        return {"code": "0", "data": [{"ordId": "ord-1", "instId": "BTC-USDT", "side": "buy", "state": "live"}]}


def make_client(market: FakeMarketApi | None = None, trade: FakeTradeApi | None = None) -> OkxClient:
    clock = FakeClock()
    return OkxClient(
        credentials=CREDS,
        public_bucket=TokenBucket(capacity=100, refill_rate=100, clock=clock),
        private_bucket=TokenBucket(capacity=100, refill_rate=100, clock=clock),
        market_api=market or FakeMarketApi(),
        account_api=FakeAccountApi(),
        trade_api=trade or FakeTradeApi(),
    )


def test_demo_flag() -> None:
    client = make_client()
    assert client.flag == "1"
    assert client.is_demo is True


def test_get_tickers() -> None:
    tickers = make_client().get_tickers(["BTC-USDT"])
    assert tickers["BTC-USDT"]["price"] == 65000.0
    assert tickers["BTC-USDT"]["ask"] == 65010.0


def test_get_candles_reversed_to_oldest_first() -> None:
    candles = make_client().get_candles("BTC-USDT", limit=3)
    assert [candle["close"] for candle in candles] == [100.5, 101.5, 102.5]


def test_public_endpoints_work_without_credentials() -> None:
    """The collector has no secrets and must not have any — public data must work.

    Regression: `_init_sdk()` required keys for EVERY call, so the collector
    crashed with 'OKX secrets missing' and wrote no market data.
    """
    clock = FakeClock()
    client = OkxClient(
        credentials=None,
        public_bucket=TokenBucket(capacity=100, refill_rate=100, clock=clock),
        private_bucket=TokenBucket(capacity=100, refill_rate=100, clock=clock),
        market_api=FakeMarketApi(),
    )
    assert client.get_tickers(["BTC-USDT"])["BTC-USDT"]["price"] == 65000.0
    assert client.get_candles("BTC-USDT", limit=3)[-1]["close"] == 102.5


def test_private_endpoints_still_require_credentials() -> None:
    """Missing keys must not quietly pass through the trading path."""
    client = OkxClient(
        credentials=None,
        public_bucket=TokenBucket(capacity=100, refill_rate=100, clock=FakeClock()),
        private_bucket=TokenBucket(capacity=100, refill_rate=100, clock=FakeClock()),
        market_api=FakeMarketApi(),
        account_api=FakeAccountApi(),
        trade_api=FakeTradeApi(),
    )
    with pytest.raises(RuntimeError, match="OKX secrets missing"):
        client.get_balance("USDT")


def test_get_balance() -> None:
    balances = make_client().get_balance("USDT")
    assert balances["USDT"] == 100.5


def make_client_with_details(details: list[dict]) -> OkxClient:
    clock = FakeClock()
    return OkxClient(
        credentials=CREDS,
        public_bucket=TokenBucket(capacity=100, refill_rate=100, clock=clock),
        private_bucket=TokenBucket(capacity=100, refill_rate=100, clock=clock),
        market_api=FakeMarketApi(),
        account_api=FakeAccountApi(details),
        trade_api=FakeTradeApi(),
    )


def test_balance_uses_cash_when_available_is_zero() -> None:
    """TP/SL freezes assets: availBal="0", but the coin is still on the account.

    Regression: `availBal or cashBal` picked the string "0" because it is truthy,
    so equity valuation lost the held asset and the drawdown circuit saw a false
    loss.
    """
    client = make_client_with_details([{"ccy": "BTC", "availBal": "0", "cashBal": "1.5"}])
    assert client.get_balance()["BTC"] == 1.5


def test_balance_handles_empty_strings_as_zero() -> None:
    client = make_client_with_details(
        [{"ccy": "ETH", "availBal": "", "cashBal": "2.25"}, {"ccy": "SOL", "availBal": "3"}]
    )
    balances = client.get_balance()
    assert balances["ETH"] == 2.25
    assert balances["SOL"] == 3.0


def test_balance_prefers_available_when_higher() -> None:
    """We do not assume in advance which field is larger — we take the actual value."""
    client = make_client_with_details([{"ccy": "USDT", "availBal": "7.5", "cashBal": "7"}])
    assert client.get_balance()["USDT"] == 7.5


def test_client_order_id_sanitized_to_alnum() -> None:
    """OKX rejects a clOrdId outside [A-Za-z0-9] — a UUID with hyphens must be sanitized."""
    trade = FakeTradeApi()
    make_client(trade=trade).place_order(
        pair="BTC-USDT",
        side="buy",
        amount_usdt=10.0,
        client_order_id="8f14e45f-ceea-467a-9575-4c1d3a4e2b11",
    )
    client_order_id = trade.last_kwargs["clOrdId"]
    assert client_order_id.isalnum()
    assert len(client_order_id) <= 32
    assert client_order_id == "8f14e45fceea467a95754c1d3a4e2b11"


def test_place_buy_order_uses_quote_ccy() -> None:
    trade = FakeTradeApi()
    order = make_client(trade=trade).place_order(
        pair="BTC-USDT", side="buy", amount_usdt=25.0, tp_price=66000.0, sl_price=64000.0
    )
    assert order["order_id"] == "ord-1"
    assert trade.last_kwargs["tgtCcy"] == "quote_ccy"
    assert trade.last_kwargs["tdMode"] == "cash"
    assert trade.last_kwargs["tpTriggerPx"] == "66000"
    assert "amount_base" not in order["request"] or order["request"]["amount_base"] is None


def test_place_sell_requires_amount_base() -> None:
    with pytest.raises(ValueError):
        make_client().place_order(pair="BTC-USDT", side="sell", amount_usdt=25.0)


def test_limit_order_requires_price() -> None:
    with pytest.raises(ValueError):
        make_client().place_order(pair="BTC-USDT", side="buy", amount_usdt=10.0, order_type="limit")


def test_unknown_order_type_rejected() -> None:
    with pytest.raises(ValueError):
        make_client().place_order(pair="BTC-USDT", side="buy", amount_usdt=10.0, order_type="stop")


def test_api_error_raised() -> None:
    with pytest.raises(OkxApiError):
        make_client(market=FakeMarketApi(ticker_code="51000")).get_tickers(["BTC-USDT"])


def test_rate_limit_error_is_subclass() -> None:
    assert issubclass(OkxRateLimitError, OkxApiError)


def test_pending_orders_normalized() -> None:
    orders = make_client().get_pending_orders("BTC-USDT")
    assert orders[0]["order_id"] == "ord-1"
    assert orders[0]["pair"] == "BTC-USDT"
