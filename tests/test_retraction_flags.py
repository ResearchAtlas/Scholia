"""Retraction checks (slice-1 spec sections 7.5 and 14, test_retraction_flags.py): whenever an
identifier resolves through OpenAlex or Crossref, whether the work is retracted is recorded with
when it was checked, over fixture records with known retractions (made up, never real works)."""

import pytest

import backend.lookup as lookup
import backend.materials as materials_module
from scholia_app import MockProvider, MockScholarly, crossref_work, openalex_work, started
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
