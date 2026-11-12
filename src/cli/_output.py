"""Shared CLI output: exactly one JSON on stdout and exit code 0/1."""

from __future__ import annotations

import json
import sys
from typing import Any


def emit(payload: dict[str, Any], *, ok: bool | None = None) -> int:
    """Prints JSON and returns the exit code (0 on ok, 1 on error).

    Contract for OpenCode: always one JSON object, no logs on stdout.
    """
    success = payload.get("ok", True) if ok is None else ok
    document = {"ok": bool(success), **payload}
    json.dump(document, sys.stdout, ensure_ascii=False)
    sys.stdout.write("\n")
    sys.stdout.flush()
    return 0 if success else 1


def fail(message: str, *, error_type: str = "error", **extra: Any) -> int:
    """Error output in JSON format."""
    return emit({"ok": False, "error": error_type, "message": message, **extra}, ok=False)


def executor_url() -> str:
    """Executor base URL (env EXECUTOR_URL, default http://executor:8080)."""
    import os

    return os.environ.get("EXECUTOR_URL", "http://executor:8080").rstrip("/")
