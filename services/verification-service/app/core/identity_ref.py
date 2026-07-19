"""claim_ref — recognising an identity again without ever storing it.

A national id or a commercial registration number is the single most valuable thing in this
service's store, and a verification database is a concentrated pile of them. So none are kept.
What is kept is:

    claim_ref = HMAC-SHA256(PII_SALT, "<claim_type>|<normalised value>")

Three properties, and each earns its place:

**Stable.** The same registration always folds to the same ref, so we can recognise it again —
which is what makes "one welcome gift per real business" possible without a list of businesses.

**One-way.** The ref names nobody. A full dump of this store reveals which identities are distinct,
not who they are. That is a guarantee no access control gives you, because it survives the access
control being wrong.

**Scoped by claim type, NOT by tenant.** This is the one deliberate difference from `subject_ref`
elsewhere on the platform. A subject pseudonym is tenant-scoped precisely so two tenants cannot
correlate the same person. Here the whole point is cross-tenant: the anti-abuse question is "has
this business already been given a gift ANYWHERE", and a tenant-scoped hash cannot answer it. That
is a real privacy cost, taken deliberately for fraud prevention, and it is why `claim_ref` never
leaves the service — handing it to a caller would let THEM correlate people across tenants, which
is the linkage the hashing exists to contain.

Normalisation matters more than it looks: `1010123456`, ` 1010123456 ` and `1010-123456` are one
registration, and a hash that treats them as three is an anti-abuse control that a space defeats.
"""
from __future__ import annotations

import hashlib
import hmac
import re
import unicodedata

_STRIP = re.compile(r"[\s\-_/\\.]+")
_PREFIX = "clm_"
_LEN = 32


def normalise(value: str) -> str:
    """One identity, one string.

    Case-folded, unicode-normalised, and stripped of the separators humans sprinkle through
    reference numbers. Arabic-Indic digits are folded to ASCII, because the same CR typed on an
    Arabic keyboard must not read as a different company.
    """
    if not value:
        return ""
    text = unicodedata.normalize("NFKC", value).strip().casefold()
    # NFKC already maps ٠١٢…٩ to 0-9; this catches the separators it leaves behind.
    return _STRIP.sub("", text)


def claim_ref(salt: str, claim_type: str, value: str) -> str:
    """The stable, one-way handle for this identity.

    An empty salt is refused rather than silently producing an unsalted digest — an unsalted hash
    of a national id is reversible by trying every id, which is not a pseudonym at all. National id
    spaces are small and enumerable; that is exactly why the salt is not optional.
    """
    if not salt:
        raise ValueError(
            "PII_SALT is empty — an unsalted hash of a national id or CR is reversible by "
            "enumerating the id space, which makes it a lookup table rather than a pseudonym")
    cleaned = normalise(value)
    if not cleaned:
        raise ValueError("cannot reference an empty claim value")
    mac = hmac.new(salt.encode("utf-8"),
                   f"{claim_type}|{cleaned}".encode("utf-8"),
                   hashlib.sha256)
    return _PREFIX + mac.hexdigest()[:_LEN]
