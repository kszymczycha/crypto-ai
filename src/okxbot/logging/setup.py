"""JSON logging configuration to files in logs/ (never to stdout in src/okxbot/).

The module is deliberately named `logging` but does not shadow the standard
library, because it is a package inside `okxbot` (the absolute import
`import logging` works).

API for the rest of the code:
    log = setup_logging("executor")
    log.info("order_placed", pair="BTC-USDT", order_id="1")

`setup_logging` returns a `StructuredLogger` that accepts arbitrary fields as
**kwargs and forwards them to stdlib via `extra=`. This is not cosmetic: the
native `logging.Logger.info("msg", pair=...)` treats `pair=` as formatting
arguments and raises `TypeError` — meaning every log with a keyword field
crashed the process.
"""

from __future__ import annotations

import json
import logging
import logging.handlers
import sys
from pathlib import Path
from typing import Any

from okxbot.config import get_settings

# LogRecord attributes reserved by stdlib — they must not be overridden in `extra=`.
_RESERVED = {
    "args", "asctime", "created", "exc_info", "exc_text", "filename", "funcName",
    "levelname", "levelno", "lineno", "message", "module", "msecs", "msg", "name",
    "pathname", "process", "processName", "relativeCreated", "stack_info",
    "thread", "threadName", "taskName",
}


class JsonFormatter(logging.Formatter):
    """Formats log records as single-line JSON."""

    def format(self, record: logging.LogRecord) -> str:
        """Returns the record as JSON: base fields + everything from `extra=`."""
        payload: dict[str, Any] = {
            "ts": self.formatTime(record, "%Y-%m-%dT%H:%M:%S%z"),
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
        }
        for key, value in record.__dict__.items():
            if key not in _RESERVED and not key.startswith("_"):
                payload[key] = value
        if record.exc_info:
            payload["exc"] = self.formatException(record.exc_info)
        return json.dumps(payload, ensure_ascii=False, default=str)


def _plain_formatter() -> logging.Formatter:
    return logging.Formatter("%(asctime)s %(levelname)s %(name)s %(message)s")


def _safe_extra(fields: dict[str, Any]) -> dict[str, Any]:
    """Protects against collision with LogRecord attributes (stdlib raises KeyError)."""
    return {
        (f"field_{key}" if key in _RESERVED else key): value
        for key, value in fields.items()
    }


class StructuredLogger:
    """Wrapper over `logging.Logger` that accepts structural fields as **kwargs."""

    def __init__(self, logger: logging.Logger) -> None:
        """Wraps an existing stdlib logger."""
        self._logger = logger

    def _log(self, level: int, msg: str, **fields: Any) -> None:
        if not self._logger.isEnabledFor(level):
            return
        # pass exc_info natively so the exception traceback ends up in the JSON.
        exc_info = bool(fields.pop("exc_info", False))
        self._logger.log(level, msg, extra=_safe_extra(fields), exc_info=exc_info or None)

    def debug(self, msg: str, **fields: Any) -> None:
        """DEBUG-level log with structural fields."""
        self._log(logging.DEBUG, msg, **fields)

    def info(self, msg: str, **fields: Any) -> None:
        """INFO-level log with structural fields."""
        self._log(logging.INFO, msg, **fields)

    def warning(self, msg: str, **fields: Any) -> None:
        """WARNING-level log with structural fields."""
        self._log(logging.WARNING, msg, **fields)

    def error(self, msg: str, **fields: Any) -> None:
        """ERROR-level log with structural fields."""
        self._log(logging.ERROR, msg, **fields)

    def exception(self, msg: str, **fields: Any) -> None:
        """ERROR-level log together with the traceback of the current exception."""
        self._log(logging.ERROR, msg, exc_info=True, **fields)

    @property
    def handlers(self) -> list[logging.Handler]:
        """View of the handlers (diagnostics and tests)."""
        return self._logger.handlers

    @property
    def level(self) -> int:
        """Current log level."""
        return self._logger.level

    def __getattr__(self, name: str) -> Any:
        """Delegates the remaining attributes to the stdlib logger (e.g. `setLevel`)."""
        return getattr(self._logger, name)


def setup_logging(name: str, *, stdout: bool = False) -> StructuredLogger:
    """Creates a logger with rotation to logs/<name>.log and optionally to stdout.

    stdout should be used only by server processes (executor, collector) running
    in Docker. The CLI does not log to stdout — JSON is the only output there.
    """
    settings = get_settings()
    logger = logging.getLogger(name)
    if not logger.handlers:
        logger.setLevel(getattr(logging, settings.log_level.upper(), logging.INFO))
        logger.propagate = False
        formatter: logging.Formatter = JsonFormatter() if settings.log_json else _plain_formatter()
        settings.logs_dir.mkdir(parents=True, exist_ok=True)
        file_handler = logging.handlers.RotatingFileHandler(
            Path(settings.logs_dir) / f"{name}.log",
            maxBytes=settings.log_max_bytes,
            backupCount=settings.log_backup_count,
            encoding="utf-8",
        )
        file_handler.setFormatter(formatter)
        logger.addHandler(file_handler)
        if stdout:
            stream_handler = logging.StreamHandler(sys.stdout)
            stream_handler.setFormatter(formatter)
            logger.addHandler(stream_handler)
    return StructuredLogger(logger)
