"""The gate runner's own guards.

This runner decides what blocks a merge. Every failure it has had was the same shape: it
reported success while checking nothing.

  * Pointed at a repo with no `services/`, it printed "ALL RUNNABLE GATES PASSED" after running
    zero tests and zero scanners. A loop over an empty set exits 0.
  * Its own metadata parser required quotes, so `gate_units=1` read as "" and every per-unit
    gate silently skipped the check that would have caught the above. The parser written to fix
    the bug reintroduced it.

So the runner is tested the way it tests everything else: by giving it a known-bad input and
demanding that it FAIL.
"""
from __future__ import annotations

import os
import pathlib
import shutil
import subprocess
import textwrap

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[3]
RUNNER = ROOT / "tools" / "ci" / "run_all_gates.sh"
GATES_D = ROOT / "tools" / "ci" / "gates.d"


def _bash() -> str | None:
    """A POSIX bash that can see this repository.

    On Windows, `bash` on PATH resolves to WSL, which lives in a different filesystem namespace
    and cannot execute a script at a C:\\ path. It does not fail loudly — it reports
    `execvpe(/bin/bash) failed` on stderr and returns an empty stdout, which reads exactly like
    a runner that produced no output. Prefer Git Bash where it exists.
    """
    for candidate in (os.environ.get("BASH"),
                      r"C:\Program Files\Git\bin\bash.exe",
                      r"C:\Program Files\Git\usr\bin\bash.exe"):
        if candidate and pathlib.Path(candidate).is_file():
            return candidate
    found = shutil.which("bash")
    if found and "System32" not in found and "system32" not in found:
        return found
    return None


BASH = _bash()
pytestmark = pytest.mark.skipif(BASH is None, reason="no POSIX bash that can see this repo")


def run(*args: str, env: dict[str, str] | None = None) -> subprocess.CompletedProcess:
    e = dict(os.environ, PYTHON=os.environ.get("PYTHON", "python"))
    e.update(env or {})
    return subprocess.run([BASH, str(RUNNER), *args], cwd=ROOT, env=e,
                          capture_output=True, text=True, timeout=900)


# ── discovery: an empty set must never pass ─────────────────────────────────
def test_no_units_is_fatal_not_a_clean_run(tmp_path):
    """The bug: pointed at a foreign repo it ran nothing and printed green."""
    empty = tmp_path / "no-units"
    empty.mkdir()
    r = run("--only", "safety:scans", env={"GATE_UNITS_DIR": str(empty)})
    assert r.returncode != 0, "a gate iterated an empty unit set and passed"
    assert "FATAL" in (r.stdout + r.stderr)
    assert "discovered 0 units" in (r.stdout + r.stderr)


def test_no_gates_is_fatal(tmp_path):
    """A runner with no gates checks nothing. Same defect, one level up."""
    empty = tmp_path / "no-gates"
    empty.mkdir()
    r = run(env={"GATE_GATES_DIR": str(empty)})
    assert r.returncode != 0
    assert "no gate files" in (r.stdout + r.stderr)


def test_a_foreign_layout_is_discovered_without_editing_the_runner(tmp_path):
    """An adopter's layout: `packages/api/src`, not `services/api/app`."""
    for unit in ("api", "web"):
        (tmp_path / "packages" / unit / "src").mkdir(parents=True)

    gate = tmp_path / "gd" / "10-count.gate"
    gate.parent.mkdir(parents=True)
    gate.write_text(textwrap.dedent('''\
        gate_name="units:found"
        gate_desc="counts what was discovered"
        gate_protects="proves discovery is config, not code"
        gate_needs=""
        gate_env=""
        gate_subject=""
        gate_units=1
        gate_slow=0
        gate_main() { services | tr '\\n' ' '; echo; [ "$(services | grep -c .)" = "2" ]; }
        '''), encoding="utf-8", newline="\n")

    r = run(env={"GATE_GATES_DIR": str(gate.parent),
                 "GATE_UNITS_DIR": str(tmp_path / "packages"),
                 "GATE_UNIT_MARKER": "src",
                 "GATE_UNIT_SKIP": ""})
    assert r.returncode == 0, r.stdout + r.stderr
    assert "api" in r.stdout and "web" in r.stdout


# ── the metadata parser that once lied ──────────────────────────────────────
def test_unquoted_metadata_is_parsed(tmp_path):
    """`gate_units=1` is unquoted. Reading it as "" disabled every per-unit check."""
    gate = tmp_path / "gd" / "10-unquoted.gate"
    gate.parent.mkdir(parents=True)
    gate.write_text(textwrap.dedent('''\
        gate_name="units:required"
        gate_desc="declares gate_units without quotes"
        gate_protects="the parser must not silently read this as empty"
        gate_needs=""
        gate_env=""
        gate_subject=""
        gate_units=1
        gate_slow=0
        gate_main() { echo "should never run: discovery is empty"; return 0; }
        '''), encoding="utf-8", newline="\n")

    empty = tmp_path / "none"
    empty.mkdir()
    r = run(env={"GATE_GATES_DIR": str(gate.parent), "GATE_UNITS_DIR": str(empty)})
    assert r.returncode != 0, "gate_units=1 was read as empty, so require_units never ran"
    assert "discovered 0 units" in (r.stdout + r.stderr)


# ── the four outcomes ───────────────────────────────────────────────────────
def _gate(tmp_path, body: str, name: str = "10-x.gate") -> pathlib.Path:
    d = tmp_path / "gd"
    d.mkdir(parents=True, exist_ok=True)
    (d / name).write_text(textwrap.dedent(body), encoding="utf-8", newline="\n")
    return d


def test_pending_never_blocks_even_under_strict(tmp_path):
    """A gate wired before its subject exists is a promise kept, not a build broken.

    Fail CI for this and the team learns that --strict is noise. Then a real SKIP gets waved
    through, which is exactly how `container (trivy)` skipped silently for months.
    """
    d = _gate(tmp_path, '''\
        gate_name="data:compile"
        gate_desc="the compiler we have not written yet"
        gate_protects="wired today so the day it lands it is already gated"
        gate_needs=""
        gate_env=""
        gate_subject="tools/data/compile.py"
        gate_units=0
        gate_slow=0
        gate_main() { echo "must not run"; return 1; }
        ''')
    for args in ([], ["--strict"]):
        r = run(*args, env={"GATE_GATES_DIR": str(d)})
        assert r.returncode == 0, f"PENDING blocked with args={args}"
        assert "PENDING" in r.stdout


def test_skip_is_a_failure_under_strict_but_not_otherwise(tmp_path):
    """SKIP means the INFRA is absent. That is a hole, and CI must not tolerate it."""
    d = _gate(tmp_path, '''\
        gate_name="needs:tool"
        gate_desc="requires a binary that does not exist"
        gate_protects="a gate that silently passes because its tool was missing is worse than none"
        gate_needs="definitely-not-a-real-binary-xyz"
        gate_env=""
        gate_subject=""
        gate_units=0
        gate_slow=0
        gate_main() { return 0; }
        ''')
    lax = run(env={"GATE_GATES_DIR": str(d)})
    assert lax.returncode == 0 and "SKIP" in lax.stdout

    strict = run("--strict", env={"GATE_GATES_DIR": str(d)})
    assert strict.returncode != 0, "--strict let a wired-but-skipped gate pass"
    assert "FAIL" in strict.stdout


def test_a_missing_env_var_is_a_skip_not_a_crash(tmp_path):
    d = _gate(tmp_path, '''\
        gate_name="needs:env"
        gate_desc="requires an env var"
        gate_protects="a database URL that is not set is absent infra, not a bug in the code"
        gate_needs=""
        gate_env="SOME_DSN_THAT_IS_NOT_SET"
        gate_subject=""
        gate_units=0
        gate_slow=0
        gate_main() { return 0; }
        ''')
    r = run(env={"GATE_GATES_DIR": str(d)})
    assert r.returncode == 0 and "SKIP" in r.stdout
    assert "SOME_DSN_THAT_IS_NOT_SET" in r.stdout


def test_a_failing_gate_blocks_the_merge(tmp_path):
    d = _gate(tmp_path, '''\
        gate_name="always:fails"
        gate_desc="catches something"
        gate_protects="the runner must actually propagate a non-zero exit"
        gate_needs=""
        gate_env=""
        gate_subject=""
        gate_units=0
        gate_slow=0
        gate_main() { echo "caught it"; return 1; }
        ''')
    r = run(env={"GATE_GATES_DIR": str(d)})
    assert r.returncode == 1
    assert "merge blocked" in r.stdout


def test_one_gate_cannot_corrupt_the_next(tmp_path):
    """Gates run in subshells. A gate that exports junk or cd's away must not poison its peer."""
    d = _gate(tmp_path, '''\
        gate_name="rude:gate"
        gate_desc="changes directory and clobbers variables"
        gate_protects="isolation: a gate is a subshell, not a mixin"
        gate_needs=""
        gate_env=""
        gate_subject=""
        gate_units=0
        gate_slow=0
        gate_main() { cd /; PY="/nonexistent"; UNITS_DIR="/nonexistent"; return 0; }
        ''', name="10-rude.gate")
    _gate(tmp_path, '''\
        gate_name="polite:gate"
        gate_desc="expects a sane environment"
        gate_protects="isolation"
        gate_needs=""
        gate_env=""
        gate_subject=""
        gate_units=0
        gate_slow=0
        gate_main() { [ -f "tools/ci/run_all_gates.sh" ] || { echo "cwd was clobbered"; return 1; }; return 0; }
        ''', name="20-polite.gate")
    r = run(env={"GATE_GATES_DIR": str(d)})
    assert r.returncode == 0, r.stdout + r.stderr


# ── --list is generated, so it cannot drift ─────────────────────────────────
def test_list_names_every_discovered_gate():
    r = run("--list")
    assert r.returncode == 0
    names = [p.read_text(encoding="utf-8").split('gate_name="', 1)[1].split('"', 1)[0]
             for p in sorted(GATES_D.glob("*.gate"))]
    assert names, "no gates shipped"
    for n in names:
        assert n in r.stdout, f"{n} is implemented but absent from --list"


def test_every_shipped_gate_declares_the_required_metadata():
    required = ("gate_name", "gate_desc", "gate_protects", "gate_needs",
                "gate_env", "gate_subject", "gate_units", "gate_slow")
    for path in sorted(GATES_D.glob("*.gate")):
        text = path.read_text(encoding="utf-8")
        missing = [k for k in required if f"\n{k}=" not in "\n" + text]
        assert not missing, f"{path.name} omits {missing}"
        assert "gate_main()" in text, f"{path.name} has no gate_main"
