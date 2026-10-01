import os
import signal
import sqlite3
import stat
import subprocess
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

import backend.db.database as database_module
from backend.db import DB_NAME, Database, DatabaseDamagedError, NewerDatabaseError, new_id
from backend.db.migrations import MIGRATIONS
from network_guard import allow_subprocess

ROOT = Path(__file__).resolve().parents[1]
START = datetime(2026, 1, 1, 3, 0, tzinfo=UTC)


def add_audit_row(conn):
    conn.execute("INSERT INTO audit_log (event) VALUES ('kept')")


def audit_events(db):
    return db.read(lambda conn: conn.execute("SELECT event FROM audit_log").fetchall())


def table_names(db):
    return db.read(lambda conn: {row[0] for row in conn.execute("SELECT name FROM sqlite_schema WHERE type = 'table'")})


def user_version(path):
    conn = sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True)
    try:
        return conn.execute("PRAGMA user_version").fetchone()[0]
    finally:
        conn.close()


def generations(data, kind="daily"):
    folder = data / "backups" / kind
    return sorted(path.name for path in folder.iterdir()) if folder.exists() else []


def snapshot(data):
    """The bytes of the database file and its WAL; a missing WAL counts as empty."""
    return {
        name: (data / name).read_bytes() if (data / name).exists() else b""
        for name in (DB_NAME, f"{DB_NAME}-wal")
    }


def leaf_page(path, table):
    """A leaf page of table, read without writing: (page number, page size)."""
    conn = sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True)
    try:
        pages = [row[0] for row in conn.execute(
            "SELECT pageno FROM dbstat WHERE name = ? AND pagetype = 'leaf' ORDER BY pageno", (table,))]
        return pages[len(pages) // 2], conn.execute("PRAGMA page_size").fetchone()[0]
    finally:
        conn.close()


def mode(path):
    return stat.S_IMODE(path.stat().st_mode)


@pytest.fixture
def open_umask():
    """A permissive umask, so owner-only modes must come from the code."""
    old = os.umask(0)
    try:
        yield
    finally:
        os.umask(old)


# Migrations and refusals


def test_a_newer_schema_is_refused_and_left_unchanged(tmp_path):
    data = tmp_path / "data"
    Database(data).close()
    conn = sqlite3.connect(data / DB_NAME)
    conn.execute(f"PRAGMA user_version = {len(MIGRATIONS) + 1}")
    conn.close()
    before = snapshot(data)

    with pytest.raises(NewerDatabaseError, match="newer version"):
        Database(data)
    assert snapshot(data) == before
    assert not (data / "backups").exists()


def test_a_backup_is_taken_before_each_migration_of_an_existing_database(tmp_path):
    data = tmp_path / "data"
    Database(data).close()
    assert not (data / "backups").exists()  # a new database had nothing to back up

    later = ("CREATE TABLE second (x INTEGER) STRICT;", "CREATE TABLE third (x INTEGER) STRICT;")
    with Database(data, migrations=MIGRATIONS + later) as db:
        assert {"second", "third"} <= table_names(db)
    daily = data / "backups" / "daily"
    assert [user_version(daily / name / DB_NAME) for name in generations(data)] == [1, 2]


def test_a_failed_migration_keeps_the_previous_schema(tmp_path):
    data = tmp_path / "data"
    with Database(data) as db:
        db.write(add_audit_row)
    broken = "CREATE TABLE extra (x INTEGER) STRICT; INSERT INTO extra VALUES (1); INSERT INTO missing VALUES (1);"

    with pytest.raises(sqlite3.OperationalError, match="missing"):
        Database(data, migrations=MIGRATIONS + (broken,))
    assert user_version(data / DB_NAME) == 1
    with Database(data) as db:
        assert "extra" not in table_names(db)
        assert audit_events(db) == [("kept",)]
    assert len(generations(data)) == 1  # the backup taken before the attempt


KILLED_MIGRATION = r"""
import os, signal, sqlite3, sys
from backend.db import Database
from backend.db.migrations import MIGRATIONS

real_connect = sqlite3.connect

def connect(*args, **kwargs):
    conn = real_connect(*args, **kwargs)
    conn.create_function("crash", 0, lambda: os.kill(os.getpid(), signal.SIGKILL))
    conn.execute("PRAGMA cache_size = 10")  # spill uncommitted pages into the WAL
    return conn

sqlite3.connect = connect
killed = '''
CREATE TABLE extra (x INTEGER) STRICT;
INSERT INTO extra WITH RECURSIVE n(i) AS (SELECT 1 UNION ALL SELECT i + 1 FROM n WHERE i < 100000) SELECT i FROM n;
SELECT crash();
'''
Database(sys.argv[1], migrations=MIGRATIONS + (killed,))
"""


def test_a_process_killed_mid_migration_leaves_the_previous_schema(tmp_path):
    data = tmp_path / "data"
    with Database(data) as db:
        db.write(add_audit_row)

    with allow_subprocess(sys.executable):  # the child imports only the standard library and backend.db
        child = subprocess.run(
            [sys.executable, "-c", KILLED_MIGRATION, str(data)], cwd=ROOT, capture_output=True, timeout=60)
    assert child.returncode == -signal.SIGKILL, child.stderr.decode()
    assert (data / f"{DB_NAME}-wal").stat().st_size > 0  # uncommitted pages reached the disk

    with Database(data) as db:  # passes quick_check
        assert db.read(lambda conn: conn.execute("PRAGMA user_version").fetchone()) == (1,)
        assert "extra" not in table_names(db)
        assert audit_events(db) == [("kept",)]
    with Database(data, migrations=MIGRATIONS + ("CREATE TABLE extra (x INTEGER) STRICT;",)) as db:
        assert "extra" in table_names(db)


# Integrity checks


def test_a_corrupt_database_is_refused_at_startup_and_left_unchanged(tmp_path):
    data = tmp_path / "data"
    with Database(data) as db:
        db.write(lambda conn: conn.executemany("INSERT INTO audit_log (event) VALUES (?)", [("e",)] * 2000))
    page, size = leaf_page(data / DB_NAME, "audit_log")
    with open(data / DB_NAME, "r+b") as file:
        file.seek((page - 1) * size)
        file.write(b"\xff" * 16)  # a b-tree page header no reader accepts
    before = snapshot(data)

    with pytest.raises(DatabaseDamagedError, match="quick_check"):
        Database(data)
    assert snapshot(data) == before


def test_a_file_that_is_not_a_database_is_refused_and_left_unchanged(tmp_path):
    data = tmp_path / "data"
    data.mkdir()
    (data / DB_NAME).write_bytes(b"not a database " * 400)
    before = snapshot(data)

    with pytest.raises(DatabaseDamagedError, match="quick_check"):
        Database(data)
    assert snapshot(data) == before


def test_a_failed_integrity_check_stops_writes_and_leaves_the_file_unchanged(tmp_path):
    data = tmp_path / "data"
    project, renamed = new_id(), new_id()
    with Database(data) as db:
        db.write(lambda conn: conn.execute(
            "INSERT INTO projects (id, name, kind) VALUES (?, 'p', 'research')", (project,)))
    # Change the id in the table row only, so it disagrees with the primary key index:
    # quick_check does not compare tables with their indexes, integrity_check does.
    page, size = leaf_page(data / DB_NAME, "projects")
    with open(data / DB_NAME, "r+b") as file:
        file.seek((page - 1) * size)
        content = file.read(size)
        assert content.count(project.encode()) == 1
        file.seek((page - 1) * size)
        file.write(content.replace(project.encode(), renamed.encode()))

    db = Database(data)
    try:
        db.write(add_audit_row)  # committed to the WAL, not yet checkpointed
        main_file = (data / DB_NAME).read_bytes()
        with pytest.raises(DatabaseDamagedError, match="integrity_check"):
            db.backup()
        with pytest.raises(DatabaseDamagedError):
            db.write(add_audit_row)
    finally:
        db.close()
    assert (data / DB_NAME).read_bytes() == main_file  # the WAL was not checkpointed into it
    assert (data / f"{DB_NAME}-wal").stat().st_size > 0
    assert list((data / "backups" / "daily").iterdir()) == []


# Backups


def test_a_backup_is_a_checked_generation_and_a_stale_temporary_one_is_removed(tmp_path):
    data = tmp_path / "data"
    stale = data / "backups" / "daily" / ".20260101T000000000000Z.tmp"
    with Database(data) as db:
        db.write(add_audit_row)
        stale.mkdir(parents=True)
        (stale / DB_NAME).write_bytes(b"partial")
        generation = db.backup()

    assert not stale.exists()
    assert generation.parent == data / "backups" / "daily"
    assert list(generation.iterdir()) == [generation / DB_NAME]
    conn = sqlite3.connect((generation / DB_NAME).as_uri() + "?mode=ro", uri=True)
    try:
        assert conn.execute("PRAGMA user_version").fetchone()[0] == len(MIGRATIONS)
        assert conn.execute("PRAGMA quick_check").fetchall() == [("ok",)]
        assert conn.execute("SELECT event FROM audit_log").fetchall() == [("kept",)]
    finally:
        conn.close()


def test_a_failed_backup_leaves_no_partial_generation_and_writes_continue(tmp_path, monkeypatch):
    def failing_sync(path):
        raise OSError("disk failure")

    data = tmp_path / "data"
    with Database(data) as db:
        monkeypatch.setattr(database_module, "_fsync", failing_sync)
        with pytest.raises(OSError, match="disk failure"):
            db.backup()
        assert list((data / "backups" / "daily").iterdir()) == []
        db.write(add_audit_row)
        assert audit_events(db) == [("kept",)]


def test_rotation_keeps_seven_daily_and_four_weekly_generations(tmp_path):
    data = tmp_path / "data"
    with Database(data) as db:
        for day in range(40):
            db.backup(now=START + timedelta(days=day))

    def names(days):
        return [(START + timedelta(days=day)).strftime("%Y%m%dT%H%M%S%fZ") for day in days]

    assert generations(data, "daily") == names(range(33, 40))
    assert generations(data, "weekly") == names([7, 14, 21, 28])


def test_an_automatic_backup_runs_at_most_once_a_day(tmp_path):
    with Database(tmp_path / "data") as db:
        assert db.backup_if_due(now=START) is not None
        assert db.backup_if_due(now=START + timedelta(hours=23)) is None
        assert db.backup_if_due(now=START + timedelta(hours=24)) is not None


# Owner-only files


def test_everything_created_in_the_data_folder_is_owner_only(tmp_path, open_umask):
    data = tmp_path / "data"
    with Database(data) as db:
        db.write(add_audit_row)
        for day in range(8):  # one more than the daily generations kept, so one moves to weekly
            db.backup(now=START + timedelta(days=day))
        created = [data, *data.rglob("*")]
        modes = {str(path.relative_to(data)): mode(path) for path in created}

    assert {DB_NAME, f"{DB_NAME}-wal", f"{DB_NAME}-shm", "backups/weekly"} <= set(modes)
    assert (len(generations(data, "daily")), len(generations(data, "weekly"))) == (7, 1)
    for path in created:
        expected = 0o700 if path.is_dir() else 0o600
        assert modes[str(path.relative_to(data))] == expected, path


def test_existing_permissions_are_left_as_they_are(tmp_path):
    data = tmp_path / "data"
    Database(data).close()
    (data / "backups").mkdir()
    os.chmod(data, 0o750)
    os.chmod(data / DB_NAME, 0o640)
    os.chmod(data / "backups", 0o750)

    with Database(data) as db:
        db.write(add_audit_row)
        db.backup()
        assert mode(data / f"{DB_NAME}-wal") == 0o640  # SQLite gives it the database file's mode
    assert (mode(data), mode(data / DB_NAME), mode(data / "backups")) == (0o750, 0o640, 0o750)
