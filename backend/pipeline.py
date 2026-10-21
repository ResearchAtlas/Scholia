"""The retrieval pipeline: the evidence a turn pre-loads (slice-1 spec F4 and section 7.4; tickets 17
and 70).

1. The query is normalized (visible, NFKC) and filtered on metadata: a selection (papers selected in the
   Library or @-mentioned) restricts it to those papers.
2. Selected papers whose evidence records (each passage with its marker, title and facts, as the request
   holds them) fit the request's room for evidence whole are read directly, in their order; otherwise
   BM25 (top 50) and dense retrieval (top 50) run concurrently, within `hybrid_ms` (backend/search.py,
   retrieve), restricted to the selection when there is one.
3. The lists are fused by reciprocal rank fusion (k 60).
4. The top 8 are kept. Reranking is off by default: no setting enables it, and it runs only when a
   caller passes a reranker in (the tier 1 `rerank` arm and tests; ticket 70: no reranker qualifies
   in M2). With one, it reranks the top 24 within `rerank_ms`; past the deadline the reranker's own
   process is ended (local_helper.Helper.rerank), and the fused order stands with the fallback traced.
   Rerank scores are never shown as confidence.
5. Access is checked in the main database in the read that fetches the original passages (retrieve).

Each record carries its paper's title, page, evidence type (full text, abstract, metadata, user note)
and retraction state, so the bundle labels them and nothing abstract-only is presented as full-text
support. The trace (`retrieval_trace`) holds the query, the candidates with their ranks, the rerank's
result or fallback, the evidence selected, the mode and why, and the time each stage took.
"""

import asyncio
import json
import time
import unicodedata

from backend import citations, context, search
from backend.search_index import readings
from backend.settings import load_settings, visible


def _ms(started):
    return round((time.monotonic() - started) * 1000, 3)


def _records(conn, rows):
    """Evidence records from readings rows [(passage id, row)], with each paper's evidence type and
    retraction state, in the order given."""
    materials = {row[0] for _, row in rows}
    facts = {m: (e, r) for m, e, r in conn.execute(
        "SELECT id, coalesce(evidence_type, 'full_text'), retraction FROM materials"
        " WHERE id IN (SELECT value FROM json_each(?))", (json.dumps(sorted(materials)),))}
    return [{**search.result(pid, row), "evidence_type": facts.get(row[0], ("full_text", "unknown"))[0],
             "retraction": facts.get(row[0], ("full_text", "unknown"))[1]} for pid, row in rows]


async def reranked(query, fused, texts, rerank, values):
    """The optional rerank step: (the top `rerank_top` of fused in the reranker's order, or None when the
    fused order stands, and what the trace records of it). rerank(query, texts, deadline) gives a score
    per text, or None past the deadline (local_helper.Helper.rerank ends the reranker's own process then);
    a reranker that cannot serve is a fallback too. The tier 1 `rerank` arm shares it (evals/tier1.py)."""
    top = fused[:values["rerank_top"]]
    began = time.monotonic()
    try:
        scores = await rerank(query, [texts[pid] for pid in top], values["rerank_ms"] / 1000)
    except Exception as error:  # the reranker could not serve: the fused order stands, traced
        return None, {"fallback": type(error).__name__, "ms": _ms(began)}
    if scores is None:
        return None, {"fallback": "deadline", "ms": _ms(began)}
    order = [top[i] for i in sorted(range(len(top)), key=lambda i: -scores[i])]
    return order, {"order": order, "ms": _ms(began)}


def fits(records, room):
    """Whether the request's evidence section holds these records whole in room tokens: the records as
    citations.bundle writes them, counted as the context assembler counts them."""
    lines = citations.bundle(citations.sources(records))
    return not context._take(lines, room - context.count(f"## {context.LABELS['research_evidence']}\n"))[2]


async def evidence(state, project_id, query, *, materials=None, rerank=None, whole=None):
    """The evidence for a turn: {"records": [...], "mode", "reason", "trace"}. materials, if given, is the
    selection (material ids, in the order chosen). rerank(query, texts, deadline) -> scores or None is the
    optional reranker. whole(records) says whether the turn's request holds the selection's records whole
    (backend/runs.py assembles it); without it, they are counted against the evidence budget (fits)."""
    started = time.monotonic()
    settings = load_settings(state["data_dir"]).values
    values, budget = settings["retrieval"], settings["context"]["research_evidence"]
    text = unicodedata.normalize("NFKC", visible(query) or "")
    db = state["db"]
    trace = {"query": text, "selection": list(materials) if materials else None}
    if materials:
        # Selected papers whose records fit the request whole are read directly, in their order (B40: the
        # records with their framing, not the passages' text, else the assembler cuts the last ones).
        rows = await asyncio.to_thread(db.read, lambda conn: list(
            readings(conn, project_id, references=False, materials=materials).items()))
        rows.sort(key=lambda item: (materials.index(item[1][0]), item[1][6]))  # paper, then passage order
        size = sum(context.count(row[4]) for _, row in rows)  # the text alone over the budget never fits
        records = await asyncio.to_thread(db.read, lambda conn: _records(conn, rows)) if rows and size <= budget else []
        if records and (whole(records) if whole else fits(records, budget)):
            trace.update(read_directly=True, selected=[r["passage_id"] for r in records], total_ms=_ms(started))
            return {"records": records, "mode": "selection", "reason": None, "trace": trace}
        trace["read_directly"] = False
    if not text:
        trace.update(selected=[], total_ms=_ms(started))
        return {"records": [], "mode": search.search_mode(state)[0], "reason": None, "trace": trace}
    found = await search.retrieve(state, project_id, text, values, materials=set(materials) if materials else None)
    trace.update(keyword=found["keyword"], dense=found["dense"], retrieve_ms=_ms(started), index=found["index"])
    fused = search.fuse([found["keyword"], found["dense"]], values["rrf_k"])
    keep = values["keep"]
    ranked = fused[:keep]
    if rerank is not None and fused:
        order, trace["rerank"] = await reranked(text, fused, {pid: row[4] for pid, row in found["found"].items()},
                                                rerank, values)
        if order is not None:
            ranked = order[:keep]
    rows = [(pid, found["found"][pid]) for pid in ranked]
    records = await asyncio.to_thread(db.read, lambda conn: _records(conn, rows))
    trace.update(fused=fused[:max(keep, values["rerank_top"])], selected=ranked, mode=found["mode"],
                 reason=found["reason"], total_ms=_ms(started))
    return {"records": records, "mode": found["mode"], "reason": found["reason"], "trace": trace}
