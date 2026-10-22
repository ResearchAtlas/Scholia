"""Recall into the request (S1-21a; slice-1 spec sections 7.3, 8 and 13; ticket 31; checklist R1, R2, R4, R5, R6, P1,
G5): PROJECT STATE and USER MEMORY by the route, at most 10 records within their budgets, the nearest in meaning first
when the search model answers in time (vec_memory), else the newest, said in the trace; never a superseded, expired,
deleted or unconfirmed personal record, nor another project's. Checked on the final serialized requests."""

import asyncio
import json

import pytest

from backend import memory
from scholia_app import MockProvider, background_idle, send, started
from test_materials import project_of
from test_memory import conversation_in, rows, write

pytestmark = pytest.mark.asyncio


def system(body):
    return body["messages"][0]["content"]


def section(body, label):
    text = system(body)
    if f"## {label}\n" not in text:
        return []
    return text.split(f"## {label}\n", 1)[1].split("\n\n## ", 1)[0].split("\n\n")


async def entered(client, project, content, kind="decision", scope="project"):
    response = await client.post("/api/memory", json={"project_id": project, "type": kind, "content": content,
                                                      "scope": scope})
    assert response.status_code == 201, response.text
    return response.json()


async def test_user_memory_holds_at_most_ten_records_newest_first_and_never_a_superseded_or_expired_one(tmp_path):
    provider = MockProvider()
    async with started(tmp_path / "data", provider) as client:
        project = await project_of(client)
        made = [await entered(client, project, f"Decision number {i}") for i in range(12)]
        old = made[0]
        await client.patch(f"/api/memory/{made[11]['id']}", json={"version": 1, "content": "Decision eleven, revised"})
        await write(client, lambda conn: conn.execute(  # G5: past its validity
            "UPDATE memory_records SET valid_to = '2000-01-01T00:00:00.000Z' WHERE id = ?", (made[10]["id"],)))
        stream = await send(client, await conversation_in(client, project), "What did we decide last time?")
        lines = section(provider.answers[-1], "USER MEMORY")
        assert len(lines) == 10 and lines[0] == "- [decision] Decision eleven, revised"
        assert not any("Decision number 11" in line or "Decision number 10" in line for line in lines)
        assert "Decision number 0" not in "\n".join(lines) and old["content"] == "Decision number 0"  # the oldest left out
        [(trace,)] = await rows(client, "SELECT data FROM run_events WHERE run_id = ? AND type = 'retrieval_trace'",
                                stream[0]["run_id"])
        trace = json.loads(trace)["memory"]
        assert trace["mode"] == "recency" and len(trace["records"]) == 10  # ids only, never their text
        assert "Decision" not in json.dumps(trace)
        [(held,)] = await rows(client, "SELECT json_extract(inputs, '$.memory_ids') FROM runs WHERE id = ?",
                               stream[0]["run_id"])
        assert set(trace["records"]) <= set(json.loads(held))  # the dispatch check covers them


async def test_the_route_decides_what_is_pre_loaded(tmp_path):
    provider = MockProvider()
    async with started(tmp_path / "data", provider) as client:
        project = await project_of(client)
        await entered(client, project, "We use semi-structured interviews.", kind="project_state")
        await entered(client, project, "We dropped the pilot.")
        conversation = await conversation_in(client, project)
        cases = {"Translate this into Chinese: hello": (False, False),  # rewriting supplied text: neither
                 "Compare interviews versus surveys": (False, True),  # a comparison: memory
                 "Why did I drop the pilot? We decided last time": (True, True),  # an earlier decision: both
                 "I keep wondering about attrition": (True, True)}  # no rule decides: both
        for message, (state, remembered) in cases.items():
            await send(client, conversation, message)
            body = provider.answers[-1]
            assert bool(section(body, "PROJECT STATE")) == state, message
            assert bool(section(body, "USER MEMORY")) == remembered, message
        assert "[state] We use semi-structured interviews." in section(provider.answers[-1], "PROJECT STATE")[0]


async def test_project_state_holds_the_state_whats_done_and_the_agents_notes_within_its_budget(tmp_path):
    provider = MockProvider()
    async with started(tmp_path / "data", provider) as client:
        project = await project_of(client)
        await entered(client, project, "Research question: do wage floors raise earnings?", kind="project_state")

        def notes(conn):
            for i in range(8):
                memory.note(conn, project_id=project, run_id=None, kind="next_step", text=f"Next step {i}", materials=())
            memory.materials_added(conn, project, [])
        await write(client, notes)
        await write(client, lambda conn: conn.execute(  # an inferred project state, labeled
            "UPDATE memory_records SET status = 'inferred' WHERE type = 'project_state'"))
        await send(client, await conversation_in(client, project), "Where are we?")
        lines = section(provider.answers[-1], "PROJECT STATE")
        assert lines[0] == "- [state (inferred)] Research question: do wage floors raise earnings?"
        assert [line for line in lines if "agent's note" in line] == [
            f"- [agent's note, next step] Next step {i}" for i in (7, 6, 5, 4, 3)]  # the latest few
        from backend import context
        assert context.count("\n".join(lines)) <= 1500


async def test_another_projects_records_never_enter_a_request_and_personal_ones_only_once_confirmed(tmp_path):
    provider = MockProvider()
    async with started(tmp_path / "data", provider) as client:
        mine, other = await project_of(client, "Mine"), await project_of(client, "Other")
        await entered(client, other, "The other project's decision.")
        await entered(client, other, "I prefer tables.", kind="preference", scope="personal")
        personal = await entered(client, other, "I am a sociologist.", kind="profile", scope="personal")
        await write(client, lambda conn: conn.execute("UPDATE memory_records SET status = 'inferred' WHERE id = ?",
                                                      (personal["id"],)))
        await send(client, await conversation_in(client, mine), "What did we decide last time?")
        held = "\n".join(section(provider.answers[-1], "USER MEMORY"))
        assert "I prefer tables." in held  # a confirmed personal record reaches every project
        assert "The other project's decision." not in held and "sociologist" not in held


async def test_a_review_locked_project_recalls_no_personal_memory(tmp_path):
    async with started(tmp_path / "data") as client:
        project = await project_of(client)
        await entered(client, project, "I prefer tables.", kind="preference", scope="personal")
        await entered(client, project, "Our own decision.")
        found = await asyncio.to_thread(client.state["db"].read, lambda conn: [
            r["content"] for r in memory.recall_candidates(conn, project, locked=True)])
        assert found == ["Our own decision."]


async def test_a_record_whose_paper_is_deleted_is_gone_and_never_recalled(tmp_path):
    from test_materials import added
    provider = MockProvider()
    async with started(tmp_path / "data", provider) as client:
        project = await project_of(client)
        [paper] = (await added(client, project, ("notes.md", b"# Notes\n\nWages rose.\n")))["materials"]

        def saved(conn):  # a saved answer that carries the paper's text
            return memory.write(conn, project_id=project, scope="project", kind="event", content="Wages rose, it says.",
                                status="confirmed", sources=[("material", paper["id"])])
        record = await write(client, saved)
        await send(client, await conversation_in(client, project), "What did we decide last time?")
        assert "Wages rose, it says." in system(provider.answers[-1])
        assert (await client.delete(f"/api/materials/{paper['id']}")).status_code == 200
        assert await rows(client, "SELECT count(*) FROM memory_records WHERE id = ?", record) == [(0,)]
        await send(client, await conversation_in(client, project), "What did we decide last time?")
        assert "Wages rose" not in system(provider.answers[-1])


async def test_recall_ranks_by_meaning_when_the_search_model_answers_and_a_forgotten_vector_goes(tmp_path):
    from cited import cited
    from test_search import idle
    async with cited(tmp_path) as client:
        project = await project_of(client)
        for i in range(11):
            await entered(client, project, f"Unrelated item {i} about scheduling")
        wages = await entered(client, project, "Wage floors raise the earnings of low-paid workers")
        await idle(client, project)
        await background_idle(client, timeout=20)
        index = client.state["index"]
        held = await asyncio.to_thread(index.memory_embedded, [wages["id"]])
        assert held == {wages["id"]}
        stream = await send(client, await conversation_in(client, project),
                            "Last time we decided what about wage floors and earnings?")
        [(trace,)] = await rows(client, "SELECT data FROM run_events WHERE run_id = ? AND type = 'retrieval_trace'"
                                        " AND json_extract(data, '$.memory') IS NOT NULL", stream[0]["run_id"])
        trace = json.loads(trace)["memory"]
        assert trace["mode"] == "similarity" and trace["records"][0] == wages["id"]
        assert (await client.delete(f"/api/memory/{wages['id']}")).json() == {"ok": True}
        assert await asyncio.to_thread(index.memory_embedded, [wages["id"]]) == set()  # removed, checked gone


async def test_an_earlier_decision_memory_lacks_is_said_missing_never_invented(tmp_path):
    # R3: the missing-information section says so; that the answer never invents one is [PAID]
    provider = MockProvider()
    async with started(tmp_path / "data", provider) as client:
        project = await project_of(client)
        conversation = await conversation_in(client, project)
        await send(client, conversation, "Why did I drop the pilot? We decided last time.")
        assert memory.MISSING in section(provider.answers[-1], "CONFLICTS AND MISSING INFORMATION")
        await entered(client, project, "We dropped the pilot because of cost.")
        await send(client, conversation, "Why did I drop the pilot? We decided last time.")
        assert memory.MISSING not in system(provider.answers[-1])
