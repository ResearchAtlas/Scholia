"""Trace reconstruction (slice-1 spec sections 7.6 and 14, test_trace_reconstruction.py; F11; ticket 17):
a cited answer is rebuilt from its record alone (the run's events, the request bodies in the content
store, the turn and its citation rows), the trace shows the exact text each call sent, owner-only, and
`GET /api/runs/{id}/trace` pages it and is gone with its run. Model calls go to a test-owned mock."""

import asyncio
import json
import stat

import pytest

from cited import ANSWER, cited, conversation_in, events_of, rows, with_papers
from scholia_app import send
from test_search import WAGES

pytestmark = pytest.mark.asyncio
QUESTION = "What do minimum wages do to earnings?"


async def test_a_cited_answer_is_rebuilt_from_its_record_alone(tmp_path):
    async with cited(tmp_path) as client:
        project, [material] = await with_papers(client, WAGES)
        conversation = await conversation_in(client, project)
        stream = await send(client, conversation, QUESTION)
        run_id = stream[0]["run_id"]
        events = await events_of(client, run_id)
        kinds = [kind for kind, _, _ in events]
        assert kinds == ["route", "retrieval_trace", "step_started", "model_attempt", "step_finished", "step_started",
                         "model_attempt", "step_finished", "citation_checks", "run_finished"]
        route, trace = events[0][1], events[1][1]
        assert route["retrieval"]["route"] == "literature" and [e["route"] for e in stream if e["type"] == "route"] == ["literature"]
        assert trace["query"] == QUESTION and trace["selected"] and set(trace["selected"]) <= set(trace["fused"])
        assert trace["mode"] == "hybrid" and "retrieve_ms" in trace and "total_ms" in trace
        answer_step, judge_step = [d for kind, d, _ in events if kind == "step_started"]
        assert (answer_step["phase"], judge_step["phase"]) == ("answer", "judge")
        assert answer_step["route"] == route["route"] and "input_tokens" in answer_step and "estimate_usd" in answer_step
        attempts = [(d, ref) for kind, d, ref in events if kind == "model_attempt"]
        assert all(d["charge"] == "reported" and d["cost_usd"] > 0 and "elapsed_ms" in d for d, _ in attempts)
        # The answer's request, from the content store: the evidence it held is the passages the trace selected.
        sent = json.loads(await asyncio.to_thread(client.state["content"].read, attempts[0][1]))
        texts = dict(await rows(client, "SELECT id, text FROM passages WHERE id IN (SELECT value FROM json_each(?))",
                                json.dumps(trace["selected"])))
        assert all(texts[pid] in sent["messages"][0]["content"] for pid in trace["selected"])
        assert sent["messages"][-1]["content"] == QUESTION
        # The answer and its checks, from the turn and the citation rows, agree with the trace.
        [(answer,)] = await rows(client, "SELECT answer FROM turns WHERE run_id = ?", run_id)
        assert json.loads(answer)["text"] == ANSWER
        [checks] = [d for kind, d, _ in events if kind == "citation_checks"]
        cited_rows = await rows(client, "SELECT passage_id, existence, support FROM citations WHERE owner_id = ?", run_id)
        assert [(c["passage_id"], c["existence"], c["support"]) for c in checks["citations"]] == cited_rows
        assert cited_rows[0][0] in trace["selected"]
        judged = json.loads(await asyncio.to_thread(client.state["content"].read, attempts[1][1]))
        assert texts[cited_rows[0][0]] in judged["messages"][-1]["content"]  # the pair the judge saw


async def test_the_trace_rebuilds_the_answer_and_keeps_the_exact_text_each_call_sent(tmp_path):
    async with cited(tmp_path) as client:
        project, _ = await with_papers(client, WAGES)
        conversation = await conversation_in(client, project)
        stream = await send(client, conversation, QUESTION)
        run_id = stream[0]["run_id"]
        page = (await client.get(f"/api/runs/{run_id}/trace", params={"limit": 3})).json()
        assert len(page["events"]) == 3 and page["next"] == 2
        rest = (await client.get(f"/api/runs/{run_id}/trace", params={"after": page["next"]})).json()
        events = page["events"] + rest["events"]
        kinds = [e["type"] for e in events]
        assert kinds[0] == "route" and {"retrieval_trace", "step_started", "model_attempt", "citation_checks",
                                       "run_finished"} <= set(kinds)
        sent = [e["body_ref"] for e in events if e["type"] == "model_attempt"]
        assert len(sent) == 2 and all(sent)  # the answer and the judge
        answer_body = (await client.get(f"/api/runs/{run_id}/trace/bodies/{sent[0]}")).content
        assert json.loads(answer_body) == client.provider.answers[0]  # byte for byte what left
        path = client.state["data_dir"] / "content" / sent[0][:2] / sent[0]
        assert stat.S_IMODE(path.stat().st_mode) == 0o600
        assert (await client.get(f"/api/runs/{run_id}/trace/bodies/{'0' * 64}")).status_code == 404
        await client.delete(f"/api/conversations/{conversation}")
        assert (await client.get(f"/api/runs/{run_id}/trace")).status_code == 404


