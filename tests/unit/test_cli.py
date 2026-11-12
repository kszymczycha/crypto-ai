"""CLI tests: the single-JSON-on-stdout contract and exit codes."""

from __future__ import annotations

import json

import pytest

import cli._http as http
import cli.signal as signal_cli
import cli.status as status_cli


def _parse(capsys: pytest.CaptureFixture[str]) -> dict:
    out = capsys.readouterr().out.strip()
    assert out, "CLI printed no JSON"
    assert "\n" not in out, "CLI printed more than one line"
    return json.loads(out)


def test_signal_success(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    monkeypatch.setattr(
        http, "post", lambda path, payload: {"ok": True, "status": "placed", "order_id": "o1"}  # noqa: ARG005
    )
    code = signal_cli.main(["--pair", "btc-usdt", "--side", "buy", "--amount-usdt", "20",
                            "--reason", "RSI 28 and price above MA20"])
    document = _parse(capsys)
    assert code == 0
    assert document["ok"] is True
    assert document["status"] == "placed"


def test_signal_executor_unavailable(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    def boom(path, payload):  # noqa: ARG001
        raise http.ExecutorUnavailable("executor unavailable")

    monkeypatch.setattr(http, "post", boom)
    code = signal_cli.main(["--pair", "BTC-USDT", "--side", "buy", "--amount-usdt", "20",
                            "--reason", "test reason"])
    document = _parse(capsys)
    assert code == 1
    assert document["ok"] is False
    assert document["error"] == "executor_unavailable"


def test_signal_payload_includes_tp_sl(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    captured: dict = {}

    def fake_post(path, payload):
        captured.update(payload)
        return {"ok": True, "status": "placed"}

    monkeypatch.setattr(http, "post", fake_post)
    signal_cli.main(["--pair", "BTC-USDT", "--side", "buy", "--amount-usdt", "20",
                     "--reason", "test reason", "--tp-pct", "2.5", "--sl-pct", "1.2"])
    capsys.readouterr()
    assert captured["tp_pct"] == 2.5
    assert captured["sl_pct"] == 1.2
    assert len(captured["idempotency_key"]) >= 8


def test_status_success(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    monkeypatch.setattr(http, "get", lambda path: {"ok": True, "balances": {"USDT": 100.0}})  # noqa: ARG005
    code = status_cli.main([])
    document = _parse(capsys)
    assert code == 0
    assert document["balances"]["USDT"] == 100.0


def test_status_failure_exit_code(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    def boom(path):  # noqa: ARG001
        raise http.ExecutorUnavailable("timeout")

    monkeypatch.setattr(http, "get", boom)
    code = status_cli.main([])
    document = _parse(capsys)
    assert code == 1
    assert document["ok"] is False


def test_rejected_signal_returns_exit_1(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    monkeypatch.setattr(
        http, "post", lambda path, payload: {"ok": False, "status": "rejected", "reason": "limit"}  # noqa: ARG005
    )
    code = signal_cli.main(["--pair", "BTC-USDT", "--side", "buy", "--amount-usdt", "500",
                            "--reason", "too much"])
    document = _parse(capsys)
    assert code == 1
    assert document["status"] == "rejected"
