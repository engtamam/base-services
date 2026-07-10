# =============================================================================
# api-gateway — Structured JSON logging with redaction (stdlib only)
# -----------------------------------------------------------------------------
# One JSON object per line, ISO timestamps, service/env tags, and a request_id
# when present. Sensitive fields are REDACTED (CWE-532: information exposure
# through log files). Uses only the Python standard library — no extra deps,
# minimal attack surface.
# =============================================================================
from __future__ import annotations

import json
import logging
import sys
from datetime import datetime, timezone
from typing import Any

# Substrings (case-insensitive) that mark a field as sensitive. Any log-record
# attribute whose name contains one of these is replaced with the censor value.
_SENSITIVE = (
    "authorization",
    "cookie",
    "password",
    "passwd",
    "secret",
    "token",
    "api_key",
    "apikey",
    "private_key",
)
_CENSOR = "[REDACTED]"

# Standard LogRecord attributes we never emit as "extra" fields.
_STD_ATTRS = set(
    vars(logging.makeLogRecord({})).keys()
) | {"message", "asctime", "taskName"}


def _is_sensitive(key: str) -> bool:
    k = key.lower()
    return any(marker in k for marker in _SENSITIVE)


def _scrub(key: str, value: Any) -> Any:
    if _is_sensitive(key):
        return _CENSOR
    if isinstance(value, dict):
        return {k: _scrub(k, v) for k, v in value.items()}
    return value


class JsonFormatter(logging.Formatter):
    """Render each record as a single-line JSON object, redacting secrets."""

    def __init__(self, service: str, env: str) -> None:
        super().__init__()
        self._service = service
        self._env = env

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "ts": datetime.fromtimestamp(record.created, tz=timezone.utc).isoformat(),
            "level": record.levelname.lower(),
            "logger": record.name,
            "service": self._service,
            "env": self._env,
            "msg": record.getMessage(),
        }
        # Merge structured extras (anything not a standard LogRecord attr),
        # scrubbing sensitive keys/values.
        for key, value in record.__dict__.items():
            if key in _STD_ATTRS or key.startswith("_"):
                continue
            payload[key] = _scrub(key, value)

        if record.exc_info:
            # Log the type only — never the full traceback/message to avoid leaks
            # in aggregated logs; full detail stays in the handler if needed.
            payload["exc_type"] = getattr(record.exc_info[0], "__name__", "Exception")

        return json.dumps(payload, ensure_ascii=False, default=str)


def configure_logging(service: str, env: str, level: str) -> None:
    """Install the JSON formatter on the root logger. Silent in test env."""
    root = logging.getLogger()
    for handler in list(root.handlers):
        root.removeHandler(handler)

    if env == "test":
        root.addHandler(logging.NullHandler())
        root.setLevel(logging.CRITICAL)
        return

    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(JsonFormatter(service=service, env=env))
    root.addHandler(handler)
    root.setLevel(getattr(logging, level.upper(), logging.INFO))


def get_logger(name: str) -> logging.Logger:
    return logging.getLogger(name)
