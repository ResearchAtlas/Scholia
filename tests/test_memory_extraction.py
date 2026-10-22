"""Memory extraction, a detached background run after each answer (S1-21a; slice-1 spec F10, sections 6.1, 8 and 13;
ticket 71; checklist X1 to X9, L1, L2, L4, I2, I3): keyed by its turn in the primary commit, on the extractor route or
the turn's own, within its $0.02 allowance (both attempts), 60 seconds and one step; proposals unless the researcher's
own words carry a record; never a failed extraction's memory. Recorded extractor replies only (the provider is
test-owned): what a real extractor would propose is [PAID]."""

import asyncio
import json

import pytest

from backend import budget_router, memory, runs
from scholia_app import MockProvider, background_idle, send, started
from test_detached_tail import wait_for
from test_materials import project_of
from test_memory import candidate, conversation_in, extracted, memory_off, records, rows, set_project

pytestmark = pytest.mark.asyncio


async def memory_run(client):
    found = await rows(client, "SELECT id FROM runs WHERE workflow = 'memory' ORDER BY started_at DESC LIMIT 1")
    return found[0][0] if found else None


def held_extraction(provider, *records):
    release = asyncio.Event()

    async def reply(body):
        await release.wait()
        return provider.answer(json.dumps({"records": list(records)}), cost=0.001)
    provider.memory_replies.append(reply)
    return release


async def test_the_primary_commit_writes_one_extraction_run_and_the_turn_ends_there(tmp_path):
    provider = MockProvider()
    release = held_extraction(provider, candidate("We use interviews.", "confirmed", "project_state",
                                                 span="we use interviews"))
    async with started(tmp_path / "data", provider) as client:
        project = await project_of(client)
        conversation = await conversation_in(client, project)
        first = await send(client, conversation, "For this project we use interviews")
        await wait_for(lambda: provider.extractions)
        run = await memory_run(client)
        assert await rows(client, "SELECT kind, source_turn_id, conversation_id, status FROM runs WHERE id = ?", run) == [
            ("background", first[0]["run_id"], None, "running")]
        assert await rows(client, "SELECT memory_status FROM turns WHERE run_id = ?", first[0]["run_id"]) == [("pending",)]
        # the next send is accepted at once; Stop on the turn leaves the extraction running
        assert (await send(client, conversation, "next"))[-1]["status"] == "succeeded"
        assert (await client.post(f"/api/runs/{first[0]['run_id']}/cancel")).json()["status"] == "succeeded"
        assert client.state["harness"].registry.is_active(run)
        release.set()
        await background_idle(client)
        assert await rows(client, "SELECT status, attempts FROM runs WHERE id = ?", run) == [("succeeded", 1)]
        assert await rows(client, "SELECT memory_status FROM turns WHERE run_id = ?", first[0]["run_id"]) == [("indexed",)]
        assert [(r["content"], r["status"]) for r in await records(client, project) if r["type"] == "project_state"] == [
            ("We use interviews.", "confirmed")]
        # its cost goes to the project only
        assert await rows(client, "SELECT paying_conversation_id, settled_usd FROM budget_reservations WHERE run_id = ?",
                          run) == [(None, 0.001)]
        [(data,)] = await rows(client, "SELECT data FROM run_events WHERE run_id = ? AND type = 'memory_proposed'", run)
        assert "interviews" not in data and json.loads(data)["counts"]["confirmed"] == 1  # ids and counts only
        [(finished,)] = await rows(client, "SELECT data FROM run_events WHERE run_id = ? AND type = 'step_finished'", run)
        assert json.loads(finished)["output"] == "held" and "interviews" not in finished  # never model text in an event
        assert await rows(client, "SELECT json_extract(inputs, '$.held') FROM runs WHERE id = ?", run) == [(None,)]


async def test_cancel_in_the_background_list_stops_an_extraction_and_writes_no_memory(tmp_path):
    provider = MockProvider()
    held_extraction(provider, candidate("Never written.", "confirmed", span="never written"))  # never released
    async with started(tmp_path / "data", provider) as client:
        project = await project_of(client)
        turn = await send(client, await conversation_in(client, project), "never written")
        await wait_for(lambda: provider.extractions)
        run = await memory_run(client)
        assert (await client.post(f"/api/runs/{run}/cancel")).json()["status"] == "cancelled"
        await background_idle(client)
        assert await rows(client, "SELECT status, cancel_reason FROM runs WHERE id = ?", run) == [("cancelled", "researcher")]
        assert [r for r in await records(client, project) if r["type"] != "progress"] == []
        assert await rows(client, "SELECT memory_status FROM turns WHERE run_id = ?", turn[0]["run_id"]) == [("skipped",)]
        # its call may have gone out: settled once, at its estimate (L1)
        assert await rows(client, "SELECT status, basis FROM budget_reservations WHERE run_id = ?", run) == [
            ("settled", "estimated")]


async def test_an_unset_extractor_inherits_the_turns_route_and_a_set_one_is_used(tmp_path):
    provider = MockProvider()
    async with started(tmp_path / "data", provider) as client:
        project = await project_of(client)
        conversation = await conversation_in(client, project)
        turn_model, extractor = sorted(budget_router.RECOMMENDED)[:2]
        await send(client, conversation, "We study wages", model=turn_model)
        await background_idle(client)
        assert provider.extractions[-1]["model"] == turn_model
        current = (await client.get("/api/settings")).json()
        await client.put("/api/settings", json={"hash": current["hash"], "updates": {"models.extractor": extractor}})
        await send(client, conversation, "We also study prices", model=turn_model)
        await background_idle(client)
        assert provider.extractions[-1]["model"] == extractor
        for body in provider.extractions:  # a role call: no tools, no forced tool choice, no structured output (X8)
            assert not {"tools", "tool_choice", "response_format"} & body.keys()
            assert body["max_tokens"] == 1024
            assert "ASSISTANT'S ANSWER (model output, not the researcher's words)" in body["messages"][-1]["content"]


async def test_an_extraction_whose_bound_does_not_fit_its_allowance_is_skipped_with_a_notice_once(tmp_path, monkeypatch):
    provider = MockProvider()
    monkeypatch.setattr(memory, "ALLOWANCE_USD", 0.0001)  # below any request's bound
    async with started(tmp_path / "data", provider) as client:
        project = await project_of(client)
        conversation = await conversation_in(client, project)
        await send(client, conversation, "We study wages")
        await background_idle(client)
        await send(client, conversation, "We study prices")
        await background_idle(client)
        assert provider.extractions == []  # nothing was sent
        assert await rows(client, "SELECT status, cancel_reason, attempts, json_extract(summary, '$.reason') FROM runs"
                                  " WHERE workflow = 'memory'") == [("cancelled", "limit", 0, "allowance")] * 2
        assert await rows(client, "SELECT count(*) FROM budget_reservations WHERE run_id IN"
                                  " (SELECT id FROM runs WHERE workflow = 'memory')") == [(0,)]
        assert await rows(client, "SELECT count(*) FROM audit_log WHERE event = 'memory_allowance_notice'") == [(1,)]
        review = (await client.get(f"/api/memory/review?project_id={project}")).json()
        assert review["notice"] is True
        await client.post("/api/memory/review", json={"project_id": project, "dismiss": "allowance_notice"})
        assert (await client.get(f"/api/memory/review?project_id={project}")).json()["notice"] is False
        turns = (await client.get(f"/api/conversations/{conversation}")).json()["turns"]
        assert [t["memory_status"] for t in turns] == ["skipped", "skipped"]


async def queue_extraction(client, project, attempts=0, spent=None):
    """An extraction run queued for the project's first turn, as a primary commit writes one (attempts: as after a
    crash; spent: a settled first attempt's cost), started at once."""
    run = memory.new_id()

    def queue(conn):
        turn = conn.execute("SELECT run_id FROM turns ORDER BY rowid LIMIT 1").fetchone()[0]
        conn.execute("INSERT INTO runs (id, project_id, kind, workflow, source_turn_id, inputs, attempts) VALUES (?, ?,"
                     " 'background', 'memory', ?, ?, ?)", (run, project, turn, json.dumps(
                         {"provider": "openrouter", "model": sorted(budget_router.RECOMMENDED)[0]}), attempts))
        if spent is not None:
            conn.execute("INSERT INTO budget_reservations (id, run_id, step_seq, project_id, estimate_usd, status,"
                         " settled_usd, basis, settled_at) VALUES (?, ?, 0, ?, ?, 'settled', ?, 'reported',"
                         " strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))", (memory.new_id(), run, project, spent, spent))
    await asyncio.to_thread(client.state["db"].write, queue)
    await client.state["harness"].kick_background()
    await background_idle(client)
    return run


async def test_a_retry_counts_within_the_allowance(tmp_path):
    # A first attempt that spent $0.019 leaves no room for a second's bound: it is skipped, not sent.
    provider = MockProvider()
    async with started(tmp_path / "data", provider) as client:
        project = await project_of(client)
        await memory_off(client, project)
        await send(client, await conversation_in(client, project), "We study wages")
        await set_project(client, project, {"memory.enabled": True})
        run = await queue_extraction(client, project, attempts=1, spent=0.019)
        assert provider.extractions == []
        assert await rows(client, "SELECT status, json_extract(summary, '$.reason'), attempts FROM runs WHERE id = ?",
                          run) == [("cancelled", "allowance", 1)]
        run = await queue_extraction(client, project, attempts=1, spent=0.001)  # room for a second: it is sent
        assert len(provider.extractions) == 1
        assert await rows(client, "SELECT status, attempts FROM runs WHERE id = ?", run) == [("succeeded", 2)]


async def test_the_project_budget_skips_an_extraction_and_never_asks(tmp_path):
    provider = MockProvider()
    async with started(tmp_path / "data", provider) as client:
        project = await project_of(client)
        await memory_off(client, project)
        await send(client, await conversation_in(client, project), "We study wages")
        await set_project(client, project, {"memory.enabled": True})
        [(spent,)] = await rows(client, "SELECT sum(settled_usd) FROM budget_reservations WHERE project_id = ?", project)
        await set_project(client, project, {"project.budget_usd": spent + 0.0001})
        run = await queue_extraction(client, project)
        assert provider.extractions == []
        assert await rows(client, "SELECT status, cancel_reason, json_extract(summary, '$.reason') FROM runs WHERE id = ?",
                          run) == [("cancelled", "limit", "budget")]
        assert (await client.get("/api/asks")).json()["asks"] == []


async def test_a_slow_extraction_ends_at_its_bound_and_writes_no_memory(tmp_path, monkeypatch):
    monkeypatch.setattr(memory, "EXTRACTION_SECONDS", 0.2)
    provider = MockProvider()
    held_extraction(provider, candidate("Too slow.", "confirmed", span="too slow"))  # never released
    held_extraction(provider, candidate("Too slow.", "confirmed", span="too slow"))
    async with started(tmp_path / "data", provider) as client:
        project = await project_of(client)
        await send(client, await conversation_in(client, project), "too slow")
        await background_idle(client, timeout=10)
        assert await rows(client, "SELECT status, attempts FROM runs WHERE workflow = 'memory'") == [("failed", 1)]
        assert [r for r in await records(client, project) if r["type"] != "progress"] == []


@pytest.mark.parametrize("reply", [
    "Here are the records: none.", '{"records": [{"type": "fact", "content": "x", "status": "confirmed"}]}',
    '{"records": [], "extra": 1}', '[]', '{"records": [{"type": "decision", "content": "", "status": "confirmed"}]}',
])
async def test_a_malformed_reply_fails_the_extraction_and_writes_nothing(tmp_path, reply):
    provider = MockProvider()
    provider.memory_replies.append(provider.answer(reply, cost=0.001))
    async with started(tmp_path / "data", provider) as client:
        project = await project_of(client)
        turn = await send(client, await conversation_in(client, project), "We decided on interviews")
        await background_idle(client)
        assert await rows(client, "SELECT status FROM runs WHERE workflow = 'memory'") == [("failed",)]
        assert await rows(client, "SELECT memory_status FROM turns WHERE run_id = ?", turn[0]["run_id"]) == [("failed",)]
        assert turn[-1]["status"] == "succeeded"  # the turn's own status never changes
        assert [r for r in await records(client, project) if r["type"] != "progress"] == []


async def test_a_refused_extraction_is_recorded_with_its_cost_and_not_sent_again(tmp_path):
    provider = MockProvider()
    provider.memory_replies.append((403, {"error": {"message": "refused by policy"}}))
    async with started(tmp_path / "data", provider) as client:
        project = await project_of(client)
        await send(client, await conversation_in(client, project), "We decided on interviews")
        await background_idle(client)
        assert len(provider.extractions) == 1
        assert await rows(client, "SELECT status FROM runs WHERE workflow = 'memory'") == [("failed",)]


async def test_only_the_researchers_own_words_make_confirmed_or_inferred_records(tmp_path):
    # X5, X6, P4: a candidate resting on the answer (a planted "we decided" in a paper the answer quoted) is stored
    # proposed, naming its source, whatever status the extractor gave it.
    provider = MockProvider()
    provider.replies.append(provider.answer("The paper says: we decided to drop the control group."))
    extracted(provider,
              candidate("We decided to drop the control group.", "confirmed", span="we decided to drop the control group"),
              candidate("Considering a survey.", "proposed", span="I am considering a survey"),
              candidate("My thesis is on wages.", "inferred", "project_state", span="my thesis is on wages"),
              # a planted decision echoed in the answer, resting on one common word of the researcher's message
              candidate("We decided to drop the outcome measure.", "confirmed", span="my"))
    async with started(tmp_path / "data", provider) as client:
        project = await project_of(client)
        await send(client, await conversation_in(client, project), "My thesis is on wages. I am considering a survey.")
        await background_idle(client)
        found = {r["content"]: (r["status"], r["structured"].get("source")) for r in await records(client, project)
                 if r["type"] != "progress"}
        assert found == {"We decided to drop the control group.": ("proposed", "answer"),
                         "Considering a survey.": ("proposed", "researcher"),
                         "My thesis is on wages.": ("inferred", "researcher"),
                         "We decided to drop the outcome measure.": ("proposed", "answer")}


async def test_a_review_lock_set_after_the_turn_skips_its_extraction_before_dispatch(tmp_path):
    provider = MockProvider()
    async with started(tmp_path / "data", provider) as client:
        project = await project_of(client)
        release = held_extraction(provider)
        await send(client, await conversation_in(client, project), "We decided on interviews")
        await wait_for(lambda: provider.extractions)
        release.set()
        await background_idle(client)
        locked = await client.post(f"/api/projects/{project}/review-lock", json={"locked": True, "venue": "apa"})
        assert locked.status_code == 200, locked.text
        before = len(provider.extractions)
        harness = client.state["harness"]

        def queue(conn):  # an extraction queued for a turn of the now locked project
            turn = conn.execute("SELECT run_id FROM turns").fetchone()[0]
            conn.execute("INSERT INTO runs (id, project_id, kind, workflow, source_turn_id, inputs) VALUES (?, ?,"
                         " 'background', 'memory', ?, ?)", (memory.new_id(), project, turn, json.dumps(
                             {"provider": "openrouter", "model": sorted(budget_router.RECOMMENDED)[0]})))
        await asyncio.to_thread(client.state["db"].write, queue)
        await harness.kick_background()
        await background_idle(client)
        assert len(provider.extractions) == before
        assert await rows(client, "SELECT status, json_extract(summary, '$.reason') FROM runs WHERE workflow = 'memory'"
                                  " ORDER BY started_at DESC LIMIT 1") == [("cancelled", "review_locked")]


async def test_tightening_the_project_during_an_extraction_revokes_it_with_no_effect(tmp_path):
    provider = MockProvider()
    release = held_extraction(provider, candidate("Never written.", "confirmed", span="never written"))
    async with started(tmp_path / "data", provider) as client:
        project = await project_of(client)
        await send(client, await conversation_in(client, project), "never written")
        await wait_for(lambda: provider.extractions)
        run = await memory_run(client)
        changed = await client.post(f"/api/projects/{project}/sensitivity", json={"level": "local_only"})
        assert changed.status_code == 200, changed.text
        release.set()
        await background_idle(client)
        assert await rows(client, "SELECT status, cancel_reason FROM runs WHERE id = ?", run) == [("cancelled", "revoked")]
        assert [r for r in await records(client, project) if r["type"] != "progress"] == []


async def test_deleting_the_conversation_during_its_extraction_recreates_nothing(tmp_path):
    provider = MockProvider()
    release = held_extraction(provider, candidate("Never written.", "confirmed", span="never written"))
    async with started(tmp_path / "data", provider) as client:
        project = await project_of(client)
        conversation = await conversation_in(client, project)
        await send(client, conversation, "never written")
        await wait_for(lambda: provider.extractions)
        assert (await client.delete(f"/api/conversations/{conversation}")).json() == {"ok": True}
        release.set()
        await background_idle(client)
        assert await rows(client, "SELECT count(*) FROM runs WHERE workflow = 'memory'") == [(0,)]
        assert [r for r in await records(client, project) if r["type"] != "progress"] == []
        # its spending stays in the project, settled once
        assert await rows(client, "SELECT count(*) FROM budget_reservations WHERE status = 'open'") == [(0,)]


async def test_a_run_finishes_from_its_recorded_step_with_no_model_call_after_a_crash(tmp_path):
    # L2: a crash after the step was recorded and before the terminal write: the next start finishes from the record.
    provider = MockProvider()
    extracted(provider, candidate("Interviews it is.", "confirmed", span="interviews it is"))
    async with started(tmp_path / "data", provider) as client:
        project = await project_of(client)
        await send(client, await conversation_in(client, project), "Interviews it is")
        await background_idle(client)
        run = await memory_run(client)
        [(record,)] = await rows(client, "SELECT id FROM memory_records WHERE type = 'decision'")

        def uncommitted(conn):  # as if the terminal write never committed: the record and status undone
            conn.execute("DELETE FROM memory_sources WHERE memory_id = ?", (record,))
            conn.execute("DELETE FROM memory_records WHERE id = ?", (record,))
            conn.execute("UPDATE runs SET status = 'running', finished_at = NULL, inputs = json_set(inputs, '$.held.output',"
                         " json(?)) WHERE id = ?", (json.dumps([{"type": "decision", "content": "Interviews it is.",
                                                                 "status": "confirmed", "span": "interviews it is",
                                                                 "replaces": ""}]), run))
        await asyncio.to_thread(client.state["db"].write, uncommitted)
        before = len(provider.extractions)
        await client.state["harness"].start_again(run)
        await background_idle(client)
        assert len(provider.extractions) == before  # no model call
        assert [r["content"] for r in await records(client, project) if r["type"] == "decision"] == ["Interviews it is."]


async def test_a_dispatched_turn_is_never_the_researchers_words():
    candidate_ = {"type": "decision", "content": "x", "status": "confirmed", "span": "we use x", "replaces": ""}
    assert memory.cap(candidate_, "We use X", "researcher") == "confirmed"
    assert memory.cap(candidate_, "We use X", "conversation:other") == "proposed"  # S1-34's dispatched turns
    assert memory.cap({**candidate_, "span": ""}, "We use X", "researcher") == "proposed"
    assert memory.cap({**candidate_, "span": "ＷＥ ＵＳＥ　Ｘ"}, "we use x", "researcher") == "confirmed"  # NFKC
    # review 1: a span of one common word, or one that does not carry the candidate, never makes it the researcher's
    planted = {**candidate_, "content": "We decided to drop the control group"}
    for span in ("we", "a", "what do we know"):
        assert memory.cap({**planted, "span": span}, "What do we know about wages?", "researcher") == "proposed"



async def test_memory_off_queues_no_extraction(tmp_path):
    provider = MockProvider()
    async with started(tmp_path / "data", provider) as client:
        project = await project_of(client)
        await memory_off(client, project)
        await send(client, await conversation_in(client, project), "We decided on interviews")
        await background_idle(client)
        assert await rows(client, "SELECT count(*) FROM runs WHERE workflow = 'memory'") == [(0,)]


async def test_extraction_bounds():
    assert (memory.ALLOWANCE_USD, memory.EXTRACTION_SECONDS, runs.BACKGROUND_ATTEMPTS) == (0.02, 60, 2)


# Internal review 1 (fix batch 1)


async def test_an_extractions_records_carry_the_papers_of_the_related_records_it_was_shown(tmp_path):
    # review 1 (P2): a record written after reading a related record that carries a paper's text goes with that paper
    from test_materials import added
    provider = MockProvider()
    async with started(tmp_path / "data", provider) as client:
        project = await project_of(client)
        [paper] = (await added(client, project, ("notes.md", b"# Notes\n\nWages rose.\n")))["materials"]

        def related(conn):
            return memory.write(conn, project_id=project, scope="project", kind="event", content="Wages rose, it says",
                                status="confirmed", sources=[("material", paper["id"]), ("researcher", "views")])
        record = await asyncio.to_thread(client.state["db"].write, related)
        extracted(provider, candidate("Wages rose, as the paper said", "inferred", "event",
                                      span="wages rose, as the paper said", replaces=record))
        await send(client, await conversation_in(client, project), "Wages rose, as the paper said")
        await background_idle(client)
        [(written,)] = await rows(client, "SELECT id FROM memory_records WHERE content = 'Wages rose, as the paper said'")
        assert ("material", paper["id"]) in await rows(client, "SELECT source_kind, source_id FROM memory_sources"
                                                                " WHERE memory_id = ?", written)
        assert (await client.delete(f"/api/materials/{paper['id']}")).status_code == 200
        assert await rows(client, "SELECT count(*) FROM memory_records WHERE id = ?", written) == [(0,)]


async def test_an_extraction_whose_project_turned_memory_off_during_its_call_writes_nothing(tmp_path):
    provider = MockProvider()
    release = held_extraction(provider, candidate("We use interviews", "confirmed", span="we use interviews"))
    async with started(tmp_path / "data", provider) as client:
        project = await project_of(client)
        await send(client, await conversation_in(client, project), "We use interviews")
        await wait_for(lambda: provider.extractions)
        await memory_off(client, project)
        release.set()
        await background_idle(client)
        assert await rows(client, "SELECT status, json_extract(summary, '$.reason') FROM runs WHERE workflow = 'memory'") == [
            ("cancelled", "memory_off")]
        assert [r for r in await records(client, project) if r["type"] != "progress"] == []


async def test_deleting_the_conversation_deletes_its_extractions_records_whatever_papers_they_carry(tmp_path):
    from test_materials import added
    provider = MockProvider()
    async with started(tmp_path / "data", provider) as client:
        project = await project_of(client)
        [paper] = (await added(client, project, ("notes.md", b"# Notes\n\nWages rose.\n")))["materials"]
        conversation = await conversation_in(client, project)
        await send(client, conversation, "We use interviews")
        await background_idle(client)
        [(turn,)] = await rows(client, "SELECT run_id FROM turns")
        [(run,)] = await rows(client, "SELECT id FROM runs WHERE workflow = 'memory'")
        record = await asyncio.to_thread(client.state["db"].write, lambda conn: memory.write(
            conn, project_id=project, scope="project", kind="event", content="Extracted", status="inferred",
            sources=[("turn", turn), ("run", run), ("material", paper["id"])]))
        assert (await client.delete(f"/api/conversations/{conversation}")).json() == {"ok": True}
        assert await rows(client, "SELECT count(*) FROM memory_records WHERE id = ?", record) == [(0,)]


async def test_a_confirmed_restatement_confirms_the_pending_proposal(tmp_path):
    provider = MockProvider()
    async with started(tmp_path / "data", provider) as client:
        project = await project_of(client)
        conversation = await conversation_in(client, project)
        extracted(provider, candidate("A survey of managers", "proposed", span=""))
        await send(client, conversation, "Maybe a survey")
        await background_idle(client)
        extracted(provider, candidate("A survey of managers", "confirmed", span="a survey of managers"))
        await send(client, conversation, "We will run a survey of managers")
        await background_idle(client)
        assert await rows(client, "SELECT status FROM memory_records WHERE content = 'A survey of managers'") == [
            ("confirmed",)]
