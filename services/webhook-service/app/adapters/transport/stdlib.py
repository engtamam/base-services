"""Stdlib transport — connects ONLY to the pinned IP handed over by core.ssrf.

Why not just `urlopen(url)`? Because that re-resolves DNS, re-opening the rebinding window
we just closed. We open the socket to the validated IP ourselves, then speak HTTP(S) with
the original `Host` header (and SNI) so virtual hosting and certificate validation still work.
Response bodies are read up to `max_response_bytes` and no further.
"""
from __future__ import annotations

import http.client
import ipaddress
import socket
import ssl
import time
from urllib.parse import urlsplit

from app.ports.webhook_transport import Response


class StdlibTransport:
    driver = "stdlib"

    def send(self, *, url: str, pinned_ip: str, body: bytes, headers: dict[str, str],
             timeout_seconds: int, max_response_bytes: int) -> Response:
        parts = urlsplit(url)
        host = parts.hostname or ""
        port = parts.port or (443 if parts.scheme == "https" else 80)
        path = parts.path or "/"
        if parts.query:
            path = f"{path}?{parts.query}"

        started = time.monotonic()
        conn = None
        try:
            family = socket.AF_INET6 if isinstance(ipaddress.ip_address(pinned_ip),
                                                   ipaddress.IPv6Address) else socket.AF_INET
            if parts.scheme == "https":
                ctx = ssl.create_default_context()      # verifies the cert against `host`
                conn = http.client.HTTPSConnection(pinned_ip, port, timeout=timeout_seconds,
                                                   context=ctx, blocksize=8192)
                conn.host = host                        # SNI + certificate hostname
            else:
                conn = http.client.HTTPConnection(pinned_ip, port, timeout=timeout_seconds)

            # connect to the PINNED address, never re-resolving `host`
            sock = socket.socket(family, socket.SOCK_STREAM)
            sock.settimeout(timeout_seconds)
            sock.connect((pinned_ip, port))
            if parts.scheme == "https":
                sock = ctx.wrap_socket(sock, server_hostname=host)
            conn.sock = sock

            send_headers = {**headers, "Host": host}
            conn.request("POST", path, body=body, headers=send_headers)
            resp = conn.getresponse()
            raw = resp.read(max_response_bytes + 1)
            truncated = raw[:max_response_bytes].decode("utf-8", "replace")
            latency = int((time.monotonic() - started) * 1000)
            return Response(ok=True, status=resp.status, body=truncated, latency_ms=latency,
                            headers={k.lower(): v for k, v in resp.getheaders()})
        except Exception as exc:                        # timeout / TLS / connection reset / DNS
            latency = int((time.monotonic() - started) * 1000)
            return Response(ok=False, status=0, latency_ms=latency,
                            error=f"{type(exc).__name__}")   # secret-free
        finally:
            if conn is not None:
                try:
                    conn.close()
                except Exception:
                    pass
