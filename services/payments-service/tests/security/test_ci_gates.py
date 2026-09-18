"""Unit tests for the CI security gates (license classifier + orchestrator)."""
from __future__ import annotations

import pytest

from ci import check_licenses as lic
from ci import security_gate as gate


# ── License classification ──────────────────────────────────────────────────
@pytest.mark.parametrize(
    "text,expected",
    [
        ("MIT License", lic.ALLOW),
        ("License :: OSI Approved :: Apache Software License", lic.ALLOW),
        ("BSD-3-Clause", lic.ALLOW),
        ("ISC License (ISCL)", lic.ALLOW),
        ("Python Software Foundation License", lic.ALLOW),
        ("Mozilla Public License 2.0 (MPL 2.0)", lic.WARN),
        ("GNU Lesser General Public License v2 (LGPLv2)", lic.WARN),
        ("GNU General Public License v3 (GPLv3)", lic.FORBID),
        ("GNU Affero General Public License v3", lic.FORBID),
        ("Server Side Public License (SSPL)", lic.FORBID),
        ("Business Source License 1.1", lic.FORBID),
        ("Commons Clause", lic.FORBID),
        ("Totally Made Up License", lic.UNKNOWN),
    ],
)
def test_classify_one(text, expected):
    assert lic.classify_one(text) == expected


def test_a_classifier_list_is_disjunctive_most_permissive_wins():
    # PyPI classifier lists are ALTERNATIVES the recipient chooses among (docutils ships as
    # Public Domain OR BSD OR GPL). Selecting the GPL on the user's behalf and then forbidding the
    # package for being GPL is backwards — a permissive alternative exists, so the package is ALLOW.
    v, _ = lic.classify_distribution("x", ["MIT License", "GPLv3"])
    assert v == lic.ALLOW
    v2, _ = lic.classify_distribution("docutils",
                                      ["License :: Public Domain",
                                       "License :: OSI Approved :: BSD License",
                                       "License :: OSI Approved :: GNU General Public License (GPL)"])
    assert v2 == lic.ALLOW


def test_a_license_expression_reads_OR_and_AND_explicitly():
    # An explicit SPDX expression removes the ambiguity: OR is a choice, AND binds both.
    assert lic.classify_expression("MIT OR GPL-3.0-only") == lic.ALLOW          # choose MIT
    assert lic.classify_expression("MIT AND GPL-3.0-only") == lic.FORBID        # both apply
    assert lic.classify_expression("Apache-2.0 OR AGPL-3.0") == lic.ALLOW       # choose Apache
    assert lic.classify_expression("MIT") == lic.ALLOW                          # no operator


def test_override_wins():
    v, detail = lic.classify_distribution("cryptography", ["something weird"])
    assert v == lic.ALLOW and detail == "override"


def test_no_metadata_is_unknown():
    v, _ = lic.classify_distribution("mystery-pkg", [])
    assert v == lic.UNKNOWN


def test_real_scan_has_no_forbidden():
    results = lic.scan()
    bad = [r for r in results if r[2] in (lic.FORBID, lic.UNKNOWN)]
    assert bad == [], f"disallowed licenses present: {bad}"


# ── Orchestrator ────────────────────────────────────────────────────────────
def test_gate_registry_shape():
    names = [n for n, _ in gate.GATES]
    assert "license" in names
    assert "sast (bandit)" in names
    assert "secret-scan" in names
    assert len(gate.GATES) == 6


def test_secret_set_parses():
    payload = '{"results": {"a.py": [{"hashed_secret": "abc", "type": "x"}]}}'
    assert gate._secret_set(payload) == {("a.py", "abc")}


def test_secret_set_bad_json_is_empty():
    assert gate._secret_set("not json") == set()


def test_gate_functions_return_valid_status():
    # license + sbom are deterministic & offline; run them for real.
    for fn in (gate.gate_license, gate.gate_sbom):
        status, _ = fn()
        assert status in (gate.PASS, gate.FAIL, gate.SKIP)
