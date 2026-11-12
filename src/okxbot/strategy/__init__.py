"""Strategy rules: hints and a deterministic entry gate.

The executor enforces `evaluate()` for buys, but it does not generate signals —
the OpenCode agent makes the decision. See `docs/strategy.md` for the rules and
the rationale behind them.
"""

from okxbot.strategy.rules import StrategyConfig, evaluate
from okxbot.strategy.signals import build_signal, default_amount_usdt

__all__ = ["StrategyConfig", "build_signal", "default_amount_usdt", "evaluate"]
