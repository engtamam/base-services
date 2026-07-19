"""Capacity to consent — is the person who agreed entitled to agree?

Consent has always assumed the subject speaks for themself. For most people that is true, and for
the ones it is not, the assumption is not a small gap: a nine-year-old could grant consent and the
platform would record it as valid, then the `consent_proof` — the artefact designed to be the legal
defence — would faithfully prove we relied on consent that was never lawful. That is worse than
holding no proof at all, because it is evidence AGAINST the platform, written by the platform.

Two decisions shape this file.

**It is CAPACITY, not "minors".** Naming it after children would have produced an age check and a
second mechanism later for every other case: a carer acting under a court order, an attorney under
a power of attorney, an executor of an estate, a delegate consenting for a company. They are all
the same shape — someone entitled to speak for someone else — so one basis field covers all of
them, and the next jurisdiction does not need a new column.

**Unknown age REFUSES.** This is the whole fix. Today's silent bug is that an absent birth date
reads as "adult" because nothing asks. When a platform has declared an age threshold, an unknown
age is precisely the case it declared the threshold FOR, so it is the last thing that should be
waved through. `CONSENT_UNKNOWN_AGE=allow` exists for platforms that genuinely cannot collect a
birth date, but it is opt-in and it is a decision someone has to write down.
"""
from __future__ import annotations

from dataclasses import dataclass

from app.core.models import CONSENT_BASES, GUARDIAN, SELF

YEAR_SECONDS = 365.2425 * 86400


class CapacityRefused(ValueError):
    """This consent cannot lawfully be recorded as given. Never downgraded to a warning."""


@dataclass(frozen=True)
class CapacityPolicy:
    age_of_majority: int = 0            # 0 = this platform does not serve minors
    unknown_age: str = "refuse"         # refuse | allow

    @property
    def enforced(self) -> bool:
        return self.age_of_majority > 0

    def majority_at(self, born_at: float) -> float:
        """When this subject may speak for themself. 0 when the birth date is unknown."""
        if not born_at or not self.enforced:
            return 0.0
        return born_at + self.age_of_majority * YEAR_SECONDS

    def check(self, *, basis: str, consented_by: str, born_at: float, now: float) -> float:
        """Validate the capacity claim and return when guardian consent must lapse (0 = never).

        Raises CapacityRefused rather than returning a flag, because every caller of this must
        stop. A boolean would let one path forget to check it, and the failure would be silent in
        exactly the way this file exists to prevent.
        """
        if basis not in CONSENT_BASES:
            raise CapacityRefused(
                f"basis {basis!r} is not one of {', '.join(CONSENT_BASES)}")

        if basis != SELF and not consented_by:
            # Someone consenting on another's behalf must be identified, or "a guardian agreed" is
            # an unfalsifiable claim and the proof records nothing anyone can check.
            raise CapacityRefused(
                f"basis {basis!r} requires consented_by — who agreed on the subject's behalf?")

        if not self.enforced:
            # The platform has declared it does not serve minors. Delegated consent is still
            # allowed and recorded (a power of attorney does not depend on an age threshold); we
            # simply have no age to check it against.
            return 0.0

        if not born_at:
            if self.unknown_age == "refuse":
                raise CapacityRefused(
                    "the subject's date of birth is unknown and this platform enforces an age of "
                    f"majority ({self.age_of_majority}). Recording this as self-consent would "
                    "assume an answer to the exact question the threshold was set to ask. Supply "
                    "subject_born_at, or record a guardian as consented_by.")
            return 0.0

        majority = self.majority_at(born_at)
        is_minor = now < majority

        if is_minor and basis == SELF:
            raise CapacityRefused(
                f"the subject is below the age of majority ({self.age_of_majority}) and cannot "
                "consent for themself; record a guardian with basis='guardian'")

        # A lapse date only means something while the subject IS a minor: it is the day they can
        # speak for themself. For an adult under a power of attorney there is no such day, and
        # returning their majority — a date in the PAST — would set expires_at behind now and mark
        # the consent expired the instant it was granted.
        if not is_minor:
            return 0.0

        # Guardian consent expires the day the subject reaches majority. Without this, consent
        # obtained from a parent when the subject was 9 would still be relied on at 30.
        return majority if basis != SELF else 0.0
