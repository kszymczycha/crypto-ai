"""Executor HTTP (FastAPI) — the only process with OKX secrets.

Endpoints (internal `botnet` network only; port not published to the host):
- GET  /health   → process status and mode (demo/live),
- POST /signal   → accepts a JSON signal, validates risk, places the order,
- GET  /state    → balance, open orders, daily P&L,
- POST /cancel   → cancels an order by ordId.

Run: `python -m okxbot.executor.service`.
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from typing import Any

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field, field_validator

from okxbot.client.okx_client import OkxApiError, OkxClient
from okxbot.config import get_risk_limits, get_settings, get_symbols, load_credentials
from okxbot.data.market import MarketService
from okxbot.executor.order_router import OrderRouter
from okxbot.executor.risk_engine import MAX_REASON_LENGTH, RiskEngine, Signal
from okxbot.logging import setup_logging
from okxbot.storage.db import StateStore
from okxbot.strategy.rules import StrategyConfig, evaluate

log = setup_logging("executor", stdout=True)


class SignalRequest(BaseModel):
    """Signal payload from OpenCode (shape validation, not policy)."""

    pair: str = Field(..., min_length=5, max_length=20)
    side: str = Field(..., pattern="^(buy|sell)$")
    amount_usdt: float = Field(..., gt=0)
    reason: str = Field(..., min_length=5, max_length=MAX_REASON_LENGTH)
    idempotency_key: str = Field(..., min_length=8, max_length=64)
    tp_pct: float | None = Field(default=None, gt=0, le=20)
    sl_pct: float | None = Field(default=None, gt=0, le=20)

    @field_validator("pair")
    @classmethod
    def _upper(cls, value: str) -> str:
        return value.upper()


class CancelRequest(BaseModel):
    """Order cancellation request."""

    order_id: str = Field(..., min_length=3)
    pair: str = Field(..., min_length=5)

    @field_validator("pair")
    @classmethod
    def _upper(cls, value: str) -> str:
        return value.upper()


class ExecutorState:
    """Process resources: OKX client, store, risk engine, router."""

    def __init__(self) -> None:
        """Builds dependencies at application startup."""
        self.settings = get_settings()
        self.store = StateStore(self.settings.db_path)
        self.store.init_schema()
        self.credentials = load_credentials()
        self.client = OkxClient(credentials=self.credentials, settings=self.settings)
        self.symbols = get_symbols()
        self.risk = RiskEngine(get_risk_limits(), self.store, symbols=self.symbols)
        self.router = OrderRouter(self.client, self.store)
        self.market = MarketService(okx=self.client, symbols=self.symbols)
        self.strategy = StrategyConfig()
        self._refresh_equity()

    def _refresh_equity(self) -> float:
        """Refreshes account capital: USDT + valuation of held base assets.

        Without crypto valuation, capital would fall on every buy (USDT goes out,
        the coin stays), and the drawdown circuit would treat a normal entry as a
        loss. Held assets are valued at `bid` — the price at which we could sell
        them right now, so it is liquidation value, not book value.

        Fail-closed: **every** error (network, missing price for a held asset)
        sets equity to 0. `RiskEngine` treats 0 as "unknown" and blocks buys
        (`portfolio_drawdown_limit: equity unknown`), but not sells. Keeping the
        previous value would be more dangerous: the circuit would work on stale
        capital and could let a buy through after a drawdown.
        """
        try:
            balances = self.client.get_balance()
        except Exception as exc:  # network/API: no capital = no measurement
            log.warning("equity_refresh_failed", error=str(exc))
            self.risk.equity_usdt = 0.0
            return 0.0

        total = float(balances.get("USDT", 0.0) or 0.0)
        held: dict[str, float] = {}
        for pair, symbol in self.symbols.items():
            quantity = float(balances.get(symbol.base, 0.0) or 0.0)
            if quantity > 0 and symbol.base != "USDT":
                held[pair] = quantity

        if held:
            try:
                tickers = self.client.get_tickers(list(held))
            except Exception as exc:
                log.warning("equity_valuation_failed", error=str(exc))
                self.risk.equity_usdt = 0.0
                return 0.0
            for pair, quantity in held.items():
                ticker = tickers.get(pair, {})
                price = float(ticker.get("bid") or ticker.get("price") or 0.0)
                if price <= 0:
                    # An asset without a price cannot silently be worth 0 — that
                    # would overstate the drawdown and block trading forever.
                    log.warning("equity_valuation_missing", pair=pair)
                    self.risk.equity_usdt = 0.0
                    return 0.0
                total += quantity * price

        self.risk.equity_usdt = total
        self.store.set_equity_start(total)
        return total

    @property
    def mode(self) -> str:
        """'demo' or 'live' based on the secrets flag."""
        return "demo" if self.credentials.flag == "1" else "live"


_state: ExecutorState | None = None


def get_state() -> ExecutorState:
    """Returns the (lazily created) process state — easy to replace in tests."""
    global _state
    if _state is None:
        _state = ExecutorState()
    return _state


@asynccontextmanager
async def lifespan(_: FastAPI):
    """Initialization at startup and shutdown at stop."""
    state = get_state()
    log.info("executor_start", mode=state.mode, port=state.settings.executor_port)
    yield
    state.store.close()
    log.info("executor_stop")


app = FastAPI(title="okx-bot executor", version="0.1.0", lifespan=lifespan)


@app.get("/health")
def health() -> dict[str, Any]:
    """Simple healthcheck (no OKX calls)."""
    state = get_state()
    return {"ok": True, "mode": state.mode, "flag": state.credentials.flag}


def evaluate_entry(state: ExecutorState, pair: str) -> tuple[bool, str]:
    """Strategy gate: the executor evaluates the entry itself and does not trust the signal reason.

    Calculated on fresh market data (closed candles — see
    `MarketService.snapshot`), not on the collector's last snapshot: that can be
    up to 60 s old, while orders go out immediately. A data-fetch error means
    refusal (fail-closed), not letting the signal through.
    """
    try:
        snapshot = state.market.snapshot([pair])[pair]
    except Exception as exc:  # network/API: no data = no entry
        log.error("strategy_snapshot_failed", pair=pair, error=str(exc))
        return False, f"strategy_unavailable: {exc}"
    evaluation = evaluate(snapshot, state.strategy)
    if not evaluation["allow_entry"]:
        reasons = "; ".join(str(reason) for reason in evaluation["reasons"])
        return False, f"strategy_rejected: hint={evaluation['hint']} ({reasons})"
    return True, ""


def _reject(state: ExecutorState, signal: Signal, reason: str) -> dict[str, Any]:
    """Stores a rejected signal (audit) and returns the `/signal` response."""
    state.store.insert_signal(
        {
            "idempotency_key": signal.idempotency_key,
            "pair": signal.pair,
            "side": signal.side,
            "amount_usdt": signal.amount_usdt,
            "tp_pct": signal.tp_pct,
            "sl_pct": signal.sl_pct,
            "reason": signal.reason,
            "status": "rejected",
        }
    )
    return {"ok": False, "status": "rejected", "order_id": "", "reason": reason}


@app.post("/signal")
def post_signal(request: SignalRequest) -> dict[str, Any]:
    """Validates the signal, enforces risk, and (optionally) places the order."""
    state = get_state()
    signal = Signal(
        pair=request.pair,
        side=request.side,
        amount_usdt=request.amount_usdt,
        reason=request.reason,
        idempotency_key=request.idempotency_key,
        tp_pct=request.tp_pct,
        sl_pct=request.sl_pct,
    )

    existing = state.store.find_signal_by_key(signal.idempotency_key)
    if existing:
        return {
            "ok": True,
            "status": "duplicate",
            "order_id": "",
            "reason": f"signal idempotency_key already accepted (id={existing['id']}, status={existing['status']})",
        }

    # Capital is refreshed before every validation: the drawdown circuit is
    # calculated from the current balance, not from the value before the process
    # restart. An API error yields equity=0, which blocks buys (fail-closed), but
    # not sells.
    state._refresh_equity()

    verdict = state.risk.validate(signal)
    if verdict.status == "rejected":
        log.warning("signal_rejected", pair=signal.pair, reason=verdict.reason)
        return _reject(state, signal, verdict.reason)

    if verdict.status == "needs_confirmation":
        state.store.insert_signal(
            {
                "idempotency_key": signal.idempotency_key,
                "pair": signal.pair,
                "side": signal.side,
                "amount_usdt": signal.amount_usdt,
                "tp_pct": signal.tp_pct,
                "sl_pct": signal.sl_pct,
                "reason": signal.reason,
                "status": "needs_confirmation",
            }
        )
        log.info("signal_needs_confirmation", pair=signal.pair, amount=signal.amount_usdt)
        return {
            "ok": True,
            "status": "needs_confirmation",
            "order_id": "",
            "reason": verdict.reason,
        }

    # The strategy gate applies only to buys. Selling is a manual emergency exit —
    # blocking it with the strategy would make it impossible to close a position.
    if signal.side == "buy":
        allowed, strategy_reason = evaluate_entry(state, signal.pair)
        if not allowed:
            log.warning("signal_strategy_rejected", pair=signal.pair, reason=strategy_reason)
            return _reject(state, signal, strategy_reason)

    signal_id = state.store.insert_signal(
        {
            "idempotency_key": signal.idempotency_key,
            "pair": signal.pair,
            "side": signal.side,
            "amount_usdt": signal.amount_usdt,
            "tp_pct": signal.tp_pct,
            "sl_pct": signal.sl_pct,
            "reason": signal.reason,
            "status": "accepted",
        }
    )
    state.risk.mark_signal(signal.pair)
    state.store.increment_signals()

    try:
        result = state.router.execute(signal, signal_id=signal_id)
    except OkxApiError as exc:
        log.error("signal_execution_failed", pair=signal.pair, error=str(exc))
        return {"ok": False, "status": "failed", "order_id": "", "reason": str(exc)}

    log.info("signal_placed", pair=signal.pair, order_id=result["order_id"], state=result["state"])
    return {
        "ok": True,
        "status": "placed",
        "order_id": result["order_id"],
        "state": result["state"],
        "filled": result["filled"],
        "avg_price": result["avg_price"],
        "tp_price": result["tp_price"],
        "sl_price": result["sl_price"],
        "reason": verdict.reason,
    }


@app.get("/state")
def get_state_endpoint() -> dict[str, Any]:
    """Balance, open orders, and daily P&L (no secrets)."""
    state = get_state()
    try:
        balances = state.client.get_balance("USDT")
        pending = state.client.get_pending_orders()
    except OkxApiError as exc:
        raise HTTPException(status_code=502, detail=f"OKX: {exc}") from exc
    return {
        "ok": True,
        "mode": state.mode,
        "balances": balances,
        "pending_orders": pending,
        "daily": state.risk.daily_summary(),
    }


@app.post("/cancel")
def post_cancel(request: CancelRequest) -> dict[str, Any]:
    """Cancels an order by ordId."""
    state = get_state()
    try:
        order = state.router.cancel(request.order_id, request.pair)
    except OkxApiError as exc:
        raise HTTPException(status_code=502, detail=f"OKX: {exc}") from exc
    return {"ok": True, "order": order}


def main() -> None:
    """Starts the uvicorn server (host/port from settings)."""
    import uvicorn

    settings = get_settings()
    uvicorn.run(
        "okxbot.executor.service:app",
        host=settings.executor_host,
        port=settings.executor_port,
        log_config=None,
    )


if __name__ == "__main__":  # pragma: no cover
    main()
