"""Domain queries. Routers call these; nothing else touches the database."""

from __future__ import annotations

import hashlib
import json
import logging
import sqlite3
import time
from dataclasses import dataclass
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

from ..errors import Conflict, NotFound
from ..ids import new_ulid, now_ms
from ..textutil import bigrams, build_match_expr, normalize, truncate

log = logging.getLogger("aihub.storage.repo")

BODY_DB_LIMIT = 256 * 1024
FTS_BODY_CHARS = 8000
FTS_BIGRAM_CHARS = 4000
PREVIEW_CHARS = 400
SUMMARY_CHARS = 200
UNSORTED = "unsorted"
# A new label starts with everything older than this already acknowledged. Zero
# would replay the entire backlog; no window at all would hide the handoff that
# was posted seconds before the receiving session first ran, which is exactly
# the flow this hub exists for.
NEW_AGENT_GRACE_MS = 24 * 60 * 60 * 1000


def iso(ms: Optional[int]) -> Optional[str]:
    if ms is None:
        return None
    return time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(ms / 1000.0)) + ".%03dZ" % (ms % 1000)


@dataclass
class PreparedAttachment:
    filename: str
    media_type: str
    sha256: str
    rel_path: str
    size_bytes: int


class Repo:
    def __init__(self, db, blobs) -> None:
        self.db = db
        self.blobs = blobs

    # ------------------------------------------------------------------ helpers
    @staticmethod
    def _payload_hash(
        title: str, body: str, to: Sequence[str], kind: str, att: Sequence[PreparedAttachment]
    ) -> str:
        canonical = json.dumps(
            {
                "title": title,
                "body": body,
                "to": sorted(to),
                "kind": kind,
                "att": sorted(a.sha256 for a in att),
            },
            sort_keys=True,
            ensure_ascii=False,
        )
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()

    def _touch_agent(
        self,
        conn: sqlite3.Connection,
        label: str,
        *,
        sent: bool = False,
        addressed_only: bool = False,
    ) -> bool:
        """Register or refresh a label. Returns True when it was newly created."""
        ts = now_ms()
        row = conn.execute(
            "SELECT label, seen_as FROM agents WHERE label = ?", (label,)
        ).fetchone()
        if row is None:
            # Anything older than the grace window counts as already seen.
            head = conn.execute(
                "SELECT COALESCE(MAX(seq), 0) FROM items WHERE created_ms < ?",
                (ts - NEW_AGENT_GRACE_MS,),
            ).fetchone()[0]
            conn.execute(
                "INSERT INTO agents(label, first_seen_ms, last_seen_ms, sent_count, seen_as)"
                " VALUES(?,?,?,?,?)",
                (label, ts, ts, 1 if sent else 0, "addressed" if addressed_only else "sender"),
            )
            conn.execute(
                "INSERT OR IGNORE INTO agent_cursors(recipient, broadcast_seq, updated_ms)"
                " VALUES(?,?,?)",
                (label, head, ts),
            )
            return True
        if addressed_only:
            # Do not refresh last_seen for a label nobody has actually polled as.
            return False
        conn.execute(
            "UPDATE agents SET last_seen_ms = ?, seen_as = 'sender',"
            " sent_count = sent_count + ? WHERE label = ?",
            (ts, 1 if sent else 0, label),
        )
        return False

    def ensure_agent(self, label: str) -> None:
        with self.db.write() as conn:
            self._touch_agent(conn, label)

    @staticmethod
    def _fts_write(
        conn: sqlite3.Connection,
        seq: int,
        title: str,
        summary: str,
        body: str,
        tags: Sequence[str],
    ) -> None:
        # Queries are NFKC-normalized before they become a MATCH expression, so
        # the index has to be built from normalized text or compatibility jamo
        # and full-width input silently fails to match itself.
        n_title = normalize(title)
        n_summary = normalize(summary)
        head = normalize(body)[:FTS_BODY_CHARS]
        conn.execute("DELETE FROM items_fts WHERE rowid = ?", (seq,))
        conn.execute(
            "INSERT INTO items_fts(rowid, title, summary, body, body_bi, tags)"
            " VALUES(?,?,?,?,?,?)",
            (
                seq,
                n_title,
                n_summary,
                head,
                bigrams(n_title + " " + n_summary + " " + head, FTS_BIGRAM_CHARS),
                " ".join(tags),
            ),
        )

    def _put_tags(self, conn: sqlite3.Connection, item_id: str, tags: Sequence[str], source: str) -> None:
        ts = now_ms()
        for tag in tags:
            conn.execute(
                "INSERT INTO tags(tag_id, label, use_count, created_ms) VALUES(?,?,0,?)"
                " ON CONFLICT(tag_id) DO NOTHING",
                (tag, tag, ts),
            )
            cur = conn.execute(
                "INSERT INTO item_tags(item_id, tag_id, source) VALUES(?,?,?)"
                " ON CONFLICT(item_id, tag_id) DO NOTHING",
                (item_id, tag, source),
            )
            if cur.rowcount:
                conn.execute("UPDATE tags SET use_count = use_count + 1 WHERE tag_id = ?", (tag,))

    @staticmethod
    def _ensure_topic(conn: sqlite3.Connection, topic: Optional[str]) -> Optional[str]:
        if not topic:
            return None
        row = conn.execute("SELECT topic_id FROM topics WHERE topic_id = ?", (topic,)).fetchone()
        ts = now_ms()
        if row is None:
            conn.execute(
                "INSERT INTO topics(topic_id, display_name, status, item_count, created_ms, updated_ms)"
                " VALUES(?,?,?,0,?,?)",
                (topic, topic, "provisional", ts, ts),
            )
        return topic

    # ------------------------------------------------------------------ writes
    def create_item(
        self,
        *,
        sender: str,
        to: Sequence[str],
        kind: str,
        title: str,
        body: str,
        topic: Optional[str],
        tags: Sequence[str],
        repo: str = "",
        host: str = "",
        ref: str = "",
        refs: Optional[List[Dict[str, Any]]] = None,
        client_msg_id: Optional[str] = None,
        priority: str = "normal",
        importance: Optional[int] = None,
        attachments: Optional[Sequence[PreparedAttachment]] = None,
        body_rel_path: Optional[str] = None,
        body_sha256: str = "",
        enqueue_classification: bool = True,
    ) -> Tuple[Dict[str, Any], bool]:
        """Insert an item. Returns (item dict, deduplicated)."""
        attachments = list(attachments or [])
        payload_hash = self._payload_hash(title, body, to, kind, attachments)

        if client_msg_id:
            existing = self.db.query_one(
                "SELECT item_id, payload_sha256 FROM items"
                " WHERE sender = ? AND client_msg_id = ?",
                (sender, client_msg_id),
            )
            if existing is not None:
                if existing["payload_sha256"] == payload_hash:
                    return self.get_item(existing["item_id"]), True
                raise Conflict(
                    "client_msg_id already used with a different payload",
                    field="client_msg_id",
                )

        item_id = new_ulid()
        ts = now_ms()
        body_bytes = len(body.encode("utf-8"))
        store_in_db = body_rel_path is None and body_bytes <= BODY_DB_LIMIT
        warnings: List[str] = []
        first_line = body.strip().splitlines()[0] if body.strip() else ""
        summary = truncate(title or first_line, SUMMARY_CHARS)

        with self.db.write() as conn:
            if client_msg_id:
                dup = conn.execute(
                    "SELECT item_id, payload_sha256 FROM items"
                    " WHERE sender = ? AND client_msg_id = ?",
                    (sender, client_msg_id),
                ).fetchone()
                if dup is not None:
                    if dup["payload_sha256"] == payload_hash:
                        found = dup["item_id"]
                        conn.execute("SELECT 1")
                        return self._get_item_conn(conn, found), True
                    raise Conflict(
                        "client_msg_id already used with a different payload",
                        field="client_msg_id",
                    )

            topic_id = self._ensure_topic(conn, topic)
            cur = conn.execute(
                "INSERT INTO items("
                " item_id, kind, title, summary, topic_id, importance, priority,"
                " sender, sender_host, sender_repo, sender_ref, is_broadcast,"
                " classification_status, classification_source,"
                " body_storage, body_bytes, body_sha256, payload_sha256,"
                " client_msg_id, refs_json, created_ms, updated_ms)"
                " VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    item_id,
                    kind,
                    title,
                    summary,
                    topic_id,
                    importance or 3,
                    priority,
                    sender,
                    host,
                    repo,
                    ref,
                    0 if to else 1,
                    "skipped" if not enqueue_classification else "pending",
                    "manual" if topic_id else "none",
                    "db" if store_in_db else "file",
                    body_bytes,
                    body_sha256,
                    payload_hash,
                    client_msg_id,
                    json.dumps(refs or [], ensure_ascii=False),
                    ts,
                    ts,
                ),
            )
            seq = int(cur.lastrowid)

            conn.execute(
                "INSERT INTO item_bodies(item_id, body, rel_path) VALUES(?,?,?)",
                (item_id, body if store_in_db else None, body_rel_path),
            )
            self._put_tags(conn, item_id, tags, "client")
            for att in attachments:
                conn.execute(
                    "INSERT INTO attachments("
                    " attachment_id, item_id, filename, media_type, size_bytes, sha256, rel_path, created_ms)"
                    " VALUES(?,?,?,?,?,?,?,?)",
                    (
                        new_ulid(),
                        item_id,
                        att.filename,
                        att.media_type,
                        att.size_bytes,
                        att.sha256,
                        att.rel_path,
                        ts,
                    ),
                )
            for recipient in to:
                conn.execute(
                    "INSERT INTO deliveries(item_id, recipient, seq, state, queued_ms)"
                    " VALUES(?,?,?,'pending',?)",
                    (item_id, recipient, seq, ts),
                )
                created = self._touch_agent(conn, recipient, addressed_only=True)
                seen = conn.execute(
                    "SELECT seen_as FROM agents WHERE label = ?", (recipient,)
                ).fetchone()
                if created or (seen is not None and seen["seen_as"] == "addressed"):
                    warnings.append(
                        "no session has ever polled as '%s'; the message is stored but"
                        " may be waiting on a label that does not exist" % recipient
                    )
            self._touch_agent(conn, sender, sent=True)
            if topic_id:
                conn.execute(
                    "UPDATE topics SET item_count = item_count + 1, updated_ms = ? WHERE topic_id = ?",
                    (ts, topic_id),
                )
            self._fts_write(conn, seq, title, summary, body, tags)

            if enqueue_classification:
                input_hash = hashlib.sha256(
                    ("%s|%s|v1" % (title, body[:12000])).encode("utf-8")
                ).hexdigest()
                conn.execute(
                    "INSERT INTO classification_jobs("
                    " job_id, item_id, input_hash, state, next_run_ms, created_ms)"
                    " VALUES(?,?,?, 'queued', ?, ?)"
                    " ON CONFLICT(item_id, input_hash) DO NOTHING",
                    (new_ulid(), item_id, input_hash, ts, ts),
                )
            out = self._get_item_conn(conn, item_id)
            out["warnings"] = warnings
            return out, False

    @staticmethod
    def _compact_broadcast_acks(conn: sqlite3.Connection, recipient: str, ts: int) -> None:
        """Fold a contiguous run of individual acks into the watermark.

        Keeping only the gaps bounds the exception table: acknowledging in order
        leaves nothing behind, and out-of-order acks cost one row until the gap
        below them is filled.
        """
        row = conn.execute(
            "SELECT broadcast_seq FROM agent_cursors WHERE recipient = ?", (recipient,)
        ).fetchone()
        cursor = int(row["broadcast_seq"]) if row else 0
        while True:
            nxt = conn.execute(
                "SELECT MIN(seq) AS s FROM items"
                " WHERE is_broadcast = 1 AND seq > ? AND sender <> ? AND status <> 'deleted'",
                (cursor, recipient),
            ).fetchone()["s"]
            if nxt is None:
                # Nothing left unread; the watermark can jump to the head.
                head = conn.execute(
                    "SELECT COALESCE(MAX(seq), 0) AS s FROM items WHERE is_broadcast = 1"
                ).fetchone()["s"]
                cursor = max(cursor, int(head))
                break
            acked = conn.execute(
                "SELECT 1 FROM broadcast_acks WHERE recipient = ? AND seq = ?",
                (recipient, int(nxt)),
            ).fetchone()
            if acked is None:
                break
            cursor = int(nxt)
        conn.execute(
            "INSERT INTO agent_cursors(recipient, broadcast_seq, updated_ms) VALUES(?,?,?)"
            " ON CONFLICT(recipient) DO UPDATE SET"
            " broadcast_seq = MAX(broadcast_seq, excluded.broadcast_seq),"
            " updated_ms = excluded.updated_ms",
            (recipient, cursor, ts),
        )
        conn.execute(
            "DELETE FROM broadcast_acks WHERE recipient = ? AND seq <= ?", (recipient, cursor)
        )

    def ack(
        self,
        recipient: str,
        item_ids: Sequence[str],
        broadcast_upto_seq: Optional[int],
        note: str = "",
        ack_all: bool = False,
    ) -> Dict[str, Any]:
        ts = now_ms()
        acked = 0
        already = 0
        missing: List[str] = []
        with self.db.write() as conn:
            self._touch_agent(conn, recipient)
            targets = list(item_ids)
            if ack_all:
                rows = conn.execute(
                    "SELECT item_id FROM deliveries WHERE recipient = ? AND state = 'pending'",
                    (recipient,),
                ).fetchall()
                targets.extend(r["item_id"] for r in rows)
                head = conn.execute(
                    "SELECT COALESCE(MAX(seq), 0) FROM items WHERE is_broadcast = 1"
                ).fetchone()[0]
                broadcast_upto_seq = max(broadcast_upto_seq or 0, int(head))
                conn.execute("DELETE FROM broadcast_acks WHERE recipient = ?", (recipient,))
            broadcast_seqs: List[int] = []
            for item_id in dict.fromkeys(targets):
                row = conn.execute(
                    "SELECT state FROM deliveries WHERE item_id = ? AND recipient = ?",
                    (item_id, recipient),
                ).fetchone()
                if row is None:
                    # Not addressed to this label: it may still be a broadcast the
                    # caller has just handled, which is acknowledged per item.
                    item = conn.execute(
                        "SELECT seq, is_broadcast FROM items WHERE item_id = ?", (item_id,)
                    ).fetchone()
                    if item is None or not item["is_broadcast"]:
                        missing.append(item_id)
                        continue
                    seq = int(item["seq"])
                    cur = conn.execute(
                        "SELECT broadcast_seq FROM agent_cursors WHERE recipient = ?",
                        (recipient,),
                    ).fetchone()
                    if cur is not None and seq <= int(cur["broadcast_seq"]):
                        already += 1
                        continue
                    exists = conn.execute(
                        "SELECT 1 FROM broadcast_acks WHERE recipient = ? AND seq = ?",
                        (recipient, seq),
                    ).fetchone()
                    if exists is not None:
                        already += 1
                        continue
                    broadcast_seqs.append(seq)
                    acked += 1
                    continue
                if row["state"] == "acked":
                    already += 1
                    continue
                conn.execute(
                    "UPDATE deliveries SET state='acked', acked_ms=?, note=?"
                    " WHERE item_id=? AND recipient=?",
                    (ts, note, item_id, recipient),
                )
                acked += 1
            if broadcast_seqs:
                for seq in broadcast_seqs:
                    conn.execute(
                        "INSERT OR IGNORE INTO broadcast_acks(recipient, seq, acked_ms)"
                        " VALUES(?,?,?)",
                        (recipient, int(seq), ts),
                    )
            if broadcast_upto_seq is not None:
                conn.execute(
                    "INSERT INTO agent_cursors(recipient, broadcast_seq, updated_ms) VALUES(?,?,?)"
                    " ON CONFLICT(recipient) DO UPDATE SET"
                    " broadcast_seq = MAX(broadcast_seq, excluded.broadcast_seq),"
                    " updated_ms = excluded.updated_ms",
                    (recipient, int(broadcast_upto_seq), ts),
                )
            self._compact_broadcast_acks(conn, recipient, ts)
            cursor_row = conn.execute(
                "SELECT broadcast_seq FROM agent_cursors WHERE recipient = ?", (recipient,)
            ).fetchone()
            pending = conn.execute(
                "SELECT COUNT(*) FROM deliveries WHERE recipient = ? AND state = 'pending'",
                (recipient,),
            ).fetchone()[0]
        return {
            "acked": acked,
            "already_acked": already,
            "not_found": missing,
            "broadcast_cursor_seq": int(cursor_row["broadcast_seq"]) if cursor_row else 0,
            "pending_direct_count": int(pending),
        }

    def set_classification(
        self,
        item_id: str,
        *,
        topic: Optional[str],
        tags: Sequence[str],
        summary: str,
        importance: Optional[int],
        kind: Optional[str],
        confidence: float,
        source: str,
    ) -> None:
        ts = now_ms()
        with self.db.write() as conn:
            row = conn.execute(
                "SELECT seq, item_id, title, topic_id, classification_source, kind"
                " FROM items WHERE item_id = ?",
                (item_id,),
            ).fetchone()
            if row is None:
                raise NotFound("item not found: %s" % item_id)
            if row["classification_source"] == "manual" and source != "manual":
                return
            old_topic = row["topic_id"]
            new_topic = self._ensure_topic(conn, topic) if topic else old_topic
            conn.execute(
                "UPDATE items SET topic_id=?, summary=?, importance=?, kind=?,"
                " classification_status='done', classification_source=?,"
                " classification_conf=?, classified_ms=?, updated_ms=? WHERE item_id=?",
                (
                    new_topic,
                    truncate(summary, SUMMARY_CHARS) or row["title"],
                    importance if importance else 3,
                    kind or row["kind"],
                    source,
                    float(confidence),
                    ts,
                    ts,
                    item_id,
                ),
            )
            if new_topic != old_topic:
                if old_topic:
                    conn.execute(
                        "UPDATE topics SET item_count = MAX(0, item_count - 1) WHERE topic_id = ?",
                        (old_topic,),
                    )
                if new_topic:
                    conn.execute(
                        "UPDATE topics SET item_count = item_count + 1, updated_ms = ?"
                        " WHERE topic_id = ?",
                        (ts, new_topic),
                    )
                    conn.execute(
                        "UPDATE topics SET status='active' WHERE topic_id=? AND status='provisional'"
                        " AND item_count >= 3",
                        (new_topic,),
                    )
            if tags:
                self._put_tags(conn, item_id, tags, source)
            all_tags = [
                r["tag_id"]
                for r in conn.execute(
                    "SELECT tag_id FROM item_tags WHERE item_id = ?", (item_id,)
                ).fetchall()
            ]
            body = self._read_body_conn(conn, item_id)
            self._fts_write(
                conn, int(row["seq"]), row["title"], truncate(summary, SUMMARY_CHARS), body, all_tags
            )

    def mark_classification_failed(self, item_id: str) -> None:
        with self.db.write() as conn:
            conn.execute(
                "UPDATE items SET classification_status='failed', updated_ms=?,"
                " topic_id = COALESCE(topic_id, ?) WHERE item_id=?",
                (now_ms(), UNSORTED, item_id),
            )

    # ------------------------------------------------------------------ reads
    def _read_body_conn(self, conn: sqlite3.Connection, item_id: str) -> str:
        row = conn.execute(
            "SELECT body, rel_path FROM item_bodies WHERE item_id = ?", (item_id,)
        ).fetchone()
        if row is None:
            return ""
        if row["body"] is not None:
            return row["body"]
        if row["rel_path"]:
            try:
                return self.blobs.read_bytes(row["rel_path"]).decode("utf-8", "replace")
            except OSError:
                return ""
        return ""

    def read_body(self, item_id: str) -> str:
        with self.db.read() as conn:
            return self._read_body_conn(conn, item_id)

    def _tags_for(self, conn: sqlite3.Connection, item_id: str) -> List[str]:
        return [
            r["tag_id"]
            for r in conn.execute(
                "SELECT tag_id FROM item_tags WHERE item_id = ? ORDER BY tag_id", (item_id,)
            ).fetchall()
        ]

    def _recipients_for(self, conn: sqlite3.Connection, item_id: str) -> List[str]:
        return [
            r["recipient"]
            for r in conn.execute(
                "SELECT recipient FROM deliveries WHERE item_id = ? ORDER BY recipient", (item_id,)
            ).fetchall()
        ]

    @staticmethod
    def _classification_label(row: sqlite3.Row) -> str:
        status = row["classification_status"]
        source = row["classification_source"]
        if source == "manual":
            return "manual"
        if status in ("pending", "running"):
            return "pending"
        if status == "failed":
            return "failed"
        if status == "skipped":
            return "manual" if source == "manual" else "pending"
        if source == "claude":
            return "auto"
        if source == "heuristic":
            return "heuristic"
        return "pending"

    def _summary_row(self, conn: sqlite3.Connection, row: sqlite3.Row, *, body: Optional[str] = None) -> Dict[str, Any]:
        item_id = row["item_id"]
        if body is None:
            body = self._read_body_conn(conn, item_id)
        att_count = conn.execute(
            "SELECT COUNT(*) FROM attachments WHERE item_id = ?", (item_id,)
        ).fetchone()[0]
        return {
            "item_id": item_id,
            "seq": int(row["seq"]),
            "kind": row["kind"],
            "title": row["title"],
            "summary": row["summary"],
            "body_preview": truncate(body, PREVIEW_CHARS),
            "body_truncated": len(body) > PREVIEW_CHARS,
            "topic": row["topic_id"],
            "tags": self._tags_for(conn, item_id),
            "sender": row["sender"],
            "to": self._recipients_for(conn, item_id),
            "priority": row["priority"],
            "importance": int(row["importance"]),
            "classification": self._classification_label(row),
            "attachment_count": int(att_count),
            "created_at": iso(row["created_ms"]),
            "created_ms": int(row["created_ms"]),
        }

    def _get_item_conn(self, conn: sqlite3.Connection, item_id: str) -> Dict[str, Any]:
        row = conn.execute("SELECT * FROM items WHERE item_id = ?", (item_id,)).fetchone()
        if row is None:
            raise NotFound("item not found: %s" % item_id)
        body = self._read_body_conn(conn, item_id)
        out = self._summary_row(conn, row, body=body)
        out.update(
            {
                "body": body,
                "body_bytes": int(row["body_bytes"]),
                "repo": row["sender_repo"],
                "host": row["sender_host"],
                "ref": row["sender_ref"],
                "refs": json.loads(row["refs_json"] or "[]"),
                "classified_at": iso(row["classified_ms"]),
                "attachments": [
                    {
                        "attachment_id": a["attachment_id"],
                        "filename": a["filename"],
                        "media_type": a["media_type"],
                        "size_bytes": int(a["size_bytes"]),
                        "sha256": a["sha256"],
                        "download_url": "/v1/items/%s/attachments/%s"
                        % (item_id, a["attachment_id"]),
                    }
                    for a in conn.execute(
                        "SELECT * FROM attachments WHERE item_id = ? ORDER BY attachment_id",
                        (item_id,),
                    ).fetchall()
                ],
                "delivery": [
                    {
                        "recipient": d["recipient"],
                        "state": d["state"],
                        "acked_at": iso(d["acked_ms"]),
                        "note": d["note"],
                    }
                    for d in conn.execute(
                        "SELECT * FROM deliveries WHERE item_id = ? ORDER BY recipient", (item_id,)
                    ).fetchall()
                ],
            }
        )
        return out

    def get_item(self, item_id: str) -> Dict[str, Any]:
        with self.db.read() as conn:
            return self._get_item_conn(conn, item_id)

    def get_attachment(self, item_id: str, attachment_id: str) -> Dict[str, Any]:
        row = self.db.query_one(
            "SELECT * FROM attachments WHERE item_id = ? AND attachment_id = ?",
            (item_id, attachment_id),
        )
        if row is None:
            raise NotFound("attachment not found")
        return dict(row)

    def list_items(
        self,
        *,
        limit: int = 20,
        after_seq: Optional[int] = None,
        topic: Optional[str] = None,
        kinds: Optional[Sequence[str]] = None,
        sender: Optional[str] = None,
        recipient: Optional[str] = None,
        since_ms: Optional[int] = None,
        until_ms: Optional[int] = None,
        order: str = "desc",
    ) -> Tuple[List[Dict[str, Any]], Optional[int]]:
        with self.db.read() as conn:
            where = ["i.status <> 'deleted'"]
            params: List[Any] = []
            if topic:
                where.append("(i.topic_id = ? OR i.topic_id LIKE ? || '-%')")
                params.extend([topic, topic])
            if kinds:
                where.append("i.kind IN (%s)" % ",".join("?" * len(kinds)))
                params.extend(kinds)
            if sender:
                where.append("i.sender = ?")
                params.append(sender)
            if recipient:
                where.append(
                    "(i.is_broadcast = 1 OR EXISTS("
                    " SELECT 1 FROM deliveries d WHERE d.item_id = i.item_id AND d.recipient = ?))"
                )
                params.append(recipient)
            if since_ms is not None:
                where.append("i.created_ms >= ?")
                params.append(since_ms)
            if until_ms is not None:
                where.append("i.created_ms < ?")
                params.append(until_ms)
            descending = order != "asc"
            if after_seq is not None:
                where.append("i.seq < ?" if descending else "i.seq > ?")
                params.append(after_seq)
            sql = (
                "SELECT i.* FROM items i WHERE %s ORDER BY i.seq %s LIMIT ?"
                % (" AND ".join(where), "DESC" if descending else "ASC")
            )
            params.append(limit + 1)
            rows = conn.execute(sql, params).fetchall()
            has_more = len(rows) > limit
            rows = rows[:limit]
            items = [self._summary_row(conn, r) for r in rows]
            next_seq = int(rows[-1]["seq"]) if has_more and rows else None
            return items, next_seq

    def search(
        self,
        query: str,
        *,
        limit: int = 20,
        offset: int = 0,
        topic: Optional[str] = None,
        tags: Optional[Sequence[str]] = None,
        kinds: Optional[Sequence[str]] = None,
        sender: Optional[str] = None,
        recipient: Optional[str] = None,
        since_ms: Optional[int] = None,
        until_ms: Optional[int] = None,
    ) -> Tuple[List[Dict[str, Any]], bool]:
        with self.db.read() as conn:
            expr = build_match_expr(query)
            if not expr:
                items, _ = self.list_items(
                    limit=limit,
                    topic=topic,
                    kinds=kinds,
                    sender=sender,
                    recipient=recipient,
                    since_ms=since_ms,
                    until_ms=until_ms,
                )
                for item in items:
                    item["score"] = 0.0
                    item["snippet"] = item.get("body_preview", "")
                return items, False

            where = ["items_fts MATCH ?", "i.status <> 'deleted'"]
            params: List[Any] = [expr]
            if topic:
                where.append("(i.topic_id = ? OR i.topic_id LIKE ? || '-%')")
                params.extend([topic, topic])
            if kinds:
                where.append("i.kind IN (%s)" % ",".join("?" * len(kinds)))
                params.extend(kinds)
            if sender:
                where.append("i.sender = ?")
                params.append(sender)
            if recipient:
                where.append(
                    "(i.is_broadcast = 1 OR EXISTS("
                    " SELECT 1 FROM deliveries d WHERE d.item_id = i.item_id AND d.recipient = ?))"
                )
                params.append(recipient)
            if since_ms is not None:
                where.append("i.created_ms >= ?")
                params.append(since_ms)
            if until_ms is not None:
                where.append("i.created_ms < ?")
                params.append(until_ms)
            if tags:
                where.append(
                    "(SELECT COUNT(*) FROM item_tags t WHERE t.item_id = i.item_id"
                    " AND t.tag_id IN (%s)) = ?" % ",".join("?" * len(tags))
                )
                params.extend(list(tags))
                params.append(len(tags))

            sql = (
                "SELECT i.*, "
                " (-bm25(items_fts, 8.0, 5.0, 1.0, 1.2, 3.0))"
                "   * (0.35 + 0.65 * recency(i.created_ms, ?))"
                "   * (1.0 + 0.15 * (i.importance - 3)) AS score,"
                " snippet(items_fts, 2, '[', ']', ' … ', 16) AS snip"
                " FROM items_fts JOIN items i ON i.seq = items_fts.rowid"
                " WHERE %s ORDER BY score DESC LIMIT ? OFFSET ?" % " AND ".join(where)
            )
            rows = conn.execute(sql, [now_ms()] + params + [limit + 1, offset]).fetchall()
            has_more = len(rows) > limit
            rows = rows[:limit]
            out = []
            for row in rows:
                item = self._summary_row(conn, row)
                item["score"] = round(float(row["score"]), 6)
                item["snippet"] = row["snip"] or item.get("body_preview", "")
                item.pop("body_preview", None)
                out.append(item)
            return out, has_more

    def inbox(
        self,
        recipient: str,
        *,
        limit: int = 20,
        include_broadcast: bool = True,
        kinds: Optional[Sequence[str]] = None,
        topics: Optional[Sequence[str]] = None,
    ) -> Dict[str, Any]:
        with self.db.read() as conn:
            cursor_row = conn.execute(
                "SELECT broadcast_seq FROM agent_cursors WHERE recipient = ?", (recipient,)
            ).fetchone()
            cursor_seq = int(cursor_row["broadcast_seq"]) if cursor_row else 0

            direct_sql = (
                "SELECT i.*, 'direct' AS delivery_kind FROM deliveries d"
                " JOIN items i ON i.item_id = d.item_id"
                " WHERE d.recipient = ? AND d.state = 'pending' AND i.status <> 'deleted'"
            )
            params: List[Any] = [recipient]
            if kinds:
                direct_sql += " AND i.kind IN (%s)" % ",".join("?" * len(kinds))
                params.extend(kinds)
            direct_sql += " ORDER BY i.seq ASC LIMIT ?"
            params.append(limit + 1)
            rows = list(conn.execute(direct_sql, params).fetchall())

            if include_broadcast and len(rows) <= limit:
                bsql = (
                    "SELECT i.*, 'broadcast' AS delivery_kind FROM items i"
                    " WHERE i.is_broadcast = 1 AND i.seq > ? AND i.sender <> ?"
                    " AND i.status <> 'deleted'"
                    " AND NOT EXISTS(SELECT 1 FROM broadcast_acks a"
                    "                WHERE a.recipient = ? AND a.seq = i.seq)"
                )
                bparams: List[Any] = [cursor_seq, recipient, recipient]
                if kinds:
                    bsql += " AND i.kind IN (%s)" % ",".join("?" * len(kinds))
                    bparams.extend(kinds)
                if topics:
                    bsql += " AND i.topic_id IN (%s)" % ",".join("?" * len(topics))
                    bparams.extend(topics)
                bsql += " ORDER BY i.seq ASC LIMIT ?"
                bparams.append(limit + 1 - len(rows))
                rows.extend(conn.execute(bsql, bparams).fetchall())

            has_more = len(rows) > limit
            rows = rows[:limit]
            items = []
            for row in rows:
                item = self._summary_row(conn, row)
                item["delivery_kind"] = row["delivery_kind"]
                items.append(item)

            pending = conn.execute(
                "SELECT COUNT(*) FROM deliveries WHERE recipient = ? AND state = 'pending'",
                (recipient,),
            ).fetchone()[0]
            head = conn.execute(
                "SELECT COALESCE(MAX(seq), 0) FROM items WHERE is_broadcast = 1"
            ).fetchone()[0]
            unseen_broadcast = conn.execute(
                "SELECT COUNT(*) FROM items i WHERE i.is_broadcast = 1 AND i.seq > ?"
                " AND i.sender <> ? AND i.status <> 'deleted'"
                " AND NOT EXISTS(SELECT 1 FROM broadcast_acks a"
                "                WHERE a.recipient = ? AND a.seq = i.seq)",
                (cursor_seq, recipient, recipient),
            ).fetchone()[0]
            return {
                "recipient": recipient,
                "items": items,
                "broadcast_cursor_seq": cursor_seq,
                "broadcast_head_seq": int(head),
                "pending_direct_count": int(pending),
                "unseen_broadcast_count": int(unseen_broadcast),
                "has_more": has_more,
            }

    def head_seq(self) -> int:
        row = self.db.query_one("SELECT COALESCE(MAX(seq), 0) AS s FROM items")
        return int(row["s"]) if row else 0

    def topics(self) -> List[Dict[str, Any]]:
        rows = self.db.query(
            "SELECT t.topic_id, t.display_name, t.description, t.status,"
            " (SELECT COUNT(*) FROM items i WHERE i.topic_id = t.topic_id"
            "   AND i.status <> 'deleted') AS count,"
            " (SELECT MAX(i.created_ms) FROM items i WHERE i.topic_id = t.topic_id) AS last_ms"
            " FROM topics t WHERE t.status <> 'deprecated'"
            " ORDER BY count DESC, t.topic_id ASC"
        )
        return [
            {
                "topic": r["topic_id"],
                "display_name": r["display_name"],
                "description": r["description"],
                "status": r["status"],
                "count": int(r["count"]),
                "last_activity": iso(r["last_ms"]),
            }
            for r in rows
        ]

    def agents(self) -> List[Dict[str, Any]]:
        rows = self.db.query(
            "SELECT a.label, a.first_seen_ms, a.last_seen_ms, a.sent_count,"
            " (SELECT COUNT(*) FROM deliveries d WHERE d.recipient = a.label"
            "   AND d.state='pending') AS pending"
            ", a.seen_as"
            " FROM agents a ORDER BY a.last_seen_ms DESC"
        )
        return [
            {
                "label": r["label"],
                "first_seen": iso(r["first_seen_ms"]),
                "last_seen": iso(r["last_seen_ms"]),
                "sent": int(r["sent_count"]),
                "pending_inbox": int(r["pending"]),
                "seen_as": r["seen_as"],
            }
            for r in rows
        ]

    def tag_catalog(self, limit: int = 200) -> List[Tuple[str, int]]:
        rows = self.db.query(
            "SELECT tag_id, use_count FROM tags ORDER BY use_count DESC LIMIT ?", (limit,)
        )
        return [(r["tag_id"], int(r["use_count"])) for r in rows]

    def health(self) -> Dict[str, Any]:
        row = self.db.query_one("SELECT COUNT(*) AS n FROM items")
        size = self.db.path.stat().st_size if self.db.path.exists() else 0
        return {
            "ok": True,
            "journal_mode": self.db.journal_mode(),
            "items": int(row["n"]) if row else 0,
            "size_bytes": size,
        }
