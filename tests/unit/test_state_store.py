"""SQLite tests: signals, idempotency, daily stats, snapshots."""

from __future__ import annotations

import pytest

from okxbot.storage.db import StateStore, utc_today


def test_schema_created(store: StateStore) -> None:
    with store._cursor() as cursor:  # noqa: SLF001 - internal test
        tables = {
            row["name"]
            for row in cursor.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
        }
    assert {"signals", "orders", "daily_stats", "market_snapshots", "kv"} <= tables


def test_insert_and_find_signal(store: StateStore) -> None:
    signal_id = store.insert_signal(
        {
            "idempotency_key": "key-1",
            "pair": "BTC-USDT",
            "side": "buy",
            "amount_usdt": 20.0,
            "reason": "test reason",
            "status": "accepted",
        }
    )
    assert signal_id > 0
    found = store.find_signal_by_key("key-1")
    assert found is not None
    assert found["pair"] == "BTC-USDT"


def test_idempotency_key_unique(store: StateStore) -> None:
    payload = {
        "idempotency_key": "key-dup",
        "pair": "BTC-USDT",
        "side": "buy",
        "amount_usdt": 10.0,
        "reason": "duplicate",
    }
    store.insert_signal(payload)
    with pytest.raises(Exception):  # noqa: B017 - sqlite3.IntegrityError
        store.insert_signal(payload)


def test_daily_stats_flow(store: StateStore) -> None:
    store.set_equity_start(1000.0)
    store.add_realized_pnl(12.5)
    store.add_realized_pnl(-2.5)
    stats = store.get_daily_stats()
    assert stats["equity_start_usdt"] == 1000.0
    assert stats["realized_pnl_usdt"] == 10.0
    assert stats["day"] == utc_today()


def test_equity_start_set_only_once(store: StateStore) -> None:
    store.set_equity_start(1000.0)
    store.set_equity_start(2000.0)
    assert store.get_daily_stats()["equity_start_usdt"] == 1000.0


def test_increment_signals(store: StateStore) -> None:
    assert store.increment_signals() == 1
    assert store.increment_signals() == 2


def test_count_open_positions(store: StateStore) -> None:
    store.insert_order({"pair": "BTC-USDT", "side": "buy", "state": "live"})
    store.insert_order({"pair": "BTC-USDT", "side": "buy", "state": "filled"})
    store.insert_order({"pair": "ETH-USDT", "side": "sell", "state": "live"})
    assert store.count_open_positions() == 1


def test_market_snapshots_roundtrip(store: StateStore) -> None:
    store.insert_market_snapshots(
        [{"pair": "BTC-USDT", "price": 65000.0, "rsi": 42.5, "ma20": 64000.0, "change_24h_pct": 1.2}]
    )
    latest = store.latest_snapshot("BTC-USDT")
    assert latest is not None
    assert latest["price"] == 65000.0
    assert latest["rsi"] == 42.5


def test_kv_roundtrip(store: StateStore) -> None:
    store.set_kv("last_signal_at:BTC-USDT", "2025-01-01T00:00:00+00:00")
    assert store.get_kv("last_signal_at:BTC-USDT") == "2025-01-01T00:00:00+00:00"
    store.set_kv("last_signal_at:BTC-USDT", "2025-01-02T00:00:00+00:00")
    assert store.get_kv("last_signal_at:BTC-USDT") == "2025-01-02T00:00:00+00:00"
