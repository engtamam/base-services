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
    """The adapter does not support this capability (e.g. presign on a local/memory store).

    A capability the wired driver cannot provide is a hard, honest error — never a silent fall
    back to a different path. The caller negotiates on `supports_presign` first; reaching here
    means it asked anyway, and the answer is a clear refusal, not a surprise.
    """
