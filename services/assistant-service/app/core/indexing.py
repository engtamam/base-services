"""Turning entries into something searchable — normalisation, tokens, chunks.

NORMALISATION IS WHERE ARABIC SEARCH IS WON OR LOST. The same word is written several ways and a
reader considers them identical, so a matcher that does not must be told:

  * short vowels (harakat) are optional and usually absent — كتب and كَتَبَ are the same word
  * alef carries hamza inconsistently — أحمد إحمد آحمد احمد are typed interchangeably
  * final ya and alef maqsura are confused constantly — على / علي
  * tatweel (ـــ) is decoration and carries no meaning at all

Without folding these, a user who types the word one way simply does not find help written the
other way, and the failure is invisible: the assistant says it does not know, and nobody can tell
that it does know.

WHAT WE DELIBERATELY DO NOT DO: stemming. Arabic morphology is root-and-pattern, so a naive
suffix-stripper mangles words and an accurate one is a large dependency. Folding orthography is
cheap and safe; guessing at morphology is neither, and half-done stemming loses recall while
appearing to add it. This is stated in the phase doc as a limit rather than implied as parity.

CHUNKING EXISTS FOR CITATION, NOT FOR SIZE. A whole entry is usually small. But an answer must cite
the passage it used, and "somewhere in this 2,000-word page" is not a citation a reader can check.
Splitting on paragraphs keeps a chunk to one idea, so the citation points at the sentence that
actually answered the question.
"""
from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass

from app.core.kb import KbEntry

# Arabic diacritics (harakat, shadda, sukun) and the superscript alef.
_HARAKAT = re.compile(r"[ؐ-ًؚ-ٰٟۖ-ۭ]")
_TATWEEL = re.compile(r"ـ")
# alef with hamza/madda -> bare alef; alef maqsura -> ya; ta marbuta -> ha
_ALEF = re.compile(r"[آأإٱ]")
_TRANS = {"ى": "ي", "ة": "ه"}

# A token is a run of letters/digits in any script. Explicitly Unicode-aware: `\w` under `re`
# already is, but naming it here keeps the intent when someone later "optimises" it to ASCII.
_TOKEN = re.compile(r"[^\W_]+", re.UNICODE)


def normalize(text: str) -> str:
    """Fold away the differences a reader does not perceive.

    NFKC first, so visually identical characters with different code points collapse before any
    script-specific rule runs — otherwise a presentation-form alef survives the alef rule and the
    word still does not match.
    """
    if not text:
        return ""
    out = unicodedata.normalize("NFKC", text)
    out = _HARAKAT.sub("", out)
    out = _TATWEEL.sub("", out)
    out = _ALEF.sub("ا", out)
    for src, dst in _TRANS.items():
        out = out.replace(src, dst)
    return out.casefold()


# Function words carry no topic. They matter because BM25 leans on IDF to discount common terms,
# and IDF is unreliable on a SMALL corpus: in a fifty-entry KB a word appearing in five entries
# looks genuinely rare, so "how do I …" scores against whichever entry happens to contain "do".
# That is how an unrelated question retrieves a confident, irrelevant passage. Dropping them is a
# precision fix, not an optimisation.
#
# Deliberately short — a long stopword list starts removing words that ARE the question ("how to
# CANCEL"), and the failure then looks like the KB is missing an entry.
_STOP_EN = frozenset("""
a an and are as at be by can do does for from had has have how i if in into is it its me my of on
or our so than that the their them then there these they this to was we what when where which who
why will with you your
""".split())

# Arabic function words and the inseparable prefixes that attach to them.
_STOP_AR = frozenset("""
في من الى إلى على عن مع هذا هذه ذلك تلك التي الذي ما ماذا كيف هل ان أن إن كان يكون قد لقد
او أو ثم حتى كل بعض عند لدى هو هي هم انا أنا نحن انت أنت لا لم لن نعم و ف ب ل ك
""".split())

_STOPWORDS = _STOP_EN | _STOP_AR


def tokenize(text: str, *, drop_stopwords: bool = True) -> list[str]:
    """Normalised tokens. The one function both indexing and querying call — if they ever differ,
    a document becomes unfindable by the exact words it contains, which is the hardest search bug
    to see because everything looks correct on both sides."""
    tokens = _TOKEN.findall(normalize(text))
    if not drop_stopwords:
        return tokens
    kept = [tok for tok in tokens if tok not in _STOPWORDS]
    # If a query is ENTIRELY function words ("how do I"), keep them: returning nothing would make
    # the query unmatchable, and an empty result there is indistinguishable from "we have no help",
    # which is a worse answer than a weak one.
    return kept or tokens


@dataclass(frozen=True)
class Chunk:
    """A citable passage: the text, and the entry it came from."""

    entry_id: str
    ordinal: int                 # position within the entry, so a citation can be stable
    text: str
    tokens: tuple[str, ...]
    lang: str

    @property
    def ref(self) -> str:
        """What an answer cites. Includes the ordinal so two passages of one entry are
        distinguishable — otherwise both cite the same id and the reader cannot tell which."""
        return f"{self.entry_id}#{self.ordinal}"


def _split_paragraphs(body: str, max_chars: int) -> list[str]:
    """Paragraphs, with anything oversized split on sentence ends rather than mid-word.

    Splitting purely by character count is what produces a citation ending mid-sentence, which
    reads as a bug in the product even when the retrieval was right.
    """
    parts = [p.strip() for p in re.split(r"\n\s*\n", body) if p.strip()]
    out: list[str] = []
    for part in parts:
        if len(part) <= max_chars:
            out.append(part)
            continue
        # Sentence-ish boundaries, including the Arabic full stop and question mark.
        sentences = re.split(r"(?<=[.!?؟۔])\s+", part)
        buf = ""
        for s in sentences:
            if buf and len(buf) + len(s) + 1 > max_chars:
                out.append(buf.strip())
                buf = s
            else:
                buf = f"{buf} {s}".strip()
        if buf.strip():
            out.append(buf.strip())
    return out


def chunk_entry(entry: KbEntry, lang: str, *, max_chars: int) -> list[Chunk]:
    """Chunks for one entry in one language.

    THE TITLE IS PREPENDED TO EVERY CHUNK. A passage lifted out of the middle of an entry loses
    what it is about — "it is 15 digits" answers nothing without "VAT number". This costs a few
    tokens per chunk and buys both better matching and a citation that makes sense alone.
    """
    title, body = entry.text(lang)
    chunks: list[Chunk] = []
    for i, para in enumerate(_split_paragraphs(body, max_chars) or [body]):
        text = f"{title}\n\n{para}" if title else para
        chunks.append(Chunk(entry_id=entry.id, ordinal=i, text=text,
                            tokens=tuple(tokenize(text)), lang=lang))
    return chunks


def chunk_all(entries: tuple[KbEntry, ...] | list[KbEntry], *, max_chars: int) -> list[Chunk]:
    """Chunk every entry in every language it is written in.

    Every language, not just the caller's: a KB where the Arabic is richer than the English should
    still answer an Arabic question from the Arabic text. Retrieval filters by language at query
    time, so indexing everything costs memory and loses nothing.
    """
    out: list[Chunk] = []
    for entry in entries:
        for lang in sorted(set(entry.title) | set(entry.body)):
            out.extend(chunk_entry(entry, lang, max_chars=max_chars))
    return out
