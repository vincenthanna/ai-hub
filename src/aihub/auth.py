"""Shared-secret authentication.

The ASGI middleware in ``app.py`` is what actually gates requests, ahead of any
body buffering. This dependency stays as a second layer so a router mounted
outside the middleware still refuses anonymous callers. Comparison uses
``hmac.compare_digest`` so a wrong token cannot be recovered by timing.
"""

from __future__ import annotations

import hmac

from fastapi import Request

from .config import accepted_tokens
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
    for candidate in accepted_tokens(cfg):
        try:
            if supplied and hmac.compare_digest(supplied, candidate):
                return
        except TypeError:
            # compare_digest rejects non-ASCII str; a bad header must be a 401,
            # not a 500 with a stack trace in the log for every attempt.
            break
    raise Unauthorized("missing or invalid %s" % HEADER_NAME)


async def require_token(request: Request) -> None:
    """FastAPI dependency form of :func:`check_token`."""
    check_token(request)
