"""Persistence layer: SQLite in `data/state.db` (WAL).

Tables:
- signals      — every accepted signal (idempotency key, reason, status),
- orders       — orders placed by the executor (linked to a signal),
- daily_stats  — daily P&L and signal counter (risk limit),
- market_snapshots — data from the collector (price/RSI history).

All operations are synchronous and thread-safe (`check_same_thread=False`
plus an internal lock). The executor calls them through `asyncio.to_thread`.
"""

from __future__ import annotations

import sqlite3
import threading
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Iterator

SCHEMA = """
CREATE TABLE IF NOT EXISTS signals (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    idempotency_key   TEXT NOT NULL,
    pair              TEXT NOT NULL,
    side              TEXT NOT NULL,
    amount_usdt       REAL NOT NULL,
    tp_pct            REAL,
    sl_pct            REAL,
    reason            TEXT NOT NULL,
    status            TEXT NOT NULL,
    source            TEXT NOT NULL DEFAULT 'opencode',
    created_at        TEXT NOT NULL,
    UNIQUE(idempotency_key)
);
CREATE INDEX IF NOT EXISTS idx_signals_pair_created ON signals(pair, created_at);

CREATE TABLE IF NOT EXISTS orders (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    signal_id         INTEGER REFERENCES signals(id),
    order_id          TEXT,
    client_order_id   TEXT,
    pair              TEXT NOT NULL,
    side              TEXT NOT NULL,
    order_type        TEXT NOT NULL,
    amount_usdt       REAL,
    amount_base       REAL,
    price             REAL,
    tp_price          REAL,
    sl_price          REAL,
    state             TEXT NOT NULL,
    filled            REAL NOT NULL DEFAULT 0,
    avg_price         REAL NOT NULL DEFAULT 0,
    error             TEXT,
    created_at        TEXT NOT NULL,
    updated_at        TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_orders_pair_state ON orders(pair, state);

CREATE TABLE IF NOT EXISTS daily_stats (
    day               TEXT PRIMARY KEY,
    realized_pnl_usdt REAL NOT NULL DEFAULT 0,
    equity_start_usdt REAL NOT NULL DEFAULT 0,
    signals_count     INTEGER NOT NULL DEFAULT 0,
    orders_count      INTEGER NOT NULL DEFAULT 0,
    updated_at        TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS market_snapshots (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    pair              TEXT NOT NULL,
    price             REAL NOT NULL,
    rsi               REAL,
    ma20              REAL,
    ma200             REAL,
    regime            TEXT,
    atr               REAL,
    atr_pct           REAL,
    vol_ratio         REAL,
    bar               TEXT,
    change_24h_pct    REAL,
    market_cap        REAL,
    ts                TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_snapshots_pair_ts ON market_snapshots(pair, ts);

CREATE TABLE IF NOT EXISTS kv (
    key               TEXT PRIMARY KEY,
    value             TEXT NOT NULL,
    updated_at        TEXT NOT NULL
);
"""


def utc_now_iso() -> str:
    """Current UTC time in ISO 8601 (seconds)."""
    return datetime.now(UTC).replace(microsecond=0).isoformat()


def utc_today() -> str:
    """Today's UTC date (YYYY-MM-DD)."""
    return datetime.now(UTC).strftime("%Y-%m-%d")


class StateStore:
    """SQLite access. One instance per process."""

    def __init__(self, db_path: Path | str) -> None:
        """Opens (lazily) a connection to the database at `db_path`."""
        self.db_path = Path(db_path)
        self._lock = threading.RLock()
        self._conn: sqlite3.Connection | None = None

    # ------------------------------------------------------------ connection
    def _connection(self) -> sqlite3.Connection:
        if self._conn is None:
            self.db_path.parent.mkdir(parents=True, exist_ok=True)
            conn = sqlite3.connect(self.db_path, check_same_thread=False, timeout=15)
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA journal_mode=WAL")
            # Foreign keys are deliberately DISABLED: orders.signal_id points to
            # signals(id), but the write order does not always allow it (orders
            # reconstructed from OKX, ordering on an order error). Consistency is
            # enforced in code, not with a hard foreign key — otherwise INSERT fails.
            conn.execute("PRAGMA foreign_keys=OFF")
            conn.execute("PRAGMA busy_timeout=15000")
            self._conn = conn
        return self._conn

    @contextmanager
    def _cursor(self) -> Iterator[sqlite3.Cursor]:
        with self._lock:
            conn = self._connection()
            cursor = conn.cursor()
            try:
                yield cursor
                conn.commit()
            except Exception:
                conn.rollback()
                raise
            finally:
                cursor.close()

    def init_schema(self) -> None:
        """Creates tables and indexes (idempotently), then adds new columns."""
        with self._cursor() as cursor:
            cursor.executescript(SCHEMA)
            self._migrate(cursor)

    def _migrate(self, cursor: sqlite3.Cursor) -> None:
        """Adds columns introduced after the database was first created.

        `CREATE TABLE IF NOT EXISTS` does not modify an existing table, so a
        database created before indicators were introduced lacks the columns for
        regime, volatility, and volume. ALTER TABLE ... ADD COLUMN is cheap in
        SQLite and does not rewrite data.
        """
        existing = {
            str(row["name"])
            for row in cursor.execute("PRAGMA table_info(market_snapshots)").fetchall()
        }
        for column, sql_type in (
            ("ma200", "REAL"),
            ("regime", "TEXT"),
            ("atr", "REAL"),
            ("atr_pct", "REAL"),
            ("vol_ratio", "REAL"),
            ("bar", "TEXT"),
        ):
            if column not in existing:
                cursor.execute(f"ALTER TABLE market_snapshots ADD COLUMN {column} {sql_type}")

    def close(self) -> None:
        """Closes the connection."""
        with self._lock:
            if self._conn is not None:
                self._conn.close()
                self._conn = None

    # --------------------------------------------------------------- signals
    def find_signal_by_key(self, idempotency_key: str) -> dict[str, Any] | None:
        """Returns a previously accepted signal with this key (idempotency)."""
        with self._cursor() as cursor:
            row = cursor.execute(
                "SELECT * FROM signals WHERE idempotency_key = ?", (idempotency_key,)
            ).fetchone()
        return dict(row) if row else None

    def insert_signal(self, signal: dict[str, Any]) -> int:
        """Stores a signal and returns its id."""
        with self._cursor() as cursor:
            cursor.execute(
                """INSERT INTO signals
                   (idempotency_key, pair, side, amount_usdt, tp_pct, sl_pct, reason, status, source, created_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    signal["idempotency_key"],
                    signal["pair"],
                    signal["side"],
                    float(signal["amount_usdt"]),
                    signal.get("tp_pct"),
                    signal.get("sl_pct"),
                    signal["reason"],
                    signal.get("status", "accepted"),
                    signal.get("source", "opencode"),
                    signal.get("created_at", utc_now_iso()),
                ),
            )
            return int(cursor.lastrowid or 0)

    # --------------------------------------------------------------- orders
    def insert_order(self, order: dict[str, Any]) -> int:
        """Stores an order (OKX state + request parameters)."""
        now = utc_now_iso()
        with self._cursor() as cursor:
            cursor.execute(
                """INSERT INTO orders
                   (signal_id, order_id, client_order_id, pair, side, order_type, amount_usdt,
                    amount_base, price, tp_price, sl_price, state, filled, avg_price, error,
                    created_at, updated_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    order.get("signal_id"),
                    order.get("order_id"),
                    order.get("client_order_id"),
                    order["pair"],
                    order["side"],
                    order.get("order_type", "market"),
                    order.get("amount_usdt"),
                    order.get("amount_base"),
                    order.get("price"),
                    order.get("tp_price"),
                    order.get("sl_price"),
                    order.get("state", "unknown"),
                    float(order.get("filled") or 0.0),
                    float(order.get("avg_price") or 0.0),
                    order.get("error"),
                    now,
                    now,
                ),
            )
            return int(cursor.lastrowid or 0)

    def open_orders(self, pair: str | None = None) -> list[dict[str, Any]]:
        """Orders in unfinished states (live/partially_filled)."""
        query = "SELECT * FROM orders WHERE state IN ('live','partially_filled','pending')"
        params: tuple[Any, ...] = ()
        if pair:
            query += " AND pair = ?"
            params = (pair,)
        with self._cursor() as cursor:
            rows = cursor.execute(query + " ORDER BY id DESC", params).fetchall()
        return [dict(row) for row in rows]

    def count_open_positions(self) -> int:
        """Number of open positions (based on buy orders in the live state)."""
        with self._cursor() as cursor:
            row = cursor.execute(
                "SELECT COUNT(*) AS n FROM orders WHERE side='buy' AND state IN ('live','partially_filled')"
            ).fetchone()
        return int(row["n"]) if row else 0

    # ------------------------------------------------------------ statistics
    def _ensure_day(self, cursor: sqlite3.Cursor, day: str) -> sqlite3.Row:
        cursor.execute(
            """INSERT INTO daily_stats (day, realized_pnl_usdt, equity_start_usdt, signals_count,
                                        orders_count, updated_at)
               VALUES (?, 0, 0, 0, 0, ?)
               ON CONFLICT(day) DO NOTHING""",
            (day, utc_now_iso()),
        )
        return cursor.execute("SELECT * FROM daily_stats WHERE day = ?", (day,)).fetchone()

    def get_daily_stats(self, day: str | None = None) -> dict[str, Any]:
        """Returns daily statistics (defaults to today, UTC)."""
        target = day or utc_today()
        with self._cursor() as cursor:
            row = self._ensure_day(cursor, target)
        return dict(row) if row else {}

    def set_equity_start(self, amount_usdt: float, day: str | None = None) -> None:
        """Sets the day's starting capital (if not set yet)."""
        target = day or utc_today()
        with self._cursor() as cursor:
            self._ensure_day(cursor, target)
            cursor.execute(
                "UPDATE daily_stats SET equity_start_usdt = ?, updated_at = ? "
                "WHERE day = ? AND equity_start_usdt = 0",
                (float(amount_usdt), utc_now_iso(), target),
            )

    def add_realized_pnl(self, pnl_usdt: float, day: str | None = None) -> float:
        """Adds the day's realized P&L and returns the new total."""
        target = day or utc_today()
        with self._cursor() as cursor:
            self._ensure_day(cursor, target)
            cursor.execute(
                "UPDATE daily_stats SET realized_pnl_usdt = realized_pnl_usdt + ?, updated_at = ? "
                "WHERE day = ?",
                (float(pnl_usdt), utc_now_iso(), target),
            )
            row = cursor.execute(
                "SELECT realized_pnl_usdt FROM daily_stats WHERE day = ?", (target,)
            ).fetchone()
        return float(row["realized_pnl_usdt"]) if row else 0.0

    def increment_signals(self, day: str | None = None) -> int:
        """Increments the daily signal counter and returns the new value."""
        target = day or utc_today()
        with self._cursor() as cursor:
            self._ensure_day(cursor, target)
            cursor.execute(
                "UPDATE daily_stats SET signals_count = signals_count + 1, updated_at = ? WHERE day = ?",
                (utc_now_iso(), target),
            )
            row = cursor.execute("SELECT signals_count FROM daily_stats WHERE day = ?", (target,)).fetchone()
        return int(row["signals_count"]) if row else 0

    # ------------------------------------------------------------ market data
    def insert_market_snapshots(self, rows: list[dict[str, Any]]) -> None:
        """Stores market snapshots from the collector."""
        now = utc_now_iso()
        with self._cursor() as cursor:
            cursor.executemany(
                """INSERT INTO market_snapshots
                   (pair, price, rsi, ma20, ma200, regime, atr, atr_pct, vol_ratio, bar,
                    change_24h_pct, market_cap, ts)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                [
                    (
                        row["pair"],
                        float(row.get("price") or 0.0),
                        row.get("rsi"),
                        row.get("ma20"),
                        row.get("ma200"),
                        row.get("regime"),
                        row.get("atr"),
                        row.get("atr_pct"),
                        row.get("vol_ratio"),
                        row.get("bar"),
                        row.get("change_24h_pct"),
                        row.get("market_cap"),
                        row.get("ts", now),
                    )
                    for row in rows
                ],
            )

    def latest_snapshot(self, pair: str) -> dict[str, Any] | None:
        """Latest stored snapshot for a pair."""
        with self._cursor() as cursor:
            row = cursor.execute(
                "SELECT * FROM market_snapshots WHERE pair = ? ORDER BY id DESC LIMIT 1", (pair,)
            ).fetchone()
        return dict(row) if row else None

    # --------------------------------------------------------------- key/value
    def set_kv(self, key: str, value: str) -> None:
        """Stores a value in the key/value table (e.g. a cooldown timestamp)."""
        with self._cursor() as cursor:
            cursor.execute(
                """INSERT INTO kv (key, value, updated_at) VALUES (?, ?, ?)
                   ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at""",
                (key, value, utc_now_iso()),
            )

    def get_kv(self, key: str) -> str | None:
        """Reads a value from the key/value table."""
        with self._cursor() as cursor:
            row = cursor.execute("SELECT value FROM kv WHERE key = ?", (key,)).fetchone()
        return str(row["value"]) if row else None
