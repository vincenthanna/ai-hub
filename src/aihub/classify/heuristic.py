"""Deterministic rule-based classifier.

This is the fallback whenever the claude CLI is absent, unauthenticated, or
failing. It keeps the hub fully functional without an LLM: items still get a
topic, tags, a summary, and a kind, just with lower precision.
"""

from __future__ import annotations

import json
import logging
import re
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

from ..models import normalize_tag
from ..textutil import normalize, truncate

log = logging.getLogger("aihub.classify.heuristic")

RULES_PATH = Path(__file__).with_name("heuristic_rules.json")
UNSORTED = "unsorted"


class HeuristicClassifier:
    def __init__(self, rules_path: Optional[Path] = None) -> None:
        self.rules_path = Path(rules_path or RULES_PATH)
        self._rules: Dict[str, Any] = {}
        self.reload()

    def reload(self) -> None:
        try:
            self._rules = json.loads(self.rules_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            log.warning("heuristic rules unreadable, using empty rule set",
                        extra={"reason": str(exc)})
            self._rules = {"threshold": 2.0, "topics": [], "kind_rules": [],
                           "importance_rules": [], "tag_rules": []}

    @property
    def topic_definitions(self) -> List[Dict[str, Any]]:
        return self._rules.get("topics", [])

    def classify(self, title: str, body: str) -> Dict[str, Any]:
        haystack = normalize("%s\n%s" % (title or "", body or "")).lower()

        best_topic = UNSORTED
        best_score = 0.0
        for topic in self.topic_definitions:
            weight = float(topic.get("weight", 1.0))
            hits = sum(1 for kw in topic.get("keywords", []) if kw.lower() in haystack)
            score = hits * weight
            if score > best_score:
                best_score, best_topic = score, topic["id"]
        threshold = float(self._rules.get("threshold", 2.0))
        topic = best_topic if best_score >= threshold else UNSORTED

        kind = "note"
        importance = 3
        for rule in self._rules.get("kind_rules", []):
            if any(m.lower() in haystack for m in rule.get("match", [])):
                kind = rule.get("kind", kind)
                importance = max(importance, int(rule.get("importance", importance)))
                break
        for rule in self._rules.get("importance_rules", []):
            if any(m.lower() in haystack for m in rule.get("match", [])):
                importance = max(importance, int(rule.get("importance", importance)))

        tags: List[str] = []
        for rule in self._rules.get("tag_rules", []):
            if any(m.lower() in haystack for m in rule.get("match", [])):
                tag = normalize_tag(rule.get("tag", ""))
                if tag and tag not in tags:
                    tags.append(tag)
        if topic != UNSORTED:
            tags.append(topic)
        tags = tags[:6]

        summary = title.strip()
        if not summary:
            sentences = re.split(r"(?<=[.!?。])\s+|\n", (body or "").strip())
            summary = next((s.strip() for s in sentences if s.strip()), "")
        return {
            "topic_id": topic,
            "topic_action": "existing",
            "topic_confidence": min(1.0, best_score / max(threshold, 1.0)) if topic != UNSORTED else 0.0,
            "tags": tags,
            "summary": truncate(summary, 200),
            "importance": importance,
            "kind": kind,
        }

    def seed_topics(self) -> List[Tuple[str, str]]:
        return [(t["id"], t.get("display", t["id"])) for t in self.topic_definitions]
