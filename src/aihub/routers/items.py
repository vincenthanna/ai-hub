"""Item upload, listing, detail and attachment download."""

from __future__ import annotations

import json
import logging
import re
import shutil
from typing import Any, Dict, List, Optional
from urllib.parse import quote

from fastapi import APIRouter, Depends, Query, Request
from pydantic import ValidationError
from starlette.datastructures import UploadFile
from fastapi.responses import FileResponse, JSONResponse

from ..auth import require_token
from ..config import MIN_FREE_BYTES
from ..errors import (
    AIHubError,
    InvalidRequest,
    PayloadTooLarge,
    StorageUnavailable,
)
from ..models import ItemCreate
from ..pagination import clamp_limit, decode_cursor, encode_cursor
from ..storage.blobs import BlobTooLarge
from ..storage.repo import PreparedAttachment

log = logging.getLogger("aihub.routers.items")
router = APIRouter(prefix="/v1", dependencies=[Depends(require_token)])


def _parse_ts(value: Optional[str], field: str) -> Optional[int]:
    if not value:
        return None
    import datetime

    text = value.strip().replace("Z", "+00:00")
    try:
        dt = datetime.datetime.fromisoformat(text)
    except ValueError:
        raise InvalidRequest("invalid timestamp, expected RFC3339", field=field)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=datetime.timezone.utc)
    return int(dt.timestamp() * 1000)


def _split(value: Optional[str]) -> Optional[List[str]]:
    if not value:
        return None
    out = [part.strip() for part in value.split(",") if part.strip()]
    return out or None


@router.post("/items", status_code=201)
async def create_item(request: Request) -> JSONResponse:
    cfg = request.app.state.config
    repo = request.app.state.repo

    # The data root usually shares a volume with everything else on the host, so
    # running it dry is a whole-machine failure, not just a hub failure.
    try:
        free = shutil.disk_usage(str(cfg.home)).free
    except OSError:
        free = None
    if free is not None and free < MIN_FREE_BYTES:
        raise StorageUnavailable(
            "server is low on disk (%d bytes free); uploads are paused" % free
        )

    content_type = (request.headers.get("content-type") or "").split(";")[0].strip().lower()

    prepared: List[PreparedAttachment] = []
    if content_type == "multipart/form-data":
        form = await request.form()
        raw = form.get("payload")
        if raw is None:
            raise InvalidRequest("multipart upload requires a 'payload' part", field="payload")
        if isinstance(raw, UploadFile):
            raw = (await raw.read()).decode("utf-8")
        try:
            payload_dict = json.loads(raw)
        except json.JSONDecodeError:
            raise InvalidRequest("'payload' part is not valid JSON", field="payload")
        uploads = [v for k, v in form.multi_items() if k == "files" and isinstance(v, UploadFile)]
        if len(uploads) > 10:
            raise InvalidRequest("at most 10 attachments per item", field="files")
        for upload in uploads:
            try:
                digest, rel, size, _ = request.app.state.blobs.put_stream(
                    upload.file, max_bytes=cfg.max_file_bytes
                )
            except BlobTooLarge:
                raise PayloadTooLarge(
                    "attachment exceeds %d bytes" % cfg.max_file_bytes, field="files"
                )
            prepared.append(
                PreparedAttachment(
                    filename=(upload.filename or "attachment")[:255],
                    media_type=upload.content_type or "application/octet-stream",
                    sha256=digest,
                    rel_path=rel,
                    size_bytes=size,
                )
            )
    elif content_type == "application/json" or not content_type:
        payload_dict = await request.json()
    else:
        raise InvalidRequest("unsupported content type: %s" % content_type)

    if not isinstance(payload_dict, dict):
        raise InvalidRequest("request body must be a JSON object")
    try:
        payload = ItemCreate(**payload_dict)
    except ValidationError as exc:
        errors = exc.errors()
        loc = errors[0].get("loc") if errors else None
        field = ".".join(str(p) for p in (loc or [])) or None
        raise AIHubError(
            errors[0]["msg"] if errors else "validation failed",
            field=field,
            status_code=422,
            code="validation_failed",
        )
    body_bytes = len(payload.body.encode("utf-8"))
    if body_bytes > cfg.max_body_bytes:
        raise PayloadTooLarge("body exceeds %d bytes" % cfg.max_body_bytes, field="body")

    # Oversized bodies land in the blob store; the DB keeps only the pointer.
    body_rel_path = None
    body_sha = ""
    if body_bytes > 256 * 1024:
        body_sha, body_rel_path, _size, _dup = request.app.state.blobs.put_bytes(
            payload.body.encode("utf-8")
        )

    async with request.app.state.db.write_lock:
        item, deduplicated = repo.create_item(
            sender=payload.sender,
            to=payload.to or [],
            kind=payload.kind,
            title=payload.title,
            body=payload.body,
            topic=payload.topic,
            tags=payload.tags,
            repo=payload.repo,
            host=payload.host,
            ref=payload.ref,
            refs=[r.model_dump() for r in payload.refs],
            client_msg_id=payload.client_msg_id,
            priority=payload.priority,
            importance=payload.importance,
            attachments=prepared,
            body_rel_path=body_rel_path,
            body_sha256=body_sha,
        )

    worker = getattr(request.app.state, "classify_worker", None)
    if worker is not None and not deduplicated:
        worker.notify()

    body: Dict[str, Any] = {
        "item_id": item["item_id"],
        "seq": item["seq"],
        "created_at": item["created_at"],
        "from": item["sender"],
        "to": item["to"],
        "topic": item["topic"],
        "classification": {"status": item["classification"]},
        "attachments": item.get("attachments", []),
        "deduplicated": deduplicated,
    }
    return JSONResponse(status_code=200 if deduplicated else 201, content=body)


@router.get("/items")
async def list_items(
    request: Request,
    limit: Optional[int] = None,
    cursor: Optional[str] = None,
    topic: Optional[str] = None,
    kind: Optional[str] = None,
    sender: Optional[str] = Query(default=None, alias="from"),
    recipient: Optional[str] = Query(default=None, alias="to"),
    since: Optional[str] = None,
    until: Optional[str] = None,
    order: str = "desc",
) -> Dict[str, Any]:
    repo = request.app.state.repo
    n = clamp_limit(limit)
    payload = decode_cursor(cursor)
    after_seq = int(payload["seq"]) if payload and "seq" in payload else None
    items, next_seq = repo.list_items(
        limit=n,
        after_seq=after_seq,
        topic=topic,
        kinds=_split(kind),
        sender=sender,
        recipient=recipient,
        since_ms=_parse_ts(since, "since"),
        until_ms=_parse_ts(until, "until"),
        order="asc" if order == "asc" else "desc",
    )
    return {
        "items": items,
        "next_cursor": encode_cursor({"seq": next_seq}) if next_seq else None,
        "has_more": next_seq is not None,
    }


@router.get("/items/{item_id}")
async def get_item(request: Request, item_id: str) -> Dict[str, Any]:
    return request.app.state.repo.get_item(item_id)


@router.get("/items/{item_id}/attachments/{attachment_id}")
async def download_attachment(request: Request, item_id: str, attachment_id: str):
    repo = request.app.state.repo
    att = repo.get_attachment(item_id, attachment_id)
    path = request.app.state.blobs.abs_path(att["rel_path"])
    if not path.is_file():
        from ..errors import NotFound

        raise NotFound("attachment blob is missing on disk")
    # Serving an uploader-supplied content type from this origin would let an
    # .html attachment execute as a same-origin page. The declared type is
    # returned as metadata on the item instead.
    safe_name = re.sub(r'[^A-Za-z0-9._-]+', "_", att["filename"])[:120] or "attachment"
    return FileResponse(
        str(path),
        media_type="application/octet-stream",
        headers={
            "ETag": '"%s"' % att["sha256"],
            "X-AIHub-Sha256": att["sha256"],
            "X-AIHub-Media-Type": att["media_type"],
            "X-Content-Type-Options": "nosniff",
            "Content-Disposition": "attachment; filename=\"%s\"; filename*=UTF-8''%s"
            % (safe_name, quote(att["filename"], safe="")),
        },
    )
