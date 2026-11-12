"""Data collector: periodically fetches the market and writes it to SQLite.

Runs as a separate container (`python -m okxbot.data.collector`).
It places no orders and has no access to secrets — public data only.
"""

from __future__ import annotations

import asyncio
import signal
from typing import Any

from okxbot.client.coingecko_client import CoinGeckoClient
from okxbot.client.okx_client import OkxClient
from okxbot.config import get_settings, get_symbols
from okxbot.data.market import MarketService
from okxbot.logging import setup_logging
from okxbot.storage.db import StateStore

_stop = asyncio.Event()


def _handle_signal(signum: int) -> None:
    setup_logging("collector", stdout=True).info("collector_signal", signum=signum)
    _stop.set()


async def collect_once(service: MarketService, store: StateStore, pairs: list[str]) -> dict[str, Any]:
    """One cycle: OKX snapshot + CoinGecko, write to the database."""
    snap = await asyncio.to_thread(service.snapshot, pairs)
    context = await service.enrich_with_coingecko(pairs)
    symbols = get_symbols()
    rows: list[dict[str, Any]] = []
    for pair, data in snap.items():
        cg_id = symbols[pair].coingecko if pair in symbols else ""
        rows.append(
            {
                "pair": pair,
                "price": data.get("price", 0.0),
                "rsi": data.get("rsi"),
                "ma20": data.get("ma20"),
                "ma200": data.get("ma200"),
                "regime": data.get("regime"),
                "atr": data.get("atr"),
                "atr_pct": data.get("atr_pct"),
                "vol_ratio": data.get("vol_ratio"),
                "bar": data.get("bar"),
                "change_24h_pct": context.get(cg_id, {}).get("change_24h_pct"),
                "market_cap": context.get(cg_id, {}).get("market_cap"),
            }
        )
    await asyncio.to_thread(store.insert_market_snapshots, rows)
    return {"rows": len(rows), "pairs": pairs}


async def run() -> None:
    """Collector loop with SIGTERM/SIGINT handling."""
    settings = get_settings()
    log = setup_logging("collector", stdout=True)
    store = StateStore(settings.db_path)
    store.init_schema()
    service = MarketService(
        okx=OkxClient(),
        coingecko=CoinGeckoClient(settings=settings),
        symbols=get_symbols(),
    )
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, _handle_signal, sig)
    log.info("collector_start", interval=settings.collector_interval_seconds)
    while not _stop.is_set():
        try:
            summary = await collect_once(service, store, list(settings.collector_pairs))
            log.info("collector_cycle", **summary)
        except Exception as exc:  # a single error does not stop the loop
            log.error("collector_cycle_failed", error=str(exc))
        try:
            await asyncio.wait_for(_stop.wait(), timeout=settings.collector_interval_seconds)
        except TimeoutError:
            continue
    log.info("collector_stop")


def main() -> None:
    """Entry point: `python -m okxbot.data.collector`."""
    try:
        asyncio.run(run())
    except KeyboardInterrupt:  # pragma: no cover
        _stop.set()


if __name__ == "__main__":  # pragma: no cover
    main()
