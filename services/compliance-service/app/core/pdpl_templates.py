"""PDPL template pack — fill-in-the-blank, bilingual Saudi-PDPL documents a tenant can publish in
minutes instead of drafting from scratch.

The templates are copied AS-IS from the authored pack under data/pdpl-templates/*.yaml: bilingual
(en/ar) BLOCK documents (`{kind: {…}}` — cover, clause, table, callout, bullets, snapshot …) with
`{{PLACEHOLDER}}` slots for the tenant's own details. This module is the thin bridge to the
legal-pages store: it lists the pack and returns a template's blocks + the tenant's values, unchanged.

We deliberately DO NOT flatten the blocks or substitute placeholders here. The UI renders the SAME
rich block engine the pack was authored in (`DocBlocksView`), which resolves `{{TOKEN}}` live from the
tenant's values and picks the language per the reader's locale — so a table stays a table and an
Arabic reader gets Arabic, none of which survives a flatten-to-text. The backend's only jobs are to
hand over the blocks and to REFUSE a document whose placeholders are not all supplied, so a policy
with a live {{HOLE}} can never be published.
"""
from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import yaml

_DIR = Path(__file__).resolve().parents[1] / "data" / "pdpl-templates"
_PLACEHOLDER = re.compile(r"\{\{([A-Z0-9_]+)\}\}")


class TemplateError(Exception):
    """An unknown template, or one published with placeholders still unfilled."""


def _dir() -> Path:
    return _DIR


def _safe(name: str) -> str:
    if not re.fullmatch(r"[A-Za-z0-9_]+", name or ""):
        raise TemplateError(f"invalid template id {name!r}")
    return name


def _placeholders_in(text: str) -> set[str]:
    return set(_PLACEHOLDER.findall(text))


def list_templates() -> list[dict[str, Any]]:
    """Every template in the pack: id, slug, kind, bilingual title, and the placeholders it needs.

    `placeholders` are scanned from the document TEXT, not the authored `placeholders:` header, which
    in the source pack is sometimes incomplete — a form built from a short list would leave live
    {{HOLE}}s in a published policy, so we trust the text."""
    out = []
    for p in sorted(_dir().glob("*.yaml")):
        raw = p.read_text(encoding="utf-8")
        doc = yaml.safe_load(raw) or {}
        found = _placeholders_in(raw) | set(doc.get("placeholders", []))
        out.append({"id": p.stem, "slug": doc.get("slug", p.stem), "kind": doc.get("kind", ""),
                    "title": doc.get("title", {}), "placeholders": sorted(found)})
    return out


def load(template_id: str) -> dict[str, Any]:
    path = _dir() / f"{_safe(template_id)}.yaml"
    if not path.is_file():
        raise TemplateError(f"no PDPL template '{template_id}'")
    return yaml.safe_load(path.read_text(encoding="utf-8")) or {}


def render(template_id: str, values: dict[str, str], *, strict: bool = True) -> dict[str, Any]:
    """The template's rich blocks + the tenant's values, ready for the legal-pages store.

    Returns {"title": {en,ar}, "slug", "blocks": [...], "values": {...}, "missing": [...]}. `missing`
    lists placeholders the document uses that `values` did not supply; with strict=True (the default)
    that raises — a page with an unfilled {{TOKEN}} must never be publishable. The blocks are passed
    through untouched: the renderer fills tokens from `values` and picks the language at read time."""
    path = _dir() / f"{_safe(template_id)}.yaml"
    if not path.is_file():
        raise TemplateError(f"no PDPL template '{template_id}'")
    raw = path.read_text(encoding="utf-8")
    doc = yaml.safe_load(raw) or {}
    required = _placeholders_in(raw) | set(doc.get("placeholders", []))
    supplied = {k: v for k, v in (values or {}).items()}
    missing = sorted(required - set(supplied))
    if strict and missing:
        raise TemplateError(f"{template_id}: unfilled placeholders {missing}")
    return {"title": doc.get("title", {}), "slug": doc.get("slug", template_id),
            "blocks": doc.get("blocks", []) or [], "values": supplied, "missing": missing}
