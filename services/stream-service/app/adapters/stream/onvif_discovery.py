"""OnvifDiscovery — find ONVIF cameras on the local network via WS-Discovery.

WS-Discovery is a multicast probe; the `probe` seam is injectable so the parsing/shaping logic is
unit-testable without a network. Lazily imported (optional dep). Returns DiscoveredCamera rows the
operator can then register (registration still runs the SSRF guard — discovery does not bypass it).
"""
from __future__ import annotations

from typing import Any, Callable

from app.ports.stream_types import DiscoveredCamera


def _default_probe(timeout_seconds: float) -> list[str]:   # pragma: no cover - needs a network
    from wsdiscovery.discovery import ThreadedWSDiscovery

    wsd = ThreadedWSDiscovery()
    wsd.start()
    try:
        services = wsd.searchServices(timeout=timeout_seconds)
        xaddrs: list[str] = []
        for s in services:
            xaddrs.extend(s.getXAddrs() or [])
        return xaddrs
    finally:
        wsd.stop()


class OnvifDiscovery:
    driver = "onvif-ws"

    def __init__(self, *, probe: Callable[[float], list[str]] = _default_probe) -> None:
        self._probe = probe

    async def discover(self, timeout_seconds: float = 3.0) -> list[DiscoveredCamera]:
        import asyncio

        xaddrs = await asyncio.to_thread(self._probe, timeout_seconds)
        cams: list[DiscoveredCamera] = []
        seen: set[str] = set()
        for xaddr in xaddrs:
            host = _host_of(xaddr)
            if not host or host in seen:
                continue
            seen.add(host)
            cams.append(DiscoveredCamera(name=host, url=xaddr, kind="onvif", onvif_host=host))
        return cams


def _host_of(xaddr: str) -> str:
    from urllib.parse import urlsplit
    try:
        return urlsplit(xaddr).hostname or ""
    except Exception:   # noqa: BLE001
        return ""
