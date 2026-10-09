"""The backend's HTTP API, on the SQLite store of one data folder.

`create_app` builds the app for a data folder. The desktop entry serves it on a
loopback port behind `LocalRequestGuard`; tests drive it in process. Errors carry
a stable `code` the interface translates, and an English `message`. Every write
goes through the single writer; database calls run off the event loop.
"""

import asyncio
import contextlib
import dataclasses
import hashlib
import json
import logging
import secrets
import shutil
import stat
import threading
import time
from functools import partial
from pathlib import Path
from typing import Literal

import anyio
from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from pydantic import BaseModel, Field, field_validator, model_validator
from starlette.convertors import Convertor, register_url_convertor
from starlette.exceptions import HTTPException as StarletteHTTPException

from backend import APP_VERSION, credentials, openrouter, openrouter_client, providers
from backend import backups
from backend import asks, materials
from backend import local_helper
from backend.db import ContentStore, Database, DatabaseClosedError, delete, new_id, utc_now
from backend.local_guard import LocalRequestGuard
from backend.outbound_gate import OutboundGate, local_origin
from backend import governance
from backend.runs import AdmissionError, Harness, _through, derived_status
from backend.settings import (INSTRUCTIONS_CAP, SettingsChanged, _split_key, instruction_file_size, instructions_size,
                              load_instructions, load_settings, visible, write_private)
from backend import budget_router, reasoning_capability

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


Level = Literal["normal", "private", "local_only"]


class NewProject(BaseModel):
    """A project and the answer to what it will hold (F1): Normal, Private, or someone else's
    submission, the review-lock preset (Local only with the lock, and the venue if known)."""
    name: str = Field(min_length=1, max_length=200)
    sensitivity: Level = "normal"
    review_lock: bool = False
    review_venue: str | None = Field(default=None, max_length=200)
    _name = field_validator("name")(classmethod(lambda cls, v: _visible(v)))

    @model_validator(mode="after")
    def _preset(self):
        if self.review_lock and self.sensitivity != "local_only":
            raise ValueError("the review lock comes with Local only")
        if not self.review_lock:
            self.review_venue = None
        return self


class SensitivityChange(BaseModel):
    level: Level
    token: str | None = Field(default=None, max_length=100)  # the confirmation a less strict level needs


class ReviewLock(BaseModel):
    locked: bool
    venue: str | None = Field(default=None, max_length=200)
    token: str | None = Field(default=None, max_length=100)  # the confirmation lifting the lock needs


class Declaration(BaseModel):
    provider: str = Field(min_length=1, max_length=1000)
    origin: str = Field(min_length=1, max_length=300)  # the server's origin as the researcher saw it


class KeyConfirmation(BaseModel):
    provider: str = Field(min_length=1, max_length=1000)
    statement: str = Field(min_length=1, max_length=100)  # the version of the statement the researcher saw
    key: str = Field(min_length=1, max_length=100)  # the reference to the key the card showed


class PrivateRouteChange(BaseModel):
    enabled: bool | None = None
    rechecked: bool = False  # the researcher checked the entry's terms again today


class AuditExport(BaseModel):
    project_id: str | None = None


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

CONFIRM_SECONDS = 600  # how long a confirmation token stays good

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
    hash: str | None = None  # the file as it was read; a change since then is refused


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
               transport=None, helper=None) -> FastAPI:
    """The app for one data folder, served at origin (e.g. "http://127.0.0.1:53111").

    session is this launch's secret, which every API request must then carry (see
    local_guard; the desktop entry always sets one); frontend_dir holds the built interface; keyring_backend selects the credential
    store (None: the system's); transport is where the outbound gate sends checked
    requests (None: the network; tests pass a mock); helper is the local model helper's
    local_helper.Config (None: the bundled helper and the offered models).
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

    async def start(db, kick=True):
        """Run the app on db: its content store, outbound gate and harness, recovered before any
        request reaches them. A restore calls it again with the database it put in place, with
        kick false: nothing runs on its own (background runs) until the restore has committed."""
        gate = OutboundGate(db, lambda: dataclasses.replace(providers.gate_inputs(data_dir),
                                                            helper_urls=local_helper.urls(state)), transport=transport)
        harness = Harness(data_dir, db, gate, keyring_backend=keyring_backend)
        content = ContentStore(db)
        # Local background work: reading materials and looking their identifiers up; full backups and exports.
        materials.register(harness, content)
        backups.register(harness, state)
        loop = asyncio.get_running_loop()

        def damaged():  # a backup's full check found it damaged: the app is limited, so its work stops too
            loop.call_soon_threadsafe(lambda: asyncio.ensure_future(harness.shutdown()))

        db.on_damage = damaged
        try:
            await (harness.recover() if kick else harness.recover(kick=False))
        except BaseException:
            await harness.shutdown()
            raise
        state.update(db=db, content=content, gate=gate, harness=harness)
        state.pop("damaged", None)
        state.pop("damaged_code", None)

    @contextlib.asynccontextmanager
    async def lifespan(app):
        state.update(data_dir=data_dir, start=start, helper_config=helper)
        # Finishing a restore a crash interrupted, opening the database (its checks, the backup
        # before a migration, migrations) and the daily backup are local maintenance: the desktop
        # entry's start deadline does not count them (see maintenance), since on a large folder
        # they are progress, not a hang. Never reset: when the database is damaged, or a restore
        # can be neither finished nor undone, the app serves health, the backups and restore only
        # (backups.Gate) until a restore puts a backup in place; the backups router closes what
        # that opens.
        db = await backups.open_at_launch(state, maintenance)
        if db is None:
            try:
                yield
            finally:
                state.clear()
            return
        try:
            await asyncio.to_thread(_sweep_deleted_project_folders, data_dir, db)
            # The daily backup runs at launch, before the app accepts a request, so the
            # database and the settings files it copies show one state. Backups while the app
            # is open and idle are S1-12's.
            with maintenance():
                await asyncio.to_thread(_daily_backup, db)
            # Background runs start only now, once that backup's full check has passed: on a
            # database it found damaged (the app is then limited) nothing runs on its own.
            if db.damaged is None:
                await backups.start_background(state)
            yield
        finally:  # after a restore, the ones it opened (the backups router closes them too; both are idempotent)
            if state.get("harness") is not None:
                await state["harness"].shutdown()
            await asyncio.to_thread(db.close)
            state.clear()

    app = FastAPI(lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)
    app.state.scholia = state  # the desktop entry reaches the harness through it at shutdown
    app.include_router(backups.router)  # backups, restore and project export, ahead of the catch-all routes
    app.include_router(materials.router)  # materials, passages, page images and retries (S1-13)
    app.include_router(asks.router)  # the shared confirmation (S1-13)
    app.include_router(local_helper.router)  # the local model helper and its models, under Advanced
    app.add_middleware(backups.Gate, state=state)  # restore only when damaged; no change during a restore

    @app.exception_handler(ApiError)
    @app.exception_handler(AdmissionError)
    async def known_error(request, error):
        return _error(error.status, error.code, error.message)

    @app.exception_handler(DatabaseClosedError)
    async def closed(request, error):  # while the app closes, or a restore swaps the database
        return _error(503, "database_unavailable", "The database is not open; try again")

    @app.exception_handler(RequestValidationError)
    async def invalid(request, error):
        return _error(400, "invalid_request", "The request is not valid")

    @app.exception_handler(StarletteHTTPException)
    async def http_error(request, error):
        codes = {404: "not_found", 405: "method_not_allowed"}
        return _error(error.status_code, codes.get(error.status_code, "http_error"), "The request was not handled")

    def unavailable():
        if "damaged" in state:  # damaged at startup and not restored yet
            return ApiError(503, "database_damaged", "The database failed its check; restore a backup")
        return ApiError(503, "database_unavailable", "The database is not open; try again")  # closing

    def db() -> Database:
        if "db" not in state:
            raise unavailable()
        return state["db"]

    def harness() -> Harness:
        if "harness" not in state:
            raise unavailable()
        return state["harness"]

    async def read(fn):
        return await asyncio.to_thread(db().read, fn)

    async def write(fn):
        return await asyncio.to_thread(db().write, fn)

    # One-time confirmation tokens for changes that need the researcher's confirmation: what each
    # confirms, and until when. ponytail: in memory, so a restart asks again.
    confirmations = {}

    def confirmation_needed(*what):
        """409 confirmation_required, with a token that confirms exactly `what` once."""
        now = time.monotonic()
        for token in [t for t, (_, until) in confirmations.items() if until < now]:
            del confirmations[token]
        token = secrets.token_urlsafe(18)
        confirmations[token] = (what, now + CONFIRM_SECONDS)
        return JSONResponse({"code": "confirmation_required", "message": "Confirm this change", "token": token},
                            status_code=409)

    def confirmed(token, *what):
        entry = confirmations.pop(token, None) if token else None
        return entry is not None and entry[0] == what and entry[1] >= time.monotonic()

    # Health and setup

    @app.get("/api/health")
    async def health():
        why = backups.limited(state)
        return {"ok": True, "version": APP_VERSION, "data_folder": str(data_dir),
                **({"database_damaged": why[1]} if why else {})}

    async def provider_list():
        configured = providers.configured(data_dir, include_off=True)
        on = providers.configured(data_dir)
        keys = await asyncio.gather(*(asyncio.to_thread(credentials.load_key, data_dir, name, keyring_backend)
                                      for name in configured))
        # A server on this Mac and when it was declared; an OpenRouter key's data-settings confirmation.
        governed = await read(lambda conn: [
            (governance.declaration(conn, p.base_url),
             governance.confirmation(conn, data_dir, p.name, key) if key is not None and p.is_openrouter else None)
            for p, key in zip(configured.values(), keys)])
        return [{"name": p.name, "kind": p.kind, "base_url": p.base_url, "has_key": key is not None,
                 "enabled": p.name in on, "local": local_origin(p.base_url) is not None,
                 "origin": local_origin(p.base_url), "declared_at": declared, "key_confirmation": confirmation}
                for p, key, (declared, confirmation) in zip(configured.values(), keys, governed)]

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
        """_store_key, ordered with every project's dispatch: a key change ends the provider's
        confirmations, which its requests may be using."""
        return await state["gate"].ordered(None, _store_key(provider, key))

    async def _store_key(provider, key):
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
            def changed(conn):  # which provider's key changed and where it went; never the key
                conn.execute("INSERT INTO audit_log (event, data) VALUES ('key_changed', ?)",
                             (json.dumps({"provider": provider, "stored_in": stored_in}),))
                # A changed key is asked about again (section 6.4), even when an earlier key returns.
                conn.execute("DELETE FROM key_attestations WHERE provider = ?", (provider,))
            await write(changed)
        return "credential_store_unavailable" if warning else None

    @app.get("/api/providers")
    async def provider_index():
        return {"providers": await provider_list()}

    @app.put("/api/keys/{provider:name}")
    async def put_key(provider: str, body: Key):
        async with harness().settings_lock:  # ordered with the provider snapshot of turn admission
            if provider not in providers.configured(data_dir, include_off=True):  # as the key is saved against it
                raise ApiError(404, "unknown_provider", "That provider is not set up")
            refuse_if_busy({provider})
            return {"ok": True, "warning": await _to_end(_save_key(provider, body.key))}

    @app.get("/api/providers/{provider:name}/models")
    async def provider_models(provider: str, refresh: bool = False, project_id: str | None = None):
        """A provider's models. With project_id, each says whether that project allows it (the
        model selector shows only those), and why not."""
        policy = None
        if project_id is not None:
            policy = await read(lambda conn: governance.policy(conn, project_id))
            if policy is None:
                raise ApiError(404, "not_found", "No such project")
        async with harness().settings_lock:  # a snapshot of the provider and its key, and its generation
            configured = providers.configured(data_dir)
            if provider not in configured:
                raise ApiError(404, "unknown_provider", "That provider is not set up")
            key = await asyncio.to_thread(credentials.load_key, data_dir, provider, keyring_backend)
            generation = openrouter_client.generation()
        # A snapshot older than a provider change caches nothing (its refresh is refused).
        models = await harness().catalog(configured[provider], key, force=refresh, generation=generation)
        status = openrouter_client.catalog_status(configured[provider], key)
        if openrouter_client.generation() != generation:  # after everything this answer reports
            raise ApiError(409, "settings_changed", "The provider changed while its models were listed")
        table = (load_settings(data_dir).values.get("providers") or {}).get(provider) or {}
        records = reasoning_capability.load_capabilities()
        return {"models": [describe_model(configured[provider], table, m, records, policy, key)
                           for m in sorted((models or {}).values(), key=lambda m: m["id"])], "status": status}

    def describe_model(provider, table, model, records, policy=None, key=None):
        """A catalog row with what the settings and the picker need: its window as reported
        and in use, whether it is offered and recommended, and its effort steps; with a
        project's policy, whether the project allows it."""
        capability = reasoning_capability.get_capability(records, model["id"], model)
        surface = capability.get("control_surface") or "unknown"
        steps = (capability.get("levels") or []) if surface == "levels" else \
            list(budget_router.EFFORT_LEVELS) if surface == "budget" else []
        refusal = policy.problem(provider, model["id"], key) if policy is not None else None
        return {**model, "window": providers.window(table, model["id"], model.get("context_length")),
                "offered": providers.offered(table, provider, model["id"], budget_router.RECOMMENDED),
                "recommended": provider.is_openrouter and model["id"] in budget_router.RECOMMENDED,
                "effort": {"surface": surface, "steps": [s for s in steps if s in budget_router.EFFORT_LEVELS]},
                **({"allowed": refusal is None, "refusal": refusal} if policy is not None else {})}

    @app.get("/api/models/recent")
    async def recent_models():
        """The last three models the researcher chose (not Auto's picks), newest first."""
        rows = await read(lambda conn: conn.execute(
            "SELECT json_extract(e.data, '$.route') AS route, json_extract(e.data, '$.plan.model')"
            " FROM run_events e JOIN runs r ON r.id = e.run_id"
            " WHERE e.type = 'route' AND json_extract(e.data, '$.plan.policy_reason') = 'chosen_model'"
            " GROUP BY 1, 2 ORDER BY max(r.started_at) DESC LIMIT 3").fetchall())

        def split(route, model):  # "<provider>:<model>": the plan records the model, so the rest is the provider
            if not (isinstance(route, str) and isinstance(model, str) and route.endswith(":" + model)):
                return None
            return {"provider": route[:-len(model) - 1], "model": model}

        return {"models": [found for route, model in rows if (found := split(route, model))]}

    # Settings and instructions

    async def settings_for(project_id):
        if project_id is not None:
            await project_row(project_id)
        return await asyncio.to_thread(load_settings, data_dir, project_id)

    @app.get("/api/settings")
    async def get_settings(project_id: str | None = None):
        async with project_files if project_id is not None else contextlib.nullcontext():  # ordered with deletion
            loaded = await settings_for(project_id)
        return {"values": loaded.values, "warnings": loaded.warnings, "problems": loaded.problems,
                "hash": loaded._digest}

    @app.put("/api/settings")
    async def put_settings(body: SettingsUpdate):
        async with project_files if body.project_id is not None else contextlib.nullcontext():
            async with harness().settings_lock:  # ordered with the budget reads of call admission
                return await save_settings(body)

    async def save_settings(body):
        loaded = await settings_for(body.project_id)  # for a project: checked to exist under the lock
        if body.hash != loaded._digest:
            raise ApiError(409, "settings_changed", "The settings changed since they were read")
        changed = _providers_changed(body.updates, providers.configured(data_dir, include_off=True))
        refuse_if_busy(changed)  # before any field is written
        try:
            saving = _finished(loaded.save, body.updates)  # under the project-files lock to its end
            # A provider turned off, moved or removed takes routes away: ordered with dispatch.
            await (state["gate"].ordered(None, saving) if changed else saving)
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
        return {"values": loaded.values, "warnings": loaded.warnings, "problems": loaded.problems,
                "hash": loaded._digest}

    @app.get("/api/instructions")
    async def get_instructions(project_id: str | None = None, with_project: str | None = None):
        """An AGENTS.md file to edit: the personal one, or project_id's. combined_bytes measures it
        with the instructions it joins: for the personal file, with_project's, if given."""
        async with project_files:  # ordered with deletion and with saves
            path = await instructions_path(project_id)
            joined = project_id or with_project
            if with_project is not None:
                await project_row(with_project)
            try:
                raw, unreadable = await asyncio.to_thread(lambda: path.read_bytes() if path.is_file() else b""), False
            except OSError:  # it exists but cannot be read: the editor says so and does not save over it
                raw, unreadable = b"", True
            _, warnings = await asyncio.to_thread(load_instructions, data_dir, joined)
            combined = await asyncio.to_thread(instructions_size, data_dir, joined)
            # The other file the cap counts with this one: the personal file for a project's, the
            # project's for the personal file.
            other = data_dir / "AGENTS.md" if project_id else \
                data_dir / "projects" / with_project / "AGENTS.md" if with_project else None
            other_bytes = await asyncio.to_thread(instruction_file_size, other) if other else 0
        text = raw.decode("utf-8", errors="replace")
        return {"text": text, "hash": hashlib.sha256(raw).hexdigest(), "combined_bytes": combined,
                "other_bytes": other_bytes, "unreadable": unreadable,
                "replaced": text.encode("utf-8") != raw,  # not UTF-8: saving it back replaces those bytes
                "warnings": warnings, "cap_bytes": INSTRUCTIONS_CAP}

    @app.put("/api/instructions")
    async def put_instructions(body: Instructions):
        async with project_files:  # the hash check and the write, one save at a time
            path = await instructions_path(body.project_id)  # the project still exists, under the lock
            if body.hash is not None:  # a file changed since the editor read it is not overwritten
                raw = await asyncio.to_thread(lambda: path.read_bytes() if path.is_file() else b"")
                if hashlib.sha256(raw).hexdigest() != body.hash:
                    raise ApiError(409, "settings_changed", "The instructions changed since they were read")
            data = body.text.encode("utf-8")
            await _finished(write_private, path, data)
        _, warnings = await asyncio.to_thread(load_instructions, data_dir, body.project_id)
        return {"ok": True, "warnings": warnings, "hash": hashlib.sha256(data).hexdigest()}

    async def instructions_path(project_id):
        if project_id is None:
            return data_dir / "AGENTS.md"
        await project_row(project_id)
        return data_dir / "projects" / project_id / "AGENTS.md"

    # Projects

    def project_dict(row):
        keys = ("id", "name", "kind", "sensitivity", "review_lock", "target_venue", "created_at", "updated_at",
                "review_venue")
        project = dict(zip(keys, row))
        project["review_lock"] = bool(project["review_lock"])
        return project

    _PROJECT_COLUMNS = "id, name, kind, sensitivity, review_lock, target_venue, created_at, updated_at, review_venue"

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
            conn.execute(
                "INSERT INTO projects (id, name, kind, sensitivity, review_lock, review_venue)"
                " VALUES (?, ?, 'research', ?, ?, ?)",
                (project_id, body.name, body.sensitivity, int(body.review_lock), visible(body.review_venue)))
            governance.record(conn, "project_created", project_id, sensitivity=body.sensitivity,
                              review_lock=body.review_lock)

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
    async def delete_project(project_id: str, purge_backups: bool = False, remove_all_trace: bool = False):
        """purge_backups: "Delete everywhere including backups"; remove_all_trace drops the titles from
        the tombstones. Otherwise an automatic backup is taken first, as before every bulk deletion."""
        row = await project_row(project_id)
        if row[2] == "general":
            raise ApiError(400, "general_project", "The General project cannot be deleted")
        if not purge_backups:  # outside the project-files lock, which it would hold for its whole copy
            try:
                async with state["backups_lock"]:  # as every backup: never under a restore staging one
                    await _finished(backups.backup_before_deletion, db())
            except Exception as error:
                log.warning("the backup before a project deletion failed (%s)", type(error).__name__)
                raise ApiError(503, "backup_failed", "The backup before the deletion failed; nothing was deleted")

        async def deleting():  # the record, the stopping of its runs, its folder and the purge, to their end
            async with project_files:
                revoked = await asyncio.to_thread(delete, db(), state["content"], "project", project_id,
                                                  remove_all_trace=remove_all_trace, on_committed=revoke_soon())
                harness().revoke(revoked)
                removed = await asyncio.to_thread(_remove_folder, data_dir / "projects" / project_id)
            result = {"ok": True}
            if not removed:  # the record is gone; its tombstone makes the next launch retry the files
                log.warning("a deleted project's folder could not be removed fully; it is retried at the next launch")
                result["files_left"] = True
            if purge_backups:  # after the folder is gone, so the fresh backup holds none of it
                result.update(await purge(project_id, {"kind": "project", "object_id": project_id}))
            return result

        try:
            return await _to_end(deleting())  # a cancelled request still purges: its object is gone for good
        except LookupError:  # deleted meanwhile by another request
            raise ApiError(404, "not_found", "No such project") from None

    async def purge(project_id, deleted):
        """Take deleted data out of the automatic backups; a failure leaves the deletion as it is. It
        waits for a restore, full backup or export in progress, so none of them meets it halfway."""
        try:
            async with state["backups_lock"]:
                return await _finished(backups.purge, db(), project_id, deleted)
        except Exception as error:
            log.warning("purging the backups after a deletion failed (%s)", type(error).__name__)
            return {"purge_failed": True}

    async def revoking(project_id, change, stricter=False):
        """Write a change that returns the project's runs it revoked, marked with the outbound gate
        so that no request of the project enters the transport after it commits, and stop them,
        both to their end. A change that makes the project stricter first waits, under that mark,
        until no full backup or export written without a passphrase can write any more of it
        (backups.Archives)."""
        async def written():
            async with state["archives"].stricter(project_id) if stricter else contextlib.nullcontext():
                return await write(change)

        async def change_and_stop():
            harness().revoke(await state["gate"].ordered(project_id, written()))
        await _to_end(change_and_stop())

    def unchanged(conn, project_id, level, locked):
        """Refuse a change made on a project whose level or lock changed since they were read."""
        now = conn.execute("SELECT sensitivity, review_lock FROM projects WHERE id = ?", (project_id,)).fetchone()
        if now is None:
            raise ApiError(404, "not_found", "No such project")
        if (now[0], bool(now[1])) != (level, locked):
            raise ApiError(409, "project_changed", "The project's protection changed meanwhile")

    @app.post("/api/projects/{project_id}/sensitivity")
    async def set_sensitivity(project_id: str, body: SensitivityChange):
        """Change a project's level. A stricter level applies at once and revokes the project's
        running work, in the same transaction; a less strict one needs the researcher's
        confirmation: the first request answers 409 confirmation_required with a token, which
        the second sends. Both are audited. The General project stays Normal, and a
        review-locked project stays Local only until its lock is lifted."""
        row = await project_row(project_id)
        if row[2] == "general":
            raise ApiError(400, "general_project", "The General project stays Normal")
        level, locked = row[3], bool(row[4])
        if body.level == level:
            return project_dict(row)
        looser = _STRICTNESS[body.level] < _STRICTNESS[level]
        if looser and locked:
            raise ApiError(409, "review_locked", "Lift the review lock first")
        if looser and not confirmed(body.token, "sensitivity", project_id, level, body.level):
            return confirmation_needed("sensitivity", project_id, level, body.level)

        def change(conn):
            unchanged(conn, project_id, level, locked)
            conn.execute("UPDATE projects SET sensitivity = ?, updated_at = ? WHERE id = ?",
                         (body.level, utc_now(), project_id))
            revoked = [] if looser else governance.revoke_running(conn, project_id)
            governance.record(conn, "sensitivity_changed", project_id, **{"from": level, "to": body.level},
                              revoked_runs=len(revoked))
            return revoked

        await revoking(project_id, change, stricter=not looser)
        return project_dict(await project_row(project_id))

    @app.post("/api/projects/{project_id}/review-lock")
    async def set_review_lock(project_id: str, body: ReviewLock):
        """Lock a project that holds someone else's submission (the review-lock preset): Local
        only, and every generative function refused, which in M1 holds whatever the venue,
        until the venue rules relax it. Locking applies at once and revokes the project's running
        work; an already locked project only takes the venue. Lifting the lock needs confirmation
        (as a less strict level does) and leaves the project Local only. Both are audited; the
        venue, which the researcher typed, is not written to the audit log."""
        row = await project_row(project_id)
        if row[2] == "general":
            raise ApiError(400, "general_project", "The General project stays Normal")
        level, locked = row[3], bool(row[4])
        if not body.locked:
            if not locked:
                return project_dict(row)
            if not confirmed(body.token, "review_lock", project_id):
                return confirmation_needed("review_lock", project_id)
        venue = visible(body.venue) if body.locked else None

        def change(conn):
            unchanged(conn, project_id, level, locked)
            conn.execute("UPDATE projects SET sensitivity = ?, review_lock = ?, review_venue = ?, updated_at = ?"
                         " WHERE id = ?", ("local_only", int(body.locked), venue, utc_now(), project_id))
            revoked = governance.revoke_running(conn, project_id) if body.locked and not locked else []
            governance.record(conn, "review_lock_changed", project_id, locked=body.locked, **{"from": level},
                              venue_set=venue is not None, revoked_runs=len(revoked))
            return revoked

        await revoking(project_id, change, stricter=body.locked and not locked)
        return project_dict(await project_row(project_id))

    # Governance: the key confirmation, declared local servers, the Private allowlist, the audit log

    @app.post("/api/key-attestations")
    async def confirm_key(body: KeyConfirmation):
        """The researcher's confirmation that OpenRouter's data settings for the provider's key are
        as the statement says (section 6.4). Scholia cannot verify them: it records the
        confirmation with a fingerprint of the key, never the key, for six months. It names the
        key the card showed (its reference), so a key changed since then is refused."""
        async with harness().settings_lock:  # ordered with key changes, which hold it too
            provider = providers.configured(data_dir, include_off=True).get(body.provider)
            if provider is None:
                raise ApiError(404, "unknown_provider", "That provider is not set up")
            if not provider.is_openrouter:
                raise ApiError(400, "not_openrouter", "Only an OpenRouter key is confirmed")
            if body.statement != governance.KEY_STATEMENT:
                raise ApiError(409, "statement_changed", "The statement changed; read it again")
            key = await asyncio.to_thread(credentials.load_key, data_dir, provider.name, keyring_backend)
            if key is None:
                raise ApiError(400, "provider_key_missing", "The provider has no key")
            shown = governance.key_reference(await asyncio.to_thread(governance.fingerprint, data_dir, key))
            if not secrets.compare_digest(shown, body.key):
                raise ApiError(409, "key_changed", "The key changed since it was shown; read it again")
            await _to_end(write(lambda conn: governance.confirm_key(conn, data_dir, provider.name, key)))
        return {"ok": True}

    @app.post("/api/local-declarations")
    async def declare_local(body: Declaration):
        """The researcher's declaration that a provider on this Mac runs its models here, for the
        exact origin the card showed (one that changed since is refused). Scholia cannot verify
        it; for a Private project it is the researcher's assurance, not a retention guarantee.
        Audited."""
        async with harness().settings_lock:  # the provider's address as the settings hold it now
            provider = providers.configured(data_dir, include_off=True).get(body.provider)
            if provider is None:
                raise ApiError(404, "unknown_provider", "That provider is not set up")
            if local_origin(provider.base_url) is None:
                raise ApiError(400, "not_local", "Only a server on this Mac can be declared")
            if local_origin(provider.base_url) != body.origin:
                raise ApiError(409, "target_changed", "The server's address changed since it was shown")
            await _to_end(write(lambda conn: governance.declare(conn, provider.name, provider.base_url)))
        return {"ok": True}

    @app.delete("/api/local-declarations/{provider:name}")
    async def withdraw_local(provider: str):
        async with harness().settings_lock:
            found = providers.configured(data_dir, include_off=True).get(provider)
            if found is None:
                raise ApiError(404, "unknown_provider", "That provider is not set up")
            # Every project's requests that could use the server are ordered with the withdrawal.
            withdrawn = await _to_end(state["gate"].ordered(
                None, write(lambda conn: governance.withdraw(conn, found.name, found.base_url))))
            if not withdrawn:
                raise ApiError(404, "not_found", "That server is not declared")
        return {"ok": True}

    @app.get("/api/private-routes")
    async def private_routes():
        return {"routes": await read(governance.allowlist), "checked_on": governance.shipped()["checked_on"]}

    @app.put("/api/private-routes/{key:name}")
    async def change_private_route(key: str, body: PrivateRouteChange):
        """Turn an allowlist entry off or on, add an OpenRouter route ("openrouter:<model id>",
        always with provider.zdr = true), or record that its terms were checked again today.
        Audited."""
        model = key.removeprefix("openrouter:")
        if model == key or not 0 < len(model) <= 200 or any(not "\x21" <= c <= "\x7e" for c in model):  # a model id
            raise ApiError(400, "not_openrouter_route", "Only an OpenRouter route can be added")

        def change(conn):
            shipped = {e["route_key"]: e for e in governance.shipped()["entries"]}
            known = {e["route_key"] for e in governance.allowlist(conn)}
            if key not in known and body.enabled is None:
                raise ApiError(404, "not_found", "No such route")
            now = utc_now()
            if body.enabled is not None:
                base = shipped.get(key) or shipped["openrouter:*"]  # an added route keeps OpenRouter's terms
                conn.execute(
                    "INSERT INTO private_routes (route_key, source, required_flags, allowed_features, terms_url,"
                    " checked_on, exceptions, enabled) VALUES (?, ?, ?, '[]', ?, ?, ?, ?)"
                    " ON CONFLICT (route_key) DO UPDATE SET enabled = excluded.enabled",
                    (key, "shipped" if key in shipped else "researcher", json.dumps(base["required_flags"]),
                     base["terms_url"], now, json.dumps(base["exceptions"]), int(body.enabled)))
            if body.rechecked:
                conn.execute("INSERT OR REPLACE INTO list_checks (list, entry_id, checked_on)"
                             " VALUES ('private_routes', ?, ?)", (key, now))
            governance.record(conn, "private_route_changed", route=key, enabled=body.enabled,
                              rechecked=body.rechecked)

        await _to_end(state["gate"].ordered(None, write(change)))  # every Private project's requests ordered with it
        return await private_routes()

    @app.get("/api/audit")
    async def audit_log(project_id: str | None = None, before: int | None = None, limit: int = 100):
        """The audit log, newest first, a page at a time (before: the seq the last page ended
        at); for one project, its own rows and every clearing of the log, with how many of its
        requests the gate allowed off this Mac since the log began or was last cleared (a row
        records the decision, not delivery: a request decided again and refused before it left counts)."""
        limit = max(1, min(limit, 500))

        def page(conn):
            rows = conn.execute(
                "SELECT seq, at, event, project_id, data FROM audit_log"
                " WHERE (?1 IS NULL OR project_id = ?1 OR event = 'audit_cleared')"
                " AND (?2 IS NULL OR seq < ?2) ORDER BY seq DESC LIMIT ?3", (project_id, before, limit)).fetchall()
            sent = conn.execute(
                "SELECT count(*) FROM audit_log WHERE project_id = ? AND event = 'outbound'"
                " AND data ->> 'decision' = 'allow' AND data ->> 'kind' NOT IN ('local_helper', 'local_provider')",
                (project_id,)).fetchone()[0] if project_id else None
            return rows, sent

        rows, sent = await read(page)
        return {"entries": [{"seq": seq, "at": at, "event": event, "project_id": project, "data": json.loads(data)}
                            for seq, at, event, project, data in rows],
                "next": rows[-1][0] if len(rows) == limit else None, "allowed_off_this_mac": sent}

    @app.post("/api/audit/export")
    async def export_audit(body: AuditExport | None = None):
        """Write the audit log (or one project's rows, with every clearing of the log) as a JSON
        file in the data folder's exports folder, owner-only. The export is recorded with its
        destination first, so no file is there unrecorded; a failure after that removes the file
        and leaves the record of the attempt."""
        project_id = (body or AuditExport()).project_id
        if project_id is not None:
            await project_row(project_id)  # an existing project's id, never other text, goes in the record
        name = f"audit-log-{time.strftime('%Y%m%dT%H%M%SZ', time.gmtime())}-{secrets.token_hex(3)}.json"

        async def exporting():
            rows = await read(lambda conn: conn.execute(
                "SELECT seq, at, event, project_id, data FROM audit_log"
                " WHERE ?1 IS NULL OR project_id = ?1 OR event = 'audit_cleared' ORDER BY seq", (project_id,)).fetchall())
            entries = [{"seq": seq, "at": at, "event": event, "project_id": project, "data": json.loads(data)}
                       for seq, at, event, project, data in rows]
            await write(lambda conn: governance.record(conn, "audit_exported", project_id,
                                                       destination=f"exports/{name}", rows=len(entries)))
            path = data_dir / "exports" / name
            try:
                await asyncio.to_thread(write_private, path, json.dumps(
                    {"format": "scholia-audit-log", "version": 1, "entries": entries}, ensure_ascii=False,
                    indent=1).encode())
            except OSError:
                await asyncio.to_thread(path.unlink, missing_ok=True)
                raise ApiError(500, "export_failed", "The audit log could not be written") from None
            return len(entries)

        rows = await _to_end(exporting())
        return {"path": str(data_dir / "exports" / name), "rows": rows}

    @app.delete("/api/audit")
    async def clear_audit(token: str | None = None):
        """Clear the audit log, after the researcher confirms (a first request answers 409
        confirmation_required with a token). A record of the clearing stays, and so do the
        records of purged backups, which a restore compares (backups._purges)."""
        if not confirmed(token, "audit_clear"):
            return confirmation_needed("audit_clear")

        def clear(conn):
            (rows,) = conn.execute("SELECT count(*) FROM audit_log").fetchone()
            governance.record(conn, "audit_cleared", rows=rows)
            conn.execute("DELETE FROM audit_log WHERE seq < (SELECT max(seq) FROM audit_log)"
                         " AND event NOT IN ('audit_cleared', 'backup_purge')")  # these stay on record
            return rows

        return {"ok": True, "rows": await _to_end(write(clear))}

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

        def move(conn, expected):
            row = conn.execute(
                "SELECT c.project_id, p.sensitivity, p.review_lock FROM conversations c"
                " JOIN projects p ON p.id = c.project_id WHERE c.id = ?", (conversation_id,)).fetchone()
            target = conn.execute("SELECT sensitivity, review_lock FROM projects WHERE id = ?",
                                  (body.project_id,)).fetchone()
            if row is None or target is None:
                raise ApiError(404, "not_found", "No such conversation or project")
            source, level, locked = row
            if source == body.project_id:
                return True
            if source != expected:  # moved meanwhile: its new project's archives are stopped first
                return False
            # Its turn, or a title run that would send its words over the old project's route;
            # ordered with admission by the single writer.
            if conn.execute("SELECT 1 FROM runs WHERE status = 'running' AND (conversation_id = ?"
                            " OR source_turn_id IN (SELECT run_id FROM turns WHERE conversation_id = ?))",
                            (conversation_id, conversation_id)).fetchone():
                raise ApiError(409, "active_run", "This conversation is running a turn")
            # A review-locked project counts as stricter than any unlocked one.
            if (_STRICTNESS[target[0]], target[1]) < (_STRICTNESS[level], locked):
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
            return True

        async def moved_from(source):
            # Its words leave the project: no full backup or export of it written without a
            # passphrase writes any more of them (backups.Archives), wherever they go next; held
            # until the move has ended, whatever happens to the request (see _to_end).
            async with state["archives"].stricter(source):
                return await write(lambda conn: move(conn, source))

        while True:
            row = await read(lambda conn: conn.execute(
                "SELECT project_id FROM conversations WHERE id = ?", (conversation_id,)).fetchone())
            if row is None:
                raise ApiError(404, "not_found", "No such conversation or project")
            if row[0] == body.project_id:
                break
            if await _to_end(moved_from(row[0])):
                break
        return await get_conversation(conversation_id)

    @app.delete("/api/conversations/{conversation_id}")
    async def delete_conversation(conversation_id: str, purge_backups: bool = False, remove_all_trace: bool = False):
        project = await read(lambda conn: conn.execute(
            "SELECT project_id FROM conversations WHERE id = ?", (conversation_id,)).fetchone())

        async def deleting():  # the deletion, the stopping of its runs and the purge, together, to their end
            revoked = await asyncio.to_thread(delete, db(), state["content"], "conversation", conversation_id,
                                              remove_all_trace=remove_all_trace, on_committed=revoke_soon())
            harness().revoke(revoked)
            if purge_backups:
                return {"ok": True, **await purge(project[0], {"kind": "conversation", "object_id": conversation_id})}
            return {"ok": True}

        try:
            return await _to_end(deleting())  # a cancelled request still purges: its object is gone for good
        except LookupError:
            raise ApiError(404, "not_found", "No such conversation") from None

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
    async def activity(limit: int = 50, run_id: str | None = None):
        """Background runs: every running one, then the newest others up to the limit (or one run).
        Each with its live progress, its open ask, its result or reason, and whether Retry applies."""
        def listing(conn):
            rows = conn.execute(
                "SELECT r.id, r.project_id, r.workflow, r.status, r.cancel_reason, r.settled_cost_usd, r.attempts,"
                " r.started_at, r.finished_at, p.name, p.kind, r.summary, r.inputs FROM runs r"
                " JOIN projects p ON p.id = r.project_id WHERE r.kind = 'background' AND (?2 IS NULL OR r.id = ?2)"
                # Every running run, where it is cancelled, then the newest others up to the limit.
                " AND (r.status = 'running' OR ?2 IS NOT NULL OR r.id IN (SELECT id FROM runs"
                " WHERE kind = 'background' AND status != 'running' ORDER BY started_at DESC LIMIT ?1))"
                " ORDER BY r.status = 'running' DESC, r.started_at DESC", (max(1, min(limit, 200)), run_id)).fetchall()
            return [(row, materials.run_details(conn, row[0], row[2], row[3], row[12], registry)) for row in rows]

        registry = harness().registry
        listed = await read(listing)
        return JSONResponse({"runs": [{
            "run_id": run, "project_id": project_id, "project_name": name, "project_kind": kind,
            "workflow": workflow, "status": derived_status(status, run, registry), "cancel_reason": cancel,
            "cost_usd": cost, "attempts": attempts, "started_at": started, "finished_at": finished,
            "result": json.loads(summary) if summary else None,
            "progress": registry.runs[run].progress if run in registry.runs else None, **details,
        } for (run, project_id, workflow, status, cancel, cost, attempts, started, finished, name, kind, summary, _),
            details in listed]}, headers={"Cache-Control": "no-store"})  # it names papers: never kept by the browser

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
