"""Lazy optional-dependency import — turns a missing extra into a clear error.

Infra adapters call this inside their constructor so that importing the adapter
module never fails; only *selecting* a driver without its extra installed raises
a friendly ConfigError (never a raw ImportError crash-on-import).
"""
from __future__ import annotations

import importlib
from typing import Any

from app.ports.errors import ConfigError


def load(module: str, extra: str) -> Any:
    try:
        return importlib.import_module(module)
    except ImportError as exc:
        raise ConfigError(
            f"driver needs the '{extra}' extra — install it: "
            f'pip install "zatca-service[{extra}]"'
        ) from exc
