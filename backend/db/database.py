"""The main database: one writer thread, read connections, migrations, checks and backups.

Standard library sqlite3 only. Every call here blocks, so none may run on an
asyncio event loop; async code uses asyncio.to_thread.
"""

import asyncio
import errno
import fcntl
import json
import logging
import os
import shutil
import sqlite3
import tempfile
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

DB_NAME = "scholia.sqlite3"
APPLICATION_ID = 0x5343484C  # "SCHL", written by migration 0001
DAILY_KEPT = 7
WEEKLY_KEPT = 4
KINDS = ("daily", "weekly")  # the automatic backups, by folder under backups/
SETTINGS_FILES = ("config.toml", "AGENTS.md")  # copied into each backup: personal and per project
_STAMP = "%Y%m%dT%H%M%S%fZ"  # backup generation folder names, oldest sorts first
BUSY_TIMEOUT_MS = 5000  # how long a connection waits for another's lock
COPY_ATTEMPTS = 3  # a copy that settings or projects changed under is taken again, at most this often in all
STOP_SECONDS = 3  # how long closing waits for a running backup to stop
_PROGRESS_STEPS = 1000  # SQLite instructions between checks for a stop

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


class DatabaseClosedError(RuntimeError):
    """The database is closed or closing: nothing more is read or written through it."""


class DatabaseDamagedError(RuntimeError):
    """An integrity check failed. Writing stops and the file is left as it is."""


class BackupBusyError(RuntimeError):
    """Settings or projects changed under every attempt to copy them with the database. Nothing was kept."""


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
        self.on_damage = None  # called once, from the thread whose check found the database damaged
        self.closing = []  # called as close() starts: what reads this database closes first (S1-17: the search index)
        self._local = threading.local()
        self._readers = []
        self._readers_lock = threading.Lock()
        self._closed = False  # set when close() starts, under _readers_lock; later reads and writes are refused
        self._held = False  # set by hold_writes(), under _readers_lock: later writes are refused
        self._reads = 0  # reads in progress, which close() waits for
        self._reads_done = threading.Condition(self._readers_lock)
        self._backup_lock = threading.Lock()
        self._commit_lock = threading.Lock()  # orders the damaged flag with commits
        self._truncation_pending = False  # a WAL truncation a reader blocked, retried after each write
        _mkdir_private(self.data_dir)
        try:
            # A new database is created owner-only before SQLite opens it. SQLite gives
            # the WAL and shared-memory files the database file's mode (and resets an
            # empty one to it on every open), so they are owner-only too.
            _create_private(self.path)
        except FileExistsError:  # refuse another app's, a newer or a damaged file before anything writes to it
            _open_checked(self.path, "quick_check", latest=len(migrations)).close()
        self._writer = ThreadPoolExecutor(1, thread_name_prefix="scholia-db-writer")
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
        with self._readers_lock:  # admitted and queued in one step, so never queued behind close() or a hold
            if self._closed or self._held:
                raise DatabaseClosedError("the database is closed")
            future = self._writer.submit(self._transaction, fn)
        return future.result()

    def hold_writes(self):
        """Refuse every write from now on (DatabaseClosedError) and wait for those already queued
        to commit, whatever thread queued them: after it, nothing commits until release_writes().
        A restore holds them from its safety copy until the database closes."""
        _refuse_event_loop()
        with self._readers_lock:
            if self._closed:
                raise DatabaseClosedError("the database is closed")
            self._held = True
        self._writer.submit(lambda: None).result()  # the single writer runs in order: every earlier write is done

    def release_writes(self):
        """Admit writes again after hold_writes()."""
        _refuse_event_loop()
        with self._readers_lock:
            self._held = False

    def read(self, fn):
        """Run fn(conn) in one read transaction on this thread's read-only connection."""
        _refuse_event_loop()
        with self._readers_lock:
            if self._closed:
                raise DatabaseClosedError("the database is closed")
            self._reads += 1
        try:
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
        finally:
            with self._readers_lock:
                self._reads -= 1
                self._reads_done.notify_all()

    @property
    def closed(self):
        """Whether close() has started. A backup in progress stops at its next check."""
        return self._closed

    @property
    def damaged(self):
        """Why writing stopped (a failed integrity check), or None."""
        return self._damaged

    def backup(self, now=None):
        """Check the database, write a backup generation and rotate old ones.

        A generation holds a copy of the database, backup.json (app, schema and
        SQLite versions) and the settings files that exist (SETTINGS_FILES, at
        the top of the data folder and in the projects/<id>/ folder of each
        project in the copy), as one state of the data folder even while the app
        changes it (see _copy).

        now, an aware UTC datetime, names the generation and defaults to the
        current time. Returns the new generation's folder. A failed integrity
        check stops all later writes and raises DatabaseDamagedError. Closing
        stops a backup in progress, which then raises DatabaseClosedError and
        leaves no generation.
        """
        _refuse_event_loop()
        with self._backup_lock:
            generation = self._backup(now or datetime.now(UTC))
        self._retry_truncation()
        return generation

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
            generation = self._backup(now)
        self._retry_truncation()
        return generation

    def purge_backups(self, now=None):
        """Take a fresh backup, then delete every other automatic backup, daily and weekly.

        Data deleted before this call is then in no automatic backup. Older
        generations are deleted only once the fresh one is published, so a failed
        backup deletes nothing. Returns (the fresh generation, how many were deleted).
        """
        _refuse_event_loop()
        with self._backup_lock:
            older = sum(len(_generations(self.backups_dir / kind)) for kind in KINDS)
            generation = self._backup(now or datetime.now(UTC))  # its rotation may move or remove some of them
            for old in [old for kind in KINDS for old in _generations(self.backups_dir / kind) if old != generation]:
                shutil.rmtree(old)
        self._retry_truncation()
        return generation, older

    def snapshot(self, target):
        """Write into target, a new folder, what a backup generation holds, without publishing it.

        Returns backup.json's fields. Used for full backups, which add the content
        store. Raises as backup() does.
        """
        _refuse_event_loop()
        with self._backup_lock:
            source = self._checked_source()
            try:
                info = self._copy(source, Path(target))
            finally:
                source.close()
        self._retry_truncation()
        return info

    def checkpoint(self):
        """Copy the WAL into the database file and truncate it to zero bytes.

        Deleted content stays in old WAL frames until then. Waits up to the busy
        timeout for readers, and returns False if one still needed the WAL. The
        truncation is then retried, without waiting, after each later write until
        it succeeds. While a backup reads, which can take longer than that, it does
        not wait, so writes are not held up behind it; the backup retries it when it
        ends. Refused once writing has stopped.
        """
        _refuse_event_loop()
        if threading.get_ident() == self._writer_ident:
            raise RuntimeError("checkpoint() cannot be called from inside a write")
        with self._readers_lock:
            if self._closed:
                raise DatabaseClosedError("the database is closed")
            future = self._writer.submit(self._truncate_wal, not self._backup_lock.locked())
        return future.result()

    def close(self):
        """Close the database. Reads and writes that start after this are refused
        (DatabaseClosedError); reads in progress finish first, and a write already
        submitted runs before the writer connection closes. So work still running when
        the app stops gets an error, never a connection closed under it. A backup in
        progress is stopped, and waited for up to STOP_SECONDS. What reads this database
        (closing: the search index) is closed first, its work drained."""
        _refuse_event_loop()
        if threading.get_ident() == self._writer_ident:
            raise RuntimeError("close() cannot be called from inside a write")
        for close in list(self.closing):  # each closes once, its work drained while this database still reads
            try:
                close()
            except Exception as error:
                log.warning("closing what reads the database failed (%s)", type(error).__name__)
        with self._readers_lock:
            if self._closed:  # closed already, or closing in another thread
                return
            self._closed = True  # a backup's progress handler and step checks see this and stop
            while self._reads:
                self._reads_done.wait()
            readers, self._readers = self._readers, []
        for conn in readers:
            conn.close()
        if self._backup_lock.acquire(timeout=STOP_SECONDS):
            self._backup_lock.release()
        else:
            log.warning("a backup did not stop within %s s of closing", STOP_SECONDS)
        self._writer.submit(self._close_writer).result()
        self._writer.shutdown()
        self._writer_ident = None  # a later thread may get its id; it must be told the database is closed

    # Writer thread

    def _open_writer(self, migrations):
        conn = _connect(self.path)
        try:
            if _switch_to_wal(conn) != "wal":
                raise RuntimeError("the database could not switch to WAL mode")
            new = None  # whether this startup found a new, empty database
            while True:
                # Decide under the write lock, so migrations another connection applied
                # meanwhile are seen and not run again. The backup then copies exactly
                # the state the migration starts from.
                conn.execute("BEGIN IMMEDIATE")
                version, has_schema = _usable_state(conn, len(migrations))
                if new is None:
                    new = not has_schema
                if version == len(migrations):
                    conn.execute("ROLLBACK")
                    break
                if not new:  # a database this startup created holds nothing to back up, before any migration
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
        if self._truncation_pending:
            try:
                self._truncate_wal(wait=False)
            except Exception as error:  # the write is committed; the next one retries
                log.warning("retrying the WAL truncation failed (%s)", type(error).__name__)
        return result

    def _truncate_wal(self, wait):
        if self._damaged:
            raise DatabaseDamagedError(self._damaged)
        conn = self._conn
        (timeout,) = conn.execute("PRAGMA busy_timeout").fetchone()
        if not wait:
            conn.execute("PRAGMA busy_timeout = 0")
        try:
            (busy, _, _) = conn.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()
        finally:
            conn.execute(f"PRAGMA busy_timeout = {timeout}")
        self._truncation_pending = busy != 0
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
        source = self._checked_source()
        try:
            stamp = now.strftime(_STAMP)
            tmp = daily / f".{stamp}.tmp"
            published = None
            try:
                self._copy(source, tmp)
                for path in (*sorted(tmp.rglob("*"), key=lambda p: -len(p.parts)), tmp):  # files before their folders
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

    def _checked_source(self):
        """The live database, open read-only after a full integrity check. A failed check stops
        all later writes (DatabaseDamagedError). Closing stops the check and every statement on it."""
        try:
            return _open_checked(self.path, "integrity_check", stop=self._stop_requested)
        except DatabaseDamagedError as error:
            with self._commit_lock:
                first, self._damaged = self._damaged is None, str(error)
            if first and self.on_damage is not None:
                try:
                    self.on_damage()
                except Exception as failure:  # the check's own answer stands
                    log.warning("reporting a damaged database failed (%s)", type(failure).__name__)
            raise
        except sqlite3.OperationalError:
            self._raise_if_closed()
            raise

    def _copy(self, source, target):
        """Copy the database (VACUUM INTO, then checked) and the settings files into target, a new
        folder, as one state of the data folder. Returns backup.json's fields, written there too.

        The app may create or delete a project or save a setting meanwhile, without waiting for
        this. So the settings files copied are the personal ones and those of each project in the
        copy (never a folder whose record is not committed yet), and the settings files and project
        folders are checked unchanged from before the database was copied to after the files were:
        then the files are those of the moment the copy read. Otherwise the copy is taken again, up
        to COPY_ATTEMPTS in all, and then BackupBusyError. ponytail: retried, not locked out, since
        such changes are rare; a folder changing faster than the copy takes keeps it from finishing.
        """
        os.mkdir(target, 0o700)
        for _ in range(COPY_ATTEMPTS):
            before = _file_state(self.data_dir)
            copy = target / DB_NAME
            _create_private(copy)
            try:
                source.execute("VACUUM INTO ?", (str(copy),))
                check = _open_checked(copy, "quick_check", stop=self._stop_requested)
            except DatabaseDamagedError as error:
                raise RuntimeError(f"the backup copy failed its check: {error}") from error
            except sqlite3.OperationalError:
                self._raise_if_closed()
                raise
            with closing(check):
                schema_version = check.execute("PRAGMA user_version").fetchone()[0]
                has_projects = check.execute("SELECT 1 FROM sqlite_schema WHERE name = 'projects'").fetchone()
                projects = {project_id for (project_id,) in check.execute("SELECT id FROM projects")} \
                    if has_projects else set()  # before migration 0001
            self._raise_if_closed()
            _copy_settings(self.data_dir, target, projects)
            if _file_state(self.data_dir) == before:
                info = {"app_version": APP_VERSION, "schema_version": schema_version,
                        "sqlite_version": sqlite3.sqlite_version}
                _create_private(target / "backup.json", json.dumps(info).encode())
                return info
            self._raise_if_closed()
            for entry in target.iterdir():  # changed meanwhile: start again from an empty folder
                shutil.rmtree(entry) if entry.is_dir() else entry.unlink()
        raise BackupBusyError("settings or projects changed during every attempt to back them up")

    def _stop_requested(self):
        return self._closed  # a progress handler: nonzero stops the running statement

    def _raise_if_closed(self):
        if self._closed:
            raise DatabaseClosedError("the database is closing; the backup stopped")

    def _retry_truncation(self):
        """Retry a WAL truncation a backup's reading held off (see checkpoint)."""
        if not self._truncation_pending:
            return
        with self._readers_lock:
            if self._closed:
                return
            future = self._writer.submit(self._truncate_wal, False)
        try:
            future.result()
        except Exception as error:  # the next write retries it
            log.warning("retrying the WAL truncation after a backup failed (%s)", type(error).__name__)

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


def _open_checked(path, check, latest=None, stop=None):
    """Open path read-only and run PRAGMA check (quick_check or integrity_check).

    Returns the open connection. Raises DatabaseDamagedError when the file is
    corrupt or the check fails. When latest is given (the live database at
    startup), also raises ForeignDatabaseError for a file that is not this app's
    database, unless it is new and empty, and NewerDatabaseError when
    user_version is above latest; and a database that will be migrated gets
    integrity_check instead of check. Nothing is written to the file or its WAL.
    stop, if given, is the connection's progress handler: when it returns true,
    the running statement stops with an "interrupted" OperationalError.
    """
    conn = None
    try:
        conn = _connect(path, readonly=True)
        if stop is not None:
            conn.set_progress_handler(stop, _PROGRESS_STEPS)
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
        if _is_damage(error):
            raise DatabaseDamagedError(f"{check} failed: {error}") from error
        raise


def _is_damage(error):
    """Whether an SQLite error says the file is corrupt or not a database, as opposed to busy,
    locked or unreadable."""
    return (getattr(error, "sqlite_errorcode", None) or 0) & 0xFF in (sqlite3.SQLITE_CORRUPT, sqlite3.SQLITE_NOTADB)


def check_identity(path, latest=len(MIGRATIONS)):
    """Check the database file at path as opening it would (see _usable_state), writing nothing
    beside it: no lock, WAL or shared-memory file is made there. A write-ahead log left beside it
    (after a crash) is read too, from an owner-only copy of both in a private temporary folder,
    since it may hold the newest schema.
    Raises ForeignDatabaseError for another application's file, NewerDatabaseError for a newer
    schema, DatabaseDamagedError for a file SQLite reads as corrupt or not a database, and OSError
    when it could not be checked: the file cannot be read, the copy cannot be made, or SQLite
    failed for another reason (such as an I/O error)."""
    _refuse_event_loop()
    path = Path(path).resolve()
    path.open("rb").close()  # one this account cannot read is not checked, rather than taken for damaged
    wal = path.with_name(path.name + "-wal")
    if wal.is_symlink() or not wal.is_file() or not wal.stat().st_size:
        return _check_identity(path.as_uri() + "?mode=ro&immutable=1", latest)  # immutable: no log is read
    # ponytail: copies the whole database (only when a crash left a log); link instead if that is too slow
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as folder:
        copy = Path(folder) / path.name
        for source, target in ((path, copy), (wal, copy.with_name(copy.name + "-wal"))):
            _create_private(target)  # owner-only, as the database it copies; copyfile keeps that mode
            shutil.copyfile(source, target)
        return _check_identity(copy.as_uri() + "?mode=ro", latest)


def _check_identity(uri, latest):
    conn = None
    try:
        conn = sqlite3.connect(uri, uri=True)
        _usable_state(conn, latest)
    except (ForeignDatabaseError, NewerDatabaseError):
        raise
    except sqlite3.DatabaseError as error:
        if not _is_damage(error):
            raise OSError(errno.EIO, "the database could not be checked") from error  # such as an I/O error
        raise DatabaseDamagedError(f"the file cannot be read as a database: {error}") from error
    finally:
        if conn is not None:
            conn.close()


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


def _settings_folders(data_dir):
    """The folders that hold settings files: the data folder, then each projects/<id>/ folder.
    Symbolic links are skipped."""
    folders = [data_dir]
    projects = data_dir / "projects"
    if projects.is_dir() and not projects.is_symlink():
        folders += sorted(path for path in projects.iterdir() if path.is_dir() and not path.is_symlink())
    return folders


def _file_state(data_dir):
    """What a backup copies from outside the database, as it is now: each settings file's identity,
    size and change times, or None where there is none, in the data folder and every project folder.
    Any save (a replaced file), project folder made or removed, or edit changes it."""
    state = {}
    for folder in _settings_folders(data_dir):
        for name in SETTINGS_FILES:
            try:
                info = os.lstat(folder / name)
                state[folder / name] = (info.st_ino, info.st_mode, info.st_size, info.st_mtime_ns, info.st_ctime_ns)
            except FileNotFoundError:
                state[folder / name] = None
    return state


def _copy_settings(data_dir, target, projects):
    """Copy the settings files that exist into target at the same relative paths, owner-only.

    Only SETTINGS_FILES, at the top of the data folder and in the projects/<id>/
    folder of each id in projects; symbolic links are skipped.
    """
    for folder in _settings_folders(data_dir):
        if folder != data_dir and folder.name not in projects:
            continue  # a project's folder is written before its record, so this one is not committed yet
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
            _create_private(target / relative, content)


def list_generations(data_dir):
    """The automatic backups in a data folder, newest first: each one's kind, name, folder, time, app
    and schema versions (None when its backup.json cannot be read) and size in bytes."""
    listed = []
    for kind in KINDS:
        for folder in _generations(Path(data_dir) / "backups" / kind):
            try:
                info = json.loads((folder / "backup.json").read_bytes())
            except (OSError, ValueError):
                info = {}
            info = info if isinstance(info, dict) else {}
            try:
                size = sum(path.lstat().st_size for path in folder.rglob("*") if path.is_file())
            except FileNotFoundError:  # rotated or purged while listed
                continue
            listed.append({
                "kind": kind, "name": folder.name, "folder": folder,
                "time": _stamp_of(folder).isoformat(timespec="milliseconds").replace("+00:00", "Z"),
                "app_version": info.get("app_version"), "schema_version": info.get("schema_version"),
                "size": size,
            })
    return sorted(listed, key=lambda generation: generation["time"], reverse=True)


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
