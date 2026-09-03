"""Health endpoint.

The unauthenticated response says only whether the server is up. Item counts,
disk figures and the auth flag are operational detail: on a LAN-exposed port
they tell a scanner how much data is here and whether the door is open.
"""

from __future__ import annotations

import hmac
import shutil
import time

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from .. import __version__
from ..config import MIN_FREE_BYTES, accepted_tokens

router = APIRouter()


def _has_token(request: Request) -> bool:
    cfg = request.app.state.config
    if not cfg.auth_enabled:
        return True
    supplied = request.headers.get("X-AIHub-Token") or ""
    if not supplied:
        auth = request.headers.get("Authorization") or ""
        if auth[:7].lower() == "bearer ":
            supplied = auth[7:].strip()
    if not supplied:
        return False
    for candidate in accepted_tokens(cfg):
        try:
            if hmac.compare_digest(supplied, candidate):
                return True
        except TypeError:
            return False
    return False


@router.get("/health")
async def health(request: Request) -> JSONResponse:
    cfg = request.app.state.config
    store = getattr(request.app.state, "store", None)

    db_info = {"ok": True, "journal_mode": None, "items": 0, "size_bytes": 0}
    if store is not None:
        try:
            db_info = store.health()
        except Exception as exc:  # pragma: no cover - defensive
            db_info = {"ok": False, "error": str(exc)}
    elif cfg.db_path.exists():
        db_info["size_bytes"] = cfg.db_path.stat().st_size

    try:
        free_bytes = shutil.disk_usage(str(cfg.home)).free
    except OSError:
        free_bytes = 0

    classifier = getattr(request.app.state, "classifier_status", None) or {
        "ok": True, "engine": "unknown", "queue_depth": 0, "inflight": 0,
    }
    healthy = bool(db_info.get("ok")) and free_bytes >= MIN_FREE_BYTES
    status = "ok" if healthy else "degraded"

    if not _has_token(request):
        return JSONResponse(status_code=200 if healthy else 503, content={"status": status})

    started_at = request.app.state.started_at
    return JSONResponse(
        status_code=200 if healthy else 503,
        content={
            "status": status,
            "version": __version__,
            "started_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(started_at)),
            "uptime_sec": int(time.time() - started_at),
            "db": db_info,
            "blobs": {"free_bytes": free_bytes, "min_free_bytes": MIN_FREE_BYTES},
            "classifier": classifier,
            "auth_enabled": cfg.auth_enabled,
            "schema_version": getattr(request.app.state, "schema_version", None),
        },
    )
