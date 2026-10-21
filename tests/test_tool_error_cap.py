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
