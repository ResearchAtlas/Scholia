"""Memory's policy wherever it is used (S1-21a fix batches 1 to 3, the `gpt-6-astra` review and its rechecks; slice-1 spec
sections 8 and 10; Harold's readings and his decision of 2026-10-23): a record's scope and validity are fixed when it is
written. Personal records hold only the researcher's own profile and preference statements, written from a Normal,
unlocked project, with no paper text; a later tightening, lock or move never retracts or converts them, nor copies of
them; a locked project recalls none. A record in a request is current and allowed when it is dispatched; a late
extraction never undoes a forget; a late write never keeps a deleted paper's text; memory switched off stops an admitted
turn at its next paid request. Each test failed before its fix."""

import asyncio
import json
import sys

import pytest

from agentic import Scripted, call, steps_reply, tool_messages
from backend import memory, runs
from scholia_app import MockProvider, background_idle, send, started
from test_detached_tail import wait_for
from test_materials import project_of
from test_memory import candidate, conversation_in, extracted, memory_off, rows, set_project

pytestmark = pytest.mark.asyncio


async def personal(client, project, content="I prefer answers about participant Alice"):
    response = await client.post("/api/memory", json={"project_id": project, "type": "preference", "scope": "personal",
                                                      "content": content})
    assert response.status_code == 201, response.text
    return response.json()["id"]


async def recalled(client, project):
    return await asyncio.to_thread(client.state["db"].read, lambda conn: [
        r["content"] for r in memory.recall_candidates(conn, project)])


async def tighten(client, project, how):
    if how == "lock":
        response = await client.post(f"/api/projects/{project}/review-lock", json={"locked": True})
    else:
        response = await client.post(f"/api/projects/{project}/sensitivity", json={"level": how})
    assert response.status_code == 200, response.text


async def turn_in(client, project, message):
    """A succeeded turn of project with this message, and a memory extraction run for it (as a primary commit writes)."""
    def write(conn):
        conversation, turn, run = memory.new_id(), memory.new_id(), memory.new_id()
        conn.execute("INSERT INTO conversations (id, project_id, title) VALUES (?, ?, 't')", (conversation, project))
        conn.execute("INSERT INTO runs (id, project_id, conversation_id, kind, workflow, status, inputs) VALUES"
                     " (?, ?, ?, 'turn', 'agent', 'succeeded', '{}')", (turn, project, conversation))
        conn.execute("INSERT INTO turns (run_id, conversation_id, seq, author, user_message, answer) VALUES"
                     " (?, ?, 0, 'researcher', ?, '{\"text\": \"ok\"}')", (turn, conversation, json.dumps({"text": message})))
        conn.execute("INSERT INTO runs (id, project_id, kind, workflow, source_turn_id, inputs) VALUES"
                     " (?, ?, 'background', 'x', ?, '{}')", (run, project, turn))
        return run
    return await asyncio.to_thread(client.state["db"].write, write)


async def test_a_private_projects_restatement_never_confirms_a_personal_proposal(tmp_path):
    # 2
    async with started(tmp_path / "data") as client:
        normal, private, other = await project_of(client, "N"), await project_of(client, "P", level="private"), \
            await project_of(client, "O")
        proposal = await asyncio.to_thread(client.state["db"].write, lambda conn: memory.write(
            conn, project_id=normal, scope="personal", kind="preference", content="I prefer tables about Alice",
            status="proposed", sources=[("project", normal)]))
        run = await turn_in(client, private, "I prefer tables about Alice")
        await asyncio.to_thread(client.state["db"].write, lambda conn: memory.apply_extraction(conn, run, [
            {"type": "preference", "content": "I prefer tables about Alice", "status": "confirmed",
             "span": "i prefer tables about alice", "replaces": ""}]))
        assert await rows(client, "SELECT status FROM memory_records WHERE id = ?", proposal) == [("proposed",)]
        assert await recalled(client, other) == []


async def test_a_late_extraction_never_undoes_a_forget_made_in_another_project(tmp_path):
    # 4: the shared personal scope
    provider = MockProvider()
    release = asyncio.Event()

    async def held(body):
        await release.wait()
        return provider.answer(json.dumps({"records": [{"type": "preference", "content": "I prefer tables",
                                                        "status": "confirmed", "span": "i prefer tables",
                                                        "replaces": ""}]}), cost=0.001)
    async with started(tmp_path / "data", provider) as client:
        a, b = await project_of(client, "A"), await project_of(client, "B")
        provider.memory_replies.append(held)
        await send(client, await conversation_in(client, b), "I prefer tables")
        await wait_for(lambda: provider.extractions)
        in_a = await conversation_in(client, a)
        await send(client, in_a, "Remember: I prefer tables")
        await send(client, in_a, "Forget: I prefer tables")
        assert await rows(client, "SELECT count(*) FROM memory_records WHERE content = 'I prefer tables'") == [(0,)]
        release.set()
        await background_idle(client)
        assert await rows(client, "SELECT status FROM memory_records WHERE content = 'I prefer tables'"
                                  " AND status IN ('confirmed', 'inferred')") == []


async def test_a_forget_in_the_views_counts_as_later_too(tmp_path):
    async with started(tmp_path / "data") as client:
        a = await project_of(client, "A")
        run = await turn_in(client, a, "I prefer tables")
        record = await personal(client, a, "I prefer tables")
        assert (await client.delete(f"/api/memory/{record}")).json() == {"ok": True}
        await asyncio.to_thread(client.state["db"].write, lambda conn: memory.apply_extraction(conn, run, [
            {"type": "preference", "content": "I prefer tables", "status": "confirmed", "span": "i prefer tables",
             "replaces": ""}]))
        assert await rows(client, "SELECT status FROM memory_records WHERE content = 'I prefer tables'") == [("proposed",)]


async def test_a_reply_recorded_after_its_paper_was_deleted_keeps_none_of_its_text(tmp_path, monkeypatch):
    # 5: the receipt is recorded after the deletion committed; a terminal write that then fails must find nothing held
    from cited import Provider, cited, with_papers
    from test_search import WAGES
    provider = Provider()
    async with cited(tmp_path, provider) as client:
        project, [paper] = await with_papers(client, WAGES)

        async def deleting(body):  # the paper's deletion starts as the reply arrives, and commits before it is recorded
            deleted.append(asyncio.create_task(client.delete(f"/api/materials/{paper}")))
            return provider.answer(json.dumps({"records": [{"type": "event", "content": "CANARY wages rose",
                                                            "status": "proposed", "span": "", "replaces": ""}]}))
        deleted = []
        provider.memory_replies.append(deleting)
        write = runs.Harness._write

        async def recorded_after_the_deletion(self, fn):
            if deleted and fn.__qualname__.endswith("_call.<locals>.record"):
                assert (await deleted[0]).status_code == 200
            return await write(self, fn)
        monkeypatch.setattr(runs.Harness, "_write", recorded_after_the_deletion)
        real = runs.Harness._finish_background

        def failing(self, conn, active, *args, **kwargs):
            if conn.execute("SELECT workflow FROM runs WHERE id = ?", (active.run_id,)).fetchone()[0] == "memory":
                raise RuntimeError("the terminal write failed")
            return real(self, conn, active, *args, **kwargs)
        monkeypatch.setattr(runs.Harness, "_finish_background", failing)
        await send(client, await conversation_in(client, project), "What do minimum wages do to earnings?")
        await wait_for(lambda: deleted and deleted[0].done(), timeout=20)
        await asyncio.sleep(1.0)  # its receipt recorded, its terminal write failed
        assert "CANARY" not in json.dumps(await rows(client, "SELECT inputs FROM runs"))
        assert "CANARY" not in json.dumps(await rows(client, "SELECT data FROM run_events"))
        assert not await rows(client, "SELECT 1 FROM materials WHERE id = ?", paper)


async def test_memory_switched_off_mid_turn_stops_the_turns_memory_tools(tmp_path):
    # 6: the switch is read when it is used, not at admission
    from cited import cited, with_papers
    from test_search import WAGES
    switched = {}

    async def off_then_read(body):
        await memory_off(switched["client"], switched["project"])
        return steps_reply(provider, [call("read_memory"), call("update_progress", "c2", kind="next_step",
                                                                text="Screen them")])
    provider = Scripted(off_then_read, "Done.")
    async with cited(tmp_path, provider) as client:
        project, _ = await with_papers(client, WAGES)
        switched.update(client=client, project=project)
        await client.post("/api/memory", json={"project_id": project, "type": "decision", "content": "SECRET decision"})
        await send(client, await conversation_in(client, project), "Translate this: hello")
        read, noted = tool_messages(provider.agent_steps[1])
        assert "SECRET" not in read["content"] and "nothing is recalled" in read["content"]
        assert "not kept" in noted["content"]
        assert await rows(client, "SELECT count(*) FROM memory_records WHERE type = 'agent_note'") == [(0,)]


async def test_memory_switched_off_mid_turn_ends_a_turn_that_recalled_memory(tmp_path):
    # fix batch 2 (finding 4) replaces dropping the sections: the turn's first step and its reply held recalled memory,
    # so the turn ends at the next step boundary, revoked, rather than sending a later step
    from cited import cited, with_papers
    from test_search import WAGES
    switched = {}

    async def off_then_list(body):
        await memory_off(switched["client"], switched["project"])
        return steps_reply(provider, [call("list_materials")])
    provider = Scripted(off_then_list, "Done.")
    async with cited(tmp_path, provider) as client:
        project, _ = await with_papers(client, WAGES)
        switched.update(client=client, project=project)
        await client.post("/api/memory", json={"project_id": project, "type": "decision", "content": "SECRET decision"})
        stream = await send(client, await conversation_in(client, project), "What did we decide last time?")
        assert "SECRET" in json.dumps(provider.agent_steps[0])
        assert len(provider.agent_steps) == 1 and stream[-1]["cancel_reason"] == "revoked"


async def test_a_personal_inference_waits_in_things_to_confirm(tmp_path):
    # 8: ticket 31: personal inferred stays proposed and unused until confirmed
    provider = MockProvider()
    extracted(provider, candidate("I prefer short answers", "inferred", "preference", span="i prefer short answers"))
    async with started(tmp_path / "data", provider) as client:
        project = await project_of(client)
        await send(client, await conversation_in(client, project), "I prefer short answers, I think")
        await background_idle(client)
        assert await rows(client, "SELECT scope, status FROM memory_records WHERE type = 'preference'") == [
            ("personal", "proposed")]
        review = (await client.get(f"/api/memory/review?project_id={project}")).json()
        assert [p["content"] for p in review["proposals"]] == ["I prefer short answers"]


async def test_a_request_with_recalled_memory_never_says_memory_is_unavailable(tmp_path):
    # 9
    provider = MockProvider()
    async with started(tmp_path / "data", provider) as client:
        project = await project_of(client)
        await client.post("/api/memory", json={"project_id": project, "type": "decision", "content": "We dropped the pilot"})
        await send(client, await conversation_in(client, project), "Why did I drop the pilot? We decided last time.")
        system = provider.answers[-1]["messages"][0]["content"]
        assert "We dropped the pilot" in system and "not available yet" not in system


# Fix batch 2 (gpt-6-astra recheck 2 on f8e5349): every source kind, status, switch and section


async def test_a_superseded_record_never_leaves_at_dispatch(tmp_path):
    # 3: only current statuses
    async with started(tmp_path / "data") as client:
        project = await project_of(client)
        record = (await client.post("/api/memory", json={"project_id": project, "type": "decision",
                                                          "content": "We use interviews"})).json()
        await client.patch(f"/api/memory/{record['id']}", json={"version": 1, "content": "We use surveys"})
        assert await asyncio.to_thread(client.state["db"].read,
                                       lambda conn: memory.allowed(conn, project, [record["id"]])) is False


async def test_memory_off_mid_turn_ends_a_turn_whose_chain_holds_memory(tmp_path):
    # 4: the chain is never rewritten (model-route rule 5), so a turn that read memory stops at the next step boundary
    from cited import cited, with_papers
    from test_search import WAGES
    switched = {}

    async def read_then_off(body):
        return steps_reply(provider, [call("read_memory")])

    async def off_then_list(body):
        await memory_off(switched["client"], switched["project"])
        return steps_reply(provider, [call("list_materials", "c2")])
    provider = Scripted(read_then_off, off_then_list, "Done.")
    async with cited(tmp_path, provider) as client:
        project, _ = await with_papers(client, WAGES)
        switched.update(client=client, project=project)
        await client.post("/api/memory", json={"project_id": project, "type": "decision", "content": "CANARY decision"})
        stream = await send(client, await conversation_in(client, project), "Translate this: hello")
        assert len(provider.agent_steps) == 2  # no third request carries the memory tool's result
        assert stream[-1]["status"] == "cancelled" and stream[-1]["cancel_reason"] == "revoked"


async def test_a_decision_in_project_state_is_not_said_missing(tmp_path):
    # 6
    provider = MockProvider()
    async with started(tmp_path / "data", provider) as client:
        project = await project_of(client)
        conversation = await conversation_in(client, project)
        await send(client, conversation, "Remember: we decided our method is semi-structured interviews")
        await send(client, conversation, "Which method did we decide on last time?")
        system = provider.answers[-1]["messages"][0]["content"]
        # said conditionally, never against what the request holds (fix batch 2's review: an unrelated state record
        # must not remove it either)
        assert "semi-structured interviews" in system and memory.MISSING in system
        assert memory.MISSING.startswith("If neither USER MEMORY nor PROJECT STATE holds")


# Fix batch 2, internal review: dependencies only where the text may come from another record, flat, never a cascade


async def extraction_shown(client, project, message, shown, candidates):
    run = await turn_in(client, project, message)

    def extract(conn):
        conn.execute("UPDATE runs SET inputs = json_set(inputs, '$.memory_ids', json(?)) WHERE id = ?",
                     (json.dumps(shown), run))
        return memory.apply_extraction(conn, run, candidates)
    return await asyncio.to_thread(client.state["db"].write, extract)


async def test_records_extracted_generation_after_generation_stay_recalled_and_quick(tmp_path):
    import time
    async with started(tmp_path / "data") as client:
        project = await project_of(client)
        for generation in range(12):
            shown = [r for (r,) in await rows(client, "SELECT id FROM memory_records ORDER BY rowid DESC LIMIT 20")]
            await extraction_shown(client, project, f"We use method {generation} and tool {generation}", shown, [
                {"type": "decision", "content": f"We use method {generation} and tool {generation}", "status": "confirmed",
                 "span": f"we use method {generation} and tool {generation}", "replaces": ""},
                {"type": "event", "content": f"Answer said thing {generation}", "status": "proposed", "span": "",
                 "replaces": ""}])
        started_at = time.monotonic()
        found = await asyncio.to_thread(client.state["db"].read, lambda conn: memory.recall_candidates(conn, project))
        assert len(found) == 12 and time.monotonic() - started_at < 1.0


async def test_a_confirmed_preference_in_the_researchers_own_words_reaches_other_projects(tmp_path):
    async with started(tmp_path / "data") as client:
        b, c = await project_of(client, "B"), await project_of(client, "C")
        state = (await client.post("/api/memory", json={"project_id": b, "type": "decision", "content": "B's own"})).json()
        await extraction_shown(client, b, "I prefer APA citations always", [state["id"]], [
            {"type": "preference", "content": "I prefer APA citations always", "status": "confirmed",
             "span": "i prefer apa citations always", "replaces": ""}])
        assert await recalled(client, c) == ["I prefer APA citations always"]


async def test_forgetting_a_record_never_deletes_one_confirmed_from_the_researchers_words(tmp_path):
    async with started(tmp_path / "data") as client:
        project = await project_of(client)
        shown = (await client.post("/api/memory", json={"project_id": project, "type": "decision",
                                                         "content": "Decision A"})).json()["id"]
        await extraction_shown(client, project, "The sample is thirty nurses", [shown], [
            {"type": "project_state", "content": "The sample is thirty nurses", "status": "confirmed",
             "span": "the sample is thirty nurses", "replaces": ""}])
        assert (await client.delete(f"/api/memory/{shown}")).json() == {"ok": True}
        assert await rows(client, "SELECT content FROM memory_records WHERE type = 'project_state'") == [
            ("The sample is thirty nurses",)]


async def test_an_earlier_decision_question_keeps_its_cannot_be_found_line_beside_unrelated_state(tmp_path):
    provider = MockProvider()
    async with started(tmp_path / "data", provider) as client:
        project = await project_of(client)
        await client.post("/api/memory", json={"project_id": project, "type": "project_state",
                                               "content": "Our research question is about wages"})
        await send(client, await conversation_in(client, project), "Which sampling method did we decide on last time?")
        assert memory.MISSING in provider.answers[-1]["messages"][0]["content"]


# Fix batch 3 (gpt-6-astra recheck 3 on 9dd3ff7; Harold's decision of 2026-10-23): a record's scope and validity are
# fixed when it is written. The reviewer's cases R1, R2, R6 and R7, asserting the decided rule.


def allowed_in(client, project, ids):
    return asyncio.to_thread(client.state["db"].read, lambda conn: memory.allowed(conn, project, ids))


async def noted_after_recalling(client, project, recalled_ids, text="Ask Alice"):
    """An agent's note written by a turn of project whose requests held recalled_ids."""
    run = await turn_in(client, project, "x")

    def noted(conn):
        turn = conn.execute("SELECT source_turn_id FROM runs WHERE id = ?", (run,)).fetchone()[0]
        conn.execute("UPDATE runs SET inputs = json_set(inputs, '$.memory_ids', json(?)) WHERE id = ?",
                     (json.dumps(recalled_ids), turn))
        return memory.note(conn, project_id=project, run_id=turn, kind="next_step", text=text, materials=())
    return await asyncio.to_thread(client.state["db"].write, noted)


@pytest.mark.parametrize("how", ["private", "local_only", "lock"])
async def test_a_later_tightening_never_retracts_or_converts_a_personal_record(tmp_path, how):
    # decision point 2: a request already holding it stays dispatchable; other projects still recall it; a locked
    # project recalls none, a Private or Local only one recalls personal records as before
    async with started(tmp_path / "data") as client:
        a, b = await project_of(client, "A"), await project_of(client, "B")
        record = await personal(client, a)
        conversation = await conversation_in(client, b)
        run = memory.new_id()
        db = client.state["db"]
        await asyncio.to_thread(db.write, lambda conn: conn.execute(
            "INSERT INTO runs (id, project_id, conversation_id, kind, workflow, inputs) VALUES (?, ?, ?, 'turn', 'agent', ?)",
            (run, b, conversation, json.dumps({"memory_ids": [record]}))))
        await tighten(client, a, how)
        assert await asyncio.to_thread(db.read, lambda conn: runs.may_dispatch(conn, run)) is True
        assert await rows(client, "SELECT scope, project_id FROM memory_records WHERE id = ?", record) == [("personal", a)]
        assert await recalled(client, b) == ["I prefer answers about participant Alice"]
        locked = how == "lock"
        found = await asyncio.to_thread(db.read, lambda conn: [r["content"] for r in memory.recall_candidates(
            conn, a, locked=locked)])
        assert found == ([] if locked else ["I prefer answers about participant Alice"])


async def test_r1_a_note_from_a_personal_record_stays_after_that_records_conversation_moves(tmp_path):
    # R1: B writes a note after recalling A's personal record; A's conversation moves into Private project C
    async with started(tmp_path / "data") as client:
        a, b = await project_of(client, "A"), await project_of(client, "B")
        c = await project_of(client, "C", level="private")
        in_a = await conversation_in(client, a)
        await send(client, in_a, "Remember: I prefer answers about participant Alice")
        [(source,)] = await rows(client, "SELECT id FROM memory_records WHERE scope = 'personal'")
        note = await noted_after_recalling(client, b, [source])
        assert (await client.post(f"/api/conversations/{in_a}/move", json={"project_id": c})).status_code == 200
        assert await rows(client, "SELECT scope FROM memory_records WHERE id = ?", source) == [("personal",)]
        assert await allowed_in(client, b, [source, note]) is True
        assert await recalled(client, b) == ["I prefer answers about participant Alice"]
        assert {k for k, _ in await rows(client, "SELECT source_kind, source_id FROM memory_sources WHERE memory_id = ?",
                                         note)} == {"run"}  # no derived sources: nothing follows another record


async def test_r2_an_answer_restating_a_personal_record_stays_sendable_after_its_project_tightens(tmp_path):
    # R2: B's answer restated A's personal record; A turns Private; B's history and extraction may still carry it
    async with started(tmp_path / "data") as client:
        a, b = await project_of(client, "A"), await project_of(client, "B")
        source = await personal(client, a)
        run = await turn_in(client, b, "x")
        await tighten(client, a, "private")

        def extract(conn):
            conn.execute("UPDATE runs SET inputs = json_set(inputs, '$.memory_ids', json(?)) WHERE id = ?",
                         (json.dumps([source]), run))
            return memory.apply_extraction(conn, run, [
                {"type": "event", "content": "Alice prefers tables", "status": "proposed", "span": "", "replaces": ""}])
        assert await asyncio.to_thread(client.state["db"].write, extract) is not None
        assert await allowed_in(client, b, [source]) is True


async def test_r6_r7_a_note_stays_in_where_we_left_off_and_the_export_after_its_source_tightens(tmp_path):
    # R6 and R7: B's note written after recalling A's personal record; A turns Private; B's Where we left off shows
    # it, and B's export (B is Normal) holds it without a passphrase: its validity was fixed when it was written
    from test_backups import archived_run, read
    destination = tmp_path / "chosen"
    destination.mkdir()
    async with started(tmp_path / "data") as client:
        a, b = await project_of(client, "A"), await project_of(client, "B")
        source = await personal(client, a)
        note = await noted_after_recalling(client, b, [source], "Ask Alice about tables")
        await tighten(client, a, "private")
        assert await allowed_in(client, b, [note]) is True
        assert (await client.get(f"/api/projects/{b}/left-off")).json()["next_step"]["content"] == "Ask Alice about tables"
        export = await archived_run(client, f"/api/projects/{b}/export", {"destination": str(destination)})
        assert export.status == "succeeded"
    held = json.loads(read(destination / export.result["file"].rsplit("/", 1)[1], "memory.json"))
    assert [r["content"] for r in held] == ["Ask Alice about tables"]


async def test_a_project_record_resting_on_a_moved_turn_stays_its_projects(tmp_path):
    # decision point 3: project records never leave their project, except with a moved conversation (G9); one another
    # conversation also stands behind stays, and stays allowed
    async with started(tmp_path / "data") as client:
        normal, private = await project_of(client, "N"), await project_of(client, "P", level="private")
        moving, staying = await conversation_in(client, normal), await conversation_in(client, normal)
        await send(client, staying, "Remember: the sample is 30 nurses")
        await send(client, moving, "Correct: the sample is 40 nurses")  # its sources: both conversations' turns
        assert (await client.post(f"/api/conversations/{moving}/move", json={"project_id": private})).status_code == 200
        [(record, project)] = await rows(client, "SELECT id, project_id FROM memory_records"
                                                 " WHERE content = 'the sample is 40 nurses'")
        assert project == normal and await allowed_in(client, normal, [record]) is True


async def test_a_personal_record_carries_no_paper_and_an_answers_preference_stays_the_projects(tmp_path):
    # decision point 1: personal records hold only what the researcher states or confirms, with no paper text
    from cited import cited, with_papers
    from test_search import WAGES
    async with cited(tmp_path) as client:
        project, [paper] = await with_papers(client, WAGES)
        run = await turn_in(client, project, "I prefer short answers")

        def extract(conn):
            conn.execute("UPDATE runs SET inputs = json_set(inputs, '$.context_materials', json(?)) WHERE id = ?",
                         (json.dumps([paper]), run))
            return memory.apply_extraction(conn, run, [
                {"type": "preference", "content": "I prefer short answers", "status": "confirmed",
                 "span": "i prefer short answers", "replaces": ""},
                {"type": "preference", "content": "Prefers minimum wage studies", "status": "proposed", "span": "",
                 "replaces": ""}])
        await asyncio.to_thread(client.state["db"].write, extract)
        found = dict(await rows(client, "SELECT content, scope FROM memory_records WHERE type = 'preference'"))
        assert found == {"I prefer short answers": "personal", "Prefers minimum wage studies": "project"}
        assert await rows(client, "SELECT count(*) FROM memory_sources s JOIN memory_records m ON m.id = s.memory_id"
                                  " WHERE m.scope = 'personal' AND s.source_kind = 'material'") == [(0,)]


async def test_r3_editing_a_proposal_makes_a_new_version_so_a_request_holding_the_old_text_is_refused(tmp_path):
    async with started(tmp_path / "data") as client:
        project = await project_of(client)
        proposal = await asyncio.to_thread(client.state["db"].write, lambda conn: memory.write(
            conn, project_id=project, scope="project", kind="decision", content="We use interviews", status="proposed",
            sources=[("project", project)]))
        assert await allowed_in(client, project, [proposal]) is True
        edited = await client.patch(f"/api/memory/{proposal}", json={"version": 1, "content": "We use surveys"})
        assert edited.status_code == 200, edited.text
        assert await allowed_in(client, project, [proposal]) is False  # the request that held "interviews"
        assert await rows(client, "SELECT content, status FROM memory_records WHERE type = 'decision'") == [
            ("We use surveys", "confirmed")]  # nothing of the old text is kept


async def test_r4_memory_off_refuses_a_nested_paid_request_of_a_turn_holding_memory(tmp_path):
    # R4: switched off during the first step, which calls answer_from_library: its inner request is never sent
    from cited import cited, with_papers
    from test_search import WAGES
    switched = {}

    async def off_then_answer(body):
        await memory_off(switched["client"], switched["project"])
        return steps_reply(provider, [call("answer_from_library", question="What do minimum wages do?")])
    provider = Scripted(off_then_answer, "Done.", inner=["Minimum wages raise earnings [S1.1]."])
    async with cited(tmp_path, provider) as client:
        project, _ = await with_papers(client, WAGES)
        switched.update(client=client, project=project)
        await client.post("/api/memory", json={"project_id": project, "type": "decision", "content": "SECRET decision"})
        stream = await send(client, await conversation_in(client, project), "What did we decide last time?")
        assert "SECRET" in json.dumps(provider.agent_steps[0])
        assert provider.inners == [] and len(provider.agent_steps) == 1
        assert stream[-1]["status"] == "cancelled" and stream[-1]["cancel_reason"] == "revoked"


async def test_r5_forget_removes_a_held_extraction_output_that_was_shown_the_record(tmp_path):
    async with started(tmp_path / "data") as client:
        project = await project_of(client)
        shown = (await client.post("/api/memory", json={"project_id": project, "type": "decision",
                                                         "content": "Decision A"})).json()["id"]
        run = await turn_in(client, project, "x")
        await asyncio.to_thread(client.state["db"].write, lambda conn: conn.execute(
            "UPDATE runs SET inputs = json_set(inputs, '$.memory_ids', json(?), '$.held.output', json(?)) WHERE id = ?",
            (json.dumps([shown]), json.dumps({"records": [{"content": "CANARY restating Decision A"}]}), run)))
        assert (await client.delete(f"/api/memory/{shown}")).json() == {"ok": True}
        assert "CANARY" not in json.dumps(await rows(client, "SELECT inputs FROM runs"))


async def test_r8_every_source_kind_a_record_may_have_is_named_in_both_languages():
    # R8: the memory views name a record's sources by catalog key; no raw key is ever shown
    import re
    from pathlib import Path
    from backend.db import migrations
    kinds = re.search(r"NEW\.source_kind NOT IN \(([^)]*)\)", migrations._0008).group(1)
    root = Path(__file__).resolve().parents[1] / "frontend" / "src" / "i18n"
    for catalog in ("en.json", "zh-CN.json"):
        keys = json.loads((root / catalog).read_text(encoding="utf-8"))
        assert [k for k in re.findall(r"'(\w+)'", kinds) if f"memory.source.{k}" not in keys] == []


# Fix batch 3, internal review


async def test_a_record_saying_the_opposite_of_the_researchers_words_is_never_theirs(tmp_path):
    # P2: "not" and "no" are terms a confirmed record must find in the researcher's words, never dropped as common
    async with started(tmp_path / "data") as client:
        project = await project_of(client)
        run = await turn_in(client, project, "I prefer tables in every answer")
        await asyncio.to_thread(client.state["db"].write, lambda conn: memory.apply_extraction(conn, run, [
            {"type": "preference", "content": "I prefer not tables in every answer", "status": "confirmed",
             "span": "i prefer tables in every answer", "replaces": ""}]))
        assert await rows(client, "SELECT scope, status FROM memory_records") == [("project", "proposed")]


async def test_an_extraction_shown_a_proposal_edited_since_still_writes_as_proposals(tmp_path):
    # P3: an edited proposal is a replaced record (section 8), not a forgotten one: only a Forget's tombstone stops it
    async with started(tmp_path / "data") as client:
        project = await project_of(client)
        proposal = await asyncio.to_thread(client.state["db"].write, lambda conn: memory.write(
            conn, project_id=project, scope="project", kind="decision", content="We use interviews", status="proposed",
            sources=[("project", project)]))
        run = await turn_in(client, project, "We sample thirty nurses")
        await asyncio.to_thread(client.state["db"].write, lambda conn: conn.execute(
            "UPDATE runs SET inputs = json_set(inputs, '$.memory_ids', json(?)) WHERE id = ?", (json.dumps([proposal]), run)))
        await client.patch(f"/api/memory/{proposal}", json={"version": 1, "content": "We use surveys"})
        written = await asyncio.to_thread(client.state["db"].write, lambda conn: memory.apply_extraction(conn, run, [
            {"type": "project_state", "content": "We sample thirty nurses", "status": "confirmed",
             "span": "we sample thirty nurses", "replaces": ""}]))
        assert written is not None and written["counts"]["confirmed"] == 1


async def test_an_extraction_holding_no_record_is_refused_at_dispatch_once_memory_is_off(tmp_path, monkeypatch):
    # P3: memory off after the extraction's own check and before its request leaves: nothing is sent
    provider = MockProvider()
    extracted(provider, candidate("we sample thirty nurses", span="we sample thirty nurses"))
    async with started(tmp_path / "data", provider) as client:
        project = await project_of(client)
        facts = runs.Harness._route_facts

        async def switched_off_meanwhile(self, *args, **kwargs):
            if sys._getframe(1).f_code.co_name == "_extraction_call":
                await memory_off(client, project)
            return await facts(self, *args, **kwargs)
        monkeypatch.setattr(runs.Harness, "_route_facts", switched_off_meanwhile)
        await send(client, await conversation_in(client, project), "We sample thirty nurses")
        await background_idle(client)
        assert provider.extractions == []
        assert await rows(client, "SELECT status, cancel_reason FROM runs WHERE workflow = 'memory'") == [
            ("cancelled", "revoked")]


# Fix batch 3, internal recheck: the negation compared both ways, and never the second word


@pytest.mark.parametrize("content, said", [
    ("I like short answers", "I do not like short answers"), ("I want summaries", "I don't want summaries"),
    ("I want summaries", "I want no summaries"), ("Prefer tabs", "Prefer tabs, no spaces"),
    ("I want summaries", "I never want summaries"), ("我喜欢摘要", "我不喜欢摘要"), ("Python", "not Python"),
    ("tables", "no tables")])
async def test_a_record_dropping_the_researchers_negation_is_never_theirs(content, said):
    assert memory.cap({"content": content, "span": said, "status": "confirmed"}, said, "researcher") == "proposed"


async def test_the_researchers_own_negated_words_stay_theirs():
    said = "I do not want summaries in every answer"
    assert memory.cap({"content": said, "span": said, "status": "confirmed"}, said, "researcher") == "confirmed"
