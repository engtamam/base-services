"""update.sh's auto-rollback — the one thing it does that a bare `helm upgrade` loop does not.

No cluster required. The rollback reads the revision to return to from `helm status -o json` BEFORE
upgrading. That output has NO "revision" key — the number lives under "version" — so a parse of
"revision" silently yields empty for every release, the rollback branch becomes unreachable, and
every failed upgrade is left up while the operator is told there was "nothing to roll back to". These
tests pin the parse against realistic helm output and guard the branch structure so the regression
cannot come back quietly.
"""
from __future__ import annotations

import pathlib
import subprocess

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[2]
UPDATE_SH = ROOT / "deploy" / "server" / "update.sh"

# A realistic `helm status -o json` body — the keys helm 3 actually emits. Note: NO "revision" key;
# the release revision is "version".
HELM_STATUS_JSON = (
    '{"name":"compliance-service","info":{"first_deployed":"...","last_deployed":"...",'
    '"status":"deployed"},"config":{},"manifest":"...","version":3,"namespace":"base-services"}'
)


# The sed body update.sh uses to read the revision out of `helm status -o json` (the "version" key).
_VERSION_SED = r's/.*"version":[ ]*\([0-9]*\).*/\1/p'


@pytest.mark.skipif(not UPDATE_SH.is_file(), reason="update.sh not present")
def test_update_sh_uses_the_version_sed():
    """update.sh reads the prior revision from the "version" key with this exact pattern."""
    assert _VERSION_SED in UPDATE_SH.read_text(encoding="utf-8"), (
        'update.sh no longer parses the revision from the "version" key — the rollback parse regressed')


@pytest.mark.skipif(not (UPDATE_SH.is_file() and __import__("shutil").which("sed")),
                    reason="update.sh or sed not present")
def test_prev_revision_is_parsed_from_the_version_key():
    """The real sed extracts the revision (3) from helm's actual JSON — the rollback can find it."""
    out = subprocess.run(["sed", "-n", _VERSION_SED], input=HELM_STATUS_JSON,
                         capture_output=True, text=True)
    assert out.stdout.strip() == "3", (
        f"the prev-revision parse yielded {out.stdout.strip()!r}, not '3' — a failed upgrade would be "
        f"left up with no rollback")


@pytest.mark.skipif(not UPDATE_SH.is_file(), reason="update.sh not present")
def test_the_broken_revision_parse_is_gone():
    """The historical bug parsed a non-existent "revision" key. It must never come back."""
    text = UPDATE_SH.read_text(encoding="utf-8")
    assert '"revision":' not in text, (
        'update.sh parses a "revision" key again — helm status -o json does not emit one; use "version"')


@pytest.mark.skipif(not UPDATE_SH.is_file(), reason="update.sh not present")
def test_first_install_and_unreadable_revision_are_kept_distinct():
    """'no prior revision' (reassuring) and 'could not read the revision' (alarming) are opposite
    operator signals — the old code collapsed them into one false-reassuring message."""
    text = UPDATE_SH.read_text(encoding="utf-8")
    # A genuine first install stays reassuring.
    assert "first install" in text
    # A release that exists but whose revision we could not read must NOT say "nothing to roll back";
    # it must tell the operator to roll back by hand. `existed` is the flag that separates them.
    assert "existed" in text and "BY HAND" in text, (
        "update.sh must distinguish a first install from an unreadable revision and point the "
        "operator at a manual rollback in the latter case")


@pytest.mark.skipif(not UPDATE_SH.is_file(), reason="update.sh not present")
def test_a_slow_pull_is_not_treated_as_a_failure():
    """A helm --wait timeout on a still-pulling image must get a bounded pull grace before it is
    called a failure — otherwise a slow registry triggers a spurious rollback of a healthy service."""
    text = UPDATE_SH.read_text(encoding="utf-8")
    assert "PULL_GRACE" in text and "rollout status" in text, (
        "update.sh must give a helm --wait timeout a pull grace (kubectl rollout status) before "
        "declaring failure, so a slow image pull is not mistaken for a broken release")


@pytest.mark.skipif(not UPDATE_SH.is_file(), reason="update.sh not present")
def test_update_sh_parses():
    """The script is valid bash (guards against an edit that breaks the whole rollout)."""
    if not (bash := __import__("shutil").which("bash")):
        pytest.skip("bash not available")
    r = subprocess.run([bash, "-n", str(UPDATE_SH)], capture_output=True, text=True)
    assert r.returncode == 0, f"update.sh has a syntax error:\n{r.stderr}"
