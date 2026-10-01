"""The content store: original files and large payloads, named by their SHA-256.

Each file lives at content/<first two hex digits>/<sha256> in the data folder
and has a content_files row. Rows are inserted only by ContentStore.put, which
writes the file first, so a row never names a file that was not written.
Records refer to a file through a foreign key to content_files; garbage
collection removes files and rows that nothing references.

Like Database, every call blocks and is refused on an event loop thread.
"""

import hashlib
import os
import re
import threading
import uuid
from datetime import UTC, datetime, timedelta

from backend.db.database import _fsync, _mkdir_private, _refuse_event_loop

IDLE_AFTER = timedelta(hours=1)  # a file added or added again more recently is never collected
_HASH = re.compile(r"[0-9a-f]{64}")
_CHUNK = 1 << 20


class ContentCorruptError(RuntimeError):
    """A stored file's bytes no longer match its hash."""


class ContentStore:
    """The content store of a Database's data folder.

    Create one per Database: its lock orders a write of a file against garbage
    collection of the same file, and it knows which temporary files are in use.
    """

    def __init__(self, db):
        self.db = db
        self.root = db.data_dir / "content"
        self._lock = threading.Lock()
        self._writing = set()  # temporary files of puts in progress, however long they take

    def put(self, data, media_type=None):
        """Store bytes, or a binary file object read to its end. Returns the SHA-256.

        The file is synced and renamed into place, and its folder synced, before
        the content_files row is committed. Storing content that is already
        there writes it again, which repairs a damaged copy and marks it as
        recently added. Must not be called inside Database.write.
        """
        _refuse_event_loop()
        tmp = self.root / f".put-{uuid.uuid4()}.tmp"
        with self._lock:
            if _mkdir_new(self.root):
                _fsync(self.db.data_dir)
            self._writing.add(tmp)
        digest, size = hashlib.sha256(), 0
        try:
            with open(tmp, "xb", opener=lambda name, flags: os.open(name, flags, 0o600)) as file:
                for chunk in _chunks(data):
                    digest.update(chunk)
                    size += len(chunk)
                    file.write(chunk)
            _fsync(tmp)
            sha256 = digest.hexdigest()
            folder = self.root / sha256[:2]
            with self._lock:  # collect_garbage removes a file only under this lock
                created = _mkdir_new(folder)
                os.utime(tmp)  # written now, however long the copy took
                os.replace(tmp, folder / sha256)
                _fsync(folder)
                if created:
                    _fsync(self.root)
        except BaseException:
            tmp.unlink(missing_ok=True)
            raise
        finally:
            with self._lock:
                self._writing.discard(tmp)
        self.db.write(lambda conn: conn.execute(
            "INSERT INTO content_files (sha256, size, media_type) VALUES (?, ?, ?)"
            " ON CONFLICT (sha256) DO NOTHING",
            (sha256, size, media_type),
        ))
        return sha256

    def read(self, sha256):
        """Return the stored bytes, checked against their hash.

        Raises ValueError for a malformed hash, FileNotFoundError for a missing
        file and ContentCorruptError when the bytes do not match.
        """
        _refuse_event_loop()
        data = self._path(sha256).read_bytes()
        if hashlib.sha256(data).hexdigest() != sha256:
            raise ContentCorruptError(f"the stored content {sha256} does not match its hash")
        return data

    def collect_garbage(self, now=None):
        """Remove files and rows nothing references once idle. Returns the number of files removed.

        A row is removed when no foreign key in the schema points at it and both
        it and its file are older than IDLE_AFTER. A file is removed when it has
        no row and was last written more than IDLE_AFTER ago; temporary files
        left by an interrupted put are removed the same way, never those of a put
        still in progress in this store. Names the store did not write
        are left alone. now, an aware UTC datetime, defaults to the current time.
        """
        _refuse_event_loop()
        cutoff = (now or datetime.now(UTC)) - IDLE_AFTER

        def remove_rows(conn):
            references = conn.execute(
                "SELECT m.name, f.\"from\" FROM sqlite_schema m, pragma_foreign_key_list(m.name) f"
                " WHERE m.type = 'table' AND f.\"table\" = 'content_files'"
            ).fetchall()
            unreferenced = " AND ".join(
                f'NOT EXISTS (SELECT 1 FROM "{table}" WHERE "{column}" = c.sha256)' for table, column in references
            )
            candidates = [sha256 for (sha256,) in conn.execute(
                "SELECT sha256 FROM content_files c"
                f" WHERE created_at < strftime('%Y-%m-%dT%H:%M:%fZ', ?, 'unixepoch') AND {unreferenced}",
                (cutoff.timestamp(),),
            )]
            idle = [sha256 for sha256 in candidates if _idle(self._path(sha256), cutoff)]
            conn.executemany("DELETE FROM content_files WHERE sha256 = ?", [(sha256,) for sha256 in idle])

        self.db.write(remove_rows)
        if not self.root.is_dir():
            return 0
        # A put renames its file into place, under the lock, before it inserts the
        # row, so a file with no row here is safe to remove only while it is idle.
        kept = set(self.db.read(lambda conn: [sha256 for (sha256,) in conn.execute("SELECT sha256 FROM content_files")]))
        removed = 0
        for path in self._stored_files():
            if path.name in kept:
                continue
            with self._lock:
                if _idle(path, cutoff):
                    path.unlink(missing_ok=True)
                    removed += 1
        for path in self.root.glob(".put-*.tmp"):  # left by an interrupted put, not one still running
            with self._lock:
                if path not in self._writing and _idle(path, cutoff):
                    path.unlink(missing_ok=True)
        return removed

    def _path(self, sha256):
        if not isinstance(sha256, str) or not _HASH.fullmatch(sha256):
            raise ValueError("a content hash is 64 lowercase hexadecimal digits")
        return self.root / sha256[:2] / sha256

    def _stored_files(self):
        """Files named as the store names them: content/<aa>/<sha256> starting with <aa>."""
        for folder in self.root.iterdir():
            if len(folder.name) != 2 or folder.is_symlink() or not folder.is_dir():
                continue
            for path in folder.iterdir():
                if _HASH.fullmatch(path.name) and path.name.startswith(folder.name) and path.is_file():
                    yield path


def _idle(path, cutoff):
    """Whether path was last written before cutoff. A missing file counts as idle."""
    try:
        return path.stat().st_mtime < cutoff.timestamp()
    except FileNotFoundError:
        return True


def _mkdir_new(path):
    """Create a folder owner-only. Returns whether it was created here."""
    existed = path.is_dir()
    _mkdir_private(path)
    return not existed


def _chunks(data):
    if isinstance(data, (bytes, bytearray, memoryview)):
        yield bytes(data)
        return
    while chunk := data.read(_CHUNK):
        yield chunk
