"""SQLite connection management.

Three properties matter here and each is enforced rather than assumed.
``PRAGMA foreign_keys`` is silently ignored inside a transaction, so every
connection is opened in autocommit mode, configured immediately, and then
verified. Reads come from a fixed-size pool instead of thread-local storage,
because anyio recycles worker threads and thread-local connections churn.
Every read is rolled back on release: an unconsumed SELECT leaves an implicit
read transaction open, which pins the WAL and stops it from ever checkpointing.
"""

from __future__ import annotations

import asyncio
import logging
import queue
import sqlite3
import threading
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator, List, Optional

log = logging.getLogger("aihub.storage.db")

HALF_LIFE_DAYS = 14.0
READ_POOL_SIZE = 8
BUSY_TIMEOUT_MS = 5000


def _recency(created_ms: Optional[int], now_ms: Optional[int]) -> float:
    """Exponential decay used by the search ranking.

    Registered as a UDF instead of SQL ``pow()`` so the ranking does not depend
    on SQLite being built with SQLITE_ENABLE_MATH_FUNCTIONS.
    """
    if created_ms is None or now_ms is None:
        return 1.0
    age_days = max(0.0, (now_ms - created_ms) / 86_400_000.0)
    return 0.5 ** (age_days / HALF_LIFE_DAYS)


def connect(path: Path) -> sqlite3.Connection:
    """Open a fully configured connection in autocommit mode."""
    conn = sqlite3.connect(
        str(path), check_same_thread=False, timeout=BUSY_TIMEOUT_MS / 1000.0,
        isolation_level=None,
    )
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA synchronous = NORMAL")
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA busy_timeout = %d" % BUSY_TIMEOUT_MS)
    conn.execute("PRAGMA temp_store = MEMORY")
    conn.execute("PRAGMA cache_size = -32000")
    if conn.execute("PRAGMA foreign_keys").fetchone()[0] != 1:
        conn.close()
        raise RuntimeError(
            "PRAGMA foreign_keys did not take effect; ON DELETE CASCADE would be a no-op"
        )
    conn.row_factory = sqlite3.Row
    conn.create_function("recency", 2, _recency)
    return conn


class Database:
    """One writer connection plus a bounded pool of reader connections."""

    def __init__(self, path: Path, *, read_pool_size: int = READ_POOL_SIZE) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._writer = connect(self.path)
        self._writer_mutex = threading.Lock()
        self.write_lock = asyncio.Lock()
        self._pool: "queue.LifoQueue[sqlite3.Connection]" = queue.LifoQueue()
        self._pool_size = read_pool_size
        self._all_readers: List[sqlite3.Connection] = []
        self._readers_lock = threading.Lock()
        self._closed = False

    @property
    def writer(self) -> sqlite3.Connection:
        """The single writer connection, for migrations and admin tooling."""
        return self._writer

    # -- readers ---------------------------------------------------------
    @contextmanager
    def read(self) -> Iterator[sqlite3.Connection]:
        try:
            conn = self._pool.get_nowait()
        except queue.Empty:
            conn = connect(self.path)
            with self._readers_lock:
                self._all_readers.append(conn)
        try:
            yield conn
        finally:
            try:
                # Close any implicit read transaction so the WAL can checkpoint.
                conn.rollback()
            except sqlite3.Error:
                pass
            if self._closed or self._pool.qsize() >= self._pool_size:
                try:
                    conn.close()
                except sqlite3.Error:
                    pass
            else:
                self._pool.put(conn)

    def query(self, sql: str, params: Any = ()) -> List[sqlite3.Row]:
        with self.read() as conn:
            return conn.execute(sql, params).fetchall()

    def query_one(self, sql: str, params: Any = ()) -> Optional[sqlite3.Row]:
        with self.read() as conn:
            return conn.execute(sql, params).fetchone()

    # -- writer ----------------------------------------------------------
    @contextmanager
    def write(self) -> Iterator[sqlite3.Connection]:
        """Run one write transaction.

        BEGIN IMMEDIATE takes the write lock up front, which removes the
        upgrade deadlock that otherwise surfaces as ``database is locked``.
        """
        with self._writer_mutex:
            self._writer.execute("BEGIN IMMEDIATE")
            try:
                yield self._writer
            except BaseException:
                try:
                    self._writer.execute("ROLLBACK")
                except sqlite3.Error:
                    pass
                raise
            else:
                self._writer.execute("COMMIT")

    def write_now(self, sql: str, params: Any = ()) -> sqlite3.Cursor:
        with self._writer_mutex:
            return self._writer.execute(sql, params)

    # -- maintenance -----------------------------------------------------
    def journal_mode(self) -> str:
        row = self.query_one("PRAGMA journal_mode")
        return row[0] if row else "unknown"

    def checkpoint(self, mode: str = "TRUNCATE") -> None:
        with self._writer_mutex:
            self._writer.execute("PRAGMA wal_checkpoint(%s)" % mode)

    def optimize(self) -> None:
        with self._writer_mutex:
            self._writer.execute("INSERT INTO items_fts(items_fts) VALUES('optimize')")
            self._writer.execute("PRAGMA optimize")

    def backup_to(self, target: Path) -> None:
        """Consistent snapshot. Copying the file directly is unsafe under WAL."""
        target.parent.mkdir(parents=True, exist_ok=True)
        with self._writer_mutex:
            dest = sqlite3.connect(str(target))
            try:
                self._writer.backup(dest)
            finally:
                dest.close()

    def close(self) -> None:
        self._closed = True
        with self._readers_lock:
            readers = list(self._all_readers)
            self._all_readers.clear()
        while True:
            try:
                self._pool.get_nowait()
            except queue.Empty:
                break
        for conn in readers:
            try:
                conn.close()
            except sqlite3.Error:
                pass
        with self._writer_mutex:
            try:
                self._writer.execute("PRAGMA optimize")
            except sqlite3.Error:
                pass
            self._writer.close()
