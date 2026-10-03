"""Backups, restore and project export: the API under Settings, then Advanced.

Automatic backups (backend.db.database) are taken at launch, while the app is idle
(once a day, checked every IDLE_CHECK_SECONDS when no run is active), and before a
bulk deletion (backup_before_deletion). purge() takes them out of deleted data's way.
Full backups and project exports are zip files in a folder the researcher chose,
written under a temporary name and renamed when complete. One that holds a Private
or Local only project is AES-encrypted with the researcher's passphrase (pyzipper)
and refused without one; others are plain zip files. Neither holds a stored key, the
logs or the lock file, and every config.toml in them keeps only the settings the app
knows, without comments (settings.known_only), so nothing typed into it by hand leaves.
A restore stops running work, waits out every other request, saves a safety copy, puts
a backup's database and settings files in place, reopens the database and starts the
harness again in the same process, then reports referenced content files that are
missing. Every one of these is audited, a restore finished at launch after a crash too.

Database and file work runs off the event loop.
"""

import asyncio
import contextlib
import errno
import hashlib
import json
import logging
import mimetypes
import os
import re
import shutil
import sqlite3
import uuid
from contextlib import closing
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from fastapi.routing import APIRoute
from pydantic import BaseModel, Field

from backend import APP_VERSION
from backend.db import (DB_NAME, BackupBusyError, Database, DatabaseClosedError, DatabaseDamagedError,
                        ForeignDatabaseError, NewerDatabaseError, utc_now)
from backend.db.database import KINDS, SETTINGS_FILES, _fsync, _mkdir_private, _open_checked, _stamp_of, \
    list_generations
from backend.db.migrations import MIGRATIONS
from backend.runs import _through
from backend.settings import known_only, write_private

log = logging.getLogger(__name__)

IDLE_CHECK_SECONDS = 600
SENSITIVE = ("private", "local_only")  # a backup or export holding such a project is encrypted
STAGING = ".staging"  # under backups/: restores and full backups in progress, emptied at launch and by a committed restore
# Under backups/: damaged databases a restore moved aside. Nothing removes one on its own; only the
# researcher's "Delete everywhere including backups" does (purge), as a deletion reaches every copy.
DAMAGED = "damaged"
_CHUNK = 1 << 20
_UUID = r"[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}"
# What a backup may hold; anything else in a backup file refuses it.
_BACKUP_ENTRY = re.compile(
    rf"{re.escape(DB_NAME)}|backup\.json|config\.toml|AGENTS\.md|projects/{_UUID}/(config\.toml|AGENTS\.md)"
    r"|content/([0-9a-f]{2})/\2[0-9a-f]{62}")


class BackupError(Exception):
    """A refused or failed request: status, a stable code the interface translates, and an English message."""

    def __init__(self, status: int, code: str, message: str):
        super().__init__(message)
        self.status, self.code, self.message = status, code, message


class _Route(APIRoute):
    """Answers a BackupError as the app answers its own errors: {"code", "message"}."""

    def get_route_handler(self):
        handler = super().get_route_handler()

        async def handle(request):
            try:
                return await handler(request)
            except BackupError as error:
                return JSONResponse({"code": error.code, "message": error.message}, status_code=error.status)
        return handle


# What a limited app (a damaged database, or a restore neither finished nor undone) and a restore in
# progress leave served (see Gate): health, the backups list and restore.
LIMITED = {("GET", "/api/health"), ("GET", "/api/backups"), ("POST", "/api/backups/restore")}


class Writers:
    """What a restore waits out: every API request but LIMITED is inside it, shared; a restore
    takes it alone. A waiting restore goes first, so new requests queue behind it."""

    def __init__(self):
        self._changed = asyncio.Condition()
        self._shared = 0
        self._alone = False
        self._waiting = 0

    @contextlib.asynccontextmanager
    async def shared(self):
        async with self._changed:
            await self._changed.wait_for(lambda: not self._alone and not self._waiting)
            self._shared += 1
        try:
            yield
        finally:
            self._shared -= 1  # at once, so a cancellation below cannot keep the slot
            if self._waiting:  # only a waiting restore waits for the slots; otherwise a request ends at once
                await asyncio.shield(self._notify())

    async def _notify(self):
        async with self._changed:
            self._changed.notify_all()

    @contextlib.asynccontextmanager
    async def alone(self):
        async with self._changed:
            self._waiting += 1
            try:
                await self._changed.wait_for(lambda: not self._alone and not self._shared)
            finally:
                self._waiting -= 1
                self._changed.notify_all()  # one that leaves without entering (cancelled) lets the queue on
            self._alone = True
        try:
            yield
        finally:
            self._alone = False
            await asyncio.shield(self._notify())


def _limit(state, code, reason):
    """Put the app in the limited state: only LIMITED is served (see Gate), and health says why."""
    state["damaged"], state["damaged_code"] = reason, code


def limited(state):
    """(code, why) when only LIMITED is served, else None: the app was limited (_limit), or the
    database it runs on found itself damaged since it opened (the full check of a backup, at
    launch, while idle or asked for), which stops its writes."""
    if "damaged" in state:
        return state.get("damaged_code", "database_damaged"), state["damaged"]
    db = state.get("db")
    if db is not None and db.damaged is not None:
        return "database_damaged", "A check found the database damaged; restore a backup"
    return None


class Gate:
    """Every API request passes it (pure ASGI, so streamed responses pass through untouched).

    Only LIMITED is served while the app is limited (see limited: a database damaged at startup or
    found damaged since, or a restore that could neither be finished nor undone; 503 with its code)
    and while a restore runs (state["restoring"]; 503 restoring): nothing else reads or writes then,
    GET routes that write an audit row and turn admissions included. Every other request runs
    inside state["writers"] shared, so a restore, once it has stopped admissions, waits until none
    is left before it copies or swaps anything; the state is checked again once a request is
    inside, since it may have changed while the request waited."""

    def __init__(self, app, state):
        self.app, self.state = app, state

    async def __call__(self, scope, receive, send):
        if (scope["type"] != "http" or not scope["path"].startswith("/api/")
                or (scope["method"], scope["path"]) in LIMITED):
            return await self.app(scope, receive, send)
        writers = self.state.get("writers")
        if (refused := self._refused()) is not None or writers is None:
            return await (refused or self.app)(scope, receive, send)
        async with writers.shared():
            return await (self._refused() or self.app)(scope, receive, send)

    def _refused(self):
        if (why := limited(self.state)) is not None:
            return JSONResponse({"code": why[0], "message": "Scholia can only restore a backup now"}, status_code=503)
        if self.state.get("restoring"):
            return JSONResponse({"code": "restoring", "message": "A backup is being restored"}, status_code=503)
        return None


class FullBackup(BaseModel):
    destination: str = Field(min_length=1, max_length=4096)  # an absolute path to a folder
    passphrase: str | None = Field(default=None, max_length=1024)


class Restore(BaseModel):
    generation: str | None = Field(default=None, max_length=100)  # an automatic backup's id, as listed
    file: str | None = Field(default=None, min_length=1, max_length=4096)  # or a full backup file
    passphrase: str | None = Field(default=None, max_length=1024)


class Export(BaseModel):
    destination: str = Field(min_length=1, max_length=4096)
    passphrase: str | None = Field(default=None, max_length=1024)


@contextlib.asynccontextmanager
async def lifespan(app):
    """Inside the app's own lifespan: clear what a crash left in staging, back up while idle, and
    at closing close the database and harness a restore opened (the app closes the ones it did)."""
    state = app.state.scholia
    state["backups_lock"] = asyncio.Lock()  # one backup, restore or export at a time
    state["restore_lock"] = asyncio.Lock()  # one restore at a time
    state["writers"] = Writers()  # see Gate
    opened = state.get("db"), state.get("harness")
    if not await asyncio.to_thread(_replay_pending, state["data_dir"]):  # else a restore is still to be finished
        await asyncio.to_thread(shutil.rmtree, state["data_dir"] / "backups" / STAGING, ignore_errors=True)
    idle = asyncio.create_task(_idle_backups(state))
    try:
        yield
    finally:
        idle.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await idle
        if (state.get("db"), state.get("harness")) != opened:
            if state.get("harness") is not None:
                await state["harness"].shutdown()
            if state.get("db") is not None:
                await asyncio.to_thread(state["db"].close)


router = APIRouter(lifespan=lifespan, route_class=_Route)


async def _idle_backups(state):
    """While the app stays open, take the day's backup at a check that finds no run active."""
    while True:
        await asyncio.sleep(IDLE_CHECK_SECONDS)
        async with state["backups_lock"]:
            db, harness = state.get("db"), state.get("harness")
            if db is None or harness is None or harness.registry.runs:
                continue
            try:
                await asyncio.to_thread(db.backup_if_due)
            except Exception as error:  # the next check tries again
                log.warning("the idle backup failed (%s)", type(error).__name__)


async def _to_end(awaitable):
    """Await to its end; a cancellation is raised after it (see backend.app._to_end)."""
    result, cancelled = await _through(awaitable)
    if cancelled:
        raise asyncio.CancelledError()
    return result


def _db(state) -> Database:
    db = state.get("db")
    if db is None or db.closed:
        raise BackupError(503, "database_unavailable", "The database is not open")
    return db


# For the deletion endpoints


def backup_before_deletion(db):
    """The automatic backup before a bulk deletion (a project), whatever the day's backup did.
    Raises when it fails, so the deletion does not go ahead without it."""
    return db.backup()


def purge(db, project_id, deleted):
    """"Delete everywhere including backups", after the deletion committed: a fresh automatic backup,
    then no older one, no damaged copy a restore moved aside and nothing left in staging that no
    restore still to be finished needs (see _purge_staging), audited. deleted says what was deleted
    ({"kind", "object_id"}). Returns {"purged_backups": how many backups and copies were deleted},
    with "staging_left": True when something in staging could not be removed. A failed backup
    deletes nothing and raises. The caller orders it with restores (see backups_lock)."""
    generation, removed = db.purge_backups()
    copies = sorted((db.backups_dir / DAMAGED).glob("*")) if (db.backups_dir / DAMAGED).is_dir() else []
    for copy in copies:
        _remove(copy)
    staged, left = _purge_staging(db.data_dir)
    db.write(lambda conn: conn.execute(
        "INSERT INTO audit_log (event, project_id, data) VALUES ('backup_purge', ?, ?)",
        (project_id, json.dumps({**deleted, "kept": f"daily/{generation.name}", "deleted_backups": removed,
                                 "deleted_damaged_copies": len(copies), "deleted_staging": staged,
                                 "staging_left": left}))))
    return {"purged_backups": removed + len(copies) + staged, **({"staging_left": True} if left else {})}


def _purge_staging(data_dir):
    """Remove each staging folder no restore still to be replayed or undone needs: copies of an
    earlier state that a failed clean-up left. Returns (removed, left): left counts those that
    could not be removed, or could not be told apart (a journal that cannot be read)."""
    root = data_dir / "backups" / STAGING
    folders = sorted(root.iterdir()) if root.is_dir() else []
    journal = _read_journal(data_dir)
    if journal == {}:
        return 0, len(folders)
    needed = [data_dir / journal[key] for key in ("staged", "aside")
              if journal and journal.get("direction") != "done" and key in journal]
    removed = left = 0
    for folder in folders:
        if any(path.is_relative_to(folder) for path in needed):
            continue
        try:
            _remove(folder)
            removed += 1
        except OSError as error:
            log.warning("a purge could not remove a copy in staging (%s)", type(error).__name__)
            left += 1
    return removed, left


# Listing


@router.get("/api/backups")
async def list_backups(request: Request):
    state = request.app.state.scholia
    listed = await asyncio.to_thread(list_generations, state["data_dir"])
    return {"backups": [{
        "id": f"{generation['kind']}/{generation['name']}", "kind": generation["kind"], "time": generation["time"],
        "app_version": generation["app_version"], "schema_version": generation["schema_version"],
        "size": generation["size"],
    } for generation in listed]}


@router.post("/api/backups")
async def back_up_now(request: Request):
    """An automatic backup now ("Back up now"), whatever the day's backup did."""
    state = request.app.state.scholia
    async with state["backups_lock"]:
        db = _db(state)
        with _backup_errors():
            generation = await _to_end(asyncio.to_thread(db.backup))
    return {"ok": True, "id": f"daily/{generation.name}"}


_FULL = (errno.ENOSPC, errno.EDQUOT)


@contextlib.contextmanager
def _file_errors(status, code, message, denied=None):
    """A file system failure as (status, code, message), logged by kind only, never with its path.
    A full disk is disk_full; a refused access is denied, (status, code, message), when given.
    SQLite's own full-disk and I/O errors count too."""
    try:
        yield
    except OSError as error:
        log.warning("a backup, restore or export failed on a file (%s, errno %s)", type(error).__name__, error.errno)
        if error.errno in _FULL:
            raise BackupError(507, "disk_full", "The disk is full") from None
        if denied and error.errno in (errno.EACCES, errno.EPERM, errno.EROFS):
            raise BackupError(*denied) from None
        raise BackupError(status, code, message) from None
    except sqlite3.OperationalError as error:
        kind = (error.sqlite_errorcode or 0) & 0xFF
        if kind not in (sqlite3.SQLITE_FULL, sqlite3.SQLITE_IOERR):
            raise
        log.warning("a backup, restore or export failed on the database (%s)", error.sqlite_errorname)
        if kind == sqlite3.SQLITE_FULL:
            raise BackupError(507, "disk_full", "The disk is full") from None
        raise BackupError(status, code, message) from None


def _written():
    """The file errors of a full backup or export, which write to the researcher's folder."""
    return _file_errors(500, "write_failed", "The file could not be written",
                        denied=(400, "destination_not_writable", "Scholia cannot write to that folder"))


@contextlib.contextmanager
def _backup_errors():
    """A backup's failures, as the interface reads them."""
    with _file_errors(500, "backup_failed", "The backup could not be written"):
        try:
            yield
        except DatabaseDamagedError:
            raise BackupError(409, "database_damaged", "The database failed its check; restore a backup") from None
        except BackupBusyError:
            raise BackupError(409, "backup_busy", "Settings kept changing during the backup; try again") from None
        except DatabaseClosedError:
            raise BackupError(503, "closing", "The app is closing") from None


# Full backups


@router.post("/api/backups/full")
async def full_backup(body: FullBackup, request: Request):
    state = request.app.state.scholia
    destination = await asyncio.to_thread(_destination, body.destination, state["data_dir"])
    async with state["backups_lock"]:
        db = _db(state)
        return await _to_end(asyncio.to_thread(_full_backup, db, destination, body.passphrase or None))


def _full_backup(db, destination, passphrase):
    with _written():
        return _write_full_backup(db, destination, passphrase)


def _write_full_backup(db, destination, passphrase):
    staging = _staging(db.data_dir)
    try:
        with _backup_errors():
            info = db.snapshot(staging / "copy")
        with closing(sqlite3.connect((staging / "copy" / DB_NAME).as_uri() + "?mode=ro", uri=True)) as copy:
            sensitive, projects = copy.execute(
                f"SELECT count(*) FILTER (WHERE sensitivity IN {SENSITIVE}), count(*) FROM projects").fetchone()
            # Only the files the copy's records refer to: one of a project deleted moments ago, which
            # collection keeps for a while, would leave without the encryption its project needed.
            hashes = [sha256 for sha256, *_ in _referenced_content(copy)]
        if sensitive and not passphrase:
            raise BackupError(400, "passphrase_required", "A Private or Local only project needs a passphrase")
        entries, left_out = [], []
        for path in sorted((staging / "copy").rglob("*")):
            name = path.relative_to(staging / "copy").as_posix()
            if not path.is_file():
                continue
            if path.name == "config.toml":  # only what the app reads: nothing typed in by hand
                raw = known_only(path.read_bytes(), personal=name == "config.toml")
                if raw is None:  # not valid TOML, so it cannot be checked
                    left_out.append(name)
                else:
                    entries.append((name, raw))
            else:
                entries.append((name, path))
        content, missing = db.data_dir / "content", 0
        for sha256 in hashes:  # content files never change; one deleted since the copy is left out
            path = content / sha256[:2] / sha256
            if path.is_file():
                entries.append((f"content/{sha256[:2]}/{sha256}", path))
            else:
                missing += 1
        record = {"encrypted": passphrase is not None, "projects": projects, "content_files": len(hashes) - missing,
                  "missing_files": missing, "settings_left_out": left_out, "schema_version": info["schema_version"]}
        path = _write_zip(destination, "scholia-backup", entries, passphrase, stop=lambda: db.closed,
                          audit=lambda file: db.write(lambda conn: conn.execute(  # its destination, never content
                              "INSERT INTO audit_log (event, data) VALUES ('full_backup', ?)",
                              (json.dumps({"file": str(file), **record}),))))
    finally:
        shutil.rmtree(staging, ignore_errors=True)
    return {"ok": True, "file": str(path), **record, "size": path.stat().st_size}


# Restore


@router.post("/api/backups/restore")
async def restore(body: Restore, request: Request):
    if (body.generation is None) == (body.file is None):
        raise BackupError(400, "invalid_request", "Name one automatic backup or one backup file")
    state = request.app.state.scholia
    pending = await asyncio.to_thread(_replay_pending, state["data_dir"])
    if state["restore_lock"].locked():  # nothing awaited from here to taking it: one restore at a time
        raise BackupError(409, "restoring", "A backup is being restored")
    # A journal left by a restore that could be neither finished nor undone: the app is limited,
    # and a new restore, which treats the folder as damaged and writes its own journal, is the way out.
    # A committed one only waits for its audit rows, which the new restore carries with its own.
    if pending and "damaged" not in state:
        raise BackupError(409, "restore_interrupted", "Open Scholia again to finish the last restore")
    async with state["restore_lock"]:
        return await _to_end(_restore(state, body))


async def _restore(state, body):
    data_dir, staging = state["data_dir"], None
    try:
        purges = await _purges(state)
        # Checked and copied while the app runs on; no rotation meanwhile, and its staging folder is
        # made under the lock a purge of staging takes, so a purge never meets it half made.
        async with state["backups_lock"]:
            with _file_errors(500, "restore_failed", "The backup could not be put in place; nothing was changed"):
                staging = await asyncio.to_thread(_staging, data_dir)
            with _file_errors(400, "backup_unreadable", "The backup could not be read"):
                await asyncio.to_thread(_stage, data_dir, body, staging / "restore")
        state["restoring"] = True  # from here only health, the backups list and this restore are served
        return await _replace(state, body, staging, purges)
    finally:  # before anything else is served again
        try:
            # A restore still to be replayed or undone keeps it: the next launch needs it
            if staging is not None and not await asyncio.to_thread(_replay_pending, data_dir):
                await asyncio.to_thread(shutil.rmtree, staging, ignore_errors=True)
        finally:
            state.pop("restoring", None)


async def _replace(state, body, staging, purges):
    """Stop the running work and wait out every other request, take the safety copy, then put the
    staged backup in place and run on it; or put everything back. purges is the last purge's
    audit record before the backup was staged."""
    data_dir, db, harness = state["data_dir"], state.get("db"), state.get("harness")
    # A database a failed restore closed, or none at all, is treated as damaged: moved aside, never copied.
    damaged = db is None or db.closed or db.damaged is not None
    if harness is not None:  # admission stops, and running work stops and drains, before anything is copied
        if await harness.shutdown():  # the harness keeps what still runs, and admits again
            await _resume(db, harness, damaged)
            raise BackupError(409, "work_running", "Some running work did not stop in time; try again")
    async with state["writers"].alone(), state["backups_lock"]:  # no other request is left inside
        safety = None
        try:  # anything that stops the restore before the swap leaves the app running as it was
            if not damaged:  # nor any write: a worker a cancelled task left running included, until the swap
                await asyncio.to_thread(db.hold_writes)
                if await _purges(state) != purges:  # Delete everywhere purged the backups meanwhile: not this one
                    raise BackupError(404, "not_found", "No such backup")
                try:  # first a safety copy of the current database, as an automatic backup
                    safety = await asyncio.to_thread(db.backup)
                except DatabaseDamagedError:
                    damaged = True
                except BackupBusyError:
                    raise BackupError(409, "backup_busy", "Settings kept changing during the safety copy; try again") \
                        from None
                except Exception as error:
                    raise BackupError(500, "safety_copy_failed",
                                      "The current database could not be backed up first; nothing was changed") from error
            earlier = await asyncio.to_thread(_audits_to_write, data_dir)  # an earlier restore's, carried on
        except BaseException as error:
            await _resume(db, harness, damaged)
            if isinstance(error, Exception) and not isinstance(error, BackupError):
                raise BackupError(500, "restore_failed", "The backup could not be put in place; nothing was changed") \
                    from error
            raise
        if db is not None and not db.closed:
            await asyncio.to_thread(db.close)
        stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%S%fZ")
        aside = data_dir / "backups" / DAMAGED / stamp if damaged else staging / "replaced"
        restored = None
        # The restore is done once committed: it is never reported as failed from there. What could not
        # be recorded is said (not_recorded) and logged; the journal, kept until the audit rows are
        # written, lets the next launch write them and end the journal (_finish_journal).
        not_recorded = []

        async def recorded(name, fn, *args):
            try:
                return await asyncio.to_thread(fn, *args)
            except Exception as error:
                log.warning("after a restore, %s could not be recorded (%s)", name, type(error).__name__)
                not_recorded.append(name)
                return None

        restore_id = str(uuid.uuid4())  # its journal's: only that journal is ever undone for it
        try:
            journal = await asyncio.to_thread(_put_in_place, data_dir, staging / "restore", aside, restore_id, earlier)
            restored = await asyncio.to_thread(Database, data_dir)  # its checks and migrations
            await state["start"](restored, kick=False)  # recovered; nothing is served or runs on its own yet
            missing = await recorded("missing_files", _missing_files, restored)
            record = {
                "id": journal["id"],
                "source": "automatic" if body.generation else "full",
                "backup": body.generation or str(Path(body.file)),
                "safety_copy": f"daily/{safety.name}" if safety else None,
                "damaged_copy": f"{DAMAGED}/{stamp}" if damaged else None,
                "missing_files": None if missing is None else len(missing),
            }
            audits = [*earlier, record]
            # Committed, before anything is served: from here no launch puts the previous state back
            await asyncio.to_thread(_commit_journal, data_dir, journal, audits)
            await start_background(state)  # its background runs start now: none ran on a database that could yet be put back
        except Exception as error:
            log.warning("a restore failed (%s); the previous state is put back", type(error).__name__)
            if restored is not None and state.get("db") is restored:  # it started: stopped before it served anything
                await _stop(state, restored)
            rolled_back = await _roll_back(state, restored, damaged, restore_id)
            await asyncio.to_thread(_remove_if_empty, aside)  # made before its journal: none, if it failed before
            if not rolled_back:
                _limit(state, "restore_interrupted", "A restore could neither be finished nor undone; restore a backup")
                _forget(state)
                raise BackupError(500, "restore_interrupted",
                                  "The restore could not be finished or undone; open Scholia again to finish it") from error
            raise BackupError(500, "restore_failed", "The backup could not be put in place; nothing was changed") \
                from error
        try:  # a committed restore never needs what staging holds, and purges never reach it
            await asyncio.to_thread(_clear_staging, data_dir, staging)
        except Exception as error:  # the next launch empties staging
            log.warning("removing the previous state after a restore failed (%s)", type(error).__name__)
        await recorded("audit", restored.write, lambda conn: [_audit_restore(conn, audit) for audit in audits])
        if "audit" not in not_recorded:
            await recorded("journal", end_journal, data_dir)
        version = await recorded("schema_version", restored.read, lambda conn: conn.execute(
            "PRAGMA user_version").fetchone()[0])
        return {"ok": True, **record, "schema_version": version, "missing_files": missing,
                "not_recorded": not_recorded}


async def _resume(db, harness, damaged):
    """A restore that stopped before it changed anything: the app runs on as it was, its writes
    admitted again and its harness, which keeps any work still running, admitting again. A failure
    to restart background work is logged; the restore's own answer stands. A damaged database runs
    nothing: the app is limited to restoring, so its harness stays stopped."""
    if damaged or db is None or db.closed or db.damaged is not None:  # as it is now: a backup may have found it
        return
    await asyncio.to_thread(db.release_writes)
    if harness is not None:
        try:
            await harness.resume()
        except Exception as error:
            log.warning("restarting background work after a refused restore failed (%s)", type(error).__name__)


async def start_background(state):
    """Start the app's background runs (start ran with kick false): after a restore commits, and at
    launch once the launch backup has checked the database. A failure is logged: the app runs on,
    and they start at the next launch."""
    try:
        await state["harness"].kick_background()
    except Exception as error:
        log.warning("starting background runs after a restore failed (%s)", type(error).__name__)


async def _stop(state, db):
    """Stop the app started on db, before it served anything: its harness, then the database. A
    failure is logged; what follows (putting the previous state back) goes on."""
    try:
        if state.get("db") is db and state.get("harness") is not None:
            await state["harness"].shutdown()
    except Exception as error:
        log.warning("stopping the harness of a restore that was not committed failed (%s)", type(error).__name__)
    try:
        await asyncio.to_thread(db.close)
    except Exception as error:
        log.warning("closing the database of a restore that was not committed failed (%s)", type(error).__name__)
    _forget(state)


def _forget(state):
    """No database runs: the closed one is not kept in state, so a later restore treats the folder as damaged."""
    for key in ("db", "content", "gate", "harness"):
        state.pop(key, None)


async def _purges(state):
    """The last purge's audit record, to tell a purge apart from rotation; None without a database."""
    db = state.get("db")
    if db is None or db.closed or db.damaged is not None:
        return None
    return await asyncio.to_thread(db.read, lambda conn: conn.execute(
        "SELECT max(seq) FROM audit_log WHERE event = 'backup_purge'").fetchone()[0])


async def _roll_back(state, restored, damaged, restore_id):
    """After a failed restore: close what it opened, put the previous files back as its journal
    says (however far the swap got; nothing moved if its journal, restore_id's, was never written,
    and an earlier restore's journal is left as it is), and run on the previous database again
    unless it was damaged. Returns False when the files could not be put back: then nothing is
    started on them, the journal says what remains, and the next launch finishes putting it back."""
    data_dir = state["data_dir"]
    if restored is not None:
        try:
            await asyncio.to_thread(restored.close)
        except Exception as error:
            log.warning("closing a restored database that failed to start failed (%s)", type(error).__name__)
    try:
        journal = await asyncio.to_thread(_read_journal, data_dir)
        if journal == {}:  # there but unreadable: what this restore moved cannot be told
            raise OSError("the restore journal cannot be read")
        if journal is not None and journal.get("id") == restore_id:
            await asyncio.to_thread(_back_from_journal, data_dir)
    except Exception as error:
        log.error("putting the previous state back after a failed restore failed (%s); the next launch finishes it",
                  type(error).__name__)
        return False
    if not damaged:
        previous = None
        try:
            previous = await asyncio.to_thread(Database, data_dir)
            await state["start"](previous)
        except Exception as error:
            log.error("the previous database could not be started again after a failed restore (%s)",
                      type(error).__name__)
            if previous is not None:
                await asyncio.to_thread(previous.close)
            damaged_now = isinstance(error, DatabaseDamagedError)
            _limit(state, "database_damaged" if damaged_now else "database_unavailable",
                   "The database could not be opened again after a failed restore; restore a backup")
            _forget(state)
    elif "damaged" not in state:  # the restored app had started and ran no more: limited again, as before
        _limit(state, "database_damaged", "The database is damaged; restore a backup")
    return True


def _back_from_journal(data_dir, audit=False):
    """Put the previous state back as the journal says; audit as for _back."""
    return _back(data_dir, json.loads((data_dir / "backups" / JOURNAL).read_bytes()), audit)


def _stage(data_dir, body, staging):
    """Copy the backup's files into staging (owner-only) and check its database: it must be this
    app's, pass integrity_check and not be newer than this version. Nothing live is changed."""
    _mkdir_private(staging)
    if body.generation is not None:
        _stage_generation(data_dir, body.generation, staging)
    else:
        _stage_file(Path(body.file), body.passphrase or None, staging)
    try:
        check = _open_checked(staging / DB_NAME, "integrity_check", latest=len(MIGRATIONS))
    except NewerDatabaseError as error:
        raise BackupError(409, "newer_schema", str(error)) from None
    except (ForeignDatabaseError, DatabaseDamagedError, sqlite3.DatabaseError):
        raise BackupError(400, "backup_damaged", "The backup's database is damaged or not Scholia's") from None
    with closing(check):
        if check.execute("SELECT NOT EXISTS (SELECT 1 FROM sqlite_schema)").fetchone()[0]:
            raise BackupError(400, "backup_damaged", "The backup's database is empty")


def _generation_folder(data_dir, generation):
    """An automatic backup's folder by its listed id ("daily/<stamp>" or "weekly/<stamp>"), or
    BackupError not_found for anything else."""
    kind, _, name = generation.partition("/")
    if kind not in KINDS or name != Path(name).name or not _stamp_of(Path(name)):
        raise BackupError(404, "not_found", "No such backup")
    return data_dir / "backups" / kind / name


def _stage_generation(data_dir, generation, staging):
    folder = _generation_folder(data_dir, generation)
    if folder.is_symlink() or not folder.is_dir():
        raise BackupError(404, "not_found", "No such backup")
    for path in sorted(folder.rglob("*")):
        relative = path.relative_to(folder).as_posix()
        if _BACKUP_ENTRY.fullmatch(relative) and not path.is_symlink() and path.is_file():
            with open(path, "rb") as source:
                _write_staged(staging, relative, source)
    if not (staging / DB_NAME).is_file():
        raise BackupError(400, "backup_damaged", "The backup holds no database")


def _stage_file(path, passphrase, staging):
    if not path.is_absolute():
        raise BackupError(400, "invalid_request", "Choose the backup file by its full path")
    pyzipper = _pyzipper()
    try:
        archive = pyzipper.AESZipFile(path)
    except FileNotFoundError:
        raise BackupError(404, "not_found", "No such backup file") from None
    except (OSError, pyzipper.BadZipFile):
        raise BackupError(400, "not_a_backup", "The file is not a Scholia backup") from None
    with archive:
        if passphrase:
            archive.setpassword(passphrase.encode("utf-8"))
        members = [info for info in archive.infolist() if not info.is_dir()]
        if not any(info.filename == DB_NAME for info in members) or not all(
                _BACKUP_ENTRY.fullmatch(info.filename) for info in members):
            raise BackupError(400, "not_a_backup", "The file is not a Scholia backup")
        if any(info.flag_bits & 1 for info in members) and not passphrase:
            raise BackupError(400, "passphrase_required", "The backup is encrypted; enter its passphrase")
        for info in members:
            try:
                with archive.open(info) as source:
                    digest = _write_staged(staging, info.filename, source)
            except RuntimeError as error:  # pyzipper: a wrong password
                raise BackupError(400, "wrong_passphrase", "The passphrase does not open this backup") from error
            except (pyzipper.BadZipFile, EOFError) as error:  # an OSError, such as a full disk, is not its fault
                raise BackupError(400, "backup_damaged", "The backup file is damaged") from error
            if info.filename.startswith("content/") and digest != info.filename.rsplit("/", 1)[1]:
                raise BackupError(400, "backup_damaged", "A file in the backup is damaged")


def _write_staged(staging, relative, source):
    """Write a file of a backup from source into staging at relative, owner-only. Returns its SHA-256."""
    target = staging / relative
    for parent in reversed(PurePosixPath(relative).parents[:-1]):
        _mkdir_private(staging / parent)
    digest = hashlib.sha256()
    with open(target, "xb", opener=lambda name, flags: os.open(name, flags, 0o600)) as file:
        while chunk := source.read(_CHUNK):
            digest.update(chunk)
            file.write(chunk)
    return digest.hexdigest()


SWAPPED = (*SETTINGS_FILES, "projects")  # what a restore replaces besides the database
SIDECARS = (f"{DB_NAME}-wal", f"{DB_NAME}-shm")
JOURNAL = "restore.json"  # under backups/: a restore putting a backup in place, until it has ended


def _put_in_place(data_dir, staged, aside, restore_id, audits=()):
    """Put the staged backup's database, settings files and content files in place of the live ones.

    The live database keeps its name until the staged one replaces it in one rename, so a crash
    never leaves the folder without one; aside (made here) keeps a link to it, its WAL files and
    the live settings files. Content files are added, never removed. Before anything moves, the
    staged files and every folder involved are synced, then a journal (JOURNAL, synced) records the
    plan; each move is synced as it is made, and every step can be taken again, so a launch after a
    crash or a power loss finishes what was begun (finish_interrupted_restore). After a failure,
    _back puts back what was moved, from the journal. restore_id names the journal; audits are
    an earlier restore's rows still to write, which it carries. Returns the journal: the caller
    commits it (_commit_journal) once the restored app has started, and ends it once its audit
    rows are written.
    """
    _mkdir_private(aside.parent)
    _mkdir_private(aside)
    _sync_tree(staged)
    for folder in (aside, aside.parent, staged.parent, staged.parent.parent, data_dir / "backups"):
        _fsync(folder)
    journal = {
        "id": restore_id,  # in the restore's audit row, which is written once
        "audits": list(audits),
        "direction": "forward",
        "staged": staged.relative_to(data_dir).as_posix(),
        "aside": aside.relative_to(data_dir).as_posix(),
        "live_database": (data_dir / DB_NAME).exists(),
        "live": [name for name in SWAPPED if _exists(data_dir / name)],
        "backup": [name for name in SWAPPED if _exists(staged / name)],
    }
    write_private(data_dir / "backups" / JOURNAL, json.dumps(journal).encode())
    _forward(data_dir, journal)
    return journal


def _commit_journal(data_dir, journal, audits):
    """The restore is committed (direction "done"), synced, before its app serves anything: a launch
    never replays or undoes it any more, and its staging goes; the launch only writes audits (the
    audit rows still to write) and ends the journal (_finish_journal)."""
    committed = {**journal, "direction": "done", "audits": audits}
    write_private(data_dir / "backups" / JOURNAL, json.dumps(committed).encode())


def _read_journal(data_dir):
    """The restore journal; None when there is none, {} when it cannot be read."""
    try:
        journal = json.loads((data_dir / "backups" / JOURNAL).read_bytes())
    except FileNotFoundError:
        return None
    except (OSError, ValueError):
        return {}
    return journal if isinstance(journal, dict) else {}


def _replay_pending(data_dir):
    """Whether a journal a launch must still replay or undo is there (one that cannot be read included)."""
    journal = _read_journal(data_dir)
    return journal is not None and journal.get("direction") != "done"


def _audits_to_write(data_dir):
    """The audit rows the journal there carries and has not written yet."""
    return (_read_journal(data_dir) or {}).get("audits", [])


def _audit_restore(conn, record):
    """The restore's audit row, once: record["id"] is its journal's."""
    conn.execute("INSERT INTO audit_log (event, data) SELECT 'restore', ? WHERE NOT EXISTS ("
                 "SELECT 1 FROM audit_log WHERE event = 'restore' AND json_extract(data, '$.id') = ?)",
                 (json.dumps(record), record["id"]))


def _keep_aside(live, target):
    """A second name for the live database, which stays in place until the backup's is moved over
    it: a hard link, or where the file system has none (exFAT), an owner-only copy made complete
    under another name before it takes target's, so a replay never finds half a copy there."""
    try:
        os.link(live, target)
        return
    except OSError as error:
        if error.errno not in (errno.ENOTSUP, errno.EOPNOTSUPP, errno.EPERM, errno.EXDEV, errno.EMLINK):
            raise
    part = target.with_name(target.name + ".part")
    part.unlink(missing_ok=True)  # one an interrupted attempt left
    with open(live, "rb") as source, open(part, "xb", opener=lambda path, flags: os.open(path, flags, 0o600)) as copy:
        shutil.copyfileobj(source, copy, _CHUNK)
    _fsync(part)
    os.rename(part, target)


def _forward(data_dir, journal):
    """Put the backup in place, from wherever an earlier attempt stopped."""
    staged, aside, live = data_dir / journal["staged"], data_dir / journal["aside"], data_dir / DB_NAME
    if not aside.is_dir():  # an empty one a failed attempt removed: made again, as at the start
        _mkdir_private(aside.parent)
        _mkdir_private(aside)
        _fsync(aside.parent)
    if (staged / DB_NAME).exists():  # the backup's database is not in place yet
        if live.exists() and not (aside / DB_NAME).exists():
            _keep_aside(live, aside / DB_NAME)
            _fsync(aside)
        for sidecar in SIDECARS:  # a damaged database's WAL holds committed work: it goes with it
            if (data_dir / sidecar).exists():
                _move(data_dir / sidecar, aside / sidecar)
        if (staged / "content").is_dir():
            _add_content(staged / "content", data_dir / "content")
        _move(staged / DB_NAME, live)
    for name in SWAPPED:  # the live one aside first, so one found in place with none aside is the live one
        if name in journal["live"] and not _exists(aside / name) and _exists(data_dir / name):
            _move(data_dir / name, aside / name)
        if name in journal["backup"] and _exists(staged / name):
            _move(staged / name, data_dir / name)


def _back(data_dir, journal, audit=False):
    """Put back what _forward moved, from wherever it stopped, and end the journal. Recorded as
    the journal's direction first, so a crash meanwhile is finished backwards too. With audit (at
    launch), or audit rows it carries, the journal is committed instead, holding the rows to write
    once the database runs (its own at launch), and returned."""
    journal = {**journal, "direction": "back"}
    write_private(data_dir / "backups" / JOURNAL, json.dumps(journal).encode())
    staged, aside, live = data_dir / journal["staged"], data_dir / journal["aside"], data_dir / DB_NAME
    for name in reversed(SWAPPED):
        if _exists(aside / name):
            _remove(data_dir / name)
            _move(aside / name, data_dir / name)
        elif name not in journal["live"]:  # there was none: one in place came from the backup
            _remove(data_dir / name)
    if not (staged / DB_NAME).exists():  # the backup's database went in
        if (aside / DB_NAME).exists():  # and is still there: its own sidecars go, then the previous one is back
            for sidecar in SIDECARS:
                _remove(data_dir / sidecar)
            _move(aside / DB_NAME, live)
        elif not journal["live_database"]:  # there was none before
            for sidecar in SIDECARS:
                _remove(data_dir / sidecar)
            live.unlink(missing_ok=True)
    else:
        (aside / DB_NAME).unlink(missing_ok=True)  # only a second link to the live one
    for sidecar in SIDECARS:
        if (aside / sidecar).exists():
            _move(aside / sidecar, data_dir / sidecar)
    _fsync(data_dir)
    _remove_if_empty(aside)  # empty now; one that is not is kept
    audits = [*journal.get("audits", []), *([{"id": journal.get("id"), "interrupted": True, "finished": "back"}]
                                             if audit else [])]
    if audits:  # rows still to write, its own at launch or an earlier restore's it carried: kept for them
        _commit_journal(data_dir, journal, audits)
        return {**journal, "direction": "done", "audits": audits}
    end_journal(data_dir)
    return None


def _move(source, target):
    """Rename source to target and sync both folders, so the move outlives a power loss."""
    os.replace(source, target)
    _fsync(target.parent)
    if source.parent != target.parent:
        _fsync(source.parent)


def _sync_tree(root):
    """Sync every file and folder under root, and root: what a journal will point at is on disk."""
    for path in sorted(root.rglob("*"), key=lambda p: -len(p.parts)):  # files before their folders
        if not path.is_symlink():
            _fsync(path)
    _fsync(root)


def end_journal(data_dir):
    """The restore has ended: its journal goes, synced."""
    (data_dir / "backups" / JOURNAL).unlink(missing_ok=True)
    _fsync(data_dir / "backups")


def finish_interrupted_restore(data_dir) -> dict | None:
    """Replay a restore a crash interrupted while it put a backup in place, or put the previous
    state back: forward or back as its journal says. Run at launch, before the database opens and
    before staging is emptied (see open_at_launch). Returns the journal as it then is, or None when
    there was none. Forward keeps it: it is committed only once the restored database runs, and is
    what puts the previous state back if it cannot. Back commits it, with the audit row still to
    write. A committed one ("done": its restore's app ran) is never replayed or undone: it is
    returned as it is. A journal that cannot be read, or that names folders outside backups/, is
    left with staging as they are, and raises."""
    data_dir = Path(data_dir)
    path = data_dir / "backups" / JOURNAL
    if not path.is_file():
        return None
    journal = json.loads(path.read_bytes())
    for key in ("staged", "aside"):
        folder = PurePosixPath(journal[key])
        if folder.is_absolute() or ".." in folder.parts or folder.parts[0] != "backups":
            raise RuntimeError("the restore journal names a folder outside backups/")
    if journal["direction"] == "done":
        return journal
    if journal["direction"] == "forward":
        _forward(data_dir, journal)
        log.warning("a restore interrupted by a crash was replayed at launch")
        return journal
    journal = _back(data_dir, journal, audit=True)
    log.warning("a restore interrupted by a crash was undone at launch")
    return journal


async def open_at_launch(state, maintenance):
    """Open the data folder's database and run the app on it (state["start"]), first replaying a
    restore a crash interrupted. The replay and the opening run inside maintenance() (the desktop
    entry's start deadline does not count them); starting the harness does not. A restore replayed
    forward keeps its journal until the restored database is open and its harness has recovered,
    then commits it before anything is served; if any of that fails, the previous state is put back
    from the journal and opened instead. A committed restore is never put back: its database is
    opened as it is. Returns the database, or None when the app stays limited (state["damaged"]
    says why): a damaged database, a restore that could neither be finished nor undone, or, after
    a restore, a database that cannot run. A finished restore's audit row is written, then its
    journal ends (_finish_journal)."""
    data_dir = state["data_dir"]
    try:
        with maintenance():
            journal = await asyncio.to_thread(finish_interrupted_restore, data_dir)
    except Exception as error:
        log.error("a restore interrupted by a crash could not be replayed (%s)", type(error).__name__)
        _limit(state, "restore_interrupted", "A restore interrupted by a crash could not be finished; restore a backup")
        return None
    if journal is None:  # background runs start once the launch backup has checked it (app.py)
        return await _open(state, maintenance, kick=False)
    if journal["direction"] == "forward":
        db = None
        try:
            db = await _open(state, maintenance, kick=False)  # nothing runs on its own until it commits
            if db is None:
                raise DatabaseDamagedError("the restored database failed its check")
            audits = [*journal.get("audits", []), {"id": journal.get("id"), "interrupted": True, "finished": "forward"}]
            await asyncio.to_thread(_commit_journal, data_dir, journal, audits)  # before anything is served
        except Exception as error:
            log.warning("the restored database could not run (%s); the previous state is put back",
                        type(error).__name__)
            if db is not None and not db.closed:
                await _stop(state, db)
            state.pop("damaged", None)
            state.pop("damaged_code", None)
            try:
                with maintenance():
                    journal = await asyncio.to_thread(_back_from_journal, data_dir, True)
            except Exception as failure:
                log.error("putting the previous state back failed (%s)", type(failure).__name__)
                _limit(state, "restore_interrupted", "A restore interrupted by a crash could not be undone; restore a backup")
                return None
        else:  # its background runs start with the launch's, once the launch backup has checked it (app.py)
            await _finish_journal(db, audits)
            return db
    try:  # committed, or put back: opened as it is, and limited, offering a restore, if it cannot run
        db = await _open(state, maintenance, kick=False)
    except Exception as error:
        log.error("the database could not run after a restore was finished at launch (%s)", type(error).__name__)
        _limit(state, "database_unavailable", "The database could not be opened; restore a backup")
        return None
    if db is not None:
        await _finish_journal(db, journal.get("audits", []))
    return db


async def _open(state, maintenance, kick=True):
    """The database opened and the app started on it (its background runs started unless kick is
    false), or None when it is damaged (the app is then limited). Any other failure closes what
    was opened and is raised."""
    db = None
    try:
        with maintenance():
            db = await asyncio.to_thread(Database, state["data_dir"])
        await state["start"](db, kick=kick)
        return db
    except DatabaseDamagedError as error:
        log.error("the database failed its check at startup; only a restore is offered")
        _limit(state, "database_damaged", str(error))
        return None
    except BaseException:
        if db is not None:
            await asyncio.to_thread(db.close)
        raise


async def _finish_journal(db, audits):
    """Write a finished restore's audit rows (each once), then end its journal. A failure leaves the
    app running and the committed journal in place, so the next launch tries again; it is logged."""
    try:
        if audits:
            await asyncio.to_thread(db.write, lambda conn: [_audit_restore(conn, audit) for audit in audits])
        await asyncio.to_thread(end_journal, db.data_dir)
    except Exception as error:
        log.warning("recording a finished restore failed (%s); the next launch tries again", type(error).__name__)


def _exists(path):
    return path.exists() or path.is_symlink()


def _remove_if_empty(folder):
    with contextlib.suppress(OSError):
        folder.rmdir()


def _clear_staging(data_dir, current):
    """After a restore committed: the previous state it set aside in current, and whatever earlier
    restores or backups left in staging. current's other contents go when the restore ends. Each
    is tried; the first failure is raised after the others."""
    failures = []
    for folder in (current / "replaced", *((data_dir / "backups" / STAGING).iterdir())):
        try:
            if folder != current and _exists(folder):
                _remove(folder)
        except OSError as error:
            failures.append(error)
    if failures:
        raise failures[0]


def _remove(path):
    if path.is_dir() and not path.is_symlink():
        shutil.rmtree(path)
    else:
        path.unlink(missing_ok=True)


def _add_content(staged, root):
    _mkdir_private(root)
    for path in sorted(staged.glob("*/*")):
        folder = root / path.parent.name
        _mkdir_private(folder)
        os.replace(path, folder / path.name)  # same bytes as any copy there, by their hash
        _fsync(folder)
    _fsync(root)


def _missing_files(db):
    """The content files the database's records refer to that are not in the content store ("file missing")."""
    rows = db.read(_referenced_content)
    root = db.data_dir / "content"
    return [{"sha256": sha256, "size": size, "media_type": media_type} for sha256, size, media_type in rows
            if not (root / sha256[:2] / sha256).is_file()]


def _referenced_content(conn):
    """(sha256, size, media_type) of every content file a record refers to, through any foreign key
    into content_files. A row nothing refers to any more (its records deleted, the file not yet
    collected) is left out."""
    references = conn.execute(
        'SELECT m.name, f."from" FROM sqlite_schema m, pragma_foreign_key_list(m.name) f'
        " WHERE m.type = 'table' AND f.\"table\" = 'content_files'").fetchall()
    referenced = " UNION ".join(f'SELECT "{column}" FROM "{table}"' for table, column in references)
    return conn.execute(f"SELECT sha256, size, media_type FROM content_files WHERE sha256 IN ({referenced})"
                        " ORDER BY sha256").fetchall()


# Project export


@router.post("/api/projects/{project_id}/export")
async def export_project(project_id: str, body: Export, request: Request):
    state = request.app.state.scholia
    destination = await asyncio.to_thread(_destination, body.destination, state["data_dir"])
    async with state["backups_lock"]:
        db = _db(state)
        return await _to_end(asyncio.to_thread(_export, db, project_id, destination, body.passphrase or None))


def _export(db, project_id, destination, passphrase):
    with _written():
        return _write_export(db, project_id, destination, passphrase)


def _write_export(db, project_id, destination, passphrase):
    data = db.read(lambda conn: _project_records(conn, project_id))
    if data is None:
        raise BackupError(404, "not_found", "No such project")
    if data["project"]["sensitivity"] in SENSITIVE and not passphrase:
        raise BackupError(400, "passphrase_required", "A Private or Local only project needs a passphrase")
    entries = [("project.json", _json({"format": "scholia-project-export", "version": 1, "app_version": APP_VERSION,
                                       "exported_at": utc_now(), "project": data["project"]})),
               ("conversations.json", _json(data["conversations"]))]
    # Names in a zip are readable without its passphrase, so files are named by id, never by title.
    for conversation in data["conversations"]:
        entries.append((f"conversations/{conversation['id']}.md", _conversation_markdown(conversation)))
    if data["artifacts"]:
        entries.append(("artifacts.json", _json(data["artifacts"])))
        for artifact in data["artifacts"]:
            entries.append((f"artifacts/{artifact['id']}.md", _artifact_markdown(artifact)))
    content = db.data_dir / "content"
    if data["materials"]:
        for material in data["materials"]:
            for version in material["versions"]:
                path = content / version["file_sha256"][:2] / version["file_sha256"] if version["file_sha256"] else None
                if path is not None and path.is_file():
                    extension = mimetypes.guess_extension(version["media_type"] or "") or ""
                    version["file"] = f"materials/{material['id']}/v{version['seq']}{extension}"
                    entries.append((version["file"], path))
                else:
                    version["file"] = None  # metadata only, or the file is missing
        entries.append(("materials.json", _json(data["materials"])))
    folder = db.data_dir / "projects" / project_id
    for name in SETTINGS_FILES:
        path = folder / name
        if path.is_file() and not path.is_symlink():
            raw = path.read_bytes()
            raw = known_only(raw) if name == "config.toml" else raw  # only what the app reads: nothing typed in by hand
            if raw is not None:
                entries.append((f"settings/{name}", raw))
    record = {"encrypted": passphrase is not None, "conversations": len(data["conversations"]),
              "turns": sum(len(c["turns"]) for c in data["conversations"]), "artifacts": len(data["artifacts"]),
              "materials": len(data["materials"])}
    path = _write_zip(destination, f"scholia-project-{project_id[:8]}", entries, passphrase, stop=lambda: db.closed,
                      audit=lambda file: db.write(lambda conn: conn.execute(  # its destination, never content
                          "INSERT INTO audit_log (event, project_id, data) VALUES ('project_export', ?, ?)",
                          (project_id, json.dumps({"file": str(file), **record})))))
    # The settings files were read while the project existed only if it still does: it is deleted record first.
    if not db.read(lambda conn: conn.execute("SELECT 1 FROM projects WHERE id = ?", (project_id,)).fetchone()):
        path.unlink(missing_ok=True)
        raise BackupError(404, "not_found", "No such project")
    return {"ok": True, "file": str(path), **record, "size": path.stat().st_size}


def _project_records(conn, project_id):
    """Everything an export holds from the database, read in one transaction, or None."""
    def rows(sql, *args):
        cursor = conn.execute(sql, args)
        names = [column[0] for column in cursor.description]
        return [dict(zip(names, row)) for row in cursor.fetchall()]

    projects = rows("SELECT id, name, kind, sensitivity, review_lock, review_venue, target_venue, created_at,"
                    " updated_at FROM projects WHERE id = ?", project_id)
    if not projects:
        return None
    conversations = rows("SELECT id, title, title_source, created_at, updated_at FROM conversations"
                         " WHERE project_id = ? ORDER BY created_at, id", project_id)
    for conversation in conversations:
        conversation["turns"] = rows(
            "SELECT t.seq, t.author, t.user_message AS message, t.answer, r.status, r.cancel_reason, t.reason_code,"
            " r.settled_cost_usd AS cost_usd, t.accounting, r.started_at, r.finished_at,"
            " t.retry_of_run_id AS continues FROM turns t JOIN runs r ON r.id = t.run_id"
            " WHERE t.conversation_id = ? ORDER BY t.seq", conversation["id"])
        for turn in conversation["turns"]:
            for key in ("message", "answer", "accounting"):
                turn[key] = json.loads(turn[key]) if turn[key] else None
    materials = rows("SELECT id, title, csl, source, source_key, evidence_type, proposed_by, resolved_at, checked_at,"
                     " checked_by, retraction, created_at, updated_at FROM materials WHERE project_id = ?"
                     " ORDER BY created_at, id", project_id)
    for material in materials:
        material["csl"] = json.loads(material["csl"]) if material["csl"] else None
        material["versions"] = rows(
            "SELECT v.seq, v.file_sha256, v.is_current, c.media_type, v.created_at FROM material_versions v"
            " LEFT JOIN content_files c ON c.sha256 = v.file_sha256 WHERE v.material_id = ? ORDER BY v.seq",
            material["id"])
    artifacts = rows("SELECT id, kind, title, language, citation_style, template_id, authors, venue, doc, doc_rev,"
                     " created_at, updated_at FROM artifacts WHERE project_id = ? ORDER BY created_at, id", project_id)
    for artifact in artifacts:
        artifact["authors"], artifact["doc"] = json.loads(artifact["authors"]), json.loads(artifact["doc"])
    return {"project": projects[0], "conversations": conversations, "materials": materials, "artifacts": artifacts}


def _conversation_markdown(conversation):
    lines = [f"# {conversation['title'] or 'Untitled conversation'}", ""]
    for turn in conversation["turns"]:
        author = "Researcher" if turn["author"] == "researcher" else "From another conversation"
        lines += [f"## {author} ({turn['started_at']})", "", (turn["message"] or {}).get("text", ""), ""]
        if turn["answer"]:
            lines += ["## Scholia", "", turn["answer"].get("text", ""), ""]
        elif turn["status"] != "running":
            lines += [f"*No answer: {turn['status']}*", ""]
    return "\n".join(lines).encode("utf-8")


def _artifact_markdown(artifact):
    """ponytail: an artifact's text, paragraph by paragraph, until S1-23's document model brings
    its own Markdown export (headings, lists, citations)."""
    blocks, stack = [], [artifact["doc"]]
    while stack:
        node = stack.pop()
        if isinstance(node, dict):
            if isinstance(node.get("text"), str):
                blocks.append(node["text"])
            stack.extend(reversed(node.get("content") or []))
        elif isinstance(node, list):
            stack.extend(reversed(node))
    return "\n\n".join([f"# {artifact['title']}", *blocks]).encode("utf-8") + b"\n"


def _json(value):
    return json.dumps(value, ensure_ascii=False, indent=2).encode("utf-8")


# Files


def _pyzipper():
    """pyzipper, imported when a zip file is first written or read rather than at launch: as it
    loads, pycryptodomex runs file(1) once to learn the interpreter's word size."""
    import pyzipper
    return pyzipper


def _destination(text, data_dir):
    """The folder a full backup or export goes to: absolute, existing, and outside the data folder."""
    path = Path(text)
    if not path.is_absolute():
        raise BackupError(400, "invalid_destination", "Choose a folder by its full path")
    if not path.is_dir():
        raise BackupError(400, "destination_not_found", "The destination folder does not exist")
    resolved = path.resolve()
    if resolved.is_relative_to(Path(data_dir).resolve()):
        raise BackupError(400, "destination_in_data_folder", "Choose a folder outside Scholia's data folder")
    return resolved


def _staging(data_dir):
    """A new owner-only folder under backups/STAGING, on the data folder's disk, for one restore or backup."""
    backups = Path(data_dir) / "backups"
    _mkdir_private(backups)
    _mkdir_private(backups / STAGING)
    folder = backups / STAGING / uuid.uuid4().hex
    os.mkdir(folder, 0o700)
    return folder


def _free_name(destination, prefix, stamp):
    name, n = f"{prefix}-{stamp}.zip", 1
    while _exists(destination / name) or _exists(destination / f".{name}.tmp"):
        n += 1
        name = f"{prefix}-{stamp}-{n}.zip"
    return destination / name


def _publish(tmp, final):
    """Give the written file tmp the name final too, or raise FileExistsError when something is
    there, leaving nothing of its own at final when it raises: a hard link; on a file system
    without them (exFAT, some shares), final is taken first, exclusively, then replaced by tmp."""
    try:
        os.link(tmp, final)
    except FileExistsError:
        raise
    except OSError as error:
        if error.errno not in (errno.EPERM, errno.ENOTSUP, errno.EOPNOTSUPP, errno.EMLINK):
            raise
        os.close(os.open(final, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600))
        try:
            os.rename(tmp, final)
        except BaseException:
            final.unlink(missing_ok=True)  # its own empty file
            raise


def _write_zip(destination, prefix, entries, passphrase, *, stop, audit):
    """Write entries [(name, bytes or a file's path)] to a new zip file in destination, owner-only:
    AES-encrypted with passphrase when given. audit(path) records it first, before anything is
    written there, so no file ever leaves the data folder unrecorded; a failure afterwards leaves
    the record of an attempt. It is written under a temporary name and given its name when complete
    and synced, never replacing a file that appeared there meanwhile (it takes the next free name,
    recorded too); on any failure, what was written there is removed. stop() is checked as it goes;
    when true (the app is closing), it stops with 503 closing. Returns the file's path."""
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    final = _free_name(destination, prefix, stamp)
    # A temporary name of its own, removed on failure only once this call has made it.
    tmp, made, published = destination / f".{final.name}.{uuid.uuid4().hex[:8]}.tmp", False, False
    audit(final)
    pyzipper = _pyzipper()
    options = {"encryption": pyzipper.WZ_AES} if passphrase else {}
    try:
        with open(tmp, "xb", opener=lambda path, flags: os.open(path, flags, 0o600)) as raw:
            made = True
            with pyzipper.AESZipFile(raw, "w", compression=pyzipper.ZIP_DEFLATED, **options) as archive:
                if passphrase:
                    archive.setpassword(passphrase.encode("utf-8"))
                for entry_name, source in entries:
                    info = archive.zipinfo_cls(entry_name, date_time=datetime.now().timetuple()[:6])
                    info.external_attr = 0o600 << 16
                    info.compress_type = pyzipper.ZIP_DEFLATED
                    info.file_size = len(source) if isinstance(source, bytes) else source.stat().st_size
                    with archive.open(info, "w") as target:
                        if isinstance(source, bytes):
                            target.write(source)
                            continue
                        with open(source, "rb") as file:
                            while chunk := file.read(_CHUNK):
                                if stop():
                                    raise DatabaseClosedError("the app is closing; the file was not finished")
                                target.write(chunk)
                    if stop():
                        raise DatabaseClosedError("the app is closing; the file was not finished")
        _fsync(tmp)
        while True:
            try:
                _publish(tmp, final)
                published = True
                tmp.unlink(missing_ok=True)  # its other name, when linked
                break
            except FileExistsError:  # a file someone put there meanwhile: never replaced
                final = _free_name(destination, prefix, stamp)
                audit(final)
        _fsync(destination)
    except BaseException as error:
        if made:
            tmp.unlink(missing_ok=True)
        if published:  # but its folder could not be synced: not a backup
            final.unlink(missing_ok=True)
        if isinstance(error, DatabaseClosedError):
            raise BackupError(503, "closing", "The app is closing") from None
        raise
    return final
