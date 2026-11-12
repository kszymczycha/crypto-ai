"""Market data: indicators, cache, market service and collector.

Note: this package deliberately does NOT import submodules eagerly. This lets
`okxbot.data.cache` (a leaf module) be used by clients from `okxbot.client`
without an import cycle. Import concrete modules:

    from okxbot.data.indicators import compute_snapshot
    from okxbot.data.market import MarketService
    from okxbot.data.cache import FileCache
"""

__all__ = ["cache", "collector", "indicators", "market"]
