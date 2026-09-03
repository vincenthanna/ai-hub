"""SQLite connection management.

Reads run on per-thread connections inside the threadpool; WAL lets them proceed
while a write is in flight. Every write goes through the single connection
guarded by ``write_lock`` and opens with BEGIN IMMEDIATE, which removes the
lock-upgrade deadlock that causes ``database is locked`` under concurrency.
"""

from __future__ import annotations

import asyncio
import logging
import sqlite3
import threading
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator, Optional

log = logging.getLogger("aihub.storage.db")

HALF_LIFE_DAYS = 14.0

_PRAGMAS = (
    ("journal_mode", "WAL"),
    ("synchronous", "NORMAL"),
    ("foreign_keys", "ON"),
    ("busy_timeout", "5000"),
    ("temp_store", "MEMORY"),
    ("cache_size", "-32000"),
)


def _recency(created_ms: Optional[int], now_ms: Optional[int]) -> float:
    """Exponential decay used by the search ranking.

    Registered as a UDF rather than written as SQL because ``pow()`` requires
    SQLite to be built with SQLITE_ENABLE_MATH_FUNCTIONS, which is not
    guaranteed on the deployment host.
    """
    if created_ms is None or now_ms is None:
        return 1.0
    age_days = max(0.0, (now_ms - created_ms) / 86_400_000.0)
    return 0.5 ** (age_days / HALF_LIFE_DAYS)


def _configure(conn: sqlite3.Connection) -> None:
    for name, value in _PRAGMAS:
        conn.execute("PRAGMA %s = %s" % (name, value))
    conn.row_factory = sqlite3.Row
    conn.create_function("recency", 2, _recency)


class Database:
    """Owns one writer connection and a pool of per-thread reader connections."""

    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._local = threading.local()
        self._readers: list[sqlite3.Connection] = []
        self._readers_lock = threading.Lock()
        self._writer = sqlite3.connect(
            str(self.path), check_same_thread=False, timeout=10.0, isolation_level=None
        )
        _configure(self._writer)
        self.write_lock = asyncio.Lock()
        self._writer_mutex = threading.Lock()

    # -- readers ---------------------------------------------------------
    def reader(self) -> sqlite3.Connection:
        conn = getattr(self._local, "conn", None)
        if conn is None:
            conn = sqlite3.connect(
                str(self.path), check_same_thread=False, timeout=10.0
            )
            _configure(conn)
            self._local.conn = conn
            with self._readers_lock:
                self._readers.append(conn)
        return conn

    def query(self, sql: str, params: Any = ()) -> list[sqlite3.Row]:
        return self.reader().execute(sql, params).fetchall()

    def query_one(self, sql: str, params: Any = ()) -> Optional[sqlite3.Row]:
        return self.reader().execute(sql, params).fetchone()

    # -- writer ----------------------------------------------------------
    @contextmanager
    def write(self) -> Iterator[sqlite3.Connection]:
        """Run a write transaction. Callers must already hold ``write_lock``.

        ``_writer_mutex`` additionally protects the connection when a write is
        driven from a worker thread rather than the event loop.
        """
        with self._writer_mutex:
            self._writer.execute("BEGIN IMMEDIATE")
            try:
                yield self._writer
            except Exception:
                self._writer.execute("ROLLBACK")
                raise
            else:
                self._writer.execute("COMMIT")

    def write_now(self, sql: str, params: Any = ()) -> sqlite3.Cursor:
        """Single-statement autocommit write, for callers outside a transaction."""
        with self._writer_mutex:
            return self._writer.execute(sql, params)

    # -- maintenance -----------------------------------------------------
    def journal_mode(self) -> str:
        row = self.reader().execute("PRAGMA journal_mode").fetchone()
        return row[0] if row else "unknown"

    def checkpoint(self, mode: str = "TRUNCATE") -> None:
        with self._writer_mutex:
            self._writer.execute("PRAGMA wal_checkpoint(%s)" % mode)

    def optimize(self) -> None:
        with self._writer_mutex:
            self._writer.execute("PRAGMA optimize")
            self._writer.execute("INSERT INTO items_fts(items_fts) VALUES('optimize')")

    def close(self) -> None:
        with self._readers_lock:
            for conn in self._readers:
                try:
                    conn.close()
                except Exception:
                    pass
            self._readers.clear()
        with self._writer_mutex:
            try:
                self._writer.execute("PRAGMA optimize")
            except Exception:
                pass
            self._writer.close()
