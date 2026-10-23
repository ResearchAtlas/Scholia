"""Paged tool outputs (S1-21b; slice-1 spec sections 8, Paging, and 13: an 8,000-character preview with a handle;
checklist O1, O2, I5, I6): a tool's output past the preview goes whole to the content store, the model gets the preview
and a handle, and read_output returns the next part; a passage counts as read only once a page showing its marker is
served; a handle is valid only in its own turn, and the kept output goes with its paper. Recorded tool-calling replies
(tests/agentic.py); nothing leaves the process."""

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


async def test_a_result_over_its_tools_own_bound_is_cut_there():
    registry = ToolRegistry()

    @registry.register("big", "A big tool.", {"type": "object", "properties": {}}, read_only=True, repeat_safe=True,
                       network=False, output_chars=10, tool_class="read_library", spends=False)
    async def big(call, *context):
        return ToolResult(id=call.id, ok=True, text="x" * 50)
    result = await ToolRouter(registry).execute(ToolCall(id="a", name="big"))
    assert result.text.startswith("x" * 10 + "\n[Cut at 10") and result.meta["cut"]


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


async def test_review_1_a_tool_bounded_within_a_page_is_cut_there_never_paged(tmp_path, monkeypatch):
    # Review 1 (sonnet P3-1): a handle would lead only to the cut note.
    async def big(call, turn):
        return ToolResult(id=call.id, ok=True, text="y" * 9000)
    monkeypatch.setitem(agent.REGISTRY._implementations, "list_materials", big)
    provider = Scripted([call("list_materials")], "Done.")
    async with cited(tmp_path, provider) as client:
        project, _ = await with_papers(client, LONG)
        conversation = await conversation_in(client, project)
        await send(client, conversation, "List them")
        [listed] = tool_messages(provider.agent_steps[-1])
        assert "[Cut at 8000 characters" in listed["content"] and "read_output" not in listed["content"]


async def test_review_1_a_passage_counts_as_shown_only_with_its_whole_marker_on_the_page():
    assert compaction.marked(compaction.PREVIEW - 6, (1, 2), 0)  # "[S1.2]" ends at the page's end
    assert not compaction.marked(compaction.PREVIEW - 5, (1, 2), 0)  # cut by the boundary
    # Recheck 1 (P3-1): completed by the page the footer names, so sequential paging reads it
    assert compaction.marked(compaction.PREVIEW - 5, (1, 2), compaction.PREVIEW)
    assert not compaction.marked(10, (1, 2), 20)  # before the page
