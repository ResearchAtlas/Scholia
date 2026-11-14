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


async def extraction_succeeded(client, turn, timeout=5.0):
    """Wait for the extraction of turn to succeed: within timeout, and failing at once on any other ending."""
    deadline = asyncio.get_running_loop().time() + timeout
    while asyncio.get_running_loop().time() < deadline:
        found = await rows(client, "SELECT status FROM runs WHERE workflow = 'memory' AND source_turn_id = ?", turn)
        if found and found[0][0] != "running":
            assert found[0][0] == "succeeded", found
            return
        await asyncio.sleep(0.02)
    raise AssertionError("the extraction did not end in time")


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


async def test_overlapping_extractions_the_later_turns_replaces_the_earlier_ones_when_the_earlier_finishes_first(
        tmp_path):
    # Section 14's opposite order with both extractions running at once (M2 audit area 4, item 3, astra F2): an earlier
    # extraction's own write is no edit of the researcher's, so the later turn's statement ends up active
    provider = MockProvider()
    earlier, later = asyncio.Event(), asyncio.Event()
    async with started(tmp_path / "data", provider) as client:
        project, conversation, record = await setup(client)
        reply(provider, says("The sample is 35 interviews", record), release=earlier)
        reply(provider, says("The sample is 40 interviews", record), release=later)
        first = await send(client, conversation, "The sample is 35 interviews")
        await wait_for(lambda: provider.extractions)
        await send(client, conversation, "The sample is 40 interviews")
        await wait_for(lambda: len(provider.extractions) == 2)
        earlier.set()
        await extraction_succeeded(client, first[0]["run_id"])
        later.set()
        await background_idle(client)
        assert await state(client) == {"the sample is 30 interviews": ("superseded", False, False),
                                       "The sample is 35 interviews": ("superseded", False, False),
                                       "The sample is 40 interviews": ("confirmed", False, False)}


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
        assert (await client.post("/api/memory/review", json={"id": pilot, "action": "forget", "version": 1})).json() == {"ok": True}
        assert await rows(client, "SELECT count(*) FROM memory_records WHERE id = ?", pilot) == [(0,)]
        assert (await client.get(f"/api/memory/review?project_id={project}")).json()["proposals"] == []


# M2 audit area 4, item 2 (astra F1): a restatement is a source of the record it restates, so deleting the conversation
# that first said it leaves the record while another turn says it; a confirmed restatement confirms an inferred record.
async def test_a_save_said_again_in_another_conversation_outlives_the_first_ones_deletion(tmp_path):
    async with started(tmp_path / "data") as client:
        project = await project_of(client)
        first, second = await conversation_in(client, project), await conversation_in(client, project)
        await send(client, first, "Remember: the sample is 30 interviews")
        stream = await send(client, second, "Remember: the sample is 30 interviews")
        assert {"type": "memory", "action": "saved", "count": 1} in stream
        [(record,)] = await rows(client, "SELECT id FROM memory_records WHERE type = 'project_state'")
        assert (await client.delete(f"/api/conversations/{first}")).status_code == 200
        assert await rows(client, "SELECT id, status FROM memory_records WHERE type = 'project_state'") == [
            (record, "confirmed")]
        assert await rows(client, "SELECT source_kind, source_id FROM memory_sources WHERE memory_id = ?", record) == [
            ("turn", stream[0]["run_id"])]


async def test_an_extractions_restatement_is_a_source_and_keeps_the_later_statement_newer(tmp_path):
    # astra's second probe: an earlier turn's extraction (35) finishing after a later restatement of 30 does not make 35
    # active: the restatement is the latest turn behind 30
    provider = MockProvider()
    release = asyncio.Event()
    async with started(tmp_path / "data", provider) as client:
        project, conversation, record = await setup(client)
        reply(provider, says("The sample is 35 interviews", record), release=release)
        reply(provider, candidate("The sample is 30 interviews", "confirmed", "project_state",
                                  span="the sample is 30 interviews"))
        await send(client, conversation, "The sample is 35 interviews")
        await wait_for(lambda: provider.extractions)
        restating = await send(client, conversation, "No, the sample is 30 interviews")
        await wait_for(lambda: len(provider.extractions) == 2)
        await extraction_succeeded(client, restating[0]["run_id"])  # the restatement's extraction finishes first
        assert ("turn", restating[0]["run_id"]) in await rows(
            client, "SELECT source_kind, source_id FROM memory_sources WHERE memory_id = ?", record)
        release.set()
        await background_idle(client)
        assert await state(client) == {"the sample is 30 interviews": ("confirmed", False, False),
                                       "The sample is 35 interviews": ("proposed", True, False)}


async def test_a_confirmed_restatement_confirms_an_inferred_record(tmp_path):
    from backend import memory
    provider = MockProvider()
    async with started(tmp_path / "data", provider) as client:
        project = await project_of(client)
        conversation = await conversation_in(client, project)
        await asyncio.to_thread(client.state["db"].write, lambda conn: memory.write(
            conn, project_id=project, scope="project", kind="project_state", content="The sample is 30 interviews",
            status="inferred", sources=[("project", project)]))
        reply(provider, candidate("The sample is 30 interviews", "confirmed", "project_state",
                                  span="the sample is 30 interviews"))
        stream = await send(client, conversation, "The sample is 30 interviews")
        await background_idle(client)
        assert sorted(await rows(client, "SELECT status, version FROM memory_records WHERE type = 'project_state'")) == [
            ("confirmed", 2), ("superseded", 1)]
        [(confirmed,)] = await rows(client, "SELECT id FROM memory_records WHERE status = 'confirmed'"
                                            " AND type = 'project_state'")
        assert ("turn", stream[0]["run_id"]) in await rows(
            client, "SELECT source_kind, source_id FROM memory_sources WHERE memory_id = ?", confirmed)


# Batch 1 (PR 62 recheck 1): what an extraction wrote is known from the record itself, and a researcher's change in
# place since the extraction started is seen along the whole chain. Driven at the record (memory.apply_extraction), so
# the order of starts, Keeps and finishes is exact.
def _turn_and_extraction(conn, project, conversation, text, start=True):
    """A succeeded turn saying text and, when start, its extraction run started now: (turn id, run id or None)."""
    import time
    from backend.db import new_id
    turn = new_id()
    time.sleep(0.005)  # each step after the last, by the clock the records keep
    conn.execute("INSERT INTO runs (id, project_id, conversation_id, kind, workflow, status, inputs) VALUES"
                 " (?, ?, ?, 'turn', 'agent', 'succeeded', '{}')", (turn, project, conversation))
    (seq,) = conn.execute("SELECT count(*) FROM turns WHERE conversation_id = ?", (conversation,)).fetchone()
    conn.execute("INSERT INTO turns (run_id, conversation_id, seq, author, user_message, answer) VALUES"
                 " (?, ?, ?, 'researcher', ?, '{\"text\": \"ok\"}')",
                 (turn, conversation, seq, json.dumps({"text": text})))
    return turn, (_extraction(conn, project, turn) if start else None)


def _extraction(conn, project, turn):
    import time
    from backend.db import new_id
    run = new_id()
    time.sleep(0.005)
    conn.execute("INSERT INTO runs (id, project_id, kind, workflow, source_turn_id, inputs) VALUES"
                 " (?, ?, 'background', 'memory', ?, '{}')", (run, project, turn))
    return run


def _says(text, replaces=""):
    return [{"type": "project_state", "content": text, "status": "confirmed", "span": text.lower(),
             "replaces": replaces}]


async def _in(client, fn):
    return await asyncio.to_thread(client.state["db"].write, fn)


@pytest.mark.parametrize("change", ["keep", "edit"])
async def test_a_researchers_change_in_place_after_an_extraction_started_is_seen_through_a_later_replacement(
        tmp_path, change):
    # both astra passes: a proposal kept or edited by the researcher after E40 started, then replaced on its own by
    # E35 (started after that change), was missed because the walk stopped at a version created before E40 started
    import time
    from backend import memory
    async with started(tmp_path / "data") as client:
        project = await project_of(client)
        conversation = await conversation_in(client, project)

        def steps(conn):
            pending = memory.write(conn, project_id=project, scope="project", kind="project_state",
                                   content="The sample is 30 interviews", status="proposed",
                                   sources=[("project", project)], structured={"extracted": True})
            t35, _ = _turn_and_extraction(conn, project, conversation, "The sample is 35 interviews", start=False)
            _, e40 = _turn_and_extraction(conn, project, conversation, "The sample is 40 interviews")
            time.sleep(0.005)
            if change == "keep":
                memory.keep(conn, pending, None)
            else:
                memory.edit(conn, pending, 1, content="The sample is 31 interviews")
            e35 = _extraction(conn, project, t35)
            time.sleep(0.005)
            memory.apply_extraction(conn, e35, _says("The sample is 35 interviews", pending))
            time.sleep(0.005)
            memory.apply_extraction(conn, e40, _says("The sample is 40 interviews", pending))
        await _in(client, steps)
        found = dict(await rows(client, "SELECT content, status FROM memory_records WHERE type = 'project_state'"))
        assert (found["The sample is 35 interviews"], found["The sample is 40 interviews"]) == ("confirmed", "proposed")


async def test_an_extractions_own_confirmation_of_a_proposal_does_not_block_the_later_statement(tmp_path):
    # astra pass 2: E30's restatement confirms the pending proposal (keep), which added a source "from the researcher"
    # and so read as the researcher's change; the later turn's 40 stayed proposed
    from backend import memory
    async with started(tmp_path / "data") as client:
        project = await project_of(client)
        conversation = await conversation_in(client, project)

        def steps(conn):
            pending = memory.write(conn, project_id=project, scope="project", kind="project_state",
                                   content="The sample is 30 interviews", status="proposed",
                                   sources=[("project", project)], structured={"extracted": True})
            _, e30 = _turn_and_extraction(conn, project, conversation, "The sample is 30 interviews")
            _, e40 = _turn_and_extraction(conn, project, conversation, "The sample is 40 interviews")
            memory.apply_extraction(conn, e30, _says("The sample is 30 interviews"))
            memory.apply_extraction(conn, e40, _says("The sample is 40 interviews", pending))
            return pending
        pending = await _in(client, steps)
        found = dict(await rows(client, "SELECT content, status FROM memory_records WHERE type = 'project_state'"))
        assert found == {"The sample is 30 interviews": "superseded", "The sample is 40 interviews": "confirmed"}
        assert ("researcher", "views") not in await rows(
            client, "SELECT source_kind, source_id FROM memory_sources WHERE memory_id = ?", pending)


async def test_an_extractions_replacement_stays_its_own_after_the_conversation_that_made_it_is_deleted(tmp_path):
    # astra pass 2: deleting the conversation whose extraction wrote 35 cleared the record's run, so a running turn
    # holding 30 was revoked at dispatch although nobody changed the record
    from backend import memory
    async with started(tmp_path / "data") as client:
        project = await project_of(client)
        first, second = await conversation_in(client, project), await conversation_in(client, project)

        def steps(conn):
            held = memory.write(conn, project_id=project, scope="project", kind="project_state",
                                content="The sample is 30 interviews", status="confirmed",
                                sources=[("project", project)])
            _, e35 = _turn_and_extraction(conn, project, first, "The sample is 35 interviews")
            memory.apply_extraction(conn, e35, _says("The sample is 35 interviews", held))
            _, again = _turn_and_extraction(conn, project, second, "The sample is 35 interviews")
            memory.apply_extraction(conn, again, _says("The sample is 35 interviews"))  # a source in `second` too
            return held
        held = await _in(client, steps)
        assert (await client.delete(f"/api/conversations/{first}")).status_code == 200
        assert await rows(client, "SELECT content, status FROM memory_records WHERE type = 'project_state'"
                                  " ORDER BY version") == [("The sample is 30 interviews", "superseded"),
                                                           ("The sample is 35 interviews", "confirmed")]
        assert await asyncio.to_thread(client.state["db"].read, lambda conn: memory.allowed(conn, project, [held]))


# Design review K, Option 2A (PR 62 batch 2): the views are a source only of what was kept in the views; a Save that
# confirms a pending proposal is the researcher's change (stamped) but goes with its last conversation (round 2, astra
# pass 1 F1; ticket 14, "deleted with its only source")
async def test_a_proposal_a_save_confirmed_goes_with_its_last_conversation(tmp_path):
    from backend import memory
    async with started(tmp_path / "data") as client:
        project = await project_of(client)
        first, second = await conversation_in(client, project), await conversation_in(client, project)

        def proposed(conn):
            _, run = _turn_and_extraction(conn, project, first, "We might use thirty interviews")
            memory.apply_extraction(conn, run, [{"type": "project_state", "content": "We use thirty interviews",
                                                 "status": "proposed", "span": "", "replaces": ""}])
        await _in(client, proposed)
        stream = await send(client, second, "Remember: we use thirty interviews")
        assert {"type": "memory", "action": "saved", "count": 1} in stream
        [(record, structured)] = await rows(client, "SELECT id, structured FROM memory_records"
                                                    " WHERE type = 'project_state'")
        assert json.loads(structured).get("researcher_at")  # the researcher's change
        assert ("researcher", "views") not in await rows(
            client, "SELECT source_kind, source_id FROM memory_sources WHERE memory_id = ?", record)
        for conversation in (first, second):
            assert (await client.delete(f"/api/conversations/{conversation}")).status_code == 200
        assert await rows(client, "SELECT count(*) FROM memory_records WHERE id = ?", record) == [(0,)]


async def test_a_proposal_kept_in_the_views_outlives_its_conversation(tmp_path):
    from backend import memory
    async with started(tmp_path / "data") as client:
        project = await project_of(client)
        conversation = await conversation_in(client, project)

        def proposed(conn):
            _, run = _turn_and_extraction(conn, project, conversation, "We might use thirty interviews")
            memory.apply_extraction(conn, run, [{"type": "project_state", "content": "We use thirty interviews",
                                                 "status": "proposed", "span": "", "replaces": ""}])
        await _in(client, proposed)
        [(record, version)] = await rows(client, "SELECT id, version FROM memory_records WHERE type = 'project_state'")
        kept = await client.post("/api/memory/review", json={"project_id": project, "id": record, "action": "keep",
                                                              "version": version})
        assert kept.status_code == 200, kept.text
        assert (await client.delete(f"/api/conversations/{conversation}")).status_code == 200
        assert await rows(client, "SELECT status FROM memory_records WHERE id = ?", record) == [("confirmed",)]
        assert await rows(client, "SELECT source_kind, source_id FROM memory_sources WHERE memory_id = ?", record) == [
            ("researcher", "views")]


# PR 62 batch 3 (round 3, both astra passes): raising a duplicate's authority is an automatic replacement too, so an
# older extraction finishing last neither confirms a newer inference nor keeps a newer proposal (section 8: its source
# turn must come after the latest turn behind the record); a newer restatement still does. Its turn is still a source.
def _says_as(text, status, span):
    return [{"type": "project_state", "content": text, "status": status, "span": span, "replaces": ""}]


@pytest.mark.parametrize("earlier, later, content, span, status", [
    # pass 1's order: the later turn's extraction infers the same words and finishes first
    ("We use thirty interviews", "It seems we use thirty interviews", "We use thirty interviews",
     "we use thirty interviews", "inferred"),
    # pass 2's order
    ("The sample is 30 interviews", "The sample is 30 interviews according to the draft", "The sample is 30 interviews",
     "the sample is 30 interviews", "inferred"),
    # pass 1's proposed variant: the later turn's extraction only proposes it (from the answer)
    ("We use thirty interviews", "What if we use thirty interviews", "We use thirty interviews", "", "proposed"),
])
async def test_an_older_extraction_finishing_last_never_raises_a_newer_duplicates_authority(
        tmp_path, earlier, later, content, span, status):
    from backend import memory
    async with started(tmp_path / "data") as client:
        project = await project_of(client)
        conversation = await conversation_in(client, project)

        def steps(conn):
            first, early = _turn_and_extraction(conn, project, conversation, earlier)
            _, late = _turn_and_extraction(conn, project, conversation, later)
            memory.apply_extraction(conn, late, _says_as(content, status, span))
            memory.apply_extraction(conn, early, _says_as(content, "confirmed", content.lower()))
            return first
        first = await _in(client, steps)
        assert await rows(client, "SELECT status, version FROM memory_records WHERE type = 'project_state'") == [
            (status, 1)]
        [(record,)] = await rows(client, "SELECT id FROM memory_records WHERE type = 'project_state'")
        assert ("turn", first) in await rows(  # the older restatement is still a source of it
            client, "SELECT source_kind, source_id FROM memory_sources WHERE memory_id = ?", record)


@pytest.mark.parametrize("status", ["inferred", "proposed"])
async def test_a_newer_extractions_restatement_still_confirms_an_earlier_duplicate(tmp_path, status):
    from backend import memory
    async with started(tmp_path / "data") as client:
        project = await project_of(client)
        conversation = await conversation_in(client, project)

        def steps(conn):
            _, early = _turn_and_extraction(conn, project, conversation, "It seems we use thirty interviews")
            memory.apply_extraction(conn, early, _says_as("We use thirty interviews", status,
                                                          "we use thirty interviews" if status == "inferred" else ""))
            _, late = _turn_and_extraction(conn, project, conversation, "We use thirty interviews")
            memory.apply_extraction(conn, late, _says_as("We use thirty interviews", "confirmed",
                                                         "we use thirty interviews"))
        await _in(client, steps)
        found = await rows(client, "SELECT status FROM memory_records WHERE type = 'project_state' ORDER BY version")
        assert found == ([("superseded",), ("confirmed",)] if status == "inferred" else [("confirmed",)])
