"""A knowledge-base entry — one thing the product can explain about itself.

THE SHAPE IS DELIBERATELY SMALL. Two fields are required: a `title` and a `body`. Everything else
is optional and each optional field buys exactly one capability, so a first KB is a map of ids to a
title and some markdown, and nothing more needs to be understood to start.

    {"billing.vat_number": {
        "title": {"en": "VAT number",     "ar": "الرقم الضريبي"},
        "body":  {"en": "The 15-digit …", "ar": "…"}}}

That matters because a KB nobody can start writing is a KB that stays empty, and an empty KB makes
the assistant useless in a way no amount of retrieval quality can fix.

WHY ONE SHAPE FOR EVERY KIND OF CONTENT. Users ask about far more than actions: what a field means,
what a status implies, why a payment failed, what the refund policy is. If actions were a different
record type from glossary terms, every one of retrieval, ranking, citation and erasure would need
two code paths and the second would rot. `kind` is a free-form LABEL on one record, not a subclass.

THE THREE FIELDS THAT ARE NOT DECORATION:

  `surfaces`  which of an adopter's UIs this belongs to. Omitted means every surface, so shared
              concepts — the glossary — are authored ONCE rather than copied per app. Copies drift;
              a filter cannot.

  `requires`  a capability. Surface says what is RELEVANT; capability says what is ALLOWED. A client
              declares its own surface, so surface is a hint and never a control: without
              `requires`, a customer app could read admin help simply by claiming to be the admin
              app. Two layers, because the outer one is caller-supplied.

  `anchor`    ties the entry to a UI element, so a `?` icon can ask for exactly this entry by id.
              That path runs no model and no retrieval — it is a dictionary lookup, and it is the
              common case. Making the common case free is the difference between help that is used
              and help that is switched off for cost.

LANGUAGE IS A MAP, NOT A PAIR. `{"en": …, "ar": …}` and not `title_en`/`title_ar`, because a
platform that hardcodes two languages has to change its schema to accept a third.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

# Every surface, when an entry does not name one. Not a magic string scattered through the code:
# retrieval, the catalog builder and the loader all compare against this one name.
ALL_SURFACES = "*"


class KbError(ValueError):
    """An entry that cannot be trusted to answer a question. Raised at LOAD time, never at ask
    time: a malformed KB must fail the operator who deployed it, not the user who asked."""


def _text_map(value: Any, *, entry_id: str, field_name: str) -> dict[str, str]:
    """A language -> text map, validated.

    A bare string is accepted and read as the default language rather than refused. Refusing it
    would mean the smallest possible KB — an id, a title, a sentence — is a schema error, and the
    cost of that is an operator who gives up before the first entry exists.
    """
    if isinstance(value, str):
        # Checked here too, not only in the map branch below. An empty bare string is the same
        # defect as an empty translation — an entry that looks present and answers with nothing —
        # and it is the easier of the two to write by accident.
        if not value.strip():
            raise KbError(f"{entry_id}: {field_name} is empty")
        return {DEFAULT_LANG: value}
    if not isinstance(value, dict) or not value:
        raise KbError(f"{entry_id}: {field_name} must be a non-empty string or "
                      f"{{lang: text}} map, got {type(value).__name__}")
    out: dict[str, str] = {}
    for lang, text in value.items():
        if not isinstance(lang, str) or not lang.strip():
            raise KbError(f"{entry_id}: {field_name} has a non-string language key")
        if not isinstance(text, str) or not text.strip():
            # An empty translation is worse than a missing one: `get()` would return "" and the
            # answer would cite an entry that says nothing. Missing falls back; empty lies.
            raise KbError(f"{entry_id}: {field_name}[{lang}] is empty — omit the language "
                          f"instead, so it falls back rather than answering with nothing")
        out[lang.strip().lower()] = text
    return out


DEFAULT_LANG = "en"


@dataclass(frozen=True)
class KbEntry:
    """One explainable thing. Frozen: the index holds these, and an entry that could be mutated
    after indexing would make a citation point at text that no longer exists."""

    id: str
    title: dict[str, str]
    body: dict[str, str]
    kind: str = ""                       # free-form: glossary | field | howto | policy | faq | action
    surfaces: tuple[str, ...] = (ALL_SURFACES,)
    anchor: str = ""                     # UI element id, for the `?` icon's direct lookup
    requires: str = ""                   # capability needed to SEE this at all
    related: tuple[str, ...] = ()
    # Where this entry came from — "file", "catalog", … Kept so an operator can tell generated
    # entries from authored ones, and so a regenerated source can replace only its own.
    source: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)

    def text(self, lang: str) -> tuple[str, str]:
        """(title, body) in `lang`, falling back rather than returning nothing.

        Order: the asked language, then the default, then any language present. The last step is
        what stops a single untranslated entry from becoming an invisible hole in the KB — showing
        an Arabic answer to an English question is worse than silence only if you assume the reader
        cannot read it, and a citation the reader can check beats a gap they cannot.
        """
        return self._pick(self.title, lang), self._pick(self.body, lang)

    @staticmethod
    def _pick(m: dict[str, str], lang: str) -> str:
        key = (lang or DEFAULT_LANG).strip().lower()
        if key in m:
            return m[key]
        # "ar-SA" should find "ar" before it gives up.
        base = key.split("-", 1)[0]
        if base in m:
            return m[base]
        if DEFAULT_LANG in m:
            return m[DEFAULT_LANG]
        return next(iter(m.values()), "")

    def serves(self, surface: str) -> bool:
        """Does this entry belong on `surface`? Untagged entries serve every surface."""
        if ALL_SURFACES in self.surfaces:
            return True
        return (surface or "").strip().lower() in self.surfaces

    def visible_to(self, caps: frozenset[str]) -> bool:
        """FAIL CLOSED. An entry that names a capability is invisible without it.

        Not "hidden from the UI" — never retrieved, never cited, never counted. Help text is a
        description of what the platform can do, and describing an admin capability to someone who
        cannot hold it is how a customer learns which door to try.
        """
        return not self.requires or self.requires in caps


def parse_entry(entry_id: str, raw: Any, *, source: str = "") -> KbEntry:
    """Build one entry from loaded data, or say precisely what is wrong with it."""
    if not isinstance(entry_id, str) or not entry_id.strip():
        raise KbError("entry id must be a non-empty string")
    entry_id = entry_id.strip()
    if not isinstance(raw, dict):
        raise KbError(f"{entry_id}: entry must be an object, got {type(raw).__name__}")

    if "title" not in raw:
        raise KbError(f"{entry_id}: missing required field 'title'")
    if "body" not in raw:
        # `detail` is accepted as an alias because "body" and "detail" are equally natural names for
        # the same thing and an author should not lose an afternoon to that coin flip.
        if "detail" not in raw:
            raise KbError(f"{entry_id}: missing required field 'body'")

    surfaces_raw = raw.get("surfaces")
    if surfaces_raw is None:
        surfaces: tuple[str, ...] = (ALL_SURFACES,)
    elif isinstance(surfaces_raw, str):
        surfaces = (surfaces_raw.strip().lower(),)
    elif isinstance(surfaces_raw, (list, tuple)):
        surfaces = tuple(s.strip().lower() for s in surfaces_raw if isinstance(s, str) and s.strip())
        if not surfaces:
            raise KbError(f"{entry_id}: 'surfaces' is empty — omit it to serve every surface, "
                          f"rather than listing none and serving nothing")
    else:
        raise KbError(f"{entry_id}: 'surfaces' must be a string or a list of strings")

    related_raw = raw.get("related") or ()
    if isinstance(related_raw, str):
        related_raw = (related_raw,)
    if not isinstance(related_raw, (list, tuple)):
        raise KbError(f"{entry_id}: 'related' must be a list of entry ids")

    meta = raw.get("metadata") or {}
    if not isinstance(meta, dict):
        raise KbError(f"{entry_id}: 'metadata' must be an object")

    return KbEntry(
        id=entry_id,
        title=_text_map(raw["title"], entry_id=entry_id, field_name="title"),
        body=_text_map(raw.get("body", raw.get("detail")), entry_id=entry_id, field_name="body"),
        kind=str(raw.get("kind") or "").strip().lower(),
        surfaces=surfaces,
        anchor=str(raw.get("anchor") or "").strip(),
        requires=str(raw.get("requires") or "").strip(),
        related=tuple(str(r).strip() for r in related_raw if str(r).strip()),
        source=source,
        metadata=meta,
    )


def parse_kb(raw: Any, *, source: str = "") -> list[KbEntry]:
    """Parse a whole KB document: `{id: entry}` or `[{id: …, …}, …]`.

    Both shapes are accepted because both are natural — a map reads well hand-authored, a list is
    what a generator emits — and rejecting either would be a schema argument that produces no
    working software.

    EVERY entry is validated, and one bad entry fails the load. Skipping it would leave an operator
    with a KB that is quietly missing the answer they wrote, discoverable only when a user asks the
    question and is told nobody knows.
    """
    if isinstance(raw, dict):
        items = raw.items()
    elif isinstance(raw, list):
        items = []
        for i, obj in enumerate(raw):
            if not isinstance(obj, dict) or not obj.get("id"):
                raise KbError(f"entry #{i}: list-form entries need an 'id'")
            items.append((obj["id"], obj))
    else:
        raise KbError(f"kb must be an object or a list, got {type(raw).__name__}")

    entries, seen = [], set()
    for entry_id, obj in items:
        entry = parse_entry(entry_id, obj, source=source)
        if entry.id in seen:
            raise KbError(f"{entry.id}: duplicate id — one of them would silently win")
        seen.add(entry.id)
        entries.append(entry)
    return entries
