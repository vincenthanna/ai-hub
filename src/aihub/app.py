"""FastAPI application factory and lifespan wiring."""

from __future__ import annotations

import contextlib
import hmac
import logging
import os
import time
import uuid
from typing import Any, AsyncIterator, Dict, Optional

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from . import __version__
from .config import Config, assert_safe_to_serve, load_config
from .errors import (
    AIHubError,
    PayloadTooLarge,
    aihub_error_handler,
    http_error_handler,
    unhandled_error_handler,
    validation_error_handler,
)
from .logging_setup import request_id_var, setup_logging

log = logging.getLogger("aihub.app")


class GateMiddleware:
    """Authenticate and bound the request body before anything is buffered.

    Authentication lives here rather than as a router dependency for two
    reasons. FastAPI reads the request body before it resolves dependencies, so
    a dependency-based check lets an unauthenticated caller push megabytes into
    the process first. And a middleware denies by default, so a router added
    later cannot become publicly writable by forgetting a dependency.
    """

    #: Only these exact paths may be reached without a token.
    PUBLIC_PATHS = frozenset({"/health"})

    def __init__(self, app, config) -> None:
        self.app = app
        self.config = config

    @staticmethod
    async def _deny(scope, receive, send, status: int, code: str, message: str) -> None:
        # Denials happen before the request-context middleware runs, so this
        # layer mints the id itself; every response carries one.
        rid = "req_" + uuid.uuid4().hex[:12]
        response = JSONResponse(
            status_code=status,
            content={
                "error": {
                    "code": code,
                    "message": message,
                    "field": None,
                    "request_id": rid,
                    "retry_after_sec": None,
                }
            },
            headers={"X-Request-Id": rid},
        )
        await response(scope, receive, send)

    def _authorized(self, headers: Dict[bytes, bytes]) -> bool:
        if not self.config.auth_enabled:
            return True
        supplied = headers.get(b"x-aihub-token", b"").decode("latin-1")
        if not supplied:
            auth = headers.get(b"authorization", b"").decode("latin-1")
            if auth[:7].lower() == "bearer ":
                supplied = auth[7:].strip()
        if not supplied:
            return False
        try:
            return hmac.compare_digest(supplied, self.config.token)
        except TypeError:
            return False

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        headers = {name.lower(): value for name, value in scope.get("headers", [])}
        path = scope.get("path", "")
        if path not in self.PUBLIC_PATHS and not self._authorized(headers):
            await self._deny(
                scope, receive, send, 401, "unauthorized",
                "missing or invalid X-AIHub-Token",
            )
            return

        limit = self.config.max_request_bytes
        declared = headers.get(b"content-length")
        if declared is not None:
            try:
                if int(declared) > limit:
                    await self._deny(
                        scope, receive, send, 413, "payload_too_large",
                        "request body exceeds %d bytes" % limit,
                    )
                    return
            except ValueError:
                pass

        # Count what actually arrives: a chunked request carries no
        # Content-Length, so the declared-size check above cannot be the only one.
        received = {"n": 0}

        async def counting_receive():
            message = await receive()
            if message["type"] == "http.request":
                received["n"] += len(message.get("body", b""))
                if received["n"] > limit:
                    raise PayloadTooLarge("request body exceeds %d bytes" % limit)
            return message

        await self.app(scope, counting_receive, send)


@contextlib.asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    """Open storage, apply migrations, then start the classification worker."""
    from .storage.blobs import BlobStore
    from .storage.db import Database
    from .storage.migrate import migrate
    from .storage.repo import Repo

    cfg = app.state.config
    db = Database(cfg.db_path)
    version = migrate(db.writer)
    blobs = BlobStore(cfg.blobs_dir)
    repo = Repo(db, blobs)
    app.state.db = db
    app.state.blobs = blobs
    app.state.repo = repo
    app.state.store = repo
    app.state.schema_version = version
    log.info(
        "storage ready",
        extra={"db_path": str(cfg.db_path), "schema_version": version, "home": str(cfg.home)},
    )

    worker = None
    if cfg.classify.enabled:
        try:
            from .classify.worker import ClassifyWorker

            worker = ClassifyWorker(app)
            await worker.start()
            app.state.classify_worker = worker
        except Exception as exc:  # pragma: no cover - the worker is optional
            log.warning("classification worker not started", extra={"reason": str(exc)})
            app.state.classify_worker = None
    else:
        app.state.classify_worker = None
        app.state.classifier_status = {
            "ok": True, "engine": "disabled", "queue_depth": 0, "inflight": 0,
        }
    try:
        yield
    finally:
        if worker is not None:
            await worker.stop()
        db.close()
        log.info("storage closed")


def _docs_enabled() -> bool:
    return (os.environ.get("AIHUB_DOCS") or "").strip().lower() in {"1", "true", "yes", "on"}


def create_app(config: Optional[Config] = None) -> FastAPI:
    cfg = config or load_config()
    assert_safe_to_serve(cfg)
    cfg.ensure_dirs()
    setup_logging(cfg.logs_dir, cfg.log_level)

    app = FastAPI(
        title="ai-hub",
        version=__version__,
        # The OpenAPI routes are registered by FastAPI itself and sit outside
        # the router stack, so they are off unless explicitly switched on.
        docs_url="/docs" if _docs_enabled() else None,
        redoc_url=None,
        openapi_url="/openapi.json" if _docs_enabled() else None,
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

    app.add_middleware(GateMiddleware, config=cfg)
    return app


app = None  # populated by __main__ or uvicorn factory


def get_app() -> FastAPI:
    """Factory entry point for ``uvicorn aihub.app:get_app --factory``."""
    return create_app()
