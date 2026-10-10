"""The packaged app's self-test.

The app runs it with `--self-test --model <embedding model>`. It checks, inside the frozen
build, the native pieces the app ships: SQLite, FTS5 secure-delete, extension loading and
sqlite-vec through APSW, the backend with one turn against an in-process provider, an
AES-encrypted zip file as backups and exports write them (pyzipper and pycryptodomex's native
code, imported only when first used), one embedding through the llama.cpp helper, whose binary and libraries are first checked against
the build's SHA-256 manifest as the app checks them before every launch, and materials read as the
app reads them, each in a child process (backend/reading.py; here, this executable started with
--read-material): a PDF, LaTeX source, a page image, one scanned PDF page through the OCR engine
(Vision), and a reading past a ceiling below any child's footprint, stopped. It prints the
results as JSON and exits non-zero when any check fails.

The checks also run from source (tests/test_self_test.py), except the embedding, which
needs the model and the helper binary.
"""

import argparse
import hashlib
import json
import math
import os
import platform
import queue
import re
import secrets
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request
import zipfile
from pathlib import Path

from backend.local_helper import EMBEDDING_MODEL, LISTENING, binary_problem, command, mismatch  # noqa: F401

MIN_SQLITE = (3, 42, 0)  # the first SQLite with FTS5 secure-delete
SQLITE_VEC_VERSION = "v0.1.9"
DIMENSIONS = 1024

# A bound for a cold start, not the app's start deadline, which is a separate setting: the
# first start on a CI runner's virtual GPU spent about 35 s preparing Metal before the model loaded.
HELPER_START_SECONDS = 120
# The scanned page: one English and one Simplified Chinese line.
OCR_LINES = ["Scholia self-test 2026", "学术研究平台"]


def runtime() -> dict:
    return {
        "python": platform.python_version(),
        "implementation": platform.python_implementation(),
        "machine": platform.machine(),
        "macos": platform.mac_ver()[0],
        "frozen": bool(getattr(sys, "frozen", False)),
        # "Python" for the python.org framework build, "" for a non-framework build
        "framework": getattr(sys, "_framework", None),
    }


def _at_least(version: str) -> bool:
    return tuple(int(part) for part in version.split(".")[:3]) >= MIN_SQLITE


def check_sqlite() -> dict:
    """The main database's SQLite: Python's sqlite3."""
    if not _at_least(sqlite3.sqlite_version):
        raise RuntimeError(f"sqlite3 has SQLite {sqlite3.sqlite_version}, older than 3.42")
    return {"sqlite": sqlite3.sqlite_version}


def check_index() -> dict:
    """The search index's SQLite: APSW, with FTS5 secure-delete and sqlite-vec."""
    import apsw
    import sqlite_vec

    version = apsw.sqlite_lib_version()
    if not _at_least(version):
        raise RuntimeError(f"APSW has SQLite {version}, older than 3.42")
    with tempfile.TemporaryDirectory() as folder:
        con = apsw.Connection(str(Path(folder, "search.sqlite3")))
        try:
            con.execute(
                "CREATE VIRTUAL TABLE fts USING fts5(text, project_id UNINDEXED, passage_id UNINDEXED);"
                "INSERT INTO fts(fts, rank) VALUES ('secure-delete', 1);"
                "INSERT INTO fts VALUES ('alpha beta', 'p1', 1), ('beta gamma', 'p1', 2);"
                "DELETE FROM fts WHERE passage_id = 1;"
            )
            setting = con.execute("SELECT v FROM fts_config WHERE k = 'secure-delete'").get
            hits = con.execute("SELECT passage_id FROM fts WHERE fts MATCH 'beta'").get
            if setting != 1 or hits != 2:
                raise RuntimeError(f"FTS5 secure-delete: setting {setting!r}, match {hits!r}")

            con.enable_load_extension(True)
            con.load_extension(sqlite_vec.loadable_path())
            con.enable_load_extension(False)
            vec_version = con.execute("SELECT vec_version()").get
            if vec_version != SQLITE_VEC_VERSION:
                raise RuntimeError(f"sqlite-vec {vec_version}, expected {SQLITE_VEC_VERSION}")
            con.execute(
                "CREATE VIRTUAL TABLE vec USING vec0("
                f"project_id TEXT PARTITION KEY, passage_id INTEGER, embedding float[{DIMENSIONS}])"
            )
            rows = [("p1", 1, 0.0), ("p1", 2, 1.0), ("p2", 3, 0.1)]
            for project, passage, value in rows:
                con.execute(
                    "INSERT INTO vec(project_id, passage_id, embedding) VALUES (?, ?, ?)",
                    (project, passage, sqlite_vec.serialize_float32([value] * DIMENSIONS)),
                )
            nearest = con.execute(
                "SELECT passage_id FROM vec WHERE embedding MATCH ? AND k = 1 AND project_id = 'p1'",
                (sqlite_vec.serialize_float32([0.05] * DIMENSIONS),),
            ).get
            if nearest != 1:
                raise RuntimeError(f"vec0 nearest neighbour in project p1 is {nearest!r}, not 1")
        finally:
            con.close()
    return {"sqlite": version, "apsw": apsw.apsw_version(), "sqlite_vec": vec_version}


def verify_model(path: Path) -> None:
    """Refuse a model file that differs from the pin."""
    if error := mismatch(path, EMBEDDING_MODEL):
        raise RuntimeError(error)


def helper_command(helper: Path, model: Path) -> list[str]:
    return command(helper, model)


def wait_for_port(lines: "queue.Queue[str | None]", log: list[str], deadline: float) -> int:
    """Read the helper's log until its `listening on` line, which comes after the model loads."""
    while (left := deadline - time.monotonic()) > 0:
        try:
            line = lines.get(timeout=left)
        except queue.Empty:
            break
        if line is None:
            raise RuntimeError("the helper exited before it was ready")
        log.append(line)
        if match := LISTENING.search(line.encode()):
            return int(match.group(1))
    raise RuntimeError(f"the helper was not ready within {HELPER_START_SECONDS} s")


_opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))  # loopback: never a proxy


def embed(port: int, key: str | None, text: str) -> list[float]:
    """One embedding from the helper's OpenAI-style endpoint."""
    request = urllib.request.Request(
        f"http://127.0.0.1:{port}/v1/embeddings",
        data=json.dumps({"input": text}).encode(),
        headers={"Content-Type": "application/json"}
        | ({"Authorization": f"Bearer {key}"} if key else {}),
    )
    with _opener.open(request, timeout=60) as response:
        return json.load(response)["data"][0]["embedding"]


def verify_embedding(vector: list[float]) -> float:
    """The vector's norm, or an error unless it is a unit vector of DIMENSIONS finite values.

    The helper's OpenAI-style endpoint returns normalized embeddings, so a zero or
    non-unit vector means the model did not run as expected.
    """
    if len(vector) != DIMENSIONS or not all(math.isfinite(x) for x in vector):
        raise RuntimeError(f"embedding has {len(vector)} values, expected {DIMENSIONS} finite")
    norm = math.sqrt(sum(x * x for x in vector))
    if abs(norm - 1) > 0.01:
        raise RuntimeError(f"embedding norm is {norm:.4f}, expected 1")
    return norm


def refused_without_key(port: int) -> bool:
    try:
        embed(port, None, "no key")
    except urllib.error.HTTPError as error:
        return error.code == 401
    return False


def check_embedding(helper: Path, model: Path) -> dict:
    if problem := binary_problem(helper):
        raise RuntimeError(f"the helper's binaries failed their check ({problem})")
    verify_model(model)
    key = secrets.token_urlsafe(32)
    env = {"LLAMA_API_KEY": key} | {k: os.environ[k] for k in ("HOME", "TMPDIR") if k in os.environ}
    started = time.monotonic()
    process = subprocess.Popen(
        helper_command(helper, model), env=env, stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, errors="replace",
    )
    lines: queue.Queue = queue.Queue()

    def pump():
        for line in process.stdout:
            lines.put(line)
        lines.put(None)

    threading.Thread(target=pump, daemon=True).start()
    log: list[str] = []
    try:
        port = wait_for_port(lines, log, started + HELPER_START_SECONDS)
        ready = time.monotonic() - started
        vector = embed(port, key, "Scholia 学术 self-test")
        norm = verify_embedding(vector)
        if not refused_without_key(port):
            raise RuntimeError("the helper answered a request without the API key")
        return {"dimensions": len(vector), "norm": round(norm, 4), "ready_seconds": round(ready, 2)}
    except Exception as error:
        while not lines.empty():
            log.append(lines.get_nowait() or "")
        raise RuntimeError(f"{error}; helper log tail: {''.join(log[-15:])}") from error
    finally:
        process.terminate()
        try:
            process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait()


def scanned_pdf(lines, size=(612, 792), dpi=300):
    """A one-page PDF of size (points) holding only an image of lines drawn in black on white, as a
    scanned page does: no text layer. lines: [(x, y, points, text)], from the page's bottom left,
    drawn by AppKit at dpi in the system font."""
    import ctypes
    import io

    import AppKit
    import pypdfium2 as pdfium
    import pypdfium2.raw as raw

    scale = dpi / 72
    width, height = round(size[0] * scale), round(size[1] * scale)
    rep = AppKit.NSBitmapImageRep.alloc().initWithBitmapDataPlanes_pixelsWide_pixelsHigh_bitsPerSample_samplesPerPixel_hasAlpha_isPlanar_colorSpaceName_bytesPerRow_bitsPerPixel_(  # noqa: E501
        None, width, height, 8, 1, False, False, AppKit.NSDeviceWhiteColorSpace, 0, 0
    )
    context = AppKit.NSGraphicsContext.graphicsContextWithBitmapImageRep_(rep)
    AppKit.NSGraphicsContext.saveGraphicsState()
    try:
        AppKit.NSGraphicsContext.setCurrentContext_(context)
        AppKit.NSColor.whiteColor().set()
        AppKit.NSRectFill(AppKit.NSMakeRect(0, 0, width, height))
        for x, y, points, text in lines:
            attributes = {
                AppKit.NSFontAttributeName: AppKit.NSFont.systemFontOfSize_(points * scale),
                AppKit.NSForegroundColorAttributeName: AppKit.NSColor.blackColor(),
            }
            AppKit.NSString.stringWithString_(text).drawAtPoint_withAttributes_(
                AppKit.NSMakePoint(x * scale, y * scale), attributes
            )
        context.flushGraphics()
    finally:
        AppKit.NSGraphicsContext.restoreGraphicsState()
    stride, pixels = rep.bytesPerRow(), bytes(rep.bitmapData()[: rep.bytesPerRow() * height])
    document = pdfium.PdfDocument.new()
    page = document.new_page(*size)
    bitmap = pdfium.PdfBitmap.new_native(width, height, raw.FPDFBitmap_Gray)
    for row in range(height):
        ctypes.memmove(ctypes.addressof(bitmap.buffer) + row * bitmap.stride, pixels[row * stride:row * stride + width], width)
    image = pdfium.PdfImage.new(document)
    image.set_bitmap(bitmap)
    image.set_matrix(pdfium.PdfMatrix().scale(*size))
    page.insert_obj(image)
    page.gen_content()
    out = io.BytesIO()
    document.save(out)
    return out.getvalue()


def _stored(folder, data):
    """data written to folder as the content store keeps a file: (its path, its SHA-256)."""
    sha256 = hashlib.sha256(data).hexdigest()
    path = Path(folder) / sha256
    path.write_bytes(data)
    return path, sha256


def check_ocr() -> dict:
    """One scanned page read as the app reads one, in its child process (backend/reading.py): the page
    rendered by pypdfium2 at 300 dpi, its text recognized by the OCR engine (backend/ocr.py), made
    into passages."""
    from backend import extraction, ocr, reading

    engine = ocr.engine()
    if engine is None:
        raise RuntimeError("no OCR engine loads")
    stats = {}
    with tempfile.TemporaryDirectory() as folder:
        scan = _stored(folder, scanned_pdf([(72, 700, 14, OCR_LINES[0]), (72, 600, 14, OCR_LINES[1])]))
        read = reading.read(*scan, extraction.PDF, stats=stats)
    found = [passage.text for passage in read.passages]
    text = "".join(found).replace(" ", "")
    missing = [line for line in OCR_LINES if line.replace(" ", "") not in text]
    if missing or (read.ocr_pages, read.status) != (1, "complete"):
        raise RuntimeError(f"OCR missed {missing}; read {found}")
    return {"lines": found, "engine": engine.version,
            "min_confidence": min(passage.boxes["ocr"]["confidence"] for passage in read.passages),
            "child_peak_mib": stats["peak_mib"]}


class _Keys:
    """An in-memory credential store for the backend check: the Keychain is never touched."""

    def __init__(self):
        self.keys = {}

    def get_password(self, service, name):
        return self.keys.get((service, name))

    def set_password(self, service, name, value):
        self.keys[(service, name)] = value

    def delete_password(self, service, name):
        self.keys.pop((service, name), None)


def check_backend() -> dict:
    """The backend as the app runs it, in a temporary data folder: startup and migrations,
    first-run setup, a project, a conversation and one turn through the outbound gate to an
    in-process stand-in for the provider, and shutdown. No request leaves the process. The
    window's bindings are imported, without opening one."""
    import asyncio

    import httpx
    import uvicorn  # noqa: F401  # bundled, as the desktop entry needs it
    import webview  # noqa: F401
    import WebKit  # noqa: F401

    from backend.app import create_app

    calls = []

    async def provider(request):
        calls.append(request.url.path)
        return httpx.Response(200, json={"choices": [{"message": {"content": "Self-test answer."}}],
                                         "usage": {"cost": 0}})

    async def drive(folder):
        origin, session = "http://127.0.0.1:1", secrets.token_urlsafe(32)  # as the desktop entry starts it
        app = create_app(folder, origin=origin, session=session, keyring_backend=_Keys(),
                         transport=httpx.MockTransport(provider))
        async with app.app.router.lifespan_context(app.app):
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url=origin,
                                         headers={"X-Scholia-Client": "local"}) as client:
                outside = await client.get("/api/projects")
                if outside.status_code != 401:
                    raise RuntimeError(f"a request without this launch's session got {outside.status_code}")
                client.headers["X-Scholia-Session"] = session
                for method, path, body in (("POST", "/api/setup", {"openrouter_key": "self-test"}),
                                           ("POST", "/api/projects", {"name": "Self-test"})):
                    response = await client.request(method, path, json=body)
                    response.raise_for_status()
                project = response.json()["id"]
                conversation = (await client.post("/api/conversations", json={"project_id": project,
                                                                                "title": "Self-test"})).json()
                stream = await client.post(f"/api/conversations/{conversation['id']}/message/stream",
                                           json={"content": "Hello"})
                if '"status": "succeeded"' not in stream.text:
                    raise RuntimeError("the turn did not succeed")
                refused = await client.get("/api/projects", headers={"X-Scholia-Client": "", "Host": "evil.example"})
                if refused.status_code != 403:
                    raise RuntimeError(f"a request from another host got {refused.status_code}")

    with tempfile.TemporaryDirectory() as folder:
        asyncio.run(drive(Path(folder) / "data"))
    if calls != ["/api/v1/chat/completions"]:
        raise RuntimeError(f"unexpected provider calls {calls}")
    return {"turn": "succeeded"}


def check_interface(folder: Path | None = None) -> dict:
    """The built interface the app bundles (or `folder`), served as the window loads it: its
    page, with the Content-Security-Policy that refuses remote images, the script and
    stylesheet the page names, and every script those import, at start or with import() when a
    part of the window is first opened (frontend/src/parts.js)."""
    import asyncio

    import httpx

    from backend.app import create_app
    from backend.desktop import frontend_folder

    async def drive(data):
        origin = "http://127.0.0.1:1"
        app = create_app(data, origin=origin, session=secrets.token_urlsafe(32), keyring_backend=_Keys(),
                         frontend_dir=folder or frontend_folder())
        async with app.app.router.lifespan_context(app.app):
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url=origin) as client:
                page = await client.get("/")
                if page.status_code != 200 or '<div id="root">' not in page.text:
                    raise RuntimeError(f"the interface's page is not served ({page.status_code})")
                if "img-src 'self' data:" not in page.headers.get("content-security-policy", ""):
                    raise RuntimeError("the page has no Content-Security-Policy refusing remote images")
                files = re.findall(r'(?:src|href)="/(assets/[^"]+)"', page.text)
                if not any(f.endswith(".js") for f in files) or not any(f.endswith(".css") for f in files):
                    raise RuntimeError("the page names no script or no stylesheet")
                for file in files:  # grows with the scripts each one imports
                    response = await client.get("/" + file)
                    if response.status_code != 200:
                        raise RuntimeError(f"{file} is not served")
                    if file.endswith(".js"):
                        for name in re.findall(r"""(?:\bfrom\s*|\bimport\s*\(?\s*)["']\./([\w.-]+\.js)["']""", response.text):
                            if f"assets/{name}" not in files:
                                files.append(f"assets/{name}")
                return len(files)

    with tempfile.TemporaryDirectory() as folder_for_data:
        return {"assets": asyncio.run(drive(Path(folder_for_data) / "data"))}


def check_encrypted_zip() -> dict:
    """An AES-encrypted zip file written and read back as full backups and exports are: the
    passphrase opens it, and nothing is read without it."""
    from backend import backups

    content = "Scholia self-test 学术研究平台".encode()
    with tempfile.TemporaryDirectory() as folder:
        path = backups._write_zip(Path(folder), "self-test", [("check.txt", content)], "self-test passphrase",
                                  stop=lambda: False, audit=lambda path: None)
        with zipfile.ZipFile(path) as plain:  # as other tools see it: 99 marks WinZip AES
            info = plain.getinfo("check.txt")
            if not info.flag_bits & 1 or info.compress_type != 99:
                raise RuntimeError("the zip file is not AES-encrypted")
        with backups._pyzipper().AESZipFile(path) as archive:
            try:
                archive.read("check.txt")
            except RuntimeError:
                pass
            else:
                raise RuntimeError("the zip file was read without its passphrase")
            archive.setpassword(b"self-test passphrase")
            if archive.read("check.txt") != content:
                raise RuntimeError("the zip file does not read back what was written")
    return {"aes": True}


def _pdf(text):
    """A one-page PDF showing text in Helvetica, written out with its cross-reference table."""
    stream = f"BT /F1 12 Tf 72 720 Td ({text}) Tj ET".encode()
    objects = [b"<< /Type /Catalog /Pages 2 0 R >>", b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
               b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Contents 4 0 R"
               b" /Resources << /Font << /F1 5 0 R >> >> >>",
               b"<< /Length %d >>\nstream\n%s\nendstream" % (len(stream), stream),
               b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>"]
    out, offsets = bytearray(b"%PDF-1.4\n"), []
    for number, body in enumerate(objects, start=1):
        offsets.append(len(out))
        out += b"%d 0 obj\n%s\nendobj\n" % (number, body)
    table = len(out)
    out += b"xref\n0 %d\n0000000000 65535 f \n" % (len(objects) + 1)
    out += b"".join(b"%010d 00000 n \n" % offset for offset in offsets)
    out += b"trailer\n<< /Size %d /Root 1 0 R >>\nstartxref\n%d\n%%%%EOF\n" % (len(objects) + 1, table)
    return bytes(out)


def check_materials() -> dict:
    """Reading materials as the app does, each in its child process (backend/reading.py): a PDF read by
    PDFium (pypdfium2) into a passage and rendered as a page image, and LaTeX source read by
    pylatexenc; with the PDF reading's child's start (to ready) and peak footprint."""
    from backend import extraction, reading

    stats = {}
    with tempfile.TemporaryDirectory() as folder:
        stored = _stored(folder, _pdf("Scholia self-test"))
        pdf = reading.read(*stored, extraction.PDF, stats=stats)
        if [p.text for p in pdf.passages] != ["Scholia self-test"]:
            raise RuntimeError("the PDF's text was not read back")
        if not reading.render(*stored, 1, 0.5).startswith(b"\x89PNG"):
            raise RuntimeError("the PDF's page was not rendered")
        source = b"\\begin{document}\\section{Check}Self-test \\emph{text}.\\end{document}"
        latex = reading.read(*_stored(folder, source), extraction.LATEX)
    if [(p.section_path, p.text) for p in latex.passages] != [(["Check"], "Self-test text.")]:
        raise RuntimeError("the LaTeX source was not read back")
    return {"pdf": pdf.version, "latex": latex.version, "child_ready_seconds": round(stats["ready_seconds"], 3),
            "child_peak_mib": stats["peak_mib"]}


def check_reading_ceiling() -> dict:
    """A reading whose child passes its memory ceiling (1 MiB, below any child's footprint) is stopped:
    it ends memory_limit and no child is left."""
    from backend import extraction, reading

    with tempfile.TemporaryDirectory() as folder:
        try:
            reading.read(*_stored(folder, _pdf("Scholia self-test")), extraction.PDF, ceiling=1024 * 1024)
        except extraction.Unreadable as unreadable:
            if unreadable.code != "memory_limit":
                raise
        else:
            raise RuntimeError("a reading past its memory ceiling was not stopped")
    if reading.LIVE:
        raise RuntimeError("a reading's child was left")
    return {"reason": "memory_limit"}


def run(helper: Path, model: Path) -> dict:
    checks = {
        "sqlite": check_sqlite,
        "index": check_index,
        "backend": check_backend,
        "interface": check_interface,
        "encrypted_zip": check_encrypted_zip,
        "materials": check_materials,
        "reading_ceiling": check_reading_ceiling,
        "embedding": lambda: check_embedding(helper, model),
        "ocr": check_ocr,
    }
    results = {}
    for name, check in checks.items():
        try:
            results[name] = {"ok": True, **check()}
        except Exception as error:  # every check runs and reports, whatever fails
            results[name] = {"ok": False, "error": f"{type(error).__name__}: {error}"}
    return {"ok": all(r["ok"] for r in results.values()), "runtime": runtime(), "checks": results}


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(prog="Scholia")
    parser.add_argument("--self-test", action="store_true", required=True,
                        help="check the native pieces of this build and exit")
    parser.add_argument("--model", type=Path, required=True, help="the pinned embedding model")
    parser.add_argument("--helper", type=Path, default=Path(sys.executable).with_name("llama-server"),
                        help="llama-server (default: beside this executable)")
    args = parser.parse_args(argv)
    result = run(args.helper, args.model)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
