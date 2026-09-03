"""Unauthenticated health endpoint."""

from __future__ import annotations

import shutil
import time

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from .. import __version__
from ..config import MIN_FREE_BYTES

router = APIRouter()


@router.get("/health")
async def health(request: Request) -> JSONResponse:
    cfg = request.app.state.config
    started_at = request.app.state.started_at

    db_info = {"ok": False, "journal_mode": None, "items": 0, "size_bytes": 0}
    store = getattr(request.app.state, "store", None)
    if store is not None:
        try:
            db_info = store.health()
        except Exception as exc:  # pragma: no cover - defensive
            db_info = {"ok": False, "error": str(exc)}
    else:
        # Phase 1: no storage layer yet, report the DB file if it exists.
        db_info["ok"] = True
        if cfg.db_path.exists():
            db_info["size_bytes"] = cfg.db_path.stat().st_size

    try:
        usage = shutil.disk_usage(str(cfg.home))
        free_bytes = usage.free
    except OSError:
        free_bytes = 0

    classifier = getattr(request.app.state, "classifier_status", None) or {
        "ok": True,
        "queue_depth": 0,
        "inflight": 0,
        "engine": "unknown",
    }

    healthy = bool(db_info.get("ok")) and free_bytes >= MIN_FREE_BYTES
    body = {
        "status": "ok" if healthy else "degraded",
        "version": __version__,
        "started_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(started_at)),
        "uptime_sec": int(time.time() - started_at),
        "db": db_info,
        "blobs": {"free_bytes": free_bytes},
        "classifier": classifier,
        "auth_enabled": cfg.auth_enabled,
    }
    return JSONResponse(status_code=200 if healthy else 503, content=body)
