# =============================================================================
# identity-service — Safe error handling
# -----------------------------------------------------------------------------
# Clients receive GENERIC error codes only — never an exception message or stack
# trace (CWE-209: information exposure through error messages). Full detail is
# logged server-side (with redaction) under the request id.
# =============================================================================
from __future__ import annotations

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.responses import JSONResponse

from app.config import get_settings
from app.core.audit import AuditUnavailable
from app.core.auth_service import AuthService
from app.core.caps_grant import CapsNotGrantable
from app.ports.legal_gate import LegalGateUnavailable
from app.ports.tenant_gate import TenantGateUnavailable
from app.middleware.request_context import get_request_id
from app.middleware.security import apply_security_headers, build_security_headers
from app.observability.logging import get_logger

_logger = get_logger("app.errors")

# Map well-known status codes to stable, generic client-facing error codes.
# Plain integers (not status.* constants) to avoid deprecation/version coupling.
_CODE_BY_STATUS: dict[int, str] = {
    400: "BAD_REQUEST",
    401: "UNAUTHORIZED",
    403: "FORBIDDEN",
    404: "NOT_FOUND",
    405: "METHOD_NOT_ALLOWED",
    408: "REQUEST_TIMEOUT",
    413: "PAYLOAD_TOO_LARGE",
    422: "VALIDATION_ERROR",
    429: "RATE_LIMITED",
}


def register_error_handlers(app: FastAPI) -> None:
    settings = get_settings()
    _sec_headers = build_security_headers(
        frame_ancestors=settings.ALLOWED_FRAME_ANCESTORS,
        is_production=settings.is_production,
    )

    def _json(code: str, status_code: int) -> JSONResponse:
        body: dict[str, str] = {"error": code}
        rid = get_request_id()
        if rid:
            body["request_id"] = rid
        resp = JSONResponse(body, status_code=status_code)
        # Catch-all 500s are produced OUTSIDE the header middleware, so apply the
        # hardened headers here too — every response is hardened, no exceptions.
        apply_security_headers(resp, _sec_headers)
        return resp

    @app.exception_handler(StarletteHTTPException)
    async def _http_exc(request: Request, exc: StarletteHTTPException):
        code = _CODE_BY_STATUS.get(exc.status_code, "ERROR")
        if exc.status_code >= 500:
            _logger.error("http_exception", extra={"status": exc.status_code})
            code = "INTERNAL_ERROR"
        return _json(code, exc.status_code)

    @app.exception_handler(CapsNotGrantable)
    async def _caps_not_grantable(request: Request, exc: CapsNotGrantable):
        # A DELIBERATE exception to the generic-code rule: the refusal must NAME the cap the caller
        # may not grant, or a silent-shaped answer teaches them it worked. Naming it leaks nothing —
        # it is a value the caller themselves supplied. `off` (roles-only) is a 400 (the feature is
        # disabled); a subset/allowlist miss is a 403 (this caller may not grant THAT capability).
        disabled = exc.policy == "off"
        body: dict[str, str] = {"error": "DIRECT_CAPS_DISABLED" if disabled else "CAPS_NOT_GRANTABLE"}
        if not disabled:
            body["cap"] = exc.cap
        rid = get_request_id()
        if rid:
            body["request_id"] = rid
        resp = JSONResponse(body, status_code=400 if disabled else 403)
        apply_security_headers(resp, _sec_headers)
        return resp

    @app.exception_handler(AuthService.LegalAcceptanceRequired)
    async def _legal_required(request: Request, exc: AuthService.LegalAcceptanceRequired):
        # DELIBERATE exception to the generic-code rule, like _caps_not_grantable: block mode must
        # tell the caller WHAT it owes and hand back the restricted accept token, or the block is a
        # dead end (no token → cannot accept → cannot ever log in). Neither field is a leak: the
        # outstanding docs are the tenant's own published legal, and the accept token is minted FOR
        # this caller, who just proved their credentials, and can do nothing but accept.
        body: dict = {"error": "LEGAL_ACCEPTANCE_REQUIRED",
                      "outstanding": exc.outstanding, "accept_token": exc.accept_token}
        rid = get_request_id()
        if rid:
            body["request_id"] = rid
        resp = JSONResponse(body, status_code=403)
        apply_security_headers(resp, _sec_headers)
        return resp

    @app.exception_handler(AuthService.TenantInactive)
    async def _tenant_inactive(request: Request, exc: AuthService.TenantInactive):
        # A suspended/deleted tenant mints nothing. 403 + a named code, no detail: which tenant and
        # what status is operational, not for the error body handed to the caller.
        _logger.warning("tenant_inactive_mint_refused",
                        extra={"path": request.url.path, "status": exc.tenant_status})
        return _json("TENANT_INACTIVE", 403)

    @app.exception_handler(TenantGateUnavailable)
    async def _tenant_gate_unavailable(request: Request, exc: TenantGateUnavailable):
        # fail-closed tenant mode with tenant-service down. 503 (retryable), named, no detail.
        _logger.error("tenant_gate_unavailable", extra={"path": request.url.path})
        return _json("TENANT_GATE_UNAVAILABLE", 503)

    @app.exception_handler(LegalGateUnavailable)
    async def _legal_unavailable(request: Request, exc: LegalGateUnavailable):
        # fail-closed legal mode with compliance down. 503 (retryable), a named code, and NO detail:
        # that the authority is down is operational; who tried to log in is not for the error body.
        _logger.error("legal_gate_unavailable", extra={"path": request.url.path})
        return _json("LEGAL_GATE_UNAVAILABLE", 503)

    @app.exception_handler(RequestValidationError)
    async def _validation_exc(request: Request, exc: RequestValidationError):
        # Do NOT echo the raw invalid input back (avoid reflected data / leaks).
        return _json("VALIDATION_ERROR", 422)

    @app.exception_handler(AuditUnavailable)
    async def _audit_unavailable(request: Request, exc: AuditUnavailable):
        # The fail-closed path, made legible to the caller. Without this the exception fell to the
        # catch-all below and surfaced as 500 INTERNAL_ERROR — telling a client "we crashed" when
        # the truth is "this action requires an audit record and the log is unreachable". Those
        # are different diagnoses, different on-call responses, and different retry strategies:
        # 503 is retryable, 500 is not.
        #
        # The body carries no action detail. That the log is down is operational; WHICH privileged
        # action someone attempted is not something an error body should reveal.
        _logger.error("audit_unavailable", extra={"path": request.url.path})
        resp = _json("AUDIT_UNAVAILABLE", 503)
        resp.headers["Retry-After"] = str(settings.AUDIT_RETRY_AFTER_SECONDS)
        return resp

    @app.exception_handler(Exception)
    async def _unhandled(request: Request, exc: Exception):
        # Log full type server-side; return a generic body to the client.
        _logger.error("unhandled_exception", extra={"exc_type": type(exc).__name__})
        return _json("INTERNAL_ERROR", 500)
