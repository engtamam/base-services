#!/usr/bin/env python
"""Render the edge config from ONE source of truth.

`Security-tools` says why, in its own docstring, better than I would: the domain comes from one
place *"so the cert, the routing host, and the URLs the CLI/UI use can never drift."* A hand-edited
Caddyfile is a fourth source of truth that agrees with the other three right up until it doesn't —
and the day it stops agreeing, the cert is for one name and the proxy answers on another.

So the edge is a pure function:  config -> text.  No Caddy binary to test it, no cluster to render
it, and every value that goes in has been through `validate.py` first (see T1: a Caddyfile is a
configuration language, and an unvalidated hostname is an injected site block).

`caddy` is the only renderer that exists. `nginx` is a second function of the same shape on the day
someone needs it — NOT an inert entry in a registry today, pretending to be a feature.
"""
from __future__ import annotations

import json
import os
import pathlib
import sys

from validate import (
    EdgeValueRejected,
    email as v_email,
    header_value as v_header,
    hostname as v_host,
    path_prefix as v_path,
    upstream as v_upstream,
)

ROOT = pathlib.Path(__file__).resolve().parents[2]

CADDY, NGINX = "caddy", "nginx"
RENDERERS = (CADDY,)          # nginx is a second renderer LATER. It is not here pretending to be one.

# The security headers the edge sets on every response. Config, because a platform's CSP is its own
# business — but note what is NOT here: HSTS. See below.
DEFAULT_HEADERS = {
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    "Referrer-Policy": "strict-origin-when-cross-origin",
    "Permissions-Policy": "geolocation=(), microphone=(), camera=()",
}


def render_caddy(cfg: dict) -> str:
    """config -> Caddyfile. Every interpolated value has been validated first.

    HSTS IS EMITTED ONLY WHEN TLS IS ACTUALLY ON (T8). An HSTS header served over plain http does
    nothing whatsoever except make you believe you are protected — the browser ignores it, and you
    stop looking. `Security-tools` gates it on `acme_enabled` and is right to.
    """
    domain = v_host(cfg["domain"])
    tls = bool(cfg.get("tls", True))
    routes = cfg.get("routes") or []
    if not routes:
        # An edge that routes nothing is not an edge. It is a 404 with a certificate.
        raise EdgeValueRejected("no routes — an edge with nothing behind it is a misconfiguration")

    headers = dict(DEFAULT_HEADERS)
    headers.update(cfg.get("headers") or {})
    if tls:
        headers["Strict-Transport-Security"] = cfg.get(
            "hsts", "max-age=31536000; includeSubDomains")

    lines: list[str] = []

    # ── global block ─────────────────────────────────────────────────────────
    if tls:
        lines += ["{", f"\temail {v_email(cfg['acme_email'])}", "}", ""]
    else:
        # Dev/loopback. No ACME, no public cert dance — and NO HSTS above.
        lines += ["{", "\tauto_https off", "}", ""]

    site = domain if tls else f"http://{domain}:{int(cfg.get('http_port', 80))}"
    lines += [f"{site} {{", "\tencode gzip zstd", ""]

    # ── security headers ─────────────────────────────────────────────────────
    lines.append("\theader {")
    lines.append("\t\t-Server")          # do not advertise what we are, or which version
    for name, value in sorted(headers.items()):
        lines.append(f'\t\t{v_header(name)} "{v_header(value)}"')
    lines.append("\t}")
    lines.append("")

    # ── routes ───────────────────────────────────────────────────────────────
    # Longest prefix first: Caddy matches `handle` blocks in order, and a short prefix declared
    # first swallows every longer one behind it. Sorting here means an operator cannot create that
    # bug by listing their routes in the order they thought of them.
    for r in sorted(routes, key=lambda r: len(r["path"]), reverse=True):
        path = v_path(r["path"])
        up = v_upstream(r["upstream"])
        directive = "handle_path" if r.get("strip_prefix") else "handle"
        lines += [f"\t{directive} {path} {{", f"\t\treverse_proxy {up}", "\t}", ""]

    # ── fallback ─────────────────────────────────────────────────────────────
    fallback = cfg.get("fallback")
    if fallback:
        root = v_path(fallback["root"])
        lines += ["\thandle {", f"\t\troot * {root}",
                  "\t\ttry_files {path} /index.html", "\t\tfile_server", "\t}"]
    else:
        # No SPA behind this edge. Say so explicitly rather than letting Caddy's default answer
        # something we did not choose.
        lines += ["\thandle {", "\t\trespond 404", "\t}"]

    lines.append("}")
    return "\n".join(lines) + "\n"


def render(cfg: dict) -> str:
    driver = cfg.get("driver", CADDY)
    if driver not in RENDERERS:
        raise EdgeValueRejected(
            f"unknown edge driver {driver!r} — built: {', '.join(RENDERERS)}. "
            f"(nginx is a second renderer of the same shape, not a flag that exists today.)")
    return render_caddy(cfg)


def main() -> int:
    cfg_path = pathlib.Path(os.environ.get("EDGE_CONFIG", ROOT / "deploy" / "edge" / "edge.json"))
    out_path = pathlib.Path(os.environ.get("EDGE_OUT", ROOT / "deploy" / "edge" / "Caddyfile"))
    if not cfg_path.is_file():
        print(f"FATAL: no edge config at {cfg_path}")
        return 1
    cfg = json.loads(cfg_path.read_text(encoding="utf-8"))
    try:
        text = render(cfg)
    except EdgeValueRejected as exc:
        print(f"FATAL: {exc}")
        return 1
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(text, encoding="utf-8")
    print(f"edge: rendered {out_path.relative_to(ROOT)} from {cfg_path.relative_to(ROOT)}")
    return 0


if __name__ == "__main__":
    sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
    sys.exit(main())
