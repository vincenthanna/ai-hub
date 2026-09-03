"""Administrative commands: ``python -m aihub.admin <cmd>``."""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from typing import List, Optional

from .config import load_config, write_config
from .storage.blobs import BlobStore
from .storage.db import Database
from .storage.migrate import current_version, migrate


def cmd_init(args: argparse.Namespace) -> int:
    cfg = load_config(args.config)
    cfg.ensure_dirs()
    write_config(cfg)
    db = Database(cfg.db_path)
    version = migrate(db._writer)
    db.close()
    BlobStore(cfg.blobs_dir)
    print("home          : %s" % cfg.home)
    print("config        : %s" % cfg.config_path)
    print("database      : %s (schema v%d)" % (cfg.db_path, version))
    print("listen        : %s:%d" % (cfg.host, cfg.port))
    print("auth_enabled  : %s" % cfg.auth_enabled)
    return 0


def cmd_token(args: argparse.Namespace) -> int:
    print(load_config(args.config).token)
    return 0


def cmd_migrate(args: argparse.Namespace) -> int:
    cfg = load_config(args.config)
    db = Database(cfg.db_path)
    before = current_version(db._writer)
    after = migrate(db._writer)
    db.close()
    print("schema %d -> %d" % (before, after))
    return 0


def cmd_stats(args: argparse.Namespace) -> int:
    cfg = load_config(args.config)
    db = Database(cfg.db_path)
    blobs = BlobStore(cfg.blobs_dir)
    conn = db.reader()
    out = {
        "items": conn.execute("SELECT COUNT(*) FROM items").fetchone()[0],
        "topics": conn.execute(
            "SELECT COUNT(*) FROM topics WHERE status <> 'deprecated'"
        ).fetchone()[0],
        "agents": conn.execute("SELECT COUNT(*) FROM agents").fetchone()[0],
        "pending_deliveries": conn.execute(
            "SELECT COUNT(*) FROM deliveries WHERE state='pending'"
        ).fetchone()[0],
        "classification": dict(
            conn.execute(
                "SELECT classification_status, COUNT(*) FROM items GROUP BY 1"
            ).fetchall()
        ),
        "queued_jobs": conn.execute(
            "SELECT COUNT(*) FROM classification_jobs WHERE state='queued'"
        ).fetchone()[0],
        "db_bytes": cfg.db_path.stat().st_size if cfg.db_path.exists() else 0,
        "blob_bytes": blobs.total_bytes(),
        "free_bytes": blobs.free_bytes(),
    }
    db.close()
    print(json.dumps(out, indent=2, ensure_ascii=False))
    return 0


def cmd_gc(args: argparse.Namespace) -> int:
    """Delete blobs no row references, plus stale upload spool files."""
    cfg = load_config(args.config)
    db = Database(cfg.db_path)
    blobs = BlobStore(cfg.blobs_dir)
    conn = db.reader()
    referenced = {r[0] for r in conn.execute("SELECT rel_path FROM attachments")}
    referenced |= {
        r[0]
        for r in conn.execute("SELECT rel_path FROM item_bodies WHERE rel_path IS NOT NULL")
    }
    removed = 0
    freed = 0
    import os

    for dirpath, _dirs, files in os.walk(blobs.root):
        if os.path.basename(dirpath) == "tmp":
            continue
        for name in files:
            full = os.path.join(dirpath, name)
            rel = os.path.relpath(full, str(blobs.root))
            if rel not in referenced:
                try:
                    freed += os.path.getsize(full)
                    if not args.dry_run:
                        os.unlink(full)
                    removed += 1
                except OSError:
                    pass
    spool = 0 if args.dry_run else blobs.sweep_tmp()
    if not args.dry_run:
        db.optimize()
        db.checkpoint()
    db.close()
    print(
        "orphan blobs %s: %d (%d bytes), spool files removed: %d"
        % ("found" if args.dry_run else "removed", removed, freed, spool)
    )
    return 0


def cmd_reclassify(args: argparse.Namespace) -> int:
    """Requeue items for classification."""
    import time

    from .ids import new_ulid

    cfg = load_config(args.config)
    db = Database(cfg.db_path)
    conn = db._writer
    where = "1=1"
    params: List[object] = []
    if args.item:
        where = "item_id IN (%s)" % ",".join("?" * len(args.item))
        params = list(args.item)
    elif args.topic:
        where = "topic_id = ?"
        params = [args.topic]
    elif args.failed:
        where = "classification_status = 'failed'"
    rows = conn.execute(
        "SELECT item_id, title FROM items WHERE %s AND classification_source <> 'manual'"
        " LIMIT ?" % where,
        params + [args.limit],
    ).fetchall()
    ts = int(time.time() * 1000)
    for row in rows:
        conn.execute(
            "UPDATE items SET classification_status='pending' WHERE item_id=?", (row[0],)
        )
        conn.execute(
            "INSERT INTO classification_jobs(job_id,item_id,input_hash,state,next_run_ms,created_ms)"
            " VALUES(?,?,?, 'queued', ?, ?)"
            " ON CONFLICT(item_id, input_hash) DO UPDATE SET state='queued', attempt=0,"
            " next_run_ms=excluded.next_run_ms, lease_until_ms=0",
            (new_ulid(), row[0], "reclassify-%d" % ts, ts, ts),
        )
    db.close()
    print("requeued %d item(s)" % len(rows))
    return 0


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(prog="aihub.admin")
    parser.add_argument("--config", default=None)
    sub = parser.add_subparsers(dest="cmd", required=True)
    sub.add_parser("init").set_defaults(func=cmd_init)
    sub.add_parser("token").set_defaults(func=cmd_token)
    sub.add_parser("migrate").set_defaults(func=cmd_migrate)
    sub.add_parser("stats").set_defaults(func=cmd_stats)
    gc = sub.add_parser("gc")
    gc.add_argument("--dry-run", action="store_true")
    gc.set_defaults(func=cmd_gc)
    rc = sub.add_parser("reclassify")
    rc.add_argument("--item", action="append", default=[])
    rc.add_argument("--topic")
    rc.add_argument("--failed", action="store_true")
    rc.add_argument("--limit", type=int, default=200)
    rc.set_defaults(func=cmd_reclassify)
    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
