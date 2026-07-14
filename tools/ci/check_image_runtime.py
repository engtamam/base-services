#!/usr/bin/env python
"""Gate: a published image can actually RUN the config its deploy values ship with.

Two ways a green build produces an image that crashes the moment it is deployed — both reported
from a real deploy, both confirmed by running the published v0.1.4 image:

  A. DRIVER WITHOUT ITS EXTRA. Every service's values default `datastore: postgres` and
     `cache: redis`, but the Dockerfile runs `pip install .` — runtime only. `asyncpg` and
     `redis` are OPTIONAL extras, so they are not in the image. The container selects the
     postgres driver, the lazy import fails, and the pod boots straight into
     `ConfigError: driver needs the 'postgres' extra`. A fresh deploy never serves a request.

  B. LOCAL WRITE ONTO A READ-ONLY ROOT. The chart sets `readOnlyRootFilesystem: true` and mounts
     a writable volume only at /tmp. `OBJECTSTORE_DRIVER=local` writes to `OBJECTSTORE_ROOT`
     (default `./.objectstore` = /app/.objectstore), which is read-only. Crash on first write.

Both are the shape this repo keeps hunting: the build is green and the artifact is broken,
because nothing checked that the IMAGE and the VALUES agree. This gate checks exactly that,
statically — no docker, no cluster.
"""
from __future__ import annotations

import pathlib
import re
import sys

ROOT = pathlib.Path(__file__).resolve().parents[2]
SERVICES = ROOT / "services"
VALUES = ROOT / "deploy" / "services"

# A driver -> the pyproject extra its adapter needs. Drivers with no extra (in-process) map to "".
DRIVER_EXTRA = {
    "postgres": "postgres", "redis": "redis", "s3": "s3", "vault": "vault",
    "memory": "", "local": "", "null": "", "env": "", "builtin": "", "system": "",
    "manual": "", "none": "", "console": "", "stdout": "",
}

# The driver keys a values `drivers:` block may set.
DRIVER_KEYS = ("datastore", "cache", "eventbus", "queue", "ratelimiter",
               "objectstore", "secrets", "tokenverifier")

# The writable mount the chart provides under readOnlyRootFilesystem. Anything a local driver
# writes must live under one of these.
WRITABLE_MOUNTS = ("/tmp",)


def _yaml_block(text: str, key: str) -> dict[str, str]:
    """The immediate `key:\\n  a: b` mapping — enough for the drivers block, no yaml dep."""
    out: dict[str, str] = {}
    lines = text.splitlines()
    for i, ln in enumerate(lines):
        if ln.rstrip() == f"{key}:":
            for sub in lines[i + 1:]:
                m = re.match(r"^(\s+)([A-Za-z0-9_]+):\s*(\S.*?)\s*$", sub)
                if not m or len(m.group(1)) == 0:
                    if sub.strip() and not sub.startswith(" "):
                        break
                    if m is None and sub.strip():
                        break
                    continue
                out[m.group(2)] = m.group(3).strip().strip('"').strip("'")
            break
    return out


def _config_default(service: str, field: str) -> str | None:
    cfg = SERVICES / service / "app" / "config.py"
    if not cfg.is_file():
        return None
    m = re.search(rf'^\s*{field}\s*:\s*str\s*=\s*"([^"]*)"', cfg.read_text(encoding="utf-8"),
                  re.MULTILINE)
    return m.group(1) if m else None


def installed_extras(service: str) -> set[str] | None:
    """The extras the Dockerfile's `pip install` brings in. None if there is no Dockerfile."""
    df = SERVICES / service / "Dockerfile"
    if not df.is_file():
        return None
    extras: set[str] = set()
    for m in re.finditer(r"pip install\s+[^\n]*", df.read_text(encoding="utf-8")):
        line = m.group(0)
        # `pip install ".[postgres,redis]"` or `.[postgres]` — capture the bracket group
        for grp in re.findall(r"\[([a-z0-9,\s]+)\]", line):
            extras.update(e.strip() for e in grp.split(",") if e.strip())
    return extras


def selected_drivers(service: str) -> dict[str, str]:
    vf = VALUES / f"{service}.values.yaml"
    drivers = _yaml_block(vf.read_text(encoding="utf-8"), "drivers") if vf.is_file() else {}
    # objectstore/secrets are often not in the drivers block — fall back to the config default.
    for field, env in (("objectstore", "OBJECTSTORE_DRIVER"), ("secrets", "SECRETS_DRIVER")):
        if field not in drivers:
            d = _config_default(service, env)
            if d:
                drivers[field] = d
    return drivers


def objectstore_root(service: str) -> str:
    vf = VALUES / f"{service}.values.yaml"
    if vf.is_file():
        m = re.search(r"OBJECTSTORE_ROOT:\s*(\S+)", vf.read_text(encoding="utf-8"))
        if m:
            return m.group(1).strip().strip('"').strip("'")
    return _config_default(service, "OBJECTSTORE_ROOT") or "./.objectstore"


def required_extras(service: str) -> dict[str, str]:
    """Every extra this service's adapters can demand at runtime -> where it was found.

    Read from the code, not from a list a human maintains: `lazy.load(module, "extra")` is the ONE
    place an adapter says "I need something the image may not have". If it is reachable from the
    registry, config can select it, and then it must be installable.
    """
    found: dict[str, str] = {}
    adapters = SERVICES / service / "app" / "adapters"
    if not adapters.is_dir():
        return found
    for py in sorted(adapters.rglob("*.py")):
        if py.name == "lazy.py":                 # the definition of load(), not a use of it
            continue
        for m in re.finditer(r"""load\(\s*["'][^"']+["']\s*,\s*["']([a-z0-9_-]+)["']""",
                             py.read_text(encoding="utf-8")):
            found.setdefault(m.group(1), py.relative_to(SERVICES / service).as_posix())
    return found


def excluded_extras(service: str) -> set[str]:
    """Extras a service DELIBERATELY does not ship in its image, declared in pyproject:

        # image-excludes: clamav, ld    (heavy native deps; opt-in build)

    The escape hatch exists because "ship everything" is not always right — a scanner daemon or a
    vendor SDK can be large or drag native code into an image most deployments never use. But it
    must be a sentence somebody wrote, in the repo, next to the dependency. That is the whole
    difference between a lean image and a broken one.
    """
    pp = SERVICES / service / "pyproject.toml"
    if not pp.is_file():
        return set()
    out: set[str] = set()
    for m in re.finditer(r"^#\s*image-excludes:\s*(.+)$", pp.read_text(encoding="utf-8"),
                         re.MULTILINE):
        out.update(e.strip() for e in m.group(1).split(",") if e.strip())
    return out


def check(service: str) -> list[str]:
    problems: list[str] = []
    installed = installed_extras(service)
    if installed is None:
        return []                                    # no image to ship; not this gate's concern
    drivers = selected_drivers(service)

    # A: every selected driver's extra must be in the image.
    for key, driver in drivers.items():
        if key not in DRIVER_KEYS:
            continue
        extra = DRIVER_EXTRA.get(driver)
        if extra is None:
            problems.append(f"{service}: values select {key}={driver!r}, an unknown driver")
            continue
        if extra and extra not in installed:
            problems.append(
                f"{service}: values select {key}={driver!r} but the image installs no "
                f"'{extra}' extra — the pod boots into ConfigError. "
                f"pip install \".[{extra}]\" in the Dockerfile, or default {key} to memory.")

    # C: every extra the image COULD need must be in the image — not merely the ones today's
    #    values happen to select.
    #
    #    Check A asks "does the image support the config we ship?". That is the wrong question, and
    #    it is why identity shipped a caps-resolver driver whose dependency was not in the image:
    #    IDENTITY_CAPS_SOURCE is a plain env knob, not a key in the values `drivers:` block, so A
    #    never looked at it. The check passed because it was reading an incomplete list — the same
    #    failure this repository keeps producing.
    #
    #    The right question is "does the image support the config a CUSTOMER can set?". They run
    #    OUR published image. If they flip a documented knob to a driver in our own registry and the
    #    pod crash-loops on a missing extra, they cannot fix it — they would have to fork the
    #    Dockerfile. A driver that is selectable but not installable is a lie in the config surface.
    #
    #    So the list is derived from the CODE: every `load(module, extra)` an adapter can reach.
    #    Nobody has to remember to update it.
    excluded = excluded_extras(service)
    for extra, where in required_extras(service).items():
        if extra in excluded:
            # Deliberately not shipped, and SAID SO in one place. The driver still fails closed at
            # boot with a clear ConfigError naming the extra — the point of this gate is that the
            # omission is a decision somebody wrote down, never an accident nobody noticed.
            continue
        if extra not in installed:
            problems.append(
                f"{service}: an adapter can require the '{extra}' extra ({where}) but the image "
                f"never installs it — the driver is SELECTABLE and not INSTALLABLE, so a customer "
                f"who sets it crash-loops on a config knob they were invited to set, and cannot "
                f"fix it without forking the image. Add it to the Dockerfile's pip install, or "
                f"delete the adapter.")

    # B: a local objectstore must write under a writable mount, given readOnlyRootFilesystem.
    if drivers.get("objectstore") == "local":
        root = objectstore_root(service)
        abs_root = root if root.startswith("/") else "/app/" + root.lstrip("./")
        if not any(abs_root.startswith(w) for w in WRITABLE_MOUNTS):
            problems.append(
                f"{service}: OBJECTSTORE_DRIVER=local writes to {abs_root} on a read-only root "
                f"(the chart mounts only {WRITABLE_MOUNTS[0]} writable) — crash on first write. "
                f"Use s3, or point OBJECTSTORE_ROOT under {WRITABLE_MOUNTS[0]}, or mount a volume.")
    return problems


def main() -> int:
    services = sorted(p.name for p in SERVICES.iterdir()
                      if p.is_dir() and p.name != "_template"
                      and (p / "Dockerfile").is_file() and (VALUES / f"{p.name}.values.yaml").is_file())
    if not services:
        print("  no publishable services with a values file — nothing to check")
        return 0

    problems = [p for s in services for p in check(s)]
    for p in problems:
        print(f"  {p}")
    if problems:
        print(f"\nimage-runtime: FAIL — {len(problems)} image(s) cannot run the config they ship")
        return 1
    print(f"image-runtime: PASS — {len(services)} services: every driver a customer can SELECT is "
          f"one the image can actually RUN, and no local store writes onto a read-only root")
    return 0


if __name__ == "__main__":
    sys.exit(main())
