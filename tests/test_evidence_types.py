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
