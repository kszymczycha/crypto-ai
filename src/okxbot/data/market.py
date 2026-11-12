"""Market data service: combines OKX (prices/candles) and CoinGecko (context)."""

from __future__ import annotations

import asyncio
import time
from typing import Any

from okxbot.client.coingecko_client import CoinGeckoClient
from okxbot.client.okx_client import OkxClient
from okxbot.config import Symbol, get_settings, get_symbols
from okxbot.data.indicators import compute_snapshot, signal_hint
from okxbot.logging import setup_logging


def drop_forming_candle(
    candles: list[dict[str, Any]], *, now_ms: int | None = None
) -> list[dict[str, Any]]:
    """Returns the series without the last, still-forming candle.

    OKX's `get_candlesticks` also returns the current, unclosed candle. Computing
    on it understates `vol_ratio` (partial volume vs the SMA of full candles) and
    makes the hint turn on and off within the same bar. All indicators must be
    computed on closed candles only.

    The bar length is derived from the difference of the last two timestamps, so
    `bar` does not need to be parsed. When the time cannot be derived (a single
    element, missing/non-numeric `ts`, non-increasing timestamps), the last candle
    is dropped — fail-closed: better to lose a bar than to compute indicators on
    an incomplete one.
    """
    if not candles:
        return candles
    if len(candles) < 2:
        return candles[:-1]
    try:
        last_ts = int(candles[-1]["ts"])
        previous_ts = int(candles[-2]["ts"])
    except (KeyError, TypeError, ValueError):
        return candles[:-1]
    interval = last_ts - previous_ts
    if interval <= 0:
        return candles[:-1]
    now = now_ms if now_ms is not None else int(time.time() * 1000)
    if last_ts + interval > now:
        return candles[:-1]
    return candles


class MarketService:
    """Provides the market snapshot and signal hints for the CLI/model."""

    def __init__(
        self,
        *,
        okx: OkxClient | None = None,
        coingecko: CoinGeckoClient | None = None,
        symbols: dict[str, Symbol] | None = None,
    ) -> None:
        """Injectable clients; lazy instances by default."""
        self.settings = get_settings()
        self.symbols = symbols or get_symbols()
        self._okx = okx
        self._coingecko = coingecko
        self._log = setup_logging("market")

    def _okx_client(self) -> OkxClient:
        if self._okx is None:
            # The public client needs no secrets — the flag from env is enough.
            self._okx = OkxClient()
        return self._okx

    def _cg_client(self) -> CoinGeckoClient:
        if self._coingecko is None:
            self._coingecko = CoinGeckoClient()
        return self._coingecko

    def snapshot(self, pairs: list[str] | None = None, *, bar: str | None = None) -> dict[str, Any]:
        """OKX snapshot: price, indicators, regime, volatility and hint.

        `bar` overrides the candle horizon from the configuration (default
        `settings.candle_bar`). Indicators are computed on the same horizon on
        which the decision is made — see docs/strategy.md.
        """
        requested = pairs or list(self.settings.collector_pairs)
        unknown = [pair for pair in requested if pair not in self.symbols]
        if unknown:
            raise ValueError(f"Unknown pairs: {', '.join(unknown)}")
        okx = self._okx_client()
        tickers = okx.get_tickers(requested)
        candle_bar = bar or self.settings.candle_bar
        candle_limit = max(
            self.settings.candle_limit,
            self.settings.trend_ma_period + 1,
            self.settings.atr_period + 1,
            self.settings.vol_period,
        )
        result: dict[str, Any] = {}
        for pair in requested:
            candles = drop_forming_candle(
                okx.get_candles(pair, bar=candle_bar, limit=candle_limit)
            )
            snap = compute_snapshot(
                candles,
                rsi_period=self.settings.rsi_period,
                ma_period=self.settings.ma_period,
                trend_ma_period=self.settings.trend_ma_period,
                atr_period=self.settings.atr_period,
                vol_period=self.settings.vol_period,
            )
            snap["bar"] = candle_bar
            snap["bid"] = tickers.get(pair, {}).get("bid", 0.0)
            snap["ask"] = tickers.get(pair, {}).get("ask", 0.0)
            snap["hint"] = signal_hint(snap)
            result[pair] = snap
        self._log.info("market_snapshot", pairs=requested, bar=candle_bar)
        return result

    async def enrich_with_coingecko(self, pairs: list[str]) -> dict[str, Any]:
        """Adds the 24h change and market cap from CoinGecko (60 s cache)."""
        cg_ids = [self.symbols[pair].coingecko for pair in pairs if self.symbols.get(pair)]
        if not cg_ids:
            return {}
        return await self._cg_client().get_prices([cid for cid in cg_ids if cid])

    def snapshot_with_context(
        self, pairs: list[str] | None = None, *, bar: str | None = None
    ) -> dict[str, Any]:
        """OKX snapshot + CoinGecko context (synchronous asyncio wrapper)."""
        requested = pairs or list(self.settings.collector_pairs)
        snap = self.snapshot(requested, bar=bar)
        try:
            context = asyncio.run(self.enrich_with_coingecko(requested))
        except Exception as exc:  # CoinGecko is optional — it does not block the snapshot
            self._log.warning("coingecko_unavailable", error=str(exc))
            context = {}
        for pair, data in snap.items():
            cg_id = self.symbols[pair].coingecko if pair in self.symbols else ""
            data["coingecko"] = context.get(cg_id, {})
        return snap
