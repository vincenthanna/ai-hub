"""FastAPI application factory and lifespan wiring."""

from __future__ import annotations

import contextlib
import logging
import time
import uuid
from typing import Any, AsyncIterator, Dict, Optional

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from . import __version__
from .config import Config, load_config
from .errors import (
    AIHubError,
    aihub_error_handler,
    http_error_handler,
    unhandled_error_handler,
    validation_error_handler,
)
from .logging_setup import request_id_var, setup_logging

log = logging.getLogger("aihub.app")


class BodySizeLimitMiddleware:
    """Reject oversized requests before they are buffered into memory."""

    def __init__(self, app, max_bytes: int) -> None:
        self.app = app
        self.max_bytes = max_bytes

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        for name, value in scope.get("headers", []):
            if name == b"content-length":
                try:
                    declared = int(value)
                except ValueError:
                    break
                if declared > self.max_bytes:
                    response = JSONResponse(
                        status_code=413,
                        content={
                            "error": {
                                "code": "payload_too_large",
                                "message": "request body exceeds %d bytes" % self.max_bytes,
                                "field": None,
                                "request_id": "",
                                "retry_after_sec": None,
                            }
                        },
                    )
                    await response(scope, receive, send)
                    return
                break
        await self.app(scope, receive, send)


@contextlib.asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    """Open storage, apply migrations, then start the classification worker."""
    from .storage.blobs import BlobStore
    from .storage.db import Database
    from .storage.migrate import migrate
    from .storage.repo import Repo

    cfg = app.state.config
    db = Database(cfg.db_path)
    version = migrate(db._writer)
    blobs = BlobStore(cfg.blobs_dir)
    repo = Repo(db, blobs)
    app.state.db = db
    app.state.blobs = blobs
    app.state.repo = repo
    app.state.store = repo
    app.state.schema_version = version
    log.info(
        "storage ready",
        extra={"db": str(cfg.db_path), "schema_version": version, "home": str(cfg.home)},
    )

    worker = None
    if cfg.classify.enabled:
        try:
            from .classify.worker import ClassifyWorker

            worker = ClassifyWorker(app)
            await worker.start()
            app.state.classify_worker = worker
        except Exception as exc:  # pragma: no cover - worker is optional
            log.warning("classification worker not started", extra={"reason": str(exc)})
            app.state.classify_worker = None
    else:
        app.state.classify_worker = None
        app.state.classifier_status = {"ok": True, "engine": "disabled", "queue_depth": 0,
                                       "inflight": 0}
    try:
        yield
    finally:
        if worker is not None:
            await worker.stop()
        db.close()
        log.info("storage closed")


def create_app(config: Optional[Config] = None) -> FastAPI:
    cfg = config or load_config()
    cfg.ensure_dirs()
    setup_logging(cfg.logs_dir, cfg.log_level)

    app = FastAPI(
        title="ai-hub",
        version=__version__,
        docs_url="/docs",
        redoc_url=None,
        openapi_url="/openapi.json",
        lifespan=lifespan,
    )
    app.state.config = cfg
    app.state.started_at = time.time()
    app.state.stats: Dict[str, Any] = {"requests": 0}

    @app.middleware("http")
    async def request_context(request: Request, call_next):
        rid = request.headers.get("X-Request-Id") or "req_" + uuid.uuid4().hex[:12]
        request.state.request_id = rid
        token = request_id_var.set(rid)
        started = time.perf_counter()
        try:
            response = await call_next(request)
        except AIHubError:
            raise
        finally:
            request_id_var.reset(token)
        duration_ms = (time.perf_counter() - started) * 1000.0
        response.headers["X-Request-Id"] = rid
        app.state.stats["requests"] += 1
        if request.url.path != "/health":
            log.info(
                "request",
                extra={
                    "method": request.method,
                    "path": request.url.path,
                    "status": response.status_code,
                    "dur_ms": round(duration_ms, 2),
                    "request_id": rid,
                },
            )
        return response

    app.add_exception_handler(AIHubError, aihub_error_handler)
    app.add_exception_handler(StarletteHTTPException, http_error_handler)
    app.add_exception_handler(RequestValidationError, validation_error_handler)
    app.add_exception_handler(Exception, unhandled_error_handler)

    from .routers import health, inbox, items, search

    app.include_router(health.router)
    app.include_router(items.router)
    app.include_router(search.router)
    app.include_router(inbox.router)

    app.add_middleware(BodySizeLimitMiddleware, max_bytes=cfg.max_request_bytes)
    return app


app = None  # populated by __main__ or uvicorn factory


def get_app() -> FastAPI:
    """Factory entry point for ``uvicorn aihub.app:get_app --factory``."""
    return create_app()
