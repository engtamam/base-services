#!/usr/bin/env python
"""Gate: the edge config is the one file where a mistake is remotely exploitable.

The edge is the single machine every request on the platform touches, and it terminates TLS. A
mistake in its config is not a bug in one service; it is an open door to all of them. So the render
path gets a gate, and the gate is mutation-tested: a gate that cannot fail is decoration.

Six rules, from phase-32:

  R1  Every value interpolated into the rendered config comes from a VALIDATED field.
  R2  A hostname carrying a Caddyfile metacharacter is REFUSED, not escaped.
  R3  HSTS appears ONLY when TLS is actually on.
  R4  The rendered config PARSES (real `caddy validate` where the binary exists; a structural check
      otherwise — and the skip is REPORTED, because a gate that silently skips is a gate that lies).
  R5  The edge container is non-root, drops ALL capabilities, has a read-only root filesystem.
  R6  A per-pod rate limiter with replicas > 1 is a FAILURE (the limit silently multiplies).
"""
from __future__ import annotations

import json
import os
import pathlib
import shutil
import subprocess
import sys

ROOT = pathlib.Path(os.environ.get("EDGE_GATE_ROOT", "") or pathlib.Path(__file__).resolve().parents[2])
EDGE = ROOT / "deploy" / "edge"
sys.path.insert(0, str(ROOT / "tools" / "edge"))


def _fail(problems: list[str]) -> int:
    print(f"safety:edge — {len(problems)} problem(s):\n")
    for p in problems:
        print(f"  {p}")
    print("\nThe edge terminates TLS for every request. A mistake here is an open door.")
    return 1


def main() -> int:
    from render import render                    # noqa: E402
    from validate import EdgeValueRejected       # noqa: E402

    problems: list[str] = []
    cfg_path = EDGE / "edge.json"
    if not cfg_path.is_file():
        print(f"FATAL: no edge config at {cfg_path} — nothing to check")
        return 1
    cfg = json.loads(cfg_path.read_text(encoding="utf-8"))

    # R1 + R2 — refuse an injected hostname (proves the validator is actually in the render path).
    try:
        render({**cfg, "domain": "evil.com {\n  reverse_proxy https://attacker\n}\nexample.com"})
        problems.append("R2: an injected hostname was RENDERED, not refused — the validator is not "
                        "in the render path. This is remote edge takeover.")
    except EdgeValueRejected:
        pass

    # The real config must render.
    try:
        rendered = render(cfg)
    except EdgeValueRejected as exc:
        return _fail([f"the shipped edge config does not render: {exc}"])

    # R3 — HSTS only with TLS.
    if cfg.get("tls", True):
        if "Strict-Transport-Security" not in rendered:
            problems.append("R3: TLS is on but HSTS is missing from the rendered config")
    else:
        if "Strict-Transport-Security" in rendered:
            problems.append("R3: HSTS is emitted over plain HTTP — it does nothing but false "
                            "assurance; the browser ignores it")

    # R4 — it must PARSE. Prefer the real binary; fall back to a structural check and SAY SO.
    caddy = shutil.which("caddy")
    if caddy:
        out = EDGE / "Caddyfile"
        out.write_text(rendered, encoding="utf-8")
        r = subprocess.run([caddy, "validate", "--config", str(out), "--adapter", "caddyfile"],
                           capture_output=True, text=True)
        if r.returncode != 0:
            problems.append(f"R4: `caddy validate` rejected the rendered config:\n{r.stderr[-400:]}")
    else:
        # Structural: balanced braces and at least one reverse_proxy. Reported as a downgrade, never
        # passed off as the real check.
        print("R4: caddy binary not found — structural check only (install caddy in CI for a real "
              "validate)")
        if rendered.count("{") != rendered.count("}"):
            problems.append("R4: unbalanced braces in the rendered config")
        if "reverse_proxy" not in rendered:
            problems.append("R4: the rendered config proxies nothing")

    # R5 — the edge container is hardened.
    dep = EDGE / "edge.deployment.yaml"
    if not dep.is_file():
        problems.append("R5: no edge deployment manifest — the edge posture is unverifiable")
    else:
        d = dep.read_text(encoding="utf-8")
        for needle, why in (
            ("runAsNonRoot: true", "must run non-root"),
            ("readOnlyRootFilesystem: true", "must have a read-only root filesystem"),
            ("drop: [ALL]", "must drop ALL capabilities"),
            ("seccompProfile", "must set a seccomp profile"),
        ):
            if needle not in d:
                problems.append(f"R5: the edge container {why} — both source repos run Caddy as "
                                f"root, and this is the one container exposed to the internet")

    # R6 — a per-pod rate limiter with more than one replica is a false limit.
    problems += _check_replica_limiter()

    # R7 — the limiter must not read request.state.principal to build its key.
    problems += _check_limiter_key_source()

    if problems:
        return _fail(problems)
    print("safety:edge OK — the edge validates, refuses injection, hardens the container, and its "
          "rate limit does not silently multiply with scale")
    return 0


def _check_limiter_key_source() -> list[str]:
    """R7 / phase-32 T-ordering. The rate-limit key must NOT be built from
    `request.state.principal`.

    That attribute is set by the auth dependency, and FastAPI runs a route's decorator
    dependencies (where `rate_limit` lives) BEFORE its parameter dependencies (where auth lives).
    So a limiter that reads `request.state.principal` sees `None` on any authenticated route, the
    subject silently drops out of the key, and everyone behind one office NAT shares an IP bucket —
    with no symptom until a customer complains. A peer hit exactly this in production.

    The subject must come from the limiter's OWN resolved dependency, so ordering cannot break it.
    This gate makes the old pattern unrepresentable.
    """
    import ast
    out = []
    for svc in sorted((ROOT / "services").glob("*/app/api/ratelimit.py")):
        try:
            tree = ast.parse(svc.read_text(encoding="utf-8"))
        except (OSError, SyntaxError):
            continue
        # An AST walk, so a docstring that MENTIONS request.state.principal (to warn against it) is
        # not mistaken for a code read of it. We look for the attribute chain `.state.principal` and
        # for getattr(<...>.state, "principal", ...) — the two ways to spell the same mistake.
        bad = False
        for n in ast.walk(tree):
            if isinstance(n, ast.Attribute) and n.attr == "principal" \
                    and isinstance(n.value, ast.Attribute) and n.value.attr == "state":
                bad = True
            if isinstance(n, ast.Call) and isinstance(n.func, ast.Name) and n.func.id == "getattr" \
                    and len(n.args) >= 2 and isinstance(n.args[0], ast.Attribute) \
                    and n.args[0].attr == "state" \
                    and isinstance(n.args[1], ast.Constant) and n.args[1].value == "principal":
                bad = True
        if bad:
            rel = svc.relative_to(ROOT).as_posix()
            out.append(f"R7: {rel} reads request.state.principal for its key — that is set by the "
                       f"auth dependency, which FastAPI may run AFTER the limiter. The subject "
                       f"vanishes and one NAT shares a bucket. Resolve the subject inside the "
                       f"limiter (its own optional dependency) instead.")
    return out


def _check_replica_limiter() -> list[str]:
    """R6 / phase-32 T2. A memory (per-pod) limiter counts per pod; with replicas > 1 the real
    budget is replicas x limit, silently. A limit that multiplies with your scale is worse than no
    limit, because you believe in it."""
    out = []
    values = ROOT / "deploy" / "services"
    if not values.is_dir():
        return out
    for vf in sorted(values.glob("*.values.yaml")):
        body = vf.read_text(encoding="utf-8")
        driver = _yaml_scalar(body, "RATELIMITER_DRIVER")
        replicas = _yaml_scalar(body, "replicas")
        if driver == "memory" and replicas and replicas.isdigit() and int(replicas) > 1:
            out.append(f"R6: {vf.name} runs {replicas} replicas with RATELIMITER_DRIVER=memory — "
                       f"the rate limit is per-pod, so the real budget is {replicas}x what you "
                       f"configured. Use the redis driver for a shared counter.")
    return out


def _yaml_scalar(body: str, key: str) -> str:
    import re
    m = re.search(rf'^\s*{re.escape(key)}\s*:\s*"?([^"\n#]+?)"?\s*(?:#.*)?$', body, re.MULTILINE)
    return m.group(1).strip() if m else ""


if __name__ == "__main__":
    sys.exit(main())
