"""subject_ref — the pseudonym that lets an immutable record outlive the person in it.

An audit event is hash-chained: redact a field and `verify()` fails from that seq onward, and
tamper-evidence is the product. A ledger row and a tax invoice are immutable for the same reason
in law. So a person's identity must never be WRITTEN into one of those records in the first place.

Instead they carry `subject_ref`: a per-tenant pseudonym.

    subject_ref = HMAC-SHA256(PII_SALT, "<tenant>|<subject>")

Two properties earn it its place:

  * It is a PURE FUNCTION. Computing it needs no lookup, so the audit/analytics WRITE path — the
    hottest path in the platform — gains zero datastore reads. Only the reverse direction
    (pseudonym -> person) needs a stored mapping, and that is read on a DSAR, not per event.

  * It is stable and tenant-scoped. The same person in one tenant always folds to the same ref, so
    the record stays queryable ("what did this subject do?") forever. Two tenants never collide.

ERASURE then means: delete the mutable PII, and destroy the LINKAGE (subject_ref -> person). The
immutable record never changes — the chain still verifies, the invoice is still filed — it simply
can no longer name anybody. That is exactly what privacy law and tax law both ask for.

PII_SALT is a FROZEN identifier (config/product.identifiers.lock.json). Change it and every
pseudonym in every immutable record re-maps: the history orphans, and an erasure can no longer
prove it erased the right person. It is guarded by safety:product for that reason.
"""
from __future__ import annotations

import hashlib
import hmac

_PREFIX = "sub_"
_LEN = 32          # 128 bits of the digest — collision-safe, and short enough to index


def subject_ref(salt: str, tenant: str, subject: str) -> str:
    """The pseudonym for `subject` within `tenant`. Pure; no I/O; stable forever.

    `salt` must be the product's PII_SALT. An empty salt is refused rather than silently
    producing an unsalted (and therefore reversible by brute force) pseudonym.
    """
    if not salt:
        raise ValueError("PII_SALT is empty — an unsalted pseudonym is reversible by guessing "
                         "every email address, which is not a pseudonym at all")
    if not subject:
        raise ValueError("subject must not be empty")
    # Idempotent: an already-wrapped pseudonym passed back in folds to ITSELF, never a second hash.
    # Double-wrapping is how an erasure re-hashes a subject_ref, matches zero rows, reports
    # already_absent, and closes the request COMPLETED over data it never actually touched.
    if is_subject_ref(subject):
        return subject
    mac = hmac.new(salt.encode("utf-8"),
                   f"{tenant}|{subject}".encode("utf-8"),
                   hashlib.sha256)
    return _PREFIX + mac.hexdigest()[:_LEN]


def is_subject_ref(value: str) -> bool:
    """True if `value` already looks like a pseudonym (so we never double-wrap one). The suffix must be
    exactly `_LEN` LOWERCASE HEX characters (audit-016): a pseudonym is _PREFIX + a truncated sha256 hex
    digest, so `sub_` + 32 arbitrary characters is NOT one — accepting any such string let a raw
    identifier shaped like `sub_<anything>` pass as an already-pseudonymised value."""
    if not isinstance(value, str) or not value.startswith(_PREFIX):
        return False
    suffix = value[len(_PREFIX):]
    return len(suffix) == _LEN and all(c in "0123456789abcdef" for c in suffix)
