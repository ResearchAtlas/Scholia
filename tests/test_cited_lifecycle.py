"""Cited answers' lifecycle under deletion, budgets, rechecks and stops (S1-19b; slice-1 spec F4, F11,
sections 4.2, 6.2, 7.5 and 10; final review F02; ticket 71): the cases external review b1 reproduced, each
a test that failed before its fix. Model calls go to a test-owned mock and the local helper is S1-16's
stand-in; nothing leaves the process."""

import asyncio
import json

import pytest

from backend import pipeline, runs as runs_module
from backend.db import deletion
from cited import ANSWER, Provider, cited, conversation_in, rows, with_papers
from scholia_app import background_idle, events, run_finished, send
from test_search import WAGES, paper, setting

pytestmark = pytest.mark.asyncio
QUESTION = "What do minimum wages do to earnings?"


async def test_a_paper_deleted_after_retrieval_does_not_reach_dispatch_through_a_passage_another_paper_reads(
        tmp_path, monkeypatch):
    # b1 (1): the dispatch check is by (passage, paper): a second paper reading the same reading does not keep
    # the deleted paper's evidence, named under its title, in the request.
    async with cited(tmp_path) as client:
        project, [material] = await with_papers(client, WAGES)
        db = client.state["db"]

        def twin(conn):  # a second paper of the project reading the same file, so the same passages
            other = "00000000-0000-4000-8000-000000000001"
            conn.execute("INSERT INTO materials (id, project_id, title, source) SELECT ?, project_id, 'Twin', source"
                         " FROM materials WHERE id = ?", (other, material))
            conn.execute("INSERT INTO material_versions (id, material_id, seq, file_sha256, is_current, media_type)"
                         " SELECT '00000000-0000-4000-8000-000000000002', ?, 0, file_sha256, 1, media_type"
                         " FROM material_versions WHERE material_id = ? AND is_current = 1", (other, material))
        await asyncio.to_thread(db.write, twin)
        real = pipeline.evidence

        async def then_delete(*args, **kwargs):
            found = await real(*args, **kwargs)
            assert (await client.delete(f"/api/materials/{material}")).status_code == 200
            return found
        monkeypatch.setattr(pipeline, "evidence", then_delete)
        conversation = await conversation_in(client, project)
        stream = await send(client, conversation, QUESTION)
        assert client.provider.answers == [] and stream[-1]["status"] in ("cancelled", "failed")
        assert await rows(client, "SELECT count(*) FROM citations") == [(0,)]


async def test_a_body_carrying_an_earlier_answer_goes_with_the_paper_that_answer_drew_on(tmp_path):
    # b1 (2): provenance follows evidence into history: a later request carrying an answer that quoted a paper
    # is linked to that paper, so deleting it removes the later body too.
    provider = Provider(answer_text="The paper reports: Minimum wages raise the earnings of low-paid workers [S1.1].")
    async with cited(tmp_path, provider) as client:
        project, [material] = await with_papers(client, WAGES)
        conversation = await conversation_in(client, project)
        await send(client, conversation, QUESTION)
        later = (await send(client, conversation, "Rewrite your last answer more formally."))[0]["run_id"]
        [(held,)] = await rows(client, "SELECT json_extract(inputs, '$.context_materials') FROM runs WHERE id = ?", later)
        assert json.loads(held) == [material]
        events_ = (await client.get(f"/api/runs/{later}/trace")).json()["events"]
        [sha] = [e["data"]["body"] for e in events_ if e["type"] == "model_attempt"]
        assert (await client.get(f"/api/runs/{later}/trace/bodies/{sha}")).status_code == 200
        assert (await client.delete(f"/api/materials/{material}")).status_code == 200
        assert (await client.get(f"/api/runs/{later}/trace/bodies/{sha}")).status_code == 410


async def test_deleting_a_paper_removes_its_text_from_the_answers_citations(tmp_path):
    # b1 (3): no quote of a deleted paper stays in the citations, or in what the conversation shows.
    async with cited(tmp_path) as client:
        project, [material] = await with_papers(client, WAGES)
        conversation = await conversation_in(client, project)
        await send(client, conversation, QUESTION)
        assert await rows(client, "SELECT count(*) FROM citations WHERE quote IS NOT NULL") == [(1,)]
        assert (await client.delete(f"/api/materials/{material}")).status_code == 200
        assert await rows(client, "SELECT existence, quote, material_id, passage_id FROM citations") == [
            ("source_removed", None, None, None)]
        shown = (await client.get(f"/api/conversations/{conversation}")).text
        assert "low-paid workers in the synthetic panel" not in shown


async def test_the_body_endpoint_never_answers_with_text_once_its_deletion_has_committed(tmp_path, monkeypatch):
    # b1 (4): the record is read again after the text: a deletion committing between them wins.
    async with cited(tmp_path) as client:
        project, [material] = await with_papers(client, WAGES)
        conversation = await conversation_in(client, project)
        run_id = (await send(client, conversation, QUESTION))[0]["run_id"]
        sha = next(e["data"]["body"] for e in (await client.get(f"/api/runs/{run_id}/trace")).json()["events"]
                   if e["type"] == "model_attempt")
        content = client.state["content"]
        real = content.read

        def read_then_deleted(name):  # the file stays (collection waits an hour); the deletion commits meanwhile
            data = real(name)
            deletion.delete(client.state["db"], content, "material", material)
            return data
        monkeypatch.setattr(content, "read", read_then_deleted)
        response = await client.get(f"/api/runs/{run_id}/trace/bodies/{sha}")
        assert response.status_code == 410 and "low-paid" not in response.text


async def test_the_judges_share_stays_held_until_the_judge_takes_it_and_no_reservation_skips_the_budget(
        tmp_path, monkeypatch):
    # b1 (5): settling the answer does not free the judge's share for other work; the judge takes it in the
    # transaction that reserves its own call, against the budgets.
    seen = {}

    async def verdicts(body):
        seen["during"] = await rows(client_holder["client"],
                                    "SELECT status, estimate_usd FROM budget_reservations ORDER BY rowid")
        return provider.answer("1: supported")
    provider = Provider(verdicts=verdicts)
    client_holder = {}
    real = runs_module.Harness._support

    async def before_judge(self, claim, ctx, checked, judge):
        seen["before"] = await rows(client_holder["client"],
                                    "SELECT status, estimate_usd FROM budget_reservations ORDER BY rowid")
        return await real(self, claim, ctx, checked, judge)
    monkeypatch.setattr(runs_module.Harness, "_support", before_judge)
    async with cited(tmp_path, provider) as client:
        client_holder["client"] = client
        project, _ = await with_papers(client, WAGES)
        conversation = await conversation_in(client, project)
        stream = await send(client, conversation, QUESTION)
        assert stream[-1]["status"] == "succeeded"
        answer, allowance, unspent = seen["before"]
        assert answer[0] == "settled" and allowance == ("open", allowance[1]) and allowance[1] > 0  # held after the answer
        assert unspent[0] == "open" and unspent[1] > 0  # the answer's unspent, held again for its judge (option C)
        assert [r[0] for r in seen["during"][1:4]] == ["released", "released", "open"]  # taken by the judge's call
        after = await rows(client, "SELECT status FROM budget_reservations ORDER BY rowid")
        assert ("open",) not in after  # any rest released when the turn ends
        [(started,)] = await rows(client, "SELECT data FROM run_events WHERE type = 'step_started'"
                                          " AND json_extract(data, '$.phase') = 'answer'")
        assert json.loads(started)["allowance_usd"] == allowance[1]


async def test_a_judge_share_that_does_not_cover_the_judge_leaves_it_unchecked_never_past_the_budget(tmp_path, monkeypatch,
                                                                                                    caplog):
    # b1 (5): a judge call the budget cannot hold is refused, not reserved past it: unchecked, said.
    from backend import budget_router
    real, judging = budget_router.request_cost, []

    def costs(rates, inputs, outputs):  # the judge's own call far past any budget
        return 10_000.0 if judging else real(rates, inputs, outputs)
    real_role = runs_module.Harness._role_call

    async def role(self, *args, **kwargs):
        judging.append(1)
        try:
            return await real_role(self, *args, **kwargs)
        finally:
            judging.pop()
    monkeypatch.setattr(budget_router, "request_cost", costs)
    monkeypatch.setattr(runs_module.Harness, "_role_call", role)
    provider = Provider(verdicts="1: supported")
    async with cited(tmp_path, provider) as client:
        project, _ = await with_papers(client, WAGES)
        conversation = await conversation_in(client, project)
        stream = await send(client, conversation, QUESTION)
        assert stream[-1]["status"] == "succeeded" and provider.judged == []
        [turn] = (await client.get(f"/api/conversations/{conversation}")).json()["turns"]
        assert "unchecked_budget" in turn["answer"]["notices"]  # unchecked for budget, said apart (option C)
        assert await rows(client, "SELECT count(*) FROM budget_reservations WHERE estimate_usd >= 10000") == [(0,)]


async def test_a_recheck_verdict_is_written_only_to_the_pair_it_was_given_for(tmp_path):
    # b1 (6): a citation re-pointed while the judge ran keeps its verdict; the new one never lands on it.
    async with cited(tmp_path) as client:
        project, [material] = await with_papers(client, WAGES)
        conversation = await conversation_in(client, project)
        run_id = (await send(client, conversation, QUESTION))[0]["run_id"]
        [(cid, old)] = await rows(client, "SELECT id, passage_id FROM citations")
        [(other,)] = await rows(client, "SELECT p.id FROM passages p WHERE p.id != ? LIMIT 1", old)

        async def repoint_then_judge(body):
            await asyncio.to_thread(client.state["db"].write, lambda conn: conn.execute(
                "UPDATE citations SET passage_id = ? WHERE id = ?", (other, cid)))
            return client.provider.answer("1: unsupported")
        client.provider.verdicts = repoint_then_judge
        [check] = (await client.post("/api/citations/check", json={"run_id": run_id})).json()["run_ids"]
        assert (await run_finished(client, check))["status"] == "succeeded"
        assert await rows(client, "SELECT passage_id, support FROM citations") == [(other, "supported")]


async def test_a_recheck_of_forty_one_pairs_runs_in_two_batches_and_judges_every_pair(tmp_path):
    # b1 (7): /check batches at judge_pairs (40) and never reports success with a pair unjudged.
    text = " ".join(f"Claim number {i} holds [S1.1]." for i in range(41))
    provider = Provider(answer_text=text)
    async with cited(tmp_path, provider) as client:
        project, _ = await with_papers(client, WAGES)
        conversation = await conversation_in(client, project)
        run_id = (await send(client, conversation, QUESTION))[0]["run_id"]
        assert await rows(client, "SELECT count(*), count(DISTINCT json_extract(context, '$.sentence')) FROM citations") \
            == [(41, 41)]
        provider.verdicts = lambda body: "\n".join(
            f"{i}: unsupported" for i in range(1, body["messages"][-1]["content"].count("Pair ") + 1))
        checks = (await client.post("/api/citations/check", json={"run_id": run_id})).json()["run_ids"]
        assert len(checks) == 2
        for check in checks:
            assert (await run_finished(client, check))["status"] == "succeeded"
        assert await rows(client, "SELECT support, count(*) FROM citations GROUP BY support") == [("unsupported", 41)]


async def test_a_set_judge_that_cannot_run_judges_nothing_and_a_recheck_uses_the_set_judge(tmp_path):
    # b1 (8): only an unset judge inherits the answer's route; a set one is used by /check too.
    from test_private_allowlist import save
    async with cited(tmp_path) as client:
        project, _ = await with_papers(client, WAGES)
        await save(client, {"models.judge": "no-such/model"})
        conversation = await conversation_in(client, project)
        run_id = (await send(client, conversation, QUESTION))[0]["run_id"]
        [turn] = (await client.get(f"/api/conversations/{conversation}")).json()["turns"]
        assert client.provider.judged == [] and "unchecked" in turn["answer"]["notices"]
        [check] = (await client.post("/api/citations/check", json={"run_id": run_id})).json()["run_ids"]
        [(inputs,)] = await rows(client, "SELECT inputs FROM runs WHERE id = ?", check)
        assert json.loads(inputs)["model"] == "no-such/model"
        assert (await run_finished(client, check))["status"] == "failed"  # visibly, never the answer's model
        assert client.provider.judged == []


async def test_a_judge_call_stopped_in_flight_keeps_the_exact_text_it_sent(tmp_path):
    # b1 (9): the body is stored before it is sent, so a Stop during the call still leaves it in the trace.
    release, judging = asyncio.Event(), asyncio.Event()

    async def held(body):
        judging.set()
        await release.wait()
        return Provider().answer("1: supported")
    provider = Provider(verdicts=held)
    async with cited(tmp_path, provider) as client:
        project, _ = await with_papers(client, WAGES)
        conversation = await conversation_in(client, project)
        stream = asyncio.create_task(client.post(f"/api/conversations/{conversation}/message/stream",
                                                 json={"content": QUESTION}))
        await asyncio.wait_for(judging.wait(), 10)
        [(run_id,)] = await rows(client, "SELECT id FROM runs WHERE kind = 'turn'")
        assert (await client.post(f"/api/runs/{run_id}/cancel")).json()["status"] == "cancelled"
        events(await stream)
        release.set()
        attempts = [e for e in (await client.get(f"/api/runs/{run_id}/trace")).json()["events"]
                    if e["type"] == "model_attempt"]
        stopped = attempts[-1]
        assert stopped["data"]["outcome"] == "cancelled" and stopped["body_ref"]
        sent = (await client.get(f"/api/runs/{run_id}/trace/bodies/{stopped['body_ref']}")).json()
        assert sent == provider.judged[0]


async def test_a_body_the_store_cannot_take_is_marked_in_the_trace(tmp_path, monkeypatch):
    # b1 (9): the receipt is recorded and settled all the same, and the trace says the text was not kept.
    async with cited(tmp_path) as client:
        project, _ = await with_papers(client, WAGES)
        monkeypatch.setattr(client.state["content"], "put", lambda *args, **kwargs: (_ for _ in ()).throw(OSError("full")))
        conversation = await conversation_in(client, project)
        stream = await send(client, conversation, QUESTION)
        assert stream[-1]["status"] == "succeeded"
        attempts = [e for e in (await client.get(f"/api/runs/{stream[0]['run_id']}/trace")).json()["events"]
                    if e["type"] == "model_attempt"]
        assert attempts and all(e["data"].get("body_unavailable") and not e["body_ref"] for e in attempts)


async def test_evidence_the_window_cut_is_said_to_be_cut_not_missing_from_the_library(tmp_path):
    # b1 (10): passages matched but none fits: the request says so, never "no passage matched".
    from test_private_allowlist import save
    async with cited(tmp_path) as client:
        project, _ = await with_papers(client, WAGES)
        await save(client, {"context.research_evidence": 1})
        conversation = await conversation_in(client, project)
        await send(client, conversation, QUESTION)
        [body] = client.provider.answers
        system = body["messages"][0]["content"]
        assert "none fits this model's context window" in system and "No passage in the researcher's library" not in system
        assert "## RESEARCH EVIDENCE" not in system


async def test_deleting_a_conversation_while_its_answer_is_out_leaves_no_judge_share_open(tmp_path):
    # Internal review of b1 (P2): the judge's share held beside the answer call is released by the deletion,
    # never left open (counted against the project) with no run to release it.
    release, answering = asyncio.Event(), asyncio.Event()

    async def held():
        answering.set()
        await release.wait()
        return provider.answer(ANSWER)
    provider = Provider(answer_text=lambda body: held())
    async with cited(tmp_path, provider) as client:
        project, _ = await with_papers(client, WAGES)
        conversation = await conversation_in(client, project)
        stream = asyncio.create_task(client.post(f"/api/conversations/{conversation}/message/stream",
                                                 json={"content": QUESTION}))
        await asyncio.wait_for(answering.wait(), 10)
        assert await rows(client, "SELECT count(*) FROM budget_reservations WHERE status = 'open'") == [(2,)]
        assert (await client.delete(f"/api/conversations/{conversation}")).status_code == 200
        release.set()
        await stream
        assert await rows(client, "SELECT count(*) FROM budget_reservations WHERE status = 'open'") == [(0,)]


async def test_a_citation_whose_paper_is_still_there_keeps_its_quote_when_its_passage_is_not(tmp_path):
    # Internal review of b1 (P3): only a deleted paper's citation loses its quote; one whose paper was re-read
    # keeps it, so a later reading holding the text can resolve it (status.md, decided 2026-10-17 (8)).
    from backend import citations
    async with cited(tmp_path) as client:
        project, [material] = await with_papers(client, WAGES)
        [(pid, text, version)] = await rows(client, "SELECT p.id, p.text, v.id FROM passages p JOIN material_versions v"
                                                    " ON v.material_id = ? ORDER BY p.ordinal LIMIT 1", material)
        record = {"material_id": material, "version_id": version, "passage_id": "gone", "text": "no reading holds this",
                  "page": None, "evidence_type": "full_text", "title": "Wage Floors"}
        [row] = await asyncio.to_thread(client.state["db"].read, lambda conn: citations.check_existence(
            conn, project, "Wages rise [S1.1].", {(1, 1): record}))
        assert (row["existence"], row["material_id"], row["quote"]) == ("source_removed", material, "no reading holds this")


async def test_a_recheck_the_judge_leaves_partly_unanswered_fails_and_writes_nothing(tmp_path):
    # Internal review of b1 (P3): never success with a pair unjudged; the earlier verdicts stand.
    async with cited(tmp_path) as client:
        project, _ = await with_papers(client, WAGES)
        conversation = await conversation_in(client, project)
        run_id = (await send(client, conversation, QUESTION))[0]["run_id"]
        client.provider.verdicts = "I cannot tell."
        [check] = (await client.post("/api/citations/check", json={"run_id": run_id})).json()["run_ids"]
        assert (await run_finished(client, check))["status"] == "failed"
        assert await rows(client, "SELECT support FROM citations") == [("supported",)]


async def test_a_paper_deleted_earlier_does_not_cost_the_conversations_later_turns_their_trace(tmp_path):
    # Recheck of b1 (P2): the history links only papers that still exist, so after a cited paper is deleted a
    # later turn's body is kept (it can hold none of the deleted text, which went with the paper).
    from test_search import CHINESE
    async with cited(tmp_path) as client:
        project, [wages, chinese] = await with_papers(client, WAGES, CHINESE)
        conversation = await conversation_in(client, project)
        await send(client, conversation, QUESTION)
        assert (await client.delete(f"/api/materials/{wages}")).status_code == 200
        later = (await send(client, conversation, "Rewrite your last answer more formally."))[0]["run_id"]
        attempts = [e for e in (await client.get(f"/api/runs/{later}/trace")).json()["events"]
                    if e["type"] == "model_attempt"]
        assert attempts and all(e["body_ref"] and not e.get("body_removed") for e in attempts)
        [(held,)] = await rows(client, "SELECT json_extract(inputs, '$.context_materials') FROM runs WHERE id = ?", later)
        assert wages not in json.loads(held or "[]")


# Fix batch b2 (external recheck b2): backend/provenance.py's one rule for text whose source is deleted.

TWIN = "00000000-0000-4000-8000-000000000001"


def twin_of(material):
    def twin(conn):  # a second paper of the project reading the same file, so the same passages
        conn.execute("INSERT INTO materials (id, project_id, title, source) SELECT ?, project_id, 'Twin', source"
                     " FROM materials WHERE id = ?", (TWIN, material))
        conn.execute("INSERT INTO material_versions (id, material_id, seq, file_sha256, is_current, media_type)"
                     " SELECT '00000000-0000-4000-8000-000000000002', ?, 0, file_sha256, 1, media_type"
                     " FROM material_versions WHERE material_id = ? AND is_current = 1", (TWIN, material))
    return twin


QUOTING = "The paper reports that minimum wages raise the earnings of low-paid workers [S1.1]."


async def test_an_earlier_answer_drawing_on_a_deleted_paper_is_left_out_of_later_requests_and_traced(tmp_path):
    # b2 (1): the answer's text goes out of the history with its paper, not only its provenance link.
    provider = Provider(answer_text=QUOTING)
    async with cited(tmp_path, provider) as client:
        project, [material] = await with_papers(client, WAGES)
        conversation = await conversation_in(client, project)
        await send(client, conversation, QUESTION)
        assert (await client.delete(f"/api/materials/{material}")).status_code == 200
        later = (await send(client, conversation, "Rewrite your last answer more formally."))[0]["run_id"]
        sent = json.dumps(provider.answers[-1], ensure_ascii=False)
        assert "low-paid workers" not in sent  # the answer quoting the deleted paper is not carried
        [(step,)] = await rows(client, "SELECT data FROM run_events WHERE run_id = ? AND type = 'step_started'", later)
        assert json.loads(step)["history_left_out"] == 1


async def test_a_paper_deleted_after_a_history_only_request_is_registered_stops_its_dispatch(tmp_path, monkeypatch):
    # b2 (2): the history's papers are the run's scope and the dispatch check's, as its evidence's are.
    provider = Provider(answer_text=QUOTING)
    async with cited(tmp_path, provider) as client:
        project, [material] = await with_papers(client, WAGES)
        conversation = await conversation_in(client, project)
        await send(client, conversation, QUESTION)
        real = runs_module.Harness._register_context

        async def then_delete(self, claim, numbered):
            await real(self, claim, numbered)
            assert (await client.delete(f"/api/materials/{material}")).status_code == 200
        monkeypatch.setattr(runs_module.Harness, "_register_context", then_delete)
        before = len(provider.answers)
        stream = await send(client, conversation, "Rewrite your last answer more formally.")
        assert len(provider.answers) == before and stream[-1]["status"] in ("cancelled", "failed")


async def test_deleting_a_paper_leaves_no_cited_sentence_in_rows_events_or_the_trace(tmp_path):
    # b2 (3): the citation's sentence goes with its quote; events name citations by id and never hold the text.
    provider = Provider(answer_text=QUOTING)
    async with cited(tmp_path, provider) as client:
        project, [material] = await with_papers(client, WAGES)
        conversation = await conversation_in(client, project)
        run_id = (await send(client, conversation, QUESTION))[0]["run_id"]
        [check] = (await client.post("/api/citations/check", json={"run_id": run_id})).json()["run_ids"]
        assert (await run_finished(client, check))["status"] == "succeeded"
        assert await rows(client, "SELECT count(*) FROM run_events WHERE data LIKE '%low-paid%'") == [(0,)]
        assert await rows(client, "SELECT count(*) FROM runs WHERE inputs LIKE '%low-paid%'") == [(0,)]
        trace = (await client.get(f"/api/runs/{run_id}/trace")).json()["events"]
        [checks] = [e for e in trace if e["type"] == "citation_checks"]
        assert checks["data"]["citations"][0]["sentence"] == QUOTING  # read from the row while its paper exists
        assert (await client.delete(f"/api/materials/{material}")).status_code == 200
        assert await rows(client, "SELECT count(*) FROM citations WHERE context LIKE '%low-paid%' OR quote IS NOT NULL") \
            == [(0,)]
        trace = (await client.get(f"/api/runs/{run_id}/trace")).json()["events"]
        [checks] = [e for e in trace if e["type"] == "citation_checks"]
        assert checks["data"]["citations"][0]["sentence"] is None
        shown = (await client.get(f"/api/conversations/{conversation}")).json()["turns"][0]["citations"]
        assert "low-paid" not in json.dumps(shown)  # the answer itself stays the researcher's record


async def test_an_inherited_judge_is_planned_and_reserved_on_the_facts_of_its_own_request(tmp_path, monkeypatch):
    # b2 (4): no reasoning means no reasoning pin, so an inherited judge's facts are read for its own request
    # (effort None): its window and rates, not the answer's, size its call and its share.
    real = runs_module.Harness._judge_route
    judged_facts = {}

    async def route(self, ctx):  # the judge's own request (no reasoning) reaches other, dearer endpoints
        model, judged, found = await real(self, ctx)
        assert found is not ctx["facts"] and found["effort"] is None  # read for its own request
        judged_facts.update({**found, "window": 65536, "rates": {k: v * 10 for k, v in found["rates"].items()}})
        return model, judged, dict(judged_facts)
    monkeypatch.setattr(runs_module.Harness, "_judge_route", route)
    async with cited(tmp_path) as client:
        project, _ = await with_papers(client, WAGES)
        conversation = await conversation_in(client, project)
        stream = await send(client, conversation, QUESTION)
        steps = [json.loads(d) for (d,) in await rows(client, "SELECT data FROM run_events WHERE run_id = ?"
                                                              " AND type = 'step_started' ORDER BY seq", stream[0]["run_id"])]
        [answer] = [s for s in steps if s["phase"] == "answer"]
        [judge] = [s for s in steps if s["phase"] == "judge"]
        assert answer["window"] != 65536 and judge["window"] == 65536
        assert judge["estimate_usd"] == runs_module.budget_router.request_cost(
            judged_facts["rates"], judge["input_tokens"], judge["max_tokens"])


async def test_a_remapped_citation_keeps_its_verdict_only_for_the_same_passage_text(tmp_path):
    # b2 (5): a re-read whose passage keeps the quote but changes the text leaves the citation unchecked.
    from backend import materials as materials_module
    async with cited(tmp_path) as client:
        project, [material] = await with_papers(client, WAGES)
        conversation = await conversation_in(client, project)
        await send(client, conversation, QUESTION)
        db = client.state["db"]

        def remap(conn, suffix):
            cid, pid, quote = conn.execute("SELECT id, passage_id, quote FROM citations").fetchone() or (None, None, None)
            if pid is None:
                pid = conn.execute("SELECT id FROM passages ORDER BY ordinal LIMIT 1 OFFSET 1").fetchone()[0]
            conn.execute("UPDATE passages SET text = ? WHERE id = ?", (quote + suffix, pid))
            conn.execute("UPDATE citations SET passage_id = NULL, existence = 'not_found', support = 'supported'"
                         " WHERE id = ?", (cid,))
            sha, kind, extraction = conn.execute(
                "SELECT v.file_sha256, coalesce(v.media_type, f.media_type), p.extraction_id FROM material_versions v"
                " JOIN content_files f ON f.sha256 = v.file_sha256 JOIN passages p ON p.id = ? WHERE v.material_id = ?",
                (pid, material)).fetchone()
            materials_module._resolve(conn, sha, materials_module.extraction.extractor_of(kind), extraction)
            return conn.execute("SELECT existence, support, support_run_id FROM citations").fetchone()
        same = await asyncio.to_thread(db.write, lambda conn: remap(conn, ""))
        assert same[:2] == ("ok", "supported")  # the passage's text is the quote's: the verdict stands
        changed = await asyncio.to_thread(db.write, lambda conn: remap(conn, " Except in winter, when it fell."))
        assert changed == ("ok", "unchecked", None)


async def test_a_selected_paper_sharing_its_reading_with_another_is_searched_as_itself(tmp_path):
    # b2 (6): keyword, dense and the final fetch filter by the papers the access check maps a passage to.
    async with cited(tmp_path) as client:
        project, [material] = await with_papers(client, WAGES)
        await asyncio.to_thread(client.state["db"].write, twin_of(material))
        from test_private_allowlist import save
        await save(client, {"context.research_evidence": 1})  # too small to read the selection whole: searched
        found = await pipeline.evidence(client.state, project, "minimum wages earnings", materials=[TWIN])
        assert found["trace"]["read_directly"] is False
        assert found["records"] and {r["material_id"] for r in found["records"]} == {TWIN}
        assert found["trace"]["keyword"] and found["trace"]["dense"]


async def test_an_answer_depending_on_a_deleted_paper_loses_every_rows_text_and_is_never_checked_again(tmp_path):
    # Harold, 2026-10-21 (option A): an answer's derived copies follow its whole dependency set. One of its two
    # papers deleted, every citation row of the answer loses its quote and sentence, whichever paper it names,
    # and /check refuses it.
    from test_search import CHINESE
    both = "Both papers report the same rise [S1.1] [S2.1]. The second paper alone reports more [S2.1]."
    provider = Provider(answer_text=both)
    async with cited(tmp_path, provider) as client:
        project, _ = await with_papers(client, WAGES, CHINESE)
        conversation = await conversation_in(client, project)
        run_id = (await send(client, conversation, "minimum wages earnings 最低工资"))[0]["run_id"]
        [(first,)] = await rows(client, "SELECT material_id FROM citations WHERE json_extract(context, '$.marker') = '[S1.1]'")
        assert (await client.delete(f"/api/materials/{first}")).status_code == 200
        assert await rows(client, "SELECT count(*) FROM citations WHERE quote IS NOT NULL"
                                  " OR json_extract(context, '$.sentence') IS NOT NULL") == [(0,)]
        response = await client.post("/api/citations/check", json={"run_id": run_id})
        assert (response.status_code, response.json()["code"]) == (409, "source_removed")


async def test_a_call_stopped_before_it_left_keeps_no_body(tmp_path, monkeypatch):
    # Internal review of b2: a body stored for a call that never left the machine is not kept.
    from backend import openrouter
    real = openrouter.query_model

    async def stop_after_keeping(*args, on_body=None, **kwargs):
        async def keep_then_stop(body):
            await on_body(body)
            raise asyncio.CancelledError()
        return await real(*args, on_body=keep_then_stop, **kwargs)
    monkeypatch.setattr(openrouter, "query_model", stop_after_keeping)
    async with cited(tmp_path) as client:
        project, _ = await with_papers(client, WAGES)
        conversation = await conversation_in(client, project)
        stream = await send(client, conversation, QUESTION)
        assert stream[-1]["status"] in ("cancelled", "failed")
        assert await rows(client, "SELECT count(*) FROM run_bodies") == [(0,)]


async def test_a_citation_mapped_to_a_re_read_passage_is_judged_on_what_that_passage_says_now(tmp_path):
    # Recheck of b2 (P3-1): a verdict belongs to the text judged, so the judge is shown the holder's text.
    from backend import citations
    async with cited(tmp_path) as client:
        project, [material] = await with_papers(client, WAGES)
        db = client.state["db"]
        now = "Minimum wages raise earnings. " * 25  # 750 characters, past the quote's 500
        [(pid, version)] = await rows(client, "SELECT p.id, v.id FROM passages p JOIN material_versions v"
                                              " ON v.material_id = ? ORDER BY p.ordinal LIMIT 1", material)
        await asyncio.to_thread(db.write, lambda conn: conn.execute("UPDATE passages SET text = ? WHERE id = ?", (now, pid)))
        record = {"material_id": material, "version_id": version, "passage_id": "gone", "text": now[:500] + " but no longer.",
                  "page": None, "evidence_type": "full_text", "title": "Wage Floors"}
        [row] = await asyncio.to_thread(db.read, lambda conn: citations.check_existence(
            conn, project, "Wages rise [S1.1].", {(1, 1): record}))
        assert (row["existence"], row["passage_id"], row["judged_text"]) == ("ok", pid, now)


# Fix batch b3 (external recheck b3): findings 3 to 8, each a test that failed before its fix.

TWINNED = paper("Wage Floors Again", "Minimum wages raise the earnings of low-paid workers in the synthetic panel.",
                "Housing costs were not measured in this second synthetic panel.")


async def test_recovery_records_an_interrupted_call_whose_body_an_earlier_call_also_sent(tmp_path):
    # b3 (3): two judge calls with the same bytes (two papers holding the same passage); the second is in flight
    # when recovery runs, and is recorded as interrupted although the first call's attempt names the same body.
    release, second = asyncio.Event(), asyncio.Event()
    calls = []

    async def verdicts(body):
        calls.append(body)
        if len(calls) == 2:
            second.set()
            await release.wait()
        return Provider().answer("1: supported")
    provider = Provider(answer_text="Low-paid workers earn more. [S1.1] Low-paid workers earn more. [S2.1]",
                        verdicts=verdicts)
    async with cited(tmp_path, provider) as client:
        project, _ = await with_papers(client, WAGES, TWINNED)
        await setting(client, "retrieval.judge_pairs", 1)
        conversation = await conversation_in(client, project)
        stream = asyncio.create_task(client.post(f"/api/conversations/{conversation}/message/stream",
                                                 json={"content": QUESTION}))
        await asyncio.wait_for(second.wait(), 10)
        assert calls[0] == calls[1]  # the same bytes, so the same stored body
        [(run_id,)] = await rows(client, "SELECT id FROM runs WHERE kind = 'turn'")
        [(step,)] = await rows(client, "SELECT max(step) FROM run_bodies WHERE run_id = ?", run_id)
        await client.state["harness"].recover(kick=False)
        attempts = [json.loads(d) for (d,) in await rows(
            client, "SELECT data FROM run_events WHERE run_id = ? AND type = 'model_attempt'", run_id)]
        assert [(a["step"], a.get("attempt")) for a in attempts if a["outcome"] == "interrupted"] == [(step, 0)]
        release.set()
        await stream


async def test_the_answers_unspent_is_held_again_against_the_budget_as_it_is_when_the_answer_settles(tmp_path):
    # b3 (4): a budget lowered while the answer is out governs the reservation its settlement holds again.
    async with cited(tmp_path) as client:
        project, _ = await with_papers(client, WAGES)
        conversation = await conversation_in(client, project)

        async def lower_then_answer(body):
            current = (await client.get("/api/settings", params={"project_id": project})).json()
            response = await client.put("/api/settings", json={"project_id": project, "hash": current["hash"],
                                                               "updates": {"project.budget_usd": 0.001}})
            assert response.status_code == 200, response.text
            return client.provider.answer(ANSWER)
        client.provider.answer_text = lower_then_answer
        stream = await send(client, conversation, QUESTION)
        finished = [json.loads(d) for (d,) in await rows(
            client, "SELECT data FROM run_events WHERE run_id = ? AND type = 'step_finished'", stream[0]["run_id"])]
        assert stream[-1]["status"] == "succeeded"
        answer = finished[0]
        assert "unspent_id" not in answer and "allowance_id" not in answer  # nothing held past the lowered budget


async def test_a_recheck_too_large_for_the_judges_window_is_split_as_the_answers_check_is(tmp_path, monkeypatch):
    # b3 (5): /check halves a batch its judge's window cannot take, through the same path as the turn's check.
    long = "and the effect held in every region, every year and every firm size of the synthetic panel " * 4
    text = " ".join(f"Claim {n} holds {long}[S1.1]." for n in range(1, 41))
    provider = Provider(answer_text=text)
    async with cited(tmp_path, provider) as client:
        project, _ = await with_papers(client, WAGES)
        conversation = await conversation_in(client, project)
        run_id = (await send(client, conversation, QUESTION))[0]["run_id"]
        real = runs_module.Harness._route_facts

        async def small(self, *args, **kwargs):  # the judge's endpoints take 4,096 tokens
            problem, facts = await real(self, *args, **kwargs)
            return problem, facts and {**facts, "window": 4096}
        monkeypatch.setattr(runs_module.Harness, "_route_facts", small)
        provider.judged.clear()
        provider.verdicts = lambda body: "\n".join(
            f"{i}: unsupported" for i in range(1, body["messages"][-1]["content"].count("Pair ") + 1))
        [check] = (await client.post("/api/citations/check", json={"run_id": run_id})).json()["run_ids"]
        assert (await run_finished(client, check))["status"] == "succeeded"
        assert len(provider.judged) > 1 and sum(b["messages"][-1]["content"].count("Pair ") for b in provider.judged) == 40
        assert await rows(client, "SELECT support, count(*) FROM citations GROUP BY support") == [("unsupported", 40)]


async def test_each_judge_call_says_which_held_amount_funded_it_and_each_is_held_apart(tmp_path):
    # b3 (6): what is left of the share and of the answer's unspent are held apart, so a later call the share's
    # rest cannot cover is said to be funded by the answer's unspent, never by the share.
    provider = Provider(answer_text="Earnings rise [S1.1]. Employment barely moves [S1.1]. Prices adjust [S1.1].")
    async with cited(tmp_path, provider) as client:
        project, _ = await with_papers(client, WAGES)
        await setting(client, "retrieval.judge_pairs", 1)
        conversation = await conversation_in(client, project)
        stream = await send(client, conversation, QUESTION)
        steps = [json.loads(d) for (d,) in await rows(client, "SELECT data FROM run_events WHERE run_id = ?"
                                                              " AND type = 'step_started' ORDER BY seq", stream[0]["run_id"])]
        [answer] = [s for s in steps if s["phase"] == "answer"]
        judges = [s for s in steps if s["phase"] == "judge"]
        assert len(judges) == 3 and judges[0]["funded"] == "share"
        share_left = answer["allowance_usd"] - judges[0]["estimate_usd"]
        assert judges[1]["estimate_usd"] > share_left and judges[1]["funded"] == "answer_unspent"
        assert ("open",) not in await rows(client, "SELECT status FROM budget_reservations")


async def test_a_marker_in_a_tilde_fence_is_code_never_a_citation_or_a_judge_call(tmp_path):
    # b3 (8): code fences of either kind, as the interface's renderer reads them.
    text = "Earnings rise [S1.1].\n\n~~~\nexample [S1.2]\n~~~\n"
    provider = Provider(answer_text=text)
    async with cited(tmp_path, provider) as client:
        project, _ = await with_papers(client, WAGES)
        conversation = await conversation_in(client, project)
        await send(client, conversation, QUESTION)
        [turn] = (await client.get(f"/api/conversations/{conversation}")).json()["turns"]
        assert [c["marker"] for c in turn["citations"]] == ["[S1.1]"]
        assert sum(b["messages"][-1]["content"].count("Pair ") for b in provider.judged) == 1


async def test_code_spans_and_fences_are_read_as_the_interfaces_renderer_reads_them():
    # b3 (8): one set of cases, read by frontend/tests/answers.test.mjs too.
    from pathlib import Path
    from backend import citations
    cases = json.loads((Path(__file__).parents[1] / "frontend/tests/fixtures/code-markers.json").read_text())
    for case in cases:
        assert [m for _, m, _ in citations.markers(case["text"])] == case["markers"], case["text"]


# Harold, 2026-10-21 (option A): the deletion class. An answer stays the conversation's record; every copy derived
# from it follows its whole dependency set (inputs.context_materials), its history's papers included.

async def test_a_paper_carried_through_history_and_cited_as_another_takes_every_copy_of_the_later_answer(tmp_path):
    # b3 (1): paper A's text reaches a later answer through history while that answer cites paper B. Deleting A
    # clears the later answer's citation sentence and quote (B's row), and /check refuses it, so nothing is
    # sent and no new body is kept.
    from test_search import CHINESE
    rewrite = "The paper reports that minimum wages raise the earnings of low-paid workers [S1.1]."
    provider = Provider(answer_text=QUOTING)
    async with cited(tmp_path, provider) as client:
        project, [a, b] = await with_papers(client, WAGES, CHINESE)
        conversation = await conversation_in(client, project)
        await send(client, conversation, QUESTION, material_ids=[a])
        provider.answer_text = rewrite
        later = (await send(client, conversation, "最低工资 rewrite that", material_ids=[b]))[0]["run_id"]
        [(cited_material, existence)] = await rows(client, "SELECT material_id, existence FROM citations WHERE owner_id = ?",
                                                   later)
        assert (cited_material, existence) == (b, "ok")
        [(held,)] = await rows(client, "SELECT json_extract(inputs, '$.context_materials') FROM runs WHERE id = ?", later)
        assert sorted(json.loads(held)) == sorted([a, b])  # its history carried A
        assert (await client.delete(f"/api/materials/{a}")).status_code == 200
        assert await rows(client, "SELECT quote, json_extract(context, '$.sentence') FROM citations WHERE owner_id = ?",
                          later) == [(None, None)]
        bodies, runs, judged = (await rows(client, "SELECT count(*) FROM run_bodies"), await rows(
            client, "SELECT count(*) FROM runs"), len(provider.judged))
        response = await client.post("/api/citations/check", json={"run_id": later})
        assert (response.status_code, response.json()["code"]) == (409, "source_removed")
        assert await rows(client, "SELECT count(*) FROM run_bodies") == bodies
        assert await rows(client, "SELECT count(*) FROM runs") == runs and len(provider.judged) == judged
        shown = (await client.get(f"/api/conversations/{conversation}")).json()["turns"]
        assert shown[1]["answer"]["text"] == rewrite  # the answer stays the conversation's record
        assert "low-paid" not in json.dumps(shown[1]["citations"])


async def test_a_recheck_whose_judge_leaves_a_pair_unanswered_fails_when_another_pair_holds_two_citations(tmp_path):
    # Internal review of b3: a pair answered for two citations does not stand in for a pair left unanswered.
    provider = Provider(answer_text="Earnings rise [S1.1][S1.1]. Prices adjust [S1.1].")
    async with cited(tmp_path, provider) as client:
        project, _ = await with_papers(client, WAGES)
        conversation = await conversation_in(client, project)
        run_id = (await send(client, conversation, QUESTION))[0]["run_id"]
        provider.verdicts = "1: unsupported"
        [check] = (await client.post("/api/citations/check", json={"run_id": run_id})).json()["run_ids"]
        assert (await run_finished(client, check))["status"] == "failed"
        assert await rows(client, "SELECT DISTINCT support FROM citations") == [("supported",)]  # nothing written


async def test_deleting_a_conversation_while_its_judge_is_out_leaves_neither_held_amount_open(tmp_path):
    # Internal review of b3: the share's rest and the answer's unspent, held apart while the judge runs, both
    # go with a deletion, named by allowance_id and unspent_id.
    release, judging = asyncio.Event(), asyncio.Event()

    async def held(body):
        judging.set()
        await release.wait()
        return Provider().answer("1: supported")
    provider = Provider(answer_text="Earnings rise [S1.1]. Prices adjust [S1.1].", verdicts=held)
    async with cited(tmp_path, provider) as client:
        project, _ = await with_papers(client, WAGES)
        await setting(client, "retrieval.judge_pairs", 1)
        conversation = await conversation_in(client, project)
        stream = asyncio.create_task(client.post(f"/api/conversations/{conversation}/message/stream",
                                                 json={"content": QUESTION}))
        await asyncio.wait_for(judging.wait(), 10)
        [(started,)] = await rows(client, "SELECT data FROM run_events WHERE type = 'step_started'"
                                          " AND json_extract(data, '$.phase') = 'judge'")
        assert "unspent_id" in json.loads(started)  # the unspent's rest, held apart
        assert (await client.delete(f"/api/conversations/{conversation}")).status_code == 200
        assert await rows(client, "SELECT count(*) FROM budget_reservations WHERE status = 'open'"
                                  " AND id <> ?", json.loads(started)["reservation_id"]) == [(0,)]
        release.set()
        await stream
        assert await rows(client, "SELECT count(*) FROM budget_reservations WHERE status = 'open'") == [(0,)]


# Fix batch b4 (external recheck b4): findings 2, 4, 6 and 7, each a test that failed before its fix.

@pytest.mark.parametrize("tightening", ["local_only", "review_lock"])
async def test_an_earlier_answer_whose_papers_project_was_tightened_is_left_out_after_the_conversation_moved(
        tmp_path, tightening):
    # b4 (2): a history dependency passes the current access check evidence does: its project's level and lock
    # allow at least what the request's project does, else the answer it carried is left out.
    from test_materials import project_of
    provider = Provider(answer_text=QUOTING)
    async with cited(tmp_path, provider) as client:
        project, _ = await with_papers(client, WAGES)
        conversation = await conversation_in(client, project)
        await send(client, conversation, QUESTION)
        await background_idle(client)  # S1-21a: its extraction runs keyed to its turn, which a move waits for
        other = await project_of(client, "Other")
        assert (await client.post(f"/api/conversations/{conversation}/move", json={"project_id": other})).status_code == 200
        response = await (client.post(f"/api/projects/{project}/sensitivity", json={"level": "local_only"})
                          if tightening == "local_only" else
                          client.post(f"/api/projects/{project}/review-lock", json={"locked": True}))
        assert response.status_code == 200, response.text
        provider.answer_text = "A plain rewrite."
        later = (await send(client, conversation, "Rewrite your last answer more formally."))
        assert later[-1]["status"] == "succeeded"
        assert "low-paid workers" not in json.dumps(provider.answers[-1], ensure_ascii=False)
        [(step,)] = await rows(client, "SELECT data FROM run_events WHERE run_id = ? AND type = 'step_started'"
                                       " AND json_extract(data, '$.phase') = 'answer'", later[0]["run_id"])
        assert json.loads(step)["history_left_out"] == 1


async def test_a_history_dependency_tightened_after_registration_stops_the_dispatch(tmp_path, monkeypatch):
    # b4 (2): the dispatch check applies the same access check to the run's dependencies.
    from test_materials import project_of
    provider = Provider(answer_text=QUOTING)
    async with cited(tmp_path, provider) as client:
        project, _ = await with_papers(client, WAGES)
        conversation = await conversation_in(client, project)
        await send(client, conversation, QUESTION)
        await background_idle(client)  # S1-21a: its extraction runs keyed to its turn, which a move waits for
        other = await project_of(client, "Other")
        assert (await client.post(f"/api/conversations/{conversation}/move", json={"project_id": other})).status_code == 200
        real = runs_module.Harness._register_context

        async def then_tighten(self, claim, numbered):
            await real(self, claim, numbered)
            response = await client.post(f"/api/projects/{project}/sensitivity", json={"level": "local_only"})
            assert response.status_code == 200
        monkeypatch.setattr(runs_module.Harness, "_register_context", then_tighten)
        before = len(provider.answers)
        stream = await send(client, conversation, "Rewrite your last answer more formally.")
        assert len(provider.answers) == before and stream[-1]["status"] in ("cancelled", "failed")


async def test_the_answers_unspent_is_held_again_in_order_with_a_budget_save(tmp_path, monkeypatch):
    # b4 (4): a budget save cannot land between the settlement's budget read and the reservation it makes.
    import threading
    loop = asyncio.get_running_loop()
    paused, resume, armed, seen = asyncio.Event(), threading.Event(), [], {}
    real = runs_module._budgets_now

    def budgets(data_dir, project_id):
        found = real(data_dir, project_id)
        if armed:
            armed.clear()
            loop.call_soon_threadsafe(paused.set)
            resume.wait(5)
        return found
    monkeypatch.setattr(runs_module, "_budgets_now", budgets)
    async with cited(tmp_path) as client:
        project, _ = await with_papers(client, WAGES)
        conversation = await conversation_in(client, project)

        async def arm_then_answer(body):
            armed.append(1)
            return client.provider.answer(ANSWER)
        client.provider.answer_text = arm_then_answer
        stream = asyncio.create_task(client.post(f"/api/conversations/{conversation}/message/stream",
                                                 json={"content": QUESTION}))
        await asyncio.wait_for(paused.wait(), 10)
        current = (await client.get("/api/settings", params={"project_id": project})).json()
        save = asyncio.create_task(client.put("/api/settings", json={"project_id": project, "hash": current["hash"],
                                                                     "updates": {"project.budget_usd": 0.001}}))
        await asyncio.wait([save], timeout=1.0)
        seen["saved_meanwhile"] = save.done()
        resume.set()
        assert (await save).status_code == 200
        events(await stream)
        assert seen["saved_meanwhile"] is False  # the save waited for the settlement's reservation


async def test_a_judge_reply_with_two_verdicts_for_a_pair_leaves_it_unchecked(tmp_path):
    # b4 (6): contradictory output is not a verdict, for the turn's check and for /check.
    provider = Provider(verdicts="1: supported\n1: unsupported")
    async with cited(tmp_path, provider) as client:
        project, _ = await with_papers(client, WAGES)
        conversation = await conversation_in(client, project)
        run_id = (await send(client, conversation, QUESTION))[0]["run_id"]
        [turn] = (await client.get(f"/api/conversations/{conversation}")).json()["turns"]
        assert [c["support"] for c in turn["citations"]] == ["unchecked"] and "unchecked" in turn["answer"]["notices"]
        provider.verdicts = "1: unsupported\n1: supported"
        await asyncio.to_thread(client.state["db"].write, lambda conn: conn.execute(
            "UPDATE citations SET support = 'supported'"))
        [check] = (await client.post("/api/citations/check", json={"run_id": run_id})).json()["run_ids"]
        assert (await run_finished(client, check))["status"] == "failed"
        assert await rows(client, "SELECT support FROM citations") == [("supported",)]  # nothing written


async def test_judge_verdicts_for_a_pair_twice_or_an_unknown_pair_are_not_taken():
    from backend import citations
    assert citations.parse_verdicts("1: supported\n2: partly\n2: partly\n3: unsupported\n7: supported", 3) == {
        1: "supported", 3: "unsupported"}


# Harold, 2026-10-21: the deletion simplification (external recheck b4, findings 1, 3 and 5).

async def test_details_reads_each_citations_status_from_its_current_row(tmp_path):
    # b4 (5): the trace's check names citations by id; their existence, support and sentence are the rows'.
    provider = Provider(answer_text=QUOTING)
    async with cited(tmp_path, provider) as client:
        project, [material] = await with_papers(client, WAGES)
        conversation = await conversation_in(client, project)
        run_id = (await send(client, conversation, QUESTION))[0]["run_id"]
        assert (await client.delete(f"/api/materials/{material}")).status_code == 200
        trace = (await client.get(f"/api/runs/{run_id}/trace")).json()["events"]
        [checks] = [e for e in trace if e["type"] == "citation_checks"]
        [row] = await rows(client, "SELECT existence, support FROM citations WHERE owner_id = ?", run_id)
        [shown] = checks["data"]["citations"]
        assert (shown["existence"], shown["support"], shown["sentence"]) == (*row, None)
        assert shown["existence"] == "source_removed"


async def test_recovery_records_a_dispatched_attempt_whose_body_went_with_a_deleted_paper(tmp_path, monkeypatch):
    # b4 (3): the attempt's record does not depend on its body: an append-only event written when the body is
    # linked names the attempt, so a crash after a deletion took the body still records the attempt.
    release, judging = asyncio.Event(), asyncio.Event()

    async def held(body):
        judging.set()
        await release.wait()
        return Provider().answer("1: supported")
    monkeypatch.setattr(runs_module.Harness, "_request_cancel", lambda self, active, reason: None)  # the crash comes first
    provider = Provider(verdicts=held)
    async with cited(tmp_path, provider) as client:
        project, [material] = await with_papers(client, WAGES)
        conversation = await conversation_in(client, project)
        stream = asyncio.create_task(client.post(f"/api/conversations/{conversation}/message/stream",
                                                 json={"content": QUESTION}))
        await asyncio.wait_for(judging.wait(), 10)
        [(run_id,)] = await rows(client, "SELECT id FROM runs WHERE kind = 'turn'")
        [(step,)] = await rows(client, "SELECT max(json_extract(data, '$.step')) FROM run_events WHERE run_id = ?"
                                       " AND type = 'step_started'", run_id)
        assert (await client.delete(f"/api/materials/{material}")).status_code == 200
        assert await rows(client, "SELECT count(*) FROM run_bodies") == [(0,)]
        await client.state["harness"].recover(kick=False)
        attempts = [json.loads(d) for (d,) in await rows(
            client, "SELECT data FROM run_events WHERE run_id = ? AND type = 'model_attempt'", run_id)]
        assert [(a["step"], a["attempt"]) for a in attempts if a["outcome"] == "interrupted"] == [(step, 0)]
        release.set()
        await stream


async def test_recovery_records_no_attempt_for_a_step_whose_call_was_recorded(tmp_path, monkeypatch):
    # Internal review of b4: a dispatch event of an attempt never sent (its time ran out) is not taken for an
    # attempt in flight once its step's call was recorded.
    release, judging = asyncio.Event(), asyncio.Event()

    async def held(body):
        judging.set()
        await release.wait()
        return Provider().answer("1: supported")
    monkeypatch.setattr(runs_module.Harness, "_request_cancel", lambda self, active, reason: None)
    provider = Provider(verdicts=held)
    async with cited(tmp_path, provider) as client:
        project, _ = await with_papers(client, WAGES)
        conversation = await conversation_in(client, project)
        stream = asyncio.create_task(client.post(f"/api/conversations/{conversation}/message/stream",
                                                 json={"content": QUESTION}))
        await asyncio.wait_for(judging.wait(), 10)
        [(run_id,)] = await rows(client, "SELECT id FROM runs WHERE kind = 'turn'")
        [(step,)] = await rows(client, "SELECT max(json_extract(data, '$.step')) FROM run_events WHERE run_id = ?"
                                       " AND type = 'step_started'", run_id)
        await asyncio.to_thread(client.state["db"].write, lambda conn: runs_module._event(
            conn, run_id, "step_finished", {"step": step, "outcome": "timeout"}))  # its call recorded, as record() does
        await client.state["harness"].recover(kick=False)
        assert await rows(client, "SELECT count(*) FROM run_events WHERE run_id = ? AND type = 'model_attempt'"
                                  " AND json_extract(data, '$.outcome') = 'interrupted'", run_id) == [(0,)]
        release.set()
        await stream


# Fix batch b5 (external recheck b5, finding 2): the judge's reply format is strict.

@pytest.mark.parametrize("reply", ["1: supported; 1: unsupported", "1: supported\n1: unchecked"])
async def test_a_judge_reply_naming_a_pair_ambiguously_leaves_it_unchecked(tmp_path, reply):
    provider = Provider(verdicts=reply)
    async with cited(tmp_path, provider) as client:
        project, _ = await with_papers(client, WAGES)
        conversation = await conversation_in(client, project)
        run_id = (await send(client, conversation, QUESTION))[0]["run_id"]
        [turn] = (await client.get(f"/api/conversations/{conversation}")).json()["turns"]
        assert [c["support"] for c in turn["citations"]] == ["unchecked"] and "unchecked" in turn["answer"]["notices"]
        [check] = (await client.post("/api/citations/check", json={"run_id": run_id})).json()["run_ids"]
        assert (await run_finished(client, check))["status"] == "failed"
        assert await rows(client, "SELECT support FROM citations") == [("unchecked",)]


async def test_each_reply_line_is_exactly_a_pair_and_a_verdict_or_its_pair_is_unchecked():
    from backend import citations
    parse = citations.parse_verdicts
    assert parse("1: supported\n2: partly\n3: unsupported", 3) == {1: "supported", 2: "partly", 3: "unsupported"}
    assert parse("  Pair 1 : Supported  \n2) partly\n3. unsupported\n4 - supported", 4) == {
        1: "supported", 2: "partly", 3: "unsupported", 4: "supported"}  # whitespace, case and the separators aside
    assert parse("Here are the verdicts.\n1: supported\nThanks.", 1) == {1: "supported"}  # prose names no pair
    for spoiled in ["1: supported.", "1: supported because it says so", "1: supported, mostly", "1: unsupported?",
                    "1: supported (partly)", "1: supportedly", "1:", "1: maybe"]:
        assert parse(f"{spoiled}\n2: partly", 2) == {2: "partly"}, spoiled
    assert parse("1: supported\n1: supported", 1) == {}  # a second line for a pair, even the same
    assert parse("1: supported\n7: unsupported", 1) == {1: "supported"}  # a pair it was not given: not taken
    assert parse("", 2) == {} and parse(None, 2) == {}
    # Internal review of b5: a pair mentioned anywhere else, in a line that is not exactly a verdict, is unchecked
    assert parse("1: supported\n2: unsupported; 1: partly", 2) == {}
    for lead in ["- 1: unsupported", "**1: unsupported**", "Pair 1 is unsupported"]:
        assert parse(f"{lead}\n1: supported", 1) == {}, lead
    assert parse("9" * 5000 + ": supported\n1: partly", 1) == {1: "partly"}  # no number is read past six digits
    assert parse("\uff11: unsupported\n1: supported", 1) == {}  # a fullwidth 1 is no verdict, but it names pair 1
