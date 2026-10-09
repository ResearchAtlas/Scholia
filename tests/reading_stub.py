"""A reading child for tests: backend.reading's own child (main), with the parsing it does
(extraction.extract, or extraction.render_page for a page image) held or made to fail as its
arguments say (the conftest fixture reading_stub starts it in place of the real child):

    hold DIR [file]   report progress (0, 1), say it holds by a file DIR/held-<pid> (holding the media
                      type, or the page's number), and wait, calling stop(), until DIR/go exists; then
                      read for real. With file, it holds before it reads the stored file instead
    sleep SECONDS     wait SECONDS without a word; then read for real
    allocate MIB      touch MIB MiB and keep them; then read for real
    spike MIB         touch MIB MiB and free them; then read for real
    spike-fail MIB    touch MIB MiB and free them; then fail as an unreadable file would
    spike-frame MIB   touch MIB MiB and free them; then send a frame past MAX_FRAME
    exit CODE         exit with CODE at once (os._exit)
    signal NAME       kill itself with signal NAME (SIGSEGV: a crash in native code)
    stall             sleep without a word, for ever
    frame NAME        send a frame that is not one (FRAMES), then read for real
    after-done NAME   read for real, then after `done`: send a frame (beat) or exit non-zero (exit)
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
    "not-finite": json.dumps({"passage": ["paragraph", "Text.", 1, [], 0, 5, {"rects": [[0, 0, float("nan"), 1]]}]}).encode(),
    "negative": json.dumps({"passage": ["paragraph", "Text.", -1, [], 0, 5, None]}).encode(),
    "past-int64": json.dumps({"passage": ["paragraph", "Text.", 2**64, [], 0, 5, None]}).encode(),
    "deep-path": json.dumps({"passage": ["paragraph", "Text.", None, ["Heading"] * 11, 0, 5, None]}).encode(),
    "empty-heading": json.dumps({"passage": ["paragraph", "Text.", None, [""] * 10, 0, 5, None]}).encode(),
    "long-heading": json.dumps({"passage": ["paragraph", "Text.", None, ["h" * 501], 0, 5, None]}).encode(),
    "nested": b'{"passage":' + b"[" * 100_000 + b"]" * 100_000 + b"}",
    "list-kind": json.dumps({"passage": [["paragraph"], "Text.", None, [], 0, 5, None]}).encode(),
    "list-error": json.dumps({"error": [["unreadable_file"]]}).encode(),
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
        super().send(key, value, flush)
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
    elif MODE == "signal":
        os.kill(os.getpid(), getattr(signal, ARGS[0]))
    elif MODE == "stall":
        while True:
            time.sleep(1)
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
        found = {}
        attempts = {"connect": lambda: socket.socket().connect(("127.0.0.1", int(ARGS[1]))),
                    "lookup": lambda: socket.getaddrinfo("example.com", 80),
                    "process": lambda: subprocess.run(["/usr/bin/true"], check=True)}
        for name, attempt in attempts.items():
            try:
                attempt()
                found[name] = "allowed"
            except PermissionError:
                found[name] = "refused"
            except OSError as error:
                found[name] = type(error).__name__
        Path(ARGS[0], "probe").write_text(json.dumps(found))


def extract(data, kind, stop=lambda: None, progress=lambda done, total: None):
    before(kind, stop, progress, data)
    return real_extract(data, kind, stop, progress)


def render_page(data, number, scale=2.0):
    before(number, lambda: None, lambda done, total: None)
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


extraction.extract, extraction.render_page, extraction.extractor_of = extract, render_page, extractor_of
if MODE == "walkthrough" and (engine := ocr.engine()) is not None:
    failing = FailsOnce(engine)
    ocr.engine = lambda: failing
reading._Out, reading._file = Out, stored_file

if __name__ == "__main__":
    sys.exit(reading.main())
