"""The content store: original files and large payloads, named by their SHA-256.

Each file lives at content/<first two hex digits>/<sha256> in the data folder
and has a content_files row. Rows are inserted only by ContentStore.put, which
writes the file first, so a row never names a file that was not written.
Records refer to a file through a foreign key to content_files; garbage
collection removes files and rows that nothing references.

Like Database, every call blocks and is refused on an event loop thread.

Collection never removes content held (ContentStore.hold), whatever its age: content stored before the
record naming it commits (put with hold), and content a reader has listed and reads later (a backup,
an export, a reading of a paper). The idle window is a second guard, for a put in progress.
"""

import collections
import contextlib
import hashlib
import json
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
        self._held = collections.Counter()  # hashes collection keeps, however old (hold)
        self._listing = 0  # readers between listing content and holding it (listing)
        # Puts counted in this store, and the count at each hash's last put: a collection's walk over files with no
        # row removes none put since it began, whatever the clock says (PR 59 batch 2). shortcut: one entry per hash
        # put while the app runs, never pruned (collections overlap); prune if a session ever puts millions.
        self._puts = 0
        self._put_at = {}

    @contextlib.contextmanager
    def hold(self, hashes=()):
        """Keep content from collection for the block, however long it takes: the hashes given (content a
        reader has listed and reads later), and each one put with hold= the list this yields (content whose
        record commits later, inside the block). On leaving, content nothing refers to is collected once idle."""
        held = _Held()
        with self._lock:
            for sha256 in hashes:
                self._held[sha256] += 1
                held.append(sha256)
        try:
            yield held
        finally:
            with self._lock:
                held.closed = True  # a put still running when the block ends takes no hold (put)
                for sha256 in held:
                    self._release(sha256)

    @contextlib.contextmanager
    def listing(self):
        """For a reader that lists content from a copy or a read of the records and holds it (hold) inside this block:
        until the block ends, collection removes no file, since one listed may meanwhile be referenced no more and,
        after a deletion, not idle-protected either (PR 59 batch 1). A file it skips goes at a later collection."""
        with self._lock:
            self._listing += 1
        try:
            yield
        finally:
            with self._lock:
                self._listing -= 1

    def put(self, data, media_type=None, hold=None):
        """Store bytes, or a binary file object read to its end. Returns the SHA-256.

        The file is synced and renamed into place, and its folder synced, before
        the content_files row is committed. Storing content that is already
        there writes it again, which repairs a damaged copy and marks it as
        recently added. The put holds its own content from the moment its file is
        in place until its row is committed (design review H, option 2B). hold, a
        list from hold(), keeps it from collection from then until that block ends:
        a caller that commits a record naming it later passes one. Must not be
        called inside Database.write.
        """
        _refuse_event_loop()
        tmp, own = self.root / f".put-{uuid.uuid4()}.tmp", None
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
                if hold is not None and not hold.closed:  # held before the file is in place, under the lock
                    # collection takes; a put that outlives its block (its caller cancelled) is not held
                    self._held[sha256] += 1
                    hold.append(sha256)
                self._puts += 1
                self._put_at[sha256] = self._puts
                self._held[sha256] += 1  # its own hold, until its row is committed (below)
                own = sha256
                os.replace(tmp, folder / sha256)
                _fsync(folder)
                if created:
                    _fsync(self.root)
        except BaseException:
            tmp.unlink(missing_ok=True)
            if own is not None:  # its file is in place, its row will not be: collected once idle
                with self._lock:
                    self._release(own)
            raise
        finally:
            with self._lock:
                self._writing.discard(tmp)
        try:
            self.db.write(lambda conn: conn.execute(
                "INSERT INTO content_files (sha256, size, media_type) VALUES (?, ?, ?)"
                " ON CONFLICT (sha256) DO NOTHING",
                (sha256, size, media_type),
            ))
        finally:
            with self._lock:
                self._release(sha256)
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

    def collect_garbage(self, now=None, released=()):
        """Remove files and rows nothing references once idle. Returns the number of files removed.

        Content held (hold, and a put until its row commits) is never removed. Otherwise a row is removed when
        no foreign key in the schema points at it and both it and its file are older
        than IDLE_AFTER. A file is removed when it has
        no row and was last written more than IDLE_AFTER ago; temporary files
        left by an interrupted put are removed the same way, never those of a put
        still in progress in this store. Names the store did not write
        are left alone. now, an aware UTC datetime, defaults to the current time.

        released: the hashes records referenced before a deletion. One nothing references now goes at once, not
        once idle (a deletion reaches every copy, slice 1 section 16; M2 audit 1 F8): its row and its file
        together, inside the transaction that sees nothing name it, under the store's lock, unless held or a
        listing is open (design review H, option 2B). No clock is asked: no record can come to name it before
        that transaction commits, and no put can place its file meanwhile, since a put holds its own content
        from placing its file until its row commits. A file it removed while the transaction then fails leaves
        an unreferenced row, which a later collection removes.
        """
        _refuse_event_loop()
        cutoff = (now or datetime.now(UTC)) - IDLE_AFTER
        released = set(released)
        with self._lock:
            began = self._puts
        at_once = 0

        def remove_rows(conn):
            nonlocal at_once
            candidates = [sha256 for (sha256,) in conn.execute(
                "SELECT sha256 FROM content_files c WHERE (created_at < strftime('%Y-%m-%dT%H:%M:%fZ', ?, 'unixepoch')"
                f" OR sha256 IN (SELECT value FROM json_each(?))) AND sha256 NOT IN ({referenced_sql(conn)})",
                (cutoff.timestamp(), json.dumps(sorted(released))),
            )]
            for sha256 in candidates:
                if sha256 in released:
                    with self._lock:  # at once: row and file together, in the transaction that saw nothing name it
                        if sha256 not in self._held and not self._listing:
                            conn.execute("DELETE FROM content_files WHERE sha256 = ?", (sha256,))
                            self._path(sha256).unlink(missing_ok=True)
                            at_once += 1
            # The idle rule, without the store's lock (a check of a file's time must not wait on it). A held put
            # that misses this check has its row put back by its own insert, which the one writer runs after
            # this transaction; its file, held, is kept below, under the lock.
            idle = [sha256 for sha256 in candidates if sha256 not in released
                    and sha256 not in self._held and _idle(self._path(sha256), cutoff)]
            conn.executemany("DELETE FROM content_files WHERE sha256 = ?", [(sha256,) for sha256 in idle])

        self.db.write(remove_rows)
        if not self.root.is_dir():
            return 0
        # A put renames its file into place, under the lock, before it inserts the
        # row, so a file with no row here is safe to remove only while it is idle.
        kept = set(self.db.read(lambda conn: [sha256 for (sha256,) in conn.execute("SELECT sha256 FROM content_files")]))
        removed = at_once
        for path in self._stored_files():
            if path.name in kept:
                continue
            with self._lock:
                if (path.name not in self._held and not self._listing and self._put_at.get(path.name, 0) <= began
                        and _idle(path, cutoff)):
                    path.unlink(missing_ok=True)
                    removed += 1
        for path in self.root.glob(".put-*.tmp"):  # left by an interrupted put, not one still running
            with self._lock:
                if path not in self._writing and _idle(path, cutoff):
                    path.unlink(missing_ok=True)
        return removed

    def _release(self, sha256):
        """One hold of sha256 ended; the caller holds the lock."""
        self._held[sha256] -= 1
        if self._held[sha256] <= 0:
            del self._held[sha256]

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


def referenced_sql(conn):
    """A query of every hash a record refers to (each foreign key into content_files), NULLs left out so NOT IN
    compares only hashes. Every reference read once: a correlated check per row would scan an unindexed column,
    such as run_events.body_ref, once per stored file."""
    references = conn.execute(
        "SELECT m.name, f.\"from\" FROM sqlite_schema m, pragma_foreign_key_list(m.name) f"
        " WHERE m.type = 'table' AND f.\"table\" = 'content_files'"
    ).fetchall()
    return " UNION ALL ".join(
        f'SELECT "{column}" FROM "{table}" WHERE "{column}" IS NOT NULL' for table, column in references)


class _Held(list):
    """The hashes one hold() block keeps; closed once the block has ended."""
    closed = False


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
