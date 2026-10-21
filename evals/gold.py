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
repository and every other git work tree, because they come from Harold's library. In it:
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
import errno
import fnmatch
import hashlib
import hmac
import json
import os
import random
import re
import secrets
import signal
import stat
import sys
import tempfile
import threading
import time
from collections import Counter, defaultdict
from datetime import date
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
ARXIV = re.compile(r"\d{4}\.\d{4,5}v[1-9]\d*", re.ASCII)  # ASCII digits only, not full-width ones
DATE = re.compile(r"\d{4}-\d{2}-\d{2}", re.ASCII)
RECALL = [{"name": "recall@8", "kind": "recall", "k": 8}]  # tier 2's metric (section 14), tier 1's item rule


# Files and folders


def field_folder(field, folder=None):
    """The folder holding a field's files: field 2's committed folder by default; field 1's only as given,
    and never inside a git work tree."""
    if folder is None:
        if field == 1:
            raise Refused("field 1's files come from Harold's library: give --folder, outside the repository "
                          "and every other git work tree")
        return FIELD2
    return private_path(field, folder)


def private_path(field, path):
    """A field's path, resolved; for field 1, refused anywhere inside a git work tree (this checkout, the main
    checkout or any other worktree, or any other repository), ignored there or not (Harold, 2026-10-21: one
    rule, no git asked). Checked for the folders given (--folder, --pdfs: each taken where it resolves, and
    walked from there) and where a linked PDF leads; under a folder no link is followed at all (the files
    below)."""
    path = Path(path).resolve()
    if field == 1:
        if (top := work_tree(path)) is not None:
            raise Refused(f"{path} is inside the repository (the git work tree {top}): field 1's files stay outside "
                          "every git work tree")
        path = Field1Path(path)
        path.accepted = path
    return path


def field1(path):
    """path as field 1's (a Field1Path): as given when it is one already (main's <folder>/pdfs, walked from
    there, a link in it refused as a link), else as private_path accepts it."""
    return path if isinstance(path, Field1Path) else private_path(1, path)


class Field1Path(Path):
    """A path of field 1's: made from a folder private_path accepted (accepted), so each walk from it knows it
    serves field 1 (_open_dir): it checks the folders above accepted again (a git init there later counts) and
    each folder it passes for .git. Paths made from it (folder / "labels") are field 1's too. Field 2's paths
    are plain: field 2 is public, and its folders may sit in a repository. The commands, the page, check and
    passages make field 1's folders so (field1); a library caller handing the walk helpers (read_bytes,
    write_json, pdf_bytes) a plain path of field 1's gets field 2's walk, without these checks."""
    accepted = None

    def with_segments(self, *segments):
        made = type(self)(*segments)
        made.accepted = self.accepted
        return made


def work_tree(path):
    """The top of the git work tree holding path (the nearest folder above it with a .git folder, or the
    .git file a worktree has), or None. The path need not exist yet.
    ponytail: by the .git entry, as git finds it by default; GIT_DIR and GIT_WORK_TREE settings are not read."""
    for folder in (path, *path.parents):
        if (folder / ".git").exists():
            return folder
    return None


def outputs(folder):
    return OUT / "field2" if folder == FIELD2 else folder / "out"


def out_root(folder):
    """The folder the output folder is walked from: evals/out/ for field 2, the field's own folder for field 1."""
    return OUT if folder == FIELD2 else folder


# Files under a folder: no link is followed (G12). Every file S1-36 opens under a field's folder, an output
# folder or the PDFs' folder is reached from that folder (its root) one component at a time, each folder
# opened O_NOFOLLOW | O_DIRECTORY relative to the one before and the file O_NOFOLLOW relative to its folder:
# a link at or below the root refuses the command. Writes go to a part file in the destination's own folder,
# renamed onto the name itself. The one exception is a PDF entry, which may be a link (to Zotero's storage,
# say): it is read only, and only where it leads passes private_path.


def _link(path):
    raise Refused(f"{path} is a link: no link is followed under a gold set's folders")


def _step(fd, name, shown):
    """The folder name in the folder fd (None: name is the root's absolute path), opened without following a link."""
    try:
        return os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
    except OSError:
        if stat.S_ISLNK(os.stat(name, dir_fd=fd, follow_symlinks=False).st_mode):
            _link(shown)
        raise


def _not_a_work_tree(fd, shown):
    """A folder at or below field 1's root holding a .git entry (a folder, or the file a worktree or submodule
    has) is a git work tree: nothing in it is read or written (Harold, 2026-10-21: field 1 outside every git work
    tree; private_path checks a root and what is above it, this each folder below)."""
    try:
        os.stat(".git", dir_fd=fd, follow_symlinks=False)
    except FileNotFoundError:
        return
    raise Refused(f"{shown} is a git work tree (it holds .git): no gold-set file is read or written in one")


def _open_dir(root, folder, create=False):
    """An open descriptor of folder, reached from root one folder at a time; folders made 0700 when create.
    For field 1 (a Field1Path root), none of them a git work tree, nor any folder above the accepted one."""
    field1 = isinstance(root, Field1Path)
    if field1:
        private_path(1, root.accepted)  # the folders above field 1's, again: one may have become a git work tree since
    root, folder = Path(root), Path(folder)
    parts = folder.relative_to(root).parts
    if create:
        for made in reversed([p for p in (root, *root.parents) if not p.exists()]):
            made.mkdir(mode=0o700)
    fd = _step(None, str(root), root)
    try:
        if field1:
            _not_a_work_tree(fd, root)
        for n, name in enumerate(parts, start=1):
            if create:
                with contextlib.suppress(FileExistsError):
                    os.mkdir(name, 0o700, dir_fd=fd)
            fd, before = _step(fd, name, root.joinpath(*parts[:n])), fd
            os.close(before)
            if field1:
                _not_a_work_tree(fd, root.joinpath(*parts[:n]))
    except BaseException:
        os.close(fd)
        raise
    return fd


@contextlib.contextmanager
def _folder_of(root, path, create=False):
    fd = _open_dir(root, Path(path).parent, create)
    try:
        yield fd
    finally:
        os.close(fd)


def _open(fd, path, flags):
    """The file path names in the folder fd, never through a link, and only a regular file (a FIFO or device is
    refused, without waiting on it)."""
    try:
        f = os.open(Path(path).name, flags | os.O_NOFOLLOW | os.O_NONBLOCK, 0o600, dir_fd=fd)
    except OSError as error:
        if error.errno == errno.ELOOP:
            _link(path)
        raise
    if not stat.S_ISREG(os.fstat(f).st_mode):
        os.close(f)
        raise Refused(f"{path} is not a plain file")
    return f


def entry(root, path):
    """The entry at path (its lstat), or None when it or its folder is not there; a link is refused."""
    try:
        with _folder_of(root, path) as fd:
            found = os.stat(Path(path).name, dir_fd=fd, follow_symlinks=False)
    except FileNotFoundError:
        return None
    if stat.S_ISLNK(found.st_mode):
        _link(path)
    return found


def names(root, folder):
    """The names in folder (none when it is not there)."""
    try:
        fd = _open_dir(root, folder)
    except FileNotFoundError:
        return []
    try:
        return sorted(os.listdir(fd))
    finally:
        os.close(fd)


def read_bytes(root, path):
    with _folder_of(root, path) as fd:
        f = _open(fd, path, os.O_RDONLY)
    with os.fdopen(f, "rb") as file:
        return file.read()


def read_json(root, path):
    return json.loads(read_bytes(root, path).decode("utf-8"))


def sha256_of(root, path):
    return hashlib.sha256(read_bytes(root, path)).hexdigest()


def write_json(root, path, data):
    """Written whole, as tier1.write_atomic writes (a part file in the file's own folder, synced, then renamed
    onto the name itself), and owner-only from its creation: the part file is made 0600, every folder made
    here 0700. A link at the name, at its part file or at any folder from root down refuses it."""
    path = Path(path)
    part = path.with_name(path.name + ".part")
    with _folder_of(root, path, create=True) as fd:
        entry(root, path)  # a link at the name itself or at its part file is refused, never written through
        entry(root, part)
        with contextlib.suppress(FileNotFoundError):  # a part file left by a cut-off save, or a hard link there:
            os.unlink(part.name, dir_fd=fd)  # removed by name, never truncated through
        f = _open(fd, part, os.O_WRONLY | os.O_CREAT | os.O_EXCL)
        with os.fdopen(f, "w", encoding="utf-8") as out:
            os.fchmod(out.fileno(), 0o600)  # owner-only whatever the umask
            out.write(json.dumps(data, ensure_ascii=False, indent=1) + "\n")
            out.flush()
            os.fsync(out.fileno())
        os.replace(part.name, path.name, src_dir_fd=fd, dst_dir_fd=fd)


def pdf_bytes(field, pdfs, name):
    """A PDF's bytes from the PDFs' folder (the root, never through a link), read only. The PDF entry itself
    may be a link: it is followed only when where it leads passes private_path."""
    path = Path(pdfs) / name
    with _folder_of(pdfs, path) as fd:
        if stat.S_ISLNK(os.stat(name, dir_fd=fd, follow_symlinks=False).st_mode):
            f = os.open(private_path(field, path), os.O_RDONLY | os.O_NONBLOCK)
            if not stat.S_ISREG(os.fstat(f).st_mode):
                os.close(f)
                raise Refused(f"{path} does not lead to a plain file")
        else:
            f = _open(fd, path, os.O_RDONLY)
    with os.fdopen(f, "rb") as file:
        return file.read()


def passage_key(paper, ordinal, text):
    """A short digest of a passage's paper, ordinal and letters and digits: the page sends it back with each
    passage it picks, so a choice that pairs one paper with another paper's passage is refused, never saved."""
    return hashlib.sha256(f"{paper}\0{ordinal}\0{_key(text)}".encode()).hexdigest()[:16]


def item_sha256(item):
    return hashlib.sha256(json.dumps(item, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def load_settings(path=SETTINGS):
    return read_json(Path(path).parent, path)


# Formats


def _keys(where, obj, required, optional=()):
    if not isinstance(obj, dict):
        return [f"{where}: not an object"]
    problems = [f"{where}: {k} missing" for k in required if k not in obj]
    return problems + [f"{where}: {k} is not a field of this format" for k in obj if k not in (*required, *optional)]


def plain_id(value):
    """An id as every reader keys it: text of the allowed shape, never a number or anything else str() would
    turn into one (G11: a pair with id 123 kept its number, so its label, keyed "123", never applied again)."""
    return isinstance(value, str) and ID.fullmatch(value) is not None


def same(value, expected):
    """Equal and of the same JSON type: true is not 1, 2.0 is not 2, 123 is not "123"."""
    return type(value) is type(expected) and value == expected


def whole(value, least):
    """A whole number of at least least (true and false are not numbers here)."""
    return isinstance(value, int) and not isinstance(value, bool) and value >= least


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
    if not same(doc["field"], field):
        return [f"papers.json: field {doc['field']!r}, expected {field}"]
    problems += _keys("papers.json extractor", doc["extractor"], ("name", "version"))
    if field == 2 and not (isinstance(doc["criteria"], dict) and whole(doc["seed"], 0)):
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
        pid = paper["id"]
        if not (plain_id(pid) and (field == 1 or ARXIV.fullmatch(pid))):
            problems.append(f"{where}: " + ("id is not a plain name" if field == 1
                                            else "not an arXiv id with its version (2601.01234v2)"))
            continue
        if pid in seen:
            problems.append(f"{where}: listed twice")
        seen.add(pid)
        problems += _text(f"{where} title", paper["title"])
        if not (whole(paper["size"], 0) and isinstance(paper["sha256"], str) and len(paper["sha256"]) == 64):
            problems.append(f"{where}: size or SHA-256 missing")
        if not (isinstance(paper["file"], str) and paper["file"] == Path(paper["file"]).name
                and paper["file"] not in ("", ".", "..")):
            problems.append(f"{where}: file is not a file name in the PDFs' folder")
        if paper["document_language"] not in LANGUAGES:
            problems.append(f"{where}: document_language {paper['document_language']!r}")
        if field == 1:
            continue
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
        if not (is_date(paper["submitted"]) and window["from"] <= paper["submitted"] <= window["to"]):
            problems.append(f"{where}: submitted {paper['submitted']!r} is outside {window['from']} to {window['to']}")
        if paper["license"] not in LICENSES:
            problems.append(f"{where}: license {paper['license']!r} is neither CC BY 4.0 nor CC0 1.0")
        if paper["abstract_url"] != f"https://arxiv.org/abs/{pid}" or paper["license_page"] != paper["abstract_url"]:
            problems.append(f"{where}: the license must be read from that version's own abstract page")
        if paper["pdf_url"] != f"https://arxiv.org/pdf/{pid}":
            problems.append(f"{where}: pdf_url is not that version's")
        if not is_date(paper["license_checked"]):
            problems.append(f"{where}: license_checked is not a date")
    if not isinstance(doc["papers"], list):
        problems.append("papers.json: papers is not a list")
    elif not doc["papers"]:  # else every set check would be skipped (they run once the paper list passes)
        problems.append("papers.json: papers is empty")
    return problems


def is_date(value):
    """A calendar date written YYYY-MM-DD (2026-02-31 is not one)."""
    if not (isinstance(value, str) and DATE.fullmatch(value)):
        return False
    try:
        date.fromisoformat(value)
    except ValueError:
        return False
    return True


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
    if not whole(loc["ordinal"], 0):
        problems.append(f"{where}: ordinal is not a passage's")
    if not (loc["page"] is None or whole(loc["page"], 1)):
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
        valid = True
        for loc in ev:
            found = check_locator(f"{where} evidence item {n}", loc, field, papers)
            problems += found
            valid = valid and not found
            if not found and papers[loc["paper"]]["document_language"] != item["document_language"]:
                problems.append(f"{where}: document_language is not its evidence's papers'")
        if not valid:
            continue
        ordinals = sorted(loc["ordinal"] for loc in ev)  # T1: tier 2 finds an item by any one of its passages
        if len({loc["paper"] for loc in ev}) > 1 or ordinals != list(range(ordinals[0], ordinals[0] + len(ev))):
            problems.append(f"{where} evidence item {n}: not one passage or the consecutive passages of one paper")
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
    if not (same(doc["kind"], kind) and same(doc["field"], field) and plain_id(doc["id"])):
        return [f"{kind}.json: kind {doc['kind']!r}, field {doc['field']!r} or id {doc['id']!r} is not this set's"]
    if doc["provisional"] is not True:
        problems.append(f"{kind}.json: drafts are provisional until a rater's labels make them gold")
    if not isinstance(doc["items"], list):
        return problems + [f"{kind}.json: items is not a list"]
    check = check_question if kind == "questions" else check_pair
    ids, texts = Counter(), defaultdict(list)
    for item in doc["items"]:
        problems += check(item, field, papers)
        if isinstance(item, dict):
            if not plain_id(item.get("id")):
                problems.append(f"{kind} item {item.get('id')!r}: id is not a plain name")
                continue
            ids[item["id"]] += 1
            texts[_key(str(item.get("text") or item.get("sentence") or ""))].append(str(item.get("id")))
    problems += [f"{kind}: id {i!r} is used {n} times" for i, n in ids.items() if n > 1]
    problems += [f"{kind}: {', '.join(same)} have the same text" for t, same in texts.items() if t and len(same) > 1]
    return problems


CONTENT = ("text", "short_answer", "answerable", "evidence")  # a question's whole content, as an edit stores it


def check_labels(doc, set_doc, papers, rater=None, sample=None):
    """A labels file's problems (L1, L3, L7): its header names this set and rater; a sample's labels (marked
    sample: true, as the sample's page writes them) are for the recorded sample's items (sample: their ids)
    only; each label that applies to its item as it now is (applies) has the right fields and types (a
    question's edit holds the whole question, its locators checked as a draft's); and the set as the labels
    leave it (effective_items) passes every rule the drafts pass, set-wide ones (repeated ids or text)
    included. Labels that do not apply are history and are not checked. The page's saves, `check`, `apply`
    and `agreement` all use this one check."""
    problems = _keys("labels", doc, ("set", "kind", "field", "rater", "set_sha256", "labels"), ("sample",))
    if problems:
        return problems
    if not all(same(doc[k], set_doc[w]) for k, w in (("set", "id"), ("kind", "kind"), ("field", "field"))):
        return [f"labels: they are set {doc['set']!r}'s ({doc['kind']}, field {doc['field']!r}), not this set's"]
    if not (isinstance(doc["rater"], str) and RATER.fullmatch(doc["rater"])):
        return [f"labels: rater {doc['rater']!r} is not a rater's name"]
    if rater is not None and doc["rater"] != rater:
        return [f"labels: they are rater {doc['rater']!r}'s, not {rater!r}'s"]
    if not isinstance(doc["labels"], dict):
        return ["labels: labels is not an object"]
    items = {i["id"]: i for i in set_doc["items"]}
    if "sample" in doc:
        if doc["sample"] is not True:
            return ["labels: sample is not true"]
        if sample is None:
            return ["labels: they are the sample's, and no sample of this set is recorded"]
        # only labels that apply: history (its item changed or removed since) is never checked against the set (G8)
        if outside := sorted(i for i, label in doc["labels"].items() if applies(items.get(i), label) and i not in sample):
            return [f"labels: they are the sample's, and {', '.join(outside)} are not in the recorded sample"]
    for item, label in doc["labels"].items():
        if not applies(items.get(item), label):
            continue  # history (changed or removed since): kept as it is, never validated against the current set
        try:
            problems += label_problems(item, label, set_doc["kind"], set_doc["field"], papers)
        except Exception as error:  # noqa: BLE001 a hand-edited file of any shape is refused, never a crash
            problems.append(f"label {item}: malformed ({type(error).__name__})")
    return problems or set_problems(set_doc, doc["labels"], papers)


def label_problems(item_id, label, kind, field, papers):
    """One label's fields and types. A pair's label is its category. A question's is keep or drop, holding
    no content, or an edit holding the whole question as the rater left it (text, answerable, evidence, and a
    short answer when answerable), each of its locators checked as a draft's."""
    where = f"label {item_id}"
    if kind == "pairs":
        found = _keys(where, label, ("item_sha256", "revision", "saved_at", "category"))
        if found:
            return found
        found += [] if label["category"] in CATEGORIES else [f"{where}: category {label['category']!r}"]
        return found + ([] if is_count(label["revision"])
                        else [f"{where}: revision is not a count"])
    found = _keys(where, label, ("item_sha256", "revision", "saved_at", "decision"), CONTENT)
    if found:
        return found
    if not is_count(label["revision"]):
        found.append(f"{where}: revision is not a count")
    if label["decision"] not in DECISIONS:
        return found + [f"{where}: decision {label['decision']!r}"]
    if label["decision"] != "edit":
        return found + ([f"{where}: keep and drop hold no content"] if any(k in label for k in CONTENT) else [])
    if missing := [k for k in ("text", "answerable", "evidence") if k not in label]:
        return found + [f"{where}: an edit holds the whole question; {', '.join(missing)} missing"]
    found += [f"{where}: {name} is not text" for name in ("text", "short_answer")
              if name in label and not isinstance(label[name], str)]
    if not isinstance(label["answerable"], bool):
        found.append(f"{where}: answerable is not true or false")
    if not (isinstance(label["evidence"], list) and all(isinstance(ev, list) and ev for ev in label["evidence"])):
        return found + [f"{where}: evidence is not a list of evidence items"]
    return found + [p for ev in label["evidence"] for loc in ev for p in check_locator(f"{where} evidence", loc, field, papers)]


def is_count(value):
    return whole(value, 1)


def effective_items(set_doc, labels):
    """The set's items as the labels leave them: an item whose label is done for it as that label leaves it
    (a dropped one out), every other item as drafted (a label for an item changed since is never applied)."""
    out = []
    for item in set_doc["items"]:
        label = labels.get(item["id"])
        if label_state(item, label) != "done":
            out.append(item)
        elif (made := labeled(item, label)) is not None:
            out.append({k: v for k, v in made.items() if k != "label"})
    return out


def set_problems(set_doc, labels, papers):
    """The rules the drafts pass, applied to the set as the labels leave it: every item, and the set-wide
    rules (no repeated id or text)."""
    try:
        effective = {**set_doc, "provisional": True, "items": effective_items(set_doc, labels)}
        return [f"as labeled, {p}" for p in check_set(effective, set_doc["kind"], set_doc["field"], papers)]
    except Exception as error:  # noqa: BLE001 never a crash on a malformed file
        return [f"as labeled: malformed ({type(error).__name__})"]


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


def verify_pdfs(papers, pdfs, field):
    """Each listed PDF checked against its size and SHA-256 before anything is read (never downloaded)."""
    for paper in papers:
        try:
            data = pdf_bytes(field, pdfs, paper["file"])
        except FileNotFoundError:
            raise Refused(f"paper {paper['id']}: {paper['file']} is missing under {pdfs}; prepare it first "
                          "(the runner never downloads)") from None
        if len(data) != paper["size"]:
            raise Refused(f"paper {paper['id']}: {paper['file']}: {len(data)} bytes, expected {paper['size']}")
        if (digest := hashlib.sha256(data).hexdigest()) != paper["sha256"]:
            raise Refused(f"paper {paper['id']}: {paper['file']}: SHA-256 {digest}, expected {paper['sha256']}")


async def read_papers(client, state, papers, pdfs, field):
    """{paper id: {state, reason, extractor, passages}}: each PDF as the app reads it, in one review-locked
    project, one file a request (a PDF is large); passages in reading order with the app's ids. A paper
    not read whole (a failed reading, pages waiting for OCR) has its state and reason and no passages."""
    from backend.search_index import readings
    project = await tier1.new_project(client, "Gold set papers")
    materials = []
    for paper in papers:
        pdf = pdf_bytes(field, pdfs, paper["file"])
        materials += await tier1.add_files(client, project, [(f"{paper['id']}.pdf", pdf)])
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
    """Every paper id in QASPER v0.3's three splits, each file checked against its pin first (read once, from
    the folder as every file is: no link followed)."""
    ids = set()
    for pin in settings["qasper"]["files"]:
        path = Path(folder) / pin["path"]
        try:
            data = read_bytes(folder, path)
        except FileNotFoundError:
            raise Refused(f"QASPER: {pin['path']} is missing under {folder}") from None
        if len(data) != pin["size"]:
            raise Refused(f"QASPER: {path.name}: {len(data)} bytes, expected {pin['size']}")
        if (digest := hashlib.sha256(data).hexdigest()) != pin["sha256"]:
            raise Refused(f"QASPER: {path.name}: SHA-256 {digest}, expected {pin['sha256']}")
        ids |= set(json.loads(data.decode("utf-8")))
    return ids


# Commands: check and passages


def load_field(field, folder):
    """(papers.json, its SHA-256, {kind: set}) of a field's folder; a set not drafted yet is absent."""
    if entry(folder, folder / "papers.json") is None:
        raise Refused(f"{folder / 'papers.json'} is missing: nothing is drafted yet")
    sets = {kind: read_json(folder, folder / f"{kind}.json") for kind in SETS if entry(folder, folder / f"{kind}.json")}
    return read_json(folder, folder / "papers.json"), sha256_of(folder, folder / "papers.json"), sets


@contextlib.contextmanager
def scratch(field, folder):
    """The temporary folder a command's app runs in, removed after: field 2's under TMPDIR; field 1's inside
    field 1's own folder, outside every git work tree as private_path holds it (Harold, 2026-10-21), with
    TMPDIR pointed into it meanwhile, so the app's reading child and any library's scratch stay there too."""
    if field != 1:
        with tempfile.TemporaryDirectory(prefix="scholia-gold-") as tmp:
            yield Path(tmp)
        return
    with tempfile.TemporaryDirectory(prefix=".tmp-scholia-gold-", dir=folder) as tmp:
        inner = Path(tmp) / "tmp"
        inner.mkdir(mode=0o700)
        saved = os.environ.get("TMPDIR"), tempfile.tempdir
        os.environ["TMPDIR"] = tempfile.tempdir = str(inner)
        try:
            yield Path(tmp)
        finally:
            if saved[0] is None:
                os.environ.pop("TMPDIR", None)
            else:
                os.environ["TMPDIR"] = saved[0]
            tempfile.tempdir = saved[1]


async def write_when_done(root, out, make):
    """The file at out says incomplete from the start, and holds the result only once it is all made (an
    interruption, refusal or failure leaves the incomplete marker with its reason)."""
    write_json(root, out, {"status": "incomplete", "reason": "running"})
    try:
        result = await make()
    except BaseException as error:
        write_json(root, out, {"status": "incomplete", "reason": f"{type(error).__name__}: {str(error)[:300]}"})
        raise
    write_json(root, out, result)
    return result


async def check(field, folder, pdfs=None, qasper=None, open_app=None, settings=None):
    """The field's report: the paper list, each set and labels file checked; with pdfs, every locator
    resolved on the app's reading; with qasper, field 2's papers checked against QASPER's. Written to the
    output folder's check.json; `status: finished` and `problems` empty when everything holds."""
    settings = settings or load_settings()
    open_app = open_app or reading_app
    if field == 1:  # field 1's walks, however called: from folders private_path accepted (Field1Path)
        folder, pdfs = field1(folder), pdfs and field1(pdfs)

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
            report["sets"][kind] = {"id": set_doc["id"], "sha256": sha256_of(folder, folder / f"{kind}.json"),
                                    **mix(kind, set_doc["items"])}
            report["differences"] += [f"{kind}: {d}" for d in differences(kind, report["sets"][kind], settings)]
            items = {i["id"]: i for i in set_doc["items"]}
            sample = None  # read first: a sample's labels are checked against it
            if entry(folder, folder / f"sample-{kind}.json"):
                drawn = read_json(folder, folder / f"sample-{kind}.json")
                if not sample_of(drawn, set_doc, settings):
                    problems.append(f"sample-{kind}.json: not a sample of this set")
                else:
                    sample = drawn["items"]
                    report["samples"][kind] = len(sample)
            for path in [folder / "labels" / n for n in names(folder, folder / "labels")
                         if fnmatch.fnmatchcase(n, f"{set_doc['id']}.*.json")]:
                labels = read_json(folder, path)
                rater = path.name[len(set_doc["id"]) + 1:-len(".json")]  # <set id>.<rater>.json: its header names it
                found = check_labels(labels, set_doc, papers, rater, sample)
                problems += [f"{path.name}: {p}" for p in found]
                if not found:
                    report["labels"][path.name] = label_counts(items, labels)
                    edited[kind] += [(path.name, made) for i, label in labels["labels"].items()
                                     if i in items and label_state(items[i], label) == "done" and "evidence" in label
                                     and (made := labeled(items[i], label)) is not None]
        if pdfs is not None and not problems:
            verify_pdfs(doc["papers"], pdfs, field)
            with scratch(field, folder) as tmp:
                async with open_app(tmp / "data") as (client, state):
                    read, _ = await read_papers(client, state, doc["papers"], pdfs, field)
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
    return await write_when_done(out_root(folder), outputs(folder) / "check.json", make)


async def passages(field, folder, pdfs, open_app=None, settings=None):
    """The passage worksheet (D1): every listed paper read by the app, written to the output folder."""
    settings = settings or load_settings()
    open_app = open_app or reading_app
    if field == 1:
        folder, pdfs = field1(folder), field1(pdfs)

    async def make():
        doc, papers_sha256, _ = load_field(field, folder)
        if problems := check_papers(doc, field, settings):
            raise Refused("; ".join(problems[:5]))
        verify_pdfs(doc["papers"], pdfs, field)
        with scratch(field, folder) as tmp:
            async with open_app(tmp / "data") as (client, state):
                read, _ = await read_papers(client, state, doc["papers"], pdfs, field)
        return worksheet(field, doc, papers_sha256, read)
    return await write_when_done(out_root(folder), outputs(folder) / "worksheet.json", make)


# Labels, the corrected set, the sample and the agreement


def applies(item, label):
    """Whether a label applies to the item as it now is (its content hash is the item's): the one test that
    every reader (the page, check, apply, agreement) uses. Only a label that applies is validated (as a whole
    item, with the set and the papers as they now are) and applied. Any other label (for an item changed or
    removed since, or not even well formed) is history: kept untouched in the file, never validated against
    the current set, never applied, and its item, if still there, is shown again for labeling."""
    return item is not None and isinstance(label, dict) and label.get("item_sha256") == item_sha256(item)


def label_state(item, label):
    """None (not labeled), "done" (the label applies) or "changed" (history: shown again, never applied)."""
    if label is None:
        return None
    return "done" if applies(item, label) else "changed"


def revision_of(label):
    """A label's revision as saving over it must name: 0 for history that has none (a save then replaces it)."""
    return label["revision"] if isinstance(label, dict) and is_count(label.get("revision")) else 0


def label_counts(items, labels):
    states = Counter(label_state(items[i], l) if i in items else "orphaned" for i, l in labels["labels"].items())
    return {"rater": labels["rater"], "labeled": states["done"], "changed": states["changed"],
            "orphaned": states["orphaned"], "unlabeled": len(items) - states["done"] - states["changed"]}


def labeled(item, label):
    """The item as a rater's label leaves it: None when dropped; a question's edit replaces the question's
    whole content with the label's (nothing is taken from the draft)."""
    if "category" in label:
        return {**item, "label": label["category"]}
    if label["decision"] == "drop":
        return None
    if label["decision"] == "keep":
        return dict(item)
    return {**{k: v for k, v in item.items() if k not in CONTENT}, **{k: label[k] for k in CONTENT if k in label}}


def apply(set_doc, labels, papers, sample=None):
    """The corrected set (L5): each item a label is done for, as the label leaves it (dropped items out),
    `provisional: false`; every other item as drafted, `provisional: true`, never counted as gold; labels
    of items no longer in the set kept out and counted. Labels that do not pass check_labels are refused
    whole: nothing invalid becomes gold. sample: the recorded sample's ids, for a sample's labels."""
    if problems := check_labels(labels, set_doc, papers, sample=sample):
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


def sample_of(sample, set_doc, settings):
    """Whether a sample file is this set's sample: the very draw the settings make of it (H1), every field and
    type the same, so an emptied, cut or hand-picked sample is refused."""
    return isinstance(sample, dict) and item_sha256(sample) == item_sha256(draw(set_doc, settings))


def recorded_sample(folder, set_doc, settings):
    """The recorded sample's item ids (sample-<set>.json), or None when none is drawn; a file that is not
    this set's draw is refused."""
    path = folder / f"sample-{set_doc['kind']}.json"
    if entry(folder, path) is None:
        return None
    if not sample_of(sample := read_json(folder, path), set_doc, settings):
        raise Refused(f"{path.name}: not a sample of this set")
    return sample["items"]


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


def agreement(set_doc, first, second, sample):
    """The agreement of two raters' labels on the recorded sample's items (sample: their ids) both labeled
    (H2; spec section 14: the overlap sample), never on any other item either labeled. Pairs: percent agreement,
    Cohen's kappa over the three categories and the confusion table (rows the first rater's). Questions:
    the same for keep or drop, then over the items both kept, for answerable, and for evidence: both chose
    the same items, and the mean overlap of the passages they chose (shared over either's)."""
    both, sampled = [], set(sample)
    for item in (i for i in set_doc["items"] if i["id"] in sampled):
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
    it up (passage_of: {(paper, ordinal): the passage as read_papers gives it}), one annotator. Drafts are
    refused (held out), and so is a locator whose passage in this reading is not its quote (letters and digits
    compared, as resolve compares): after a reading moved it, its ordinal names another passage."""
    if corrected.get("corrected") is not True or corrected.get("kind") != "questions":
        raise Refused("tier 2 scores a corrected question set (gold.py apply), never the drafts")
    queries = []
    for item in corrected["items"]:
        if item["provisional"] or not item["answerable"]:  # provisional is not gold; unanswerable is the abstention row's
            continue
        for loc in (loc for ev in item["evidence"] for loc in ev):
            found = passage_of.get((loc["paper"], loc["ordinal"]))
            if found is None:
                raise Refused(f"question {item['id']}: passage {loc['ordinal']} of {loc['paper']} is not in this reading")
            if _key(found["text"]) != _key(loc["quote"]):
                raise Refused(f"question {item['id']}: passage {loc['ordinal']} of {loc['paper']} is no longer its quote "
                              "in this reading (check --pdfs reports where it moved)")
        queries.append({"key": item["id"], "text": item["text"], "group": len(queries), "project": project,
                        "query_language": item["query_language"], "document_language": item["document_language"],
                        "annotators": [[[passage_of[(loc["paper"], loc["ordinal"])]["id"] for loc in ev]
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
        if field == 1:
            folder = field1(folder)  # field 1's walks (Field1Path): checked again at each save (_open_dir)
        doc, papers_sha256, sets = load_field(field, folder)
        if kind not in sets:
            raise Refused(f"{kind}.json is not drafted yet")
        self.set = sets[kind]
        problems = check_papers(doc, field, settings)
        self.papers = {} if problems else {p["id"]: p for p in doc["papers"]}  # ids keyed only once they are checked
        if problems := problems or check_set(self.set, kind, field, self.papers):
            raise Refused("the set does not pass its check: " + "; ".join(problems[:5]))
        self.items = {item["id"]: item for item in self.set["items"]}
        order = list(self.items)
        if kind == "pairs" and settings["review"]["pair_order"] == "shuffled":
            random.Random(f"{settings['review']['order_seed']}:{self.set['id']}").shuffle(order)
        self.sample = None  # the recorded sample's ids, on the sample's page
        if sample:
            self.sample = recorded_sample(folder, self.set, settings)
            if self.sample is None:
                raise Refused(f"sample-{kind}.json is missing: draw it first (gold.py sample)")
            order = [i for i in order if i in set(self.sample)]
        self.order = order
        self.show_category = settings["review"]["show_drafted_category"]
        self.passages = {}
        if kind == "questions":
            path = outputs(folder) / "worksheet.json"
            if entry(out_root(folder), path) is None:
                raise Refused(f"{path} is missing: write the worksheet first (gold.py passages)")
            sheet = read_json(out_root(folder), path)
            try:
                if not (sheet["status"] == "finished" and same(sheet["field"], field)
                        and sheet["papers_sha256"] == papers_sha256):
                    raise ValueError
                self.passages = {pid: {p["ordinal"]: p for p in entry["passages"] if p["usable"]}
                                 for pid, entry in sheet["papers"].items()}
            except (LookupError, TypeError, AttributeError, ValueError):  # a hand-edited file: refused, not a crash
                raise Refused(f"{path} is not a finished worksheet of this paper list: write it again") from None
        self.shown_papers = set(self.papers)
        if self.sample is not None and field == 1:  # the colleague sees the sampled items' papers, no more
            self.shown_papers = {loc["paper"] for i in order for loc in locators_of_item(kind, self.items[i])}
            self.passages = {pid: found for pid, found in self.passages.items() if pid in self.shown_papers}
        self.root, self.path = folder, folder / "labels" / f"{self.set['id']}.{rater}.json"
        self.notices = []
        if entry(folder, self.path.with_name(self.path.name + ".part")):
            self.notices.append("cut_off")  # a save cut off before its rename: the labels file is as last saved
        self.labels = self.read_labels()
        self.set_sha256 = sha256_of(folder, folder / f"{kind}.json")

    def read_labels(self):
        """The labels file as it is now (another page on the same file may have saved since); a file that
        cannot be read, or is not this set's and rater's, is refused and left as it is."""
        if entry(self.root, self.path) is None:
            return {}
        try:
            labels = read_json(self.root, self.path)
        except (OSError, ValueError) as error:
            raise Refused(f"{self.path.name} cannot be read ({type(error).__name__}); it is left as it is") from None
        if isinstance(labels, dict) and ("sample" in labels) != (self.sample is not None):
            raise Refused(f"{self.path.name}: " + ("these are the sample's labels: open the sample's page (--sample)"
                                                   if self.sample is None else "these labels are not the sample's")
                          + "; it is left as it is")
        if problems := check_labels(labels, self.set, self.papers, self.rater, self.sample):
            raise Refused(f"{self.path.name}: " + "; ".join(problems[:5]) + "; it is left as it is")
        return labels["labels"]

    @contextlib.contextmanager
    def file_lock(self):
        """This page's lock, and the labels file's for every process: two pages on one file never lose a save.
        The lock file beside it is owner-only and ignored by git."""
        import fcntl
        with self.lock:
            with _folder_of(self.root, self.path, create=True) as folder:
                fd = _open(folder, self.path.with_name(f".{self.path.name}.lock"), os.O_WRONLY | os.O_CREAT)
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
        if not applies(self.items[item_id], label):  # history: the item is shown again; only its revision matters
            return {"state": "changed", "revision": revision_of(label)}
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
        if not (isinstance(choice, dict) and set(choice) == {"paper", "ordinal", "key"} and isinstance(choice["paper"], str)
                and isinstance(choice["ordinal"], int) and isinstance(choice["key"], str)):
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
        """The label a request gives, checked for its shape: (fields to store) or ValueError(code). A question
        is sent whole, as the page shows it (text, answerable, evidence, and a short answer when answerable),
        or dropped; it is stored as keep when it is the draft as drafted, else as an edit holding it whole.
        Whether it passes the drafts' rules is checked with the whole set (save)."""
        from backend.settings import visible
        if not isinstance(given, dict):
            raise ValueError("label_invalid")
        if self.kind == "pairs":
            if set(given) != {"category"} or given["category"] not in CATEGORIES:
                raise ValueError("label_invalid")
            return {"category": given["category"]}
        if given.get("decision") == "drop" and set(given) == {"decision"}:
            return {"decision": "drop"}
        if not (given.get("decision") == "keep"
                and {"decision", "text", "answerable", "evidence"} <= set(given) <= {"decision", *CONTENT}):
            raise ValueError("label_invalid")
        content = {}
        for name in ("text", "short_answer"):
            if name in given:
                text = visible(given[name]) if isinstance(given[name], str) else None
                if text is None:
                    raise ValueError("text_blank")
                if len(text) > TEXT_BOUND[name]:
                    raise ValueError("text_too_long")
                content[name] = text
        if not isinstance(given["answerable"], bool):
            raise ValueError("label_invalid")
        content["answerable"] = given["answerable"]
        chosen = given["evidence"]
        if not (isinstance(chosen, list) and len(chosen) <= 30
                and all(isinstance(ev, list) and 0 < len(ev) <= 10 for ev in chosen)):
            raise ValueError("evidence_invalid")
        content["evidence"] = [[self.locator(c) for c in ev] for ev in chosen]

        def plain(question):  # whole locators: a passage picked again with another quote, page, kind or section is an edit
            return {k: question[k] for k in ("text", "short_answer", "answerable", "evidence") if k in question}
        return {"decision": "keep"} if plain(content) == plain(item) else {"decision": "edit", **content}

    def save(self, body):
        """(status, answer) for a save: written to the labels file (whole, owner-only) before it is answered as
        saved. Refused, with nothing written: a request carrying another revision of the item's label than the
        file's (409, with the label as saved), and one that would leave the set breaking a rule the drafts
        pass (400, naming the rule)."""
        if not (isinstance(body, dict) and set(body) == {"item", "base", "label"} and body["item"] in self.order
                and isinstance(body["base"], int) and not isinstance(body["base"], bool)):
            return 400, {"code": "label_invalid"}
        item = self.items[body["item"]]
        with self.file_lock():
            self.labels = self.read_labels()
            current = self.labels.get(item["id"])
            if body["base"] != revision_of(current):
                return 409, {"code": "changed_elsewhere", "label": self.shown_label(item["id"]), "done": self.done()}
            try:
                label = self.label_of(item, body["label"])
            except ValueError as error:
                return 400, {"code": str(error)}
            entry = {**label, "item_sha256": item_sha256(item), "revision": body["base"] + 1,
                     "saved_at": time.strftime("%Y-%m-%dT%H:%M:%S%z")}
            labels = {**self.labels, item["id"]: entry}
            if problems := set_problems(self.set, labels, self.papers):
                return 400, {"code": rule_code(problems[0]), "problem": problems[0]}
            doc = {"set": self.set["id"], "kind": self.kind, "field": self.field, "rater": self.rater,
                   "set_sha256": self.set_sha256, **({"sample": True} if self.sample is not None else {}),
                   "labels": dict(sorted(labels.items()))}
            try:
                write_json(self.root, self.path, doc)
            except OSError:
                return 500, {"code": "not_saved"}
            self.labels = labels
            return 200, {"label": self.shown_label(item["id"]), "done": self.done()}


def rule_code(problem):
    """The page's code for the first rule a save would break (its catalog says it in the rater's language)."""
    for marker, code in (("have the same text", "duplicate_text"), ("marked", "text_language"),
                         ("an answerable question has", "answerable_needs_answer_and_evidence"),
                         ("an unanswerable question has no", "unanswerable_has_no_answer"),
                         ("longer than", "text_too_long"), ("empty or not text", "text_blank")):
        if marker in problem:
            return code
    return "evidence_invalid" if "evidence" in problem else "label_invalid"


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
            try:
                length = int(self.headers.get("Content-Length") or 0)
            except ValueError:
                return self._send(411, {"code": "length_required"})
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
            if args.set not in sets:
                raise Refused(f"{args.set}.json is not drafted yet")
            set_doc, settings = sets[args.set], load_settings()
            problems = check_papers(papers_doc, args.field, settings)
            papers = {} if problems else {p["id"]: p for p in papers_doc["papers"]}
            if problems := problems or check_set(set_doc, args.set, args.field, papers):
                raise Refused(f"{args.set}.json does not pass its check: " + "; ".join(problems[:5]))
            if args.command == "sample":
                out = folder / f"sample-{args.set}.json"
                if entry(folder, out):
                    raise Refused(f"{out.name} exists: a sample is drawn once, before anyone labels it")
                write_json(folder, out, draw(set_doc, settings))
                print(f"{out}: {len(read_json(folder, out)['items'])} items")
            else:
                if not all(RATER.fullmatch(r) for r in (args.raters if args.command == "agreement" else [args.rater])):
                    raise Refused("a rater is named with lower-case letters, digits and hyphens")

                def labels_of(rater):
                    path = folder / "labels" / f"{set_doc['id']}.{rater}.json"
                    labels = read_json(folder, path)
                    if problems := check_labels(labels, set_doc, papers, rater, sample_if(labels)):
                        raise Refused(f"{path.name}: " + "; ".join(problems[:5]))
                    return labels

                def sample_if(labels):  # the recorded sample, read only for a sample's labels
                    return recorded_sample(folder, set_doc, settings) if isinstance(labels, dict) and "sample" in labels else None
                if args.command == "agreement":
                    first, second = map(labels_of, args.raters)
                    if (sample := recorded_sample(folder, set_doc, settings)) is None:
                        raise Refused("agreement is measured on the recorded sample: draw it first (gold.py sample)")
                    print(json.dumps(agreement(set_doc, first, second, sample), ensure_ascii=False, indent=1))
                else:
                    out = outputs(folder) / f"{set_doc['id']}.corrected.{args.rater}.json"
                    labels = labels_of(args.rater)
                    corrected = apply(set_doc, labels, papers, sample_if(labels))
                    write_json(out_root(folder), out, corrected)
                    print(f"{out}: {corrected['counts']}")
    except (Refused, OSError, ValueError) as refused:
        sys.exit(f"refused: {refused}")
    except (KeyboardInterrupt, asyncio.CancelledError):
        sys.exit("stopped: the output file says incomplete")


if __name__ == "__main__":
    main()
