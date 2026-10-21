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
from scholia_app import events, run_finished, send
from test_search import WAGES

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
        answer, allowance = seen["before"]
        assert answer[0] == "settled" and allowance == ("open", allowance[1]) and allowance[1] > 0  # held after the answer
        assert seen["during"][1][0] == "released" and seen["during"][2][0] == "open"  # taken by the judge's own call
        after = await rows(client, "SELECT status FROM budget_reservations ORDER BY rowid")
        assert ("open",) not in after  # any rest released when the turn ends
        [(started,)] = await rows(client, "SELECT data FROM run_events WHERE type = 'step_started'"
                                          " AND json_extract(data, '$.phase') = 'answer'")
        assert json.loads(started)["allowance_usd"] == allowance[1]


async def test_a_judge_share_that_does_not_cover_the_judge_leaves_it_unchecked_never_past_the_budget(tmp_path, monkeypatch):
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
        assert "unchecked" in turn["answer"]["notices"]
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
