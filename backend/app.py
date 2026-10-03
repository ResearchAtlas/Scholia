"""The backend's HTTP API, on the SQLite store of one data folder.

`create_app` builds the app for a data folder. The desktop entry serves it on a
loopback port behind `LocalRequestGuard`; tests drive it in process. Errors carry
a stable `code` the interface translates, and an English `message`. Every write
goes through the single writer; database calls run off the event loop.
"""

import asyncio
import contextlib
import json
import logging
import shutil
import stat
import threading
import time
from functools import partial
from pathlib import Path

import anyio
from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from pydantic import BaseModel, Field, field_validator
from starlette.convertors import Convertor, register_url_convertor
from starlette.exceptions import HTTPException as StarletteHTTPException

from backend import APP_VERSION, credentials, openrouter, openrouter_client, providers
from backend.db import ContentStore, Database, delete, new_id, utc_now
from backend.local_guard import LocalRequestGuard
from backend.outbound_gate import OutboundGate
from backend.runs import AdmissionError, Harness, _through, derived_status
from backend.settings import (INSTRUCTIONS_CAP, SettingsChanged, _split_key, load_instructions, load_settings,
                              visible, write_private)

log = logging.getLogger(__name__)

PROJECT_DEFAULTS = {  # written to a new project's config.toml
    "project.citation_style": "apa7",
    "project.citation_style_zh": "gbt7714-2025",
    "project.template": "imrad",
    "project.budget_usd": 50,
}


class _AnyName(Convertor):
    """A path segment holding any non-empty name: slashes and line breaks included."""
    regex = "(?s:.+)"

    def convert(self, value):
        return value

    def to_string(self, value):
        return value


register_url_convertor("name", _AnyName())  # a provider's name is any TOML key


async def _to_end(awaitable):
    """Await to its end. A cancellation waits for it and is raised after it, so what it
    writes is always followed through (caches cleared, audit written, runs revoked) and
    a lock held around it covers all of it."""
    result, cancelled = await _through(awaitable)
    if cancelled:
        raise asyncio.CancelledError()
    return result


async def _finished(fn, *args):
    """Run fn in a worker thread to its end (see _to_end)."""
    return await _to_end(asyncio.to_thread(fn, *args))


class ApiError(Exception):
    def __init__(self, status: int, code: str, message: str):
        super().__init__(message)
        self.status, self.code, self.message = status, code, message


def _error(status, code, message):
    return JSONResponse({"code": code, "message": message}, status_code=status)


def _visible(text):
    """A name or title, trimmed; one with no visible character (see runs.visible) is refused."""
    if text is None:
        return None
    if visible(text) is None:
        raise ValueError("it needs a visible character")
    return visible(text)


class NewProject(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    _name = field_validator("name")(classmethod(lambda cls, v: _visible(v)))


class ProjectChange(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=200)
    target_venue: str | None = Field(default=None, max_length=200)
    _name = field_validator("name")(classmethod(lambda cls, v: _visible(v)))


class NewConversation(BaseModel):
    project_id: str | None = None
    title: str | None = Field(default=None, max_length=200)


class Rename(BaseModel):
    title: str = Field(min_length=1, max_length=200)

    _title = field_validator("title")(classmethod(lambda cls, v: _visible(v)))


class Move(BaseModel):
    project_id: str = Field(min_length=1, max_length=100)


# Sensitivity levels from least to most strict (ticket 14: a conversation moves only to a
# project at an equal or stricter level).
_STRICTNESS = {"normal": 0, "private": 1, "local_only": 2}

# The interface's pages load only its own files. Images in model output are never fetched
# from elsewhere (hardening PR02A): the interface shows them as links, and this refuses any
# it misses. Inline styles are allowed because the dialogs' scroll lock inserts one.
_CSP = ("default-src 'self'; img-src 'self' data:; style-src 'self' 'unsafe-inline'; object-src 'none';"
        " base-uri 'none'; frame-ancestors 'none'; form-action 'self'")


class Message(BaseModel):
    content: str
    model: str | None = Field(default=None, max_length=512)  # as long as the model catalog accepts
    provider: str | None = None  # any configured provider's name; admission checks it is one
    effort: str | None = None


class Continue(BaseModel):
    model: str | None = Field(default=None, max_length=512)
    provider: str | None = None
    effort: str | None = None


def _usable_key(key):
    """A provider key, trimmed (a pasted key often ends in a line break); it must be printable
    ASCII with no spaces, as a Bearer token in a request header needs."""
    key = key.strip()
    if not key or any(not "\x21" <= c <= "\x7e" for c in key):
        raise ValueError("a key is printable text without spaces")
    return key


class Key(BaseModel):
    key: str = Field(min_length=1, max_length=1000)
    _key = field_validator("key")(classmethod(lambda cls, v: _usable_key(v)))


class Setup(BaseModel):
    openrouter_key: str = Field(min_length=1, max_length=1000)
    _key = field_validator("openrouter_key")(classmethod(lambda cls, v: _usable_key(v)))


class SettingsUpdate(BaseModel):
    project_id: str | None = None
    hash: str | None = None  # the file hash the client read; a mismatch refuses the save
    updates: dict


class Instructions(BaseModel):
    project_id: str | None = None
    text: str


class EventStream(StreamingResponse):
    """A server-sent event stream that always listens for the client's disconnect and
    stops the stream when it comes, whatever ASGI version the server speaks. However
    the response ends (finished, disconnected, or failed before it started), on_close
    runs, so a closed window stops its turn as Stop does and a claim is never left
    behind. The request body has been read before the response starts, so this
    listener is the only reader of `receive`."""

    def __init__(self, events, on_close):
        super().__init__(events, media_type="text/event-stream", headers={"Cache-Control": "no-store"})
        self.on_close = on_close

    async def __call__(self, scope, receive, send):
        try:
            async with anyio.create_task_group() as group:
                async def run_then_stop(fn):
                    await fn()
                    group.cancel_scope.cancel()

                group.start_soon(run_then_stop, partial(self.stream_response, send))
                await run_then_stop(partial(self.listen_for_disconnect, receive))
        finally:
            self.on_close()


def create_app(data_dir, *, origin: str, dev_origins=(), session=None, frontend_dir=None, keyring_backend=None,
               transport=None) -> FastAPI:
    """The app for one data folder, served at origin (e.g. "http://127.0.0.1:53111").

    session is this launch's secret, which every API request must then carry (see
    local_guard; the desktop entry always sets one); frontend_dir holds the built interface; keyring_backend selects the credential
    store (None: the system's); transport is where the outbound gate sends checked
    requests (None: the network; tests pass a mock).
    """
    data_dir = Path(data_dir)
    frontend_dir = Path(frontend_dir).resolve() if frontend_dir else None
    state = {"maintenance_lock": threading.Lock()}  # see maintenance
    # ponytail: one lock for every write to a project's folder and for deleting a project, so a
    # write can never recreate the folder of a project deleted meanwhile; such writes are rare.
    project_files = asyncio.Lock()

    @contextlib.contextmanager
    def maintenance():
        """Mark local maintenance for the desktop entry's start deadline: state holds when the
        current maintenance started and how long finished maintenance took, both changed with
        the clock read under state["maintenance_lock"], which the deadline reads under too."""
        lock = state.setdefault("maintenance_lock", threading.Lock())  # made with the app; again after a restart
        with lock:
            started = state["maintenance_started"] = time.monotonic()
        try:
            yield
        finally:
            with lock:
                state["maintenance_seconds"] = state.get("maintenance_seconds", 0.0) + time.monotonic() - started
                del state["maintenance_started"]

    @contextlib.asynccontextmanager
    async def lifespan(app):
        # Opening the database (its checks, the backup before a migration, migrations) and the
        # daily backup are local maintenance: the desktop entry's start deadline does not count
        # them (see maintenance), since on a large folder they are progress, not a hang.
        with maintenance():
            db = await asyncio.to_thread(Database, data_dir)
        content = ContentStore(db)
        gate = OutboundGate(db, lambda: providers.gate_inputs(data_dir), transport=transport)
        harness = Harness(data_dir, db, gate, keyring_backend=keyring_backend)
        state.update(db=db, content=content, gate=gate, harness=harness)
        try:
            await harness.recover()
            await asyncio.to_thread(_sweep_deleted_project_folders, data_dir, db)
            # The daily backup runs at launch, before the app accepts a request, so the
            # database and the settings files it copies show one state. Backups while the app
            # is open and idle are S1-12's.
            with maintenance():
                await asyncio.to_thread(_daily_backup, db)
            yield
        finally:
            await harness.shutdown()
            await asyncio.to_thread(db.close)
            state.clear()

    app = FastAPI(lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)
    app.state.scholia = state  # the desktop entry reaches the harness through it at shutdown

    @app.exception_handler(ApiError)
    @app.exception_handler(AdmissionError)
    async def known_error(request, error):
        return _error(error.status, error.code, error.message)

    @app.exception_handler(RequestValidationError)
    async def invalid(request, error):
        return _error(400, "invalid_request", "The request is not valid")

    @app.exception_handler(StarletteHTTPException)
    async def http_error(request, error):
        codes = {404: "not_found", 405: "method_not_allowed"}
        return _error(error.status_code, codes.get(error.status_code, "http_error"), "The request was not handled")

    def db() -> Database:
        return state["db"]

    def harness() -> Harness:
        return state["harness"]

    async def read(fn):
        return await asyncio.to_thread(db().read, fn)

    async def write(fn):
        return await asyncio.to_thread(db().write, fn)

    # Health and setup

    @app.get("/api/health")
    async def health():
        return {"ok": True, "version": APP_VERSION, "data_folder": str(data_dir)}

    async def provider_list():
        configured = providers.configured(data_dir)
        keys = await asyncio.gather(*(asyncio.to_thread(credentials.load_key, data_dir, name, keyring_backend)
                                      for name in configured))
        return [{"name": p.name, "kind": p.kind, "base_url": p.base_url, "has_key": key is not None}
                for p, key in zip(configured.values(), keys)]

    @app.get("/api/setup")
    async def setup_status():
        listed = await provider_list()
        return {"needed": not any(p["has_key"] for p in listed)}

    @app.post("/api/setup")
    async def setup(body: Setup):
        async with harness().settings_lock:  # ordered with the provider snapshot of turn admission
            refuse_if_busy({providers.OPENROUTER})

            async def set_up():
                await asyncio.to_thread(_ensure_openrouter, data_dir)
                return await _save_key(providers.OPENROUTER, body.openrouter_key)

            return {"ok": True, "warning": await _to_end(set_up())}

    def revoke_soon():
        """For a deletion's on_committed: stop its runs on this loop as soon as it commits."""
        loop, active = asyncio.get_running_loop(), harness()
        return lambda revoked: loop.call_soon_threadsafe(active.revoke, revoked)

    def forget_providers():
        """Forget what was learned about providers (catalogs, reasoning negotiation), after a
        provider's settings or key changed. It starts a new catalog generation."""
        openrouter.clear_negotiation_cache()
        openrouter_client.clear_cache()

    def refuse_if_busy(names):
        """A provider's settings or key do not change under work that is calling it."""
        if any(harness().provider_busy(name) for name in names):
            raise ApiError(409, "active_run", "A running task uses this provider; stop it or wait")

    async def _save_key(provider, key):
        """Store a key, clear what was learned with the old one, and audit the change.
        Callers run it to its end (_to_end), so a stored key is always followed through.
        A save that fails may still have changed the key (a file replaced before its
        folder could be synced): it is followed through too, audited as uncertain."""
        stored_in = "uncertain"
        try:
            warning = await asyncio.to_thread(credentials.save_key, data_dir, provider, key, keyring_backend)
            stored_in = "file" if warning else "credential_store"
        except credentials.CredentialsFileError:
            raise ApiError(500, "key_not_saved", "The key could not be saved") from None
        finally:
            forget_providers()
            await write(lambda conn: conn.execute(  # which provider's key changed and where it went; never the key
                "INSERT INTO audit_log (event, data) VALUES ('key_changed', ?)",
                (json.dumps({"provider": provider, "stored_in": stored_in}),)))
        return "credential_store_unavailable" if warning else None

    @app.get("/api/providers")
    async def provider_index():
        return {"providers": await provider_list()}

    @app.put("/api/keys/{provider:name}")
    async def put_key(provider: str, body: Key):
        async with harness().settings_lock:  # ordered with the provider snapshot of turn admission
            if provider not in providers.configured(data_dir):  # as the key is saved against it
                raise ApiError(404, "unknown_provider", "That provider is not set up")
            refuse_if_busy({provider})
            return {"ok": True, "warning": await _to_end(_save_key(provider, body.key))}

    @app.get("/api/providers/{provider:name}/models")
    async def provider_models(provider: str, refresh: bool = False):
        async with harness().settings_lock:  # a snapshot of the provider and its key, and its generation
            configured = providers.configured(data_dir)
            if provider not in configured:
                raise ApiError(404, "unknown_provider", "That provider is not set up")
            key = await asyncio.to_thread(credentials.load_key, data_dir, provider, keyring_backend)
            generation = openrouter_client.generation()
        general = await read(lambda conn: conn.execute("SELECT id FROM projects WHERE kind = 'general'").fetchone()[0])
        async with state["gate"].async_client(general) as client:
            # A snapshot older than a provider change caches nothing (its refresh is refused).
            models = await openrouter_client.models(client, configured[provider], key, force=refresh,
                                                    generation=generation)
        status = openrouter_client.catalog_status(configured[provider], key)
        if openrouter_client.generation() != generation:  # after everything this answer reports
            raise ApiError(409, "settings_changed", "The provider changed while its models were listed")
        return {"models": sorted((models or {}).values(), key=lambda m: m["id"]), "status": status}

    # Settings and instructions

    async def settings_for(project_id):
        if project_id is not None:
            await project_row(project_id)
        return await asyncio.to_thread(load_settings, data_dir, project_id)

    @app.get("/api/settings")
    async def get_settings(project_id: str | None = None):
        async with project_files if project_id is not None else contextlib.nullcontext():  # ordered with deletion
            loaded = await settings_for(project_id)
        return {"values": loaded.values, "warnings": loaded.warnings, "hash": loaded._digest}

    @app.put("/api/settings")
    async def put_settings(body: SettingsUpdate):
        async with project_files if body.project_id is not None else contextlib.nullcontext():
            async with harness().settings_lock:  # ordered with the budget reads of call admission
                return await save_settings(body)

    async def save_settings(body):
        loaded = await settings_for(body.project_id)  # for a project: checked to exist under the lock
        if body.hash != loaded._digest:
            raise ApiError(409, "settings_changed", "The settings changed since they were read")
        changed = _providers_changed(body.updates, providers.configured(data_dir))
        refuse_if_busy(changed)  # before any field is written
        try:
            await _finished(loaded.save, body.updates)  # under the project-files lock to its end
        except SettingsChanged:
            raise ApiError(409, "settings_changed", "The settings changed since they were read") from None
        except ValueError:
            raise ApiError(400, "invalid_setting", "A setting is not valid") from None
        finally:
            # A provider may have changed, whatever became of the request or of the save after
            # it replaced the file: what was learned about it no longer holds. ponytail: cleared
            # even when nothing was written, which only costs a re-learn.
            if changed:
                forget_providers()
        return {"values": loaded.values, "warnings": loaded.warnings, "hash": loaded._digest}

    @app.get("/api/instructions")
    async def get_instructions(project_id: str | None = None):
        async with project_files if project_id is not None else contextlib.nullcontext():  # ordered with deletion
            path = await instructions_path(project_id)
            text = await asyncio.to_thread(
                lambda: path.read_text(encoding="utf-8", errors="replace") if path.is_file() else "")
            combined, warnings = await asyncio.to_thread(load_instructions, data_dir, project_id)
        return {"text": text, "warnings": warnings, "cap_bytes": INSTRUCTIONS_CAP}

    @app.put("/api/instructions")
    async def put_instructions(body: Instructions):
        async with project_files if body.project_id is not None else contextlib.nullcontext():
            path = await instructions_path(body.project_id)  # the project still exists, under the lock
            await _finished(write_private, path, body.text.encode("utf-8"))
        _, warnings = await asyncio.to_thread(load_instructions, data_dir, body.project_id)
        return {"ok": True, "warnings": warnings}

    async def instructions_path(project_id):
        if project_id is None:
            return data_dir / "AGENTS.md"
        await project_row(project_id)
        return data_dir / "projects" / project_id / "AGENTS.md"

    # Projects

    def project_dict(row):
        keys = ("id", "name", "kind", "sensitivity", "review_lock", "target_venue", "created_at", "updated_at")
        project = dict(zip(keys, row))
        project["review_lock"] = bool(project["review_lock"])
        return project

    _PROJECT_COLUMNS = "id, name, kind, sensitivity, review_lock, target_venue, created_at, updated_at"

    async def project_row(project_id):
        row = await read(lambda conn: conn.execute(
            f"SELECT {_PROJECT_COLUMNS} FROM projects WHERE id = ?", (project_id,)).fetchone())
        if row is None:
            raise ApiError(404, "not_found", "No such project")
        return row

    @app.get("/api/projects")
    async def list_projects():
        rows = await read(lambda conn: conn.execute(
            f"SELECT {_PROJECT_COLUMNS} FROM projects ORDER BY kind = 'general' DESC, updated_at DESC").fetchall())
        return {"projects": [project_dict(row) for row in rows]}

    @app.post("/api/projects", status_code=201)
    async def create_project(body: NewProject):
        project_id = new_id()
        folder = data_dir / "projects" / project_id

        def insert(conn):
            conn.execute("INSERT INTO projects (id, name, kind) VALUES (?, ?, 'research')", (project_id, body.name))
            conn.execute("INSERT INTO audit_log (event, project_id, data) VALUES ('project_created', ?, '{}')",
                         (project_id,))

        async def creating():  # the folder, then the record; a folder with no record is removed
            try:
                await asyncio.to_thread(_write_project_folder, data_dir, project_id)
                await write(insert)
            except BaseException:
                await asyncio.to_thread(shutil.rmtree, folder, ignore_errors=True)
                raise

        await _to_end(creating())  # a cancelled request still ends with both, or neither
        return project_dict(await project_row(project_id))

    @app.get("/api/projects/{project_id}")
    async def get_project(project_id: str):
        return project_dict(await project_row(project_id))

    @app.patch("/api/projects/{project_id}")
    async def change_project(project_id: str, body: ProjectChange):
        changes = body.model_dump(exclude_unset=True)
        if "name" in changes and changes["name"] is None:
            raise ApiError(400, "invalid_request", "A project needs a name")

        def update(conn):
            if conn.execute("SELECT 1 FROM projects WHERE id = ?", (project_id,)).fetchone() is None:
                raise ApiError(404, "not_found", "No such project")
            for column, value in changes.items():
                conn.execute(f"UPDATE projects SET {column} = ?, updated_at = ? WHERE id = ?",
                             (value, utc_now(), project_id))
        await write(update)
        return project_dict(await project_row(project_id))

    @app.delete("/api/projects/{project_id}")
    async def delete_project(project_id: str):
        row = await project_row(project_id)
        if row[2] == "general":
            raise ApiError(400, "general_project", "The General project cannot be deleted")
        async def deleting():  # the record, the stopping of its runs and its folder, to their end
            revoked = await asyncio.to_thread(delete, db(), state["content"], "project", project_id,
                                              on_committed=revoke_soon())
            harness().revoke(revoked)
            return await asyncio.to_thread(_remove_folder, data_dir / "projects" / project_id)

        async with project_files:
            try:
                removed = await _to_end(deleting())
            except LookupError:  # deleted meanwhile by another request
                raise ApiError(404, "not_found", "No such project") from None
        if not removed:  # the record is gone; its tombstone makes the next launch retry the files
            log.warning("a deleted project's folder could not be removed fully; it is retried at the next launch")
            return {"ok": True, "files_left": True}
        return {"ok": True}

    # Conversations

    def conversation_dict(row):
        keys = ("id", "project_id", "title", "title_source", "title_rev", "created_at", "updated_at")
        return dict(zip(keys, row))

    _CONVERSATION_COLUMNS = "id, project_id, title, title_source, title_rev, created_at, updated_at"

    @app.get("/api/conversations")
    async def list_conversations(project_id: str | None = None):
        rows = await read(lambda conn: conn.execute(
            f"SELECT {_CONVERSATION_COLUMNS} FROM conversations"
            " WHERE ? IS NULL OR project_id = ? ORDER BY updated_at DESC", (project_id, project_id)).fetchall())
        active = harness().registry.turns
        return {"conversations": [{**conversation_dict(row), "running": row[0] in active} for row in rows]}

    @app.post("/api/conversations", status_code=201)
    async def create_conversation(body: NewConversation):
        conversation_id = new_id()
        title = visible(body.title)  # a title with nothing visible is no title, so one is generated

        def insert(conn):
            project_id = body.project_id or conn.execute(
                "SELECT id FROM projects WHERE kind = 'general'").fetchone()[0]
            if conn.execute("SELECT 1 FROM projects WHERE id = ?", (project_id,)).fetchone() is None:
                raise ApiError(404, "not_found", "No such project")
            conn.execute(
                "INSERT INTO conversations (id, project_id, title, title_source) VALUES (?, ?, ?, ?)",
                (conversation_id, project_id, title, "researcher" if title else None))
            conn.execute("UPDATE projects SET updated_at = ? WHERE id = ?", (utc_now(), project_id))
        await write(insert)
        return await get_conversation(conversation_id)

    @app.get("/api/conversations/{conversation_id}")
    async def get_conversation(conversation_id: str):
        def fetch(conn):
            row = conn.execute(f"SELECT {_CONVERSATION_COLUMNS} FROM conversations WHERE id = ?",
                               (conversation_id,)).fetchone()
            turns = conn.execute(
                "SELECT t.run_id, t.seq, t.author, t.user_message, t.answer, t.result_saved, t.reason_code,"
                " r.status, r.cancel_reason, r.settled_cost_usd, r.started_at, r.finished_at, t.retry_of_run_id,"
                " t.accounting"
                " FROM turns t JOIN runs r ON r.id = t.run_id WHERE t.conversation_id = ? ORDER BY t.seq",
                (conversation_id,)).fetchall()
            return row, turns
        row, turns = await read(fetch)
        if row is None:
            raise ApiError(404, "not_found", "No such conversation")
        registry = harness().registry
        return {**conversation_dict(row), "running": conversation_id in registry.turns, "turns": [{
            "run_id": run_id, "seq": seq, "author": author, "message": json.loads(message),
            "answer": json.loads(answer) if answer else None, "result_saved": bool(saved),
            "status": derived_status(status, run_id, registry), "reason_code": reason, "cancel_reason": cancel,
            "cost_usd": cost, "accounting": json.loads(accounting) if accounting else None,
            "started_at": started, "finished_at": finished, "continues": retry_of,
        } for run_id, seq, author, message, answer, saved, reason, status, cancel, cost, started, finished, retry_of,
            accounting in turns]}

    @app.put("/api/conversations/{conversation_id}")
    async def rename_conversation(conversation_id: str, body: Rename):
        def rename(conn):
            # Every rename, even to the same text, moves title_rev, so a title generated
            # from an earlier state is never written over it.
            cursor = conn.execute(
                "UPDATE conversations SET title = ?, title_source = 'researcher', title_rev = title_rev + 1,"
                " updated_at = ? WHERE id = ?", (body.title, utc_now(), conversation_id))
            if cursor.rowcount == 0:
                raise ApiError(404, "not_found", "No such conversation")
        await write(rename)
        return await get_conversation(conversation_id)

    @app.post("/api/conversations/{conversation_id}/move")
    async def move_conversation(conversation_id: str, body: Move):
        """Move a conversation to another project at an equal or stricter level, with its
        turns and their runs, so its history moves intact. What it spent stays in the
        spending of the project where it was spent. Refused while it runs a turn or a
        title run."""
        if conversation_id in harness().registry.turns:
            raise ApiError(409, "active_run", "This conversation is running a turn")

        def move(conn):
            row = conn.execute(
                "SELECT c.project_id, p.sensitivity FROM conversations c JOIN projects p ON p.id = c.project_id"
                " WHERE c.id = ?", (conversation_id,)).fetchone()
            target = conn.execute("SELECT sensitivity FROM projects WHERE id = ?", (body.project_id,)).fetchone()
            if row is None or target is None:
                raise ApiError(404, "not_found", "No such conversation or project")
            source, level = row
            if source == body.project_id:
                return
            # Its turn, or a title run that would send its words over the old project's route;
            # ordered with admission by the single writer.
            if conn.execute("SELECT 1 FROM runs WHERE status = 'running' AND (conversation_id = ?"
                            " OR source_turn_id IN (SELECT run_id FROM turns WHERE conversation_id = ?))",
                            (conversation_id, conversation_id)).fetchone():
                raise ApiError(409, "active_run", "This conversation is running a turn")
            if _STRICTNESS[target[0]] < _STRICTNESS[level]:
                raise ApiError(409, "less_strict_project", "A conversation moves only to a project as strict or stricter")
            now = utc_now()
            conn.execute("UPDATE conversations SET project_id = ?, updated_at = ? WHERE id = ?",
                         (body.project_id, now, conversation_id))
            conn.execute(
                "UPDATE runs SET project_id = ? WHERE conversation_id = ?"
                " OR source_turn_id IN (SELECT run_id FROM turns WHERE conversation_id = ?)",
                (body.project_id, conversation_id, conversation_id))
            conn.execute("INSERT INTO audit_log (event, project_id, data) VALUES ('conversation_moved', ?, ?)",
                         (body.project_id, json.dumps({"conversation_id": conversation_id, "from": source})))

        await write(move)
        return await get_conversation(conversation_id)

    @app.delete("/api/conversations/{conversation_id}")
    async def delete_conversation(conversation_id: str):
        async def deleting():  # the deletion and the stopping of its runs, together, to their end
            revoked = await asyncio.to_thread(delete, db(), state["content"], "conversation", conversation_id,
                                              on_committed=revoke_soon())
            harness().revoke(revoked)

        try:
            await _to_end(deleting())
        except LookupError:
            raise ApiError(404, "not_found", "No such conversation") from None
        return {"ok": True}

    # Turns and runs

    @app.post("/api/conversations/{conversation_id}/message/stream")
    async def send_message(conversation_id: str, body: Message):
        claim = await harness().admit_turn(conversation_id, body.content, model=body.model,
                                           provider=body.provider, effort=body.effort)
        return stream(claim)

    def stream(claim):
        async def sse():
            async for event in harness().events(claim):
                yield f"data: {json.dumps(event)}\n\n"
        return EventStream(sse(), on_close=lambda: harness().stream_closed(claim))

    @app.post("/api/runs/{run_id}/continue")
    async def continue_run(run_id: str, body: Continue | None = None):
        body = body or Continue()
        claim = await harness().continue_turn(run_id, model=body.model, provider=body.provider, effort=body.effort)
        return stream(claim)

    @app.post("/api/runs/{run_id}/cancel")
    async def cancel_run(run_id: str):
        result = await harness().cancel(run_id)
        if result is None:
            raise ApiError(404, "not_found", "No such run")
        return result

    @app.get("/api/activity")
    async def activity(limit: int = 50):
        rows = await read(lambda conn: conn.execute(
            "SELECT r.id, r.project_id, r.workflow, r.status, r.cancel_reason, r.settled_cost_usd, r.attempts,"
            " r.started_at, r.finished_at, p.name, p.kind FROM runs r JOIN projects p ON p.id = r.project_id"
            # Every running run, where it is cancelled, then the newest others up to the limit.
            " WHERE r.kind = 'background' AND (r.status = 'running' OR r.id IN (SELECT id FROM runs"
            " WHERE kind = 'background' AND status != 'running' ORDER BY started_at DESC LIMIT ?))"
            " ORDER BY r.status = 'running' DESC, r.started_at DESC", (max(1, min(limit, 200)),)).fetchall())
        registry = harness().registry
        return {"runs": [{
            "run_id": run_id, "project_id": project_id, "project_name": name, "project_kind": kind,
            "workflow": workflow, "status": derived_status(status, run_id, registry), "cancel_reason": cancel,
            "cost_usd": cost, "attempts": attempts, "started_at": started, "finished_at": finished,
        } for run_id, project_id, workflow, status, cancel, cost, attempts, started, finished, name, kind in rows]}

    # The interface: built files only, from inside their folder

    @app.api_route("/api/{rest:path}", methods=["GET", "POST", "PUT", "PATCH", "DELETE"])
    async def unknown_api(rest: str):
        raise ApiError(404, "not_found", "No such endpoint")

    @app.get("/{path:path}")
    async def interface(path: str):
        file = static_file(frontend_dir, path)
        if file is None:
            raise ApiError(404, "not_found", "Not found")
        return FileResponse(file, headers={"Cache-Control": "no-cache", "Content-Security-Policy": _CSP})

    return LocalRequestGuard(app, origin=origin, dev_origins=dev_origins, session=session)


def _providers_changed(updates, configured) -> set:
    """The providers a settings update would change, reading each key as the settings file
    does (quoted parts included). Changing the whole providers table changes every one."""
    names = set()
    for key, value in updates.items():
        try:
            parts = _split_key(key)
        except Exception:  # not a TOML key: the save refuses it
            continue
        if parts[:1] == ("providers",):
            if len(parts) > 1:
                names.add(parts[1])
            else:
                names.update(configured)
                names.update(value if isinstance(value, dict) else ())
    return names


def static_file(root: Path | None, path: str) -> Path | None:
    """The file under root that a navigation path names, or None.

    Every segment must be an ordinary name: no empty, "." or ".." segment, no
    dotfile, no backslash, colon or control character. The resolved file must stay
    inside root (so a symbolic link cannot lead out) and be a regular file. A path
    with no file extension that names no file is a route of the single-page app,
    and gets index.html.
    """
    if root is None:
        return None
    segments = path.split("/") if path else []
    for segment in segments:
        if (not segment or segment.startswith(".") or any(c in segment for c in "\\:")
                or any(ord(c) < 32 or ord(c) == 127 for c in segment)):
            return None
    candidate = root.joinpath(*segments) if segments else root / "index.html"
    for target in (candidate, None if (segments and "." in segments[-1]) else root / "index.html"):
        if target is None:
            continue
        try:
            resolved = target.resolve(strict=True)
        except (OSError, RuntimeError):
            continue
        if resolved.is_relative_to(root) and stat.S_ISREG(resolved.stat().st_mode):
            return resolved
    return None


def _remove_folder(path) -> bool:
    """Remove a folder and everything in it. Returns whether nothing is left."""
    path = Path(path)
    if not path.exists() and not path.is_symlink():
        return True
    errors = []
    shutil.rmtree(path, onexc=lambda function, failed, error: errors.append(error))
    return not errors and not path.exists() and not path.is_symlink()


def _daily_backup(db):
    try:
        db.backup_if_due()
    except Exception as error:  # the app stays open; the next launch tries again
        log.warning("the daily backup failed (%s)", type(error).__name__)


def _sweep_deleted_project_folders(data_dir, db):
    """Remove the folders of projects that were deleted but whose files a failure left behind.
    Only folders named by a project's tombstone are touched."""
    projects = Path(data_dir) / "projects"
    if not projects.is_dir() or projects.is_symlink():
        return
    deleted = {object_id for (object_id,) in db.read(lambda conn: conn.execute(
        "SELECT object_id FROM tombstones WHERE kind = 'project'").fetchall())}
    for folder in projects.iterdir():
        if folder.name in deleted and not _remove_folder(folder):
            log.warning("a deleted project's folder still could not be removed")


def _ensure_openrouter(data_dir):
    personal = load_settings(data_dir)
    table = personal.values.get("providers", {}).get(providers.OPENROUTER, {})
    updates = {}
    if not table.get("kind"):
        updates["providers.openrouter.kind"] = "openrouter"
    if not table.get("base_url"):
        updates["providers.openrouter.base_url"] = providers.OPENROUTER_BASE_URL
    if updates:
        personal.save(updates)


def _write_project_folder(data_dir, project_id):
    settings = load_settings(data_dir, project_id)
    settings.save(PROJECT_DEFAULTS)
    write_private(Path(data_dir) / "projects" / project_id / "AGENTS.md", b"")
