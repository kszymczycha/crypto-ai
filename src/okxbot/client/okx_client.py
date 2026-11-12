"""OKX API v5 client built on the official `python-okx`.

Responsibilities:
- HMAC signing (handled by the SDK),
- demo/live mode via `flag` ('1' = sandbox, '0' = live),
- rate limiting (token bucket),
- retry with exponential backoff for 429/5xx and OKX rate-limit codes,
- response normalization to plain dicts (never raw payloads to the CLI).

The module is used exclusively by the `executor` container (which holds the secrets).
"""

from __future__ import annotations

import random
import time
import uuid
from typing import Any

from okxbot.client.rate_limiter import TokenBucket, make_private_bucket, make_public_bucket
from okxbot.config import OkxCredentials, Settings, get_settings
from okxbot.logging import setup_logging

# OKX codes meaning rate limit / transient server errors.
RETRYABLE_CODES = {"50011", "50013", "50001", "50004", "50026"}
RETRYABLE_HTTP = {429, 500, 502, 503, 504}


class OkxApiError(RuntimeError):
    """Error returned by OKX (code != '0')."""

    def __init__(self, code: str, message: str, *, endpoint: str = "") -> None:
        """Builds the error from the OKX code and message."""
        self.code = code
        self.message = message
        self.endpoint = endpoint
        super().__init__(f"OKX error {code}: {message} ({endpoint})")


class OkxRateLimitError(OkxApiError):
    """OKX rate limit (code 50011 or HTTP 429)."""


class OkxClient:
    """Thin, testable wrapper around `python-okx` for spot."""

    def __init__(
        self,
        credentials: OkxCredentials | None = None,
        settings: Settings | None = None,
        *,
        public_bucket: TokenBucket | None = None,
        private_bucket: TokenBucket | None = None,
        account_api: Any | None = None,
        trade_api: Any | None = None,
        market_api: Any | None = None,
    ) -> None:
        """Initializes the client. All dependencies can be injected in tests."""
        self.settings = settings or get_settings()
        self.credentials = credentials
        self.public_bucket = public_bucket or make_public_bucket()
        self.private_bucket = private_bucket or make_private_bucket()
        self._log = setup_logging("okx_client")
        self._account_api = account_api
        self._trade_api = trade_api
        self._market_api = market_api

    # ------------------------------------------------------------------ mode
    @property
    def flag(self) -> str:
        """'1' for demo/sandbox, '0' for live."""
        return self.credentials.flag if self.credentials else self.settings.okx_flag

    @property
    def is_demo(self) -> bool:
        """True when the client runs on the sandbox."""
        return self.flag == "1"

    # ------------------------------------------------------- SDK (lazy init)
    def _require_credentials(self) -> OkxCredentials:
        if self.credentials is None:
            raise RuntimeError("OKX secrets missing — the private client requires load_credentials()")
        return self.credentials

    def _init_public_sdk(self) -> None:
        """Initializes the market client. Does not require secrets.

        OKX public endpoints are not signed, so the collector and CLI can fetch
        prices without access to API keys — this is intentional (the collector has
        no secrets and must not have any).
        """
        if self._market_api:
            return
        try:
            import okx.MarketData as MarketData  # type: ignore[import-not-found]
        except ImportError as exc:  # pragma: no cover - depends on the Docker image
            raise RuntimeError("The python-okx package is missing in the environment") from exc
        self._market_api = MarketData.MarketAPI(flag=self.flag)

    def _init_private_sdk(self) -> None:
        """Initializes the signed clients. Requires secrets.

        The key check intentionally precedes the early return: injecting SDK
        clients (tests) must not disable the check. Without it the executor could
        place an order on an unauthenticated path and nobody would notice.
        """
        creds = self._require_credentials()
        if self._account_api and self._trade_api:
            return
        try:
            import okx.Account as Account  # type: ignore[import-not-found]
            import okx.Trade as Trade  # type: ignore[import-not-found]
        except ImportError as exc:  # pragma: no cover - depends on the Docker image
            raise RuntimeError("The python-okx package is missing in the environment") from exc
        if not self._account_api:
            self._account_api = Account.AccountAPI(
                creds.api_key, creds.secret_key, creds.passphrase, False, creds.flag
            )
        if not self._trade_api:
            self._trade_api = Trade.TradeAPI(
                creds.api_key, creds.secret_key, creds.passphrase, False, creds.flag
            )

    # ------------------------------------------------------------- retry
    def _call(self, endpoint: str, fn: Any, *args: Any, **kwargs: Any) -> Any:
        """Calls the SDK with rate limiting and retry. Returns the raw SDK dict."""
        bucket = self.private_bucket if endpoint.startswith("private:") else self.public_bucket
        last_exc: Exception | None = None
        for attempt in range(self.settings.http_max_retries + 1):
            bucket.acquire()
            try:
                result = fn(*args, **kwargs)
                code = str(result.get("code", "0")) if isinstance(result, dict) else "0"
                if code == "0":
                    return result
                message = str(result.get("msg", "unknown error"))
                if code in RETRYABLE_CODES:
                    raise OkxRateLimitError(code, message, endpoint=endpoint)
                raise OkxApiError(code, message, endpoint=endpoint)
            except OkxRateLimitError as exc:
                last_exc = exc
                delay = min(
                    self.settings.backoff_max_seconds,
                    self.settings.backoff_base_seconds * (2**attempt),
                )
                delay *= 0.75 + random.random() / 2  # jitter
                self._log.warning("okx_retry", endpoint=endpoint, attempt=attempt, delay=round(delay, 2))
                time.sleep(delay)
        raise OkxRateLimitError("429", "rate limit exceeded", endpoint=endpoint) from last_exc

    # -------------------------------------------------------- public
    def get_tickers(self, pairs: list[str]) -> dict[str, dict[str, Any]]:
        """Returns tickers for the pairs: last, bid, ask, vol24h."""
        self._init_public_sdk()
        out: dict[str, dict[str, Any]] = {}
        for pair in pairs:
            raw = self._call("public:get_ticker", self._market_api.get_ticker, instId=pair)
            data = (raw.get("data") or [{}])[0]
            out[pair] = {
                "price": _to_float(data.get("last")),
                "bid": _to_float(data.get("bidPx")),
                "ask": _to_float(data.get("askPx")),
                "vol24h": _to_float(data.get("vol24h")),
                "ts": data.get("ts"),
            }
        return out

    def get_candles(self, pair: str, *, bar: str = "1m", limit: int = 100) -> list[dict[str, Any]]:
        """Returns OHLCV candles, oldest to newest."""
        self._init_public_sdk()
        raw = self._call(
            "public:get_candlesticks",
            self._market_api.get_candlesticks,
            instId=pair,
            bar=bar,
            limit=str(limit),
        )
        rows = raw.get("data") or []
        candles = [
            {
                "ts": row[0],
                "open": _to_float(row[1]),
                "high": _to_float(row[2]),
                "low": _to_float(row[3]),
                "close": _to_float(row[4]),
                "vol": _to_float(row[5]),
            }
            for row in rows
            if len(row) >= 6
        ]
        candles.reverse()  # OKX returns newest first
        return candles

    # --------------------------------------------------------- private
    def get_balance(self, ccy: str | None = None) -> dict[str, float]:
        """Returns the account balance as {currency: total balance}.

        We take the **larger** of `availBal` and `cashBal`, not `availBal or cashBal`.
        Reason: OKX returns these fields as strings, and `"0"` is truthy in Python.
        TP/SL orders freeze assets, so `availBal` can be `"0"` while `cashBal` is
        non-zero — the old version then reported 0.0 and lost the coin actually
        held. For capital valuation (the drawdown circuit breaker) what matters
        is the total balance, not what can be sold right now.

        Consequence for callers: this is not "the amount available for trading".
        A sale based on this value may exceed `availBal`; OKX will reject it
        (fail-safe) rather than sell less without the caller's knowledge.
        """
        self._init_private_sdk()
        kwargs = {"ccy": ccy} if ccy else {}
        raw = self._call("private:get_balance", self._account_api.get_account_balance, **kwargs)
        balances: dict[str, float] = {}
        for account in raw.get("data") or []:
            for detail in account.get("details") or []:
                currency = str(detail.get("ccy", ""))
                if not currency:
                    continue
                # Convert before comparing: "" and None give 0.0, and "0" must not
                # win just because it is a non-empty string.
                available = _to_float(detail.get("availBal"))
                cash = _to_float(detail.get("cashBal"))
                balances[currency] = max(available, cash)
        return balances

    def get_pending_orders(self, pair: str | None = None) -> list[dict[str, Any]]:
        """Returns open spot orders."""
        self._init_private_sdk()
        kwargs: dict[str, Any] = {"instType": "SPOT"}
        if pair:
            kwargs["instId"] = pair
        raw = self._call("private:get_orders_pending", self._trade_api.get_order_list, **kwargs)
        return [_normalize_order(item) for item in raw.get("data") or []]

    def place_order(
        self,
        *,
        pair: str,
        side: str,
        amount_usdt: float | None = None,
        amount_base: float | None = None,
        order_type: str = "market",
        price: float | None = None,
        tp_price: float | None = None,
        sl_price: float | None = None,
        client_order_id: str | None = None,
    ) -> dict[str, Any]:
        """Places a spot order. For a buy, `amount_usdt` (tgtCcy=quote_ccy).

        For a sell, pass `amount_base`. TP/SL are passed as the
        `tpTriggerPx`/`slTriggerPx` parameters (supported by OKX for spot market).
        """
        self._init_private_sdk()
        if order_type not in {"market", "limit"}:
            raise ValueError("order_type must be 'market' or 'limit'")
        if order_type == "limit" and price is None:
            raise ValueError("A limit order requires the price parameter")
        if side == "buy" and amount_usdt is None:
            raise ValueError("A buy requires amount_usdt")
        if side == "sell" and amount_base is None:
            raise ValueError("A sell requires amount_base")
        kwargs: dict[str, Any] = {
            "instId": pair,
            "tdMode": "cash",
            "side": side,
            "ordType": order_type,
        }
        if client_order_id:
            # OKX accepts only alphanumerics (max 32) — idempotency_key is often a UUID.
            kwargs["clOrdId"] = _sanitize_client_order_id(client_order_id)
        if side == "buy":
            kwargs["sz"] = _fmt(amount_usdt)
            kwargs["tgtCcy"] = "quote_ccy"
        else:
            kwargs["sz"] = _fmt(amount_base)
        if order_type == "limit":
            kwargs["px"] = _fmt(price)
        if tp_price is not None:
            kwargs["tpTriggerPx"] = _fmt(tp_price)
        if sl_price is not None:
            kwargs["slTriggerPx"] = _fmt(sl_price)
        raw = self._call("private:place_order", self._trade_api.place_order, **kwargs)
        data = (raw.get("data") or [{}])[0]
        order = _normalize_order(data)
        order["request"] = {
            "pair": pair,
            "side": side,
            "order_type": order_type,
            "amount_usdt": amount_usdt,
            "amount_base": amount_base,
        }
        return order

    def cancel_order(self, order_id: str, pair: str) -> dict[str, Any]:
        """Cancels an order by `ordId`."""
        self._init_private_sdk()
        raw = self._call(
            "private:cancel_order",
            self._trade_api.cancel_order,
            instId=pair,
            ordId=order_id,
        )
        return _normalize_order((raw.get("data") or [{}])[0])


def _to_float(value: Any) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def _sanitize_client_order_id(raw: str) -> str:
    """Normalizes a client identifier to the OKX format (alphanumerics, max 32)."""
    cleaned = "".join(char for char in str(raw) if char.isalnum())
    return cleaned[:32] or uuid.uuid4().hex[:32]


def _fmt(value: float | None) -> str:
    return f"{value:.8f}".rstrip("0").rstrip(".") if value is not None else ""


def _normalize_order(item: dict[str, Any]) -> dict[str, Any]:
    """Reduces the OKX response to a stable, minimal shape."""
    return {
        "order_id": item.get("ordId", ""),
        "client_order_id": item.get("clOrdId", ""),
        "pair": item.get("instId", ""),
        "side": item.get("side", ""),
        "state": item.get("state", item.get("ordState", "")),
        "amount": _to_float(item.get("sz")),
        "filled": _to_float(item.get("accFillSz") or item.get("fillSz")),
        "avg_price": _to_float(item.get("avgPx")),
        "fee": _to_float(item.get("fee")),
        "ts": item.get("cTime") or item.get("uTime"),
    }
