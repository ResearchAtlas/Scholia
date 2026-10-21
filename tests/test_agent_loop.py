"""The agent loop (slice-1 spec sections 6.2, 6.3 and 6.6; S1-19c): one loop per turn, each tool call checked,
run and recorded; the read tools' bounds and access checks; answer_from_library whole inside one tool; each
assistant step stored whole and sent back unmodified (model-route rule 5); the route kept through a wait.
Model calls go to recorded tool-calling replies (tests/agentic.py); nothing leaves the process."""

import asyncio
import json

import pytest

from agentic import REASONING, Scripted, call, tool_messages
from cited import cited, conversation_in, events_of, rows, with_papers
from scholia_app import send
from test_search import WAGES, paper

QUESTION = "What do minimum wages do to earnings?"


async def material_of(client, project):
    return (await rows(client, "SELECT id FROM materials WHERE project_id = ?", project))[0][0]


@pytest.mark.asyncio
async def test_a_turn_runs_tool_calls_until_the_model_answers_and_records_each_call(tmp_path):
    provider = Scripted([call("search_passages", query="minimum wages earnings")],
                        "Minimum wages raise the earnings of low-paid workers [S1.1].")
    async with cited(tmp_path, provider) as client:
        project, [material] = await with_papers(client, WAGES)
        conversation = await conversation_in(client, project)
        stream = await send(client, conversation, "Hello there")  # no rule fires: the router's fallback pre-loads
        assert stream[-1]["status"] == "succeeded"
        assert [e["type"] for e in stream if e["type"] in ("tool_call", "step")] == ["step", "tool_call", "step"]
        first, second = provider.agent_steps
        assert {t["function"]["name"] for t in first["tools"]} == {
            "list_materials", "search_passages", "read_passage", "answer_from_library", "use_skill", "read_skill_file",
            "ask", "open_place"}
        assert "tool_choice" not in first  # never forced (model-route rule 4)
        [result] = tool_messages(second)
        assert result["tool_call_id"] == "call_search_passages" and "[S1.1] Wage Floors" in result["content"]
        events = await events_of(client, stream[0]["run_id"])
        kinds = [k for k, _, _ in events]
        assert kinds.index("tool_call_started") < kinds.index("tool_result") < kinds.index("citation_checks")
        [started] = [d for k, d, _ in events if k == "tool_call_started"]
        assert started["name"] == "search_passages" and started["arguments"]["query"] == {"chars": 22}  # never the text
        [done] = [d for k, d, _ in events if k == "tool_result"]
        assert done["ok"] and done["meta"]["passages"]
        [turn] = (await client.get(f"/api/conversations/{conversation}")).json()["turns"]
        [chip] = turn["citations"]
        assert (chip["existence"], chip["material_id"]) == ("ok", material)  # read in the run, through the tool
        inputs = json.loads((await rows(client, "SELECT inputs FROM runs WHERE id = ?", stream[0]["run_id"]))[0][0])
        assert material in inputs["context_materials"]  # registered with the dispatch check and deletion's links


@pytest.mark.asyncio
async def test_a_reply_of_tool_calls_alone_is_a_step_not_a_malformed_answer(tmp_path):
    provider = Scripted({"calls": [call("list_materials")], "content": None}, "The library holds one paper.")
    async with cited(tmp_path, provider) as client:
        project, _ = await with_papers(client, WAGES)
        conversation = await conversation_in(client, project)
        stream = await send(client, conversation, "Which papers do I have?")
        assert stream[-1]["status"] == "succeeded"  # G6
        [listed] = tool_messages(provider.agent_steps[1])
        assert "Wage Floors" in listed["content"] and "full text" in listed["content"]


@pytest.mark.asyncio
async def test_each_assistant_step_is_sent_back_whole_with_its_reasoning_blocks_unmodified(tmp_path):
    provider = Scripted({"calls": [call("list_materials")], "content": "Let me look.", "reasoning_details": REASONING},
                        "One paper.")
    async with cited(tmp_path, provider) as client:
        project, _ = await with_papers(client, WAGES)
        conversation = await conversation_in(client, project)
        await send(client, conversation, "Which papers do I have?")
        second = provider.agent_steps[1]
        user = max(i for i, m in enumerate(second["messages"]) if m["role"] == "user")
        assistant, tool = second["messages"][user + 1:]
        assert assistant["reasoning_details"] == REASONING and assistant["content"] == "Let me look."
        assert assistant["tool_calls"] == [call("list_materials")]  # byte-equal as the provider sent it
        assert tool["role"] == "tool"


@pytest.mark.asyncio
async def test_a_chain_that_outgrows_the_window_stops_the_turn_and_is_never_cut(tmp_path):
    long = "中" * 20000  # about 60,000 counted tokens a step, in a 128K window
    provider = Scripted(*[{"calls": [call("list_materials", call_id=f"c{i}")], "content": long} for i in range(5)])
    async with cited(tmp_path, provider) as client:
        project, _ = await with_papers(client, WAGES)
        conversation = await conversation_in(client, project)
        stream = await send(client, conversation, "Which papers do I have?")
        assert stream[-1]["cancel_reason"] == "limit"
        assert {"type": "limit_reached", "limit": "chain_over_window"} in stream
        assert len(provider.agent_steps) == 2  # the third request would not fit: never sent with a shortened chain
        second = provider.agent_steps[1]
        assert [m["content"] for m in second["messages"] if m["role"] == "assistant"][-1] == long


@pytest.mark.asyncio
async def test_a_rejected_continuation_fails_safely_and_is_never_retried_with_edited_history(tmp_path):
    provider = Scripted([call("list_materials")], (400, {"error": {"message": "reasoning block mismatch"}}))
    async with cited(tmp_path, provider) as client:
        project, _ = await with_papers(client, WAGES)
        conversation = await conversation_in(client, project)
        stream = await send(client, conversation, "Which papers do I have?")
        assert stream[-1]["status"] == "failed" and {"type": "error", "code": "request"} in stream
        assert len(provider.agent_steps) == 2  # no third step, no retry without the chain


@pytest.mark.asyncio
async def test_read_passage_reads_whole_passages_in_order_within_its_bounds_and_lists_the_rest(tmp_path):
    long = paper("Long Panel", *[f"Paragraph {i} " + "word " * 300 for i in range(12)])
    async with cited(tmp_path, Scripted(after="Read.")) as client:
        project, [material] = await with_papers(client, long)
        provider = client.provider
        [(fifth,)] = await rows(client, "SELECT ordinal FROM passages WHERE text LIKE 'Paragraph 5 %'")
        provider.script += [[call("read_passage", material_id=material)], [call("read_passage", material_id=material,
                                                                                   ordinal=fifth, call_id="c2")]]
        conversation = await conversation_in(client, project)
        await send(client, conversation, "Read it")
        first, second = (tool_messages(b)[-1]["content"] for b in provider.agent_steps[1:3])
        assert len(first) <= 8000 + 200 and "Passages left out, by ordinal:" in first
        assert first.index("Paragraph 0") < first.index("Paragraph 1")
        assert "Paragraph 5 " in second and "Paragraph 4 " not in second


@pytest.mark.asyncio
async def test_read_passage_refuses_a_paper_of_another_project_and_a_deleted_one(tmp_path):
    async with cited(tmp_path, Scripted(after="Done.")) as client:
        project, [mine] = await with_papers(client, WAGES)
        other, [theirs] = await with_papers(client, paper("Elsewhere", "Text of another project."))
        deleted = (await with_papers(client, paper("Gone Paper", "Soon removed.")))[1][0]
        assert (await client.delete(f"/api/materials/{deleted}")).status_code in (200, 204)
        client.provider.script += [[call("read_passage", material_id=theirs, call_id="a"),
                                    call("read_passage", material_id=deleted, call_id="b"),
                                    call("read_passage", material_id=mine, call_id="c")]]
        conversation = await conversation_in(client, project)
        stream = await send(client, conversation, "Read them")
        refused, gone, read = tool_messages(client.provider.agent_steps[1])
        assert json.loads(refused["content"])["error"] == "not_found"
        assert json.loads(gone["content"])["error"] == "not_found"
        assert "[S" in read["content"] and "Text of another project" not in json.dumps(client.provider.agent_steps[1])
        assert stream[-1]["status"] == "succeeded"


@pytest.mark.asyncio
async def test_answer_from_library_runs_the_pipeline_and_its_checks_whole_and_its_verdicts_are_reused(tmp_path):
    drafted = "Minimum wages raise the earnings of low-paid workers [S1.1]."
    provider = Scripted([call("answer_from_library", question=QUESTION)], drafted, inner=[drafted])
    async with cited(tmp_path, provider) as client:
        project, _ = await with_papers(client, WAGES)
        conversation = await conversation_in(client, project)
        stream = await send(client, conversation, "Hello there")
        assert stream[-1]["status"] == "succeeded"
        assert len(provider.inners) == 1 and len(provider.judged) == 1  # the tool's judge; the delivered answer reuses it
        [result] = tool_messages(provider.agent_steps[1])
        assert result["content"].startswith(drafted) and "[S1.1] supported" in result["content"]
        events = await events_of(client, stream[0]["run_id"])
        phases = [d["phase"] for k, d, _ in events if k == "step_started"]
        assert phases == ["answer", "answer_from_library", "judge", "answer"]
        [checks] = [d for k, d, _ in events if k == "citation_checks"]
        assert checks["pairs"]["reused"] == 1 and checks["citations"][0]["support"] == "supported"


@pytest.mark.asyncio
async def test_a_rewritten_sentence_is_judged_again(tmp_path):
    drafted = "Minimum wages raise the earnings of low-paid workers [S1.1]."
    provider = Scripted([call("answer_from_library", question=QUESTION)],
                        "Wage floors lift low earnings [S1.1].", inner=[drafted])
    async with cited(tmp_path, provider) as client:
        project, _ = await with_papers(client, WAGES)
        conversation = await conversation_in(client, project)
        await send(client, conversation, "Hello there")
        assert len(provider.judged) == 2  # the delivered wording is checked, not the tool's


@pytest.mark.asyncio
async def test_the_route_and_effort_stay_the_turns_through_its_steps_and_a_wait(tmp_path):
    provider = Scripted([call("ask", question="Which outcome?", options=["Earnings", "Jobs"])],
                        [call("list_materials", call_id="c2")], "Done.")
    async with cited(tmp_path, provider) as client:
        project, _ = await with_papers(client, WAGES)
        conversation = await conversation_in(client, project)
        sending = asyncio.create_task(send(client, conversation, "Which papers do I have?"))
        ask = await asked(client, conversation)
        provider.catalog = []  # a catalog change meanwhile changes nothing for this turn
        answered = await client.post(f"/api/runs/{ask['run_id']}/asks/{ask['ask_id']}", json={"option": "Jobs"})
        assert answered.status_code == 200
        stream = await sending
        assert stream[-1]["status"] == "succeeded"
        events = await events_of(client, stream[0]["run_id"])
        routes = {(d["route"], d["effort"]) for k, d, _ in events if k == "step_started"}
        assert len(routes) == 1  # A9, D15
        [answer] = tool_messages(provider.agent_steps[1])
        assert answer["content"] == "The researcher answered: Jobs"


async def asked(client, conversation, timeout=10.0):
    deadline = asyncio.get_running_loop().time() + timeout
    while True:
        found = (await client.get(f"/api/asks?conversation_id={conversation}")).json()["asks"]
        if found:
            return found[0]
        assert asyncio.get_running_loop().time() < deadline, "no ask was raised"
        await asyncio.sleep(0.05)


@pytest.mark.asyncio
async def test_invisible_or_blank_arguments_are_a_validation_error_for_the_model(tmp_path):
    provider = Scripted([call("search_passages", query="​ ⁠")], "Nothing.")
    async with cited(tmp_path, provider) as client:
        project, _ = await with_papers(client, WAGES)
        conversation = await conversation_in(client, project)
        await send(client, conversation, "Which papers do I have?")
        [error] = tool_messages(provider.agent_steps[1])
        assert json.loads(error["content"]) == {"error": "validation_error", "message": "The query is empty."}  # G7


@pytest.mark.asyncio
async def test_the_projects_tool_rules_deny_then_ask_then_allow(tmp_path):
    provider = Scripted([call("list_materials"), call("search_passages", query="wages", call_id="c2")], "Done.")
    async with cited(tmp_path, provider) as client:
        project, _ = await with_papers(client, WAGES)
        settings = (await client.get(f"/api/settings?project_id={project}")).json()
        saved = await client.put("/api/settings", json={"project_id": project, "hash": settings["hash"], "updates": {
            "tools.deny": ["list_materials"], "tools.ask": ["list_materials", "search_passages"]}})
        assert saved.status_code == 200, saved.text
        conversation = await conversation_in(client, project)
        sending = asyncio.create_task(send(client, conversation, "Which papers do I have?"))
        ask = await asked(client, conversation)  # search_passages is asked about; list_materials, denied, is not
        assert ask["params"]["approval"] == "search_passages" and ask["options"] == ["allow", "decline"]
        await client.post(f"/api/runs/{ask['run_id']}/asks/{ask['ask_id']}", json={"option": "decline"})
        await sending
        denied, declined = tool_messages(provider.agent_steps[1])
        assert json.loads(denied["content"])["error"] == "denied"
        assert json.loads(declined["content"])["error"] == "denied"


def test_every_tool_declares_section_six_three_s_facts_and_a_closed_argument_schema():
    from backend import agent
    from backend.tools.types import ToolDefinition
    declared = {"read_only", "repeat_safe", "network", "output_chars", "tool_class", "spends"}
    assert declared <= set(ToolDefinition.model_fields)
    tools = agent.REGISTRY.list_tools()
    assert {t.name for t in tools} == {"list_materials", "search_passages", "read_passage", "answer_from_library",
                                       "use_skill", "read_skill_file", "ask", "open_place"}  # section 6.3, S1-19's
    for tool in tools:
        assert all(getattr(tool, name) is not None for name in declared), tool.name
        assert tool.args_schema["type"] == "object" and tool.args_schema["additionalProperties"] is False
        assert agent.REGISTRY.get_implementation(tool.name) is not None
    assert [t.name for t in tools if t.spends] == ["answer_from_library"]
    assert {t.tool_class for t in tools} == {"read_library", "pipeline", "agent_control"}


def test_the_tool_router_refuses_arguments_its_schema_does_not_allow():
    from backend import agent
    from backend.tools.router import ToolRouter
    from backend.tools.types import ToolCall
    router = ToolRouter(agent.REGISTRY, deny=["open_place"], ask=["open_place", "ask"])
    assert (router.rule("open_place"), router.rule("ask"), router.rule("list_materials")) == ("deny", "ask", "allow")
    cases = [("ask", {"question": "Q", "options": ["a", "b", "c", "d"]}), ("ask", {"question": "Q"}),
             ("read_passage", {"material_id": "m", "ordinal": True}), ("read_passage", {"material_id": "m", "x": 1}),
             ("search_passages", {"query": 3}), ("nothing", {}), ("open_place", {"place": "library"})]
    assert [router.check(ToolCall(id="c", name=n, arguments=a)).type for n, a in cases] == [
        "validation_error"] * 6 + ["denied"]
    assert router.check(ToolCall(id="c", name="ask", arguments={"question": "Q", "options": ["a"]})) is None
