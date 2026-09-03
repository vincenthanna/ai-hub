"""Structured JSON-line logging.

One JSON object per line keeps logs greppable without a shipper. The auth token
and request bodies are never logged; only identifiers and timings are.
"""

from __future__ import annotations

import contextvars
import json
import logging
import logging.handlers
import time
from pathlib import Path
from typing import Any, Dict

request_id_var: contextvars.ContextVar[str] = contextvars.ContextVar("request_id", default="")

_RESERVED = {
    "args", "asctime", "created", "exc_info", "exc_text", "filename", "funcName",
    "levelname", "levelno", "lineno", "module", "msecs", "message", "msg", "name",
    "pathname", "process", "processName", "relativeCreated", "stack_info",
    "thread", "threadName", "taskName", "color_message",
}


class JsonLineFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload: Dict[str, Any] = {
            "ts": time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(record.created))
            + ".%03dZ" % (record.msecs,),
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
        }
        rid = request_id_var.get()
        if rid:
            payload["request_id"] = rid
        for key, value in record.__dict__.items():
            if key not in _RESERVED and not key.startswith("_"):
                payload[key] = value
        if record.exc_info:
            payload["exc"] = self.formatException(record.exc_info)
        return json.dumps(payload, ensure_ascii=False, default=str)


def safe_extra(**fields: Any) -> Dict[str, Any]:
    """Rename keys that would collide with built-in LogRecord attributes.

    ``logging.makeRecord`` raises KeyError when ``extra`` carries a reserved
    name such as ``name`` or ``module``, so every structured field goes through
    this helper.
    """
    out: Dict[str, Any] = {}
    for key, value in fields.items():
        out[("x_" + key) if key in _RESERVED else key] = value
    return out


def setup_logging(log_dir: Path, level: str = "INFO", *, to_stdout: bool = True) -> None:
    log_dir.mkdir(parents=True, exist_ok=True)
    root = logging.getLogger()
    root.setLevel(getattr(logging, level.upper(), logging.INFO))
    for handler in list(root.handlers):
        root.removeHandler(handler)

    formatter = JsonLineFormatter()
    file_handler = logging.handlers.RotatingFileHandler(
        str(log_dir / "server.log"), maxBytes=10 * 1024 * 1024, backupCount=5, encoding="utf-8"
    )
    file_handler.setFormatter(formatter)
    root.addHandler(file_handler)

    if to_stdout:
        stream = logging.StreamHandler()
        stream.setFormatter(formatter)
        root.addHandler(stream)

    # uvicorn's own access log is disabled at startup; keep its error log.
    logging.getLogger("uvicorn.access").disabled = True
    for name in ("uvicorn", "uvicorn.error"):
        logger = logging.getLogger(name)
        logger.handlers = []
        logger.propagate = True
