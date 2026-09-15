"""Request context (contextvars) + structured JSON logging with redaction.

Logged context: request_id, user_id, role, actor, action, target, execution_id.
Redaction: sensitive keys are never written to logs.
"""
from __future__ import annotations

import contextvars
import json
import logging
import re
import sys
import uuid
from datetime import datetime, timezone
from typing import Any

request_id_var: contextvars.ContextVar[str | None] = contextvars.ContextVar("request_id", default=None)
user_id_var: contextvars.ContextVar[str | None] = contextvars.ContextVar("user_id", default=None)
role_var: contextvars.ContextVar[str | None] = contextvars.ContextVar("role", default=None)
actor_var: contextvars.ContextVar[str | None] = contextvars.ContextVar("actor", default=None)
action_var: contextvars.ContextVar[str | None] = contextvars.ContextVar("action", default=None)
target_var: contextvars.ContextVar[str | None] = contextvars.ContextVar("target", default=None)
execution_id_var: contextvars.ContextVar[str | None] = contextvars.ContextVar("execution_id", default=None)

SENSITIVE_KEY_RE = re.compile(
    r"(password|passwd|secret|token|authorization|api[_-]?key|credential|private[_-]?key|webhook[_-]?sign|refresh)", re.I
)

_CONTEXT_FIELDS = (
    ("request_id", request_id_var),
    ("user_id", user_id_var),
    ("role", role_var),
    ("actor", actor_var),
    ("action", action_var),
    ("target", target_var),
    ("execution_id", execution_id_var),
)


def new_request_id() -> str:
    return f"req_{uuid.uuid4().hex}"


def now_utc() -> datetime:
    return datetime.now(timezone.utc)


def redact(value: Any) -> Any:
    """Recursively remove values of sensitive keys from a structure before logging."""
    if isinstance(value, dict):
        return {k: ("***REDACTED***" if SENSITIVE_KEY_RE.search(str(k)) else redact(v)) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [redact(v) for v in value]
    return value


class StructuredFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "ts": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
        }
        for name, var in _CONTEXT_FIELDS:
            v = var.get()
            if v is not None:
                payload[name] = v
        data = getattr(record, "data", None)
        if data is not None:
            payload["data"] = redact(data)
        if record.exc_info:
            payload["exc"] = self.formatException(record.exc_info)
        return json.dumps(payload, ensure_ascii=False, default=str)


def setup_logging(level: str = "INFO") -> None:
    root = logging.getLogger()
    root.setLevel(getattr(logging, level.upper(), logging.INFO))
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(StructuredFormatter())
    root.handlers = [handler]
    # Quiet down noisy access logs; we log our own request lines.
    logging.getLogger("uvicorn.access").disabled = True


def log_event(msg: str, data: dict[str, Any] | None = None, level: int = logging.INFO, logger: str | None = None) -> None:
    logging.getLogger(logger or "ghayath").log(level, msg, extra={"data": data} if data is not None else {})
