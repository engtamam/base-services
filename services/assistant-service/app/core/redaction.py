"""Redaction — what must not leave, and what must not be stored.

TWO DESTINATIONS, ONE RULE. A help question goes to a third-party model and into a conversation
row. Both are places a card number should never arrive, and users type card numbers into help
boxes constantly, because the help box is where you go when a payment failed.

ON BY DEFAULT. A redactor that ships disabled protects nobody: the deployment that most needs it is
the one that never read the setting. The cost of it being on is a rare over-redaction; the cost of
it being off is a permanent record of somebody's card in a log nobody can reach to erase.

BOUNDED PATTERNS ONLY. Every pattern here is written without nested quantifiers over an unbounded
run. That constraint is not stylistic: a pattern like `(?:\\d[ -]*?){13,16}` backtracks
catastrophically on a long digit string, and a user can paste one. A redactor that hangs the worker
is a denial of service wearing a compliance hat — and it is reached before authentication matters,
because the text is redacted on the way in.

REDACTION IS ONE-WAY. There is no vault and no re-inflation. Reversible redaction is a second copy
of the secret plus a key, which is strictly more to protect than the thing it replaced.
"""
from __future__ import annotations

import re
from dataclasses import dataclass

# Each pattern is anchored on word boundaries and uses bounded repetition. Ordered longest-first
# where two could overlap, so an IBAN is not half-consumed by the digit-run rule.
_PATTERNS: tuple[tuple[str, re.Pattern[str], str], ...] = (
    # IBAN — country code, check digits, then up to 30 alphanumerics. Bounded.
    ("iban", re.compile(r"\b[A-Z]{2}\d{2}[A-Z0-9]{10,30}\b"), "[iban]"),
    # Card-like digit runs, allowing single separators. `\d{4}(?:[ -]?\d{4}){2,4}` is bounded at
    # every step: no quantifier nests over another unbounded quantifier.
    ("card", re.compile(r"\b\d{4}(?:[ -]?\d{4}){2,4}\b"), "[card]"),
    ("email", re.compile(r"\b[A-Za-z0-9._%+-]{1,64}@[A-Za-z0-9.-]{1,255}\.[A-Za-z]{2,24}\b"), "[email]"),
    # Saudi national ID / Iqama: exactly 10 digits, 1 = citizen, 2 = resident. Bounded, and placed
    # BEFORE the phone rule so a bare 10-digit id is labelled national_id rather than swept up as a
    # phone (a Saudi mobile starts with 0, so it never matches this). KSA-market PII — the same
    # identifier audit-service already treats as sensitive (national_id/iqama).
    ("national_id", re.compile(r"\b[12]\d{9}\b"), "[national_id]"),
    # International and local mobile forms, bounded length.
    ("phone", re.compile(r"(?<![\w])\+?\d{1,4}[ -]?\(?\d{1,4}\)?(?:[ -]?\d{2,4}){2,4}(?![\w])"), "[phone]"),
    # A long opaque token: api keys, bearer secrets. Bounded upper limit so it cannot scan forever.
    ("secret", re.compile(r"\b(?:sk|pk|ghp|gho|api[_-]?key)[-_][A-Za-z0-9]{16,64}\b", re.I), "[secret]"),
)


@dataclass(frozen=True)
class Redacted:
    text: str
    hits: tuple[str, ...] = ()

    @property
    def changed(self) -> bool:
        return bool(self.hits)


def redact(text: str, *, enabled: bool = True, max_chars: int = 20_000) -> Redacted:
    """Replace anything that looks like a secret or an identifier.

    `max_chars` bounds the work regardless of pattern: a caller can always send more text than a
    regex should be asked to scan, and truncating is a better failure than a stalled worker. The
    limit is applied by REFUSING to scan beyond it rather than by silently passing the tail
    through — passing it through would mean the unscanned portion is the part that leaks.
    """
    if not enabled or not text:
        return Redacted(text=text)
    if len(text) > max_chars:
        text = text[:max_chars]
    hits: list[str] = []
    out = text
    for name, pattern, replacement in _PATTERNS:
        out, n = pattern.subn(replacement, out)
        if n:
            hits.append(name)
    return Redacted(text=out, hits=tuple(hits))
