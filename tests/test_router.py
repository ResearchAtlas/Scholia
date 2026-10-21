"""The router (slice-1 spec section 7.3, section 14's routing rows; ticket 17; ticket 71): rules first,
in English and Chinese, a selection restricting literature, the small model only when one is set, its
deadline and fallback, and the route recorded as the run's first event. Model calls go to a test-owned
mock behind the outbound gate."""

import asyncio
import json

import pytest

from backend import budget_router, router
from scholia_app import MockProvider, send, started


@pytest.mark.parametrize("message, route, rule", [
    ("Please rewrite this paragraph more clearly.", "none", "rewrite"),
    ("帮我润色这段话", "none", "rewrite"),
    ("把这句话翻译成英文", "none", "rewrite"),
    ("Why did I choose a cohort design last time?", "memory", "earlier_decision"),
    ("我们决定用哪种抽样方法？", "memory", "earlier_decision"),
    ("上次我们讨论了什么", "memory", "earlier_decision"),
    ("Find papers on minimum wages", "find_papers", "find_papers"),
    ("帮我找文献", "find_papers", "find_papers"),
    ("这个领域的最新研究有哪些", "find_papers", "find_papers"),
    ("Compare cohort and case-control studies", "memory_literature", "comparison"),
    ("What is the difference between these designs, versus RCTs?", "memory_literature", "comparison"),
    ("比较这两篇论文的方法", "memory_literature", "comparison"),
    ("两种方法的区别是什么", "memory_literature", "comparison"),
])
def test_each_rule_fires_in_english_and_chinese(message, route, rule):
    assert router.rules(message) == (route, rule)


def test_a_selection_restricts_literature_whatever_the_message_says():
    assert router.rules("Please translate this", selected=True) == ("literature", "selection")


def test_with_no_rule_and_no_small_model_the_fallback_decides_without_a_call():
    for has, route in ((True, "literature"), (False, "none")):
        decision = router.decide("What does the panel show?", has_materials=has)
        assert (decision["route"], decision["decided_by"]) == (route, "fallback")
    assert router.decide("What does the panel show?", small_model=True)["decided_by"] == "router"
    assert router.parse("Literature.") == "literature" and router.parse("maybe") is None


async def route_events(client, run_id):
    rows = await asyncio.to_thread(client.state["db"].read, lambda conn: conn.execute(
        "SELECT seq, type, data FROM run_events WHERE run_id = ? ORDER BY seq", (run_id,)).fetchall())
    return [(seq, kind, json.loads(data)) for seq, kind, data in rows]


@pytest.mark.asyncio
async def test_the_route_is_the_runs_first_event_and_is_streamed(tmp_path):
    async with started(tmp_path / "data") as client:
        conversation = (await client.post("/api/conversations", json={"title": "t"})).json()["id"]
        stream = await send(client, conversation, "Translate this sentence into French.")
        assert [e for e in stream if e["type"] == "route"] == [{"type": "route", "route": "none", "decided_by": "rules"}]
        [(seq, kind, data), *_] = await route_events(client, stream[0]["run_id"])
        assert (seq, kind, data["retrieval"]["route"], data["retrieval"]["rule"]) == (0, "route", "none", "rewrite")


@pytest.mark.asyncio
async def test_an_unset_router_model_makes_no_call_and_a_set_one_makes_one(tmp_path):
    provider = MockProvider()
    async with started(tmp_path / "data", provider) as client:
        conversation = (await client.post("/api/conversations", json={"title": "t"})).json()["id"]
        await send(client, conversation, "What does the panel show?")
        assert len(provider.chats) == 1  # the answer alone: the rules' fallback, no router call
        current = (await client.get("/api/settings")).json()
        model = sorted(budget_router.RECOMMENDED)[0]
        await client.put("/api/settings", json={"hash": current["hash"], "updates": {"models.router": model}})
        provider.replies.append(lambda body: provider.answer("none"))
        stream = await send(client, conversation, "What does the panel show?")
        routed = [b for b in provider.chats if b["messages"][0]["content"].startswith(router.ROUTER_RULES)]
        assert len(routed) == 1 and routed[0]["model"] == model and "tool_choice" not in routed[0]
        events = await route_events(client, stream[0]["run_id"])
        decided = [d["retrieval"] for _, kind, d in events if kind == "route" and d.get("retrieval", {}).get("decided_by")
                   in ("router", "fallback")]
        assert decided[-1]["route"] == "none" and decided[-1]["decided_by"] == "router"


@pytest.mark.asyncio
async def test_a_slow_router_falls_back_and_the_fallback_is_recorded(tmp_path):
    provider = MockProvider()
    async with started(tmp_path / "data", provider) as client:
        current = (await client.get("/api/settings")).json()
        model = sorted(budget_router.RECOMMENDED)[0]
        await client.put("/api/settings", json={"hash": current["hash"], "updates": {
            "models.router": model, "retrieval.router_ms": 100}})
        conversation = (await client.post("/api/conversations", json={"title": "t"})).json()["id"]

        async def slow(body):
            await asyncio.sleep(1)
            return provider.answer("literature")
        provider.replies.append(slow)
        stream = await send(client, conversation, "What does the panel show?")
        assert stream[-1]["status"] == "succeeded"
        events = await route_events(client, stream[0]["run_id"])
        [fallback] = [d["retrieval"] for _, kind, d in events if kind == "route" and d.get("retrieval", {}).get(
            "decided_by") == "fallback"]
        assert (fallback["route"], fallback["reason"]) == ("none", "timeout")  # no materials: no retrieval


@pytest.mark.asyncio
async def test_a_memory_route_preloads_nothing_and_says_so(tmp_path):
    provider = MockProvider()
    async with started(tmp_path / "data", provider) as client:
        conversation = (await client.post("/api/conversations", json={"title": "t"})).json()["id"]
        await send(client, conversation, "What did we decide last time about sampling?")
        [body] = provider.answers
        system = body["messages"][0]["content"]
        assert "## RESEARCH EVIDENCE" not in system and "could not be retrieved rather than inventing them" in system
