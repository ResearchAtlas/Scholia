"""The packaged app's self-test.

The app runs it with `--self-test --model <embedding model>`. It checks, inside the frozen
build, the native pieces the app ships: SQLite, FTS5 secure-delete, extension loading and
sqlite-vec through APSW, one embedding through the llama.cpp helper, and one OCR page
through Vision. It prints the results as JSON and exits non-zero when any check fails.

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
from pathlib import Path

MIN_SQLITE = (3, 42, 0)  # the first SQLite with FTS5 secure-delete
SQLITE_VEC_VERSION = "v0.1.9"
DIMENSIONS = 1024

# Qwen's own Q8_0 GGUF of Qwen3-Embedding-0.6B (Apache-2.0), at a pinned revision.
EMBEDDING_MODEL = {
    "file": "Qwen3-Embedding-0.6B-Q8_0.gguf",
    "size": 639_150_592,
    "sha256": "06507c7b42688469c4e7298b0a1e16deff06caf291cf0a5b278c308249c3e439",
    "url": "https://huggingface.co/Qwen/Qwen3-Embedding-0.6B-GGUF/resolve/"
    "370f27d7550e0def9b39c1f16d3fbaa13aa67728/Qwen3-Embedding-0.6B-Q8_0.gguf",
}
# A bound for a cold start, not the app's start deadline, which is a separate setting: the
# first start on a CI runner's virtual GPU spent about 35 s preparing Metal before the model loaded.
HELPER_START_SECONDS = 120
# Per helper process: context, physical batch and slots.
HELPER_LIMITS = ["-c", "4096", "-ub", "2048", "-np", "2"]
LISTENING = re.compile(r"listening on http://127\.0\.0\.1:(\d+)")
# The OCR page: one English and one Simplified Chinese line.
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


def mismatch(path: Path, pin: dict) -> str | None:
    """Why the file at `path` differs from `pin` (its size and SHA-256), or None."""
    size = path.stat().st_size
    if size != pin["size"]:
        return f"{path.name}: {size} bytes, expected {pin['size']}"
    digest = hashlib.sha256()
    with open(path, "rb") as f:
        while block := f.read(1 << 20):
            digest.update(block)
    if digest.hexdigest() != pin["sha256"]:
        return f"{path.name}: SHA-256 {digest.hexdigest()}, expected {pin['sha256']}"
    return None


def verify_model(path: Path) -> None:
    """Refuse a model file that differs from the pin."""
    if error := mismatch(path, EMBEDDING_MODEL):
        raise RuntimeError(error)


def helper_command(helper: Path, model: Path) -> list[str]:
    return [
        str(helper), "-m", str(model), "--offline", "--host", "127.0.0.1", "--port", "0",
        "--no-webui", "--embedding", "--pooling", "last", *HELPER_LIMITS,
    ]


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
        if match := LISTENING.search(line):
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


def render_page(lines=OCR_LINES, width=1600, height=420):
    """A page image (CGImage) with `lines` drawn in black on white."""
    import AppKit
    import Quartz  # noqa: F401  (registers the CGImage type that rep.CGImage() returns)

    rep = AppKit.NSBitmapImageRep.alloc().initWithBitmapDataPlanes_pixelsWide_pixelsHigh_bitsPerSample_samplesPerPixel_hasAlpha_isPlanar_colorSpaceName_bytesPerRow_bitsPerPixel_(  # noqa: E501
        None, width, height, 8, 4, True, False, AppKit.NSDeviceRGBColorSpace, 0, 0
    )
    context = AppKit.NSGraphicsContext.graphicsContextWithBitmapImageRep_(rep)
    AppKit.NSGraphicsContext.saveGraphicsState()
    try:
        AppKit.NSGraphicsContext.setCurrentContext_(context)
        AppKit.NSColor.whiteColor().set()
        AppKit.NSRectFill(AppKit.NSMakeRect(0, 0, width, height))
        attributes = {
            AppKit.NSFontAttributeName: AppKit.NSFont.systemFontOfSize_(64),
            AppKit.NSForegroundColorAttributeName: AppKit.NSColor.blackColor(),
        }
        for i, line in enumerate(lines):
            AppKit.NSString.stringWithString_(line).drawAtPoint_withAttributes_(
                AppKit.NSMakePoint(80, height - 140 - i * 140), attributes
            )
        context.flushGraphics()
    finally:
        AppKit.NSGraphicsContext.restoreGraphicsState()
    return rep.CGImage()


def recognize(image) -> list[tuple[str, float]]:
    """Vision's text lines in `image`, accurate level, Simplified Chinese first."""
    import Foundation
    import Vision

    request = Vision.VNRecognizeTextRequest.alloc().init()
    request.setRecognitionLevel_(Vision.VNRequestTextRecognitionLevelAccurate)
    request.setRecognitionLanguages_(["zh-Hans", "en-US"])
    handler = Vision.VNImageRequestHandler.alloc().initWithCGImage_options_(
        image, Foundation.NSDictionary.dictionary()
    )
    ok, error = handler.performRequests_error_([request], None)
    if not ok:
        raise RuntimeError(f"Vision failed: {error}")
    return [
        (str(candidate.string()), float(candidate.confidence()))
        for candidate in (obs.topCandidates_(1)[0] for obs in request.results())
    ]


def check_ocr() -> dict:
    import objc

    with objc.autorelease_pool():
        found = recognize(render_page())
    text = "".join(line for line, _ in found).replace(" ", "")
    missing = [line for line in OCR_LINES if line.replace(" ", "") not in text]
    if missing:
        raise RuntimeError(f"OCR missed {missing}; read {[line for line, _ in found]}")
    return {"lines": [line for line, _ in found], "min_confidence": round(min(c for _, c in found), 3)}


def run(helper: Path, model: Path) -> dict:
    checks = {
        "sqlite": check_sqlite,
        "index": check_index,
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
