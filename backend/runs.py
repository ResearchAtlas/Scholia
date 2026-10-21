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

Every model call goes through one dispatch path (Harness._call): its request is built by the
context assembler (backend/context.py) for the window every endpoint it can reach accepts
(backend/reach.py), its reservation is sized from that request at the highest rate it can
reach, and it passes the dispatch check (may_dispatch) inside the outbound
gate's decision transaction: a run revoked by a deletion or a change to its
project (cancel_reason 'revoked' in the record), or one whose owners are gone or
whose project is review-locked, sends nothing more. A revoked run ends cancelled
with cancel_reason revoked, keeping its settled costs and applying no effect.
Admission checks what the project allows (backend/governance.py) and refuses a
route it does not, before anything is written.

Background runs (titles) run outside any turn, so the conversation accepts its
next message at once. Each model call they start is counted in `attempts`, in its
own transaction, before the call. On every start, including after a crash, a
background run is finished from a recorded finished step with no model call, or
restarted while it has made fewer than 2 attempts, or else marked interrupted.
Its effects, terminal status and settled cost are written in one transaction.

Local background work (reading a material, identifier lookups, full backups and project
exports; the workflows registered in `Harness.workflows`) runs the same way without a model
call: it starts again from its inputs after a restart, unless its handler finds what it needs
held only in memory gone (a passphrase), and then ends interrupted. Blocking work runs in a
thread to its end (`Harness.work`), stopping at its next check once cancellation is requested.
Its progress is live only (`ActiveRun.progress`). Its effect, terminal status and summary are
written in one transaction; a revocation, or a cancellation requested before that transaction,
leaves no effect.
"""

import asyncio
import json
import logging
import threading
import time
import traceback
from contextlib import suppress
from dataclasses import dataclass, field

from backend import (budget_router, citations, context, credentials, governance, openrouter, openrouter_client,
                     provenance, providers, reach, router, spending)
from backend.evidence import historical_citations
from backend.search_index import still_read
from backend.db import new_id, utc_now
from backend.openrouter_client import catalog_read, catalog_status, get_model_metadata
from backend.settings import load_instructions, load_settings, visible

log = logging.getLogger(__name__)

STALE_CLAIM_SECONDS = 30  # an admitted turn whose response has not started by then was dropped
MODEL_CALL_SECONDS = 120  # one call's total bound, both attempts included
MAX_MESSAGE_CHARS = 100_000
BACKGROUND_ATTEMPTS = 2
CANCEL_WAIT_SECONDS = 10  # how long a cancel request waits to report how the run ended
SYSTEM_RULES = (
    "You are Scholia, a research assistant working inside the researcher's own project. "
    "Answer in the language of the researcher's message unless they ask otherwise. "
    "Say plainly when you do not know something.\n"
    # S1-19: cited answers (slice-1 spec F4 and section 7.5; ticket 17's claim wording)
    "When the request holds RESEARCH EVIDENCE, cite every claim you draw from it with its marker, such as "
    "[S1.2], right after the claim, and cite only the markers given there. Phrase what you draw from a paper "
    "as what that paper reports, under the conditions it states, not as established fact. If the evidence "
    "does not answer the question, say so plainly and do not fill the gap from general knowledge. Say when a "
    "record is only an abstract or metadata rather than the paper's text, and when a paper is marked retracted."
)
RETRIEVAL_ROUTES = ("literature", "memory_literature")
MAX_SELECTION = 50  # papers a message may select
JUDGE_SECONDS = 60  # one support-judge call's total bound
TITLE_RULES = (
    "Write a title of at most six words for a conversation that begins with the message below. "
    "Use the language of the message. Reply with the title only, without quotation marks."
)


def _unusable(table, provider, model) -> str | None:
    """Why a model may not be called, or None: model_not_offered when its provider does not offer
    it (Recommended, All or Pick) or its catalog, once read, no longer lists it; model_window when
    its window is not usable (slice-1 spec section 8: needed, or under 4,096); catalog_unread when
    its provider's catalog has not been read, so its window is unknown (an OpenRouter model's
    always, another's unless a setting gives it). Before a call, the window every endpoint it can
    reach accepts is checked too (Harness._route_facts)."""
    if not providers.offered(table, provider, model, budget_router.RECOMMENDED):
        return "model_not_offered"
    route = providers.Route(provider, model)
    row = get_model_metadata(route)
    if row is None and catalog_read(route):
        return "model_not_offered"
    if row is None and provider.is_openrouter:
        return "catalog_unread"
    # A price the catalog does not list for the model as a whole is read per endpoint before the call
    # (Harold, 2026-10-20: refused only when an endpoint it can reach publishes none; Harness._route_facts).
    if providers.window(table, model, (row or {}).get("context_length"))["status"] == "ok":
        return None
    return "model_window" if row is not None else "catalog_unread"


_REFUSED = {  # an admission refusal's English text, by code; the interface shows its own by code
    "model_not_offered": "That model is not offered for this provider",
    "model_window": "That model has no usable window; set one in Settings",
    "catalog_unread": "The provider's model list could not be read; try again or set a window",
    "route_unverified": "The model's endpoints could not be read from the provider; try again",
    "route_unpriced": "The model's price is not published, or it lists a charge Scholia cannot estimate",
    "route_unsupported": "No endpoint of that model accepts this request",
    "message_over_window": "The message is too large for this model's window",
    "instructions_over_window": "The project's instructions are too large for this model's window",
    "evidence_over_window": "The evidence this needs does not fit this model's window",
}


def _default_provider(configured, policy) -> str:
    """The provider of a message that names none: OpenRouter when it is set up, else the only
    one; counting only the providers the project's level may use, when it may use any (a Local
    only project, a declared server)."""
    usable = {name: p for name, p in configured.items() if policy.allows_provider(p)} or configured
    return providers.OPENROUTER if providers.OPENROUTER in usable or len(usable) != 1 else next(iter(usable))


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
    context: dict | None = None  # what an admitted turn, or local work, needs to run; in memory only
    call: "_Call | None" = None  # the model call admitted last, until it is settled or released
    provider: str | None = None  # the provider its model calls go to
    closing: asyncio.Future | None = None  # the write that records a claim stopped before it started
    progress: dict | None = None  # local work's progress, live only: {"done", "total"}
    follows: list = field(default_factory=list)  # runs its terminal transaction records (record_in), held till it commits
    answered: asyncio.Event = field(default_factory=asyncio.Event)  # one of its asks was answered (asks.answer_ask)


class Registry:
    """The runs this process is working on: at most one turn per conversation."""

    def __init__(self):
        self.runs: dict[str, ActiveRun] = {}
        self.turns: dict[str, ActiveRun] = {}  # by conversation
        self.closed = False
        self.dropped: list[str] = []  # stale claims released, whose runs are still to be recorded
        # Background runs whose terminal record this process wrote, each added once that record has
        # committed and before the run is released: a read taken just before the commit still says
        # running, and is not interrupted. A run whose terminal write raised is never added, so once
        # released it reads interrupted, as its record still says running (Harness._ended).
        # ponytail: one id per background run finished in this launch; a bounded log if that grows large
        self.finished: set[str] = set()
        # Background runs a retry takes over (materials.retry_run): never held from then on. The lock
        # makes holding a run (add_background) and retiring it (retire, from a write transaction's
        # thread, or the event loop) one decision: whichever comes first, the other is refused.
        self.retired: set[str] = set()
        self.lock = threading.Lock()

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
        with self.lock:
            if self.closed or run_id in self.runs or run_id in self.retired:
                return None
            active = ActiveRun(run_id, "background", None)
            self.runs[run_id] = active
            return active

    def retire(self, run_id: str) -> bool:
        """Keep a run this process does not hold from ever being held here: False, retiring nothing,
        when it is held, or while the harness is closed (a resume holds its runs again)."""
        with self.lock:
            if self.closed or run_id in self.runs:
                return False
            self.retired.add(run_id)
            return True

    def unretire(self, run_id: str) -> None:
        """Undo a retire whose retry failed: a kick or a resume may hold the run again."""
        with self.lock:
            self.retired.discard(run_id)

    def release(self, active: ActiveRun) -> None:
        with self.lock:  # from a write transaction's thread too (record_background)
            if self.runs.get(active.run_id) is active:
                del self.runs[active.run_id]
            if active.conversation_id is not None and self.turns.get(active.conversation_id) is active:
                del self.turns[active.conversation_id]

    def is_active(self, run_id: str) -> bool:
        return run_id in self.runs


def derived_status(status: str, run_id: str, registry: Registry) -> str:
    """A run's status as it reads now: a running run this process does not hold is interrupted. A
    background run is held from before its record commits (record_background, start_local) until its
    terminal record has, and is in `finished` from then on, so a read that raced its start or its end
    never takes it for interrupted."""
    if status == "running" and not registry.is_active(run_id) and run_id not in registry.finished:
        return "interrupted"
    return status


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


class RunOutcome(Exception):
    """Ends local work otherwise than succeeded: status failed (reason, a stable code), cancelled
    (cancel_reason limit, or researcher for an answer that declined it) or interrupted."""

    def __init__(self, status, reason=None, cancel_reason=None):
        super().__init__(reason or status)
        self.status, self.reason, self.cancel_reason = status, reason, cancel_reason


def _running(conn, run_id) -> bool:
    row = conn.execute("SELECT status FROM runs WHERE id = ?", (run_id,)).fetchone()
    return row is not None and row[0] == "running"


def _revoked(conn, run_id) -> bool:
    """Whether a running run was revoked in the record, by a deletion or a change to its project."""
    row = conn.execute("SELECT cancel_reason FROM runs WHERE id = ? AND status = 'running'", (run_id,)).fetchone()
    return row is not None and row[0] == "revoked"


def _let_go(conn, run_id):
    """A background run's terminal record takes out what its inputs hold only while it runs (`held`:
    a merge's record, backend/references.py), so nothing of it outlasts the run however it ends."""
    conn.execute("UPDATE runs SET inputs = json_remove(inputs, '$.held') WHERE id = ?"
                 " AND json_type(inputs, '$.held') IS NOT NULL", (run_id,))


def may_dispatch(conn, run_id) -> bool:
    """The check before every dispatch (slice-1 spec section 10, Revocation), which the outbound
    gate reads in its decision transaction, so retries are covered too: the run is still running
    and not revoked; every owner it has still exists (a turn's project and conversation; detached
    work's project and source turn; project background work's project alone, whose missing
    conversation and source turn are no deletion); its project is not review-locked; and every
    passage its request holds (inputs.evidence, S1-19's [passage id, material id] pairs) is still read
    by the paper the request names it under. The project's current level and the route it allows are
    the gate's own checks there."""
    row = conn.execute(
        "SELECT r.status, r.cancel_reason, r.kind, p.review_lock,"
        " r.conversation_id IS NOT NULL AND EXISTS (SELECT 1 FROM conversations WHERE id = r.conversation_id),"
        " r.source_turn_id IS NULL OR EXISTS (SELECT 1 FROM turns WHERE run_id = r.source_turn_id),"
        " r.project_id, json_extract(r.inputs, '$.evidence'), json_extract(r.inputs, '$.context_materials')"
        " FROM runs r JOIN projects p ON p.id = r.project_id WHERE r.id = ?", (run_id,)).fetchone()
    if row is None:
        return False
    status, cancel_reason, kind, locked, has_conversation, has_source, project_id, evidence, held = row
    owners = has_conversation if kind in ("turn", "child") else has_source
    if not (status == "running" and cancel_reason is None and not locked and bool(owners)):
        return False
    # S1-19: every passage in the request's context is still read by the paper it is named under
    # (section 10, Revocation: each PR that adds a context source registers its records here).
    wanted = {tuple(pair) for pair in json.loads(evidence)} if evidence else set()
    if held and not provenance.carried(conn, json.loads(held)):  # a paper whose text the request carries is gone
        return False
    return not wanted or still_read(conn, project_id, wanted) == wanted


@dataclass
class _Call:
    """One admitted model call: its run, step, reservation and whether it may have left."""
    run_id: str
    step: int
    reservation_id: str
    dispatched: bool = False
    route: str | None = None  # the route key, once the call is under way
    retention: dict | None = None  # the retention terms the gate applied when it let the call out
    bodies: list = field(default_factory=list)  # S1-19: each attempt's request body as stored (its sha256, or None)
    materials: tuple = ()  # the papers whose text its request holds, for the bodies' deletion links


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
        self.content = None  # the data folder's content store, for the trace's request bodies (set by the app)
        self.registry = Registry()
        # Local background work by workflow: async handler(harness, active, project_id, inputs) ->
        # (summary, effect), where effect(conn), if any, is applied in the run's terminal transaction.
        self.workflows = {}
        # By workflow: ending(conn, active, project_id), called in a local run's terminal transaction
        # whatever its outcome and path (_finish_local, or _finish_background for one stopped before its
        # work began; S1-17: a project's index run, asked for again while it ran, runs once more).
        self.endings = {}
        self._tasks = set()  # detached tasks, kept referenced until they finish
        # Index runs a kick started again after they were left running unheld (a terminal write that failed): once
        # each, so a failure that lasts never makes a loop of them (the next trigger, Retry or launch takes it then)
        self._started_again = set()
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

    async def recover(self, kick=True) -> None:
        """Record what a crash left behind, then restart the background runs (unless kick is
        false: a restore starts them once it has committed, see kick_background). Call once at
        startup, before serving requests."""

        def record(conn):
            _release_allowances(conn)  # a held share no call was made against is not spent, nor in flight
            in_flight = {run_id for (run_id,) in conn.execute(
                "SELECT DISTINCT run_id FROM budget_reservations WHERE status = 'open' AND run_id IS NOT NULL")}
            spending.settle_left_open(conn)
            now = utc_now()
            # S1-19b: a call in flight at the crash kept its exact request body (stored and linked before it was
            # sent); its attempt is recorded now, naming it, with an unknown charge, for every kind of run.
            for run_id, step, sha in conn.execute(
                    "SELECT b.run_id, b.step, b.sha256 FROM run_bodies b JOIN runs r ON r.id = b.run_id"
                    " WHERE r.status = 'running' AND NOT EXISTS (SELECT 1 FROM run_events e WHERE e.run_id = b.run_id"
                    " AND e.type = 'model_attempt' AND json_extract(e.data, '$.body') = b.sha256)"
                    " ORDER BY b.run_id, b.step, b.attempt").fetchall():
                _event(conn, run_id, "model_attempt", {"step": step, "outcome": "interrupted", "dispatched": None,
                                                       "charge": "unknown", "body": sha})
            for run_id, revoked in conn.execute(
                    "SELECT id, cancel_reason = 'revoked' FROM runs WHERE status = 'running' AND kind IN ('turn', 'child')"
            ).fetchall():
                status = "cancelled" if revoked else "interrupted"  # a revoked run stays revoked
                _event(conn, run_id, "run_finished", {"status": status})
                conn.execute(
                    "UPDATE runs SET status = ?, finished_at = ?, settled_cost_usd = ? WHERE id = ?",
                    (status, now, spending.run_cost(conn, run_id), run_id))
                conn.execute(
                    "UPDATE turns SET reason_code = ?, memory_status = coalesce(memory_status, 'skipped'),"
                    " accounting = ? WHERE run_id = ?",
                    ("cancelled" if revoked else "interrupted",
                     json.dumps(_accounting(conn, run_id, complete=run_id not in in_flight)), run_id))

        await self._write(record)
        if kick:
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

    async def resume(self) -> None:
        """Admit again after a shutdown the database outlives (a restore that did not go ahead).
        Work still running stays this harness's; background runs it stopped start again."""
        self.registry.closed = False
        await self.kick_background()

    # Cancel

    def _request_cancel(self, active: ActiveRun, reason: str) -> None:
        """Cancel a run once; repeating it changes nothing, so a cancellation in progress
        is never interrupted. A background run stopping for a shutdown it would restart after
        (a refused restore) is the exception: a later Cancel or revocation becomes its reason,
        without interrupting its cleanup, so it ends instead. A claim whose response never
        started has run nothing: it is released and recorded at once. A background run whose
        task is still to come (its record being written, see record_background) stays held:
        its task sees the request as it starts, and ends it."""
        if active.cancel_requested.is_set():
            if active.kind == "background" and active.cancel_reason == "shutdown" and reason != "shutdown":
                active.cancel_reason = reason
            return
        active.cancel_reason = reason
        active.cancel_requested.set()
        if active.task is not None:
            if active.started:
                active.task.cancel()
            # else it sees the request as it starts, and ends through its own cleanup
        elif active.kind == "turn" and self.registry.runs.get(active.run_id) is active:
            self.registry.release(active)
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
        return any(active.provider == name for active in list(self.registry.runs.values()))  # changed on other threads

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
        message = json.loads(message)
        return await self.admit_turn(conversation_id, message.get("text", ""), model=model, provider=provider,
                                     effort=effort, retry_of=run_id, material_ids=message.get("material_ids"))

    async def admit_turn(self, conversation_id: str, message, *, model=None, provider=None, effort=None,
                         retry_of=None, material_ids=None) -> ActiveRun:
        """Validate a message and admit its turn, or raise AdmissionError having written nothing.
        material_ids is the papers it selects (the Library's selection or @-mentions): its literature is
        restricted to them."""
        if material_ids is not None and (not isinstance(material_ids, list) or len(material_ids) > MAX_SELECTION
                                         or not all(isinstance(m, str) for m in material_ids)):
            raise AdmissionError(400, "selection_invalid", "Choose at most 50 papers")
        material_ids = list(dict.fromkeys(material_ids)) if material_ids else None
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
            return await self._admit(claim, message, model=model, provider=provider, effort=effort, retry_of=retry_of,
                                     material_ids=material_ids)
        except BaseException:
            self.registry.release(claim)
            raise

    async def _admit(self, claim, message, *, model, provider, effort, retry_of, material_ids=None):
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
            # What the project allows (its level, the review lock, the Private allowlist and the
            # declared local servers); the admission transaction checks the level and lock again.
            policy = await self._read(lambda conn: governance.policy(conn, project_id))
            if policy is None:
                raise AdmissionError(404, "not_found", "No such conversation")
            if policy.locked:  # M1: every generative function, until the venue rules relax it
                raise AdmissionError(403, "review_locked", "This project is review-locked")
            # A model asked for must be named; a blank default in a settings file is passed over.
            chosen = visible(model) or visible(project_settings.values.get("models", {}).get("default")) \
                or visible(personal.values["models"]["default"]) or "auto"
            configured = providers.configured(self.data_dir, personal)
            if not configured:
                raise AdmissionError(400, "no_provider", "No model provider is set up")
            provider_name = provider or _default_provider(configured, policy)
            if provider_name not in configured:
                raise AdmissionError(400, "unknown_provider", "That provider is not set up")
            provider_config = configured[provider_name]
            claim.provider = provider_name
            # The models the provider offers (Recommended, All or Pick): a model it does not
            # offer is refused, and Auto picks among those it does.
            table = (personal.values.get("providers") or {}).get(provider_name) or {}
            if not policy.allows_provider(provider_config):  # none of its models, whichever is asked for
                raise AdmissionError(403, policy.problem(provider_config, chosen),
                                     "This project's protection does not allow that provider")
        # The key is read outside the lock (a credential store may ask the researcher first);
        # it cannot change meanwhile, since key changes are refused while claim.provider is set,
        # and neither can the provider's settings, so the plan is made from the snapshot.
        key = await asyncio.to_thread(credentials.load_key, self.data_dir, provider_name, self.keyring_backend)
        if key is None:
            raise AdmissionError(400, "provider_key_missing", "The provider has no key")
        zero_retention = policy.zero_retention and provider_config.is_openrouter
        # Which models OpenRouter serves with zero retention now; and a catalog never read, whose
        # windows nothing here knows yet (status.md's S1-19 follow-up).
        if (zero_retention and catalog_status(provider_config, key)["stale"]) \
                or not catalog_read(providers.Route(provider_config, chosen)):
            await self.catalog(provider_config, key)
        if chosen != budget_router.AUTO and (problem := _unusable(table, provider_config, chosen)):
            raise AdmissionError(400, problem, _REFUSED[problem])

        def plan_among(allowed):
            return budget_router.create_run_plan(
                message, chosen, is_openrouter=provider_config.is_openrouter,
                offered=lambda m: _unusable(table, provider_config, m) is None and allowed(m),
                picked=table["models"] if isinstance(table.get("models"), list) else ())

        plan = plan_among(lambda m: policy.problem(provider_config, m, key) is None)
        # Before anything is written or sent: a model the project does not allow is refused, and
        # when Auto found none, the project's rule is named if it ruled out the one Auto would pick.
        model_for_problem = plan.model or plan_among(lambda m: True).model
        if model_for_problem is None and not catalog_read(providers.Route(provider_config, chosen)):
            raise AdmissionError(400, "catalog_unread", _REFUSED["catalog_unread"])  # Auto cannot tell
        if model_for_problem is None:
            raise AdmissionError(400, "model_needed", "Choose a model for this provider")
        if problem := policy.problem(provider_config, model_for_problem, key):
            raise AdmissionError(403, problem, "This project's protection does not allow that model")
        if zero_retention and not await self._read(lambda conn: governance.key_attested(
                conn, self.data_dir, provider_config.name, key)):
            raise AdmissionError(403, "key_not_confirmed", "Confirm the key's OpenRouter data settings first")
        route = providers.Route(provider_config, plan.model)
        problem, facts = await self._route_facts(route, key, table, zero_retention=zero_retention, effort=effort)
        tried = {plan.model}
        while problem and chosen == budget_router.AUTO:
            # Auto passes over a model its endpoints cannot serve (unpriced, unread, too small) to the next
            # it would take; with none left, the first model's problem is named.
            following = plan_among(lambda m: m not in tried and policy.problem(provider_config, m, key) is None)
            if following.model is None:
                break
            tried.add(following.model)
            candidate = providers.Route(provider_config, following.model)
            found, found_facts = await self._route_facts(candidate, key, table, zero_retention=zero_retention,
                                                         effort=effort)
            if not found:
                plan, route, problem, facts = following, candidate, None, found_facts
        if problem:
            raise AdmissionError(400, problem, _REFUSED[problem])
        instructions, _ = await asyncio.to_thread(load_instructions, self.data_dir, project_id)
        budgets = personal.values["context"]

        def admit(conn):
            moved = conn.execute("SELECT project_id FROM conversations WHERE id = ?", (claim.conversation_id,)).fetchone()
            if moved is not None and moved[0] != project_id:  # moved since it was read: its settings were another's
                raise AdmissionError(409, "conversation_moved", "The conversation moved to another project; send again")
            level = conn.execute("SELECT sensitivity, review_lock FROM projects WHERE id = ?", (project_id,)).fetchone()
            if level is not None and (level[0], bool(level[1])) != (policy.level, policy.locked):
                raise AdmissionError(409, "project_changed", "The project's protection changed; send again")
            if claim.cancel_requested.is_set():  # stopped while it was admitted (shutdown): write nothing
                raise AdmissionError(503, "shutting_down", "The app is closing") if claim.cancel_reason == "shutdown" \
                    else AdmissionError(409, "cancelled", "The message was cancelled")
            if not conn.execute("SELECT 1 FROM conversations WHERE id = ?", (claim.conversation_id,)).fetchone():
                raise AdmissionError(404, "not_found", "No such conversation")
            (seq,) = conn.execute("SELECT coalesce(max(seq) + 1, 0) FROM turns WHERE conversation_id = ?",
                                  (claim.conversation_id,)).fetchone()
            # Only succeeded turns' answers: a failed, cancelled or revoked turn's text never enters a
            # later request as the assistant's words (section 1.5). Newest first; an earlier answer's
            # citations are marked historical, not current evidence (the saved answer is unchanged).
            # An earlier answer may quote the evidence its turn held, so it is carried only while every paper
            # its turn depended on (inputs.context_materials) exists, and those papers are this request's too
            # (backend/provenance.py: the rule for what text may be sent once its source is deleted).
            earlier = [(u, a, json.loads(held or "[]")) for u, a, held in conn.execute(
                "SELECT t.user_message, t.answer, json_extract(r.inputs, '$.context_materials') FROM turns t"
                " JOIN runs r ON r.id = t.run_id WHERE t.conversation_id = ? AND r.status = 'succeeded'"
                " AND t.answer IS NOT NULL ORDER BY t.seq DESC", (claim.conversation_id,))]
            kept_turns = [turn for turn in earlier if provenance.carried(conn, turn[2])]
            history_left_out = len(earlier) - len(kept_turns)  # traced with the answer's step
            history = [(json.loads(u).get("text", ""), historical_citations(json.loads(a).get("text", "")))
                       for u, a, _ in kept_turns]
            history_materials = [held for _, _, held in kept_turns]  # newest first, as history is
            if material_ids and conn.execute(
                    "SELECT count(*) FROM materials WHERE project_id = ? AND id IN (SELECT value FROM json_each(?))",
                    (project_id, json.dumps(material_ids))).fetchone()[0] != len(material_ids):
                raise AdmissionError(400, "selection_invalid", "A selected paper is not in this project")
            has_materials = conn.execute("SELECT 1 FROM materials WHERE project_id = ? LIMIT 1",
                                         (project_id,)).fetchone() is not None
            decision = router.decide(message, selected=bool(material_ids), has_materials=has_materials,
                                     small_model=router_model is not None,
                                     deadline_ms=personal.values["retrieval"]["rules_ms"])
            try:  # the request without its evidence: one whose own parts do not fit is refused before anything is written
                request = context.assemble(
                    "answer", window=facts["window"], output=_output(budgets["output_limit"], facts), budgets=budgets,
                    margin_percent=budgets["margin_percent"], system_rules=SYSTEM_RULES, request=message,
                    sections={"agents_md": [instructions] if instructions else []}, history=history)
            except context.OverWindow as over:  # before anything is written or sent
                raise AdmissionError(400, over.code, _REFUSED[over.code]) from None
            plan.predicted_cost = budget_router.request_cost(facts["rates"], request.input_tokens, request.max_tokens)
            conn.execute(
                "INSERT INTO runs (id, project_id, conversation_id, kind, workflow, limits, inputs)"
                " VALUES (?, ?, ?, 'turn', 'agent', ?, ?)",
                (claim.run_id, project_id, claim.conversation_id,
                 json.dumps({"model_call_seconds": MODEL_CALL_SECONDS}),
                 json.dumps({"route": route.key, "effort": effort,
                             **({"material_ids": material_ids} if material_ids else {})})))
            if retry_of is not None and conn.execute(
                    "SELECT 1 FROM turns WHERE run_id = ? AND seq = ? - 1", (retry_of, seq)).fetchone() is None:
                raise AdmissionError(409, "not_continuable", "A newer turn was sent meanwhile")
            conn.execute(
                "INSERT INTO turns (run_id, conversation_id, seq, author, user_message, retry_of_run_id)"
                " VALUES (?, ?, ?, 'researcher', ?, ?)",
                (claim.run_id, claim.conversation_id, seq,
                 json.dumps({"text": message, **({"material_ids": material_ids} if material_ids else {})}), retry_of))
            _event(conn, claim.run_id, "route", {"route": route.key, "effort": facts["effort"], "plan": plan.to_dict(),
                                                 "window": facts["window"], "endpoints": facts["endpoints"],
                                                 "retrieval": decision})
            return seq, request, history, decision, (history_materials, history_left_out)

        router_model = visible(personal.values["models"].get("router"))
        (seq, request, history, decision, (history_materials, history_left_out)), cancelled = \
            await self._write_through(admit)
        if cancelled:  # its request went away while the run was written: it never runs, as by Stop
            await _through(self._write(lambda conn: self._finish_turn(
                conn, claim.run_id, "cancelled", "researcher", "cancelled")))
            raise asyncio.CancelledError()
        claim.context = {
            "project_id": project_id, "seq": seq, "route": route, "key": key, "request": request, "facts": facts,
            "estimate": plan.predicted_cost, "zero_retention": zero_retention, "message": message,
            "selection": material_ids, "decision": decision, "history": history, "budgets": budgets,
            "instructions": instructions, "personal": personal, "table": table, "policy": policy,
            "provider": provider_config, "router_model": router_model, "history_materials": history_materials,
            "history_left_out": history_left_out,
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
            decision = ctx["decision"]
            if decision["decided_by"] == "router":  # no rule fired, and a small model is set
                decision = await self._router_step(claim, ctx)
            await handed({"type": "route", "route": decision["route"], "decided_by": decision["decided_by"]})
            found = {"records": [], "mode": None, "reason": None}
            if decision["route"] in RETRIEVAL_ROUTES:
                found = await self._evidence(claim, ctx)
            numbered, request = self._cited_request(ctx, found, decision)
            await self._register_context(claim, numbered)  # the dispatch check now covers its passages
            judge = await self._judge_route(ctx)
            # The judge's share is reserved with the answer: a budget that cannot hold both refuses the step
            # before the answer call (section 6.2, final review F02; C5). The share is a typical check's cost
            # (citations.typical_judge_input, Harold's option C of 2026-10-21) at the judge route's highest rate
            # and the least output limit, held until the judge's calls take it; a check that needs more draws on
            # the answer's unspent reservation, then the budget's room (_reserve).
            judge_estimate = 0.0
            if numbered and judge[1] is not None:
                judge_estimate = budget_router.request_cost(judge[2]["rates"], citations.typical_judge_input(
                    [r["text"] for r in numbered.values()], ctx["personal"].values["retrieval"]["judge_pairs"]),
                    context.MIN_OUTPUT)
            estimate = budget_router.request_cost(ctx["facts"]["rates"], request.input_tokens, request.max_tokens)
            call = await self._reserve(claim, ctx["project_id"], claim.conversation_id, estimate, allowance=judge_estimate,
                                       phase="answer", step={**_step(ctx["route"], ctx["facts"], request),
                                                             "judge_estimate_usd": judge_estimate,
                                                             # earlier answers left out: one of their papers is deleted
                                                             **({"history_left_out": ctx["history_left_out"]}
                                                                if ctx["history_left_out"] else {})})
            await handed({"type": "step", "seq": call.step, "phase": "answer"})
            result = await self._call(claim, call, ctx["route"], ctx["key"], request, ctx["facts"],
                                      zero_retention=ctx["zero_retention"], materials=ctx["context_materials"],
                                      rehold=(ctx, await self._budgets(ctx["project_id"])) if judge_estimate else None)
            if not result.ok or visible(result.content) is None:  # tool calls alone: no tools were offered
                code = result.error_kind or "malformed"
                final = await self._write(lambda conn: self._finish_turn(conn, claim.run_id, "failed", None,
                                                                          code, claim=claim))
                if final["status"] == "failed":  # not a Stop that came first
                    emit({"type": "error", "code": code})
                emit({"type": "run_finished", **final})
                return
            # The delivered answer's checks, before its primary commit: existence and mapping, then the judge.
            checked = await self._read(lambda conn: citations.check_existence(
                conn, ctx["project_id"], result.content, numbered))
            verdicts, outcome = await self._support(claim, ctx, checked, judge)
            ctx["cited"] = (result.content, numbered, verdicts)  # checked again in the primary commit
            ctx["check_outcome"] = outcome
            unsearched = (found.get("trace") or {}).get("index") in ("building", "unavailable")
            notices = [n for n, shown in (("index_unavailable", unsearched),
                                          ("keyword_only", found["mode"] == "keyword_only" and not unsearched),
                                          ("unchecked", outcome["failed"] > 0),
                                          ("unchecked_budget", outcome["budget"] > 0),
                                          ("skills_unavailable", not ctx["facts"].get("tools")),
                                          ("retracted", any(c.get("retraction") == "retracted" for c in checked)))
                       if shown]
            answer = {"text": result.content, **({"reasoning": result.reasoning} if result.reasoning else {}),
                      **({"cut": True} if result.cut else {}),  # saved, and marked as stopped at its output limit
                      **({"notices": notices} if notices else {})}
            try:
                final = await self._write(lambda conn: self._commit_answer(conn, claim, answer, ctx))
            except _Cancelled:  # the Stop came first: no answer is published
                raise asyncio.CancelledError() from None
            except Exception as error:  # the answer could not be saved: it is still shown, marked unsaved
                log.error("the answer could not be saved (%s at %s)", type(error).__name__, _where(error))
                emit({"type": "chat_response", "content": result.content, "reasoning": result.reasoning,
                      "result_saved": False, "error": "save_failed", **({"cut": True} if result.cut else {}),
                      **({"notices": answer["notices"]} if answer.get("notices") else {})})
                with suppress(Exception):
                    final = await _through(self._write(lambda conn: self._finish_turn(
                        conn, claim.run_id, "failed", None, "save_failed", claim=claim)))
                    emit({"type": "run_finished", **final[0]})
                return
            emit({"type": "chat_response", "content": result.content, "reasoning": result.reasoning,
                  "result_saved": True, **({"cut": True} if result.cut else {}),
                  **({"notices": answer["notices"]} if answer.get("notices") else {})})
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
        if _revoked(conn, run_id):  # revoked in the record, which this process may not have heard yet
            status, cancel_reason, reason_code, limit = "cancelled", "revoked", "cancelled", None
        _release_allowances(conn, run_id)
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
        if not _running(conn, claim.run_id) or _revoked(conn, claim.run_id):  # deleted, ended or revoked: nothing to publish
            raise _Cancelled()
        _release_allowances(conn, claim.run_id)
        cost = spending.run_cost(conn, claim.run_id)
        # Existence and mapping again, in this transaction: a passage re-read or removed while the judge ran is
        # mapped by its quote or marked source_removed (its verdict then unchecked), never a row that fails.
        text, numbered, verdicts = ctx.get("cited") or ("", {}, {})
        checked = [{**c, "id": new_id()} for c in citations.apply_verdicts(
            citations.check_existence(conn, ctx["project_id"], text, numbered), verdicts)]
        now = utc_now()
        citations.write_rows(conn, claim.run_id, checked, now)
        # By id, never the sentence: an event cannot lose text when its source is deleted (backend/provenance.py);
        # the trace reads each sentence from its citation row (backend/answers.py).
        _event(conn, claim.run_id, "citation_checks", {"citations": [
            {k: c.get(k) for k in ("id", "marker", "existence", "support", "passage_id")} for c in checked],
            # every pair's outcome, counted: judged, failed (the judge failed, timed out, refused or left it
            # unanswered, or it fits no judge window) and unchecked for budget
            **({"pairs": ctx["check_outcome"]} if ctx.get("check_outcome") else {})})
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

    async def _reserve(self, active, project_id, paying_conversation_id, estimate, *, phase, step, allowance=0.0,
                       from_allowance=False):
        """Admit one model call: its reservation and step_started event (with step, what _step
        records of its route and request), against the budgets as they are now (read under
        settings_lock). A cancellation during the write releases the reservation once it is written.
        allowance: an amount held beside the call, in the same transaction and against the same budgets,
        for a later call of the step (the support judge's share, reserved with its answer, final review
        F02); from_allowance: a judge call, which takes what is held for it, in this order (Harold's
        option C, 2026-10-21): the share, then the answer's unspent reservation (held again when the
        answer settled, _call's rehold), then the budget's room; both held amounts are released and the
        call reserved in one transaction, against the budgets, so nothing else can take them meanwhile;
        any rest is held again, and its step records what funded it ("funded": share, answer_unspent or
        budget). No reservation is ever written past a budget: one that does not fit raises
        BudgetExceeded, writing nothing. An allowance left unspent is released when the turn ends
        (_release_allowances)."""
        run_id = active.run_id
        context = active.context if active.context is not None else {}

        def reserve(conn):
            if not _running(conn, run_id) or _revoked(conn, run_id):  # deleted, ended or revoked since it was admitted
                raise _Cancelled()
            (own,) = conn.execute("SELECT budget_usd FROM conversations WHERE id = ?",
                                  (paying_conversation_id,)).fetchone() or (None,)
            (seq,) = conn.execute("SELECT count(*) FROM run_events WHERE run_id = ? AND type = 'step_started'",
                                  (run_id,)).fetchone()
            budgets = {"project_budget_usd": project_budget,
                       "conversation_budget_usd": own if own is not None else default_budget}
            held, funded = {}, {}
            for name in ("allowance", "unspent") if from_allowance else ():
                held_id = context.get(name)
                row = held_id and conn.execute(
                    "SELECT estimate_usd FROM budget_reservations WHERE id = ? AND status = 'open'", (held_id,)).fetchone()
                if row and spending.release(conn, held_id):
                    held[name] = row[0]
            reservation = spending.reserve(conn, run_id=run_id, step_seq=seq, project_id=project_id,
                                           paying_conversation_id=paying_conversation_id, estimate_usd=estimate,
                                           **budgets)
            if from_allowance:
                share, unspent = held.get("allowance", 0.0), held.get("unspent", 0.0)
                funded = {"funded": "share" if estimate <= share else
                          "answer_unspent" if estimate <= share + unspent else "budget"}
            rest = allowance if not from_allowance else sum(held.values()) - estimate
            kept = None
            if rest > 0:
                try:
                    kept = spending.reserve(conn, run_id=run_id, step_seq=seq, project_id=project_id,
                                            paying_conversation_id=paying_conversation_id, estimate_usd=rest, **budgets)
                except spending.BudgetExceeded:
                    if not from_allowance:
                        raise  # the share must fit with its answer (C5)
                    kept = None  # a budget lowered meanwhile: the rest is not held again; this call still runs
            _event(conn, run_id, "step_started", {"step": seq, "phase": phase, "estimate_usd": estimate,
                                                   "reservation_id": reservation, **step, **funded,
                                                   **({"allowance_id": kept, "allowance_usd": rest} if kept else {})})
            return _Call(run_id, seq, reservation), kept

        async with self.settings_lock:
            project_budget, default_budget = await self._budgets(project_id)
            try:
                (call, kept), cancelled = await self._write_through(reserve)
            except _Cancelled:
                raise asyncio.CancelledError() from None
        active.call = call
        if allowance or from_allowance:
            context["allowance"] = kept
        if from_allowance:
            context["unspent"] = None  # taken with the share
        if cancelled or active.cancel_requested.is_set():
            raise asyncio.CancelledError()
        return call

    # S1-19: the router's small model, the evidence, the cited request and the support judge

    async def _role_call(self, claim, ctx, model, function, rules, text, *, timeout=None, from_allowance=False,
                         materials=(), facts=None):
        """One role call (the router's small model, the support judge) on the turn's provider: model, or the
        turn's own route when None; on the project's allowed routes, through the dispatch path, never
        forcing a tool choice. materials: the papers whose passages text holds. Returns the call's result, or
        None when the route cannot serve it (not allowed, unusable, too small for the request)."""
        # The turn's route and privacy controls, but not its reasoning effort: a role step's output limit is the
        # least a step has, which reasoning tokens would use up. Without reasoning the request reaches the
        # endpoints a request without it reaches (no reasoning pin), so its facts are read for that request:
        # given by the caller who planned with them (the judge's share, _judge_route), else read here.
        route = ctx["route"]
        if model is not None:
            if ctx["policy"].problem(ctx["provider"], model, ctx["key"]) or \
                    _unusable(ctx["table"], ctx["provider"], model):
                return None
            route = providers.Route(ctx["provider"], model)
        if facts is None:
            problem, facts = await self._route_facts(route, ctx["key"], ctx["table"],
                                                     zero_retention=ctx["zero_retention"], effort=None)
            if problem:
                return None
        budgets = ctx["budgets"]
        try:
            request = context.assemble(function, window=facts["window"], output=_output(context.MIN_OUTPUT, facts),
                                       budgets=budgets, margin_percent=budgets["margin_percent"], system_rules=rules,
                                       request=text)
        except context.OverWindow:
            return None
        estimate = budget_router.request_cost(facts["rates"], request.input_tokens, request.max_tokens)
        call = await self._reserve(claim, ctx["project_id"], claim.conversation_id, estimate, phase=function,
                                   step=_step(route, facts, request), from_allowance=from_allowance)
        return await self._call(claim, call, route, ctx["key"], request, facts, zero_retention=ctx["zero_retention"],
                                timeout=timeout, materials=materials)

    async def _router_step(self, claim, ctx):
        """The small model's route, within `router_ms` (section 7.3); on failure, timeout or refusal, the
        fallback. Recorded as a route event either way."""
        decision = dict(ctx["decision"])
        deadline = ctx["personal"].values["retrieval"]["router_ms"] / 1000
        result = await self._role_call(claim, ctx, ctx["router_model"], "route", router.ROUTER_RULES, ctx["message"],
                                       timeout=deadline)
        chosen = router.parse(result.content) if result is not None and result.ok else None
        if chosen is None:
            has_materials = await self._read(lambda conn: conn.execute(
                "SELECT 1 FROM materials WHERE project_id = ? LIMIT 1", (ctx["project_id"],)).fetchone() is not None)
            decision.update(route=router.fallback(has_materials), decided_by="fallback",
                            reason="unavailable" if result is None else (result.error_kind or "unparsed"))
        else:
            decision.update(route=chosen, decided_by="router")

        def record(conn):
            if not _running(conn, claim.run_id) or _revoked(conn, claim.run_id):
                raise _Cancelled()
            _event(conn, claim.run_id, "route", {"retrieval": decision})
        try:
            await self._write(record)
        except _Cancelled:
            raise asyncio.CancelledError() from None
        return decision

    async def _evidence(self, claim, ctx):
        """The pipeline's evidence for the turn (backend/pipeline.py), traced. Nothing when the app has no
        search index (keyword-only and unavailable, said)."""
        from backend import pipeline, search  # search imports this module
        state = search._STATES.get(self)
        if state is None:
            found = {"records": [], "mode": "keyword_only", "reason": "index_unavailable",
                     "trace": {"index": "unavailable"}}
        else:
            found = await pipeline.evidence(state, ctx["project_id"], ctx["message"], materials=ctx["selection"])

        def record(conn):
            if not _running(conn, claim.run_id) or _revoked(conn, claim.run_id):
                raise _Cancelled()
            _event(conn, claim.run_id, "retrieval_trace", {**found["trace"], "mode": found["mode"],
                                                           "reason": found["reason"]})
        try:
            await self._write(record)
        except _Cancelled:
            raise asyncio.CancelledError() from None
        return found

    def _cited_request(self, ctx, found, decision):
        """The answer step's request with its evidence and notes, and the evidence records it holds,
        numbered as the request numbers them (records the capacity policy left out are not in it)."""
        numbered = citations.sources(found["records"])
        lines = citations.bundle(numbered)
        budgets, facts = ctx["budgets"], ctx["facts"]
        index = (found.get("trace") or {}).get("index", "ready")

        def assemble(evidence, held, cut=False):
            return context.assemble(
                "answer", window=facts["window"], output=_output(budgets["output_limit"], facts), budgets=budgets,
                margin_percent=budgets["margin_percent"], system_rules=SYSTEM_RULES, request=ctx["message"],
                sections={"agents_md": [ctx["instructions"]] if ctx["instructions"] else [], "research_evidence": evidence,
                          "conflicts": citations.missing(held, mode=found["mode"], route=decision["route"], index=index,
                                                         cut=cut)},
                history=ctx["history"])
        request = assemble(lines, numbered)
        kept = len(lines) - request.cuts.get("research_evidence", 0)
        ctx["history_kept"] = len(ctx["history"]) - request.cuts.get("history", 0)  # newest first, as they fit
        if lines and not kept:  # the window left no room for any of it: the request says so, not "none matched"
            request = assemble([], {}, cut=True)
            request.cuts["research_evidence"] = len(lines)
            ctx["history_kept"] = len(ctx["history"]) - request.cuts.get("history", 0)
        return dict(list(numbered.items())[:kept]), request

    async def _register_context(self, claim, numbered):
        """Name every paper whose text the request carries on its run, before its dispatch (backend/provenance.py):
        its evidence's and its history's (inputs.context_materials, also its scope in inputs.material_ids, so
        deleting one revokes the run), and its evidence's [passage, paper] pairs (inputs.evidence); the dispatch
        check refuses a run when one of those papers is gone or no longer reads the passage."""
        papers = sorted({r["material_id"] for r in numbered.values()})
        pairs = sorted({(r["passage_id"], r["material_id"]) for r in numbered.values()})
        # every paper whose text the request carries: its evidence, and the earlier turns its history holds
        shown = claim.context["history_materials"][:claim.context.get("history_kept", 0)]
        held = sorted({*papers, *(m for turn in shown for m in turn)})
        claim.context["context_materials"] = held
        if not held:
            return

        def record(conn):
            if not _running(conn, claim.run_id) or _revoked(conn, claim.run_id):
                raise _Cancelled()
            (inputs,) = conn.execute("SELECT inputs FROM runs WHERE id = ?", (claim.run_id,)).fetchone()
            inputs = json.loads(inputs or "{}")
            inputs["material_ids"] = sorted({*inputs.get("material_ids", []), *held})
            inputs["evidence"] = [list(pair) for pair in pairs]
            inputs["context_materials"] = held
            conn.execute("UPDATE runs SET inputs = ? WHERE id = ?", (json.dumps(inputs), claim.run_id))
        try:
            await self._write(record)
        except _Cancelled:
            raise asyncio.CancelledError() from None

    async def _judge_route(self, ctx):
        """(model or None, route, facts) for the support judge: the turn's own route and privacy controls
        when `[models] judge` is unset (ticket 71); the set model when the project allows it and it can
        run; (None, None, None) when it is set but cannot run here, so the citations stay unchecked, said
        in the answer, and no other model judges them in its place."""
        model = visible(ctx["personal"].values["models"].get("judge"))
        if model is None:  # the turn's route, read for the judge's own request (no reasoning: no reasoning pin)
            problem, facts = await self._route_facts(ctx["route"], ctx["key"], ctx["table"],
                                                     zero_retention=ctx["zero_retention"], effort=None)
            return (None, ctx["route"], facts) if not problem else (None, None, None)
        if not ctx["policy"].problem(ctx["provider"], model, ctx["key"]) and \
                not _unusable(ctx["table"], ctx["provider"], model):
            route = providers.Route(ctx["provider"], model)
            problem, facts = await self._route_facts(route, ctx["key"], ctx["table"],
                                                     zero_retention=ctx["zero_retention"], effort=None)
            if not problem:
                return model, route, facts
        return None, None, None

    async def _support(self, claim, ctx, checked, judge):
        """({(sentence, passage id): verdict}, {"judged", "failed", "budget": pairs}). Every pair is judged, up to
        `judge_pairs` per call, a batch too large for the judge's window halved (section 7.5), each sentence as
        delivered (section 6.2). Each call is reserved from the judge's share, then the answer's unspent
        reservation, then the budget's room (_reserve, one transaction, against the budgets); a call none of
        them covers is not made, and its pairs and the rest are unchecked for budget. A call that fails, times
        out or refuses, a pair it leaves unanswered, or one too large for any window, is unchecked: failed."""
        wanted = citations.pairs(checked)
        outcome = {"judged": 0, "failed": 0, "budget": 0}
        if not wanted:
            return {}, outcome
        if judge[1] is None:  # a judge is set that cannot run here: unchecked, said, never another model
            return {}, {**outcome, "failed": len(wanted)}
        texts = {c["passage_id"]: c["judged_text"] for c in checked if c["existence"] == "ok"}
        material_of = {c["passage_id"]: c["material_id"] for c in checked if c["existence"] == "ok"}
        verdicts = {}
        queue = citations.batches(wanted, ctx["personal"].values["retrieval"]["judge_pairs"])
        while queue:
            batch = queue.pop(0)
            try:
                result = await self._role_call(claim, ctx, judge[0], "judge", citations.JUDGE_RULES,
                                               citations.judge_request(batch, texts), timeout=JUDGE_SECONDS,
                                               from_allowance=True, facts=judge[2],
                                               materials={*ctx["context_materials"], *(material_of[pid] for _, pid in batch)})
            except spending.BudgetExceeded:  # neither the share, the answer's unspent nor the budget covers it
                outcome["budget"] += len(batch) + sum(len(rest) for rest in queue)
                break
            if result is None and len(batch) > 1:  # too large for the judge's window: halved
                queue[:0] = [batch[:len(batch) // 2], batch[len(batch) // 2:]]
                continue
            if result is None or not result.ok:
                outcome["failed"] += len(batch)
                continue
            found = citations.parse_verdicts(result.content, len(batch))
            outcome["judged"] += len(found)
            outcome["failed"] += len(batch) - len(found)
            verdicts.update({pair: found[i] for i, pair in enumerate(batch, start=1) if i in found})
        return verdicts, outcome

    async def _general(self):
        return await self._read(lambda conn: conn.execute("SELECT id FROM projects WHERE kind = 'general'").fetchone()[0])

    async def catalog(self, provider, key, *, force=False, generation=None):
        """The provider's models (openrouter_client.models), read through the gate under the
        General project, which stays Normal: a catalog request holds no project's content."""
        async with self.gate.async_client(await self._general()) as client:
            return await openrouter_client.models(client, provider, key, force=force, generation=generation)

    async def _route_facts(self, route, key, table, *, zero_retention, effort):
        """(None, facts) for a request on route, or (a problem code, None). facts: the window W every
        endpoint the request can reach accepts (with the window settings), the highest rates among
        them, the effort it is sent with, their tags, and the catalog row its payload is built from
        (the M2 charter's model-route rules 1, 3 and 6; backend/reach.py). OpenRouter's endpoint
        listing for the model is read, like the catalog, through the gate under the General project."""
        row = get_model_metadata(route, key) if zero_retention else get_model_metadata(route)
        listing = openrouter_client.cached_endpoints(route.provider, route.model)
        if route.provider.is_openrouter and listing is None:
            async with self.gate.async_client(await self._general()) as client:
                listing = await openrouter_client.endpoints(client, route.provider, route.model)
        reasoning, pin = openrouter.resolve_model_reasoning(route, effort, model_entry=row, zdr_enabled=zero_retention)
        # The output limit is always relied on, so every endpoint must take max_tokens (rule 2).
        needs = ("max_tokens", "reasoning") if reasoning else ("max_tokens",)
        found = reach.reach(route.provider, row, listing, zero_retention=zero_retention, pin=pin, needs=needs)
        if reasoning and found.problem == "route_unsupported":
            # Its reasoning record does not name exactly the endpoints it would reach (rule 1): sent without.
            effort = None
            found = reach.reach(route.provider, row, listing, zero_retention=zero_retention, needs=("max_tokens",))
        if found.problem == "route_unverified":  # read the listing again next time, not an hour later
            openrouter_client.forget_endpoints(route.provider, route.model)
        if found.problem:
            return found.problem, None
        window = providers.window(table, route.model, found.window)
        setting = (table.get("windows") or {}).get(route.model) or table.get("default_window")
        if window["status"] != "ok" or (found.unreported and not setting):  # an endpoint's window is unknown
            return "model_window", None
        if found.output is not None and found.output < context.MIN_OUTPUT:  # cannot take the least output limit
            return "route_unsupported", None
        return None, {"window": window["in_use"], "rates": found.rates, "effort": effort,
                      "endpoints": list(found.tags), "row": row, "output": found.output, "tools": found.tools}

    async def _call(self, active, call, route, key, request, facts, *, output=None, zero_retention=False, timeout=None,
                    materials=(), rehold=None):
        """The dispatch path: every model call goes through here, and nowhere else calls the provider
        adapter (tests/test_dispatch_path.py). It sends the request the context assembler built, for
        an admitted call (its reservation), through the gate with the dispatch check, and records it,
        settling its reservation. facts are what _route_facts found for the route. zero_retention
        sends it with provider.zdr = true (a Private project's OpenRouter call). Each attempt is
        recorded with the retention terms the gate applied when it let it out. output(result), if
        given, is recorded on the finished step, so a run can later finish from the record. timeout bounds
        the call (MODEL_CALL_SECONDS by default). Each attempt's exact request body goes to the content store
        (owner-only), for the trace's exact outbound text: its model_attempt event names it (body), and a
        run_bodies row keeps it with the papers whose passages it holds (materials, run_body_materials), so
        deleting one of them deletes it (backend/db/deletion.py). A body of a paper deleted before it is
        recorded is not kept."""
        if not isinstance(request, context.Request):
            raise TypeError("a model request is built by the context assembler")
        if context.serialized_tokens(request.messages, request.tools) + request.max_tokens > request.window:
            raise ValueError("a model request must fit its window")  # counted again, as it is sent
        if active.cancel_requested.is_set():
            raise asyncio.CancelledError()
        project_id = await self._read(lambda conn: conn.execute(
            "SELECT b.project_id FROM budget_reservations b JOIN runs r ON r.id = b.run_id"
            " WHERE b.id = ? AND b.status = 'open' AND r.status = 'running'", (call.reservation_id,)).fetchone())
        if project_id is None:  # its run, conversation or project was deleted (or it ended) since admission
            raise asyncio.CancelledError()
        def dispatched(terms=None):  # the gate let the request out: from here it may be billed
            call.dispatched, call.retention = True, terms

        async def keep(body):  # stored, and linked to its papers, before it is sent: a stop or a crash keeps it
            ref = await asyncio.to_thread(_stored, self.content, body)
            number = len(call.bodies)
            call.bodies.append(ref)
            if ref is not None:
                await self._write(lambda conn: _keep_body(conn, call, number, ref))

        call.materials = tuple(sorted(set(materials)))

        call.route = route.key
        async with self.gate.async_client(project_id[0], admit=lambda conn: may_dispatch(conn, active.run_id),
                                          provider=route.provider.name) as client:
            result = await openrouter.query_model(
                client, route, key, request.messages, timeout=timeout or MODEL_CALL_SECONDS, effort=facts["effort"],
                max_tokens=request.max_tokens, zdr_enabled=zero_retention, on_dispatch=dispatched, on_body=keep,
                # The catalog row the route's facts were read from, so the request is the one they
                # describe; a Private call is limited to the endpoints it lists, as the gate checks.
                model_entry=facts["row"])
            finished = {"step": call.step, "outcome": result.error_kind or "ok",
                        **({"cut": "output_limit"} if result.cut else {})}  # stopped at its output limit
            if output is not None and result.ok:
                finished["output"] = output(result)

            def record(conn):
                # The receipt settles the reservation by its id even if the run was deleted
                # meanwhile; the run's own record goes with the run.
                exists = conn.execute("SELECT 1 FROM runs WHERE id = ?", (active.run_id,)).fetchone() is not None
                if exists:
                    for number, attempt in enumerate(result.attempts):
                        _event(conn, active.run_id, "model_attempt", {
                            "step": call.step, "route": route.key, **attempt.record(),
                            **_kept_body(conn, call, number, attempt.dispatched)})
                if result.dispatched:
                    _settle_attempts(conn, call.reservation_id, facts["rates"], result.attempts)
                    if rehold is not None and exists:  # the answer's unspent, held again for its judge (_reserve)
                        held = _hold_unspent(conn, call, *rehold)
                        if held:
                            finished.update(allowance_id=held[0], allowance_usd=held[1])
                if exists:
                    _event(conn, active.run_id, "step_finished", finished)
                if not result.dispatched:
                    spending.release(conn, call.reservation_id)

            # Recorded before the client closes, and to its end even if cancelled meanwhile.
            _, cancelled = await self._write_through(record)
        active.call = None
        if cancelled:
            raise asyncio.CancelledError()
        return result

    # Background runs

    async def kick_background(self) -> None:
        """Start every background run that is running in the record but not in this process, but for
        one tried again already (materials.retry_run): its retry does its work."""
        if self.registry.closed:
            return
        rows = await self._read(lambda conn: conn.execute(
            "SELECT id FROM runs WHERE kind = 'background' AND status = 'running'"
            " AND json_extract(inputs, '$.retried_by') IS NULL ORDER BY started_at").fetchall())
        for (run_id,) in rows:
            active = self.registry.add_background(run_id)
            if active is not None:
                active.task = asyncio.create_task(self._background(active))

    async def start_again(self, run_id):
        """Start the one background run run_id if it is recorded running, not tried again (retried_by) and held by no
        process here (left so by a terminal write that failed), as a kick would start it, but this run only."""
        if self.registry.closed:
            return
        if await self._read(lambda conn: conn.execute(
                "SELECT 1 FROM runs WHERE id = ? AND kind = 'background' AND status = 'running'"
                " AND json_extract(inputs, '$.retried_by') IS NULL", (run_id,)).fetchone()) is None:
            return
        active = self.registry.add_background(run_id)
        if active is not None:
            active.task = asyncio.create_task(self._background(active))

    async def _ended(self, active, write):
        """Await a background run's terminal write; once it has committed, the run is known finished
        here (Registry.finished), before it is released. A write that raises marks nothing."""
        result = await write
        self.registry.finished.add(active.run_id)
        return result

    async def _background(self, active: ActiveRun) -> None:
        active.started = True
        index_run = False  # S1-17's index run: started again once if it is left running (see finally)
        try:
            if active.cancel_requested.is_set():  # stopped before it began
                raise asyncio.CancelledError()
            row = await self._read(lambda conn: conn.execute(
                "SELECT r.project_id, r.workflow, r.attempts, r.inputs, r.status, t.user_message, r.cancel_reason"
                " FROM runs r LEFT JOIN turns t ON t.run_id = r.source_turn_id WHERE r.id = ?",
                (active.run_id,)).fetchone())
            if row is None or row[4] != "running" or json.loads(row[3] or "{}").get("retried_by"):
                return  # rule 1: finished (or deleted), or tried again (its retry does its work); never run again
            project_id, workflow, attempts, inputs, _, source, cancel_reason = row
            inputs = json.loads(inputs or "{}")
            if cancel_reason == "revoked":  # revoked while it waited, or before a crash: no call, no effect
                await self._ended(active, self._write(
                    lambda conn: self._finish_background(conn, active, "cancelled", None, inputs)))
                return
            if workflow in self.workflows:  # it returns once its terminal record has committed
                index_run = workflow == "index"
                await self._ended(active, self._local(active, project_id, workflow, inputs))
                return
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
                await self._ended(active, self._write(
                    lambda conn: self._finish_background(conn, active, "interrupted", None, inputs)))
                return
            await self._ended(active, self._write(lambda conn: self._finish_background(
                conn, active, "succeeded" if output is not None else "failed", output, inputs)))
        except asyncio.CancelledError:
            call = active.call

            def cancelled(conn):
                reason = "revoked" if active.cancel_reason == "revoked" else "researcher"
                self._finish_background(conn, active, "cancelled", None, None, cancel_reason=reason)

            def stop(conn):  # the reason is read as it commits: a Cancel meanwhile overrides a shutdown
                _close_call(conn, call)
                if active.cancel_reason == "shutdown":  # it stays running and restarts next time
                    return True
                cancelled(conn)
                return False
            (running, _) = await _through(self._write(stop))
            if not running:  # its terminal record committed
                self.registry.finished.add(active.run_id)
            elif active.cancel_reason != "shutdown":  # a Cancel came once that had committed
                await self._ended(active, _through(self._write(cancelled)))
        except spending.BudgetExceeded:
            await self._ended(active, _through(self._write(
                lambda conn: self._finish_background(conn, active, "failed", None, None))))
        except Exception as error:  # its record still says running: kept for the next start
            log.error("background run failed unexpectedly (%s at %s)", type(error).__name__, _where(error))
        finally:
            # Released only after its terminal record committed (and it is in `finished`), or still
            # running in the record: left for the next start, or by a terminal write that raised, which
            # reads interrupted from here on. At no moment does a finished run read interrupted.
            self.registry.release(active)
            if (index_run and active.run_id not in self.registry.finished and not active.cancel_requested.is_set()
                    and not self.registry.closed and active.run_id not in self._started_again):
                # An index run left running in its record, held by no process (its terminal write failed) is started
                # again in this session, once, and only it (start_again), so what it was asked to do (a rerun marked
                # on it included) is never left waiting for an unrelated trigger or the next launch. Other runs keep
                # their own rules (a reading so left reads interrupted, with Retry; a model call's run waits for the
                # next start); a turn's end still kicks them all, as before.
                self._started_again.add(active.run_id)
                self._detach(self.start_again(active.run_id))
            if active.follows:  # recorded mid-run by writes that committed (a run with no row ends at its start)
                if self.registry.closed:
                    for held in active.follows:
                        self.registry.release(held)  # in the record: the next launch starts them
                    active.follows.clear()
                else:
                    self.start_follows(active)
            if active.cancel_reason == "shutdown" and not self.registry.closed:  # admitting again (resume)
                again = self.registry.add_background(active.run_id)  # at once: a Cancel always finds it active
                if again is not None:
                    again.task = asyncio.create_task(self._background(again))

    async def _background_call(self, active, project_id, workflow, inputs):
        """One model call for a background run. Returns its output, or None if it failed. Workflows:
        `title`, and `citation_check` (S1-19: POST /api/citations/check, the support judge run again on the
        exact (sentence, passage) pairs its inputs name, one batch of up to `judge_pairs` a run; its output
        binds each verdict to the pair judged)."""
        if workflow not in ("title", "citation_check"):
            raise ValueError(f"unknown background workflow {workflow!r}")
        if not inputs.get("message"):  # its source turn is gone: nothing to title or check
            return None
        async with self.settings_lock:  # the provider snapshot, as for a turn (see _admit)
            active.provider = inputs.get("provider")
            route = await asyncio.to_thread(providers.resolve_route, self.data_dir, inputs.get("provider"),
                                            inputs.get("model"))
            personal = await asyncio.to_thread(load_settings, self.data_dir)
            table = ((personal.values.get("providers") or {}).get(route.provider.name) or {}) if route else {}
        key = route and await asyncio.to_thread(credentials.load_key, self.data_dir, route.provider.name,
                                                self.keyring_backend)
        if route is None or key is None:
            return None
        # Under the project's policy now; the gate refuses a route it no longer allows. A Private
        # title resumed at a launch reads which models OpenRouter serves with zero retention, as a
        # turn's admission does.
        policy = await self._read(lambda conn: governance.policy(conn, project_id))
        if policy is None:
            return None
        zero_retention = policy.zero_retention and route.provider.is_openrouter
        if (zero_retention and catalog_status(route.provider, key)["stale"]) or not catalog_read(route):
            await self.catalog(route.provider, key)
        if _unusable(table, route.provider, route.model):  # a model its provider no longer offers: no title
            return None
        problem, facts = await self._route_facts(route, key, table, zero_retention=zero_retention, effort=None)
        if problem:
            return None
        budgets = personal.values["context"]
        papers = ()  # the papers whose passages the request holds
        if workflow == "title":
            rules, text, output = TITLE_RULES, inputs["message"], lambda result: _clean_title(result.content)
        else:
            check = await self._read(lambda conn: _check_request(conn, inputs))
            if check is None:
                return None
            judged, texts = check
            text = citations.judge_request([(s, p) for s, p, _ in judged], texts)
            papers = inputs.get("context_materials") or ()
            rules = citations.JUDGE_RULES

            def output(result):  # each citation's verdict with the pair it was given for; None (the run fails,
                found = citations.parse_verdicts(result.content, len(judged))  # writing nothing) if one is unanswered
                if len(found) < len(judged):
                    return None
                return {cid: [found[i], pid, citations.sentence_key(sentence)]
                        for i, (sentence, pid, cids) in enumerate(judged, start=1) for cid in cids}
        try:  # a role step's output limit is the least a step has
            request = context.assemble(workflow, window=facts["window"], output=_output(context.MIN_OUTPUT, facts),
                                       budgets=budgets,
                                       margin_percent=budgets["margin_percent"], system_rules=rules, request=text)
        except context.OverWindow:
            return None
        estimate = budget_router.request_cost(facts["rates"], request.input_tokens, request.max_tokens)

        def start(conn):
            if not _running(conn, active.run_id) or _revoked(conn, active.run_id):
                raise _Cancelled()
            conn.execute("UPDATE runs SET attempts = attempts + 1 WHERE id = ?", (active.run_id,))  # before the call
            (step,) = conn.execute("SELECT count(*) FROM run_events WHERE run_id = ? AND type = 'step_started'",
                                   (active.run_id,)).fetchone()
            reservation = spending.reserve(conn, run_id=active.run_id, step_seq=step, project_id=project_id,
                                           paying_conversation_id=None, estimate_usd=estimate,
                                           project_budget_usd=budget, conversation_budget_usd=None)
            _event(conn, active.run_id, "step_started", {"step": step, "phase": workflow, "estimate_usd": estimate,
                                                          "reservation_id": reservation,
                                                          **_step(route, facts, request)})
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
        result = await self._call(active, call, route, key, request, facts, zero_retention=zero_retention,
                                  output=output, materials=papers)
        return (output(result) or None) if result.ok else None

    # Local background work

    async def start_local(self, project_id, workflow, inputs, context=None) -> str:
        """Admit local work as a background run of the project and start it; returns its id. context
        is held in memory only (a passphrase): it is in the registry before the run's row exists, so
        no other start of background runs can run it without it."""
        run_id = new_id()
        active = self.registry.add_background(run_id)
        if active is None:
            raise AdmissionError(503, "shutting_down", "The app is closing")
        active.context = context
        try:
            await self._write(lambda conn: conn.execute(
                "INSERT INTO runs (id, project_id, kind, workflow, inputs) VALUES (?, ?, 'background', ?, ?)",
                (run_id, project_id, workflow, json.dumps(inputs))))
        except BaseException:
            self.registry.release(active)
            raise
        active.task = asyncio.create_task(self._background(active))
        return run_id

    async def record_background(self, count, write, committed=None):
        """Record new background runs in one transaction and start them. write(conn, ids) gets count
        fresh run ids, records the runs it needs under some of them, and returns (result, the ids it
        used). Each id is held in the registry before the transaction commits, and the runs recorded
        start as soon as it has, so no read ever finds one running in the record and not held here
        (which reads as interrupted). The ids it did not use are released inside the transaction.
        committed(result), if given, is a coroutine run once the transaction has committed, whatever
        happens to the caller after (a detached task: S1-17's start of a run a request was answered
        with); it is awaited unless the caller was cancelled, and an error it raises then reaches the
        caller after the commit (unawaited, after a cancellation, it is logged by asyncio). Returns the
        result; a cancellation of the caller waits for the transaction (see _through), starts the runs and
        committed, and is raised after."""
        actives = [self.registry.add_background(new_id()) for _ in range(count)]
        if None in actives:
            for active in actives:
                if active is not None:
                    self.registry.release(active)
            raise AdmissionError(503, "shutting_down", "The app is closing")
        def releasing(conn):
            result, used = write(conn, [a.run_id for a in actives])
            for active in actives:
                if active.run_id not in used:
                    self.registry.release(active)
            return result, used
        try:
            (result, used), cancelled = await _through(self._write(releasing))
        except BaseException:
            for active in actives:
                self.registry.release(active)
            raise
        for active in actives:
            if active.run_id in used:
                active.task = asyncio.create_task(self._background(active))
        after = self._detach(committed(result)) if committed is not None else None
        if cancelled:
            raise asyncio.CancelledError()
        if after is not None:
            await asyncio.shield(after)
        return result

    async def work(self, active, fn):
        """Run blocking fn in a worker thread to its end, whatever cancellations arrive meanwhile, so
        nothing it does outlasts the run's record. fn should check active.cancel_requested and stop;
        if it then raises, the run is cancelled. Its result is returned even when a cancellation
        came after it was done."""
        try:
            result, _ = await _through(asyncio.to_thread(fn))
        except Exception:
            if active.cancel_requested.is_set():
                raise asyncio.CancelledError() from None
            raise
        return result

    def record_in(self, conn, active, project_id, workflow, inputs):
        """Record a background run of the project in local work's terminal transaction (from its
        effect, on the writer thread): held here from before that transaction commits, as
        record_background holds its runs, and started by _local once it has (released if it did
        not), so no read finds it running in the record and not held. While the app is closing it is
        recorded unheld, and starts at the next launch."""
        held = self.registry.add_background(new_id())
        if held is not None:
            active.follows.append(held)
        conn.execute("INSERT INTO runs (id, project_id, kind, workflow, inputs) VALUES (?, ?, 'background', ?, ?)",
                     (held.run_id if held is not None else new_id(), project_id, workflow, json.dumps(inputs)))

    async def _local(self, active, project_id, workflow, inputs):
        try:
            summary, effect = await self.workflows[workflow](self, active, project_id, inputs)
            outcome = ("succeeded", None)
        except RunOutcome as ended:
            summary, effect, outcome = {"reason": ended.reason} if ended.reason else None, None, \
                (ended.status, ended.cancel_reason)
        except Exception as error:  # a defect: the run ends failed rather than starting again at each launch
            log.error("local background work failed unexpectedly (%s at %s)", type(error).__name__, _where(error))
            summary, effect, outcome = {"reason": "internal"}, None, ("failed", None)
        # The terminal record is written to its end whatever cancellations arrive meanwhile: work
        # whose effect is done (a published archive) keeps its outcome, and _finish_local decides
        # in its transaction whether a cancellation came before an effect it still has to apply.
        try:
            await _through(self._write(lambda conn: self._finish_local(conn, active, *outcome, summary, effect)))
        except BaseException as error:  # nothing of it was written, the runs its effect recorded neither
            for held in active.follows:
                self.registry.release(held)
            active.follows.clear()
            if not isinstance(error, RunOutcome):
                raise
            # Its effect found it could not be applied.
            await _through(self._write(lambda conn: self._finish_local(
                conn, active, error.status, error.cancel_reason, {"reason": error.reason} if error.reason else None)))
        self.start_follows(active)  # recorded with its terminal record, which has committed

    def start_follows(self, active):
        """Start the runs active recorded with record_in once the write that recorded them has committed:
        its terminal record, or, for local work that records one mid-run (a lookup's title change asking
        for its project's index run, S1-17), that write."""
        for held in active.follows:
            held.task = asyncio.create_task(self._background(held))
        active.follows.clear()

    def _finish_local(self, conn, active, status, cancel_reason, summary, effect=None):
        """Local work's effect, terminal status and summary, in one transaction. A revocation in the
        record ends it cancelled; so does a cancellation requested before this transaction when it
        has an effect to apply here, which is then left out. Work whose effect is already done (a
        file written) keeps its outcome."""
        run_id = active.run_id
        if not _running(conn, run_id):
            return
        if _revoked(conn, run_id):
            status, cancel_reason, effect = "cancelled", "revoked", None
        elif effect is not None and active.cancel_requested.is_set() and active.cancel_reason != "shutdown":
            status, cancel_reason, effect = "cancelled", "researcher", None
        if status == "succeeded" and effect is not None:
            effect(conn)  # may raise RunOutcome, which rolls this transaction back
        reason = (summary or {}).get("reason") if status != "succeeded" else None
        _let_go(conn, run_id)
        _event(conn, run_id, "run_finished", {"status": status, **({"reason": reason} if reason else {})})
        conn.execute("UPDATE runs SET status = ?, cancel_reason = ?, waiting = NULL, finished_at = ?, summary = ?,"
                     " settled_cost_usd = ? WHERE id = ?",
                     (status, cancel_reason, utc_now(), json.dumps(summary) if summary else None,
                      spending.run_cost(conn, run_id), run_id))
        self._ending(conn, active)

    def _ending(self, conn, active):
        """The run's workflow's ending, if it has one (endings), in its terminal transaction."""
        workflow, project_id = conn.execute("SELECT workflow, project_id FROM runs WHERE id = ?",
                                            (active.run_id,)).fetchone()
        if (ending := self.endings.get(workflow)) is not None:
            ending(conn, active, project_id)

    def _finish_background(self, conn, active, status, output, inputs, cancel_reason=None):
        """The run's effect, terminal status and settled cost, in one transaction. A cancel
        requested before this transaction checks for it wins, on every path: the run ends
        cancelled, with no effect. One that comes later finds the run ended, and the cancel
        request reports how (Harness.cancel)."""
        run_id = active.run_id
        if not _running(conn, run_id):
            return
        revoked = _revoked(conn, run_id)  # in the record, which this process may not have heard yet
        if revoked or (active.cancel_requested.is_set() and active.cancel_reason != "shutdown"):
            status, output = "cancelled", None
            cancel_reason = "revoked" if revoked or active.cancel_reason == "revoked" else "researcher"
        if status == "succeeded" and output is not None and inputs.get("check"):
            for cid, (verdict, pid, key) in output.items():  # S1-19: the answer's citations, checked again
                # only a citation that still has the sentence (by its hash) and passage its verdict was given for
                row = conn.execute("SELECT json_extract(context, '$.sentence') FROM citations WHERE id = ? AND owner_id = ?"
                                   " AND existence = 'ok' AND passage_id = ?", (cid, inputs["check"], pid)).fetchone()
                if row is not None and citations.sentence_key(row[0]) == key:
                    conn.execute("UPDATE citations SET support = ?, support_run_id = ?, checked_at = ? WHERE id = ?",
                                 (verdict, run_id, utc_now(), cid))
        elif status == "succeeded" and output is not None:
            # The title is written only if nobody changed it since the run was queued.
            conn.execute(
                "UPDATE conversations SET title = ?, title_source = 'generated', title_rev = title_rev + 1,"
                " updated_at = ? WHERE id = ? AND title_rev = ? AND coalesce(title_source, '') <> 'researcher'",
                (output, utc_now(), inputs["conversation_id"], inputs["title_rev"]))
        _let_go(conn, run_id)
        _event(conn, run_id, "run_finished", {"status": status})
        conn.execute("UPDATE runs SET status = ?, cancel_reason = ?, waiting = NULL, finished_at = ?,"
                     " settled_cost_usd = ? WHERE id = ?",
                     (status, cancel_reason, utc_now(), spending.run_cost(conn, run_id), run_id))
        self._ending(conn, active)  # a local run cancelled before its work began ends here too


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


def _output(setting, facts):
    """A step's output setting, no larger than the completion limit its endpoints list."""
    return min(setting, facts["output"]) if facts["output"] else setting


def _check_request(conn, inputs):
    """For a citation_check run: ([(sentence, passage id, citation ids)] for the pairs its inputs name, their
    passages' texts), or None (the run fails, writing nothing) when one of their passages is gone or a pair's
    sentence is no longer its citations' (its paper deleted, or the citation re-pointed): a check never
    reports success with a pair it did not judge. Sentences are read from the citation rows, by the hash
    the inputs keep (backend/provenance.py)."""
    pairs = inputs.get("pairs") or []
    texts = dict(conn.execute("SELECT id, text FROM passages WHERE id IN (SELECT value FROM json_each(?))",
                              (json.dumps(sorted({p for _, p, _, _ in pairs})),)).fetchall())
    judged = []
    for key, pid, _, cids in pairs:
        found = conn.execute("SELECT json_extract(context, '$.sentence') FROM citations WHERE id = ? AND passage_id = ?",
                             (cids[0], pid)).fetchone()
        if pid not in texts or found is None or citations.sentence_key(found[0]) != key:
            return None
        judged.append((found[0] or "", pid, cids))
    return (judged, texts) if judged else None


def _stored(content, body):
    """The content store's name for an attempt's request body, or None (no body, no store, or a store that
    could not take it: the attempt's receipt is recorded and settled all the same)."""
    if content is None or body is None:
        return None
    try:
        return content.put(body, "application/json")
    except Exception as error:
        log.warning("a request body could not be kept for the trace (%s)", type(error).__name__)
        return None


def _step(route, facts, request):
    """What a step_started event records of a step's route and request: the route and effort, which a
    turn keeps from its admission to its end; the window, the endpoints it can reach, the request's
    counted size and output limit, and what the assembler left out."""
    return {"route": route.key, "effort": facts["effort"], "window": request.window, "endpoints": facts["endpoints"],
            "input_tokens": request.input_tokens, "max_tokens": request.max_tokens,
            **({"cuts": request.cuts} if request.cuts else {})}


def _settle_attempts(conn, reservation_id, rates, attempts):
    """Settle a recorded call from all of its dispatched attempts: their reported costs, or
    reported tokens at the step's rates where an attempt reported no cost (then marked estimated). An
    attempt that went out and reported no usage keeps the estimate: the call settles at the
    reservation's estimate, or at what the others cost when that is more, marked estimated."""
    total, basis = 0.0, "reported"
    for attempt in attempts:
        if not attempt.dispatched:
            continue
        cost = attempt.reported_cost
        if cost is None:
            cost = budget_router.cost_from_usage(rates, attempt.usage)
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


def _keep_body(conn, call, number, ref):
    """Keep an attempt's exact request body, before it is sent, in run_bodies with the papers its text
    holds (run_body_materials), so a deletion of one of them removes it: only while its run exists and every
    one of those papers does (backend/provenance.py; a body of a paper deleted already is never kept)."""
    if conn.execute("SELECT 1 FROM runs WHERE id = ?", (call.run_id,)).fetchone() is None or \
            not provenance.carried(conn, call.materials):
        return
    conn.execute("INSERT OR IGNORE INTO run_bodies (run_id, step, attempt, sha256) VALUES (?, ?, ?, ?)",
                 (call.run_id, call.step, number, ref))
    conn.executemany("INSERT OR IGNORE INTO run_body_materials (sha256, material_id) VALUES (?, ?)",
                     [(ref, material) for material in call.materials])


def _kept_body(conn, call, number, dispatched):
    """What an attempt's model_attempt event says of its exact request body (kept by _keep_body before it
    was sent): {"body": sha256} (the trace says it was removed when its row is gone with a deleted paper);
    {"body_unavailable": True} when the content store could not take it; nothing for an attempt that never
    left the machine, whose body is not kept."""
    if not 0 <= number < len(call.bodies):
        return {}
    ref = call.bodies[number]
    if not dispatched:
        if ref is not None:
            conn.execute("DELETE FROM run_bodies WHERE run_id = ? AND step = ? AND attempt = ?",
                         (call.run_id, call.step, number))
            conn.execute("DELETE FROM run_body_materials WHERE sha256 = ? AND NOT EXISTS ("
                         "SELECT 1 FROM run_bodies WHERE sha256 = ?)", (ref, ref))
        return {}
    return {"body": ref} if ref is not None else {"body_unavailable": True}


def _hold_unspent(conn, call, ctx, budgets):
    """The answer's reservation less its settled cost, held again for its judge, in the transaction that settles
    it, against the budgets (Harold's option C, 2026-10-21): (its id, its amount), which the answer's
    step_finished event names (allowance_id), as _release_allowances and deletion read it; None when none is
    left or a budget no longer holds it."""
    row = conn.execute("SELECT run_id, step_seq, project_id, paying_conversation_id, estimate_usd - settled_usd"
                       " FROM budget_reservations WHERE id = ? AND status = 'settled'", (call.reservation_id,)).fetchone()
    if row is None or row[4] <= 0:
        return None
    (own,) = conn.execute("SELECT budget_usd FROM conversations WHERE id = ?", (row[3],)).fetchone() or (None,)
    try:
        held = spending.reserve(conn, run_id=row[0], step_seq=row[1], project_id=row[2], paying_conversation_id=row[3],
                                estimate_usd=round(row[4], 9), project_budget_usd=budgets[0],
                                conversation_budget_usd=own if own is not None else budgets[1])
    except spending.BudgetExceeded:
        return None
    ctx["unspent"] = held
    return held, round(row[4], 9)


def _release_allowances(conn, run_id=None):
    """Release the open allowances (the judge's share, named by the answer's step_started allowance_id, and the
    answer's unspent held again, named by its step_finished) of a run that ends, or of every run after a crash:
    no call was made against them."""
    for (reservation,) in conn.execute(
            "SELECT b.id FROM budget_reservations b JOIN run_events e ON e.run_id = b.run_id"
            " AND e.type IN ('step_started', 'step_finished') AND json_extract(e.data, '$.allowance_id') = b.id"
            " WHERE b.status = 'open' AND (? IS NULL OR b.run_id = ?)", (run_id, run_id)).fetchall():
        spending.release(conn, reservation)


def _close_call(conn, call):
    """Close a call cut off before it was recorded: one that may have gone out settles at
    its estimate and leaves a content-free attempt with an unknown charge; one that never
    left is released. Either happens once; a call already recorded is left as it is."""
    if call is None:
        return
    if call.dispatched:
        if spending.settle(conn, call.reservation_id) and _running(conn, call.run_id):
            _event(conn, call.run_id, "model_attempt", {
                "step": call.step, "route": call.route, "retention": call.retention, "outcome": "cancelled",
                "http_status": None, "dispatched": True, "charge": "unknown",
                **_kept_body(conn, call, len(call.bodies) - 1, True)})
    else:
        spending.release(conn, call.reservation_id)
        for number in range(len(call.bodies)):  # its text never left the machine: none of it is kept
            _kept_body(conn, call, number, False)


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
