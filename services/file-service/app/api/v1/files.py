# =============================================================================
# file-service — upload / download / metadata / signed-url / delete
# =============================================================================
from __future__ import annotations

from fastapi import APIRouter, Depends, Header, HTTPException, Request, status, Query
from pydantic import BaseModel, Field
from starlette.responses import RedirectResponse, Response

from app.api.auth import Principal, require_user
from app.api.authz import require_capability
from app.api.deps import get_guard
from app.api.ratelimit import rate_limit
from app.config import get_settings
from app.core.file_repository import FileRepository
from app.core.file_service import (
    BadDigest, FileService, LargeUploadUnavailable, TooLarge, TypeNotAllowed,
)
from app.core.models import FileObject
from app.core.upload_session import UploadSessionStore
from app.ports.errors import NotFound
from app.ports.object_store import CompletedPart
from app.ports.scanner import Rejected

router = APIRouter(prefix="/api/v1/files", tags=["files"])
public_router = APIRouter(tags=["files-public"])

_UPLOAD = require_capability("files.upload")
_READ = require_capability("files.read")


def build_files(request: Request) -> FileService:
    c = request.app.state.container
    s = get_settings()
    return FileService(
        repo=FileRepository(c.datastore), object_store=c.object_store,
        scanner=c.scanner, signed_url=c.signed_url,
        max_upload_bytes=s.MAX_UPLOAD_BYTES, allowed_content_types=s.allowed_content_types,
        max_page_limit=s.MAX_PAGE_LIMIT,
        sessions=UploadSessionStore(c.datastore), queue=c.queue,
        scan_queue=s.FILE_SCAN_QUEUE, max_object_bytes=s.MAX_OBJECT_BYTES,
        part_size=s.MULTIPART_PART_SIZE, scan_policy=s.LARGE_UPLOAD_SCAN,
    )


class InitiateIn(BaseModel):
    filename: str = Field(default="upload.bin", max_length=255)
    content_type: str = Field(default="application/octet-stream", max_length=255)
    sha256: str = Field(min_length=64, max_length=64)
    size: int = Field(ge=0)


class PartIn(BaseModel):
    part_number: int = Field(ge=1)
    etag: str = Field(min_length=1, max_length=256)


class CompleteIn(BaseModel):
    parts: list[PartIn] = Field(min_length=1)


def _safe_filename(name: str) -> str:
    return (name or "upload.bin").replace('"', "").replace("\r", "").replace("\n", "")[:255]


async def _load_scoped(svc: FileService, file_id: str, principal: Principal) -> FileObject:
    try:
        f = await svc.get(file_id)
    except NotFound:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "NOT_FOUND")
    if f.tenant and principal.tenant and f.tenant != principal.tenant \
            and "platform.files" not in principal.caps:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "NOT_FOUND")  # hide existence
    return f


@router.post("", status_code=201, dependencies=[Depends(_UPLOAD), Depends(rate_limit)])
async def upload(request: Request, principal: Principal = Depends(require_user),
                 x_filename: str = Header(default="upload.bin", alias="X-Filename")) -> dict:
    s = get_settings()
    svc = build_files(request)
    # Negotiation (fail-closed): a body over the inline boundary must use the direct-to-bucket path
    # if the store supports it — never silently buffer a huge object in the app. If it does not, the
    # inline cap stands and the client gets a plain 413.
    # A body over the inline boundary is refused up front (before we read it) — never silently
    # buffered. The client discovers the boundary and the large endpoint from the manifest
    # (large_uploads / max_inline_bytes), so the error body stays generic (CWE-209).
    declared_len = request.headers.get("content-length")
    if declared_len and declared_len.isdigit() and int(declared_len) > s.MAX_INLINE_BYTES:
        raise HTTPException(413, "PAYLOAD_TOO_LARGE")
    data = await request.body()
    content_type = request.headers.get("content-type", "application/octet-stream")
    try:
        f = await svc.upload(tenant=principal.tenant or "", owner=principal.subject,
                             filename=_safe_filename(x_filename), content_type=content_type, data=data)
    except TooLarge:
        raise HTTPException(413, "PAYLOAD_TOO_LARGE")
    except TypeNotAllowed:
        raise HTTPException(415, "UNSUPPORTED_MEDIA_TYPE")
    except Rejected:
        raise HTTPException(422, "REJECTED")
    return f.to_dict()


# ── Large, direct-to-bucket upload (Phase 34) ────────────────────────────────
@router.post("/uploads", status_code=201, dependencies=[Depends(_UPLOAD), Depends(rate_limit)])
async def initiate_large_upload(request: Request, body: InitiateIn,
                                principal: Principal = Depends(require_user)) -> dict:
    """Begin a direct-to-bucket upload. Returns either a dedup hit (already yours) or a presigned
    multipart plan the client PUTs its parts to."""
    svc = build_files(request)
    try:
        result = await svc.begin_large_upload(
            tenant=principal.tenant or "", owner=principal.subject,
            filename=_safe_filename(body.filename), content_type=body.content_type,
            sha256=body.sha256, size=body.size)
    except LargeUploadUnavailable:
        raise HTTPException(status.HTTP_501_NOT_IMPLEMENTED,
                            detail="LARGE_UPLOADS_UNAVAILABLE")
    except BadDigest:
        raise HTTPException(422, "BAD_SHA256")
    except TooLarge:
        raise HTTPException(413, "PAYLOAD_TOO_LARGE")
    except TypeNotAllowed:
        raise HTTPException(415, "UNSUPPORTED_MEDIA_TYPE")

    if result.deduped_file is not None:
        return {"deduplicated": True, "file": result.deduped_file.to_dict()}
    plan = result.plan
    return {
        "deduplicated": False,
        "session": result.session.public_dict(),
        "upload": {
            "part_size": plan.part_size, "part_count": plan.part_count,
            "parts": [{"part_number": p.part_number, "url": p.url} for p in plan.urls],
        },
    }


@router.post("/uploads/{session_id}/complete", dependencies=[Depends(_UPLOAD)])
async def complete_large_upload(request: Request, session_id: str, body: CompleteIn,
                                principal: Principal = Depends(require_user)) -> dict:
    """Client finished the parts. We assemble, confirm the object landed, and queue the scan."""
    svc = build_files(request)
    try:
        parts = [CompletedPart(part_number=p.part_number, etag=p.etag) for p in body.parts]
        sess = await svc.complete_large_upload(session_id=session_id, tenant=principal.tenant or "",
                                               owner=principal.subject, parts=parts)
    except NotFound:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "NOT_FOUND")
    return sess.public_dict()


@router.get("/uploads/{session_id}", dependencies=[Depends(_READ)])
async def upload_status(request: Request, session_id: str,
                        principal: Principal = Depends(require_user)) -> dict:
    svc = build_files(request)
    try:
        sess = await svc.get_session(session_id, tenant=principal.tenant or "",
                                     owner=principal.subject)
    except NotFound:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "NOT_FOUND")
    return sess.public_dict()


@router.delete("/uploads/{session_id}", dependencies=[Depends(_UPLOAD)])
async def abort_upload(request: Request, session_id: str,
                       principal: Principal = Depends(require_user)) -> dict:
    """Cancel an in-flight upload — releases the multipart's parts in the bucket, not just the row."""
    svc = build_files(request)
    try:
        sess = await svc.abort_session(session_id, tenant=principal.tenant or "",
                                       owner=principal.subject)
    except NotFound:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "NOT_FOUND")
    return sess.public_dict()


@router.get("", dependencies=[Depends(rate_limit)])
async def list_files(request: Request, principal: Principal = Depends(require_user),
                     limit: int = Query(default=100, ge=1, le=1000),
                     after: str | None = Query(default=None, max_length=512)) -> dict:
    """This tenant's files, newest first, keyset-paged.

    The tenant is taken from the token, never from a parameter, and the filter is pushed into
    the datastore: the cost of this call is the size of the PAGE, not the size of the table.

    `limit` is clamped server-side; `after` is the opaque cursor from the previous response.
    A cursor is not an offset — an offset silently skips a row when a file is uploaded between
    two pages.
    """
    svc = build_files(request)
    return await svc.list(principal.tenant or "", limit=limit, after=after)


@router.get("/{file_id}/metadata")
async def metadata(request: Request, file_id: str, principal: Principal = Depends(_READ),
                   guard=Depends(get_guard)) -> dict:
    f = await _load_scoped(build_files(request), file_id, principal)
    async with guard.step("respond") as step:
        body = f.to_dict()
        step.result = body
    return body


@router.get("/{file_id}")
async def download(request: Request, file_id: str, principal: Principal = Depends(_READ)):
    """Serve the file. On a presign-capable store the bytes come STRAIGHT from the bucket via a
    short-lived redirect (the app is never in the data path); otherwise they stream through us."""
    svc = build_files(request)
    f = await _load_scoped(svc, file_id, principal)
    redirect = await svc.download_redirect(f)
    if redirect is not None:
        return RedirectResponse(redirect, status_code=status.HTTP_302_FOUND)
    _, data = await svc.download(file_id)
    return Response(content=data, media_type=f.content_type,
                    headers={"Content-Disposition": f'attachment; filename="{_safe_filename(f.filename)}"'})


@router.post("/{file_id}/signed-url")
async def signed_url(request: Request, file_id: str, principal: Principal = Depends(_READ)) -> dict:
    svc = build_files(request)
    await _load_scoped(svc, file_id, principal)
    token = svc.signed_token(file_id)
    return {"url": f"/files/signed/{token}", "expires_in": get_settings().SIGNED_URL_TTL_SECONDS}


@router.delete("/{file_id}", dependencies=[Depends(_UPLOAD)])
async def delete(request: Request, file_id: str, principal: Principal = Depends(require_user)) -> dict:
    svc = build_files(request)
    await _load_scoped(svc, file_id, principal)
    return await svc.delete(file_id)


# ── Public: signed URL (no auth — the token IS the grant) ────────────────────
@public_router.get("/files/signed/{token}")
async def signed_download(request: Request, token: str):
    svc = build_files(request)
    f = await svc.resolve_signed_file(token)
    if f is None:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "FORBIDDEN")
    # On a presign-capable store, hand the client a short-lived bucket URL rather than proxying the
    # bytes — a large shared file is served by the bucket, not by us.
    redirect = await svc.download_redirect(f)
    if redirect is not None:
        return RedirectResponse(redirect, status_code=status.HTTP_302_FOUND,
                                headers={"X-File-Access": "signed"})
    _, data = await svc.download(f.id)
    return Response(content=data, media_type=f.content_type,
                    headers={"Content-Disposition": f'attachment; filename="{_safe_filename(f.filename)}"',
                             "X-File-Access": "signed"})
