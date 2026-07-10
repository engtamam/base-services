"""Static RouteTable — parse routes from config JSON (inline or file). Zero-hardcoded."""
from __future__ import annotations

import json

from app.core.models import Route
from app.ports.errors import ConfigError


class StaticRouteTable:
    driver = "static"

    def __init__(self, routes_json: str, routes_file: str = "") -> None:
        raw = routes_json or "[]"
        if routes_file:
            try:
                with open(routes_file, encoding="utf-8") as fh:
                    raw = fh.read()
            except OSError as exc:
                raise ConfigError(f"GATEWAY_ROUTES_FILE unreadable: {exc}") from exc
        try:
            data = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise ConfigError(f"GATEWAY_ROUTES is not valid JSON: {exc}") from exc
        if not isinstance(data, list):
            raise ConfigError("GATEWAY_ROUTES must be a JSON array of route objects")
        routes: list[Route] = []
        for item in data:
            try:
                routes.append(Route.from_dict(item))
            except (ValueError, KeyError, TypeError) as exc:
                raise ConfigError(f"invalid route {item!r}: {exc}") from exc
        # longest-prefix first so match() returns the most specific route
        self._routes = sorted(routes, key=lambda r: len(r.prefix), reverse=True)

    def match(self, path: str) -> Route | None:
        for route in self._routes:
            if route.matches_path(path):
                return route
        return None

    def all(self) -> list[Route]:
        return list(self._routes)
