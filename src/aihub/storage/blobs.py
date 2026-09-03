"""Content-addressed blob storage with atomic writes.

Files are written to a temp spool, fsynced, then moved into place with
``os.replace``, which is atomic within a filesystem. The DB commit happens after
the file is durable: a crash in between leaves an unreferenced blob that the GC
reclaims, whereas the reverse order would leave a DB row pointing at nothing.
"""

from __future__ import annotations

import hashlib
import logging
import os
import shutil
import uuid
from pathlib import Path
from typing import BinaryIO, Optional, Tuple

log = logging.getLogger("aihub.storage.blobs")

CHUNK = 1024 * 1024


class BlobTooLarge(Exception):
    def __init__(self, limit: int) -> None:
        super().__init__("blob exceeds %d bytes" % limit)
        self.limit = limit


class BlobStore:
    def __init__(self, root: Path) -> None:
        self.root = Path(root)
        self.tmp = self.root / "tmp"
        self.root.mkdir(parents=True, exist_ok=True)
        self.tmp.mkdir(parents=True, exist_ok=True)

    def rel_path_for(self, sha256: str) -> str:
        return "%s/%s/%s" % (sha256[0:2], sha256[2:4], sha256)

    def abs_path(self, rel_path: str) -> Path:
        """Resolve a stored relative path, refusing anything outside the root."""
        candidate = (self.root / rel_path).resolve()
        root = self.root.resolve()
        if candidate != root and root not in candidate.parents:
            raise ValueError("blob path escapes the store root: %r" % (rel_path,))
        return candidate

    def exists(self, sha256: str) -> bool:
        return self.abs_path(self.rel_path_for(sha256)).is_file()

    def _finalize(self, tmp_path: Path, digest: str) -> Tuple[str, bool]:
        rel = self.rel_path_for(digest)
        final = self.abs_path(rel)
        if final.is_file():
            tmp_path.unlink(missing_ok=True)
            return rel, True
        final.parent.mkdir(parents=True, exist_ok=True)
        os.replace(str(tmp_path), str(final))
        # fsync the directory so the rename itself survives a power loss.
        dir_fd = os.open(str(final.parent), os.O_RDONLY)
        try:
            os.fsync(dir_fd)
        finally:
            os.close(dir_fd)
        return rel, False

    def put_bytes(self, data: bytes, *, max_bytes: Optional[int] = None) -> Tuple[str, str, int, bool]:
        """Store ``data``. Returns (sha256, rel_path, size, deduplicated)."""
        if max_bytes is not None and len(data) > max_bytes:
            raise BlobTooLarge(max_bytes)
        digest = hashlib.sha256(data).hexdigest()
        tmp_path = self.tmp / ("%s.part" % uuid.uuid4().hex)
        with open(tmp_path, "wb") as fh:
            fh.write(data)
            fh.flush()
            os.fsync(fh.fileno())
        rel, dedup = self._finalize(tmp_path, digest)
        return digest, rel, len(data), dedup

    def put_stream(self, stream: BinaryIO, *, max_bytes: Optional[int] = None) -> Tuple[str, str, int, bool]:
        """Stream ``stream`` to disk without buffering it all in memory."""
        hasher = hashlib.sha256()
        size = 0
        tmp_path = self.tmp / ("%s.part" % uuid.uuid4().hex)
        try:
            with open(tmp_path, "wb") as fh:
                while True:
                    chunk = stream.read(CHUNK)
                    if not chunk:
                        break
                    size += len(chunk)
                    if max_bytes is not None and size > max_bytes:
                        raise BlobTooLarge(max_bytes)
                    hasher.update(chunk)
                    fh.write(chunk)
                fh.flush()
                os.fsync(fh.fileno())
        except Exception:
            tmp_path.unlink(missing_ok=True)
            raise
        rel, dedup = self._finalize(tmp_path, hasher.hexdigest())
        return hasher.hexdigest(), rel, size, dedup

    def read_bytes(self, rel_path: str) -> bytes:
        return self.abs_path(rel_path).read_bytes()

    def open(self, rel_path: str) -> BinaryIO:
        return open(self.abs_path(rel_path), "rb")

    def size(self, rel_path: str) -> int:
        return self.abs_path(rel_path).stat().st_size

    def delete(self, rel_path: str) -> bool:
        path = self.abs_path(rel_path)
        if not path.is_file():
            return False
        path.unlink()
        return True

    def total_bytes(self) -> int:
        total = 0
        for dirpath, _dirnames, filenames in os.walk(self.root):
            for name in filenames:
                try:
                    total += os.path.getsize(os.path.join(dirpath, name))
                except OSError:
                    pass
        return total

    def free_bytes(self) -> int:
        return shutil.disk_usage(str(self.root)).free

    def sweep_tmp(self, older_than_sec: int = 86400) -> int:
        """Delete abandoned .part spool files. Returns the count removed."""
        import time

        cutoff = time.time() - older_than_sec
        removed = 0
        for path in self.tmp.glob("*.part"):
            try:
                if path.stat().st_mtime < cutoff:
                    path.unlink()
                    removed += 1
            except OSError:
                pass
        return removed
