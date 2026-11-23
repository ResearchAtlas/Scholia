"""Citations after a paper's reading changes (design review J, Option 1, Harold 2026-11-14; section 7.1): one chooser
(citations.locate) and one recomputation by paper (citations.retarget) at every reading change. Each scenario is a
finding of PR 58's cycles or of design review J (its cases L, M and N). Synthetic papers, the tests' stand-in helper
and the network guard only."""

import asyncio

import pytest

import synthetic_materials as synthetic
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


async def read_as(client, monkeypatch, material, name, kind=extraction.MARKDOWN):
    """The paper's file read again by another version of its reader (an app update's re-read)."""
    monkeypatch.setitem(extraction.EXTRACTORS, kind, (extraction.EXTRACTORS[kind][0], name))
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


@pytest.mark.parametrize(("lines", "marked", "now"), [
    (("Minimum wages raise the earn-", "ings of low-paid workers in the synthetic panel."),
     {"earnings": "earn\ufffeings"}, OLD),  # the word joined
    (("Minimum wages raise the earnings of low-paid workers in the GPT-", "4 synthetic panel."),
     {"GPT-4": "GPT\ufffe4"}, "Minimum wages raise the earnings of low-paid workers in the GPT-4 synthetic panel.")])  # kept
async def test_a_citation_whose_quote_holds_pdfiums_line_end_mark_follows_the_pdf_4_reread_and_keeps_its_verdict(  # V1
        tmp_path, monkeypatch, lines, marked, now):
    # PDF extraction before pdf-4 kept U+FFFE in a word broken at a line's end; pdf-4 joins it or keeps a hyphen
    read_in_process(monkeypatch)
    real, pdf4 = extraction.extract, extraction.EXTRACTORS[extraction.PDF][1]
    paper_pdf = synthetic.pdf([[(72, 750, 18, "Wage Floors"), (72, 720, 10, lines[0]), (72, 708, 10, lines[1])]])
    async with cited(tmp_path) as client:
        monkeypatch.setitem(extraction.EXTRACTORS, extraction.PDF, ("pdf", "pdf-3"))
        rewording(monkeypatch, real, marked)  # as pdf-3 read it
        project, [material] = await with_papers(client, ("Wage Floors.pdf", paper_pdf))
        conversation = await conversation_in(client, project)
        await send(client, conversation, QUESTION)
        before = await chip_of(client, conversation)
        assert (before["existence"], before["support"]) == ("ok", "supported") and "\ufffe" in before["quote"]
        monkeypatch.setattr(extraction, "extract", real)
        await read_as(client, monkeypatch, material, pdf4, extraction.PDF)
        await idle(client, project)
        chip, read_now = await chip_of(client, conversation), await evidence(client, project)
        assert chip["passage_id"] in read_now and chip["passage_id"] != before["passage_id"]
        assert read_now[chip["passage_id"]][4] == now
        assert (chip["existence"], chip["support"]) == ("ok", "supported")


async def test_a_new_version_that_only_drops_a_hyphen_does_not_keep_a_citation_without_the_mark(tmp_path, monkeypatch):
    # only PDFium's mark is mapped (extraction.joined); a hyphen dropped by a new version changes the text
    read_in_process(monkeypatch)
    real = extraction.extract
    signed = "Minimum wages change the earnings of low-paid workers by -0.3 percent in the synthetic panel."
    async with cited(tmp_path) as client:
        monkeypatch.setitem(extraction.EXTRACTORS, extraction.MARKDOWN, ("markdown", "markdown-0"))
        project, [material] = await with_papers(client, paper("Wage Floors", signed))
        conversation = await conversation_in(client, project)
        await send(client, conversation, QUESTION)
        before = await chip_of(client, conversation)
        assert (before["existence"], before["support"], before["quote"]) == ("ok", "supported", signed)
        rewording(monkeypatch, real, {"by -0.3": "by 0.3"})
        await read_as(client, monkeypatch, material, "markdown-1")
        await idle(client, project)
        assert await rows(client, "SELECT passage_id, existence, support FROM citations") == [(None, "not_found", "supported")]
        chip = await chip_of(client, conversation)
        assert chip["existence"] != "ok"


async def test_a_new_version_that_only_drops_a_hyphen_past_the_quote_finds_the_citation_but_not_its_verdict(
        tmp_path, monkeypatch):
    # the quote (its first QUOTE_CHARS) still holds; the passage's text changed past it, so its verdict is not kept
    read_in_process(monkeypatch)
    real = extraction.extract
    long = OLD + " " + "Regional prices adjust slowly after a wage floor is raised in the panel. " * 7 + "Hours fell by -0.3."
    assert long.index("-0.3") > citations.QUOTE_CHARS
    async with cited(tmp_path) as client:
        monkeypatch.setitem(extraction.EXTRACTORS, extraction.MARKDOWN, ("markdown", "markdown-0"))
        project, [material] = await with_papers(client, paper("Wage Floors", long))
        conversation = await conversation_in(client, project)
        await send(client, conversation, QUESTION)
        assert ((await chip_of(client, conversation))["existence"], (await chip_of(client, conversation))["support"]) == (
            "ok", "supported")
        rewording(monkeypatch, real, {"by -0.3": "by 0.3"})
        await read_as(client, monkeypatch, material, "markdown-1")
        await idle(client, project)
        chip = await chip_of(client, conversation)
        assert (chip["existence"], chip["support"]) == ("ok", "unchecked")


async def test_a_pdf_3_passage_marked_only_past_its_quote_keeps_its_verdict_after_the_reread(tmp_path, monkeypatch):
    # the old passage text holds the mark and the quote (its first QUOTE_CHARS) does not: joined all the same
    read_in_process(monkeypatch)
    real = extraction.extract
    long = OLD + " " + "Regional prices adjust slowly after a wage floor is raised in the panel. " * 7 + "Hours fell by a little."
    assert long.index("Hours") > citations.QUOTE_CHARS
    async with cited(tmp_path) as client:
        monkeypatch.setitem(extraction.EXTRACTORS, extraction.MARKDOWN, ("markdown", "markdown-0"))
        rewording(monkeypatch, real, {"a little": "a lit\ufffetle"})  # as pdf-3 read a broken word
        project, [material] = await with_papers(client, paper("Wage Floors", long))
        conversation = await conversation_in(client, project)
        await send(client, conversation, QUESTION)
        before = await chip_of(client, conversation)
        assert (before["existence"], before["support"]) == ("ok", "supported") and "\ufffe" not in before["quote"]
        assert await rows(client, "SELECT instr(text, char(65534)) > 0 FROM passages WHERE id = ?",
                          before["passage_id"]) == [(1,)]  # the old passage text holds the mark
        monkeypatch.setattr(extraction, "extract", real)
        await read_as(client, monkeypatch, material, "markdown-1")
        await idle(client, project)
        chip = await chip_of(client, conversation)
        assert chip["passage_id"] != before["passage_id"] and (chip["existence"], chip["support"]) == ("ok", "supported")


async def test_a_pdf_3_citation_never_moves_to_a_passage_that_differs_from_its_own_but_by_a_hyphen(tmp_path):
    # r1 pass 2: setting every hyphen aside made "-0.3" and "0.3" one text, so the chooser moved a citation of the
    # negative passage onto the positive one and kept its verdict; the mark is now mapped as pdf-4 reads it (joined)
    negative = "We estimate the effect at -0.3 percent in the synthetic panel of regions."
    positive = "We estimate the effect at 0.3 percent in the synthetic panel of regions."
    async with cited(tmp_path) as client:
        project, [material] = await with_papers(client, paper("Signs", positive, negative))
        conversation = await conversation_in(client, project)
        await send(client, conversation, QUESTION)

        def as_pdf_3(conn):  # its passage gone, its quote the negative passage as pdf-3 read it
            conn.execute("UPDATE citations SET passage_id = NULL, existence = 'source_removed', support = 'supported',"
                         " quote = ?", (negative.replace("estimate", "esti\ufffemate"),))
            citations.retarget(conn, material)
        await asyncio.to_thread(client.state["db"].write, as_pdf_3)
        assert await rows(client, "SELECT p.text, c.existence, c.support FROM citations c"
                                  " JOIN passages p ON p.id = c.passage_id") == [(negative, "ok", "supported")]


async def test_a_new_version_that_drops_a_minus_sign_inside_a_quote_whose_passage_held_the_mark_elsewhere(
        tmp_path, monkeypatch):
    # r1 pass 1: a mark past the quote made the comparison set every hyphen aside, so "-0.3" to "0.3" kept the verdict
    read_in_process(monkeypatch)
    real = extraction.extract
    long = ("Minimum wages change the earnings of low-paid workers by -0.3 percent in the synthetic panel. "
            + "Regional prices adjust slowly after a wage floor is raised in the panel. " * 7 + "Hours fell by a little.")
    assert long.index("-0.3") < citations.QUOTE_CHARS < long.index("a little")
    async with cited(tmp_path) as client:
        monkeypatch.setitem(extraction.EXTRACTORS, extraction.MARKDOWN, ("markdown", "markdown-0"))
        rewording(monkeypatch, real, {"a little": "a lit\ufffetle"})  # as pdf-3 read a broken word, past the quote
        project, [material] = await with_papers(client, paper("Wage Floors", long))
        conversation = await conversation_in(client, project)
        await send(client, conversation, QUESTION)
        before = await chip_of(client, conversation)
        assert (before["existence"], before["support"]) == ("ok", "supported") and "\ufffe" not in before["quote"]
        rewording(monkeypatch, real, {"by -0.3": "by 0.3"})  # the new version, read with no mark
        await read_as(client, monkeypatch, material, "markdown-1")
        await idle(client, project)
        assert await rows(client, "SELECT passage_id, existence FROM citations") == [(None, "not_found")]


async def test_a_citation_of_a_text_paper_whose_own_text_holds_u_fffe_follows_an_identical_reread(tmp_path, monkeypatch):
    # r1 batch review 2: a noncharacter in a Markdown file is the file's own; the quote as written still matches
    read_in_process(monkeypatch)
    held = OLD.replace("earnings", "earn\ufffeings")
    async with cited(tmp_path) as client:
        monkeypatch.setitem(extraction.EXTRACTORS, extraction.MARKDOWN, ("markdown", "markdown-0"))
        project, [material] = await with_papers(client, paper("Wage Floors", held))
        conversation = await conversation_in(client, project)
        await send(client, conversation, QUESTION)
        assert (await chip_of(client, conversation))["quote"] == held
        await read_as(client, monkeypatch, material, "markdown-1")
        await idle(client, project)
        chip = await chip_of(client, conversation)
        assert (chip["existence"], chip["support"]) == ("ok", "supported")


async def test_a_text_papers_citation_with_no_passage_keeps_its_verdict_on_its_quote_as_written(tmp_path):
    # retarget's quote branch: a Markdown passage that holds U+FFFE itself is its quote as written
    held = OLD.replace("earnings", "earn\ufffeings")
    async with cited(tmp_path) as client:
        project, [material] = await with_papers(client, paper("Wage Floors", held))
        conversation = await conversation_in(client, project)
        await send(client, conversation, QUESTION)

        def removed_then_retargeted(conn):
            conn.execute("UPDATE citations SET passage_id = NULL, existence = 'source_removed', quote = ?", (held,))
            citations.retarget(conn, material)
        await asyncio.to_thread(client.state["db"].write, removed_then_retargeted)
        assert await rows(client, "SELECT existence, support FROM citations") == [("ok", "supported")]


async def test_a_pdf_3_quote_whose_dropped_label_held_the_hyphen_follows_with_its_verdict(tmp_path):
    # r2 pass 1: "Abstract-Work-" / "flows ..." reads "Work-flows ..." under pdf-4 and "Work<mark>flows ..." under pdf-3,
    # the label gone from both; joined of the quote without the label gives "Workflows" unless its first word is
    # taken as hyphened (which is asked after the plain form)
    now = "Work-flows are measured in a synthetic panel of regions across many years and wages."
    async with cited(tmp_path) as client:
        project, [material] = await with_papers(client, paper("Wage Floors", now))
        conversation = await conversation_in(client, project)
        await send(client, conversation, QUESTION)

        def as_pdf_3(conn):
            conn.execute("UPDATE citations SET passage_id = NULL, existence = 'source_removed', support = 'supported',"
                         " quote = ?", (now.replace("Work-flows", "Work\ufffeflows"),))
            citations.retarget(conn, material)
        await asyncio.to_thread(client.state["db"].write, as_pdf_3)
        assert await rows(client, "SELECT p.text, c.existence, c.support FROM citations c"
                                  " JOIN passages p ON p.id = c.passage_id") == [(now, "ok", "supported")]


async def test_a_pdf_3_passage_whose_dropped_label_held_the_hyphen_keeps_its_verdict_through_the_reread(tmp_path, monkeypatch):
    # retarget's old-text branch: the cited passage is still there when the pdf-4 reading commits
    read_in_process(monkeypatch)
    real, pdf4 = extraction.extract, extraction.EXTRACTORS[extraction.PDF][1]
    paper_pdf = synthetic.pdf([[(72, 750, 18, "Wage Floors"), (72, 720, 10, "Abstract-Minimum-"),
                                (72, 708, 10, "wages raise the earnings of low-paid workers in the synthetic panel.")]])
    now = "Minimum-wages raise the earnings of low-paid workers in the synthetic panel."
    async with cited(tmp_path) as client:
        monkeypatch.setitem(extraction.EXTRACTORS, extraction.PDF, ("pdf", "pdf-3"))
        rewording(monkeypatch, real, {"Minimum-wages": "Minimum\ufffewages"})  # as pdf-3 read it, its label dropped
        project, [material] = await with_papers(client, ("Wage Floors.pdf", paper_pdf))
        conversation = await conversation_in(client, project)
        await send(client, conversation, QUESTION)
        before = await chip_of(client, conversation)
        assert (before["existence"], before["support"]) == ("ok", "supported") and "\ufffe" in before["quote"]
        monkeypatch.setattr(extraction, "extract", real)
        await read_as(client, monkeypatch, material, pdf4, extraction.PDF)
        await idle(client, project)
        chip, read_now = await chip_of(client, conversation), await evidence(client, project)
        assert chip["passage_id"] != before["passage_id"] and read_now[chip["passage_id"]][4] == now
        assert (chip["existence"], chip["support"]) == ("ok", "supported")


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



async def test_a_project_export_says_of_a_citation_what_its_chip_says(tmp_path):
    # PR 63's phase B internal review (P3-3): the export read a citation's stored existence and verdict, where the
    # conversation read applies resolve_now (T7): a citation whose replacement lacks its quote exported as ok, supported
    import json
    from test_backups import archived_run, read
    async with cited(tmp_path) as client:
        project, [material] = await with_papers(client, WAGES)
        conversation = await conversation_in(client, project)
        await send(client, conversation, QUESTION)
        await added(client, project, paper("Wage Floors", "Rents rose in the synthetic city."), material_id=material)
        await idle(client, project)
        assert (await chip_of(client, conversation))["existence"] == "source_removed"
        destination = tmp_path / "chosen"
        destination.mkdir()
        run = await archived_run(client, f"/api/projects/{project}/export", {"destination": str(destination)})
        assert run.status == "succeeded", run
    [exported] = json.loads(read(destination / run.result["file"].rsplit("/", 1)[1], "conversations.json"))
    [citation] = exported["turns"][0]["citations"]
    assert (citation["existence"], citation["support"], citation["quote"]) == ("source_removed", "unchecked", None)


async def test_the_chooser_takes_the_citations_own_passage_then_its_text_then_its_page_then_its_place(tmp_path):
    # PR 63's phase B internal review (P3-4): no test pinned locate's order. Five passages of one reading hold the
    # quote; each step of the order is asked for with the steps before it out of the way.
    quote = "the quote"
    async with cited(tmp_path) as client:
        project, [material] = await with_papers(client, paper("Five", *[f"Paragraph {i} holds {quote}." for i in range(5)]))
        db = client.state["db"]
        passages = await rows(client, "SELECT id, ordinal FROM passages WHERE instr(text, ?) > 0 ORDER BY ordinal", quote)
        assert len(passages) == 5
        [(a, oa), (b, _), (c, oc), (d, _), (e, oe)] = passages

        def shape(conn):
            conn.execute("UPDATE passages SET text = ? WHERE id = ?", (f"The old text, with {quote}.", b))
            conn.execute("UPDATE passages SET page = 7 WHERE id IN (?, ?)", (d, e))
        await asyncio.to_thread(db.write, shape)

        def chosen(**given):
            return asyncio.to_thread(db.read, lambda conn: citations.locate(conn, material, quote, **given)[0])
        old = f"The old text, with {quote}."
        assert await chosen(text=old, page=7, passage_id=a, ordinal=oe) == a  # its own passage, while it holds the quote
        assert await chosen(text=old, page=7, passage_id=None, ordinal=oe) == b  # then one with its old passage's text
        assert await chosen(text="Gone.", page=7, passage_id=None, ordinal=oc) == d  # then on its page, nearest its place
        assert await chosen(text="Gone.", page=7, passage_id=None, ordinal=oe) == e
        assert await chosen(text="Gone.", page=None, passage_id=None, ordinal=oe) == c  # with no page: pageless first,
        assert await chosen(text="Gone.", page=None, passage_id=None, ordinal=oa) == a  # then nearest its place
        # a pdf-3 citation's quote and old text (holding PDFium's mark) are taken as pdf-4 reads them (joined)
        assert await chosen(text=f"The ol\ufffed text, with {quote}.", page=7, passage_id=None, ordinal=oe) == b

        def located(held):
            return asyncio.to_thread(db.read, lambda conn: citations.locate(conn, material, held))
        assert await located("the qu\ufffeote") is not None
        assert await located("\ufffe") is None  # nothing once joined
        assert await located("the qu-ote") is None  # any other hyphen counts
        # a text paper's own U+FFFE: its passage as written comes before one equal to it only once joined
        await asyncio.to_thread(db.write, lambda conn: [conn.execute("UPDATE passages SET text = ? WHERE id = ?", (t, i))
                                                       for t, i in ((f"Own\ufffe text, with {quote}.", c), (f"Own text, with {quote}.", e))])
        assert await chosen(text=f"Own\ufffe text, with {quote}.", page=7, passage_id=None, ordinal=oe) == c
        # the old text as pdf-4 reads it, before so with its first word taken as hyphened (a dropped label's)
        await asyncio.to_thread(db.write, lambda conn: [conn.execute("UPDATE passages SET text = ? WHERE id = ?", (t, i))
                                                       for t, i in ((f"Decide, with {quote}.", c), (f"De-cide, with {quote}.", e))])
        assert await chosen(text=f"De\ufffecide, with {quote}.", page=7, passage_id=None, ordinal=oe) == c


async def test_a_citation_with_no_passage_keeps_its_verdict_when_its_quote_is_a_passage_but_for_the_mark(tmp_path):
    # retarget's quote branch (no old passage), joined as its text branch is
    async with cited(tmp_path) as client:
        project, [material] = await with_papers(client, WAGES)
        conversation = await conversation_in(client, project)
        await send(client, conversation, QUESTION)
        assert await rows(client, "SELECT support FROM citations") == [("supported",)]

        def removed_then_retargeted(conn):
            conn.execute("UPDATE citations SET passage_id = NULL, existence = 'source_removed', quote = ?",
                         (OLD.replace("earnings", "earn\ufffeings"),))
            citations.retarget(conn, material)
        await asyncio.to_thread(client.state["db"].write, removed_then_retargeted)
        assert await rows(client, "SELECT existence, support FROM citations") == [("ok", "supported")]


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
