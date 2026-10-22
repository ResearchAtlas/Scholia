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
