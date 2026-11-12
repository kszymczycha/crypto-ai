"""Integration tests against the real OKX API (DEMO only).

Skipped unless `OKX_FLAG=1` is set and a secrets file exists. They never run on
a live account — an extra `flag == '1'` assertion.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

pytestmark = pytest.mark.skipif(
    os.environ.get("OKX_FLAG") != "1" or not Path(os.environ.get("SECRETS_FILE", "/run/secrets/okx.toml")).exists(),
    reason="requires OKX_FLAG=1 and a demo secrets file",
)


@pytest.fixture(scope="module")
def client():
    """Client with demo credentials; hard-refuses to work on live."""
    from okxbot.client.okx_client import OkxClient
    from okxbot.config import load_credentials

    creds = load_credentials()
    if creds.flag != "1":
        pytest.skip("secrets are not demo (flag != '1')")
    return OkxClient(credentials=creds)


def test_public_ticker(client) -> None:
    tickers = client.get_tickers(["BTC-USDT"])
    assert tickers["BTC-USDT"]["price"] > 0


def test_public_candles(client) -> None:
    candles = client.get_candles("BTC-USDT", bar="1m", limit=20)
    assert len(candles) >= 10
    assert candles[-1]["close"] > 0


def test_balance_readable(client) -> None:
    balances = client.get_balance("USDT")
    assert isinstance(balances, dict)


@pytest.mark.skipif(
    os.environ.get("OKX_PLACE_TEST_ORDER") != "1",
    reason="placing an order requires OKX_PLACE_TEST_ORDER=1",
)
def test_place_tiny_demo_order(client) -> None:
    """Places a minimal demo order (deliberately opt-in)."""
    order = client.place_order(pair="BTC-USDT", side="buy", amount_usdt=5.0, order_type="market")
    assert order["order_id"]
