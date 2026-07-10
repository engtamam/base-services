"""Change detection: narrow the matrix without ever skipping a service that changed.

The danger in "build only what changed" is building too little. Every test here that narrows the
set is paired with the reasoning for why that is safe, and the shared-path and fail-safe cases
assert the opposite: the full set.
"""
from __future__ import annotations

import sys
import pathlib

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
import changed_services as cs  # noqa: E402

ALL = ["billing-service", "identity-service", "webhook-service"]


def _select(monkeypatch, files):
    monkeypatch.setattr(cs, "changed_files", lambda base: files)
    return cs.selected("origin/main", ALL)


def test_one_service_change_selects_only_that_service(monkeypatch):
    chosen, reason = _select(monkeypatch, ["services/billing-service/app/core/stores.py"])
    assert chosen == ["billing-service"], reason


def test_two_services_select_both(monkeypatch):
    chosen, _ = _select(monkeypatch, [
        "services/billing-service/app/x.py",
        "services/webhook-service/tests/y.py",
    ])
    assert chosen == ["billing-service", "webhook-service"]


def test_the_chart_forces_the_full_set(monkeypatch):
    chosen, reason = _select(monkeypatch, ["deploy/chart/templates/deployment.yaml"])
    assert chosen == ALL and "shared" in reason


def test_the_gate_runner_forces_the_full_set(monkeypatch):
    chosen, _ = _select(monkeypatch, ["tools/ci/run_all_gates.sh"])
    assert chosen == ALL


def test_the_template_seed_forces_the_full_set(monkeypatch):
    """Every service is a copy of _template; a change there could belong in all of them."""
    chosen, _ = _select(monkeypatch, ["services/_template/app/config.py"])
    assert chosen == ALL


def test_a_workflow_change_forces_the_full_set(monkeypatch):
    chosen, _ = _select(monkeypatch, [".github/workflows/ci.yml"])
    assert chosen == ALL


def test_a_root_file_forces_the_full_set(monkeypatch):
    """A root README/LICENSE/pyproject is not owned by one service — rebuild all."""
    for f in ("README.md", "pyproject.toml", ".gitattributes"):
        chosen, _ = _select(monkeypatch, [f])
        assert chosen == ALL, f


def test_a_mixed_change_including_a_shared_path_is_full(monkeypatch):
    """One service plus one shared file is NOT 'just that service'."""
    chosen, _ = _select(monkeypatch, [
        "services/billing-service/app/x.py",
        "tools/ci/check_scan_discipline.py",
    ])
    assert chosen == ALL


def test_an_unresolvable_diff_builds_everything(monkeypatch):
    monkeypatch.setattr(cs, "changed_files", lambda base: None)
    chosen, reason = cs.selected("origin/main", ALL)
    assert chosen == ALL and "fail safe" in reason


def test_an_empty_diff_builds_everything(monkeypatch):
    """No resolved changes is 'I could not tell', not 'nothing changed'. Build all."""
    chosen, reason = _select(monkeypatch, [])
    assert chosen == ALL and "fail safe" in reason


def test_a_change_to_an_unknown_service_dir_builds_everything(monkeypatch):
    """A path under services/ that matches no known service (a rename/delete) — do not skip."""
    chosen, reason = _select(monkeypatch, ["services/ghost-service/app/x.py"])
    assert chosen == ALL and "fail safe" in reason
