"""Cited answers' API (slice-1 spec sections 9 and F11): a run's trace, paged, with the exact request
bodies it sent; and an answer's citations, resolved again or checked again.

- `GET /api/runs/{id}/trace?after=&limit=`: the run's events in order (route, retrieval_trace,
  step_started, model_attempt, step_finished, citation_checks, run_finished, ...), a page at a time.
  A deleted run's trace is gone with it.
- `GET /api/runs/{id}/trace/bodies/{sha256}`: the exact text one of the run's calls sent, from the
  content store (owner-only files); only a body one of the run's own events names.
- `POST /api/citations/resolve`: each citation's existence now (a batch existence check): its passage
  still read by its paper, or its quote held by a current reading of the paper (status.md, decided
  2026-10-17 (8)), else `source_removed`. Nothing is written.
- `POST /api/citations/check`: the support judge again on an answer's citations, as a background run of
  the project (workflow `citation_check`): admitted and reserved like any model call, listed with its
  cost, up to `[retrieval] judge_pairs` pairs a run; the passages it sends are registered with the
  dispatch check, so one deleted or re-read before its dispatch stops it.
"""

import asyncio
import json

from fastapi import APIRouter, Request
from fastapi.responses import Response
from pydantic import BaseModel, Field

from backend import governance
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
        return conn.execute("SELECT seq, at, type, data, body_ref FROM run_events WHERE run_id = ? AND seq > ?"
                            " ORDER BY seq LIMIT ?", (run_id, after, limit + 1)).fetchall()
    rows = await asyncio.to_thread(_state(request)["db"].read, read)
    events = [{"seq": seq, "at": at, "type": kind, "data": json.loads(data), "body_ref": ref}
              for seq, at, kind, data, ref in rows[:limit]]
    return {"events": events, "next": events[-1]["seq"] if len(rows) > limit else None}


@router.get("/api/runs/{run_id}/trace/bodies/{sha256}")
async def body(run_id: str, sha256: str, request: Request):
    state = _state(request)
    named = await asyncio.to_thread(state["db"].read, lambda conn: conn.execute(
        "SELECT 1 FROM run_events WHERE run_id = ? AND body_ref = ?", (run_id, sha256)).fetchone())
    if named is None:
        raise AdmissionError(404, "not_found", "No such request body")
    data = await asyncio.to_thread(state["content"].read, sha256)
    return Response(data, media_type="application/json")


def resolve_now(conn, citation_ids):
    """{citation id: existence} as it stands now, for the citations given."""
    rows = conn.execute(
        "SELECT c.id, c.passage_id, c.material_id, c.quote, c.existence, m.project_id FROM citations c"
        " LEFT JOIN materials m ON m.id = c.material_id WHERE c.id IN (SELECT value FROM json_each(?))",
        (json.dumps(citation_ids),)).fetchall()
    found = {}
    for cid, pid, material, quote, existence, project in rows:
        if existence in ("not_found", "not_read_in_run") or project is None:
            found[cid] = existence if existence in ("not_found", "not_read_in_run") else "source_removed"
            continue
        current = readings(conn, project, references=False, materials=[material])
        found[cid] = "ok" if pid in current or (quote and any(quote in r[4] for r in current.values())) \
            else "source_removed"
    return found


@router.post("/api/citations/resolve")
async def resolve(body: Resolve, request: Request):
    found = await asyncio.to_thread(_state(request)["db"].read, lambda conn: resolve_now(conn, body.citation_ids))
    return {"citations": [{"id": cid, "existence": found[cid]} for cid in body.citation_ids if cid in found]}


@router.post("/api/citations/check", status_code=202)
async def check(body: Check, request: Request):
    """Start a support check of an answer's citations; returns its background run's id."""
    harness = _state(request)["harness"]

    def record(conn, ids):
        row = conn.execute(
            "SELECT r.project_id, json_extract(r.inputs, '$.route') FROM turns t JOIN runs r ON r.id = t.run_id"
            " WHERE t.run_id = ? AND r.status = 'succeeded'", (body.run_id,)).fetchone()
        if row is None:
            raise AdmissionError(404, "not_found", "No such answer")
        project_id, route = row
        policy = governance.policy(conn, project_id)
        if policy is None or policy.locked:
            raise AdmissionError(403, "review_locked", "This project is review-locked")
        cited = conn.execute("SELECT passage_id, material_id FROM citations WHERE owner_kind = 'answer' AND owner_id = ?"
                             " AND existence = 'ok' AND passage_id IS NOT NULL", (body.run_id,)).fetchall()
        if not cited:
            raise AdmissionError(409, "nothing_to_check", "The answer has no citation to check")
        provider, _, model = (route or ":").partition(":")
        conn.execute("INSERT INTO runs (id, project_id, kind, workflow, source_turn_id, inputs)"
                     " VALUES (?, ?, 'background', 'citation_check', ?, ?)",
                     (ids[0], project_id, body.run_id, json.dumps({"check": body.run_id, "provider": provider, "model": model,
                                 # registered with the dispatch check and deletion's scope links, as a turn's are
                                 "passage_ids": sorted({p for p, _ in cited}),
                                 "material_ids": sorted({m for _, m in cited if m})})))
        return ids[0], [ids[0]]

    run_id = await harness.record_background(1, record)
    return {"run_id": run_id}

