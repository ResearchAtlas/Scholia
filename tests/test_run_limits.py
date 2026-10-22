"""A turn's limits (slice-1 spec sections 13 and 14's test_run_limits.py row; S1-19c): 12 agent steps, 40 tool
calls and 15 minutes a turn (a project's own limits first), each stop recorded as limit_hit with Continue
offered. Model calls go to recorded tool-calling replies; nothing leaves the process."""

import pytest

from agentic import Scripted, call
from cited import cited, conversation_in, events_of, with_papers
from scholia_app import send
from test_search import WAGES

pytestmark = pytest.mark.asyncio


def looping(n=50, per_step=1):
    return [[call("list_materials", call_id=f"c{i}_{j}") for j in range(per_step)] for i in range(n)]


async def project_limits(client, project, **limits):
    settings = (await client.get(f"/api/settings?project_id={project}")).json()
    saved = await client.put("/api/settings", json={"project_id": project, "hash": settings["hash"],
                                                    "updates": {f"limits.{k}": v for k, v in limits.items()}})
    assert saved.status_code == 200, saved.text


async def stopped(client, conversation, limit):
    stream = await send(client, conversation, "Which papers do I have?")
    assert {"type": "limit_reached", "limit": limit} in stream
    assert (stream[-1]["status"], stream[-1]["cancel_reason"]) == ("cancelled", "limit")
    events = await events_of(client, stream[0]["run_id"])
    [hit] = [d for k, d, _ in events if k == "limit_hit"]
    turn = (await client.get(f"/api/conversations/{conversation}")).json()["turns"][-1]
    assert turn["reason_code"] == limit
    return stream, hit, turn


async def test_a_turn_stops_after_twelve_agent_steps_and_offers_continue(tmp_path):
    provider = Scripted(*looping())
    async with cited(tmp_path, provider) as client:
        project, _ = await with_papers(client, WAGES)
        conversation = await conversation_in(client, project)
        stream, hit, _ = await stopped(client, conversation, "agent_steps")
        assert hit == {"agent_steps": 12} and len(provider.agent_steps) == 12
        assert (await client.post(f"/api/runs/{stream[0]['run_id']}/continue")).status_code == 200  # Continue offered


async def test_a_turn_stops_at_its_tool_call_limit_before_the_call_past_it(tmp_path):
    provider = Scripted(*looping(per_step=7))
    async with cited(tmp_path, provider) as client:
        project, _ = await with_papers(client, WAGES)
        conversation = await conversation_in(client, project)
        stream, hit, _ = await stopped(client, conversation, "tool_calls")
        assert hit == {"tool_calls": 40}
        events = await events_of(client, stream[0]["run_id"])
        assert sum(k == "tool_call_started" for k, _, _ in events) == 40


async def test_a_projects_own_limits_come_first(tmp_path):
    provider = Scripted(*looping())
    async with cited(tmp_path, provider) as client:
        project, _ = await with_papers(client, WAGES)
        await project_limits(client, project, agent_steps=3)
        conversation = await conversation_in(client, project)
        _, hit, _ = await stopped(client, conversation, "agent_steps")
        assert hit == {"agent_steps": 3} and len(provider.agent_steps) == 3


async def test_a_turn_stops_past_its_time_limit(tmp_path, monkeypatch):
    from backend import runs
    clock = [1000.0]

    def ticking():  # each look at the turn's clock is six minutes later
        clock[0] += 360
        return clock[0]
    provider = Scripted(*looping())
    async with cited(tmp_path, provider) as client:
        project, _ = await with_papers(client, WAGES)
        conversation = await conversation_in(client, project)
        monkeypatch.setattr(runs, "_clock", ticking)
        _, hit, _ = await stopped(client, conversation, "turn_minutes")
        assert hit == {"turn_minutes": 15} and len(provider.agent_steps) < 12
