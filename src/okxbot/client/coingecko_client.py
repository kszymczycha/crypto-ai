"""CoinGecko client with an on-disk cache and a rate limiter (Demo plan: ~100 req/min).

Used by `collector` for market data (prices, market cap, 24h change).
Data is written to the cache and to `data/state.db`; no API keys required.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import httpx

from okxbot.client.rate_limiter import TokenBucket, make_coingecko_bucket
from okxbot.config import Settings, get_settings
from okxbot.data.cache import FileCache
from okxbot.logging import setup_logging

__all__ = ["CoinGeckoClient", "CoinGeckoError", "FileCache"]

RETRYABLE_STATUS = {429, 500, 502, 503, 504}


class CoinGeckoError(RuntimeError):
    """CoinGecko error (HTTP or network)."""


class CoinGeckoClient:
    """Asynchronous CoinGecko client with cache and backoff."""

    def __init__(
        self,
        settings: Settings | None = None,
        *,
        bucket: TokenBucket | None = None,
        http_client: httpx.AsyncClient | None = None,
        cache: FileCache | None = None,
    ) -> None:
        """Initializes the client. Dependencies are injectable in tests."""
        self.settings = settings or get_settings()
        self.bucket = bucket or make_coingecko_bucket()
        self._http = http_client
        self.cache = cache or FileCache(Path(self.settings.cache_dir))
        self._log = setup_logging("coingecko")

    async def _get(self, path: str, params: dict[str, Any]) -> Any:
        url = f"{self.settings.coingecko_base_url}{path}"
        client = self._http or httpx.AsyncClient(timeout=self.settings.http_timeout_seconds)
        owns_client = self._http is None
        last_error: Exception | None = None
        try:
            for attempt in range(self.settings.http_max_retries + 1):
                await _async_acquire(self.bucket)
                try:
                    response = await client.get(url, params=params)
                    if response.status_code == 200:
                        return response.json()
                    if response.status_code in RETRYABLE_STATUS:
                        raise CoinGeckoError(f"HTTP {response.status_code}")
                    raise CoinGeckoError(f"HTTP {response.status_code}: {response.text[:200]}")
                except (httpx.HTTPError, CoinGeckoError) as exc:
                    last_error = exc
                    delay = min(
                        self.settings.backoff_max_seconds,
                        self.settings.backoff_base_seconds * (2**attempt),
                    )
                    self._log.warning(
                        "coingecko_retry", path=path, attempt=attempt, delay=round(delay, 2),
                        error=str(exc),
                    )
                    if attempt < self.settings.http_max_retries:
                        import asyncio

                        await asyncio.sleep(delay)
            raise CoinGeckoError(f"CoinGecko did not respond: {last_error}")
        finally:
            if owns_client:
                await client.aclose()

    async def get_prices(self, coingecko_ids: list[str], vs: str = "usd") -> dict[str, dict[str, float]]:
        """Prices and 24h stats for the given ids (cache TTL 60 s)."""
        key = f"simple_price:{vs}:{','.join(sorted(coingecko_ids))}"
        cached = self.cache.get(key, self.settings.cache_ttl_prices_seconds)
        if cached is not None:
            return cached
        raw = await self._get(
            "/simple/price",
            {
                "ids": ",".join(coingecko_ids),
                "vs_currencies": vs,
                "include_24hr_change": "true",
                "include_market_cap": "true",
            },
        )
        prices: dict[str, dict[str, float]] = {}
        for cg_id, values in (raw or {}).items():
            prices[cg_id] = {
                "price": float(values.get(vs, 0.0)),
                "change_24h_pct": float(values.get(f"{vs}_24h_change", 0.0)),
                "market_cap": float(values.get(f"{vs}_market_cap", 0.0)),
            }
        self.cache.set(key, prices)
        return prices

    async def get_coin_metadata(self, coingecko_id: str) -> dict[str, Any]:
        """Asset metadata (cache TTL 300 s) — symbol, name, category."""
        key = f"coins:{coingecko_id}"
        cached = self.cache.get(key, self.settings.cache_ttl_metadata_seconds)
        if cached is not None:
            return cached
        raw = await self._get(f"/coins/{coingecko_id}", {"localization": "false", "tickers": "false"})
        meta = {
            "id": raw.get("id", coingecko_id),
            "symbol": raw.get("symbol", ""),
            "name": raw.get("name", ""),
            "market_cap_rank": raw.get("market_cap_rank"),
        }
        self.cache.set(key, meta)
        return meta


async def _async_acquire(bucket: TokenBucket) -> None:
    """Waits for a token without blocking (the bucket is synchronous)."""
    import asyncio

    while not bucket.try_acquire():
        await asyncio.sleep(0.05)
