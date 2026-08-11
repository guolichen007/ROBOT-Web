from __future__ import annotations

from typing import Any

from fastapi import HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse


class PlatformError(Exception):
    def __init__(
        self,
        code: str,
        message: str,
        *,
        status_code: int = 409,
        details: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.status_code = status_code
        self.details = details or {}


def error_response(
    request: Request,
    *,
    code: str,
    message: str,
    status_code: int,
    details: dict[str, Any] | None = None,
) -> JSONResponse:
    return JSONResponse(
        status_code=status_code,
        content={
            "error": {
                "code": code,
                "message": message,
                "request_id": getattr(request.state, "request_id", None),
                "details": details or {},
            }
        },
    )


async def platform_error_handler(request: Request, exc: Exception) -> JSONResponse:
    assert isinstance(exc, PlatformError)
    return error_response(
        request,
        code=exc.code,
        message=exc.message,
        status_code=exc.status_code,
        details=exc.details,
    )


async def http_error_handler(request: Request, exc: Exception) -> JSONResponse:
    assert isinstance(exc, HTTPException)
    if isinstance(exc.detail, dict):
        code = str(exc.detail.get("code", "API_ERROR"))
        message = str(exc.detail.get("message", "请求失败"))
        details: dict[str, Any] = dict(exc.detail.get("details", {}))
    else:
        code = {
            401: "AUTH_REQUIRED",
            403: "PERMISSION_DENIED",
            404: "RESOURCE_NOT_FOUND",
            409: "REQUEST_CONFLICT",
            429: "RATE_LIMITED",
        }.get(exc.status_code, "API_ERROR")
        message = str(exc.detail)
        details = {}
    return error_response(
        request, code=code, message=message, status_code=exc.status_code, details=details
    )


async def validation_error_handler(request: Request, exc: Exception) -> JSONResponse:
    assert isinstance(exc, RequestValidationError)
    return error_response(
        request,
        code="INVALID_REQUEST",
        message="请求参数校验失败",
        status_code=422,
        details={"errors": exc.errors()},
    )
