"""The search index file (slice-1 spec sections 4.3 and 7.2; ticket 17's upkeep and deletion):
`<data folder>/index/search.sqlite3`, derived from the main database and rebuildable.

It is opened only here, only through APSW, whose own SQLite loads extensions (the main database
stays on Python's sqlite3), and these are the only connections that load sqlite-vec. One writer
thread makes every change, in order; searches read on connections of their own.

- `fts_passages`: FTS5 with stored content and secure-delete on, over each passage's index text
  (its material's title, its section path, then its text), tokenized by `scholia` (`tokens`),
  registered on every connection; `project_id` and `passage_id` unindexed.
- `vec_passages`: vec0, partitioned by project, each passage's embedding (1024 float32, cosine).
- `vec_memory`: memory embeddings, partitioned by scope, written from S1-21.
- `index_rows`: the rowid the two share for one project's passage, its material and kind, a digest
  of its index text and whether it is embedded; no text.
- `index_meta`: the model, its revision, quantization, runtime and dimensions, the tokenizer and its
  version, the file's schema version, the main database file it was built from, the last index_queue
  sequence applied, and whether a whole rebuild is under way.

Rows are kept per project. The writer applies the main database's index_queue in order (`apply`):
an add takes the passage's index text from the main database if a current version of a material
in that project reads it now (the reading the Library shows), else adds nothing, since a removal
follows; a remove deletes the passage's rows. Removed rows are checked gone before the pass commits
(else it rolls back and its rows wait for the next pass), then the WAL is checkpointed and
truncated, and the applied queue rows are deleted from the main database. A pass or a truncation
that fails is tried again at the next pass and, without waiting for one, after a short delay.
Reference passages are indexed for keywords, marked by kind and never embedded; search leaves them
out.

At open: a file that cannot be read, one of another schema or built from another database file (a
restore, or one undone), one a whole rebuild did not finish, or one whose last applied
sequence the main database cannot have (a restored or replaced database: its queue's high-water mark
is lower) is replaced and rebuilt from the main database's current readings, in the writer, while
the app runs; another tokenizer version re-tokenizes the keyword rows; another model, revision,
quantization, runtime or dimension drops every vector, so that search never uses one from another
model. A whole rebuild that fails discards its partial file and leaves the index unavailable until
one succeeds; it is tried again in the writer, sooner and then less often. The index closes with
its database (Database.closing), its timers cancelled and its writer drained.
Nothing here logs text, a title, a query or a path.
"""

import concurrent.futures
import hashlib
import json
import logging
import os
import queue
import re
import threading
import unicodedata
from pathlib import Path

import apsw
import apsw.fts5
import sqlite_vec

from backend.db import DatabaseClosedError
from backend.db.deletion import _reads_now
from backend.settings import _make_private_dirs

log = logging.getLogger(__name__)

FILE = Path("index") / "search.sqlite3"
DIMENSIONS = 1024
TOKENIZER, TOKENIZER_VERSION = "scholia", "1"
SCHEMA_VERSION = "1"  # a file of another schema is replaced and rebuilt
QUEUE_PAGE = 1000  # queue rows applied in one index transaction
MAX_TERMS = 64  # a query's distinct tokens, at most
# A failed pass, WAL truncation or whole rebuild is tried again after 1 s, doubling with each failure in a
# row to at most 60 s (each kind counted apart); a success sets it back to 1 s.
RETRY_SECONDS = (1, 60)
STOP_EVERY = 1000  # rows between a project rebuild's looks at whether its run was stopped
# Han characters: CJK unified ideographs, extension A, the compatibility ideographs and extensions B on.
_HAN = "㐀-䶿一-鿿豈-﫿\U00020000-\U0003134f"
# A run of Han characters, or a run of other letters and digits (combining marks kept with them).
_RUNS = re.compile(rf"([{_HAN}]+)|(?:[^\W_{_HAN}]|[̀-ͯ])+")
DAMAGE = (apsw.CorruptError, apsw.NotADBError)  # a file SQLite finds damaged, or not a database

SCHEMA = f"""
CREATE TABLE index_meta (key TEXT PRIMARY KEY, value TEXT NOT NULL) STRICT;
CREATE TABLE index_rows (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    project_id TEXT NOT NULL,
    passage_id TEXT NOT NULL,
    material_id TEXT NOT NULL,
    kind TEXT NOT NULL,
    digest TEXT NOT NULL,
    embedded INTEGER NOT NULL DEFAULT 0,
    UNIQUE (project_id, passage_id)
) STRICT;
CREATE INDEX index_rows_by_material ON index_rows (project_id, material_id, embedded, kind);
CREATE VIRTUAL TABLE fts_passages USING fts5(text, project_id UNINDEXED, passage_id UNINDEXED, tokenize = '{TOKENIZER}');
INSERT INTO fts_passages (fts_passages, rank) VALUES ('secure-delete', 1);
"""
VECTORS = f"""
CREATE VIRTUAL TABLE IF NOT EXISTS vec_passages USING vec0(
    project_id TEXT PARTITION KEY, +passage_id TEXT, embedding float[{DIMENSIONS}] distance_metric=cosine);
CREATE VIRTUAL TABLE IF NOT EXISTS vec_memory USING vec0(
    scope TEXT PARTITION KEY, +memory_id TEXT, embedding float[{DIMENSIONS}] distance_metric=cosine);
"""


class CleanupFailed(Exception):
    """Removed rows were still found before the pass committed: it rolled back."""


class _Stopped(Exception):
    """The run a project's rebuild works for was stopped: its transaction rolls back."""


class Unavailable(Exception):
    """A whole rebuild of the file failed: nothing is read from the partial file, or written to it, until
    one succeeds (it is tried again, SearchIndex._rebuild_later)."""


# Tokens


def tokens(text):
    """(start, end, token, lone) for each token of text, offsets in characters (StringTokenizer turns
    them into UTF-8 byte offsets): each run of letters and digits other than Han, NFKC-normalized (so
    full-width forms match their usual ones) and lowercased, with no stemming; each Han run as its
    overlapping character bigrams, and a lone Han character as itself (lone true). Punctuation and
    spaces split. A token is only ever a run of word characters, so it can be quoted in a query."""
    for match in _RUNS.finditer(text):
        start, end = match.span()
        normal = unicodedata.normalize("NFKC", match.group()).lower()
        exact = len(normal) == end - start  # offsets inside the run hold
        for part in _RUNS.finditer(normal):
            word, at = part.group(), start + part.start() if exact else start
            if part.group(1) is None:
                yield at, at + len(word) if exact else end, word, False
            elif len(word) == 1:
                yield at, at + 1 if exact else end, word, True
            else:
                for i in range(len(word) - 1):
                    yield (at + i, at + i + 2, word[i:i + 2], False) if exact else (start, end, word[i:i + 2], False)


@apsw.fts5.StringTokenizer
def _tokenizer(con, args):
    def tokenize(text, reason, locale):
        for start, end, token, _ in tokens(text):
            yield start, end, token
    return tokenize


def match(text):
    """The FTS5 query for typed text, or None when it has no token: its distinct tokens, each quoted,
    joined with OR, so nothing typed is ever FTS5 syntax; a lone Han character as a prefix, so it
    also finds the bigrams it starts (one only in second position is missed: dense search covers
    meaning)."""
    terms = dict.fromkeys(f'"{token}"' + ("*" if lone else "") for _, _, token, lone in tokens(text))
    return " OR ".join(list(terms)[:MAX_TERMS]) or None


def index_text(title, section_path, text):
    """A passage's index text (section 7.1): its material's title and section path, then its text."""
    path = " > ".join(str(part) for part in section_path or [] if part)
    return "\n".join(part for part in (title, path, text) if part)


def digest(text):
    return hashlib.sha256(text.encode()).hexdigest()[:32]


# The main database's side


def readings(conn, project_id, passage_ids=None, references=True):
    """{passage id: (material id, title, section path, kind, text, page, ordinal, version id)} for the
    passages a current version of a material in the project reads now (its file read by this version
    of its type's extractor: the reading the Library shows, deletion._reads_now), all of them or those
    of passage_ids; without references, never a reference passage. A passage two of the project's
    materials read is given once, for the first added."""
    sql = (f"SELECT p.id, m.id, m.title, p.section_path, p.kind, p.text, p.page, p.ordinal, w.id"
           f" FROM materials m JOIN material_versions w ON w.material_id = m.id AND w.is_current = 1"
           f" JOIN extractions e ON {_reads_now('w', 'e')} JOIN passages p ON p.extraction_id = e.id"
           f" WHERE m.project_id = ?{'' if references else ' AND p.kind != ' + repr('reference')}")
    args = [project_id]
    if passage_ids is not None:
        sql += " AND p.id IN (SELECT value FROM json_each(?))"
        args.append(json.dumps(list(passage_ids)))
    found = {}
    for pid, material, title, path, kind, text, page, ordinal, version in conn.execute(
            sql + " ORDER BY m.created_at, m.id, p.ordinal", args):
        found.setdefault(pid, (material, title, json.loads(path) if path else [], kind, text, page, ordinal, version))
    return found


def granted(conn, project_id):
    """The ids of the passages a current version of a material in the project reads now (as readings)."""
    return {pid for (pid,) in conn.execute(
        f"SELECT p.id FROM materials m JOIN material_versions w ON w.material_id = m.id AND w.is_current = 1"
        f" JOIN extractions e ON {_reads_now('w', 'e')} JOIN passages p ON p.extraction_id = e.id"
        f" WHERE m.project_id = ?", (project_id,))}


def connect(path, create=False):
    """An APSW connection to the index file at path, with the tokenizer registered, secure delete on (freed
    pages are zeroed) and sqlite-vec loaded if it can be: (connection, whether it loaded). Only with create
    may it make the file; the index makes it owner-only first (_create), so no connection ever makes it."""
    flags = apsw.SQLITE_OPEN_READWRITE | (apsw.SQLITE_OPEN_CREATE if create else 0)
    conn = apsw.Connection(str(path), flags=flags)
    conn.set_busy_timeout(5000)
    conn.register_fts5_tokenizer(TOKENIZER, _tokenizer)
    conn.execute("PRAGMA secure_delete = ON")
    loaded = False
    try:
        conn.enable_load_extension(True)
        conn.load_extension(sqlite_vec.loadable_path())
        loaded = True
    except Exception as error:  # search is keyword-only, and says so
        log.warning("sqlite-vec could not be loaded (%s); search is keyword-only", type(error).__name__)
    finally:
        conn.enable_load_extension(False)
    return conn, loaded


def self_check(folder):
    """The packaged app's check of this module (backend/self_test.py), on a new file in folder: its
    SQLite, the schema with FTS5 secure-delete and the tokenizer, one passage found by a Chinese bigram
    and one by its vector within its project's partition, then removed and checked gone."""
    conn, loaded = connect(Path(folder) / "search.sqlite3", create=True)
    try:
        if not loaded:
            raise RuntimeError("sqlite-vec could not be loaded")
        conn.execute(SCHEMA)
        conn.execute(VECTORS)
        rows = [(1, "最低工资的合成研究 minimum wages", "p1", "a", 0.0), (2, "employment effects", "p1", "b", 1.0),
                (3, "最低工资 in another project", "p2", "c", 0.1)]
        for rowid, text, project, passage, value in rows:
            conn.execute("INSERT INTO fts_passages (rowid, text, project_id, passage_id) VALUES (?, ?, ?, ?)",
                         (rowid, text, project, passage))
            conn.execute("INSERT INTO vec_passages (rowid, project_id, passage_id, embedding) VALUES (?, ?, ?, ?)",
                         (rowid, project, passage, sqlite_vec.serialize_float32([value, 1.0 - value] * (DIMENSIONS // 2))))
        setting = conn.execute("SELECT v FROM fts_passages_config WHERE k = 'secure-delete'").get
        hits = [p for (p,) in conn.execute("SELECT passage_id FROM fts_passages WHERE fts_passages MATCH ? AND project_id = 'p1'",
                                           (match("工资"),))]
        nearest = conn.execute("SELECT passage_id FROM vec_passages WHERE embedding MATCH ? AND k = 1 AND project_id = 'p1'",
                               (sqlite_vec.serialize_float32([0.05, 0.95] * (DIMENSIONS // 2)),)).get
        if setting != 1 or hits != ["a"] or nearest != "a":
            raise RuntimeError(f"index check: secure-delete {setting!r}, keyword {hits!r}, nearest {nearest!r}")
        conn.execute("DELETE FROM fts_passages WHERE rowid = 1; DELETE FROM vec_passages WHERE rowid = 1")
        left = conn.execute("SELECT (SELECT count(*) FROM fts_passages WHERE rowid = 1)"
                            " + (SELECT count(*) FROM vec_passages WHERE rowid = 1)").get
        if left or conn.execute("SELECT count(*) FROM fts_passages WHERE fts_passages MATCH ?", (match("合成"),)).get:
            raise RuntimeError("index check: a removed passage is still found")
        return {"sqlite": apsw.sqlite_lib_version(), "apsw": apsw.apsw_version(),
                "sqlite_vec": conn.execute("SELECT vec_version()").get}
    finally:
        conn.close()


def high_water(conn):
    """The highest index_queue sequence this database has ever given (AUTOINCREMENT's record)."""
    row = conn.execute("SELECT seq FROM sqlite_sequence WHERE name = 'index_queue'").fetchone()
    return row[0] if row else 0


class SearchIndex:
    """The index file of one data folder, applying db's queue. identity: the model, revision,
    quantization, runtime and dimensions that index_meta records for its vectors."""

    def __init__(self, data_dir, db, identity):
        self.path = Path(data_dir) / FILE
        self.db, self.identity = db, {key: str(value) for key, value in identity.items()}
        self.vectors = False  # sqlite-vec loaded: dense search is possible
        self.building = False  # the whole file is being rebuilt
        self.closed = False
        self._shut = False  # close() has run (it runs once)
        self.damaged = False  # a read found the file damaged: it is being replaced
        self._conn = None
        self._truncate = False  # a WAL truncation not done yet: tried again at each pass, and soon (_retry_later)
        self._retry, self._retries = None, 0  # the timer of the next try after a failure, and how many failed
        self.unusable = False  # a whole rebuild failed: its partial file is neither read nor written (Unavailable)
        self._rebuild_retry, self._rebuild_failures = None, 0  # the same, for a whole rebuild
        self.rebuilt = None  # called (in the writer) once a damaged file found while the app runs is rebuilt
        self._readers = queue.SimpleQueue()  # idle read connections
        self._generation = 0  # a replaced file's readers are closed, not used again
        self._lock = threading.Lock()
        self._writer = concurrent.futures.ThreadPoolExecutor(1, thread_name_prefix="scholia-index-writer")

    # Connections

    def _connect(self):
        return connect(self.path)

    def _write(self, fn, *args, withdraw=None):
        """fn(*args) in the writer, and its result. withdraw(), if given, is asked while fn waits its turn
        (behind a whole rebuild, say): a true answer takes it off the writer's queue before it starts, and
        None is returned; once it has started, it is waited for."""
        if self.closed:
            raise DatabaseClosedError("the search index is closed")
        future = self._writer.submit(self._guarded, fn, *args)
        while withdraw is not None and not future.done():
            if withdraw() and future.cancel():
                return None
            try:
                return future.result(timeout=0.05)
            except concurrent.futures.TimeoutError:
                pass
        return future.result()

    def _guarded(self, fn, *args):
        """fn in the writer: one that finds the file damaged (storing vectors, a project's rebuild, a
        pass, a truncation) has it replaced and rebuilt, as a read that finds it so does."""
        try:
            return fn(*args)
        except DAMAGE:
            self._damaged()
            raise

    def _read(self, fn):
        """fn(conn) on an idle read connection, in one read transaction."""
        if self.closed:
            raise DatabaseClosedError("the search index is closed")
        if self.unusable:
            raise Unavailable()
        with self._lock:
            generation = self._generation
        try:
            conn = self._readers.get_nowait()
        except queue.Empty:
            conn, _ = self._connect()
        try:
            with conn:
                return fn(conn)
        except DAMAGE:
            self._damaged()
            raise
        finally:
            with self._lock:
                keep = not self.closed and generation == self._generation
            self._readers.put(conn) if keep else conn.close()

    def _close_readers(self):
        with self._lock:
            self._generation += 1
        while True:
            try:
                self._readers.get_nowait().close()
            except queue.Empty:
                return

    # Opening and staleness

    def open(self):
        """Open the file, checking it against the main database (see the module's docstring); returns
        whether its vectors are wanted again (dropped, or the file is being rebuilt) and the whole
        rebuild's future, if one was needed, which runs on in the writer."""
        dropped, rebuild = self._write(self._open)
        return dropped or rebuild, self._rebuild_soon() if rebuild else None

    def _create(self):
        """The file, made owner-only before SQLite opens it (SQLite gives its WAL and shared-memory files
        the file's mode)."""
        _make_private_dirs(self.path.parent)
        try:
            os.close(os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600))
        except FileExistsError:
            pass

    def _open(self):
        if self.closed:
            return False, False
        self._create()
        try:
            self._conn, self.vectors = self._connect()
            self._conn.execute("PRAGMA journal_mode = WAL")
            tables = {name for (name,) in self._conn.execute("SELECT name FROM sqlite_schema WHERE type = 'table'")}
            if "index_meta" not in tables:
                return False, True
            meta = dict(self._conn.execute("SELECT key, value FROM index_meta"))
            if meta.get("state") != "ready" or meta.get("schema") != SCHEMA_VERSION \
                    or meta.get("database") != self._database() or int(meta.get("last_seq", -1)) > self.db.read(high_water):
                return False, True
            if "vec_passages" in tables and not self.vectors:
                # Vectors it can neither use nor remove (sqlite-vec did not load): replaced by a file
                # without them, so no deletion leaves one behind and none of another model is kept.
                return False, True
            self._vector_tables()
            self._checkpoint()  # a truncation a reader held off before the last close
            wanted = self.vectors and "vec_passages" not in tables and bool(self._conn.execute(
                "SELECT 1 FROM index_rows WHERE kind != 'reference' LIMIT 1").fetchone())  # sqlite-vec loads again
            if (meta.get("tokenizer"), meta.get("tokenizer_version")) != (TOKENIZER, TOKENIZER_VERSION):
                with self._conn:  # stored text, tokenized again
                    self._conn.execute("INSERT INTO fts_passages (fts_passages) VALUES ('rebuild')")
                    self._meta(tokenizer=TOKENIZER, tokenizer_version=TOKENIZER_VERSION)
            if any(meta.get(key) != value for key, value in self.identity.items()):
                self._drop_vectors()
                wanted = True
            return wanted, False
        except apsw.Error as error:  # a file SQLite cannot use (damaged, not a database): replaced
            log.warning("the search index could not be used (%s); it is rebuilt", type(error).__name__)
            return False, True

    def _database(self):
        """Which main database file the index was built from: its inode. A restore puts another
        file in place, and putting the previous one back after a failed restore brings back its own, so
        an index built in between is never taken for this database's (the queue's high-water mark alone
        cannot tell them apart)."""
        return str(os.stat(self.db.path).st_ino)  # not the device number, which a removable volume's mount can change

    def _vector_tables(self):
        if self.vectors:
            self._conn.execute(VECTORS)

    def _drop_vectors(self):
        """Every vector goes (from another model, revision, quantization, runtime or dimension)."""
        with self._conn:
            if self.vectors:
                self._conn.execute("DROP TABLE IF EXISTS vec_passages; DROP TABLE IF EXISTS vec_memory")
                self._conn.execute(VECTORS)
            self._conn.execute("UPDATE index_rows SET embedded = 0")
            self._meta(**self.identity)
        self._checkpoint()

    def _meta(self, **values):
        self._conn.executemany("INSERT OR REPLACE INTO index_meta (key, value) VALUES (?, ?)",
                               [(key, str(value)) for key, value in values.items()])

    def _rebuild_soon(self, then=None):
        """_rebuild_all in the writer, without waiting for it, then then() there once it has succeeded. One
        that fails discards its partial file and leaves the index unusable (never read as ready), and is
        tried again soon, its papers' embeddings asked for once one succeeds (self.rebuilt). A success,
        by any path, disarms a retry still to come."""
        def done(future):
            if not future.cancelled() and future.exception() is not None:
                log.error("rebuilding the search index failed (%s)", type(future.exception()).__name__)

        def rebuild():
            if self.closed:  # closed while this waited its turn: nothing is unlinked or made
                return
            try:
                if not self._rebuild_all():
                    return
            except Exception as error:
                self._discard()
                log.error("rebuilding the search index failed (%s); it is tried again", type(error).__name__)
                self._rebuild_later()
                return
            with self._lock:
                self._rebuild_failures = 0
                if self._rebuild_retry is not None:
                    self._rebuild_retry.cancel()
                    self._rebuild_retry = None
            if then is not None and not self.closed:  # once the file is ready, whatever the pass after it meets
                then()
            try:
                self._apply()  # what the queue brought meanwhile
            except Exception as error:  # the file is rebuilt; the pass is tried again soon (_apply)
                log.warning("applying the index queue after its rebuild failed (%s)", type(error).__name__)
        self.building = True  # from the moment it is asked for: no file is taken for ready meanwhile
        future = self._writer.submit(rebuild)
        future.add_done_callback(done)
        return future

    def _discard(self):
        """A failed whole rebuild's partial file goes (it may hold text a deletion since has removed from
        the main database): the index stays unusable, building, until a rebuild succeeds."""
        self.unusable = True
        if self._conn is not None:
            self._conn.close()
            self._conn = None
        self._close_readers()
        for suffix in ("", "-wal", "-shm"):
            try:
                Path(f"{self.path}{suffix}").unlink(missing_ok=True)
            except OSError as error:
                log.warning("a failed rebuild's partial index file could not be removed (%s)", type(error).__name__)

    def _rebuild_later(self):
        """Another whole rebuild after a delay that doubles with each failure in a row (RETRY_SECONDS), one at
        a time, none once the index is closed (close cancels it)."""
        def again():
            with self._lock:
                self._rebuild_retry = None
                if self.closed:
                    return
            try:
                self._rebuild_soon(lambda: self.rebuilt and self.rebuilt())
            except RuntimeError:  # the writer stopped meanwhile (closing)
                pass
        with self._lock:
            if self.closed or self._rebuild_retry is not None:
                return
            delay = min(RETRY_SECONDS[0] * 2 ** self._rebuild_failures, RETRY_SECONDS[1])
            self._rebuild_failures += 1
            timer = threading.Timer(delay, again)
            timer.daemon = True
            self._rebuild_retry = timer  # held before it starts, so close() always finds it to cancel
            timer.start()

    def _rebuild_all(self):
        """Replace the file with a new one built from the main database's current readings, project by
        project in the order of their ids: marked building until every project's rows are in, so a
        launch that finds it unfinished starts again. The last sequence applied is the queue's
        high-water mark read first; the queue then brings in what changed meanwhile (the caller's next
        pass). Readers never make the file (connect), so the new one is this writer's, owner-only.
        Returns whether it finished (else the index closed meanwhile). One that raises leaves building
        set: the caller discards its file."""
        self.building = True
        if self.closed:
            return False
        if self._conn is not None:
            self._conn.close()
            self._conn = None
        self._close_readers()
        for suffix in ("", "-wal", "-shm"):
            Path(f"{self.path}{suffix}").unlink(missing_ok=True)
        self._create()
        self._conn, self.vectors = self._connect()
        self._conn.execute("PRAGMA journal_mode = WAL")
        with self._conn:
            self._conn.execute(SCHEMA)
            self._meta(state="building", last_seq=0, schema=SCHEMA_VERSION, database=self._database(),
                       tokenizer=TOKENIZER, tokenizer_version=TOKENIZER_VERSION, **self.identity)
        self._vector_tables()
        last = self.db.read(high_water)
        for (project,) in self.db.read(lambda conn: conn.execute("SELECT id FROM projects ORDER BY id").fetchall()):
            if self.closed:
                return False
            found = self.db.read(lambda conn: readings(conn, project))
            with self._conn:
                for pid, reading in found.items():
                    self._add(project, pid, reading)
        with self._conn:
            self._meta(state="ready", last_seq=last)
        self.damaged = self.unusable = False
        self.building = False
        return True

    def _damaged(self):
        """A read or a pass found the file damaged: it is replaced and rebuilt in the writer, once, and its
        vectors are wanted again (self.rebuilt), as when a launch rebuilds it."""
        with self._lock:
            if self.damaged or self.closed:
                return
            self.damaged = True
        log.warning("the search index is damaged; it is rebuilt")
        self._rebuild_soon(lambda: self.rebuilt and self.rebuilt())

    # Applying the queue

    def apply(self, stop=None):
        """Apply the queue's rows not applied yet (from any thread; see the module's docstring), a page
        (QUEUE_PAGE rows, one transaction) at a time; stop(), if given, is asked before each page, and a
        true answer leaves the rest to the next pass (and withdraws a pass still waiting its turn)."""
        return self._write(self._apply, stop, withdraw=stop)

    def apply_soon(self):
        """apply, in the writer, without waiting for it; a failure is logged."""
        def done(future):
            if not future.cancelled() and future.exception() is not None:
                log.warning("applying the index queue failed (%s)", type(future.exception()).__name__)
        if not self.closed:
            self._writer.submit(self._apply).add_done_callback(done)

    def _apply(self, stop=None):
        """A pass over the queue. One that fails is tried again soon (_retry_later), so a removal it
        did not apply never waits on other work; one that finds the file damaged has it rebuilt."""
        try:
            applied = self._apply_queue(stop)
        except DAMAGE:
            self._damaged()
            raise
        except Exception:
            self._retry_later()
            raise
        if not self._truncate:
            self._retries = 0
        return applied

    def _retry_later(self):
        """A pass in the writer after a delay that doubles with each failure in a row (RETRY_SECONDS);
        one at a time, none once the index is closed."""
        with self._lock:
            if self.closed or self._retry is not None:
                return
            delay = min(RETRY_SECONDS[0] * 2 ** self._retries, RETRY_SECONDS[1])
            self._retries += 1
            self._retry = threading.Timer(delay, self._retried)
            self._retry.daemon = True
            self._retry.start()

    def _retried(self):
        with self._lock:
            self._retry = None
        try:
            self.apply_soon()
        except RuntimeError:  # the writer stopped meanwhile (closing)
            pass

    def _apply_queue(self, stop=None):
        applied = 0
        if self._truncate and self._conn is not None:
            self._checkpoint()
        while self._conn is not None and not self.closed and not self.building and not (stop and stop()):
            last = int(self._conn.execute("SELECT value FROM index_meta WHERE key = 'last_seq'").fetchone()[0])
            rows = self.db.read(lambda conn: conn.execute(
                "SELECT seq, target, target_id, project_id, op FROM index_queue WHERE seq > ? ORDER BY seq LIMIT ?",
                (last, QUEUE_PAGE)).fetchall())
            if not rows:
                break
            wanted = {}
            for _, target, pid, project, op in rows:
                if target == "passage" and op == "add":
                    wanted.setdefault(project, set()).add(pid)
            found = self.db.read(lambda conn: {project: readings(conn, project, ids) for project, ids in wanted.items()})
            removed, replaced = [], False
            with self._conn:
                for _, target, pid, project, op in rows:
                    if target != "passage":
                        continue  # memory: S1-21's
                    if op == "remove":
                        removed += self._remove(project, pid)
                    elif (reading := found[project].get(pid)) is not None:
                        replaced = self._add(project, pid, reading) or replaced
                self._meta(last_seq=rows[-1][0])
            if removed or replaced:  # what was removed or replaced is in old WAL frames
                self._checkpoint()
            applied += len(rows)
            self._prune(rows[-1][0])
        return applied

    def _prune(self, seq):
        """The applied rows leave the queue (migration 0001's note); the next pass deletes them if this fails."""
        try:
            self.db.write(lambda conn: conn.execute("DELETE FROM index_queue WHERE seq <= ?", (seq,)))
        except Exception as error:
            log.warning("the applied index queue rows could not be deleted (%s)", type(error).__name__)

    def _add(self, project, pid, reading):
        """A passage's rows, or its new index text (its title changed, or another paper's now): an embedding
        of the old text goes. Returns whether text was replaced."""
        material, title, path, kind, text = reading[:5]
        indexed = index_text(title, path, text)
        mark = digest(indexed)
        row = self._conn.execute("SELECT id, digest, embedded FROM index_rows WHERE project_id = ? AND passage_id = ?",
                                 (project, pid)).fetchone()
        if row is None:
            self._conn.execute("INSERT INTO index_rows (project_id, passage_id, material_id, kind, digest)"
                               " VALUES (?, ?, ?, ?, ?)", (project, pid, material, kind, mark))
            self._conn.execute("INSERT INTO fts_passages (rowid, text, project_id, passage_id) VALUES (?, ?, ?, ?)",
                               (self._conn.last_insert_rowid(), indexed, project, pid))
            return False
        rowid, before, embedded = row
        self._conn.execute("UPDATE index_rows SET material_id = ?, kind = ? WHERE id = ?", (material, kind, rowid))
        if before == mark:
            return False
        self._conn.execute("UPDATE fts_passages SET text = ? WHERE rowid = ?", (indexed, rowid))
        if embedded and self.vectors:
            self._conn.execute("DELETE FROM vec_passages WHERE rowid = ?", (rowid,))
        self._conn.execute("UPDATE index_rows SET digest = ?, embedded = 0 WHERE id = ?", (mark, rowid))
        return True

    def _remove(self, project, pid):
        row = self._conn.execute("SELECT id, embedded FROM index_rows WHERE project_id = ? AND passage_id = ?",
                                 (project, pid)).fetchone()
        if row is None:
            return []
        self._conn.execute("DELETE FROM fts_passages WHERE rowid = ?", (row[0],))
        if row[1] and self.vectors:
            self._conn.execute("DELETE FROM vec_passages WHERE rowid = ?", (row[0],))
        self._conn.execute("DELETE FROM index_rows WHERE id = ?", (row[0],))
        self._verify(row[0])
        return [row[0]]

    def _verify(self, rowid):
        """Inside the pass's transaction, as a row is removed: nothing of it is left in any table, or
        the pass rolls back (rowids are never reused, so none can be another passage's)."""
        left = self._conn.execute("SELECT (SELECT count(*) FROM fts_passages WHERE rowid = ?1)"
                                  " + (SELECT count(*) FROM index_rows WHERE id = ?1)", (rowid,)).fetchone()[0]
        if self.vectors:
            left += self._conn.execute("SELECT count(*) FROM vec_passages WHERE rowid = ?", (rowid,)).fetchone()[0]
        if left:
            raise CleanupFailed()

    def _checkpoint(self):
        """Old WAL frames hold what was deleted: copied into the file, and the WAL truncated. The duty is
        recorded before it is tried and cleared only once it succeeds: one that fails for any reason (a
        reader holds it off, an I/O error) is tried again at the start of every pass and soon after
        (_retry_later), though the rows it follows are applied already. A damaged file is replaced."""
        self._truncate = True
        try:
            self._conn.wal_checkpoint(mode=apsw.SQLITE_CHECKPOINT_TRUNCATE)
        except DAMAGE:
            raise
        except apsw.Error as error:
            log.warning("the search index's WAL could not be truncated yet (%s); it is tried again", type(error).__name__)
            self._retry_later()
            return
        self._truncate = False

    # A project's rebuild

    def rebuild_project(self, project_id, run_id, stop=None):
        """Replace the project's rows from the main database (keyword rows; its vectors go, to be
        embedded again), in one transaction, so search keeps its keyword rows throughout. Done once
        per run: a run started again after a restart goes on to its embeddings. stop(), if given, is
        asked every STOP_EVERY rows: a true answer rolls the rebuild back, leaving the rows as they were (or
        withdraws it while it waits its turn)."""
        return self._write(self._rebuild_project, project_id, run_id, stop, withdraw=stop)

    def _rebuild_project(self, project_id, run_id, stop=None):
        def check(count):
            if stop is not None and count % STOP_EVERY == 0 and stop():
                raise _Stopped()
        self._apply(stop)
        if self.building or self._conn.execute("SELECT 1 FROM index_meta WHERE key = ?",
                                                (f"rebuilt:{run_id}",)).fetchone():
            return False
        try:
            check(0)
            found = self.db.read(lambda conn: readings(conn, project_id))
            with self._conn:
                old = self._conn.execute("SELECT passage_id FROM index_rows WHERE project_id = ?", (project_id,)).fetchall()
                for count, (pid,) in enumerate(old, start=1):
                    self._remove(project_id, pid)
                    check(count)
                for count, (pid, reading) in enumerate(found.items(), start=len(old) + 1):
                    self._add(project_id, pid, reading)
                    check(count)
                self._conn.execute("DELETE FROM index_meta WHERE key LIKE 'rebuilt:%'")
                self._meta(**{f"rebuilt:{run_id}": 1})
        except _Stopped:
            return False
        self._checkpoint()
        return True

    # Embeddings

    def missing(self, project_id, limit, skip=()):
        """Up to limit of the project's passages that have no embedding yet, as (rowid, passage id,
        digest), leaving out skip (rowids); never a reference passage."""
        return self._read(lambda conn: conn.execute(
            "SELECT id, passage_id, digest FROM index_rows WHERE project_id = ? AND embedded = 0 AND kind != 'reference'"
            " AND id NOT IN (SELECT value FROM json_each(?)) ORDER BY id LIMIT ?",
            (project_id, json.dumps(list(skip)), limit)).fetchall())

    def store(self, project_id, embedded, stop=None):
        """Write [(rowid, passage id, digest, vector)] in one transaction: each only while its row is
        there with that digest and no embedding (else it was removed, or its text changed, meanwhile).
        Returns how many were written. stop(), if given, withdraws it while it waits its turn."""
        return self._write(self._store, project_id, embedded, withdraw=stop)

    def _store(self, project_id, embedded):
        if self.unusable:
            raise Unavailable()
        if not self.vectors or self.building:
            return 0
        written = 0
        with self._conn:
            for rowid, pid, mark, vector in embedded:
                if len(vector) != DIMENSIONS or not self._conn.execute(
                        "SELECT 1 FROM index_rows WHERE id = ? AND project_id = ? AND passage_id = ? AND digest = ?"
                        " AND embedded = 0", (rowid, project_id, pid, mark)).fetchone():
                    continue
                self._conn.execute("INSERT INTO vec_passages (rowid, project_id, passage_id, embedding) VALUES (?, ?, ?, ?)",
                                   (rowid, project_id, pid, sqlite_vec.serialize_float32(vector)))
                self._conn.execute("UPDATE index_rows SET embedded = 1 WHERE id = ?", (rowid,))
                written += 1
        return written

    # Search

    def keyword(self, project_id, query, limit):
        """BM25's top passage ids in the project for the typed query, best first, references left out."""
        expression = match(query)
        if expression is None:
            return []
        return [pid for (pid,) in self._read(lambda conn: conn.execute(
            "SELECT passage_id FROM fts_passages WHERE fts_passages MATCH ? AND project_id = ?"
            " AND rowid NOT IN (SELECT id FROM index_rows WHERE project_id = ? AND kind = 'reference')"
            " ORDER BY rank LIMIT ?", (expression, project_id, project_id, limit)).fetchall())]

    def dense(self, project_id, vector, limit):
        """The nearest embedded passage ids in the project to vector, nearest first (references have none)."""
        if not self.vectors:
            return []
        return [pid for (pid,) in self._read(lambda conn: conn.execute(
            "SELECT passage_id FROM vec_passages WHERE embedding MATCH ? AND k = ? AND project_id = ? ORDER BY distance",
            (sqlite_vec.serialize_float32(vector), limit, project_id)).fetchall())]

    def counts(self, project_id):
        """{material id: (indexed, embedded, embeddable)} for the project's rows."""
        return {material: (indexed, embedded or 0, embeddable or 0) for material, indexed, embedded, embeddable
                in self._read(lambda conn: conn.execute(
                    "SELECT material_id, count(*), sum(embedded), sum(kind != 'reference') FROM index_rows"
                    " WHERE project_id = ? GROUP BY material_id", (project_id,)).fetchall())}

    def unembedded(self):
        """The ids of the projects some of whose passages lack embeddings."""
        return {project for (project,) in self._read(lambda conn: conn.execute(
            "SELECT DISTINCT project_id FROM index_rows WHERE embedded = 0 AND kind != 'reference'").fetchall())}

    def reconcile(self, project_id, stop=None):
        """The project's rows checked against the main database, the access check's own source: a row
        of a passage no current version in the project reads now goes, and a passage one reads that has
        no row gets one (the queue keeps both in step; this makes any drift good). Returns (added,
        removed). stop() withdraws it while it waits its turn."""
        return self._write(self._reconcile, project_id, withdraw=stop)

    def _reconcile(self, project_id):
        if self.building or self._conn is None or self.closed:
            return 0, 0
        reads = self.db.read(lambda conn: granted(conn, project_id))
        have = {pid for (pid,) in self._conn.execute("SELECT passage_id FROM index_rows WHERE project_id = ?",
                                                     (project_id,))}
        gone, new = sorted(have - reads), reads - have
        if not gone and not new:
            return 0, 0
        found = self.db.read(lambda conn: readings(conn, project_id, new)) if new else {}
        with self._conn:
            removed = [rowid for pid in gone for rowid in self._remove(project_id, pid)]
            for pid, reading in found.items():
                self._add(project_id, pid, reading)
        if removed:
            self._checkpoint()
        return len(found), len(removed)

    # Closing

    def close(self):
        """Stop the writer once the job it runs ends (a whole rebuild stops between projects), and close
        every connection."""
        with self._lock:
            if self._shut:  # closed already (the database's close closes it too)
                return
            self._shut = self.closed = True
            for timer in (self._retry, self._rebuild_retry):
                if timer is not None:
                    timer.cancel()

        def end():
            if self._conn is not None:
                self._conn.close()
                self._conn = None
        try:
            self._writer.submit(end).result()
        finally:
            self._writer.shutdown()
            self._close_readers()
