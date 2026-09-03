"""Background classification worker.

Classification never runs on the request path. Uploads return in tens of
milliseconds with ``classification_status='pending'`` and this worker fills in
topic, tags and summary afterwards. Jobs live in the database, so a restart in
the middle of a batch loses nothing.
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Any, Dict, List, Optional, Sequence, Tuple

from ..ids import now_ms
from ..models import TOPIC_RE, normalize_tag
from ..textutil import truncate
from .claude_cli import ClaudeFailed, ClaudeUnavailable, classify_batch, probe
from .heuristic import UNSORTED, HeuristicClassifier

log = logging.getLogger("aihub.classify.worker")

KINDS = ("note", "message", "handoff", "issue", "decision", "artifact")
LEASE_SEC = 300
CIRCUIT_OPEN_SEC = 900
# Consecutive auth failures mean an expired login, which no amount of retrying
# fixes; the worker demotes itself permanently and says so in /health.
AUTH_FAILURES_BEFORE_DEMOTION = 3
NEW_TOPICS_PER_DAY = 3
MAX_TOPICS = 50
MIN_NEW_TOPIC_CONFIDENCE = 0.70


def edit_distance(a: str, b: str) -> int:
    if a == b:
        return 0
    if len(a) < len(b):
        a, b = b, a
    previous = list(range(len(b) + 1))
    for i, ca in enumerate(a, 1):
        current = [i]
        for j, cb in enumerate(b, 1):
            current.append(
                min(previous[j] + 1, current[j - 1] + 1, previous[j - 1] + (ca != cb))
            )
        previous = current
    return previous[-1]


class ClassifyWorker:
    def __init__(self, app) -> None:
        self.app = app
        self.cfg = app.state.config
        self.repo = app.state.repo
        self.db = app.state.db
        self.heuristic = HeuristicClassifier()
        self._task: Optional[asyncio.Task] = None
        self._wake = asyncio.Event()
        self._stopping = False
        self.engine = "heuristic"
        self.circuit_open_until = 0.0
        self.inflight = 0
        self.last_error = ""
        self.auth_failures = 0
        self.demoted = False
        self.calls_today = 0
        self.calls_day = ""

    # ------------------------------------------------------------ lifecycle
    async def start(self) -> None:
        try:
            version = await probe(self.cfg.classify.claude_bin)
            self.engine = "claude"
            log.info("claude CLI available", extra={"cli_version": version})
        except ClaudeUnavailable as exc:
            self.engine = "heuristic"
            log.warning(
                "claude CLI unavailable, classifying with rules only",
                extra={"reason": str(exc)},
            )
        self._seed_topics()
        self._publish_status()
        self._task = asyncio.create_task(self._loop(), name="aihub-classify")

    async def stop(self) -> None:
        self._stopping = True
        self._wake.set()
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):
                pass
            self._task = None

    def notify(self) -> None:
        """Wake the loop immediately after an upload."""
        self._wake.set()

    # ------------------------------------------------------------- helpers
    def _seed_topics(self) -> None:
        ts = now_ms()
        with self.db.write() as conn:
            for topic_id, display in self.heuristic.seed_topics():
                conn.execute(
                    "INSERT INTO topics(topic_id, display_name, status, item_count,"
                    " created_ms, updated_ms) VALUES(?,?,'active',0,?,?)"
                    " ON CONFLICT(topic_id) DO NOTHING",
                    (topic_id, display, ts, ts),
                )

    def _publish_status(self) -> None:
        try:
            queued = self.db.query_one(
                "SELECT COUNT(*) AS n FROM classification_jobs WHERE state='queued'"
            )
        except Exception:
            queued = None
        self.app.state.classifier_status = {
            # ok=False when claude is permanently unusable, so an expired login
            # is visible rather than an endless quiet retry.
            "ok": not self.demoted,
            "engine": "heuristic" if self.demoted else self.engine,
            "queue_depth": int(queued["n"]) if queued else 0,
            "inflight": self.inflight,
            "circuit_open": time.time() < self.circuit_open_until,
            "calls_today": self.calls_today,
            "daily_call_limit": self.cfg.classify.max_calls_per_day,
            "last_error": self.last_error[:200],
        }

    def _claude_usable(self) -> bool:
        if self.demoted or self.engine != "claude":
            return False
        if time.time() < self.circuit_open_until:
            return False
        return self._budget_left() > 0

    def _budget_left(self) -> int:
        """Remaining claude invocations for today.

        The hub spends the owner's subscription rate limit, not a metered
        balance, so an unbounded upload loop would lock the owner out of their
        own Claude Code rather than produce a bill.
        """
        day = time.strftime("%Y-%m-%d", time.gmtime())
        if day != self.calls_day:
            self.calls_day, self.calls_today = day, 0
        return max(0, self.cfg.classify.max_calls_per_day - self.calls_today)

    def _spend_call(self) -> None:
        self._budget_left()
        self.calls_today += 1
        if self.calls_today == self.cfg.classify.max_calls_per_day:
            log.warning(
                "daily classification budget spent; falling back to rules",
                extra={"limit": self.cfg.classify.max_calls_per_day},
            )

    def _open_circuit(self, reason: str) -> None:
        self.circuit_open_until = time.time() + CIRCUIT_OPEN_SEC
        self.last_error = reason
        log.warning(
            "classification circuit opened, falling back to rules",
            extra={"reason": reason[:200], "seconds": CIRCUIT_OPEN_SEC},
        )

    def _lease_batch(self, size: int) -> List[Dict[str, Any]]:
        """Claim up to ``size`` runnable jobs and return their item payloads."""
        ts = now_ms()
        leased: List[Dict[str, Any]] = []
        with self.db.write() as conn:
            # Reclaim jobs whose lease expired because the server died mid-run.
            conn.execute(
                "UPDATE classification_jobs SET state='queued', lease_until_ms=0"
                " WHERE state='running' AND lease_until_ms < ?",
                (ts,),
            )
            rows = conn.execute(
                "SELECT job_id, item_id, attempt, max_attempts FROM classification_jobs"
                " WHERE state='queued' AND next_run_ms <= ?"
                " ORDER BY next_run_ms ASC, job_id ASC LIMIT ?",
                (ts, size),
            ).fetchall()
            for row in rows:
                changed = conn.execute(
                    "UPDATE classification_jobs SET state='running', started_ms=?,"
                    " lease_until_ms=? WHERE job_id=? AND state='queued'",
                    (ts, ts + LEASE_SEC * 1000, row["job_id"]),
                ).rowcount
                if not changed:
                    continue
                item = conn.execute(
                    "SELECT item_id, title, classification_source FROM items WHERE item_id=?",
                    (row["item_id"],),
                ).fetchone()
                if item is None or item["classification_source"] == "manual":
                    conn.execute(
                        "UPDATE classification_jobs SET state='abandoned', finished_ms=?"
                        " WHERE job_id=?",
                        (ts, row["job_id"]),
                    )
                    continue
                conn.execute(
                    "UPDATE items SET classification_status='running' WHERE item_id=?",
                    (row["item_id"],),
                )
                leased.append(
                    {
                        "job_id": row["job_id"],
                        "item_id": row["item_id"],
                        "attempt": int(row["attempt"]),
                        "max_attempts": int(row["max_attempts"]),
                        "title": item["title"],
                    }
                )
        for job in leased:
            job["body"] = self.repo.read_body(job["item_id"])
        return leased

    def _catalog(self) -> Tuple[List[Tuple[str, str, int]], List[str]]:
        rows = self.db.query(
            "SELECT topic_id, display_name, item_count FROM topics"
            " WHERE status <> 'deprecated' ORDER BY item_count DESC LIMIT ?",
            (self.cfg.classify.max_topics_in_prompt,),
        )
        topics = [(r["topic_id"], r["display_name"], int(r["item_count"])) for r in rows]
        tags = [t for t, _ in self.repo.tag_catalog(60)]
        return topics, tags

    def _resolve_topic(self, raw: Dict[str, Any], known: Sequence[str]) -> str:
        """Apply the anti-proliferation rules the model is not trusted to follow."""
        topic = str(raw.get("topic_id") or "").strip().lower()
        topic = "".join(ch if ch.isalnum() or ch == "-" else "-" for ch in topic).strip("-")
        if not topic or not TOPIC_RE.match(topic):
            return UNSORTED
        if topic in known:
            return topic
        action = str(raw.get("topic_action") or "existing")
        try:
            confidence = float(raw.get("topic_confidence") or 0.0)
        except (TypeError, ValueError):
            confidence = 0.0
        # Near-duplicate of an existing topic: absorb it instead of forking.
        for existing in known:
            if (
                edit_distance(topic, existing) <= 2
                or topic.startswith(existing)
                or existing.startswith(topic)
            ):
                return existing
        if action != "new" or confidence < MIN_NEW_TOPIC_CONFIDENCE:
            return UNSORTED
        if len(known) >= MAX_TOPICS:
            log.info("topic cap reached, routing to unsorted", extra={"topics": len(known)})
            return UNSORTED
        day = time.strftime("%Y-%m-%d", time.gmtime())
        row = self.db.query_one(
            "SELECT value, window_key FROM counters WHERE name='new_topics'"
        )
        used = int(row["value"]) if row and row["window_key"] == day else 0
        if used >= NEW_TOPICS_PER_DAY:
            log.info("daily new-topic budget spent", extra={"used": used})
            return UNSORTED
        with self.db.write() as conn:
            conn.execute(
                "INSERT INTO counters(name, value, window_key) VALUES('new_topics',1,?)"
                " ON CONFLICT(name) DO UPDATE SET"
                " value = CASE WHEN counters.window_key = excluded.window_key"
                "              THEN counters.value + 1 ELSE 1 END,"
                " window_key = excluded.window_key",
                (day,),
            )
        return topic

    def _sanitize(self, raw: Dict[str, Any], known: Sequence[str]) -> Dict[str, Any]:
        tags: List[str] = []
        for tag in (raw.get("tags") or [])[:6]:
            norm = normalize_tag(str(tag))
            if norm and norm not in tags:
                tags.append(norm)
        try:
            importance = int(raw.get("importance") or 3)
        except (TypeError, ValueError):
            importance = 3
        kind = str(raw.get("kind") or "note")
        try:
            confidence = float(raw.get("topic_confidence") or 0.0)
        except (TypeError, ValueError):
            confidence = 0.0
        return {
            "topic": self._resolve_topic(raw, known),
            "tags": tags,
            "summary": truncate(str(raw.get("summary") or ""), 200),
            "importance": max(1, min(5, importance)),
            "kind": kind if kind in KINDS else "note",
            "confidence": max(0.0, min(1.0, confidence)),
        }

    def _apply(self, item_id: str, result: Dict[str, Any], source: str) -> None:
        self.repo.set_classification(
            item_id,
            topic=result["topic"],
            tags=result["tags"],
            summary=result["summary"],
            importance=result["importance"],
            kind=result["kind"],
            confidence=result["confidence"],
            source=source,
        )

    def _finish_job(self, job_id: str, state: str, error_kind: str = "", detail: str = "") -> None:
        ts = now_ms()
        with self.db.write() as conn:
            conn.execute(
                "UPDATE classification_jobs SET state=?, finished_ms=?, error_kind=?,"
                " error_detail=?, lease_until_ms=0 WHERE job_id=?",
                (state, ts, error_kind, detail[:500], job_id),
            )

    def _requeue(self, job: Dict[str, Any], delay_sec: int, error_kind: str, detail: str) -> None:
        ts = now_ms()
        with self.db.write() as conn:
            conn.execute(
                "UPDATE classification_jobs SET state='queued', attempt=attempt+1,"
                " next_run_ms=?, lease_until_ms=0, error_kind=?, error_detail=?"
                " WHERE job_id=?",
                (ts + delay_sec * 1000, error_kind, detail[:500], job["job_id"]),
            )
            conn.execute(
                "UPDATE items SET classification_status='pending' WHERE item_id=?",
                (job["item_id"],),
            )

    # ---------------------------------------------------------------- loop
    async def _process(self, jobs: List[Dict[str, Any]]) -> None:
        known = [t for t, _n, _c in self._catalog()[0]]
        topics, tags = self._catalog()

        if self._claude_usable():
            self.inflight = len(jobs)
            self._publish_status()
            self._spend_call()
            try:
                raw_results = await classify_batch(
                    jobs,
                    topics,
                    tags,
                    claude_bin=self.cfg.classify.claude_bin,
                    model=self.cfg.classify.model,
                    timeout=float(self.cfg.classify.timeout_sec),
                )
            except ClaudeUnavailable as exc:
                self.auth_failures += 1
                if self.auth_failures >= AUTH_FAILURES_BEFORE_DEMOTION:
                    self.demoted = True
                    log.error(
                        "claude CLI permanently unusable; classification is now"
                        " rule-based only. Re-authenticate with `claude /login`"
                        " on the server host and restart.",
                        extra={"reason": str(exc)[:200]},
                    )
                self._open_circuit(str(exc))
                raw_results = None
            except ClaudeFailed as exc:
                self.last_error = str(exc)
                if exc.kind in ("rate_limit", "timeout"):
                    for job in jobs:
                        if job["attempt"] + 1 <= job["max_attempts"]:
                            self._requeue(job, 30, exc.kind, exc.detail)
                        else:
                            self._fallback_one(job, exc.kind, exc.detail)
                    self.inflight = 0
                    self._publish_status()
                    return
                raw_results = None
            finally:
                self.inflight = 0

            if raw_results is not None:
                self.auth_failures = 0
                by_ref: Dict[int, Dict[str, Any]] = {}
                for entry in raw_results:
                    if not isinstance(entry, dict):
                        continue
                    try:
                        ref = int(entry.get("ref"))
                    except (TypeError, ValueError):
                        continue
                    if not (0 <= ref < len(jobs)) or ref in by_ref:
                        continue
                    # The check value must match the item this ref points at, or
                    # a reordered response would cross-assign topics silently.
                    expected = (jobs[ref].get("item_id") or "")[:8]
                    if str(entry.get("check") or "") != expected:
                        log.warning(
                            "discarding classification with a mismatched check value",
                            extra={"ref": ref},
                        )
                        continue
                    by_ref[ref] = entry
                for index, job in enumerate(jobs):
                    entry = by_ref.get(index)
                    if entry is None:
                        # No result for this item: rules rather than a wrong topic.
                        self._fallback_one(job, "schema", "missing ref %d" % index)
                        continue
                    try:
                        self._apply(job["item_id"], self._sanitize(entry, known), "claude")
                        self._finish_job(job["job_id"], "succeeded")
                    except Exception as exc:  # pragma: no cover - defensive
                        log.warning("apply failed", extra={"reason": str(exc)})
                        self._fallback_one(job, "unknown", str(exc))
                self._publish_status()
                return

        for job in jobs:
            self._fallback_one(job, "", "")
        self._publish_status()

    def _fallback_one(self, job: Dict[str, Any], error_kind: str, detail: str) -> None:
        try:
            raw = self.heuristic.classify(job.get("title") or "", job.get("body") or "")
            # The rule topics are seeded into the catalogue at startup, so they
            # must be passed as known here or every result collapses to unsorted.
            known = [t for t, _n, _c in self._catalog()[0]]
            self._apply(job["item_id"], self._sanitize(raw, known), "heuristic")
            self._finish_job(job["job_id"], "succeeded", error_kind, detail)
        except Exception as exc:  # pragma: no cover - defensive
            log.error("heuristic classification failed", extra={"reason": str(exc)})
            self.repo.mark_classification_failed(job["item_id"])
            self._finish_job(job["job_id"], "failed", "unknown", str(exc))

    async def _loop(self) -> None:
        idle_poll = 3.0
        while not self._stopping:
            try:
                jobs = await asyncio.get_event_loop().run_in_executor(
                    None, self._lease_batch, self.cfg.classify.batch_size
                )
                if not jobs:
                    self._publish_status()
                    try:
                        await asyncio.wait_for(self._wake.wait(), timeout=idle_poll)
                    except asyncio.TimeoutError:
                        pass
                    self._wake.clear()
                    continue
                # A lone job waits briefly so a burst can travel as one CLI call.
                if len(jobs) == 1 and self._claude_usable():
                    await asyncio.sleep(min(self.cfg.classify.batch_wait_sec, 10.0))
                    more = await asyncio.get_event_loop().run_in_executor(
                        None, self._lease_batch, self.cfg.classify.batch_size - 1
                    )
                    jobs.extend(more)
                await self._process(jobs)
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # pragma: no cover - keep the loop alive
                log.exception("classify loop error", extra={"reason": str(exc)})
                await asyncio.sleep(5.0)
