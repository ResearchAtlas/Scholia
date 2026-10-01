import asyncio
import hashlib
import io
import os
import stat
from datetime import UTC, datetime, timedelta

import pytest

from backend.db import Database, new_id
from backend.db.content import IDLE_AFTER, ContentCorruptError, ContentStore

LATER = timedelta(hours=2)  # beyond IDLE_AFTER
assert LATER > IDLE_AFTER


@pytest.fixture
def db(tmp_path):
    database = Database(tmp_path / "data")
    yield database
    database.close()


@pytest.fixture
def store(db):
    return ContentStore(db)


def later():
    return datetime.now(UTC) + LATER


def rows(db):
    return db.read(lambda conn: conn.execute("SELECT sha256, size, media_type FROM content_files ORDER BY sha256").fetchall())


def stored(store):
    return sorted(path.name for path in store.root.rglob("*") if path.is_file())


def mode(path):
    return stat.S_IMODE(path.stat().st_mode)


def age(path, by=LATER):
    """Make a file look as if it was last written `by` ago."""
    then = (datetime.now(UTC) - by).timestamp()
    os.utime(path, (then, then))


def add_material_version(db, sha256):
    def add(conn):
        general = conn.execute("SELECT id FROM projects WHERE kind = 'general'").fetchone()[0]
        material = new_id()
        conn.execute("INSERT INTO materials (id, project_id, source) VALUES (?, ?, 'upload')", (material, general))
        conn.execute(
            "INSERT INTO material_versions (id, material_id, seq, file_sha256) VALUES (?, ?, 0, ?)",
            (new_id(), material, sha256))

    db.write(add)


def add_extraction(db, sha256):
    db.write(lambda conn: conn.execute(
        "INSERT INTO extractions (id, file_sha256, extractor, extractor_version, status) VALUES (?, ?, 'x', '1', 'done')",
        (new_id(), sha256)))


def add_event_body(db, sha256):
    def add(conn):
        general = conn.execute("SELECT id FROM projects WHERE kind = 'general'").fetchone()[0]
        run = new_id()
        conn.execute("INSERT INTO runs (id, project_id, kind) VALUES (?, ?, 'background')", (run, general))
        conn.execute("INSERT INTO run_events (run_id, seq, type, body_ref) VALUES (?, 0, 'tool_result', ?)", (run, sha256))

    db.write(add)


# Writing and reading


def test_put_stores_bytes_once_under_their_hash_and_reads_them_back(store, db):
    data = b"%PDF-1.7 a paper"
    sha256 = store.put(data, media_type="application/pdf")
    assert sha256 == hashlib.sha256(data).hexdigest()
    assert (store.root / sha256[:2] / sha256).read_bytes() == data
    assert store.read(sha256) == data
    assert store.put(data, media_type="text/plain") == sha256
    assert store.put(io.BytesIO(data)) == sha256  # a stream gives the same name
    assert rows(db) == [(sha256, len(data), "application/pdf")]
    assert stored(store) == [sha256]


def test_a_stream_is_stored_in_chunks(store, monkeypatch):
    monkeypatch.setattr("backend.db.content._CHUNK", 3)
    data = bytes(range(256)) * 5
    sha256 = store.put(io.BytesIO(data))
    assert store.read(sha256) == data


def test_empty_content_can_be_stored(store, db):
    sha256 = store.put(b"")
    assert store.read(sha256) == b""
    assert rows(db) == [(sha256, 0, None)]


def test_a_corrupted_file_is_refused_and_storing_it_again_repairs_it(store):
    data = b"original bytes"
    sha256 = store.put(data)
    path = store.root / sha256[:2] / sha256
    path.write_bytes(b"original bytez")
    with pytest.raises(ContentCorruptError):
        store.read(sha256)
    path.write_bytes(data + b"x")  # a longer file is refused too
    with pytest.raises(ContentCorruptError):
        store.read(sha256)
    assert store.put(data) == sha256
    assert store.read(sha256) == data


def test_a_missing_file_raises(store):
    sha256 = store.put(b"gone")
    (store.root / sha256[:2] / sha256).unlink()
    with pytest.raises(FileNotFoundError):
        store.read(sha256)


@pytest.mark.parametrize("name", [
    "../../aab.sqlite3", "", "A" * 64, "a" * 63, "a" * 65, "g" * 64, "../" + "a" * 61, "a" * 64 + "\n", None, b"a" * 64,
])
def test_a_malformed_hash_is_refused_before_any_path_is_built(store, name):
    with pytest.raises(ValueError, match="64 lowercase"):
        store.read(name)


class Failing(io.RawIOBase):
    """A stream that fails after its first chunk."""

    def __init__(self):
        self.calls = 0

    def readable(self):
        return True

    def read(self, size=-1):
        self.calls += 1
        if self.calls > 1:
            raise OSError("the upload was cut off")
        return b"first chunk"


def test_a_write_that_fails_part_way_leaves_nothing(store, db):
    with pytest.raises(OSError, match="cut off"):
        store.put(Failing())
    assert rows(db) == []
    assert list(store.root.iterdir()) == []


def test_a_failed_row_insert_leaves_a_file_that_garbage_collection_removes(store, db, monkeypatch):
    real_write = db.write

    def failing_write(fn):
        raise OSError("disk full")

    monkeypatch.setattr(db, "write", failing_write)
    with pytest.raises(OSError, match="disk full"):
        store.put(b"orphan")
    monkeypatch.setattr(db, "write", real_write)
    sha256 = hashlib.sha256(b"orphan").hexdigest()
    assert stored(store) == [sha256] and rows(db) == []
    assert store.collect_garbage() == 0  # not idle yet
    assert store.collect_garbage(now=later()) == 1
    assert stored(store) == []


@pytest.mark.asyncio
async def test_content_calls_are_refused_on_the_event_loop(store):
    for call in (lambda: store.put(b"x"), lambda: store.read("a" * 64), store.collect_garbage):
        with pytest.raises(RuntimeError, match="event loop"):
            call()
    sha256 = await asyncio.to_thread(store.put, b"x")
    assert await asyncio.to_thread(store.read, sha256) == b"x"


def test_everything_the_store_creates_is_owner_only(tmp_path, monkeypatch):
    old = os.umask(0)
    try:
        with Database(tmp_path / "data") as db:
            store = ContentStore(db)
            sha256 = store.put(b"private")
            created = [store.root, *store.root.rglob("*")]
            assert len(created) == 3
            for path in created:
                assert mode(path) == (0o700 if path.is_dir() else 0o600), path
    finally:
        os.umask(old)


def test_existing_folder_modes_are_left_as_they_are(store):
    sha256 = store.put(b"first")
    os.chmod(store.root, 0o750)
    os.chmod(store.root / sha256[:2], 0o750)
    store.put(b"first")
    store.put(b"second")
    assert (mode(store.root), mode(store.root / sha256[:2])) == (0o750, 0o750)


# Garbage collection


def test_unreferenced_idle_files_and_rows_are_collected(store, db):
    sha256 = store.put(b"unused")
    assert store.collect_garbage() == 0  # just added
    assert store.collect_garbage(now=later()) == 1
    assert rows(db) == [] and stored(store) == []


@pytest.mark.parametrize("reference", [add_material_version, add_extraction, add_event_body])
def test_referenced_files_are_kept(store, db, reference):
    sha256 = store.put(b"in use")
    reference(db, sha256)
    assert store.collect_garbage(now=later()) == 0
    assert store.read(sha256) == b"in use"
    assert [row[0] for row in rows(db)] == [sha256]


def test_an_old_row_whose_file_was_just_written_again_is_kept(store, db):
    sha256 = store.put(b"again")
    db.write(lambda conn: conn.execute(
        "UPDATE content_files SET created_at = '2020-01-01T00:00:00.000Z'"))  # the row is long idle
    store.put(b"again")  # about to be referenced again
    assert store.collect_garbage() == 0
    assert [row[0] for row in rows(db)] == [sha256]
    assert store.read(sha256) == b"again"


class SlowStream(io.RawIOBase):
    """A stream whose copy takes longer than IDLE_AFTER: its temporary file looks old when it ends."""

    def __init__(self, store, data):
        self.store, self.data = store, data

    def readable(self):
        return True

    def read(self, size=-1):
        chunk, self.data = self.data, b""
        if not chunk:
            for tmp in self.store.root.glob(".put-*.tmp"):
                age(tmp)
        return chunk


def test_a_slow_put_counts_as_just_written(store, db):
    data = b"slow upload" * 10_000  # larger than the write buffer, so nothing is written after the stream ends
    sha256 = store.put(data)
    db.write(lambda conn: conn.execute("UPDATE content_files SET created_at = '2020-01-01T00:00:00.000Z'"))
    assert store.put(SlowStream(store, data)) == sha256  # added again, about to be referenced
    assert store.collect_garbage() == 0
    assert store.read(sha256) == data
    assert [row[0] for row in rows(db)] == [sha256]


def test_a_new_row_keeps_a_file_with_an_old_timestamp(store, db):
    sha256 = store.put(b"copied with its old time")
    age(store.root / sha256[:2] / sha256)
    assert store.collect_garbage() == 0
    assert [row[0] for row in rows(db)] == [sha256]


def test_an_unreferenced_row_whose_file_is_missing_is_removed(store, db):
    sha256 = store.put(b"lost")
    (store.root / sha256[:2] / sha256).unlink()
    assert store.collect_garbage(now=later()) == 0
    assert rows(db) == []


def test_a_file_without_a_row_is_collected_once_idle(store, db):
    sha256 = store.put(b"no row")
    db.write(lambda conn: conn.execute("DELETE FROM content_files"))  # as after a crash before the row was written
    assert store.collect_garbage() == 0
    path = store.root / sha256[:2] / sha256
    age(path)
    assert store.collect_garbage() == 1
    assert not path.exists()


def test_files_left_by_an_interrupted_put_are_collected_once_idle(store):
    store.put(b"x")
    fresh, stale = store.root / ".put-fresh.tmp", store.root / ".put-stale.tmp"
    fresh.write_bytes(b"partial")
    stale.write_bytes(b"partial")
    age(stale)
    store.collect_garbage()
    assert fresh.exists() and not stale.exists()


def test_names_the_store_did_not_write_are_left_alone(store):
    sha256 = store.put(b"x")
    db_folder = store.root / sha256[:2]
    others = [
        store.root / "notes.txt",
        store.root / "zz" / ("f" * 64),  # not a hex folder
        db_folder / "readme",
        db_folder / ("0" * 64 if not sha256.startswith("0") else "1" * 64),  # in the wrong folder
    ]
    (store.root / "zz").mkdir()
    for path in others:
        path.write_bytes(b"keep me")
        age(path)
    store.collect_garbage(now=later())
    assert all(path.exists() for path in others)


def test_collection_on_an_empty_data_folder_does_nothing(store):
    assert store.collect_garbage(now=later()) == 0
    assert not store.root.exists()


def test_a_put_between_row_removal_and_file_removal_keeps_both(store, db, monkeypatch):
    """The same content is added again after collection removed its row but before it removed the file."""
    data = b"shared bytes"
    sha256 = store.put(data)
    path = store.root / sha256[:2] / sha256
    age(path)
    db.write(lambda conn: conn.execute("UPDATE content_files SET created_at = '2020-01-01T00:00:00.000Z'"))
    real_read = db.read

    def put_then_read(fn):
        store.put(data)
        return real_read(fn)

    monkeypatch.setattr(db, "read", put_then_read)
    assert store.collect_garbage() == 0
    monkeypatch.setattr(db, "read", real_read)
    assert store.read(sha256) == data
    assert [row[0] for row in rows(db)] == [sha256]
    add_material_version(db, sha256)  # the new reference is valid


def test_a_put_after_collection_looked_for_rows_keeps_the_file(store, db, monkeypatch):
    """The same content is added again after collection read the rows, before it took the lock."""
    data = b"shared bytes"
    sha256 = store.put(data)
    path = store.root / sha256[:2] / sha256
    age(path)
    db.write(lambda conn: conn.execute("UPDATE content_files SET created_at = '2020-01-01T00:00:00.000Z'"))
    real_read = db.read

    def read_then_put(fn):
        result = real_read(fn)
        store.put(data)
        return result

    monkeypatch.setattr(db, "read", read_then_put)
    assert store.collect_garbage() == 0
    monkeypatch.setattr(db, "read", real_read)
    assert store.read(sha256) == data
    assert [row[0] for row in rows(db)] == [sha256]


def test_a_put_while_collection_checks_a_row_keeps_the_row(store, db, monkeypatch):
    """The same content is added again between collection's idle check of a row and its removal."""
    data = b"shared bytes"
    sha256 = store.put(data)
    path = store.root / sha256[:2] / sha256
    age(path)
    db.write(lambda conn: conn.execute("UPDATE content_files SET created_at = '2020-01-01T00:00:00.000Z'"))
    import backend.db.content as content_module
    real_idle = content_module._idle
    seen = []

    def idle_then_put(checked, cutoff):
        result = real_idle(checked, cutoff)
        if not seen:
            seen.append(checked)
            # The put's file write happens now; its row insert waits for this transaction.
            with store._lock:
                os.utime(checked)
        return result

    monkeypatch.setattr(content_module, "_idle", idle_then_put)
    assert store.collect_garbage() == 0  # the row went, but the file is fresh again
    monkeypatch.setattr(content_module, "_idle", real_idle)
    assert store.read(sha256) == data
    store.put(data)  # the put's row insert, after collection committed
    assert [row[0] for row in rows(db)] == [sha256]
