"""Sortable identifier generation.

Item identifiers are ULIDs: a 48-bit millisecond timestamp followed by 80 bits
of randomness, rendered as 26 Crockford base32 characters. Lexicographic order
matches chronological order, so ``ORDER BY item_id`` needs no extra index and
B-tree inserts stay at the right edge.
"""

from __future__ import annotations

import os
import re
import threading
import time

_CROCKFORD = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"
_DECODE = {c: i for i, c in enumerate(_CROCKFORD)}

ULID_RE = re.compile(r"^[0-9A-HJKMNP-TV-Z]{26}$")


def _encode(value: int, length: int) -> str:
    out = []
    for _ in range(length):
        out.append(_CROCKFORD[value & 0x1F])
        value >>= 5
    return "".join(reversed(out))


_MAX_RAND = (1 << 80) - 1
_lock = threading.Lock()
_last_ts = -1
_last_rand = 0


def new_ulid(now_ms: int | None = None) -> str:
    """Return a fresh 26-character ULID.

    Within the same millisecond the random component is incremented rather than
    redrawn, so identifiers minted in a tight loop still sort in creation order.
    """
    global _last_ts, _last_rand
    ts = int(time.time() * 1000) if now_ms is None else int(now_ms)
    with _lock:
        if ts == _last_ts:
            if _last_rand >= _MAX_RAND:
                ts += 1
                rand = int.from_bytes(os.urandom(10), "big")
            else:
                rand = _last_rand + 1
        else:
            rand = int.from_bytes(os.urandom(10), "big")
        _last_ts = ts
        _last_rand = rand
    return _encode(ts, 10) + _encode(rand, 16)


def ulid_timestamp_ms(ulid: str) -> int:
    """Extract the embedded millisecond timestamp from a ULID."""
    if not ULID_RE.match(ulid):
        raise ValueError("not a valid ULID: %r" % (ulid,))
    value = 0
    for ch in ulid[:10]:
        value = (value << 5) | _DECODE[ch]
    return value


def is_ulid(value: str) -> bool:
    return bool(ULID_RE.match(value or ""))


def now_ms() -> int:
    return int(time.time() * 1000)
