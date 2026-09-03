"""Uniform error envelope for every ai-hub HTTP response."""

from __future__ import annotations

from typing import Any, Dict, Optional

from fastapi import Request
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException


class AIHubError(Exception):
    """Domain error carrying an HTTP status and a stable machine-readable code."""

    status_code = 500
    code = "internal_error"

    def __init__(
        self,
        message: str,
        *,
        field: Optional[str] = None,
        retry_after_sec: Optional[int] = None,
        status_code: Optional[int] = None,
        code: Optional[str] = None,
    ) -> None:
        super().__init__(message)
        self.message = message
        self.field = field
        self.retry_after_sec = retry_after_sec
        if status_code is not None:
            self.status_code = status_code
        if code is not None:
            self.code = code


class InvalidRequest(AIHubError):
    status_code = 400
    code = "invalid_request"


class Unauthorized(AIHubError):
    status_code = 401
    code = "unauthorized"


class NotFound(AIHubError):
    status_code = 404
    code = "not_found"


class Conflict(AIHubError):
    status_code = 409
    code = "conflict"


class PayloadTooLarge(AIHubError):
    status_code = 413
    code = "payload_too_large"


class StorageUnavailable(AIHubError):
    status_code = 503
    code = "storage_unavailable"


def envelope(
    code: str,
    message: str,
    *,
    request_id: str = "",
    field: Optional[str] = None,
    retry_after_sec: Optional[int] = None,
    extra: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    body: Dict[str, Any] = {
        "error": {
            "code": code,
            "message": message,
            "field": field,
            "request_id": request_id,
            "retry_after_sec": retry_after_sec,
        }
    }
    if extra:
        body["error"].update(extra)
    return body


def _request_id(request: Request) -> str:
    return getattr(request.state, "request_id", "") or ""


async def aihub_error_handler(request: Request, exc: AIHubError) -> JSONResponse:
    return JSONResponse(
        status_code=exc.status_code,
        content=envelope(
            exc.code,
            exc.message,
            request_id=_request_id(request),
            field=exc.field,
            retry_after_sec=exc.retry_after_sec,
        ),
    )


_STATUS_CODES = {
    400: "invalid_request",
    401: "unauthorized",
    403: "forbidden",
    404: "not_found",
    405: "method_not_allowed",
    409: "conflict",
    422: "validation_failed",
    413: "payload_too_large",
    415: "unsupported_media_type",
    429: "rate_limited",
    503: "storage_unavailable",
}


async def http_error_handler(request: Request, exc: StarletteHTTPException) -> JSONResponse:
    code = _STATUS_CODES.get(exc.status_code, "internal_error")
    return JSONResponse(
        status_code=exc.status_code,
        content=envelope(code, str(exc.detail), request_id=_request_id(request)),
        headers=getattr(exc, "headers", None),
    )


async def validation_error_handler(
    request: Request, exc: RequestValidationError
) -> JSONResponse:
    # Pydantic includes the offending input value; echoing it back would put
    # request bodies into logs and responses.
    errors = [
        {k: v for k, v in item.items() if k != "input"}
        for item in jsonable_encoder(exc.errors())
    ]
    first_field = None
    if errors:
        loc = errors[0].get("loc") or []
        first_field = ".".join(str(p) for p in loc if p != "body") or None
    return JSONResponse(
        status_code=422,
        content=envelope(
            "validation_failed",
            "request validation failed",
            request_id=_request_id(request),
            field=first_field,
            extra={"details": errors[:10]},
        ),
    )


async def unhandled_error_handler(request: Request, exc: Exception) -> JSONResponse:
    return JSONResponse(
        status_code=500,
        content=envelope(
            "internal_error", "internal server error", request_id=_request_id(request)
        ),
    )
