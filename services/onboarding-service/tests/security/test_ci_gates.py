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


def test_forbid_beats_allow_when_mixed():
    v, _ = lic.classify_distribution("x", ["MIT License", "GPLv3"])
    assert v == lic.FORBID


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
