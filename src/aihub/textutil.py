"""Text helpers for indexing and searching mixed Korean/English content.

SQLite's ``unicode61`` tokenizer splits on whitespace and punctuation only, so a
Korean query for "메모리" never matches the indexed token "메모리누수". A second
FTS column holds a bigram stream built from runs of CJK syllables, and queries
are rewritten through the same function so partial Korean matches work while
English identifiers and file paths keep whole-word behaviour.
"""

from __future__ import annotations

import re
import unicodedata
from typing import Iterable, List

# Hangul syllables, Hangul jamo, CJK ideographs, Hiragana, Katakana.
_CJK_RANGES = (
    (0xAC00, 0xD7A3),
    (0x1100, 0x11FF),
    (0x3130, 0x318F),
    (0x4E00, 0x9FFF),
    (0x3040, 0x309F),
    (0x30A0, 0x30FF),
)

_FTS_SPECIALS = re.compile(r'[\"\'\(\)\*\:\^\{\}\[\]]')
_TOKEN_RE = re.compile(r'"[^"]*"|\S+')


def is_cjk(ch: str) -> bool:
    cp = ord(ch)
    return any(lo <= cp <= hi for lo, hi in _CJK_RANGES)


def cjk_runs(text: str) -> Iterable[str]:
    """Yield maximal runs of CJK characters found in text."""
    run: List[str] = []
    for ch in text:
        if is_cjk(ch):
            run.append(ch)
        elif run:
            yield "".join(run)
            run = []
    if run:
        yield "".join(run)


def bigrams(text: str, limit_chars: int = 0) -> str:
    """Build the space-separated bigram stream indexed in the ``body_bi`` column.

    A run of one character is emitted as-is. Longer runs become overlapping
    two-character pairs, so "메모리누수" becomes "메모 모리 리누 누수".
    """
    if limit_chars and len(text) > limit_chars:
        text = text[:limit_chars]
    out: List[str] = []
    for run in cjk_runs(text):
        if len(run) == 1:
            out.append(run)
            continue
        for i in range(len(run) - 1):
            out.append(run[i : i + 2])
    return " ".join(out)


def normalize(text: str) -> str:
    """NFKC-normalize and collapse whitespace."""
    return re.sub(r"\s+", " ", unicodedata.normalize("NFKC", text or "")).strip()


def _sanitize(token: str) -> str:
    cleaned = _FTS_SPECIALS.sub(" ", token).strip()
    # A token made only of punctuation indexes to nothing and would make the
    # MATCH expression either error out or match everything.
    if not any(ch.isalnum() or is_cjk(ch) for ch in cleaned):
        return ""
    return cleaned


def build_match_expr(query: str) -> str:
    """Translate a user query into a safe FTS5 MATCH expression.

    User input is never passed through verbatim. Each token is rebuilt: quoted
    runs stay phrases, a leading ``-`` becomes NOT, CJK tokens additionally
    search the bigram column, and ASCII tokens of three or more characters also
    try a prefix match.
    """
    query = normalize(query)
    if not query:
        return ""
    clauses: List[str] = []
    negations: List[str] = []
    for raw in _TOKEN_RE.findall(query):
        negate = raw.startswith("-") and len(raw) > 1
        if negate:
            raw = raw[1:]
        if raw.startswith('"') and raw.endswith('"') and len(raw) >= 2:
            phrase = _sanitize(raw[1:-1])
            if not phrase:
                continue
            clause = '"%s"' % phrase
        else:
            token = _sanitize(raw)
            if not token:
                continue
            alts = ['"%s"' % token]
            has_cjk = any(is_cjk(c) for c in token)
            if has_cjk:
                bi = bigrams(token)
                if bi:
                    alts.append('body_bi : "%s"' % bi)
            elif len(token) >= 3 and token.isalnum():
                alts.append("%s*" % token)
            clause = "(%s)" % " OR ".join(alts)
        if negate:
            negations.append(clause)
        else:
            clauses.append(clause)
    if not clauses and negations:
        return ""
    expr = " AND ".join(clauses)
    for neg in negations:
        expr += " NOT %s" % neg
    return expr


def truncate(text: str, limit: int) -> str:
    """Cut text to at most ``limit`` characters, appending an ellipsis marker."""
    if text is None:
        return ""
    if limit <= 0 or len(text) <= limit:
        return text
    return text[: max(0, limit - 1)] + "…"


def clip_for_classification(text: str, head: int = 12000, tail: int = 2000) -> str:
    """Keep the head and tail of a long body so classification stays cheap."""
    if len(text) <= head + tail:
        return text
    omitted = len(text) - head - tail
    return "%s\n\n[... %d characters omitted ...]\n\n%s" % (
        text[:head],
        omitted,
        text[-tail:],
    )
