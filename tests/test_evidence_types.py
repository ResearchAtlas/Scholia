"""Evidence types (slice-1 spec section 14, test_evidence_types.py; ticket 17 "Evidence records"),
for what S1-13 builds: an uploaded file is full text, and metadata a lookup brings (an abstract
included) never becomes a passage, so abstract-only text is never shown as the paper's full text.
The answer-side checks come with S1-19's evidence records."""

import pytest

import backend.lookup as lookup
import backend.materials as materials_module
import synthetic_materials as synthetic
from scholia_app import MockProvider, MockScholarly, openalex_work, started
from test_materials import added, project_of, rows, settled

pytestmark = pytest.mark.asyncio


@pytest.fixture(autouse=True)
def quick(monkeypatch):
    monkeypatch.setattr(lookup, "SPACING", {source: 0.0 for source in lookup.SPACING})
    monkeypatch.setattr(materials_module, "WAIT_SECONDS", 0.01)


async def test_an_uploaded_file_is_full_text_and_a_looked_up_abstract_adds_no_passage(tmp_path):
    record = {**openalex_work(synthetic.DOI, "Resolved"),
              "abstract_inverted_index": {"Abstract-Only-Canary": [0], "text": [1]}}
    async with started(tmp_path / "data", MockProvider(scholarly=MockScholarly(openalex={synthetic.DOI: record}))) as client:
        project = await project_of(client)
        await added(client, project, ("paper.pdf", synthetic.paper_pdf()))
        [paper] = await settled(client, project)
        assert paper["evidence_type"] == "full_text" and paper["title"] == "Resolved"
        passages = (await client.get(f"/api/material-versions/{paper['version']['id']}/passages")).json()["passages"]
        assert len(passages) == paper["extraction"]["passages"]
        assert not any("Abstract-Only-Canary" in p["text"] for p in passages)
        assert await rows(client, "SELECT count(*) FROM passages WHERE text LIKE '%Abstract-Only-Canary%'") == [(0,)]
        assert "Abstract-Only-Canary" not in str(paper["csl"])  # nor kept as the paper's metadata


async def test_a_scanned_papers_recognized_text_is_its_full_text(tmp_path, monkeypatch):
    from test_ocr import LINE, Engine, scan, use

    use(monkeypatch, Engine())  # S1-20: its page read by a test-owned engine (in this process)
    async with started(tmp_path / "data") as client:
        project = await project_of(client)
        await added(client, project, ("scan.pdf", scan()))
        [paper] = await settled(client, project)
        assert (paper["evidence_type"], paper["state"]) == ("full_text", "ready")
        [passage] = (await client.get(f"/api/material-versions/{paper['version']['id']}/passages")).json()["passages"]
        assert (passage["kind"], passage["text"]) == ("paragraph", LINE.text)


# S1-14: reference files


async def test_a_reference_files_abstract_is_never_a_passage_or_an_index_row(tmp_path):
    from test_reference_import import imported, ris
    async with started(tmp_path / "data") as client:
        project = await project_of(client)
        result = await imported(client, project, ris(("With an Abstract", "AB  - Abstract-Only-Canary text.\n"),
                                                     ("Metadata Only", "PY  - 2020\n")))
        found = {p["title"]: p for p in await settled(client, project)}
        assert found["With an Abstract"]["evidence_type"] == "abstract"  # never full_text
        assert found["Metadata Only"]["evidence_type"] == "metadata"
        assert found["With an Abstract"]["csl"]["abstract"] == "Abstract-Only-Canary text."  # kept as its record's
        assert await rows(client, "SELECT count(*) FROM passages") == [(0,)]
        assert await rows(client, "SELECT count(*) FROM index_queue") == [(0,)]
        assert await rows(client, "SELECT count(*) FROM extractions") == [(0,)]
        version = found["With an Abstract"]["version"]["id"]
        assert (await client.get(f"/api/material-versions/{version}/passages")).json() == {"passages": [], "total": 0}
        assert len(result["imported"]) == 2


async def test_a_file_attached_later_makes_an_imported_paper_full_text(tmp_path):
    from test_reference_import import imported, ris
    async with started(tmp_path / "data") as client:
        project = await project_of(client)
        [paper] = (await imported(client, project, ris(("Attach", "AB  - An abstract.\n"))))["imported"]
        await added(client, project, ("attach.md", b"# Attach\n\nIts full text.\n"), material_id=paper)
        [found] = await settled(client, project)
        assert found["evidence_type"] == "full_text" and found["extraction"]["passages"] > 0


# S1-19: the answer side (C9). Abstracts and metadata are never passages (above), so they never enter a
# request as evidence; every record a request carries says its evidence type, and the chip keeps it.

async def test_an_answers_evidence_says_its_type_and_an_abstract_never_enters_it_as_full_text(tmp_path):
    from cited import cited, conversation_in, with_papers
    from scholia_app import send
    from test_reference_import import imported, ris
    from test_search import WAGES
    async with cited(tmp_path) as client:
        project, _ = await with_papers(client, WAGES)
        await imported(client, project, ris(("Wage Abstract", "AB  - Minimum wages raise earnings Abstract-Only-Canary.\n")))
        await settled(client, project)
        conversation = await conversation_in(client, project)
        await send(client, conversation, "What do minimum wages do to earnings?")
        [body] = client.provider.answers
        evidence = body["messages"][0]["content"].split("## RESEARCH EVIDENCE")[1]
        assert "Abstract-Only-Canary" not in str(body) and "Wage Abstract" not in evidence
        assert "[S1.1] Wage Floors (" in evidence and "full text" in evidence.split("\n")[1]
        [turn] = (await client.get(f"/api/conversations/{conversation}")).json()["turns"]
        assert [c["evidence_type"] for c in turn["citations"]] == ["full_text"]


async def test_an_abstract_or_metadata_record_is_labeled_as_such_never_as_full_text():
    from backend import citations
    for kind, label in (("abstract", "abstract"), ("metadata", "metadata"), ("full_text", "full text")):
        [line] = citations.bundle({(1, 1): {"title": "T", "page": None, "evidence_type": kind, "text": "x"}})
        assert line.startswith(f"[S1.1] T ({label})") and (kind == "full_text" or "full text" not in line)
