#!/usr/bin/env python3
"""base-services — self-contained secret generation for a server install.

The platform's secrets must NEVER live in git, but the server pipeline must still stand up a cluster
with no secret manager wired yet. So this splits the problem:

  * A COMMITTED, SECRET-FREE SCHEMA (secrets.schema.json) records only the STRUCTURE — which secret
    holds which keys, and how each key is produced (a const, a shared generated value, a per-role
    DSN, an alias). No sensitive value is ever written to it.

  * GENERATION happens here, ON THE SERVER, at install time. Shared values (INTERNAL_SERVICE_TOKEN,
    PII_SALT, the token secret, POSTGRES_PASSWORD, the per-install KEKs) are minted ONCE and written
    byte-identically into every secret that references them — a different PII_SALT in two pods would
    pseudonymise the same person two different ways, which is a data-integrity bug, not a cosmetic
    one. DATASTORE_DSN is built from the shared POSTGRES_PASSWORD so it always matches the password
    the postgres StatefulSet boots with (platform-secrets).

WHAT IS MINTED, AND WHAT IS NOT. Only the platform-internal secrets a fresh install needs to BOOT are
generated (below). It deliberately does NOT mint object-store credentials or third-party provider
tokens (SMTP/Twilio/ZATCA/...): those are real credentials an operator supplies to MATCH their own
S3 / provider, and inventing a random one would only break the integration. Add those to the same
secret out-of-band (or via the operator secrets API).

Modes:
  python3 gen-secrets.py --build     # (dev) regenerate secrets.schema.json from each config.py
  python3 gen-secrets.py             # (server) generate values + kubectl apply (preserves existing)

A production install would instead point at a secret manager (Vault / sealed-secrets); the schema is
exactly the contract such a manager would fill.
"""
from __future__ import annotations

import base64
import json
import os
import re
import secrets
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent.parent                      # repo root (deploy/server/ -> ../..)
SERVICES = ROOT / "services"
SCHEMA = HERE / "secrets.schema.json"

PRODUCT_NAME = os.environ.get("PRODUCT_NAME", "base-services")
NS = os.environ.get("NS", os.environ.get("NAMESPACE", "base-services"))
# A fresh install routes the DB through PgBouncer by DEFAULT (deploy/k3s/platform/pgbouncer.yaml,
# applied by bootstrap alongside Postgres): services keep a generous local pool while PgBouncer
# multiplexes onto a small, bounded server-connection count, so the fleet does not silently ship the
# unpooled 5-slot-per-service bottleneck that a manual cutover would be needed to undo. Set
# PG_HOST=postgres:5432 to bring up DIRECT to Postgres instead (no pooler). The generated DSN carries
# no sslmode, matching today's plaintext in-cluster hop; PgBouncer accepts plaintext clients by default.
PG_HOST = os.environ.get("PG_HOST", "pgbouncer:6432")
PG_DB = os.environ.get("PG_DB", "base_services")
POSTGRES_USER = os.environ.get("POSTGRES_USER", "base_services")
REDIS_URL = os.environ.get("REDIS_URL", "redis://redis:6379/0")

# Non-sensitive constants — carried verbatim into the schema and every secret.
CONSTANTS = {
    "CACHE_URL": REDIS_URL, "EVENTBUS_URL": REDIS_URL, "QUEUE_URL": REDIS_URL, "RATELIMIT_URL": REDIS_URL,
    "POSTGRES_DB": PG_DB, "POSTGRES_USER": POSTGRES_USER,
}
# Keys minted ONCE and shared byte-identically wherever they appear.
SHARED = ["INTERNAL_SERVICE_TOKEN", "PII_SALT", "TOKEN_HS256_SECRET", "POSTGRES_PASSWORD",
          "SECRETS_KEK", "BACKUP_KEK"]
# Aliases: key names that must always hold the SAME value as another.
ALIASES = {"TOKEN_SIGNING_SECRET": "TOKEN_HS256_SECRET"}
# Generated Ed25519 private keys (shared). None in base-services today; the mechanism stays.
ED25519: set[str] = set()

# THE MINTABLE VOCABULARY: the only config-fields gen-secrets owns. A service's key set is the
# intersection of this with the fields its config.py declares — so a service that has no KEK gets no
# KEK. Everything else in a config.py (URLs the chart sets in env, provider tokens, object-store
# creds) is out of scope by design.
MINTABLE: dict[str, dict] = {
    "DATASTORE_DSN": {"t": "dsn", "role": POSTGRES_USER},
    "CACHE_URL": {"t": "const"}, "EVENTBUS_URL": {"t": "const"},
    "QUEUE_URL": {"t": "const"}, "RATELIMIT_URL": {"t": "const"},
    "INTERNAL_SERVICE_TOKEN": {"t": "shared"}, "PII_SALT": {"t": "shared"},
    "TOKEN_HS256_SECRET": {"t": "shared"}, "TOKEN_SIGNING_SECRET": {"t": "alias", "of": "TOKEN_HS256_SECRET"},
    "SECRETS_KEK": {"t": "shared"}, "BACKUP_KEK": {"t": "shared"},
}

_FIELD = re.compile(r"^\s+([A-Z][A-Z0-9_]+)\s*:\s*(?:str|int|bool|float)", re.MULTILINE)


def _b64d(v: str) -> str:
    return base64.b64decode(v).decode("utf-8", "replace")


def _config_fields(svc: Path) -> set[str]:
    cfg = svc / "app" / "config.py"
    if not cfg.is_file():
        return set()
    return set(_FIELD.findall(cfg.read_text(encoding="utf-8")))


def build_schema() -> None:
    """Regenerate the committed, secret-free schema from each service's config.py (dev-only)."""
    out: dict[str, dict] = {
        # The datastore StatefulSet's own secret — its password is the SAME shared value the DSNs use.
        "platform-secrets": {"keys": {"POSTGRES_USER": {"t": "const"}, "POSTGRES_PASSWORD": {"t": "shared"}}},
    }
    for svc in sorted(p for p in SERVICES.iterdir() if p.is_dir() and p.name != "_template"):
        fields = _config_fields(svc)
        keys = {k: dict(MINTABLE[k]) for k in MINTABLE if k in fields}
        if keys:
            out[f"{svc.name}-secrets"] = {"keys": keys}
    SCHEMA.write_text(json.dumps(out, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    total = sum(len(s["keys"]) for s in out.values())
    print(f"wrote {SCHEMA} — {len(out)} secrets, {total} keys, no sensitive values")


def _gen_token() -> str:
    return secrets.token_hex(32)


def _gen_ed25519() -> str:
    return subprocess.run(["openssl", "genpkey", "-algorithm", "ed25519"],
                          capture_output=True, text=True, check=True).stdout


def _load_existing() -> tuple[dict[str, str], dict[str, str]]:
    """Recover reusable values already in the cluster, so a re-run does NOT mint fresh passwords that
    drift from persisted data. A regenerated POSTGRES_PASSWORD is the worst case: the postgres PVC
    keeps the OLD role password, so every service then fails `password authentication failed` and sits
    0/1. Returns (shared_values, role_passwords); empty on a fresh cluster."""
    r = subprocess.run(["kubectl", "-n", NS, "get", "secret", "-o", "json"],
                       capture_output=True, text=True)
    if r.returncode != 0:
        return {}, {}
    shared: dict[str, str] = {}
    role_pw: dict[str, str] = {}
    for item in json.loads(r.stdout or "{}").get("items", []):
        for key, val in (item.get("data") or {}).items():
            dec = _b64d(val)
            if (key in SHARED or key in ED25519) and key not in shared:
                shared[key] = dec
            elif key.endswith("_DSN") and "://" in dec:
                try:
                    role, tail = dec.split("://", 1)[1].split(":", 1)
                    role_pw.setdefault(role, tail.split("@", 1)[0])
                except (ValueError, IndexError):
                    pass
    return shared, role_pw


def generate_and_apply() -> None:
    schema = json.loads(SCHEMA.read_text(encoding="utf-8"))
    reuse_shared, reuse_role_pw = _load_existing()
    if reuse_shared or reuse_role_pw:
        print(f"↻ preserving {len(reuse_shared)} shared + {len(reuse_role_pw)} role secret(s) already in {NS}")
    pool: dict[str, str] = dict(CONSTANTS)
    for k in SHARED:
        pool[k] = reuse_shared.get(k) or _gen_token()
    for k in ED25519:
        pool[k] = reuse_shared.get(k) or _gen_ed25519()
    role_pw: dict[str, str] = dict(reuse_role_pw)

    def dsn_for(role: str) -> str:
        if role == pool["POSTGRES_USER"]:
            pw = pool["POSTGRES_PASSWORD"]                 # base services: the superuser
        else:
            pw = role_pw.setdefault(role, _gen_token())    # a least-privilege role: per-role, consistent
        return f"postgresql://{role}:{pw}@{PG_HOST}/{PG_DB}"

    for name, spec in schema.items():
        data: dict[str, str] = {}
        for key, meta in spec["keys"].items():
            t = meta["t"]
            if t == "const":
                data[key] = pool[key]
            elif t == "alias":
                data[key] = pool[meta["of"]]
            elif t in ("shared", "ed25519"):
                data[key] = pool[key]
            elif t == "dsn":
                data[key] = dsn_for(meta["role"])
        _apply_secret(name, data)
    print(f"✔ generated + applied {len(schema)} secrets to {NS}")


def _apply_secret(name: str, data: dict[str, str]) -> None:
    args = ["kubectl", "-n", NS, "create", "secret", "generic", name]
    for k, v in data.items():
        args += [f"--from-literal={k}={v}"]
    args += ["--dry-run=client", "-o", "yaml"]
    y = subprocess.run(args, capture_output=True, text=True, check=True).stdout
    subprocess.run(["kubectl", "-n", NS, "apply", "-f", "-"], input=y, text=True, check=True,
                   stdout=subprocess.DEVNULL)
    print(f"  ✔ {name} ({len(data)} keys)")


if __name__ == "__main__":
    if len(sys.argv) >= 2 and sys.argv[1] == "--build":
        build_schema()
    else:
        if not SCHEMA.exists():
            sys.exit(f"no {SCHEMA} — run: python3 {sys.argv[0]} --build")
        generate_and_apply()
