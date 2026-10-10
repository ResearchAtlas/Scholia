"""Deleted sources stay out (slice-1 spec section 14, test_deletion_deny_first.py), for what S1-13
builds: once a material is deleted, nothing of it is read back (its record, versions, passages and
page images), its passages leave its project's index after they were added, and a file shared with
another project stays there. Deletion while a material is read or looked up is in test_materials.py
and test_identifier_lookup.py; memory and the index's own checks come with S1-17 and S1-21."""

import asyncio

import pytest

import synthetic_materials as synthetic
from scholia_app import started
from test_materials import added, kept_queue, project_of, rows, settled  # noqa: F401

pytestmark = pytest.mark.asyncio


async def test_a_deleted_material_is_read_back_nowhere_and_leaves_its_projects_index(tmp_path, kept_queue):
    async with started(tmp_path / "data") as client:
        mine, theirs = await project_of(client, "Mine"), await project_of(client, "Theirs")
        file = ("paper.pdf", synthetic.paper_pdf())  # one file: each PDF made is a new document
        [paper] = (await added(client, mine, file))["materials"]
        await added(client, theirs, file)
        [ready] = await settled(client, mine)
        [kept] = await settled(client, theirs)
        version = ready["version"]["id"]
        passages = (await client.get(f"/api/material-versions/{version}/passages")).json()["passages"]
        assert (await client.delete(f"/api/materials/{paper['id']}")).status_code == 200
        for path in (f"/api/materials/{paper['id']}", f"/api/materials/{paper['id']}/versions",
                     f"/api/material-versions/{version}/passages", f"/api/material-versions/{version}/pages/1"):
            assert (await client.get(path)).status_code == 404, path
        listed = await client.get(f"/api/projects/{mine}/materials")
        assert listed.json()["materials"] == [], listed.text
        removed = await rows(client, "SELECT project_id, count(*) FROM index_queue WHERE op = 'remove' GROUP BY 1")
        assert removed == [(mine, len(passages))]  # only its own project's rows; the other keeps the shared file
        # The shared extraction is the other project's still: readable there.
        assert (await client.get(f"/api/material-versions/{kept['version']['id']}/passages")).json()["total"] == len(passages)
        one = (await client.get(f"/api/passages/{passages[0]['id']}")).json()
        assert [m["project_id"] for m in one["materials"]] == [theirs]



@pytest.mark.parametrize("deleted", ["markdown", "latex"])
async def test_deleting_one_reading_of_a_file_read_two_ways_removes_its_passages_only(tmp_path, deleted, kept_queue):
    source = b"\\section{Method}\n\nText with \\emph{emphasis} here.\n"  # Markdown and LaTeX alike
    async with started(tmp_path / "data") as client:
        project = await project_of(client)
        [as_markdown] = (await added(client, project, ("notes.md", source)))["materials"]
        [as_latex] = (await added(client, project, ("other.md", b"# Other\n\nAnother file entirely.\n")))["materials"]
        await settled(client, project)
        await added(client, project, ("notes.tex", source), material_id=as_latex["id"])  # Replace file: the same bytes
        await settled(client, project)
        readings = dict(await rows(client, "SELECT e.extractor, e.id FROM extractions e JOIN material_versions v"
                                           " ON v.file_sha256 = e.file_sha256 WHERE v.material_id = ?", as_markdown["id"]))
        assert set(readings) == {"markdown", "latex"}  # one stored file, read two ways
        passages = {name: {p for (p,) in await rows(client, "SELECT id FROM passages WHERE extraction_id = ?", extraction)}
                    for name, extraction in readings.items()}
        kept_reading = "latex" if deleted == "markdown" else "markdown"
        survivor = as_latex if deleted == "markdown" else as_markdown
        target = as_markdown if deleted == "markdown" else as_latex
        assert (await client.delete(f"/api/materials/{target['id']}")).status_code == 200
        removed = {t for (t,) in await rows(client, "SELECT target_id FROM index_queue WHERE op = 'remove'")}
        # By what was deleted, not by the file: the deleted reading's passages leave the index, the other's stay.
        assert passages[deleted] <= removed and not passages[kept_reading] & removed
        # Its extraction goes with its last reader; the other reading is the survivor's still.
        assert await rows(client, "SELECT extractor FROM extractions WHERE id IN (?, ?)", *readings.values()) == [
            (kept_reading,)]
        assert await rows(client, "SELECT count(*) FROM passages WHERE extraction_id = ?", readings[deleted]) == [(0,)]
        [current] = (await client.get(f"/api/projects/{project}/materials")).json()["materials"]
        texts = (await client.get(f"/api/material-versions/{current['version']['id']}/passages")).json()["passages"]
        assert current["id"] == survivor["id"] and {p["id"] for p in texts} == passages[kept_reading]


async def test_a_stale_index_never_surfaces_a_deleted_or_replaced_paper(tmp_path, monkeypatch):
    """S1-17: every search checks access again in the main database (slice-1 spec 4.3): passages the index
    still holds, its removals not applied yet, are never returned."""
    from backend.search_index import SearchIndex
    from test_search import app, find, idle, index_rows, paper

    async with app(tmp_path, install=False) as client:
        project = await project_of(client)
        [deleted] = (await added(client, project, paper("Deleted", "The aubergine finding.")))["materials"]
        [replaced] = (await added(client, project, paper("Replaced", "The obsolete quince finding.")))["materials"]
        await idle(client, project)
        monkeypatch.setattr(SearchIndex, "_apply", lambda self, stop=None: 0)  # the index applies nothing from here
        assert (await client.delete(f"/api/materials/{deleted['id']}")).status_code == 200
        await added(client, project, paper("Replaced", "The current finding."), material_id=replaced["id"])
        await idle(client, project)
        texts = {r[0] for r in await index_rows(client, project)}
        assert len(texts) == 4  # stale: still the old rows
        for query in ("aubergine", "quince", "deleted", "obsolete"):
            assert (await find(client, project, query))["results"] == [], query


async def test_a_deleted_scanned_papers_recognized_passages_are_read_back_nowhere_and_leave_its_index(tmp_path, monkeypatch,
                                                                                                         kept_queue):
    from backend import ocr
    from test_ocr import Engine, scan

    monkeypatch.setattr(ocr, "engine", lambda: Engine())  # S1-20: its page read by a test-owned engine
    async with started(tmp_path / "data") as client:
        project = await project_of(client)
        [paper] = (await added(client, project, ("scan.pdf", scan())))["materials"]
        [ready] = await settled(client, project)
        version = ready["version"]["id"]
        [passage] = (await client.get(f"/api/material-versions/{version}/passages")).json()["passages"]
        assert passage["boxes"]["ocr"]
        await asyncio.to_thread(client.state["index"].apply)
        assert await searched(client, project, "Recognized text of a scanned page") == {passage["id"]}  # found before
        assert (await client.delete(f"/api/materials/{paper['id']}")).status_code == 200
        for path in (f"/api/material-versions/{version}/passages", f"/api/material-versions/{version}/pages/1",
                     f"/api/passages/{passage['id']}"):
            assert (await client.get(path)).status_code == 404, path
        assert await rows(client, "SELECT op FROM index_queue WHERE target_id = ? ORDER BY seq", passage["id"]) == [
            ("add",), ("remove",)]
        assert await rows(client, "SELECT count(*) FROM passages") == [(0,)]
        # S1-17: search reads it back nowhere either, once the index has applied its queue (keyword search:
        # this test has no helper)
        await asyncio.to_thread(client.state["index"].apply)
        assert await searched(client, project, "Recognized text of a scanned page") == set()


async def searched(client, project, query):
    """The passage ids the project's search returns for query (S1-17's API)."""
    response = await client.post(f"/api/projects/{project}/search", json={"query": query, "limit": 50})
    assert response.status_code == 200, response.text
    return {r["passage_id"] for r in response.json()["results"]}


async def test_a_superseded_reading_is_removed_through_the_deletion_service_and_read_back_nowhere(tmp_path, monkeypatch,
                                                                                                    kept_queue):
    """S1-20: a reading another reading of its file replaced leaves each index it was in, its passages
    are read back nowhere, and its removal leaves a tombstone (section 4.2) and no dangling reference."""
    import backend.extraction as extraction
    from test_ocr import cite
    from scholia_app import run_finished

    notes = b"# Notes\n\nA paragraph of synthetic text.\n"
    async with started(tmp_path / "data") as client:
        mine, theirs = await project_of(client, "Mine"), await project_of(client, "Theirs")
        monkeypatch.setitem(extraction.EXTRACTORS, extraction.MARKDOWN, ("markdown", "markdown-0"))
        [paper] = (await added(client, mine, ("notes.md", notes)))["materials"]
        [read] = await settled(client, mine)
        await added(client, theirs, ("notes.md", notes))
        await settled(client, theirs)
        [(older,)] = await rows(client, "SELECT id FROM extractions")
        earlier = [p for (p,) in await rows(client, "SELECT id FROM passages WHERE extraction_id = ?", older)]
        await cite(client, paper["id"], earlier[0], "synthetic text")
        monkeypatch.setitem(extraction.EXTRACTORS, extraction.MARKDOWN, ("markdown", "markdown-1"))
        again = await client.post(f"/api/material-versions/{read['version']['id']}/read")
        assert (await run_finished(client, again.json()["run_id"]))["status"] == "succeeded"
        for passage in earlier:
            assert (await client.get(f"/api/passages/{passage}")).status_code == 404
            assert {p for (_, p, op) in await rows(client, "SELECT target_id, project_id, op FROM index_queue"
                                                          " WHERE target_id = ? AND op = 'remove'", passage)} == {mine, theirs}
        assert await rows(client, "SELECT count(*) FROM passages WHERE extraction_id = ?", older) == [(0,)]
        assert await rows(client, "SELECT object_id, kind, title FROM tombstones") == [(older, "reading", None)]
        assert await rows(client, "SELECT count(*) FROM audit_log WHERE event = 'deletion'") == [(0,)]
        assert await rows(client, "SELECT count(*) FROM citations c LEFT JOIN passages p ON p.id = c.passage_id"
                                  " WHERE c.passage_id IS NOT NULL AND p.id IS NULL") == [(0,)]
        # S1-17: search reads the superseded passages back nowhere, once the index has applied its queue
        # (keyword search: this test has no helper); each project finds the newer reading's instead.
        await asyncio.to_thread(client.state["index"].apply)
        newer = {p for (p,) in await rows(client, "SELECT id FROM passages")}
        for project in (mine, theirs):
            found = await searched(client, project, "A paragraph of synthetic text")
            assert found and not found & set(earlier) and found <= newer

