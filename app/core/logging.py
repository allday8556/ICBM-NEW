"""Structured JSON logging. Every record carries the active ``correlation_id``."""

import json
import logging
import sys
from datetime import UTC, datetime
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Any

from app.core.correlation import get_correlation_id

LOG_FILE_NAME = "icbm.jsonl"

_STANDARD_ATTRS = frozenset(vars(logging.LogRecord("", 0, "", 0, "", (), None))) | {
    "message",
    "asctime",
    "correlation_id",
}
_HANDLER_MARK = "_icbm_handler"


class CorrelationFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        if getattr(record, "correlation_id", None) is None:
            record.correlation_id = get_correlation_id()
        return True


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "ts": datetime.fromtimestamp(record.created, UTC).isoformat(timespec="milliseconds"),
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
            "correlation_id": getattr(record, "correlation_id", None),
        }
        for key, value in record.__dict__.items():
            if key not in _STANDARD_ATTRS and not key.startswith("_"):
                payload[key] = value
        if record.exc_info:
            payload["exc"] = self.formatException(record.exc_info)
        return json.dumps(payload, ensure_ascii=False, default=str)


def configure_logging(level: str, log_dir: Path | None) -> Path | None:
    """Install JSON handlers on the root logger (idempotent). Returns the log file path."""
    root = logging.getLogger()
    for handler in list(root.handlers):
        if getattr(handler, _HANDLER_MARK, False):
            root.removeHandler(handler)
            handler.close()

    formatter = JsonFormatter()
    correlation = CorrelationFilter()
    handlers: list[logging.Handler] = [logging.StreamHandler(sys.stdout)]

    log_file: Path | None = None
    if log_dir is not None:
        log_dir.mkdir(parents=True, exist_ok=True)
        log_file = log_dir / LOG_FILE_NAME
        handlers.append(
            RotatingFileHandler(log_file, maxBytes=10_000_000, backupCount=5, encoding="utf-8")
        )

    for handler in handlers:
        handler.setFormatter(formatter)
        handler.addFilter(correlation)
        setattr(handler, _HANDLER_MARK, True)
        root.addHandler(handler)
    root.setLevel(level.upper())

    # Uvicorn runs with log_config=None; route its loggers through the JSON handlers.
    for name in ("uvicorn", "uvicorn.error", "uvicorn.access"):
        uv = logging.getLogger(name)
        uv.handlers.clear()
        uv.propagate = True
    return log_file
