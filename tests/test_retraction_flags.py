"""Retraction checks (slice-1 spec sections 7.5 and 14, test_retraction_flags.py): whenever an
identifier resolves through OpenAlex or Crossref, whether the work is retracted is recorded with
when it was checked, over fixture records with known retractions (made up, never real works)."""

import asyncio
import json

import pytest

import backend.lookup as lookup
import backend.materials as materials_module
from scholia_app import MockProvider, MockScholarly, crossref_work, openalex_work, run_finished, started
from test_materials import added, listing, project_of, settled

pytestmark = pytest.mark.asyncio


@pytest.fixture(autouse=True)
def quick(monkeypatch):
    monkeypatch.setattr(lookup, "SPACING", {source: 0.0 for source in lookup.SPACING})
    monkeypatch.setattr(materials_module, "WAIT_SECONDS", 0.01)


def crossref_updated(doi, kind):
    return {**crossref_work(doi, f"Crossref {kind}"), "updated-by": [{"type": kind, "DOI": "10.5555/notice"}]}


FIXTURES = [  # (DOI, OpenAlex record, Crossref record, expected retraction)
    ("10.5555/openalex.retracted", openalex_work("10.5555/openalex.retracted", "OA retracted", retracted=True), None,
     "retracted"),
    ("10.5555/openalex.standing", openalex_work("10.5555/openalex.standing", "OA standing"), None, "none"),
    ("10.5555/crossref.retracted", None, crossref_work("10.5555/crossref.retracted", "CR retracted", retracted=True),
     "retracted"),
    ("10.5555/crossref.withdrawn", None, crossref_updated("10.5555/crossref.withdrawn", "withdrawal"), "retracted"),
    ("10.5555/crossref.corrected", None, crossref_updated("10.5555/crossref.corrected", "correction"), "none"),
    ("10.5555/nowhere", None, None, "unknown"),  # not resolved: nothing recorded
]


async def test_every_resolved_reference_records_its_retraction_and_when_it_was_checked(tmp_path):
    mock = MockScholarly(openalex={d: oa for d, oa, _, _ in FIXTURES if oa},
                         crossref={d: cr for d, _, cr, _ in FIXTURES if cr}, arxiv={"2401.00002": "A preprint"})
    async with started(tmp_path / "data", MockProvider(scholarly=mock)) as client:
        project = await project_of(client)
        files = [(f"{n}.md", f"# Paper {n}\n\ndoi:{doi}\n".encode()) for n, (doi, *_) in enumerate(FIXTURES)]
        await added(client, project, *files, ("preprint.md", b"# Preprint\n\narXiv:2401.00002\n"))
        papers = {p["csl"].get("DOI") or p["source_key"] or p["title"]: p for p in await settled(client, project)}
        for doi, _, _, expected in FIXTURES:
            paper = papers[doi if expected != "unknown" else "5"]  # unresolved: titled by its file name
            assert paper["retraction"] == expected, doi
            assert (paper["retraction_checked_at"] is not None) == (expected != "unknown"), doi
            if expected != "unknown":
                assert paper["retraction_checked_at"] == paper["checked_at"]
        assert papers["arxiv:2401.00002"]["retraction"] == "unknown"  # arXiv says nothing on retraction
        listed = {p["id"]: p["retraction"] for p in (await listing(client, project))["materials"]}
        assert sorted(listed.values()).count("retracted") == 3  # the Library flags each one


@pytest.mark.parametrize("updates", [7, "retraction", None, [7], [{"DOI": "10.5555/notice"}], [{"type": 7}],
                                     [{"type": ""}], [{"type": "  "}]])
async def test_a_later_answer_whose_retraction_cannot_be_read_leaves_the_flag_as_it_was(tmp_path, updates):
    doi = "10.5555/crossref.retracted"
    mock = MockScholarly(crossref={doi: crossref_work(doi, "CR retracted", retracted=True)})
    async with started(tmp_path / "data", MockProvider(scholarly=mock)) as client:
        project = await project_of(client)
        result = await added(client, project, ("paper.md", f"# Paper\n\ndoi:{doi}\n".encode()))
        [flagged] = await settled(client, project)
        assert flagged["retraction"] == "retracted"
        mock.crossref[doi] = {**crossref_work(doi, "CR retracted, again"), "updated-by": updates}  # unreadable here
        await asyncio.to_thread(client.state["db"].write, lambda conn: conn.execute(
            "UPDATE runs SET status = 'failed' WHERE id = ?", (result["lookup_run_id"],)))
        again = await client.post(f"/api/runs/{result['lookup_run_id']}/retry")
        assert (await run_finished(client, again.json()["run_id"]))["status"] == "succeeded"
        [paper] = await settled(client, project)
        assert paper["title"] == "CR retracted, again"  # its readable fields are kept
        assert (paper["retraction"], paper["retraction_checked_at"]) == ("retracted", flagged["retraction_checked_at"])


async def test_a_crossref_record_with_no_updates_is_not_retracted():
    record = crossref_work("10.5555/x", "Standing")
    assert "updated-by" not in record and lookup._crossref("10.5555/x", json.dumps({"message": record}).encode()).retracted is False


async def test_a_reference_files_record_whose_doi_is_retracted_is_flagged(tmp_path):
    # S1-14: a fixture reference with a known retraction, imported from a reference file.
    from test_reference_import import imported, ris
    doi = "10.5555/reference.retracted"
    mock = MockScholarly(openalex={doi: openalex_work(doi, "Retracted Work", retracted=True)})
    async with started(tmp_path / "data", MockProvider(scholarly=mock)) as client:
        project = await project_of(client)
        await imported(client, project, ris(("A Retracted Reference", f"DO  - {doi}\n"), ("Standing", "PY  - 2020\n")))
        found = {p["title"]: p for p in await settled(client, project)}
        flagged = found["A Retracted Reference"]
        assert (flagged["retraction"], bool(flagged["retraction_checked_at"])) == ("retracted", True)
        assert found["Standing"]["retraction"] == "unknown"  # no identifier: never looked up


# S1-19: a cited paper recorded as retracted is flagged in the answer and its chip (C10)

async def test_a_cited_paper_recorded_as_retracted_is_flagged_in_the_evidence_the_answer_and_its_chip(tmp_path):
    from cited import cited, conversation_in, with_papers
    from scholia_app import send
    from test_search import WAGES
    async with cited(tmp_path) as client:
        project, [material] = await with_papers(client, WAGES)
        await asyncio.to_thread(client.state["db"].write, lambda conn: conn.execute(
            "UPDATE materials SET retraction = 'retracted' WHERE id = ?", (material,)))
        conversation = await conversation_in(client, project)
        stream = await send(client, conversation, "What do minimum wages do to earnings?")
        [body] = client.provider.answers
        assert "[S1.1] Wage Floors (" in body["messages"][0]["content"]
        assert "RETRACTED)" in body["messages"][0]["content"].split("[S1.1] Wage Floors (")[1].split("\n")[0]
        [response] = [e for e in stream if e["type"] == "chat_response"]
        assert "retracted" in response["notices"]
        [turn] = (await client.get(f"/api/conversations/{conversation}")).json()["turns"]
        assert "retracted" in turn["answer"]["notices"] and [c["retracted"] for c in turn["citations"]] == [True]


async def test_a_paper_that_is_not_retracted_carries_no_flag(tmp_path):
    from cited import cited, conversation_in, with_papers
    from scholia_app import send
    from test_search import WAGES
    async with cited(tmp_path) as client:
        project, _ = await with_papers(client, WAGES)
        conversation = await conversation_in(client, project)
        await send(client, conversation, "What do minimum wages do to earnings?")
        [body] = client.provider.answers
        assert "RETRACTED" not in body["messages"][0]["content"].split("## RESEARCH EVIDENCE")[1]
        [turn] = (await client.get(f"/api/conversations/{conversation}")).json()["turns"]
        assert "retracted" not in turn["answer"].get("notices", []) and [c["retracted"] for c in turn["citations"]] == [False]
