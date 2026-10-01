import json
import logging
import os
import shutil
import signal
import sqlite3
import stat
import subprocess
import sys
import threading
import time
from contextlib import suppress
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

import backend.db.database as database_module
from backend import APP_VERSION
from backend.db import (
    APPLICATION_ID,
    DB_NAME,
    Database,
    DatabaseDamagedError,
    ForeignDatabaseError,
    NewerDatabaseError,
    new_id,
)
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


def root_page(path, table):
    """The root b-tree page of table, read without writing: (page number, page size).

    Uses sqlite_schema, which every SQLite build has (dbstat is optional).
    """
    conn = sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True)
    try:
        (page,) = conn.execute("SELECT rootpage FROM sqlite_schema WHERE name = ?", (table,)).fetchone()
        return page, conn.execute("PRAGMA page_size").fetchone()[0]
    finally:
        conn.close()


def mode(path):
    return stat.S_IMODE(path.stat().st_mode)


def mismatch_table_and_index(data):
    """A database whose projects table disagrees with its primary key index.

    quick_check does not compare tables with their indexes, so it still opens;
    integrity_check fails.
    """
    project, renamed = new_id(), new_id()
    with Database(data) as db:
        db.write(lambda conn: conn.execute(
            "INSERT INTO projects (id, name, kind) VALUES (?, 'p', 'research')", (project,)))
    page, size = root_page(data / DB_NAME, "projects")  # two rows: the root is the only page
    with open(data / DB_NAME, "r+b") as file:
        file.seek((page - 1) * size)
        content = file.read(size)
        assert content.count(project.encode()) == 1
        file.seek((page - 1) * size)
        file.write(content.replace(project.encode(), renamed.encode()))


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


def test_an_existing_database_at_version_zero_is_backed_up_before_migrating(tmp_path):
    data = tmp_path / "data"
    data.mkdir()
    conn = sqlite3.connect(data / DB_NAME)
    conn.execute("CREATE TABLE notes (text TEXT)")
    conn.execute("INSERT INTO notes VALUES ('kept')")
    conn.execute(f"PRAGMA application_id = {APPLICATION_ID}")
    conn.commit()
    conn.close()

    Database(data).close()
    generation, *later_ones = generations(data)
    assert len(later_ones) == len(MIGRATIONS) - 1  # one backup before each migration
    backup = sqlite3.connect((data / "backups" / "daily" / generation / DB_NAME).as_uri() + "?mode=ro", uri=True)
    try:
        assert backup.execute("PRAGMA user_version").fetchone()[0] == 0
        assert backup.execute("SELECT text FROM notes").fetchall() == [("kept",)]
    finally:
        backup.close()


@pytest.mark.parametrize("application_id, version, has_table", [
    (0, 1, True),
    (0, 0, True),
    (0x12345678, 1, False),
], ids=["no-id-in-range-version", "no-id-no-version", "other-id"])
def test_another_apps_database_is_refused_and_left_unchanged(tmp_path, application_id, version, has_table):
    data = tmp_path / "data"
    data.mkdir()
    conn = sqlite3.connect(data / DB_NAME)
    if has_table:
        conn.execute("CREATE TABLE other (x)")
    conn.execute(f"PRAGMA application_id = {application_id}")
    conn.execute(f"PRAGMA user_version = {version}")
    conn.close()
    before = snapshot(data)

    with pytest.raises(ForeignDatabaseError, match="not this app's database"):
        Database(data)
    assert snapshot(data) == before
    assert not (data / "backups").exists()


@pytest.mark.parametrize("content", ["zero bytes", "empty database"])
def test_an_empty_existing_file_becomes_this_apps_database(tmp_path, content):
    data = tmp_path / "data"
    data.mkdir()
    if content == "zero bytes":
        (data / DB_NAME).touch()
    else:
        conn = sqlite3.connect(data / DB_NAME)
        conn.execute("PRAGMA journal_mode = WAL")  # writes a header, but no schema
        conn.close()

    with Database(data) as db:
        stamped = db.read(lambda conn: (
            conn.execute("PRAGMA application_id").fetchone()[0], conn.execute("PRAGMA user_version").fetchone()[0]))
    assert stamped == (APPLICATION_ID, len(MIGRATIONS))


def test_a_backup_is_taken_before_each_migration_of_an_existing_database(tmp_path):
    data = tmp_path / "data"
    Database(data).close()
    assert not (data / "backups").exists()  # a new database had nothing to back up

    later = ("CREATE TABLE second (x INTEGER) STRICT;", "CREATE TABLE third (x INTEGER) STRICT;")
    with Database(data, migrations=MIGRATIONS + later) as db:
        assert {"second", "third"} <= table_names(db)
    daily = data / "backups" / "daily"
    assert [user_version(daily / name / DB_NAME) for name in generations(data)] == [len(MIGRATIONS), len(MIGRATIONS) + 1]


def test_a_failed_migration_keeps_the_previous_schema(tmp_path):
    data = tmp_path / "data"
    with Database(data) as db:
        db.write(add_audit_row)
    broken = "CREATE TABLE extra (x INTEGER) STRICT; INSERT INTO extra VALUES (1); INSERT INTO missing VALUES (1);"

    with pytest.raises(sqlite3.OperationalError, match="missing"):
        Database(data, migrations=MIGRATIONS + (broken,))
    assert user_version(data / DB_NAME) == len(MIGRATIONS)
    with Database(data) as db:
        assert "extra" not in table_names(db)
        assert audit_events(db) == [("kept",)]
    assert len(generations(data)) == 1  # the backup taken before the attempt


def open_together(data, migrations=MIGRATIONS):
    """Open two Database instances on data from two threads at once."""
    start = threading.Barrier(2)
    opened, errors = [], []

    def open_one():
        start.wait()
        try:
            opened.append(Database(data, migrations=migrations))
        except Exception as error:
            errors.append(error)

    threads = [threading.Thread(target=open_one) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(30)
    for db in opened:
        db.close()
    return errors


def test_two_instances_migrating_an_outdated_database_apply_each_migration_once(tmp_path, monkeypatch):
    data = tmp_path / "data"
    Database(data).close()
    both_at_backup = threading.Barrier(2, timeout=1)
    real_backup = Database._backup

    def backup_together(self, now):
        # Hold each instance at its pre-migration backup until the other arrives. An
        # instance that chose its migration from a version read before taking the
        # write lock would arrive too, and then run a migration already applied.
        with suppress(threading.BrokenBarrierError):
            both_at_backup.wait()
        return real_backup(self, now)

    monkeypatch.setattr(Database, "_backup", backup_together)
    errors = open_together(data, MIGRATIONS + ("CREATE TABLE second (x INTEGER) STRICT;",))
    assert errors == []
    assert user_version(data / DB_NAME) == len(MIGRATIONS) + 1
    assert len(generations(data)) == 1  # only the instance that migrated backed up


def test_two_instances_starting_on_a_new_data_folder_both_open_it(tmp_path):
    # The interleaving varies between attempts; repeating makes a regression likely to show.
    for attempt in range(20):
        data = tmp_path / f"data{attempt}"
        assert open_together(data) == []
        with Database(data) as db:
            assert db.read(lambda conn: conn.execute("SELECT count(*) FROM projects").fetchone()) == (1,)


def hold_write_lock(path):
    """A connection holding the write lock on path, as another instance's setup would."""
    conn = sqlite3.connect(path, autocommit=True, check_same_thread=False)
    conn.execute("BEGIN IMMEDIATE")
    return conn


def test_a_startup_waits_for_a_lock_held_while_it_switches_to_wal(tmp_path):
    # Switching to WAL upgrades a read lock to a write lock, and SQLite reports busy
    # for that at once, without calling the busy handler.
    data = tmp_path / "data"
    data.mkdir()
    (data / DB_NAME).touch()  # a new, empty file another instance is setting up
    blocker = hold_write_lock(data / DB_NAME)
    release = threading.Timer(0.3, blocker.execute, ("ROLLBACK",))
    release.start()
    try:
        with Database(data) as db:
            assert db.read(lambda conn: conn.execute("SELECT count(*) FROM projects").fetchone()) == (1,)
    finally:
        release.join()
        blocker.close()


def test_the_wal_switch_stops_waiting_after_the_busy_timeout(tmp_path, monkeypatch):
    monkeypatch.setattr(database_module, "BUSY_TIMEOUT_MS", 200)
    data = tmp_path / "data"
    data.mkdir()
    (data / DB_NAME).touch()
    blocker = hold_write_lock(data / DB_NAME)
    try:
        started = time.monotonic()
        with pytest.raises(sqlite3.OperationalError, match="locked"):
            Database(data)
        assert 0.2 <= time.monotonic() - started < 5
    finally:
        blocker.close()
    assert (data / DB_NAME).stat().st_size == 0


def test_the_startup_check_reads_the_file_header_in_one_snapshot(tmp_path, monkeypatch):
    """Another instance commits migration 0001 just before the startup check reads the schema list."""
    data = tmp_path / "data"
    data.mkdir()
    (data / DB_NAME).touch()  # a new, empty file that another instance is setting up
    real_connect = database_module._connect
    hooked, other = [], []

    def connect(path, **kwargs):
        conn = real_connect(path, **kwargs)
        if kwargs.get("readonly") and not hooked:  # this instance's startup check only
            hooked.append(conn)

            def before_each_statement(sql):
                if "sqlite_schema" in sql and not other:
                    other.append("started")
                    try:
                        Database(data).close()
                        other[0] = "migrated"
                    except Exception as error:  # a trace callback cannot raise; record it
                        other[0] = error

            conn.set_trace_callback(before_each_statement)
        return conn

    monkeypatch.setattr(database_module, "_connect", connect)
    with Database(data) as db:
        assert db.read(lambda conn: conn.execute("SELECT count(*) FROM projects").fetchone()) == (1,)
    assert other == ["migrated"]


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
        assert db.read(lambda conn: conn.execute("PRAGMA user_version").fetchone()) == (len(MIGRATIONS),)
        assert "extra" not in table_names(db)
        assert audit_events(db) == [("kept",)]
    with Database(data, migrations=MIGRATIONS + ("CREATE TABLE extra (x INTEGER) STRICT;",)) as db:
        assert "extra" in table_names(db)


# Integrity checks


def test_a_corrupt_database_is_refused_at_startup_and_left_unchanged(tmp_path):
    data = tmp_path / "data"
    with Database(data) as db:
        db.write(lambda conn: conn.executemany("INSERT INTO audit_log (event) VALUES (?)", [("e",)] * 2000))
    page, size = root_page(data / DB_NAME, "audit_log")
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
    mismatch_table_and_index(data)

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


def test_a_damaged_database_that_needs_migrating_is_refused_before_any_change(tmp_path):
    data = tmp_path / "data"
    mismatch_table_and_index(data)  # passes quick_check, fails integrity_check
    conn = sqlite3.connect(data / DB_NAME)
    conn.execute("PRAGMA journal_mode = DELETE")  # opening it in WAL mode would change its header
    conn.close()
    before = snapshot(data)

    with pytest.raises(DatabaseDamagedError, match="integrity_check"):
        Database(data, migrations=MIGRATIONS + ("CREATE TABLE second (x INTEGER) STRICT;",))
    assert snapshot(data) == before
    assert not (data / "backups").exists()


def test_a_write_in_flight_when_damage_is_found_does_not_commit(tmp_path):
    data = tmp_path / "data"
    mismatch_table_and_index(data)
    db = Database(data)
    started, proceed, outcome = threading.Event(), threading.Event(), []

    def paused(conn):
        conn.execute("INSERT INTO audit_log (event) VALUES ('in flight')")
        started.set()
        assert proceed.wait(10)

    def run():
        try:
            db.write(paused)
            outcome.append("committed")
        except DatabaseDamagedError:
            outcome.append("refused")

    writer = threading.Thread(target=run)
    try:
        writer.start()
        assert started.wait(10)
        with pytest.raises(DatabaseDamagedError, match="integrity_check"):
            db.backup()
        proceed.set()
        writer.join(10)
        assert outcome == ["refused"]
        assert audit_events(db) == []
    finally:
        proceed.set()
        db.close()


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
    assert {path.name for path in generation.iterdir()} == {DB_NAME, "backup.json"}
    assert json.loads((generation / "backup.json").read_text()) == {
        "app_version": APP_VERSION, "schema_version": len(MIGRATIONS), "sqlite_version": sqlite3.sqlite_version}
    assert mode(generation / "backup.json") == 0o600
    conn = sqlite3.connect((generation / DB_NAME).as_uri() + "?mode=ro", uri=True)
    try:
        assert conn.execute("PRAGMA user_version").fetchone()[0] == len(MIGRATIONS)
        assert conn.execute("PRAGMA application_id").fetchone()[0] == APPLICATION_ID
        assert conn.execute("PRAGMA quick_check").fetchall() == [("ok",)]
        assert conn.execute("SELECT event FROM audit_log").fetchall() == [("kept",)]
    finally:
        conn.close()


@pytest.mark.parametrize("failing", ["copy", "temporary folder", "daily folder"])
def test_a_failed_backup_leaves_no_generation_and_is_retried(tmp_path, monkeypatch, failing):
    """A sync failure, before or after the rename, leaves nothing that counts as a backup."""
    real_fsync = database_module._fsync
    targets = {
        "copy": lambda path: path.name == DB_NAME,
        "temporary folder": lambda path: path.name.endswith(".tmp"),
        "daily folder": lambda path: path.name == "daily",  # synced after the rename
    }

    def failing_sync(path):
        if targets[failing](Path(path)):
            raise OSError("disk failure")
        real_fsync(path)

    data = tmp_path / "data"
    with Database(data) as db:
        monkeypatch.setattr(database_module, "_fsync", failing_sync)
        with pytest.raises(OSError, match="disk failure"):
            db.backup_if_due(now=START)
        assert list((data / "backups" / "daily").iterdir()) == []
        db.write(add_audit_row)
        assert audit_events(db) == [("kept",)]

        monkeypatch.setattr(database_module, "_fsync", real_fsync)
        assert db.backup_if_due(now=START + timedelta(minutes=1)) is not None


def test_a_backup_whose_retention_fails_still_counts_and_retention_is_retried(tmp_path, monkeypatch, caplog):
    data = tmp_path / "data"
    real_rotate = database_module._rotate

    def failing_rotate(backups, keep):
        raise OSError(13, "Permission denied", str(backups))

    with Database(data) as db:
        for day in range(7):
            db.backup(now=START + timedelta(days=day))
        monkeypatch.setattr(database_module, "_rotate", failing_rotate)
        with caplog.at_level(logging.WARNING, logger="backend.db.database"):
            generation = db.backup(now=START + timedelta(days=7))  # published; retention then fails
        assert generation.is_dir()
        assert len(generations(data, "daily")) == 8
        assert "retention" in caplog.text
        assert str(data) not in caplog.text  # logs carry no file paths

        monkeypatch.setattr(database_module, "_rotate", real_rotate)
        assert db.backup_if_due(now=START + timedelta(days=7, hours=1)) is None  # that backup counted
    assert len(generations(data, "daily")) == 7  # and retention ran on the next check
    assert generations(data, "weekly") == [START.strftime("%Y%m%dT%H%M%S%fZ")]


def test_rotation_never_removes_the_generation_just_published(tmp_path):
    """After the clock moves back, a new backup can sort before the kept ones."""
    data = tmp_path / "data"
    with Database(data) as db:
        for day in range(40):  # dailies at days 33 to 39, weeklies at 7, 14, 21 and 28
            db.backup(now=START + timedelta(days=day))
        generation = db.backup(now=START + timedelta(days=32))
        assert (generation / DB_NAME).is_file()
    assert generation.name in generations(data, "daily")
    assert len(generations(data, "daily")) == 7
    assert len(generations(data, "weekly")) == 4


def test_a_migration_does_not_run_without_its_backup(tmp_path, monkeypatch):
    data = tmp_path / "data"
    Database(data).close()

    def losing_rotate(backups, keep):  # as if retention had removed the new generation
        for generation in (backups / "daily").iterdir():
            shutil.rmtree(generation)

    monkeypatch.setattr(database_module, "_rotate", losing_rotate)
    with pytest.raises(RuntimeError, match="backup"):
        Database(data, migrations=MIGRATIONS + ("CREATE TABLE second (x INTEGER) STRICT;",))
    assert user_version(data / DB_NAME) == len(MIGRATIONS)


def test_a_backup_includes_the_settings_files_and_nothing_else(tmp_path, open_umask):
    data = tmp_path / "data"
    project, empty_project = new_id(), new_id()
    settings = {
        "config.toml": '[ui]\nlanguage = "en"\n',
        "AGENTS.md": "Personal instructions\n",
        f"projects/{project}/config.toml": "[project]\n",
        f"projects/{project}/AGENTS.md": "Project instructions\n",
    }
    others = {
        "credentials.json": '{"openrouter": "never copied"}',  # the key fallback file
        f"projects/{project}/notes.txt": "not a settings file",
        "logs/app.log": "not a settings file",
    }
    with Database(data) as db:
        for name, text in {**settings, **others}.items():
            (data / name).parent.mkdir(parents=True, exist_ok=True)
            (data / name).write_text(text)
        (data / "projects" / empty_project).mkdir()
        generation = db.backup()

    copied = {str(path.relative_to(generation)) for path in generation.rglob("*") if path.is_file()}
    assert copied == {DB_NAME, "backup.json", *settings}
    for name, text in settings.items():
        assert (generation / name).read_text() == text
    for path in generation.rglob("*"):
        assert mode(path) == (0o700 if path.is_dir() else 0o600), path


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
        # SQLite gives the WAL and shared-memory files the database file's mode, and
        # resets an empty one to it on every open, so they are never broader than it.
        sidecars = (mode(data / f"{DB_NAME}-wal"), mode(data / f"{DB_NAME}-shm"))
    assert sidecars == (0o640, 0o640)
    assert (mode(data), mode(data / DB_NAME), mode(data / "backups")) == (0o750, 0o640, 0o750)
