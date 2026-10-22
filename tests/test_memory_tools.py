"""The agent's memory tools (S1-21a; slice-1 spec F10, sections 6.3 and 8; checklist P3, P4): update_progress writes
an agent's note, marked as the agent's and never confirmed, carrying the papers the turn's request held;
read_project_state and read_memory read within the preview and the project's checks, and what they give the model joins
the request's context before the next step. Recorded tool-calling replies only (tests/agentic.py)."""

import json

import pytest

from agentic import Scripted, call, tool_messages
from backend import agent
from cited import cited, conversation_in, events_of, rows, with_papers
from scholia_app import send
from test_memory import set_project
from test_search import WAGES

pytestmark = pytest.mark.asyncio


async def test_update_progress_writes_the_agents_note_carrying_the_turns_papers(tmp_path):
    provider = Scripted([call("search_passages", query="wages"),
                         call("update_progress", "c2", kind="next_step", text="Screen the 2020 papers")], "Noted.")
    async with cited(tmp_path, provider) as client:
        project, [material] = await with_papers(client, WAGES)
        conversation = await conversation_in(client, project)
        stream = await send(client, conversation, "Note what is next")
        assert stream[-1]["status"] == "succeeded"
        assert {"type": "memory", "action": "noted", "count": 1} in stream
        [(record, kind, status, structured)] = await rows(
            client, "SELECT id, type, status, structured FROM memory_records WHERE type = 'agent_note'")
        assert (kind, status, json.loads(structured)) == ("agent_note", "inferred", {"kind": "next_step"})
        sources = await rows(client, "SELECT source_kind, source_id FROM memory_sources WHERE memory_id = ?", record)
        assert ("material", material) in sources and ("run", stream[0]["run_id"]) in sources
        [(data,)] = await rows(client, "SELECT data FROM run_events WHERE run_id = ? AND type = 'memory_proposed'",
                               stream[0]["run_id"])
        assert "Screen" not in data  # by id only
        left = (await client.get(f"/api/projects/{project}/left-off")).json()
        assert left["next_step"]["content"] == "Screen the 2020 papers"
        # it goes with the paper its turn read (backend/provenance.py)
        assert (await client.delete(f"/api/materials/{material}")).status_code == 200
        assert await rows(client, "SELECT count(*) FROM memory_records WHERE id = ?", record) == [(0,)]


async def test_read_project_state_and_read_memory_join_the_requests_context(tmp_path):
    provider = Scripted([call("read_project_state"), call("read_memory", "c2", query="interviews")], "Done.")
    async with cited(tmp_path, provider) as client:
        project, _ = await with_papers(client, WAGES)
        state = (await client.post("/api/memory", json={"project_id": project, "type": "project_state",
                                                         "content": "Method: interviews"})).json()
        decision = (await client.post("/api/memory", json={"project_id": project, "type": "decision",
                                                            "content": "We code interviews twice"})).json()
        conversation = await conversation_in(client, project)
        stream = await send(client, conversation, "Translate this: hello")  # a rewrite pre-loads no memory
        assert "## PROJECT STATE" not in provider.agent_steps[0]["messages"][0]["content"]
        read_state, read_memory = tool_messages(provider.agent_steps[1])
        assert read_state["content"].split("\n")[0] == "- [state] Method: interviews"
        assert read_state["content"].split("\n")[1].startswith("- [done] Added 1 paper to the library")
        assert read_memory["content"] == "- [decision] We code interviews twice"
        inputs = json.loads((await rows(client, "SELECT inputs FROM runs WHERE id = ?", stream[0]["run_id"]))[0][0])
        assert {state["id"], decision["id"]} <= set(inputs["memory_ids"])
        results = [d for k, d, _ in await events_of(client, stream[0]["run_id"]) if k == "tool_result"]
        assert all("interviews" not in json.dumps(d) for d in results)


async def test_with_memory_off_the_tools_read_and_write_nothing(tmp_path):
    provider = Scripted([call("update_progress", kind="open_question", text="Why?"), call("read_memory", "c2")], "Ok.")
    async with cited(tmp_path, provider) as client:
        project, _ = await with_papers(client, WAGES)
        await client.post("/api/memory", json={"project_id": project, "type": "decision", "content": "Kept"})
        await set_project(client, project, {"memory.enabled": False})
        await send(client, await conversation_in(client, project), "Go")
        noted, read = tool_messages(provider.agent_steps[1])
        assert "not kept" in noted["content"] and "nothing is recalled" in read["content"]
        assert await rows(client, "SELECT count(*) FROM memory_records WHERE type = 'agent_note'") == [(0,)]


async def test_update_progress_refuses_a_blank_note_and_never_takes_a_confirmed_status():
    tool = agent.REGISTRY.get_definition("update_progress")
    assert tool.args_schema["properties"]["kind"]["enum"] == ["open_question", "next_step"]
    assert "status" not in tool.args_schema["properties"] and tool.args_schema["additionalProperties"] is False
    assert (tool.read_only, tool.repeat_safe, tool.network, tool.spends) == (False, False, False, False)
    for name in ("read_project_state", "read_memory"):
        found = agent.REGISTRY.get_definition(name)
        assert (found.read_only, found.tool_class, found.output_chars) == (True, "read_library", 8000)
