"""Shared fixtures: working directory with config, candles, temporary store."""

from __future__ import annotations

import os
import shutil
from pathlib import Path

import pytest

# In the test container the code lives in /app, but the repo files (.gitignore, README, secrets/)
# are excluded from the image by .dockerignore and mounted separately as /repo.
# This way the security tests read the REAL files, not their copies inside the image.
REPO_ROOT = Path(os.environ.get("OKXBOT_REPO_ROOT") or Path(__file__).resolve().parents[1])


@pytest.fixture()
def config_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Copies config/ into tmp_path and sets CONFIG_DIR/DATA_DIR/LOGS_DIR."""
    target = tmp_path / "config"
    shutil.copytree(REPO_ROOT / "config", target)
    data = tmp_path / "data"
    logs = tmp_path / "logs"
    data.mkdir()
    logs.mkdir()
    monkeypatch.setenv("CONFIG_DIR", str(target))
    monkeypatch.setenv("DATA_DIR", str(data))
    monkeypatch.setenv("LOGS_DIR", str(logs))
    _clear_settings_cache()
    yield target
    _clear_settings_cache()


def _clear_settings_cache() -> None:
    from okxbot.config import get_risk_limits, get_settings, get_symbols

    get_settings.cache_clear()
    get_risk_limits.cache_clear()
    get_symbols.cache_clear()


@pytest.fixture()
def store(config_dir: Path):
    """Temporary StateStore with an initialized schema."""
    from okxbot.storage.db import StateStore

    db_path = config_dir.parent / "data" / "state.db"
    state = StateStore(db_path)
    state.init_schema()
    yield state
    state.close()


@pytest.fixture()
def cache_dir(config_dir: Path) -> Path:
    """Cache directory in tmp_path (isolation: no entries from previous tests)."""
    target = config_dir.parent / "data" / "cache"
    target.mkdir(parents=True, exist_ok=True)
    return target


def make_candles(closes: list[float]) -> list[dict[str, object]]:
    """Builds candles from a list of closing prices."""
    return [
        {
            "ts": str(index),
            "open": price,
            "high": price * 1.01,
            "low": price * 0.99,
            "close": price,
            "vol": 1.0,
        }
        for index, price in enumerate(closes)
    ]


@pytest.fixture()
def rising_candles() -> list[dict[str, object]]:
    """30 candles in an uptrend (high RSI)."""
    return make_candles([100.0 + index * 0.5 for index in range(30)])


@pytest.fixture()
def falling_candles() -> list[dict[str, object]]:
    """30 candles in a downtrend (low RSI)."""
    return make_candles([100.0 - index * 0.5 for index in range(30)])
