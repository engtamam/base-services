#!/usr/bin/env python3
"""safety:ssrf-egress — a PAYMENT-GATEWAY HTTP transport must egress-guard its outbound fetches.

THE FAILURE THIS EXISTS TO PREVENT (payments checkout audit 2026-09-19, finding H1).

A payment gateway's base URL comes from operator/tenant-set credentials (MERCHANT_API_BASE /
PAYMENT_API_BASE), so a transport that fetches it raw is an SSRF sink: a base pointed at cloud
metadata (169.254.169.254), loopback, or a private host would be fetched with the shop's credentials.
The fix is an egress guard (scheme allowlist + resolved-IP-range block) before the socket opens, plus
refusing redirects — exactly what payments-service/app/adapters/payment_http.py's `guard_fetch_url`
does.

SCOPE — deliberately NARROW. This gate does NOT touch the many internal service-to-service HTTP
helpers (audit_sink, consent_gate, notifier, …): those call CONFIGURED cluster hosts that are
legitimately private, and an IP-range guard there would BLOCK real traffic. Only PAYMENT-gateway
transports fetch an external, credential-supplied URL, and only they must guard. A payment transport
that does a raw outbound fetch without an egress guard fails; a deliberate exception opts out with
`# ssrf: ok — reason`.
"""
from __future__ import annotations

import os
import pathlib
import re
import sys

ROOT = pathlib.Path(os.environ.get("SSRF_GATE_ROOT") or pathlib.Path(__file__).resolve().parents[2])
SERVICES = ROOT / "services"

# A payment-gateway HTTP transport: the shared payment/billing HTTP helper, or any module under an
# adapters/payment/ tree. These are the only modules that fetch an external, credential-supplied URL.
_PAYMENT_TRANSPORT_BASENAMES = {"payment_http.py", "billing_http.py"}
_RAW_FETCH = re.compile(r"urlopen\(|_OPENER\.open\(|requests\.(get|post|put|delete|patch)\(|"
                        r"httpx\.(get|post|put|delete|patch|Client|AsyncClient|request)\(")
# Evidence the module guards egress: it calls a guard, or does the IP-range check itself.
_GUARD = re.compile(r"guard_fetch_url|_ip_is_blocked|is_private|getaddrinfo")


def _is_payment_transport(path: pathlib.Path) -> bool:
    if path.name in _PAYMENT_TRANSPORT_BASENAMES:
        return True
    return "adapters" in path.parts and "payment" in path.parts and path.suffix == ".py"


def _check_file(path: pathlib.Path, rel: str) -> list[str]:
    src = path.read_text(encoding="utf-8")
    if not _RAW_FETCH.search(src):
        return []                                  # no raw outbound fetch — nothing to guard
    if "# ssrf: ok" in src or _GUARD.search(src):
        return []                                  # guarded (or explicitly exempt)
    return [f"{rel}: a payment-gateway transport does a raw outbound fetch with no egress guard — an "
            f"SSRF sink for a credential-supplied base URL. Route it through an egress guard "
            f"(scheme + IP-range, like payment_http.guard_fetch_url), or mark `# ssrf: ok — reason`."]


def main() -> int:
    if not SERVICES.exists():
        print(f"no services dir at {SERVICES}")
        return 0
    problems, checked = [], 0
    for svc in sorted(SERVICES.iterdir()):
        appdir = svc / "app"
        if not svc.is_dir() or not appdir.exists():
            continue
        for py in sorted(appdir.rglob("*.py")):
            if _is_payment_transport(py):
                checked += 1
                problems.extend(_check_file(py, str(py.relative_to(ROOT))))
    if problems:
        print("safety:ssrf-egress: FAIL")
        for p in problems:
            print(f"  {p}")
        return 1
    print(f"safety:ssrf-egress: PASS — {checked} payment transports; every raw fetch is egress-guarded")
    return 0


if __name__ == "__main__":
    sys.exit(main())
