#!/usr/bin/env python
"""META-GATE: the workflow and the gate script must agree, in BOTH directions.

Two failure modes this catches, both of which look green:

  * A gate is added to `run_all_gates.sh` and never invoked by CI. It is
    decoration — it protects nothing on the branch that matters.
  * The workflow claims a gate that no longer exists. The badge is a lie.

This is the same class of defect as a config knob nobody reads: the thing is
declared, documented, and inert. We already guard that in every service; the CI
deserves the same treatment.
"""
from __future__ import annotations

import pathlib
import re
import sys

ROOT = pathlib.Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "tools" / "ci" / "run_all_gates.sh"
WORKFLOW = ROOT / ".github" / "workflows" / "ci.yml"

# Gates the workflow deliberately does not name individually because the strict
# full-suite step covers them. Must stay EMPTY unless justified here, in words.
WORKFLOW_EXEMPT: set[str] = set()


def gates_from_script() -> set[str]:
    """Read the `--list` block straight from the script.

    Deliberately does NOT shell out: on Windows `bash` may resolve to WSL, and a meta-gate
    that cannot run on a maintainer's machine is a meta-gate nobody runs before pushing.
    """
    text = SCRIPT.read_text(encoding="utf-8")
    m = re.search(r"cat <<'LIST'\n(.*?)\nLIST\n", text, re.DOTALL)
    if not m:
        print("FAIL: could not find the `--list` block in run_all_gates.sh")
        raise SystemExit(1)
    # A gate line looks like:  "  name:with-colons   description..."
    return {g.group(1) for g in
            (re.match(r"^  ([a-z][a-z0-9:_-]+)\s{2,}\S", line) for line in m.group(1).splitlines())
            if g}


def gates_from_workflow() -> set[str]:
    """Every gate the workflow names in a `--only <gate>` invocation or a comment tag."""
    text = WORKFLOW.read_text(encoding="utf-8")
    named = set(re.findall(r"--only\s+([a-z][a-z0-9:_-]+)", text))
    # `gates:` marker comments let a single strict run declare what it covers.
    named |= set(re.findall(r"#\s*gates:\s*(.+)", text) and
                 [g.strip() for line in re.findall(r"#\s*gates:\s*(.+)", text)
                  for g in line.split(",")] or [])
    return {g for g in named if g}


def main() -> int:
    if not WORKFLOW.exists():
        print(f"FAIL: no workflow at {WORKFLOW.relative_to(ROOT)} — the gates run nowhere.")
        return 1

    script = gates_from_script()
    workflow = gates_from_workflow()
    if not script:
        print("FAIL: could not parse any gate from `--list`.")
        return 1

    missing = script - workflow - WORKFLOW_EXEMPT
    unknown = workflow - script

    if missing:
        print("FAIL: gates defined in run_all_gates.sh but never run by CI:")
        for g in sorted(missing):
            print(f"    - {g}")
        print("  A gate CI does not run protects nothing. Add it to the workflow,")
        print("  or delete it from the script.")
    if unknown:
        print("FAIL: the workflow runs gates that do not exist:")
        for g in sorted(unknown):
            print(f"    - {g}")
        print("  The badge would be claiming a check nobody defines.")

    if missing or unknown:
        return 1

    print(f"  workflow and script agree on all {len(script)} gates: "
          f"{', '.join(sorted(script))}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
