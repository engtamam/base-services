#!/usr/bin/env python3
"""safety:postgres-tls — the server half of DB-in-transit encryption cannot silently regress.

Postgres serving TLS is one `ssl=on` and one mounted cert away from cleartext, and the failure is
INVISIBLE: drop the arg or the mount and postgres starts fine, unencrypted, green rollout. The one
service that carries real PII (operations) then streams phone numbers over the pod network in the
clear, and no control anywhere notices. This gate asserts the three things that keep the wire
encrypted, so a future edit to datastores.yaml or bootstrap.sh trips CI instead of production:

  1. the postgres container is told ssl=on and pointed at a cert + key,
  2. the cert Secret (postgres-tls) is actually mounted where those paths resolve,
  3. bootstrap mints that Secret on the host before the manifests apply.

It also guards the ORDERING landmine: gen-secrets.py must NOT append sslmode=require to a DSN while
this is only the server half — a DSN that demands TLS before the wider rollout is verified fails
every service at boot. When the client half legitimately lands, flip ALLOW_DSN_SSLMODE below in the
same change that verifies the server serves TLS (deploy/server/verify-postgres-tls.sh)."""
from __future__ import annotations

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
DATASTORES = ROOT / "deploy" / "k3s" / "platform" / "datastores.yaml"
BOOTSTRAP = ROOT / "deploy" / "k3s" / "bootstrap.sh"
GEN_SECRETS = ROOT / "deploy" / "server" / "gen-secrets.py"

# Flip to True ONLY in the change that lands the client half AND has verify-postgres-tls.sh passing.
ALLOW_DSN_SSLMODE = False

MOUNT = "/etc/postgres-tls"


def fail(msg: str) -> None:
    print(f"FAIL: {msg}")
    sys.exit(1)


def main() -> None:
    for p in (DATASTORES, BOOTSTRAP, GEN_SECRETS):
        if not p.is_file():
            fail(f"expected file missing: {p.relative_to(ROOT).as_posix()}")

    ds = DATASTORES.read_text(encoding="utf-8")
    # 1. the server is actually told to serve TLS, pointed at the mounted cert + key.
    if not re.search(r"ssl\s*=\s*on", ds):
        fail("datastores.yaml: postgres is not started with ssl=on — the DB serves cleartext")
    for want in (f"ssl_cert_file={MOUNT}/tls.crt", f"ssl_key_file={MOUNT}/tls.key"):
        if want not in ds:
            fail(f"datastores.yaml: missing postgres arg '{want}'")
    # 2. the cert Secret is mounted where those paths resolve.
    if "secretName: postgres-tls" not in ds:
        fail("datastores.yaml: the postgres-tls Secret is not mounted — ssl=on would fail to start")
    if f"mountPath: {MOUNT}" not in ds:
        fail(f"datastores.yaml: postgres-tls is not mounted at {MOUNT}")

    # 3. bootstrap mints the Secret on the host, before applying the platform manifests.
    bs = BOOTSTRAP.read_text(encoding="utf-8")
    if "ensure_postgres_tls" not in bs or "create secret tls postgres-tls" not in bs:
        fail("bootstrap.sh: no host-side step mints the postgres-tls Secret (ensure_postgres_tls)")
    if bs.find("ensure_postgres_tls\n") < 0 and re.search(r"\bensure_postgres_tls\b", bs) is None:
        fail("bootstrap.sh: ensure_postgres_tls is defined but never called")

    # 4. the ordering landmine: no sslmode=require in a minted DSN until the client half lands.
    if not ALLOW_DSN_SSLMODE:
        gs = GEN_SECRETS.read_text(encoding="utf-8")
        if re.search(r"sslmode\s*=\s*require", gs):
            fail("gen-secrets.py mints a DSN with sslmode=require while this is the SERVER half only. "
                 "A DSN that demands TLS before the rollout is verified fails every service at boot. "
                 "Land the client half with verify-postgres-tls.sh passing, then set "
                 "ALLOW_DSN_SSLMODE=True in check_postgres_tls.py in the SAME change.")

    print("OK: postgres serves TLS (server half wired; DSNs still plain until the client half lands)")


if __name__ == "__main__":
    main()
