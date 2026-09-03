"""Forward-only schema migrations keyed on PRAGMA user_version."""

from __future__ import annotations

import hashlib
import logging
import re
import sqlite3
import time
from pathlib import Path
from typing import List, Tuple

from ..logging_setup import safe_extra

log = logging.getLogger("aihub.storage.migrate")

MIGRATIONS_DIR = Path(__file__).with_name("migrations")
_NAME_RE = re.compile(r"^(\d{4})_([a-z0-9_]+)\.sql$")


def discover() -> List[Tuple[int, str, Path]]:
    out: List[Tuple[int, str, Path]] = []
    if not MIGRATIONS_DIR.is_dir():
        return out
    for path in sorted(MIGRATIONS_DIR.iterdir()):
        match = _NAME_RE.match(path.name)
        if match:
            out.append((int(match.group(1)), match.group(2), path))
    out.sort(key=lambda row: row[0])
    return out


def current_version(conn: sqlite3.Connection) -> int:
    return int(conn.execute("PRAGMA user_version").fetchone()[0])


def migrate(conn: sqlite3.Connection) -> int:
    """Apply every migration newer than user_version. Returns the new version."""
    version = current_version(conn)
    applied = 0
    for number, name, path in discover():
        if number <= version:
            continue
        sql = path.read_text(encoding="utf-8")
        checksum = hashlib.sha256(sql.encode("utf-8")).hexdigest()
        log.info("applying migration", extra=safe_extra(version=number, migration=name))
        # executescript() commits any open transaction before it runs, so the
        # transaction has to live inside the script itself. Recording the version
        # in the same script keeps "schema applied" and "version bumped" atomic.
        script = "BEGIN;\n%s\n%s\n%s\nPRAGMA user_version = %d;\nCOMMIT;" % (
            sql,
            "CREATE TABLE IF NOT EXISTS schema_migrations ("
            " version INTEGER PRIMARY KEY, name TEXT NOT NULL,"
            " applied_ms INTEGER NOT NULL, checksum TEXT NOT NULL);",
            "INSERT OR REPLACE INTO schema_migrations(version,name,applied_ms,checksum)"
            " VALUES(%d,'%s',%d,'%s');"
            % (number, name.replace("'", "''"), int(time.time() * 1000), checksum),
            number,
        )
        try:
            conn.executescript(script)
        except Exception:
            try:
                conn.execute("ROLLBACK")
            except sqlite3.OperationalError:
                pass
            raise
        version = number
        applied += 1
    if applied:
        log.info("migrations applied", extra=safe_extra(count=applied, version=version))
    return version
