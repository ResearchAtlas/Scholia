"""The gold sets (S1-36; slice-1 spec section 14, tickets 17, 22, 70 and 72): their formats and checks, the
passage worksheet a drafter writes them from, the review pages for correcting the questions and labeling
the citation pairs, the second rater's sample and the agreement, the corrected set, and the shapes tier 2
(retrieval) and tier 3 (the support judge) are scored in.

    uv run --no-sync python -I evals/gold.py check --field 2 [--pdfs DIR --qasper DIR]
    sandbox-exec -f tests/loopback-only.sb uv run --no-sync python -I evals/gold.py passages --field 2 --pdfs DIR
    uv run --no-sync python -I evals/gold.py review --field 2 --set questions|pairs --rater NAME [--lang en|zh-CN]
        [--sample [--colleague]]
    uv run --no-sync python -I evals/gold.py sample --field 2 --set questions|pairs
    uv run --no-sync python -I evals/gold.py agreement --field 2 --set questions|pairs --raters NAME NAME
    uv run --no-sync python -I evals/gold.py apply --field 2 --set questions|pairs --rater NAME

A field's files sit in one folder: field 2's in evals/gold/field2/ (public: the paper list, the sets, the
samples and the labels Harold chooses to commit), field 1's in the folder given with --folder, outside the
repository or under the git-ignored evals/local/, because they come from Harold's library. In it:
papers.json, questions.json, pairs.json, sample-<set>.json and labels/<set id>.<rater>.json. What is
generated from whole papers (the worksheet, the check's report, a corrected set) goes to a git-ignored
output folder: evals/out/field2/ for field 2, <folder>/out/ for field 1. Files the runner writes are
owner-only (0600, in folders it makes 0700), each written whole (a part file, then a rename).

The drafts are provisional, are never rewritten by any command here, and are held out: tier 2 is not
used for tuning (section 14), so no module but this one reads evals/gold/ (a static test). Nothing here
calls a model or opens an outbound connection: drafting is the drafter's own work from the worksheet,
and papers are read by the app as the desktop runs it (a temporary data folder, an in-memory credential
store, a review-locked project, so nothing is looked up). The PDFs are never downloaded here and never
enter git: each is checked against its size and SHA-256 in papers.json before anything is read.

The review page is served from evals/review_page/ by a server on 127.0.0.1 that accepts only its own
origin and host and this launch's secret (backend/local_guard.py's checks), serves only that folder's
files, and logs nothing. Each decision is written to the labels file before the page says it is saved.
"""

import argparse
import asyncio
import contextlib
import hashlib
import hmac
import json
import os
import random
import re
import secrets
import signal
import sys
import tempfile
import threading
import time
from collections import Counter, defaultdict
from difflib import SequenceMatcher
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path[:0] = [str(ROOT), str(ROOT / "tools")]

from backend.i18n import LANGUAGES  # noqa: E402  each supported interface language (English and Chinese today)
from evals import metrics, tier1  # noqa: E402
from evals.tier1 import Refused, _key  # noqa: E402

GOLD = HERE / "gold"
FIELD2 = GOLD / "field2"
LOCAL = HERE / "local"  # field 1's place inside the repository, ignored by git
OUT = HERE / "out"  # generated files, ignored by git
PAGE = HERE / "review_page"
SETTINGS = GOLD / "settings.json"

SETS = ("questions", "pairs")
# The two licenses a field 2 paper version may carry (spec section 14, ticket 70), as arXiv names them.
LICENSES = ("http://creativecommons.org/licenses/by/4.0/", "http://creativecommons.org/publicdomain/zero/1.0/")
KINDS = ("paragraph", "table", "caption", "abstract", "title")  # every passage kind but reference (section 7.1)
CATEGORIES = ("supported", "partly", "unsupported")
DECISIONS = ("keep", "edit", "drop")
VERDICTS = ("supported", "partly", "unsupported", "unchecked")  # the judge's (section 7.5)
COPY_BOUND = 40  # letters and digits in a row a pair sentence may share with its passage (S5)
TEXT_BOUND = {"text": 1000, "short_answer": 500}  # characters of a question or short answer, drafted or edited
MAX_BODY = 256 * 1024  # bytes of one save's request
ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}")
RATER = re.compile(r"[a-z][a-z0-9-]{0,39}")
ARXIV = re.compile(r"\d{4}\.\d{4,5}v[1-9]\d*")
DATE = re.compile(r"\d{4}-\d{2}-\d{2}")
RECALL = [{"name": "recall@8", "kind": "recall", "k": 8}]  # tier 2's metric (section 14), tier 1's item rule


# Files and folders


def field_folder(field, folder=None):
    """The folder holding a field's files: field 2's committed folder by default; field 1's only as given,
    and never inside the repository except under evals/local/ (git ignores it)."""
    if folder is None:
        if field == 1:
            raise Refused("field 1's files come from Harold's library: give --folder, outside the repository "
                          "or under evals/local/")
        return FIELD2
    return private_path(field, folder)


def private_path(field, path):
    path = Path(path).resolve()
    if field == 1 and path.is_relative_to(ROOT) and not path.is_relative_to(LOCAL):
        raise Refused(f"{path} is inside the repository: field 1's files stay outside it or under evals/local/")
    return path


def outputs(folder):
    return OUT / "field2" if folder == FIELD2 else folder / "out"


def write_json(path, data):
    """Written whole, as tier1.write_atomic writes (a part file, synced, then renamed over the file), and
    owner-only from its creation: the part file is made 0600, and every folder made here 0700."""
    path = Path(path)
    for folder in reversed([p for p in (path.parent, *path.parent.parents) if not p.exists()]):
        folder.mkdir(mode=0o700)
    part = path.with_name(path.name + ".part")
    with os.fdopen(os.open(part, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600), "w", encoding="utf-8") as f:
        os.fchmod(f.fileno(), 0o600)  # a part file left by an earlier cut-off save keeps its mode otherwise
        f.write(json.dumps(data, ensure_ascii=False, indent=1) + "\n")
        f.flush()
        os.fsync(f.fileno())
    os.replace(part, path)


def read_json(path):
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def sha256_of(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def passage_key(paper, ordinal, text):
    """A short digest of a passage's paper, ordinal and letters and digits: the page sends it back with each
    passage it picks, so a choice that pairs one paper with another paper's passage is refused, never saved."""
    return hashlib.sha256(f"{paper}\0{ordinal}\0{_key(text)}".encode()).hexdigest()[:16]


def item_sha256(item):
    return hashlib.sha256(json.dumps(item, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def load_settings(path=SETTINGS):
    return read_json(path)


# Formats


def _keys(where, obj, required, optional=()):
    if not isinstance(obj, dict):
        return [f"{where}: not an object"]
    problems = [f"{where}: {k} missing" for k in required if k not in obj]
    return problems + [f"{where}: {k} is not a field of this format" for k in obj if k not in (*required, *optional)]


def _text(where, value, bound=None):
    if not isinstance(value, str) or not value.strip():
        return [f"{where}: empty or not text"]
    return [f"{where}: longer than {bound} characters"] if bound and len(value) > bound else []


def _han(ch):
    return "㐀" <= ch <= "鿿" or "豈" <= ch <= "﫿" or "\U00020000" <= ch <= "\U0003134f"


def language_problem(text, language):
    """Why text is not in language, or None: Chinese (Simplified) holds Han characters, each in GB2312,
    English none. English terms and full-width punctuation in Chinese text are fine.
    ponytail: GB2312 stands for Simplified Chinese; a rare simplified character outside it is refused too,
    and is then written another way."""
    if not isinstance(language, str) or language not in LANGUAGES:
        return f"language {language!r} is not one of {list(LANGUAGES)}"
    if not isinstance(text, str):
        return "not text"
    han = [ch for ch in text if _han(ch)]
    if language == "en":
        return "marked en but holds Chinese characters" if han else None
    if not han:
        return "marked zh-CN but holds no Chinese characters"
    other = []
    for ch in han:
        try:
            ch.encode("gb2312")
        except UnicodeEncodeError:
            other.append(ch)
    return f"marked zh-CN (Simplified) but holds {''.join(sorted(set(other)))}" if other else None


def attribution(paper):
    return f"{', '.join(paper['authors'])}. {paper['title']}. arXiv:{paper['id']}"


def check_papers(doc, field, settings):
    """The paper list's problems: each paper's fields, and for field 2 the selection's criteria (spec
    section 14, ticket 70): primary category, submission window, the exact two licenses, read from that
    version's own abstract page."""
    problems = _keys("papers.json", doc, ("field", "papers", "extractor") + (("criteria", "seed") if field == 2 else ()),
                     ("note",))
    if problems:
        return problems
    if doc["field"] != field:
        return [f"papers.json: field {doc['field']}, expected {field}"]
    problems += _keys("papers.json extractor", doc["extractor"], ("name", "version"))
    if field == 2 and not (isinstance(doc["criteria"], dict) and isinstance(doc["seed"], int)):
        problems.append("papers.json: the selection's criteria and seed are not recorded")
    criteria = settings["field2_papers"]
    seen = set()
    for i, paper in enumerate(doc["papers"] if isinstance(doc["papers"], list) else []):
        where = f"paper {paper.get('id', i) if isinstance(paper, dict) else i}"
        common = ("id", "title", "file", "size", "sha256", "document_language")
        if field == 2:
            required = common + ("authors", "primary_category", "submitted", "topic", "abstract_url", "pdf_url",
                                 "license", "license_page", "license_checked")
        else:
            required = common
        found = _keys(where, paper, required, ("authors", "note"))
        if found:
            problems += found
            continue
        if paper["id"] in seen:
            problems.append(f"{where}: listed twice")
        seen.add(paper["id"])
        problems += _text(f"{where} title", paper["title"])
        if not (isinstance(paper["size"], int) and isinstance(paper["sha256"], str) and len(paper["sha256"]) == 64):
            problems.append(f"{where}: size or SHA-256 missing")
        if paper["document_language"] not in LANGUAGES:
            problems.append(f"{where}: document_language {paper['document_language']!r}")
        if field == 1:
            if not ID.fullmatch(str(paper["id"])):
                problems.append(f"{where}: id is not a plain name")
            continue
        pid = str(paper["id"])
        if not ARXIV.fullmatch(pid):
            problems.append(f"{where}: not an arXiv id with its version (2601.01234v2)")
        if not (isinstance(paper["authors"], list) and paper["authors"] and all(isinstance(a, str) and a.strip()
                                                                               for a in paper["authors"])):
            problems.append(f"{where}: authors missing")
        if paper["document_language"] != "en":
            problems.append(f"{where}: field 2's papers are English")
        problems += _text(f"{where} topic", paper["topic"])
        if paper["primary_category"] not in criteria["primary_categories"]:
            problems.append(f"{where}: primary category {paper['primary_category']!r} is not one of "
                            f"{criteria['primary_categories']}")
        window = criteria["submitted"]
        if not (DATE.fullmatch(str(paper["submitted"])) and window["from"] <= paper["submitted"] <= window["to"]):
            problems.append(f"{where}: submitted {paper['submitted']!r} is outside {window['from']} to {window['to']}")
        if paper["license"] not in LICENSES:
            problems.append(f"{where}: license {paper['license']!r} is neither CC BY 4.0 nor CC0 1.0")
        if paper["abstract_url"] != f"https://arxiv.org/abs/{pid}" or paper["license_page"] != paper["abstract_url"]:
            problems.append(f"{where}: the license must be read from that version's own abstract page")
        if paper["pdf_url"] != f"https://arxiv.org/pdf/{pid}":
            problems.append(f"{where}: pdf_url is not that version's")
        if not DATE.fullmatch(str(paper["license_checked"])):
            problems.append(f"{where}: license_checked is not a date")
    if not isinstance(doc["papers"], list):
        problems.append("papers.json: papers is not a list")
    return problems


def check_locator(where, loc, field, papers):
    """A locator's problems: its fields, its paper, a usable passage kind, and for field 2 its attribution
    and its paper version's license (spec section 14)."""
    problems = _keys(where, loc, ("paper", "ordinal", "page", "kind", "section_path", "quote")
                     + (("attribution", "license") if field == 2 else ()))
    if problems:
        return problems
    if not isinstance(loc["paper"], str):
        return [f"{where}: paper is not a paper's id"]
    paper = papers.get(loc["paper"])
    if paper is None:
        return [f"{where}: paper {loc['paper']!r} is not in papers.json"]
    if not (isinstance(loc["ordinal"], int) and loc["ordinal"] >= 0):
        problems.append(f"{where}: ordinal is not a passage's")
    if not (loc["page"] is None or isinstance(loc["page"], int) and loc["page"] >= 1):
        problems.append(f"{where}: page is not a page number")
    if loc["kind"] == "reference":
        problems.append(f"{where}: a reference passage is never evidence (section 7.1)")
    elif loc["kind"] not in KINDS:
        problems.append(f"{where}: kind {loc['kind']!r}")
    if not (isinstance(loc["section_path"], list) and all(isinstance(s, str) for s in loc["section_path"])):
        problems.append(f"{where}: section_path is not a list of headings")
    problems += _text(f"{where} quote", loc["quote"])
    if field == 2:
        if loc["attribution"] != attribution(paper):
            problems.append(f"{where}: attribution is not its paper's ({attribution(paper)})")
        if loc["license"] != paper["license"] or loc["license"] not in LICENSES:
            problems.append(f"{where}: license is not its paper version's")
    return problems


def papers_of(item):
    return sorted({loc["paper"] for ev in item.get("evidence") or [] for loc in ev})


def multi_paper(item):
    return len(papers_of(item)) > 1


def check_question(item, field, papers):
    where = f"question {item.get('id') if isinstance(item, dict) else '?'}"
    problems = _keys(where, item, ("id", "text", "query_language", "document_language", "answerable", "evidence"),
                     ("short_answer", "note"))
    if problems:
        return problems
    problems += _text(f"{where} text", item["text"], TEXT_BOUND["text"])
    if problem := language_problem(item["text"], item["query_language"]):
        problems.append(f"{where}: {problem}")
    if item["document_language"] not in LANGUAGES:
        problems.append(f"{where}: document_language {item['document_language']!r}")
    if not isinstance(item["answerable"], bool) or not isinstance(item["evidence"], list):
        return problems + [f"{where}: answerable or evidence of the wrong type"]
    if item["answerable"]:
        if "short_answer" not in item:
            problems.append(f"{where}: an answerable question has a short answer")
        else:
            problems += _text(f"{where} short_answer", item["short_answer"], TEXT_BOUND["short_answer"])
        if not item["evidence"]:
            problems.append(f"{where}: an answerable question has evidence")
    else:
        if "short_answer" in item:
            problems.append(f"{where}: an unanswerable question has no short answer")
        if item["evidence"]:
            problems.append(f"{where}: an unanswerable question has no evidence")
    for n, ev in enumerate(item["evidence"], start=1):
        if not (isinstance(ev, list) and ev):
            problems.append(f"{where} evidence item {n}: not a list of passages")
            continue
        for loc in ev:
            found = check_locator(f"{where} evidence item {n}", loc, field, papers)
            problems += found
            if not found and papers[loc["paper"]]["document_language"] != item["document_language"]:
                problems.append(f"{where}: document_language is not its evidence's papers'")
    return problems


def check_pair(item, field, papers):
    where = f"pair {item.get('id') if isinstance(item, dict) else '?'}"
    problems = _keys(where, item, ("id", "sentence", "sentence_language", "passage", "drafted_category"), ("note",))
    if problems:
        return problems
    problems += _text(f"{where} sentence", item["sentence"], TEXT_BOUND["text"])
    if problem := language_problem(item["sentence"], item["sentence_language"]):
        problems.append(f"{where}: {problem}")
    if item["drafted_category"] not in CATEGORIES:
        problems.append(f"{where}: drafted_category {item['drafted_category']!r} is not one of {list(CATEGORIES)}")
    found = check_locator(f"{where} passage", item["passage"], field, papers)
    problems += found
    if not found and isinstance(item["sentence"], str):
        a, b = _key(item["sentence"]), _key(item["passage"]["quote"])
        if SequenceMatcher(None, a, b, autojunk=False).find_longest_match(0, len(a), 0, len(b)).size > COPY_BOUND:
            problems.append(f"{where}: the sentence copies its passage (more than {COPY_BOUND} letters and digits "
                            "in a row); write it in new words")
    return problems


def check_set(doc, kind, field, papers):
    """A draft set's problems: its header, each item's, and no repeated id or question text."""
    problems = _keys(f"{kind}.json", doc, ("id", "kind", "field", "provisional", "items"), ("note",))
    if problems:
        return problems
    if doc["kind"] != kind or doc["field"] != field or not ID.fullmatch(str(doc["id"])):
        return [f"{kind}.json: kind {doc['kind']!r}, field {doc['field']!r} or id {doc['id']!r} is not this set's"]
    if doc["provisional"] is not True:
        problems.append(f"{kind}.json: drafts are provisional until a rater's labels make them gold")
    if not isinstance(doc["items"], list):
        return problems + [f"{kind}.json: items is not a list"]
    check = check_question if kind == "questions" else check_pair
    ids, texts = Counter(), Counter()
    for item in doc["items"]:
        problems += check(item, field, papers)
        if isinstance(item, dict):
            if not ID.fullmatch(str(item.get("id"))):
                problems.append(f"{kind} item {item.get('id')!r}: id is not a plain name")
            ids[item.get("id")] += 1
            texts[_key(str(item.get("text") or item.get("sentence") or ""))] += 1
    problems += [f"{kind}: id {i!r} is used {n} times" for i, n in ids.items() if n > 1]
    problems += [f"{kind}: {n} items have the same text" for t, n in texts.items() if t and n > 1]
    return problems


EDITS = ("text", "short_answer", "answerable", "evidence")  # what a question's edit may change


def check_labels(doc, set_doc, papers, rater=None):
    """A labels file's problems (L1): its header names this set and rater; each label's fields; and each
    label for an item as it now is (one that will be applied) leaves a question that passes every rule a
    draft passes, its locators included. A label for an item changed since is not applied, so not checked
    here: the page shows the item again, and its next save is checked."""
    problems = _keys("labels", doc, ("set", "kind", "field", "rater", "set_sha256", "labels"), ("sample",))
    if problems:
        return problems
    if (doc["set"], doc["kind"], doc["field"]) != (set_doc["id"], set_doc["kind"], set_doc["field"]):
        return [f"labels: they are set {doc['set']!r}'s ({doc['kind']}, field {doc['field']}), not this set's"]
    if rater is not None and doc["rater"] != rater:
        return [f"labels: they are rater {doc['rater']!r}'s, not {rater!r}'s"]
    if not isinstance(doc["labels"], dict):
        return ["labels: labels is not an object"]
    items = {i["id"]: i for i in set_doc["items"]}
    for item, label in doc["labels"].items():
        if set_doc["kind"] == "pairs":
            problems += _keys(f"label {item}", label, ("item_sha256", "revision", "saved_at", "category"))
            if isinstance(label, dict) and label.get("category") not in CATEGORIES:
                problems.append(f"label {item}: category {label.get('category')!r}")
        else:
            found = _keys(f"label {item}", label, ("item_sha256", "revision", "saved_at", "decision"), EDITS)
            if not found:
                try:
                    found = label_content_problems(item, label, items.get(item), set_doc["field"], papers)
                except Exception as error:  # noqa: BLE001 a hand-edited file of any shape is refused, never a crash
                    found = [f"label {item}: malformed ({type(error).__name__})"]
            problems += found
        if isinstance(label, dict) and not (isinstance(label.get("revision"), int) and label["revision"] >= 1):
            problems.append(f"label {item}: revision is not a count")
    return problems


def label_content_problems(item_id, label, drafted, field, papers):
    """A question label's content problems: its decision; each field it changes, of the right type, its
    evidence locators each checked as a draft's are (whatever the item's state, since the page shows them);
    and for an item as it now is (one the label will be applied to), the question it leaves must pass every
    rule a draft passes."""
    where = f"label {item_id}"
    if label["decision"] not in DECISIONS:
        return [f"{where}: decision {label['decision']!r}"]
    if (label["decision"] == "edit") != any(k in label for k in EDITS):
        return [f"{where}: an edit names what it changes; keep and drop change nothing"]
    found = [f"{where}: {name} is not text" for name in ("text", "short_answer")
             if name in label and not isinstance(label[name], str)]
    if "answerable" in label and not isinstance(label["answerable"], bool):
        found.append(f"{where}: answerable is not true or false")
    if "evidence" in label:
        if not (isinstance(label["evidence"], list) and all(isinstance(ev, list) and ev for ev in label["evidence"])):
            return found + [f"{where}: evidence is not a list of evidence items"]
        found += [p for ev in label["evidence"] for loc in ev for p in check_locator(f"{where} evidence", loc, field, papers)]
    if not found and drafted is not None and label_state(drafted, label) == "done":
        made = labeled(drafted, label)
        if made is not None:
            found += [f"{where}: {p}" for p in check_question(made, field, papers)]
    return found


def mix(kind, items):
    """The set's counts (D2): per language, answerable, unanswerable, several papers and paper for
    questions; per drafted category, language and paper for pairs; passages cited more than once."""
    cited = Counter()
    if kind == "questions":
        for item in items:
            cited.update({(loc["paper"], loc["ordinal"]) for ev in item["evidence"] for loc in ev})
        out = {"items": len(items),
               "query_languages": dict(sorted(Counter(i["query_language"] for i in items).items())),
               "unanswerable": dict(sorted(Counter(i["query_language"] for i in items if not i["answerable"]).items())),
               "multi_paper": dict(sorted(Counter(i["query_language"] for i in items if multi_paper(i)).items())),
               "per_paper": dict(sorted(Counter(p for i in items for p in papers_of(i)).items()))}
    else:
        cited.update((i["passage"]["paper"], i["passage"]["ordinal"]) for i in items)
        out = {"items": len(items),
               "sentence_languages": dict(sorted(Counter(i["sentence_language"] for i in items).items())),
               "drafted_categories": dict(sorted(Counter(i["drafted_category"] for i in items).items())),
               "per_paper": dict(sorted(Counter(i["passage"]["paper"] for i in items).items()))}
    out["passages_cited_more_than_once"] = sum(1 for n in cited.values() if n > 1)
    return out


def differences(kind, counts, settings):
    """Where a set's mix differs from the settings' targets: reported, never refused."""
    target = settings[kind]
    found = [] if counts["items"] == target["count"] else [f"{counts['items']} items, target {target['count']}"]
    for name, wanted in target.items():
        if isinstance(wanted, dict):
            have = counts.get(name, {})
            found += [f"{name} {k}: {have.get(k, 0)}, target {v}" for k, v in wanted.items() if have.get(k, 0) != v]
    return found


# Reading the papers through the app


@contextlib.asynccontextmanager
async def reading_app(data_dir):
    """The app on a new data folder as the desktop runs it, an in-memory credential store and no search
    model: (client, state)."""
    import httpx
    from backend.app import create_app
    from backend.self_test import _Keys
    app = create_app(data_dir, origin=tier1.ORIGIN, keyring_backend=_Keys())
    async with app.app.router.lifespan_context(app.app):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url=tier1.ORIGIN,
                                     headers={"X-Scholia-Client": "local"}, timeout=3600) as client:
            yield client, app.app.state.scholia


def verify_pdfs(papers, pdfs):
    """Each listed PDF checked against its size and SHA-256 before anything is read (never downloaded)."""
    from backend.local_helper import mismatch
    for paper in papers:
        path = Path(pdfs) / paper["file"]
        if not path.is_file():
            raise Refused(f"paper {paper['id']}: {paper['file']} is missing under {pdfs}; prepare it first "
                          "(the runner never downloads)")
        if problem := mismatch(path, paper):
            raise Refused(f"paper {paper['id']}: {problem}")


async def read_papers(client, state, papers, pdfs):
    """{paper id: {state, reason, extractor, passages}}: each PDF as the app reads it, in one review-locked
    project, one file a request (a PDF is large); passages in reading order with the app's ids. A paper
    not read whole (a failed reading, pages waiting for OCR) has its state and reason and no passages."""
    from backend.search_index import readings
    project = await tier1.new_project(client, "Gold set papers")
    materials = []
    for paper in papers:
        materials += await tier1.add_files(client, project, [(f"{paper['id']}.pdf", (Path(pdfs) / paper["file"]).read_bytes())])
    if len(set(materials)) != len(materials):
        raise Refused("two listed papers are the same file")
    await tier1.until_read(state)
    response = await client.get(f"/api/projects/{project}/materials")
    listed = {m["id"]: m for m in response.json()["materials"]}
    found = await asyncio.to_thread(state["db"].read, lambda c: readings(c, project))
    out = {}
    for paper, material in zip(papers, materials, strict=True):
        described = listed[material]
        extraction = described.get("extraction")
        passages = sorted(({"id": pid, "ordinal": r[6], "page": r[5], "kind": r[3], "section_path": r[2], "text": r[4]}
                           for pid, r in found.items() if r[0] == material), key=lambda p: p["ordinal"])
        out[paper["id"]] = {"state": described["state"], "reason": described["reason"],  # no text: needs_attention
                            "extractor": [extraction["extractor"], extraction["version"]] if extraction else None,
                            "passages": passages if described["state"] == "ready" else []}
    return out, project


def readable(text):
    """A passage's text without the noncharacters PDF extraction leaves at a line-break hyphen (U+FFFE): the
    worksheet, the quotes and the page show it so; quotes are compared by letters and digits either way."""
    return text.replace("\ufffe", "").replace("\uffff", "")


def worksheet(field, doc, papers_sha256, read):
    """The drafter's worksheet: every paper's passages as the app reads them, with what a locator needs;
    reference passages marked not usable. `finished` only when every paper was read whole."""
    papers = {p["id"]: p for p in doc["papers"]}
    extractors = {tuple(r["extractor"]) for r in read.values() if r["extractor"]}
    sheet = {"status": "finished" if all(r["state"] == "ready" for r in read.values()) else "incomplete",
             "field": field, "papers_sha256": papers_sha256,
             "extractor": dict(zip(("name", "version"), extractors.pop())) if len(extractors) == 1 else None,
             "not_read_whole": {pid: r["reason"] for pid, r in sorted(read.items()) if r["state"] != "ready"},
             "papers": {}}
    for pid in sorted(read):
        r, paper = read[pid], papers[pid]
        entry = {"title": paper["title"], "document_language": paper["document_language"], "state": r["state"],
                 "reason": r["reason"]}
        if field == 2:
            entry.update(attribution=attribution(paper), license=paper["license"])
        entry["passages"] = [{"ordinal": p["ordinal"], "page": p["page"], "kind": p["kind"],
                              "section_path": p["section_path"], "usable": p["kind"] != "reference",
                              "text": readable(p["text"])}
                             for p in r["passages"]]
        sheet["papers"][pid] = entry
    return sheet


def locators_of_item(kind, item):
    return [loc for ev in item["evidence"] for loc in ev] if kind == "questions" else [item["passage"]]


def locators_of(kind, items):
    for item in items:
        if kind == "questions":
            for n, ev in enumerate(item["evidence"], start=1):
                yield f"question {item['id']} evidence item {n}", ev
        else:
            yield f"pair {item['id']} passage", [item["passage"]]


def resolve(kind, items, read, extractor):
    """(problems, moved): every locator confirmed against the app's reading. With the extractor the set
    was drafted against, the passage at its ordinal must be its quote (letters and digits compared, as
    tier 1 compares) with its page, kind and section; with another, each quote is mapped as tier 1 maps
    evidence and each locator whose passage moved is reported, never rewritten."""
    problems, moved = [], []
    for where, locs in locators_of(kind, items):
        for loc in locs:
            paper = read.get(loc["paper"])
            if paper is None or paper["state"] != "ready":
                problems.append(f"{where}: paper {loc['paper']} was not read whole "
                                f"({paper['reason'] if paper else 'not read'}); its locators are refused")
                continue
            usable = [p for p in paper["passages"] if p["kind"] != "reference"]
            if paper["extractor"] == [extractor["name"], extractor["version"]]:
                at = next((p for p in usable if p["ordinal"] == loc["ordinal"]), None)
                if at is None or _key(at["text"]) != _key(loc["quote"]):
                    problems.append(f"{where}: the quote is not passage {loc['ordinal']} of {loc['paper']}")
                elif (at["page"], at["kind"], at["section_path"]) != (loc["page"], loc["kind"], loc["section_path"]):
                    problems.append(f"{where}: passage {loc['ordinal']}'s page, kind or section differ from the locator's")
                continue
            hits = [p for p in usable if _key(p["text"]) == _key(loc["quote"])] or [
                p for p in usable if p["ordinal"] in tier1.evidence_passages(loc["quote"], [(q["ordinal"], q["text"])
                                                                                           for q in usable])]
            if not hits:
                problems.append(f"{where}: the quote matches no passage of {loc['paper']} as read now")
            elif [p["ordinal"] for p in hits] != [loc["ordinal"]]:
                moved.append(f"{where}: {loc['paper']} passage {loc['ordinal']} is now {[p['ordinal'] for p in hits]}")
    return problems, moved


def qasper_ids(folder, settings):
    """Every paper id in QASPER v0.3's three splits, each file checked against its pin first."""
    from backend.local_helper import mismatch
    ids = set()
    for pin in settings["qasper"]["files"]:
        path = Path(folder) / pin["path"]
        if not path.is_file():
            raise Refused(f"QASPER: {pin['path']} is missing under {folder}")
        if problem := mismatch(path, pin):
            raise Refused(f"QASPER: {problem}")
        ids |= set(read_json(path))
    return ids


# Commands: check and passages


def load_field(field, folder):
    """(papers.json, its SHA-256, {kind: set}) of a field's folder; a set not drafted yet is absent."""
    if not (folder / "papers.json").is_file():
        raise Refused(f"{folder / 'papers.json'} is missing: nothing is drafted yet")
    sets = {kind: read_json(folder / f"{kind}.json") for kind in SETS if (folder / f"{kind}.json").is_file()}
    return read_json(folder / "papers.json"), sha256_of(folder / "papers.json"), sets


async def write_when_done(out, make):
    """The file at out says incomplete from the start, and holds the result only once it is all made (an
    interruption, refusal or failure leaves the incomplete marker with its reason)."""
    write_json(out, {"status": "incomplete", "reason": "running"})
    try:
        result = await make()
    except BaseException as error:
        write_json(out, {"status": "incomplete", "reason": f"{type(error).__name__}: {str(error)[:300]}"})
        raise
    write_json(out, result)
    return result


async def check(field, folder, pdfs=None, qasper=None, open_app=None, settings=None):
    """The field's report: the paper list, each set and labels file checked; with pdfs, every locator
    resolved on the app's reading; with qasper, field 2's papers checked against QASPER's. Written to the
    output folder's check.json; `status: finished` and `problems` empty when everything holds."""
    settings = settings or load_settings()
    open_app = open_app or reading_app

    async def make():
        doc, papers_sha256, sets = load_field(field, folder)
        if field == 2 and pdfs is not None and qasper is None:
            raise Refused("field 2's check with the PDFs also needs --qasper (QASPER's papers are left out)")
        problems = check_papers(doc, field, settings)
        papers = {p["id"]: p for p in doc["papers"]} if not problems else {}
        edited = defaultdict(list)  # (labels file, the item as its label leaves it), for edited evidence
        report = {"status": "finished", "field": field, "papers_sha256": papers_sha256, "papers": len(papers),
                  "sets": {}, "labels": {}, "samples": {}, "differences": [], "moved": [], "resolved": False}
        if field == 2 and not problems:
            target = settings["field2_papers"]
            groups = len({p["topic"] for p in papers.values()})
            report["topic_groups"] = groups
            if len(papers) != target["count"]:
                report["differences"].append(f"{len(papers)} papers, target {target['count']}")
            if not target["topic_groups"]["min"] <= groups <= target["topic_groups"]["max"]:
                report["differences"].append(f"{groups} topic groups, target {target['topic_groups']['min']} to "
                                             f"{target['topic_groups']['max']}")
        if qasper is not None and not problems:
            taken = qasper_ids(qasper, settings)
            problems += [f"paper {pid}: a QASPER paper (left out of field 2)" for pid in papers
                         if pid.rsplit("v", 1)[0] in taken]
        for kind, set_doc in sets.items():
            found = check_set(set_doc, kind, field, papers) if papers else []
            problems += found
            if found or not papers:
                continue
            report["sets"][kind] = {"id": set_doc["id"], "sha256": sha256_of(folder / f"{kind}.json"),
                                    **mix(kind, set_doc["items"])}
            report["differences"] += [f"{kind}: {d}" for d in differences(kind, report["sets"][kind], settings)]
            items = {i["id"]: i for i in set_doc["items"]}
            for path in sorted((folder / "labels").glob(f"{set_doc['id']}.*.json")):
                labels = read_json(path)
                found = check_labels(labels, set_doc, papers)
                problems += [f"{path.name}: {p}" for p in found]
                if not found:
                    report["labels"][path.name] = label_counts(items, labels)
                    edited[kind] += [(path.name, made) for i, label in labels["labels"].items()
                                     if i in items and label_state(items[i], label) == "done" and "evidence" in label
                                     and (made := labeled(items[i], label)) is not None]
            if (folder / f"sample-{kind}.json").is_file():
                sample = read_json(folder / f"sample-{kind}.json")
                if sample.get("set") != set_doc["id"] or not set(sample.get("items", [])) <= set(items):
                    problems.append(f"sample-{kind}.json: not a sample of this set")
                else:
                    report["samples"][kind] = len(sample["items"])
        if pdfs is not None and not problems:
            verify_pdfs(doc["papers"], pdfs)
            with tempfile.TemporaryDirectory(prefix="scholia-gold-") as tmp:
                async with open_app(Path(tmp) / "data") as (client, state):
                    read, _ = await read_papers(client, state, doc["papers"], pdfs)
            report["not_read_whole"] = {pid: r["reason"] for pid, r in sorted(read.items()) if r["state"] != "ready"}
            for kind, set_doc in sets.items():
                found, moved = resolve(kind, set_doc["items"], read, doc["extractor"])
                problems += found
                report["moved"] += moved
            for name, made in edited["questions"]:  # evidence a rater chose resolves as drafted evidence does
                found, moved = resolve("questions", [made], read, doc["extractor"])
                problems += [f"{name}: {p}" for p in found]
                report["moved"] += [f"{name}: {m}" for m in moved]
            extractors = sorted({tuple(r["extractor"]) for r in read.values() if r["extractor"]})
            if extractors != [(doc["extractor"]["name"], doc["extractor"]["version"])]:
                report["extractor_now"] = [list(e) for e in extractors]
            report["resolved"] = not problems
        report["problems"] = problems
        return report
    return await write_when_done(outputs(folder) / "check.json", make)


async def passages(field, folder, pdfs, open_app=None, settings=None):
    """The passage worksheet (D1): every listed paper read by the app, written to the output folder."""
    settings = settings or load_settings()
    open_app = open_app or reading_app

    async def make():
        doc, papers_sha256, _ = load_field(field, folder)
        if problems := check_papers(doc, field, settings):
            raise Refused("; ".join(problems[:5]))
        verify_pdfs(doc["papers"], pdfs)
        with tempfile.TemporaryDirectory(prefix="scholia-gold-") as tmp:
            async with open_app(Path(tmp) / "data") as (client, state):
                read, _ = await read_papers(client, state, doc["papers"], pdfs)
        return worksheet(field, doc, papers_sha256, read)
    return await write_when_done(outputs(folder) / "worksheet.json", make)


# Labels, the corrected set, the sample and the agreement


def label_state(item, label):
    """None (not labeled), "changed" (labeled before the item changed: shown again, never applied) or
    "done"."""
    if label is None:
        return None
    return "done" if label["item_sha256"] == item_sha256(item) else "changed"


def label_counts(items, labels):
    states = Counter(label_state(items[i], l) if i in items else "orphaned" for i, l in labels["labels"].items())
    return {"rater": labels["rater"], "labeled": states["done"], "changed": states["changed"],
            "orphaned": states["orphaned"], "unlabeled": len(items) - states["done"] - states["changed"]}


def labeled(item, label):
    """The item as a rater's label leaves it: None when dropped."""
    if "category" in label:
        return {**item, "label": label["category"]}
    if label["decision"] == "drop":
        return None
    out = {**item, **{k: label[k] for k in ("text", "short_answer", "answerable", "evidence") if k in label}}
    if not out["answerable"]:
        out.pop("short_answer", None)
        out["evidence"] = []
    return out


def apply(set_doc, labels, papers):
    """The corrected set (L5): each item a label is done for, as the label leaves it (dropped items out),
    `provisional: false`; every other item as drafted, `provisional: true`, never counted as gold; labels
    of items no longer in the set kept out and counted. Labels that do not pass check_labels are refused
    whole: nothing invalid becomes gold."""
    if problems := check_labels(labels, set_doc, papers):
        raise Refused("the labels do not pass their check: " + "; ".join(problems[:5]))
    items, out = [], {"labeled": 0, "dropped": 0, "changed": 0, "provisional": 0}
    for item in set_doc["items"]:
        label = labels["labels"].get(item["id"])
        state = label_state(item, label)
        if state == "done":
            made = labeled(item, label)
            if made is None:
                out["dropped"] += 1
                continue
            items.append({**made, "provisional": False})
            out["labeled"] += 1
        else:
            out["changed" if state == "changed" else "provisional"] += 1
            items.append({**item, "provisional": True})
    counts = {**out, "orphaned": sum(1 for i in labels["labels"] if i not in {x["id"] for x in set_doc["items"]})}
    return {"id": f"{set_doc['id']}.corrected", "kind": set_doc["kind"], "field": set_doc["field"], "corrected": True,
            "rater": labels["rater"], "set_content_sha256": item_sha256(set_doc),
            "labels_content_sha256": item_sha256(labels),
            "counts": counts, "items": items}


def features(item, names):
    return tuple(str(multi_paper(item) if name == "multi_paper" else item[name]) for name in names)


def draw(set_doc, settings):
    """The second rater's sample (H1): the settings' fraction of the set, stratified by the settings'
    features, each stratum's share by largest remainder, drawn with the recorded seed."""
    kind, config = set_doc["kind"], settings["sample"]
    names = config["strata"][kind]
    strata = defaultdict(list)
    for item in set_doc["items"]:
        strata[features(item, names)].append(item["id"])
    total = round(config["fraction"] * len(set_doc["items"]))
    quota = {s: config["fraction"] * len(ids) for s, ids in strata.items()}
    take = {s: int(q) for s, q in quota.items()}
    for s in sorted(strata, key=lambda s: (take[s] - quota[s], s))[:total - sum(take.values())]:
        take[s] += 1
    rng = random.Random(config["seed"])
    chosen = [i for s in sorted(strata) for i in rng.sample(sorted(strata[s]), take[s])]
    return {"set": set_doc["id"], "kind": kind, "fraction": config["fraction"], "seed": config["seed"], "strata": names,
            "drawn": {"|".join(s): {"items": len(strata[s]), "drawn": take[s]} for s in sorted(strata)},
            "items": sorted(chosen)}


def kappa(pairs, categories):
    """Cohen's kappa of (a, b) labels over the categories; None when both raters used one category alone."""
    n = len(pairs)
    if not n:
        return None
    agreed = sum(a == b for a, b in pairs) / n
    chance = sum(sum(a == c for a, _ in pairs) / n * sum(b == c for _, b in pairs) / n for c in categories)
    return None if chance == 1 else (agreed - chance) / (1 - chance)


def _agreement(pairs, categories):
    n = len(pairs)
    table = {a: {b: sum(1 for x, y in pairs if (x, y) == (a, b)) for b in categories} for a in categories}
    value = kappa(pairs, categories)
    return {"both_labeled": n, "percent_agreement": round(100 * sum(a == b for a, b in pairs) / n, 2) if n else None,
            "kappa": None if value is None else round(value, 4), "confusion": table}


def agreement(set_doc, first, second):
    """The agreement of two raters' labels on the items both labeled (H2). Pairs: percent agreement,
    Cohen's kappa over the three categories and the confusion table (rows the first rater's). Questions:
    the same for keep or drop, then over the items both kept, for answerable, and for evidence: both chose
    the same items, and the mean overlap of the passages they chose (shared over either's)."""
    both = []
    for item in set_doc["items"]:
        a, b = (labels["labels"].get(item["id"]) for labels in (first, second))
        if label_state(item, a) == "done" and label_state(item, b) == "done":
            both.append((item, a, b))
    out = {"set": set_doc["id"], "raters": [first["rater"], second["rater"]]}
    if set_doc["kind"] == "pairs":
        return {**out, **_agreement([(a["category"], b["category"]) for _, a, b in both], CATEGORIES)}
    kept = [(item, labeled(item, a), labeled(item, b)) for item, a, b in both]
    out["kept"] = _agreement([(x is not None, y is not None) for _, x, y in kept], (True, False))
    both_kept = [(x, y) for _, x, y in kept if x is not None and y is not None]
    out["answerable"] = _agreement([(x["answerable"], y["answerable"]) for x, y in both_kept], (True, False))

    def chosen(item):
        return {frozenset((loc["paper"], loc["ordinal"]) for loc in ev) for ev in item["evidence"]}
    answerable = [(chosen(x), chosen(y)) for x, y in both_kept if x["answerable"] and y["answerable"]]
    overlaps = [len(set().union(*x) & set().union(*y)) / len(set().union(*x) | set().union(*y))
                for x, y in answerable if x or y]
    out["evidence"] = {"both_answerable": len(answerable), "same_items": sum(x == y for x, y in answerable),
                       "mean_passage_overlap": None if not overlaps else round(metrics.mean(overlaps), 4)}
    return out


# Tiers 2 and 3: the shapes they are scored in (run at the preview gate, never on the drafts in M2)


def tier2_queries(corrected, passage_of, project):
    """A corrected set's gold answerable questions as the queries tier1.run_arms takes, each its own
    group, with its query and document languages; each evidence item as the app's passage ids that make
    it up (passage_of: {(paper, ordinal): passage id}), one annotator. Drafts are refused (held out)."""
    if corrected.get("corrected") is not True or corrected.get("kind") != "questions":
        raise Refused("tier 2 scores a corrected question set (gold.py apply), never the drafts")
    queries = []
    for item in corrected["items"]:
        if item["provisional"] or not item["answerable"]:  # provisional is not gold; unanswerable is the abstention row's
            continue
        if missing := [loc for ev in item["evidence"] for loc in ev if (loc["paper"], loc["ordinal"]) not in passage_of]:
            raise Refused(f"question {item['id']}: passage {missing[0]['ordinal']} of {missing[0]['paper']} is not in this reading")
        queries.append({"key": item["id"], "text": item["text"], "group": len(queries), "project": project,
                        "query_language": item["query_language"], "document_language": item["document_language"],
                        "annotators": [[[passage_of[(loc["paper"], loc["ordinal"])] for loc in ev]
                                        for ev in item["evidence"]]]})
    return queries


def tier2_scores(queries, rankings):
    """recall@8 per query by tier 1's item rule (an item found when any of its passages is in the top 8),
    and its mean overall and per query language and per document language, reported apart."""
    rows = [tier1.score_qasper(ranking, q["annotators"], RECALL)["recall@8"] for q, ranking in zip(queries, rankings,
                                                                                                    strict=True)]

    def summary(values):
        return {"mean": None if not values else round(metrics.mean(values), 4), "n": len(values)}
    out = {"all": summary(rows)}
    for side in ("query_language", "document_language"):
        out[side] = {lang: summary([v for v, q in zip(rows, queries) if q[side] == lang])
                     for lang in sorted({q[side] for q in queries})}
    return {"recall@8": out, "scores": rows}


def judge_scores(labels, verdicts, settings=None):
    """The support judge against labeled pairs (tier 3, spec section 14): of the pairs labeled unsupported,
    the share it flagged (at least the settings' threshold), and of those labeled supported (at most).
    labels {pair id: category}; verdicts {pair id: verdict}. A pair the judge left unchecked, or gave no
    verdict for, counts as not flagged and is counted apart; partly supported pairs count in neither."""
    config = (settings or load_settings())["judge"]
    flagged = set(config["flagged_verdicts"])
    out = {}
    for category in ("unsupported", "supported"):
        ids = sorted(i for i, c in labels.items() if c == category)
        hits = sum(1 for i in ids if verdicts.get(i) in flagged)
        out[category] = {"labeled": len(ids), "flagged": hits, "share": round(hits / len(ids), 4) if ids else None,
                         "unchecked": sum(1 for i in ids if verdicts.get(i) == "unchecked"),
                         "no_verdict": sum(1 for i in ids if verdicts.get(i) not in VERDICTS)}
    low, high = out["unsupported"]["share"], out["supported"]["share"]
    out["meets"] = None if low is None or high is None else (
        low >= config["unsupported_flagged_at_least"] and high <= config["supported_flagged_at_most"])
    return out


# The review page


def kept_as_sent(sent, label):
    """Whether every field a request names ends up in the label as sent: typed text as trimmed (visible),
    answerable as given, evidence as the same passages in the same items."""
    from backend.settings import visible
    if not isinstance(sent, dict) or sent.get("decision") != "edit":
        return True
    for name in EDITS:
        if name not in sent:
            continue
        if name not in label:
            return False
        if name == "evidence":
            if [[(c.get("paper"), c.get("ordinal")) for c in ev] for ev in sent[name]] != \
                    [[(loc["paper"], loc["ordinal"]) for loc in ev] for ev in label[name]]:
                return False
        elif label[name] != (visible(sent[name]) if name in ("text", "short_answer") else sent[name]):
            return False
    return True


def keyed(evidence):
    return [[{**loc, "key": passage_key(loc["paper"], loc["ordinal"], loc["quote"])} for loc in ev] for ev in evidence]


class Review:
    """One review page: a set's items as the page shows them (for pairs, never the drafted category or a
    judge's verdict), one rater's labels file and the saves to it."""

    def __init__(self, field, folder, kind, rater, *, sample=False, colleague=False, settings=None):
        settings = settings or load_settings()
        if not RATER.fullmatch(rater or ""):
            raise Refused("a rater is named with lower-case letters, digits and hyphens")
        if sample and field == 1 and not colleague:
            raise Refused("field 1's sample holds passages from Harold's library: its page is written only for a "
                          "colleague named with --colleague")
        self.field, self.kind, self.rater, self.lock = field, kind, rater, threading.Lock()
        doc, papers_sha256, sets = load_field(field, folder)
        if kind not in sets:
            raise Refused(f"{kind}.json is not drafted yet")
        self.papers = {p["id"]: p for p in doc["papers"]}
        self.set = sets[kind]
        if problems := check_papers(doc, field, settings) or check_set(self.set, kind, field, self.papers):
            raise Refused("the set does not pass its check: " + "; ".join(problems[:5]))
        self.items = {item["id"]: item for item in self.set["items"]}
        order = list(self.items)
        if kind == "pairs" and settings["review"]["pair_order"] == "shuffled":
            random.Random(f"{settings['review']['order_seed']}:{self.set['id']}").shuffle(order)
        self.sample = None
        if sample:
            self.sample = read_json(folder / f"sample-{kind}.json")
            if self.sample.get("set") != self.set["id"] or not set(self.sample["items"]) <= set(self.items):
                raise Refused(f"sample-{kind}.json is not a sample of this set")
            order = [i for i in order if i in set(self.sample["items"])]
        self.order = order
        self.show_category = settings["review"]["show_drafted_category"]
        self.passages = {}
        if kind == "questions":
            path = outputs(folder) / "worksheet.json"
            if not path.is_file():
                raise Refused(f"{path} is missing: write the worksheet first (gold.py passages)")
            sheet = read_json(path)
            if sheet.get("status") != "finished" or sheet.get("field") != field or sheet.get("papers_sha256") != papers_sha256:
                raise Refused(f"{path} is not a finished worksheet of this paper list: write it again")
            self.passages = {pid: {p["ordinal"]: p for p in entry["passages"] if p["usable"]}
                             for pid, entry in sheet["papers"].items()}
        self.shown_papers = set(self.papers)
        if self.sample is not None and field == 1:  # the colleague sees the sampled items' papers, no more
            self.shown_papers = {loc["paper"] for i in order for loc in locators_of_item(kind, self.items[i])}
            self.passages = {pid: found for pid, found in self.passages.items() if pid in self.shown_papers}
        self.path = folder / "labels" / f"{self.set['id']}.{rater}.json"
        self.notices = []
        if self.path.with_name(self.path.name + ".part").exists():
            self.notices.append("cut_off")  # a save cut off before its rename: the labels file is as last saved
        self.labels = self.read_labels()
        self.set_sha256 = sha256_of(folder / f"{kind}.json")

    def read_labels(self):
        """The labels file as it is now (another page on the same file may have saved since); a file that
        cannot be read, or is not this set's and rater's, is refused and left as it is."""
        if not self.path.exists():
            return {}
        try:
            labels = read_json(self.path)
        except (OSError, ValueError) as error:
            raise Refused(f"{self.path.name} cannot be read ({type(error).__name__}); it is left as it is") from None
        if problems := check_labels(labels, self.set, self.papers, self.rater):
            raise Refused(f"{self.path.name}: " + "; ".join(problems[:5]) + "; it is left as it is")
        return labels["labels"]

    @contextlib.contextmanager
    def file_lock(self):
        """This page's lock, and the labels file's for every process: two pages on one file never lose a save.
        The lock file beside it is owner-only and ignored by git."""
        import fcntl
        if not self.path.parent.exists():
            self.path.parent.mkdir(mode=0o700, parents=True)
        with self.lock:
            fd = os.open(self.path.with_name(f".{self.path.name}.lock"), os.O_WRONLY | os.O_CREAT, 0o600)
            try:
                fcntl.flock(fd, fcntl.LOCK_EX)
                yield
            finally:
                os.close(fd)

    def shown(self, item):
        if self.kind == "pairs":
            loc = item["passage"]
            out = {"id": item["id"], "sentence": item["sentence"], "sentence_language": item["sentence_language"],
                   "passage": {k: loc[k] for k in ("paper", "page", "kind", "section_path", "quote")}}
            if self.show_category:
                out["drafted_category"] = item["drafted_category"]
            return out
        out = {k: item[k] for k in ("id", "text", "query_language", "document_language", "answerable", "short_answer")
               if k in item}
        return {**out, "evidence": keyed(item["evidence"])}

    def shown_label(self, item_id):
        label = self.labels.get(item_id)
        if label is None:
            return None
        out = {k: v for k, v in label.items() if k != "item_sha256"}
        if "evidence" in out:
            out["evidence"] = keyed(out["evidence"])
        return {**out, "state": label_state(self.items[item_id], label)}

    def done(self):
        return sum(1 for i in self.order if label_state(self.items[i], self.labels.get(i)) == "done")

    def data(self):
        with self.file_lock():
            self.labels = self.read_labels()
            return {"kind": self.kind, "field": self.field, "set": self.set["id"], "rater": self.rater,
                    "sample": self.sample is not None, "notices": self.notices,
                    "papers": {pid: p["title"] for pid, p in self.papers.items() if pid in self.shown_papers},
                    "items": [self.shown(self.items[i]) for i in self.order],
                    "labels": {i: self.shown_label(i) for i in self.order if i in self.labels},
                    "done": self.done(), "total": len(self.order)}

    def paper_passages(self, paper):
        if paper not in self.passages:
            return None
        return [{**{k: p[k] for k in ("ordinal", "page", "kind", "section_path", "text")}, "key": passage_key(paper, p["ordinal"], p["text"])}
                for p in self.passages[paper].values()]

    def locator(self, choice):
        """The locator of a passage the rater chose: a usable passage of a listed paper, from the worksheet."""
        if not (isinstance(choice, dict) and set(choice) == {"paper", "ordinal", "key"}):
            raise ValueError("evidence_invalid")
        found = self.passages.get(choice["paper"], {}).get(choice["ordinal"])
        if found is None or isinstance(choice["ordinal"], bool) or choice["key"] != passage_key(choice["paper"], found["ordinal"], found["text"]):
            raise ValueError("evidence_invalid")  # not that paper's passage as the page was shown it
        loc = {"paper": choice["paper"], "ordinal": found["ordinal"], "page": found["page"], "kind": found["kind"],
               "section_path": found["section_path"], "quote": found["text"]}
        if self.field == 2:
            paper = self.papers[choice["paper"]]
            loc.update(attribution=attribution(paper), license=paper["license"])
        return loc

    def label_of(self, item, given):
        """The label a request gives, checked: (fields to store) or ValueError(code)."""
        from backend.settings import visible
        if not isinstance(given, dict):
            raise ValueError("label_invalid")
        if self.kind == "pairs":
            if set(given) != {"category"} or given["category"] not in CATEGORIES:
                raise ValueError("label_invalid")
            return {"category": given["category"]}
        if given.get("decision") not in DECISIONS or not set(given) <= {"decision", "text", "short_answer",
                                                                          "answerable", "evidence"}:
            raise ValueError("label_invalid")
        edits = {k: v for k, v in given.items() if k != "decision"}
        if (given["decision"] == "edit") != bool(edits):
            raise ValueError("edit_needs_a_change" if given["decision"] == "edit" else "label_invalid")
        label = {"decision": given["decision"]}
        for name in ("text", "short_answer"):
            if name in edits:
                text = visible(edits[name]) if isinstance(edits[name], str) else None
                if text is None:
                    raise ValueError("text_blank")
                if len(text) > TEXT_BOUND[name]:
                    raise ValueError("text_too_long")
                label[name] = text
        if "answerable" in edits:
            if not isinstance(edits["answerable"], bool):
                raise ValueError("label_invalid")
            label["answerable"] = edits["answerable"]
        if "evidence" in edits:
            chosen = edits["evidence"]
            if not (isinstance(chosen, list) and len(chosen) <= 30
                    and all(isinstance(ev, list) and 0 < len(ev) <= 10 for ev in chosen)):
                raise ValueError("evidence_invalid")
            label["evidence"] = [[self.locator(c) for c in ev] for ev in chosen]
        if not label.get("answerable", item["answerable"]):
            # The question this label leaves is unanswerable (said here, or drafted so): it keeps no answer and
            # no evidence, so a request naming either is then refused as not kept (kept_as_sent).
            label.pop("short_answer", None)
            label.pop("evidence", None)
        made = labeled(item, label)
        if made is not None and (problems := check_question(made, self.field, self.papers)):
            raise ValueError("text_language" if any("marked" in p for p in problems) else
                             "answerable_needs_answer_and_evidence" if made["answerable"] else "label_invalid")
        return label

    def save(self, body):
        """(status, answer) for a save: written to the labels file (whole, owner-only) before it is
        answered as saved; refused when it carries an older revision of the item's label than the file's."""
        if not (isinstance(body, dict) and set(body) - {"revert"} == {"item", "base", "label"}
                and body["item"] in self.order and isinstance(body["base"], int) and not isinstance(body["base"], bool)
                and isinstance(body.get("revert", False), bool)):
            return 400, {"code": "label_invalid"}
        item = self.items[body["item"]]
        with self.file_lock():
            self.labels = self.read_labels()
            current = self.labels.get(item["id"])
            if body["base"] != (current["revision"] if current else 0):
                return 409, {"code": "changed_elsewhere", "label": self.shown_label(item["id"])}
            given = body["label"]
            edited = (current is not None and label_state(item, current) == "done" and current.get("decision") == "edit")
            if edited and isinstance(given, dict) and given.get("decision") == "keep" and not body.get("revert"):
                # Keep after an edit keeps the edit (the page shows it); going back to the draft is its own action
                return 400, {"code": "keep_would_revert"}
            if edited and isinstance(given, dict) and given.get("decision") == "edit":
                # An edit builds on the saved one: what it does not name stays as saved, never silently dropped
                given = {**{k: current[k] for k in EDITS if k in current and k != "evidence"},
                         **({"evidence": [[{"paper": loc["paper"], "ordinal": loc["ordinal"],
                                            "key": passage_key(loc["paper"], loc["ordinal"], loc["quote"])}
                                           for loc in ev]
                                          for ev in current["evidence"]]} if "evidence" in current else {}),
                         **given}
            try:
                label = self.label_of(item, given)
            except ValueError as error:
                return 400, {"code": str(error)}
            if not kept_as_sent(body["label"], label):
                # The record would not hold a change the rater made (another window made the question
                # unanswerable, so its answer and evidence would go): not saved, and the page is told why.
                return 409, {"code": "change_not_kept", "label": self.shown_label(item["id"])}
            entry = {**label, "item_sha256": item_sha256(item), "revision": body["base"] + 1,
                     "saved_at": time.strftime("%Y-%m-%dT%H:%M:%S%z")}
            labels = {**self.labels, item["id"]: entry}
            doc = {"set": self.set["id"], "kind": self.kind, "field": self.field, "rater": self.rater,
                   "set_sha256": self.set_sha256, **({"sample": True} if self.sample else {}),
                   "labels": dict(sorted(labels.items()))}
            try:
                write_json(self.path, doc)
            except OSError:
                return 500, {"code": "not_saved"}
            self.labels = labels
            return 200, {"label": self.shown_label(item["id"]), "done": self.done()}


TYPES = {".html": "text/html; charset=utf-8", ".js": "text/javascript; charset=utf-8",
         ".css": "text/css; charset=utf-8", ".json": "application/json"}
HEADERS = {"Content-Security-Policy": "default-src 'self'; base-uri 'none'; form-action 'none'; frame-ancestors 'none'",
           "X-Content-Type-Options": "nosniff", "Referrer-Policy": "no-referrer", "Cache-Control": "no-store"}


class _Handler(BaseHTTPRequestHandler):
    server_version = "Scholia-review"
    sys_version = ""

    def log_message(self, *args):  # no access log: paths, bodies and texts stay out of every log
        pass

    def do_GET(self):
        self._handle()

    def do_POST(self):
        self._handle()

    def _send(self, status, body, kind="application/json"):
        if isinstance(body, dict):
            body = json.dumps(body, ensure_ascii=False).encode()
        self.send_response(status)
        for name, value in {**HEADERS, "Content-Type": kind, "Content-Length": str(len(body))}.items():
            self.send_header(name, value)
        self.end_headers()
        self.wfile.write(body)

    def _handle(self):
        from backend.app import static_file
        server = self.server
        path = urlsplit(self.path).path
        scope = {"client": self.client_address[:2], "method": self.command, "path": path,
                 "headers": [(k.lower().encode("latin-1"), v.encode("latin-1")) for k, v in self.headers.items()]}
        if problem := server.guard.problem(scope):
            return self._send(problem[0], {"code": problem[1]})
        if not path.startswith("/api/"):
            target = static_file(PAGE, path.lstrip("/"))
            if target is None or target.suffix not in TYPES:
                return self._send(404, {"code": "not_found"})
            return self._send(200, target.read_bytes(), TYPES[target.suffix])
        given = self.headers.get_all("X-Scholia-Session") or []
        if len(given) != 1 or not hmac.compare_digest(given[0].encode(), server.secret.encode()):
            return self._send(401, {"code": "session_required"})
        review = server.review
        if self.command == "GET" and path == "/api/data":
            try:
                return self._send(200, review.data())
            except (Refused, OSError):  # the labels file unreadable meanwhile (left as it is), or its folder
                return self._send(500, {"code": "labels_unreadable"})
        if self.command == "GET" and path == "/api/passages":
            found = review.paper_passages((parse_qs(urlsplit(self.path).query).get("paper") or [""])[0])
            return self._send(200, {"passages": found}) if found is not None else self._send(404, {"code": "not_found"})
        if self.command == "POST" and path == "/api/label":
            length = int(self.headers.get("Content-Length") or 0)
            if length > MAX_BODY:
                return self._send(413, {"code": "request_too_large"})
            try:
                body = json.loads(self.rfile.read(length))
            except ValueError:
                return self._send(400, {"code": "label_invalid"})
            try:
                return self._send(*review.save(body))
            except (Refused, OSError):  # nothing was written: the labels file unreadable, or its lock not taken
                return self._send(500, {"code": "not_saved"})
        return self._send(404, {"code": "not_found"})


def serve(review, lang="en"):
    """The page's server on 127.0.0.1 and a free port, not yet serving: (server, the page's address with
    this launch's secret in its fragment, which the browser never sends)."""
    from backend.local_guard import LocalRequestGuard
    server = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    server.daemon_threads = True
    origin = f"http://127.0.0.1:{server.server_address[1]}"
    server.secret = secrets.token_urlsafe(32)
    server.guard = LocalRequestGuard(None, origin=origin, session=server.secret)
    server.review = review
    return server, f"{origin}/#session={server.secret}&lang={lang}"


# The command line


def stoppable(work):
    """Run a command's coroutine so that SIGTERM stops it as Ctrl-C does: cancelled, so its incomplete
    marker stays and its temporary data folder is removed (as tier1's run)."""
    async def main_task():
        asyncio.get_running_loop().add_signal_handler(signal.SIGTERM, asyncio.current_task().cancel)
        return await work
    return asyncio.run(main_task())


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    sub = parser.add_subparsers(dest="command", required=True)
    commands = {name: sub.add_parser(name) for name in ("check", "passages", "review", "sample", "agreement", "apply")}
    for name, command in commands.items():
        command.add_argument("--field", type=int, choices=(1, 2), required=True)
        command.add_argument("--folder", help="the field's folder (field 1: required, outside the repository)")
        if name in ("review", "sample", "agreement", "apply"):
            command.add_argument("--set", choices=SETS, required=True)
    for name in ("check", "passages"):
        commands[name].add_argument("--pdfs", help="the folder holding the papers' PDFs (field 1: <folder>/pdfs)")
    commands["check"].add_argument("--qasper", help="QASPER v0.3's prepared folder (field 2)")
    for name in ("review", "apply"):
        commands[name].add_argument("--rater", required=True)
    commands["review"].add_argument("--lang", choices=LANGUAGES, default="en")
    commands["review"].add_argument("--sample", action="store_true", help="the second rater's page: the sample only")
    commands["review"].add_argument("--colleague", action="store_true",
                                    help="field 1: write the sample's page for a colleague (Harold's decision)")
    commands["agreement"].add_argument("--raters", nargs=2, required=True)
    args = parser.parse_args(argv)
    try:
        folder = field_folder(args.field, args.folder)
        pdfs = getattr(args, "pdfs", None)
        if pdfs is not None:
            pdfs = private_path(args.field, pdfs)
        elif args.command == "passages":
            if args.field == 2:
                parser.error("field 2's worksheet needs --pdfs")
            pdfs = folder / "pdfs"
        if args.command == "check":
            report = stoppable(check(args.field, folder, pdfs, args.qasper))
            print(json.dumps(report, ensure_ascii=False, indent=1))
            if report["problems"]:
                sys.exit(f"{len(report['problems'])} problems")
        elif args.command == "passages":
            sheet = stoppable(passages(args.field, folder, pdfs))
            print(f"{outputs(folder) / 'worksheet.json'}: {sheet['status']}"
                  + (f", not read whole: {sheet['not_read_whole']}" if sheet["not_read_whole"] else ""))
        elif args.command == "review":
            review = Review(args.field, folder, args.set, args.rater, sample=args.sample, colleague=args.colleague)
            server, address = serve(review, args.lang)
            if "cut_off" in review.notices:
                print("note: an earlier save was cut off before it finished; the labels file is as last saved")
            print(f"labels: {review.path}")
            print(f"open: {address}", flush=True)
            with contextlib.suppress(KeyboardInterrupt):
                server.serve_forever()
        else:
            papers_doc, _, sets = load_field(args.field, folder)
            papers = {p["id"]: p for p in papers_doc["papers"]}
            if args.set not in sets:
                raise Refused(f"{args.set}.json is not drafted yet")
            set_doc = sets[args.set]
            if args.command == "sample":
                out = folder / f"sample-{args.set}.json"
                if out.exists():
                    raise Refused(f"{out.name} exists: a sample is drawn once, before anyone labels it")
                write_json(out, draw(set_doc, load_settings()))
                print(f"{out}: {len(read_json(out)['items'])} items")
            else:
                if not all(RATER.fullmatch(r) for r in (args.raters if args.command == "agreement" else [args.rater])):
                    raise Refused("a rater is named with lower-case letters, digits and hyphens")

                def labels_of(rater):
                    path = folder / "labels" / f"{set_doc['id']}.{rater}.json"
                    labels = read_json(path)
                    if problems := check_labels(labels, set_doc, papers, rater):
                        raise Refused(f"{path.name}: " + "; ".join(problems[:5]))
                    return labels
                if args.command == "agreement":
                    print(json.dumps(agreement(set_doc, *map(labels_of, args.raters)), ensure_ascii=False, indent=1))
                else:
                    out = outputs(folder) / f"{set_doc['id']}.corrected.{args.rater}.json"
                    corrected = apply(set_doc, labels_of(args.rater), papers)
                    write_json(out, corrected)
                    print(f"{out}: {corrected['counts']}")
    except (Refused, OSError, ValueError) as refused:
        sys.exit(f"refused: {refused}")
    except (KeyboardInterrupt, asyncio.CancelledError):
        sys.exit("stopped: the output file says incomplete")


if __name__ == "__main__":
    main()
