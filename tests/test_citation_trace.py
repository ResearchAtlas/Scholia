"""Cited answers (slice-1 spec F4, sections 7.4, 7.5 and 14's test_citation_trace.py row; tickets 17
and 71): the evidence a turn carries, each citation recorded and mapped to a passage read in the run,
existence checked locally, the trace, and the citation endpoints. Model calls go to a test-owned mock
and the local helper is S1-16's stand-in; nothing leaves the process."""

import asyncio
import json

import pytest

from backend import citations
from cited import ANSWER, Provider, cited, conversation_in, events_of, rows, with_papers
from scholia_app import run_finished, send
from test_search import WAGES, paper

pytestmark = pytest.mark.asyncio
QUESTION = "What do minimum wages do to earnings?"


async def test_a_cited_answer_is_recorded_with_every_citation_mapped_to_a_passage_read_in_the_run(tmp_path):
    async with cited(tmp_path) as client:
        project, [material] = await with_papers(client, WAGES)
        conversation = await conversation_in(client, project)
        stream = await send(client, conversation, QUESTION)
        assert stream[-1]["status"] == "succeeded"
        [body] = client.provider.answers
        system = body["messages"][0]["content"]
        assert "## RESEARCH EVIDENCE\n[S1.1] Wage Floors (" in system and "full text" in system
        assert "a paper reports" in system or "what that paper reports" in system  # claim wording (ticket 17)
        run_id = stream[0]["run_id"]
        [turn] = (await client.get(f"/api/conversations/{conversation}")).json()["turns"]
        [chip] = turn["citations"]
        assert (chip["existence"], chip["support"], chip["material_id"], chip["marker"]) == (
            "ok", "supported", material, "[S1.1]")
        events = await events_of(client, run_id)
        [trace] = [d for kind, d, _ in events if kind == "retrieval_trace"]
        assert chip["passage_id"] in trace["selected"]  # read in the run
        [checks] = [d for kind, d, _ in events if kind == "citation_checks"]
        assert checks["citations"][0]["support"] == "supported"
        assert turn["answer"]["text"] == ANSWER and chip["sentence"] == ANSWER  # stored exactly as the model wrote it


async def test_a_marker_naming_no_record_or_one_that_does_not_parse_is_not_found(tmp_path):
    provider = Provider(answer_text="Wages rise [S1.1]. Prices fall [S4.2]. Unclear [S1].")
    async with cited(tmp_path, provider) as client:
        project, _ = await with_papers(client, WAGES)
        conversation = await conversation_in(client, project)
        await send(client, conversation, QUESTION)
        [turn] = (await client.get(f"/api/conversations/{conversation}")).json()["turns"]
        assert [(c["marker"], c["existence"], c["support"]) for c in turn["citations"]] == [
            ("[S1.1]", "ok", "supported"), ("[S4.2]", "not_found", "unchecked"), ("[S1]", "not_found", "unchecked")]
        [judged] = provider.judged
        assert judged["messages"][-1]["content"].count("Pair ") == 1  # only the citation that exists is judged


async def test_a_citation_whose_passage_was_re_read_maps_by_its_quote_or_is_source_removed(tmp_path):
    async with cited(tmp_path) as client:
        project, [material] = await with_papers(client, WAGES)
        db = client.state["db"]
        record = await asyncio.to_thread(db.read, lambda conn: next(
            {"passage_id": pid, "material_id": r[0], "version_id": r[7], "title": r[1], "text": r[4], "page": r[5],
             "evidence_type": "full_text"}
            for pid, r in citations.readings(conn, project, references=False).items()))
        gone = {**record, "passage_id": "00000000-0000-4000-8000-000000000000"}  # a passage no longer read
        [held] = await asyncio.to_thread(db.read, lambda conn: citations.check_existence(
            conn, project, "It rises [S1.1].", {(1, 1): gone}))
        assert (held["existence"], held["passage_id"]) == ("ok", record["passage_id"])  # a current reading holds its quote
        [removed] = await asyncio.to_thread(db.read, lambda conn: citations.check_existence(
            conn, project, "It rises [S1.1].", {(1, 1): {**gone, "text": "No reading holds this quote."}}))
        assert (removed["existence"], removed["passage_id"]) == ("source_removed", None)


async def test_selected_papers_that_fit_are_read_whole_and_larger_ones_are_searched_within(tmp_path):
    other = paper("Rents", "Housing rents rose in the synthetic city.")
    async with cited(tmp_path) as client:
        project, [wages, rents] = await with_papers(client, WAGES, other)
        conversation = await conversation_in(client, project)
        response = await client.post(f"/api/conversations/{conversation}/message/stream",
                                     json={"content": "Summarize this paper.", "material_ids": [rents]})
        assert response.status_code == 200
        [body] = client.provider.answers
        system = body["messages"][0]["content"]
        assert "Housing rents rose" in system and "Minimum wages" not in system  # only the selection, read whole
        bad = await client.post(f"/api/conversations/{conversation}/message/stream",
                                json={"content": "Summarize.", "material_ids": ["00000000-0000-4000-8000-000000000000"]})
        assert (bad.status_code, bad.json()["code"]) == (400, "selection_invalid")


async def test_an_answer_with_no_evidence_states_it_and_a_keyword_only_search_says_so(tmp_path):
    # Without the search model, search is keyword-only and says so; a question no keyword matches finds
    # nothing, and the request says the library holds no evidence (abstention wording, not filler).
    provider = Provider(answer_text="The library holds no evidence on this.")
    async with cited(tmp_path, provider, install=False) as client:
        project, _ = await with_papers(client, WAGES)
        conversation = await conversation_in(client, project)
        stream = await send(client, conversation, "Quasars?")
        [body] = provider.answers
        system = body["messages"][0]["content"]
        assert "No passage in the researcher's library matched this question" in system
        assert "keyword search only" in system and "## RESEARCH EVIDENCE" not in system
        [turn] = (await client.get(f"/api/conversations/{conversation}")).json()["turns"]
        assert "keyword_only" in turn["answer"]["notices"] and stream[-1]["status"] == "succeeded"
        assert turn["citations"] == [] and provider.judged == []  # nothing cited, nothing to judge


async def test_the_citation_endpoints_resolve_existence_and_check_support_as_a_run(tmp_path):
    async with cited(tmp_path) as client:
        project, [material] = await with_papers(client, WAGES)
        conversation = await conversation_in(client, project)
        stream = await send(client, conversation, QUESTION)
        [turn] = (await client.get(f"/api/conversations/{conversation}")).json()["turns"]
        [chip] = turn["citations"]
        resolved = (await client.post("/api/citations/resolve", json={"citation_ids": [chip["id"]]})).json()
        assert resolved == {"citations": [{"id": chip["id"], "existence": "ok"}]}
        client.provider.verdicts = "1: partly"
        started_check = await client.post("/api/citations/check", json={"run_id": stream[0]["run_id"]})
        assert started_check.status_code == 202
        done = await run_finished(client, started_check.json()["run_id"])
        assert done["status"] == "succeeded" and done["workflow"] == "citation_check"
        assert await rows(client, "SELECT support FROM citations WHERE id = ?", chip["id"]) == [("partly",)]
        [listed] = [r for r in (await client.get("/api/activity")).json()["runs"] if r["run_id"] == done["run_id"]]
        assert listed["workflow"] == "citation_check" and listed["cost_usd"] > 0  # listed with its cost


async def test_a_route_without_tools_is_chat_with_the_library_and_says_skills_are_unavailable(tmp_path):
    # N1: no reachable endpoint of the route takes tool calls: the router's pre-load, one answer call with
    # no tools, the delivered-answer checks, and the notice.
    from backend import budget_router
    plain = [{"tag": "example", "context_length": 128000, "supported_parameters": ["max_tokens", "temperature"]}]
    provider = Provider(endpoints={m: plain for m in budget_router.RECOMMENDED})
    async with cited(tmp_path, provider) as client:
        project, _ = await with_papers(client, WAGES)
        conversation = await conversation_in(client, project)
        stream = await send(client, conversation, QUESTION)
        [body] = provider.answers
        assert "tools" not in body and "tool_choice" not in body
        assert "## RESEARCH EVIDENCE\n[S1.1]" in body["messages"][0]["content"]  # the pre-load
        [response] = [e for e in stream if e["type"] == "chat_response"]
        assert "skills_unavailable" in response["notices"]
        [turn] = (await client.get(f"/api/conversations/{conversation}")).json()["turns"]
        assert "skills_unavailable" in turn["answer"]["notices"]
        assert [(c["existence"], c["support"]) for c in turn["citations"]] == [("ok", "supported")]
        assert len(provider.judged) == 1


async def test_a_route_whose_endpoints_all_take_tools_gives_no_skills_notice(tmp_path):
    async with cited(tmp_path) as client:
        project, _ = await with_papers(client, WAGES)
        conversation = await conversation_in(client, project)
        await send(client, conversation, QUESTION)
        [turn] = (await client.get(f"/api/conversations/{conversation}")).json()["turns"]
        assert "skills_unavailable" not in turn["answer"].get("notices", [])


async def test_a_citation_inside_a_table_list_or_code_span_is_found_and_checked(tmp_path):
    # G3: a marker in Markdown structure is still a marker, at its offset in the text as written.
    text = ("| Effect | Source |\n|---|---|\n| Earnings rise | [S1.1] |\n\n- Employment barely moves [S1.2]\n\n"
            "Written `[S1.1]` in code.")
    provider = Provider(answer_text=text)
    async with cited(tmp_path, provider) as client:
        project, _ = await with_papers(client, WAGES)
        conversation = await conversation_in(client, project)
        await send(client, conversation, QUESTION)
        [turn] = (await client.get(f"/api/conversations/{conversation}")).json()["turns"]
        assert [(c["marker"], c["existence"]) for c in turn["citations"]] == [
            ("[S1.1]", "ok"), ("[S1.2]", "ok"), ("[S1.1]", "ok")]
        assert [text[c["offset"]:c["offset"] + 6] for c in turn["citations"]] == ["[S1.1]", "[S1.2]", "[S1.1]"]
        assert turn["answer"]["text"] == text


async def test_a_point_inside_a_marker_or_a_number_does_not_end_a_sentence():
    text = "Earnings rose 3.5 percent [S1.1][S1.2]. Employment barely moved [S1.2]! 该论文报告 [S1.1]。"
    assert [s.strip() for _, s in citations.sentences(text)] == [
        "Earnings rose 3.5 percent [S1.1][S1.2].", "Employment barely moved [S1.2]!", "该论文报告 [S1.1]。"]
