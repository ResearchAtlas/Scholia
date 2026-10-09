"""The query check's identifier rule (slice-1 spec section 14, test_query_screen.py; F3a step 3,
"Resolving an identifier is not a search"): a DOI read from an uploaded file is resolved, and one
found only in model text or memory is never sent. The rest of this file's rows (search queries,
the Private one-time approval, memory fragments in queries) come with S1-27's find_papers."""

import asyncio
import re
from pathlib import Path

import pytest

import backend.lookup as lookup
import backend.materials as materials_module
import synthetic_materials as synthetic
from backend.db import new_id
from scholia_app import MockProvider, MockScholarly, openalex_work, send, started
from test_materials import added, project_of, settled

pytestmark = pytest.mark.asyncio

MODEL_DOI = "10.5555/said.by.the.model"
MEMORY_DOI = "10.5555/kept.in.memory"


@pytest.fixture(autouse=True)
def quick(monkeypatch):
    monkeypatch.setattr(lookup, "SPACING", {source: 0.0 for source in lookup.SPACING})
    monkeypatch.setattr(materials_module, "WAIT_SECONDS", 0.01)


async def test_a_doi_read_from_an_uploaded_file_is_resolved(tmp_path):
    mock = MockScholarly(openalex={synthetic.DOI: openalex_work(synthetic.DOI, "Resolved")})
    async with started(tmp_path / "data", MockProvider(scholarly=mock)) as client:
        project = await project_of(client)
        await added(client, project, ("paper.pdf", synthetic.paper_pdf()))
        [paper] = await settled(client, project)
        assert paper["title"] == "Resolved"
        assert [path for _, path, _ in mock.requests] == [f"/works/doi:{synthetic.DOI}"]


async def test_a_doi_found_only_in_model_text_or_memory_is_never_resolved(tmp_path):
    mock = MockScholarly(openalex={MODEL_DOI: openalex_work(MODEL_DOI, "No"), MEMORY_DOI: openalex_work(MEMORY_DOI, "No")})
    provider = MockProvider(scholarly=mock)
    provider.replies = [provider.answer(f"You might read doi:{MODEL_DOI} on this.")]
    async with started(tmp_path / "data", provider) as client:
        project = await project_of(client)
        conversation = (await client.post("/api/conversations", json={"project_id": project})).json()["id"]
        await send(client, conversation, "What should I read?")
        await asyncio.to_thread(client.state["db"].write, lambda conn: conn.execute(
            "INSERT INTO memory_records (id, scope, project_id, type, content, status) VALUES (?, 'project', ?, 'fact',"
            " ?, 'confirmed')", (new_id(), project, f"Harold mentioned doi:{MEMORY_DOI}")))
        await added(client, project, ("notes.md", b"# Notes\n\nNo identifier of its own.\n"))
        [paper] = await settled(client, project)
        assert paper["lookup"]["outcome"] == "no_identifier"
        assert mock.requests == []


async def test_identifiers_reach_a_lookup_only_from_a_materials_own_text():
    # The one call of lookup.resolve takes what materials._identifiers read from the material's passages.
    backend = Path(__file__).resolve().parents[1] / "backend"
    callers = {path.name: len(re.findall(r"lookup\.resolve\(", path.read_text(encoding="utf-8")))
               for path in backend.rglob("*.py")}
    assert {name: n for name, n in callers.items() if n} == {"materials.py": 1}
    source = (backend / "materials.py").read_text(encoding="utf-8")
    assert "found = _identifiers(conn, project_id, materials, versions)" in source
