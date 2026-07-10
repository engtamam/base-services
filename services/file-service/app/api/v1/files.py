# =============================================================================
# file-service — upload / download / metadata / signed-url / delete
# =============================================================================
from __future__ import annotations

from fastapi import APIRouter, Depends, Header, HTTPException, Request, status, Query
from starlette.responses import Response

from app.api.auth import Principal, require_user
from app.api.authz import require_capability
from app.api.deps import get_guard
from app.api.ratelimit import rate_limit
from app.config import get_settings
from app.core.file_repository import FileRepository
from app.core.file_service import FileService, TooLarge, TypeNotAllowed
from app.core.models import FileObject
from app.ports.errors import NotFound
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
    )


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
    data = await request.body()
    content_type = request.headers.get("content-type", "application/octet-stream")
    svc = build_files(request)
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
async def download(request: Request, file_id: str, principal: Principal = Depends(_READ)) -> Response:
    svc = build_files(request)
    f = await _load_scoped(svc, file_id, principal)
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
async def signed_download(request: Request, token: str) -> Response:
    result = await build_files(request).resolve_signed(token)
    if result is None:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "FORBIDDEN")
    f, data = result
    return Response(content=data, media_type=f.content_type,
                    headers={"Content-Disposition": f'attachment; filename="{_safe_filename(f.filename)}"',
                             "X-File-Access": "signed"})
