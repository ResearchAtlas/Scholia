"""The main database: one writer thread, read connections, migrations, checks and backups.

Standard library sqlite3 only. Every call here blocks, so none may run on an
asyncio event loop; async code uses asyncio.to_thread.
"""

import asyncio
import fcntl
import json
import logging
import os
import shutil
import sqlite3
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from datetime import UTC, datetime, timedelta
from pathlib import Path

from backend import APP_VERSION
from backend.db.migrations import MIGRATIONS

log = logging.getLogger(__name__)

DB_NAME = "aab.sqlite3"
APPLICATION_ID = 0x41414252  # "AABR", written by migration 0001
DAILY_KEPT = 7
WEEKLY_KEPT = 4
SETTINGS_FILES = ("config.toml", "AGENTS.md")  # copied into each backup: personal and per project
_STAMP = "%Y%m%dT%H%M%S%fZ"  # backup generation folder names, oldest sorts first
BUSY_TIMEOUT_MS = 5000  # how long a connection waits for another's lock

_PRAGMAS = (
    "foreign_keys = ON",
    f"busy_timeout = {BUSY_TIMEOUT_MS}",
    "synchronous = FULL",
    "fullfsync = ON",  # macOS: F_FULLFSYNC on commit, not a plain fsync
    "checkpoint_fullfsync = ON",
    "secure_delete = ON",
    "journal_size_limit = 67108864",
    "temp_store = MEMORY",
    "recursive_triggers = ON",  # REPLACE must fire delete triggers (off by default)
)


class NewerDatabaseError(RuntimeError):
    """The database was written by a newer version of the app. It is left unchanged."""


class ForeignDatabaseError(RuntimeError):
    """The file is not this app's database. It is left unchanged."""


class DatabaseDamagedError(RuntimeError):
    """An integrity check failed. Writing stops and the file is left as it is."""


def new_id() -> str:
    return str(uuid.uuid4())


def utc_now() -> str:
    """The current time in the schema's form, e.g. 2026-10-02T03:18:00.123Z."""
    return datetime.now(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")


class Database:
    """The main database in a data folder.

    Opening checks an existing file read-only (a newer schema is refused and
    quick_check must pass), then migrates it on the writer thread, taking a
    backup before each migration unless the database is new and empty.
    """

    def __init__(self, data_dir, *, migrations=MIGRATIONS):
        _refuse_event_loop()
        self.data_dir = Path(data_dir)
        self.path = self.data_dir / DB_NAME
        self.backups_dir = self.data_dir / "backups"
        self._damaged = None  # why writing stopped, once an integrity check fails
        self._local = threading.local()
        self._readers = []
        self._readers_lock = threading.Lock()
        self._backup_lock = threading.Lock()
        self._commit_lock = threading.Lock()  # orders the damaged flag with commits
        _mkdir_private(self.data_dir)
        try:
            # A new database is created owner-only before SQLite opens it. SQLite gives
            # the WAL and shared-memory files the database file's mode (and resets an
            # empty one to it on every open), so they are owner-only too.
            _create_private(self.path)
        except FileExistsError:  # refuse another app's, a newer or a damaged file before anything writes to it
            _open_checked(self.path, "quick_check", latest=len(migrations)).close()
        self._writer = ThreadPoolExecutor(1, thread_name_prefix="aab-db-writer")
        try:
            self._writer_ident = self._writer.submit(self._open_writer, migrations).result()
        except BaseException:
            self._writer.shutdown()
            raise

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()

    def write(self, fn):
        """Run fn(conn) on the writer thread in one BEGIN IMMEDIATE transaction.

        Returns fn's result after COMMIT. If fn raises, the transaction rolls
        back and the exception propagates. fn must not commit or roll back.
        """
        _refuse_event_loop()
        if threading.get_ident() == self._writer_ident:
            raise RuntimeError("write() cannot be called from inside a write")
        return self._writer.submit(self._transaction, fn).result()

    def read(self, fn):
        """Run fn(conn) in one read transaction on this thread's read-only connection."""
        _refuse_event_loop()
        conn = getattr(self._local, "conn", None)
        if conn is None:
            conn = _connect(self.path, readonly=True, check_same_thread=False)
            with self._readers_lock:
                self._readers.append(conn)
            self._local.conn = conn
        conn.execute("BEGIN")
        try:
            return fn(conn)
        finally:
            if conn.in_transaction:
                conn.execute("ROLLBACK")

    def backup(self, now=None):
        """Check the database, write a backup generation and rotate old ones.

        A generation holds a copy of the database, backup.json (app, schema and
        SQLite versions) and the settings files that exist (SETTINGS_FILES, at
        the top of the data folder and in each projects/<id>/ folder).

        now, an aware UTC datetime, names the generation and defaults to the
        current time. Returns the new generation's folder. A failed integrity
        check stops all later writes and raises DatabaseDamagedError.
        """
        _refuse_event_loop()
        with self._backup_lock:
            return self._backup(now or datetime.now(UTC))

    def backup_if_due(self, now=None):
        """Back up unless the latest backup is less than a day old. Returns the folder or None.

        Retention runs either way, so a rotation an earlier backup could not
        finish is retried.
        """
        _refuse_event_loop()
        now = now or datetime.now(UTC)
        with self._backup_lock:
            latest = _generations(self.backups_dir / "daily")[-1:]
            if latest and timedelta(0) <= now - _stamp_of(latest[0]) < timedelta(days=1):
                self._apply_retention()
                return None
            return self._backup(now)

    def checkpoint(self):
        """Copy the WAL into the database file and truncate it to zero bytes.

        Deleted content stays in old WAL frames until then. Returns False if a
        reader still needed the WAL, so it could not be truncated; a later
        checkpoint does it. Refused once writing has stopped.
        """
        _refuse_event_loop()
        if threading.get_ident() == self._writer_ident:
            raise RuntimeError("checkpoint() cannot be called from inside a write")
        return self._writer.submit(self._checkpoint).result()

    def close(self):
        _refuse_event_loop()
        if threading.get_ident() == self._writer_ident:
            raise RuntimeError("close() cannot be called from inside a write")
        with self._readers_lock:
            readers, self._readers = self._readers, []
        for conn in readers:
            conn.close()
        self._writer.submit(self._close_writer).result()
        self._writer.shutdown()

    # Writer thread

    def _open_writer(self, migrations):
        conn = _connect(self.path)
        try:
            if _switch_to_wal(conn) != "wal":
                raise RuntimeError("the database could not switch to WAL mode")
            while True:
                # Decide under the write lock, so migrations another connection applied
                # meanwhile are seen and not run again. The backup then copies exactly
                # the state the migration starts from.
                conn.execute("BEGIN IMMEDIATE")
                version, has_schema = _usable_state(conn, len(migrations))
                if version == len(migrations):
                    conn.execute("ROLLBACK")
                    break
                if has_schema:  # skip only a new, empty database
                    with self._backup_lock:
                        generation = self._backup(datetime.now(UTC))
                    if not (generation / DB_NAME).is_file():
                        raise RuntimeError("the backup taken before this migration is missing, so it did not run")
                # One transaction: the script, then the user_version bump, then COMMIT.
                conn.executescript(f"{migrations[version]}\nPRAGMA user_version = {version + 1};\nCOMMIT;")
        except BaseException:
            if conn.in_transaction:
                conn.execute("ROLLBACK")
            self._conn = conn
            self._close_writer()
            raise
        self._conn = conn
        return threading.get_ident()

    def _transaction(self, fn):
        if self._damaged:
            raise DatabaseDamagedError(self._damaged)
        conn = self._conn
        conn.execute("BEGIN IMMEDIATE")
        try:
            result = fn(conn)
            with self._commit_lock:  # damage found while fn ran stops this commit too
                if self._damaged:
                    raise DatabaseDamagedError(self._damaged)
                conn.execute("COMMIT")
        except BaseException:
            if conn.in_transaction:
                conn.execute("ROLLBACK")
            raise
        return result

    def _checkpoint(self):
        if self._damaged:
            raise DatabaseDamagedError(self._damaged)
        (busy, _, _) = self._conn.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()
        return busy == 0

    def _close_writer(self):
        if self._damaged:
            # Closing the last connection would checkpoint the WAL into the damaged file.
            self._conn.setconfig(sqlite3.SQLITE_DBCONFIG_NO_CKPT_ON_CLOSE, True)
        self._conn.close()

    # Backups

    def _backup(self, now):
        daily = self.backups_dir / "daily"
        _mkdir_private(self.backups_dir)
        _mkdir_private(daily)
        for stale in daily.glob(".*.tmp"):  # left by a crash during an earlier backup
            shutil.rmtree(stale)
        try:
            source = _open_checked(self.path, "integrity_check")
        except DatabaseDamagedError as error:
            with self._commit_lock:
                self._damaged = str(error)
            raise
        try:
            stamp = now.strftime(_STAMP)
            tmp = daily / f".{stamp}.tmp"
            os.mkdir(tmp, 0o700)
            published = None
            try:
                copy, info = tmp / DB_NAME, tmp / "backup.json"
                _create_private(copy)
                source.execute("VACUUM INTO ?", (str(copy),))
                try:
                    check = _open_checked(copy, "quick_check")
                except DatabaseDamagedError as error:
                    raise RuntimeError(f"the backup copy failed its check: {error}") from error
                with closing(check):
                    schema_version = check.execute("PRAGMA user_version").fetchone()[0]
                _create_private(info, json.dumps({
                    "app_version": APP_VERSION,
                    "schema_version": schema_version,
                    "sqlite_version": sqlite3.sqlite_version,
                }).encode())
                settings = _copy_settings(self.data_dir, tmp)
                for path in (copy, info, *reversed(settings), tmp):  # files before their folders
                    _fsync(path)
                generation = daily / stamp
                os.rename(tmp, generation)
                published = generation
                _fsync(daily)
            except BaseException:
                # A failed backup must not count as one, even after its rename.
                shutil.rmtree(published or tmp, ignore_errors=True)
                raise
        finally:
            source.close()
        self._apply_retention(keep=generation)
        return generation

    def _apply_retention(self, keep=None):
        """Rotate old generations, never keep. A failure is logged, not raised: the
        backup itself is already published, and the next backup or check retries this."""
        try:
            _rotate(self.backups_dir, keep)
        except Exception as error:
            log.warning(
                "backup retention failed (%s, errno %s); it is retried at the next backup check",
                type(error).__name__, getattr(error, "errno", None),
            )


def _connect(path, *, readonly=False, check_same_thread=True):
    if readonly:
        conn = sqlite3.connect(
            Path(path).resolve().as_uri() + "?mode=ro", uri=True,
            autocommit=True, check_same_thread=check_same_thread,
        )
    else:
        conn = sqlite3.connect(path, autocommit=True, check_same_thread=check_same_thread)
    try:
        for pragma in _PRAGMAS:
            conn.execute(f"PRAGMA {pragma}")
    except BaseException:
        conn.close()
        raise
    return conn


def _switch_to_wal(conn):
    """Set WAL mode and return the journal mode, retrying while another connection holds a lock.

    The switch upgrades a read lock to a write lock, and SQLite reports busy for
    that at once instead of calling the busy handler, since waiting there could
    deadlock. Two instances setting up a new data folder meet this, so the switch
    is retried for up to the busy timeout.
    """
    deadline = time.monotonic() + BUSY_TIMEOUT_MS / 1000
    while True:
        try:
            return conn.execute("PRAGMA journal_mode = WAL").fetchone()[0]
        except sqlite3.OperationalError as error:
            if error.sqlite_errorcode & 0xFF != sqlite3.SQLITE_BUSY or time.monotonic() >= deadline:
                raise
        time.sleep(0.01)


def _open_checked(path, check, latest=None):
    """Open path read-only and run PRAGMA check (quick_check or integrity_check).

    Returns the open connection. Raises DatabaseDamagedError when the file is
    corrupt or the check fails. When latest is given (the live database at
    startup), also raises ForeignDatabaseError for a file that is not this app's
    database, unless it is new and empty, and NewerDatabaseError when
    user_version is above latest; and a database that will be migrated gets
    integrity_check instead of check. Nothing is written to the file or its WAL.
    """
    conn = None
    try:
        conn = _connect(path, readonly=True)
        if latest is not None:  # the live database at startup
            version, has_schema = _usable_state(conn, latest)
            if has_schema and version < latest:
                check = "integrity_check"  # it is about to be migrated: check it fully before anything changes it
        rows = conn.execute(f"PRAGMA {check}").fetchall()
        if rows != [("ok",)]:
            raise DatabaseDamagedError(f"{check} failed: {rows[0][0]}")
        return conn
    except BaseException as error:
        if conn is not None:
            conn.close()
        # Corrupt or not a database, as opposed to busy, locked or unreadable.
        if (getattr(error, "sqlite_errorcode", None) or 0) & 0xFF in (sqlite3.SQLITE_CORRUPT, sqlite3.SQLITE_NOTADB):
            raise DatabaseDamagedError(f"{check} failed: {error}") from error
        raise


def _usable_state(conn, latest):
    """(user_version, whether it has any schema), if this app can open and migrate it.

    Raises ForeignDatabaseError for another application's file (a new, empty file
    is accepted; migration 0001 stamps it) and NewerDatabaseError for a schema
    above latest.
    """
    version, app_id, has_schema = conn.execute(  # one statement, so one consistent snapshot
        "SELECT user_version, application_id, EXISTS (SELECT 1 FROM sqlite_schema)"
        " FROM pragma_user_version, pragma_application_id"
    ).fetchone()
    new_file = app_id == version == 0 and not has_schema
    if app_id != APPLICATION_ID and not new_file:
        raise ForeignDatabaseError(
            f"This file is not this app's database (application id {app_id:#x}). It was left unchanged."
        )
    if version > latest:
        raise NewerDatabaseError(
            f"This database was written by a newer version of the app (schema {version}; "
            f"this version knows up to {latest}). Update the app, or restore a backup."
        )
    return version, bool(has_schema)


def _rotate(backups, keep):
    """Keep the newest daily generations; promote one a week to weekly, keeping the newest of those.

    keep, a generation just published (or None), is never moved or removed and
    counts as the newest daily one whatever its date, since the clock may have
    moved back.
    """
    daily, weekly = backups / "daily", backups / "weekly"
    others = [generation for generation in _generations(daily) if generation != keep]
    kept_others = DAILY_KEPT - 1 if keep is not None else DAILY_KEPT
    for old in others[:max(0, len(others) - kept_others)]:
        kept = _generations(weekly)
        if not kept or _stamp_of(old) - _stamp_of(kept[-1]) >= timedelta(days=7):
            _mkdir_private(weekly)
            os.rename(old, weekly / old.name)
        else:
            shutil.rmtree(old)
    for old in _generations(weekly)[:-WEEKLY_KEPT]:
        shutil.rmtree(old)


def _copy_settings(data_dir, target):
    """Copy the settings files that exist into target at the same relative paths, owner-only.

    Only SETTINGS_FILES, at the top of the data folder and in each projects/<id>/
    folder; symbolic links are skipped. Returns the paths created, each folder
    before the files in it.
    """
    folders = [data_dir]
    projects = data_dir / "projects"
    if projects.is_dir() and not projects.is_symlink():
        folders += sorted(path for path in projects.iterdir() if path.is_dir() and not path.is_symlink())
    created = []
    for folder in folders:
        for name in SETTINGS_FILES:
            source = folder / name
            if source.is_symlink() or not source.is_file():
                continue
            try:
                content = source.read_bytes()
            except FileNotFoundError:  # removed since the listing
                continue
            relative = source.relative_to(data_dir)
            for parent in reversed(relative.parents[:-1]):  # e.g. projects, then projects/<id>
                if not (target / parent).exists():
                    _mkdir_private(target / parent)
                    created.append(target / parent)
            _create_private(target / relative, content)
            created.append(target / relative)
    return created


def _generations(folder):
    """Finished backup generations in folder, oldest first. Other entries are ignored."""
    if not folder.is_dir():
        return []
    return sorted(path for path in folder.iterdir() if _stamp_of(path))


def _stamp_of(path):
    try:
        return datetime.strptime(path.name, _STAMP).replace(tzinfo=UTC)
    except ValueError:
        return None


def _create_private(path, content=b""):
    """Create a new file owner-only (0600). Raises FileExistsError if it exists."""
    with open(path, "xb", opener=lambda name, flags: os.open(name, flags, 0o600)) as file:
        file.write(content)


def _mkdir_private(path):
    """Create a folder owner-only (0700). An existing folder keeps its permissions."""
    try:
        os.mkdir(path, 0o700)
    except FileExistsError:
        pass


def _fsync(path):
    fd = os.open(path, os.O_RDONLY)
    try:
        if hasattr(fcntl, "F_FULLFSYNC"):
            fcntl.fcntl(fd, fcntl.F_FULLFSYNC)  # macOS: flush the drive's cache as well
        else:
            os.fsync(fd)
    finally:
        os.close(fd)


def _refuse_event_loop():
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return
    raise RuntimeError("database calls block; run them off the event loop with asyncio.to_thread")
