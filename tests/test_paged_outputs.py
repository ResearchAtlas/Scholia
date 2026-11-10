"""Paged tool outputs (S1-21b; slice-1 spec sections 8, Paging, and 13: an 8,000-character preview with a handle;
checklist O1, O2, I5, I6): a tool's output past the preview goes whole to the content store, the model gets the preview
and a handle, and read_output returns the next part; a passage counts as read only once a page showing its marker is
served; a handle is valid only in its own turn, and the kept output goes with its paper. Recorded tool-calling replies
(tests/agentic.py); nothing leaves the process."""

import asyncio
import json
import logging
import os
import re
import stat

import pytest

from agentic import Scripted, call, tool_messages
from backend import agent, compaction
from backend.tools.registry import ToolRegistry
from backend.tools.router import ToolRouter
from backend.tools.types import ToolCall, ToolResult
from cited import cited, conversation_in, events_of, rows, with_papers
from scholia_app import send
from test_search import paper

pytestmark = pytest.mark.asyncio
LONG = paper("Long Panel", *[f"Paragraph {i} " + "word " * 300 for i in range(14)])
HANDLE = agent.traced_id("call_read_passage") + ".1"  # the call's content-free id and its number in the turn


def markers(text):
    return re.findall(r"^\[(S\d+\.\d+)\]", text, re.M)


async def test_o1_o2_read_passage_past_its_preview_is_paged_and_only_pages_served_count_as_read(tmp_path):
    pages = {}

    def second(body):  # the model reads on from where the preview stopped
        pages["first"] = tool_messages(body)[-1]["content"]
        return [call("read_output", call_id="c2", handle=HANDLE, offset=compaction.PREVIEW)]

    def answer(body):
        pages["second"] = tool_messages(body)[-1]["content"]
        seen, unseen = markers(pages["second"])[0], f"S1.{int(markers(pages['second'])[-1].split('.')[1]) + 1}"
        pages["seen"], pages["unseen"] = seen, unseen
        return f"Paragraph text is long [{seen}]. Another claim [{unseen}]."
    async with cited(tmp_path, Scripted([call("read_passage", material_id="m")], second, answer)) as client:
        project, [material] = await with_papers(client, LONG)
        client.provider.script[0] = [call("read_passage", material_id=material)]
        conversation = await conversation_in(client, project)
        stream = await send(client, conversation, "Read the long paper")
        assert stream[-1]["status"] == "succeeded"
        first = pages["first"]
        assert first.startswith("[S1.1] Long Panel") and len(first) <= compaction.PREVIEW + 200
        assert f"handle {HANDLE}, offset {compaction.PREVIEW}" in first  # the preview names its handle
        assert first[:compaction.PREVIEW].count("Paragraph") < 14  # the rest is past the preview
        second_page = pages["second"]
        assert "Paragraph" in second_page and set(markers(second_page)).isdisjoint(markers(first))
        # Read in the run: passages whose marker a served page showed; one never paged is not.
        [turn] = (await client.get(f"/api/conversations/{conversation}")).json()["turns"]
        by_marker = {c["marker"]: c["existence"] for c in turn["citations"]}
        assert by_marker[f"[{pages['seen']}]"] == "ok" and by_marker[f"[{pages['unseen']}]"] == "not_found"
        # Kept whole, owner-only, under the turn's run; the trace keeps its length, never its text.
        run = stream[0]["run_id"]
        [(sha,)] = await rows(client, "SELECT sha256 FROM run_bodies WHERE run_id = ? AND attempt >= ?", run,
                              compaction.OUTPUT_SLOT)
        path = client.state["db"].data_dir / "content" / sha[:2] / sha
        assert stat.S_IMODE(os.stat(path).st_mode) == 0o600 and "Paragraph 13" in path.read_text()
        results = [d for k, d, _ in await events_of(client, run) if k == "tool_result"]
        assert results[0]["meta"]["paged"] > compaction.PREVIEW and "Paragraph" not in json.dumps(results)
        # The kept output goes with its paper.
        assert (await client.delete(f"/api/materials/{material}")).status_code == 200
        assert await rows(client, "SELECT count(*) FROM run_bodies WHERE sha256 = ?", sha) == [(0,)]


async def test_o1_a_handle_from_another_turn_or_past_the_end_is_refused(tmp_path):
    provider = Scripted([call("read_passage", material_id="m")], "Read.",
                        [call("read_output", call_id="c3", handle=HANDLE, offset=compaction.PREVIEW)], "Done.")
    async with cited(tmp_path, provider) as client:
        project, [material] = await with_papers(client, LONG)
        provider.script[0] = [call("read_passage", material_id=material)]
        conversation = await conversation_in(client, project)
        await send(client, conversation, "Read the long paper")
        await send(client, conversation, "Read on")
        [refused] = tool_messages(provider.agent_steps[-1])
        assert json.loads(refused["content"])["error"] == "not_found"  # valid only within its own turn


async def test_o1_reading_past_the_end_or_an_unknown_handle_is_refused(tmp_path):
    seen = {}

    def read_badly(body):
        seen["first"] = tool_messages(body)[-1]["content"]
        return [call("read_output", call_id="c2", handle=HANDLE, offset=10 ** 6),
                call("read_output", call_id="c3", handle="call-unknown", offset=0)]
    provider = Scripted([call("read_passage", material_id="m")], read_badly, "Done.")
    async with cited(tmp_path, provider) as client:
        project, [material] = await with_papers(client, LONG)
        provider.script[0] = [call("read_passage", material_id=material)]
        conversation = await conversation_in(client, project)
        await send(client, conversation, "Read the long paper")
        past, unknown = tool_messages(provider.agent_steps[-1])[-2:]
        assert json.loads(past["content"])["error"] == "validation_error"
        assert json.loads(unknown["content"])["error"] == "not_found"


async def test_i5_no_paged_text_reaches_the_logs(tmp_path, caplog):
    caplog.set_level(logging.DEBUG)
    provider = Scripted([call("read_passage", material_id="m")],
                        [call("read_output", call_id="c2", handle=HANDLE, offset=compaction.PREVIEW)], "Done.")
    async with cited(tmp_path, provider) as client:
        project, [material] = await with_papers(client, LONG)
        provider.script[0] = [call("read_passage", material_id=material)]
        conversation = await conversation_in(client, project)
        caplog.clear()
        await send(client, conversation, "Read the long paper")
    assert "Paragraph 9" not in caplog.text and "word word" not in caplog.text


async def test_the_router_returns_a_result_whole_whatever_its_tools_budget():
    # Fix batch 1 (gpt-6-astra review 1, finding 1): the budget bounds the preview, never what is kept.
    registry = ToolRegistry()

    @registry.register("big", "A big tool.", {"type": "object", "properties": {}}, read_only=True, repeat_safe=True,
                       network=False, output_chars=10, tool_class="read_library", spends=False)
    async def big(call, *context):
        return ToolResult(id=call.id, ok=True, text="x" * 50)
    result = await ToolRouter(registry).execute(ToolCall(id="a", name="big"))
    assert result.text == "x" * 50


async def test_pages_are_whole_and_say_how_to_read_on():
    text = "a" * 20000
    first = compaction.preview(text, "h")
    assert first.startswith("a" * compaction.PREVIEW) and "offset 8000" in first
    middle = compaction.page(text, 8000, "h")
    assert middle.startswith("a" * compaction.PREVIEW) and middle[compaction.PREVIEW] == "\n" and "offset 16000" in middle
    assert compaction.page(text, 16000, "h").endswith("[End of the output.]")


async def test_a_pending_passage_keeps_its_marker_when_another_tool_takes_it_back():
    record = lambda p: {"passage_id": p, "material_id": "m", "text": p, "title": "T", "evidence_type": "full_text"}
    evidence = agent.Evidence({})
    first, later = evidence.add([record("p1"), record("p2")], shown=False)
    evidence.show([first])
    before = set(evidence.numbered)
    [again] = evidence.add([record("p2")])  # a search finds the passage still pending in a paged output
    evidence.keep([again], (), before)  # and does not show it after all
    assert again == later and later in evidence.pending and later not in evidence.numbered
    [other] = evidence.add([record("p3")])
    assert other not in (first, later)  # its marker is never given to another passage


async def test_o2_read_passage_reads_whole_passages_up_to_its_bound_and_lists_the_rest_by_ordinal(tmp_path):
    longer = paper("Longer Panel", *[f"Paragraph {i} " + "word " * 300 for i in range(40)])  # past 50,000 characters
    provider = Scripted([call("read_passage", material_id="m")], "Read.")
    async with cited(tmp_path, provider) as client:
        project, [material] = await with_papers(client, longer)
        provider.script[0] = [call("read_passage", material_id=material)]
        conversation = await conversation_in(client, project)
        run = (await send(client, conversation, "Read it"))[0]["run_id"]
        [(sha,)] = await rows(client, "SELECT sha256 FROM run_bodies WHERE run_id = ? AND attempt >= ?", run,
                              compaction.OUTPUT_SLOT)
        whole = (client.state["db"].data_dir / "content" / sha[:2] / sha).read_text()
        assert len(whole) <= agent.READ_CHARS and "Paragraph 0 " in whole and "Paragraph 39 " not in whole
        assert whole.rstrip().endswith("Read one with its ordinal.)") and "Passages left out, by ordinal:" in whole


def reading_on(handle):
    """A step that reads the second page of the output handle names, once the preview showed it."""
    def step(body):
        return [call("read_output", call_id="next", handle=handle, offset=compaction.PREVIEW)]
    return step


async def test_fix1_answer_from_library_past_the_preview_is_kept_whole_and_its_end_paged(tmp_path):
    # gpt-6-astra review 1, finding 1: its ending and its citation checks were cut, with no handle and no kept row.
    long = "Minimum wages raise the earnings of low-paid workers [S1.1]. " + "More detail follows here. " * 400 + "END-MARK."
    handle = agent.traced_id("call_answer_from_library") + ".1"
    provider = Scripted([call("answer_from_library", question="What do minimum wages do?")], reading_on(handle),
                        "Done.", inner=[long])
    async with cited(tmp_path, provider) as client:
        project, _ = await with_papers(client, paper("Wage Floors", "Minimum wages raise the earnings of low-paid workers."))
        conversation = await conversation_in(client, project)
        run = (await send(client, conversation, "Answer from my library"))[0]["run_id"]
        first, second = tool_messages(provider.agent_steps[-1])
        assert f"handle {handle}, offset {compaction.PREVIEW}" in first["content"] and "END-MARK" not in first["content"]
        assert "END-MARK" in second["content"] and "Citation checks, in order:" in second["content"]
        assert await rows(client, "SELECT count(*) FROM run_bodies WHERE run_id = ? AND attempt = ?", run,
                          compaction.OUTPUT_SLOT + 1) == [(1,)]


async def test_fix1_every_tool_is_paged_past_its_preview(tmp_path, monkeypatch):
    async def big(call, turn):
        return ToolResult(id=call.id, ok=True, text="y" * 9000 + " END-MARK")
    monkeypatch.setitem(agent.REGISTRY._implementations, "list_materials", big)
    handle = agent.traced_id("call_list_materials") + ".1"
    provider = Scripted([call("list_materials")], reading_on(handle), "Done.")
    async with cited(tmp_path, provider) as client:
        project, _ = await with_papers(client, LONG)
        conversation = await conversation_in(client, project)
        await send(client, conversation, "List them")
        first, second = tool_messages(provider.agent_steps[-1])
        assert len(first["content"]) <= compaction.PREVIEW + 200 and "END-MARK" in second["content"]


@pytest.mark.parametrize("failure", ["refused", "raised"])
async def test_fix1_an_output_that_could_not_be_kept_is_never_promised_as_paged(tmp_path, monkeypatch, failure):
    # gpt-6-astra review 1, finding 2: ok, a paged count and "Read on" with a handle that answered not_found.
    def keep_output(*args):
        if failure == "raised":
            raise RuntimeError("the database refused it")
        return False
    monkeypatch.setattr(compaction, "keep_output", keep_output)
    async def big(call, turn):
        return ToolResult(id=call.id, ok=True, text="y" * 9000)
    monkeypatch.setitem(agent.REGISTRY._implementations, "list_materials", big)
    provider = Scripted([call("list_materials")], "Done.")
    async with cited(tmp_path, provider) as client:
        project, _ = await with_papers(client, LONG)
        conversation = await conversation_in(client, project)
        run = (await send(client, conversation, "List them"))[0]["run_id"]
        [listed] = tool_messages(provider.agent_steps[-1])
        assert "read_output" not in listed["content"] and "could not be kept" in listed["content"]
        [result] = [d for k, d, _ in await events_of(client, run) if k == "tool_result"]
        assert "paged" not in result["meta"] and result["meta"]["cut"]


async def test_review_1_a_passage_counts_as_shown_only_with_its_whole_marker_on_the_page():
    assert compaction.marked(compaction.PREVIEW - 6, "[S1.2]", 0)  # "[S1.2]" ends at the page's end
    assert not compaction.marked(compaction.PREVIEW - 5, "[S1.2]", 0)  # cut by the boundary
    # Recheck 1 (P3-1): completed by the page the footer names, so sequential paging reads it
    assert compaction.marked(compaction.PREVIEW - 5, "[S1.2]", compaction.PREVIEW)
    assert not compaction.marked(10, "[S1.2]", 20)  # before the page


async def test_fix1_papers_and_memory_records_past_the_preview_count_as_given_only_once_paged(tmp_path, monkeypatch):
    # A listed paper (its title in the request) and a memory record are registered only once a served page shows them.
    from backend.db.database import new_id
    listing, remembering = agent.traced_id("call_list_materials") + ".1", agent.traced_id("call_read_memory") + ".2"
    provider = Scripted([call("list_materials"), call("read_memory")],
                        [call("read_output", call_id="p1", handle=listing, offset=compaction.PREVIEW),
                         call("read_output", call_id="p2", handle=remembering, offset=compaction.PREVIEW)], "Done.")
    async with cited(tmp_path, provider) as client:
        project, [selected] = await with_papers(client, LONG)
        papers = [new_id() for _ in range(400)]
        await asyncio.to_thread(client.state["db"].write, lambda conn: conn.executemany(
            "INSERT INTO materials (id, project_id, title, source) VALUES (?, ?, ?, 'upload')",
            [(m, project, f"Paper {i:04d}") for i, m in enumerate(papers)]))
        records = [(await client.post("/api/memory", json={"project_id": project, "type": "decision",
                                                           "content": f"Decision {i}: " + "detail " * 138})).json()["id"]
                   for i in range(10)]
        conversation = await conversation_in(client, project)
        # a message selecting a paper recalls no memory (memory.wants): what the request holds is the tools' alone
        stream = await send(client, conversation, "What do I have?", material_ids=[selected])
        results = [d for k, d, _ in await events_of(client, stream[0]["run_id"]) if k == "tool_result"]
        listed, remembered, page1, page2 = (r["meta"] for r in results)
        assert len(listed["materials"]) < 401 and 0 < len(remembered["memory"]) < 10
        assert set(page2["memory"]) - set(remembered["memory"])  # (a record the boundary cuts is in both)
        inputs = json.loads((await rows(client, "SELECT inputs FROM runs WHERE id = ?", stream[0]["run_id"]))[0][0])
        # held only what the preview and the page showed: none of the page's records before it was served
        assert set(inputs["memory_ids"]) == set(remembered["memory"]) | set(page2["memory"])
        never = [m for m in papers if m not in inputs["context_materials"]]
        assert never and set(listed["materials"]) | set(page1["materials"]) <= set(inputs["context_materials"])
        assert set(page1["materials"]) - set(listed["materials"])  # the page names what it listed


async def test_fix1_review_a_page_at_any_offset_registers_every_item_it_shows_any_of(tmp_path):
    # Fix batch 1 internal review (P1-2): a page starting inside a line, at an offset the footer did not name, shows the
    # rest of that paper's title; its paper is registered, and its trace names it.
    from backend.db.database import new_id
    listing = agent.traced_id("call_list_materials") + ".1"
    offset = compaction.PREVIEW + 3007
    provider = Scripted([call("list_materials")], [call("read_output", call_id="p1", handle=listing, offset=offset)],
                        "Done.")
    async with cited(tmp_path, provider) as client:
        project, _ = await with_papers(client, LONG)
        papers = [new_id() for _ in range(300)]
        await asyncio.to_thread(client.state["db"].write, lambda conn: conn.executemany(
            "INSERT INTO materials (id, project_id, title, source) VALUES (?, ?, ?, 'upload')",
            [(m, project, f"Paper {i:04d} " + "on minimum wages " * 6) for i, m in enumerate(papers)]))
        conversation = await conversation_in(client, project)
        run = (await send(client, conversation, "What do I have?"))[0]["run_id"]
        [(sha,)] = await rows(client, "SELECT sha256 FROM run_bodies WHERE run_id = ? AND attempt >= ?", run,
                              compaction.OUTPUT_SLOT)
        whole = (client.state["db"].data_dir / "content" / sha[:2] / sha).read_text()
        begun = whole[:offset].rsplit("\n", 1)[1]  # the line the page starts inside
        assert begun and not whole[offset - 1] == "\n"
        straddled = begun.split(" | ", 1)[0]
        after = whole[offset + compaction.PREVIEW:].split("\n")[2].split(" | ", 1)[0]  # a line wholly past the page
        inputs = json.loads((await rows(client, "SELECT inputs FROM runs WHERE id = ?", run))[0][0])
        assert straddled in inputs["context_materials"] and after not in inputs["context_materials"]
        page = [d for k, d, _ in await events_of(client, run) if k == "tool_result"][-1]["meta"]
        assert straddled in page["materials"]


async def test_fix2_forget_removes_a_kept_output_and_the_pages_served_from_it(tmp_path):
    # gpt-6-astra recheck 2 (P1): a memory tool's kept output, and a page served from it, outlived a Forget of a record
    # they hold: read_output answered with the forgotten text, and the trace served both copies.
    from agentic import steps_reply
    handle = agent.traced_id("call_read_memory") + ".1"
    held = {}

    async def forget_then_read_on(body):  # a record past the preview, never shown, is forgotten mid-turn
        assert (await held["client"].delete(f"/api/memory/{held['records'][0]}")).status_code in (200, 204)
        return steps_reply(provider, [call("read_output", "p1", handle=handle, offset=compaction.PREVIEW)])
    provider = Scripted([call("read_memory")], forget_then_read_on, "Done.",
                        [call("read_memory")], [call("read_output", "p2", handle=handle, offset=compaction.PREVIEW)],
                        "Done.")
    async with cited(tmp_path, provider) as client:
        held["client"] = client
        project, [selected] = await with_papers(client, LONG)
        held["records"] = [(await client.post("/api/memory", json={
            "project_id": project, "type": "decision", "content": f"Decision {i}: " + "detail " * 138})).json()["id"]
            for i in range(10)]  # newest first in read_memory: record 0 is last, wholly past the preview
        conversation = await conversation_in(client, project)

        async def bodies(run):
            return await rows(client, "SELECT attempt, sha256 FROM run_bodies WHERE run_id = ?", run)

        async def served(run, sha):
            return (await client.get(f"/api/runs/{run}/trace/bodies/{sha}")).status_code
        # a message selecting a paper recalls no memory, so the turn holds only what the tool showed
        first = (await send(client, conversation, "What did we decide?", material_ids=[selected]))[0]["run_id"]
        [read_on] = [d for k, d, _ in await events_of(client, first) if k == "tool_result" and d["name"] == "read_output"]
        assert not read_on["ok"] and held["records"][0] not in (read_on.get("meta") or {}).get("memory", [])
        [_, after] = tool_messages(provider.agent_steps[-1])
        assert json.loads(after["content"])["error"] == "not_found"  # the output went with the record
        assert "Decision 0:" not in json.dumps(provider.agent_steps[-1])
        assert not [a for a, _ in await bodies(first) if a >= compaction.OUTPUT_SLOT]
        second = (await send(client, conversation, "What did we decide?", material_ids=[selected]))[0]["run_id"]
        [_, page] = tool_messages(provider.agent_steps[-1])
        [(whole,)] = [(s,) for a, s in await bodies(second) if a >= compaction.OUTPUT_SLOT]
        [paging] = [d for k, d, _ in await events_of(client, second) if k == "tool_result" and d["name"] == "read_output"]
        page_sha = paging["result"]
        assert "detail" in page["content"] and held["records"][1] in paging["meta"]["memory"]  # the oldest's tail
        assert await served(second, whole) == 200 and await served(second, page_sha) == 200
        assert (await client.delete(f"/api/memory/{held['records'][1]}")).status_code in (200, 204)
        kept = {s for _, s in await bodies(second)}
        assert whole not in kept and page_sha not in kept  # in the deletion's own transaction
        assert await served(second, whole) == 410 and await served(second, page_sha) == 410  # removed, said so



async def test_fix2_a_conversations_deletion_removes_its_kept_outputs_pages_and_their_links(tmp_path):
    # Fix batch 2 sweep: every stored copy this PR keeps goes with its run's conversation, links included.
    handle = agent.traced_id("call_read_memory") + ".1"
    provider = Scripted([call("read_memory")], [call("read_output", "p1", handle=handle, offset=compaction.PREVIEW)],
                        "Done.")
    async with cited(tmp_path, provider) as client:
        project, [selected] = await with_papers(client, LONG)
        for i in range(10):
            await client.post("/api/memory", json={"project_id": project, "type": "decision",
                                                   "content": f"Decision {i}: " + "detail " * 138})
        conversation = await conversation_in(client, project)
        run = (await send(client, conversation, "What did we decide?", material_ids=[selected]))[0]["run_id"]
        shas = [s for (s,) in await rows(client, "SELECT sha256 FROM run_bodies WHERE run_id = ?", run)]
        linked = await rows(client, "SELECT count(*) FROM run_body_memory WHERE sha256 IN (SELECT value FROM json_each(?))",
                            json.dumps(shas))
        assert linked[0][0] > 0  # the kept output and the page follow their records
        assert (await client.delete(f"/api/conversations/{conversation}")).status_code == 200
        for table in ("run_bodies", "run_body_memory", "run_body_materials"):
            assert await rows(client, f"SELECT count(*) FROM {table} WHERE sha256 IN (SELECT value FROM json_each(?))",
                              json.dumps(shas)) == [(0,)], table


async def test_fix3_read_passage_stays_within_its_bound_counting_what_it_emits(tmp_path):
    # gpt-6-astra recheck 3 (P1, finding 2): the title is repeated on every passage, so 40 paragraphs under a 990-character
    # title stored 71,749 characters against the 50,000-character read bound.
    title = "A Very Long Title " + "on minimum wages and employment " * 30  # about 990 characters
    longer = paper("Long Panel", *[f"Paragraph {i} " + "word " * 300 for i in range(40)])
    provider = Scripted([call("read_passage", material_id="m")], "Read.")
    async with cited(tmp_path, provider) as client:
        project, [material] = await with_papers(client, longer)
        response = await client.patch(f"/api/materials/{material}", json={"title": title[:990]})  # the researcher's edit
        assert response.status_code == 200, response.text
        provider.script[0] = [call("read_passage", material_id=material)]
        conversation = await conversation_in(client, project)
        run = (await send(client, conversation, "Read it"))[0]["run_id"]
        [(sha,)] = await rows(client, "SELECT sha256 FROM run_bodies WHERE run_id = ? AND attempt >= ?", run,
                              compaction.OUTPUT_SLOT)
        whole = (client.state["db"].data_dir / "content" / sha[:2] / sha).read_text()
        assert len(whole) <= agent.READ_CHARS  # lines, separators and the omission notice counted
        # whole passages only, as many as fit: the room left is less than one more (each about 2,500 characters here)
        assert "Passages left out, by ordinal:" in whole and len(whole) > agent.READ_CHARS - 2600
