"""Shared port error vocabulary. Adapters raise these; the core catches these."""
from __future__ import annotations


class PortError(Exception):
    """Base class for all port/adapter errors."""


class ConfigError(PortError):
    """A driver/config value is invalid or an adapter is misconfigured.

    Raised at boot for an unknown driver, or when a selected driver is missing
    its optional dependency. Surfaces as a hard, fail-fast startup error.
    """


class NotFound(PortError):
    """The requested key/record does not exist."""


class Unavailable(PortError):
    """The backing system is unreachable/unhealthy."""


class InvalidToken(PortError):
    """A token failed verification (bad signature, expired, wrong iss/aud, ...)."""


class Unsupported(PortError):
    """The operation is not available on this driver (e.g. presigning a local/memory store)."""
