"""
utils/logger.py — Structured logging

We write two log streams:
  1. logs/pipeline.log  — newline-delimited JSON, machine-parseable
  2. stderr             — human-readable colored output via Rich

Interview talking point:
  "Structured JSON logs mean we can grep, jq, or ship to a log
   aggregator (Datadog, Loki) without writing parsers. The Rich
   handler gives a developer-friendly view in the terminal."
"""

import json
import logging
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from rich.logging import RichHandler


class JSONFormatter(logging.Formatter):
    """
    Emits each log record as a single JSON line.
    Fields: timestamp, level, logger, message, + any 'extra' dict.
    """

    def format(self, record: logging.LogRecord) -> str:  # noqa: A003
        payload: dict[str, Any] = {
            "timestamp": datetime.fromtimestamp(
                record.created, tz=timezone.utc
            ).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }

        # Merge any extra fields the caller passed via logger.info(..., extra={...})
        for key, value in record.__dict__.items():
            if key not in {
                "name", "msg", "args", "levelname", "levelno", "pathname",
                "filename", "module", "exc_info", "exc_text", "stack_info",
                "lineno", "funcName", "created", "msecs", "relativeCreated",
                "thread", "threadName", "processName", "process", "message",
                "taskName",
            }:
                payload[key] = value

        if record.exc_info:
            payload["exception"] = self.formatException(record.exc_info)

        return json.dumps(payload, default=str)


def setup_logging(log_file: str = "logs/pipeline.log", level: str = "INFO") -> None:
    """
    Call once at startup from main.py.
    Creates the log directory if it doesn't exist.
    """
    log_path = Path(log_file)
    log_path.parent.mkdir(parents=True, exist_ok=True)

    root = logging.getLogger()
    root.setLevel(getattr(logging, level.upper(), logging.INFO))

    # File handler — JSON
    fh = logging.FileHandler(log_path, encoding="utf-8")
    fh.setFormatter(JSONFormatter())
    fh.setLevel(logging.DEBUG)
    root.addHandler(fh)

    # Console handler — Rich coloured output
    rh = RichHandler(
        rich_tracebacks=True,
        markup=True,
        show_path=False,
        omit_repeated_times=False,
    )
    rh.setLevel(getattr(logging, level.upper(), logging.INFO))
    root.addHandler(rh)

    # Quiet down noisy third-party loggers
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)
    logging.getLogger("openai").setLevel(logging.WARNING)


def get_logger(name: str) -> logging.Logger:
    """Return a module-level logger. Call at module top-level."""
    return logging.getLogger(name)
