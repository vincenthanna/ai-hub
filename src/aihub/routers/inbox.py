"""Per-recipient inbox polling and acknowledgement."""

from __future__ import annotations

import asyncio
from typing import Any, Dict, Optional

from fastapi import APIRouter, Depends, Query, Request

from ..auth import require_token
from ..errors import InvalidRequest
from ..models import AckRequest, normalize_label
from ..pagination import clamp_limit
from .items import _split

router = APIRouter(prefix="/v1", dependencies=[Depends(require_token)])

MAX_WAIT_SEC = 25.0
POLL_INTERVAL_SEC = 0.5


@router.get("/inbox")
async def inbox(
    request: Request,
    recipient: str = Query(alias="as"),
    limit: Optional[int] = None,
    include_broadcast: bool = True,
    kind: Optional[str] = None,
    topics: Optional[str] = None,
    wait_sec: float = 0.0,
) -> Dict[str, Any]:
    repo = request.app.state.repo
    try:
        label = normalize_label(recipient)
    except ValueError as exc:
        raise InvalidRequest(str(exc), field="as")
    n = clamp_limit(limit)

    # Registering the label also pins its broadcast watermark at the current
    # head, so a brand-new session does not inherit the whole backlog.
    async with request.app.state.db.write_lock:
        repo.ensure_agent(label)

    result = repo.inbox(
        label,
        limit=n,
        include_broadcast=include_broadcast,
        kinds=_split(kind),
        topics=_split(topics),
    )
    if result["items"] or wait_sec <= 0:
        return result

    deadline = asyncio.get_event_loop().time() + min(float(wait_sec), MAX_WAIT_SEC)
    while asyncio.get_event_loop().time() < deadline:
        await asyncio.sleep(POLL_INTERVAL_SEC)
        result = repo.inbox(
            label,
            limit=n,
            include_broadcast=include_broadcast,
            kinds=_split(kind),
            topics=_split(topics),
        )
        if result["items"]:
            break
    return result


@router.post("/inbox/ack")
async def ack(request: Request, payload: AckRequest) -> Dict[str, Any]:
    if not payload.item_ids and payload.broadcast_upto_seq is None and not payload.all:
        raise InvalidRequest(
            "provide item_ids, broadcast_upto_seq, or all=true", field="item_ids"
        )
    async with request.app.state.db.write_lock:
        return request.app.state.repo.ack(
            payload.recipient,
            payload.item_ids,
            payload.broadcast_upto_seq,
            note=payload.note,
            ack_all=payload.all,
        )


@router.get("/agents")
async def agents(request: Request) -> Dict[str, Any]:
    return {"agents": request.app.state.repo.agents()}
