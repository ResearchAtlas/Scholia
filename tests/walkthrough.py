"""Serves the app for interface work and rendered walkthroughs, never with real data.

    uv run python tests/walkthrough.py [--port N] [--dev] [--request-log FILE]

The backend runs on a new temporary data folder, with an in-memory credential store
(never the Keychain), a test-owned provider that answers with synthetic text, and test-owned
stand-ins for OpenAlex, Crossref and arXiv that answer made-up records, behind the test network
block (network_guard.py), so this process reaches nothing but its own listener. It serves the
built interface (frontend/dist) and prints the window's address with this launch's session, and
the folder of synthetic materials (a PDF, DOCX, HTML, Markdown and LaTeX file, and more) it wrote
for the walkthrough to add. The local model helper's search model is a synthetic file with a pin
of its own, served by a test-owned download source (each source redirecting to the file host
it uses, the file sent slowly so a download can be watched and cancelled), and written to an
"offline" folder for the import flow, whose path is printed. The helper binary is a test-owned
stand-in in a bundle with its manifest (S1-17): a small program, the one child process the network
block allows, that opens no socket and only prints the line a llama-server prints once it listens;
its HTTP side is answered here, behind the outbound gate's transport, with synthetic vectors
(synthetic_materials.embedding). While the file named by "helper control" exists, it answers
embedding requests 503, as a helper that stopped answering. --dev serves no interface and admits the Vite server on
127.0.0.1:5173 instead (`npm run dev` in frontend/), with no session, on port 8765, where
that server sends API requests. Without --port, a free port is used. --request-log appends
each request the test-owned provider receives to FILE, one JSON line each.

tests/walkthrough_driver.mjs drives the rendered walkthrough against this server.
"""

import argparse
import asyncio
import hashlib
import json
import secrets
import socket
import sys
import tempfile
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
from scholia_app import FakeKeyring, MockProvider, MockScholarly, crossref_work, openalex_work  # noqa: E402

from backend import local_helper  # noqa: E402
from backend.app import create_app  # noqa: E402
from backend.budget_router import MODEL_TIERS  # noqa: E402

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
        "latex": "10.5555/scholia.walkthrough.floors", "local": "10.5555/scholia.walkthrough.codebook"}
ARXIV_ID = "2401.00001"
RECORDS = MockScholarly(
    openalex={DOIS["pdf"]: openalex_work(DOIS["pdf"], "Minimum Wages and Employment in a Synthetic Panel",
                                         authors=("Ana Example", "Bo Sample"), year=2024),
              DOIS["latex"]: openalex_work(DOIS["latex"], "最低工资的合成模型：一项方法说明",
                                           authors=("Chen Example",), year=2023, venue="合成经济研究"),
              DOIS["local"]: openalex_work(DOIS["local"], "A Codebook for Synthetic Interviews", year=2022)},
    crossref={DOIS["docx"]: crossref_work(DOIS["docx"], "Wages Across Synthetic Cities", retracted=True)},
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
        "long-notes.md": long_notes(),
        "crowded-page.pdf": synthetic_materials.pdf(  # one page of 220 passages, a line of three items each
            [[(72 + (i % 3) * 150, 780 - (i // 3) * 3.4, 1.2, f"Item {i}.") for i in range(660)]]),
        # S1-17's search flows: an English and a Chinese paper with no identifier.
        "Wage floors and employment.md": synthetic_materials.SEARCH_NOTES,
        "最低工资与就业笔记.md": synthetic_materials.CHINESE_NOTES,
    }
    for name, data in files.items():
        (folder / name).write_bytes(data)
    return folder


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
    """The stand-in helper's HTTP side: health, and synthetic embeddings (503 while control exists)."""
    if request.url.path == "/health":
        return httpx.Response(200, json={"status": "ok"})
    if request.url.path != "/v1/embeddings" or control.exists():
        return httpx.Response(503, json={"error": "unavailable"})
    texts = json.loads(request.content)["input"]
    return httpx.Response(200, json={"data": [{"index": i, "embedding": synthetic_materials.embedding(text)}
                                              for i, text in enumerate(texts)]})


class SyntheticProvider(MockProvider):
    log = None  # the --request-log file
    control = None  # the stand-in helper's control file (S1-17)

    async def __call__(self, request):
        if self.log is not None:
            body = json.loads(request.content) if request.content and request.method == "POST" else {}
            with open(self.log, "a", encoding="utf-8") as out:
                out.write(json.dumps({"method": request.method, "host": request.url.host, "path": request.url.path,
                                      "model": body.get("model") if isinstance(body, dict) else None}) + "\n")
        if request.url.host == "127.0.0.1":  # the stand-in helper (S1-17)
            return helper_answer(request, self.control)
        if request.url.host in FILE_HOSTS or request.url.host in FILE_HOSTS.values():
            return download_source(request)
        if request.url.path.endswith("/models"):
            return httpx.Response(200, json={"data": CATALOG})
        return await super().__call__(request)


def synthetic(body):
    first = ((body or {}).get("messages") or [{}])[0].get("content", "")
    text = "Designing a cohort study" if first.startswith("Write a title") else ANSWER
    return 200, {"choices": [{"message": {"role": "assistant", "content": text}}],
                 "usage": {"prompt_tokens": 120, "completion_tokens": 180, "total_tokens": 300, "cost": 0.0042}}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--port", type=int)
    parser.add_argument("--dev", action="store_true")
    parser.add_argument("--request-log", type=Path)
    args = parser.parse_args(argv)

    provider = SyntheticProvider(zero_retention=[model["id"] for model in CATALOG[::2]],  # half have zero retention
                                 scholarly=RECORDS)
    provider.replies = [synthetic] * 1000
    provider.title_replies = [synthetic] * 1000
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
    local_helper.write_manifest(binary.parents[1])
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.bind(("127.0.0.1", args.port if args.port is not None else 8765 if args.dev else 0))
    origin = f"http://127.0.0.1:{sock.getsockname()[1]}"
    session = None if args.dev else secrets.token_urlsafe(32)
    app = create_app(data_dir, origin=origin, session=session,
                     dev_origins=("http://127.0.0.1:5173",) if args.dev else (),
                     frontend_dir=None if args.dev else ROOT / "frontend" / "dist",
                     keyring_backend=FakeKeyring(), transport=httpx.MockTransport(provider),
                     helper=local_helper.Config(binary=binary, models={MODEL["id"]: MODEL}))
    print(f"data folder: {data_dir}", flush=True)
    print(f"materials: {write_materials()}", flush=True)
    print(f"model file: {offline}", flush=True)
    print(f"helper control: {provider.control}", flush=True)
    print(f"open: {origin}/" + ("" if args.dev else f"#session={session}"), flush=True)
    server = uvicorn.Server(uvicorn.Config(app, loop="asyncio", http="h11", ws="none", log_level="warning"))
    with network_guard.allow_subprocess(str(binary)):  # the stand-in helper only (S1-17)
        asyncio.run(server.serve(sockets=[sock]))


if __name__ == "__main__":
    main()
