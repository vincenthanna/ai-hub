from __future__ import annotations

import pytest

from aihub.ids import is_ulid, new_ulid, ulid_timestamp_ms
from aihub.models import normalize_label, normalize_tag
from aihub.pagination import clamp_limit, decode_cursor, encode_cursor
from aihub.textutil import bigrams, build_match_expr, clip_for_classification, truncate


def test_ulid_is_sortable_and_unique():
    ids = [new_ulid() for _ in range(5000)]
    assert ids == sorted(ids)
    assert len(set(ids)) == len(ids)
    assert all(is_ulid(i) for i in ids)


def test_ulid_timestamp_roundtrip():
    ulid = new_ulid(1_700_000_000_000)
    assert ulid_timestamp_ms(ulid) == 1_700_000_000_000


def test_cursor_roundtrip_and_rejection():
    c = encode_cursor({"seq": 42})
    assert decode_cursor(c) == {"seq": 42}
    assert decode_cursor(None) is None
    from aihub.errors import InvalidRequest

    with pytest.raises(InvalidRequest):
        decode_cursor("!!!!not-valid!!!!")


def test_cursor_rejects_non_dict_payload():
    import base64
    import json

    raw = base64.urlsafe_b64encode(json.dumps([1, 2]).encode()).decode().rstrip("=")
    from aihub.errors import InvalidRequest

    with pytest.raises(InvalidRequest):
        decode_cursor(raw)


def test_clamp_limit():
    from aihub.errors import InvalidRequest

    assert clamp_limit(None) == 20
    assert clamp_limit(100) == 100
    with pytest.raises(InvalidRequest):
        clamp_limit(0)
    with pytest.raises(InvalidRequest):
        clamp_limit(101)


def test_korean_bigrams():
    assert bigrams("메모리누수") == "메모 모리 리누 누수"
    assert bigrams("가") == "가"
    assert bigrams("abc") == ""


def test_match_expr_is_injection_safe():
    assert build_match_expr('a" OR 1=1 --') == '("a") AND ("OR") AND ("1=1")'
    assert build_match_expr("---") == ""
    assert build_match_expr("   ") == ""
    expr = build_match_expr("uv 메모리누수")
    assert "body_bi" in expr and "uv" in expr


def test_label_normalization():
    assert normalize_label("Backend Work") == "backend-work"
    assert normalize_label("  FRONT_end  ") == "front_end"
    for bad in ["", "   ", "!!!", "-"]:
        with pytest.raises(ValueError):
            normalize_label(bad)


def test_tag_normalization():
    assert normalize_tag("Auth!!") == "auth"
    assert normalize_tag("!!!") is None


def test_truncate_and_clip():
    assert truncate("abcdef", 3) == "ab…"
    assert truncate("ab", 10) == "ab"
    clipped = clip_for_classification("x" * 30000, head=100, tail=50)
    assert len(clipped) < 30000
    assert "characters omitted" in clipped
