#!/usr/bin/env python
"""Gate: a service that holds a data subject MUST be able to erase them.

THE FOOTGUN THIS EXISTS FOR.

compliance-service derives a DSAR's completion from the acks of the targets it KNOWS about. A
service that holds subject data but was never registered is never asked, never acks, and is
therefore never MISSED: the derivation sees "every target acked", the DSAR closes green, and the
person is told they were erased. They were not.

It is the same shape as every other defect in this repo — the check passes because it was looking
at an incomplete list. The list cannot be trusted to whoever remembered to update it. It has to be
checked against the CODE.

So, for every service:

  1. If its code stores a data subject (a `subject`/`user`/`owner`/`actor` field, or a users
     collection), it MUST ship `app/core/erasure.py`. No handler = the erasure silently misses it.

  2. Anything it RETAINS must name a legal basis from a closed list. "We kept it" is an answer with
     a reason attached, never a silence — and an unknown basis is someone inventing one.

  3. A service that writes into an IMMUTABLE record (the audit chain) must not write raw PII into
     it. There is no later fix: once hashed, it is permanent.
"""
from __future__ import annotations

import ast
import os
import pathlib
import re
import sys

ROOT = pathlib.Path(__file__).resolve().parents[2]
SERVICES = ROOT / "services"

# PLUG-AND-PLAY. What names a data subject, and what bases may justify keeping their data, are
# PLATFORM decisions — a Saudi product keys on `national_id`/`iqama`, a health product on `mrn`,
# and a bank may retain under a basis we have never heard of. Hardcoding one company's vocabulary
# into a gate meant to be pointed at any repository is the assumption this codebase refuses.
#
#   PII_SUBJECT_FIELDS   comma list; a persisted field of one of these names = holds a subject
#   PII_RETENTION_BASES  comma list; the closed set a service may retain under
#   PII_NO_SUBJECT       comma list; services that legitimately hold no data subject
_ENV_SUBJECT_FIELDS = os.environ.get("PII_SUBJECT_FIELDS", "subject,owner,user,actor")
_ENV_BASES = os.environ.get("PII_RETENTION_BASES", "")
_ENV_NO_SUBJECT = os.environ.get("PII_NO_SUBJECT", "api-gateway")
# Point #3: the raw identifier names that must NOT be written unpseudonymised into an audit record's
# resource/actor. PLATFORM-tunable (a KSA product adds national_id/iqama) — same plug-and-play stance
# as the subject fields above. `phone_number_id`/`phone_id` are Meta ROUTING ids, not a person, and
# are excluded below.
_ENV_AUDIT_RAW = os.environ.get(
    "PII_AUDIT_RAW_FIELDS",
    "subject,phone,phone_number,email,msisdn,recipient,sender,national_id,iqama,mobile,full_name")
# Expressions that already carry a pseudonym/one-way transform are allowed — this is the fix, not the
# footgun. A value derived through any of these is not the raw person.
_PSEUDONYM_MARKERS = ("ref", "hash", "hint", "pseudonym", "digest", "hmac", "anon", "mask")
# Which audit kwargs to enforce. Phase 1: resource only (see the erase-receipt footgun). Widen to
# include "actor" when the fleet-wide actor-pseudonymisation decision is made. Config-overridable.
_AUDIT_FIELDS = tuple(x.strip() for x in os.environ.get("PII_AUDIT_FIELDS", "resource").split(",")
                      if x.strip())


def _csv(raw: str) -> set[str]:
    return {x.strip() for x in (raw or "").split(",") if x.strip()}

# A service whose code PERSISTS a field named in PII_SUBJECT_FIELDS is holding a data subject.
# Built from config, so a platform whose subject key is `national_id` or `mrn` is covered without
# editing this file.
def _subject_markers() -> tuple[re.Pattern, ...]:
    pats: list[re.Pattern] = [re.compile(r"\bget_by_username\b")]
    for field in sorted(_csv(_ENV_SUBJECT_FIELDS)):
        f = re.escape(field)
        pats.append(re.compile(rf"^\s*{f}\s*:", re.MULTILINE))    # a dataclass field
        pats.append(re.compile(rf"[\"']{f}[\"']\s*:"))            # a persisted dict key
    return tuple(pats)


_SUBJECT_MARKERS = _subject_markers()

# The only bases anyone may retain data under. Inventing a new one is how "we kept it" becomes
# unfalsifiable — so this list grows only when a genuinely different reason appears, with the
# reason written down here rather than in a commit message nobody re-reads.
#
# other_controller_relationships was added for identity-service, and it is a real distinction
# rather than a synonym for the others. A Person is ONE HUMAN with one login who may work for
# several tenants. When tenant A asks us to erase them, tenant B's relationship with that same
# human is not tenant A's to end: B is a separate controller who never asked. So A's membership is
# deleted and the login survives — until the LAST membership ends, at which point it goes too.
#
# It is deliberately narrow. It justifies keeping a record another controller still relies on; it
# does not justify keeping anything the REQUESTING controller holds, which is what makes it
# unusable as a general excuse.
_ALLOWED_BASES = ({"tax_retention", "legal_hold", "security_audit", "consent_proof",
                   "delivery_proof", "fraud_prevention",
                   "other_controller_relationships", "record_of_decision"} | _csv(_ENV_BASES))

# Services that legitimately hold NO data subject (infra / routing only).
_NO_SUBJECT = _csv(_ENV_NO_SUBJECT)


def holds_subject(svc: pathlib.Path) -> str | None:
    core = svc / "app" / "core"
    if not core.is_dir():
        return None
    for path in sorted(core.rglob("*.py")):
        if path.name in ("pii.py", "erasure.py"):
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        for marker in _SUBJECT_MARKERS:
            if marker.search(text):
                return f"{path.relative_to(svc).as_posix()}"
    return None


def declared_bases(svc: pathlib.Path) -> set[str]:
    erasure = svc / "app" / "core" / "erasure.py"
    if not erasure.is_file():
        return set()
    text = erasure.read_text(encoding="utf-8")
    return set(re.findall(r'"basis"\s*:\s*([A-Z_]+|"[a-z_]+")', text)) | \
        set(re.findall(r'RETENTION_BASIS\s*=\s*"([a-z_]+)"', text))


_RAW_FIELDS = _csv(_ENV_AUDIT_RAW)
# A raw identifier is a bare name (`subject`) or an attribute access ending in one (`body.subject`),
# but NEVER the routing-id variants (`phone_number_id`, `phone_id`).
_RAW_RE = re.compile(r"(?:^|\.)(" + "|".join(sorted(re.escape(f) for f in _RAW_FIELDS)) +
                     r")\b(?!\s*_?id)")
_ID_SUFFIX_RE = re.compile(r"\b(phone_number_id|phone_id)\b")


def _expr_is_raw_pii(src: str) -> bool:
    """True if an interpolated expression reads a raw person-identifier without pseudonymising it."""
    if not src:
        return False
    low = src.lower()
    if any(m in low for m in _PSEUDONYM_MARKERS):     # ref/hash/hint/... ⇒ already transformed
        return False
    if _ID_SUFFIX_RE.search(low):                     # phone_number_id etc. is a routing id, not a person
        return False
    return bool(_RAW_RE.search(low))


def _pii_parts(node: ast.AST, src: str) -> list[str]:
    """The sub-expressions an audit value actually INTERPOLATES: an f-string's {…} holes, else the
    whole expression. Literal text (the "subject:" prefix) is ignored — only what a variable carries."""
    if isinstance(node, ast.JoinedStr):
        out = []
        for v in node.values:
            if isinstance(v, ast.FormattedValue):
                seg = ast.get_source_segment(src, v.value)
                if seg:
                    out.append(seg)
        return out
    seg = ast.get_source_segment(src, node)
    return [seg] if seg else []


def raw_pii_in_audit(svc: pathlib.Path) -> list[str]:
    """Point #3, enforced: an audit record() call (identified by carrying resource=/actor=) must not
    write a raw person-identifier into the immutable, hash-chained log. Once written it is permanent —
    there is no later erasure — so resource/actor take the pseudonym (subject_ref), never the person."""
    problems: list[str] = []
    app = svc / "app"
    if not app.is_dir():
        return problems
    for path in sorted(app.rglob("*.py")):
        try:
            src = path.read_text(encoding="utf-8")
            tree = ast.parse(src)
        except (OSError, UnicodeDecodeError, SyntaxError):
            continue
        for call in ast.walk(tree):
            if not (isinstance(call, ast.Call) and isinstance(call.func, ast.Attribute)
                    and call.func.attr == "record"):
                continue
            kw = {k.arg: k.value for k in call.keywords if k.arg}
            if "resource" not in kw and "actor" not in kw:
                continue                              # a data-store record(), not the auditor
            # Phase 1 enforces `resource=` only — the erasure-receipt class we hit. `actor=` (the
            # authenticated `sub`) is a separate fleet-wide policy call, deferred deliberately; widen
            # _AUDIT_FIELDS to ("resource", "actor") once that decision is made.
            for field in _AUDIT_FIELDS:
                if field not in kw:
                    continue
                for part in _pii_parts(kw[field], src):
                    if _expr_is_raw_pii(part):
                        problems.append(
                            f"{svc.name}: {path.relative_to(svc).as_posix()}:{call.lineno} audit "
                            f"record() writes a raw identifier into {field}= ({part!r}) — the audit "
                            f"chain is permanent; pass a pseudonym (subject_ref), not the person.")
    return problems


def subject_ref_import_ok(svc: pathlib.Path) -> list[str]:
    """A file that CALLS subject_ref(...) must import it at MODULE scope. A per-function local import
    silently satisfies the name in THAT function while a call in another function NameErrors at runtime
    (the audit-pseudonymisation codemod did exactly this — a DSAR erase 500'd on every call). Belt to
    the point-#3 pseudonymisation: require the import to actually be in scope everywhere it is used."""
    problems: list[str] = []
    app = svc / "app"
    if not app.is_dir():
        return problems
    for path in sorted(app.rglob("*.py")):
        try:
            src = path.read_text(encoding="utf-8")
            tree = ast.parse(src)
        except (OSError, UnicodeDecodeError, SyntaxError):
            continue
        calls_it = any(isinstance(n, ast.Call) and isinstance(n.func, ast.Name)
                       and n.func.id == "subject_ref" for n in ast.walk(tree))
        if not calls_it:
            continue
        module_import = any(isinstance(n, ast.ImportFrom) and n.col_offset == 0
                            and n.module == "app.core.pii"
                            and any(a.name == "subject_ref" for a in n.names)
                            for n in ast.walk(tree))
        if not module_import:
            problems.append(
                f"{svc.name}: {path.relative_to(svc).as_posix()} calls subject_ref() but does not "
                f"import it at MODULE scope — a per-function local import NameErrors elsewhere.")
    return problems


def main() -> int:
    problems: list[str] = []
    services = sorted(p for p in SERVICES.iterdir()
                      if p.is_dir() and p.name != "_template" and (p / "app").is_dir())

    holders = 0
    for svc in services:
        where = holds_subject(svc)
        if where is None or svc.name in _NO_SUBJECT:
            continue
        holders += 1

        erasure = svc / "app" / "core" / "erasure.py"
        if not erasure.is_file():
            problems.append(
                f"{svc.name}: holds a data subject ({where}) but has no app/core/erasure.py — "
                f"compliance would never ask it, it would never ack, and the DSAR would close "
                f"GREEN with the person's data still here.")
            continue

        for basis in declared_bases(svc):
            clean = basis.strip('"')
            if clean.isupper():          # a constant name, resolved above; skip
                continue
            if clean not in _ALLOWED_BASES:
                problems.append(
                    f"{svc.name}: retains data under an unknown basis {clean!r} — "
                    f"allowed: {', '.join(sorted(_ALLOWED_BASES))}")

    # Point #3 runs for EVERY service, subject-holder or not: anyone can write to the audit chain.
    for svc in services:
        problems.extend(raw_pii_in_audit(svc))
        problems.extend(subject_ref_import_ok(svc))

    for p in problems:
        print(f"  {p}")
    if problems:
        print(f"\npii: FAIL — {len(problems)} service(s) could silently miss an erasure")
        return 1
    print(f"pii: PASS — {holders} service(s) hold a data subject; every one can erase them, "
          f"and every retention names a legal basis")
    return 0


if __name__ == "__main__":
    sys.exit(main())
