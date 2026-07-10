#!/usr/bin/env python
"""Pin published image digests into deploy/services/*.values.yaml.

The chart refuses to render an image that is not digest-pinned: a `:latest` tag with
`imagePullPolicy: IfNotPresent` silently redeploys whatever is already on the node. So a
release is not "some images were pushed" — it is "these exact digests, and the chart renders
with them".

Input is a JSON map produced by the release workflow:

    {"wallet-service": "sha256:abc…", "billing-service": "sha256:def…"}

Usage:
    python tools/release/pin_digests.py --digests digests.json \
        --registry ghcr.io/OWNER --out deploy/pinned
    python tools/release/pin_digests.py --verify        # every values file is pinned

`--out` writes copies rather than mutating the working tree: a release artifact is a thing you
can hand to a cluster, not a commit you have to make from CI.
"""
from __future__ import annotations

import argparse
import json
import pathlib
import re
import sys

ROOT = pathlib.Path(__file__).resolve().parents[2]
VALUES_DIR = ROOT / "deploy" / "services"

PLACEHOLDER = "sha256:" + "0" * 64
DIGEST_RE = re.compile(r"^sha256:[a-f0-9]{64}$")


def values_files() -> list[pathlib.Path]:
    return sorted(VALUES_DIR.glob("*.values.yaml"))


def service_of(path: pathlib.Path) -> str:
    return path.name.replace(".values.yaml", "")


def _replace_line(text: str, key: str, value: str) -> str:
    """Rewrite `  key: ...` under the image block, preserving its trailing comment."""
    pattern = re.compile(rf"^(\s*{key}:\s*)(\"[^\"]*\"|\S+)(.*)$", re.MULTILINE)
    if not pattern.search(text):
        raise SystemExit(f"FAIL: no `{key}:` line to pin")
    return pattern.sub(lambda m: f'{m.group(1)}"{value}"{m.group(3)}', text, count=1)


def pin(digests: dict[str, str], registry: str | None, out_dir: pathlib.Path) -> int:
    # Validate EVERYTHING before writing ANYTHING. A half-pinned release directory is worse
    # than a failed one: it looks deployable. Same rule as a settlement that cannot be paid —
    # refuse, and mutate nothing.
    missing = [service_of(p) for p in values_files() if service_of(p) not in digests]
    if missing:
        print("FAIL: no published digest for: " + ", ".join(sorted(missing)))
        print("  Every service must be built and pushed before a release is pinned.")
        return 1

    unknown = set(digests) - {service_of(p) for p in values_files()}
    if unknown:
        print(f"FAIL: digests for services with no values file: {sorted(unknown)}")
        return 1

    malformed = [f"{s}: {d!r}" for s, d in sorted(digests.items())
                 if not DIGEST_RE.match(str(d).strip())]
    if malformed:
        print("FAIL: these are not sha256 digests (a tag is not a digest):")
        for m in malformed:
            print(f"    - {m}")
        return 1

    out_dir.mkdir(parents=True, exist_ok=True)
    for path in values_files():
        service = service_of(path)
        digest = digests[service].strip()
        text = path.read_text(encoding="utf-8")
        text = _replace_line(text, "digest", digest)
        if registry:
            text = _replace_line(text, "repository", f"{registry.rstrip('/')}/{service}")
        (out_dir / path.name).write_text(text, encoding="utf-8")
        print(f"  pinned {service:24} {digest[:19]}…")

    print(f"\nwrote {len(values_files())} pinned values files to {out_dir.relative_to(ROOT)}")
    return 0


def verify(directory: pathlib.Path) -> int:
    """Every values file names a real digest — never the placeholder, never a tag."""
    bad: list[str] = []
    for path in sorted(directory.glob("*.values.yaml")):
        text = path.read_text(encoding="utf-8")
        m = re.search(r'^\s*digest:\s*"?([^"\s]+)"?', text, re.MULTILINE)
        digest = m.group(1) if m else ""
        if not DIGEST_RE.match(digest):
            bad.append(f"{path.name}: {digest!r} is not a digest")
        elif digest == PLACEHOLDER:
            bad.append(f"{path.name}: still the build placeholder")
    if bad:
        print("FAIL: unpinned images would be deployed:")
        for b in bad:
            print(f"    - {b}")
        return 1
    print(f"  all {len(list(directory.glob('*.values.yaml')))} values files are digest-pinned")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--digests", help="JSON file: {service: sha256:...}")
    ap.add_argument("--registry", help="e.g. ghcr.io/OWNER — rewrites image.repository")
    ap.add_argument("--out", default="deploy/pinned", help="where to write pinned values")
    ap.add_argument("--verify", metavar="DIR", nargs="?", const="deploy/pinned",
                    help="check that a directory's values files are digest-pinned")
    args = ap.parse_args()

    if args.verify:
        return verify(ROOT / args.verify)
    if not args.digests:
        ap.error("--digests is required unless --verify")

    digests = json.loads(pathlib.Path(args.digests).read_text(encoding="utf-8"))
    return pin(digests, args.registry, ROOT / args.out)


if __name__ == "__main__":
    sys.exit(main())
