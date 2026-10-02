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
from functools import partial
from pathlib import Path

import anyio
from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from pydantic import BaseModel, Field
from starlette.exceptions import HTTPException as StarletteHTTPException

from backend import APP_VERSION, credentials, openrouter, openrouter_client, providers
from backend.db import ContentStore, Database, delete, new_id, utc_now
from backend.local_guard import LocalRequestGuard
from backend.outbound_gate import OutboundGate
from backend.runs import AdmissionError, Harness, derived_status
from backend.settings import INSTRUCTIONS_CAP, SettingsChanged, load_instructions, load_settings, write_private

log = logging.getLogger(__name__)

PROJECT_DEFAULTS = {  # written to a new project's config.toml
    "project.citation_style": "apa7",
    "project.citation_style_zh": "gbt7714-2025",
    "project.template": "imrad",
    "project.budget_usd": 50,
}


class ApiError(Exception):
    def __init__(self, status: int, code: str, message: str):
        super().__init__(message)
        self.status, self.code, self.message = status, code, message


def _error(status, code, message):
    return JSONResponse({"code": code, "message": message}, status_code=status)


class NewProject(BaseModel):
    name: str = Field(min_length=1, max_length=200)


class ProjectChange(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=200)
    target_venue: str | None = Field(default=None, max_length=200)


class NewConversation(BaseModel):
    project_id: str | None = None
    title: str | None = Field(default=None, max_length=200)


class Rename(BaseModel):
    title: str = Field(min_length=1, max_length=200)


class Message(BaseModel):
    content: str
    model: str | None = Field(default=None, max_length=200)
    provider: str | None = Field(default=None, max_length=100)
    effort: str | None = None


class Key(BaseModel):
    key: str = Field(min_length=1, max_length=1000)


class Setup(BaseModel):
    openrouter_key: str = Field(min_length=1, max_length=1000)


class SettingsUpdate(BaseModel):
    project_id: str | None = None
    hash: str | None = None  # the file hash the client read; a mismatch refuses the save
    updates: dict


class Instructions(BaseModel):
    project_id: str | None = None
    text: str


class EventStream(StreamingResponse):
    """A server-sent event stream that always listens for the client's disconnect and
    stops the stream when it comes, whatever ASGI version the server speaks, so a
    closed window stops its turn as Stop does. The request body has been read before
    the response starts, so this listener is the only reader of `receive`."""

    def __init__(self, events):
        super().__init__(events, media_type="text/event-stream", headers={"Cache-Control": "no-store"})

    async def __call__(self, scope, receive, send):
        async with anyio.create_task_group() as group:
            async def run_then_stop(fn):
                await fn()
                group.cancel_scope.cancel()

            group.start_soon(run_then_stop, partial(self.stream_response, send))
            await run_then_stop(partial(self.listen_for_disconnect, receive))


def create_app(data_dir, *, origin: str, dev_origins=(), frontend_dir=None, keyring_backend=None,
               transport=None) -> FastAPI:
    """The app for one data folder, served at origin (e.g. "http://127.0.0.1:53111").

    frontend_dir holds the built interface; keyring_backend selects the credential
    store (None: the system's); transport is where the outbound gate sends checked
    requests (None: the network; tests pass a mock).
    """
    data_dir = Path(data_dir)
    frontend_dir = Path(frontend_dir).resolve() if frontend_dir else None
    state = {}

    @contextlib.asynccontextmanager
    async def lifespan(app):
        db = await asyncio.to_thread(Database, data_dir)
        content = ContentStore(db)
        gate = OutboundGate(db, lambda: providers.gate_inputs(data_dir), transport=transport)
        harness = Harness(data_dir, db, gate, keyring_backend=keyring_backend)
        state.update(db=db, content=content, gate=gate, harness=harness)
        try:
            await harness.recover()
            try:
                await asyncio.to_thread(db.backup_if_due)
            except Exception as error:  # the app still opens; the next launch tries again
                log.warning("the daily backup failed (%s)", type(error).__name__)
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
        return {"ok": True, "version": APP_VERSION}

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
        await asyncio.to_thread(_ensure_openrouter, data_dir)
        warning = await _save_key(providers.OPENROUTER, body.openrouter_key)
        return {"ok": True, "warning": warning}

    async def _save_key(provider, key):
        try:
            warning = await asyncio.to_thread(credentials.save_key, data_dir, provider, key, keyring_backend)
        except credentials.CredentialsFileError:
            raise ApiError(500, "key_not_saved", "The key could not be saved") from None
        openrouter.clear_negotiation_cache()
        openrouter_client.clear_cache()
        return "credential_store_unavailable" if warning else None

    @app.get("/api/providers")
    async def provider_index():
        return {"providers": await provider_list()}

    @app.put("/api/keys/{provider}")
    async def put_key(provider: str, body: Key):
        if provider not in providers.configured(data_dir):
            raise ApiError(404, "unknown_provider", "That provider is not set up")
        return {"ok": True, "warning": await _save_key(provider, body.key)}

    @app.get("/api/providers/{provider}/models")
    async def provider_models(provider: str, refresh: bool = False):
        configured = providers.configured(data_dir)
        if provider not in configured:
            raise ApiError(404, "unknown_provider", "That provider is not set up")
        key = await asyncio.to_thread(credentials.load_key, data_dir, provider, keyring_backend)
        general = await read(lambda conn: conn.execute("SELECT id FROM projects WHERE kind = 'general'").fetchone()[0])
        async with state["gate"].async_client(general) as client:
            models = await openrouter_client.models(client, configured[provider], key, force=refresh)
        status = openrouter_client.catalog_status(configured[provider], key)
        return {"models": sorted((models or {}).values(), key=lambda m: m["id"]), "status": status}

    # Settings and instructions

    async def settings_for(project_id):
        if project_id is not None:
            await project_row(project_id)
        return await asyncio.to_thread(load_settings, data_dir, project_id)

    @app.get("/api/settings")
    async def get_settings(project_id: str | None = None):
        loaded = await settings_for(project_id)
        return {"values": loaded.values, "warnings": loaded.warnings, "hash": loaded._digest}

    @app.put("/api/settings")
    async def put_settings(body: SettingsUpdate):
        loaded = await settings_for(body.project_id)
        if body.hash != loaded._digest:
            raise ApiError(409, "settings_changed", "The settings changed since they were read")
        try:
            await asyncio.to_thread(loaded.save, body.updates)
        except SettingsChanged:
            raise ApiError(409, "settings_changed", "The settings changed since they were read") from None
        except ValueError:
            raise ApiError(400, "invalid_setting", "A setting is not valid") from None
        if any(key.split(".")[0] == "providers" for key in body.updates):
            openrouter.clear_negotiation_cache()
            openrouter_client.clear_cache()
        return {"values": loaded.values, "warnings": loaded.warnings, "hash": loaded._digest}

    @app.get("/api/instructions")
    async def get_instructions(project_id: str | None = None):
        path = await instructions_path(project_id)
        text = path.read_text(encoding="utf-8", errors="replace") if path.is_file() else ""
        combined, warnings = await asyncio.to_thread(load_instructions, data_dir, project_id)
        return {"text": text, "warnings": warnings, "cap_bytes": INSTRUCTIONS_CAP}

    @app.put("/api/instructions")
    async def put_instructions(body: Instructions):
        path = await instructions_path(body.project_id)
        await asyncio.to_thread(write_private, path, body.text.encode("utf-8"))
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
        # The folder first: if the record then fails, an unused folder is all that is left.
        await asyncio.to_thread(_write_project_folder, data_dir, project_id)

        def insert(conn):
            conn.execute("INSERT INTO projects (id, name, kind) VALUES (?, ?, 'research')", (project_id, body.name))
            conn.execute("INSERT INTO audit_log (event, project_id, data) VALUES ('project_created', ?, '{}')",
                         (project_id,))
        try:
            await write(insert)
        except BaseException:
            await asyncio.to_thread(shutil.rmtree, folder, ignore_errors=True)
            raise
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
        revoked = await asyncio.to_thread(delete, db(), state["content"], "project", project_id)
        harness().revoke(revoked)
        await asyncio.to_thread(shutil.rmtree, data_dir / "projects" / project_id, ignore_errors=True)
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

        def insert(conn):
            project_id = body.project_id or conn.execute(
                "SELECT id FROM projects WHERE kind = 'general'").fetchone()[0]
            if conn.execute("SELECT 1 FROM projects WHERE id = ?", (project_id,)).fetchone() is None:
                raise ApiError(404, "not_found", "No such project")
            conn.execute(
                "INSERT INTO conversations (id, project_id, title, title_source) VALUES (?, ?, ?, ?)",
                (conversation_id, project_id, body.title, "researcher" if body.title else None))
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
                " r.status, r.cancel_reason, r.settled_cost_usd, r.started_at, r.finished_at"
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
            "cost_usd": cost, "started_at": started, "finished_at": finished,
        } for run_id, seq, author, message, answer, saved, reason, status, cancel, cost, started, finished in turns]}

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

    @app.delete("/api/conversations/{conversation_id}")
    async def delete_conversation(conversation_id: str):
        try:
            revoked = await asyncio.to_thread(delete, db(), state["content"], "conversation", conversation_id)
        except LookupError:
            raise ApiError(404, "not_found", "No such conversation") from None
        harness().revoke(revoked)
        return {"ok": True}

    # Turns and runs

    @app.post("/api/conversations/{conversation_id}/message/stream")
    async def send_message(conversation_id: str, body: Message):
        claim = await harness().admit_turn(conversation_id, body.content, model=body.model,
                                           provider=body.provider, effort=body.effort)

        async def sse():
            async for event in harness().events(claim):
                yield f"data: {json.dumps(event)}\n\n"
        return EventStream(sse())

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
            " r.started_at, r.finished_at, p.name FROM runs r JOIN projects p ON p.id = r.project_id"
            " WHERE r.kind = 'background' ORDER BY r.started_at DESC LIMIT ?", (max(1, min(limit, 200)),)).fetchall())
        registry = harness().registry
        return {"runs": [{
            "run_id": run_id, "project_id": project_id, "project_name": name, "workflow": workflow,
            "status": derived_status(status, run_id, registry), "cancel_reason": cancel, "cost_usd": cost,
            "attempts": attempts, "started_at": started, "finished_at": finished,
        } for run_id, project_id, workflow, status, cancel, cost, attempts, started, finished, name in rows]}

    # The interface: built files only, from inside their folder

    @app.api_route("/api/{rest:path}", methods=["GET", "POST", "PUT", "PATCH", "DELETE"])
    async def unknown_api(rest: str):
        raise ApiError(404, "not_found", "No such endpoint")

    @app.get("/{path:path}")
    async def interface(path: str):
        file = static_file(frontend_dir, path)
        if file is None:
            raise ApiError(404, "not_found", "Not found")
        return FileResponse(file, headers={"Cache-Control": "no-cache"})

    return LocalRequestGuard(app, origins=[origin, *dev_origins])


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
