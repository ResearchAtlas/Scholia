"""Search over a project's papers (slice-1 spec F3a step 4, sections 4.3, 5, 7.2, 7.4 and 10; tickets
17 and 71): index runs, the search model's offer at a project's first material, hybrid search and
the index's status and rebuild. The index file itself is backend/search_index.py.

Index runs (workflow `index`) are project background work, recorded in the transaction that queues
their passages (`queued`: an extraction's commit, a shared reading at an add, a title change), so
none is lost. Each names its materials (`inputs.material_ids`, the deletion service's scope link):
it applies the queue first (keyword search), then, one run at a time, embeds its materials'
passages through the local helper, `[retrieval] embedding_batch` at a time, each committed to the
index as one batch. Every embedding request carries the run's dispatch check, read in the outbound
gate's decision transaction: the run still runs, unrevoked, and the passage is still one a current
version in the project reads (`_may_index`); so nothing deleted or replaced is sent, and a vector is
written only while its passage's row is there with the text it was made from. Without the search
model, the run ends after the keyword part and says search is keyword-only. Embedding sends nothing
off the Mac, so a stricter level or the review lock leaves indexing running (governance's
UNSENT_WORKFLOWS); deleting one of its materials revokes it, and the others get a new run.

The offer (workflow `model_offer`, ask kind `model_download`): the first material whose reading
gives a Normal or Private project passages, while the model is not installed and no download runs,
records one offer run, unless the project has one running, one that ran to its end, or one the
researcher answered (whatever became of its run after). It asks through the shared
confirmation (Download from Hugging Face, Download from ModelScope, Later), shown where the work
started. The answer is the consent: a download answer records the start, then starts one download
(S1-16's start_download, the General project's client); a download already running or the model
installed means it starts nothing, and says so. Later sends nothing. An offer no longer needed (the
model installed, or a download started meanwhile) closes; a stricter level or the lock revokes it,
nothing being downloaded. A Local only project gets none: search there is keyword-only until the
model is installed or imported.

Search (`POST /api/projects/{id}/search`): the typed query, visible and NFKC-normalized, goes to BM25
(top `bm25_candidates`) and, within `hybrid_ms` including the query's embedding, dense retrieval (top
`dense_candidates`), concurrently; the candidates are checked in the main database (passages a current
version in the project reads now, never references) in the read that fetches them; the survivors are
fused by reciprocal rank fusion (`rrf_k`) and cut to the limit. Search says when it is keyword-only and
why, and how much of the library meaning search covers.
"""

import asyncio
import json
import logging
import re
import unicodedata
import weakref
from pathlib import Path

from fastapi import APIRouter, Request
from pydantic import BaseModel, Field

from backend import asks, local_helper
from backend.db import deletion, new_id
from backend.local_helper import EMBEDDING, HelperUnavailable, Refused
from backend.runs import AdmissionError, RunOutcome, _event, _revoked, _running, derived_status
from backend.search_index import DAMAGE, DIMENSIONS, SearchIndex, Unavailable, digest, index_text, readings
from backend.settings import load_settings, visible

log = logging.getLogger(__name__)

QUERY_CHARS = 1000
MAX_LIMIT = 50
WAIT_SECONDS = 0.5  # an offer's look at its answer
HELPER_WAIT_SECONDS = 30  # how long an index run started at launch waits for the helper's startup
# Why search is keyword-only where an index run ends with its keyword part, succeeded: the model is not
# installed (its install starts the embeddings), or sqlite-vec did not load (the next launch where it
# loads embeds them). For any other reason (the helper cannot serve now) it fails, and Retry applies.
WAITING_REASONS = ("model_missing", "vectors_unavailable")
DENSE_LIMIT = 4096  # vec0's largest k
OFFER_OPTIONS = ["huggingface", "modelscope", "later"]
# Qwen3-Embedding's instruction for retrieval queries (its model card); passages are embedded without one.
QUERY_INSTRUCTION = "Instruct: Given a web search query, retrieve relevant passages that answer the query\nQuery:"

router = APIRouter()
_STATES = weakref.WeakKeyDictionary()  # harness -> the app's state, for the hooks materials.py calls


class Query(BaseModel):
    query: str = Field(max_length=QUERY_CHARS)
    limit: int | None = Field(default=None, ge=1, le=MAX_LIMIT)


def _state(request):
    return request.app.state.scholia


# Opening the index with the database


def identity(config):
    """What index_meta records of the vectors: the configured search model (its id, the file's SHA-256 as
    its revision, its quantization), the helper's runtime (its build's SHA-256 of llama-server) and the
    dimensions."""
    pin = config.models.get(EMBEDDING, local_helper.EMBEDDING_MODEL)
    quantization = re.search(r"(Q\d+_[0-9A-Z_]+|F16|F32|BF16)", pin["file"])
    runtime = "none"
    if config.binary is not None:
        try:
            listed = json.loads((Path(config.binary).parent.parent / local_helper.MANIFEST).read_text())
            runtime = listed.get(f"MacOS/{Path(config.binary).name}") or "none"
        except (OSError, ValueError, AttributeError):
            runtime = "unknown"
    return {"model": pin["id"], "revision": pin["sha256"], "quantization": quantization.group(1) if quantization
            else pin["file"], "runtime": runtime, "dimensions": DIMENSIONS}


async def open_index(state, db):
    """Open the index for db (at launch, and again on the database a restore puts in place), closing
    the one before. While the model is installed, papers whose passages lack embeddings get index runs,
    one per project, recorded unstarted (the app's background runs start them, backups.start_background,
    never before): every read paper when the vectors are wanted again (dropped, or the file rebuilt),
    else those the index holds unembedded (work an earlier run left: a helper that could not serve, a
    Cancel, a crash), but those a running index run names. A failure leaves search keyword-only with
    nothing indexed (index_unavailable), and the app runs."""
    previous = state.pop("index", None)
    if previous is not None:
        await asyncio.to_thread(previous.close)
    index = SearchIndex(state["data_dir"], db, identity(state.get("helper_config") or local_helper.Config()))
    try:
        wanted, rebuilding = await asyncio.to_thread(index.open)
    except Exception as error:
        log.error("the search index could not be opened (%s); search is unavailable", type(error).__name__)
        await asyncio.to_thread(index.close)
        return
    state["index"] = index
    state["model_installed"] = lambda: asyncio.ensure_future(_embed_all(state))
    loop = asyncio.get_running_loop()

    def rebuilt():  # a damaged file replaced while the app runs (in the writer): its papers are embedded again
        if _installed(state):
            try:
                loop.call_soon_threadsafe(state["model_installed"])
            except RuntimeError:  # the app stopped meanwhile: the next launch embeds them (index.unembedded)
                pass
    index.rebuilt = rebuilt
    deletion.CLEANUP[db] = index.apply  # a deletion's removals, applied after its commit
    if rebuilding is None:
        index.apply_soon()
    if not _installed(state):
        return
    try:
        pending = None if wanted else await asyncio.to_thread(index.unembedded)
    except Exception as error:  # unreadable now (being replaced): the papers it cannot tell about are taken as pending
        log.warning("the search index could not be read at open (%s)", type(error).__name__)
        pending = None

    def record(conn):
        papers = _read_papers(conn)
        if pending is not None:
            papers = {project: [m for m in materials if m in pending.get(project, ())] for project, materials in papers.items()}
        for project, materials in papers.items():
            if materials:
                _insert_run(conn, None, project, {"material_ids": materials})
    await asyncio.to_thread(db.write, record)


def _read_papers(conn):
    """{project id: [material ids]}: the papers each project's index holds or should hold, not named by
    a running index run."""
    named = {m for (m,) in conn.execute("SELECT j.value FROM runs r, json_each(r.inputs, '$.material_ids') j"
                                        " WHERE r.workflow = 'index' AND r.status = 'running'")}
    found = {}
    for project, material in conn.execute("SELECT DISTINCT m.project_id, m.id FROM materials m JOIN material_versions v"
                                          " ON v.material_id = m.id AND v.is_current = 1 WHERE v.file_sha256 IS NOT NULL"
                                          " ORDER BY m.project_id, m.created_at"):
        if material not in named:
            found.setdefault(project, []).append(material)
    return found


def _insert_run(conn, run_id, project_id, inputs):
    conn.execute("INSERT INTO runs (id, project_id, kind, workflow, inputs) VALUES (?, ?, 'background', 'index', ?)",
                 (run_id or new_id(), project_id, json.dumps(inputs)))


def _installed(state):
    """Whether the search model's file is in place (S1-16's check: its size; every launch checks its hash)."""
    local = state.get("local_helper")
    if local is not None and EMBEDDING in local.helpers:
        helper = local.helpers[EMBEDDING]
        return local_helper.installed(helper.path, helper.pin)
    config = state.get("helper_config") or local_helper.Config()
    pin = config.models.get(EMBEDDING)
    return pin is not None and local_helper.installed(local_helper.model_path(state["data_dir"], pin), pin)


def _downloading(state):
    local = state.get("local_helper")
    return local is not None and local.download is not None and local.download.state == "running"


async def _embed_all(state):
    """The model was just installed, or a damaged file was rebuilt while the app runs: every project whose
    index has passages without embeddings gets an index run for those papers, those a running index run
    names too. That run may already have found the model missing, or be ending; a second run for papers it
    embeds finds nothing missing and ends."""
    index, harness = state.get("index"), state.get("harness")
    if index is None or harness is None:
        return
    try:
        pending = await asyncio.to_thread(index.unembedded)
        for project, materials in pending.items():
            await _record(harness, project, {"material_ids": materials})
    except Exception as error:
        log.warning("index runs after the model's install could not be recorded (%s)", type(error).__name__)


async def _record(harness, project_id, inputs):
    def write(conn, ids):
        if conn.execute("SELECT 1 FROM projects WHERE id = ?", (project_id,)).fetchone() is None:
            return None, []
        _insert_run(conn, ids[0], project_id, inputs)
        return ids[0], ids
    return await harness.record_background(1, write)


async def after_deletion(state, revoked, project_id=None, files=()):
    """After a material's deletion, papers whose passages lack embeddings get a new index run: those of
    the index runs it revoked that are still there, and those of the project whose current file the
    deleted material also had (the deletion queued their passages again, with their own title)."""
    index, harness = state.get("index"), state.get("harness")
    if index is None or harness is None:
        return

    def wanted(conn):
        found = {}
        for project, inputs in conn.execute("SELECT project_id, inputs FROM runs WHERE workflow = 'index'"
                                            " AND id IN (SELECT value FROM json_each(?))", (json.dumps(list(revoked)),)):
            found.setdefault(project, set()).update(json.loads(inputs or "{}").get("material_ids") or [])
        if project_id is not None and files:
            found.setdefault(project_id, set()).update(m for (m,) in conn.execute(
                "SELECT m.id FROM materials m JOIN material_versions v ON v.material_id = m.id AND v.is_current = 1"
                " WHERE m.project_id = ? AND v.file_sha256 IN (SELECT value FROM json_each(?))",
                (project_id, json.dumps(list(files)))))
        return {project: materials & {m for (m,) in conn.execute("SELECT id FROM materials WHERE project_id = ?",
                                                                  (project,))} for project, materials in found.items()}
    try:
        papers = await asyncio.to_thread(harness.db.read, wanted)
        if not any(papers.values()):
            return
        pending = await asyncio.to_thread(index.unembedded)
        for project, materials in papers.items():
            if rest := [m for m in pending.get(project, []) if m in materials]:
                await _record(harness, project, {"material_ids": rest})
    except Exception as error:
        log.warning("index runs after a deletion could not be recorded (%s)", type(error).__name__)


# Hooks for backend/materials.py, inside the transactions that queue passages


def queued(conn, harness, project_id, material_ids, origin, record):
    """The materials' passages were just queued for the project's index, in this transaction: an index
    run for them, and the search model's offer at the project's first material (see the module's
    docstring). record(conn, project id, workflow, inputs) records a run that starts with the commit."""
    record(conn, project_id, "index", {"material_ids": list(material_ids)})
    state = _STATES.get(harness) if harness is not None else None
    if state is None or _installed(state) or _downloading(state):
        return
    level = conn.execute("SELECT sensitivity FROM projects WHERE id = ?", (project_id,)).fetchone()
    if level is None or level[0] == "local_only":
        return
    # Once per project (ticket 71): none while one runs or has run to its end, nor once the researcher
    # has answered one, whatever became of its run after the answer (cancelled, revoked, failed).
    offered = conn.execute("SELECT id, status FROM runs WHERE project_id = ? AND workflow = 'model_offer'",
                           (project_id,)).fetchall()
    if not any(status in ("running", "succeeded") or asks.researcher_answered(conn, run) for run, status in offered):
        record(conn, project_id, "model_offer", {"origin": origin})


def retitled(conn, project_id, material_id, record):
    """A material's title changed (the researcher's edit or a lookup), in this transaction: its passages'
    index text changes with it (section 7.1), so they are queued again, with an index run."""
    count = conn.execute(f"""INSERT INTO index_queue (target, target_id, project_id, op)
        SELECT 'passage', p.id, ?, 'add' FROM materials m
        JOIN material_versions w ON w.material_id = m.id AND w.is_current = 1
        JOIN extractions e ON {deletion._reads_now('w', 'e')} JOIN passages p ON p.extraction_id = e.id
        WHERE m.id = ? AND m.project_id = ? ORDER BY p.ordinal""", (project_id, material_id, project_id)).rowcount
    if count:
        record(conn, project_id, "index", {"material_ids": [material_id]})


# The workflows


def register(harness, state):
    """The index and model_offer workflows, as the harness's local background work."""
    _STATES[harness] = state

    async def index_run(harness, active, project_id, inputs):
        try:
            return await _index_run(state, harness, active, project_id, inputs)
        except (Unavailable, *DAMAGE):  # the file is being replaced or rebuilt again; its rebuild's runs embed
            raise RunOutcome("failed", "index_unavailable") from None

    async def offer_run(harness, active, project_id, inputs):
        return await _offer_run(state, harness, active, project_id, inputs)

    harness.workflows["index"] = index_run
    harness.workflows["model_offer"] = offer_run


def search_mode(state):
    """("hybrid", None), or ("keyword_only", why): no index, sqlite-vec not loaded, or the helper's own
    reason (local_helper.Local.status: the model missing, the helper failed, and so on)."""
    index = state.get("index")
    if index is None or index.closed or index.unusable:
        return "keyword_only", "index_unavailable"
    if not index.vectors:
        return "keyword_only", "vectors_unavailable"
    local = state.get("local_helper")
    if local is None:
        return "keyword_only", "closing"
    found = local.status()["search"]
    return found["mode"], found["reason"]


def _may_index(conn, run_id, project_id, passage_id):
    """The dispatch check of one embedding request, in the gate's decision transaction: the run still
    runs, unrevoked, and a current version in the project still reads the passage. (Its project is
    its owner: a deleted project takes its runs with it.)"""
    row = conn.execute("SELECT status, cancel_reason FROM runs WHERE id = ?", (run_id,)).fetchone()
    return tuple(row or ()) == ("running", None) and bool(readings(conn, project_id, [passage_id], references=False))


def _text(reading):
    _, title, path, _, text = reading[:5]
    return index_text(title, path, text)


async def _index_run(state, harness, active, project_id, inputs):
    index, db, run_id = state.get("index"), harness.db, active.run_id
    if index is None:
        raise RunOutcome("failed", "index_unavailable")
    materials = inputs.get("material_ids") or []

    async def drained(fn, *args):  # a change to the index, finished (or withdrawn) before the run's end is recorded
        done = await harness.work(active, lambda: fn(*args))
        if active.cancel_requested.is_set():
            raise asyncio.CancelledError()
        return done
    stop = active.cancel_requested.is_set  # asked between pages of the queue and every STOP_EVERY rebuilt rows
    if inputs.get("rebuild"):
        await drained(index.rebuild_project, project_id, run_id, stop)
    else:
        await drained(index.apply, stop)
    # At launch, background runs start before the helper's own startup has run: a moment's wait.
    for _ in range(int(HELPER_WAIT_SECONDS / 0.05)):
        if state.get("local_helper") is not None or harness.registry.closed:
            break
        await asyncio.sleep(0.05)
    mode, reason = search_mode(state)
    if mode != "hybrid" and reason in WAITING_REASONS:  # keyword search only until that changes, said
        return {"mode": "keyword_only", "reason": reason}, None  # the model's install embeds them (_embed_all)
    if mode != "hybrid":  # the helper cannot serve now: failed with its reason, and Retry embeds what is missing
        raise RunOutcome("failed", reason)
    batch = load_settings(state["data_dir"]).values["retrieval"]["embedding_batch"]

    async def progress():
        counts = await asyncio.to_thread(index.counts, project_id)
        mine = [counts[m] for m in materials if m in counts]
        active.progress = {"done": sum(c[1] for c in mine), "total": sum(c[2] for c in mine)}

    async with state.setdefault("embedding", asyncio.Lock()):  # one index run embeds at a time
        await progress()
        embedded, skip = 0, []
        while rows := await asyncio.to_thread(index.missing, project_id, materials, batch, skip):
            found = await asyncio.to_thread(db.read, lambda conn: readings(conn, project_id, [r[1] for r in rows],
                                                                             references=False))
            vectors = []
            for rowid, pid, mark in rows:
                reading = found.get(pid)
                if reading is None or digest(_text(reading)) != mark:  # gone or changed: the queue brings that in
                    skip.append(rowid)
                    continue
                try:
                    [vector] = await local_helper.embed(state, [_text(reading)], project_id=project_id,
                                                        admit=lambda conn, pid=pid: _may_index(conn, run_id, project_id, pid))
                except HelperUnavailable as unavailable:
                    def now(conn, pid=pid):
                        return (_running(conn, run_id) and not _revoked(conn, run_id),
                                bool(readings(conn, project_id, [pid], references=False)))
                    running, current = await asyncio.to_thread(db.read, now)
                    if not running:
                        raise RunOutcome("cancelled", "project_changed", "revoked") from None
                    if not current:  # deleted or replaced as it was asked for: refused by the check, never sent
                        skip.append(rowid)
                        continue
                    raise RunOutcome("failed", unavailable.reason) from None
                if len(vector) != DIMENSIONS:  # not this model's: nothing would ever be stored
                    raise RunOutcome("failed", "request_failed")
                vectors.append((rowid, pid, mark, vector))
            embedded += await drained(index.store, project_id, vectors, stop) or 0
            await progress()
    return {"mode": "hybrid", "embedded": embedded}, None


async def _offer_run(state, harness, active, project_id, inputs):
    db, run_id = harness.db, active.run_id

    async def read(fn):
        return await asyncio.to_thread(db.read, fn)

    async def write(fn):
        return await asyncio.to_thread(db.write, fn)

    started = await read(lambda conn: conn.execute(
        "SELECT json_extract(data, '$.download') FROM run_events WHERE run_id = ? AND type = 'step_started'"
        " AND json_extract(data, '$.download') IS NOT NULL", (run_id,)).fetchone())
    if started is not None:  # its start was recorded before a restart: never again (no automatic resume)
        return {"answer": started[0], "outcome": "started" if _installed(state) or _downloading(state)
                else "start_interrupted"}, None
    while True:
        ask, answer = await read(lambda conn: asks.asked(conn, run_id))
        if answer is not None:
            if answer.get("withdrawn"):
                return {"outcome": answer["withdrawn"]}, None
            break
        policy = await read(lambda conn: asks._current_policy(conn, project_id))
        if policy is None:
            raise RunOutcome("cancelled", "project_changed", "revoked")
        closing = "not_needed" if _installed(state) or _downloading(state) else \
            "local_only" if policy["level"] == "local_only" else None
        if closing is not None:  # no longer asked: the model is here or coming, or the project offers none now
            if ask is not None:
                await write(lambda conn: asks.withdraw(conn, run_id, ask["ask_id"], closing))
            return {"outcome": closing}, None
        if ask is None or ask["policy"] != policy:  # asked anew under the project's protection as it is now

            def again(conn):
                if ask is not None:
                    asks.withdraw(conn, run_id, ask["ask_id"], "policy_changed")
                return asks.raise_ask(conn, run_id, "model_download", OFFER_OPTIONS, {"model": EMBEDDING},
                                      inputs.get("origin"))
            try:
                await write(again)
            except asks.AskRefused:  # revoked or ended meanwhile
                raise RunOutcome("cancelled", "project_changed", "revoked") from None
        await asyncio.sleep(WAIT_SECONDS)
    option = answer.get("option")
    if option not in OFFER_OPTIONS[:2]:
        return {"answer": "later"}, None

    def start(conn):  # recorded before the call, so a crash never starts a second download from this answer
        if not _running(conn, run_id) or _revoked(conn, run_id):
            return False
        _event(conn, run_id, "step_started", {"download": option})
        return True

    if not await write(start):
        raise RunOutcome("cancelled", "project_changed", "revoked")
    local = state.get("local_helper")
    try:
        if local is None:
            raise Refused(503, "closing", "The app is closing")
        await local.start_download(EMBEDDING, option, project_id)
        outcome = "started"
    except Refused as refused:  # download_running, already_installed, disk_full, ...: nothing started, said
        outcome = refused.code
    return {"answer": option, "outcome": outcome}, None


# Search


async def _counts(index, project_id):
    """The index's counts for the project, or none while the file cannot be read (being replaced)."""
    try:
        return await asyncio.to_thread(index.counts, project_id)
    except Exception as error:
        log.warning("the search index could not be read (%s)", type(error).__name__)
        return {}


def fuse(ranked_lists, k):
    """Reciprocal rank fusion: each passage scores the sum of 1 / (k + rank) over the lists it is in;
    best first, ties in the order first seen."""
    scores = {}
    for ranked in ranked_lists:
        for rank, pid in enumerate(ranked, start=1):
            scores[pid] = scores.get(pid, 0.0) + 1.0 / (k + rank)
    return sorted(scores, key=lambda pid: -scores[pid])


async def _dense(state, index, project_id, text, depth):
    [vector] = await local_helper.embed(state, [QUERY_INSTRUCTION + text], project_id=project_id, query=True)
    return await asyncio.to_thread(index.dense, project_id, vector, depth)


async def search(state, project_id, query, limit=None):
    """A project's passages for a typed query (see the module's docstring)."""
    text = visible(query)
    if text is None:
        raise AdmissionError(400, "query_needed", "Type something to search for")
    text = unicodedata.normalize("NFKC", text)
    db, index = state["db"], state.get("index")
    if await asyncio.to_thread(db.read, lambda conn: conn.execute(
            "SELECT 1 FROM projects WHERE id = ?", (project_id,)).fetchone()) is None:
        raise AdmissionError(404, "not_found", "No such project")
    values = load_settings(state["data_dir"]).values["retrieval"]
    limit = min(limit or values["keep"], MAX_LIMIT)  # the setting may be larger than the API allows
    mode, reason = search_mode(state)
    if index is None or index.closed or index.unusable:  # not opened, being replaced, or its rebuild failed (retried)
        return {"results": [], "mode": mode, "reason": reason, "index": "unavailable", "coverage": None}
    keyword = asyncio.ensure_future(asyncio.to_thread(index.keyword, project_id, text, values["bm25_candidates"]))
    counts = await _counts(index, project_id)
    coverage = {"embedded": sum(c[1] for c in counts.values()), "total": sum(c[2] for c in counts.values())}
    dense = []
    if mode == "hybrid" and coverage["embedded"]:
        try:  # the deadline covers the query's embedding: past it, keyword results, said; a helper start goes on
            dense = await asyncio.wait_for(_dense(state, index, project_id, text,
                                                  min(values["dense_candidates"], DENSE_LIMIT)), values["hybrid_ms"] / 1000)
        except TimeoutError:
            mode, reason = "keyword_only", "deadline"
        except HelperUnavailable as unavailable:
            mode, reason = "keyword_only", unavailable.reason
        except Exception as error:  # the file being replaced: keyword results, said
            log.warning("a dense search failed (%s)", type(error).__name__)
            mode, reason = "keyword_only", "index_unavailable"
    try:
        keyword = await keyword
    except Exception as error:  # the file damaged: replaced meanwhile (search_index), nothing found now
        log.warning("a keyword search failed (%s)", type(error).__name__)
        keyword, mode, reason = [], "keyword_only", "index_unavailable"
    # Access is checked again in the main database before any passage is used, in the read that
    # fetches the originals: only passages a current version in this project reads now, no reference.
    found = await asyncio.to_thread(db.read, lambda conn: readings(conn, project_id, {*keyword, *dense}, references=False))
    ranked = fuse([[p for p in keyword if p in found], [p for p in dense if p in found]], values["rrf_k"])[:limit]
    results = [{"passage_id": pid, "material_id": r[0], "title": r[1], "section_path": r[2], "kind": r[3], "text": r[4],
                "page": r[5], "ordinal": r[6], "version_id": r[7]} for pid in ranked for r in [found[pid]]]
    return {"results": results, "mode": mode, "reason": reason,
            "index": "building" if index.building or index.damaged else "ready",
            "coverage": coverage}


@router.post("/api/projects/{project_id}/search")
async def search_passages(project_id: str, body: Query, request: Request):
    return await search(_state(request), project_id, body.query, body.limit)


@router.get("/api/projects/{project_id}/index")
async def index_status(project_id: str, request: Request):
    """The project's index: whether it is ready or being rebuilt, whether search is keyword-only and
    why, its passages indexed and embedded (in all, and by paper), and its index run: a rebuild while
    one runs (a newer run never hides it), else the latest."""
    state = _state(request)
    registry = state["harness"].registry

    def latest(conn):
        if conn.execute("SELECT 1 FROM projects WHERE id = ?", (project_id,)).fetchone() is None:
            raise AdmissionError(404, "not_found", "No such project")
        rebuilding = conn.execute("SELECT id, status, 1 FROM runs WHERE project_id = ? AND workflow = 'index'"
                                  " AND status = 'running' AND json_extract(inputs, '$.rebuild') ORDER BY rowid DESC",
                                  (project_id,)).fetchall()
        return rebuilding, conn.execute("SELECT id, status, json_extract(inputs, '$.rebuild') FROM runs WHERE project_id = ?"
                                        " AND workflow = 'index' ORDER BY rowid DESC LIMIT 1", (project_id,)).fetchone()

    rebuilding, run = await asyncio.to_thread(state["db"].read, latest)
    # Its status worked out once (a run may be released between two looks at the registry).
    run, status = next(((r, "running") for r in rebuilding if derived_status(r[1], r[0], registry) == "running"),
                       (run, run and derived_status(run[1], run[0], registry)))
    active = run and registry.runs.get(run[0])
    index = state.get("index")
    if index is not None and (index.closed or index.unusable):
        index = None
    counts = await _counts(index, project_id) if index is not None else {}
    mode, reason = search_mode(state)
    return {"state": "unavailable" if index is None else "building" if index.building or index.damaged else "ready",
            "mode": mode, "reason": reason,
            "passages": {"indexed": sum(c[0] for c in counts.values()), "embedded": sum(c[1] for c in counts.values()),
                         "embeddable": sum(c[2] for c in counts.values())},
            "materials": {m: {"indexed": c[0], "embedded": c[1], "embeddable": c[2]} for m, c in counts.items()},
            "run": {"run_id": run[0], "status": status, "rebuild": bool(run[2]),
                    "progress": active.progress if active is not None else None} if run else None}


@router.post("/api/projects/{project_id}/index/rebuild", status_code=202)
async def rebuild(project_id: str, request: Request):
    """Rebuild the project's index from the main database: keyword rows at once, then embeddings, as an
    index run. A rebuild already running is the one returned."""
    harness = _state(request)["harness"]

    def record(conn, ids):
        if conn.execute("SELECT 1 FROM projects WHERE id = ?", (project_id,)).fetchone() is None:
            raise AdmissionError(404, "not_found", "No such project")
        for (run,) in conn.execute("SELECT id FROM runs WHERE project_id = ? AND workflow = 'index' AND status = 'running'"
                                   " AND json_extract(inputs, '$.rebuild')", (project_id,)).fetchall():
            if derived_status("running", run, harness.registry) == "running":
                return run, []
        materials = [m for (m,) in conn.execute("SELECT id FROM materials WHERE project_id = ? ORDER BY created_at",
                                                (project_id,))]
        _insert_run(conn, ids[0], project_id, {"material_ids": materials, "rebuild": True})
        return ids[0], ids

    return {"run_id": await harness.record_background(1, record)}

