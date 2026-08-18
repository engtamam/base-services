"""Every image in the raw k3s platform manifests is pinned by DIGEST, not a moving tag.

The chart guards already refuse an unpinned SERVICE image — but the datastores and the object store
are RAW manifests (deploy/k3s/**), not chart-rendered, so that guard never saw them. That is exactly
how postgres, redis and seaweedfs shipped pinned only to `:16.4-alpine`, `:7.4-alpine`, `:3.73`.

A tag is a NAME the registry can silently repoint; the pod running today may be correct, but the next
pull on a node reboot can deliver a different image with nothing in the manifest changing. A digest is
content. This test fails the build the moment any k3s manifest reintroduces a tag-only image, so the
fix cannot regress unnoticed — the whole point of item 8.
"""
from __future__ import annotations

import pathlib
import re

ROOT = pathlib.Path(__file__).resolve().parents[2]
K3S = ROOT / "deploy" / "k3s"

# `image:` value, ignoring surrounding quotes and trailing comments.
_IMAGE = re.compile(r"^\s*image:\s*['\"]?([^'\"#\s]+)")


def _image_lines() -> list[tuple[str, int, str]]:
    out: list[tuple[str, int, str]] = []
    for path in sorted(K3S.rglob("*.yaml")):
        for n, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            m = _IMAGE.match(line)
            if m:
                out.append((str(path.relative_to(ROOT)), n, m.group(1)))
    return out


def test_there_are_images_to_check():
    # A guard that matches nothing is not a guard — prove the scan actually finds the manifests.
    assert _image_lines(), "no image: lines found under deploy/k3s — the scan path is wrong"


def test_every_k3s_image_is_digest_pinned():
    unpinned = [(f, n, img) for f, n, img in _image_lines() if "@sha256:" not in img]
    assert not unpinned, (
        "these k3s images are pinned to a MOVING tag, not a digest — the next pull can silently "
        "deliver a different image:\n"
        + "\n".join(f"  {f}:{n}  {img}" for f, n, img in unpinned)
        + "\nPin with repo:tag@sha256:<digest> (keep the tag for legibility; the digest is what pulls).")
