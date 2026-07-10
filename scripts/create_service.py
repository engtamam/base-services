#!/usr/bin/env python3
# =============================================================================
# base-services — smart service generator
# -----------------------------------------------------------------------------
# Clones services/_template into a new 100% self-contained service, picks the
# ports/adapters you want, then SELF-VERIFIES (pytest + security gate) and only
# commits on green. Atomic: builds in a temp dir, moves into place on success,
# rolls back on any failure. Stdlib only.
#
#   python scripts/create_service.py my-service --desc "does things"
#   python scripts/create_service.py my-service --cache redis --dry-run
#   python scripts/create_service.py my-service --spec my.spec.toml
# =============================================================================
from __future__ import annotations

import argparse
import json
import re
import secrets
import shutil
import subprocess
import sys
import tempfile
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
TEMPLATE = ROOT / "services" / "_template"
SERVICES = ROOT / "services"
REGISTRY = SERVICES / ".registry.json"

NAME_RE = re.compile(r"^[a-z][a-z0-9-]{0,47}[a-z0-9]$")

# flag dest -> (env var, allowed drivers)
DRIVERS: dict[str, tuple[str, set[str]]] = {
    "datastore": ("DATASTORE_DRIVER", {"memory", "postgres"}),
    "cache": ("CACHE_DRIVER", {"memory", "redis"}),
    "event_bus": ("EVENTBUS_DRIVER", {"memory", "redis"}),
    "queue": ("QUEUE_DRIVER", {"memory", "redis"}),
    "object_store": ("OBJECTSTORE_DRIVER", {"local", "memory", "s3"}),
    "secrets": ("SECRETS_DRIVER", {"env", "file", "vault"}),
    "token_verifier": ("TOKEN_VERIFIER_DRIVER", {"null", "hs256", "rs256", "jwks"}),
    "rate_limiter": ("RATELIMITER_DRIVER", {"memory", "redis"}),
}

# Drivers safe to ACTIVATE with no external system / no required keys. Others are
# recorded in the spec + .env.example but the runtime .env keeps a zero-infra
# default, so the generated service still boots and verifies with zero infra.
SAFE_ACTIVATABLE: dict[str, set[str]] = {
    "DATASTORE_DRIVER": {"memory"},
    "CACHE_DRIVER": {"memory"},
    "EVENTBUS_DRIVER": {"memory"},
    "QUEUE_DRIVER": {"memory"},
    "OBJECTSTORE_DRIVER": {"local", "memory"},
    "SECRETS_DRIVER": {"env"},
    "TOKEN_VERIFIER_DRIVER": {"null"},
    "RATELIMITER_DRIVER": {"memory"},
}

COPY_EXCLUDE_DIRS = {
    ".venv", ".git", "__pycache__", ".pytest_cache", ".objectstore",
    ".ruff_cache", ".mypy_cache",
}
COPY_EXCLUDE_FILES = {"sbom.cdx.json", ".env"}
# Skipped by the placeholder engine (huge/tokenless/binary).
NO_REPLACE_RELPATHS = {"app/security/cwe.json"}


class GenError(Exception):
    pass


# ── Pure helpers (unit-tested) ──────────────────────────────────────────────
def validate_name(name: str) -> str:
    if "/" in name or "\\" in name or ".." in name or name != name.strip():
        raise GenError(f"invalid service name (path traversal?): {name!r}")
    if not NAME_RE.match(name):
        raise GenError(
            f"invalid service name: {name!r} — must be kebab-case "
            "(lowercase, digits, hyphens), 2-49 chars"
        )
    return name


def validate_driver(dest: str, value: str) -> str:
    env_var, allowed = DRIVERS[dest]
    if value not in allowed:
        raise GenError(
            f"unknown {dest} driver: {value!r} — allowed: {', '.join(sorted(allowed))}"
        )
    return value


def replace_placeholders(text: str, name: str) -> str:
    return text.replace("{service-name}", name).replace("service-template", name)


def should_replace(relpath: str) -> bool:
    return relpath.replace("\\", "/") not in NO_REPLACE_RELPATHS


def load_spec(path: Path) -> dict:
    with open(path, "rb") as fh:
        data = tomllib.load(fh)
    return data


def load_registry() -> dict[str, int]:
    if REGISTRY.exists():
        try:
            return json.loads(REGISTRY.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            return {}
    return {}


def assign_port(registry: dict[str, int], name: str, requested: int | None, base: int = 8001) -> int:
    if requested is not None:
        return requested
    used = set(registry.values())
    port = base
    while port in used:
        port += 1
    return port


def resolve_config(args, spec: dict) -> dict:
    """Merge spec + CLI flags (flags win). Returns {name, description, port, drivers}."""
    drivers: dict[str, str] = {}
    spec_drivers = spec.get("drivers", {}) if spec else {}
    for dest, (env_var, _allowed) in DRIVERS.items():
        # spec uses dest keys (e.g. datastore = "postgres")
        val = getattr(args, dest, None) or spec_drivers.get(dest)
        if val:
            drivers[env_var] = validate_driver(dest, val)
    return {
        "name": args.name or spec.get("name"),
        "description": args.desc or spec.get("description", ""),
        "port": args.port if args.port is not None else spec.get("port"),
        "drivers": drivers,
    }


def build_env_file(name: str, port: int, drivers: dict[str, str]) -> tuple[str, list[str]]:
    """Return (.env text, list of drivers recorded-but-not-activated)."""
    token = secrets.token_urlsafe(48)
    lines = [
        "# Generated by scripts/create_service.py — local runtime config.",
        "# NEVER commit real secrets. Regenerate the token for production.",
        "ENV=development",
        f"SERVICE_NAME={name}",
        f"PORT={port}",
        f"INTERNAL_SERVICE_TOKEN={token}",
    ]
    deferred: list[str] = []
    for env_var, value in drivers.items():
        if value in SAFE_ACTIVATABLE.get(env_var, set()):
            lines.append(f"{env_var}={value}")
        else:
            deferred.append(f"{env_var}={value}")
    if deferred:
        lines.append("")
        lines.append("# Chosen but NOT auto-activated (need connection/keys — set before use):")
        for d in deferred:
            lines.append(f"# {d}")
    return "\n".join(lines) + "\n", deferred


def build_spec_file(name: str, description: str, port: int, drivers: dict[str, str]) -> str:
    lines = [
        "# service.spec.toml — the record of how this service was generated.",
        f'name = "{name}"',
        f'description = "{description}"',
        f"port = {port}",
        "",
        "[drivers]",
    ]
    # map env var back to dest for readability
    env_to_dest = {v[0]: k for k, v in DRIVERS.items()}
    for env_var, value in drivers.items():
        lines.append(f'{env_to_dest[env_var]} = "{value}"')
    return "\n".join(lines) + "\n"


# ── Copy + build ────────────────────────────────────────────────────────────
def copy_template(dst: Path, name: str) -> int:
    count = 0
    for src in TEMPLATE.rglob("*"):
        rel = src.relative_to(TEMPLATE)
        parts = set(rel.parts)
        if parts & COPY_EXCLUDE_DIRS:
            continue
        if src.is_dir():
            continue
        if src.name in COPY_EXCLUDE_FILES:
            continue
        target = dst / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        relstr = str(rel).replace("\\", "/")
        raw = src.read_bytes()
        if should_replace(relstr):
            try:
                text = raw.decode("utf-8")
                target.write_text(replace_placeholders(text, name), encoding="utf-8", newline="")
            except UnicodeDecodeError:
                target.write_bytes(raw)
        else:
            target.write_bytes(raw)
        count += 1
    return count


def verify(service_dir: Path, python: str) -> tuple[bool, str]:
    py = python or _template_python()
    if py is None:
        return False, "no python for verification (pass --python or create template .venv)"
    # 1) tests
    t = subprocess.run([py, "-m", "pytest", "-q"], cwd=service_dir, capture_output=True, text=True)
    if t.returncode != 0:
        return False, "pytest failed:\n" + t.stdout[-1500:]
    # 2) security gate
    g = subprocess.run([py, "ci/security_gate.py"], cwd=service_dir, capture_output=True, text=True)
    if g.returncode != 0:
        return False, "security gate failed:\n" + g.stdout[-1500:]
    return True, "pytest + security gate green"


def _template_python() -> str | None:
    for candidate in (TEMPLATE / ".venv" / "Scripts" / "python.exe",
                      TEMPLATE / ".venv" / "bin" / "python"):
        if candidate.exists():
            return str(candidate)
    return None


# ── Orchestration ───────────────────────────────────────────────────────────
def generate(args) -> int:
    spec = load_spec(Path(args.spec)) if args.spec else {}
    cfg = resolve_config(args, spec)
    if not cfg["name"]:
        raise GenError("a service name is required (positional or in --spec)")
    name = validate_name(cfg["name"])
    target = SERVICES / name

    registry = load_registry()
    port = assign_port(registry, name, cfg["port"])
    drivers = cfg["drivers"]

    if target.exists() and not args.force:
        raise GenError(f"service already exists: {target} (use --force to overwrite)")

    if args.dry_run:
        print("── DRY RUN — plan only, nothing written ──")
        print(f"  name        : {name}")
        print(f"  path        : services/{name}")
        print(f"  port        : {port}")
        print(f"  description : {cfg['description'] or '(none)'}")
        print(f"  drivers     : {drivers or '(all template defaults)'}")
        return 0

    tmp = Path(tempfile.mkdtemp(prefix=f"gen-{name}-"))
    try:
        build_dir = tmp / name
        count = copy_template(build_dir, name)
        env_text, deferred = build_env_file(name, port, drivers)
        (build_dir / ".env").write_text(env_text, encoding="utf-8")
        (build_dir / "service.spec.toml").write_text(
            build_spec_file(name, cfg["description"], port, drivers), encoding="utf-8"
        )

        verified = "skipped (--no-verify)"
        if not args.no_verify:
            ok, detail = verify(build_dir, args.python)
            if not ok:
                raise GenError(f"verification FAILED — rolling back.\n{detail}")
            verified = detail

        if target.exists():
            shutil.rmtree(target)
        shutil.move(str(build_dir), str(target))
        registry[name] = port
        REGISTRY.write_text(json.dumps(registry, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    # A service that cannot be deployed is not finished. Emit its chart values now, so the
    # deploy guards (tests/deploy) cover it from its first commit rather than from whenever
    # someone remembers to hand-write YAML.
    deploy_values = "(skipped)"
    try:
        import gen_deploy_values  # noqa: PLC0415  (same directory as this script)

        gen_deploy_values.main()
        deploy_values = f"deploy/services/{name}.values.yaml"
    except Exception as exc:  # never fail service creation over this
        deploy_values = f"(failed: {exc})"

    print("=" * 60)
    print(f"  Service created: services/{name}")
    print("=" * 60)
    print(f"  port        : {port}")
    print(f"  drivers     : {drivers or '(template defaults)'}")
    if deferred:
        print(f"  needs config: {', '.join(deferred)} (recorded, set before use)")
    print(f"  files       : {count}")
    print(f"  verified    : {verified}")
    print(f"  deploy      : {deploy_values}")
    print("  next steps  :")
    print(f"    cd services/{name}")
    print("    # (dev) reuse deps or: py -3.12 -m venv .venv && .venv/Scripts/python -m pip install -e \".[dev,ci]\"")
    print("    uvicorn app.main:app")
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="Generate a self-contained base-services service.")
    p.add_argument("name", nargs="?", help="service name (kebab-case)")
    p.add_argument("--desc", default=None, help="service description")
    p.add_argument("--port", type=int, default=None, help="port (auto-assigned if omitted)")
    p.add_argument("--spec", default=None, help="service.spec.toml to read (flags override)")
    p.add_argument("--python", default=None, help="python used for verification (default: template .venv)")
    p.add_argument("--dry-run", action="store_true", help="print the plan, write nothing")
    p.add_argument("--force", action="store_true", help="overwrite an existing service")
    p.add_argument("--no-verify", action="store_true", help="skip pytest + security gate")
    for dest in DRIVERS:
        flag = "--" + dest.replace("_", "-")
        p.add_argument(flag, dest=dest, default=None, help=f"{dest} driver")
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return generate(args)
    except GenError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
