"""No scanning — accept anything (default; pair with type/size validation)."""
from __future__ import annotations


class NoneScanner:
    driver = "none"

    def scan(self, data: bytes, content_type: str) -> None:
        return None
