"""Pure cores: seal/redact, SSRF matrix, Retry-After backoff, mapping (no eval), circuit."""
from __future__ import annotations

import pytest

from app.core import circuit
from app.core.backoff import next_delay, parse_retry_after
from app.core.mapping import MappingError, apply, get_path, validate
from app.core.seal import redact, seal, spec_secret_aad, token_aad, unseal
from app.core.ssrf import SsrfBlocked
from app.core.ssrf import validate as ssrf_validate


# ── seal ──────────────────────────────────────────────────────────────────────
def test_seal_roundtrip_and_wrong_key():
    tok = seal("ya29.SECRET", "k" * 32)
    assert "SECRET" not in tok
    assert unseal(tok, "k" * 32) == "ya29.SECRET"
    assert unseal(tok, "wrong") is None


def test_seal_empty_and_bad_token():
    assert seal("", "k") == ""
    assert unseal("", "k") is None
    assert unseal("not-base64!!", "k") is None


def test_a_token_bound_to_a_connection_cannot_be_relocated():
    """safety:aesgcm-aad — a token sealed for connection A must not unseal under connection B's
    slot, or a DB tamperer could copy A's access token into B's row and call the provider as A."""
    key = "k" * 32
    tok = seal("ya29.ACCESS", key, token_aad("cn_A", "access"))
    # Right slot round-trips; a relocated connection id, or the refresh field, fails the tag → None.
    assert unseal(tok, key, token_aad("cn_A", "access")) == "ya29.ACCESS"
    assert unseal(tok, key, token_aad("cn_B", "access")) is None
    assert unseal(tok, key, token_aad("cn_A", "refresh")) is None
    # A spec's client secret is likewise bound to its spec id.
    cs = seal("client-secret", key, spec_secret_aad("spec_1"))
    assert unseal(cs, key, spec_secret_aad("spec_1")) == "client-secret"
    assert unseal(cs, key, spec_secret_aad("spec_2")) is None


def test_redact_removes_credentials_from_text():
    assert "ya29.SECRETTOKEN" not in redact("failed: ya29.SECRETTOKEN", "ya29.SECRETTOKEN")
    assert redact("nothing", "") == "nothing"        # short/empty secrets never mangle text


# ── SSRF ──────────────────────────────────────────────────────────────────────
DANGEROUS = ["http://169.254.169.254/", "http://[fd00:ec2::254]/", "http://100.100.100.200/",
             "http://127.0.0.1:8069/", "http://localhost/", "http://10.1.2.3/",
             "http://192.168.1.1/", "http://172.16.0.1/", "http://[::1]/", "http://[fe80::1]/",
             "http://0.0.0.0/"]


@pytest.mark.parametrize("url", DANGEROUS)
def test_private_and_metadata_blocked_at_the_ip_layer(url):
    # even with require_https OFF — the scheme check must never be the only defence
    with pytest.raises(SsrfBlocked):
        ssrf_validate(url, require_https=False, allow_private=False)


def test_metadata_blocked_even_when_private_ips_allowed():
    for url in ("http://169.254.169.254/", "http://[fd00:ec2::254]/"):
        with pytest.raises(SsrfBlocked, match="metadata"):
            ssrf_validate(url, require_https=False, allow_private=True)


@pytest.mark.parametrize("url", ["file:///etc/passwd", "gopher://x/", "ftp://x/"])
def test_bad_schemes_blocked(url):
    with pytest.raises(SsrfBlocked):
        ssrf_validate(url, require_https=False)


def test_https_required_and_credentials_rejected():
    with pytest.raises(SsrfBlocked, match="https"):
        ssrf_validate("http://example.com/")
    with pytest.raises(SsrfBlocked, match="credentials"):
        ssrf_validate("https://u:p@example.com/")


def test_public_host_allowed_and_pinned():
    t = ssrf_validate("https://example.com/odoo")
    assert t.host == "example.com" and t.ip and t.ip != "example.com"


def test_allowlist_enforced():
    with pytest.raises(SsrfBlocked, match="allow-listed"):
        ssrf_validate("https://example.com/", allowed_hosts=frozenset({"other.com"}))


# ── backoff ───────────────────────────────────────────────────────────────────
def test_retry_after_is_obeyed_and_capped():
    assert next_delay(0, base_seconds=5, max_seconds=3600, max_attempts=5, retry_after=120) == 120.0
    assert next_delay(0, base_seconds=5, max_seconds=60, max_attempts=5, retry_after=99999) == 60.0


def test_parse_retry_after():
    assert parse_retry_after("120") == 120.0
    assert parse_retry_after("Wed, 21 Oct 2026 07:28:00 GMT") is None   # HTTP-date ignored
    assert parse_retry_after(None) is None
    assert parse_retry_after("-5") is None


def test_jitter_spreads_and_exhausts():
    delays = [next_delay(2, base_seconds=5, max_seconds=3600, max_attempts=5, seed=f"c{i}")
              for i in range(200)]
    ceiling = 5 * 4
    assert all(0 <= d <= ceiling for d in delays) and len(set(delays)) > 150
    assert next_delay(5, base_seconds=5, max_seconds=60, max_attempts=5) is None


# ── mapping (no eval, fail-closed) ────────────────────────────────────────────
def test_mapping_ops():
    m = {"email": {"op": "path", "value": "properties.email"},
         "plan": {"op": "const", "value": "pro"},
         "name": {"op": "concat", "value": ["first", "last"]},
         "missing": {"op": "path", "value": "nope", "default": "n/a"}}
    validate(m)
    out = apply(m, {"properties": {"email": "a@b.com"}, "first": "Ali", "last": "Saud"})
    assert out == {"email": "a@b.com", "plan": "pro", "name": "Ali Saud", "missing": "n/a"}


def test_unknown_op_and_bad_concat_rejected():
    with pytest.raises(MappingError):
        validate({"x": {"op": "__import__", "value": "os"}})
    with pytest.raises(MappingError):
        validate({"x": {"op": "concat", "value": "notalist"}})
    with pytest.raises(MappingError):
        validate([1, 2])


def test_hostile_values_are_never_executed():
    payload = "__import__('os').system('rm -rf /')"
    assert apply({"v": {"op": "path", "value": "v"}}, {"v": payload}) == {"v": payload}


def test_missing_field_is_dropped_not_invented():
    assert apply({"v": {"op": "path", "value": "nope"}}, {}) == {}


def test_get_path_never_raises():
    assert get_path({"a": 1}, "a.b.c") is None
    assert get_path({}, "x") is None


# ── circuit ───────────────────────────────────────────────────────────────────
def test_circuit_opens_and_half_opens():
    out = circuit.on_failure(consecutive_failures=4, state=circuit.CLOSED, now=100, threshold=5)
    assert out["circuit_state"] == circuit.OPEN
    assert circuit.state_now(circuit_state="open", opened_at=100, now=500, reset_seconds=300) == circuit.HALF_OPEN
    assert not circuit.can_attempt(circuit.OPEN)
    reopened = circuit.on_failure(consecutive_failures=0, state=circuit.HALF_OPEN, now=600, threshold=99)
    assert reopened["circuit_state"] == circuit.OPEN
