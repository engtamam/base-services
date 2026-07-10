"""Text analyzer (pure) — the shared tokenizer/normalizer for indexing AND querying.

Indexing and querying MUST run identical analysis, or a term stored one way won't match a
query typed another way. So this one module does both.

KSA-critical: Arabic normalization. Saudi content is written with and without diacritics
(tashkeel) and with several visually-identical letter variants, so `مُحَمَّد` and `محمد`,
or `إبراهيم` and `ابراهيم`, are the *same word* to a user. Without folding, search silently
fails for half your Arabic corpus. We:
  • strip tashkeel (the combining marks U+064B..U+0652, U+0670, plus tatweel U+0640);
  • fold alef/hamza variants (أ إ آ ٱ → ا), yaa (ى → ي), taa-marbuta (ة → ه);
  • apply Unicode NFKC + case-fold for Latin/mixed text.
No regex over user input beyond a fixed character class → no ReDoS.
"""
from __future__ import annotations

import unicodedata

# Combining tashkeel + tatweel + superscript alef.
_TASHKEEL = {0x0640, 0x0670, *range(0x064B, 0x0653), *range(0x06D6, 0x06ED)}
_ARABIC_FOLD = {
    "أ": "ا", "إ": "ا", "آ": "ا", "ٱ": "ا",  # hamza-alef → alef
    "ى": "ي",                                                              # alef maqsura → yaa
    "ة": "ه",                                                              # taa marbuta → haa
    "ؤ": "و", "ئ": "ي",                                          # hamza on waw/yaa
}

_STOPWORDS = frozenset({
    # English
    "a", "an", "the", "and", "or", "of", "to", "in", "on", "for", "is", "are", "at", "by", "with",
    # Arabic (common particles)
    "في", "من", "على", "الى", "عن", "مع", "هذا", "هذه", "ذلك", "التي", "الذي", "و", "او",
})


def normalize(text: str, *, arabic: bool = True) -> str:
    text = unicodedata.normalize("NFKC", text or "").casefold()
    if arabic:
        out = []
        for ch in text:
            cp = ord(ch)
            if cp in _TASHKEEL:
                continue                      # drop diacritics/tatweel
            out.append(_ARABIC_FOLD.get(ch, ch))
        text = "".join(out)
    return text


def _is_token_char(ch: str) -> bool:
    return ch.isalnum() or ch == "_"


def tokenize(text: str, *, arabic: bool = True, stopwords: bool = True) -> list[str]:
    norm = normalize(text, arabic=arabic)
    tokens, cur = [], []
    for ch in norm:
        if _is_token_char(ch):
            cur.append(ch)
        elif cur:
            tokens.append("".join(cur))
            cur = []
    if cur:
        tokens.append("".join(cur))
    if stopwords:
        tokens = [t for t in tokens if t not in _STOPWORDS]
    return tokens


def analyze(text: str, *, arabic: bool = True, stopwords: bool = True) -> list[str]:
    return tokenize(text, arabic=arabic, stopwords=stopwords)


def edit_distance_le1(a: str, b: str) -> bool:
    """True if `a` and `b` differ by at most one insert/delete/substitution. Bounded, fast."""
    if a == b:
        return True
    la, lb = len(a), len(b)
    if abs(la - lb) > 1:
        return False
    if la == lb:                              # one substitution
        return sum(x != y for x, y in zip(a, b)) == 1
    # one insertion/deletion — walk the shorter against the longer
    if la > lb:
        a, b, la, lb = b, a, lb, la
    i = j = edits = 0
    while i < la and j < lb:
        if a[i] == b[j]:
            i += 1
            j += 1
        else:
            edits += 1
            j += 1
            if edits > 1:
                return False
    return True
