"""On-disk HTTP cache (JSON + TTL) in `data/cache/`.

Leaf module: it imports only the standard library, so it can be used by both the
CoinGecko client and the data layer without import cycles.
"""

from __future__ import annotations

import hashlib
import json
import time
from pathlib import Path
from typing import Any


class FileCache:
    """Simple on-disk JSON cache with a TTL measured from the write."""

    def __init__(self, directory: Path | str) -> None:
        """Sets the cache directory (created on first write)."""
        self.directory = Path(directory)

    def _path(self, key: str) -> Path:
        digest = hashlib.sha256(key.encode("utf-8")).hexdigest()[:24]
        return self.directory / f"{digest}.json"

    def get(self, key: str, ttl_seconds: int) -> Any | None:
        """Returns the cached value, or None if it is missing or expired."""
        try:
            payload = json.loads(self._path(key).read_text(encoding="utf-8"))
        except (FileNotFoundError, json.JSONDecodeError, OSError):
            return None
        if time.time() - float(payload.get("ts", 0)) > ttl_seconds:
            return None
        return payload.get("value")

    def set(self, key: str, value: Any) -> None:
        """Writes the value atomically (temp file + rename)."""
        self.directory.mkdir(parents=True, exist_ok=True)
        target = self._path(key)
        tmp = target.with_suffix(".tmp")
        tmp.write_text(
            json.dumps({"ts": time.time(), "key": key, "value": value}, ensure_ascii=False),
            encoding="utf-8",
        )
        tmp.replace(target)

    def invalidate(self, key: str) -> None:
        """Removes the cache entry if it exists."""
        self._path(key).unlink(missing_ok=True)

    def clear(self) -> int:
        """Clears the whole cache and returns the number of removed files."""
        removed = 0
        if not self.directory.exists():
            return 0
        for path in self.directory.glob("*.json"):
            path.unlink(missing_ok=True)
            removed += 1
        return removed
