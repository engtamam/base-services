"""L3 — a registry of named dynamic-Flow handlers, so one endpoint can serve several flows.

GENERIC: booking is one entry; a future survey/dynamic-form handler registers alongside it under its
own name. The endpoint resolves which handler to run (by a name it derives however it likes — a path
segment, the flow definition, a flow_token prefix); the registry only maps name -> FlowHandler. It
imposes no routing scheme, so it never constrains a platform's topology.
"""
from __future__ import annotations

from app.core.flow_dispatch import FlowHandler


class FlowRegistry:
    def __init__(self) -> None:
        self._handlers: dict[str, FlowHandler] = {}

    def register(self, handler: FlowHandler) -> None:
        self._handlers[handler.name] = handler

    def get(self, name: str) -> FlowHandler | None:
        return self._handlers.get(name)

    def names(self) -> tuple[str, ...]:
        return tuple(self._handlers)
