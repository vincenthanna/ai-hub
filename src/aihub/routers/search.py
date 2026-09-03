"""Keyword search and topic catalogue."""

from __future__ import annotations

import time
from typing import Any, Dict, Optional

from fastapi import APIRouter, Depends, Query, Request

from ..auth import require_token
from ..pagination import clamp_limit
from .items import _parse_ts, _split

router = APIRouter(prefix="/v1", dependencies=[Depends(require_token)])

MAX_RESPONSE_BYTES = 64 * 1024


@router.get("/search")
async def search(
    request: Request,
    q: str = "",
    limit: Optional[int] = None,
    offset: int = 0,
    topic: Optional[str] = None,
    tags: Optional[str] = None,
    kind: Optional[str] = None,
    sender: Optional[str] = Query(default=None, alias="from"),
    recipient: Optional[str] = Query(default=None, alias="to"),
    since: Optional[str] = None,
    until: Optional[str] = None,
) -> Dict[str, Any]:
    repo = request.app.state.repo
    n = clamp_limit(limit)
    started = time.perf_counter()
    items, has_more = repo.search(
        q,
        limit=n,
        offset=max(0, offset),
        topic=topic,
        tags=_split(tags),
        kinds=_split(kind),
        sender=sender,
        recipient=recipient,
        since_ms=_parse_ts(since, "since"),
        until_ms=_parse_ts(until, "until"),
    )
    truncated = False
    # Keep the payload small enough that a Claude session can read it.
    while items and len(str(items).encode("utf-8")) > MAX_RESPONSE_BYTES:
        items.pop()
        truncated = True
    return {
        "query": {"q": q, "topic": topic, "tags": _split(tags)},
        "items": items,
        "has_more": has_more or truncated,
        "truncated": truncated,
        "took_ms": round((time.perf_counter() - started) * 1000, 2),
    }


@router.get("/topics")
async def topics(request: Request) -> Dict[str, Any]:
    rows = request.app.state.repo.topics()
    return {"topics": rows, "total_items": sum(r["count"] for r in rows)}
