"""Shared-secret authentication.

Every endpoint except ``GET /health`` requires the ``X-AIHub-Token`` header.
Comparison uses ``hmac.compare_digest`` so a wrong token cannot be recovered by
timing the response.
"""

from __future__ import annotations

import hmac

from fastapi import Request

from .errors import Unauthorized

HEADER_NAME = "X-AIHub-Token"


def check_token(request: Request) -> None:
    cfg = request.app.state.config
    if not cfg.auth_enabled:
        return
    supplied = request.headers.get(HEADER_NAME) or ""
    if not supplied:
        auth = request.headers.get("Authorization") or ""
        if auth.lower().startswith("bearer "):
            supplied = auth[7:].strip()
    if not supplied or not hmac.compare_digest(supplied, cfg.token):
        raise Unauthorized("missing or invalid %s" % HEADER_NAME)


async def require_token(request: Request) -> None:
    """FastAPI dependency form of :func:`check_token`."""
    check_token(request)
