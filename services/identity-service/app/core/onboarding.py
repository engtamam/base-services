"""Onboarding methods — the ONE gate that decides HOW a new person may come to exist in this
deployment, so every method that follows (invite token, join request, invite link, temporary
password) asks the same authority instead of each inventing its own on/off knob.

Plug-and-play and FAIL-CLOSED: a deployment lists the methods it wants in `ONBOARDING_METHODS`; a
method NOT in that list is refused, and a method NOBODY has heard of (a typo) is refused at BOOT
rather than silently doing nothing. The vocabulary is CLOSED — a new method is a new entry here and
in the gate, never free text — which is what lets a panel enumerate exactly the options that work.

BACKWARD COMPATIBLE. An existing deployment sets nothing, and the empty list means "derive from the
legacy knobs": open self-service registration follows `REGISTRATION_ENABLED` exactly as before, and
admin-invite stays available. So nothing changes for anyone until they opt into the explicit list.
"""
from __future__ import annotations

from app.ports.errors import ConfigError

# The closed vocabulary. Each is claimed by a later method in this release; listed here so the gate,
# the boot guard, and the panel all read ONE source and cannot drift.
OPEN = "open"                       # self-service registration (REGISTRATION_ENABLED)
INVITE_TOKEN = "invite_token"       # an admin creates the person + a single-use token (#624)
JOIN_REQUEST = "join_request"       # a stranger requests to join; an admin approves (#625)
INVITE_LINK = "invite_link"         # a reusable/open invite link with a preset role (#626)
TEMP_PASSWORD = "temp_password"     # an admin sets a temporary password, forced-changed on first login (#627)
KNOWN_METHODS = frozenset({OPEN, INVITE_TOKEN, JOIN_REQUEST, INVITE_LINK, TEMP_PASSWORD})


class OnboardingMethodDisabled(Exception):
    """A person tried to onboard by a method this deployment has not enabled — refused, fail-closed."""

    def __init__(self, method: str) -> None:
        super().__init__(method)
        self.method = method


class OnboardingPolicy:
    def __init__(self, methods_csv: str, *, registration_enabled: bool) -> None:
        self._explicit = [m.strip() for m in (methods_csv or "").split(",") if m.strip()]
        unknown = [m for m in self._explicit if m not in KNOWN_METHODS]
        if unknown:
            # Loud at boot: an unrecognised method would otherwise be a silently-dead setting, the
            # exact advertised-but-unread hole this vocabulary exists to close.
            raise ConfigError(
                f"ONBOARDING_METHODS names unknown method(s) {unknown}; "
                f"known methods are {sorted(KNOWN_METHODS)}")
        self._registration_enabled = registration_enabled

    @property
    def is_explicit(self) -> bool:
        """True when a deployment gave an explicit ONBOARDING_METHODS list — then that list is the
        whole authority. False = legacy mode, where the older per-path knobs (REGISTRATION_ENABLED,
        IDENTITY_MEMBER_ADD_MODE) still decide and this gate must NOT add a new restriction."""
        return bool(self._explicit)

    def enabled(self, method: str) -> bool:
        if method not in KNOWN_METHODS:
            raise ValueError(f"unknown onboarding method {method!r}")
        if self._explicit:
            return method in self._explicit
        # Legacy derivation (no explicit list): behave exactly as the platform did before this gate.
        if method == OPEN:
            return self._registration_enabled
        if method == INVITE_TOKEN:
            return True                 # admin invite was always available
        return False                    # the new methods are off until explicitly enabled

    def require(self, method: str) -> None:
        """Raise OnboardingMethodDisabled unless `method` is enabled — the guard every onboarding
        route calls before doing anything."""
        if not self.enabled(method):
            raise OnboardingMethodDisabled(method)

    def list_enabled(self) -> list[str]:
        """The enabled methods, for a signup UI to render exactly the options that will work."""
        return [m for m in sorted(KNOWN_METHODS) if self.enabled(m)]


def build_onboarding_policy(settings) -> OnboardingPolicy:
    return OnboardingPolicy(getattr(settings, "ONBOARDING_METHODS", ""),
                            registration_enabled=settings.REGISTRATION_ENABLED)
