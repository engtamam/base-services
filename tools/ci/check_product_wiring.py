#!/usr/bin/env python
"""Gate: product identity actually reaches a service, and only through knobs that exist.

`config/product.json` is where the product's name, brand and frozen identifiers live. A config
file that nothing reads is not configuration — it is a document that validates itself. This gate
checks the wiring in `scripts/product.py` in both directions:

  1. **Every mapped env var is a real Settings field.** A mapping to `JWT_ISSUER` when the field
     is called `TOKEN_ISSUER` deploys perfectly clean: Kubernetes sets an env var nobody reads,
     the service uses its default, and the product ships under the wrong issuer. (When this map
     was first drafted it named `JWT_ISSUER`, `USER_AGENT` and `EVENT_NS`. None of the three
     exist. This gate is what found that.)

  2. **Every mapped path exists in product.json.** A mapping to `identity.legalNam` silently
     resolves to None and emits nothing at all.

  3. **The generated values files are up to date.** Editing product.json without regenerating
     leaves the cluster on the old identity, and the diff nobody ran is invisible in review.

Not checked here, on purpose: that every product field has a consumer. `legal.vatNumber` and
`support.securityEmail` are read by humans and by documents, not by a Settings field. Demanding a
consumer for each would push us to invent knobs, which is the defect this gate exists to prevent.
"""
from __future__ import annotations

import ast
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts"))

import product  # noqa: E402  — scripts/ is not a package; the sys.path line above is the import


def settings_fields(service: str) -> set[str] | None:
    """Field names on a service's Settings, read from the AST.

    Parsed, not imported: importing a service's config module pulls in pydantic-settings and the
    whole app package, and a Settings with a required field raises at import time. The gate must
    not need a service's dependencies installed to check a mapping.
    """
    config = ROOT / "services" / service / "app" / "config.py"
    if not config.is_file():
        return None
    tree = ast.parse(config.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, ast.ClassDef) and node.name == "Settings":
            return {item.target.id for item in node.body
                    if isinstance(item, ast.AnnAssign) and isinstance(item.target, ast.Name)}
    return None


def main() -> int:
    problems: list[str] = []
    cfg, source = product.load()

    for service, mapping in sorted(product.WIRING.items()):
        fields = settings_fields(service)
        if fields is None:
            problems.append(f"{service}: mapped in scripts/product.py but has no app/config.py")
            continue
        for var, dotted in sorted(mapping.items()):
            if var not in fields:
                problems.append(
                    f"{service}: env var {var!r} is not a field on its Settings. "
                    f"Kubernetes would set it and nobody would read it.")
            if product.dig(cfg, dotted) is None:
                problems.append(
                    f"{service}: {var} maps to {dotted!r}, which does not exist in {source.name}")

    # (3) the generated files match what the generator would write right now
    sys.path.insert(0, str(ROOT / "scripts"))
    import gen_deploy_values  # noqa: PLC0415 — imported here so a mapping error reports first

    for path in sorted((ROOT / "deploy" / "services").glob("*.values.yaml")):
        name = path.name.removesuffix(".values.yaml")
        expected = gen_deploy_values.render(name, cfg)
        actual = path.read_text(encoding="utf-8")
        if actual.replace("\r\n", "\n") != expected.replace("\r\n", "\n"):
            problems.append(
                f"{path.relative_to(ROOT).as_posix()} is stale — run "
                f"`python scripts/gen_deploy_values.py`")

    for problem in problems:
        print(f"  {problem}")

    wired = {s: product.env_for(s, cfg) for s in product.WIRING}
    live = {s: e for s, e in wired.items() if e}
    if problems:
        print(f"\nproduct-wiring: FAIL — {len(problems)} problem(s)")
        return 1
    print(f"product-wiring: PASS — {len(product.WIRING)} service(s) mapped, "
          f"{len(live)} currently inherit a value from {source.name}; "
          f"every mapped knob is a real Settings field")
    return 0


if __name__ == "__main__":
    sys.exit(main())
