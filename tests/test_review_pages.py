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

    def full_disk(path, data):
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
    assert page.save("q01", {"decision": "keep"})[0] == 200
    assert page.save("q03", {"decision": "drop"})[0] == 200
    status, answer = page.save("q04", {"decision": "edit", "text": "  长段落主要讨论了什么？ ", "short_answer": "检索深度"})
    assert status == 200 and answer["label"]["text"] == "长段落主要讨论了什么？"  # trimmed, as typed text is
    picked = [[pick(page, "2601.00001v1", 4)], [pick(page, "2601.00002v2", 1), pick(page, "2601.00002v2", 2)]]
    status, answer = page.save("q02", {"decision": "edit", "evidence": picked[:1]})
    assert status == 200 and answer["done"] == 4
    labels = json.loads(labels_file(folder, "questions").read_text())["labels"]
    assert [labels[i]["decision"] for i in ("q01", "q02", "q03", "q04")] == ["keep", "edit", "drop", "edit"]
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
    for choice in ({"paper": "2601.00001v1", "ordinal": 6}, {"paper": "2601.00001v1", "ordinal": 99},
                   {"paper": "other", "ordinal": 1}, {"paper": "2601.00001v1", "ordinal": True},
                   {"paper": "2601.00001v1", "ordinal": 1, "quote": "made up"}):
        assert page.save("q01", {"decision": "edit", "evidence": [[choice]]}) == (400, {"code": "evidence_invalid"})
    pairs = pages(field(tmp_path / "b"), "pairs")
    assert pairs.get("/api/passages?paper=2601.00001v1")[0] == 404


@pytest.mark.parametrize("label, code", [
    ({"decision": "edit"}, "edit_needs_a_change"),
    ({"decision": "keep", "text": "x"}, "label_invalid"),
    ({"decision": "edit", "text": "   "}, "text_blank"),
    ({"decision": "edit", "text": "​​"}, "text_blank"),
    ({"decision": "edit", "short_answer": "️"}, "text_blank"),
    ({"decision": "edit", "text": "x" * 1001}, "text_too_long"),
    ({"decision": "edit", "text": "這是繁體字的問題嗎？"}, "text_language"),
    ({"decision": "edit", "evidence": []}, "answerable_needs_answer_and_evidence"),
    ({"decision": "edit", "answerable": "yes"}, "label_invalid"),
    ({"decision": "maybe"}, "label_invalid"),
])
def test_edits_need_visible_bounded_text_and_keep_the_question_valid(tmp_path, pages, label, code):
    folder = field(tmp_path)
    page = pages(folder, "questions")
    assert page.save("q04", label) == (400, {"code": code})
    assert not labels_file(folder, "questions").exists()


def test_an_edit_at_the_length_bound_and_a_question_made_answerable_are_saved(tmp_path, pages):
    page = pages(field(tmp_path), "questions")
    assert page.save("q01", {"decision": "edit", "text": "W" * 999 + "?"})[0] == 200
    status, answer = page.save("q03", {"decision": "edit", "answerable": True, "short_answer": "Not said",
                                       "evidence": [[pick(page, "2601.00002v2", 0)]]})
    assert status == 200 and answer["label"]["answerable"] is True
    # An edit builds on the saved one: what it does not name stays as saved
    status, answer = page.save("q03", {"decision": "edit", "text": "What hardware ran the synthetic experiments?"})
    assert status == 200 and answer["label"]["short_answer"] == "Not said" and len(answer["label"]["evidence"]) == 1
    assert page.save("q05", {"decision": "edit", "answerable": True}) == (400, {"code": "answerable_needs_answer_and_evidence"})


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
        assert harold.save(item, {"decision": "keep"})[0] == 200
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


# Sol review 1's findings, rebuilt as tests


def test_a_passage_picked_under_one_paper_is_never_saved_as_another_papers(tmp_path, pages):
    folder = field(tmp_path)
    page = pages(folder, "questions")
    a = pick(page, "2601.00001v1", 1)  # shown while paper A's passages were loaded
    stale = {**a, "paper": "2601.00002v2"}  # the paper selector moved to B before the click
    assert page.save("q01", {"decision": "edit", "evidence": [[stale]]}) == (400, {"code": "evidence_invalid"})
    assert page.save("q01", {"decision": "edit", "evidence": [[{"paper": "2601.00001v1", "ordinal": 1}]]}) == \
        (400, {"code": "evidence_invalid"})  # a choice without the key it was shown with
    assert not labels_file(folder, "questions").exists()
    status, answer = page.save("q01", {"decision": "edit", "evidence": [[a]]})
    assert status == 200 and answer["label"]["evidence"][0][0]["paper"] == "2601.00001v1"
    assert answer["label"]["evidence"][0][0]["quote"].startswith("We compare dense and keyword retrieval")


def test_keep_after_an_edit_keeps_the_edit_and_going_back_to_the_draft_is_explicit(tmp_path, pages):
    folder = field(tmp_path)
    page = pages(folder, "questions")
    assert page.save("q01", {"decision": "edit", "text": "How many synthetic questions did dense retrieval find evidence for, of 100?"})[0] == 200
    assert page.save("q01", {"decision": "keep"}) == (400, {"code": "keep_would_revert"})
    saved = json.loads(labels_file(folder, "questions").read_text())["labels"]["q01"]
    assert saved["decision"] == "edit" and saved["text"].endswith("of 100?")
    # The page's Keep re-sends the edit it shows: still an edit, nothing lost
    shown = page.get("/api/data")[1]["labels"]["q01"]
    status, answer = page.save("q01", {"decision": "edit", "text": shown["text"]})
    assert status == 200 and answer["label"]["decision"] == "edit" and answer["label"]["text"] == shown["text"]
    # Revert is its own action, and apply then gives the draft
    status, payload, _ = page.request("POST", "/api/label", {"item": "q01", "base": answer["label"]["revision"],
                                                             "label": {"decision": "keep"}, "revert": True})
    assert status == 200 and json.loads(payload)["label"]["decision"] == "keep"
    labels = json.loads(labels_file(folder, "questions").read_text())
    drafted = json.loads((folder / "questions.json").read_text())
    corrected = gold.apply(drafted, labels, page.review.papers)
    assert next(i for i in corrected["items"] if i["id"] == "q01")["text"] == drafted["items"][0]["text"]
    assert page.request("POST", "/api/label", {"item": "q02", "base": 0, "label": {"decision": "keep"},
                                               "revert": "yes"})[0] == 400


def test_a_label_file_whose_edits_break_the_rules_opens_no_page_and_never_becomes_gold(tmp_path):
    folder = field(tmp_path)
    doc = json.loads((folder / "questions.json").read_text())
    q01 = doc["items"][0]
    path = labels_file(folder, "questions")
    path.parent.mkdir()
    bad = {"set": doc["id"], "kind": "questions", "field": 2, "rater": "harold", "set_sha256": "x", "labels": {
        "q01": {"item_sha256": gold.item_sha256(q01), "revision": 1, "saved_at": "t", "decision": "edit",
                "answerable": True, "evidence": []}}}
    path.write_text(json.dumps(bad))
    with pytest.raises(gold.Refused, match="an answerable question has evidence"):
        gold.Review(2, folder, "questions", "harold")
    papers = {p["id"]: p for p in json.loads((folder / "papers.json").read_text())["papers"]}
    with pytest.raises(gold.Refused, match="do not pass their check"):
        gold.apply(doc, bad, papers)
    with pytest.raises(SystemExit, match="an answerable question has evidence"):
        gold.main(["apply", "--field", "2", "--folder", str(folder), "--set", "questions", "--rater", "harold"])
    report = asyncio.run(gold.check(2, folder, settings=gold.load_settings()))
    assert any("label q01: question q01: an answerable question has evidence" in p for p in report["problems"])
    for label, expected in (({"decision": "keep", "text": "x"}, "an edit names what it changes"),
                            ({"decision": "edit"}, "an edit names what it changes"),
                            ({"decision": "edit", "text": "繁體字問題"}, "marked en but holds Chinese characters"),
                            ({"decision": "edit", "evidence": [[{**q01["evidence"][0][0], "license": "x"}]]},
                             "license is not its paper version's")):
        bad["labels"]["q01"] = {"item_sha256": gold.item_sha256(q01), "revision": 1, "saved_at": "t", **label}
        assert any(expected in p for p in gold.check_labels(bad, doc, papers)), label
    # A label for an item changed since is not applied, so its content is checked at its next save
    bad["labels"]["q01"] = {"item_sha256": "older", "revision": 1, "saved_at": "t", "decision": "edit", "evidence": []}
    assert gold.check_labels(bad, doc, papers) == []


def test_a_conflicting_window_saving_only_what_it_changed_keeps_the_other_windows_edit(tmp_path, pages):
    folder = field(tmp_path)
    one, two = pages(folder, "questions"), pages(folder, "questions")
    assert one.save("q01", {"decision": "edit", "text": "How many of 100 synthetic questions did dense retrieval answer?"},
                    base=0)[0] == 200
    # Window two had opened its edit before: changing only the short answer, it is first told of the conflict
    assert two.save("q01", {"decision": "edit", "short_answer": "71"}, base=0)[0] == 409
    # and saving again sends only that change, on top of window one's saved edit
    status, answer = two.save("q01", {"decision": "edit", "short_answer": "71"}, base=1)
    assert status == 200 and answer["label"]["text"].startswith("How many of 100") and answer["label"]["short_answer"] == "71"


def test_an_edit_making_a_question_unanswerable_keeps_no_answer_or_evidence(tmp_path, pages):
    folder = field(tmp_path)
    page = pages(folder, "questions")
    assert page.save("q01", {"decision": "edit", "short_answer": "71 of them",
                             "evidence": [[pick(page, "2601.00001v1", 2)]]})[0] == 200
    status, answer = page.save("q01", {"decision": "edit", "answerable": False})  # names only answerable
    assert status == 200 and answer["label"]["answerable"] is False
    assert "short_answer" not in answer["label"] and "evidence" not in answer["label"]


def test_a_passage_key_names_its_paper_and_ordinal_as_well_as_its_text():
    keys = {gold.passage_key(paper, ordinal, "The same text.") for paper in ("a", "b") for ordinal in (1, 2)}
    assert len(keys) == 4 and gold.passage_key("a", 1, "The  same text") == gold.passage_key("a", 1, "the same TEXT.")


@pytest.mark.parametrize("label, state", [
    ({"decision": "edit", "text": 5}, "done"),
    ({"decision": "edit", "text": None}, "done"),
    ({"decision": "edit", "evidence": [[{"paper": ["x"], "ordinal": 1, "page": 1, "kind": "paragraph",
                                         "section_path": [], "quote": "q", "attribution": "a", "license": "l"}]]}, "done"),
    ({"decision": "edit", "evidence": "abc"}, "changed"),
    ({"decision": "edit", "evidence": [5]}, "changed"),
    ({"decision": "edit", "evidence": [[{"paper": "2601.00001v1", "ordinal": 1}]]}, "changed"),
])
def test_a_malformed_labels_file_is_refused_naming_the_label_never_a_crash(tmp_path, label, state):
    folder = field(tmp_path)
    doc = json.loads((folder / "questions.json").read_text())
    path = labels_file(folder, "questions")
    path.parent.mkdir()
    sha = gold.item_sha256(doc["items"][0]) if state == "done" else "an older version"
    path.write_text(json.dumps({"set": doc["id"], "kind": "questions", "field": 2, "rater": "harold", "set_sha256": "x",
                                "labels": {"q01": {"item_sha256": sha, "revision": 1, "saved_at": "t", **label}}}))
    with pytest.raises(gold.Refused, match="label q01"):
        gold.Review(2, folder, "questions", "harold")
    report = asyncio.run(gold.check(2, folder, settings=gold.load_settings()))
    assert any(p.startswith("field2-questions.harold.json: label q01") for p in report["problems"])


# A change the rater names is saved as sent, or not saved at all (charter: statuses say saved only after the record does)


def test_a_retried_answer_on_a_question_made_unanswerable_elsewhere_is_not_saved(tmp_path, pages):
    folder = field(tmp_path)
    one, two = pages(folder, "questions"), pages(folder, "questions")
    assert one.save("q01", {"decision": "edit", "answerable": False}, base=0)[0] == 200
    before = labels_file(folder, "questions").read_bytes()
    for change in ({"short_answer": "71"}, {"evidence": [[pick(two, "2601.00001v1", 2)]]}):
        status, answer = two.save("q01", {"decision": "edit", **change}, base=1)
        assert status == 409 and answer["code"] == "change_not_kept" and answer["label"]["answerable"] is False
        assert labels_file(folder, "questions").read_bytes() == before  # nothing written


def test_an_edit_naming_answerable_false_with_an_answer_is_not_saved(tmp_path, pages):
    folder = field(tmp_path)
    page = pages(folder, "questions")
    status, answer = page.save("q01", {"decision": "edit", "answerable": False, "short_answer": "71"})
    assert status == 409 and answer["code"] == "change_not_kept" and not labels_file(folder, "questions").exists()


def test_a_same_field_change_after_a_conflict_is_saved_as_sent_and_shown_as_saved(tmp_path, pages):
    folder = field(tmp_path)
    one, two = pages(folder, "questions"), pages(folder, "questions")
    assert one.save("q01", {"decision": "edit", "text": "Window one's wording of the question?"}, base=0)[0] == 200
    status, answer = two.save("q01", {"decision": "edit", "text": "  Window two's wording of the question?  "}, base=1)
    assert status == 200 and answer["label"]["text"] == "Window two's wording of the question?"  # as typed, trimmed
    assert one.get("/api/data")[1]["labels"]["q01"]["text"] == answer["label"]["text"]  # what every page then shows
    a, b = pick(two, "2601.00001v1", 2), pick(two, "2601.00002v2", 0)
    assert two.save("q01", {"decision": "edit", "evidence": [[a]]}, base=2)[0] == 200
    status, answer = one.save("q01", {"decision": "edit", "evidence": [[b]]}, base=3)
    assert status == 200 and [[loc["paper"] for loc in ev] for ev in answer["label"]["evidence"]] == [["2601.00002v2"]]


def test_an_answer_named_for_a_question_that_stays_unanswerable_is_not_saved(tmp_path, pages):
    folder = field(tmp_path)
    one, two = pages(folder, "questions"), pages(folder, "questions")
    # Window two had made the drafted-unanswerable q03 answerable; window one then dropped it, which applies no edit
    assert two.save("q03", {"decision": "edit", "answerable": True, "short_answer": "Not said",
                            "evidence": [[pick(two, "2601.00001v1", 2)]]}, base=0)[0] == 200
    assert one.save("q03", {"decision": "drop"}, base=1)[0] == 200
    before = labels_file(folder, "questions").read_bytes()
    # Window two's retry names only the answer it changed: the question would stay unanswerable, so not saved
    status, answer = two.save("q03", {"decision": "edit", "short_answer": "Still not said"}, base=2)
    assert status == 409 and answer["code"] == "change_not_kept" and answer["label"]["decision"] == "drop"
    assert labels_file(folder, "questions").read_bytes() == before
    status, answer = two.save("q03", {"decision": "edit", "evidence": [[pick(two, "2601.00001v1", 2)]]}, base=2)
    assert status == 409 and answer["code"] == "change_not_kept"
