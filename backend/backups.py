"""Backups, restore and project export: the API under Settings, then Advanced.

Automatic backups (backend.db.database) are taken at launch, while the app is idle
(once a day, checked every IDLE_CHECK_SECONDS when no run is active), and before a
bulk deletion (backup_before_deletion). purge() takes them out of deleted data's way.
Full backups and project exports are zip files in a folder the researcher chose,
written under a temporary name and renamed when complete. One that holds a Private
or Local only project is AES-encrypted with the researcher's passphrase (pyzipper)
and refused without one; others are plain zip files. Neither ever holds a key, the
logs or the lock file. A restore saves a safety copy, stops running work, puts a
backup's database and settings files in place, reopens the database and starts the
harness again in the same process, then reports referenced content files that are
missing. Every one of these is audited.

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
from backend.settings import without_ignored

log = logging.getLogger(__name__)

IDLE_CHECK_SECONDS = 600
SENSITIVE = ("private", "local_only")  # a backup or export holding such a project is encrypted
STAGING = ".staging"  # under backups/: restores and full backups in progress, emptied at launch
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


# What a database damaged at startup leaves served (see LimitedMode).
LIMITED = {("GET", "/api/health"), ("GET", "/api/backups"), ("POST", "/api/backups/restore")}


class LimitedMode:
    """While the database is damaged at startup ("damaged" in the app's state), only health, the
    backups list and restore are served; every other API request gets 503 database_damaged.
    Pure ASGI, so streamed responses pass through it untouched."""

    def __init__(self, app, state):
        self.app, self.state = app, state

    async def __call__(self, scope, receive, send):
        if (scope["type"] == "http" and "damaged" in self.state and scope["path"].startswith("/api/")
                and (scope["method"], scope["path"]) not in LIMITED):
            response = JSONResponse({"code": "database_damaged",
                                     "message": "The database failed its check; restore a backup"}, status_code=503)
            return await response(scope, receive, send)
        return await self.app(scope, receive, send)


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
    opened = state.get("db"), state.get("harness")
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
    then no older one and no damaged copy a restore moved aside, audited. deleted says what was
    deleted ({"kind", "object_id"}). Returns the number of backups and copies deleted. A failed
    backup deletes nothing and raises. The caller orders it with restores (see backups_lock)."""
    generation, removed = db.purge_backups()
    copies = sorted((db.backups_dir / DAMAGED).glob("*")) if (db.backups_dir / DAMAGED).is_dir() else []
    for copy in copies:
        _remove(copy)
    db.write(lambda conn: conn.execute(
        "INSERT INTO audit_log (event, project_id, data) VALUES ('backup_purge', ?, ?)",
        (project_id, json.dumps({**deleted, "kept": f"daily/{generation.name}", "deleted_backups": removed,
                                 "deleted_damaged_copies": len(copies)}))))
    return removed + len(copies)


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
            hashes = [sha256 for (sha256,) in copy.execute("SELECT sha256 FROM content_files ORDER BY sha256")]
        if sensitive and not passphrase:
            raise BackupError(400, "passphrase_required", "A Private or Local only project needs a passphrase")
        entries = [(path.relative_to(staging / "copy").as_posix(), path)
                   for path in sorted((staging / "copy").rglob("*")) if path.is_file()]
        content, missing = db.data_dir / "content", 0
        for sha256 in hashes:  # content files never change; one deleted since the copy is left out
            path = content / sha256[:2] / sha256
            if path.is_file():
                entries.append((f"content/{sha256[:2]}/{sha256}", path))
            else:
                missing += 1
        path = _write_zip(destination, "scholia-backup", entries, passphrase, stop=lambda: db.closed)
    finally:
        shutil.rmtree(staging, ignore_errors=True)
    result = {"file": str(path), "encrypted": passphrase is not None, "projects": projects,
              "content_files": len(hashes) - missing, "missing_files": missing, "size": path.stat().st_size,
              "schema_version": info["schema_version"]}
    db.write(lambda conn: conn.execute(  # the destination, never content
        "INSERT INTO audit_log (event, data) VALUES ('full_backup', ?)", (json.dumps(result),)))
    return {"ok": True, **result}


# Restore


@router.post("/api/backups/restore")
async def restore(body: Restore, request: Request):
    if (body.generation is None) == (body.file is None):
        raise BackupError(400, "invalid_request", "Name one automatic backup or one backup file")
    state = request.app.state.scholia
    # Project folder writes wait (see backend.app's project-files lock) while the folder is swapped.
    async with state["backups_lock"], state.get("project_files") or contextlib.nullcontext():
        return await _to_end(_restore(state, body))


async def _restore(state, body):
    data_dir = state["data_dir"]
    with _file_errors(500, "restore_failed", "The backup could not be put in place; nothing was changed"):
        staging = await asyncio.to_thread(_staging, data_dir)
    try:
        with _file_errors(400, "backup_unreadable", "The backup could not be read"):
            await asyncio.to_thread(_stage, data_dir, body, staging / "restore")
        db, harness = state.get("db"), state.get("harness")
        damaged = db is None or db.damaged is not None
        safety = None
        if not damaged:  # first a safety copy of the current database, as an automatic backup
            try:
                safety = await asyncio.to_thread(db.backup)
            except DatabaseDamagedError:
                damaged = True
            except BackupBusyError:
                raise BackupError(409, "backup_busy", "Settings kept changing during the safety copy; try again") \
                    from None
            except Exception as error:
                raise BackupError(500, "safety_copy_failed",
                                  "The current database could not be backed up first; nothing was changed") from error
        if harness is not None:  # stop running work as closing the app does
            await harness.shutdown()
        if db is not None:
            await asyncio.to_thread(db.close)
        stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%S%fZ")
        aside = data_dir / "backups" / DAMAGED / stamp if damaged else staging / "replaced"
        undo = None
        try:
            undo = await asyncio.to_thread(_put_in_place, data_dir, staging / "restore", aside)
            restored = await asyncio.to_thread(Database, data_dir)  # its checks and migrations
        except Exception as error:
            if undo is not None:
                await asyncio.to_thread(undo)
            if not damaged:  # back to the database as it was, running again
                await state["start"](await asyncio.to_thread(Database, data_dir))
            log.warning("a restore failed (%s); the previous database was put back", type(error).__name__)
            raise BackupError(500, "restore_failed", "The backup could not be put in place; nothing was changed") \
                from error
        await state["start"](restored)
        missing = await asyncio.to_thread(_missing_files, restored)
        record = {
            "source": "automatic" if body.generation else "full",
            "backup": body.generation or str(Path(body.file)),
            "safety_copy": f"daily/{safety.name}" if safety else None,
            "damaged_copy": f"{DAMAGED}/{stamp}" if damaged else None,
            "missing_files": len(missing),
        }
        await asyncio.to_thread(restored.write, lambda conn: conn.execute(
            "INSERT INTO audit_log (event, data) VALUES ('restore', ?)", (json.dumps(record),)))
        version = await asyncio.to_thread(restored.read, lambda conn: conn.execute(
            "PRAGMA user_version").fetchone()[0])
        return {"ok": True, **record, "schema_version": version, "missing_files": missing}
    finally:
        await asyncio.to_thread(shutil.rmtree, staging, ignore_errors=True)


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


def _stage_generation(data_dir, generation, staging):
    kind, _, name = generation.partition("/")
    folder = data_dir / "backups" / kind / name
    if kind not in KINDS or name != Path(name).name or not _stamp_of(Path(name)) or folder.is_symlink() \
            or not folder.is_dir():
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


def _put_in_place(data_dir, staged, aside):
    """Put the staged backup's database, settings files and content files in place of the live ones.

    The live database keeps its name until the staged one replaces it in one rename, so a crash
    never leaves the folder without one; aside (made here) keeps a link to it, its WAL files and
    the live settings files. Content files are added, never removed. Returns undo(), which puts
    back what was moved (content files stay; nothing refers to the extra ones). A failure partway
    undoes what was done before it is raised.
    """
    _mkdir_private(aside.parent)
    _mkdir_private(aside)
    live, sidecars = data_dir / DB_NAME, (f"{DB_NAME}-wal", f"{DB_NAME}-shm")
    done = []  # (step, name), in order

    def undo():
        for step, name in reversed(done):
            if step == "in":  # a staged file or folder put in place
                _remove(data_dir / name)
            elif step == "aside":  # a live one moved aside
                _remove(data_dir / name)  # a sidecar the restored database made as it was opened
                os.replace(aside / name, data_dir / name)
            elif step == "database":  # the staged database replaced the live one, which aside links
                for sidecar in sidecars:
                    _remove(data_dir / sidecar)
                if (aside / DB_NAME).exists():
                    os.replace(aside / DB_NAME, live)
                else:
                    live.unlink(missing_ok=True)
            elif step == "link":
                (aside / DB_NAME).unlink(missing_ok=True)
        _fsync(data_dir)
        with contextlib.suppress(OSError):  # empty now; one that is not is kept
            aside.rmdir()

    try:
        if live.exists():
            os.link(live, aside / DB_NAME)
            done.append(("link", DB_NAME))
        for sidecar in sidecars:  # a damaged database's WAL holds committed work: it goes with it
            if (data_dir / sidecar).exists():
                os.replace(data_dir / sidecar, aside / sidecar)
                done.append(("aside", sidecar))
        if (staged / "content").is_dir():
            _add_content(staged / "content", data_dir / "content")
        os.replace(staged / DB_NAME, live)
        done.append(("database", DB_NAME))
        for name in (*SETTINGS_FILES, "projects"):
            if (data_dir / name).exists() or (data_dir / name).is_symlink():
                os.replace(data_dir / name, aside / name)
                done.append(("aside", name))
            if (staged / name).exists():
                os.replace(staged / name, data_dir / name)
                done.append(("in", name))
        _fsync(aside)
        _fsync(data_dir)
    except BaseException:
        undo()
        raise
    return undo


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
    """The content files the database refers to that are not in the content store ("file missing")."""
    rows = db.read(lambda conn: conn.execute(
        "SELECT sha256, size, media_type FROM content_files ORDER BY sha256").fetchall())
    root = db.data_dir / "content"
    return [{"sha256": sha256, "size": size, "media_type": media_type} for sha256, size, media_type in rows
            if not (root / sha256[:2] / sha256).is_file()]


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
            raw = without_ignored(raw) if name == "config.toml" else raw  # never a key typed into it by hand
            if raw is not None:
                entries.append((f"settings/{name}", raw))
    path = _write_zip(destination, f"scholia-project-{project_id[:8]}", entries, passphrase, stop=lambda: db.closed)
    # The settings files were read while the project existed only if it still does: it is deleted record first.
    if not db.read(lambda conn: conn.execute("SELECT 1 FROM projects WHERE id = ?", (project_id,)).fetchone()):
        path.unlink(missing_ok=True)
        raise BackupError(404, "not_found", "No such project")
    result = {"file": str(path), "encrypted": passphrase is not None, "conversations": len(data["conversations"]),
              "turns": sum(len(c["turns"]) for c in data["conversations"]), "artifacts": len(data["artifacts"]),
              "materials": len(data["materials"]), "size": path.stat().st_size}
    db.write(lambda conn: conn.execute(  # the destination, never content
        "INSERT INTO audit_log (event, project_id, data) VALUES ('project_export', ?, ?)",
        (project_id, json.dumps(result))))
    return {"ok": True, **result}


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


def _write_zip(destination, prefix, entries, passphrase, *, stop):
    """Write entries [(name, bytes or a file's path)] to a new zip file in destination, owner-only:
    AES-encrypted with passphrase when given. It is written under a temporary name and renamed
    when complete and synced. stop() is checked as it goes; when true (the app is closing), the
    file is removed and DatabaseClosedError raised. Returns the file's path."""
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    name, n = f"{prefix}-{stamp}.zip", 1
    while (destination / name).exists() or (destination / f".{name}.tmp").exists():
        n += 1
        name = f"{prefix}-{stamp}-{n}.zip"
    final, tmp = destination / name, destination / f".{name}.tmp"
    pyzipper = _pyzipper()
    options = {"encryption": pyzipper.WZ_AES} if passphrase else {}
    try:
        with open(tmp, "xb", opener=lambda path, flags: os.open(path, flags, 0o600)) as raw:
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
        os.rename(tmp, final)
        _fsync(destination)
    except DatabaseClosedError:
        tmp.unlink(missing_ok=True)
        raise BackupError(503, "closing", "The app is closing") from None
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise
    return final
