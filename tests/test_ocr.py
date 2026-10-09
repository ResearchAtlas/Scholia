"""OCR for scanned pages (slice-1 spec F3a step 2, sections 1, 7.1, 13 and 15; S1-20), on synthetic
files. The reading's behavior is checked with a test-owned engine (Engine) in place of the platform's;
Vision itself reads synthetic scans drawn by AppKit. Lifecycle cases first."""

import ast
import asyncio
import builtins
import io
import json
import logging
import os
import sys
import threading
import time
from pathlib import Path

import pytest

import backend.extraction as extraction
import backend.lookup as lookup
import backend.materials as materials_module
from backend import ocr
from backend.db import new_id
from backend.self_test import scanned_pdf
import synthetic_materials as synthetic
from scholia_app import MockProvider, MockScholarly, background_idle, openalex_work, run_finished, started
from test_materials import added, listing, project_of, rows, settled

DOI = "10.5555/scholia.scanned.001"
LINE = ocr.Line("Recognized text of a scanned page, read by the engine.", (0.1, 0.1, 0.9, 0.12), 0.9)


class Engine:
    """A test-owned OCR engine. recognize(bitmap) records the bitmap's size and returns lines (a list,
    or a function of the call's number from 1), raising ocr.Failed on the calls numbered in fail and
    sleeping delay seconds. With hold, each call waits until go is set (reached is set as it waits)."""

    version = "test-engine-1"

    def __init__(self, lines=(LINE,), fail=(), hold=False, delay=0.0):
        self.lines, self.fail, self.delay, self.calls = lines, set(fail), delay, []
        self.reached, self.go = threading.Event(), threading.Event()
        if not hold:
            self.go.set()

    def recognize(self, bitmap):
        self.calls.append((bitmap.width, bitmap.height))
        self.reached.set()
        self.go.wait(10)
        time.sleep(self.delay)
        if len(self.calls) in self.fail:
            raise ocr.Failed()
        return list(self.lines(len(self.calls)) if callable(self.lines) else self.lines)


def use(monkeypatch, engine):
    monkeypatch.setattr(ocr, "engine", lambda: engine)
    return engine


def scan(pages=1):
    """A PDF of pages holding only an image each, as a scanned paper is."""
    return synthetic.pdf([[]] * pages, scanned=range(1, pages + 1))


def read(data, engine, monkeypatch):
    use(monkeypatch, engine)
    return extraction.extract(data, extraction.PDF)


async def held(engine):
    await asyncio.to_thread(engine.reached.wait, 10)
    assert engine.reached.is_set()


@pytest.fixture
def quick(monkeypatch):
    monkeypatch.setattr(lookup, "SPACING", {source: 0.0 for source in lookup.SPACING})
    monkeypatch.setattr(materials_module, "WAIT_SECONDS", 0.01)


# Lifecycle: cancellation, retries, deletion, a change of level, a failed commit, restarts, the limit


@pytest.mark.asyncio
async def test_a_reading_cancelled_while_a_page_is_recognized_stops_there_writes_nothing_and_retry_reads_it_whole(
        tmp_path, monkeypatch):
    engine = use(monkeypatch, Engine(hold=True))
    async with started(tmp_path / "data") as client:
        project = await project_of(client)
        [paper] = (await added(client, project, ("scan.pdf", scan(2))))["materials"]
        await held(engine)
        cancelling = asyncio.create_task(client.post(f"/api/runs/{paper['run_id']}/cancel"))
        await asyncio.sleep(0.1)
        engine.go.set()
        await cancelling
        run = await run_finished(client, paper["run_id"])
        assert run["status"] == "cancelled" and run["retryable"]
        assert len(engine.calls) == 1  # its second page was never recognized
        assert await rows(client, "SELECT count(*) FROM extractions") == [(0,)]
        assert await rows(client, "SELECT count(*) FROM index_queue") == [(0,)]
        [stopped] = await settled(client, project)
        assert (stopped["state"], stopped["reason"]) == ("needs_attention", "stopped")
        again = await client.post(f"/api/runs/{paper['run_id']}/retry")
        assert (await run_finished(client, again.json()["run_id"]))["status"] == "succeeded"
        assert len(engine.calls) == 3  # read from its start
        [ready] = await settled(client, project)
        assert (ready["state"], ready["extraction"]["ocr_pages"], ready["extraction"]["status"]) == ("ready", 2, "complete")
        assert await rows(client, "SELECT count(*) FROM extractions") == [(1,)]


@pytest.mark.asyncio
async def test_a_failed_recognition_writes_nothing_says_so_and_retry_reads_the_paper(tmp_path, monkeypatch):
    engine = use(monkeypatch, Engine(fail={2}))  # the second page fails, the first was read
    async with started(tmp_path / "data") as client:
        project = await project_of(client)
        [paper] = (await added(client, project, ("scan.pdf", scan(2))))["materials"]
        run = await run_finished(client, paper["run_id"])
        assert (run["status"], run["result"], run["retryable"]) == ("failed", {"reason": "ocr_failed"}, True)
        assert await rows(client, "SELECT count(*) FROM extractions") == [(0,)]
        assert await rows(client, "SELECT count(*) FROM passages") == [(0,)]
        assert await rows(client, "SELECT count(*) FROM index_queue") == [(0,)]
        [failed] = await settled(client, project)
        assert (failed["state"], failed["reason"], failed["extraction"]) == ("needs_attention", "ocr_failed", None)
        again = await client.post(f"/api/runs/{paper['run_id']}/retry")
        assert (await run_finished(client, again.json()["run_id"]))["status"] == "succeeded"
        [ready] = await settled(client, project)
        assert ready["state"] == "ready" and ready["extraction"]["passages"] == 2
        assert await rows(client, "SELECT count(*) FROM extractions") == [(1,)]
        (n,) = (await rows(client, "SELECT count(*) FROM passages"))[0]
        assert await rows(client, "SELECT count(*), count(DISTINCT target_id) FROM index_queue WHERE op = 'add'") == [(n, n)]
        assert await rows(client, "SELECT count(*) FROM audit_log WHERE event = 'outbound'") == [(0,)]  # OCR sends nothing


@pytest.mark.asyncio
@pytest.mark.parametrize("deleted", ["material", "project"])
async def test_deleting_while_a_page_is_recognized_sends_no_later_page_and_writes_nothing(tmp_path, monkeypatch, deleted):
    engine = use(monkeypatch, Engine(hold=True))
    async with started(tmp_path / "data") as client:
        project = await project_of(client)
        [paper] = (await added(client, project, ("scan.pdf", scan(2))))["materials"]
        await held(engine)
        target = f"/api/materials/{paper['id']}" if deleted == "material" else f"/api/projects/{project}"
        assert (await client.delete(target)).status_code == 200
        engine.go.set()
        await background_idle(client, timeout=10)
        assert len(engine.calls) == 1
        assert await rows(client, "SELECT count(*) FROM extractions") == [(0,)]
        assert await rows(client, "SELECT count(*) FROM passages") == [(0,)]
        assert await rows(client, "SELECT count(*) FROM index_queue WHERE op = 'add'") == [(0,)]
        if deleted == "material":
            assert await rows(client, "SELECT status, cancel_reason FROM runs WHERE id = ?", paper["run_id"]) == [
                ("cancelled", "revoked")]


@pytest.mark.asyncio
async def test_one_projects_deletion_during_recognition_leaves_another_projects_reading_of_the_file(tmp_path, monkeypatch):
    engine = use(monkeypatch, Engine(hold=True))
    async with started(tmp_path / "data") as client:
        mine, theirs, same = await project_of(client, "Mine"), await project_of(client, "Theirs"), ("scan.pdf", scan(2))
        [paper] = (await added(client, mine, same))["materials"]
        [other] = (await added(client, theirs, same))["materials"]
        while len(engine.calls) < 2:  # both readings are on their first page
            await asyncio.sleep(0.01)
        assert (await client.delete(f"/api/materials/{paper['id']}")).status_code == 200
        engine.go.set()
        assert (await run_finished(client, other["run_id"]))["status"] == "succeeded"
        assert (await run_finished(client, paper["run_id"]))["cancel_reason"] == "revoked"
        assert len(engine.calls) == 3  # the revoked reading sent no later page
        [read] = await settled(client, theirs)
        assert read["state"] == "ready"
        assert await rows(client, "SELECT count(*) FROM extractions") == [(1,)]
        assert await rows(client, "SELECT DISTINCT project_id FROM index_queue WHERE op = 'add'") == [(theirs,)]


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["local_only", "review_lock"])
async def test_a_stricter_level_during_recognition_lets_the_reading_finish_and_its_doi_follows_the_level(
        tmp_path, monkeypatch, quick, change):
    engine = use(monkeypatch, Engine([ocr.Line(f"doi:{DOI}", (0.1, 0.1, 0.5, 0.12), 0.5)], hold=True))
    provider = MockProvider(scholarly=MockScholarly(openalex={DOI: openalex_work(DOI, "A Scanned Paper, Resolved")}))
    async with started(tmp_path / "data", provider) as client:
        project = await project_of(client)
        result = await added(client, project, ("scan.pdf", scan()))
        await held(engine)
        if change == "local_only":
            response = await client.post(f"/api/projects/{project}/sensitivity", json={"level": "local_only"})
        else:
            response = await client.post(f"/api/projects/{project}/review-lock", json={"locked": True})
        assert response.status_code == 200, response.text
        engine.go.set()
        assert (await run_finished(client, result["materials"][0]["run_id"]))["status"] == "succeeded"  # it finishes
        stopped = await run_finished(client, result["lookup_run_id"])  # the lookup it waited for does not send
        assert (stopped["status"], stopped["cancel_reason"]) == ("cancelled", "revoked")
        [paper] = await settled(client, project)
        assert (paper["state"], paper["checked_by"]) == ("ready", None) and provider.scholarly.requests == []
        again = await client.post(f"/api/runs/{result['lookup_run_id']}/retry")
        if change == "local_only":  # tried again, it asks first: nothing is sent before the answer
            assert again.status_code == 201
            deadline = asyncio.get_running_loop().time() + 10
            while not (asks := (await listing(client, project))["asks"]):
                assert asyncio.get_running_loop().time() < deadline
                await asyncio.sleep(0.02)
            assert asks[0]["params"]["identifiers"] == 1 and provider.scholarly.requests == []
        else:  # locked: never looked up
            assert (again.status_code, again.json()["code"]) == (403, "lookup_locked")


@pytest.mark.asyncio
async def test_a_newer_readings_failed_commit_leaves_the_older_reading_and_its_citations_and_retry_reads_it(
        tmp_path, monkeypatch, quick):
    use(monkeypatch, None)  # read first by S1-13's extractor: scanned pages waiting
    data = tmp_path / "data"
    async with started(data) as client:
        project = await project_of(client)
        [paper] = (await added(client, project, ("paper.pdf", synthetic.paper_pdf(scanned=1))))["materials"]
        [waiting] = await settled(client, project)
        assert waiting["reason"] == "ocr_waiting"
        [(older, cited)] = await rows(client, "SELECT e.id, p.id FROM extractions e JOIN passages p"
                                              " ON p.extraction_id = e.id ORDER BY p.ordinal LIMIT 1")
        citation = await cite(client, paper["id"], cited, "Minimum Wages")
    use(monkeypatch, Engine())

    def failing(conn, earlier, newer):
        raise RuntimeError("a failure in the commit's last step")

    real_supersede = materials_module._supersede
    monkeypatch.setattr(materials_module, "_supersede", failing)
    async with started(data, setup=False) as client:
        await asyncio.sleep(0.5)
        await background_idle(client, timeout=10)
        [(run, status)] = await rows(client, "SELECT id, status FROM runs WHERE workflow = 'extract'"
                                             " AND json_extract(inputs, '$.outdated') IS NOT NULL")
        [row] = (await client.get("/api/activity", params={"run_id": run})).json()["runs"]
        assert (row["status"], row["retryable"]) == ("interrupted", True)  # nothing of it committed
        assert await rows(client, "SELECT id FROM extractions") == [(older,)]
        assert await rows(client, "SELECT passage_id FROM citations WHERE id = ?", citation) == [(cited,)]
        assert await rows(client, "SELECT count(*) FROM runs WHERE workflow = 'lookup'") == [(1,)]  # none recorded with it
        [outdated] = await settled(client, project)
        assert outdated["state"] == "needs_attention"
        monkeypatch.setattr(materials_module, "_supersede", real_supersede)
        again = await client.post(f"/api/runs/{run}/retry")
        assert (await run_finished(client, again.json()["run_id"]))["status"] == "succeeded"
        [ready] = await settled(client, project)
        assert (ready["state"], ready["extraction"]["ocr_pages"]) == ("ready", 1)
        assert await rows(client, "SELECT count(*) FROM extractions WHERE id = ?", older) == [(0,)]
        [(now,)] = await rows(client, "SELECT passage_id FROM citations WHERE id = ?", citation)
        assert now not in (None, cited)


@pytest.mark.asyncio
async def test_a_reading_stopped_by_a_shutdown_mid_page_starts_again_at_the_next_launch_and_writes_once(
        tmp_path, monkeypatch):
    data, engine = tmp_path / "data", use(monkeypatch, Engine(hold=True))
    async with started(data) as client:
        project = await project_of(client)
        [paper] = (await added(client, project, ("scan.pdf", scan(2))))["materials"]
        await held(engine)
        threading.Timer(0.3, engine.go.set).start()  # the page in hand ends while the app closes
    assert len(engine.calls) == 1
    async with started(data, setup=False) as client:
        assert (await run_finished(client, paper["run_id"]))["status"] == "succeeded"
        [ready] = await settled(client, project)
        assert ready["state"] == "ready"
        assert len(engine.calls) == 3
        assert await rows(client, "SELECT count(*) FROM extractions") == [(1,)]


@pytest.mark.asyncio
async def test_recognition_counts_within_the_readings_time_limit(tmp_path, monkeypatch):
    use(monkeypatch, Engine(delay=0.4))
    monkeypatch.setattr(materials_module, "EXTRACTION_SECONDS", 0.2)
    async with started(tmp_path / "data") as client:
        project = await project_of(client)
        [paper] = (await added(client, project, ("scan.pdf", scan(3))))["materials"]
        run = await run_finished(client, paper["run_id"])
        assert (run["status"], run["cancel_reason"], run["result"]) == ("cancelled", "limit", {"reason": "time_limit"})
        assert await rows(client, "SELECT count(*) FROM extractions") == [(0,)]


# What a reading of scanned pages gives


def test_a_scanned_page_is_rendered_at_300_dpi_and_read_by_the_engine_and_text_pages_are_not(monkeypatch):
    engine = Engine()
    found = read(synthetic.paper_pdf(scanned=1), engine, monkeypatch)
    assert engine.calls == [(2550, 3301)]  # a letter page at 300 dpi (pypdfium2 rounds a side up)
    assert (found.pages, found.ocr_pages, found.status) == (3, 1, "complete")
    assert found.version == extraction.extractor_of(extraction.PDF)[1] and found.version.endswith("+test-engine-1")
    [recognized] = [p for p in found.passages if p.page == 3]
    assert recognized.text == LINE.text and recognized.boxes == {"rects": [[0.1, 0.1, 0.9, 0.12]], "ocr": {"confidence": 0.9}}
    use(monkeypatch, None)
    assert [p.text for p in extraction.extract(synthetic.paper_pdf(scanned=1), extraction.PDF).passages] == [
        p.text for p in found.passages if p.page != 3]  # the text pages read as before


def test_a4_and_a_poster_are_rendered_at_300_dpi_or_within_the_pixel_bound(monkeypatch):
    a4 = Engine()
    read(synthetic.pdf([[]], scanned={1}, size=(595.28, 841.89)), a4, monkeypatch)
    assert a4.calls == [(2481, 3508)]
    poster = Engine()
    read(synthetic.pdf([[]], scanned={1}, size=(5669.0, 5669.0)), poster, monkeypatch)  # 2 m by 2 m
    [(width, height)] = poster.calls
    assert width * height <= extraction.MAX_OCR_PIXELS and width * height > 0.97 * extraction.MAX_OCR_PIXELS
    strip = Engine()
    read(synthetic.pdf([[]], scanned={1}, size=(10.0, 14400.0)), strip, monkeypatch)
    assert max(strip.calls[0]) <= extraction.MAX_PAGE_SIDE


def test_a_page_mostly_of_unmapped_characters_is_recognized_and_its_text_layer_gives_nothing(monkeypatch):
    unmapped = synthetic_unmapped("Qbsujdjqbou tqfbljoh bu mfohui bcpvu fbsojoht.")
    use(monkeypatch, None)
    assert extraction.extract(unmapped, extraction.PDF).passages == []  # the scanned-page check takes it
    engine = Engine()
    found = read(unmapped, engine, monkeypatch)
    assert len(engine.calls) == 1 and [p.text for p in found.passages] == [LINE.text]


def synthetic_unmapped(text):
    """A one-page PDF whose line is set in a Type 3 font with no ToUnicode map: none of its characters
    has a Unicode mapping, as with some scanned and OCR-less PDFs."""
    glyph = b"0 0 d0 0 0 500 700 re f"
    font = (b"<< /Type /Font /Subtype /Type3 /FontBBox [0 0 500 700] /FontMatrix [0.001 0 0 0.001 0 0]"
            b" /CharProcs << /g 6 0 R >> /Encoding << /Type /Encoding /Differences [32 /g 65 "
            + b" ".join([b"/g"] * 58) + b"] >> /FirstChar 32 /LastChar 122 /Widths [" + b" ".join([b"500"] * 91) + b"] >>")
    content = b"BT /F1 12 Tf 72 700 Td (" + text.encode() + b") Tj ET"
    return raw_pdf([b"<< /Type /Catalog /Pages 2 0 R >>", b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
                    b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Resources << /Font << /F1 4 0 R >> >>"
                    b" /Contents 5 0 R >>", font,
                    b"<< /Length %d >>\nstream\n%s\nendstream" % (len(content), content),
                    b"<< /Length %d >>\nstream\n%s\nendstream" % (len(glyph), glyph)])


def raw_pdf(objects):
    out, offsets = bytearray(b"%PDF-1.4\n"), []
    for number, body in enumerate(objects, start=1):
        offsets.append(len(out))
        out += b"%d 0 obj\n%s\nendobj\n" % (number, body)
    xref = len(out)
    out += b"xref\n0 %d\n0000000000 65535 f \n" % (len(objects) + 1) + b"".join(b"%010d 00000 n \n" % at for at in offsets)
    return bytes(out + b"trailer\n<< /Size %d /Root 1 0 R >>\nstartxref\n%d\n%%%%EOF\n" % (len(objects) + 1, xref))


@pytest.mark.parametrize("rotation", [90, 270])
def test_a_rotated_scanned_page_is_recognized_as_displayed_and_its_boxes_map_back(monkeypatch, rotation):
    engine = Engine()
    found = read(synthetic.pdf([[]], scanned={1}, rotation=rotation), engine, monkeypatch)
    assert engine.calls == [(2550, 3301)]  # as shown: upright, letter-sized
    assert found.passages[0].boxes["rects"] == [[0.1, 0.1, 0.9, 0.12]]


def lines_at(*items):
    """Engine lines from (top, text) or (top, text, left, confidence), each 0.012 of the page high."""
    found = []
    for top, text, *rest in items:
        left, confidence = (rest + [0.1, 0.9])[:2] if rest else (0.1, 0.9)
        found.append(ocr.Line(text, (left, top, 0.9, top + 0.012), confidence))
    return found


def test_recognized_lines_make_paragraphs_captions_and_references_as_a_text_page_does(monkeypatch):
    engine = Engine(lambda call: lines_at(
        (0.10, "Wages rose in the synthetic panel, and employ-"),
        (0.113, "ment effects were small across regions."),
        (0.15, "最低工资提高了收入，"), (0.163, "就业影响很小。"),
        (0.20, "Figure 2. Earnings by region in the scan."),
        (0.25, "References"),
        (0.27, "Smith, J. (2020). A synthetic paper."),
        (0.29, "Doe, A. (2019). Another synthetic paper.")))
    found = read(scan(), engine, monkeypatch)
    assert [(p.kind, p.text) for p in found.passages] == [
        ("paragraph", "Wages rose in the synthetic panel, and employment effects were small across regions."),
        ("paragraph", "最低工资提高了收入，就业影响很小。"),
        ("caption", "Figure 2. Earnings by region in the scan."),
        ("reference", "Smith, J. (2020). A synthetic paper."),
        ("reference", "Doe, A. (2019). Another synthetic paper.")]
    assert [len(p.boxes["rects"]) for p in found.passages] == [2, 2, 1, 1, 1]  # a box for each line
    assert all(p.section_path == ["References"] for p in found.passages[3:])
    assert not any(p.kind == "title" for p in found.passages)  # no font size: no title, no heading by size


def test_a_reference_list_on_a_text_page_continues_on_the_scanned_page_after_it(monkeypatch):
    engine = Engine(lines_at((0.1, "Roe, B. (2018). A third synthetic paper.")))
    found = read(synthetic.paper_pdf(scanned=1), engine, monkeypatch)  # its references begin on page 2
    [recognized] = [p for p in found.passages if p.page == 3]
    assert recognized.kind == "reference" and recognized.section_path == ["References"]


def test_a_second_column_starts_a_paragraph_and_every_line_is_kept_in_the_engines_order(monkeypatch):
    engine = Engine(lines_at((0.10, "Left column first line of text", 0.1), (0.113, "and its second line of text.", 0.1),
                             (0.10, "Right column first line of text", 0.55), (0.113, "and its second line of text.", 0.55)))
    found = read(scan(), engine, monkeypatch)
    assert [p.text for p in found.passages] == ["Left column first line of text and its second line of text.",
                                                "Right column first line of text and its second line of text."]


def test_a_long_recognized_paragraph_splits_at_sentences_under_the_passage_limit(monkeypatch):
    sentence = "This synthetic sentence fills a recognized line of the scan. "
    engine = Engine(lines_at(*[(0.05 + 0.0125 * n, sentence.strip()) for n in range(60)]))
    found = read(scan(), engine, monkeypatch)
    assert len(found.passages) > 1 and all(len(p.text) <= extraction.MAX_PASSAGE for p in found.passages)
    assert all(p.text.endswith(".") for p in found.passages)
    assert " ".join(p.text for p in found.passages) == " ".join([sentence.strip()] * 60)
    starts = [p.char_start for p in found.passages]
    assert starts == sorted(starts) and found.passages[0].char_start == 0  # offsets into the page's recognized text


def test_no_line_is_left_out_for_its_confidence_and_each_passage_keeps_its_lowest(monkeypatch):
    engine = Engine(lines_at((0.10, "A line read with confidence.", 0.1, 1.0),
                             (0.113, "A faint line the engine doubts", 0.1, 0.1), (0.126, "and a third.", 0.1, 0.5)))
    [passage] = read(scan(), engine, monkeypatch).passages
    assert "faint line" in passage.text and passage.boxes["ocr"] == {"confidence": 0.1}


def test_a_scanned_page_with_no_text_found_gives_no_passage(monkeypatch):
    found = read(scan(), Engine(()), monkeypatch)
    assert (found.passages, found.ocr_pages, found.status) == ([], 1, "complete")


def test_a_page_the_engine_fails_on_makes_the_reading_fail_with_its_reason(monkeypatch):
    with pytest.raises(extraction.Unreadable) as failed:
        read(scan(2), Engine(fail={2}), monkeypatch)
    assert failed.value.code == "ocr_failed"


def test_more_recognized_text_than_a_page_may_hold_is_refused(monkeypatch):
    monkeypatch.setattr(extraction, "MAX_PAGE_CHARS", 20)
    with pytest.raises(extraction.Unreadable) as refused:
        read(scan(), Engine(), monkeypatch)
    assert refused.value.code == "unreadable_file"


def test_without_an_engine_scanned_pages_wait_under_s1_13s_own_version(monkeypatch):
    use(monkeypatch, None)
    found = extraction.extract(synthetic.paper_pdf(scanned=2), extraction.PDF)
    assert (found.ocr_pages, found.status) == (2, "ocr_needed") and {p.page for p in found.passages} == {1, 2}
    assert not found.version.split("+")[-1].startswith(("vision", "test-engine"))
    use(monkeypatch, Engine())
    assert extraction.extractor_of(extraction.PDF)[1] == found.version + "+test-engine-1"  # a reading of its own


def test_pages_render_while_a_page_is_recognized(monkeypatch):
    """The PDF library's lock, held by the reading, is let go while the engine reads."""
    rendered = []

    def lines(call):
        other = threading.Thread(target=lambda: rendered.append(extraction.render_page(synthetic.paper_pdf(), 1, 0.5)))
        other.start()
        other.join(5)
        return [LINE]

    read(scan(), Engine(lines), monkeypatch)
    assert len(rendered) == 1 and rendered[0].startswith(b"\x89PNG")
    assert extraction.PDFIUM.acquire(blocking=False)  # and the reading let it go at its end
    extraction.PDFIUM.release()


def test_a_scanned_page_is_rendered_in_memory_and_no_file_is_written(monkeypatch):
    opened = []
    real_open, real_os_open = builtins.open, os.open

    def watched_open(file, mode="r", *args, **kwargs):
        if any(flag in mode for flag in "wax+"):
            opened.append(file)
        return real_open(file, mode, *args, **kwargs)

    def watched_os_open(path, flags, *args, **kwargs):
        if flags & (os.O_WRONLY | os.O_RDWR | os.O_CREAT):
            opened.append(path)
        return real_os_open(path, flags, *args, **kwargs)

    data = synthetic.paper_pdf(scanned=2)
    monkeypatch.setattr(builtins, "open", watched_open)
    monkeypatch.setattr(os, "open", watched_os_open)
    read(data, Engine(), monkeypatch)
    assert opened == []


def test_identifiers_in_recognized_text_count_on_the_first_pages_only(monkeypatch):
    first = read(scan(), Engine(lines_at((0.1, f"doi:{DOI}"))), monkeypatch)
    assert extraction.identifiers(first.passages) == [("doi", DOI)]
    later = read(scan(3), Engine(lambda call: lines_at((0.1, f"doi:{DOI}")) if call == 3 else []), monkeypatch)
    assert extraction.identifiers(later.passages) == []


def test_only_the_ocr_module_imports_vision():
    root = Path(__file__).resolve().parents[1] / "backend"
    found = set()
    for path in root.rglob("*.py"):
        for node in ast.walk(ast.parse(path.read_text())):
            names = [a.name for a in node.names] if isinstance(node, ast.Import) else \
                [node.module or ""] if isinstance(node, ast.ImportFrom) else []
            if any(name.split(".")[0] == "Vision" for name in names):
                found.add(path.name)
    assert found == {"ocr.py"}


@pytest.mark.skipif(sys.platform != "darwin", reason="Vision is macOS only")
def test_vision_reads_small_english_and_chinese_lines_of_a_scan_where_they_are_drawn():
    assert ocr.engine() is ocr.VISION
    mixed = [f"我们使用 {method} 方法估计 {outcome} 的效应。" for method in ("difference-in-differences",
             "regression discontinuity", "instrumental variable") for outcome in ("employment", "minimum wage")]
    data = scanned_pdf([(72, 700, 8, "Eight point text about synthetic minimum wages."),
                        (72, 650, 10, "Ten point text: employment effects are small."),
                        (72, 600, 10.5, "最低工资的合成研究。"),
                        *[(72, 560 - 24 * n, 10.5, line) for n, line in enumerate(mixed)]])
    found = extraction.extract(data, extraction.PDF)
    assert found.version.endswith("+vision-3") and (found.ocr_pages, found.status) == (1, "complete")
    texts = [p.text for p in found.passages]
    assert "Eight point text about synthetic minimum wages." in texts
    assert "Ten point text: employment effects are small." in texts
    assert any("最低工资" in text and "合成研究" in text for text in texts)
    for line in mixed:  # every Chinese line keeps its Han characters beside its two English terms
        assert any(line.split(" ")[0] in text and "效应" in text for text in texts), line
    eight = next(p for p in found.passages if p.text.startswith("Eight"))
    [[left, top, right, bottom]] = eight.boxes["rects"]
    assert abs(left - 72 / 612) < 0.01 and top < 1 - 700 / 792 < bottom + 0.01  # over the line as it was drawn
    assert all(0 < p.boxes["ocr"]["confidence"] <= 1 for p in found.passages)
    assert vars(ocr.VISION) == {}  # it keeps nothing between pages or readings


@pytest.mark.skipif(sys.platform != "darwin", reason="Vision is macOS only")
def test_vision_reads_a_page_with_no_chinese_again_so_its_english_lines_are_whole():
    english = ["The synthetic panel includes regional data on wages and employment for each study year.",
               "Standard errors are clustered by region; the paragraph was drawn and ends here.",
               "Table 2 reports the main estimates; columns differ in the controls that are included.",
               "doi:10.5555/scholia.scanned.001"]
    found = extraction.extract(scanned_pdf([(72, 700 - 30 * n, 10, line) for n, line in enumerate(english)]),
                               extraction.PDF)
    texts = [p.text.replace(" ", "") for p in found.passages]
    assert all(line.replace(" ", "") in texts for line in english), texts
    assert extraction.identifiers(found.passages) == [("doi", DOI)]


# Through the app


async def cite(client, material, passage, quote, page=1):
    """A synthetic citation of a passage (the answers that write citations come with S1-19)."""
    citation = new_id()
    await asyncio.to_thread(client.state["db"].write, lambda conn: conn.execute(
        "INSERT INTO citations (id, owner_kind, owner_id, material_id, passage_id, quote, page, existence)"
        " VALUES (?, 'answer', ?, ?, ?, ?, ?, 'ok')", (citation, new_id(), material, passage, quote, page)))
    return citation


@pytest.mark.asyncio
async def test_a_scanned_paper_is_ready_with_its_pages_read_by_text_recognition(tmp_path, monkeypatch, quick):
    use(monkeypatch, Engine())
    async with started(tmp_path / "data") as client:
        project = await project_of(client)
        await added(client, project, ("scan.pdf", synthetic.paper_pdf(scanned=2)))
        [paper] = await settled(client, project)
        assert (paper["state"], paper["reason"]) == ("ready", None)
        assert paper["extraction"]["status"] == "complete" and paper["extraction"]["ocr_pages"] == 2
        assert paper["extraction"]["version"].endswith("+test-engine-1")
        passages = (await client.get(f"/api/material-versions/{paper['version']['id']}/passages",
                                     params={"page": 3})).json()["passages"]
        assert [p["boxes"] for p in passages] == [{"rects": [[0.1, 0.1, 0.9, 0.12]], "ocr": {"confidence": 0.9}}]


@pytest.mark.asyncio
async def test_a_scan_in_which_no_text_is_found_needs_attention(tmp_path, monkeypatch):
    use(monkeypatch, Engine(()))
    async with started(tmp_path / "data") as client:
        project = await project_of(client)
        await added(client, project, ("scan.pdf", scan(2)))
        [paper] = await settled(client, project)
        assert (paper["state"], paper["reason"], paper["extraction"]["ocr_pages"]) == ("needs_attention", "no_text", 2)


async def earlier_library(data, monkeypatch, scholarly=None):
    """A data folder as S1-13 left it: a text PDF, and one with scanned pages waiting, read with no engine,
    in two projects that share the scanned file. Returns {name: (project, material)}."""
    use(monkeypatch, None)
    async with started(data, MockProvider(scholarly=scholarly or MockScholarly())) as client:
        first, second = await project_of(client, "First"), await project_of(client, "Second")
        [text] = (await added(client, first, ("text.pdf", synthetic.paper_pdf())))["materials"]
        scan_file = ("scan.pdf", synthetic.paper_pdf(title="A Scan", scanned=1))
        [scanned] = (await added(client, first, scan_file))["materials"]
        await settled(client, first)
        [shared] = (await added(client, second, scan_file))["materials"]
        assert shared["run_id"] is None  # the first project's reading, shared
        listed = {m["id"]: m for m in await settled(client, first) + await settled(client, second)}
        assert listed[scanned["id"]]["reason"] == "ocr_waiting" and listed[text["id"]]["state"] == "ready"
    return {"text": (first, text["id"]), "scanned": (first, scanned["id"]), "shared": (second, shared["id"])}


@pytest.mark.asyncio
async def test_papers_read_before_ocr_are_read_again_once_at_launch_and_their_old_readings_go(tmp_path, monkeypatch,
                                                                                              quick):
    data = tmp_path / "data"
    papers = await earlier_library(data, monkeypatch)
    async with started(data, setup=False) as client:
        before = dict(await rows(client, "SELECT id, extractor_version FROM extractions"))
        old_text = {sha: [t for (t,) in await rows(client, "SELECT p.text FROM passages p JOIN extractions e"
                                                         " ON e.id = p.extraction_id WHERE e.file_sha256 = ? ORDER BY ordinal", sha)]
                    for (sha,) in await rows(client, "SELECT DISTINCT file_sha256 FROM extractions")}
        old_ids = [p for (p,) in await rows(client, "SELECT id FROM passages")]
    engine = use(monkeypatch, Engine())
    async with started(data, setup=False) as client:
        await asyncio.sleep(0.3)
        runs = await rows(client, "SELECT json_extract(inputs, '$.version_id'), json_extract(inputs, '$.outdated')"
                                  " FROM runs WHERE workflow = 'extract' AND json_extract(inputs, '$.outdated') IS NOT NULL")
        assert len(runs) == 2  # one for each file: the scan's reading serves both projects' papers when it commits
        assert {mark for _, mark in runs} == {"/".join(extraction.extractor_of(extraction.PDF))}
        listed = {m["id"]: m for p in {p for p, _ in papers.values()} for m in await settled(client, p)}
        for name, (_, material) in papers.items():
            assert (listed[material]["state"], listed[material]["reason"]) == ("ready", None), name
        assert listed[papers["scanned"][1]]["extraction"]["ocr_pages"] == 1
        assert await rows(client, "SELECT count(*) FROM extractions GROUP BY file_sha256") == [(1,), (1,)]  # one each
        assert len(engine.calls) == 1  # the shared scan recognized once
        calls = len(engine.calls)
        after = await rows(client, "SELECT id FROM extractions")
        assert not {e for (e,) in after} & set(before)  # the earlier readings are gone
        for sha, texts in old_text.items():
            new = [t for (t,) in await rows(client, "SELECT p.text FROM passages p JOIN extractions e"
                                                    " ON e.id = p.extraction_id WHERE e.file_sha256 = ? ORDER BY ordinal", sha)]
            assert new[:len(texts)] == texts  # the text pages read as before; the scanned page's lines after
        assert await rows(client, f"SELECT count(*) FROM passages WHERE id IN ({','.join('?' * len(old_ids))})",
                          *old_ids) == [(0,)]
        removed = {(t, p) for t, p in await rows(client, "SELECT target_id, project_id FROM index_queue WHERE op = 'remove'")}
        first, second = papers["text"][0], papers["shared"][0]
        scanned_old = [p for (p,) in await rows(client, "SELECT target_id FROM index_queue WHERE op = 'add'"
                                                        " AND project_id = ?", second)
                       if p in old_ids]
        assert scanned_old and all((p, second) in removed for p in scanned_old)  # out of each index that had them
        assert all((p, first) in removed for p in old_ids)
    async with started(data, setup=False) as client:  # the next launch reads nothing again
        await asyncio.sleep(0.3)
        assert len(await rows(client, "SELECT id FROM runs WHERE workflow = 'extract'"
                                      " AND json_extract(inputs, '$.outdated') IS NOT NULL")) == 2
        assert len(engine.calls) == calls


@pytest.mark.asyncio
async def test_a_failed_reread_is_not_queued_again_and_retry_reads_it(tmp_path, monkeypatch, quick):
    data = tmp_path / "data"
    use(monkeypatch, None)
    async with started(data) as client:
        project = await project_of(client)
        await added(client, project, ("scan.pdf", synthetic.paper_pdf(scanned=1)))
        await settled(client, project)
    use(monkeypatch, Engine(fail={1}))
    async with started(data, setup=False) as client:
        await asyncio.sleep(0.3)
        await background_idle(client, timeout=10)
        [(run,)] = await rows(client, "SELECT id FROM runs WHERE workflow = 'extract' AND status = 'failed'")
        [paper] = await settled(client, project)
        assert (paper["state"], paper["reason"]) == ("needs_attention", "ocr_failed")
    use(monkeypatch, Engine())
    async with started(data, setup=False) as client:
        await asyncio.sleep(0.3)
        assert await rows(client, "SELECT count(*) FROM runs WHERE workflow = 'extract'") == [(2,)]  # none queued again
        again = await client.post(f"/api/runs/{run}/retry")
        assert (await run_finished(client, again.json()["run_id"]))["status"] == "succeeded"
        [paper] = await settled(client, project)
        assert paper["state"] == "ready"


@pytest.mark.asyncio
async def test_a_reread_points_citations_at_the_new_passages_or_leaves_them_unresolved(tmp_path, monkeypatch, quick):
    data = tmp_path / "data"
    papers = await earlier_library(data, monkeypatch)
    async with started(data, setup=False) as client:
        version = (await client.get(f"/api/materials/{papers['text'][1]}")).json()["version"]["id"]
        passages = (await client.get(f"/api/material-versions/{version}/passages")).json()["passages"]
        passage, text = next((p["id"], p["text"]) for p in passages if p["text"].startswith("Minimum wages raise"))
        assert any("wages" in p["text"] and p["ordinal"] < passages[[q["id"] for q in passages].index(passage)]["ordinal"]
                   for p in passages)  # an earlier passage holds the short quote too
        found = await cite(client, papers["text"][1], passage, "wages")
        gone, extracted = new_id(), passages[0]
        [(reading,)] = await rows(client, "SELECT extraction_id FROM passages WHERE id = ?", passage)
        await asyncio.to_thread(client.state["db"].write, lambda conn: conn.execute(
            "INSERT INTO passages (id, extraction_id, ordinal, page, section_path, kind, text) VALUES (?, ?, 9999, 1, '[]',"
            " 'paragraph', 'A passage the next reading does not give.')", (gone, reading)))
        lost = await cite(client, papers["text"][1], gone, "A quote no reading of it holds.")
    use(monkeypatch, Engine())
    async with started(data, setup=False) as client:
        await asyncio.sleep(0.3)
        await settled(client, papers["text"][0])
        [(now, page, existence)] = await rows(client, "SELECT passage_id, page, existence FROM citations WHERE id = ?", found)
        assert now != passage and (page, existence) == (1, "ok")
        assert (await client.get(f"/api/passages/{now}")).json()["text"] == text  # its own passage again, not the first holding "wages"
        assert await rows(client, "SELECT passage_id, quote, existence FROM citations WHERE id = ?", lost) == [
            (None, "A quote no reading of it holds.", "not_found")]
        assert (await client.get(f"/api/passages/{passage}")).status_code == 404


@pytest.mark.asyncio
@pytest.mark.parametrize("level", ["normal", "local_only", "review_locked"])
async def test_a_doi_found_only_by_ocr_is_looked_up_after_the_reread_as_the_level_allows(tmp_path, monkeypatch, quick,
                                                                                        level):
    data = tmp_path / "data"
    scholarly = MockScholarly(openalex={DOI: openalex_work(DOI, "A Scanned Paper, Resolved")})
    use(monkeypatch, None)
    async with started(data, MockProvider(scholarly=scholarly)) as client:
        if level == "review_locked":
            project = (await client.post("/api/projects", json={"name": "Review", "sensitivity": "local_only",
                                                               "review_lock": True})).json()["id"]
        else:
            project = await project_of(client, level=level)
        await added(client, project, ("scan.pdf", scan()))
        [paper] = await settled(client, project)
        assert paper["extraction"]["status"] == "ocr_needed"
        if level != "review_locked":
            assert paper["lookup"]["outcome"] == "no_identifier"
        if level == "local_only":  # the earlier batch asked nothing: it had no identifier to send
            assert (await listing(client, project))["asks"] == []
    use(monkeypatch, Engine([ocr.Line(f"doi:{DOI}", (0.1, 0.1, 0.5, 0.12), 0.5)]))
    async with started(data, MockProvider(scholarly=scholarly), setup=False) as client:
        await asyncio.sleep(0.3)
        if level == "local_only":
            deadline = asyncio.get_running_loop().time() + 10
            while not (asks := (await listing(client, project))["asks"]):
                assert asyncio.get_running_loop().time() < deadline
                await asyncio.sleep(0.02)
            assert scholarly.requests == []
            assert (await client.post(f"/api/runs/{asks[0]['run_id']}/asks/{asks[0]['ask_id']}",
                                      json={"option": "lookup"})).status_code == 200
        [paper] = await settled(client, project)
        assert paper["state"] == "ready"
        if level == "review_locked":
            assert paper["checked_by"] is None and scholarly.requests == []
        else:
            assert (paper["title"], paper["lookup"]["outcome"]) == ("A Scanned Paper, Resolved", "resolved")
            assert [path for _, path, _ in scholarly.requests] == [f"/works/doi:{DOI}"]


@pytest.mark.asyncio
async def test_reading_scanned_pages_logs_no_recognized_text_or_file_name(tmp_path, monkeypatch, caplog, quick):
    caplog.set_level(logging.DEBUG)
    secret = "Participant-Eight-Canary"
    def lines(call):
        if call == 3:  # an engine's own error, its text quoting the page: logged by its type only
            raise RuntimeError(f"{secret} could not be read")
        return [ocr.Line(f"{secret} said this on a scanned page.", (0.1, 0.1, 0.9, 0.12), 0.4)]

    use(monkeypatch, Engine(lines, fail={2}))
    async with started(tmp_path / "data") as client:
        project = await project_of(client)
        for name, data in ((f"{secret}.pdf", scan()), (f"{secret}-two.pdf", scan()), (f"{secret}-three.pdf", scan())):
            await added(client, project, (name, data))  # one at a time: the engine's calls in this order
            await settled(client, project)
        reasons = sorted(m["reason"] or "" for m in await settled(client, project))
        assert reasons == ["", "internal", "ocr_failed"]
    assert secret not in caplog.text and "RuntimeError" in caplog.text


@pytest.mark.asyncio
async def test_rereading_at_launch_works_at_every_level_and_in_a_locked_project(tmp_path, monkeypatch, quick):
    data = tmp_path / "data"
    use(monkeypatch, None)
    async with started(data) as client:
        projects = [await project_of(client, "Normal"), await project_of(client, "Local", level="local_only"),
                    (await client.post("/api/projects", json={"name": "Locked", "sensitivity": "local_only",
                                                             "review_lock": True})).json()["id"]]
        for n, project in enumerate(projects):
            await added(client, project, ("scan.pdf", synthetic.paper_pdf(title=f"Scan {n}", doi=None, scanned=1)))
        for project in projects:
            await settled(client, project)
    use(monkeypatch, Engine())
    async with started(data, setup=False) as client:
        await asyncio.sleep(0.3)
        for project in projects:
            [paper] = await settled(client, project)
            assert (paper["state"], paper["extraction"]["ocr_pages"]) == ("ready", 1), project


@pytest.mark.asyncio
async def test_a_file_being_read_or_read_by_this_version_is_not_read_again(tmp_path, monkeypatch):
    use(monkeypatch, None)  # read first with no engine
    async with started(tmp_path / "data") as client:
        project = await project_of(client)
        await added(client, project, ("scan.pdf", scan()))
        [paper] = await settled(client, project)
        engine = use(monkeypatch, Engine(hold=True))  # now outdated: its reading named no engine
        read = await client.post(f"/api/material-versions/{paper['version']['id']}/read")  # Read again, held
        assert read.status_code == 201
        await held(engine)
        await materials_module.read_outdated(client.state["harness"])  # being read: none
        assert await rows(client, "SELECT count(*) FROM runs WHERE workflow = 'extract'") == [(2,)]
        engine.go.set()
        assert (await run_finished(client, read.json()["run_id"]))["status"] == "succeeded"
        await materials_module.read_outdated(client.state["harness"])  # read by this version: none
        assert await rows(client, "SELECT count(*) FROM runs WHERE workflow = 'extract'") == [(2,)]


def test_a_page_without_chinese_keeps_its_first_reading_when_the_second_fails(monkeypatch):
    calls = []

    def read(self, bitmap, detect):
        calls.append(detect)
        if detect:
            raise ocr.Failed()
        return [LINE]

    monkeypatch.setattr(ocr.Vision, "_read", read)  # on the class: the engine object keeps no state of its own
    assert ocr.VISION.recognize(None) == [LINE] and calls == [False, True]
    monkeypatch.setattr(ocr.Vision, "_read", lambda self, bitmap, detect: [ocr.Line("最低工资", LINE.box, 0.5)]
                        if not detect else 1 / 0)
    assert ocr.VISION.recognize(None)[0].text == "最低工资"  # a page with Chinese is read once


def test_vision_is_the_engine_on_macos_and_a_copy_that_cannot_load_it_fails_the_page(monkeypatch):
    """No other version of the reading is made where Vision does not load: its scanned pages fail, to be tried again."""
    monkeypatch.setattr(sys, "platform", "darwin")
    assert ocr.engine() is ocr.VISION
    monkeypatch.setitem(sys.modules, "Vision", None)  # import Vision raises ImportError
    assert extraction.extractor_of(extraction.PDF)[1].endswith("+vision-3")
    with pytest.raises(extraction.Unreadable) as failed:
        extraction.extract(scan(), extraction.PDF)
    assert failed.value.code == "ocr_failed"
    monkeypatch.setattr(sys, "platform", "win32")
    assert ocr.engine() is None  # elsewhere, scanned pages wait for OCR (the Windows plan)
