"""External clients: OKX v5 (REST) and CoinGecko.

Exports are lazy (PEP 562): `from okxbot.client import OkxClient` works, but
importing `okxbot.client.rate_limiter` does not require httpx or python-okx. This
lets purely computational modules (e.g. the rate limiter) be tested without
network dependencies.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:  # pragma: no cover - only for type checkers
    from okxbot.client.coingecko_client import CoinGeckoClient, CoinGeckoError, FileCache
    from okxbot.client.okx_client import OkxApiError, OkxClient, OkxRateLimitError
    from okxbot.client.rate_limiter import TokenBucket

__all__ = [
    "CoinGeckoClient",
    "CoinGeckoError",
    "FileCache",
    "OkxApiError",
    "OkxClient",
    "OkxRateLimitError",
    "TokenBucket",
]

_EXPORTS = {
    "OkxClient": "okxbot.client.okx_client",
    "OkxApiError": "okxbot.client.okx_client",
    "OkxRateLimitError": "okxbot.client.okx_client",
    "CoinGeckoClient": "okxbot.client.coingecko_client",
    "CoinGeckoError": "okxbot.client.coingecko_client",
    "FileCache": "okxbot.client.coingecko_client",
    "TokenBucket": "okxbot.client.rate_limiter",
}


def __getattr__(name: str) -> Any:
    """Imports a symbol from the submodule only on first use."""
    module_path = _EXPORTS.get(name)
    if module_path is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    from importlib import import_module

    return getattr(import_module(module_path), name)


def __dir__() -> list[str]:
    """Shows the lazy exports in `dir()`."""
    return sorted(__all__)
