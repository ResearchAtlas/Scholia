"""Cited answers' API (slice-1 spec sections 9 and F11): a run's trace, paged, with the exact request
bodies it sent; and an answer's citations, resolved again or checked again.

- `GET /api/runs/{id}/trace?after=&limit=`: the run's events in order (route, retrieval_trace,
  step_started, model_attempt, step_finished, citation_checks, run_finished, ...), a page at a time.
  A deleted run's trace is gone with it.
- `GET /api/runs/{id}/trace/bodies/{sha256}`: the exact text one of the run's calls sent, from the
  content store (owner-only files); only a body the run keeps (run_bodies). A body removed because a
  paper whose passages it held was deleted answers 410 `body_removed`, and the trace marks its attempt
  (`body_removed`), never its text.
- `POST /api/citations/resolve`: each citation's existence now (a batch existence check): its passage
  still read by its paper, or its quote held by a current reading of the paper (status.md, decided
  2026-10-17 (8)), else `source_removed`; a paper not read yet by this app's extractor (its file just
  replaced, or being read again) keeps the stored existence. Nothing is written. The conversation read and
  the trace use it only to mark a found citation removed.
- `POST /api/citations/check`: the support judge again on an answer's citations, as background runs of
  the project (workflow `citation_check`), one per batch of up to `[retrieval] judge_pairs` pairs, so
  every pair is judged: each admitted and reserved like any model call and listed with its cost; on
  `[models] judge` when it is set (a judge that cannot run fails the run, visibly), else the answer's
  route. Each run names the exact (sentence, passage) pairs it judges, by the sentence's hash and the
  citations' ids, and a verdict is written only to a citation that still has that sentence and passage. The passages it sends are registered with
  the dispatch check, so one deleted or re-read before its dispatch stops it.
"""

import asyncio
import json

from fastapi import APIRouter, Request
from fastapi.responses import Response
from pydantic import BaseModel, Field

from backend import citations, governance, provenance
from backend.settings import load_settings, visible
from backend.db import ContentCorruptError
from backend.db.deletion import _reads_now
from backend.runs import AdmissionError
from backend.search_index import readings

router = APIRouter()
PAGE = 200


def _state(request):
    return request.app.state.scholia


class Resolve(BaseModel):
    citation_ids: list[str] = Field(min_length=1, max_length=200)


class Check(BaseModel):
    run_id: str = Field(max_length=64)


@router.get("/api/runs/{run_id}/trace")
async def trace(run_id: str, request: Request, after: int = -1, limit: int = PAGE):
    limit = max(1, min(limit, PAGE))

    def read(conn):
        if conn.execute("SELECT 1 FROM runs WHERE id = ?", (run_id,)).fetchone() is None:
            raise AdmissionError(404, "not_found", "No such run")
        rows = conn.execute("SELECT seq, at, type, data FROM run_events WHERE run_id = ? AND seq > ?"
                            " ORDER BY seq LIMIT ?", (run_id, after, limit + 1)).fetchall()
        kept = {sha for (sha,) in conn.execute("SELECT sha256 FROM run_bodies WHERE run_id = ?", (run_id,))}
        # A check's citations are read from their current rows (existence, support, sentence), which a
        # deletion changes; the event names each citation by id (backend/provenance.py).
        current = {cid: {"existence": existence, "support": support, "sentence": sentence}
                   for cid, existence, support, sentence in conn.execute(
                       "SELECT id, existence, support, json_extract(context, '$.sentence') FROM citations"
                       " WHERE owner_id = ?", (run_id,))}
        for cid, existence in resolve_now(conn, list(current)).items():  # as it stands now (T7): removed since
            if current[cid]["existence"] == "ok" and existence == "source_removed":
                current[cid].update(existence=existence, support="unchecked")
        return rows, kept, current
    rows, kept, current = await asyncio.to_thread(_state(request)["db"].read, read)
    events = []
    for seq, at, kind, data, in rows[:limit]:
        data = json.loads(data)
        if kind == "citation_checks":
            gone = {"existence": "source_removed", "support": "unchecked", "sentence": None}  # its row is gone
            data["citations"] = [{**c, **current.get(c["id"], gone)} if "id" in c else c
                                 for c in data.get("citations", [])]
        sent = data.get("body") if kind == "model_attempt" else None
        events.append({"seq": seq, "at": at, "type": kind, "data": data,
                       "body_ref": sent if sent in kept else None, **({"body_removed": True} if sent and sent not in kept
                                                                     else {})})
    return {"events": events, "next": events[-1]["seq"] if len(rows) > limit else None}


@router.get("/api/runs/{run_id}/trace/bodies/{sha256}")
async def body(run_id: str, sha256: str, request: Request):
    state = _state(request)

    def find(conn):
        if conn.execute("SELECT 1 FROM run_bodies WHERE run_id = ? AND sha256 = ?", (run_id, sha256)).fetchone():
            return "kept"
        named = conn.execute("SELECT 1 FROM run_events WHERE run_id = ? AND type = 'model_attempt'"
                             " AND ? IN (json_extract(data, '$.body'), json_extract(data, '$.reply'))",
                             (run_id, sha256)).fetchone()  # a request body, or an agent step's reply (S1-19c)
        return "removed" if named else None
    found = await asyncio.to_thread(state["db"].read, find)
    if found == "kept":
        try:
            data = await asyncio.to_thread(state["content"].read, sha256)
        except (OSError, ContentCorruptError):  # removed between that read and this one (a deletion, then collection)
            data = None
        # Read again once the text is in hand: a deletion that committed meanwhile wins, so no text leaves
        # after the record says it was removed.
        found = await asyncio.to_thread(state["db"].read, find)
        if found == "kept" and data is not None:
            return Response(data, media_type="application/json")
    if found in ("removed", "kept"):
        raise AdmissionError(410, "body_removed", "This text was removed with a deleted paper")
    raise AdmissionError(404, "not_found", "No such request body")


def resolve_now(conn, citation_ids):
    """{citation id: existence} as it stands now, for the citations given. A paper whose current file is not
    read yet by this app's extractor (its file just replaced, or a newer extractor reading it again) has no
    reading to judge by: its citations keep their stored existence until it is read."""
    rows = conn.execute(
        "SELECT c.id, c.passage_id, c.material_id, c.quote, c.existence, m.project_id FROM citations c"
        " LEFT JOIN materials m ON m.id = c.material_id WHERE c.id IN (SELECT value FROM json_each(?))",
        (json.dumps(citation_ids),)).fetchall()
    found, read = {}, {}
    for cid, pid, material, quote, existence, project in rows:
        if existence in ("not_found", "not_read_in_run") or project is None:
            found[cid] = existence if existence in ("not_found", "not_read_in_run") else "source_removed"
            continue
        if pid and readings(conn, project, [pid], references=False, materials=[material]):
            found[cid] = "ok"  # its passage is read now: no need to load the paper
            continue
        if material not in read:  # each paper's current reading once, however many of its citations
            read[material] = (readings(conn, project, references=False, materials=[material]),
                              conn.execute(f"SELECT 1 FROM material_versions w JOIN extractions e ON {_reads_now('w', 'e')}"
                                           " WHERE w.material_id = ? LIMIT 1", (material,)).fetchone() is not None)
        current, is_read = read[material]
        found[cid] = "ok" if quote and any(quote in r[4] for r in current.values()) \
            else "source_removed" if is_read else existence
    return found


@router.post("/api/citations/resolve")
async def resolve(body: Resolve, request: Request):
    found = await asyncio.to_thread(_state(request)["db"].read, lambda conn: resolve_now(conn, body.citation_ids))
    return {"citations": [{"id": cid, "existence": found[cid]} for cid in body.citation_ids if cid in found]}


@router.post("/api/citations/check", status_code=202)
async def check(body: Check, request: Request):
    """Start a support check of an answer's citations: {"run_ids": [...]}, one background run per batch."""
    state = _state(request)
    harness = state["harness"]
    values = await asyncio.to_thread(lambda: load_settings(state["data_dir"]).values)
    size, judge = values["retrieval"]["judge_pairs"], visible(values["models"].get("judge"))

    def cited(conn):
        return conn.execute(
            "SELECT c.id, json_extract(c.context, '$.sentence'), c.passage_id, c.material_id FROM citations c"
            " WHERE c.owner_kind = 'answer' AND c.owner_id = ? AND c.existence = 'ok' AND c.passage_id IS NOT NULL"
            " ORDER BY c.rowid", (body.run_id,)).fetchall()
    expected = await asyncio.to_thread(state["db"].read, cited)

    def record(conn, ids):
        row = conn.execute(
            "SELECT r.project_id, json_extract(r.inputs, '$.route'), json_extract(r.inputs, '$.context_materials')"
            " FROM turns t JOIN runs r ON r.id = t.run_id WHERE t.run_id = ? AND r.status = 'succeeded'",
            (body.run_id,)).fetchone()
        if row is None:
            raise AdmissionError(404, "not_found", "No such answer")
        project_id, route, held = row
        policy = governance.policy(conn, project_id)
        if policy is None or policy.locked:
            raise AdmissionError(403, "review_locked", "This project is review-locked")
        # An answer one of whose papers is gone is never sent again (backend/provenance.py, option A).
        held = json.loads(held or "[]")
        if not provenance.carried(conn, held):
            raise AdmissionError(409, "source_removed", "A paper this answer drew on was deleted")
        pairs = {}  # (sentence, passage) -> [citation ids, material]; a sentence gone with its source is not sent
        for cid, sentence, pid, material in cited(conn):
            if sentence:
                pairs.setdefault((sentence, pid), [[], material])[0].append(cid)
        # The papers each run's own request carries (backend/provenance.py): the answer's whole dependency set,
        # its passages' and those every sentence it sends cites, which are its scope and its dispatch check's,
        # and its body's links.
        cited_by = {}
        for sentence, material in conn.execute(
                "SELECT json_extract(context, '$.sentence'), material_id FROM citations WHERE owner_kind = 'answer'"
                " AND owner_id = ? AND material_id IS NOT NULL", (body.run_id,)):
            cited_by.setdefault(sentence, set()).add(material)
        if not pairs:
            raise AdmissionError(409, "nothing_to_check", "The answer has no citation to check")
        # Each pair named by its sentence's hash, never the text (backend/provenance.py)
        batches = citations.batches([[citations.sentence_key(s), p, m, cids] for (s, p), (cids, m) in pairs.items()],
                                    size)
        if len(batches) > len(ids):  # citations added since the count above: asked again
            raise AdmissionError(409, "changed", "The answer's citations changed; check again")
        provider, _, model = (route or ":").partition(":")
        keyed = {citations.sentence_key(sentence): sentence for sentence, _ in pairs}
        for run_id, batch in zip(ids, batches):
            carried = sorted({*held, *(m for key, _, m, _ in batch for m in (m, *cited_by.get(keyed[key], ())))})
            conn.execute(
                "INSERT INTO runs (id, project_id, kind, workflow, source_turn_id, inputs)"
                " VALUES (?, ?, 'background', 'citation_check', ?, ?)",
                (run_id, project_id, body.run_id, json.dumps({
                    "check": body.run_id, "provider": provider, "model": judge or model, "pairs": batch,
                    # registered with the dispatch check and deletion's scope links, as a turn's are
                    "evidence": sorted({(p, m) for _, p, m, _ in batch}),
                    "material_ids": carried, "context_materials": carried})))
        return ids[:len(batches)], ids[:len(batches)]

    run_ids = await harness.record_background(max(1, -(-len(expected) // size)) + 1, record)
    return {"run_ids": run_ids}
