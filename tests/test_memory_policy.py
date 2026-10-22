"""Memory's policy wherever it is used (S1-21a fix batch 1, `gpt-6-astra` review 1 on `84b5a6d`; slice-1 spec sections 8
and 10; Harold's readings): a record in a request is still allowed when it is dispatched, not only present; nothing of a
Private, Local only or review-locked project becomes, stays or comes back as personal memory; a late extraction never
undoes a forget made in another project; a late write never restores a deleted paper's text; memory switched off stops
an admitted turn's memory tools; a personal inference waits in Things to confirm. Each test failed before its fix."""

import asyncio
import json

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


@pytest.mark.parametrize("how", ["private", "local_only", "lock"])
async def test_a_request_holding_a_personal_record_is_refused_once_its_project_is_tightened(tmp_path, how):
    # 1: section 10's dispatch check: every memory record in the request's context still exists and is still allowed
    async with started(tmp_path / "data") as client:
        a, b = await project_of(client, "A"), await project_of(client, "B")
        record = await personal(client, a)
        conversation = await conversation_in(client, b)
        run = memory.new_id()
        db = client.state["db"]
        await asyncio.to_thread(db.write, lambda conn: conn.execute(
            "INSERT INTO runs (id, project_id, conversation_id, kind, workflow, inputs) VALUES (?, ?, ?, 'turn', 'agent', ?)",
            (run, b, conversation, json.dumps({"memory_ids": [record]}))))
        assert await asyncio.to_thread(db.read, lambda conn: runs.may_dispatch(conn, run)) is True
        await tighten(client, a, how)
        assert await asyncio.to_thread(db.read, lambda conn: runs.may_dispatch(conn, run)) is False


@pytest.mark.parametrize("how", ["private", "lock"])
async def test_a_tightened_projects_personal_record_stays_hidden_when_the_project_is_deleted(tmp_path, how):
    # 3: hidden stays hidden, or goes
    async with started(tmp_path / "data") as client:
        a, c = await project_of(client, "A"), await project_of(client, "C")
        record = await personal(client, a)
        assert await recalled(client, c) == ["I prefer answers about participant Alice"]
        await tighten(client, a, how)
        assert await recalled(client, c) == []
        if how == "lock":  # lifted, the project stays Local only; deleting it is what the finding names
            pass
        assert (await client.delete(f"/api/projects/{a}")).status_code == 200
        assert await recalled(client, c) == []
        assert await rows(client, "SELECT count(*) FROM memory_records WHERE id = ? AND scope = 'personal'", record) == [(0,)]


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


async def test_a_record_derived_from_a_related_record_follows_it_when_its_project_is_tightened(tmp_path):
    # 1: B's extraction was shown A's personal record; A turns Private; nothing derived from it reaches C
    async with started(tmp_path / "data") as client:
        a, b, c = await project_of(client, "A"), await project_of(client, "B"), await project_of(client, "C")
        source = await personal(client, a, "I prefer answers about participant Alice")
        run = await turn_in(client, b, "Write it as I prefer")

        def extract(conn):  # as _extraction_call registers what its request held, then the effect
            conn.execute("UPDATE runs SET inputs = json_set(inputs, '$.memory_ids', json(?)) WHERE id = ?",
                         (json.dumps([source]), run))
            return memory.apply_extraction(conn, run, [{"type": "preference", "content": "Write it as I prefer, naming Alice",
                                                        "status": "confirmed", "span": "write it as i prefer",
                                                        "replaces": ""}])
        await asyncio.to_thread(client.state["db"].write, extract)
        [(derived, status)] = await rows(client, "SELECT id, status FROM memory_records WHERE content LIKE 'Write it as I prefer%'")
        assert status == "proposed"  # "Alice" is not the researcher's word here: restated, so a proposal
        assert await asyncio.to_thread(client.state["db"].read, lambda conn: memory.allowed(conn, c, [derived])) is True
        await tighten(client, a, "private")
        assert await recalled(client, c) == []
        assert await asyncio.to_thread(client.state["db"].read, lambda conn: memory.allowed(conn, c, [derived])) is False


async def test_an_extraction_shown_a_record_since_tightened_writes_nothing(tmp_path):
    async with started(tmp_path / "data") as client:
        a, b = await project_of(client, "A"), await project_of(client, "B")
        source = await personal(client, a, "I prefer answers about participant Alice")
        run = await turn_in(client, b, "Write it as I prefer")
        await asyncio.to_thread(client.state["db"].write, lambda conn: conn.execute(
            "UPDATE runs SET inputs = json_set(inputs, '$.memory_ids', json(?)) WHERE id = ?", (json.dumps([source]), run)))
        await tighten(client, a, "private")  # while B's extraction runs
        assert await asyncio.to_thread(client.state["db"].write, lambda conn: memory.apply_extraction(conn, run, [
            {"type": "preference", "content": "Answers naming Alice", "status": "proposed", "span": "",
             "replaces": ""}])) is None


async def test_a_project_record_resting_on_a_turn_moved_to_a_stricter_project_is_never_sent(tmp_path):
    # 2: every source kind: a turn or run now in a stricter project invalidates the record
    async with started(tmp_path / "data") as client:
        normal, private = await project_of(client, "N"), await project_of(client, "P", level="private")
        moving, staying = await conversation_in(client, normal), await conversation_in(client, normal)
        await send(client, staying, "Remember: the sample is 30 nurses")
        await send(client, moving, "Correct: the sample is 40 nurses")  # its sources: both conversations' turns
        assert await client.post(f"/api/conversations/{moving}/move", json={"project_id": private})
        lines, ids = await asyncio.to_thread(client.state["db"].read, lambda conn: memory.project_state(conn, normal))
        assert not any("40 nurses" in line for line in lines)
        record = (await rows(client, "SELECT id FROM memory_records WHERE content = 'the sample is 40 nurses'"))[0][0]
        assert await asyncio.to_thread(client.state["db"].read, lambda conn: memory.allowed(conn, normal, [record])) is False


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


async def test_an_agent_note_written_after_recalling_a_personal_record_follows_its_project(tmp_path):
    # the note may restate what the request recalled: it follows the recalled record's project, as a proposal does
    async with started(tmp_path / "data") as client:
        a, b, c = await project_of(client, "A"), await project_of(client, "B"), await project_of(client, "C")
        source = await personal(client, a)
        run = await turn_in(client, b, "x")

        def noted(conn):
            turn = conn.execute("SELECT source_turn_id FROM runs WHERE id = ?", (run,)).fetchone()[0]
            conn.execute("UPDATE runs SET inputs = json_set(inputs, '$.memory_ids', json(?)) WHERE id = ?",
                         (json.dumps([source]), turn))
            return memory.note(conn, project_id=b, run_id=turn, kind="next_step", text="Ask Alice", materials=())
        note = await asyncio.to_thread(client.state["db"].write, noted)
        assert await asyncio.to_thread(client.state["db"].read, lambda conn: memory.allowed(conn, b, [note])) is True
        await tighten(client, a, "private")
        assert await asyncio.to_thread(client.state["db"].read, lambda conn: memory.allowed(conn, b, [note])) is False


async def test_an_earlier_decision_question_keeps_its_cannot_be_found_line_beside_unrelated_state(tmp_path):
    provider = MockProvider()
    async with started(tmp_path / "data", provider) as client:
        project = await project_of(client)
        await client.post("/api/memory", json={"project_id": project, "type": "project_state",
                                               "content": "Our research question is about wages"})
        await send(client, await conversation_in(client, project), "Which sampling method did we decide on last time?")
        assert memory.MISSING in provider.answers[-1]["messages"][0]["content"]
