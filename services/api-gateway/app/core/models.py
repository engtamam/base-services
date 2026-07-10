"""Route model — one entry in the (config-driven) gateway route table."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

AUTH_MODES = ("none", "optional", "required")


@dataclass(frozen=True)
class Route:
    prefix: str
    upstream: str
    auth: str = "none"                       # none | optional | required
    strip_prefix: bool = True
    methods: tuple[str, ...] = ()            # empty = all methods
    capabilities: tuple[str, ...] = ()       # required caps (auth=required)
    cache_ttl: int = 0                       # seconds; 0 = no cache

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "Route":
        prefix = str(d["prefix"])
        upstream = str(d["upstream"])
        auth = d.get("auth", "none")
        if auth not in AUTH_MODES:
            raise ValueError(f"route {prefix!r}: invalid auth {auth!r}")
        if "://" not in upstream:
            raise ValueError(f"route {prefix!r}: upstream must be an absolute URL")
        if not prefix.startswith("/"):
            raise ValueError(f"route {prefix!r}: prefix must start with '/'")
        return cls(
            prefix=prefix.rstrip("/") or "/", upstream=upstream, auth=auth,
            strip_prefix=bool(d.get("strip_prefix", True)),
            methods=tuple(m.upper() for m in d.get("methods", []) or []),
            capabilities=tuple(d.get("capabilities", []) or []),
            cache_ttl=int(d.get("cache_ttl", 0)),
        )

    def matches_path(self, path: str) -> bool:
        return path == self.prefix or path.startswith(self.prefix + "/")

    def allows_method(self, method: str) -> bool:
        return not self.methods or method.upper() in self.methods

    def build_url(self, path: str) -> str:
        """Append the (prefix-stripped) path to the fixed upstream host.

        The upstream host is config; the request only contributes a path — so the
        target host can never be changed by the caller (SSRF-safe).
        """
        remainder = path
        if self.strip_prefix and (path == self.prefix or path.startswith(self.prefix + "/")):
            remainder = path[len(self.prefix):]
        return self.upstream.rstrip("/") + "/" + remainder.lstrip("/")

    def summary(self) -> dict[str, Any]:
        return {"prefix": self.prefix, "upstream": self.upstream, "auth": self.auth,
                "methods": list(self.methods) or ["*"], "cache_ttl": self.cache_ttl}
