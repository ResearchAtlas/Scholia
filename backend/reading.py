"""Reading a material in a child process, under a memory ceiling and a time ceiling (slice-1 spec
sections 1.5, 7.1 and 13; ticket 60 B1, Harold's decisions of 2026-10-09).

Each reading (an extract run) and each page image parses its file in a child process of its own,
started for it and ended with it: `read` and `render` are the parent's side, `main` the child's.
The child reads the stored file itself, checks it against its hash, parses it with
backend/extraction.py, whose per-parser bounds stay as defence in depth, and sends back what it
found in frames. The parent keeps everything that touches the database, so a child that is killed
or dies leaves nothing written, and it never holds the file's bytes.

The parent's thread watches its child every WATCH_SECONDS, until the child has exited and been
reaped:
- stop(), the caller's check: Cancel, a revocation, the 30-minute limit and shutdown raise there;
  the child is killed and reaped, then that is raised.
- memory: the kernel's record of the child's largest physical footprint (proc_pid_rusage), which
  keeps a peak that came and went between two looks. Past the ceiling the child is killed (SIGKILL)
  and the reading fails memory_limit; the peak is read once more after the child exits, before it
  is reaped, so the same file always gets the same outcome. macOS enforces neither RLIMIT_AS nor
  RLIMIT_DATA, hence the watch. A footprint that cannot be read ends the child (the watch fails
  closed).
- time: a child reports at every page or block it reads (and at least every BEAT_SECONDS while it
  reports at all). One that sends nothing for STEP_SECONDS is stuck in one step, as pylatexenc's
  tokenizer is on a long run of plain text (its time grows with the run's square); it is killed and
  the reading fails step_limit. A whole reading may take longer, as a long scanned book read page
  by page does: the 30-minute limit (backend/materials.py) stays the outer bound.

Frames are a 4-byte big-endian length and a body: a JSON object with one key (ASCII), or a page
image's PNG. The child sends `ready` (the extractor and version it computed after importing its
parser), then `progress` and `beat` as it works, then a reading's passages, one a frame, and `done`,
or a page's PNG, or `error`. The parent refuses a frame from its header when it is longer than
MAX_FRAME (MAX_PNG for a page image), checks every field's type, and counts a reading's passages,
text, rectangles and section paths as they arrive against what S1-13's bounds allow, so its buffer
never holds more than one frame and one read, and what it keeps never exceeds the reading's
bounds. Anything else ends the child. A result counts only after `done`, a zero exit and a peak at
or under the ceiling.

The child refuses, in an audit hook installed before it reads its request, every connection, name
lookup and process start; it writes its frames to a copy of its stdout and points stdout and
stderr at /dev/null, so nothing a library prints reaches the parent; and it exits as soon as its
stdin ends, which is when the parent dies, however it dies. Its environment is HOME and TMPDIR
only. It never imports the database, the server or the window. Logs carry codes, counts and MiB,
never a path, a name, a hash or text.
"""

import contextlib
import ctypes
import hashlib
import json
import logging
import os
import re
import selectors
import struct
import subprocess
import sys
import threading
import time
import traceback
from pathlib import Path

from backend import extraction

log = logging.getLogger(__name__)

# The ceilings: see the PR's measurements (largest accepted shapes, with Vision for scanned pages).
READING_CEILING = 1024 * 1024 * 1024  # bytes of physical footprint, a reading's child at its peak
RENDER_CEILING = 512 * 1024 * 1024  # a page image's child
STEP_SECONDS = 120.0  # a child that sends nothing for this long is stuck in one step
WATCH_SECONDS = 0.01
BEAT_SECONDS = 1.0  # the child's report while it works, at most this far apart
MAX_REQUEST = 64 * 1024
MAX_FRAME = 1024 * 1024  # a JSON frame: a passage's is some hundreds of KiB at most (2,000 characters, a
# section path of ten headings of 500, a rectangle for each line)
# A page image's frame: its RGBA rows, a filter byte a row, and room for zlib's and the chunks' overhead.
MAX_PNG = extraction.MAX_PAGE_PIXELS * 4 + extraction.MAX_PAGE_SIDE + 1024 * 1024
# A reading's passages at most: a block's pieces but its last are each longer than a quarter of
# MAX_PASSAGE (extraction._split), and every passage's text counts against MAX_TEXT_CHARS.
MAX_PASSAGES = extraction.MAX_BLOCKS + extraction.MAX_TEXT_CHARS // (extraction.MAX_PASSAGE // 4)
_PNG = b"\x89PNG\r\n\x1a\n"
_ROOT = Path(__file__).resolve().parents[1]
LIVE = set()  # the pids of children not yet reaped
_live_lock = threading.Lock()


class ChildError(RuntimeError):
    """The child failed as no file makes it fail: it did not start, ended before it was ready, named
    another extractor version, or met an error it did not expect. The run fails internal."""


def command():
    """The child's command: the packaged app's own executable (tools/app.py), or this module."""
    if getattr(sys, "frozen", False):
        return [sys.executable, "--read-material"]
    return [sys.executable, "-m", "backend.reading"]


def read(path, sha256, kind, stop=lambda: None, progress=lambda done, total: None, *, ceiling=None, stats=None):
    """The stored file at path (its SHA-256 sha256) read as media type kind, in a child: an
    extraction.Extracted equal to extraction.extract's. Raises extraction.Unreadable (its codes,
    and memory_limit or step_limit), FileNotFoundError for a missing or changed file, ChildError,
    or what stop() raised. stats, a dict, gets the child's start (to ready), its peak footprint and its
    longest silence between two frames."""
    received = _Reading(kind, progress)
    request = {"op": "read", "path": str(path), "sha256": sha256, "kind": kind}
    received.exit = _run(request, ceiling or READING_CEILING, MAX_FRAME, stop, received, stats)
    return received.result()


def render(path, sha256, number, scale, stop=lambda: None, *, ceiling=None, stats=None):
    """Page number of the stored PDF at path as a PNG, rendered in a child as extraction.render_page
    does. Raises IndexError for a page it does not have, and otherwise as read."""
    received = _Render()
    request = {"op": "render", "path": str(path), "sha256": sha256, "number": number, "scale": scale}
    received.exit = _run(request, ceiling or RENDER_CEILING, MAX_PNG, stop, received, stats)
    return received.result()


# The parent


class _Usage(ctypes.Structure):  # struct rusage_info_v4, <sys/resource.h>: a UUID, then 35 counters
    _fields_ = [("uuid", ctypes.c_uint8 * 16), ("counters", ctypes.c_uint64 * 35)]


_RUSAGE_INFO_V4 = 4
_LIFETIME_MAX_PHYS_FOOTPRINT = 28  # ri_lifetime_max_phys_footprint
_libproc = None


def _peak(pid):
    """The child's largest physical footprint so far, in bytes, as the kernel records it (readable
    until the child is reaped), or None when it cannot be read."""
    global _libproc
    if _libproc is None:
        _libproc = ctypes.CDLL("/usr/lib/libproc.dylib", use_errno=True)
    usage = _Usage()
    if _libproc.proc_pid_rusage(pid, _RUSAGE_INFO_V4, ctypes.byref(usage)) != 0:
        return None
    return usage.counters[_LIFETIME_MAX_PHYS_FOOTPRINT]


def _exited(pid):
    """Whether the child has exited, leaving it to be reaped (its footprint stays readable)."""
    return os.waitid(os.P_PID, pid, os.WEXITED | os.WNOHANG | os.WNOWAIT) is not None


def _frame(buffer, limit):
    """The next whole frame's body, taken off buffer, or None; past limit, refused from its header."""
    if len(buffer) < 4:
        return None
    size = int.from_bytes(buffer[:4], "big")
    if size > limit:
        raise _Bad("a frame past its bound")
    if len(buffer) < 4 + size:
        return None
    with memoryview(buffer) as view:
        body = view[4:4 + size].tobytes()
    del buffer[:4 + size]
    return body


class _Bad(Exception):
    """A frame that is not one; its text is a code, never content."""


def _run(request, ceiling, limit, stop, received, stats):
    """Start the child, send it request, and watch it to its end (see the module's docstring), giving
    each frame to received.take. Returns its exit status; the child is always reaped."""
    started = time.monotonic()
    try:
        child = subprocess.Popen(command(), stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                                 env={k: os.environ[k] for k in ("HOME", "TMPDIR") if k in os.environ},
                                 cwd=None if getattr(sys, "frozen", False) else _ROOT)
    except OSError as error:
        log.error("a reading's child did not start (%s)", type(error).__name__)
        raise ChildError("start") from None
    with _live_lock:
        LIVE.add(child.pid)
    selector, peak, longest = selectors.DefaultSelector(), 0, 0.0
    try:
        body = json.dumps(request).encode()
        try:  # its stdin stays open: it ends when this process does
            child.stdin.write(struct.pack(">I", len(body)) + body)
            child.stdin.flush()
        except OSError:
            pass  # it ended already: its output ends before ready
        fd = child.stdout.fileno()
        selector.register(fd, selectors.EVENT_READ)
        buffer, last, ended = bytearray(), time.monotonic(), False
        while True:
            stop()
            peak = _peak(child.pid)
            if peak is None:
                log.error("a reading's child's memory could not be read; it was stopped")
                raise ChildError("watch")
            if peak > ceiling:
                raise _Ceiling(peak)
            if ended:
                if _exited(child.pid):
                    break
                time.sleep(WATCH_SECONDS)
            elif selector.select(WATCH_SECONDS):
                chunk = os.read(fd, 1 << 16)
                if not chunk:
                    ended = True
                    continue
                buffer += chunk
                while (body := _frame(buffer, limit)) is not None:
                    if stats is not None and "ready_seconds" not in stats:
                        stats["ready_seconds"] = time.monotonic() - started
                    received.take(body)
                    longest, last = max(longest, time.monotonic() - last), time.monotonic()
            if time.monotonic() - last > STEP_SECONDS:
                log.warning("a reading's child sent nothing for %d s; it was stopped", STEP_SECONDS)
                raise extraction.Unreadable("step_limit")
        peak = _peak(child.pid)  # its last word, read before it is reaped
        if peak is None:
            raise ChildError("watch")
        if peak > ceiling:
            raise _Ceiling(peak)
        if buffer:
            raise _Bad("a frame cut short")
        return child.wait()
    except _Ceiling as passed:
        log.warning("a reading's child passed its memory ceiling (%d MiB at %d MiB); it was stopped",
                    ceiling >> 20, passed.peak >> 20)
        raise extraction.Unreadable("memory_limit") from None
    except _Bad as bad:
        raise received.bad(str(bad)) from None
    finally:
        if child.returncode is None:
            with contextlib.suppress(ProcessLookupError):
                child.kill()
            child.wait()
        for stream in (child.stdin, child.stdout):
            with contextlib.suppress(OSError):
                stream.close()
        selector.close()
        with _live_lock:
            LIVE.discard(child.pid)
        if stats is not None:
            stats.update(seconds=time.monotonic() - started, peak_mib=round(peak / 2**20, 1) if peak else None,
                         longest_step_seconds=longest)


class _Ceiling(Exception):
    def __init__(self, peak):
        super().__init__()
        self.peak = peak


def _message(body):
    """A JSON frame as (key, value), or _Bad."""
    if len(body) > MAX_FRAME or body[:1] != b"{":
        raise _Bad("a frame that is not JSON")
    try:
        message = json.loads(body)
    except ValueError:
        raise _Bad("a frame that is not JSON") from None
    if type(message) is not dict or len(message) != 1:
        raise _Bad("a frame of other fields")
    return next(iter(message.items()))


def _number(value):
    return type(value) in (int, float)


def _optional_int(value):
    return value is None or type(value) is int


_ERRORS = {"unreadable_file", "encrypted_file", "ocr_failed", "file_missing", "no_page", "internal"}
_TYPE = re.compile(r"[A-Za-z_][A-Za-z0-9_.]{0,99}")
_WHERE = re.compile(r"[A-Za-z0-9_.-]{1,100}:\d{1,7}")


class _Received:
    """What a child has sent, frame by frame. A wrong frame before `ready` is the child's own
    failure (ChildError); after it, the file's (unreadable_file)."""

    def __init__(self, kind):
        self.expected = list(extraction.extractor_of(kind))
        self.ready = False
        self.exit = None

    def bad(self, why):
        log.warning("a reading's child sent %s; it was stopped", why)
        return extraction.Unreadable() if self.ready else ChildError("frame")

    def started(self, key, value):
        """The first frame: ready, naming the extractor version the parent reads as, unless the child
        failed as it started (an internal error, as when its parser does not import)."""
        if key == "error" and type(value) is list and value[:1] == ["internal"]:
            self.failed(value)
        if key != "ready":
            raise _Bad("a frame before ready")
        if value != self.expected:
            log.error("a reading's child names another extractor version")
            raise ChildError("version")
        self.ready = True

    def failed(self, value):
        if type(value) is not list or not value or value[0] not in _ERRORS:
            raise _Bad("an error that is not one")
        code = value[0]
        if code == "file_missing":
            raise FileNotFoundError("the stored file is missing or changed")
        if code == "no_page":
            raise IndexError("no such page")
        if code == "internal":
            kind = value[1] if len(value) > 1 and type(value[1]) is str and _TYPE.fullmatch(value[1]) else "unknown"
            where = value[2] if len(value) > 2 and type(value[2]) is str and _WHERE.fullmatch(value[2]) else "unknown"
            log.error("a reading's child failed unexpectedly (%s at %s)", kind, where)
            raise ChildError("internal")
        raise extraction.Unreadable(code)

    def ended(self):
        """Raised where the child ended without its result: before ready, ChildError."""
        if not self.ready:
            log.error("a reading's child ended before it was ready (exit %s)", self.exit)
            raise ChildError("ended")
        log.warning("a reading's child ended before its result (exit %s)", self.exit)
        raise extraction.Unreadable()


class _Reading(_Received):
    def __init__(self, kind, progress):
        super().__init__(kind)
        self.progress, self.passages, self.done = progress, [], None
        self.chars = self.rects = self.built = 0
        self.path = []  # the last passage's section path, which the next one shares when it is the same

    def take(self, body):
        key, value = _message(body)
        if not self.ready:
            return self.started(key, value)
        if self.done is not None:
            raise _Bad("a frame after done")
        if key == "beat":
            return
        if key == "progress":
            if type(value) is not list or len(value) != 2 or not all(type(v) is int and v >= 0 for v in value):
                raise _Bad("a progress that is not one")
            return self.progress(*value)
        if key == "passage":
            return self.passages.append(self._passage(value))
        if key == "done":
            if type(value) is not list or len(value) != 3 or not _optional_int(value[0]) \
                    or type(value[1]) is not int or value[1] < 0 or value[2] not in ("complete", "ocr_needed"):
                raise _Bad("a done that is not one")
            self.done = value
            return
        if key == "error":
            return self.failed(value)
        raise _Bad("a frame of other fields")

    def _passage(self, value):
        """A passage, its fields' types checked and counted against the reading's bounds."""
        if type(value) is not list or len(value) != 7:
            raise _Bad("a passage that is not one")
        kind, text, page, path, start, end, boxes = value
        if type(kind) is not str or len(kind) > 32 or type(text) is not str or not _optional_int(page) \
                or type(path) is not list or not all(type(p) is str for p in path) \
                or not _optional_int(start) or not _optional_int(end):
            raise _Bad("a passage that is not one")
        rects = self._boxes(boxes)
        self.chars += len(text)
        self.built += sum(map(len, path))
        self.rects += rects
        if len(self.passages) >= MAX_PASSAGES or self.chars > extraction.MAX_TEXT_CHARS \
                or self.built > extraction.MAX_BUILT_CHARS or self.rects > extraction.MAX_RECTS:
            raise _Bad("more than a reading's bounds allow")
        if path == self.path:
            path = self.path
        self.path = path
        return extraction.Passage(kind, text, page, path, start, end, boxes)

    @staticmethod
    def _boxes(boxes):
        """The number of rectangles in a passage's boxes, their shape checked: None, or rectangles
        (four numbers each) and a recognized passage's confidence."""
        if boxes is None:
            return 0
        if type(boxes) is not dict or not boxes or not set(boxes) <= {"rects", "ocr"}:
            raise _Bad("boxes that are not boxes")
        rects = boxes.get("rects", [])
        if type(rects) is not list or not all(type(r) is list and len(r) == 4 and all(map(_number, r)) for r in rects):
            raise _Bad("boxes that are not boxes")
        if "ocr" in boxes and (type(boxes["ocr"]) is not dict or list(boxes["ocr"]) != ["confidence"]
                               or not _number(boxes["ocr"]["confidence"])):
            raise _Bad("boxes that are not boxes")
        return len(rects)

    def result(self):
        if self.done is None or self.exit != 0:
            self.ended()
        pages, ocr_pages, status = self.done
        return extraction.Extracted(*self.expected, self.passages, pages, ocr_pages, status)


class _Render(_Received):
    def __init__(self):
        super().__init__(extraction.PDF)
        self.image = None

    def take(self, body):
        if self.ready and self.image is None and body.startswith(_PNG):
            self.image = body
            return
        key, value = _message(body)
        if not self.ready:
            return self.started(key, value)
        if self.image is not None:
            raise _Bad("a frame after the image")
        if key == "beat":
            return
        if key == "error":
            return self.failed(value)
        raise _Bad("a frame of other fields")

    def result(self):
        if self.image is None or self.exit != 0:
            self.ended()
        return self.image


# The child


_REFUSED = {"socket.connect", "socket.sendto", "socket.sendmsg", "socket.getaddrinfo", "socket.gethostbyname",
            "socket.gethostbyaddr", "socket.getnameinfo", "subprocess.Popen", "os.system", "os.exec",
            "os.posix_spawn", "os.spawn", "os.fork", "os.forkpty", "pty.spawn"}


def _refuse(event, args):
    if event in _REFUSED:
        raise PermissionError(f"a reading opens no connection and starts no process ({event})")


class _Out:
    """The child's frames, on a copy of its stdout."""

    def __init__(self, fd):
        self.stream, self.sent = os.fdopen(fd, "wb", buffering=1 << 16), time.monotonic()

    def send(self, key, value, flush=True):
        self.raw(json.dumps({key: value}, separators=(",", ":")).encode(), flush)

    def raw(self, body, flush=True):
        self.stream.write(struct.pack(">I", len(body)))
        self.stream.write(body)
        if flush:
            self.stream.flush()
            self.sent = time.monotonic()

    def beat(self):
        """stop() for the extractor: nothing stops it here (the parent ends the child), but it says
        the reading moves, at most every BEAT_SECONDS."""
        if time.monotonic() - self.sent >= BEAT_SECONDS:
            self.send("beat", 1)


def _where(error):
    """Where an error was raised, as file:line, without its message (backend.runs._where)."""
    frames = traceback.extract_tb(error.__traceback__)
    return f"{frames[-1].filename.rsplit('/', 1)[-1]}:{frames[-1].lineno}" if frames else "unknown"


def _request():
    """The parent's one request from stdin: a length and a JSON object."""
    data = b""
    while len(data) < 4 or len(data) < 4 + int.from_bytes(data[:4], "big"):
        if len(data) >= 4 and int.from_bytes(data[:4], "big") > MAX_REQUEST:
            raise ValueError("the request is too long")
        chunk = os.read(0, 4096)
        if not chunk:
            raise EOFError("no request")
        data += chunk
    return json.loads(data[4:4 + int.from_bytes(data[:4], "big")])


def _orphaned():
    """Read stdin to its end, which comes when the parent closes it or dies, then exit at once."""
    while os.read(0, 4096):
        pass
    os._exit(0)


def _file(path, sha256):
    """The stored file's bytes, checked against their hash (as ContentStore.read), or None."""
    try:
        data = Path(path).read_bytes()
    except FileNotFoundError:
        return None
    return data if hashlib.sha256(data).hexdigest() == sha256 else None


def main():
    """The child: one request, one reading or page image, its frames on stdout."""
    sys.addaudithook(_refuse)
    out = _Out(os.dup(1))
    quiet = os.open(os.devnull, os.O_WRONLY)
    os.dup2(quiet, 1)
    os.dup2(quiet, 2)
    os.close(quiet)
    request = _request()
    threading.Thread(target=_orphaned, daemon=True).start()
    try:
        kind = request["kind"] if request["op"] == "read" else extraction.PDF
        out.send("ready", list(extraction.extractor_of(kind)))
        data = _file(request["path"], request["sha256"])
        if data is None:
            out.send("error", ["file_missing"])
        elif request["op"] == "read":
            extracted = extraction.extract(data, kind, out.beat,
                                           lambda done, total: out.send("progress", [done, total]))
            data = None
            for p in extracted.passages:
                out.send("passage", [p.kind, p.text, p.page, p.section_path, p.char_start, p.char_end, p.boxes],
                         flush=False)
            out.send("done", [extracted.pages, extracted.ocr_pages, extracted.status])
        else:
            try:
                image = extraction.render_page(data, request["number"], request["scale"])
            except IndexError:  # render_page's own: no such page
                out.send("error", ["no_page"])
            else:
                data = None
                out.raw(image)
    except extraction.Unreadable as unreadable:
        out.send("error", [unreadable.code])
    except Exception as error:  # a defect, not the file: logged by the parent as its type and place
        out.send("error", ["internal", type(error).__name__, _where(error)])
    return 0


if __name__ == "__main__":
    sys.exit(main())
