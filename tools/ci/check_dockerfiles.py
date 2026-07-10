#!/usr/bin/env python
"""Gate: every publishable service COPYs only files its build context actually provides.

A service's Dockerfile can be correct, its files can all exist, and the build can still fail:

    failed to compute cache key: "/README.md": not found

because the service's `.dockerignore` excludes a file the Dockerfile then `COPY`s. That is what
stopped the images being built: every service had `*.md` in `.dockerignore`, and every
Dockerfile does `COPY pyproject.toml README.md ./` because `pyproject.toml` declares
`readme = "README.md"`, which `pip install .` requires.

This is a STATIC check — it does not run docker, so it works on a machine without it and costs
nothing. It reproduces docker's context filtering (`.dockerignore` semantics, `!` negations,
last-match-wins) and asserts every `COPY <src>` names something the context still contains.

The failure lands in the release job, after every gate is green and after `discover services`.
A gate that runs before the build is the only place to catch it.
"""
from __future__ import annotations

import pathlib
import re
import sys

ROOT = pathlib.Path(__file__).resolve().parents[2]
SERVICES = ROOT / "services"


def dockerignore_excludes(service: pathlib.Path, rel: str) -> bool:
    """True if `rel` (a context-relative path) is excluded by this service's .dockerignore.

    Docker semantics: last matching pattern wins; a `!pattern` re-includes. A `#` is a comment
    ONLY at the start of a line — an inline `# ...` is part of the pattern (a real trap we hit:
    `!README.md   # note` negates a file literally named "README.md   # note").
    """
    di = service / ".dockerignore"
    if not di.is_file():
        return False
    excluded = False
    for raw in di.read_text(encoding="utf-8").splitlines():
        line = raw.rstrip("\n")
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        negate = line.startswith("!")
        pattern = line[1:] if negate else line
        pattern = pattern.strip().rstrip("/")
        if _matches(pattern, rel):
            excluded = not negate
    return excluded


def _matches(pattern: str, rel: str) -> bool:
    """A small subset of docker's matcher: exact, `*.ext`, and `dir/**` / `dir`."""
    base = rel.split("/")[-1]
    if pattern in (rel, base):
        return True
    if pattern.startswith("*.") and base.endswith(pattern[1:]):
        return True
    if pattern.endswith("/**") and rel.startswith(pattern[:-3] + "/"):
        return True
    if "/" not in pattern and pattern == base:
        return True
    return False


# COPY [--flags] src... dest    — collect the sources (everything but the last token)
_COPY = re.compile(r"^\s*COPY\s+(?P<body>.+)$", re.MULTILINE)


def copied_sources(dockerfile: pathlib.Path) -> list[str]:
    out: list[str] = []
    for m in _COPY.finditer(dockerfile.read_text(encoding="utf-8")):
        toks = [t for t in m.group("body").split() if not t.startswith("--")]
        if "--from=" in m.group("body"):
            continue                       # copied from an earlier STAGE, not the build context
        out.extend(toks[:-1])              # last token is the destination
    return out


def main() -> int:
    services = sorted(p for p in SERVICES.iterdir()
                      if p.is_dir() and p.name != "_template" and (p / "Dockerfile").is_file())
    if not services:
        print("  no publishable services with a Dockerfile — nothing to check")
        return 0

    problems: list[str] = []
    for svc in services:
        for src in copied_sources(svc / "Dockerfile"):
            if any(ch in src for ch in "*?["):
                continue                   # a glob; let docker expand it
            path = svc / src
            if not path.exists():
                problems.append(f"{svc.name}: Dockerfile COPYs '{src}', which does not exist")
            elif dockerignore_excludes(svc, src):
                problems.append(
                    f"{svc.name}: Dockerfile COPYs '{src}', but .dockerignore EXCLUDES it — "
                    f"the build fails with '\"/{src}\": not found'")

    for p in problems:
        print(f"  {p}")
    if problems:
        print(f"\ndockerfiles: FAIL — {len(problems)} build(s) would fail after the gates go green")
        return 1
    print(f"dockerfiles: PASS — {len(services)} services COPY only files their context provides")
    return 0


if __name__ == "__main__":
    sys.exit(main())
