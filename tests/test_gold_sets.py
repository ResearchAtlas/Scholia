"""The gold sets (S1-36; slice-1 spec section 14, tickets 17, 22, 70 and 72): evals/gold.py's formats and
checks, the passage worksheet read through the app, the corrected set, the second rater's sample and the
agreement, and the shapes tiers 2 and 3 are scored in. Synthetic sets only (tests/gold_fixtures.py and
synthetic PDFs made here): no real paper, no download, no model. Lifecycle cases first.
"""

import asyncio
import contextlib
import copy
import hashlib
import json
import os
import re
import stat
from pathlib import Path

import pytest

import gold_fixtures as fixtures
import synthetic_materials as synthetic
from evals import gold, tier1
from scholia_app import started

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture(autouse=True)
def quick(monkeypatch):
    monkeypatch.setattr(tier1, "POLL_SECONDS", 0.05)


def opener(seen=None, clients=None):
    """The app the commands read with: the test app (its provider and scholarly sources test-owned mocks)."""
    @contextlib.asynccontextmanager
    async def open_app(data):
        if seen is not None:
            seen.append(data)
        async with started(data, setup=False) as client:
            if clients is not None:
                clients.append(client)
            yield client, client.state
    return open_app


# Synthetic PDFs, read by the app

PAGES = {
    "2601.00011v1": [[(72, 720, 18, "Synthetic Retrieval Study"), (72, 690, 14, "1 Introduction"),
                      (72, 670, 10, "Dense retrieval found the evidence passage for 71 of 100"),
                      (72, 658, 10, "synthetic questions in the made-up corpus."),
                      (72, 630, 10, "Keyword retrieval missed paraphrased questions most often,"),
                      (72, 618, 10, "especially those written in another language."),
                      (72, 590, 14, "References"), (72, 570, 10, "Doe, A. (2020). An earlier synthetic paper.")]],
    "2601.00012v3": [[(72, 720, 18, "Synthetic Reranking Notes"), (72, 690, 14, "2 Findings"),
                      (72, 670, 10, "A reranker reordered the top fifty passages for each query."),
                      (72, 642, 10, "Reranking raised recall at eight by four points overall.")]],
}


def pdf_field(tmp_path, field=2):
    """A field folder whose paper list pins two synthetic PDFs (in tmp_path / pdfs)."""
    folder, pdfs = tmp_path / "field", tmp_path / "pdfs"
    folder.mkdir()
    pdfs.mkdir()
    papers = []
    for pid, pages in PAGES.items():
        data = synthetic.pdf(pages)
        (pdfs / f"{pid}.pdf").write_bytes(data)
        record = fixtures.paper_record(field, pid if field == 2 else pid.replace(".", "-"), pages[0][0][3], "en")
        record.update(file=f"{pid}.pdf", size=len(data), sha256=hashlib.sha256(data).hexdigest())
        papers.append(record)
    doc = {"field": field, "extractor": {"name": "pending", "version": "pending"}, "papers": papers}
    if field == 2:
        doc.update(criteria={"note": "synthetic"}, seed=1)
    write(folder / "papers.json", doc)
    return folder, pdfs


def write(path, doc):
    path.write_text(json.dumps(doc, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")


def from_sheet(sheet, papers, field, pid, text_start):
    """The locator of the worksheet passage whose text starts so (as a drafter writes it)."""
    entry = sheet["papers"][pid]
    found = next(p for p in entry["passages"] if p["text"].startswith(text_start))
    loc = {"paper": pid, "ordinal": found["ordinal"], "page": found["page"], "kind": found["kind"],
           "section_path": found["section_path"], "quote": found["text"]}
    if field == 2:
        loc.update(attribution=entry["attribution"], license=entry["license"])
    return loc


async def drafted(tmp_path, field=2):
    """A folder read by `passages`, its paper list carrying the reading's extractor, and a small set
    drafted from the worksheet: (folder, pdfs, worksheet)."""
    folder, pdfs = pdf_field(tmp_path, field)
    sheet = await gold.passages(field, folder, pdfs, opener())
    doc = json.loads((folder / "papers.json").read_text())
    doc["extractor"] = sheet["extractor"]
    write(folder / "papers.json", doc)
    a, b = (p["id"] for p in doc["papers"])
    papers = {p["id"]: p for p in doc["papers"]}
    loc = lambda pid, start: from_sheet(sheet, papers, field, pid, start)  # noqa: E731
    write(folder / "questions.json", {"id": "t-questions", "kind": "questions", "field": field, "provisional": True, "items": [
        {"id": "q1", "text": "How many synthetic questions did dense retrieval find?", "query_language": "en",
         "document_language": "en", "answerable": True, "short_answer": "71 of 100",
         "evidence": [[loc(a, "Dense retrieval")]]},
        {"id": "q2", "text": "重排序把召回率提高了多少？", "query_language": "zh-CN", "document_language": "en",
         "answerable": True, "short_answer": "四个百分点", "evidence": [[loc(b, "Reranking raised")], [loc(a, "Keyword")]]},
        {"id": "q3", "text": "Who funded the synthetic study?", "query_language": "en", "document_language": "en",
         "answerable": False, "evidence": []}]})
    write(folder / "pairs.json", {"id": "t-pairs", "kind": "pairs", "field": field, "provisional": True, "items": [
        {"id": "p1", "sentence": "Most synthetic questions had their evidence found by dense search.",
         "sentence_language": "en", "passage": loc(a, "Dense retrieval"), "drafted_category": "supported"}]})
    return folder, pdfs, sheet


# Lifecycle


@pytest.mark.asyncio
async def test_an_interrupted_worksheet_leaves_an_incomplete_marker_and_no_data_folder(tmp_path, monkeypatch):
    folder, pdfs = pdf_field(tmp_path)
    reached, seen = asyncio.Event(), []

    async def held(state):
        reached.set()
        await asyncio.sleep(3600)
    monkeypatch.setattr(tier1, "until_read", held)
    (gold.outputs(folder)).mkdir()
    (gold.outputs(folder) / "worksheet.json").write_text(json.dumps({"status": "finished"}))  # an earlier one
    task = asyncio.ensure_future(gold.passages(2, folder, pdfs, opener(seen)))
    await asyncio.wait_for(reached.wait(), 60)
    assert json.loads((gold.outputs(folder) / "worksheet.json").read_text())["status"] == "incomplete"
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    written = json.loads((gold.outputs(folder) / "worksheet.json").read_text())
    assert written == {"status": "incomplete", "reason": "CancelledError: "}
    assert seen and not seen[0].parent.exists()  # the temporary data folder is gone
    assert not list(gold.outputs(folder).glob("*.part"))


@pytest.mark.asyncio
async def test_a_failing_check_leaves_an_incomplete_report(tmp_path, monkeypatch, qasper_pins):
    folder, pdfs, _ = await drafted(tmp_path)
    pin_qasper(qasper_pins, qasper_folder(tmp_path))

    async def failed(*args):
        raise RuntimeError("the reading child died")
    monkeypatch.setattr(gold, "read_papers", failed)
    with pytest.raises(RuntimeError):
        await gold.check(2, folder, pdfs, qasper_folder(tmp_path), opener())
    report = json.loads((gold.outputs(folder) / "check.json").read_text())
    assert report == {"status": "incomplete", "reason": "RuntimeError: the reading child died"}


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["missing", "size", "sha256"])
async def test_a_pdf_missing_or_differing_from_its_pin_is_refused_before_anything_is_read(tmp_path, change):
    folder, pdfs = pdf_field(tmp_path)
    path = pdfs / "2601.00012v3.pdf"
    if change == "missing":
        path.unlink()
    elif change == "size":
        path.write_bytes(path.read_bytes() + b"\n")
    else:
        data = bytearray(path.read_bytes())
        data[-2] ^= 1
        path.write_bytes(bytes(data))
    seen = []
    with pytest.raises(tier1.Refused, match="2601.00012v3"):
        await gold.passages(2, folder, pdfs, opener(seen))
    assert seen == []  # no app, no project: refused before anything is built
    assert json.loads((gold.outputs(folder) / "worksheet.json").read_text())["status"] == "incomplete"


@pytest.mark.asyncio
async def test_a_paper_the_app_cannot_read_is_reported_and_its_locators_refused(tmp_path, reading_stub, qasper_pins):
    folder, pdfs, _ = await drafted(tmp_path)
    pin_qasper(qasper_pins, qasper_folder(tmp_path))
    reading_stub("exit", 1)  # every reading from here on fails
    report = await gold.check(2, folder, pdfs, qasper_folder(tmp_path), opener())
    assert set(report["not_read_whole"]) == {"2601.00011v1", "2601.00012v3"}
    assert any("was not read whole" in p and "its locators are refused" in p for p in report["problems"])
    assert report["resolved"] is False
    sheet = await gold.passages(2, folder, pdfs, opener())
    assert sheet["status"] == "incomplete" and set(sheet["not_read_whole"]) == {"2601.00011v1", "2601.00012v3"}


# The worksheet and locators (the app's reading)


def qasper_folder(tmp_path, ids=("1503.00841",)):
    """QASPER-shaped split files under their pins' paths, and settings pinning them."""
    folder = tmp_path / "qasper"
    for name in ("train", "dev", "test"):
        path = folder / "extracted" / f"qasper-{name}-v0.3.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({i: {} for i in ids} if name == "dev" else {}))
    return folder


@pytest.fixture
def qasper_pins(monkeypatch):
    """The settings with the synthetic QASPER files' pins in place of the real ones."""
    real = gold.load_settings()

    def pinned(path=None):
        settings = copy.deepcopy(real)
        settings["qasper"]["files"] = [{"path": f"extracted/qasper-{n}-v0.3.json", **pinned.pins.get(n, {})}
                                       for n in ("train", "dev", "test")]
        return settings
    pinned.pins = {}
    monkeypatch.setattr(gold, "load_settings", pinned)
    return pinned


def pin_qasper(qasper_pins, folder):
    for name in ("train", "dev", "test"):
        data = (folder / "extracted" / f"qasper-{name}-v0.3.json").read_bytes()
        qasper_pins.pins[name] = {"size": len(data), "sha256": hashlib.sha256(data).hexdigest()}


@pytest.mark.asyncio
async def test_the_worksheet_lists_every_passage_as_the_app_reads_it_and_marks_references(tmp_path):
    clients = []
    folder, pdfs = pdf_field(tmp_path)
    sheet = await gold.passages(2, folder, pdfs, opener(clients=clients))
    assert sheet["status"] == "finished" and sheet["not_read_whole"] == {}
    from backend import extraction
    assert sheet["extractor"] == dict(zip(("name", "version"), extraction.extractor_of(extraction.PDF)))
    a = sheet["papers"]["2601.00011v1"]
    assert a["attribution"] == "A. Synthetic, B. Example. Synthetic Retrieval Study. arXiv:2601.00011v1"
    assert a["license"] == "http://creativecommons.org/licenses/by/4.0/"
    assert [p["ordinal"] for p in a["passages"]] == list(range(len(a["passages"])))
    assert {p["page"] for p in a["passages"]} == {1}
    refs = [p for p in a["passages"] if p["kind"] == "reference"]
    assert refs and not any(p["usable"] for p in refs) and all(p["usable"] for p in a["passages"] if p["kind"] != "reference")
    # The app as the desktop runs it, review-locked: nothing was looked up, nothing left the process.
    assert clients[0].provider.requests == [] and clients[0].provider.scholarly.requests == []
    # The same inputs give the same worksheet, byte for byte.
    first = (gold.outputs(folder) / "worksheet.json").read_bytes()
    await gold.passages(2, folder, pdfs, opener())
    assert (gold.outputs(folder) / "worksheet.json").read_bytes() == first
    assert stat.S_IMODE((gold.outputs(folder) / "worksheet.json").stat().st_mode) == 0o600


@pytest.mark.asyncio
async def test_every_locator_resolves_and_a_quote_matching_no_passage_is_refused(tmp_path, qasper_pins):
    folder, pdfs, _ = await drafted(tmp_path)
    qasper = qasper_folder(tmp_path)
    pin_qasper(qasper_pins, qasper)
    report = await gold.check(2, folder, pdfs, qasper, opener())
    assert report["problems"] == [] and report["resolved"] is True and report["moved"] == []
    doc = json.loads((folder / "questions.json").read_text())
    loc = doc["items"][0]["evidence"][0][0]
    loc["quote"] = "  Dense   retrieval found the evidence passage for 71 of 100\nsynthetic questions in the made-up corpus. "
    write(folder / "questions.json", doc)
    assert (await gold.check(2, folder, pdfs, qasper, opener()))["problems"] == []  # spacing is not text
    loc["quote"] = "Dense retrieval found the evidence passage for 72 of 100 synthetic questions in the made-up corpus."
    write(folder / "questions.json", doc)
    report = await gold.check(2, folder, pdfs, qasper, opener())
    assert report["problems"] == ["question q1 evidence item 1: the quote is not passage 1 of 2601.00011v1"]
    assert report["resolved"] is False


@pytest.mark.asyncio
async def test_with_another_extractor_version_moved_locators_are_reported_and_unmatched_ones_refused(tmp_path, qasper_pins):
    folder, pdfs, _ = await drafted(tmp_path)
    qasper = qasper_folder(tmp_path)
    pin_qasper(qasper_pins, qasper)
    doc = json.loads((folder / "papers.json").read_text())
    doc["extractor"] = {"name": "pdf", "version": "pdf-2+older"}  # the set was drafted against another reading
    write(folder / "papers.json", doc)
    questions = json.loads((folder / "questions.json").read_text())
    questions["items"][0]["evidence"][0][0]["ordinal"] = 7  # where the older reading had it
    write(folder / "questions.json", questions)
    report = await gold.check(2, folder, pdfs, qasper, opener())
    assert report["problems"] == []
    assert report["moved"] == ["question q1 evidence item 1: 2601.00011v1 passage 7 is now [1]"]
    assert report["extractor_now"] and report["extractor_now"] != [["pdf", "pdf-2+older"]]
    questions["items"][0]["evidence"][0][0]["quote"] = "A paragraph no reading of this paper has."
    write(folder / "questions.json", questions)
    report = await gold.check(2, folder, pdfs, qasper, opener())
    assert report["problems"] == ["question q1 evidence item 1: the quote matches no passage of 2601.00011v1 as read now"]
    # The set file is never rewritten by the check.
    assert json.loads((folder / "questions.json").read_text()) == questions


@pytest.mark.asyncio
async def test_a_qasper_paper_is_refused_whatever_its_version(tmp_path, qasper_pins):
    folder, pdfs, _ = await drafted(tmp_path)
    qasper = qasper_folder(tmp_path, ids=("2601.00012",))
    pin_qasper(qasper_pins, qasper)
    report = await gold.check(2, folder, pdfs, qasper, opener())
    assert "paper 2601.00012v3: a QASPER paper (left out of field 2)" in report["problems"]
    (qasper / "extracted" / "qasper-train-v0.3.json").write_text("{}\n")  # differs from its pin
    with pytest.raises(tier1.Refused, match="QASPER"):
        await gold.check(2, folder, pdfs, qasper, opener())
    with pytest.raises(tier1.Refused, match="--qasper"):
        await gold.check(2, folder, pdfs, None, opener())


def test_the_real_qasper_pins_are_the_three_splits_and_the_test_split_matches_tier_one():
    pins = {p["path"]: p for p in gold.load_settings()["qasper"]["files"]}
    assert set(pins) == {f"extracted/qasper-{n}-v0.3.json" for n in ("train", "dev", "test")}
    assert pins["extracted/qasper-test-v0.3.json"] == tier1.load_manifest()["benchmarks"]["qasper"]["files"]["test"]


# Formats (synthetic sets in the fixtures' folders)


def field(tmp_path, number=2, sample=False):
    return fixtures.write_field(tmp_path / f"field{number}", number, sample).resolve()


def problems_of(folder, number=2):
    report = asyncio.run(gold.check(number, folder, settings=gold.load_settings()))
    return report["problems"], report


def edit_set(folder, kind, change):
    doc = json.loads((folder / f"{kind}.json").read_text())
    change(doc["items"])
    write(folder / f"{kind}.json", doc)


def test_the_synthetic_sets_pass_and_the_report_gives_their_mix(tmp_path):
    folder = field(tmp_path, sample=True)
    problems, report = problems_of(folder)
    assert problems == []
    q, p = report["sets"]["questions"], report["sets"]["pairs"]
    assert q["query_languages"] == {"en": 3, "zh-CN": 3} and q["unanswerable"] == {"en": 1, "zh-CN": 1}
    assert q["multi_paper"] == {"en": 1} and q["per_paper"] == {"2601.00001v1": 4, "2601.00002v2": 1}
    assert p["drafted_categories"] == {"partly": 2, "supported": 2, "unsupported": 2}
    assert p["passages_cited_more_than_once"] == 1  # one passage in two pairs: allowed, reported
    assert "questions: 6 items, target 100" in report["differences"] and "2 papers, target 24" in report["differences"]
    assert report["topic_groups"] == 1 and "1 topic groups, target 4 to 6" in report["differences"]  # the settings' targets: reported, not refused
    assert report["samples"] == {"questions": 1, "pairs": 1}
    assert report["resolved"] is False  # no PDFs given: formats only, said so


@pytest.mark.parametrize("kind, change, expected", [
    ("questions", lambda items: items[0].pop("short_answer"), "question q01: an answerable question has a short answer"),
    ("questions", lambda items: items[0].update(evidence=[]), "question q01: an answerable question has evidence"),
    ("questions", lambda items: items[2].update(evidence=items[0]["evidence"]), "question q03: an unanswerable question has no evidence"),
    ("questions", lambda items: items[2].update(short_answer="x"), "question q03: an unanswerable question has no short answer"),
    ("questions", lambda items: items[0].update(query_language="zh-CN"), "question q01: marked zh-CN but holds no Chinese characters"),
    ("questions", lambda items: items[3].update(query_language="en"), "question q04: marked en but holds Chinese characters"),
    ("questions", lambda items: items[3].update(text="長段落討論了什麼？"), "question q04: marked zh-CN (Simplified) but holds"),
    ("questions", lambda items: items[0].update(query_language="fr"), "question q01: language 'fr' is not one of"),
    ("questions", lambda items: items[1].update(id="q01"), "questions: id 'q01' is used 2 times"),
    ("questions", lambda items: items[1].update(text=items[0]["text"].upper()), "questions: q01, q02 have the same text"),
    ("questions", lambda items: items[0]["evidence"][0][0].update(kind="reference"), "a reference passage is never evidence"),
    ("questions", lambda items: items[0]["evidence"][0][0].update(char_start=3), "char_start is not a field of this format"),
    ("questions", lambda items: items[0]["evidence"][0][0].pop("attribution"), "question q01 evidence item 1: attribution missing"),
    ("questions", lambda items: items[0]["evidence"][0][0].update(attribution="Someone else"), "attribution is not its paper's"),
    ("questions", lambda items: items[0]["evidence"][0][0].pop("license"), "question q01 evidence item 1: license missing"),
    ("questions", lambda items: items[0]["evidence"][0][0].update(license="http://creativecommons.org/licenses/by-nc-sa/4.0/"),
     "license is not its paper version's"),
    ("questions", lambda items: items[0]["evidence"][0][0].update(paper="2601.09999v1"), "is not in papers.json"),
    ("questions", lambda items: items[0].update(document_language="zh-CN"), "document_language is not its evidence's papers'"),
    ("questions", lambda items: items[0].update(text="x" * 1001), "question q01 text: longer than 1000 characters"),
    ("questions", lambda items: items[0].update(provisional=True), "provisional is not a field of this format"),
    ("questions", lambda items: items[0].update(id=["q01"]), "questions item ['q01']: id is not a plain name"),
    ("questions", lambda items: items[0]["evidence"][0][0].update(ordinal=True), "evidence item 1: ordinal is not a passage's"),
    ("questions", lambda items: items[0]["evidence"][0][0].update(page=True), "evidence item 1: page is not a page number"),
    # T1: an evidence item is one passage or the consecutive passages of one paper
    ("questions", lambda items: items[1]["evidence"][0].append(items[1]["evidence"][1][0]),
     "question q02 evidence item 1: not one passage or the consecutive passages of one paper"),
    ("questions", lambda items: items[1]["evidence"][0].append(items[0]["evidence"][0][0]),
     "question q02 evidence item 1: not one passage or the consecutive passages of one paper"),
    ("questions", lambda items: items[1]["evidence"][1].append(items[1]["evidence"][1][0]),
     "question q02 evidence item 2: not one passage or the consecutive passages of one paper"),
    # G11: an id is text of the allowed shape, kept as validated (a number is not its text)
    ("pairs", lambda items: items[0].update(id=123), "pairs item 123: id is not a plain name"),
    ("pairs", lambda items: items[0].update(id=True), "pairs item True: id is not a plain name"),
    ("pairs", lambda items: items[0].update(drafted_category="maybe"), "pair p01: drafted_category 'maybe' is not one of"),
    ("pairs", lambda items: items[0].update(verdict="supported"), "pair p01: verdict is not a field of this format"),
    ("pairs", lambda items: items[0].update(judge={"support": "supported"}), "pair p01: judge is not a field of this format"),
    ("pairs", lambda items: items[0].pop("sentence_language"), "pair p01: sentence_language missing"),
    ("pairs", lambda items: items[0]["passage"].update(kind="reference"), "a reference passage is never evidence"),
    ("pairs", lambda items: items[0].update(sentence="Indeed: " + items[0]["passage"]["quote"]),
     "pair p01: the sentence copies its passage"),
])
def test_each_rule_refuses_naming_the_item(tmp_path, kind, change, expected):
    folder = field(tmp_path)
    edit_set(folder, kind, change)
    problems, _ = problems_of(folder)
    assert any(expected in p for p in problems), problems


def test_a_chinese_text_with_english_terms_and_full_width_punctuation_is_chinese():
    assert gold.language_problem("BM25 和 dense 检索的 recall@8 分别是多少？（合成数据）", "zh-CN") is None
    assert gold.language_problem("What is recall@8?（synthetic）", "en") is None


def test_a_sentence_sharing_a_short_span_with_its_passage_is_its_own(tmp_path):
    folder = field(tmp_path)
    edit_set(folder, "pairs", lambda items: items[0].update(sentence="Dense retrieval found evidence for most synthetic questions."))
    assert problems_of(folder)[0] == []


def test_drafts_are_provisional_and_a_set_names_its_own_kind_and_field(tmp_path):
    folder = field(tmp_path)
    doc = json.loads((folder / "questions.json").read_text())
    write(folder / "questions.json", {**doc, "provisional": False})
    assert "questions.json: drafts are provisional until a rater's labels make them gold" in problems_of(folder)[0]
    write(folder / "questions.json", {**doc, "field": 1})
    assert any("is not this set's" in p for p in problems_of(folder)[0])
    for header in ({"field": 2.0}, {"id": 123}, {"id": ["field2-questions"]}):  # G11: kept as validated, not as compared
        write(folder / "questions.json", {**doc, **header})
        assert any("is not this set's" in p for p in problems_of(folder)[0]), header


@pytest.mark.parametrize("change, expected", [
    (lambda p: p.update(license="http://arxiv.org/licenses/nonexclusive-distrib/1.0/"), "is neither CC BY 4.0 nor CC0 1.0"),
    (lambda p: p.update(license="http://creativecommons.org/licenses/by-sa/4.0/"), "is neither CC BY 4.0 nor CC0 1.0"),
    (lambda p: p.update(license_page="https://arxiv.org/abs/2601.00001"), "from that version's own abstract page"),
    (lambda p: p.update(id="2601.00001"), "not an arXiv id with its version"),
    (lambda p: p.update(primary_category="cs.LG"), "primary category 'cs.LG'"),
    (lambda p: p.update(submitted="2025-12-31"), "is outside 2026-01-01 to 2026-09-30"),
    (lambda p: p.pop("sha256"), "sha256 missing"),
    (lambda p: p.update(authors=[]), "authors missing"),
    (lambda p: p.update(document_language="zh-CN"), "field 2's papers are English"),
    (lambda p: p.pop("topic"), "topic missing"),
    (lambda p: p.update(id=["2601.00001v1"]), "not an arXiv id with its version"),  # G11: refused, not a crash
    (lambda p: p.update(size=True), "size or SHA-256 missing"),
    (lambda p: p.update(file=123), "file is not a file name"),  # refused, not a crash at the PDFs' path
    (lambda p: p.update(file="../elsewhere/a.pdf"), "file is not a file name"),
    (lambda p: p.update(file="/elsewhere/a.pdf"), "file is not a file name"),
    (lambda p: p.update(license_checked="２０２６-１０-０１"), "license_checked is not a date"),  # full-width digits
    (lambda p: p.update(submitted=["2026-01-05"]), "is outside 2026-01-01 to 2026-09-30"),
    (lambda p: p.update(submitted="2026-02-31"), "is outside 2026-01-01 to 2026-09-30"),  # T12: no such day
    (lambda p: p.update(license_checked="2026-99-99"), "license_checked is not a date"),
])
def test_the_paper_list_holds_only_versions_meeting_the_criteria(tmp_path, change, expected):
    folder = field(tmp_path)
    doc = json.loads((folder / "papers.json").read_text())
    change(doc["papers"][0])
    write(folder / "papers.json", doc)
    assert any(expected in p for p in problems_of(folder)[0]), problems_of(folder)[0]
    doc = json.loads((folder / "papers.json").read_text())
    write(folder / "papers.json", {k: v for k, v in doc.items() if k != "seed"})
    assert "papers.json: seed missing" in problems_of(folder)[0]


@pytest.mark.parametrize("number", [1, 2])
def test_an_empty_paper_list_is_refused_and_never_skips_the_sets_checks(tmp_path, number):
    """T6: papers: [] gave a finished report with no problems, however malformed questions.json was."""
    folder = field(tmp_path, number)
    doc = json.loads((folder / "papers.json").read_text())
    write(folder / "papers.json", {**doc, "papers": []})
    edit_set(folder, "questions", lambda items: items.append(5))
    assert "papers.json: papers is empty" in problems_of(folder, number)[0]


def test_field_one_takes_the_same_format_without_licenses_and_with_chinese_documents(tmp_path):
    folder = field(tmp_path, 1)
    problems, report = problems_of(folder, 1)
    assert problems == []
    doc = json.loads((folder / "questions.json").read_text())
    assert doc["items"][1]["document_language"] == "zh-CN" and doc["items"][1]["query_language"] == "zh-CN"
    assert "attribution" not in doc["items"][0]["evidence"][0][0]
    assert all(stat.S_IMODE(p.stat().st_mode) == 0o600 for p in gold.outputs(folder).iterdir())
    assert stat.S_IMODE(gold.outputs(folder).stat().st_mode) == 0o700


@pytest.mark.parametrize("where", ["evals/gold/field1", "tests/field1", "."])
def test_field_one_refuses_a_folder_inside_the_repository_but_evals_local(where):
    with pytest.raises(tier1.Refused, match="inside the repository"):
        gold.field_folder(1, ROOT / where)
    with pytest.raises(tier1.Refused, match="--folder"):
        gold.field_folder(1, None)
    assert gold.field_folder(1, ROOT / "evals" / "local" / "harold") == ROOT / "evals" / "local" / "harold"
    with pytest.raises(SystemExit, match="inside the repository"):
        gold.main(["check", "--field", "1", "--folder", str(ROOT / where)])
    with pytest.raises(SystemExit, match="inside the repository"):
        gold.main(["passages", "--field", "1", "--folder", "/tmp/x", "--pdfs", str(ROOT / where)])


def test_the_labels_format_refuses_another_set_or_rater_and_a_missing_field(tmp_path):
    folder = field(tmp_path)
    doc = json.loads((folder / "pairs.json").read_text())
    papers = {p["id"]: p for p in json.loads((folder / "papers.json").read_text())["papers"]}
    good = {"set": doc["id"], "kind": "pairs", "field": 2, "rater": "harold", "set_sha256": "x",
            "labels": {"p01": {"item_sha256": gold.item_sha256(doc["items"][0]), "revision": 1, "saved_at": "t",
                               "category": "supported"}}}
    assert gold.check_labels(good, doc, papers, "harold") == []
    assert "they are rater 'harold''s" in gold.check_labels(good, doc, papers, "lee")[0]
    assert "not this set's" in gold.check_labels({**good, "set": "field2-questions"}, doc, papers)[0]
    broken = copy.deepcopy(good)
    del broken["labels"]["p01"]["saved_at"]
    assert gold.check_labels(broken, doc, papers) == ["label p01: saved_at missing"]
    broken["labels"]["p01"].update(saved_at="t", category="maybe")
    assert gold.check_labels(broken, doc, papers) == ["label p01: category 'maybe'"]


def test_check_refuses_a_labels_file_whose_header_names_another_rater_than_its_name(tmp_path):
    """T10: field2-pairs.harold.json holding lee's header passed check, while review and apply for harold refuse it."""
    folder = field(tmp_path)
    pairs = json.loads((folder / "pairs.json").read_text())
    (folder / "labels").mkdir()
    write(folder / "labels" / f"{pairs['id']}.harold.json", labels_for(pairs, "lee", {}))
    assert f"{pairs['id']}.harold.json: labels: they are rater 'lee''s, not 'harold''s" in problems_of(folder)[0]
    write(folder / "labels" / f"{pairs['id']}.harold.json", labels_for(pairs, "harold", {}))
    assert problems_of(folder)[0] == []


# Labels, the corrected set, the sample and the agreement


def papers_by_id(folder):
    return {p["id"]: p for p in json.loads((folder / "papers.json").read_text())["papers"]}


def edit(item, **changes):
    """A question's edit label as the page stores it: the whole question, as the rater left it."""
    out = {"decision": "edit", **{k: item[k] for k in ("text", "short_answer", "answerable", "evidence") if k in item},
           **changes}
    if not out["answerable"]:
        out.pop("short_answer", None)
        out["evidence"] = []
    return out


def labels_for(doc, rater, decisions):
    return {"set": doc["id"], "kind": doc["kind"], "field": doc["field"], "rater": rater, "set_sha256": "x",
            "labels": {i["id"]: {"item_sha256": gold.item_sha256(i), "revision": 1, "saved_at": "t", **decisions[i["id"]]}
                       for i in doc["items"] if i["id"] in decisions}}


def test_the_corrected_set_applies_done_labels_and_keeps_the_rest_provisional(tmp_path):
    folder = field(tmp_path)
    doc = json.loads((folder / "questions.json").read_text())
    labels = labels_for(doc, "harold", {
        "q01": {"decision": "keep"}, "q02": {"decision": "drop"},
        "q03": edit(doc["items"][2], answerable=True, short_answer="Not said", evidence=doc["items"][0]["evidence"]),
        "q04": edit(doc["items"][3], answerable=False), "q05": {"decision": "keep"}})
    labels["labels"]["q05"]["item_sha256"] = "an older version of the item"
    labels["labels"]["gone"] = {"item_sha256": "x", "revision": 1, "saved_at": "t", "decision": "keep"}
    papers = papers_by_id(folder)
    corrected = gold.apply(doc, labels, papers)
    by_id = {i["id"]: i for i in corrected["items"]}
    assert corrected["counts"] == {"labeled": 3, "dropped": 1, "changed": 1, "provisional": 1, "orphaned": 1}
    assert "q02" not in by_id and by_id["q01"]["provisional"] is False and by_id["q01"]["text"] == doc["items"][0]["text"]
    assert by_id["q03"]["answerable"] and by_id["q03"]["short_answer"] == "Not said"
    assert by_id["q04"]["answerable"] is False and by_id["q04"]["evidence"] == [] and "short_answer" not in by_id["q04"]
    assert by_id["q05"]["provisional"] is True and by_id["q06"]["provisional"] is True  # changed, and not labeled
    every_drop = labels_for(doc, "harold", {i["id"]: {"decision": "drop"} for i in doc["items"]})
    assert gold.apply(doc, every_drop, papers)["items"] == [] and gold.apply(doc, every_drop, papers)["counts"]["dropped"] == 6
    nothing = gold.apply(doc, labels_for(doc, "harold", {}), papers)
    assert nothing["counts"]["provisional"] == 6 and all(i["provisional"] for i in nothing["items"])


def test_the_sample_is_a_fifth_stratified_and_the_same_for_the_same_seed():
    settings = gold.load_settings()
    items = []
    for n in range(100):
        language = "en" if n % 2 else "zh-CN"
        answerable = n % 5 != 0
        items.append({"id": f"q{n:03}", "query_language": language, "answerable": answerable,
                      "evidence": [[{"paper": "a", "ordinal": 1}], [{"paper": "b" if n % 10 == 1 else "a", "ordinal": 2}]]
                      if answerable else []})
    doc = {"id": "s", "kind": "questions", "items": items}
    sample = gold.draw(doc, settings)
    assert len(sample["items"]) == 20 and len(set(sample["items"])) == 20
    assert sample == gold.draw(doc, settings)  # the same seed, the same draw
    drawn = {i["id"]: i for i in items if i["id"] in sample["items"]}
    assert sum(i["query_language"] == "en" for i in drawn.values()) == 10
    assert sum(not i["answerable"] for i in drawn.values()) == 4
    assert sample["drawn"]["en|True|True"] == {"items": 10, "drawn": 2}
    other = gold.draw(doc, {**settings, "sample": {**settings["sample"], "seed": 1}})
    assert other["items"] != sample["items"] and len(other["items"]) == 20


def test_pair_agreement_gives_percent_kappa_and_the_confusion_table(tmp_path):
    doc = {"id": "s", "kind": "pairs", "field": 2,
           "items": [{"id": f"p{n}", "sentence": str(n)} for n in range(4)]}

    def score(a, b):
        return gold.agreement(doc, labels_for(doc, "harold", {f"p{n}": {"category": c} for n, c in enumerate(a)}),
                              labels_for(doc, "lee", {f"p{n}": {"category": c} for n, c in enumerate(b)}))
    same = score(["supported", "partly", "unsupported", "supported"], ["supported", "partly", "unsupported", "supported"])
    assert same["both_labeled"] == 4 and same["percent_agreement"] == 100.0 and same["kappa"] == 1.0
    chance = score(["supported", "supported", "unsupported", "unsupported"], ["supported", "unsupported", "supported", "unsupported"])
    assert chance["percent_agreement"] == 50.0 and chance["kappa"] == 0.0  # 'partly' unused by both
    assert chance["confusion"]["supported"] == {"supported": 1, "partly": 0, "unsupported": 1}
    lopsided = score(["supported", "supported", "supported", "unsupported"], ["supported", "supported", "unsupported", "unsupported"])
    # p_o = 3/4; p_e = (3/4)(2/4) + (1/4)(2/4) = 1/2; kappa = (3/4 - 1/2) / (1 - 1/2) = 0.5
    assert lopsided["kappa"] == 0.5
    one = score(["supported"] * 4, ["supported"] * 4)
    assert one["kappa"] is None and one["percent_agreement"] == 100.0  # kappa undefined, said so
    partial = gold.agreement(doc, labels_for(doc, "harold", {"p0": {"category": "partly"}}), labels_for(doc, "lee", {}))
    assert partial["both_labeled"] == 0 and partial["kappa"] is None


def test_question_agreement_covers_keep_or_drop_answerable_and_evidence(tmp_path):
    folder = field(tmp_path)
    doc = json.loads((folder / "questions.json").read_text())
    first = labels_for(doc, "harold", {"q01": {"decision": "keep"}, "q02": {"decision": "keep"},
                                       "q03": {"decision": "drop"}, "q04": {"decision": "keep"}})
    second = labels_for(doc, "lee", {"q01": {"decision": "keep"},
                                     "q02": edit(doc["items"][1], evidence=doc["items"][1]["evidence"][:1]),
                                     "q03": {"decision": "keep"}, "q04": edit(doc["items"][3], answerable=False)})
    out = gold.agreement(doc, first, second)
    assert out["kept"]["both_labeled"] == 4 and out["kept"]["percent_agreement"] == 75.0
    assert out["answerable"]["both_labeled"] == 3 and out["answerable"]["percent_agreement"] == round(200 / 3, 2)
    # q01 the same evidence; q02: one item of two, one passage of three (the split paragraph's two out)
    assert out["evidence"] == {"both_answerable": 2, "same_items": 1, "mean_passage_overlap": round((1 + 1 / 3) / 2, 4)}


# Tiers 2 and 3


@pytest.mark.asyncio
async def test_tier_two_takes_a_corrected_sets_gold_answerable_questions_in_tier_ones_shape(tmp_path):
    folder, pdfs, _ = await drafted(tmp_path)
    doc = json.loads((folder / "questions.json").read_text())
    with pytest.raises(tier1.Refused, match="never the drafts"):
        gold.tier2_queries(doc, {}, "p")
    corrected = gold.apply(doc, labels_for(doc, "harold", {"q1": {"decision": "keep"}, "q3": {"decision": "keep"}}),
                           papers_by_id(folder))
    papers = json.loads((folder / "papers.json").read_text())["papers"]
    async with opener()(tmp_path / "data") as (client, state):
        read, project = await gold.read_papers(client, state, papers, pdfs, 2)
        passage_of = {(pid, p["ordinal"]): p["id"] for pid, r in read.items() for p in r["passages"]}
        queries = gold.tier2_queries(corrected, passage_of, project)
        assert [q["key"] for q in queries] == ["q1"]  # q2 is provisional (unlabeled), q3 unanswerable
        out = await tier1.run_arms(state, {"keyword_only": tier1.keyword_only}, queries,
                                   lambda answer, query: [r["passage_id"] for r in answer["results"]])
    scores = gold.tier2_scores(queries, out["keyword_only"]["rankings"])
    assert scores["scores"] == [1.0] and scores["recall@8"]["query_language"] == {"en": {"mean": 1.0, "n": 1}}


def test_tier_two_recall_at_eight_follows_tier_ones_item_rule_by_language():
    queries = [{"annotators": [[["a", "b"], ["c"]]], "query_language": "en", "document_language": "en"},
               {"annotators": [[["d"], ["e"]]], "query_language": "zh-CN", "document_language": "en"},
               {"annotators": [[["f"]]], "query_language": "zh-CN", "document_language": "zh-CN"}]
    rankings = [["x", "b", "y"], ["d"] + ["z"] * 7 + ["e"], ["f"]]  # a split item found by one of its passages
    scores = gold.tier2_scores(queries, rankings)
    assert scores["scores"] == [0.5, 0.5, 1.0]
    assert scores["recall@8"]["query_language"] == {"en": {"mean": 0.5, "n": 1}, "zh-CN": {"mean": 0.75, "n": 2}}
    assert scores["recall@8"]["document_language"] == {"en": {"mean": 0.5, "n": 2}, "zh-CN": {"mean": 1.0, "n": 1}}


def test_the_judge_is_scored_against_labeled_pairs_with_unchecked_counted_apart():
    labels = {**{f"u{n}": "unsupported" for n in range(10)}, **{f"s{n}": "supported" for n in range(10)}, "m": "partly"}
    verdicts = {**{f"u{n}": "unsupported" for n in range(8)}, "u8": "unchecked", "u9": "supported",
                **{f"s{n}": "supported" for n in range(8)}, "s8": "unsupported", "s9": "partly", "m": "unsupported"}
    out = gold.judge_scores(labels, verdicts)
    assert out["unsupported"] == {"labeled": 10, "flagged": 8, "share": 0.8, "unchecked": 1, "no_verdict": 0}
    assert out["supported"] == {"labeled": 10, "flagged": 1, "share": 0.1, "unchecked": 0, "no_verdict": 0}
    assert out["meets"] is True
    verdicts["u7"] = "unchecked"
    assert gold.judge_scores(labels, verdicts)["meets"] is False  # unchecked is not flagged
    assert gold.judge_scores({}, {})["meets"] is None


# Static guards: the hold-out, no model call, nothing private committed


def tracked_text_files():
    skip = {".git", "node_modules", ".venv", "dist", "build", "local", "out", "__pycache__", "dev docs", ".worktrees",
            ".scratch"}
    for path in ROOT.rglob("*"):
        if any(part in skip for part in path.relative_to(ROOT).parts):
            continue
        if path.is_file() and path.suffix in {".py", ".js", ".mjs", ".jsx", ".ts", ".sh"}:
            yield path


def test_no_module_but_the_gold_runner_reads_the_held_out_sets():
    allowed = {ROOT / "evals" / "gold.py", ROOT / "tests" / "test_gold_sets.py", ROOT / "tests" / "test_review_pages.py",
               ROOT / "tests" / "gold_fixtures.py"}
    # ponytail: a text search for the folder's name; a path built another way would pass it.
    pattern = re.compile(r"""evals/gold/|["']gold["']""")
    found = [str(p.relative_to(ROOT)) for p in tracked_text_files() if p not in allowed and pattern.search(p.read_text(errors="ignore"))]
    assert found == []


def test_the_gold_runner_calls_no_model_and_builds_no_outbound_client():
    source = (ROOT / "evals" / "gold.py").read_text()
    assert not re.search(r"openrouter|providers|outbound_gate|urllib\.request|http\.client|socket\.create_connection", source)
    clients = re.findall(r"AsyncClient\((.*)", source)
    assert clients and all("transport=httpx.ASGITransport" in c for c in clients)


def test_nothing_private_or_whole_is_committed_under_evals():
    evals = ROOT / "evals"
    files = [p for p in evals.rglob("*") if p.is_file() and not p.relative_to(evals).parts[0] in ("local", "out")]
    assert not [p for p in files if p.suffix.lower() == ".pdf"]
    for path in files:
        if path.suffix == ".json":
            doc = json.loads(path.read_text())
            assert not (isinstance(doc, dict) and doc.get("field") == 1), path
    ignored = (ROOT / ".gitignore").read_text().splitlines()
    assert "evals/local/" in ignored and "evals/out/" in ignored


def test_the_gold_sets_public_text_states_the_hold_out_and_never_calls_scholia_bilingual():
    text = (ROOT / "evals" / "gold" / "README.md").read_text() + (ROOT / "evals" / "README.md").read_text()
    assert "bilingual" not in text.lower()
    assert "held out" in text and "provisional" in text and "CC BY 4.0" in text and "CC0 1.0" in text
    assert str(gold.COPY_BOUND) in (ROOT / "evals" / "gold" / "README.md").read_text()  # the fixed bound, recorded


def test_the_fixture_quotes_are_shown_whole_near_the_passage_limit():
    assert 1850 < len(fixtures.LONG) <= 2000


def test_files_written_are_owner_only(tmp_path):
    path = tmp_path / "made" / "x.json"
    gold.write_json(tmp_path, path, {"a": 1})
    assert stat.S_IMODE(path.stat().st_mode) == 0o600 and stat.S_IMODE(path.parent.stat().st_mode) == 0o700
    assert os.listdir(path.parent) == ["x.json"]


def test_the_committed_field_two_drafts_pass_their_checks_and_meet_the_settings():
    settings = gold.load_settings()
    papers = json.loads((gold.FIELD2 / "papers.json").read_text())
    assert gold.check_papers(papers, 2, settings) == []
    assert len(papers["papers"]) == settings["field2_papers"]["count"]
    assert len({p["topic"] for p in papers["papers"]}) == 4
    by_id = {p["id"]: p for p in papers["papers"]}
    for kind in gold.SETS:
        doc = json.loads((gold.FIELD2 / f"{kind}.json").read_text())
        assert gold.check_set(doc, kind, 2, by_id) == []
        assert gold.differences(kind, gold.mix(kind, doc["items"]), settings) == []
        sample = json.loads((gold.FIELD2 / f"sample-{kind}.json").read_text())
        assert sample == gold.draw(doc, settings) and len(sample["items"]) == 20
    assert not (gold.FIELD2 / "labels").exists()  # nothing is labeled in M2


def test_sigterm_stops_a_command_as_ctrl_c_does_leaving_its_marker_and_no_data_folder(tmp_path, monkeypatch):
    import signal
    folder, pdfs = pdf_field(tmp_path)
    seen = []

    async def held(state):
        os.kill(os.getpid(), signal.SIGTERM)  # as `kill <pid>` would
        await asyncio.sleep(3600)
    monkeypatch.setattr(tier1, "until_read", held)
    monkeypatch.setattr(gold, "reading_app", opener(seen))
    with pytest.raises(SystemExit, match="stopped"):
        gold.main(["passages", "--field", "2", "--folder", str(folder), "--pdfs", str(pdfs)])
    assert json.loads((gold.outputs(folder) / "worksheet.json").read_text())["status"] == "incomplete"
    assert seen and not seen[0].parent.exists()


def test_a_refusal_before_any_work_still_replaces_an_earlier_finished_report(tmp_path):
    folder, pdfs = pdf_field(tmp_path)
    gold.write_json(folder, gold.outputs(folder) / "check.json", {"status": "finished", "problems": []})
    with pytest.raises(tier1.Refused, match="--qasper"):
        asyncio.run(gold.check(2, folder, pdfs, None, opener()))
    written = json.loads((gold.outputs(folder) / "check.json").read_text())
    assert written["status"] == "incomplete" and "--qasper" in written["reason"]


def test_a_written_file_is_owner_only_from_its_creation_even_over_an_old_part_file(tmp_path, monkeypatch):
    path = tmp_path / "a" / "b" / "x.json"
    path.parent.mkdir(parents=True)
    (path.parent / "x.json.part").write_text("cut off")
    os.chmod(path.parent / "x.json.part", 0o644)
    modes = []
    real = os.replace
    monkeypatch.setattr(gold.os, "replace", lambda a, b, **at: modes.append(stat.S_IMODE(os.stat(a, dir_fd=at["src_dir_fd"]).st_mode)) or real(a, b, **at))
    gold.write_json(tmp_path, path, {"a": 1})
    assert modes == [0o600] and stat.S_IMODE(path.stat().st_mode) == 0o600
    deep = tmp_path / "c" / "d" / "y.json"
    gold.write_json(tmp_path, deep, {})
    assert all(stat.S_IMODE(p.stat().st_mode) == 0o700 for p in (tmp_path / "c", tmp_path / "c" / "d"))


def test_apply_and_agreement_take_only_a_plainly_named_rater(tmp_path):
    folder = field(tmp_path)
    for argv in (["apply", "--rater", "../../x"], ["agreement", "--raters", "harold", "../x"]):
        with pytest.raises(SystemExit, match="rater"):
            gold.main([argv[0], "--field", "2", "--folder", str(folder), "--set", "pairs", *argv[1:]])


@pytest.mark.asyncio
async def test_the_check_resolves_evidence_a_rater_chose_as_it_resolves_drafted_evidence(tmp_path, qasper_pins):
    folder, pdfs, sheet = await drafted(tmp_path)
    qasper = qasper_folder(tmp_path)
    pin_qasper(qasper_pins, qasper)
    doc = json.loads((folder / "questions.json").read_text())
    papers = papers_by_id(folder)
    good = from_sheet(sheet, papers, 2, "2601.00011v1", "Keyword")
    labels = labels_for(doc, "harold", {"q1": edit(doc["items"][0], evidence=[[good]])})
    (folder / "labels").mkdir()
    write(folder / "labels" / "t-questions.harold.json", labels)
    assert (await gold.check(2, folder, pdfs, qasper, opener()))["problems"] == []
    labels["labels"]["q1"]["evidence"] = [[{**good, "quote": "A paragraph this paper never had."}]]
    write(folder / "labels" / "t-questions.harold.json", labels)
    report = await gold.check(2, folder, pdfs, qasper, opener())
    assert report["problems"] == ["t-questions.harold.json: question q1 evidence item 1: the quote is not passage 2 "
                                  "of 2601.00011v1"]


def test_field_one_is_refused_inside_any_git_work_tree_but_its_ignored_evals_local(tmp_path):
    repo, worktree, plain, bare = tmp_path / "repo", tmp_path / "wt", tmp_path / "plain", tmp_path / "bare"
    (repo / ".git").mkdir(parents=True)
    (repo / ".gitignore").write_text("# local\nevals/local/\n")
    worktree.mkdir()
    (worktree / ".git").write_text("gitdir: /somewhere/.git/worktrees/wt\n")  # a worktree's .git is a file
    (worktree / ".gitignore").write_text("evals/local/\n")
    (bare / ".git").mkdir(parents=True)  # a repository whose .gitignore does not ignore evals/local/
    nested = repo / "vendor" / "other"
    (nested / ".git").mkdir(parents=True)  # a nested repository: its own rules decide
    (plain / "x").mkdir(parents=True)
    (plain / "link").symlink_to(repo / "notes")  # a link into a work tree is checked where it leads
    for inside in (repo / "evals" / "field1-private", repo / "notes" / "new", worktree / "evals" / "f1", repo,
                   bare / "evals" / "local" / "x", nested / "evals" / "local" / "x", plain / "link" / "f1"):
        with pytest.raises(tier1.Refused, match="inside the repository|does not ignore"):
            gold.field_folder(1, inside)
    assert gold.field_folder(1, repo / "evals" / "local" / "harold") == (repo / "evals" / "local" / "harold").resolve()
    assert gold.field_folder(1, worktree / "evals" / "local") == (worktree / "evals" / "local").resolve()
    assert gold.field_folder(1, plain / "x") == (plain / "x").resolve()


def test_from_a_worktree_the_main_checkout_is_refused_too():
    git = ROOT / ".git"
    if not git.is_file():
        pytest.skip("this checkout is not a git worktree")
    gitdir = Path(git.read_text().split(":", 1)[1].strip())  # <main>/.git/worktrees/<name>
    main = gitdir.parents[2]
    with pytest.raises(tier1.Refused, match="inside the repository"):
        gold.field_folder(1, main / "evals" / "field1-private")
    if gold.ignores_local(main):  # once the main checkout's .gitignore carries evals/local/
        assert gold.field_folder(1, main / "evals" / "local" / "x") == (main / "evals" / "local" / "x").resolve()
    else:
        with pytest.raises(tier1.Refused, match="does not ignore"):
            gold.field_folder(1, main / "evals" / "local" / "x")
    assert gold.ignores_local(ROOT)  # this branch's .gitignore does


def test_apply_and_agreement_check_the_drafts_before_any_labels(tmp_path):
    folder = field(tmp_path)
    doc = json.loads((folder / "questions.json").read_text())
    doc["items"].append(5)
    write(folder / "questions.json", doc)
    for argv in (["apply", "--rater", "harold"], ["agreement", "--raters", "harold", "lee"]):
        with pytest.raises(SystemExit, match="questions.json does not pass its check"):
            gold.main([argv[0], "--field", "2", "--folder", str(folder), "--set", "questions", *argv[1:]])


def outcome(action):
    try:
        action()
    except (tier1.Refused, SystemExit) as refused:
        return str(refused)
    return "ok"


EVERY = {"check", "apply", "sample", "pairs page", "questions page", "sample page", "passages"}


@pytest.mark.parametrize("entry, refusing", [
    ("papers.json", EVERY),
    ("pairs.json", EVERY),
    ("sample-pairs.json", {"check", "sample", "sample page"}),
    ("labels", {"check", "apply", "pairs page", "questions page", "sample page"}),
    ("out", {"check", "apply", "questions page", "passages"}),
    ("pdfs", {"passages"}),
])
def test_a_link_under_field_ones_folder_into_a_git_work_tree_is_refused_by_every_command(tmp_path, entry, refusing):
    """G10: field 1's folder passes G7's guard, but an entry in it (labels/, out/, a set, the PDFs) is a link
    into a git work tree. Every command checks each file where it really leads before opening it: the ones
    that would read or write through the link refuse, and nothing is written in the repository."""
    repo = fixtures.git("init", tmp_path / "repo")
    folder = field(tmp_path, 1)
    pairs = json.loads((folder / "pairs.json").read_text())
    gold.write_json(folder, folder / "labels" / "field1-pairs.harold.json", labels_for(pairs, "harold", {}))
    gold.write_json(folder, folder / "sample-pairs.json", gold.draw(pairs, gold.load_settings()))
    (folder / "pdfs").mkdir()
    (folder / entry).rename(repo / entry)
    (folder / entry).symlink_to(repo / entry)
    fixtures.git("add", repo)  # what is there already is not the question

    def repo_now():
        return fixtures.git("status", repo), sorted(p.relative_to(repo) for p in repo.rglob("*") if ".git" not in p.parts)
    before = repo_now()
    command = ["--field", "1", "--folder", str(folder)]
    actions = {
        "check": lambda: gold.main(["check", *command]),
        "apply": lambda: gold.main(["apply", *command, "--set", "pairs", "--rater", "harold"]),
        "sample": lambda: gold.main(["sample", *command, "--set", "pairs"]),
        "pairs page": lambda: gold.Review(1, folder, "pairs", "harold"),
        "questions page": lambda: gold.Review(1, folder, "questions", "harold"),
        "sample page": lambda: gold.Review(1, folder, "pairs", "lee", sample=True, colleague=True),
        "passages": lambda: gold.main(["passages", *command]),  # last: it rewrites the worksheet's marker
    }
    found = {name: outcome(action) for name, action in actions.items()}
    assert {name for name, said in found.items() if "is a link" in said} == refusing, found
    assert repo_now() == before


# G12: no link is followed where S1-36 writes, or where it reads its own files


@pytest.mark.parametrize("number", [1, 2])
def test_a_check_report_linked_to_the_drafts_is_refused_and_the_drafts_stay_as_they_were(tmp_path, monkeypatch, number):
    """sol's reproduction: <output>/check.json is a link to the field's questions.json; check wrote its
    marker and then its report through it, over the drafts. Field 2 with its own folders (evals/out/field2/)."""
    folder = field(tmp_path, number)
    if number == 2:
        monkeypatch.setattr(gold, "FIELD2", folder)
        monkeypatch.setattr(gold, "OUT", tmp_path / "evals-out")
        (tmp_path / "evals-out" / "field2").mkdir(parents=True)
    drafts = (folder / "questions.json").read_bytes()
    report = gold.outputs(folder) / "check.json"
    report.symlink_to(folder / "questions.json")
    with pytest.raises(SystemExit, match="is a link"):
        gold.main(["check", "--field", str(number), "--folder", str(folder)])
    assert (folder / "questions.json").read_bytes() == drafts and report.is_symlink()


@pytest.mark.parametrize("name, command", [
    ("out/check.json", ["check"]),
    ("out/worksheet.json", ["passages"]),
    ("out/field1-pairs.corrected.harold.json", ["apply", "--set", "pairs", "--rater", "harold"]),
    ("sample-pairs.json", ["sample", "--set", "pairs"]),
    ("out/check.json.part", ["check"]),
    ("labels", ["check"]),  # a folder level: field 1's labels/ and out/ (and field 2's evals/out/ below)
    ("out", ["check"]),
])
def test_a_link_at_any_written_name_or_folder_is_refused_and_nothing_is_written_through_it(tmp_path, name, command):
    folder = field(tmp_path, 1)
    pairs = json.loads((folder / "pairs.json").read_text())
    gold.write_json(folder, folder / "labels" / "field1-pairs.harold.json", labels_for(pairs, "harold", {}))
    victim = tmp_path / "victim"
    victim.mkdir()
    (victim / "kept.json").write_text("kept")
    target = folder / name
    if target.is_dir():
        for child in target.iterdir():
            child.rename(victim / child.name)
        target.rmdir()
        target.symlink_to(victim, target_is_directory=True)
    else:
        target.unlink(missing_ok=True)
        target.symlink_to(victim / "kept.json")
    before = sorted((p.name, p.read_bytes()) for p in victim.iterdir())
    with pytest.raises(SystemExit, match="is a link"):
        gold.main([command[0], "--field", "1", "--folder", str(folder), *command[1:]])
    assert sorted((p.name, p.read_bytes()) for p in victim.iterdir()) == before and target.is_symlink()


@pytest.mark.parametrize("linked", ["out", "out/field2"])
def test_field_twos_output_folders_are_never_links(tmp_path, monkeypatch, linked):
    folder = field(tmp_path)
    monkeypatch.setattr(gold, "FIELD2", folder)
    monkeypatch.setattr(gold, "OUT", tmp_path / "evals" / "out")
    victim = tmp_path / "victim"
    victim.mkdir()
    (tmp_path / "evals" / linked).parent.mkdir(parents=True, exist_ok=True)
    (tmp_path / "evals" / linked).symlink_to(victim, target_is_directory=True)
    with pytest.raises(tier1.Refused, match="is a link"):
        asyncio.run(gold.check(2, folder, settings=gold.load_settings()))
    assert list(victim.iterdir()) == []


def test_a_pdf_may_be_a_link_outside_any_git_work_tree_but_not_into_one_nor_its_folder(tmp_path):
    folder, pdfs = pdf_field(tmp_path, 1)
    papers = json.loads((folder / "papers.json").read_text())["papers"]
    storage = tmp_path / "zotero-storage"
    repo = fixtures.git("init", tmp_path / "repo")
    storage.mkdir()
    for paper in papers:  # each PDF moved to Harold's storage and linked from the PDFs' folder
        (pdfs / paper["file"]).rename(storage / paper["file"])
        (pdfs / paper["file"]).symlink_to(storage / paper["file"])
    gold.verify_pdfs(papers, pdfs, 1)
    first = papers[0]["file"]
    assert gold.pdf_bytes(1, pdfs, first) == (storage / first).read_bytes()
    (storage / first).rename(repo / first)
    (pdfs / first).unlink()
    (pdfs / first).symlink_to(repo / first)
    with pytest.raises(tier1.Refused, match="inside the repository"):
        gold.verify_pdfs(papers, pdfs, 1)
    linked = tmp_path / "linked-pdfs"
    linked.symlink_to(pdfs, target_is_directory=True)
    with pytest.raises(tier1.Refused, match="is a link"):
        gold.verify_pdfs(papers, linked, 1)


def test_a_hard_link_or_fifo_at_a_part_file_or_a_read_file_is_never_written_through_or_waited_on(tmp_path):
    """Internal review 6: a hard link at check.json.part to the drafts (O_NOFOLLOW does not stop one) was
    truncated and written; a FIFO there or at a file read hung the command."""
    folder = field(tmp_path)
    drafts = (folder / "questions.json").read_bytes()
    os.link(folder / "questions.json", gold.outputs(folder) / "check.json.part")
    assert problems_of(folder)[0] == []
    assert (folder / "questions.json").read_bytes() == drafts
    os.mkfifo(gold.outputs(folder) / "check.json.part")
    assert problems_of(folder)[0] == []  # the stale part name is removed, never opened
    pairs = (folder / "pairs.json").read_bytes()
    (folder / "pairs.json").unlink()
    os.mkfifo(folder / "pairs.json")
    with pytest.raises(tier1.Refused, match="not a plain file"):
        asyncio.run(gold.check(2, folder, settings=gold.load_settings()))
    (folder / "pairs.json").unlink()
    (folder / "pairs.json").write_bytes(pairs)
