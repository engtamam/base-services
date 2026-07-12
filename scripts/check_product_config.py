#!/usr/bin/env python3
"""check_product_config.py — guard rails for config/product.json.

The JSON Schema validates SHAPE. This script checks the four things a schema cannot see.

  1. `locale.rtl` must be a subset of `locale.supported`, and `locale.default` must be in it.
     Otherwise the UI flips right-to-left for a language the product does not ship.

  2. Every bilingual block carries exactly the locales in `locale.supported`.
     A missing `ar` is not caught by a schema — it is caught by a customer.

  3. `identifiers` must equal the COMMITTED lock file, and the lock may not change, unless the
     commit body carries a `MIGRATION:` line.

     The lock exists because `product.json` is never committed (same rule as `.env`). Comparing
     it against its own previous commit therefore compared it against nothing: `git show` failed,
     and the check skipped on every run since the day it was written.

     This is the check that matters. A rebrand that silently edits `signatureDomain` invalidates
     the signature on every artifact ever signed. Forever. No schema sees it, because the new
     value is perfectly well-formed. No test sees it, because tests sign and verify in the same
     process, with the same config.

  4. Versioned identifiers (`linkNs`, `signatureDomain`) may only move forward by bumping the
     version suffix — `acme.link.v1` -> `acme.link.v2` — never by editing the stem in place.
     Old artifacts must keep verifying against the old value.

Plus one rule the schema cannot express because it does not know the slug:
no service name may contain the product name. `billing`, never `acme-billing`.

Zero runtime dependencies. `jsonschema` is used when installed and skipped when not, so this
runs on a bare box and still enforces the rules that matter.

Usage:
    python scripts/check_product_config.py            # config/product.json, else the example
    python scripts/check_product_config.py <path>
    python scripts/check_product_config.py --strict   # a skipped schema check is a FAILURE

Exit codes:  0 ok · 1 a rule was broken · 2 the config could not be read
"""
from __future__ import annotations

import json
import re
import subprocess
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
CONFIG = ROOT / "config" / "product.json"
EXAMPLE = ROOT / "config" / "product.example.json"
SCHEMA = ROOT / "config" / "product.schema.json"
# COMMITTED. product.json is not — which is exactly why the freeze needs its own file.
LOCK = ROOT / "config" / "product.identifiers.lock.json"

# Written into data a customer already holds. See config/product.example.json -> identifiers._frozen
FROZEN = ("jwtIssuer", "linkNs", "signatureDomain", "eventNs", "userAgent",
          "rolloutSalt", "piiSalt")

# Of those, the ones that may only ever move forward by a version bump.
VERSIONED = ("linkNs", "signatureDomain")
_VERSIONED_RE = re.compile(r"^(?P<stem>[a-z0-9.]+)\.v(?P<n>\d+)$")

# A commit that intends to change a frozen identifier must say so, in the commit body — on a
# line of its own, beginning at column zero, followed by a reason. See _commit_allows_migration.
MIGRATION_MARKER = "MIGRATION:"
_MIGRATION_MIN_REASON = 10       # "MIGRATION: x" is not a reason; the compatibility window is

# Blocks that are one string per locale.
I18N_PATHS = (
    ("identity", "tagline"),
    ("identity", "description"),
    ("brand", "documentFooter"),
    ("legal", "address"),
)

_errors: list[str] = []
_notes: list[str] = []


def fail(msg: str) -> None:
    _errors.append(msg)


def note(msg: str) -> None:
    _notes.append(msg)


def _strip_meta(obj: Any) -> Any:
    """Drop `_readme` / `_frozen` / `_help` keys — prose, not configuration."""
    if isinstance(obj, dict):
        return {k: _strip_meta(v) for k, v in obj.items() if not k.startswith("_")}
    if isinstance(obj, list):
        return [_strip_meta(v) for v in obj]
    return obj


def _dig(cfg: dict, path: tuple[str, ...]) -> Any:
    node: Any = cfg
    for key in path:
        if not isinstance(node, dict) or key not in node:
            return None
        node = node[key]
    return node


def _git(*args: str) -> str | None:
    """Run git, or return None when there is no repo, no git, or no history yet."""
    try:
        out = subprocess.run(
            ["git", *args], cwd=ROOT, capture_output=True, text=True, timeout=10,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    return out.stdout if out.returncode == 0 else None


# ── 1. shape ─────────────────────────────────────────────────────────────────────
def check_schema(cfg: dict, strict: bool = False) -> None:
    try:
        import jsonschema  # type: ignore
    except ImportError:
        if strict:
            # A gate that skips is a gate that passes. CI runs --strict for exactly this reason.
            fail("jsonschema is not installed and --strict was requested: the SHAPE of the "
                 "config was not validated at all. `pip install jsonschema`.")
        else:
            note("jsonschema not installed — shape not validated (every rule below still ran)")
        return
    try:
        schema = json.loads(SCHEMA.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as e:
        fail(f"config/product.schema.json unreadable: {e}")
        return
    validator = jsonschema.Draft202012Validator(schema)
    for err in sorted(validator.iter_errors(cfg), key=lambda e: list(e.path)):
        where = "/".join(str(p) for p in err.path) or "(root)"
        fail(f"schema: {where}: {err.message}")


# ── 2. locales ───────────────────────────────────────────────────────────────────
def check_locales(cfg: dict) -> None:
    loc = cfg.get("locale", {})
    supported = set(loc.get("supported", []))
    rtl = set(loc.get("rtl", []))
    default = loc.get("default")

    if extra := rtl - supported:
        fail(f"locale.rtl has {sorted(extra)} which are not in locale.supported — "
             f"the UI would flip right-to-left for a language the product does not ship")
    if default and default not in supported:
        fail(f"locale.default {default!r} is not in locale.supported")


# ── 3. every customer-visible string ships in every language ─────────────────────
def check_i18n(cfg: dict) -> None:
    supported = set(cfg.get("locale", {}).get("supported", []))
    if not supported:
        return
    for path in I18N_PATHS:
        block = _dig(cfg, path)
        if not isinstance(block, dict):
            continue
        keys = {k for k in block if not k.startswith("_")}
        if missing := supported - keys:
            fail(f"{'.'.join(path)}: missing {sorted(missing)} — every customer-visible string "
                 f"ships in every supported language from day one, never bolted on later")
        if extra := keys - supported:
            fail(f"{'.'.join(path)}: has {sorted(extra)}, which is not in locale.supported")


# ── 4. frozen identifiers ────────────────────────────────────────────────────────
def _lock_rel() -> str:
    """The lock's repo-relative path, or its bare name when it lives outside ROOT (tests)."""
    return LOCK.relative_to(ROOT).as_posix() if LOCK.is_relative_to(ROOT) else LOCK.name


def _read_lock() -> dict | None:
    """The COMMITTED frozen identifiers. See config/product.identifiers.lock.json."""
    try:
        return _strip_meta(json.loads(LOCK.read_text(encoding="utf-8")))
    except (OSError, json.JSONDecodeError):
        return None


def _previous_lock() -> dict | None:
    """The lock as of the last commit — to catch someone editing the lock itself."""
    raw = _git("show", f"HEAD:{_lock_rel()}")
    if raw is None:
        return None
    try:
        return _strip_meta(json.loads(raw))
    except json.JSONDecodeError:
        return None


def _commit_allows_migration() -> bool:
    """True only if the commit body carries a MIGRATION: DIRECTIVE, not a mention of one.

    This was `MIGRATION_MARKER in body` — a substring search over the whole message. Any commit
    whose body mentioned the string anywhere granted itself the exemption, including a commit
    that merely *documented the rule*. The commit that introduced this lock did exactly that:
    its body reads "...requires a MIGRATION: line in the commit body", so on that commit the
    freeze was disarmed and a silent change to `signatureDomain` passed with exit 0.

    A guard that the act of explaining it can disable is not a guard.

    So the marker must begin a line, at column zero — a prose sentence that happens to name it
    is indented, quoted, or mid-sentence — and it must be followed by an actual reason. An empty
    `MIGRATION:` is someone reaching for the exemption without saying why.
    """
    body = _git("log", "-1", "--format=%B") or ""
    for line in body.splitlines():
        if not line.startswith(MIGRATION_MARKER):
            continue                      # indented or mid-sentence: prose, not a directive
        reason = line[len(MIGRATION_MARKER):].strip()
        if len(reason) >= _MIGRATION_MIN_REASON:
            return True
    return False


def _check_version_bump(key: str, old: str, new: str) -> None:
    """A versioned identifier may only move v(n) -> v(n+1) on the same stem."""
    m_old, m_new = _VERSIONED_RE.match(old), _VERSIONED_RE.match(new)
    if not m_new:
        fail(f"identifiers.{key}: {new!r} must end in a version suffix, e.g. {old!r}")
        return
    if not m_old:
        return  # no sane baseline to compare against (was empty in the example)
    if m_old["stem"] != m_new["stem"]:
        fail(f"identifiers.{key}: the stem changed {m_old['stem']!r} -> {m_new['stem']!r}. "
             f"Only the version may move — old artifacts must keep verifying.")
        return
    if int(m_new["n"]) != int(m_old["n"]) + 1:
        fail(f"identifiers.{key}: the version must increment by one "
             f"(v{m_old['n']} -> v{int(m_old['n']) + 1}), got v{m_new['n']}")


_WHY_FROZEN = (
    "    These are written into data your customers already hold, and into artifacts\n"
    "    you have already signed:\n"
    "      jwtIssuer        every live session and refresh token is rejected\n"
    "      linkNs           every share link ever sent stops resolving\n"
    "      signatureDomain  every signed artifact fails verification, forever\n"
)


def check_frozen(cfg: dict) -> None:
    """The identifiers must equal the COMMITTED lock, and the lock may not move casually.

    Two comparisons, because there are two ways to break this:

      1. `product.json` drifts from the lock — a rebrand that edited `identifiers` by hand.
      2. The lock itself is edited — the same rebrand, one file deeper.

    The old version compared `product.json` against `git show HEAD:config/product.json`. That
    file is never committed, so git returned nothing and the check skipped on every single run.
    An absent baseline is not a passing check; it is no check at all.
    """
    current = _strip_meta(cfg.get("identifiers", {}))
    lock = _read_lock()
    if lock is None:
        fail(f"{_lock_rel()} is missing or unreadable — the frozen "
             f"identifiers have no baseline, so nothing is frozen")
        return

    # (1) product.json vs the lock
    drifted = {k for k in FROZEN if str(current.get(k, "")) != str(lock.get(k, ""))}
    if drifted and not _commit_allows_migration():
        listing = "\n".join(
            f"    {k}: lock={lock.get(k)!r} -> product.json={current.get(k)!r}"
            for k in sorted(drifted))
        fail("identifiers in product.json do not match the committed lock:\n"
             f"{listing}\n\n{_WHY_FROZEN}\n"
             "    To rebrand, edit `identity` and `brand`. Leave `identifiers` alone.\n"
             f"    If the change is intended, edit BOTH files and put '{MIGRATION_MARKER} <why>'\n"
             "    in the commit body, with the compatibility window.")

    # (2) the lock vs its own last commit
    previous = _previous_lock()
    if previous is None:
        note("the lock has no previous commit yet — nothing to compare it against")
        return

    # ADDING a new frozen identifier is not ROTATING one. A key that did not exist in the previous
    # lock and is empty ("not yet chosen") in the new one breaks nothing: no record anywhere was
    # written with it, so nothing can orphan. Only a key whose VALUE moves — including from empty
    # to chosen once data exists, and any change to a non-empty value — is a migration.
    changed = {k for k in FROZEN
               if previous.get(k) != lock.get(k)
               and not (k not in previous and not lock.get(k))}
    if not changed:
        return

    if not _commit_allows_migration():
        listing = "\n".join(f"    {k}: {previous.get(k)!r} -> {lock.get(k)!r}"
                            for k in sorted(changed))
        fail("the frozen-identifier LOCK changed without a migration note:\n"
             f"{listing}\n\n{_WHY_FROZEN}\n"
             f"    Put '{MIGRATION_MARKER} <why>' in the commit body and ship the "
             f"compatibility window with it.")
        return

    note(f"{MIGRATION_MARKER} found in commit body — identifier change permitted")
    for key in sorted(changed & set(VERSIONED)):
        old_value, new_value = str(previous.get(key, "")), str(lock.get(key, ""))
        if old_value:                     # "" means never chosen; the first value is not a bump
            _check_version_bump(key, old_value, new_value)


# ── 5. a real product.json has no placeholders left ──────────────────────────────
# The schema permits an empty string everywhere, so product.example.json validates as shipped.
# A filled product.json must not. These are the fields a product cannot run without.
REQUIRED_FILLED = (
    ("identity", "slug"),
    ("identity", "name"),
    ("support", "email"),
    ("support", "securityEmail"),
    ("brand", "wordmark"),
    ("identifiers", "jwtIssuer"),
    ("identifiers", "linkNs"),
    ("identifiers", "signatureDomain"),
    ("identifiers", "eventNs"),
    ("identifiers", "userAgent"),
)

# Values carried over from the example that are never right in production.
EXAMPLE_HOSTS = ("example.com", "example.org")


def check_filled(cfg: dict) -> None:
    """Only for a real product.json — the example is allowed to be empty."""
    for path in REQUIRED_FILLED:
        if not str(_dig(cfg, path) or "").strip():
            fail(f"{'.'.join(path)} is empty. An empty value is a visible placeholder in the "
                 f"example, never a shipped product.")

    for key, url in (cfg.get("urls") or {}).items():
        if key.startswith("_") or not isinstance(url, str):
            continue
        if any(h in url for h in EXAMPLE_HOSTS):
            fail(f"urls.{key} still points at the example host: {url!r}")

    for path in I18N_PATHS:
        block = _dig(cfg, path)
        if isinstance(block, dict):
            for loc, text in block.items():
                if not loc.startswith("_") and not str(text).strip():
                    fail(f"{'.'.join(path)}.{loc} is empty — it would print as a blank string "
                         f"to a customer reading that language")


# ── 6. the product name appears nowhere it must not ──────────────────────────────
def check_service_names(cfg: dict) -> None:
    slug = cfg.get("identity", {}).get("slug", "")
    if not slug:
        return
    for name in cfg.get("services", {}).get("names", []):
        if slug in name:
            fail(f"services.names: {name!r} contains the product slug {slug!r}. "
                 f"A rename must never touch a service name, a database name, a table, "
                 f"a JSON key, a log field, or a header.")


def main(argv: list[str]) -> int:
    args = [a for a in argv[1:] if not a.startswith("-")]
    strict = "--strict" in argv[1:]
    if args:
        path = Path(args[0])
    elif CONFIG.exists():
        path = CONFIG
    else:
        path = EXAMPLE
        note("config/product.json not present — validating the example instead "
             "(a filled product.json is never committed, same rule as .env)")

    is_example = path.name.endswith(".example.json")

    try:
        cfg = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as e:
        print(f"FATAL  cannot read {path}: {e}", file=sys.stderr)
        return 2

    rel = path.relative_to(ROOT).as_posix() if path.is_relative_to(ROOT) else path.name

    check_schema(cfg, strict=strict)
    check_locales(cfg)
    check_i18n(cfg)
    check_frozen(cfg)
    check_service_names(cfg)
    if is_example:
        note("example file — placeholder values are expected and not treated as errors")
    else:
        check_filled(cfg)

    for n in _notes:
        print(f"note   {n}")
    for e in _errors:
        print(f"FAIL   {e}", file=sys.stderr)

    if _errors:
        print(f"\n{len(_errors)} problem(s) in {rel}", file=sys.stderr)
        return 1
    print(f"ok     {rel}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
