"""Stateless reducers (slice-1 spec sections 6.6 and 14's test_replay_reducers.py row; S1-19c): a turn's state
is a pure function of its recorded events. Recorded runs ended by cancellation, an approval, an ask and an
interruption are replayed through runs.replay, twice, and compared with the run's own record. Nothing leaves
the process."""

import asyncio
import json

import pytest

from agentic import Scripted, call
from backend.runs import replay
from cited import cited, conversation_in, rows, with_papers
from scholia_app import send
from test_agent_loop import asked
from test_search import WAGES

pytestmark = pytest.mark.asyncio


async def replayed_matches_record(client, run_id):
    events = [(k, json.loads(d)) for k, d in await rows(
        client, "SELECT type, data FROM run_events WHERE run_id = ? ORDER BY seq", run_id)]
    [(status, cancel_reason, waiting, skills)] = await rows(
        client, "SELECT status, cancel_reason, waiting, skills FROM runs WHERE id = ?", run_id)
    state = replay(events)
    assert state == replay(list(events))  # the same events, the same state
    assert (state["status"], state["cancel_reason"], state["waiting"], state["skills"]) == (
        status, cancel_reason, waiting, json.loads(skills))
    assert state["steps"] == sum(k == "step_started" for k, _ in events)
    return state


async def waiting_turn(client, provider):
    project, _ = await with_papers(client, WAGES)
    conversation = await conversation_in(client, project)
    sending = asyncio.create_task(send(client, conversation, "Which papers do I have?"))
    return project, conversation, sending, await asked(client, conversation)


async def test_a_turn_cancelled_while_waiting_on_an_ask_replays_to_its_record(tmp_path):
    provider = Scripted([call("use_skill", name="help"), call("ask", call_id="c2", question="Go on?", options=["Yes"])])
    async with cited(tmp_path, provider) as client:
        _, _, sending, ask = await waiting_turn(client, provider)
        events = [(k, json.loads(d)) for k, d in await rows(
            client, "SELECT type, data FROM run_events WHERE run_id = ? ORDER BY seq", ask["run_id"])]
        assert replay(events)["waiting"] == "ask"  # waiting, as the record says while it waits
        await client.post(f"/api/runs/{ask['run_id']}/cancel")
        await sending
        state = await replayed_matches_record(client, ask["run_id"])
        assert (state["status"], state["cancel_reason"], state["skills"]) == ("cancelled", "researcher", ["help"])


async def test_an_approved_tool_call_and_an_answered_ask_replay_to_their_record(tmp_path):
    provider = Scripted([call("list_materials")], [call("ask", call_id="c2", question="Which?", options=["A", "B"])],
                        "Done.")
    async with cited(tmp_path, provider) as client:
        project, _ = await with_papers(client, WAGES)
        settings = (await client.get(f"/api/settings?project_id={project}")).json()
        await client.put("/api/settings", json={"project_id": project, "hash": settings["hash"],
                                                "updates": {"tools.ask": ["list_materials"]}})
        conversation = await conversation_in(client, project)
        sending = asyncio.create_task(send(client, conversation, "Which papers do I have?"))
        for option in ("allow", "B"):
            ask = await asked(client, conversation)
            assert (await client.post(f"/api/runs/{ask['run_id']}/asks/{ask['ask_id']}", json={"option": option})).status_code == 200
            while (await client.get(f"/api/asks?conversation_id={conversation}")).json()["asks"] == [ask]:
                await asyncio.sleep(0.02)
        assert (await sending)[-1]["status"] == "succeeded"
        state = await replayed_matches_record(client, ask["run_id"])
        assert (state["status"], state["tool_calls"]) == ("succeeded", 2)


async def test_an_interrupted_turn_replays_to_its_record(tmp_path):
    provider = Scripted([call("ask", question="Go on?", options=["Yes"])])
    async with cited(tmp_path, provider) as client:
        _, _, sending, ask = await waiting_turn(client, provider)
        await client.state["harness"].shutdown()  # the app closing: the turn ends interrupted
        await sending
        state = await replayed_matches_record(client, ask["run_id"])
        assert (state["status"], state["cancel_reason"]) == ("interrupted", None)


async def test_a_turn_stopped_at_a_limit_replays_to_its_record(tmp_path):
    provider = Scripted(*[[call("list_materials", call_id=f"c{i}")] for i in range(20)])
    async with cited(tmp_path, provider) as client:
        project, _ = await with_papers(client, WAGES)
        conversation = await conversation_in(client, project)
        stream = await send(client, conversation, "Which papers do I have?")
        state = await replayed_matches_record(client, stream[0]["run_id"])
        assert (state["status"], state["cancel_reason"], state["limit"]) == ("cancelled", "limit", "agent_steps")
