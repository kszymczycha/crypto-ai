"""Loading YAML configuration, TOML secrets and project paths.

Override order: defaults -> YAML files -> environment variables.
OKX secrets are read only here and only by the executor process.
"""

from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path
from typing import Any

import yaml

PROJECT_ROOT = Path(__file__).resolve().parents[2]


def _config_dir() -> Path:
    return Path(os.environ.get("CONFIG_DIR", PROJECT_ROOT / "config"))


def _secrets_dir() -> Path:
    return Path(os.environ.get("SECRETS_DIR", PROJECT_ROOT / "secrets"))


def _data_dir() -> Path:
    return Path(os.environ.get("DATA_DIR", PROJECT_ROOT / "data"))


def _logs_dir() -> Path:
    return Path(os.environ.get("LOGS_DIR", PROJECT_ROOT / "logs"))


def _load_yaml(path: Path) -> dict[str, Any]:
    with path.open("r", encoding="utf-8") as handle:
        return yaml.safe_load(handle) or {}


@dataclass(frozen=True)
class RiskLimits:
    """Hard risk limits enforced by the executor."""

    max_position_pct: float
    max_daily_loss_pct: float
    max_portfolio_drawdown_pct: float
    max_open_positions: int
    max_order_usdt: float
    min_order_usdt: float
    require_confirmation_above_usdt: float
    allowed_sides: tuple[str, ...]
    allowed_order_types: tuple[str, ...]
    cooldown_seconds_between_signals: int
    max_signals_per_day: int


@dataclass(frozen=True)
class Settings:
    """Aggregate application configuration (YAML + environment)."""

    app_name: str
    timezone: str
    data_dir: Path
    logs_dir: Path
    db_path: Path
    cache_dir: Path
    executor_host: str
    executor_port: int
    collector_interval_seconds: int
    collector_pairs: tuple[str, ...]
    coingecko_base_url: str
    cache_ttl_prices_seconds: int
    cache_ttl_metadata_seconds: int
    http_timeout_seconds: float
    http_max_retries: int
    backoff_base_seconds: float
    backoff_max_seconds: float
    log_level: str
    log_json: bool
    log_max_bytes: int
    log_backup_count: int
    rsi_period: int
    ma_period: int
    trend_ma_period: int
    atr_period: int
    vol_period: int
    candle_bar: str
    candle_limit: int
    secrets_file: Path = field(default_factory=lambda: Path("/run/secrets/okx.toml"))
    okx_flag: str = "1"
    idempotency_window_seconds: int = 300


@dataclass(frozen=True)
class Symbol:
    """Trading pair with a CoinGecko mapping."""

    symbol: str
    base: str
    quote: str
    coingecko: str
    max_order_usdt: float


def _default_settings() -> Settings:
    raw = _load_yaml(_config_dir() / "settings.yaml")
    if not raw:
        raise RuntimeError("config/settings.yaml not found — check CONFIG_DIR")
    app = raw.get("app", {})
    paths = raw.get("paths", {})
    executor = raw.get("executor", {})
    collector = raw.get("collector", {})
    http = raw.get("http", {})
    logging_cfg = raw.get("logging", {})
    indicators = raw.get("indicators", {})
    data_dir = _data_dir()
    return Settings(
        app_name=str(app.get("name", "okx-bot")),
        timezone=str(app.get("timezone", "UTC")),
        data_dir=data_dir,
        logs_dir=_logs_dir(),
        db_path=data_dir / str(paths.get("db_file", "state.db")),
        cache_dir=data_dir / "cache",
        executor_host=str(os.environ.get("EXECUTOR_HOST", executor.get("host", "0.0.0.0"))),
        executor_port=int(os.environ.get("EXECUTOR_PORT", executor.get("port", 8080))),
        collector_interval_seconds=int(
            os.environ.get("COLLECTOR_INTERVAL_SECONDS", collector.get("interval_seconds", 60))
        ),
        collector_pairs=tuple(collector.get("pairs", ["BTC-USDT", "ETH-USDT"])),
        coingecko_base_url=str(collector.get("coingecko_base_url", "https://api.coingecko.com/api/v3")),
        cache_ttl_prices_seconds=int(collector.get("cache_ttl_prices_seconds", 60)),
        cache_ttl_metadata_seconds=int(collector.get("cache_ttl_metadata_seconds", 300)),
        http_timeout_seconds=float(http.get("timeout_seconds", 10)),
        http_max_retries=int(http.get("max_retries", 3)),
        backoff_base_seconds=float(http.get("backoff_base_seconds", 0.5)),
        backoff_max_seconds=float(http.get("backoff_max_seconds", 8)),
        log_level=os.environ.get("LOG_LEVEL", str(logging_cfg.get("level", "INFO"))),
        log_json=bool(logging_cfg.get("json", True)),
        log_max_bytes=int(logging_cfg.get("max_bytes", 5_242_880)),
        log_backup_count=int(logging_cfg.get("backup_count", 5)),
        rsi_period=int(indicators.get("rsi_period", 14)),
        ma_period=int(indicators.get("ma_period", 20)),
        trend_ma_period=int(indicators.get("trend_ma_period", 200)),
        atr_period=int(indicators.get("atr_period", 14)),
        vol_period=int(indicators.get("vol_period", 20)),
        candle_bar=str(os.environ.get("CANDLE_BAR", indicators.get("candle_bar", "4H"))),
        candle_limit=int(
            os.environ.get("CANDLE_LIMIT", indicators.get("candle_limit", 300))
        ),
        secrets_file=Path(os.environ.get("SECRETS_FILE", paths.get("secrets_file", "/run/secrets/okx.toml"))),
        okx_flag=os.environ.get("OKX_FLAG", "1"),
        idempotency_window_seconds=int(executor.get("idempotency_window_seconds", 300)),
    )


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Returns the (cached) application settings."""
    return _default_settings()


@lru_cache(maxsize=1)
def get_risk_limits() -> RiskLimits:
    """Returns the risk limits from config/risk_limits.yaml."""
    raw = _load_yaml(_config_dir() / "risk_limits.yaml")
    if not raw:
        raise RuntimeError("config/risk_limits.yaml not found — check CONFIG_DIR")
    return RiskLimits(
        max_position_pct=float(raw["max_position_pct"]),
        max_daily_loss_pct=float(raw["max_daily_loss_pct"]),
        max_portfolio_drawdown_pct=float(raw["max_portfolio_drawdown_pct"]),
        max_open_positions=int(raw["max_open_positions"]),
        max_order_usdt=float(raw["max_order_usdt"]),
        min_order_usdt=float(raw["min_order_usdt"]),
        require_confirmation_above_usdt=float(raw["require_confirmation_above_usdt"]),
        allowed_sides=tuple(raw.get("allowed_sides", ["buy", "sell"])),
        allowed_order_types=tuple(raw.get("allowed_order_types", ["market", "limit"])),
        cooldown_seconds_between_signals=int(raw.get("cooldown_seconds_between_signals", 10)),
        max_signals_per_day=int(raw.get("max_signals_per_day", 40)),
    )


@lru_cache(maxsize=1)
def get_symbols() -> dict[str, Symbol]:
    """Returns the pairs from config/symbols.yaml, keyed by the OKX symbol."""
    raw = _load_yaml(_config_dir() / "symbols.yaml")
    if not raw:
        raise RuntimeError("config/symbols.yaml not found — check CONFIG_DIR")
    result: dict[str, Symbol] = {}
    for item in raw.get("pairs", []):
        symbol = str(item["symbol"])
        result[symbol] = Symbol(
            symbol=symbol,
            base=str(item.get("base", symbol.split("-")[0])),
            quote=str(item.get("quote", "USDT")),
            coingecko=str(item.get("coingecko", "")),
            max_order_usdt=float(item.get("max_order_usdt", get_risk_limits().max_order_usdt)),
        )
    return result


@dataclass(frozen=True)
class OkxCredentials:
    """OKX secrets. Repr hides the values — they do not leak into logs."""

    api_key: str = field(repr=False)
    secret_key: str = field(repr=False)
    passphrase: str = field(repr=False)
    flag: str = "1"


def resolve_credentials_path() -> Path:
    """Resolves the secrets file: SECRETS_FILE, then secrets/okx_{demo,live}.toml.

    Mode-specific files take precedence (depending on OKX_FLAG), so that demo
    cannot accidentally use live keys.
    """
    explicit = os.environ.get("SECRETS_FILE")
    if explicit:
        return Path(explicit)
    flag = get_settings().okx_flag
    order = ["okx_demo.toml", "okx.toml"] if flag == "1" else ["okx_live.toml", "okx.toml"]
    for name in order:
        candidate = _secrets_dir() / name
        if candidate.exists():
            return candidate
    return Path(get_settings().secrets_file)


def load_credentials(path: Path | None = None) -> OkxCredentials:
    """Loads OKX secrets from a TOML file (executor process only)."""
    target = path or resolve_credentials_path()
    with target.open("rb") as handle:
        raw = tomllib.load(handle)
    flag = str(raw.get("flag", get_settings().okx_flag))
    if flag not in {"0", "1"}:
        raise ValueError("secrets: 'flag' must be '0' (live) or '1' (demo)")
    return OkxCredentials(
        api_key=str(raw["api_key"]),
        secret_key=str(raw["secret_key"]),
        passphrase=str(raw["passphrase"]),
        flag=flag,
    )


def ensure_dirs(settings: Settings | None = None) -> None:
    """Creates the data, cache and log directories."""
    cfg = settings or get_settings()
    for directory in (cfg.data_dir, cfg.cache_dir, cfg.logs_dir):
        directory.mkdir(parents=True, exist_ok=True)
