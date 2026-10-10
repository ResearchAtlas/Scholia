"""A reading child for tests: backend.reading's own child (main), with the parsing it does
(extraction.extract, or extraction.render_page for a page image) held or made to fail as its
arguments say (the conftest fixture reading_stub starts it in place of the real child):

    hold DIR [file]   report progress (0, 1), say it holds by a file DIR/held-<pid> (holding the media
                      type, or the page's number), and wait, calling stop(), until DIR/go exists; then
                      read for real. With file, it holds before it reads the stored file instead
    sleep SECONDS     wait SECONDS without a word; then read for real
    beat SECONDS      the real child, reporting while it works at most every SECONDS (BEAT_SECONDS)
    allocate MIB      touch MIB MiB and keep them; then read for real
    spike MIB         touch MIB MiB and free them; then read for real
    spike-fail MIB    touch MIB MiB and free them; then fail as an unreadable file would
    spike-frame MIB   touch MIB MiB and free them; then send a frame past MAX_FRAME
    exit CODE         exit with CODE at once (os._exit)
    quiet-exit S      say nothing for S seconds, then exit with 1 (os._exit): an end at any point of a tick
    signal NAME       kill itself with signal NAME (SIGSEGV: a crash in native code)
    stall             sleep without a word, for ever
    sentinel-dies DIR its sentinel ends (exit 7) where it would stop, before the child's wait for its stop;
                      it says so by a file DIR/sentinel, and every signal the child sends is listed in DIR/kills
    gil-stall DIR S   say it holds by a file DIR/held-<pid>, then stall S seconds in native code holding
                      the GIL (libc's sleep through ctypes.PyDLL), as a parser stuck in C would
    frame NAME        send a frame that is not one (FRAMES), then read for real
    after-done NAME   read for real, then after `done`: send a frame (beat) or exit non-zero (exit)
    done NAME         read for real, its `done` changed as NAME says (see Out.send)
    done-then-stall DIR  read for real, then once `done` is sent say so by a file DIR/held-<pid> and stall
                      60 s in native code holding the GIL
    png NAME          a page image whose PNG header says NAME: huge (past the page bounds), a pixel past
                      or two pixels past (MAX_PAGE_SIDE)
    version           name another extractor version in `ready`
    no-start          exit before `ready`
    canary TEXT       print TEXT to stdout and stderr, then abort
    probe DIR PORT    try a connection to 127.0.0.1:PORT, a name lookup and a process start, writing
                      how each went to DIR/probe; then read for real
    walkthrough DIR   the walkthrough server's child (tests/walkthrough.py): a file holding
                      WALKTHROUGH-MEMORY takes memory without end, one holding WALKTHROUGH-SLOW says
                      nothing for ever, so each is stopped as a crafted file would be; and this Mac's
                      OCR engine fails its first recognition of a page with dark ink, once (DIR/ocr-failed
                      records it across children); every other file is read for real
"""

import json
import os
import re
import signal
import socket
import struct
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from backend import extraction, ocr, reading  # noqa: E402

MODE, ARGS = sys.argv[1], sys.argv[2:]
FRAMES = {
    "oversized": None,  # a header past MAX_FRAME, and then more than the parent would hold
    "not-json": b"{not json",
    "wrong-fields": json.dumps({"passage": [1, 2, 3]}).encode(),
    "wrong-types": json.dumps({"passage": ["paragraph", 7, None, [], None, None, None]}).encode(),
    "bad-boxes": json.dumps({"passage": ["paragraph", "Text.", 1, [], 0, 5, {"rects": [[0, 0, 1]]}]}).encode(),
    "two-keys": json.dumps({"beat": 1, "progress": [0, 1]}).encode(),
    "unknown-error": json.dumps({"error": ["no_such_code"]}).encode(),
    "unknown-kind": json.dumps({"passage": ["poem", "Text.", None, [], 0, 5, None]}).encode(),
    "surrogate": json.dumps({"passage": ["paragraph", "Half \ud800 a pair.", None, [], 0, 5, None]}).encode(),
    "not-finite": json.dumps(
        {"passage": ["paragraph", "Text.", 1, [], 0, 5, {"rects": [[0, 0, float("nan"), 1]]}]}).encode(),
    "negative": json.dumps({"passage": ["paragraph", "Text.", -1, [], 0, 5, None]}).encode(),
    "past-int64": json.dumps({"passage": ["paragraph", "Text.", 2**64, [], 0, 5, None]}).encode(),
    "deep-path": json.dumps({"passage": ["paragraph", "Text.", None, ["Heading"] * 11, 0, 5, None]}).encode(),
    "empty-heading": json.dumps({"passage": ["paragraph", "Text.", None, [""] * 10, 0, 5, None]}).encode(),
    "long-heading": json.dumps({"passage": ["paragraph", "Text.", None, ["h" * 501], 0, 5, None]}).encode(),
    "nested": b'{"passage":' + b"[" * 100_000 + b"]" * 100_000 + b"}",
    "list-kind": json.dumps({"passage": [["paragraph"], "Text.", None, [], 0, 5, None]}).encode(),
    "list-error": json.dumps({"error": [["unreadable_file"]]}).encode(),
    "bigint-rect": b'{"passage":["paragraph","Text.",1,[],0,5,{"rects":[[0,0,1,1' + b"0" * 400 + b']]}]}',
    "no-page": json.dumps({"error": ["no_page"]}).encode(),
    # Values outside what the extraction promises (section 7.1), each for a PDF's reading unless named
    "long-passage": json.dumps({"passage": ["paragraph", "a" * 2001, 1, [], 0, 2001, None]}).encode(),
    "empty-passage": json.dumps({"passage": ["paragraph", "", 1, [], 0, 0, None]}).encode(),
    "page-zero": json.dumps({"passage": ["paragraph", "Text.", 0, [], 0, 5, None]}).encode(),
    "page-out-of-order": json.dumps({"passage": ["paragraph", "Text.", 2, [], 0, 5, None]}).encode(),  # page 1 after
    "no-page-in-a-pdf": json.dumps({"passage": ["paragraph", "Text.", None, [], 0, 5, None]}).encode(),
    "reversed-offsets": json.dumps({"passage": ["paragraph", "Text.", None, [], 9, 1, None]}).encode(),  # a text's
    "one-offset": json.dumps({"passage": ["paragraph", "Text.", 1, [], 3, None, None]}).encode(),
    "confidence-past-one": json.dumps(
        {"passage": ["paragraph", "Text.", 1, [], 0, 5, {"ocr": {"confidence": 2.0}}]}).encode(),
    "rectangle-backwards": json.dumps(
        {"passage": ["paragraph", "Text.", 1, [], 0, 5, {"rects": [[0.5, 0, 0.1, 1]]}]}).encode(),
    "more-rectangles-than-characters": json.dumps(
        {"passage": ["paragraph", "ab", 1, [], 0, 2, {"rects": [[0, 0, 1, 1]] * 3}]}).encode(),
    "progress-past-total": json.dumps({"progress": [3, 2]}).encode(),
    "page-in-a-text": json.dumps({"passage": ["paragraph", "Text.", 3, [], 0, 5, None]}).encode(),  # a text's reading
    "boxes-in-a-text": json.dumps(
        {"passage": ["paragraph", "Text.", None, [], 0, 5, {"rects": [[0, 0, 1, 1]]}]}).encode(),
}
real_extract, real_render, real_extractor_of = extraction.extract, extraction.render_page, extraction.extractor_of
real_file = reading._file
out = None  # the child's frames (reading._Out), once main has made it


class Out(reading._Out):
    def __init__(self, fd):
        global out
        super().__init__(fd)
        out = self

    def send(self, key, value, flush=True):
        if key == "done" and MODE == "done":  # what its done says, changed as ARGS[0] names
            pages, ocr_pages, status = value
            if ARGS[0] == "page-past-pages":  # a last passage past the document's pages, in page order
                super().send("passage", ["paragraph", "Text.", (pages or 0) + 1, [], 0, 5, None])
            value = {"page-past-pages": value, "scanned-past-pages": [pages, (pages or 0) + 5, status],
                     "ocr-needed-without-scans": [pages, 0, "ocr_needed"], "pages-in-a-text": [3, 0, status],
                     "scanned-in-a-text": [None, 1, status]}[ARGS[0]]
        super().send(key, value, flush)
        if key == "done" and MODE == "done-then-stall":
            import ctypes

            Path(ARGS[0], f"held-{os.getpid()}").write_text("done")
            ctypes.PyDLL(None).sleep(60)
        if key == "done" and MODE == "after-done":
            if ARGS[0] == "exit":
                self.stream.flush()
                os._exit(3)
            super().send("beat", 1)


def touch(mib):
    block = bytearray(mib << 20)
    block[::4096] = b"x" * len(range(0, len(block), 4096))
    return block


def hold(what, stop=lambda: None, progress=lambda done, total: None):
    folder = Path(ARGS[0])
    progress(0, 1)
    (folder / f"held-{os.getpid()}").write_text(str(what))
    while not (folder / "go").exists():
        stop()
        time.sleep(0.01)


def before(what, stop, progress, data=b""):
    """What the mode does where the child would parse."""
    if MODE == "walkthrough" and b"WALKTHROUGH-MEMORY" in data:
        kept = []
        while True:
            kept.append(touch(64))
    elif MODE == "walkthrough" and b"WALKTHROUGH-SLOW" in data:
        while True:
            time.sleep(1)
    elif MODE == "hold":
        if ARGS[1:] != ["file"]:
            hold(what, stop, progress)
    elif MODE == "sleep":
        time.sleep(float(ARGS[0]))
    elif MODE == "allocate":
        before.kept = touch(int(ARGS[0]))
    elif MODE in ("spike", "spike-fail", "spike-frame"):
        touch(int(ARGS[0]))
        if MODE == "spike-fail":
            raise extraction.Unreadable()
        if MODE == "spike-frame":
            out.raw(b"x" * (reading.MAX_FRAME + 1))
    elif MODE == "exit":
        os._exit(int(ARGS[0]))
    elif MODE == "quiet-exit":
        time.sleep(float(ARGS[0]))
        os._exit(1)
    elif MODE == "signal":
        os.kill(os.getpid(), getattr(signal, ARGS[0]))
    elif MODE == "stall":
        while True:
            time.sleep(1)
    elif MODE == "gil-stall":
        import ctypes

        Path(ARGS[0], f"held-{os.getpid()}").write_text(str(what))
        ctypes.PyDLL(None).sleep(int(ARGS[1]))  # PyDLL: the GIL is held through the call
    elif MODE == "frame":
        if ARGS[0] == "oversized":
            out.stream.write(struct.pack(">I", 0xFFFFFFFF))
            out.stream.write(b"\x00" * (64 << 20))
        else:
            out.raw(FRAMES[ARGS[0]])
    elif MODE == "canary":
        print(ARGS[0], flush=True)
        os.write(2, ARGS[0].encode())
        os.abort()
    elif MODE == "probe":
        import _socket

        found, port, by_name = {}, int(ARGS[1]), ("localhost", int(ARGS[1]))  # a name, resolved without a network
        udp = (socket.AF_INET, socket.SOCK_DGRAM)
        attempts = {"connect": lambda: socket.socket().connect(("127.0.0.1", port)),
                    "connect by name": lambda: socket.socket().connect(by_name),
                    "connect_ex by name": lambda: socket.socket().connect_ex(by_name),
                    "sendto by name": lambda: socket.socket(*udp).sendto(b"x", by_name),
                    "sendmsg by name": lambda: socket.socket(*udp).sendmsg([b"x"], [], 0, by_name),
                    "the C class by name": lambda: _socket.socket().connect(by_name),
                    "lookup": lambda: socket.getaddrinfo("example.com", 80),
                    "process": lambda: subprocess.run(["/usr/bin/true"], check=True)}
        for name, attempt in attempts.items():
            try:
                attempt()
                found[name] = "allowed"
            except PermissionError as error:  # the audit event that refused it, from its message
                found[name] = str(error).rsplit("(", 1)[-1].rstrip(")")
            except OSError as error:
                found[name] = type(error).__name__
        Path(ARGS[0], "probe").write_text(json.dumps(found))


def extract(data, kind, stop=lambda: None, progress=lambda done, total: None):
    before(kind, stop, progress, data)
    return real_extract(data, kind, stop, progress)


def render_page(data, number, scale=2.0):
    before(number, lambda: None, lambda done, total: None)
    if MODE == "png":  # a PNG whose header says an image of a size ARGS[0] names
        width, height = {"huge": (30_000, 30_000), "a pixel past": (extraction.MAX_PAGE_SIDE + 1, 1),
                         "two pixels past": (extraction.MAX_PAGE_SIDE + 2, 1)}[ARGS[0]]
        return reading._PNG + struct.pack(">I", 13) + b"IHDR" + struct.pack(">IIBBBBB", width, height, 8, 6, 0, 0, 0)
    return real_render(data, number, scale)


def stored_file(path, sha256):
    if MODE == "hold" and ARGS[1:] == ["file"]:
        hold("file")
    return real_file(path, sha256)


def extractor_of(kind):
    if MODE == "no-start":
        os._exit(1)
    name, version = real_extractor_of(kind)
    return name, version + ("-other" if MODE == "version" else "")


class FailsOnce:
    """This Mac's OCR engine, whose first recognition of a page with dark ink fails, once (the
    walkthrough's scanned appendix is an even gray); its version is the engine's."""

    def __init__(self, engine):
        self.engine, self.version = engine, engine.version

    def recognize(self, bitmap):
        failed = Path(ARGS[0], "ocr-failed")
        if not failed.exists() and re.search(rb"[\x00-\x3f]", bitmap.pixels):
            failed.touch()
            raise ocr.Failed()
        return self.engine.recognize(bitmap)


if MODE == "sentinel-dies":
    real_kill = os.kill

    def kill(pid, sig):
        if pid == os.getpid() and sig == signal.SIGSTOP:  # in the sentinel, as it would stop: it ends instead
            Path(ARGS[0], "sentinel").write_text(str(pid))
            os._exit(7)
        with open(Path(ARGS[0], "kills"), "a") as kills:
            kills.write(f"{pid} {sig}\n")
        return real_kill(pid, sig)

    os.kill = kill

if MODE == "beat":  # the real child, its reports while it works closer together; nothing else changed
    reading.BEAT_SECONDS = float(ARGS[0])
else:
    extraction.extract, extraction.render_page, extraction.extractor_of = extract, render_page, extractor_of
if MODE == "walkthrough" and (engine := ocr.engine()) is not None:
    failing = FailsOnce(engine)
    ocr.engine = lambda: failing
reading._Out, reading._file = Out, stored_file

if __name__ == "__main__":
    sys.exit(reading.main())
