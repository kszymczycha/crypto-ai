"""Collector tests: one cycle gathers data and writes it to SQLite."""

from __future__ import annotations

import pytest

from okxbot.data.collector import collect_once
from okxbot.data.market import MarketService, drop_forming_candle
from okxbot.storage.db import StateStore


class FakeOkx:
    """Fake OKX client returning constant tickers and candles."""

    def get_tickers(self, pairs):
        return {pair: {"price": 100.0, "bid": 99.9, "ask": 100.1} for pair in pairs}

    def get_candles(self, pair, bar="1m", limit=60):  # noqa: ARG002
        return [
            {"ts": str(i), "open": 100.0, "high": 101.0, "low": 99.0, "close": 100.0 + i * 0.1, "vol": 1.0}
            for i in range(40)
        ]


class FakeCoinGecko:
    """Fake CoinGecko client."""

    async def get_prices(self, ids, vs="usd"):  # noqa: ARG002
        return {cg_id: {"price": 100.0, "change_24h_pct": 2.5, "market_cap": 1e12} for cg_id in ids}


@pytest.fixture()
def service(config_dir) -> MarketService:  # noqa: ARG001
    from okxbot.config import get_symbols

    return MarketService(okx=FakeOkx(), coingecko=FakeCoinGecko(), symbols=get_symbols())


async def test_collect_once_writes_snapshots(service: MarketService, store: StateStore) -> None:
    summary = await collect_once(service, store, ["BTC-USDT", "ETH-USDT"])
    assert summary["rows"] == 2
    btc = store.latest_snapshot("BTC-USDT")
    assert btc is not None
    assert btc["price"] > 0
    assert btc["rsi"] is not None
    assert btc["change_24h_pct"] == 2.5


async def test_collect_once_single_pair(service: MarketService, store: StateStore) -> None:
    summary = await collect_once(service, store, ["BTC-USDT"])
    assert summary["rows"] == 1
    assert summary["pairs"] == ["BTC-USDT"]


def test_snapshot_shape(service: MarketService) -> None:
    snap = service.snapshot(["BTC-USDT"])
    data = snap["BTC-USDT"]
    assert set(data) >= {"price", "rsi", "ma20", "trend", "hint", "bid", "ask"}


def _bar_series(now_ms: int, bar_ms: int, count: int) -> list[dict]:
    """Candles ending exactly at `now_ms` (the last one is forming)."""
    start = now_ms - (count - 1) * bar_ms
    return [
        {"ts": str(start + index * bar_ms), "open": 100.0, "high": 101.0, "low": 99.0, "close": 100.0, "vol": 1.0}
        for index in range(count)
    ]


def test_drop_forming_candle_removes_incomplete_bar() -> None:
    """The last candle from OKX is unclosed — indicators must not see it."""
    now = 1_700_000_000_000
    bar = 4 * 3_600_000
    candles = _bar_series(now, bar, 4)
    closed = drop_forming_candle(candles, now_ms=now)
    assert len(closed) == 3
    assert closed[-1]["ts"] == str(now - bar)


def test_drop_forming_candle_keeps_closed_series() -> None:
    """When the last bar has already closed, we do not lose data."""
    now = 1_700_000_000_000
    bar = 4 * 3_600_000
    candles = _bar_series(now - bar, bar, 4)
    assert len(drop_forming_candle(candles, now_ms=now)) == 4


def test_drop_forming_candle_fails_closed_without_timestamp() -> None:
    """Missing/non-increasing `ts` = we cannot tell whether the bar is complete — drop the last one."""
    assert drop_forming_candle([{"open": 1.0}], now_ms=0) == []
    candles = [{"ts": "5"}, {"ts": "5"}]
    assert len(drop_forming_candle(candles, now_ms=10_000)) == 1


def test_snapshot_unknown_pair(service: MarketService) -> None:
    with pytest.raises(ValueError):
        service.snapshot(["NOPE-USDT"])


def test_snapshot_with_context(service: MarketService) -> None:
    snap = service.snapshot_with_context(["BTC-USDT"])
    assert snap["BTC-USDT"]["coingecko"]["change_24h_pct"] == 2.5


def test_snapshot_with_context_when_coingecko_fails(config_dir) -> None:  # noqa: ARG001
    from okxbot.config import get_symbols

    class BrokenCg:
        async def get_prices(self, ids, vs="usd"):  # noqa: ARG002
            raise RuntimeError("down")

    service = MarketService(okx=FakeOkx(), coingecko=BrokenCg(), symbols=get_symbols())
    snap = service.snapshot_with_context(["BTC-USDT"])
    assert snap["BTC-USDT"]["coingecko"] == {}
