#!/usr/bin/env python
"""META-GATE: the workflow and the gate script must agree, in BOTH directions.

Two failure modes this catches, both of which look green:

  * A gate is added to `run_all_gates.sh` and never invoked by CI. It is
    decoration — it protects nothing on the branch that matters.
  * The workflow claims a gate that no longer exists. The badge is a lie.

This is the same class of defect as a config knob nobody reads: the thing is
declared, documented, and inert. We already guard that in every service; the CI
deserves the same treatment.

It used to compare THREE places: described in `--list`, implemented in the script, run by the
workflow. All three need checking. We deleted the third instead -- a gate's description
now lives in its own gate file, beside its command, so "implemented but never described" cannot
be expressed. Do not gate a class of bug you can make unrepresentable.
"""
from __future__ import annotations

import pathlib
import re
import sys

ROOT = pathlib.Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "tools" / "ci" / "run_all_gates.sh"
GATES_DIR = ROOT / "tools" / "ci" / "gates.d"
WORKFLOW = ROOT / ".github" / "workflows" / "ci.yml"
WORKFLOWS_DIR = ROOT / ".github" / "workflows"

# Gates the workflow deliberately does not name individually because the strict
# full-suite step covers them. Must stay EMPTY unless justified here, in words.
WORKFLOW_EXEMPT: set[str] = set()


def gates_from_script() -> set[str]:
    """Every gate DISCOVERED in gates.d/, read from each file's `gate_name`.

    Deliberately does NOT shell out: on Windows `bash` may resolve to WSL, and a meta-gate that
    cannot run on a maintainer's machine is a meta-gate nobody runs before pushing.
    """
    if not GATES_DIR.is_dir():
        print(f"FAIL: no gates directory at {GATES_DIR.relative_to(ROOT).as_posix()}")
        raise SystemExit(1)

    names: set[str] = set()
    for path in sorted(GATES_DIR.glob("*.gate")):
        text = path.read_text(encoding="utf-8")
        m = re.search(r'^gate_name="([^"]+)"', text, re.MULTILINE)
        if not m:
            # A gate file with no name is a gate the runner lists as "" and nobody notices is
            # missing from the workflow.
            print(f"FAIL: {path.name} declares no gate_name")
            raise SystemExit(1)
        names.add(m.group(1))
    return names


def gates_from_workflow() -> set[str]:
    """Every gate the workflow names in a `--only <gate>` invocation or a comment tag."""
    text = WORKFLOW.read_text(encoding="utf-8")
    named = set(re.findall(r"--only\s+([a-z][a-z0-9:_-]+)", text))
    # `gates:` marker comments let a single strict run declare what it covers.
    named |= set(re.findall(r"#\s*gates:\s*(.+)", text) and
                 [g.strip() for line in re.findall(r"#\s*gates:\s*(.+)", text)
                  for g in line.split(",")] or [])
    return {g for g in named if g}


def workflows_parse() -> list[str]:
    """A workflow that is not valid YAML does not run AT ALL — GitHub skips it silently.

    This gate used to read ci.yml as text, so it happily confirmed "the workflow runs every
    gate" about a file GitHub was refusing to load. An unquoted `name:` containing `: ` was
    enough. Parse every workflow, so the gates cannot be green about a CI that never ran.
    """
    try:
        import yaml
    except ImportError:
        return ["pyyaml is not installed — cannot verify the workflows are valid YAML"]

    problems: list[str] = []
    for path in sorted((ROOT / ".github" / "workflows").glob("*.y*ml")):
        try:
            loaded = yaml.safe_load(path.read_text(encoding="utf-8"))
        except yaml.YAMLError as exc:
            problems.append(f"{path.name} is not valid YAML: {exc}")
            continue
        if not isinstance(loaded, dict) or "jobs" not in loaded:
            problems.append(f"{path.name} has no `jobs:` — it defines nothing")
    return problems


# The REAL INFRA a gate needs, and the env var that proves it was provided. `ci` and `release` run
# the same gates — but they are different workflows, and a gate is only as good as the environment
# it runs in.
#
# THIS EXISTS BECAUSE OF A REAL FAILURE. MinIO was added to `ci` and not to `release`. Both ran the
# same ObjectStore contract; in `ci` it ran against a real server and passed, in `release` it
# SKIPPED, and `--strict` turned the skip into a failure. The tag was refused. Same gates, different
# environments — the check "passed" because it was looking at a different thing than anyone thought.
#
# Comparing which gates a workflow runs is not enough. What they run AGAINST has to match too.
REQUIRED_INFRA = {
    "DATASTORE_TEST_DSN": "a real postgres (the contracts gate proves the param did not skip)",
    "OBJECTSTORE_TEST_ENDPOINT": "a real S3-compatible server (MinIO) for the ObjectStore contract",
}


def infra_parity() -> list[str]:
    """Every workflow that runs the gates must provide every piece of infra the gates need."""
    problems = []
    for wf in sorted(WORKFLOWS_DIR.glob("*.yml")):
        body = wf.read_text(encoding="utf-8")
        # Only workflows that actually run the gate runner or the per-service suites.
        if "run_all_gates.sh" not in body:
            continue
        for var, why in REQUIRED_INFRA.items():
            if var not in body:
                problems.append(
                    f"{wf.name} runs the gates but never sets {var} — it needs {why}. "
                    f"The gate will SKIP there and pass here, and under --strict a skip is a "
                    f"failure: the two workflows disagree about what was proven.")
    return problems


def main() -> int:
    if not WORKFLOW.exists():
        print(f"FAIL: no workflow at {WORKFLOW.relative_to(ROOT)} — the gates run nowhere.")
        return 1

    broken = workflows_parse()
    if broken:
        print("FAIL: a workflow will not run:")
        for b in broken:
            print(f"    - {b}")
        print("  GitHub skips an unparseable workflow silently. A green checkout means nothing.")
        return 1

    infra = infra_parity()
    if infra:
        print("FAIL: the workflows run the same gates against DIFFERENT infrastructure:")
        for i in infra:
            print(f"    - {i}")
        return 1

    script = gates_from_script()
    workflow = gates_from_workflow()
    if not script:
        print("FAIL: gates.d/ contains no gates. The runner would check nothing and exit 0.")
        return 1

    missing = script - workflow - WORKFLOW_EXEMPT
    unknown = workflow - script

    if missing:
        print("FAIL: gates defined in gates.d/ but never run by CI:")
        for g in sorted(missing):
            print(f"    - {g}")
        print("  A gate CI does not run protects nothing. Add it to the workflow,")
        print("  or delete its file from gates.d/.")
    if unknown:
        print("FAIL: the workflow runs gates that do not exist:")
        for g in sorted(unknown):
            print(f"    - {g}")
        print("  The badge would be claiming a check nobody defines.")

    if missing or unknown:
        return 1

    print(f"  workflow and gates.d/ agree on all {len(script)} gates: "
          f"{', '.join(sorted(script))}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
