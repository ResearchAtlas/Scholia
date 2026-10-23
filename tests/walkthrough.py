"""Serves the app for interface work and rendered walkthroughs, never with real data.

    uv run python tests/walkthrough.py [--port N] [--dev] [--request-log FILE]

The backend runs on a new temporary data folder, with an in-memory credential store
(never the Keychain), a test-owned provider that answers with synthetic text, and test-owned
stand-ins for OpenAlex, Crossref and arXiv that answer made-up records, behind the test network
block (network_guard.py), so this process reaches nothing but its own listener. It serves the
built interface (frontend/dist) and prints the window's address with this launch's session, and
the folder of synthetic materials (a PDF, DOCX, HTML, Markdown and LaTeX file, and more) it wrote
for the walkthrough to add. Materials are read as the app reads them, each in a child process
(backend/reading.py), here tests/reading_stub.py's walkthrough child, allowed by its whole command:
scanned pages are read by this Mac's own OCR engine (Vision), whose first recognition of a page with
dark ink fails once, so the walkthrough shows a failed reading and its retry (S1-20); and two
synthetic files are stopped at the reading's ceilings, one taking memory without end and one
silent past the time ceiling (lowered here to STEP_SECONDS, printed as "step ceiling"), as crafted
files would be. The local model helper's search model is a synthetic file with a pin
of its own, served by a test-owned download source (each source redirecting to the file host
it uses, the file sent slowly so a download can be watched and cancelled), and written to an
"offline" folder for the import flow, whose path is printed. The helper binary is a test-owned
stand-in in a bundle with its manifest (S1-17): a small program, allowed by its path as the reading
child is by its whole command (the network block refuses every other child process), that opens no
socket and only prints the line a llama-server prints once it listens; its HTTP side is answered
here, behind the outbound gate's transport, with synthetic vectors (synthetic_materials.embedding).
While the file named by "helper control" exists, it answers embedding requests 503, as a helper that
stopped answering. It counts tokens as llama-server's tokenizer at its most spread out (one per byte),
refuses an input over its slot as llama-server does, and refuses one passage of "Unmapped glyphs".
--dev serves no interface and admits the Vite server on 127.0.0.1:5173 instead (`npm run dev` in
frontend/), with no session, on port 8765, where that server sends API requests. Without --port, a
free port is used. --request-log appends each request the test-owned provider receives to FILE, one
JSON line each.

S1-15: a test-owned Zotero (scholia_app.MockZotero) answers at Zotero's address behind the same transport,
with made-up libraries, collections and items, whose PDFs are synthetic files in the materials folder.
It reads its state from the file named by "zotero state": not_running, api_off, ready, changed (ready,
with one item's title and PDF changed since, for the re-import), or pruned (ready, with the group "Synthetic
Lab" and the collection "Linked files" gone since). Its requests go to the request log too,
with their query ("target").

S1-19b: the provider's synthetic replies know each call's role: a title, the support judge (its pairs judged
supported, partly and unsupported in turn), the small router (literature), an answer citing the evidence
its request carries ([S1.1] and on; the earlier flows' own walkthrough question keeps its formatted
answer), and an abstention when the request says the library matched nothing.
One model (NO_TOOLS) lists no endpoint that takes tools, for chat with the library. The request log
records each call's role.

S1-21a: the provider's memory extractions propose a decision for a message that considers a survey (and nothing for any
other); the agent writes a next step with update_progress when asked to note one. Where we left off reads the clock as
this launch's own, moved forward by the number of seconds in the file named by "memory clock".

tests/walkthrough_driver.mjs drives the rendered walkthrough against this server.
"""

import argparse
import asyncio
import hashlib
import json
import re
import secrets
import socket
import sys
import tempfile
from datetime import timedelta
from pathlib import Path

sys.path[:0] = [str(Path(__file__).parent), str(Path(__file__).parents[1])]
import network_guard  # noqa: E402
import synthetic_materials  # noqa: E402

# pycryptodomex learns the CPU architecture once, through platform.architecture(), which runs
# the local `file` command; the network block refuses every subprocess, so encrypted backups
# and exports (pyzipper) would fail under it. Loading it first runs that one local command
# before the block starts.
from Cryptodome.Hash import SHA1  # noqa: E402, F401

network_guard.start()

import httpx  # noqa: E402
import uvicorn  # noqa: E402
from scholia_app import FakeKeyring, MockProvider, MockScholarly, MockZotero, crossref_work, openalex_work, streaming  # noqa: E402

from backend import local_helper, memory, reading, router  # noqa: E402
from backend.app import create_app  # noqa: E402
from backend.budget_router import MODEL_TIERS  # noqa: E402
from backend.citations import JUDGE_RULES  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
ANSWER = """A cohort study follows a group of people over time to see who develops an outcome.

1. **Prospective** cohorts enrol people now and follow them forward.
2. **Retrospective** cohorts use records that already exist.

> Exposure is measured before the outcome occurs.

A figure from the source would appear as a link: ![Survival curve](https://example.org/figure.png)

队列研究的关键在于暴露先于结局被测量。"""


# A synthetic model listing: the router's preferred models, with made-up windows and prices,
# and two others (one with no reported window).
CATALOG = [{"id": model, "name": model.split("/", 1)[1].replace("-", " ").title(), "context_length": 131072 * (i % 3 + 1),
            "pricing": {"prompt": f"{0.0000002 * (i + 1):.10f}", "completion": f"{0.0000008 * (i + 1):.10f}"},
            "supported_parameters": ["tools", "reasoning"]}
           for i, model in enumerate(dict.fromkeys(m for tier in MODEL_TIERS.values() for m in tier))]
CATALOG += [{"id": "example/long-context-mini", "name": "Long Context Mini", "context_length": 1000000,
             "pricing": {"prompt": "0.0000001", "completion": "0.0000004"}},
            {"id": "example/unlisted-window", "name": "Unlisted Window", "pricing": {"prompt": "0", "completion": "0"}}]


# Made-up identifiers (10.5555 is a test prefix) and the records the stand-ins answer for them.
DOIS = {"pdf": "10.5555/scholia.walkthrough.wages", "docx": "10.5555/scholia.walkthrough.cities",
        "latex": "10.5555/scholia.walkthrough.floors", "local": "10.5555/scholia.walkthrough.codebook",
        "scan": "10.5555/scholia.walkthrough.scan"}
ARXIV_ID = "2401.00001"
RECORDS = MockScholarly(
    openalex={DOIS["pdf"]: openalex_work(DOIS["pdf"], "Minimum Wages and Employment in a Synthetic Panel",
                                         authors=("Ana Example", "Bo Sample"), year=2024),
              DOIS["latex"]: openalex_work(DOIS["latex"], "最低工资的合成模型：一项方法说明",
                                           authors=("Chen Example",), year=2023, venue="合成经济研究"),
              DOIS["local"]: openalex_work(DOIS["local"], "A Codebook for Synthetic Interviews", year=2022),
              DOIS["scan"]: openalex_work(DOIS["scan"], "A Scanned Letter on Synthetic Wages",
                                          authors=("Dana Example",), year=1998),
              # S1-15: the Zotero item's DOI, completing its record
              "10.5555/scholia.walkthrough.zotero": openalex_work("10.5555/scholia.walkthrough.zotero",
                                                                  "An OpenAlex Record for a Zotero Item",
                                                                  venue="Journal of Synthetic Towns"),
              # S1-14: a reference file's record whose work is retracted, and a NoteExpress record's
              synthetic_materials.REF_DOI: openalex_work(synthetic_materials.REF_DOI, "An OpenAlex Record",
                                                         retracted=True),
              "10.5555/scholia.noteexpress.001": openalex_work("10.5555/scholia.noteexpress.001", "A NoteExpress Record",
                                                                venue="合成经济研究")},
    crossref={DOIS["docx"]: crossref_work(DOIS["docx"], "Wages Across Synthetic Cities", retracted=True),
              # S1-14: the reference file's Chinese record, completed from Crossref
              synthetic_materials.REF_DOI_ZH: crossref_work(synthetic_materials.REF_DOI_ZH, "A Crossref Record")},
    arxiv={ARXIV_ID: "Labour Market Notes on a Synthetic Economy"})


def long_notes(paragraphs=3000):
    """A long synthetic Markdown paper: its title, then numbered paragraphs, a section every 150 of them."""
    parts = ["# Long Synthetic Notes\n"]
    for i in range(1, paragraphs + 1):
        if i % 150 == 1:
            parts.append(f"## Part {i // 150 + 1}\n")
        parts.append(f"Paragraph {i} of the long synthetic notes, written for the walkthrough.\n")
    return "\n".join(parts).encode()


def write_materials():
    """The synthetic files the walkthrough adds, written to a new temporary folder, which is returned."""
    folder = Path(tempfile.mkdtemp(prefix="scholia-walkthrough-materials-"))
    files = {
        "minimum-wages.pdf": synthetic_materials.paper_pdf(doi=DOIS["pdf"]),
        "synthetic-cities.docx": synthetic_materials.paper_docx(doi=DOIS["docx"]),
        "wage-floors.tex": synthetic_materials.paper_latex(doi=DOIS["latex"]),
        "labour-notes.md": synthetic_materials.paper_markdown(arxiv=ARXIV_ID),
        "city-report.html": synthetic_materials.paper_html(title="City Wage Report (synthetic)", doi=""),
        "scanned-appendix.pdf": synthetic_materials.paper_pdf(title="Scanned Appendix", doi=None, scanned=2),
        "damaged.pdf": b"%PDF-1.7\n" + b"\x00 not a whole PDF " * 40,
        "interview-codebook.md": f"# Interview Codebook\n\ndoi:{DOIS['local']}\n\nSynthetic codes only.\n".encode(),
        # S1-14: a reference file (three new records, a duplicate of the scanned letter by its DOI and one
        # of the Markdown notes by title and year), a CNKI-style NoteExpress export, and a file to attach.
        "reference-library.ris": synthetic_materials.references_ris() + (
            f"\nTY  - JOUR\nTI  - A Scanned Letter on Synthetic Wages\nDO  - {DOIS['scan']}\n"
            "AB  - A scanned letter on synthetic wages, read from a reference file's record.\nKW  - wages\nER  - \n"
            "\nTY  - JOUR\nTI  - Labour Market Notes on a Synthetic Economy\nPY  - 2024\n"
            "AB  - Notes on a synthetic labour market, from a reference file.\nER  - \n").encode(),
        "cnki-export.txt": synthetic_materials.references_noteexpress(),
        "chapter-full-text.md": b"# A Chapter without an Identifier\n\nThe full text of the synthetic chapter, attached later.\n",
        "long-notes.md": long_notes(),
        "crowded-page.pdf": synthetic_materials.pdf(  # one page of 220 passages, a line of three items each
            [[(72 + (i % 3) * 150, 780 - (i // 3) * 3.4, 1.2, f"Item {i}.") for i in range(660)]]),
        "many-blank-pages.pdf": synthetic_materials.pdf(  # 20,000 pages, all but the first (crowded-page's) blank: 2.4 MB
            [[(72 + (i % 3) * 150, 780 - (i // 3) * 3.4, 1.2, f"Item {i}.") for i in range(660)]] + [[]] * 19_999),
        # S1-17's search flows: an English and a Chinese paper with no identifier.
        "Wage floors and employment.md": synthetic_materials.SEARCH_NOTES,
        "最低工资与就业笔记.md": synthetic_materials.CHINESE_NOTES,
        "Unmapped glyphs.md": synthetic_materials.GLYPH_NOTES,  # past the helper's slot, and refused (S1-18's finding)
        "scanned-letter.pdf": synthetic_materials.scanned_letter(DOIS["scan"]),  # S1-20: an image, no text layer
        # The reading's ceilings (backend/reading.py): marked for the walkthrough's reading child.
        "large-figures.pdf": synthetic_materials.paper_pdf(title="Large Figures").replace(
            b"\n", b"\n%WALKTHROUGH-MEMORY\n", 1),
        "slow-source.tex": b"% WALKTHROUGH-SLOW\n" + synthetic_materials.paper_latex(title="A Slow Source"),
        # S1-15: the PDFs the test-owned Zotero's attachments name
        **{name: synthetic_materials.paper_pdf(title, doi=doi) for name, (title, doi) in ZOTERO_PDFS.items()},
    }
    for name, data in files.items():
        (folder / name).write_bytes(data)
    return folder


# S1-15: the test-owned Zotero's items (made up), by key: (title, DOI, its PDF's file in the materials folder
# or a file URL elsewhere, linked, collection).
ZOTERO_DOI = "10.5555/scholia.walkthrough.zotero"
ZOTERO_PDFS = {"zotero-wages.pdf": ("Minimum Wages in Synthetic Towns", ZOTERO_DOI),
               "zotero-linked.pdf": ("Linked Notes on Synthetic Labour Markets", None),
               "zotero-hanzi.pdf": ("合成数据中的工资与就业", None),
               "zotero-hanzi-revised.pdf": ("合成数据中的工资与就业（修订版）", None)}
ZOTERO_ITEMS = {
    "ZWAGE001": ("Minimum Wages in Synthetic Towns", ZOTERO_DOI, "zotero-wages.pdf", False, "WAGECOL1"),
    "ZLINK002": ("Linked Notes on Synthetic Labour Markets", None, "zotero-linked.pdf", True, "LINKCOL2"),
    "ZMETA003": ("A Synthetic Working Paper Without a File", None, None, False, "WAGECOL1"),
    # ZNAS0004's URL is refused by name before anything is opened (pdf_not_local, its ".."); a real share's
    # path is refused by the Mac's mount table instead (pdf_on_network), which differs from Mac to Mac.
    "ZNAS0004": ("A Report Kept on a Network Share (synthetic)", None, "file:///tmp/../net/nas.example/share/report.pdf", False,
                 "WAGECOL1"),
    "ZHAN0005": ("合成数据中的工资与就业", None, "zotero-hanzi.pdf", False, "WAGECOL1"),
    "ZLONG006": ("A Very Long Synthetic Title About Minimum Wages, Hours, Employment and Earnings in Small Towns and "
                 "Large Cities Across Several Synthetic Decades, Written to Test How the Browser Shortens It", None, None,
                 False, None),
}


class WalkthroughZotero(MockZotero):
    """The walkthrough's Zotero: its state read from the control file at each request (see the module)."""

    GROUPS = [(4711, "Synthetic Lab"), (4712, "合成课题组")]
    COLLECTIONS = [{"key": "WAGECOL1", "name": "Wage studies"}, {"key": "LINKCOL2", "name": "Linked files", "parent": "WAGECOL1"}]

    def __init__(self, control, materials):
        super().__init__(server_id="walkthrough-zotero", groups=self.GROUPS)
        # the folder by its own path: Scholia follows no link to an attachment (/var is one on macOS)
        self.control, self.materials = control, materials.resolve()
        self.collections["users/0"] = self.COLLECTIONS
        for key, (title, doi, pdf, linked, collection) in ZOTERO_ITEMS.items():
            path = pdf if pdf is None or pdf.startswith("file:") else self.materials / pdf
            self.item(key, title, doi=doi, pdf=path, linked=linked, collections=[collection] if collection else [])

    async def __call__(self, request):
        state = self.control.read_text().strip() if self.control.exists() else "not_running"
        self.state = "ready" if state in ("changed", "pruned") else state
        self.groups = self.GROUPS[1:] if state == "pruned" else self.GROUPS
        self.collections["users/0"] = self.COLLECTIONS[:1] if state == "pruned" else self.COLLECTIONS
        if state == "changed":  # the Chinese item retitled in Zotero, its PDF replaced by a revised one
            item = self.items["users/0", "ZHAN0005"]
            item["data"]["title"] = item["csljson"]["title"] = ZOTERO_PDFS["zotero-hanzi-revised.pdf"][0]
            self.files["users/0", "ZHAN000Z"] = (self.materials / "zotero-hanzi-revised.pdf").as_uri()
        return await super().__call__(request)


# The search model the walkthrough offers: a synthetic file, never a model, with its own pin and the
# real sources' addresses, so the gate classifies and audits its download as it does the real one.
MODEL_BYTES = b"Scholia walkthrough: a synthetic stand-in for the search model file, not a model. " * 2048
MODEL = {**local_helper.EMBEDDING_MODEL, "name": "Qwen3-Embedding-0.6B Q8_0 (synthetic file)", "size": len(MODEL_BYTES),
         "sha256": hashlib.sha256(MODEL_BYTES).hexdigest()}
FILE_HOSTS = {"huggingface.co": "us.aws.cdn.hf.co", "modelscope.cn": "cdn-lfs-cn-1.modelscope.cn"}


class SlowFile(httpx.AsyncByteStream):
    """The model file in 200 pieces, 50 ms apart: a download takes about 10 s."""

    async def __aiter__(self):
        piece = len(MODEL_BYTES) // 200 + 1
        for start in range(0, len(MODEL_BYTES), piece):
            await asyncio.sleep(0.05)
            yield MODEL_BYTES[start:start + piece]


def download_source(request):
    """The test-owned download source: each source redirects to its file host, which sends the file."""
    if request.url.host in FILE_HOSTS:
        return httpx.Response(302, headers={"Location": f"https://{FILE_HOSTS[request.url.host]}/files/model?signed=0"})
    return httpx.Response(200, stream=SlowFile())


# S1-17: the stand-in helper. It prints its listening line and waits; it reads no model and opens no socket.
STAND_IN = """#!{python}
import time
print("srv  llama_server: listening on http://127.0.0.1:50001", flush=True)
while True:
    time.sleep(1)
"""


def helper_answer(request, control):
    """The stand-in helper's HTTP side: health, its tokenizer and slot, and synthetic embeddings (503
    while control exists), refusing an input with synthetic_materials.GLYPHS_REFUSED in it."""
    if request.url.path == "/health":
        return httpx.Response(200, json={"status": "ok"})
    if request.url.path not in ("/v1/embeddings", "/tokenize") or control.exists():
        return httpx.Response(503, json={"error": "unavailable"})
    body = json.loads(request.content)
    slot = local_helper.CONTEXT // local_helper.SLOTS
    if (answer := synthetic_materials.helper_tokenizer(request.url.path, body, slot,
                                                       refuse=synthetic_materials.GLYPHS_REFUSED)) is not None:
        return httpx.Response(answer[0], json=answer[1])
    texts = body["input"]
    return httpx.Response(200, json={"data": [{"index": i, "embedding": synthetic_materials.embedding(text)}
                                              for i, text in enumerate(texts)]})


# S1-19b: a model with no endpoint that takes tools (one without zero retention, so no Private flow uses it)
NO_TOOLS = "deepseek/deepseek-v4-flash"
NO_TOOLS_ENDPOINTS = [{"tag": "example", "context_length": 131072,
                       "supported_parameters": ["max_tokens", "temperature", "reasoning", "include_reasoning"]}]


def role(body):
    """A chat call's role, from its first message: title, judge, router, memory (an extraction) or answer."""
    first = ((body or {}).get("messages") or [{}])[0].get("content", "") if isinstance(body, dict) else ""
    return "title" if first.startswith("Write a title") else "judge" if first.startswith(JUDGE_RULES) else \
        "router" if first.startswith(router.ROUTER_RULES) else "memory" if first.startswith(memory.EXTRACT_RULES) else "answer"


# S1-21a: what the synthetic extractor proposes (walkthrough step 91), by the researcher's message
CONSIDERING = ("I am considering a survey of managers", "我在考虑对管理者做问卷调查")
PROPOSED = ("A survey of managers is being considered", "正在考虑对管理者做问卷调查")


def memory_reply(body):
    said = body["messages"][-1]["content"].split("RESEARCHER'S MESSAGE:\n", 1)[-1].split("\n\nASSISTANT'S ANSWER", 1)[0]
    records = [{"type": "decision", "content": PROPOSED[i], "status": "proposed", "span": said, "replaces": ""}
               for i, text in enumerate(CONSIDERING) if text in said]
    return 200, {"choices": [{"message": {"role": "assistant", "content": json.dumps({"records": records}, ensure_ascii=False)}}],
                 "usage": {"prompt_tokens": 200, "completion_tokens": 30, "total_tokens": 230, "cost": 0.0005}}


class SyntheticProvider(MockProvider):
    log = None  # the --request-log file
    control = None  # the stand-in helper's control file (S1-17)

    async def __call__(self, request):
        if self.log is not None:
            body = json.loads(request.content) if request.content and request.method == "POST" else {}
            chat = request.url.path.endswith("/chat/completions")
            with open(self.log, "a", encoding="utf-8") as out:
                out.write(json.dumps({"method": request.method, "host": request.url.host, "path": request.url.path,
                                      "model": body.get("model") if isinstance(body, dict) else None,
                                      **({"role": role(body)} if chat else {})}) + "\n")
        if (request.url.host, request.url.port) == MockZotero.ORIGIN:  # S1-15: the test-owned Zotero
            if self.log is not None:
                with open(self.log, "a", encoding="utf-8") as out:
                    out.write(json.dumps({"method": request.method, "host": "zotero",
                                          "target": request.url.raw_path.decode()}) + "\n")
            return await super().__call__(request)
        if request.url.host == "127.0.0.1":  # the stand-in helper (S1-17)
            return helper_answer(request, self.control)
        if request.url.host in FILE_HOSTS or request.url.host in FILE_HOSTS.values():
            return download_source(request)
        if request.url.path.endswith("/models"):
            return httpx.Response(200, json={"data": CATALOG})
        return await super().__call__(request)


VERDICTS = ("supported", "partly", "unsupported")
CITED = ("The synthetic notes report that wage floors raised the earnings of low-paid workers {}.",
         "They report that employment fell slightly in the smallest firms {}.",
         "They also report that prices fell in every region {}.")  # the judge finds this one unsupported
ABSTAIN = "Your library holds no evidence on this question, so I cannot answer it from your papers."


# S1-19c: the agent's steps for the help skill and the ask tool (walkthrough steps 73 and 74)
HELP = ("How do I add a reference file?", "如何添加参考文献文件？")
HELP_ANSWER = ("Open the Library's Add menu and choose A reference file, then pick your BibTeX, RIS or CSL JSON file. "
               "I have opened the Library for you.",
               "打开文献库的“添加”菜单，选择“参考文献文件”，再选择你的 BibTeX、RIS 或 CSL JSON 文件。我已为你打开文献库。")
ASKING = ("Which outcome should the summary focus on?", "摘要应侧重哪个结果？")
NOTING = ("Note the next step for this project", "记下这个项目的下一步")  # S1-21a: update_progress (step 92)
NEXT_STEP = ("Code the first five interviews", "为前五份访谈编码")
OUTCOMES = (["Earnings", "Employment"], ["收入", "就业"])


def agent_step(body):
    """The agent's reply to a step of the help or ask flow, or None for any other request: a tool call, then
    the answer once the tools have answered."""
    messages = body["messages"]
    last = max(i for i, m in enumerate(messages) if m.get("role") == "user")
    request, done = messages[last]["content"], [m for m in messages[last + 1:] if m.get("role") == "tool"]
    if request in HELP:
        zh = HELP.index(request)
        if len(done) < 2:
            name, arguments = (("use_skill", {"name": "help"}), ("open_place", {"place": "library"}))[len(done)]
            return tool_call(f"w{len(done)}", name, arguments)
        return HELP_ANSWER[zh]
    if request in NOTING:
        zh = NOTING.index(request)
        if not done:
            return tool_call("w-note", "update_progress", {"kind": "next_step", "text": NEXT_STEP[zh]})
        return "Noted." if not zh else "已记下。"
    if request in ASKING:
        zh = ASKING.index(request)
        if not done:
            return tool_call("w-ask", "ask", {"question": request, "options": OUTCOMES[zh]})
        chosen = done[-1]["content"].removeprefix("The researcher answered: ")
        return (f"You chose {chosen}: the summary will focus on it." if not zh else f"你选择了{chosen}：摘要将侧重于此。")
    return None


def tool_call(call_id, name, arguments):
    return 200, {"choices": [{"message": {"role": "assistant", "content": None, "tool_calls": [
        {"id": call_id, "type": "function", "function": {"name": name, "arguments": json.dumps(arguments)}}]},
        "finish_reason": "tool_calls"}], "usage": {"prompt_tokens": 120, "completion_tokens": 20, "total_tokens": 140,
                                                    "cost": 0.0012}}


def synthetic(body):
    kind = role(body)
    first = ((body or {}).get("messages") or [{}])[0].get("content", "")
    stepped = agent_step(body) if kind == "answer" and body.get("tools") else None
    if isinstance(stepped, tuple):
        return stepped
    if stepped is not None:
        text = stepped
    elif kind == "title":
        text = "Designing a cohort study"
    elif kind == "judge":
        pairs = body["messages"][-1]["content"].count("Pair ")
        text = "\n".join(f"{i}: {VERDICTS[(i - 1) % 3]}" for i in range(1, pairs + 1))
    elif kind == "router":
        text = "literature"
    elif "No passage in the researcher's library matched" in first:
        text = ABSTAIN
    elif "## RESEARCH EVIDENCE" in first and not any(  # the earlier flows' own question keeps its formatted answer
            mark in body["messages"][-1]["content"] for mark in ("synthetic walkthrough question", "合成演示问题")):
        markers = re.findall(r"^\[S[0-9]+\.[0-9]+\]", first.split("## RESEARCH EVIDENCE", 1)[1], re.M)
        text = " ".join(sentence.format(markers[i % len(markers)]) for i, sentence in enumerate(CITED))
    else:
        text = ANSWER
    return 200, {"choices": [{"message": {"role": "assistant", "content": text}}],
                 "usage": {"prompt_tokens": 120, "completion_tokens": 180, "total_tokens": 300, "cost": 0.0042}}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--port", type=int)
    parser.add_argument("--dev", action="store_true")
    parser.add_argument("--request-log", type=Path)
    args = parser.parse_args(argv)

    # The reading child: tests/reading_stub.py's walkthrough mode (see the docstring), with its own folder.
    child = [sys.executable, str(ROOT / "tests" / "reading_stub.py"), "walkthrough",
             tempfile.mkdtemp(prefix="scholia-walkthrough-reading-")]
    reading.command = lambda: child
    # The silent file is stopped in 20 s rather than the app's 60; a healthy reading's child on a loaded Mac
    # (two test suites running) has gone 5 s without a word, so the ceiling stays well above that.
    reading.STEP_SECONDS = 20.0
    provider = SyntheticProvider(zero_retention=[model["id"] for model in CATALOG[::2]],  # half have zero retention
                                 scholarly=RECORDS)
    provider.replies = [synthetic] * 5000
    provider.title_replies = [synthetic] * 1000
    provider.memory_replies = [memory_reply] * 1000  # S1-21a
    provider.endpoints[NO_TOOLS] = NO_TOOLS_ENDPOINTS  # S1-19b
    provider.log = args.request_log
    data_dir = Path(tempfile.mkdtemp(prefix="scholia-walkthrough-"))
    harness = Path(tempfile.mkdtemp(prefix="scholia-walkthrough-files-"))
    offline = harness / "offline" / MODEL["file"]  # the file the import flow names
    offline.parent.mkdir()
    offline.write_bytes(MODEL_BYTES)
    binary = harness / "Scholia.app" / "Contents" / "MacOS" / "llama-server"  # the stand-in (S1-17)
    for folder in (binary.parent, binary.parents[1] / "Frameworks" / "llama-cpp", binary.parents[1] / "Resources"):
        folder.mkdir(parents=True)
    binary.write_text(STAND_IN.format(python=sys.executable))
    binary.chmod(0o755)
    provider.control = harness / "helper-offline"
    clock = harness / "memory-clock"  # S1-21a: seconds Where we left off's clock is moved forward
    clock.write_text("0")
    real_clock = memory._clock
    memory._clock = lambda: real_clock() + timedelta(seconds=float(clock.read_text() or 0))
    local_helper.write_manifest(binary.parents[1])
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.bind(("127.0.0.1", args.port if args.port is not None else 8765 if args.dev else 0))
    origin = f"http://127.0.0.1:{sock.getsockname()[1]}"
    session = None if args.dev else secrets.token_urlsafe(32)
    app = create_app(data_dir, origin=origin, session=session,
                     dev_origins=("http://127.0.0.1:5173",) if args.dev else (),
                     frontend_dir=None if args.dev else ROOT / "frontend" / "dist",
                     keyring_backend=FakeKeyring(), transport=httpx.MockTransport(streaming(provider)),
                     helper=local_helper.Config(binary=binary, models={MODEL["id"]: MODEL}))
    print(f"data folder: {data_dir}", flush=True)
    materials = write_materials()
    zotero_control = harness / "zotero-state"  # S1-15
    zotero_control.write_text("api_off")
    provider.zotero = WalkthroughZotero(zotero_control, materials)
    print(f"materials: {materials}", flush=True)
    print(f"zotero state: {zotero_control}", flush=True)
    print(f"model file: {offline}", flush=True)
    print(f"helper control: {provider.control}", flush=True)
    print(f"memory clock: {clock}", flush=True)
    print(f"step ceiling: {reading.STEP_SECONDS:g}", flush=True)
    print(f"open: {origin}/" + ("" if args.dev else f"#session={session}"), flush=True)
    server = uvicorn.Server(uvicorn.Config(app, loop="asyncio", http="h11", ws="none", log_level="warning"))
    with network_guard.allow_command(*child):  # the reading child alone, by its whole command
        with network_guard.allow_subprocess(str(binary)):  # and the stand-in helper (S1-17)
            asyncio.run(server.serve(sockets=[sock]))


if __name__ == "__main__":
    main()
