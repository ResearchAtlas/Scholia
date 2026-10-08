"""Materials: adding files, reading them into passages, looking up their identifiers, and the
Library's API (slice-1 spec F3a, sections 3 S7, 4.2, 5, 7.1, 10 and 13; ADR 0001; ticket 71).

Adding files (`POST /api/projects/{id}/materials`): each file is hashed and stored once in the
content store, then one transaction writes, per file, the project's material and its first
version (or a new version of a material being replaced), and the background runs that follow:
- an `extract` run per new version whose file has no extraction yet. An extraction finished by
  another project is shared instead, and its passages are queued for this project's index at once.
- one `lookup` run for the batch, unless the project is review-locked, which never looks up.
A request that fails validation writes nothing; the same file added again to the same project
reports the paper it already is.

An `extract` run reads its version's file (backend/extraction.py) within EXTRACTION_SECONDS and
writes the extraction, its passages and the index queue's `add` rows for its project in its
terminal transaction, so a partial extraction is never written and a revoked one writes nothing.
Extractions are shared by file and extractor version; a run that finds one written meanwhile uses it.
Each version is read as the type its file was detected as when it was added (its media_type), so
the same bytes added as Markdown and as LaTeX are two extractions of one stored file.

A `lookup` run waits for its materials' extractions, takes each material's first identifier from
the text of the version it was made for (`inputs.versions`; extraction.identifiers), and resolves
the distinct ones (backend/lookup.py) through the outbound gate with the dispatch check. A Local
only project first asks once for the batch, through the shared confirmation (backend/asks.py),
naming the services and the number of distinct identifiers; the answer covers this run and its
retries only. Each material's metadata is written in its own transaction while the run is running
and not revoked, and only while that version is still the material's current file: a file replaced
meanwhile makes the older lookup stale, so its identifier is not sent (its ask is closed) and its
answer is not applied. Metadata the researcher edited is never replaced, though the retraction
check is recorded. A failed lookup leaves the material
as it was. Both runs name their materials in `inputs.material_ids`, the deletion service's scope
link (backend/db/deletion.py), so deleting a material revokes them.

A paper reads Reading while its version's extraction runs, Ready once an extraction with text is
committed, and Needs attention otherwise, with its reason.
"""

import asyncio
import base64
import binascii
import json
import logging
import time

from fastapi import APIRouter, Depends, Request
from fastapi.responses import Response
from pydantic import BaseModel, Field

from backend import asks, backups, extraction, lookup
from backend.db import ContentCorruptError, delete, new_id, utc_now
from backend.outbound_gate import OutboundDenied
from backend.runs import (AdmissionError, RunOutcome, _event, _revoked, _running, _through, derived_status,
                          may_dispatch)
from backend.settings import visible

log = logging.getLogger(__name__)

EXTRACTION_SECONDS = 30 * 60  # per material version (section 13)
MAX_FILES = 20  # per request
WAIT_SECONDS = 0.5  # a lookup's look at whether its materials have been read, and at its ask
PASSAGE_PAGE = 500
_WORKFLOWS = ("extract", "lookup")
_SHARED = ("complete", "ocr_needed")  # an extraction's statuses: written whole, never partly


def _no_store(response: Response):
    """No answer about a material is kept in the browser's cache, so none outlives its deletion there."""
    response.headers["Cache-Control"] = "no-store"


router = APIRouter(dependencies=[Depends(_no_store)])


class NewFile(BaseModel):
    name: str = Field(min_length=1, max_length=255)
    data: str = Field(min_length=1, max_length=(extraction.MAX_FILE_BYTES + 2) // 3 * 4 + 4)  # base64


class Upload(BaseModel):
    files: list[NewFile] = Field(min_length=1, max_length=MAX_FILES)
    conversation_id: str | None = Field(default=None, max_length=100)  # attached in a conversation
    material_id: str | None = Field(default=None, max_length=100)  # a new version of this material


class MaterialChange(BaseModel):
    title: str | None = Field(default=None, max_length=1000)
    authors: list[str] | None = Field(default=None, max_length=100)  # "Family, Given" or a name, one each
    year: int | None = Field(default=None, ge=1000, le=2200)
    venue: str | None = Field(default=None, max_length=1000)
    doi: str | None = Field(default=None, max_length=300)


def _state(request):
    return request.app.state.scholia


def _refused(status, code, message):
    return AdmissionError(status, code, message)


# Adding files


@router.post("/api/projects/{project_id}/materials", status_code=201)
async def add_files(project_id: str, body: Upload, request: Request):
    state = _state(request)
    db, content, harness = state["db"], state["content"], state["harness"]
    if body.material_id is not None and len(body.files) != 1:
        raise _refused(400, "invalid_request", "A file replaces one material")
    files = []
    for item in body.files:
        name = visible(item.name)
        if name is None:
            raise _refused(400, "invalid_request", "A file needs a name")
        try:
            data = base64.b64decode(item.data, validate=True)
        except (binascii.Error, ValueError):
            raise _refused(400, "invalid_request", "A file's data is not base64") from None
        if len(data) > extraction.MAX_FILE_BYTES:
            raise _refused(413, "file_too_large", "The file is too large")
        kind = extraction.media_type(name, data)
        if kind is None:
            raise _refused(400, "unsupported_file", "Scholia reads PDF, DOCX, HTML, Markdown and LaTeX files")
        files.append((name, data, kind))

    def check(conn):
        if conn.execute("SELECT 1 FROM projects WHERE id = ?", (project_id,)).fetchone() is None:
            raise _refused(404, "not_found", "No such project")
        if body.conversation_id is not None and conn.execute(
                "SELECT 1 FROM conversations WHERE id = ? AND project_id = ?",
                (body.conversation_id, project_id)).fetchone() is None:
            raise _refused(404, "not_found", "No such conversation")
        if body.material_id is not None and conn.execute(
                "SELECT 1 FROM materials WHERE id = ? AND project_id = ?", (body.material_id, project_id)).fetchone() is None:
            raise _refused(404, "not_found", "No such material")

    await asyncio.to_thread(db.read, check)
    stored = []
    for name, data, kind in files:  # stored before the records: a file no record names is collected later
        stored.append((name, await asyncio.to_thread(content.put, data, kind), kind))

    def record(conn, ids):  # ids: one for each file's reading and one for the lookup, each used only if needed
        check(conn)
        ids, used = iter(ids), []
        locked = conn.execute("SELECT review_lock FROM projects WHERE id = ?", (project_id,)).fetchone()[0]
        added, looked_up = [], {}  # looked_up: {material id: the version its lookup is for}
        for name, sha256, kind in stored:
            if body.material_id is not None:
                material = body.material_id
                (seq,) = conn.execute("SELECT coalesce(max(seq) + 1, 0) FROM material_versions WHERE material_id = ?",
                                      (material,)).fetchone()
                conn.execute("UPDATE material_versions SET is_current = 0 WHERE material_id = ?", (material,))
                conn.execute("UPDATE materials SET updated_at = ? WHERE id = ?", (utc_now(), material))
            else:
                same = conn.execute(
                    "SELECT m.id FROM materials m JOIN material_versions v ON v.material_id = m.id AND v.is_current = 1"
                    " WHERE m.project_id = ? AND v.file_sha256 = ?", (project_id, sha256)).fetchone()
                if same is not None:
                    added.append({"id": same[0], "existing": True})
                    continue
                material, seq = new_id(), 0
                title = name.rsplit(".", 1)[0].strip() or name
                conn.execute("INSERT INTO materials (id, project_id, title, source, evidence_type, proposed_by)"
                             " VALUES (?, ?, ?, 'upload', 'full_text', 'researcher')", (material, project_id, title[:500]))
            version = new_id()
            conn.execute("INSERT INTO material_versions (id, material_id, seq, file_sha256, is_current, media_type)"
                         " VALUES (?, ?, ?, ?, 1, ?)", (version, material, seq, sha256, kind))
            extractor, extractor_version = extraction.extractor_of(kind)
            shared = conn.execute(
                f"SELECT id FROM extractions WHERE file_sha256 = ? AND extractor = ? AND extractor_version = ?"
                f" AND status IN {_SHARED}", (sha256, extractor, extractor_version)).fetchone()
            run = None
            if shared is not None:  # read already, for another project or version: shared, not read again
                _queue_adds(conn, shared[0], project_id)
            else:
                run = next(ids)
                used.append(run)
                conn.execute("INSERT INTO runs (id, project_id, kind, workflow, inputs) VALUES (?, ?, 'background',"
                             " 'extract', ?)", (run, project_id, json.dumps({"material_ids": [material],
                                                                             "version_id": version})))
            added.append({"id": material, "existing": False, "version_id": version, "run_id": run})
            looked_up[material] = version
        lookup_run = None
        if looked_up and not locked:  # a review-locked project never looks identifiers up
            lookup_run = next(ids)
            used.append(lookup_run)
            origin = {"conversation_id": body.conversation_id} if body.conversation_id else None
            conn.execute("INSERT INTO runs (id, project_id, kind, workflow, inputs) VALUES (?, ?, 'background',"
                         " 'lookup', ?)", (lookup_run, project_id, json.dumps({"material_ids": list(looked_up),
                                                                                "versions": looked_up, "origin": origin})))
        conn.execute("UPDATE projects SET updated_at = ? WHERE id = ?", (utc_now(), project_id))
        return {"materials": added, "lookup_run_id": lookup_run}, used

    # The runs start with the commit (record_background): none is ever running in the record and not held.
    return await harness.record_background(len(stored) + 1, record)


async def _to_end(awaitable):
    """A write followed through even when the request goes away (see backend.app._to_end)."""
    result, cancelled = await _through(awaitable)
    if cancelled:
        raise asyncio.CancelledError()
    return result


def _queue_adds(conn, extraction_id, project_id):
    """Queue the extraction's passages for the project's search index (S1-17 applies the queue;
    adding a passage the project's index holds already changes nothing there)."""
    conn.execute("INSERT INTO index_queue (target, target_id, project_id, op)"
                 " SELECT 'passage', id, ?, 'add' FROM passages WHERE extraction_id = ? ORDER BY ordinal",
                 (project_id, extraction_id))


# Reading


class _Stop(Exception):
    pass


class _TimeLimit(Exception):
    pass


def register(harness, content):
    """The extract and lookup workflows, as the harness's local background work, on its content store."""
    pace = lookup.Pace()  # shared by this harness's lookups, so each source's spacing holds across runs

    async def extract_run(harness, active, project_id, inputs):
        version_id = inputs["version_id"]
        row = await asyncio.to_thread(harness.db.read, lambda conn: conn.execute(
            f"SELECT v.file_sha256, {_VERSION_TYPE} FROM material_versions v JOIN content_files c"
            " ON c.sha256 = v.file_sha256 WHERE v.id = ?", (version_id,)).fetchone())
        if row is None or row[1] not in extraction.EXTRACTORS:
            raise RunOutcome("failed", "not_found")
        sha256, kind = row
        deadline = time.monotonic() + EXTRACTION_SECONDS

        def stop():
            if active.cancel_requested.is_set():
                raise _Stop()
            if time.monotonic() > deadline:
                raise _TimeLimit()

        def progress(done, total):
            active.progress = {"done": done, "total": total}

        def read():
            return extraction.extract(content.read(sha256), kind, stop, progress)

        try:
            extracted = await harness.work(active, read)
        except _TimeLimit:
            raise RunOutcome("cancelled", "time_limit", "limit") from None
        except extraction.Unreadable as unreadable:
            raise RunOutcome("failed", unreadable.code) from None
        except (FileNotFoundError, ContentCorruptError):
            raise RunOutcome("failed", "file_missing") from None
        summary = {"passages": len(extracted.passages), "pages": extracted.pages, "ocr_pages": extracted.ocr_pages}
        return summary, lambda conn: _store(conn, version_id, sha256, extracted)

    async def lookup_run(harness, active, project_id, inputs):
        return await _look_up(harness, active, project_id, inputs, pace)

    harness.workflows["extract"] = extract_run
    harness.workflows["lookup"] = lookup_run


def _store(conn, version_id, sha256, extracted):
    """The extraction, its passages and the project's index queue rows, in the run's terminal
    transaction; an extraction of the same file and version written meanwhile is shared instead."""
    found = conn.execute("SELECT m.project_id FROM material_versions v JOIN materials m ON m.id = v.material_id"
                         " WHERE v.id = ?", (version_id,)).fetchone()
    if found is None:
        raise RunOutcome("failed", "not_found")
    shared = conn.execute(f"SELECT id FROM extractions WHERE file_sha256 = ? AND extractor = ? AND extractor_version = ?"
                          f" AND status IN {_SHARED}",
                          (sha256, extracted.extractor, extracted.version)).fetchone()
    if shared is not None:
        extraction_id = shared[0]
    else:
        extraction_id = new_id()
        conn.execute("INSERT INTO extractions (id, file_sha256, extractor, extractor_version, status, pages, ocr_pages)"
                     " VALUES (?, ?, ?, ?, ?, ?, ?)",
                     (extraction_id, sha256, extracted.extractor, extracted.version,
                      "ocr_needed" if extracted.ocr_pages else "complete", extracted.pages, extracted.ocr_pages))
        conn.executemany(
            "INSERT INTO passages (id, extraction_id, ordinal, page, section_path, kind, text, char_start, char_end, boxes)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            [(new_id(), extraction_id, ordinal, p.page, json.dumps(p.section_path), p.kind, p.text, p.char_start,
              p.char_end, json.dumps(p.boxes) if p.boxes else None) for ordinal, p in enumerate(extracted.passages)])
    _queue_adds(conn, extraction_id, found[0])


# Looking up identifiers


async def _look_up(harness, active, project_id, inputs, pace):
    run_id, db = active.run_id, harness.db

    async def read(fn):
        return await asyncio.to_thread(db.read, fn)

    async def write(fn):
        return await asyncio.to_thread(db.write, fn)

    materials = inputs.get("material_ids") or []
    started = time.monotonic()
    while await read(lambda conn: conn.execute(  # its materials are read first
            "SELECT count(*) FROM runs r, json_each(r.inputs, '$.material_ids') j WHERE r.workflow = 'extract'"
            " AND r.status = 'running' AND r.project_id = ? AND j.value IN (SELECT value FROM json_each(?))",
            (project_id, json.dumps(materials))).fetchone()[0]) and time.monotonic() - started < EXTRACTION_SECONDS + 60:
        await asyncio.sleep(WAIT_SECONDS)

    wanted = await read(lambda conn: _identifiers(conn, project_id, materials, inputs.get("versions") or {}))
    distinct = list(dict.fromkeys(found for _, found in wanted.values() if found))
    if not await _write_if_running(write, run_id, lambda conn: [
            _event(conn, run_id, "step_finished", {"material_id": material, "identifier": None, "outcome": "no_identifier"})
            for material, (_, found) in wanted.items() if found is None]):
        return {"identifiers": len(distinct)}, None
    active.progress = {"done": 0, "total": len(distinct)}
    if not distinct:
        return {"identifiers": 0}, None
    policy = await read(lambda conn: conn.execute(
        "SELECT sensitivity, review_lock FROM projects WHERE id = ?", (project_id,)).fetchone())
    if policy is None:  # deleted meanwhile, its runs with it
        raise RunOutcome("cancelled", "project_changed", "revoked")
    level, locked = policy
    if locked:  # never looks up (its run is revoked when the lock comes)
        raise RunOutcome("failed", "lookup_locked")
    approved = False
    if level == "local_only":
        approved = await _approval(read, write, run_id, project_id, inputs, distinct, wanted)
    resolved, missed, asking, stale = 0, [], [[]], []  # asking[0]: the papers the identifier in hand is for

    def admit(conn):
        """The gate's check of each request, in the transaction that decides it (after the source's turn,
        before every attempt): the run may still send, and at least one of its papers still has the file
        the identifier came from. A replaced one is refused, so its identifier is never sent."""
        if not may_dispatch(conn, run_id):
            return False
        if not _current(conn, asking[0]):
            stale.append(True)
            return False
        return True

    async with harness.gate.async_client(project_id, approved=approved, admit=admit) as client:
        for done, (scheme, value) in enumerate(distinct, start=1):
            mine = asking[0] = [(m, version) for m, (version, found) in wanted.items() if found == (scheme, value)]
            if not await read(lambda conn: _current(conn, mine)):  # every paper it was for has another file now
                found, outcome = None, "replaced"
            else:
                try:
                    found, outcome = await lookup.resolve(client, scheme, value, pace), "resolved"
                except lookup.Failed as failed:
                    found, outcome = None, failed.code
                except OutboundDenied as denied:
                    if denied.reason == "revoked" and stale:  # its papers' files were replaced as it waited
                        stale.clear()
                        found, outcome = None, "replaced"
                    elif denied.reason == "revoked":  # deleted, tightened or locked meanwhile: nothing more is sent
                        raise RunOutcome("cancelled", "project_changed", "revoked") from None
                    else:
                        found, outcome = None, "refused"
            if not await _write_if_running(write, run_id, lambda conn: _apply(conn, run_id, project_id, mine, scheme,
                                                                              value, found, outcome)):
                break
            resolved += found is not None
            missed += [outcome] if outcome in ("unavailable", "refused") else []
            active.progress = {"done": done, "total": len(distinct)}
    if missed:  # a source gave no answer (or could not be asked): the run failed, and Retry asks again
        raise RunOutcome("failed", "unavailable" if "unavailable" in missed else "refused")
    return {"identifiers": len(distinct), "resolved": resolved}, None


async def _write_if_running(write, run_id, fn):
    """fn's write, made only while the run is running and not revoked; whether it was made."""
    def guarded(conn):
        if not _running(conn, run_id) or _revoked(conn, run_id):
            return False
        fn(conn)
        return True
    return await write(guarded)


def _identifiers(conn, project_id, materials, versions):
    """{material id: (version id, (scheme, identifier) or None)}: the first DOI the version's own text
    gives, else the first arXiv ID. The version is the one the lookup was made for (versions), or the
    current one for a lookup that names none; materials gone since are left out. A result is about
    that version, and is sent and applied only while it is still the material's current file."""
    found = {}
    for material in materials:
        row = conn.execute(f"SELECT v.id, v.file_sha256, {_VERSION_TYPE} FROM materials m JOIN material_versions v"
                           " ON v.material_id = m.id AND (v.id = ?3 OR (?3 IS NULL AND v.is_current = 1))"
                           " LEFT JOIN content_files c ON c.sha256 = v.file_sha256 WHERE m.id = ?1 AND m.project_id = ?2",
                           (material, project_id, versions.get(material))).fetchone()
        if row is None:
            continue
        extracted = _extraction(conn, *row[1:])
        # Read in order only as far as identifiers() looks (its first pages or characters), however
        # many passages that takes: the cursor is read lazily, and identifiers() stops at its window.
        passages = (extraction.Passage(kind, text, page) for kind, text, page in conn.execute(
            "SELECT kind, text, page FROM passages WHERE extraction_id = ? ORDER BY ordinal",
            (extracted[0],))) if extracted else ()
        ids = extraction.identifiers(passages)
        found[material] = (row[0], next((i for i in ids if i[0] == "doi"), None) or next(iter(ids), None))
    return found


def _current(conn, pairs):
    """Of [(material id, version id)], those whose version is still its material's current one."""
    return [(m, v) for m, v in pairs if conn.execute(
        "SELECT 1 FROM material_versions WHERE id = ? AND material_id = ? AND is_current = 1", (v, m)).fetchone()]


async def _approval(read, write, run_id, project_id, inputs, distinct, wanted):
    """For a Local only project: the researcher's answer to this batch's ask, asked once. True to look
    up; the run ends cancelled when the researcher declines, and looks up without asking once the
    project no longer needs it (made less strict), closing the ask. The ask records the identifiers
    it asked about, and its answer covers exactly those: a run that finds others to send (after a
    restart, its paper's file replaced meanwhile) closes it and asks again. An ask whose papers all
    have another file since is closed (replaced): there is nothing left for it to send."""
    covers = sorted(f"{scheme}:{value}" for scheme, value in distinct)
    asked_for = [(m, v) for m, (v, found) in wanted.items() if found]  # the papers whose identifiers it covers
    ask, answer = await read(lambda conn: asks.asked(conn, run_id))
    if not await read(lambda conn: _current(conn, asked_for)):  # every paper it was for has another file now
        if ask is not None:
            await write(lambda conn: asks.withdraw(conn, run_id, ask["ask_id"], "replaced"))
        return False
    if ask is None or ask.get("covers") != covers:
        services = sorted({service for scheme, _ in distinct for service in lookup.SERVICES[scheme]})

        def ask_again(conn):
            if ask is not None:
                asks.withdraw(conn, run_id, ask["ask_id"], "identifiers_changed")
            return asks.raise_ask(conn, run_id, "identifier_lookup", ["lookup", "skip"],
                                  {"services": services, "identifiers": len(distinct)}, inputs.get("origin"), covers)
        try:
            ask_id = await write(ask_again)
        except asks.AskRefused:  # revoked or ended meanwhile
            raise RunOutcome("cancelled", "project_changed", "revoked") from None
    else:
        ask_id = ask["ask_id"]
    while True:
        _, answer = await read(lambda conn: asks.asked(conn, run_id))
        if answer is not None:
            if answer.get("withdrawn"):
                return False
            if answer.get("option") == "lookup":
                return True
            raise RunOutcome("cancelled", "declined", "researcher")
        level = await read(lambda conn: conn.execute("SELECT sensitivity FROM projects WHERE id = ?",
                                                     (project_id,)).fetchone())
        if level is None:
            raise RunOutcome("cancelled", "project_changed", "revoked")
        if level[0] != "local_only" and await write(lambda conn: asks.withdraw(conn, run_id, ask_id, "policy_changed")):
            return False
        if not await read(lambda conn: _current(conn, asked_for)) \
                and await write(lambda conn: asks.withdraw(conn, run_id, ask_id, "replaced")):
            return False
        await asyncio.sleep(WAIT_SECONDS)


def _apply(conn, run_id, project_id, pairs, scheme, value, found, outcome):
    """An identifier's outcome for the papers it was found in, [(material id, version id)]: their
    metadata is written only while that version is still the material's current file; a paper whose
    file was replaced meanwhile keeps what its newer file gives (outcome replaced)."""
    now = utc_now()
    for material, version in pairs:
        row = conn.execute("SELECT m.checked_by, v.id = ? FROM materials m JOIN material_versions v"
                           " ON v.material_id = m.id AND v.is_current = 1 WHERE m.id = ? AND m.project_id = ?",
                           (version, material, project_id)).fetchone()
        if row is None:
            continue
        mine, result = (found, outcome) if row[1] else (None, "replaced")
        if mine is not None:
            if row[0] != "researcher":  # what the researcher edited stays
                conn.execute("UPDATE materials SET title = ?, csl = ?, source_key = ?, resolved_at = ?, checked_at = ?,"
                             " checked_by = 'lookup', updated_at = ? WHERE id = ?",
                             (mine.csl["title"], json.dumps(mine.csl), mine.source_key, now, now, now, material))
            if mine.retracted is not None:
                conn.execute("UPDATE materials SET retraction = ?, retraction_checked_at = ? WHERE id = ?",
                             ("retracted" if mine.retracted else "none", now, material))
        _event(conn, run_id, "step_finished", {"material_id": material, "identifier": f"{scheme}:{value}",
                                               "source": mine.source if mine else None, "outcome": result})


# The Library


# A version's media type: its own, or its stored file's for a version added before it had one.
_VERSION_TYPE = "coalesce(v.media_type, c.media_type)"


def _extraction(conn, sha256, kind):
    """The latest extraction of the file as a file of that media type (by its extractor): (id,
    extractor, version, status, pages, ocr_pages, passages), or None."""
    if sha256 is None or kind not in extraction.EXTRACTORS:
        return None
    return conn.execute(
        "SELECT e.id, e.extractor, e.extractor_version, e.status, e.pages, e.ocr_pages,"
        " (SELECT count(*) FROM passages WHERE extraction_id = e.id) FROM extractions e"
        f" WHERE e.file_sha256 = ? AND e.extractor = ? AND e.status IN {_SHARED} ORDER BY e.rowid DESC LIMIT 1",
        (sha256, extraction.EXTRACTORS[kind][0])).fetchone()


_MATERIAL_COLUMNS = ("m.id, m.project_id, m.title, m.csl, m.source, m.source_key, m.evidence_type, m.resolved_at,"
                     " m.checked_at, m.checked_by, m.retraction, m.retraction_checked_at, m.created_at, m.updated_at,"
                     f" v.id, v.seq, v.file_sha256, {_VERSION_TYPE}, c.size, v.created_at")


def _describe(conn, row, registry):
    """A material as the Library shows it, with its state: reading, ready or needs_attention (and why)."""
    (material, project, title, csl, source, source_key, evidence, resolved_at, checked_at, checked_by, retraction,
     retraction_checked_at, created_at, updated_at, version, seq, sha256, media_type, size, version_at) = row
    extracted = _extraction(conn, sha256, media_type)
    run = conn.execute("SELECT id, status, cancel_reason, summary FROM runs WHERE workflow = 'extract'"
                       " AND json_extract(inputs, '$.version_id') = ? ORDER BY rowid DESC LIMIT 1",
                       (version,)).fetchone() if version else None
    state, reason, progress = "ready", None, None
    if sha256 is None:
        pass  # metadata only
    elif run is not None and run[1] == "running":
        state = "reading"
        active = registry.runs.get(run[0])
        progress = active.progress if active is not None else None
    elif extracted is not None:
        if extracted[5]:
            state, reason = "needs_attention", "ocr_waiting"
        elif not extracted[6]:
            state, reason = "needs_attention", "no_text"
    elif run is None:
        state, reason = "needs_attention", "not_read"
    else:
        state = "needs_attention"
        reason = (json.loads(run[3] or "{}").get("reason") or
                  {"limit": "time_limit"}.get(run[2]) or ("stopped" if run[1] == "cancelled" else run[1]))
    found = conn.execute(
        "SELECT r.id, r.status, r.cancel_reason, r.waiting FROM runs r, json_each(r.inputs, '$.material_ids') j"
        " WHERE r.workflow = 'lookup' AND j.value = ? ORDER BY r.rowid DESC LIMIT 1", (material,)).fetchone()
    looked = None
    if found is not None:
        event = conn.execute("SELECT data FROM run_events WHERE run_id = ? AND type = 'step_finished'"
                             " AND json_extract(data, '$.material_id') = ? ORDER BY seq DESC LIMIT 1",
                             (found[0], material)).fetchone()
        outcome = json.loads(event[0]) if event else {}
        looked = {"run_id": found[0], "status": derived_status(found[1], found[0], registry),
                  "cancel_reason": found[2], "waiting": found[3] == "ask", "outcome": outcome.get("outcome"),
                  "identifier": outcome.get("identifier"), "source": outcome.get("source")}
    return {
        "id": material, "project_id": project, "title": title, "csl": json.loads(csl) if csl else {},
        "source": source, "source_key": source_key, "evidence_type": evidence, "resolved_at": resolved_at,
        "checked_at": checked_at, "checked_by": checked_by, "retraction": retraction,
        "retraction_checked_at": retraction_checked_at, "created_at": created_at, "updated_at": updated_at,
        "state": state, "reason": reason, "progress": progress,
        "version": {"id": version, "seq": seq, "media_type": media_type, "size": size, "created_at": version_at}
        if version else None,
        "extraction": {"extractor": extracted[1], "version": extracted[2], "status": extracted[3],
                       "pages": extracted[4], "ocr_pages": extracted[5], "passages": extracted[6]} if extracted else None,
        "reading": {"run_id": run[0], "status": derived_status(run[1], run[0], registry)} if run else None,
        "lookup": looked,
    }


@router.get("/api/projects/{project_id}/materials")
async def list_materials(project_id: str, request: Request):
    """The project's papers, newest first, with the asks of its runs that wait for the researcher."""
    state = _state(request)
    registry = state["harness"].registry

    def listing(conn):
        if conn.execute("SELECT 1 FROM projects WHERE id = ?", (project_id,)).fetchone() is None:
            raise _refused(404, "not_found", "No such project")
        rows = conn.execute(
            f"SELECT {_MATERIAL_COLUMNS} FROM materials m LEFT JOIN material_versions v ON v.material_id = m.id"
            " AND v.is_current = 1 LEFT JOIN content_files c ON c.sha256 = v.file_sha256 WHERE m.project_id = ?"
            " ORDER BY m.created_at DESC, m.id", (project_id,)).fetchall()
        return {"materials": [_describe(conn, row, registry) for row in rows],
                "asks": asks.open_asks(conn, project_id=project_id)}

    return await asyncio.to_thread(state["db"].read, listing)


def _material_row(conn, material_id):
    return conn.execute(
        f"SELECT {_MATERIAL_COLUMNS} FROM materials m LEFT JOIN material_versions v ON v.material_id = m.id"
        " AND v.is_current = 1 LEFT JOIN content_files c ON c.sha256 = v.file_sha256 WHERE m.id = ?",
        (material_id,)).fetchone()


@router.get("/api/materials/{material_id}")
async def get_material(material_id: str, request: Request):
    state = _state(request)

    def fetch(conn):
        row = _material_row(conn, material_id)
        if row is None:
            raise _refused(404, "not_found", "No such material")
        return _describe(conn, row, state["harness"].registry)

    return await asyncio.to_thread(state["db"].read, fetch)


@router.patch("/api/materials/{material_id}")
async def change_material(material_id: str, body: MaterialChange, request: Request):
    """The researcher's edit of a paper's details: kept as checked by the researcher, so no lookup
    replaces it. Typed text needs a visible character; a DOI must fit the pattern."""
    state = _state(request)
    changes = body.model_dump(exclude_unset=True)
    if "title" in changes and visible(changes["title"]) is None:
        raise _refused(400, "title_needed", "A paper needs a title")
    doi = None
    if changes.get("doi"):
        doi = extraction.clean_doi(changes["doi"].strip().removeprefix("https://doi.org/").removeprefix("doi:"))
        if doi is None:
            raise _refused(400, "invalid_doi", "That is not a DOI")

    def update(conn):
        row = conn.execute("SELECT title, csl FROM materials WHERE id = ?", (material_id,)).fetchone()
        if row is None:
            raise _refused(404, "not_found", "No such material")
        csl = json.loads(row[1]) if row[1] else {}
        title = visible(changes["title"]) if "title" in changes else row[0]
        csl["title"] = title
        if "authors" in changes:
            authors = [_author(name) for name in changes["authors"] or [] if visible(name)]
            csl.pop("author", None) if not authors else csl.__setitem__("author", authors)
        if "year" in changes:
            csl.pop("issued", None) if changes["year"] is None else csl.__setitem__(
                "issued", {"date-parts": [[changes["year"]]]})
        if "venue" in changes:
            venue = visible(changes["venue"] or "")
            csl.pop("container-title", None) if venue is None else csl.__setitem__("container-title", venue)
        if "doi" in changes:
            csl.pop("DOI", None) if doi is None else csl.__setitem__("DOI", doi)
        now = utc_now()
        conn.execute("UPDATE materials SET title = ?, csl = ?, checked_by = 'researcher', checked_at = ?, updated_at = ?"
                     " WHERE id = ?", (title, json.dumps(csl), now, now, material_id))
        return _describe(conn, _material_row(conn, material_id), state["harness"].registry)

    return await _to_end(asyncio.to_thread(state["db"].write, update))


def _author(name):
    name = visible(name)
    family, comma, given = name.partition(",")
    if comma and visible(family) and visible(given):
        return {"family": visible(family), "given": visible(given)}
    return {"literal": name}


@router.delete("/api/materials/{material_id}")
async def delete_material(material_id: str, request: Request, purge_backups: bool = False,
                          remove_all_trace: bool = False):
    """Delete a paper through the deletion service, which revokes its reading and lookup in the same
    transaction; with purge_backups, its older backups go too."""
    state = _state(request)
    harness, loop = state["harness"], asyncio.get_running_loop()
    project = await asyncio.to_thread(state["db"].read, lambda conn: conn.execute(
        "SELECT project_id FROM materials WHERE id = ?", (material_id,)).fetchone())
    if project is None:
        raise _refused(404, "not_found", "No such material")

    async def deleting():
        revoked = await asyncio.to_thread(
            delete, state["db"], state["content"], "material", material_id, remove_all_trace=remove_all_trace,
            on_committed=lambda ids: loop.call_soon_threadsafe(harness.revoke, ids))
        harness.revoke(revoked)
        if not purge_backups:
            return {"ok": True}
        try:
            async with state["backups_lock"]:
                purged = await asyncio.to_thread(backups.purge, state["db"], project[0],
                                                 {"kind": "material", "object_id": material_id})
        except Exception as error:
            log.warning("purging the backups after a deletion failed (%s)", type(error).__name__)
            purged = {"purge_failed": True}
        return {"ok": True, **purged}

    try:
        return await _to_end(deleting())
    except LookupError:
        raise _refused(404, "not_found", "No such material") from None


@router.get("/api/materials/{material_id}/versions")
async def material_versions(material_id: str, request: Request):
    def fetch(conn):
        if conn.execute("SELECT 1 FROM materials WHERE id = ?", (material_id,)).fetchone() is None:
            raise _refused(404, "not_found", "No such material")
        rows = conn.execute(f"SELECT v.id, v.seq, v.is_current, v.file_sha256, {_VERSION_TYPE}, c.size, v.created_at"
                            " FROM material_versions v LEFT JOIN content_files c ON c.sha256 = v.file_sha256"
                            " WHERE v.material_id = ? ORDER BY v.seq", (material_id,)).fetchall()
        return {"versions": [{"id": v, "seq": seq, "is_current": bool(current), "media_type": kind, "size": size,
                              "created_at": at, "extraction": (lambda e: e and {"status": e[3], "pages": e[4],
                                                                                "passages": e[6]})(_extraction(conn, sha, kind))}
                             for v, seq, current, sha, kind, size, at in rows]}

    return await asyncio.to_thread(_state(request)["db"].read, fetch)


def _passage(row):
    pid, ordinal, page, path, kind, text, start, end, boxes = row
    return {"id": pid, "ordinal": ordinal, "page": page, "section_path": json.loads(path) if path else [],
            "kind": kind, "text": text, "char_start": start, "char_end": end,
            "boxes": json.loads(boxes) if boxes else None}


_PASSAGE_COLUMNS = "id, ordinal, page, section_path, kind, text, char_start, char_end, boxes"


@router.get("/api/material-versions/{version_id}/passages")
async def version_passages(version_id: str, request: Request, offset: int = 0, limit: int = PASSAGE_PAGE):
    """A version's passages in order, a page at a time."""
    limit, offset = max(1, min(limit, PASSAGE_PAGE)), max(0, offset)

    def fetch(conn):
        row = conn.execute(f"SELECT v.file_sha256, {_VERSION_TYPE} FROM material_versions v"
                           " LEFT JOIN content_files c ON c.sha256 = v.file_sha256 WHERE v.id = ?", (version_id,)).fetchone()
        if row is None:
            raise _refused(404, "not_found", "No such version")
        extracted = _extraction(conn, *row)
        if extracted is None:
            return {"passages": [], "total": 0}
        rows = conn.execute(f"SELECT {_PASSAGE_COLUMNS} FROM passages WHERE extraction_id = ? ORDER BY ordinal"
                            " LIMIT ? OFFSET ?", (extracted[0], limit, offset)).fetchall()
        return {"passages": [_passage(r) for r in rows], "total": extracted[6], "pages": extracted[4]}

    return await asyncio.to_thread(_state(request)["db"].read, fetch)


@router.get("/api/passages/{passage_id}")
async def get_passage(passage_id: str, request: Request):
    """One passage with its quote selector (its text, and the text around it) and the materials whose
    current file it comes from."""
    def fetch(conn):
        row = conn.execute(f"SELECT {_PASSAGE_COLUMNS}, extraction_id FROM passages WHERE id = ?",
                           (passage_id,)).fetchone()
        if row is None:
            raise _refused(404, "not_found", "No such passage")
        before = conn.execute("SELECT text FROM passages WHERE extraction_id = ? AND ordinal = ?",
                              (row[9], row[1] - 1)).fetchone()
        after = conn.execute("SELECT text FROM passages WHERE extraction_id = ? AND ordinal = ?",
                             (row[9], row[1] + 1)).fetchone()
        materials = [(m, p, v) for m, p, v, kind, extractor in conn.execute(
            f"SELECT m.id, m.project_id, v.id, {_VERSION_TYPE}, e.extractor FROM extractions e JOIN material_versions v"
            " ON v.file_sha256 = e.file_sha256 JOIN content_files c ON c.sha256 = v.file_sha256"
            " JOIN materials m ON m.id = v.material_id WHERE e.id = ? ORDER BY m.created_at", (row[9],))
            if extraction.EXTRACTORS.get(kind, (None,))[0] == extractor]  # versions read by this extractor
        return {**_passage(row[:9]), "selector": {"type": "TextQuoteSelector", "exact": row[5],
                                                   "prefix": before[0][-32:] if before else "",
                                                   "suffix": after[0][:32] if after else ""},
                "materials": [{"id": m, "project_id": p, "version_id": v} for m, p, v in materials]}

    return await asyncio.to_thread(_state(request)["db"].read, fetch)


@router.get("/api/material-versions/{version_id}/pages/{number}")
async def page_image(version_id: str, number: int, request: Request, scale: float = 2.0):
    """A PDF page rendered as a PNG by pypdfium2, in memory; never written to disk."""
    state = _state(request)
    row = await asyncio.to_thread(state["db"].read, lambda conn: conn.execute(
        f"SELECT v.file_sha256, {_VERSION_TYPE} FROM material_versions v JOIN content_files c ON c.sha256 = v.file_sha256"
        " WHERE v.id = ?", (version_id,)).fetchone())
    if row is None:
        raise _refused(404, "not_found", "No such version")
    if row[1] != extraction.PDF:
        raise _refused(400, "not_a_pdf", "Only a PDF has page images")

    def render():
        return extraction.render_page(state["content"].read(row[0]), number, max(0.5, min(scale, 3.0)))

    try:
        image = await asyncio.to_thread(render)
    except IndexError:
        raise _refused(404, "not_found", "No such page") from None
    except (extraction.Unreadable, FileNotFoundError, ContentCorruptError):
        raise _refused(409, "file_missing", "The file cannot be read") from None
    return Response(image, media_type="image/png", headers={"Cache-Control": "no-store"})  # see _no_store


def run_details(conn, run_id, workflow, status, inputs):
    """What the background-run list shows of a material's run beside its status: the titles of the
    papers it works on (up to three, with their count), its open ask, and whether Retry applies."""
    if workflow not in _WORKFLOWS:
        return {}
    ids = json.loads(inputs or "{}").get("material_ids") or []
    titles = [title for (title,) in conn.execute(
        "SELECT title FROM materials WHERE id IN (SELECT value FROM json_each(?)) ORDER BY created_at LIMIT 3",
        (json.dumps(ids),))]
    (kept,) = conn.execute("SELECT count(*) FROM materials WHERE id IN (SELECT value FROM json_each(?))",
                           (json.dumps(ids),)).fetchone()
    ask = asks.open_asks(conn, run_id=run_id) if status == "running" else []
    return {"materials": {"titles": titles, "count": kept}, "ask": ask[0] if ask else None,
            "retryable": _retry(conn, run_id)[1] is None}


def _retry(conn, run_id):
    """A retry of a material's run: ((project id, workflow, the new run's inputs), None), or (None,
    (status, code, message)) saying why it cannot be tried again. The background-run list's Retry
    and the retry endpoint both ask this, so the list offers Retry exactly where the endpoint takes
    it: a run that failed, was stopped or was interrupted, whose papers are still here; a reading
    whose version is still current, unread and not being read; a lookup in a project not locked."""
    row = conn.execute("SELECT project_id, workflow, status, inputs FROM runs WHERE id = ?", (run_id,)).fetchone()
    if row is None:
        return None, (404, "not_found", "No such run")
    project_id, workflow, status, inputs = row
    inputs = json.loads(inputs or "{}")
    if workflow not in _WORKFLOWS or status not in ("failed", "cancelled", "interrupted"):
        return None, (409, "not_retryable", "This run cannot be tried again")
    kept = [m for m in inputs.get("material_ids", []) if conn.execute(
        "SELECT 1 FROM materials WHERE id = ? AND project_id = ?", (m, project_id)).fetchone()]
    if not kept:
        return None, (404, "not_found", "Its papers no longer exist")
    if workflow == "extract":
        version = conn.execute(f"SELECT v.file_sha256, {_VERSION_TYPE} FROM material_versions v LEFT JOIN"
                               " content_files c ON c.sha256 = v.file_sha256 WHERE v.id = ? AND v.is_current = 1",
                               (inputs.get("version_id"),)).fetchone()
        if version is None or _extraction(conn, *version) is not None or conn.execute(
                "SELECT 1 FROM runs WHERE workflow = 'extract' AND status = 'running'"
                " AND json_extract(inputs, '$.version_id') = ?", (inputs.get("version_id"),)).fetchone():
            return None, (409, "not_retryable", "This paper is read already, or being read")
        return (project_id, workflow, {"material_ids": kept, "version_id": inputs["version_id"]}), None
    if conn.execute("SELECT review_lock FROM projects WHERE id = ?", (project_id,)).fetchone()[0]:
        return None, (403, "lookup_locked", "A review-locked project never looks identifiers up")
    return (project_id, workflow, {"material_ids": kept, "origin": None, "versions": dict(conn.execute(
        "SELECT material_id, id FROM material_versions WHERE is_current = 1 AND material_id IN"
        " (SELECT value FROM json_each(?))", (json.dumps(kept),)).fetchall())}), None


@router.post("/api/runs/{run_id}/retry", status_code=201)
async def retry_run(run_id: str, request: Request):
    """Read a version again, or look a batch up again, after a run that failed, was stopped or was
    interrupted: a new run from the old one's inputs (see _retry). A Local only lookup asks again."""
    state = _state(request)

    def again(conn, ids):
        retry, refusal = _retry(conn, run_id)
        if refusal is not None:
            raise _refused(*refusal)
        project_id, workflow, inputs = retry
        conn.execute("INSERT INTO runs (id, project_id, kind, workflow, inputs) VALUES (?, ?, 'background', ?, ?)",
                     (ids[0], project_id, workflow, json.dumps(inputs)))
        return ids[0], ids

    return {"run_id": await state["harness"].record_background(1, again)}
