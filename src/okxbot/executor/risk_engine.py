"""Risk engine: validates a signal BEFORE anything reaches OKX.

Rule: every signal goes through `validate()`; on violation the verdict is
`rejected` with a readable reason. Threshold values come exclusively from
`config/risk_limits.yaml` — never from the signal payload (the model cannot
override them).

Portfolio-level stop: the engine tracks the capital High-Water Mark (HWM) — the
highest recorded `equity_usdt`. A drawdown from the peak above
`max_portfolio_drawdown_pct` blocks every new BUY. This is not a daily limit:
the HWM only moves up and does not reset at midnight, so it protects against a
series of losing days that a single day does not exceed.

**Wiring (known limitation of this iteration):** the HWM is updated only when
someone sets `equity_usdt`. Today only `executor/service.py` does that at
process start, so the HWM covers capital at process-start moments, not at every
order — and `add_realized_pnl()` is not called anywhere in `src/`, so P&L from
closed orders does not reach the balance. The circuit is logically correct and
will not let a buy through after the threshold is crossed, but full protection
only comes from refreshing capital before validating a signal (a separate change
in `service.py`).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

from okxbot.config import RiskLimits, Symbol
from okxbot.storage.db import StateStore

MAX_REASON_LENGTH = 280

# Key in `kv` for the High-Water Mark. In the database, not process memory: the
# HWM must survive an executor restart — otherwise a restart after a drawdown
# resets the protection.
HWM_KEY = "portfolio_hwm_usdt"


@dataclass(frozen=True)
class Signal:
    """Incoming signal from OpenCode (Pydantic-validated in the API)."""

    pair: str
    side: str
    amount_usdt: float
    reason: str
    idempotency_key: str
    tp_pct: float | None = None
    sl_pct: float | None = None


@dataclass(frozen=True)
class Verdict:
    """Validation result: allowed | needs_confirmation | rejected."""

    status: str
    reason: str = ""
    checks: tuple[str, ...] = field(default_factory=tuple)

    @property
    def allowed(self) -> bool:
        """True when the signal can be executed without extra confirmation."""
        return self.status == "allowed"


class RiskEngine:
    """Enforces limits: amount, positions, daily P&L, cooldown, signal counter."""

    def __init__(
        self,
        limits: RiskLimits,
        store: StateStore,
        *,
        symbols: dict[str, Symbol] | None = None,
        equity_usdt: float = 0.0,
    ) -> None:
        """`equity_usdt` is the account capital used for percentage limits."""
        self.limits = limits
        self.store = store
        self.symbols = symbols or {}
        self._equity_usdt = 0.0
        self.equity_usdt = equity_usdt

    @property
    def equity_usdt(self) -> float:
        """Last recorded account capital (0.0 = unknown)."""
        return self._equity_usdt

    @equity_usdt.setter
    def equity_usdt(self, value: float) -> None:
        """Sets capital and raises the HWM if a new peak was just reached.

        A setter instead of a plain attribute, because the HWM must not depend
        on whether the caller remembered to update it. Every path that knows the
        capital (today: process start in `service.py`) updates the peak
        automatically.
        """
        self._equity_usdt = float(value)
        self.observe_equity(self._equity_usdt)

    def observe_equity(self, equity_usdt: float) -> float:
        """Records capital: raises the HWM when it is a new peak; returns the HWM.

        The HWM moves **up only** and does not reset at the end of the day. An
        unknown value (<= 0) is ignored — it cannot be used to conclude that
        capital fell, and resetting the peak would disable the circuit.
        """
        hwm = self.portfolio_hwm()
        if equity_usdt <= 0:
            return hwm
        if equity_usdt > hwm:
            self.store.set_kv(HWM_KEY, repr(float(equity_usdt)))
            return float(equity_usdt)
        return hwm

    def portfolio_hwm(self) -> float:
        """Reads the HWM from storage; 0.0 when it is missing or corrupted."""
        raw = self.store.get_kv(HWM_KEY)
        if not raw:
            return 0.0
        try:
            value = float(raw)
        except ValueError:
            # A corrupted value is treated as missing, not as a zero peak:
            # a negative or unparsable HWM must not disable the circuit.
            return 0.0
        return value if value > 0 else 0.0

    def portfolio_drawdown_pct(self) -> float | None:
        """Drawdown from the HWM in percent; None when capital or HWM is unknown."""
        hwm = self.portfolio_hwm()
        if hwm <= 0 or self.equity_usdt <= 0:
            return None
        return max(0.0, (hwm - self.equity_usdt) / hwm * 100.0)

    def _now(self) -> datetime:
        return datetime.now(UTC)

    def _last_signal_at(self, pair: str) -> datetime | None:
        raw = self.store.get_kv(f"last_signal_at:{pair}")
        if not raw:
            return None
        try:
            return datetime.fromisoformat(raw)
        except ValueError:
            return None

    def validate(self, signal: Signal) -> Verdict:
        """Checks the signal against all risk limits."""
        checks: list[str] = []
        limits = self.limits

        if signal.side not in limits.allowed_sides:
            return Verdict(
                "rejected",
                f"side '{signal.side}' is not allowed",
                ("side",),
            )
        checks.append("side")

        if signal.pair not in self.symbols:
            return Verdict("rejected", f"pair '{signal.pair}' is not supported", ("pair",))
        checks.append("pair")

        if not signal.reason or len(signal.reason.strip()) < 5:
            return Verdict("rejected", "missing reason", ("reason",))
        if len(signal.reason) > MAX_REASON_LENGTH:
            return Verdict("rejected", f"reason is longer than {MAX_REASON_LENGTH} characters", ("reason",))
        checks.append("reason")

        if not signal.idempotency_key:
            return Verdict("rejected", "missing idempotency_key", ("idempotency",))
        checks.append("idempotency")

        amount = float(signal.amount_usdt)
        if amount < limits.min_order_usdt:
            return Verdict("rejected", f"amount {amount} < min_order_usdt {limits.min_order_usdt}", ("amount",))
        if amount > limits.max_order_usdt:
            return Verdict("rejected", f"amount {amount} > max_order_usdt {limits.max_order_usdt}", ("amount",))
        symbol_limit = self.symbols[signal.pair].max_order_usdt
        if amount > symbol_limit:
            return Verdict("rejected", f"amount {amount} > pair limit {symbol_limit}", ("amount",))
        checks.append("amount")

        if self.equity_usdt > 0:
            position_cap = self.equity_usdt * limits.max_position_pct / 100.0
            if amount > position_cap:
                return Verdict(
                    "rejected",
                    f"amount {amount} exceeds {limits.max_position_pct}% of equity ({position_cap:.2f})",
                    ("position_pct",),
                )
        checks.append("position_pct")

        if self.store.count_open_positions() >= limits.max_open_positions:
            return Verdict("rejected", f"max_open_positions={limits.max_open_positions} reached", ("open_positions",))
        checks.append("open_positions")

        # Portfolio-level stop. Checked before the daily counters: a drawdown
        # from the peak is more important than daily P&L, and the rejection
        # should be the rule the user sees in the reason.
        #
        # Selling is excluded from the limit: closing a position during a
        # drawdown is desirable. Blocking sells while the circuit is tripped
        # would lock the user into the position the circuit is meant to protect
        # against.
        if signal.side == "buy":
            drawdown = self.portfolio_drawdown_pct()
            if drawdown is None:
                # Fail-closed: without capital the drawdown cannot be measured,
                # so it must not be assumed absent. The daily loss limit behaves
                # the opposite way today (it silently disables) — that is its gap.
                return Verdict(
                    "rejected",
                    "portfolio_drawdown_limit: equity unknown "
                    "(no capital to measure drawdown)",
                    (*checks, "portfolio_drawdown"),
                )
            if drawdown >= limits.max_portfolio_drawdown_pct:
                hwm = self.portfolio_hwm()
                return Verdict(
                    "rejected",
                    f"portfolio_drawdown_limit: drawdown {drawdown:.2f}% from peak "
                    f"{hwm:.2f} ≥ {limits.max_portfolio_drawdown_pct}%",
                    (*checks, "portfolio_drawdown"),
                )
            checks.append("portfolio_drawdown")

        stats = self.store.get_daily_stats()
        if int(stats.get("signals_count", 0)) >= limits.max_signals_per_day:
            return Verdict("rejected", f"max_signals_per_day={limits.max_signals_per_day} reached", ("daily_signals",))
        checks.append("daily_signals")

        equity_start = float(stats.get("equity_start_usdt") or 0.0)
        realized = float(stats.get("realized_pnl_usdt") or 0.0)
        if equity_start > 0:
            loss_pct = -realized / equity_start * 100.0 if realized < 0 else 0.0
            if loss_pct >= limits.max_daily_loss_pct:
                return Verdict(
                    "rejected",
                    f"daily loss limit {limits.max_daily_loss_pct}% reached ({loss_pct:.2f}%)",
                    ("daily_loss",),
                )
        checks.append("daily_loss")

        last_at = self._last_signal_at(signal.pair)
        if last_at is not None:
            earliest = last_at + timedelta(seconds=limits.cooldown_seconds_between_signals)
            if self._now() < earliest:
                return Verdict("rejected", f"pair cooldown until {earliest.isoformat()}", ("cooldown",))
        checks.append("cooldown")

        if amount > limits.require_confirmation_above_usdt:
            return Verdict(
                "needs_confirmation",
                f"amount {amount} exceeds confirmation threshold {limits.require_confirmation_above_usdt}",
                tuple(checks),
            )

        return Verdict("allowed", "", tuple(checks))

    def mark_signal(self, pair: str) -> None:
        """Stores the last signal timestamp for a pair (cooldown)."""
        self.store.set_kv(f"last_signal_at:{pair}", self._now().isoformat())

    def daily_summary(self) -> dict[str, Any]:
        """Daily summary: P&L, counters, limits."""
        stats = self.store.get_daily_stats()
        return {
            "day": stats.get("day"),
            "realized_pnl_usdt": float(stats.get("realized_pnl_usdt") or 0.0),
            "equity_start_usdt": float(stats.get("equity_start_usdt") or 0.0),
            "signals_count": int(stats.get("signals_count") or 0),
            "max_signals_per_day": self.limits.max_signals_per_day,
            "open_positions": self.store.count_open_positions(),
            "max_open_positions": self.limits.max_open_positions,
            "equity_usdt": self.equity_usdt,
            "portfolio_hwm_usdt": self.portfolio_hwm(),
            "portfolio_drawdown_pct": self.portfolio_drawdown_pct(),
            "max_portfolio_drawdown_pct": self.limits.max_portfolio_drawdown_pct,
        }
