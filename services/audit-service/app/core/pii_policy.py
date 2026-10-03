"""The policy that makes the erasure design TRUE rather than merely intended.

Phase 30 says a raw identity must never enter the hash chain, because once it is hashed it is
permanent: you cannot redact it without breaking `verify()` from that seq onward. But a rule that
depends on every caller in every service remembering to pseudonymise is not a rule — it is a hope.
One forgotten call site and the person is in the chain forever.

So audit enforces it at its OWN boundary, where it cannot be bypassed.

    AUDIT_PII_MODE = fold | enforce | off

  * **fold** (default, and the reason this is safe by default): audit computes the pseudonym
    ITSELF for any actor that is not already one, and redacts the configured personal fields out
    of `metadata`. A caller that sends an email gets a `sub_…` written to the chain. Raw PII
    cannot land, no matter who calls or what they forget. Nothing breaks.

  * **enforce**: refuse the write. For a platform that wants its callers to be explicit and would
    rather have a loud 400 than a silent transformation.

  * **off**: legacy passthrough. Documented, never the default, and the only mode in which this
    service can be made to violate its own guarantee.

WHICH FIELDS ARE PERSONAL IS CONFIG, NOT CODE. `AUDIT_PII_FIELDS` is a comma list. A Saudi
platform needs `national_id` and `iqama`; a health platform needs `mrn`. Hardcoding an English
office-worker's idea of PII into a platform meant to be plugged into anything is exactly the kind
of assumption this codebase refuses to make.
"""
from __future__ import annotations

from typing import Any

from app.core.pii import is_subject_ref, subject_ref

FOLD, ENFORCE, OFF = "fold", "enforce", "off"
MODES = (FOLD, ENFORCE, OFF)

_REDACTED = "[redacted]"


class PiiPolicyError(ValueError):
    """A raw identity was offered to an append-only record, in `enforce` mode."""


def parse_fields(raw: str) -> tuple[str, ...]:
    """`"email, phone ,national_id"` -> ("email", "phone", "national_id"). Case-folded."""
    return tuple(f.strip().lower() for f in (raw or "").split(",") if f.strip())


class PiiPolicy:
    """Applied to every event BEFORE it is hashed. There is no second chance."""

    def __init__(self, *, mode: str, fields: tuple[str, ...], salt: str) -> None:
        if mode not in MODES:
            # Fail fast at boot, like every other driver choice in this codebase.
            from app.ports.errors import ConfigError
            raise ConfigError(f"unknown AUDIT_PII_MODE {mode!r} — allowed: {', '.join(MODES)}")
        # A mode that pseudonymises but has no salt is a service that CLAIMS erasure and cannot
        # deliver it. Refuse at BOOT, like every other misconfiguration here — never per-request,
        # where it would surface as a random 500 long after deploy.
        if mode in (FOLD, ENFORCE) and not salt:
            from app.ports.errors import ConfigError
            raise ConfigError(
                f"AUDIT_PII_MODE={mode!r} requires PII_SALT. An unsalted pseudonym is reversible "
                f"by guessing every email address, so it is not a pseudonym — and a service that "
                f"cannot pseudonymise cannot honestly claim it can erase anyone. Set PII_SALT "
                f"(it is a frozen product identifier), or run AUDIT_PII_MODE=off and accept that "
                f"raw identities become permanent in the chain."
            )
        self.mode = mode
        self.fields = fields
        self._salt = salt

    def apply(self, *, tenant: str, actor: str, ip: str,
              metadata: dict[str, Any] | None) -> tuple[str, str, dict[str, Any]]:
        """Return (actor, ip, metadata) as they are SAFE to hash into the chain."""
        meta = dict(metadata or {})

        if self.mode == OFF:
            return actor, ip, meta

        # ── the actor ───────────────────────────────────────────────────────
        if actor and not is_subject_ref(actor):
            if self.mode == ENFORCE:
                raise PiiPolicyError(
                    f"audit actor {actor!r} is a raw identity, not a subject_ref. Hashing it into "
                    f"the chain would make it permanent and unerasable. Compute "
                    f"subject_ref(PII_SALT, tenant, subject) at the call site, or run in "
                    f"AUDIT_PII_MODE=fold and audit will do it for you."
                )
            if not self._salt:
                # A pseudonym with no salt is reversible by guessing every email on earth.
                # Refusing is the only safe answer; folding to something weak would be worse
                # than not folding at all, because it would LOOK pseudonymous.
                raise PiiPolicyError(
                    "AUDIT_PII_MODE=fold needs PII_SALT — an unsalted pseudonym is not a pseudonym"
                )
            actor = subject_ref(self._salt, tenant, actor)

        # ── the IP ──────────────────────────────────────────────────────────
        # An IP address is personal data in its own right (GDPR Rec. 30), and it is the field
        # people forget, because it does not look like a name.
        if ip and "ip" in self.fields:
            if self.mode == ENFORCE:
                raise PiiPolicyError("audit ip is personal data and would be permanent in the chain")
            ip = ""

        # ── free-form metadata: where PII actually sneaks in ────────────────
        # Walk RECURSIVELY (dicts and lists at every depth): a personal field nested one level down —
        # metadata={"owner": {"email": ...}} — used to sail straight into the permanent chain because
        # only the top-level keys were checked (audit-010).
        leaked, redacted = self._scan(meta)
        if leaked:
            if self.mode == ENFORCE:
                raise PiiPolicyError(
                    f"audit metadata carries personal fields {sorted(leaked)} (at any depth) — "
                    f"permanent and unerasable once hashed. Store a subject_ref instead."
                )
            meta = redacted

        return actor, ip, meta

    def _scan(self, obj: Any) -> tuple[set[str], Any]:
        """Return (set of personal field-keys found at any depth, a redacted deep copy)."""
        leaked: set[str] = set()

        def walk(o: Any) -> Any:
            if isinstance(o, dict):
                out = {}
                for k, v in o.items():
                    if str(k).lower() in self.fields:
                        leaked.add(k)
                        out[k] = _REDACTED
                    else:
                        out[k] = walk(v)
                return out
            if isinstance(o, list):
                return [walk(v) for v in o]
            return o

        return leaked, walk(obj)
