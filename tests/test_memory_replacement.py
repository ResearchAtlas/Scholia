"""Replacing a record (S1-21a; slice-1 spec section 8, Replacing a record and Late extractions; section 14's memory
orderings; checklist L6, L7, X7, C4, G4): an extraction replaces an active record only when it is at least as
authoritative, its source turn came after the latest turn behind the record as it stands when the extraction finishes,
and the researcher has not changed the record since; else it waits as a proposal naming the record (older when its turn
came first), which Keep confirms only at the version it saw. Finish order is controlled by holding recorded replies."""

import asyncio
import json

import pytest

from scholia_app import MockProvider, background_idle, send, started
from test_detached_tail import wait_for
from test_materials import project_of
from test_memory import candidate, conversation_in, rows

pytestmark = pytest.mark.asyncio


def reply(provider, *records, release=None):
    async def answer(body):
        if release is not None:
            await release.wait()
        return provider.answer(json.dumps({"records": list(records)}), cost=0.001)
    provider.memory_replies.append(answer)


async def state(client):
    """{content: (status, older, out of date)} of the project_state records."""
    found = await rows(client, "SELECT content, status, json_extract(structured, '$.older'),"
                               " json_extract(structured, '$.out_of_date') FROM memory_records WHERE type = 'project_state'")
    return {content: (status, bool(older), bool(stale)) for content, status, older, stale in found}


async def setup(client):
    project = await project_of(client)
    conversation = await conversation_in(client, project)
    await send(client, conversation, "Remember: the sample is 30 interviews")  # a command: no extraction of its own
    [(record,)] = await rows(client, "SELECT id FROM memory_records WHERE type = 'project_state'")
    return project, conversation, record


def says(text, record, status="confirmed"):
    return candidate(text, status, "project_state", span=text.lower(), replaces=record)


async def proposal(client, content):
    [(record, version)] = await rows(client, "SELECT id, version FROM memory_records WHERE content = ?", content)
    return record, version


async def test_the_later_turns_extraction_replaces_the_earlier_ones_when_it_finishes_last(tmp_path):
    provider = MockProvider()
    async with started(tmp_path / "data", provider) as client:
        project, conversation, record = await setup(client)
        reply(provider, says("The sample is 35 interviews", record))
        await send(client, conversation, "The sample is 35 interviews")
        await background_idle(client)
        [(second,)] = await rows(client, "SELECT id FROM memory_records WHERE status = 'confirmed' AND type = 'project_state'")
        reply(provider, says("The sample is 40 interviews", second))
        await send(client, conversation, "The sample is 40 interviews")
        await background_idle(client)
        assert await state(client) == {"the sample is 30 interviews": ("superseded", False, False),
                                       "The sample is 35 interviews": ("superseded", False, False),
                                       "The sample is 40 interviews": ("confirmed", False, False)}
        assert await rows(client, "SELECT version FROM memory_records WHERE content = 'The sample is 40 interviews'") == [(3,)]


async def test_an_earlier_turns_extraction_finishing_last_waits_as_a_proposal_marked_older(tmp_path):
    provider = MockProvider()
    release = asyncio.Event()
    async with started(tmp_path / "data", provider) as client:
        project, conversation, record = await setup(client)
        reply(provider, says("The sample is 35 interviews", record), release=release)
        reply(provider, says("The sample is 40 interviews", record))
        await send(client, conversation, "The sample is 35 interviews")
        await wait_for(lambda: provider.extractions)
        await send(client, conversation, "The sample is 40 interviews")
        await wait_for(lambda: len(provider.extractions) == 2)
        await asyncio.sleep(0.2)
        release.set()
        await background_idle(client)
        assert await state(client) == {"the sample is 30 interviews": ("superseded", False, False),
                                       "The sample is 40 interviews": ("confirmed", False, False),
                                       "The sample is 35 interviews": ("proposed", True, False)}
        # Things to confirm lists it with the record it would replace; the researcher may confirm it
        review = (await client.get(f"/api/memory/review?project_id={project}")).json()
        [shown] = review["proposals"]
        assert shown["structured"]["older"] is True and shown["replaces"]["content"] == "The sample is 40 interviews"
        kept = await client.post("/api/memory/review", json={"id": shown["id"], "action": "keep", "version": shown["version"]})
        assert kept.status_code == 200, kept.text
        assert (await state(client))["The sample is 35 interviews"] == ("confirmed", False, False)
        assert (await state(client))["The sample is 40 interviews"][0] == "superseded"


@pytest.mark.parametrize("later", ["Correct: the sample is 50 interviews", "Forget: the sample is 30 interviews"])
async def test_an_extraction_finishing_after_a_later_command_stores_only_a_proposal_and_restores_nothing(tmp_path, later):
    provider = MockProvider()
    release = asyncio.Event()
    async with started(tmp_path / "data", provider) as client:
        project, conversation, record = await setup(client)
        reply(provider, says("The sample is 35 interviews", record), release=release)
        await send(client, conversation, "The sample is 35 interviews")
        await wait_for(lambda: provider.extractions)
        await send(client, conversation, later)
        release.set()
        await background_idle(client)
        found = await state(client)
        # a correction leaves it a proposal; a forget of a record its request held revokes it, writing nothing
        assert found.get("The sample is 35 interviews", ("proposed",))[0] == "proposed"
        active = {content for content, (status, _, _) in found.items() if status == "confirmed"}
        assert active == ({"the sample is 50 interviews"} if later.startswith("Correct") else set())


async def test_a_proposal_conflicting_with_a_confirmed_record_is_rejected_then_accepted_when_kept(tmp_path):
    provider = MockProvider()
    async with started(tmp_path / "data", provider) as client:
        project, conversation, record = await setup(client)
        reply(provider, says("The sample may be 45 interviews", record, "inferred"))
        await send(client, conversation, "The sample may be 45 interviews")
        await background_idle(client)
        assert await state(client) == {"the sample is 30 interviews": ("confirmed", False, False),
                                       "The sample may be 45 interviews": ("proposed", False, False)}
        pending, version = await proposal(client, "The sample may be 45 interviews")
        kept = await client.post("/api/memory/review", json={"id": pending, "action": "keep", "version": version})
        assert kept.status_code == 200 and kept.json()["status"] == "confirmed"
        assert await state(client) == {"the sample is 30 interviews": ("superseded", False, False),
                                       "The sample may be 45 interviews": ("confirmed", False, False)}


async def test_a_record_the_researcher_edits_while_an_extraction_runs_is_not_replaced(tmp_path):
    # L7: and confirming the proposal after a second edit returns it to Things to confirm, out of date
    provider = MockProvider()
    release = asyncio.Event()
    async with started(tmp_path / "data", provider) as client:
        project, conversation, record = await setup(client)
        reply(provider, says("The sample is 35 interviews", record), release=release)
        await send(client, conversation, "The sample is 35 interviews")
        await wait_for(lambda: provider.extractions)
        edited = (await client.patch(f"/api/memory/{record}", json={"version": 1, "content": "The sample is 32"})).json()
        release.set()
        await background_idle(client)
        found = await state(client)
        assert found["The sample is 32"][0] == "confirmed" and found["The sample is 35 interviews"][0] == "proposed"
        pending, version = await proposal(client, "The sample is 35 interviews")
        assert await rows(client, "SELECT supersedes_id FROM memory_records WHERE id = ?", pending) == [(edited["id"],)]
        await client.patch(f"/api/memory/{edited['id']}", json={"version": edited["version"], "content": "The sample is 33"})
        stale = await client.post("/api/memory/review", json={"id": pending, "action": "keep", "version": version})
        assert stale.status_code == 409 and stale.json()["code"] == "out_of_date"
        assert (await state(client))["The sample is 35 interviews"] == ("proposed", False, True)
        assert (await state(client))["The sample is 33"][0] == "confirmed"


async def test_a_duplicate_is_not_stored_twice(tmp_path):
    provider = MockProvider()
    async with started(tmp_path / "data", provider) as client:
        project, conversation, record = await setup(client)
        reply(provider, candidate("The sample is 30 interviews", "confirmed", "project_state",
                                  span="the sample is 30 interviews"))
        await send(client, conversation, "As I said, the sample is 30 interviews")
        await background_idle(client)
        assert await state(client) == {"the sample is 30 interviews": ("confirmed", False, False)}
        [(data,)] = await rows(client, "SELECT data FROM run_events WHERE type = 'memory_proposed' AND run_id IN"
                                       " (SELECT id FROM runs WHERE workflow = 'memory')")
        assert json.loads(data)["counts"]["duplicates"] == 1


async def test_edit_and_forget_in_things_to_confirm(tmp_path):
    provider = MockProvider()
    async with started(tmp_path / "data", provider) as client:
        project, conversation, record = await setup(client)
        reply(provider, says("Maybe a survey", record, "proposed"), candidate("A pilot in May", "proposed", "event"))
        await send(client, conversation, "I am thinking about a survey and a pilot")
        await background_idle(client)
        survey, version = await proposal(client, "Maybe a survey")
        edited = await client.post("/api/memory/review", json={"id": survey, "action": "edit", "version": version,
                                                                "content": "A survey of 200 people"})
        assert edited.status_code == 200 and edited.json()["content"] == "A survey of 200 people"
        assert (await state(client))["A survey of 200 people"] == ("confirmed", False, False)
        pilot, _ = await proposal(client, "A pilot in May")
        assert (await client.post("/api/memory/review", json={"id": pilot, "action": "forget"})).json() == {"ok": True}
        assert await rows(client, "SELECT count(*) FROM memory_records WHERE id = ?", pilot) == [(0,)]
        assert (await client.get(f"/api/memory/review?project_id={project}")).json()["proposals"] == []
