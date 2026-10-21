"""Compact errors (slice-1 spec section 6.6 and 14's test_tool_error_cap.py row; S1-19c): a failed tool call
returns a short structured error to the model, and after 3 consecutive failures the run stops, offering
Continue; a success between failures starts the count again. Nothing leaves the process."""

import json

import pytest

from agentic import Scripted, call, tool_messages
from cited import cited, conversation_in, with_papers
from scholia_app import send
from test_search import WAGES

pytestmark = pytest.mark.asyncio


async def test_three_failed_calls_in_a_row_stop_the_run(tmp_path):
    provider = Scripted([call("no_such_tool", call_id="a")], [call("read_passage", call_id="b", material_id="x")],
                        [{"id": "c", "type": "function", "function": {"name": "list_materials", "arguments": "{not json"}}],
                        "never reached")
    async with cited(tmp_path, provider) as client:
        project, _ = await with_papers(client, WAGES)
        conversation = await conversation_in(client, project)
        stream = await send(client, conversation, "Which papers do I have?")
        assert {"type": "limit_reached", "limit": "tool_errors"} in stream
        assert stream[-1]["cancel_reason"] == "limit" and len(provider.agent_steps) == 3
        errors = [json.loads(tool_messages(b)[-1]["content"]) for b in provider.agent_steps[1:]]
        assert [e["error"] for e in errors] == ["validation_error", "not_found"]
        assert all(len(e["message"]) < 120 for e in errors)  # short, structured


async def test_a_success_between_failures_starts_the_count_again(tmp_path):
    provider = Scripted([call("no_such_tool", call_id="a")], [call("no_such_tool", call_id="b")],
                        [call("list_materials", call_id="c")], [call("no_such_tool", call_id="d")],
                        [call("no_such_tool", call_id="e")], "Answered.")
    async with cited(tmp_path, provider) as client:
        project, _ = await with_papers(client, WAGES)
        conversation = await conversation_in(client, project)
        stream = await send(client, conversation, "Which papers do I have?")
        assert stream[-1]["status"] == "succeeded"
