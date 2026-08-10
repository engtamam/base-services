"""PDPL template pack — fill-in-the-blank, bilingual Saudi-PDPL documents a tenant can publish in
minutes instead of drafting from scratch.

The templates themselves are copied AS-IS from the authored pack under data/pdpl-templates/*.yaml:
bilingual (en/ar) block documents with `{{PLACEHOLDER}}` slots for the tenant's own details
(company name, DPO contact, dates…). This module is the thin bridge between that authored form and
the legal-pages store: it lists the pack, substitutes a tenant's values into the placeholders, and
flattens the blocks into the `{locale: {title, sections}}` shape LegalPageService.draft expects — so
a template becomes a real, versioned, acceptance-gated legal page through the SAME publish path as
any hand-written one.

Deliberately forgiving: an unknown block type degrades to its readable text rather than raising, and
a missing placeholder is reported (not guessed) so a half-filled policy is caught before it is
published, never after.
"""
from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import yaml

_DIR = Path(__file__).resolve().parents[1] / "data" / "pdpl-templates"
_PLACEHOLDER = re.compile(r"\{\{([A-Z0-9_]+)\}\}")
_LOCALES = ("en", "ar")


def _dir() -> Path:
    return _DIR


def _placeholders_in(text: str) -> set[str]:
    return set(_PLACEHOLDER.findall(text))


def list_templates() -> list[dict[str, Any]]:
    """Every template in the pack: id, slug, title (both languages), and the placeholders it needs.

    The `placeholders` returned are the ACTUAL slots in the document, scanned from its text — not the
    authored `placeholders:` header, which in the source pack is sometimes incomplete. A form built
    from an incomplete list would leave live {{HOLE}}s in a published policy, so we trust the text."""
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


class TemplateError(Exception):
    """An unknown template, or one published with placeholders still unfilled."""


def render(template_id: str, values: dict[str, str], *, strict: bool = True) -> dict[str, Any]:
    """Fill `values` into the template and flatten it to the legal-pages content shape.

    Returns {"content": {locale: {title, sections}}, "slug", "missing": [...]}. `missing` lists
    placeholders the template declares but `values` did not supply; with strict=True (the default)
    an unfilled placeholder raises rather than publishing a document with a literal {{HOLE}} in it."""
    path = _dir() / f"{_safe(template_id)}.yaml"
    if not path.is_file():
        raise TemplateError(f"no PDPL template '{template_id}'")
    raw = path.read_text(encoding="utf-8")
    doc = yaml.safe_load(raw) or {}
    # The REQUIRED set is every slot in the text (∪ declared), not the header alone — the source
    # pack's header is sometimes short, and a slot we don't ask for is a {{HOLE}} in a live policy.
    required = _placeholders_in(raw) | set(doc.get("placeholders", []))
    supplied = {k: v for k, v in (values or {}).items()}
    missing = sorted(required - set(supplied))
    if strict and missing:
        raise TemplateError(f"{template_id}: unfilled placeholders {missing}")

    content: dict[str, Any] = {}
    for loc in _LOCALES:
        title = _text(doc.get("title", {}), loc, supplied)
        sections = []
        for block in doc.get("blocks", []) or []:
            sections.extend(_render_block(block, loc, supplied))
        content[loc] = {"title": title, "sections": [s for s in sections if s.get("body") or s.get("heading")]}
    return {"content": content, "slug": doc.get("slug", template_id), "missing": missing}


# ── internals ──────────────────────────────────────────────────────────────────
def _safe(name: str) -> str:
    if not re.fullmatch(r"[A-Za-z0-9_]+", name or ""):
        raise TemplateError(f"invalid template id {name!r}")
    return name


def _sub(text: str, values: dict[str, str]) -> str:
    return _PLACEHOLDER.sub(lambda m: str(values.get(m.group(1), m.group(0))), text)


def _text(val: Any, loc: str, values: dict[str, str]) -> str:
    """Pull localized text from a value that may be {en,ar}, a plain string, or a list, and
    substitute placeholders. Falls back to the other locale so a half-translated block is never
    blank."""
    if val is None:
        return ""
    if isinstance(val, dict):
        raw = val.get(loc) or val.get("en") or val.get("ar") or ""
        return _sub(str(raw), values)
    if isinstance(val, (list, tuple)):
        return " · ".join(_text(v, loc, values) for v in val)
    return _sub(str(val), values)


def _render_block(block: dict[str, Any], loc: str, values: dict[str, str]) -> list[dict[str, str]]:
    """One authored block → zero or more {heading, body} sections. Handles the pack's block
    vocabulary; anything unrecognised degrades to its flattened text so nothing is silently lost."""
    if not isinstance(block, dict) or not block:
        return []
    kind, body = next(iter(block.items()))
    b = body if isinstance(body, dict) else {}

    def heading() -> str:
        n = b.get("n")
        t = _text(b.get("title") or b.get("heading") or b.get("label"), loc, values)
        return f"{n}. {t}".strip(". ") if n and t else t

    if kind == "cover":                       # the title/subtitle already seed the doc title
        return [{"heading": "", "body": _text(b.get("subtitle"), loc, values)}]
    if kind in ("lead", "callout", "helper"):
        return [{"heading": "", "body": _text(b.get("text") or body, loc, values)}]
    if kind in ("clause", "section"):
        paras = b.get("paras") or b.get("body") or b.get("text") or []
        if isinstance(paras, (str, dict)):
            paras = [paras]
        return [{"heading": heading(),
                 "body": "\n\n".join(_text(p, loc, values) for p in paras)}]
    if kind == "bullets":
        items = b.get("items") or body or []
        return [{"heading": heading(), "body": "\n".join("• " + _text(i, loc, values) for i in items)}]
    if kind in ("key", "fields"):
        items = b.get("items") or b.get("rows") or []
        lines = []
        for it in items:
            if isinstance(it, dict):
                lines.append(f"{_text(it.get('label'), loc, values)}: {_text(it.get('value'), loc, values)}".strip(": "))
            else:
                lines.append(_text(it, loc, values))
        return [{"heading": heading(), "body": "\n".join(l for l in lines if l)}]
    if kind in ("table", "snapshot"):
        rows = b.get("rows") or []
        headers = [_text(h, loc, values) for h in (b.get("headers") or [])]
        lines = []
        for row in rows:
            cells = [_text(c, loc, values) for c in (row if isinstance(row, list) else [row])]
            if headers and len(headers) == len(cells):
                lines.append(" · ".join(f"{h}: {c}" for h, c in zip(headers, cells)))
            else:
                lines.append(" · ".join(cells))
        return [{"heading": heading(), "body": "\n".join(lines)}]
    if kind == "references":
        items = b.get("items") or body or []
        return [{"heading": _text(b.get("title"), loc, values) or ("References" if loc == "en" else "المراجع"),
                 "body": "\n".join(_text(i, loc, values) for i in items)}]
    if kind in ("footer_band", "signoff", "id"):
        return []                             # chrome, not content
    # Unknown block: keep its text rather than drop it.
    return [{"heading": heading(), "body": _text(body, loc, values)}]
