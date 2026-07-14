"""The rate-limit policy: composite keys, route classes, and honest headers."""
from __future__ import annotations

import json

import pytest

from app.core.ratelimit_policy import RateLimitPolicy


def policy(key_parts="subject,ip", routes=None, dmax=100, dwin=60):
    return RateLimitPolicy.from_config(
        key_parts=key_parts,
        routes_json=json.dumps(routes or []),
        default_limit=dmax, default_window_s=dwin)


# ── composite key (T7) ───────────────────────────────────────────────────────
def test_authenticated_request_keys_on_subject():
    p = policy()
    cls = p.classify("/api/v1/x")
    k = p.key(cls, subject="sara", ip="1.2.3.4", tenant=None)
    assert "s:sara" in k and "i:1.2.3.4" in k


def test_anonymous_request_falls_through_to_ip():
    p = policy(key_parts="subject")          # subject-only policy
    cls = p.classify("/api/v1/x")
    k = p.key(cls, subject=None, ip="1.2.3.4", tenant=None)
    assert "i:1.2.3.4" in k, "an unbucketed request is an unlimited one — never leave it keyless"


def test_two_users_behind_one_nat_get_separate_budgets():
    """The office-NAT problem: keyed on subject, two colleagues do not share one bucket."""
    p = policy(key_parts="subject,ip")
    cls = p.classify("/api/v1/x")
    a = p.key(cls, subject="alice", ip="10.0.0.1", tenant=None)
    b = p.key(cls, subject="bob", ip="10.0.0.1", tenant=None)
    assert a != b


def test_the_class_is_always_in_the_key():
    """The auth budget and the api budget for the same person are SEPARATE counters — hammering
    login must not spend your read budget."""
    p = policy(routes=[{"prefix": "/api/v1/auth", "class": "auth", "limit": 5, "window_s": 60}])
    auth_key = p.key(p.classify("/api/v1/auth/login"), subject="sara", ip="1.1.1.1", tenant=None)
    api_key = p.key(p.classify("/api/v1/users"), subject="sara", ip="1.1.1.1", tenant=None)
    assert auth_key != api_key


def test_empty_key_parts_is_refused_at_boot():
    """An empty key is one global bucket for everyone — a self-inflicted DoS the first time any
    client is busy."""
    with pytest.raises(ValueError):
        policy(key_parts="")


# ── route classes ────────────────────────────────────────────────────────────
def test_longest_prefix_wins():
    p = policy(routes=[
        {"prefix": "/api", "class": "api", "limit": 100, "window_s": 60},
        {"prefix": "/api/v1/auth", "class": "auth", "limit": 5, "window_s": 60},
    ])
    assert p.classify("/api/v1/auth/login").name == "auth"
    assert p.classify("/api/v1/users").name == "api"


def test_unmatched_route_uses_the_default():
    p = policy(dmax=42)
    cls = p.classify("/whatever")
    assert cls.name == "default" and cls.limit == 42


def test_cost_comes_from_the_route_class():
    p = policy(routes=[{"prefix": "/search", "class": "search",
                        "limit": 10, "window_s": 60, "cost": 5}])
    assert p.classify("/search/q").cost == 5
