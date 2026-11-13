"""Explicit memory commands (S1-21a; slice-1 spec F10 Explicit requests, section 8 Words that count; checklist C1 to C4,
G1, G10, L5c, W1): recognized by rules in the researcher's own message and applied by code before any model call; a
bare "save this", "update this" or "forget this" asks first, and only the researcher's answer saves or forgets."""

import asyncio
import json

import pytest

from scholia_app import MockProvider, background_idle, send, started
from test_materials import project_of
from test_memory import conversation_in, extracted, candidate, records, rows

pytestmark = pytest.mark.asyncio


async def open_ask(client, conversation, timeout=5.0):
    deadline = asyncio.get_running_loop().time() + timeout
    while asyncio.get_running_loop().time() < deadline:
        found = (await client.get(f"/api/asks?conversation_id={conversation}")).json()["asks"]
        if found:
            return found[0]
        await asyncio.sleep(0.02)
    raise AssertionError("no ask")


@pytest.mark.parametrize("message, saved", [
    ("Remember: my method is semi-structured interviews", "my method is semi-structured interviews"),
    ("记住：我的方法是半结构化访谈", "我的方法是半结构化访谈"),
])
async def test_a_save_command_is_saved_confirmed_before_the_answer_and_the_answer_is_told(tmp_path, message, saved):
    provider = MockProvider()
    seen = []

    async def answering(body):  # the record is read back before the answer step's request is sent
        seen.append(await rows(client, "SELECT content, status FROM memory_records WHERE type = 'project_state'"))
        return provider.answer("Saved: noted.")
    provider.replies.append(answering)
    async with started(tmp_path / "data", provider) as client:
        project = await project_of(client)
        conversation = await conversation_in(client, project)
        stream = await send(client, conversation, message)
        assert {"type": "memory", "action": "saved", "count": 1} in stream
        assert seen == [[(saved, "confirmed")]]
        assert f"confirmed: {saved}" in provider.answers[0]["messages"][0]["content"]  # USER MEMORY tells the model
        [record] = [r for r in await records(client, project) if r["type"] != "progress"]
        assert (record["content"], record["status"], record["scope"]) == (saved, "confirmed", "project")
        assert record["sources"] == [{"kind": "turn", "id": stream[0]["run_id"]}]
        [turn] = (await client.get(f"/api/conversations/{conversation}")).json()["turns"]
        assert turn["memory"] == [{"action": "saved", "count": 1, "records": [
            {"id": record["id"], "type": "project_state", "status": "confirmed", "content": saved, "scope": "project"}]}]
        [(data,)] = await rows(client, "SELECT data FROM run_events WHERE type = 'memory_proposed' AND run_id = ?",
                               stream[0]["run_id"])
        assert saved not in data  # an event names records by id only


async def test_a_preference_is_personal_in_a_normal_project_and_the_projects_own_in_a_private_one(tmp_path):
    from backend import memory
    async with started(tmp_path / "data") as client:
        normal = await project_of(client)
        await send(client, await conversation_in(client, normal), "Remember: I prefer short answers")
        private = await project_of(client, "Private one", level="private")
        local = await project_of(client, "Local one", level="local_only")
        for project in (private, local):  # reading 4: nothing from them can reach another project's request
            await asyncio.to_thread(client.state["db"].write, lambda conn, project=project: memory.save_command(
                conn, project_id=project, run_id=None, action="save", text="I prefer tables"))
        assert await rows(client, "SELECT scope, project_id FROM memory_records WHERE type = 'preference'"
                                  " ORDER BY content, project_id") == sorted([("personal", normal)]) + sorted(
            [("project", private), ("project", local)], key=lambda r: r[1])


async def test_update_and_correct_replace_the_record_most_like_them_with_a_new_version(tmp_path):
    async with started(tmp_path / "data") as client:
        project = await project_of(client)
        conversation = await conversation_in(client, project)
        await send(client, conversation, "Remember: the sample is 30 interviews")
        await send(client, conversation, "Remember: I prefer short answers")
        stream = await send(client, conversation, "Correct: the sample is 40 interviews")
        assert {"type": "memory", "action": "updated", "count": 1} in stream
        found = await rows(client, "SELECT content, status, version FROM memory_records WHERE type = 'project_state'"
                                   " ORDER BY version")
        assert found == [("the sample is 30 interviews", "superseded", 1), ("the sample is 40 interviews", "confirmed", 2)]
        assert await rows(client, "SELECT status FROM memory_records WHERE type = 'preference'") == [("confirmed",)]


async def test_forget_with_clear_text_deletes_the_record_at_once_and_revokes_the_runs_holding_it(tmp_path):
    provider = MockProvider()
    release = asyncio.Event()

    async def held(body):
        await release.wait()
        return provider.answer('{"records": []}', cost=0)
    async with started(tmp_path / "data", provider) as client:
        project = await project_of(client)
        conversation = await conversation_in(client, project)
        await send(client, conversation, "Remember: the pilot used a survey")
        provider.memory_replies.append(held)  # the next extraction holds the record as related, while it waits
        await send(client, conversation, "Thanks")
        await asyncio.sleep(0.2)
        [(record,)] = await rows(client, "SELECT id FROM memory_records WHERE type != 'progress'")
        stream = await send(client, conversation, "Forget: the pilot used a survey")
        assert {"type": "memory", "action": "forgotten", "count": 1} in stream
        assert await rows(client, "SELECT count(*) FROM memory_records WHERE id = ?", record) == [(0,)]
        release.set()
        await background_idle(client)
        statuses = await rows(client, "SELECT status, cancel_reason FROM runs WHERE workflow = 'memory' ORDER BY started_at")
        assert ("cancelled", "revoked") in statuses or len(provider.extractions) < 3
        assert "the pilot used a survey" not in provider.answers[-1]["messages"][0]["content"]  # not recalled
        assert len(provider.extractions) == 1  # a command's turn is not extracted again: only "Thanks" was


async def test_a_bare_save_this_asks_and_only_the_researchers_answer_saves_the_earlier_answer(tmp_path):
    provider = MockProvider()
    provider.replies.append(provider.answer("Cohort studies follow people over time."))
    async with started(tmp_path / "data", provider) as client:
        project = await project_of(client)
        conversation = await conversation_in(client, project)
        await send(client, conversation, "What is a cohort study?")
        sending = asyncio.create_task(send(client, conversation, "save this"))
        ask = await open_ask(client, conversation)
        assert ask["kind"] == "memory_save" and ask["options"] == ["save", "not_now"] and ask["text_box"]
        assert ask["params"]["candidate"] == "Cohort studies follow people over time."  # shown, read now
        assert await rows(client, "SELECT count(*) FROM memory_records WHERE type != 'progress'") == [(0,)]  # nothing yet
        answered = await client.post(f"/api/runs/{ask['run_id']}/asks/{ask['ask_id']}", json={"option": "save"})
        assert answered.status_code == 200, answered.text
        stream = await sending
        assert {"type": "memory", "action": "saved", "count": 1} in stream
        [(content, status)] = await rows(client, "SELECT content, status FROM memory_records WHERE type != 'progress'")
        assert (content, status) == ("Cohort studies follow people over time.", "confirmed")


async def test_not_now_saves_nothing_and_a_typed_answer_saves_the_researchers_own_words(tmp_path):
    provider = MockProvider()
    async with started(tmp_path / "data", provider) as client:
        project = await project_of(client)
        conversation = await conversation_in(client, project)
        await send(client, conversation, "What is a cohort study?")
        sending = asyncio.create_task(send(client, conversation, "save this"))
        ask = await open_ask(client, conversation)
        await client.post(f"/api/runs/{ask['run_id']}/asks/{ask['ask_id']}", json={"option": "not_now"})
        assert {"type": "memory", "action": "declined", "count": 0} in await sending
        assert await rows(client, "SELECT count(*) FROM memory_records WHERE type != 'progress'") == [(0,)]
        sending = asyncio.create_task(send(client, conversation, "保存这个"))
        ask = await open_ask(client, conversation)
        await client.post(f"/api/runs/{ask['run_id']}/asks/{ask['ask_id']}", json={"text": "队列研究随访人群"})
        await sending
        assert await rows(client, "SELECT content, status FROM memory_records WHERE type != 'progress'") == [
            ("队列研究随访人群", "confirmed")]


async def test_a_bare_forget_this_asks_about_what_the_last_turn_saved_and_forgets_it_on_the_answer(tmp_path):
    async with started(tmp_path / "data") as client:
        project = await project_of(client)
        conversation = await conversation_in(client, project)
        await send(client, conversation, "Remember: we exclude studies before 2000")
        [(record,)] = await rows(client, "SELECT id FROM memory_records WHERE type != 'progress'")
        sending = asyncio.create_task(send(client, conversation, "forget this"))
        ask = await open_ask(client, conversation)
        assert ask["kind"] == "memory_forget" and not ask["text_box"]
        assert [r["id"] for r in ask["params"]["records"]] == [record]
        assert await rows(client, "SELECT count(*) FROM memory_records WHERE id = ?", record) == [(1,)]
        await client.post(f"/api/runs/{ask['run_id']}/asks/{ask['ask_id']}", json={"option": "forget"})
        assert {"type": "memory", "action": "forgotten", "count": 1} in await sending
        assert await rows(client, "SELECT count(*) FROM memory_records WHERE id = ?", record) == [(0,)]


async def test_a_bare_update_this_asks_for_the_new_text_and_replaces_the_record_it_named(tmp_path):
    async with started(tmp_path / "data") as client:
        project = await project_of(client)
        conversation = await conversation_in(client, project)
        await send(client, conversation, "Remember: the deadline is May")
        sending = asyncio.create_task(send(client, conversation, "update this"))
        ask = await open_ask(client, conversation)
        assert ask["kind"] == "memory_update" and ask["options"] == ["not_now"] and ask["text_box"]
        await client.post(f"/api/runs/{ask['run_id']}/asks/{ask['ask_id']}", json={"text": "The deadline is June"})
        await sending
        assert await rows(client, "SELECT content, status FROM memory_records WHERE type != 'progress' ORDER BY version") == [
            ("the deadline is May", "superseded"), ("The deadline is June", "confirmed")]


async def test_a_command_that_commits_keeps_its_record_when_the_answer_call_fails(tmp_path):
    provider = MockProvider()
    provider.replies.append((500, {"error": {"message": "boom"}}))
    provider.replies.append((500, {"error": {"message": "boom"}}))
    async with started(tmp_path / "data", provider) as client:
        project = await project_of(client)
        conversation = await conversation_in(client, project)
        stream = await send(client, conversation, "Remember: the ethics approval came in March")
        assert stream[-1]["status"] == "failed"
        [turn] = (await client.get(f"/api/conversations/{conversation}")).json()["turns"]
        assert turn["memory"][0]["records"][0]["content"] == "the ethics approval came in March"  # said from the record
        assert await rows(client, "SELECT status FROM memory_records WHERE type != 'progress'") == [("confirmed",)]


async def test_a_forget_naming_nothing_forgets_nothing_and_says_so(tmp_path):
    async with started(tmp_path / "data") as client:
        project = await project_of(client)
        stream = await send(client, await conversation_in(client, project), "Forget: the qualitative pilot")
        assert {"type": "memory", "action": "no_match", "count": 0} in stream


async def test_a_command_never_reaches_another_projects_records(tmp_path):
    async with started(tmp_path / "data") as client:
        first, second = await project_of(client, "One"), await project_of(client, "Two")
        await send(client, await conversation_in(client, first), "Remember: the sample is 30 interviews")
        stream = await send(client, await conversation_in(client, second), "Forget: the sample is 30 interviews")
        assert {"type": "memory", "action": "no_match", "count": 0} in stream  # G10
        assert await rows(client, "SELECT count(*) FROM memory_records WHERE project_id = ? AND type = 'project_state'",
                          first) == [(1,)]


async def test_a_correction_never_replaces_a_record_that_shares_only_common_words(tmp_path):
    from backend import memory
    async with started(tmp_path / "data") as client:
        project = await project_of(client)
        conversation = await conversation_in(client, project)
        await send(client, conversation, "Remember: the deadline is in May")
        await send(client, conversation, "Correct: the sample is 40 interviews")
        assert sorted(await rows(client, "SELECT content, status FROM memory_records WHERE type != 'progress'")) == [
            ("the deadline is in May", "confirmed"), ("the sample is 40 interviews", "confirmed")]
        await send(client, conversation, "Remember: the deadline is in May")  # sent again: kept once
        assert await rows(client, "SELECT count(*) FROM memory_records WHERE content = 'the deadline is in May'") == [(1,)]
    assert memory.likeness("the deadline is in May", "the sample is 40 interviews") == 0.0


async def test_nothing_of_a_private_project_becomes_or_changes_personal_memory(tmp_path):
    # review 1 (P1): an update from a Private project never rewrites a personal record; the views refuse a personal
    # record there; a personal record whose turn moved to a stricter project stays personal (fix batch 3, Harold's
    # decision of 2026-10-23: a record's scope and validity are fixed when it is written)
    from backend import memory
    async with started(tmp_path / "data") as client:
        normal = await project_of(client)
        conversation = await conversation_in(client, normal)
        await send(client, conversation, "Remember: I prefer short answers")
        private = await project_of(client, "Private one", level="private")
        await asyncio.to_thread(client.state["db"].write, lambda conn: memory.save_command(
            conn, project_id=private, run_id=None, action="update", text="I prefer short answers about Alice"))
        assert sorted(await rows(client, "SELECT content, scope, status FROM memory_records WHERE type = 'preference'")) == [
            ("I prefer short answers", "personal", "confirmed"), ("I prefer short answers about Alice", "project", "confirmed")]
        refused = await client.post("/api/memory", json={"project_id": private, "type": "preference", "scope": "personal",
                                                          "content": "Alice prefers tables"})
        assert refused.status_code == 400 and refused.json()["code"] == "personal_from_strict"
        other = await project_of(client, "Other")
        recalled = lambda: asyncio.to_thread(client.state["db"].read, lambda conn: [  # noqa: E731
            r["content"] for r in memory.recall_candidates(conn, other)])
        assert await recalled() == ["I prefer short answers"]
        await client.post(f"/api/conversations/{conversation}/move", json={"project_id": private})
        assert await recalled() == ["I prefer short answers"]  # never retracted


async def test_saving_what_was_only_inferred_confirms_it(tmp_path):
    from backend import memory
    async with started(tmp_path / "data") as client:
        project = await project_of(client)
        await asyncio.to_thread(client.state["db"].write, lambda conn: memory.write(
            conn, project_id=project, scope="personal", kind="preference", content="I prefer short answers",
            status="inferred", sources=[("project", project)]))
        stream = await send(client, await conversation_in(client, project), "Remember: I prefer short answers")
        assert {"type": "memory", "action": "saved", "count": 1} in stream
        assert await rows(client, "SELECT status FROM memory_records WHERE content = 'I prefer short answers'"
                                  " ORDER BY version") == [("superseded",), ("confirmed",)]


async def test_an_update_that_matches_nothing_says_saved(tmp_path):
    async with started(tmp_path / "data") as client:
        project = await project_of(client)
        stream = await send(client, await conversation_in(client, project), "Update: the deadline is in June")
        assert {"type": "memory", "action": "saved", "count": 1} in stream


async def test_forget_can_name_a_pending_proposal(tmp_path):
    from backend import memory
    async with started(tmp_path / "data") as client:
        project = await project_of(client)
        proposal = await asyncio.to_thread(client.state["db"].write, lambda conn: memory.write(
            conn, project_id=project, scope="project", kind="decision", content="Maybe a survey of managers",
            status="proposed", sources=[("project", project)]))
        stream = await send(client, await conversation_in(client, project), "Forget: maybe a survey of managers")
        assert {"type": "memory", "action": "forgotten", "count": 1} in stream
        assert await rows(client, "SELECT count(*) FROM memory_records WHERE id = ?", proposal) == [(0,)]



async def test_saving_what_is_pending_keeps_the_proposal(tmp_path):
    from backend import memory
    async with started(tmp_path / "data") as client:
        project = await project_of(client)
        proposal = await asyncio.to_thread(client.state["db"].write, lambda conn: memory.write(
            conn, project_id=project, scope="project", kind="decision", content="We run a survey of managers",
            status="proposed", sources=[("project", project)]))
        await send(client, await conversation_in(client, project), "Remember: we run a survey of managers")
        assert await rows(client, "SELECT id, status FROM memory_records WHERE type = 'decision'") == [(proposal, "confirmed")]


async def test_a_forget_answered_after_its_record_changed_forgets_nothing_and_says_so(tmp_path):
    # S1-21a follow-up MB9: the forget acts on the records as they were when it was decided (their version and status,
    # checked in the deletion's own transaction); one edited meanwhile is not forgotten, and the model is told so
    provider = MockProvider()
    async with started(tmp_path / "data", provider) as client:
        project = await project_of(client)
        conversation = await conversation_in(client, project)
        await send(client, conversation, "Remember: we exclude studies before 2000")
        [(record,)] = await rows(client, "SELECT id FROM memory_records WHERE type != 'progress'")
        sending = asyncio.create_task(send(client, conversation, "forget this"))
        ask = await open_ask(client, conversation)
        edited = await client.patch(f"/api/memory/{record}", json={"version": 1, "content": "we exclude studies before 1990"})
        assert edited.status_code == 200
        await client.post(f"/api/runs/{ask['run_id']}/asks/{ask['ask_id']}", json={"option": "forget"})
        assert {"type": "memory", "action": "forgotten", "count": 0} in await sending
        assert sorted(await rows(client, "SELECT content, status FROM memory_records WHERE type != 'progress'")) == [
            ("we exclude studies before 1990", "confirmed"), ("we exclude studies before 2000", "superseded")]
        system = provider.answers[-1]["messages"][0]["content"]
        assert "changed since" in system


async def test_a_forget_with_text_finds_the_agents_note_too(tmp_path):
    # S1-21a follow-up MB3: forgetting by its words reaches every record the views can forget, notes and progress too
    from backend import memory
    async with started(tmp_path / "data") as client:
        project = await project_of(client)
        conversation = await conversation_in(client, project)
        await send(client, conversation, "Hello")
        [(turn,)] = await rows(client, "SELECT run_id FROM turns LIMIT 1")
        await asyncio.to_thread(client.state["db"].write, lambda conn: memory.note(
            conn, project_id=project, run_id=turn, kind="next_step", text="Ask Alice about the coding scheme",
            materials=()))
        stream = await send(client, conversation, "Forget: Ask Alice about the coding scheme")
        assert {"type": "memory", "action": "forgotten", "count": 1} in stream
        assert await rows(client, "SELECT count(*) FROM memory_records WHERE type = 'agent_note'") == [(0,)]


async def test_a_forget_answered_after_its_record_moved_to_another_project_leaves_it(tmp_path):
    # PR 51 recheck 2 (P1 1): a record whose conversation moved to another project since the ask was raised is that
    # project's now; the Forget asked in this project never deletes it
    async with started(tmp_path / "data") as client:
        project = await project_of(client)
        private = await project_of(client, "Private one", level="private")
        moving, other, asking = (await conversation_in(client, project), await conversation_in(client, project),
                                 await conversation_in(client, project))
        await send(client, moving, "Remember: we exclude studies before 2000")
        await send(client, other, "Remember: we exclude studies after 2020")
        sending = asyncio.create_task(send(client, asking, "Forget: we exclude studies"))
        ask = await open_ask(client, asking)
        assert len(ask["params"]["records"]) == 2
        assert (await client.post(f"/api/conversations/{moving}/move", json={"project_id": private})).status_code == 200
        await client.post(f"/api/runs/{ask['run_id']}/asks/{ask['ask_id']}", json={"option": "forget"})
        assert {"type": "memory", "action": "forgotten", "count": 1} in await sending
        assert await rows(client, "SELECT content, project_id FROM memory_records WHERE type != 'progress'") == [
            ("we exclude studies before 2000", private)]


async def test_a_forget_stopped_once_its_deletion_committed_still_records_and_says_what_it_forgot(tmp_path, monkeypatch):
    # PR 52's follow-up, item 9 (design review P's separate finding): Stop landed while the forget's deletion ran in its
    # worker. The deletion committed, but the loop was cut there: no memory_proposed event and no stream event, the turn
    # read cancelled with nothing said of the record it deleted. The deletions and their record run to their end, as a
    # tool's result does (Harness._tool's keeping), and then the Stop is honored.
    import time
    from backend.db import deletion
    real = deletion.delete
    async with started(tmp_path / "data") as client:
        harness, loop = client.state["harness"], asyncio.get_running_loop()
        project = await project_of(client)
        conversation = await conversation_in(client, project)
        await send(client, conversation, "Remember: the pilot used a survey")
        await background_idle(client)
        [(record,)] = await rows(client, "SELECT id FROM memory_records WHERE type != 'progress'")

        def deleting(*args, **kwargs):
            found = real(*args, **kwargs)  # committed: the Stop lands while its worker is still to return
            loop.call_soon_threadsafe(harness._request_cancel, harness.registry.turns[conversation], "researcher")
            time.sleep(0.2)
            return found
        monkeypatch.setattr(deletion, "delete", deleting)
        stream = await send(client, conversation, "Forget: the pilot used a survey")
        assert stream[-1]["status"] == "cancelled"
        assert await rows(client, "SELECT count(*) FROM memory_records WHERE id = ?", record) == [(0,)]
        [(data,)] = await rows(client, "SELECT data FROM run_events WHERE type = 'memory_proposed' AND run_id = ?",
                               stream[0]["run_id"])
        assert (json.loads(data)["action"], json.loads(data)["count"]) == ("forgotten", 1)
        assert {"type": "memory", "action": "forgotten", "count": 1} in stream
        [turn] = (await client.get(f"/api/conversations/{conversation}")).json()["turns"][-1:]
        assert [(m["action"], m["count"]) for m in turn["memory"]] == [("forgotten", 1)]



async def test_a_stop_during_a_forget_of_several_records_deletes_none_after_it_and_says_what_it_forgot(tmp_path,
                                                                                                     monkeypatch):
    # The internal review's F4: Stop is immediate (SPEC:412). The deletion it landed on commits, and the forget records
    # and says that one; the records still to be deleted are kept, as they were before the follow-up.
    import time
    from backend import memory
    from backend.db import deletion
    real = deletion.delete
    async with started(tmp_path / "data") as client:
        harness, loop = client.state["harness"], asyncio.get_running_loop()
        project = await project_of(client)
        conversation = await conversation_in(client, project)
        first = await send(client, conversation, "What do I study?")
        await background_idle(client)
        await asyncio.to_thread(client.state["db"].write, lambda conn: [memory.save_command(
            conn, project_id=project, run_id=first[0]["run_id"], action="save", text=text)
            for text in ("the pilot used a survey", "the cohort is 40 nurses", "interviews ran in May")])
        made = await rows(client, "SELECT id FROM memory_records WHERE type != 'progress'")
        assert len(made) == 3
        deleted = []

        def deleting(*args, **kwargs):
            found = real(*args, **kwargs)
            deleted.append(args[3])
            if len(deleted) == 1:  # committed: the Stop lands while its worker is still to return
                loop.call_soon_threadsafe(harness._request_cancel, harness.registry.turns[conversation], "researcher")
                time.sleep(0.2)
            return found
        monkeypatch.setattr(deletion, "delete", deleting)
        sending = asyncio.create_task(send(client, conversation, "forget this"))
        ask = await open_ask(client, conversation)
        assert len(ask["params"]["records"]) == 3
        await client.post(f"/api/runs/{ask['run_id']}/asks/{ask['ask_id']}", json={"option": "forget"})
        stream = await sending
        assert stream[-1]["status"] == "cancelled" and len(deleted) == 1
        assert len(await rows(client, "SELECT id FROM memory_records WHERE type != 'progress'")) == 2
        [(data,)] = await rows(client, "SELECT data FROM run_events WHERE type = 'memory_proposed' AND run_id = ?",
                               stream[0]["run_id"])
        assert (json.loads(data)["action"], json.loads(data)["count"]) == ("forgotten", 1)
        assert {"type": "memory", "action": "forgotten", "count": 1} in stream


# Fix batch 1, B-F2 (item 9; SPEC:650, run events before the final state): once a forget's deletion committed, the turn's
# record and its `memory` event say what it forgot, also when a later deletion fails (the count recorded, then the turn
# fails) and when the run is revoked after a deletion committed. No deletion starts after Stop or a revocation (SPEC:412).


async def _three_to_forget(client, monkeypatch, deleting):
    """A conversation with three saved records and deletion.delete replaced by deleting(real, *args, **kwargs); its
    forget sent ("forget this") and answered Forget: (the send's task, the project, the conversation)."""
    from backend import memory
    from backend.db import deletion
    real = deletion.delete
    project = await project_of(client)
    conversation = await conversation_in(client, project)
    first = await send(client, conversation, "What do I study?")
    await background_idle(client)
    await asyncio.to_thread(client.state["db"].write, lambda conn: [memory.save_command(
        conn, project_id=project, run_id=first[0]["run_id"], action="save", text=text)
        for text in ("the pilot used a survey", "the cohort is 40 nurses", "interviews ran in May")])
    monkeypatch.setattr(deletion, "delete", lambda *args, **kwargs: deleting(real, *args, **kwargs))
    sending = asyncio.create_task(send(client, conversation, "forget this"))
    ask = await open_ask(client, conversation)
    assert len(ask["params"]["records"]) == 3
    await client.post(f"/api/runs/{ask['run_id']}/asks/{ask['ask_id']}", json={"option": "forget"})
    return sending, project, conversation


async def _forgotten(client, stream, count):
    [(data,)] = await rows(client, "SELECT data FROM run_events WHERE type = 'memory_proposed' AND run_id = ?",
                           stream[0]["run_id"])
    assert (json.loads(data)["action"], json.loads(data)["count"]) == ("forgotten", count)
    assert {"type": "memory", "action": "forgotten", "count": count} in stream


@pytest.mark.parametrize("after_commit", [False, True])
async def test_a_forget_whose_later_deletion_fails_records_what_it_forgot_and_then_fails(tmp_path, monkeypatch,
                                                                                        after_commit):
    # The first review's B-F2, first probe: the second deletion fails before its transaction (or after its commit, so
    # its record is gone too); nothing was recorded or said of the one already forgotten, and the turn failed
    calls = []

    def deleting(real, *args, **kwargs):
        calls.append(args[3])
        if len(calls) == 2:
            if after_commit:
                real(*args, **kwargs)
            raise RuntimeError("the deletion failed")
        return real(*args, **kwargs)
    async with started(tmp_path / "data") as client:
        sending, _, _ = await _three_to_forget(client, monkeypatch, deleting)
        stream = await sending
        assert stream[-1]["status"] == "failed" and len(calls) == 2
        left = 1 if after_commit else 2
        assert len(await rows(client, "SELECT id FROM memory_records WHERE type != 'progress'")) == left
        await _forgotten(client, stream, 3 - left)


async def test_a_forget_revoked_once_its_deletion_committed_records_what_it_forgot_and_deletes_no_more(tmp_path,
                                                                                                    monkeypatch):
    # The first review's B-F2, second probe: the project is made Private through its endpoint while the first deletion's
    # worker is still to return; the run ends revoked, and its record said memory: []
    import threading
    reached, go, calls = threading.Event(), threading.Event(), []

    def deleting(real, *args, **kwargs):
        found = real(*args, **kwargs)
        calls.append(args[3])
        if len(calls) == 1:  # committed: the project changes while its worker is still to return
            reached.set()
            go.wait(10)
        return found
    async with started(tmp_path / "data") as client:
        sending, project, conversation = await _three_to_forget(client, monkeypatch, deleting)
        assert await asyncio.to_thread(reached.wait, 10)
        assert (await client.post(f"/api/projects/{project}/sensitivity", json={"level": "private"})).status_code == 200
        go.set()
        stream = await sending
        assert (stream[-1]["status"], stream[-1].get("cancel_reason")) == ("cancelled", "revoked") and len(calls) == 1
        assert len(await rows(client, "SELECT id FROM memory_records WHERE type != 'progress'")) == 2
        await _forgotten(client, stream, 1)
        [turn] = (await client.get(f"/api/conversations/{conversation}")).json()["turns"][-1:]
        assert [(m["action"], m["count"]) for m in turn["memory"]] == [("forgotten", 1)]


@pytest.mark.parametrize("change", ["private", "review lock", "ended"])
async def test_a_forget_revoked_in_the_record_before_the_turn_hears_of_it_deletes_no_more(tmp_path, monkeypatch, change):
    # Cycle 2's B-R2-F1: the policy change commits the run's revocation, and the harness is told only after its write
    # returns. In that moment the loop saw no cancellation and the deletion's own transaction did not read the run, so
    # every record left was deleted. Now each deletion reads its run there: one revoked (or ended in the record, here
    # written directly) deletes nothing more.
    import threading
    reached, go, calls, held = threading.Event(), threading.Event(), [], []

    def deleting(real, *args, **kwargs):
        found = real(*args, **kwargs)
        calls.append(args[3])  # committed
        if len(calls) == 1:  # the project changes (or the conversation goes) while its worker is still to return
            reached.set()
            go.wait(10)
        return found
    async with started(tmp_path / "data") as client:
        harness = client.state["harness"]
        real_revoke = harness.revoke

        def revoke(run_ids):  # the policy change's notification held: committed, not yet told to the harness
            if run_ids and not go.is_set():
                held.append(list(run_ids))
                return
            real_revoke(run_ids)
        monkeypatch.setattr(harness, "revoke", revoke)
        sending, project, conversation = await _three_to_forget(client, monkeypatch, deleting)
        assert await asyncio.to_thread(reached.wait, 10)
        if change == "ended":
            run_id = harness.registry.turns[conversation].run_id
            await asyncio.to_thread(client.state["db"].write, lambda conn: conn.execute(
                "UPDATE runs SET status = 'cancelled', cancel_reason = 'researcher' WHERE id = ?", (run_id,)))
            ids = []
        else:
            path, body = ((f"/api/projects/{project}/review-lock", {"locked": True}) if change == "review lock" else
                          (f"/api/projects/{project}/sensitivity", {"level": "private"}))
            assert (await client.post(path, json=body)).status_code == 200
            [ids] = held
            assert await rows(client, "SELECT cancel_reason FROM runs WHERE id IN (%s)" % ",".join("?" * len(ids)),
                              *ids) == [("revoked",)] * len(ids)
        go.set()
        try:
            stream = await asyncio.wait_for(sending, 20)
        finally:
            real_revoke(ids)
        assert len(calls) == 1
        assert len(await rows(client, "SELECT id FROM memory_records WHERE type != 'progress'")) == 2
        if change == "ended":  # nothing is recorded on a run that has ended
            return
        assert (stream[-1]["status"], stream[-1].get("cancel_reason")) == ("cancelled", "revoked")
        await _forgotten(client, stream, 1)
        [turn] = (await client.get(f"/api/conversations/{conversation}")).json()["turns"][-1:]
        assert [(m["action"], m["count"]) for m in turn["memory"]] == [("forgotten", 1)]
