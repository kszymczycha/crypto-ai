"""CoinGecko tests with a fake httpx transport (no network) and an on-disk cache."""

from __future__ import annotations

from pathlib import Path

import httpx
import pytest

from okxbot.client.coingecko_client import CoinGeckoClient, CoinGeckoError, FileCache
from okxbot.client.rate_limiter import TokenBucket


class FakeClock:
    """Clock for the bucket."""

    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now


def _client_with(handler, cache_dir: Path) -> CoinGeckoClient:
    transport = httpx.MockTransport(handler)
    http_client = httpx.AsyncClient(transport=transport)
    return CoinGeckoClient(
        bucket=TokenBucket(capacity=100, refill_rate=100, clock=FakeClock()),
        http_client=http_client,
        cache=FileCache(cache_dir),
    )


async def test_get_prices_ok(cache_dir: Path) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert "simple/price" in str(request.url)
        return httpx.Response(
            200,
            json={"bitcoin": {"usd": 65000.0, "usd_24h_change": 1.5, "usd_market_cap": 1.2e12}},
        )

    client = _client_with(handler, cache_dir)
    prices = await client.get_prices(["bitcoin"])
    assert prices["bitcoin"]["price"] == 65000.0
    assert prices["bitcoin"]["change_24h_pct"] == 1.5


async def test_cache_prevents_second_request(cache_dir: Path) -> None:
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:  # noqa: ARG001
        calls["n"] += 1
        return httpx.Response(200, json={"bitcoin": {"usd": 1.0}})

    client = _client_with(handler, cache_dir)
    await client.get_prices(["bitcoin"])
    await client.get_prices(["bitcoin"])
    assert calls["n"] == 1


async def test_http_error_raises(cache_dir: Path) -> None:
    def handler(request: httpx.Request) -> httpx.Response:  # noqa: ARG001
        return httpx.Response(404, text="not found")

    client = _client_with(handler, cache_dir)
    with pytest.raises(CoinGeckoError):
        await client.get_prices(["nope"])


async def test_metadata_cached(cache_dir: Path) -> None:
    def handler(request: httpx.Request) -> httpx.Response:  # noqa: ARG001
        return httpx.Response(200, json={"id": "bitcoin", "symbol": "btc", "name": "Bitcoin", "market_cap_rank": 1})

    client = _client_with(handler, cache_dir)
    meta = await client.get_coin_metadata("bitcoin")
    assert meta["symbol"] == "btc"
    assert meta["market_cap_rank"] == 1


def test_file_cache_expiry(tmp_path: Path) -> None:
    cache = FileCache(tmp_path)
    cache.set("k", {"v": 1})
    assert cache.get("k", ttl_seconds=60) == {"v": 1}
    assert cache.get("k", ttl_seconds=0) is None
    assert cache.get("missing", ttl_seconds=60) is None
