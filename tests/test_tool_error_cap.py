"""Compact errors (slice-1 spec section 6.6 and 14's test_tool_error_cap.py row; S1-19c): a failed tool call
returns a short structured error to the model, and after 3 consecutive failures the run stops and asks the
researcher, on the shared confirmation, whether to keep going; the loop goes on with their answer, or ends. A
success between failures starts the count again. Nothing leaves the process."""

import asyncio
import json

import pytest

from agentic import Scripted, call, tool_messages
from cited import cited, conversation_in, events_of, with_papers
from scholia_app import send
from test_agent_loop import asked
from test_search import WAGES

pytestmark = pytest.mark.asyncio
FAILING = ([call("no_such_tool", call_id="a")], [call("read_passage", call_id="b", material_id="x")],
           [{"id": "c", "type": "function", "function": {"name": "list_materials", "arguments": "{not json"}}])


async def test_three_failed_calls_in_a_row_stop_the_run_and_ask_the_researcher_who_stops_it(tmp_path):
    provider = Scripted(*FAILING, "never reached")
    async with cited(tmp_path, provider) as client:
        project, _ = await with_papers(client, WAGES)
        conversation = await conversation_in(client, project)
        sending = asyncio.create_task(send(client, conversation, "Which papers do I have?"))
        ask = await asked(client, conversation)
        assert (ask["kind"], ask["options"], ask["params"]["tool_errors"]) == ("choice", ["continue", "stop"], 3)
        assert len(provider.agent_steps) == 3  # the run waits; nothing more is sent
        await client.post(f"/api/runs/{ask['run_id']}/asks/{ask['ask_id']}", json={"option": "stop"})
        stream = await sending
        assert (stream[-1]["status"], stream[-1]["cancel_reason"]) == ("cancelled", "researcher")
        assert len(provider.agent_steps) == 3
        errors = [json.loads(tool_messages(b)[-1]["content"]) for b in provider.agent_steps[1:]]
        assert [e["error"] for e in errors] == ["validation_error", "not_found"]
        assert all(len(e["message"]) < 120 for e in errors)  # short, structured
        turn = (await client.get(f"/api/conversations/{conversation}")).json()["turns"][-1]
        assert turn["reason_code"] == "tool_errors"


async def test_keep_going_continues_the_loop_with_the_researchers_words(tmp_path):
    provider = Scripted(*FAILING, "Answered after all.")
    async with cited(tmp_path, provider) as client:
        project, _ = await with_papers(client, WAGES)
        conversation = await conversation_in(client, project)
        sending = asyncio.create_task(send(client, conversation, "Which papers do I have?"))
        ask = await asked(client, conversation)
        await client.post(f"/api/runs/{ask['run_id']}/asks/{ask['ask_id']}", json={"text": "Try list_materials once"})
        stream = await sending
        assert stream[-1]["status"] == "succeeded" and len(provider.agent_steps) == 4
        last = provider.agent_steps[-1]["messages"]
        assert last[-1] == {"role": "user", "content": "Try list_materials once"}  # their words, after the chain
        events = await events_of(client, stream[0]["run_id"])
        assert [d.get("by") for k, d, _ in events if k == "ask_answered"] == ["researcher"]


async def test_a_success_between_failures_starts_the_count_again(tmp_path):
    provider = Scripted([call("no_such_tool", call_id="a")], [call("no_such_tool", call_id="b")],
                        [call("list_materials", call_id="c")], [call("no_such_tool", call_id="d")],
                        [call("no_such_tool", call_id="e")], "Answered.")
    async with cited(tmp_path, provider) as client:
        project, _ = await with_papers(client, WAGES)
        conversation = await conversation_in(client, project)
        stream = await send(client, conversation, "Which papers do I have?")
        assert stream[-1]["status"] == "succeeded"


async def test_the_researchers_words_follow_the_steps_last_tool_result(tmp_path):
    provider = Scripted([call("no_such_tool", call_id="a"), call("no_such_tool", call_id="b"),
                         call("no_such_tool", call_id="c"), call("list_materials", call_id="d")], "Done.")
    async with cited(tmp_path, provider) as client:
        project, _ = await with_papers(client, WAGES)
        conversation = await conversation_in(client, project)
        sending = asyncio.create_task(send(client, conversation, "Which papers do I have?"))
        ask = await asked(client, conversation)
        await client.post(f"/api/runs/{ask['run_id']}/asks/{ask['ask_id']}", json={"text": "Go on"})
        assert (await sending)[-1]["status"] == "succeeded"
        roles = [m["role"] for m in provider.agent_steps[1]["messages"]]
        assert roles[-6:] == ["assistant", "tool", "tool", "tool", "tool", "user"]  # tool results right after their step


async def test_a_change_of_protection_asks_again_rather_than_stopping(tmp_path, monkeypatch):
    from backend import agent
    monkeypatch.setattr(agent, "ANSWERED_POLL_SECONDS", 0.05)
    provider = Scripted(*FAILING, "never reached")
    async with cited(tmp_path, provider) as client:
        project, _ = await with_papers(client, WAGES)
        conversation = await conversation_in(client, project)
        sending = asyncio.create_task(send(client, conversation, "Which papers do I have?"))
        first = await asked(client, conversation)
        await asyncio.to_thread(client.state["db"].write, lambda conn: conn.execute(
            "UPDATE projects SET sensitivity = 'private' WHERE id = ?", (project,)))  # a change that revokes nothing
        for _ in range(200):
            found = (await client.get(f"/api/asks?conversation_id={conversation}")).json()["asks"]
            if found and found[0]["ask_id"] != first["ask_id"]:
                break
            await asyncio.sleep(0.05)
        assert found and found[0]["ask_id"] != first["ask_id"] and found[0]["params"]["tool_errors"] == 3  # asked anew
        await client.post(f"/api/runs/{found[0]['run_id']}/asks/{found[0]['ask_id']}", json={"option": "stop"})
        stream = await asyncio.wait_for(sending, 20)
        assert (stream[-1]["status"], stream[-1]["cancel_reason"]) == ("cancelled", "researcher")
