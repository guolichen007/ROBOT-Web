from __future__ import annotations

import logging
import time
from pathlib import Path
from uuid import uuid4

from fastapi import FastAPI, HTTPException, Request, WebSocket
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles

from app.core.config import get_settings
from app.core.errors import (
    PlatformError,
    http_error_handler,
    platform_error_handler,
    validation_error_handler,
)
from app.core.logging import configure_logging
from app.core.websocket import monitor_socket
from app.modules.admin.router import router as admin_router
from app.modules.alarms.router import router as alarms_router
from app.modules.auth.router import router as auth_router
from app.modules.commands.router import router as commands_router
from app.modules.enrollment.router import router as enrollment_router
from app.modules.history.router import router as history_router
from app.modules.maps.router import router as maps_router
from app.modules.media.router import router as media_router
from app.modules.operations.router import router as operations_router
from app.modules.reports.router import router as reports_router
from app.modules.robots.router import router as robots_router
from app.modules.system.router import router as system_router
from app.modules.tasks.router import router as tasks_router

settings = get_settings()
configure_logging("api")
logger = logging.getLogger(__name__)

app = FastAPI(
    title=settings.app_name,
    version="2.0.0-baseline",
    docs_url="/api/docs" if settings.enable_api_docs else None,
    openapi_url="/api/openapi.json" if settings.enable_api_docs else None,
)
app.add_exception_handler(PlatformError, platform_error_handler)
app.add_exception_handler(HTTPException, http_error_handler)
app.add_exception_handler(RequestValidationError, validation_error_handler)
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.origins,
    allow_credentials=True,
    allow_methods=["GET", "POST", "PUT", "DELETE", "OPTIONS"],
    allow_headers=[
        "Authorization",
        "Content-Type",
        "Idempotency-Key",
        "X-CSRF-Token",
        "X-Request-ID",
    ],
)


@app.middleware("http")
async def request_context(request: Request, call_next):
    request_id = request.headers.get("x-request-id") or str(uuid4())
    request.state.request_id = request_id
    started = time.perf_counter()
    try:
        response = await call_next(request)
    except Exception:
        logger.exception("unhandled request error", extra={"request_id": request_id})
        return JSONResponse(
            status_code=500,
            content={
                "error": {
                    "code": "INTERNAL_ERROR",
                    "message": "服务器内部错误",
                    "request_id": request_id,
                    "details": {},
                }
            },
        )
    response.headers["X-Request-ID"] = request_id
    response.headers["X-Response-Time-Ms"] = f"{(time.perf_counter() - started) * 1000:.2f}"
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["Referrer-Policy"] = "same-origin"
    return response


for router in (
    system_router,
    auth_router,
    robots_router,
    commands_router,
    maps_router,
    tasks_router,
    alarms_router,
    history_router,
    admin_router,
    media_router,
    operations_router,
    reports_router,
    enrollment_router,
):
    app.include_router(router)

asset_root = Path(settings.asset_root)
asset_root.mkdir(parents=True, exist_ok=True)
app.mount("/assets", StaticFiles(directory=asset_root), name="assets")


@app.websocket("/ws/v1/monitor")
async def websocket_monitor(websocket: WebSocket, ticket: str, after: str = "0-0") -> None:
    await monitor_socket(websocket, ticket, after)
