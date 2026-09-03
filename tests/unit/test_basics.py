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


def test_fts_reserved_words_do_not_break_the_query():
    """Bare AND*/NOT* land in operator position and abort the whole query."""
    import sqlite3

    conn = sqlite3.connect(":memory:")
    conn.execute(
        "CREATE VIRTUAL TABLE f USING fts5(title,summary,body,body_bi,tags,"
        " tokenize=\"unicode61 remove_diacritics 2 tokenchars '_-.'\", prefix='2 3')"
    )
    conn.execute(
        "INSERT INTO f(rowid,title,summary,body,body_bi,tags)"
        " VALUES(1,'t','s','memory AND leak NOT found 메모리누수','메모 모리 리누 누수','bug')"
    )
    for query in ["NOT", "AND", "memory AND leak", "search NOT found", "OR", "NEAR",
                  "메모리", "메모리leak", "가", 'a" OR 1=1 --', "C++"]:
        expr = build_match_expr(query)
        if not expr:
            continue
        conn.execute("SELECT COUNT(*) FROM f WHERE f MATCH ?", (expr,)).fetchone()


def test_negated_phrase_keeps_its_words_together():
    assert build_match_expr('python -"memory leak"') == (
        '("python" OR "python"*) NOT "memory leak"'
    )


def test_cursor_rejects_wrong_seq_types():
    import base64
    import json

    from aihub.errors import InvalidRequest

    for bad in [{"seq": "zzz"}, {"seq": {"a": 1}}, {"seq": None},
                {"seq": True}, {"other": 1}, {"seq": -1}]:
        raw = base64.urlsafe_b64encode(
            json.dumps(bad).encode()
        ).decode().rstrip("=")
        with pytest.raises(InvalidRequest):
            decode_cursor(raw)


def test_index_and_query_share_normalization():
    """Compatibility jamo must reach the same bigrams as composed syllables."""
    from aihub.textutil import normalize

    assert bigrams(normalize("\u3141\u3154\u3141\u3157\u3139\u3163")) == bigrams("메모리")
