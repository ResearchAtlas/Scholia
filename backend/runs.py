"""Runs: turn admission, the turn's commit boundaries, cancel, recovery and detached work.

A researcher's message starts one turn, a run of kind `turn`. Its life is a few
separate transactions on the single writer (each BEGIN IMMEDIATE, full sync):

1. Admission, before any paid work: the `turns` row with the message and the
   `runs` row with status running, together. A request that fails validation, or
   finds its conversation busy (`409 active_run`), writes nothing.
2. For each model call: a budget reservation with a `step_started` event, then,
   after the call, every HTTP attempt as a `model_attempt` event, the
   `step_finished` event and the reservation's settlement, together.
3. The primary commit: the answer, the turn's succeeded status and the run's
   settled cost together, with the cancel flag checked inside the transaction.
   The answer is streamed only after it. The same transaction writes the
   detached post-answer work (a title for an untitled conversation) as a
   background run keyed to the turn.

A run's status never returns to running. Cancellation (Stop, a closed stream,
deletion or shutdown) before the primary commit ends the run cancelled (or
interrupted at shutdown), settling a call that may have gone out at its estimate
and releasing a reservation whose call never left. A running turn that is not
in this process's registry reads as interrupted, and startup records it so.

Background runs (titles) run outside any turn, so the conversation accepts its
next message at once. Each model call they start is counted in `attempts`, in its
own transaction, before the call. On every start, including after a crash, a
background run is finished from a recorded finished step with no model call, or
restarted while it has made fewer than 2 attempts, or else marked interrupted.
Its effects, terminal status and settled cost are written in one transaction.
"""

import asyncio
import json
import logging
import threading
import time
import traceback
from contextlib import suppress
from dataclasses import dataclass, field

from backend import budget_router, credentials, openrouter, providers, spending
from backend.db import new_id, utc_now
from backend.openrouter_client import get_model_metadata
from backend.settings import load_instructions, load_settings, visible

log = logging.getLogger(__name__)

STALE_CLAIM_SECONDS = 30  # an admitted turn whose response has not started by then was dropped
MODEL_CALL_SECONDS = 120  # one call's total bound, both attempts included
MAX_MESSAGE_CHARS = 100_000
HISTORY_TURNS = 20  # ponytail: a fixed history window until the context assembler counts tokens
BACKGROUND_ATTEMPTS = 2
CANCEL_WAIT_SECONDS = 10  # how long a cancel request waits to report how the run ended
SYSTEM_RULES = (
    "You are Scholia, a research assistant working inside the researcher's own project. "
    "Answer in the language of the researcher's message unless they ask otherwise. "
    "Say plainly when you do not know something."
)
TITLE_RULES = (
    "Write a title of at most six words for a conversation that begins with the message below. "
    "Use the language of the message. Reply with the title only, without quotation marks."
)


class AdmissionError(Exception):
    """A request that cannot start. Nothing was written. code is stable for the interface."""

    def __init__(self, status: int, code: str, message: str):
        super().__init__(message)
        self.status, self.code, self.message = status, code, message


@dataclass
class ActiveRun:
    run_id: str
    kind: str  # "turn" or "background"
    conversation_id: str | None
    admitted: float | None = None  # when its admission finished; None while it is being admitted
    task: asyncio.Task | None = None
    started: bool = False  # its task has begun; one cancelled before that would never run its cleanup
    cancel_requested: threading.Event = field(default_factory=threading.Event)  # read inside transactions
    cancel_reason: str | None = None  # "researcher", "revoked" or "shutdown"
    events: asyncio.Queue = field(default_factory=asyncio.Queue)
    wanted: asyncio.Event = field(default_factory=asyncio.Event)  # the reader asked for the next event
    context: dict | None = None  # what an admitted turn needs to run
    call: "_Call | None" = None  # the model call admitted last, until it is settled or released
    provider: str | None = None  # the provider its model calls go to
    closing: asyncio.Future | None = None  # the write that records a claim stopped before it started


class Registry:
    """The runs this process is working on: at most one turn per conversation."""

    def __init__(self):
        self.runs: dict[str, ActiveRun] = {}
        self.turns: dict[str, ActiveRun] = {}  # by conversation
        self.closed = False
        self.dropped: list[str] = []  # stale claims released, whose runs are still to be recorded

    def claim_turn(self, conversation_id: str) -> ActiveRun:
        if self.closed:
            raise AdmissionError(503, "shutting_down", "The app is closing")
        held = self.turns.get(conversation_id)
        if held is not None:
            if held.task is None and held.admitted is not None and time.monotonic() - held.admitted > STALE_CLAIM_SECONDS:
                # Admitted and recorded, but its response was dropped before it started, so its turn never ran.
                # A claim still being admitted is never released: its admission may yet write its run.
                log.warning("released a stale claim on a conversation whose response never started")
                self.release(held)
                self.dropped.append(held.run_id)
            else:
                raise AdmissionError(409, "active_run", "This conversation is already running a turn")
        claim = ActiveRun(new_id(), "turn", conversation_id)
        self.turns[conversation_id] = claim
        self.runs[claim.run_id] = claim
        return claim

    def add_background(self, run_id: str) -> ActiveRun | None:
        if self.closed or run_id in self.runs:
            return None
        active = ActiveRun(run_id, "background", None)
        self.runs[run_id] = active
        return active

    def release(self, active: ActiveRun) -> None:
        if self.runs.get(active.run_id) is active:
            del self.runs[active.run_id]
        if active.conversation_id is not None and self.turns.get(active.conversation_id) is active:
            del self.turns[active.conversation_id]

    def is_active(self, run_id: str) -> bool:
        return run_id in self.runs


def derived_status(status: str, run_id: str, registry: Registry) -> str:
    """A run's status as it reads now: a running run this process does not hold is interrupted."""
    return "interrupted" if status == "running" and not registry.is_active(run_id) else status


def _event(conn, run_id, event_type, data):
    (seq,) = conn.execute("SELECT coalesce(max(seq) + 1, 0) FROM run_events WHERE run_id = ?", (run_id,)).fetchone()
    conn.execute("INSERT INTO run_events (run_id, seq, type, data) VALUES (?, ?, ?, ?)",
                 (run_id, seq, event_type, json.dumps(data)))


def _where(error: BaseException) -> str:
    """Where an unexpected error was raised, as file:line, without its message (which may hold content)."""
    frames = traceback.extract_tb(error.__traceback__)
    return f"{frames[-1].filename.rsplit('/', 1)[-1]}:{frames[-1].lineno}" if frames else "unknown"


class _Cancelled(Exception):
    """Raised inside a transaction to roll it back because cancellation was requested."""


def _running(conn, run_id) -> bool:
    row = conn.execute("SELECT status FROM runs WHERE id = ?", (run_id,)).fetchone()
    return row is not None and row[0] == "running"


@dataclass
class _Call:
    """One admitted model call: its run, step, reservation and whether it may have left."""
    run_id: str
    step: int
    reservation_id: str
    dispatched: bool = False
    route: str | None = None  # the route key, once the call is under way


class Harness:
    """Runs turns and background work against one data folder.

    db and content are the folder's Database and ContentStore; gate is its
    OutboundGate. keyring_backend selects the credential store (tests and
    walkthroughs inject their own; None uses the system's).
    """

    def __init__(self, data_dir, db, gate, *, keyring_backend=None):
        self.data_dir = data_dir
        self.db = db
        self.gate = gate
        self.keyring_backend = keyring_backend
        self.registry = Registry()
        self._tasks = set()  # detached tasks, kept referenced until they finish
        # Orders settings saves with the budget reads of call admission: a lowered budget either
        # lands before a call reads it or after that call was admitted.
        self.settings_lock = asyncio.Lock()

    async def _write(self, fn):
        return await asyncio.to_thread(self.db.write, fn)

    async def _write_through(self, fn):
        """Like _write, but a cancellation, however often it comes, waits for the write to
        finish and comes back as (result, True), so the caller can undo or settle what it
        wrote. An error from the write itself is raised."""
        return await _through(self._write(fn))

    def _detach(self, coroutine):
        task = asyncio.get_running_loop().create_task(coroutine)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        return task

    async def _read(self, fn):
        return await asyncio.to_thread(self.db.read, fn)

    # Startup and shutdown

    async def recover(self) -> None:
        """Record what a crash left behind, then restart the background runs. Call once
        at startup, before serving requests."""

        def record(conn):
            in_flight = {run_id for (run_id,) in conn.execute(
                "SELECT DISTINCT run_id FROM budget_reservations WHERE status = 'open' AND run_id IS NOT NULL")}
            spending.settle_left_open(conn)
            now = utc_now()
            for (run_id,) in conn.execute(
                    "SELECT id FROM runs WHERE status = 'running' AND kind IN ('turn', 'child')").fetchall():
                _event(conn, run_id, "run_finished", {"status": "interrupted"})
                conn.execute(
                    "UPDATE runs SET status = 'interrupted', finished_at = ?, settled_cost_usd = ? WHERE id = ?",
                    (now, spending.run_cost(conn, run_id), run_id))
                conn.execute(
                    "UPDATE turns SET reason_code = 'interrupted', memory_status = coalesce(memory_status, 'skipped'),"
                    " accounting = ? WHERE run_id = ?",
                    (json.dumps(_accounting(conn, run_id, complete=run_id not in in_flight)), run_id))

        await self._write(record)
        await self.kick_background()

    async def shutdown(self, timeout: float = 10.0) -> int:
        """Stop admitting, cancel what is running and wait for it, up to timeout seconds.
        Returns how many tasks were still running; the database refuses them once it
        closes (see Database.close)."""
        self.registry.closed = True
        tasks = []
        for active in list(self.registry.runs.values()):
            self._request_cancel(active, "shutdown")
            tasks += [t for t in (active.task, active.closing) if t is not None]
        # Detached work (starting background runs, recording stopped claims) reads and writes
        # the database too: it must finish before the database closes.
        tasks += [t for t in self._tasks if not t.done()]
        pending = ()
        if tasks:
            _, pending = await asyncio.wait(tasks, timeout=timeout)
            if pending:  # the database closes anyway: they get DatabaseClosedError, never a closed connection
                log.warning("%d tasks did not stop within %s s of shutdown; closing the database", len(pending), timeout)
        return len(pending)

    # Cancel

    def _request_cancel(self, active: ActiveRun, reason: str) -> None:
        """Cancel a run once; repeating it changes nothing, so a cancellation in progress
        is never interrupted. A claim whose response never started has run nothing: it is
        released and recorded at once."""
        if active.cancel_requested.is_set():
            return
        active.cancel_reason = reason
        active.cancel_requested.set()
        if active.task is not None:
            if active.started:
                active.task.cancel()
            # else it sees the request as it starts, and ends through its own cleanup
        elif self.registry.runs.get(active.run_id) is active:
            self.registry.release(active)
            if active.kind == "turn":
                status = "interrupted" if reason == "shutdown" else "cancelled"
                cancel_reason = None if status == "interrupted" else ("revoked" if reason == "revoked" else "researcher")
                active.closing = self._detach(self._write(
                    lambda conn: self._finish_turn(conn, active.run_id, status, cancel_reason, status)))

    def stream_closed(self, claim: ActiveRun) -> None:
        """A turn's response ended, however it ended: a turn still running, or one that
        never started, is cancelled as by Stop."""
        if self.registry.runs.get(claim.run_id) is claim:
            self._request_cancel(claim, "researcher")

    def provider_busy(self, name: str) -> bool:
        """Whether running work calls this provider, so its settings or key may not change now."""
        return any(active.provider == name for active in self.registry.runs.values())

    async def cancel(self, run_id: str, reason: str = "researcher") -> dict | None:
        """Request cancellation and report how the run ended, or None if no such run.

        A run decides at its commit whether the request came first: one whose result
        commits before it sees the request keeps that result, and the reply says so
        rather than promising a cancellation. The reply is "cancelling" only if the run
        has not ended within CANCEL_WAIT_SECONDS. Repeating it is safe; a finished run
        is left as it is."""
        await self._record_dropped()
        active = self.registry.runs.get(run_id)
        if active is not None:
            self._request_cancel(active, reason)
            if active.closing is not None:  # its response never started: recorded now
                return await active.closing
            if active.task is not None:
                await asyncio.wait({active.task}, timeout=CANCEL_WAIT_SECONDS)
                if not active.task.done():
                    return {"run_id": run_id, "status": "cancelling"}
        row = await self._read(lambda conn: conn.execute("SELECT status FROM runs WHERE id = ?", (run_id,)).fetchone())
        if row is None:
            return None
        return {"run_id": run_id, "status": derived_status(row[0], run_id, self.registry)}

    async def _record_dropped(self):
        """Record the turns of released stale claims, which never ran, as interrupted."""
        while self.registry.dropped:
            run_id = self.registry.dropped.pop()
            await self._write(lambda conn: self._finish_turn(conn, run_id, "interrupted", None, "interrupted"))

    def revoke(self, run_ids) -> None:
        """Stop the active runs a deletion revoked."""
        for run_id in run_ids:
            active = self.registry.runs.get(run_id)
            if active is not None:
                self._request_cancel(active, "revoked")

    # Turns

    async def continue_turn(self, run_id: str, *, model=None, provider=None, effort=None) -> ActiveRun:
        """Admit a new turn that continues an interrupted one, or one stopped at a limit or
        revoked by a change to its project, from the conversation as it is now. It is the
        conversation's latest turn; the new turn sends its message again and records
        which run it continues. Its recorded steps are not reused."""
        row = await self._read(lambda conn: conn.execute(
            "SELECT t.conversation_id, t.user_message, r.status, r.cancel_reason,"
            " t.seq = (SELECT max(seq) FROM turns WHERE conversation_id = t.conversation_id)"
            " FROM turns t JOIN runs r ON r.id = t.run_id WHERE t.run_id = ?", (run_id,)).fetchone())
        if row is None:
            raise AdmissionError(404, "not_found", "No such turn")
        conversation_id, message, status, cancel_reason, latest = row
        status = derived_status(status, run_id, self.registry)
        if not latest or not (status == "interrupted" or (status == "cancelled" and cancel_reason in ("limit", "revoked"))):
            raise AdmissionError(409, "not_continuable", "Only the latest interrupted or stopped turn can be continued")
        return await self.admit_turn(conversation_id, json.loads(message).get("text", ""), model=model,
                                     provider=provider, effort=effort, retry_of=run_id)

    async def admit_turn(self, conversation_id: str, message, *, model=None, provider=None, effort=None,
                         retry_of=None) -> ActiveRun:
        """Validate a message and admit its turn, or raise AdmissionError having written nothing."""
        if visible(message) is None:  # nothing visible to ask, invisible characters included
            raise AdmissionError(400, "empty_message", "The message is empty")
        if model is not None and visible(model) is None:
            raise AdmissionError(400, "model_needed", "Choose a model")
        if len(message) > MAX_MESSAGE_CHARS:
            raise AdmissionError(400, "message_too_long", "The message is too long")
        if effort is not None and effort not in budget_router._REASONING_OUTPUT_TOKENS:
            raise AdmissionError(400, "invalid_effort", "Unknown effort level")
        claim = self.registry.claim_turn(conversation_id)
        try:
            await self._record_dropped()
            return await self._admit(claim, message, model=model, provider=provider, effort=effort, retry_of=retry_of)
        except BaseException:
            self.registry.release(claim)
            raise

    async def _admit(self, claim, message, *, model, provider, effort, retry_of):
        conversation = await self._read(lambda conn: conn.execute(
            "SELECT project_id FROM conversations WHERE id = ?", (claim.conversation_id,)).fetchone())
        if conversation is None:
            raise AdmissionError(404, "not_found", "No such conversation")
        (project_id,) = conversation
        # The provider snapshot (settings and route) is taken under settings_lock, which
        # provider and key changes also hold: a change lands before it, or is refused as busy
        # once claim.provider names the provider.
        async with self.settings_lock:
            personal, project_settings = await asyncio.to_thread(
                lambda: (load_settings(self.data_dir), load_settings(self.data_dir, project_id)))
            # A model asked for must be named; a blank default in a settings file is passed over.
            chosen = visible(model) or visible(project_settings.values.get("models", {}).get("default")) \
                or visible(personal.values["models"]["default"]) or "auto"
            configured = providers.configured(self.data_dir, personal)
            if not configured:
                raise AdmissionError(400, "no_provider", "No model provider is set up")
            provider_name = provider or (
                providers.OPENROUTER if providers.OPENROUTER in configured or len(configured) != 1
                else next(iter(configured)))
            if provider_name not in configured:
                raise AdmissionError(400, "unknown_provider", "That provider is not set up")
            provider_config = configured[provider_name]
            claim.provider = provider_name
            # The models the provider offers (Recommended, All or Pick): a model it does not
            # offer is refused, and Auto picks among those it does.
            table = (personal.values.get("providers") or {}).get(provider_name) or {}

            def offered(m):
                return providers.offered(table, provider_config, m, budget_router.RECOMMENDED)

            if chosen != budget_router.AUTO and not offered(chosen):
                raise AdmissionError(400, "model_not_offered", "That model is not offered for this provider")
            plan = budget_router.create_run_plan(
                message, chosen, lambda m: providers.Route(provider_config, m), effort=effort,
                is_openrouter=provider_config.is_openrouter, offered=offered,
                picked=table["models"] if isinstance(table.get("models"), list) else ())
            if plan.model is None:
                raise AdmissionError(400, "model_needed", "Choose a model for this provider")
            route = providers.Route(provider_config, plan.model)
        # The key is read outside the lock (a credential store may ask the researcher first);
        # it cannot change meanwhile, since key changes are refused while claim.provider is set.
        key = await asyncio.to_thread(credentials.load_key, self.data_dir, provider_name, self.keyring_backend)
        if key is None:
            raise AdmissionError(400, "provider_key_missing", "The provider has no key")
        instructions, _ = await asyncio.to_thread(load_instructions, self.data_dir, project_id)

        def admit(conn):
            moved = conn.execute("SELECT project_id FROM conversations WHERE id = ?", (claim.conversation_id,)).fetchone()
            if moved is not None and moved[0] != project_id:  # moved since it was read: its settings were another's
                raise AdmissionError(409, "conversation_moved", "The conversation moved to another project; send again")
            if claim.cancel_requested.is_set():  # stopped while it was admitted (shutdown): write nothing
                raise AdmissionError(503, "shutting_down", "The app is closing") if claim.cancel_reason == "shutdown" \
                    else AdmissionError(409, "cancelled", "The message was cancelled")
            if not conn.execute("SELECT 1 FROM conversations WHERE id = ?", (claim.conversation_id,)).fetchone():
                raise AdmissionError(404, "not_found", "No such conversation")
            (seq,) = conn.execute("SELECT coalesce(max(seq) + 1, 0) FROM turns WHERE conversation_id = ?",
                                  (claim.conversation_id,)).fetchone()
            history = conn.execute(
                "SELECT t.user_message, t.answer FROM turns t JOIN runs r ON r.id = t.run_id"
                " WHERE t.conversation_id = ? AND r.status = 'succeeded' AND t.answer IS NOT NULL"
                " ORDER BY t.seq DESC LIMIT ?", (claim.conversation_id, HISTORY_TURNS)).fetchall()
            conn.execute(
                "INSERT INTO runs (id, project_id, conversation_id, kind, workflow, limits, inputs)"
                " VALUES (?, ?, ?, 'turn', 'agent', ?, ?)",
                (claim.run_id, project_id, claim.conversation_id,
                 json.dumps({"model_call_seconds": MODEL_CALL_SECONDS}),
                 json.dumps({"route": route.key, "effort": effort})))
            if retry_of is not None and conn.execute(
                    "SELECT 1 FROM turns WHERE run_id = ? AND seq = ? - 1", (retry_of, seq)).fetchone() is None:
                raise AdmissionError(409, "not_continuable", "A newer turn was sent meanwhile")
            conn.execute(
                "INSERT INTO turns (run_id, conversation_id, seq, author, user_message, retry_of_run_id)"
                " VALUES (?, ?, ?, 'researcher', ?, ?)",
                (claim.run_id, claim.conversation_id, seq, json.dumps({"text": message}), retry_of))
            _event(conn, claim.run_id, "route", {"route": route.key, "plan": plan.to_dict()})
            return seq, history[::-1]

        (seq, history), cancelled = await self._write_through(admit)
        if cancelled:  # its request went away while the run was written: it never runs, as by Stop
            await _through(self._write(lambda conn: self._finish_turn(
                conn, claim.run_id, "cancelled", "researcher", "cancelled")))
            raise asyncio.CancelledError()
        messages = [{"role": "system", "content": SYSTEM_RULES + (f"\n\n{instructions}" if instructions else "")}]
        for user_message, answer in history:
            messages.append({"role": "user", "content": json.loads(user_message).get("text", "")})
            messages.append({"role": "assistant", "content": json.loads(answer).get("text", "")})
        messages.append({"role": "user", "content": message})
        claim.context = {
            "project_id": project_id, "seq": seq, "route": route, "key": key, "messages": messages,
            "effort": effort, "estimate": plan.predicted_cost,
        }
        claim.admitted = time.monotonic()
        return claim

    async def events(self, claim: ActiveRun):
        """Start an admitted turn and yield its events until it ends.

        The turn is pulled by its stream: it starts when the response starts, and it
        goes on to its next step (the reservation, then the model call) only when the
        reader asks for the next event, so it never runs ahead of a reader that has
        stalled. A response dropped before it starts spends nothing; its claim goes
        stale and is released. Stopping early (a closed stream) cancels the turn, like
        Stop. The turn runs in its own task only so that a cancellation is delivered
        once and its cleanup always completes.
        """
        if claim.cancel_requested.is_set() or self.registry.runs.get(claim.run_id) is not claim:
            return  # cancelled before its response started
        claim.task = asyncio.create_task(self._turn(claim))
        try:
            while True:
                claim.wanted.set()
                event = await claim.events.get()
                if event is None:
                    return
                yield event
        finally:
            self.stream_closed(claim)

    async def _turn(self, claim: ActiveRun) -> None:
        claim.started = True
        ctx = claim.context
        emit = claim.events.put_nowait

        async def handed(event):  # give the reader one event, then wait until it asks for the next
            claim.wanted.clear()
            emit(event)
            await claim.wanted.wait()

        try:
            if claim.cancel_requested.is_set():  # stopped before it began
                raise asyncio.CancelledError()
            await handed({"type": "run_started", "run_id": claim.run_id, "conversation_id": claim.conversation_id,
                          "seq": ctx["seq"]})
            call = await self._reserve(claim, ctx["project_id"], claim.conversation_id, ctx["estimate"],
                                       phase="answer")
            await handed({"type": "step", "seq": call.step, "phase": "answer"})
            result = await self._call(claim, call, ctx["route"], ctx["key"], ctx["messages"], effort=ctx["effort"])
            if not result.ok:
                final = await self._write(lambda conn: self._finish_turn(conn, claim.run_id, "failed", None,
                                                                          result.error_kind, claim=claim))
                if final["status"] == "failed":  # not a Stop that came first
                    emit({"type": "error", "code": result.error_kind})
                emit({"type": "run_finished", **final})
                return
            answer = {"text": result.content, **({"reasoning": result.reasoning} if result.reasoning else {})}
            try:
                final = await self._write(lambda conn: self._commit_answer(conn, claim, answer, ctx))
            except _Cancelled:  # the Stop came first: no answer is published
                raise asyncio.CancelledError() from None
            except Exception as error:  # the answer could not be saved: it is still shown, marked unsaved
                log.error("the answer could not be saved (%s at %s)", type(error).__name__, _where(error))
                emit({"type": "chat_response", "content": result.content, "reasoning": result.reasoning,
                      "result_saved": False, "error": "save_failed"})
                with suppress(Exception):
                    final = await _through(self._write(lambda conn: self._finish_turn(
                        conn, claim.run_id, "failed", None, "save_failed", claim=claim)))
                    emit({"type": "run_finished", **final[0]})
                return
            emit({"type": "chat_response", "content": result.content, "reasoning": result.reasoning,
                  "result_saved": True})
            emit({"type": "run_finished", **final})
        except asyncio.CancelledError:
            final, _ = await _through(self._stop_turn(claim))
            emit({"type": "run_finished", **final})
        except spending.BudgetExceeded as exceeded:  # stopped at a limit: Continue is offered once it is raised
            final, _ = await _through(self._write(lambda conn: self._finish_turn(
                conn, claim.run_id, "cancelled", "limit", "budget", limit={"budget": exceeded.budget}, claim=claim)))
            if final.get("cancel_reason") == "limit":  # not a Stop that came first
                emit({"type": "limit_reached", "budget": exceeded.budget})
            emit({"type": "run_finished", **final})
        except Exception as error:  # a defect, not a provider failure; the turn ends failed
            log.error("turn failed unexpectedly (%s at %s)", type(error).__name__, _where(error))
            with suppress(Exception):
                final, _ = await _through(self._stop_turn(claim, status="failed", reason="internal"))
                emit({"type": "run_finished", **final})
        finally:
            self.registry.release(claim)
            claim.events.put_nowait(None)
            if not self.registry.closed:
                self._detach(self.kick_background())

    async def _stop_turn(self, claim, *, status=None, reason=None):
        """End a turn that did not reach its primary commit, settling its open call once."""
        if status is None:
            status = "interrupted" if claim.cancel_reason == "shutdown" else "cancelled"
        cancel_reason = None if status != "cancelled" else ("revoked" if claim.cancel_reason == "revoked" else "researcher")
        call = claim.call

        def stop(conn):
            _close_call(conn, call)
            return self._finish_turn(conn, claim.run_id, status, cancel_reason, reason or status, claim=claim)

        return await self._write(stop)

    def _finish_turn(self, conn, run_id, status, cancel_reason, reason_code, limit=None, claim=None):
        """Write a terminal status for a turn that has no answer, if it is still running.
        Returns the status the turn has afterwards. With claim, a Stop, revocation or
        shutdown requested before this transaction wins over any other ending, as at the
        primary commit: the turn ends cancelled, or interrupted by the shutdown (so
        Continue is offered)."""
        if claim is not None and claim.cancel_requested.is_set():
            limit = None
            if claim.cancel_reason == "shutdown":
                status, cancel_reason, reason_code = "interrupted", None, "interrupted"
            else:
                status, reason_code = "cancelled", "cancelled"
                cancel_reason = "revoked" if claim.cancel_reason == "revoked" else "researcher"
        if _running(conn, run_id):
            if limit is not None:
                _event(conn, run_id, "limit_hit", limit)
            cost = spending.run_cost(conn, run_id)
            _event(conn, run_id, "run_finished", {"status": status, "reason": reason_code})
            conn.execute("UPDATE runs SET status = ?, cancel_reason = ?, finished_at = ?, settled_cost_usd = ?"
                         " WHERE id = ?", (status, cancel_reason, utc_now(), cost, run_id))
            conn.execute("UPDATE turns SET reason_code = ?, memory_status = 'skipped', result_saved = 0,"
                         " accounting = ? WHERE run_id = ?",
                         (reason_code, json.dumps(_accounting(conn, run_id)), run_id))
        row = conn.execute("SELECT status, cancel_reason, settled_cost_usd FROM runs WHERE id = ?",
                           (run_id,)).fetchone()
        if row is None:
            return {"run_id": run_id, "status": "deleted"}
        return {"run_id": run_id, "status": row[0], **({"cancel_reason": row[1]} if row[1] else {}),
                "cost_usd": row[2], "accounting": _accounting(conn, run_id)}

    def _commit_answer(self, conn, claim, answer, ctx):
        """The primary commit. Refused if cancellation was requested, so no answer is
        published after a Stop the turn saw first; a later Stop is told the turn succeeded."""
        if claim.cancel_requested.is_set():
            raise _Cancelled()
        if not _running(conn, claim.run_id):  # deleted (or ended) since it was admitted: nothing to publish
            raise _Cancelled()
        cost = spending.run_cost(conn, claim.run_id)
        _event(conn, claim.run_id, "run_finished", {"status": "succeeded"})
        conn.execute("UPDATE turns SET answer = ?, result_saved = 1, phase = 'answer', memory_status = 'skipped',"
                     " accounting = ? WHERE run_id = ?",
                     (json.dumps(answer), json.dumps(_accounting(conn, claim.run_id)), claim.run_id))
        conn.execute("UPDATE runs SET status = 'succeeded', finished_at = ?, settled_cost_usd = ? WHERE id = ?",
                     (utc_now(), cost, claim.run_id))
        conn.execute("UPDATE conversations SET updated_at = ? WHERE id = ?", (utc_now(), claim.conversation_id))
        # Detached post-answer work, written with the answer so it is never lost: one title run
        # per conversation, ever, for a conversation that is still untitled. A title run that
        # failed or was cancelled is not replaced by another paid one.
        untitled = conn.execute(
            "SELECT c.title_rev FROM conversations c WHERE c.id = ? AND c.title IS NULL AND c.title_source IS NULL"
            " AND NOT EXISTS (SELECT 1 FROM runs r JOIN turns t ON t.run_id = r.source_turn_id"
            "                 WHERE r.workflow = 'title' AND t.conversation_id = c.id)",
            (claim.conversation_id,)).fetchone()
        if untitled is not None:
            conn.execute(
                "INSERT INTO runs (id, project_id, kind, workflow, source_turn_id, inputs)"
                " VALUES (?, ?, 'background', 'title', ?, ?)",
                (new_id(), ctx["project_id"], claim.run_id, json.dumps({
                    "conversation_id": claim.conversation_id, "title_rev": untitled[0],
                    "provider": ctx["route"].provider.name, "model": ctx["route"].model,
                })))  # content-free: the message is read from the source turn when the run calls
        return {"run_id": claim.run_id, "status": "succeeded", "cost_usd": cost,
                "accounting": _accounting(conn, claim.run_id)}

    # Model calls

    async def _budgets(self, project_id):
        """The project's budget and the default conversation budget, as the settings say now."""
        personal, project = await asyncio.to_thread(
            lambda: (load_settings(self.data_dir), load_settings(self.data_dir, project_id)))
        return project.values["project"]["budget_usd"], personal.values["budget"]["conversation_usd"]

    async def _reserve(self, active, project_id, paying_conversation_id, estimate, *, phase):
        """Admit one model call: its reservation and step_started event, against the budgets
        as they are now (read under settings_lock). A cancellation during the write releases
        the reservation once it is written."""
        run_id = active.run_id

        def reserve(conn):
            if not _running(conn, run_id):  # deleted or ended since it was admitted
                raise _Cancelled()
            (own,) = conn.execute("SELECT budget_usd FROM conversations WHERE id = ?",
                                  (paying_conversation_id,)).fetchone() or (None,)
            (step,) = conn.execute("SELECT count(*) FROM run_events WHERE run_id = ? AND type = 'step_started'",
                                   (run_id,)).fetchone()
            reservation = spending.reserve(conn, run_id=run_id, step_seq=step, project_id=project_id,
                                           paying_conversation_id=paying_conversation_id, estimate_usd=estimate,
                                           project_budget_usd=project_budget,
                                           conversation_budget_usd=own if own is not None else default_budget)
            _event(conn, run_id, "step_started", {"step": step, "phase": phase, "estimate_usd": estimate,
                                                   "reservation_id": reservation})
            return _Call(run_id, step, reservation)

        async with self.settings_lock:
            project_budget, default_budget = await self._budgets(project_id)
            try:
                call, cancelled = await self._write_through(reserve)
            except _Cancelled:
                raise asyncio.CancelledError() from None
        active.call = call
        if cancelled or active.cancel_requested.is_set():
            raise asyncio.CancelledError()
        return call

    async def _call(self, active, call, route, key, messages, *, effort=None, max_tokens=None, output=None):
        """Dispatch one admitted call through the gate and record it, settling its
        reservation. output(result), if given, is recorded on the finished step, so a
        run can later finish from the record."""
        if active.cancel_requested.is_set():
            raise asyncio.CancelledError()
        project_id = await self._read(lambda conn: conn.execute(
            "SELECT b.project_id FROM budget_reservations b JOIN runs r ON r.id = b.run_id"
            " WHERE b.id = ? AND r.status = 'running'", (call.reservation_id,)).fetchone())
        if project_id is None:  # its run, conversation or project was deleted (or it ended) since admission
            raise asyncio.CancelledError()
        def dispatched():  # the gate let the request out: from here it may be billed
            call.dispatched = True

        call.route = route.key
        async with self.gate.async_client(project_id[0]) as client:
            result = await openrouter.query_model(
                client, route, key, messages, timeout=MODEL_CALL_SECONDS, effort=effort, max_tokens=max_tokens,
                model_entry=get_model_metadata(route), on_dispatch=dispatched)
            finished = {"step": call.step, "outcome": result.error_kind or "ok"}
            if output is not None and result.ok:
                finished["output"] = output(result)

            def record(conn):
                # The receipt settles the reservation by its id even if the run was deleted
                # meanwhile; the run's own record goes with the run.
                if conn.execute("SELECT 1 FROM runs WHERE id = ?", (active.run_id,)).fetchone() is not None:
                    for attempt in result.attempts:
                        _event(conn, active.run_id, "model_attempt",
                               {"step": call.step, "route": route.key, **attempt.record()})
                    _event(conn, active.run_id, "step_finished", finished)
                if result.dispatched:
                    _settle_attempts(conn, call.reservation_id, route, result.attempts)
                else:
                    spending.release(conn, call.reservation_id)

            # Recorded before the client closes, and to its end even if cancelled meanwhile.
            _, cancelled = await self._write_through(record)
        active.call = None
        if cancelled:
            raise asyncio.CancelledError()
        return result

    # Background runs

    async def kick_background(self) -> None:
        """Start every background run that is running in the record but not in this process."""
        if self.registry.closed:
            return
        rows = await self._read(lambda conn: conn.execute(
            "SELECT id FROM runs WHERE kind = 'background' AND status = 'running' ORDER BY started_at").fetchall())
        for (run_id,) in rows:
            active = self.registry.add_background(run_id)
            if active is not None:
                active.task = asyncio.create_task(self._background(active))

    async def _background(self, active: ActiveRun) -> None:
        active.started = True
        try:
            if active.cancel_requested.is_set():  # stopped before it began
                raise asyncio.CancelledError()
            row = await self._read(lambda conn: conn.execute(
                "SELECT r.project_id, r.workflow, r.attempts, r.inputs, r.status, t.user_message FROM runs r"
                " LEFT JOIN turns t ON t.run_id = r.source_turn_id WHERE r.id = ?",
                (active.run_id,)).fetchone())
            if row is None or row[4] != "running":
                return  # rule 1: finished (or deleted); never run again
            project_id, workflow, attempts, inputs, _, source = row
            inputs = json.loads(inputs or "{}")
            inputs["message"] = json.loads(source).get("text", "")[:4000] if source else None  # held in memory only
            recorded = await self._read(lambda conn: conn.execute(
                "SELECT data FROM run_events WHERE run_id = ? AND type = 'step_finished' ORDER BY seq DESC LIMIT 1",
                (active.run_id,)).fetchone())
            if recorded is not None:  # rule 2: finish from the record, whatever it says, with no model call
                step = json.loads(recorded[0])
                output = step.get("output") if step.get("outcome") == "ok" else None
            elif attempts < BACKGROUND_ATTEMPTS:  # rule 3: another model call
                output = await self._background_call(active, project_id, workflow, inputs)
            else:  # rule 4
                await self._write(lambda conn: self._finish_background(conn, active, "interrupted", None, inputs))
                return
            await self._write(lambda conn: self._finish_background(
                conn, active, "succeeded" if output is not None else "failed", output, inputs))
        except asyncio.CancelledError:
            call, shutdown = active.call, active.cancel_reason == "shutdown"
            cancel_reason = "revoked" if active.cancel_reason == "revoked" else "researcher"

            def stop(conn):
                _close_call(conn, call)
                if not shutdown:  # at shutdown it stays running and restarts next time
                    self._finish_background(conn, active, "cancelled", None, None, cancel_reason=cancel_reason)
            await _through(self._write(stop))
        except spending.BudgetExceeded:
            await _through(self._write(lambda conn: self._finish_background(conn, active, "failed", None, None)))
        except Exception as error:
            log.error("background run failed unexpectedly (%s at %s)", type(error).__name__, _where(error))
        finally:
            self.registry.release(active)

    async def _background_call(self, active, project_id, workflow, inputs):
        """One model call for a background run. Returns its output, or None if it failed."""
        if workflow != "title":
            raise ValueError(f"unknown background workflow {workflow!r}")
        if not inputs.get("message"):  # its source turn is gone: nothing to title
            return None
        async with self.settings_lock:  # the provider snapshot, as for a turn (see _admit)
            active.provider = inputs.get("provider")
            route = await asyncio.to_thread(providers.resolve_route, self.data_dir, inputs.get("provider"),
                                            inputs.get("model"))
            if route is not None:  # a model its provider no longer offers is not called: no title
                personal = await asyncio.to_thread(load_settings, self.data_dir)
                table = (personal.values.get("providers") or {}).get(route.provider.name) or {}
                if not providers.offered(table, route.provider, route.model, budget_router.RECOMMENDED):
                    route = None
        key = route and await asyncio.to_thread(credentials.load_key, self.data_dir, route.provider.name,
                                                self.keyring_backend)
        if route is None or key is None:
            return None
        def start(conn):
            if not _running(conn, active.run_id):
                raise _Cancelled()
            conn.execute("UPDATE runs SET attempts = attempts + 1 WHERE id = ?", (active.run_id,))  # before the call
            (step,) = conn.execute("SELECT count(*) FROM run_events WHERE run_id = ? AND type = 'step_started'",
                                   (active.run_id,)).fetchone()
            estimate = budget_router.estimate_title_cost(route)
            reservation = spending.reserve(conn, run_id=active.run_id, step_seq=step, project_id=project_id,
                                           paying_conversation_id=None, estimate_usd=estimate,
                                           project_budget_usd=budget, conversation_budget_usd=None)
            _event(conn, active.run_id, "step_started", {"step": step, "phase": "title", "estimate_usd": estimate,
                                                          "reservation_id": reservation})
            return _Call(active.run_id, step, reservation)

        async with self.settings_lock:  # the project's budget as it is now
            budget, _ = await self._budgets(project_id)
            try:
                call, cancelled = await self._write_through(start)
            except _Cancelled:
                raise asyncio.CancelledError() from None
        active.call = call
        if cancelled:
            raise asyncio.CancelledError()
        messages = [{"role": "system", "content": TITLE_RULES}, {"role": "user", "content": inputs["message"]}]
        result = await self._call(active, call, route, key, messages, max_tokens=60,
                                  output=lambda result: _clean_title(result.content))
        return _clean_title(result.content) if result.ok else None

    def _finish_background(self, conn, active, status, output, inputs, cancel_reason=None):
        """The run's effect, terminal status and settled cost, in one transaction. A cancel
        requested before this transaction checks for it wins, on every path: the run ends
        cancelled, with no effect. One that comes later finds the run ended, and the cancel
        request reports how (Harness.cancel)."""
        run_id = active.run_id
        if not _running(conn, run_id):
            return
        if active.cancel_requested.is_set() and active.cancel_reason != "shutdown":
            status, output = "cancelled", None
            cancel_reason = "revoked" if active.cancel_reason == "revoked" else "researcher"
        if status == "succeeded" and output is not None:
            # The title is written only if nobody changed it since the run was queued.
            conn.execute(
                "UPDATE conversations SET title = ?, title_source = 'generated', title_rev = title_rev + 1,"
                " updated_at = ? WHERE id = ? AND title_rev = ? AND coalesce(title_source, '') <> 'researcher'",
                (output, utc_now(), inputs["conversation_id"], inputs["title_rev"]))
        _event(conn, run_id, "run_finished", {"status": status})
        conn.execute("UPDATE runs SET status = ?, cancel_reason = ?, finished_at = ?, settled_cost_usd = ? WHERE id = ?",
                     (status, cancel_reason, utc_now(), spending.run_cost(conn, run_id), run_id))


async def _through(awaitable):
    """Await to the end, whatever cancellations arrive meanwhile. Returns (result, whether
    a cancellation arrived), so the caller can honor it after its write is safe."""
    task = asyncio.ensure_future(awaitable)
    cancelled = False
    while True:
        try:
            return await asyncio.shield(task), cancelled
        except asyncio.CancelledError:
            if task.cancelled():
                raise
            cancelled = True


def _settle_attempts(conn, reservation_id, route, attempts):
    """Settle a recorded call from all of its dispatched attempts: their reported costs, or
    priced reported tokens where an attempt reported no cost (then marked estimated). An
    attempt that went out and reported no usage keeps the estimate: the call settles at the
    reservation's estimate, or at what the others cost when that is more, marked estimated."""
    total, basis = 0.0, "reported"
    for attempt in attempts:
        if not attempt.dispatched:
            continue
        cost = attempt.reported_cost
        if cost is None:
            cost = budget_router.cost_from_usage(route, attempt.usage)
            if cost is None:
                basis = "unknown"
                continue
            basis = "estimated" if basis == "reported" else basis
        total += cost
    if basis == "reported":
        spending.settle(conn, reservation_id, total)
    elif basis == "estimated":
        spending.settle(conn, reservation_id, None, estimated_usd=total)
    else:
        row = conn.execute("SELECT estimate_usd FROM budget_reservations WHERE id = ?", (reservation_id,)).fetchone()
        spending.settle(conn, reservation_id, None, estimated_usd=max(total, row[0]) if row else None)


def _close_call(conn, call):
    """Close a call cut off before it was recorded: one that may have gone out settles at
    its estimate and leaves a content-free attempt with an unknown charge; one that never
    left is released. Either happens once; a call already recorded is left as it is."""
    if call is None:
        return
    if call.dispatched:
        if spending.settle(conn, call.reservation_id) and _running(conn, call.run_id):
            _event(conn, call.run_id, "model_attempt", {
                "step": call.step, "route": call.route, "outcome": "cancelled", "http_status": None,
                "dispatched": True, "charge": "unknown"})
    else:
        spending.release(conn, call.reservation_id)


def _accounting(conn, run_id, complete=True):
    """The run's spending by basis: reported, estimated, attempts with no usable usage, and
    whether the record is complete (False when a call was in flight at a crash, whose
    attempt left no record and whose cost is counted at its estimate)."""
    rows = conn.execute("SELECT basis, coalesce(sum(settled_usd), 0) FROM budget_reservations"
                        " WHERE run_id = ? AND status = 'settled' GROUP BY basis", (run_id,)).fetchall()
    totals = dict(rows)
    unknown = sum(1 for (data,) in conn.execute(
        "SELECT data FROM run_events WHERE run_id = ? AND type = 'model_attempt'", (run_id,))
        if json.loads(data).get("charge") == "unknown" and json.loads(data).get("dispatched"))
    return {"reported_usd": totals.get("reported", 0), "estimated_usd": totals.get("estimated", 0),
            "unknown_attempts": unknown, "complete": complete}


def _clean_title(text):
    if not isinstance(text, str):
        return None
    return visible(" ".join(text.split()).strip("\"'“”‘’「」『』 ")[:80])
