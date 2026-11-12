"""Config tests: YAML, TOML secrets, a representation without leaks."""

from __future__ import annotations

from pathlib import Path

import pytest

from okxbot.config import OkxCredentials, get_risk_limits, get_settings, get_symbols, load_credentials


def test_settings_loaded(config_dir: Path) -> None:
    settings = get_settings()
    assert settings.executor_port == 8080
    assert settings.collector_interval_seconds == 60
    assert "BTC-USDT" in settings.collector_pairs


def test_risk_limits_loaded(config_dir: Path) -> None:
    limits = get_risk_limits()
    assert limits.max_order_usdt == 50.0
    assert limits.min_order_usdt == 5.0
    assert limits.max_open_positions == 3
    assert "buy" in limits.allowed_sides


def test_symbols_loaded(config_dir: Path) -> None:
    symbols = get_symbols()
    assert symbols["BTC-USDT"].coingecko == "bitcoin"
    assert symbols["SOL-USDT"].max_order_usdt == 25.0


def test_load_credentials_from_toml(tmp_path: Path, config_dir: Path) -> None:  # noqa: ARG001
    secret_file = tmp_path / "okx.toml"
    secret_file.write_text(
        'api_key = "abc"\nsecret_key = "def"\npassphrase = "ghi"\nflag = "1"\n', encoding="utf-8"
    )
    creds = load_credentials(secret_file)
    assert creds.flag == "1"
    assert creds.api_key == "abc"


def test_credentials_repr_hides_secrets() -> None:
    creds = OkxCredentials(api_key="SECRET-KEY", secret_key="SECRET-SECRET", passphrase="SECRET-PASS")
    text = repr(creds)
    assert "SECRET" not in text


def test_credentials_invalid_flag(tmp_path: Path, config_dir: Path) -> None:  # noqa: ARG001
    secret_file = tmp_path / "bad.toml"
    secret_file.write_text(
        'api_key = "a"\nsecret_key = "b"\npassphrase = "c"\nflag = "2"\n', encoding="utf-8"
    )
    with pytest.raises(ValueError):
        load_credentials(secret_file)


def test_missing_credentials_file(tmp_path: Path, config_dir: Path) -> None:  # noqa: ARG001
    with pytest.raises(FileNotFoundError):
        load_credentials(tmp_path / "nope.toml")


def test_resolve_prefers_demo_file_for_flag_1(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, config_dir: Path
) -> None:  # noqa: ARG001
    from okxbot.config import resolve_credentials_path

    secrets_dir = tmp_path / "secrets"
    secrets_dir.mkdir()
    (secrets_dir / "okx_demo.toml").write_text('api_key="a"\nsecret_key="b"\npassphrase="c"\nflag="1"\n')
    (secrets_dir / "okx_live.toml").write_text('api_key="x"\nsecret_key="y"\npassphrase="z"\nflag="0"\n')
    monkeypatch.delenv("SECRETS_FILE", raising=False)
    monkeypatch.setenv("SECRETS_DIR", str(secrets_dir))
    monkeypatch.setenv("OKX_FLAG", "1")
    get_settings.cache_clear()
    assert resolve_credentials_path().name == "okx_demo.toml"


def test_resolve_prefers_live_file_for_flag_0(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, config_dir: Path
) -> None:  # noqa: ARG001
    from okxbot.config import resolve_credentials_path

    secrets_dir = tmp_path / "secrets"
    secrets_dir.mkdir()
    (secrets_dir / "okx_demo.toml").write_text('api_key="a"\nsecret_key="b"\npassphrase="c"\nflag="1"\n')
    (secrets_dir / "okx_live.toml").write_text('api_key="x"\nsecret_key="y"\npassphrase="z"\nflag="0"\n')
    monkeypatch.delenv("SECRETS_FILE", raising=False)
    monkeypatch.setenv("SECRETS_DIR", str(secrets_dir))
    monkeypatch.setenv("OKX_FLAG", "0")
    get_settings.cache_clear()
    assert resolve_credentials_path().name == "okx_live.toml"


def test_resolve_honours_explicit_secrets_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, config_dir: Path) -> None:  # noqa: ARG001, E501
    from okxbot.config import resolve_credentials_path

    explicit = tmp_path / "custom.toml"
    explicit.write_text('api_key="a"\nsecret_key="b"\npassphrase="c"\nflag="1"\n')
    monkeypatch.setenv("SECRETS_FILE", str(explicit))
    get_settings.cache_clear()
    assert resolve_credentials_path() == explicit


def test_settings_raises_without_config(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    empty = tmp_path / "empty"
    empty.mkdir()
    monkeypatch.setenv("CONFIG_DIR", str(empty))
    get_settings.cache_clear()
    # A missing file = FileNotFoundError (readable message), empty content = RuntimeError.
    with pytest.raises((FileNotFoundError, RuntimeError)):
        get_settings()
