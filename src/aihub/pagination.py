"""Keyset pagination cursors.

Offset pagination skips or repeats rows when items are inserted mid-scan. The
cursor is an opaque base64url payload holding the last seen sort key; clients
pass it back untouched.
"""

from __future__ import annotations

import base64
import json
from typing import Any, Dict, Optional

from .errors import InvalidRequest


def encode_cursor(payload: Dict[str, Any]) -> str:
    raw = json.dumps(payload, separators=(",", ":"), sort_keys=True).encode("utf-8")
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


MAX_CURSOR_CHARS = 512


def decode_cursor(cursor: Optional[str]) -> Optional[Dict[str, Any]]:
    if not cursor:
        return None
    if len(cursor) > MAX_CURSOR_CHARS:
        raise InvalidRequest("malformed cursor", field="cursor")
    padded = cursor + "=" * (-len(cursor) % 4)
    try:
        raw = base64.urlsafe_b64decode(padded.encode("ascii"))
        value = json.loads(raw.decode("utf-8"))
    except Exception:
        raise InvalidRequest("malformed cursor", field="cursor")
    if not isinstance(value, dict):
        raise InvalidRequest("malformed cursor", field="cursor")
    seq = value.get("seq")
    # bool is a subclass of int, and a string seq would make `seq < ?` match
    # every row under SQLite's type ordering, looping the caller forever.
    if not isinstance(seq, int) or isinstance(seq, bool) or seq < 0:
        raise InvalidRequest("malformed cursor", field="cursor")
    return value


def clamp_limit(limit: Optional[int], default: int = 20, maximum: int = 100) -> int:
    if limit is None:
        return default
    if limit < 1 or limit > maximum:
        raise InvalidRequest(
            "limit must be between 1 and %d" % maximum, field="limit"
        )
    return limit
