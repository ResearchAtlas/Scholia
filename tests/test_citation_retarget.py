"""Citations after a paper's reading changes (design review J, Option 1, Harold 2026-11-14; section 7.1): one chooser
(citations.locate) and one recomputation by paper (citations.retarget) at every reading change. Each scenario is a
finding of PR 58's cycles or of design review J (its cases L, M and N). Synthetic papers, the tests' stand-in helper
and the network guard only."""

import asyncio

import pytest

from backend import citations, extraction
from backend.db import new_id, utc_now
from cited import cited, conversation_in, rows, with_papers
from scholia_app import read_in_process, run_finished, send
from test_materials import added
from test_search import WAGES, idle, paper

pytestmark = pytest.mark.asyncio
QUESTION = "What do minimum wages do to earnings?"
OLD = "Minimum wages raise the earnings of low-paid workers in the synthetic panel."
REFERENCED = f"# Wage Floors\n\n## Findings\n\n{OLD} Rents rose too.\n\n## References\n\n{OLD}\n".encode()


async def chip_of(client, conversation):
    [chip] = (await client.get(f"/api/conversations/{conversation}")).json()["turns"][0]["citations"]
    return chip


async def evidence(client, project):
    return await asyncio.to_thread(client.state["db"].read, lambda conn: citations.readings(conn, project, references=False))


async def read_as(client, monkeypatch, material, name):
    """The paper's file read again by another version of the Markdown reader (an app update's re-read)."""
    monkeypatch.setitem(extraction.EXTRACTORS, extraction.MARKDOWN, ("markdown", name))
    [(version,)] = await rows(client, "SELECT id FROM material_versions WHERE material_id = ? AND is_current = 1", material)
    again = await client.post(f"/api/material-versions/{version}/read")
    assert (await run_finished(client, again.json()["run_id"]))["status"] == "succeeded"


def rewording(monkeypatch, real, words):
    def extract(data, kind, stop=lambda: None, progress=lambda d, t: None):
        found = real(data, kind, stop, progress)
        for passage in found.passages:
            for old, new in words.items():
                passage.text = passage.text.replace(old, new)
        return found
    monkeypatch.setattr(extraction, "extract", extract)


async def test_a_reread_never_points_a_citation_at_a_reference_passage(tmp_path, monkeypatch):
    # rounds 1 to 3, the open row: supersede and the null-passage loop without the reference filter
    read_in_process(monkeypatch)
    real = extraction.extract
    async with cited(tmp_path) as client:
        monkeypatch.setitem(extraction.EXTRACTORS, extraction.MARKDOWN, ("markdown", "markdown-0"))
        rewording(monkeypatch, real, {" Rents rose too.": ""})  # the earlier reader gave the body passage as OLD
        project, [material] = await with_papers(client, ("Wage Floors.md", REFERENCED))
        conversation = await conversation_in(client, project)
        await send(client, conversation, QUESTION)
        before = await chip_of(client, conversation)
        assert (before["existence"], before["support"], before["quote"]) == ("ok", "supported", OLD)
        monkeypatch.setattr(extraction, "extract", real)
        await read_as(client, monkeypatch, material, "markdown-1")  # the body passage now says more; References holds OLD
        await idle(client, project)
        chip, read_now = await chip_of(client, conversation), await evidence(client, project)
        assert chip["passage_id"] in read_now, await rows(client, "SELECT c.existence, p.kind, p.text FROM citations c"
                                                                " LEFT JOIN passages p ON p.id = c.passage_id")
        assert (chip["existence"], chip["support"]) == ("ok", "unchecked")


async def test_a_reread_takes_the_new_passages_page(tmp_path, monkeypatch):
    # round 3, the open row: page = coalesce(new, old) in supersede and the null-passage loop
    read_in_process(monkeypatch)
    async with cited(tmp_path) as client:
        monkeypatch.setitem(extraction.EXTRACTORS, extraction.MARKDOWN, ("markdown", "markdown-0"))
        project, [material] = await with_papers(client, WAGES)
        conversation = await conversation_in(client, project)
        await send(client, conversation, QUESTION)
        await asyncio.to_thread(client.state["db"].write, lambda conn: conn.execute("UPDATE citations SET page = 7"))
        await read_as(client, monkeypatch, material, "markdown-1")
        await idle(client, project)
        chip = await chip_of(client, conversation)
        assert (chip["existence"], chip["support"], chip["page"]) == ("ok", "supported", None)


async def test_a_citation_a_reread_lost_is_found_again_by_a_different_replacement(tmp_path, monkeypatch):
    # round 2 pass 1 finding 3, the open row: the null-passage loop goes by the citation's old version's file
    read_in_process(monkeypatch)
    real = extraction.extract
    async with cited(tmp_path) as client:
        monkeypatch.setitem(extraction.EXTRACTORS, extraction.MARKDOWN, ("markdown", "markdown-0"))
        project, [material] = await with_papers(client, WAGES)
        conversation = await conversation_in(client, project)
        await send(client, conversation, QUESTION)
        rewording(monkeypatch, real, {"raise the earnings": "lift the pay"})
        await read_as(client, monkeypatch, material, "markdown-1")  # this reading words the quote otherwise
        await idle(client, project)
        assert await rows(client, "SELECT passage_id, existence FROM citations") == [(None, "not_found")]  # S1-20 (3)
        monkeypatch.setattr(extraction, "extract", real)
        await added(client, project, paper("Wage Floors", "A new opening paragraph.", OLD), material_id=material)
        await idle(client, project)
        chip, read_now = await chip_of(client, conversation), await evidence(client, project)
        assert chip["existence"] == "ok" and chip["passage_id"] in read_now and OLD in read_now[chip["passage_id"]][4]


async def test_a_citation_stored_removed_with_no_passage_is_found_again_by_a_later_reading(tmp_path):
    # this review's: an answer committed after its passage went and while no reading held its quote is stored
    # source_removed with its paper and quote and no passage (check_existence: "so a later reading holding it can
    # resolve it"); no loop takes that row
    async with cited(tmp_path) as client:
        project, [material] = await with_papers(client, WAGES)
        conversation = await conversation_in(client, project)
        run_id = (await send(client, conversation, QUESTION))[0]["run_id"]
        await added(client, project, paper("Wage Floors", "Rents rose in the synthetic city."), material_id=material)
        await idle(client, project)

        def commit(conn):  # what the answer's commit writes then, by the product's own functions
            [(version,)] = conn.execute("SELECT material_version_id FROM citations").fetchall()
            conn.execute("DELETE FROM citations")
            [row] = citations.check_existence(conn, project, "It rises [S1.1].", {(1, 1): {
                "material_id": material, "version_id": version, "passage_id": new_id(), "text": OLD, "page": None,
                "evidence_type": "full_text", "title": "Wage Floors"}})
            citations.write_rows(conn, run_id, [{**row, "id": new_id()}], utc_now())
        await asyncio.to_thread(client.state["db"].write, commit)
        assert await rows(client, "SELECT existence, passage_id, material_id, quote FROM citations") == [
            ("source_removed", None, material, OLD)]
        await added(client, project, paper("Wage Floors", "A new opening paragraph.", OLD), material_id=material)
        await idle(client, project)
        chip, read_now = await chip_of(client, conversation), await evidence(client, project)
        resolved = (await client.post("/api/citations/resolve", json={"citation_ids": [chip["id"]]})).json()
        assert (chip["existence"], resolved["citations"][0]["existence"]) == ("ok", "ok")
        assert chip["passage_id"] in read_now


async def test_a_citation_without_its_quote_follows_a_reread_by_its_passages_own_text(tmp_path, monkeypatch):
    # kept behavior (S1-20 (3), with option A's cleared quotes): a row with no quote still follows its passage's text
    read_in_process(monkeypatch)
    async with cited(tmp_path) as client:
        monkeypatch.setitem(extraction.EXTRACTORS, extraction.MARKDOWN, ("markdown", "markdown-0"))
        project, [material] = await with_papers(client, WAGES)
        conversation = await conversation_in(client, project)
        await send(client, conversation, QUESTION)
        before = await chip_of(client, conversation)
        await asyncio.to_thread(client.state["db"].write, lambda conn: conn.execute("UPDATE citations SET quote = NULL"))
        await read_as(client, monkeypatch, material, "markdown-1")
        await idle(client, project)
        chip, read_now = await chip_of(client, conversation), await evidence(client, project)
        assert chip["passage_id"] in read_now and chip["passage_id"] != before["passage_id"]
        assert (chip["existence"], chip["support"], chip["quote"]) == ("ok", "supported", None)


async def test_a_replaced_papers_citation_stays_removed_when_its_old_file_is_read_again_for_another_paper(tmp_path, monkeypatch):
    # the null-passage loop goes by the citation's recorded version, so it can point a citation into a reading its
    # paper no longer reads, stored ok
    read_in_process(monkeypatch)
    real = extraction.extract
    async with cited(tmp_path) as client:
        monkeypatch.setitem(extraction.EXTRACTORS, extraction.MARKDOWN, ("markdown", "markdown-0"))
        project, [material] = await with_papers(client, WAGES)
        other = (await client.post("/api/projects", json={"name": "Another"})).json()["id"]
        await added(client, other, WAGES)  # the same file, another project's paper
        await idle(client, other)
        conversation = await conversation_in(client, project)
        await send(client, conversation, QUESTION)
        await asyncio.to_thread(client.state["db"].write, lambda conn: conn.execute(
            "UPDATE citations SET passage_id = NULL, existence = 'not_found'"))  # as a re-read that lost it leaves it
        await added(client, project, paper("Wage Floors", "Rents rose in the synthetic city."), material_id=material)
        await idle(client, project)
        [(theirs,)] = await rows(client, "SELECT id FROM materials WHERE project_id = ?", other)
        await read_as(client, monkeypatch, theirs, "markdown-1")  # the old file, read again for the other paper
        await idle(client, other)
        [(existence, passage)] = await rows(client, "SELECT existence, passage_id FROM citations")
        assert passage is None or passage in await evidence(client, project), (existence, passage)
        assert not (existence == "ok" and passage is not None)


async def test_a_recheck_after_a_replacement_without_the_quote_has_nothing_to_check(tmp_path):
    # a reader of stored existence without the read-time overlay: /api/citations/check
    async with cited(tmp_path) as client:
        project, [material] = await with_papers(client, WAGES)
        conversation = await conversation_in(client, project)
        run_id = (await send(client, conversation, QUESTION))[0]["run_id"]
        await added(client, project, paper("Wage Floors", "Rents rose in the synthetic city."), material_id=material)
        await idle(client, project)
        assert (await chip_of(client, conversation))["existence"] == "source_removed"
        response = await client.post("/api/citations/check", json={"run_id": run_id})
        ended = [(await run_finished(client, r))["status"] for r in response.json().get("run_ids", [])]
        assert response.status_code == 409, (response.status_code, ended, await rows(
            client, "SELECT status, cancel_reason FROM runs WHERE workflow = 'citation_check'"))


async def test_only_the_replaced_papers_citation_moves_when_two_projects_read_one_file_and_again_changes_nothing(
        tmp_path, monkeypatch):
    # the writer's probes: a file shared by two projects' papers; the remap repeated; then a re-read both follow
    read_in_process(monkeypatch)
    async with cited(tmp_path) as client:
        monkeypatch.setitem(extraction.EXTRACTORS, extraction.MARKDOWN, ("markdown", "markdown-0"))
        project, [material] = await with_papers(client, WAGES)
        other = (await client.post("/api/projects", json={"name": "Another"})).json()["id"]
        [theirs] = [m["id"] for m in (await added(client, other, WAGES))["materials"]]
        await idle(client, other)
        mine, yours = await conversation_in(client, project), await conversation_in(client, other)
        await send(client, mine, QUESTION)
        await send(client, yours, QUESTION)
        before = await chip_of(client, yours)
        await added(client, project, paper("Wage Floors", "A new opening paragraph.", OLD), material_id=material)
        await idle(client, project)
        moved, kept = await chip_of(client, mine), await chip_of(client, yours)
        assert moved["passage_id"] != before["passage_id"] and (moved["existence"], moved["support"]) == ("ok", "supported")
        assert (kept["passage_id"], kept["existence"], kept["support"]) == (before["passage_id"], "ok", "supported")
        stored = await rows(client, "SELECT * FROM citations ORDER BY id")
        for _ in range(3):
            await asyncio.to_thread(client.state["db"].write, lambda conn: [
                citations.retarget(conn, m) for m in (material, theirs)])
        assert await rows(client, "SELECT * FROM citations ORDER BY id") == stored
        await read_as(client, monkeypatch, theirs, "markdown-1")
        await idle(client, other)
        again = await chip_of(client, yours)
        assert again["passage_id"] != before["passage_id"] and (again["existence"], again["support"]) == ("ok", "supported")


async def test_a_citation_stored_removed_on_a_passage_a_reread_keeps_exists_again(tmp_path, monkeypatch):
    # the review note on supersede's found branch leaving existence as it was
    read_in_process(monkeypatch)
    async with cited(tmp_path) as client:
        monkeypatch.setitem(extraction.EXTRACTORS, extraction.MARKDOWN, ("markdown", "markdown-0"))
        project, [material] = await with_papers(client, WAGES)
        conversation = await conversation_in(client, project)
        await send(client, conversation, QUESTION)
        before = await chip_of(client, conversation)
        await asyncio.to_thread(client.state["db"].write, lambda conn: conn.execute(
            "UPDATE citations SET existence = 'source_removed', support = 'unchecked'"))
        await read_as(client, monkeypatch, material, "markdown-1")
        await idle(client, project)
        chip, read_now = await chip_of(client, conversation), await evidence(client, project)
        assert chip["existence"] == "ok" and chip["passage_id"] in read_now and chip["passage_id"] != before["passage_id"]



async def test_a_papers_reading_changes_only_where_its_citations_are_pointed_again():
    # Design review J's source pin: a paper's reading changes only in materials.add_version (its versions) and
    # materials._store (a reading and its passages, served by _serve), each of which points the paper's citations again
    # (citations.retarget); a citation's passage is set only in backend/citations.py and backend/db/deletion.py. A new
    # path that changes a reading without retarget, or points a citation elsewhere, fails here.
    import ast
    import re
    from pathlib import Path
    root = Path(__file__).resolve().parents[1] / "backend"
    writes = re.compile(r"(INSERT INTO|UPDATE) (material_versions|extractions|passages)\b")
    points = re.compile(r"UPDATE citations SET[^\"']*passage_id")
    writers, pointers, functions = set(), set(), {}
    for path in sorted(root.rglob("*.py")):
        source = path.read_text()
        if points.search(source):
            pointers.add(path.relative_to(root).as_posix())
        for node in ast.parse(source).body:
            for n in [node, *(node.body if isinstance(node, ast.ClassDef) else [])]:
                if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    text = ast.get_source_segment(source, n) or ""
                    functions[(path.name, n.name)] = text
                    if writes.search(text):
                        writers.add((path.name, n.name))
    assert writers == {("materials.py", "add_version"), ("materials.py", "_store")}, writers
    assert pointers == {"citations.py", "db/deletion.py"}, pointers
    assert "citations.retarget(conn, material)" in functions["materials.py", "add_version"]
    assert "_serve(" in functions["materials.py", "_store"]
    assert "citations.retarget(conn, material)" in functions["materials.py", "_serve"]
