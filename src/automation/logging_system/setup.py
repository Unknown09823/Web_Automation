"""Structured, rotating logging setup.

Three sinks:
  - console: human-readable
  - activity: rotating file (INFO+) JSON-formatted
  - error: rotating file (ERROR+) JSON-formatted
  - debug: rotating file (DEBUG+) when ``debug`` level is set
"""
from __future__ import annotations

import json
import logging
import logging.handlers
import sys
import time
from pathlib import Path
from typing import Any


class JsonFormatter(logging.Formatter):
    """Format records as one JSON object per line."""

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "ts": time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(record.created)),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        if record.exc_info:
            payload["exc"] = self.formatException(record.exc_info)
        for key, val in record.__dict__.items():
            if key in payload or key.startswith("_"):
                continue
            if key in (
                "name", "msg", "args", "levelname", "levelno", "pathname",
                "filename", "module", "exc_info", "exc_text", "stack_info",
                "lineno", "funcName", "created", "msecs", "relativeCreated",
                "thread", "threadName", "processName", "process", "message",
                "taskName",
            ):
                continue
            try:
                json.dumps(val)
                payload[key] = val
            except (TypeError, ValueError):
                payload[key] = repr(val)
        return json.dumps(payload, default=str)


def setup_logging(
    log_dir: str | Path = "data/logs",
    level: str = "INFO",
    debug_logs: bool = False,
    max_bytes: int = 10 * 1024 * 1024,
    backup_count: int = 10,
) -> None:
    """Configure root logger with console + rotating file sinks."""
    log_path = Path(log_dir)
    log_path.mkdir(parents=True, exist_ok=True)

    root = logging.getLogger()
    # idempotent: clear existing handlers we own
    for h in list(root.handlers):
        root.removeHandler(h)
    root.setLevel(getattr(logging, level.upper(), logging.INFO))

    fmt_console = logging.Formatter(
        "%(asctime)s %(levelname)-8s %(name)s :: %(message)s"
    )
    json_fmt = JsonFormatter()

    console = logging.StreamHandler(sys.stdout)
    console.setFormatter(fmt_console)
    console.setLevel(root.level)
    root.addHandler(console)

    activity = logging.handlers.RotatingFileHandler(
        log_path / "activity.log",
        maxBytes=max_bytes,
        backupCount=backup_count,
        encoding="utf-8",
    )
    activity.setFormatter(json_fmt)
    activity.setLevel(logging.INFO)
    root.addHandler(activity)

    error = logging.handlers.RotatingFileHandler(
        log_path / "error.log",
        maxBytes=max_bytes,
        backupCount=backup_count,
        encoding="utf-8",
    )
    error.setFormatter(json_fmt)
    error.setLevel(logging.ERROR)
    root.addHandler(error)

    if debug_logs:
        debug = logging.handlers.RotatingFileHandler(
            log_path / "debug.log",
            maxBytes=max_bytes,
            backupCount=backup_count,
            encoding="utf-8",
        )
        debug.setFormatter(json_fmt)
        debug.setLevel(logging.DEBUG)
        root.addHandler(debug)
        root.setLevel(logging.DEBUG)

    # silence noisy third-party loggers at INFO
    for noisy in ("urllib3", "asyncio", "httpx", "httpcore"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
