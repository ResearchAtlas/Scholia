"""The gold sets' review pages (S1-36; slice-1 spec section 14 and section 1.5's local-server rules): the
server evals/gold.py starts for one set and one rater, on the synthetic sets of tests/gold_fixtures.py.
Each test registers the server's port with the network block, which refuses every other connection.
Lifecycle cases first: a save that fails, a save cut off, a labels file that is not this set's.
"""

import asyncio
import contextlib
import http.client
import json
import re
import threading
from pathlib import Path

import pytest

import gold_fixtures as fixtures
from evals import gold
from network_guard import register_server

PAGE = Path(gold.__file__).with_name("review_page")


class Page:
    """A review server running in a thread, and requests to it as its page makes them."""

    def __init__(self, review):
        self.review = review
        self.server, self.address = gold.serve(review)
        self.host, self.port = register_server(self.server.socket)
        self.origin = f"http://127.0.0.1:{self.port}"
        self.secret = self.server.secret
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def request(self, method, path, body=None, headers=None, raw=None, length=None):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        sent = {"Host": f"127.0.0.1:{self.port}", "Origin": self.origin, "X-Scholia-Session": self.secret,
                "Content-Type": "application/json", **(headers or {})}
        sent = {k: v for k, v in sent.items() if v is not None}
        data = raw if raw is not None else (None if body is None else json.dumps(body).encode())
        conn.putrequest(method, path, skip_host=True, skip_accept_encoding=True)
        for name, value in sent.items():
            conn.putheader(name, value)
        conn.putheader("Content-Length", str(len(data or b"") if length is None else length))
        conn.endheaders(data)
        response = conn.getresponse()
        payload = response.read()
        conn.close()
        return response.status, payload, dict(response.getheaders())

    def get(self, path, **options):
        status, payload, headers = self.request("GET", path, **options)
        return status, json.loads(payload) if headers.get("Content-Type") == "application/json" else payload

    def save(self, item, label, base=None):
        if base is None:
            base = (self.get("/api/data")[1]["labels"].get(item) or {}).get("revision", 0)
        status, payload, _ = self.request("POST", "/api/label", {"item": item, "base": base, "label": label})
        return status, json.loads(payload)

    def question(self, item):
        """The question as the page shows it: its draft, or the whole question its saved edit holds."""
        data = self.get("/api/data")[1]
        shown = next(i for i in data["items"] if i["id"] == item)
        label = data["labels"].get(item)
        if label and label["state"] == "done" and label["decision"] == "edit":
            shown = {**{k: v for k, v in shown.items() if k != "short_answer"},
                     **{k: label[k] for k in ("text", "short_answer", "answerable", "evidence") if k in label}}
        return shown

    def send(self, item, base=None, **changes):
        """Save a question as the page does: whole, every field as shown, with the changes made in the form."""
        shown = {**self.question(item), **changes}
        label = {"decision": "keep", "text": shown["text"], "answerable": shown["answerable"],
                 "evidence": [[{k: loc[k] for k in ("paper", "ordinal", "key")} if "page" in loc else loc  # shown: as chosen
                               for loc in ev] for ev in shown["evidence"]]
                 if shown["answerable"] else []}
        if shown["answerable"]:
            label["short_answer"] = shown.get("short_answer", "")
        return self.save(item, label, base)

    def close(self):
        self.server.shutdown()
        self.server.server_close()


@pytest.fixture
def pages():
    opened = []

    def open_page(folder, kind, rater="harold", number=2, **options):
        page = Page(gold.Review(number, folder, kind, rater, **options))
        opened.append(page)
        return page
    yield open_page
    for page in opened:
        page.close()


def pick(page, paper, ordinal):
    """A passage as the page sends it back when chosen in the picker: with the key the server gave it."""
    found = next(p for p in page.get(f"/api/passages?paper={paper}")[1]["passages"] if p["ordinal"] == ordinal)
    return {"paper": paper, "ordinal": ordinal, "key": found["key"]}


def field(tmp_path, number=2, sample=False):
    return fixtures.write_field(tmp_path / f"field{number}", number, sample).resolve()


def labels_file(folder, kind, rater="harold", number=2):
    return folder / "labels" / f"field{number}-{kind}.{rater}.json"


# Lifecycle


def test_a_failed_write_says_not_saved_leaves_the_file_as_it_was_and_the_retry_saves(tmp_path, pages, monkeypatch):
    folder = field(tmp_path)
    page = pages(folder, "pairs")
    assert page.save("p01", {"category": "supported"})[0] == 200
    before = labels_file(folder, "pairs").read_bytes()
    real = gold.write_json

    def full_disk(root, path, data):
        raise OSError(28, "No space left on device")
    monkeypatch.setattr(gold, "write_json", full_disk)
    status, answer = page.save("p02", {"category": "unsupported"})
    assert status == 500 and answer == {"code": "not_saved"}
    assert labels_file(folder, "pairs").read_bytes() == before
    assert "p02" not in page.get("/api/data")[1]["labels"]  # nothing claims it was saved
    monkeypatch.setattr(gold, "write_json", real)
    status, answer = page.save("p02", {"category": "unsupported"})
    assert status == 200 and answer["label"]["category"] == "unsupported" and answer["done"] == 2
    assert json.loads(labels_file(folder, "pairs").read_text())["labels"]["p02"]["category"] == "unsupported"


def test_a_save_cut_off_before_its_rename_leaves_the_last_file_whole_and_is_reported(tmp_path, pages):
    folder = field(tmp_path)
    page = pages(folder, "pairs")
    page.save("p01", {"category": "partly"})
    path = labels_file(folder, "pairs")
    path.with_name(path.name + ".part").write_text('{"set": "field2-pai')  # killed mid-write
    again = pages(folder, "pairs")
    data = again.get("/api/data")[1]
    assert data["notices"] == ["cut_off"] and data["labels"]["p01"]["category"] == "partly"
    assert again.save("p02", {"category": "supported"})[0] == 200
    assert not path.with_name(path.name + ".part").exists()


@pytest.mark.parametrize("contents, reason", [
    ('{"set": "field2-pa', "cannot be read"),
    (json.dumps({"set": "field2-questions", "kind": "questions", "field": 2, "rater": "harold", "set_sha256": "x",
                 "labels": {}}), "not this set's"),
    (json.dumps({"set": "field2-pairs", "kind": "pairs", "field": 2, "rater": "lee", "set_sha256": "x", "labels": {}}),
     "rater 'lee''s"),
])
def test_a_labels_file_unreadable_or_of_another_set_is_refused_and_left_as_it_is(tmp_path, contents, reason):
    folder = field(tmp_path)
    path = labels_file(folder, "pairs")
    path.parent.mkdir()
    path.write_text(contents)
    with pytest.raises(gold.Refused, match=re.escape(reason)):
        gold.Review(2, folder, "pairs", "harold")
    assert path.read_text() == contents


# The server's bounds


def test_the_page_is_served_and_its_data_needs_this_launchs_secret(tmp_path, pages):
    page = pages(field(tmp_path), "pairs")
    assert page.address == f"{page.origin}/#session={page.secret}&lang=en"
    status, body, headers = page.request("GET", "/", headers={"X-Scholia-Session": None, "Origin": None})
    assert status == 200 and b'<script src="page.js"' in body
    assert headers["Content-Security-Policy"].startswith("default-src 'self'")
    assert headers["X-Content-Type-Options"] == "nosniff" and headers["Cache-Control"] == "no-store"
    for path in ("/page.js", "/page.css", "/catalog.json"):
        assert page.request("GET", path, headers={"X-Scholia-Session": None})[0] == 200
    assert page.get("/api/data", headers={"X-Scholia-Session": None})[0] == 401
    assert page.get("/api/data", headers={"X-Scholia-Session": "x" * 43})[0] == 401
    assert page.get("/api/data")[0] == 200


@pytest.mark.parametrize("headers, path, method, expected", [
    ({"Origin": "http://127.0.0.1:1"}, "/api/data", "GET", 403),
    ({"Origin": "null"}, "/api/label", "POST", 403),
    ({"Host": "evil.example:80"}, "/api/data", "GET", 403),
    ({"Origin": None}, "/api/label", "POST", 403),  # a change with no origin and no client header
    ({"Sec-Fetch-Site": "cross-site", "Origin": None}, "/api/data", "GET", 403),
    ({"Content-Type": "text/plain"}, "/api/label", "POST", 415),
    ({}, "/../gold.py", "GET", 404),
    ({}, "/%2e%2e/gold.py", "GET", 404),
    ({}, "/.hidden", "GET", 404),
    ({}, "/api/elsewhere", "GET", 404),
    ({}, "/", "PUT", 501),
])
def test_requests_from_elsewhere_or_outside_the_page_folder_are_refused(tmp_path, pages, headers, path, method, expected):
    page = pages(field(tmp_path), "pairs")
    body = b'{"item": "p01", "base": 0, "label": {"category": "supported"}}' if method in ("POST", "PUT") else b""
    assert page.request(method, path, headers=headers, raw=body)[0] == expected
    assert not labels_file(page.review.path.parents[1], "pairs").exists()


def test_an_oversized_save_is_refused_before_it_is_read(tmp_path, pages):
    page = pages(field(tmp_path), "pairs")
    # It says it is larger than the bound and sends a little: answered from the length, the body never read.
    status, payload, _ = page.request("POST", "/api/label", raw=b"{}", length=gold.MAX_BODY + 1)
    assert status == 413 and json.loads(payload)["code"] == "request_too_large"


def test_the_server_logs_no_text_path_or_body(tmp_path, pages, capfd, caplog):
    page = pages(field(tmp_path), "pairs")
    assert fixtures.CANARY in json.dumps(page.get("/api/data")[1])
    page.save("p06", {"category": "partly"})
    page.request("GET", "/nothing-here")
    out, err = capfd.readouterr()
    logged = out + err + caplog.text
    assert fixtures.CANARY not in logged and "/api/" not in logged and "nothing-here" not in logged and "p06" not in logged


def test_the_page_inserts_set_and_paper_text_as_text_never_as_markup():
    script = (PAGE / "page.js").read_text()
    assert not re.search(r"innerHTML|outerHTML|insertAdjacentHTML|document\.write|eval\(|new Function", script)
    assert not re.search(r"https?://", (PAGE / "index.html").read_text() + script + (PAGE / "page.css").read_text())


# Pairs


def test_pairs_are_labeled_blind_to_the_drafted_category_in_a_seeded_shuffled_order(tmp_path, pages):
    folder = field(tmp_path)
    page = pages(folder, "pairs")
    status, raw, _ = page.request("GET", "/api/data")
    data = json.loads(raw)
    assert status == 200 and b"drafted_category" not in raw and b"verdict" not in raw and b"judge" not in raw
    for category in gold.CATEGORIES:  # no drafted category anywhere in the data
        assert f'"{category}"'.encode() not in raw
    order = [i["id"] for i in data["items"]]
    assert sorted(order) == [f"p0{n}" for n in range(1, 7)] and order != sorted(order)
    assert [i["id"] for i in pages(folder, "pairs", rater="lee").get("/api/data")[1]["items"]] == order  # seeded
    assert set(data["items"][0]) == {"id", "sentence", "sentence_language", "passage"}
    for category, item in zip(gold.CATEGORIES, ("p01", "p02", "p03")):
        status, answer = page.save(item, {"category": category})
        assert status == 200 and answer["label"]["category"] == category and answer["label"]["state"] == "done"
    saved = json.loads(labels_file(folder, "pairs").read_text())
    assert {i: label["category"] for i, label in saved["labels"].items()} == {"p01": "supported", "p02": "partly",
                                                                            "p03": "unsupported"}
    assert saved["rater"] == "harold" and saved["set"] == "field2-pairs"
    for wrong in ({"category": "maybe"}, {"category": "supported", "note": "x"}, {"decision": "keep"}, "supported"):
        assert page.save("p04", wrong)[0] == 400


# Questions


def test_questions_are_kept_edited_or_dropped_and_their_evidence_confirmed_or_changed(tmp_path, pages):
    folder = field(tmp_path)
    before = (folder / "questions.json").read_bytes()
    page = pages(folder, "questions")
    data = page.get("/api/data")[1]
    assert [i["id"] for i in data["items"]] == ["q01", "q02", "q03", "q04", "q05", "q06"]  # drafted order
    q02 = data["items"][1]
    assert q02["evidence"][1][0]["quote"].startswith("Reranking raised") and q02["evidence"][1][0]["page"] == 1
    assert page.send("q01")[1]["label"]["decision"] == "keep"  # the draft as shown: kept
    assert page.save("q03", {"decision": "drop"})[0] == 200
    status, answer = page.send("q04", text="  长段落主要讨论了什么？ ", short_answer="检索深度")
    assert status == 200 and answer["label"]["text"] == "长段落主要讨论了什么？"  # trimmed, as typed text is
    picked = [[pick(page, "2601.00001v1", 4)], [pick(page, "2601.00002v2", 1), pick(page, "2601.00002v2", 2)]]
    status, answer = page.send("q02", evidence=picked[:1])
    assert status == 200 and answer["done"] == 4
    labels = json.loads(labels_file(folder, "questions").read_text())["labels"]
    assert [labels[i]["decision"] for i in ("q01", "q02", "q03", "q04")] == ["keep", "edit", "drop", "edit"]
    # an edit holds the whole question, as the rater left it
    assert set(labels["q04"]) - {"item_sha256", "revision", "saved_at"} == {"decision", "text", "short_answer",
                                                                            "answerable", "evidence"}
    assert labels["q04"]["evidence"] == json.loads(before)["items"][3]["evidence"]
    loc = labels["q02"]["evidence"][0][0]
    assert loc["quote"].startswith("Table 1.") and loc["license"] == gold.LICENSES[0] and loc["attribution"]
    assert (folder / "questions.json").read_bytes() == before  # the drafts are never rewritten
    corrected = gold.apply(json.loads(before), json.loads(labels_file(folder, "questions").read_text()),
                           page.review.papers)
    assert corrected["counts"] == {"labeled": 3, "dropped": 1, "changed": 0, "provisional": 2, "orphaned": 0}


def test_the_evidence_picker_offers_a_papers_usable_passages_and_refuses_any_other(tmp_path, pages):
    page = pages(field(tmp_path), "questions")
    status, found = page.get("/api/passages?paper=2601.00001v1")
    assert status == 200 and [p["ordinal"] for p in found["passages"]] == [0, 1, 2, 3, 4, 5]  # no reference
    assert page.get("/api/passages?paper=unknown")[0] == 404
    good = pick(page, "2601.00001v1", 1)
    for choice in ({**good, "ordinal": 6}, {**good, "ordinal": 99}, {**good, "paper": "other"}, {**good, "ordinal": True},
                   {"paper": "2601.00001v1", "ordinal": 1}, {**good, "quote": "made up"}, {**good, "key": "0" * 16}):
        assert page.send("q01", evidence=[[choice]]) == (400, {"code": "evidence_invalid"})
    pairs = pages(field(tmp_path / "b"), "pairs")
    assert pairs.get("/api/passages?paper=2601.00001v1")[0] == 404


@pytest.mark.parametrize("change", [{"text": "Dense retrieval found the evidence passage for 72 of 100 questions."},
                                    {"page": 2}, {"kind": "table"}, {"section_path": ["2 Results"]}])
def test_a_repicked_passage_whose_locator_changed_is_saved_as_an_edit_holding_the_new_locator(tmp_path, pages, change):
    """T9: the worksheet written again after the reading changed: passage 2 of 2601.00001v1 has another quote,
    page, kind or section at the same ordinal. Picking it again was stored as keep (paper and ordinal compared
    only), so apply kept the draft's stale locator."""
    folder = field(tmp_path)
    sheet_path = gold.outputs(folder) / "worksheet.json"
    sheet = json.loads(sheet_path.read_text())
    sheet["papers"]["2601.00001v1"]["passages"][2].update(change)
    gold.write_json(folder, sheet_path, sheet)
    page = pages(folder, "questions")
    assert page.send("q02")[1]["label"]["decision"] == "keep"  # other passages as drafted: still kept
    status, saved = page.send("q01", evidence=[[pick(page, "2601.00001v1", 2)]])
    assert status == 200 and saved["label"]["decision"] == "edit"
    loc = saved["label"]["evidence"][0][0]
    assert {k: loc[k] for k in ("page", "kind", "section_path")} == {
        k: sheet["papers"]["2601.00001v1"]["passages"][2][k] for k in ("page", "kind", "section_path")}
    assert loc["quote"] == sheet["papers"]["2601.00001v1"]["passages"][2]["text"]


def test_an_evidence_item_is_one_passage_or_the_consecutive_passages_of_one_paper(tmp_path, pages):
    """T1: one item mixing papers, or one paper's passages with a gap, was saved; tier 2 would count either
    found by any one of its passages."""
    folder = field(tmp_path)
    page = pages(folder, "questions")
    a, b = "2601.00001v1", "2601.00002v2"
    for item in ([pick(page, a, 4), pick(page, b, 1)], [pick(page, a, 2), pick(page, a, 4)],
                 [pick(page, b, 1), pick(page, b, 1)]):
        status, answer = page.send("q02", evidence=[item])
        assert status == 400 and answer["code"] == "evidence_invalid" and "consecutive passages" in answer["problem"]
    assert not labels_file(folder, "questions").exists()  # nothing saved
    status, saved = page.send("q02", evidence=[[pick(page, a, 4)], [pick(page, b, 2), pick(page, b, 1)]])
    assert status == 200 and saved["label"]["decision"] == "edit"  # picked in either order: one item


@pytest.mark.parametrize("changes, code", [
    ({"text": "   "}, "text_blank"),
    ({"text": "\u200b\u200b"}, "text_blank"),
    ({"short_answer": "\ufe0f"}, "text_blank"),
    ({"short_answer": ""}, "text_blank"),
    ({"text": "x" * 1001}, "text_too_long"),
    ({"text": "這是繁體字的問題嗎？"}, "text_language"),
    ({"evidence": []}, "answerable_needs_answer_and_evidence"),
    ({"answerable": "yes"}, "label_invalid"),
])
def test_edits_need_visible_bounded_text_and_keep_the_question_valid(tmp_path, pages, changes, code):
    folder = field(tmp_path)
    page = pages(folder, "questions")
    status, answer = page.send("q04", **changes)
    assert status == 400 and answer["code"] == code
    assert not labels_file(folder, "questions").exists()


@pytest.mark.parametrize("label, code", [
    ({"decision": "keep"}, "label_invalid"),  # a keep holds the whole question
    ({"decision": "edit", "text": "x", "answerable": False, "evidence": []}, "label_invalid"),  # only keep or drop is sent
    ({"decision": "drop", "text": "x"}, "label_invalid"),
    ({"decision": "keep", "text": "Who paid for it?", "answerable": True, "evidence": []},
     "answerable_needs_answer_and_evidence"),
    ({"decision": "keep", "text": "Who paid for it?", "answerable": False, "evidence": [], "short_answer": "Nobody"},
     "unanswerable_has_no_answer"),
])
def test_a_request_that_is_not_a_whole_question_or_a_drop_is_refused(tmp_path, pages, label, code):
    folder = field(tmp_path)
    page = pages(folder, "questions")
    status, answer = page.save("q03", label)
    assert status == 400 and answer["code"] == code and not labels_file(folder, "questions").exists()


def test_an_edit_at_the_length_bound_and_a_question_made_answerable_are_saved(tmp_path, pages):
    page = pages(field(tmp_path), "questions")
    assert page.send("q01", text="W" * 999 + "?")[0] == 200
    status, answer = page.send("q03", answerable=True, short_answer="Not said", evidence=[[pick(page, "2601.00002v2", 0)]])
    assert status == 200 and answer["label"]["answerable"] is True and len(answer["label"]["evidence"]) == 1


# Stop and resume; two windows; changed items


def test_work_resumes_where_it_stopped(tmp_path, pages):
    folder = field(tmp_path)
    page = pages(folder, "pairs")
    order = [i["id"] for i in page.get("/api/data")[1]["items"]]
    page.save(order[0], {"category": "supported"})
    page.save(order[1], {"category": "partly"})
    page.close()
    data = pages(folder, "pairs").get("/api/data")[1]
    assert data["done"] == 2 and data["total"] == 6
    assert {i: label["category"] for i, label in data["labels"].items()} == {order[0]: "supported", order[1]: "partly"}
    new = pages(folder, "pairs", rater="lee").get("/api/data")[1]  # a new rater: nothing done
    assert new["done"] == 0 and new["labels"] == {}


def test_a_save_from_an_older_window_is_refused_and_the_saved_label_returned(tmp_path, pages):
    folder = field(tmp_path)
    one, two = pages(folder, "pairs"), pages(folder, "pairs")
    assert one.save("p01", {"category": "supported"}, base=0)[0] == 200
    status, answer = two.save("p01", {"category": "unsupported"}, base=0)  # it last saw no label
    assert status == 409 and answer["code"] == "changed_elsewhere" and answer["label"]["category"] == "supported"
    assert json.loads(labels_file(folder, "pairs").read_text())["labels"]["p01"]["category"] == "supported"
    assert two.save("p01", {"category": "unsupported"}, base=1)[0] == 200  # after it reloads: a deliberate change
    assert json.loads(labels_file(folder, "pairs").read_text())["labels"]["p01"]["revision"] == 2
    assert two.save("p02", {"category": "partly"}, base=0)[0] == 200  # two pages on one file: neither loses a save
    assert one.save("p03", {"category": "supported"}, base=0)[0] == 200
    saved = json.loads(labels_file(folder, "pairs").read_text())["labels"]
    assert sorted(saved) == ["p01", "p02", "p03"] and one.get("/api/data")[1]["done"] == 3


def test_a_label_for_an_item_changed_since_is_shown_again_and_never_applied(tmp_path, pages):
    folder = field(tmp_path)
    pages(folder, "pairs").save("p01", {"category": "supported"})
    doc = json.loads((folder / "pairs.json").read_text())
    doc["items"][0]["sentence"] = "Dense retrieval located evidence for many synthetic questions."
    doc["items"].pop()  # p06 removed from the set
    (folder / "pairs.json").write_text(json.dumps(doc, ensure_ascii=False))
    data = pages(folder, "pairs").get("/api/data")[1]
    assert data["labels"]["p01"]["state"] == "changed" and data["done"] == 0
    labels = json.loads(labels_file(folder, "pairs").read_text())
    labels["labels"]["p06"] = {**labels["labels"]["p01"]}
    labels_file(folder, "pairs").write_text(json.dumps(labels))
    corrected = gold.apply(doc, labels, {p["id"]: p for p in json.loads((folder / "papers.json").read_text())["papers"]})
    assert corrected["counts"]["changed"] == 1 and corrected["counts"]["orphaned"] == 1
    assert next(i for i in corrected["items"] if i["id"] == "p01")["provisional"] is True


# The second rater


def test_the_colleagues_page_shows_only_the_sample_and_none_of_harolds_labels(tmp_path, pages):
    folder = field(tmp_path, sample=True)
    harold = pages(folder, "questions")
    for item in ("q01", "q02", "q03", "q04", "q05", "q06"):
        assert harold.send(item)[0] == 200
    sample = json.loads((folder / "sample-questions.json").read_text())["items"]
    status, raw, _ = pages(folder, "questions", rater="lee", sample=True).request("GET", "/api/data")
    data = json.loads(raw)
    assert [i["id"] for i in data["items"]] == sample and data["sample"] is True
    assert data["labels"] == {} and data["done"] == 0 and b"harold" not in raw and b'"keep"' not in raw
    colleague = pages(folder, "questions", rater="lee", sample=True)
    outside = next(i for i in ("q01", "q02", "q03", "q04", "q05", "q06") if i not in sample)
    assert colleague.save(outside, {"decision": "keep"}) == (400, {"code": "label_invalid"})
    assert colleague.save(sample[0], {"decision": "drop"})[0] == 200
    assert json.loads(labels_file(folder, "questions", "lee").read_text())["sample"] is True


def test_field_ones_sample_page_is_written_only_for_a_named_colleague(tmp_path, pages):
    folder = field(tmp_path, 1, sample=True)
    with pytest.raises(gold.Refused, match="--colleague"):
        gold.Review(1, folder, "pairs", "lee", sample=True)
    with pytest.raises(SystemExit, match="--colleague"):
        gold.main(["review", "--field", "1", "--folder", str(folder), "--set", "pairs", "--rater", "lee", "--sample"])
    page = pages(folder, "pairs", rater="lee", number=1, sample=True, colleague=True)
    assert len(page.get("/api/data")[1]["items"]) == 1
    assert page.save(page.get("/api/data")[1]["items"][0]["id"], {"category": "partly"})[0] == 200
    assert oct(labels_file(folder, "pairs", "lee", 1).stat().st_mode & 0o777) == "0o600"
    assert oct((folder / "labels").stat().st_mode & 0o777) == "0o700"


def test_a_rater_is_named_plainly(tmp_path):
    with pytest.raises(gold.Refused, match="rater"):
        gold.Review(2, field(tmp_path), "pairs", "../harold")


# The page's two languages


def placeholders(text):
    return sorted(re.findall(r"\{(\w+)\}", text))


def test_the_page_catalog_has_the_same_keys_and_placeholders_in_each_language():
    catalog = json.loads((PAGE / "catalog.json").read_text())
    assert set(catalog) == set(gold.LANGUAGES)
    en = catalog["en"]
    for lang, strings in catalog.items():
        assert set(strings) == set(en), lang
        assert all(placeholders(strings[k]) == placeholders(en[k]) for k in en), lang
        assert all(v.strip() for v in strings.values())


def test_the_page_uses_only_catalog_keys_and_the_template_holds_no_text_of_its_own():
    from html.parser import HTMLParser
    catalog = json.loads((PAGE / "catalog.json").read_text())["en"]
    script = (PAGE / "page.js").read_text()
    used = set(re.findall(r"\bt\('([\w.-]+)'", script))
    dynamic = {"state.", "lang.", "title.", "keys.", "error.", "notice.", "q.label.", "p."}  # t(`state.${...}`)
    assert used <= set(catalog), used - set(catalog)
    assert all(any(k.startswith(p) for p in dynamic) or k in used or k in {"page.title", "nav.label", "nav.previous",
                                                                          "nav.next", "nav.nextOpen"} for k in catalog)

    class Text(HTMLParser):
        found, keys = [], set()

        def handle_starttag(self, tag, attrs):
            for name, value in attrs:
                if name in ("data-i18n", "data-i18n-aria"):
                    self.keys.add(value)

        def handle_data(self, data):
            if data.strip():
                self.found.append(data.strip())
    parser = Text()
    parser.feed((PAGE / "index.html").read_text())
    assert parser.found == [] and parser.keys <= set(catalog)


def test_the_command_prints_the_address_and_serves_until_stopped(tmp_path, monkeypatch, capsys):
    folder = field(tmp_path)
    started = []

    def serve_forever(self):
        started.append(register_server(self.socket))
        raise KeyboardInterrupt
    monkeypatch.setattr(gold.ThreadingHTTPServer, "serve_forever", serve_forever)
    gold.main(["review", "--field", "2", "--folder", str(folder), "--set", "pairs", "--rater", "harold", "--lang", "zh-CN"])
    out = capsys.readouterr().out
    assert started and re.search(r"open: http://127\.0\.0\.1:\d+/#session=[\w-]{43}&lang=zh-CN", out)
    assert f"labels: {labels_file(folder, 'pairs')}" in out


def test_field_ones_colleague_sees_only_the_sampled_items_papers(tmp_path, pages):
    folder = field(tmp_path, 1, sample=True)
    page = pages(folder, "questions", rater="lee", number=1, sample=True, colleague=True)
    data = page.get("/api/data")[1]
    cited = {loc["paper"] for item in data["items"] for ev in item["evidence"] for loc in ev}
    assert set(data["papers"]) == cited
    for paper in ("p-wages", "p-zh"):
        assert page.get(f"/api/passages?paper={paper}")[0] == (200 if paper in cited else 404)
    harold = pages(folder, "questions", number=1)
    assert set(harold.get("/api/data")[1]["papers"]) == {"p-wages", "p-zh"}


def test_a_lock_that_cannot_be_taken_says_not_saved(tmp_path, pages, monkeypatch):
    folder = field(tmp_path)
    page = pages(folder, "pairs")

    @contextlib.contextmanager
    def refused():
        raise PermissionError(13, "Permission denied")
        yield
    monkeypatch.setattr(page.review, "file_lock", refused)
    status, payload, _ = page.request("POST", "/api/label", {"item": "p01", "base": 0, "label": {"category": "partly"}})
    assert status == 500 and json.loads(payload) == {"code": "not_saved"}
    assert page.get("/api/data")[0] == 500


# Sol reviews 1 and 2's findings (G1 to G5), rebuilt as tests; G6 is the page's, in walkthrough step 80b


def test_a_passage_picked_under_one_paper_is_never_saved_as_another_papers(tmp_path, pages):
    folder = field(tmp_path)
    page = pages(folder, "questions")
    a = pick(page, "2601.00001v1", 1)  # shown while paper A's passages were loaded
    stale = {**a, "paper": "2601.00002v2"}  # the paper selector moved to B before the click
    assert page.send("q01", evidence=[[stale]]) == (400, {"code": "evidence_invalid"})
    assert not labels_file(folder, "questions").exists()
    status, answer = page.send("q01", evidence=[[a]])
    assert status == 200 and answer["label"]["evidence"][0][0]["paper"] == "2601.00001v1"
    assert answer["label"]["evidence"][0][0]["quote"].startswith("We compare dense and keyword retrieval")


def test_keep_after_an_edit_keeps_the_edit_and_revert_sends_the_draft(tmp_path, pages):
    folder = field(tmp_path)
    page = pages(folder, "questions")
    drafted = json.loads((folder / "questions.json").read_text())
    assert page.send("q01", text="How many synthetic questions did dense retrieval find evidence for, of 100?")[0] == 200
    status, answer = page.send("q01")  # Keep as edited: the question as shown, the edit
    assert status == 200 and answer["label"]["decision"] == "edit" and answer["label"]["text"].endswith("of 100?")
    q01 = drafted["items"][0]  # Revert: the draft as drafted, sent whole
    status, answer = page.send("q01", text=q01["text"], short_answer=q01["short_answer"],
                               evidence=[[{**loc, "key": gold.passage_key(loc["paper"], loc["ordinal"], loc["quote"])}
                                          for loc in ev] for ev in q01["evidence"]])
    assert status == 200 and answer["label"]["decision"] == "keep"
    corrected = gold.apply(drafted, json.loads(labels_file(folder, "questions").read_text()), page.review.papers)
    assert next(i for i in corrected["items"] if i["id"] == "q01")["text"] == q01["text"]


def test_making_a_question_answerable_again_restores_nothing_from_the_draft(tmp_path, pages):
    folder = field(tmp_path)
    page = pages(folder, "questions")
    assert page.send("q01", text="Who funded the synthetic study, if anyone?", answerable=False)[0] == 200
    # reopened: the form shows no answer and no evidence, and checking Answerable alone sends just that
    status, answer = page.send("q01", answerable=True, short_answer="", evidence=[])
    assert status == 400 and answer["code"] == "text_blank"
    status, answer = page.send("q01", answerable=True, short_answer="Nobody said", evidence=[])
    assert status == 400 and answer["code"] == "answerable_needs_answer_and_evidence"
    saved = json.loads(labels_file(folder, "questions").read_text())["labels"]["q01"]
    assert saved["answerable"] is False and saved["evidence"] == [] and "short_answer" not in saved


def test_an_edit_repeating_another_questions_text_is_refused_naming_the_rule(tmp_path, pages):
    folder = field(tmp_path)
    page = pages(folder, "questions")
    drafted = json.loads((folder / "questions.json").read_text())
    status, answer = page.send("q02", text=drafted["items"][0]["text"].upper())
    assert status == 400 and answer["code"] == "duplicate_text" and "q01, q02 have the same text" in answer["problem"]
    assert not labels_file(folder, "questions").exists()
    # a hand-made labels file with the same repetition: no page, no check, no gold
    path = labels_file(folder, "questions")
    path.parent.mkdir(exist_ok=True)
    q02 = drafted["items"][1]
    labels = {"set": drafted["id"], "kind": "questions", "field": 2, "rater": "harold", "set_sha256": "x", "labels": {
        "q02": {"item_sha256": gold.item_sha256(q02), "revision": 1, "saved_at": "t", "decision": "edit",
                **{k: q02[k] for k in ("short_answer", "answerable", "evidence")}, "text": drafted["items"][0]["text"]}}}
    path.write_text(json.dumps(labels))
    with pytest.raises(gold.Refused, match="have the same text"):
        gold.Review(2, folder, "questions", "harold")
    with pytest.raises(gold.Refused, match="have the same text"):
        gold.apply(drafted, labels, page.review.papers)
    with pytest.raises(SystemExit, match="have the same text"):
        gold.main(["agreement", "--field", "2", "--folder", str(folder), "--set", "questions", "--raters", "harold", "lee"])
    report = asyncio.run(gold.check(2, folder, settings=gold.load_settings()))
    assert any("as labeled, questions: q01, q02 have the same text" in p for p in report["problems"])


def test_a_label_file_whose_edits_break_the_rules_opens_no_page_and_never_becomes_gold(tmp_path):
    folder = field(tmp_path)
    doc = json.loads((folder / "questions.json").read_text())
    q01 = doc["items"][0]
    path = labels_file(folder, "questions")
    path.parent.mkdir()
    whole = {"decision": "edit", "text": q01["text"], "answerable": True, "evidence": []}
    bad = {"set": doc["id"], "kind": "questions", "field": 2, "rater": "harold", "set_sha256": "x", "labels": {
        "q01": {"item_sha256": gold.item_sha256(q01), "revision": 1, "saved_at": "t", **whole}}}
    path.write_text(json.dumps(bad))
    with pytest.raises(gold.Refused, match="an answerable question has"):
        gold.Review(2, folder, "questions", "harold")
    papers = {p["id"]: p for p in json.loads((folder / "papers.json").read_text())["papers"]}
    with pytest.raises(gold.Refused, match="do not pass their check"):
        gold.apply(doc, bad, papers)
    with pytest.raises(SystemExit, match="an answerable question has"):
        gold.main(["apply", "--field", "2", "--folder", str(folder), "--set", "questions", "--rater", "harold"])
    report = asyncio.run(gold.check(2, folder, settings=gold.load_settings()))
    assert any("as labeled, question q01: an answerable question has" in p for p in report["problems"])
    for label, expected in (({"decision": "keep", "text": "x"}, "keep and drop hold no content"),
                            ({"decision": "edit", "text": "Partly?"}, "an edit holds the whole question"),
                            ({**whole, "text": "繁體字問題", "short_answer": "x", "evidence": q01["evidence"]},
                             "marked en but holds Chinese characters"),
                            ({**whole, "short_answer": "x", "evidence": [[{**q01["evidence"][0][0], "license": "x"}]]},
                             "license is not its paper version's")):
        bad["labels"]["q01"] = {"item_sha256": gold.item_sha256(q01), "revision": 1, "saved_at": "t", **label}
        assert any(expected in p for p in gold.check_labels(bad, doc, papers)), label
    # A label for an item changed since is never applied, so the set as labeled is the draft there
    bad["labels"]["q01"] = {"item_sha256": "older", "revision": 1, "saved_at": "t", **whole}
    assert gold.check_labels(bad, doc, papers) == []


def test_a_save_from_a_page_that_loaded_an_older_revision_is_refused_then_saved_whole_as_sent(tmp_path, pages):
    folder = field(tmp_path)
    one, two = pages(folder, "questions"), pages(folder, "questions")
    assert one.send("q01", base=0, text="How many of 100 synthetic questions did dense retrieval answer?")[0] == 200
    status, answer = two.send("q01", base=0, short_answer="71")  # two loaded revision 0
    assert status == 409 and answer["code"] == "changed_elsewhere" and answer["label"]["text"].startswith("How many of 100")
    # saving again, deliberately, replaces the saved question with the one two shows, whole and as sent
    drafted_text = json.loads((folder / "questions.json").read_text())["items"][0]["text"]
    status, answer = two.send("q01", base=1, text=drafted_text, short_answer="71")
    assert status == 200 and answer["label"]["text"] == drafted_text and answer["label"]["short_answer"] == "71"
    assert one.get("/api/data")[1]["labels"]["q01"]["short_answer"] == "71"  # and every page then shows it


def test_an_edit_making_a_question_unanswerable_holds_no_answer_or_evidence(tmp_path, pages):
    folder = field(tmp_path)
    page = pages(folder, "questions")
    status, answer = page.send("q01", answerable=False)
    assert status == 200 and answer["label"]["answerable"] is False and answer["label"]["evidence"] == []
    assert "short_answer" not in answer["label"]


def test_a_passage_key_names_its_paper_and_ordinal_as_well_as_its_text():
    keys = {gold.passage_key(paper, ordinal, "The same text.") for paper in ("a", "b") for ordinal in (1, 2)}
    assert len(keys) == 4 and gold.passage_key("a", 1, "The  same text") == gold.passage_key("a", 1, "the same TEXT.")


@pytest.mark.parametrize("label, state", [
    ({"decision": "edit", "text": 5, "answerable": True, "evidence": []}, "done"),
    ({"decision": "edit", "text": None, "answerable": True, "evidence": []}, "done"),
    ({"decision": "edit", "text": "x", "answerable": True, "evidence": [[{
        "paper": ["x"], "ordinal": 1, "page": 1, "kind": "paragraph", "section_path": [], "quote": "q", "attribution": "a",
        "license": "l"}]]}, "done"),
    ({"decision": "edit", "text": "x", "answerable": True, "evidence": "abc"}, "changed"),
    ({"decision": "edit", "text": "x", "answerable": True, "evidence": [5]}, "changed"),
    ({"decision": "edit", "text": "x", "answerable": True, "evidence": [[{"paper": "2601.00001v1", "ordinal": 1}]]},
     "changed"),
])
def test_a_malformed_label_that_applies_is_refused_naming_it_and_history_is_left_alone(tmp_path, label, state):
    folder = field(tmp_path)
    doc = json.loads((folder / "questions.json").read_text())
    path = labels_file(folder, "questions")
    path.parent.mkdir()
    sha = gold.item_sha256(doc["items"][0]) if state == "done" else "an older version"
    path.write_text(json.dumps({"set": doc["id"], "kind": "questions", "field": 2, "rater": "harold", "set_sha256": "x",
                                "labels": {"q01": {"item_sha256": sha, "revision": 1, "saved_at": "t", **label}}}))
    report = asyncio.run(gold.check(2, folder, settings=gold.load_settings()))
    if state == "changed":  # history: never validated, so it opens no matter its shape (L3)
        assert gold.Review(2, folder, "questions", "harold").data()["labels"]["q01"] == {"state": "changed", "revision": 1}
        assert report["problems"] == []
        return
    with pytest.raises(gold.Refused, match="label q01"):
        gold.Review(2, folder, "questions", "harold")
    assert any(p.startswith("field2-questions.harold.json: label q01") for p in report["problems"])


def test_a_conflict_answers_with_the_saved_label_and_the_count_done(tmp_path, pages):
    folder = field(tmp_path)
    one, two = pages(folder, "questions"), pages(folder, "questions")
    assert one.save("q03", {"decision": "drop"}, base=0)[0] == 200
    status, answer = two.send("q03", base=0)
    assert status == 409 and answer["label"]["decision"] == "drop" and answer["done"] == 1


def test_malformed_choices_revisions_and_lengths_are_refused_never_a_dead_request(tmp_path, pages):
    folder = field(tmp_path)
    page = pages(folder, "questions")
    good = pick(page, "2601.00001v1", 1)
    for choice in ({**good, "paper": ["2601.00001v1"]}, {**good, "ordinal": [1]}, {**good, "key": 5}):
        assert page.send("q01", evidence=[[choice]]) == (400, {"code": "evidence_invalid"})
    status, payload, _ = page.request("POST", "/api/label", raw=b"{}", length="²")  # a digit int() refuses
    assert status == 411 and json.loads(payload)["code"] == "length_required"
    doc = json.loads((folder / "pairs.json").read_text())
    labels = {"set": doc["id"], "kind": "pairs", "field": 2, "rater": "harold", "set_sha256": "x", "labels": {
        "p01": {"item_sha256": gold.item_sha256(doc["items"][0]), "revision": True, "saved_at": "t", "category": "partly"}}}
    assert gold.check_labels(labels, doc, page.review.papers) == ["label p01: revision is not a count"]


def test_every_id_keyed_lookup_in_the_page_has_no_inherited_keys():
    # An id such as 'constructor' passes the id rules, so no lookup keyed by one may be a plain object.
    script = (PAGE / "page.js").read_text()
    assert not re.search(r"=\s*\{\s*\}\s*;", script)  # no plain-object cache or map
    for name in ("passageCache = new Map()", "edits = Object.create(null)", "strings = Object.create(null)",
                 "data.papers = Object.assign(Object.create(null)", "data.labels = Object.assign(Object.create(null)"):
        assert name in script, name


def test_a_paper_named_constructor_is_served_like_any_other(tmp_path, pages):
    folder = field(tmp_path, 1)
    papers = json.loads((folder / "papers.json").read_text())
    papers["papers"][0]["id"] = "constructor"
    for name in ("questions", "pairs"):
        doc = json.loads((folder / f"{name}.json").read_text())
        text = json.dumps(doc).replace('"p-wages"', '"constructor"')
        (folder / f"{name}.json").write_text(text)
    (folder / "papers.json").write_text(json.dumps(papers))
    sheet = json.loads((gold.outputs(folder) / "worksheet.json").read_text())
    sheet["papers"] = {("constructor" if k == "p-wages" else k): v for k, v in sheet["papers"].items()}
    sheet["papers_sha256"] = gold.sha256_of(folder, folder / "papers.json")
    gold.write_json(folder, gold.outputs(folder) / "worksheet.json", sheet)
    page = pages(folder, "questions", number=1)
    assert "constructor" in page.get("/api/data")[1]["papers"]
    assert page.get("/api/passages?paper=constructor")[0] == 200
    assert page.send("q01", evidence=[[pick(page, "constructor", 1)]])[0] == 200


# G8: a label applies to its item as it now is, or it is history (L3)


def test_a_label_whose_paper_was_removed_after_its_item_changed_is_history_and_the_item_reopens(tmp_path, pages):
    folder = field(tmp_path)
    page = pages(folder, "questions")
    assert page.send("q02", short_answer="Dense; reranking added four points")[0] == 200  # cites paper b
    page.close()
    # Paper b is then removed: the drafts stop citing it (q02 changes, the pairs on b go), and the worksheet follows
    b = "2601.00002v2"
    questions = json.loads((folder / "questions.json").read_text())
    questions["items"][1]["evidence"] = questions["items"][1]["evidence"][:1]
    (folder / "questions.json").write_text(json.dumps(questions, ensure_ascii=False))
    pairs = json.loads((folder / "pairs.json").read_text())
    pairs["items"] = [i for i in pairs["items"] if i["passage"]["paper"] != b]
    (folder / "pairs.json").write_text(json.dumps(pairs, ensure_ascii=False))
    papers = json.loads((folder / "papers.json").read_text())
    papers["papers"] = [p for p in papers["papers"] if p["id"] != b]
    (folder / "papers.json").write_text(json.dumps(papers))
    sheet = json.loads((gold.outputs(folder) / "worksheet.json").read_text())
    del sheet["papers"][b]
    sheet["papers_sha256"] = gold.sha256_of(folder, folder / "papers.json")
    gold.write_json(folder, gold.outputs(folder) / "worksheet.json", sheet)
    labels = json.loads(labels_file(folder, "questions").read_text())
    labels["labels"]["gone"] = {"item_sha256": "x", "revision": 1, "decision": "edit", "evidence": [[{"paper": b}]]}
    labels["labels"]["not-a-label"] = "anything"
    labels_file(folder, "questions").write_text(json.dumps(labels))
    before = labels_file(folder, "questions").read_bytes()
    page = pages(folder, "questions")  # opens: the old label is history
    assert page.get("/api/data")[1]["labels"]["q02"] == {"state": "changed", "revision": 1}
    report = asyncio.run(gold.check(2, folder, settings=gold.load_settings()))
    assert report["problems"] == [] and report["labels"]["field2-questions.harold.json"]["changed"] == 1
    corrected = gold.apply(questions, labels, page.review.papers)
    assert corrected["counts"]["changed"] == 1 and corrected["counts"]["orphaned"] == 2
    assert next(i for i in corrected["items"] if i["id"] == "q02")["provisional"] is True  # never applied
    assert labels_file(folder, "questions").read_bytes() == before  # kept untouched until q02 is labeled again
    status, answer = page.send("q02", base=1)
    assert status == 200 and answer["label"]["state"] == "done"
    saved = json.loads(labels_file(folder, "questions").read_text())["labels"]
    assert saved["gone"] == labels["labels"]["gone"] and saved["not-a-label"] == "anything"  # history stays as it was


def test_saving_over_a_history_label_needs_its_revision_and_readers_leave_history_alone(tmp_path, pages):
    folder = field(tmp_path)
    doc = json.loads((folder / "questions.json").read_text())
    path = labels_file(folder, "questions")
    path.parent.mkdir()
    labels = {"set": doc["id"], "kind": "questions", "field": 2, "rater": "harold", "set_sha256": "x", "labels": {
        "q01": {"item_sha256": "older", "revision": 3, "saved_at": "t", "decision": "drop"},
        "q02": {"item_sha256": "older", "revision": "two", "decision": "keep"},  # history without a count revision
        "q03": {"item_sha256": gold.item_sha256(doc["items"][2]), "revision": 1, "saved_at": "t", "decision": "drop"}}}
    path.write_text(json.dumps(labels))
    page = pages(folder, "questions")
    data = page.get("/api/data")[1]["labels"]
    assert data["q01"] == {"state": "changed", "revision": 3} and data["q02"] == {"state": "changed", "revision": 0}
    assert page.send("q01", base=0)[0] == 409 and page.send("q01", base=3)[1]["label"]["revision"] == 4
    assert page.send("q02", base=0)[1]["label"]["revision"] == 1
    lee = {**labels, "rater": "lee"}
    (folder / "labels" / "field2-questions.lee.json").write_text(json.dumps(lee))
    out = json.loads(subprocess_free_agreement(folder))
    assert out["kept"]["both_labeled"] == 1  # q03 only: the history labels are never compared


def subprocess_free_agreement(folder):
    import io
    from contextlib import redirect_stdout
    buffer = io.StringIO()
    with redirect_stdout(buffer):
        gold.main(["agreement", "--field", "2", "--folder", str(folder), "--set", "questions", "--raters", "harold", "lee"])
    return buffer.getvalue()


def test_a_labels_folder_linked_into_a_git_work_tree_after_the_page_opened_gets_nothing(tmp_path, pages):
    """G10, sol's reproduction: field 1's labels/ is a link into a git work tree. Present when the page opens,
    the page refuses to open; made after it opened, the save says not saved, and nothing (the label, its part
    file, the lock file) is written there."""
    repo = fixtures.git("init", tmp_path / "repo")
    (repo / "leak").mkdir()
    folder = field(tmp_path, 1)
    (folder / "labels").symlink_to(repo / "leak")
    with pytest.raises(gold.Refused, match="is a link"):
        gold.Review(1, folder, "pairs", "harold")
    (folder / "labels").unlink()
    page = pages(folder, "pairs", number=1)
    (folder / "labels").symlink_to(repo / "leak")
    assert page.save("p01", {"category": "supported"}, base=0) == (500, {"code": "not_saved"})
    assert page.get("/api/data")[0] == 500
    assert list((repo / "leak").iterdir()) == []
    assert fixtures.git("status", repo) == ""


# G11: ids are text, kept as validated


def test_an_item_id_that_is_not_text_is_refused_before_any_page_sample_or_label(tmp_path):
    """sol's reproduction: a pair with id 123 passed (str(id) was checked, the number kept), its label was
    stored under "123", a reload showed none done, apply called it orphaned and a second save crashed."""
    folder = field(tmp_path)
    pairs = json.loads((folder / "pairs.json").read_text())
    pairs["items"][0]["id"] = 123
    (folder / "pairs.json").write_text(json.dumps(pairs, ensure_ascii=False))
    with pytest.raises(gold.Refused, match="id is not a plain name"):
        gold.Review(2, folder, "pairs", "harold")
    (folder / "sample-pairs.json").unlink(missing_ok=True)
    for argv in (["apply", "--rater", "harold"], ["agreement", "--raters", "harold", "lee"], ["sample"]):
        with pytest.raises(SystemExit, match="pairs.json does not pass its check"):
            gold.main([argv[0], "--field", "2", "--folder", str(folder), "--set", "pairs", *argv[1:]])
    assert not (folder / "sample-pairs.json").exists()
    papers = json.loads((folder / "papers.json").read_text())
    papers["papers"][0]["id"] = [papers["papers"][0]["id"]]  # a paper id that is not text: refused, never a crash
    (folder / "papers.json").write_text(json.dumps(papers))
    with pytest.raises(gold.Refused, match="not an arXiv id"):
        gold.Review(2, folder, "questions", "harold")
    with pytest.raises(SystemExit, match="not an arXiv id"):
        gold.main(["apply", "--field", "2", "--folder", str(folder), "--set", "questions", "--rater", "harold"])


def test_a_labels_header_or_sample_of_another_type_is_refused(tmp_path):
    folder = field(tmp_path, sample=True)
    doc = json.loads((folder / "pairs.json").read_text())
    papers = {p["id"]: p for p in json.loads((folder / "papers.json").read_text())["papers"]}
    good = {"set": doc["id"], "kind": "pairs", "field": 2, "rater": "harold", "set_sha256": "x", "labels": {}}
    assert gold.check_labels(good, doc, papers) == []
    for header in ({"field": 2.0}, {"rater": 7}, {"rater": "Harold Z"}):
        assert gold.check_labels({**good, **header}, doc, papers) != [], header
    for items in ([1], [["p01"]], "p01"):
        (folder / "sample-pairs.json").write_text(json.dumps({"set": doc["id"], "items": items}))
        assert "sample-pairs.json: not a sample of this set" in asyncio.run(
            gold.check(2, folder, settings=gold.load_settings()))["problems"], items
        with pytest.raises(gold.Refused, match="not a sample of this set"):
            gold.Review(2, folder, "pairs", "lee", sample=True)


@pytest.mark.parametrize("name", ["field1-pairs.harold.json", "field1-pairs.harold.json.part",
                                  ".field1-pairs.harold.json.lock"])
def test_a_labels_file_part_or_lock_linked_into_a_git_work_tree_gets_nothing(tmp_path, pages, name):
    """G10: the labels file itself, its part file or its lock file, each a link into a git work tree made
    after the page opened: the save says not saved and nothing is written in the repository."""
    repo = fixtures.git("init", tmp_path / "repo")
    folder = field(tmp_path, 1)
    page = pages(folder, "pairs", number=1)
    (folder / "labels").mkdir()
    (folder / "labels" / name).symlink_to(repo / "leak.json")
    assert page.save("p01", {"category": "supported"}, base=0) == (500, {"code": "not_saved"})
    assert not (repo / "leak.json").exists() and fixtures.git("status", repo) == ""


@pytest.mark.parametrize("sheet", ["[]", '{"status": "finished", "field": 2, "papers_sha256": "SHA", "papers": [1]}',
                                   '{"status": "finished"}'])
def test_a_worksheet_of_another_shape_is_refused_not_a_crash(tmp_path, sheet):
    folder = field(tmp_path)
    sheet = sheet.replace("SHA", gold.sha256_of(folder, folder / "papers.json"))
    (gold.outputs(folder) / "worksheet.json").write_text(sheet)
    with pytest.raises(gold.Refused, match="not a finished worksheet"):
        gold.Review(2, folder, "questions", "harold")
