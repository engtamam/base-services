"""ClamAV scanner — infra-gated (needs a clamd daemon + the 'clamd' package)."""
from __future__ import annotations

from app.adapters.lazy import load
from app.ports.errors import ConfigError
from app.ports.scanner import Rejected


class ClamavScanner:
    driver = "clamav"

    def __init__(self, host: str = "", port: int = 3310) -> None:
        clamd = load("clamd", "clamav")
        try:
            self._client = (clamd.ClamdNetworkSocket(host, port) if host
                            else clamd.ClamdUnixSocket())
        except Exception as exc:  # pragma: no cover - needs a daemon
            raise ConfigError(f"clamd unavailable: {exc}") from exc

    def scan(self, data: bytes, content_type: str) -> None:  # pragma: no cover - needs a daemon
        import io

        result = self._client.instream(io.BytesIO(data))
        status = result.get("stream", ("OK",))[0]
        if status != "OK":
            raise Rejected("malware detected")
