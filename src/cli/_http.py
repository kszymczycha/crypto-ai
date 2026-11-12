"""HTTP client for the executor with retry on 429/5xx (for the CLI running in the opencode container)."""

from __future__ import annotations

import time
from typing import Any

import httpx

from cli._output import executor_url

RETRYABLE = {429, 500, 502, 503, 504}


class ExecutorUnavailable(RuntimeError):
    """The executor does not respond or returned an error."""


def _request(method: str, path: str, *, json_body: dict[str, Any] | None = None, retries: int = 3) -> dict[str, Any]:
    url = f"{executor_url()}{path}"
    last_error = "no response"
    for attempt in range(retries):
        try:
            response = httpx.request(method, url, json=json_body, timeout=15.0)
            if response.status_code == 200:
                return response.json()
            if response.status_code in RETRYABLE:
                last_error = f"HTTP {response.status_code}"
            else:
                raise ExecutorUnavailable(f"HTTP {response.status_code}: {response.text[:200]}")
        except httpx.HTTPError as exc:
            last_error = str(exc)
        if attempt < retries - 1:
            time.sleep(min(8.0, 0.5 * (2**attempt)))
    raise ExecutorUnavailable(f"Executor {url} did not respond ({last_error})")


def get(path: str) -> dict[str, Any]:
    """GET to the executor."""
    return _request("GET", path)


def post(path: str, payload: dict[str, Any]) -> dict[str, Any]:
    """POST to the executor."""
    return _request("POST", path, json_body=payload)
