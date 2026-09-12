"""Round-2 fixes (file-service). Maps to docs/audits/round2/file-service.json."""
from __future__ import annotations

import asyncio

import pytest

from app.config import Settings


def run(c):
    return asyncio.run(c)


# r2-03 — production with a scan policy but no scanner refuses to start (not just 'skip').
def test_scan_policy_without_a_scanner_is_refused_in_production():
    for policy in ("async", "require"):
        with pytest.raises(ValueError, match="SCANNER_DRIVER='none'"):
            Settings(ENV="production", LARGE_UPLOAD_SCAN=policy, SCANNER_DRIVER="none",
                     INTERNAL_SERVICE_TOKEN="x" * 32).assert_large_scan_ok()
    # a real scanner is accepted
    Settings(ENV="production", LARGE_UPLOAD_SCAN="async", SCANNER_DRIVER="magic",
             INTERNAL_SERVICE_TOKEN="x" * 32).assert_large_scan_ok()


# r2-08 — the hmac signer must have its own secret in production, not fall back to the internal token.
def test_signed_url_secret_required_in_production():
    with pytest.raises(ValueError, match="SIGNED_URL_SECRET is required"):
        Settings(ENV="production", SIGNED_URL_DRIVER="hmac", SIGNED_URL_SECRET="",
                 SCANNER_DRIVER="magic", INTERNAL_SERVICE_TOKEN="x" * 32).assert_signed_url_secret_ok()
    # a distinct secret is accepted
    Settings(ENV="production", SIGNED_URL_DRIVER="hmac", SIGNED_URL_SECRET="s" * 40,
             SCANNER_DRIVER="magic", INTERNAL_SERVICE_TOKEN="x" * 32).assert_signed_url_secret_ok()


# r2-07 — download_redirect honors the configured PRESIGN_TTL, not a hardcoded 300.
def test_download_redirect_uses_configured_presign_ttl():
    from app.core.file_service import FileService
    from app.core.models import FileObject

    class _Store:
        supports_presign = True
        def __init__(self): self.ttl = None
        async def presign_get(self, sha, name, ttl):
            self.ttl = ttl
            return f"https://bucket/{sha}?ttl={ttl}"

    store = _Store()
    svc = FileService(repo=None, object_store=store, scanner=None, signed_url=None,
                      max_upload_bytes=1, allowed_content_types=frozenset(), presign_ttl=900)
    f = FileObject(id="f1", tenant="t", owner="o", filename="a.bin", content_type="application/octet-stream",
                   size=1, sha256="a" * 64)
    run(svc.download_redirect(f))
    assert store.ttl == 900          # the knob, not 300
