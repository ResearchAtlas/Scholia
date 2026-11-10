# Every test runs behind the outbound network block in network_guard.py. It is
# installed when pytest imports this file, before any project or test module.
import network_guard
from network_guard import _reset_network_registrations  # noqa: F401

network_guard.start()

# pycryptodomex (under pyzipper) learns the interpreter's word size by running file(1) the first
# time it loads a library; the network block refuses that child process, so it is told what
# file(1) says on macOS. Its cffi loader, when cffi is installed, runs nothing.
from Cryptodome.Util import _raw_api  # noqa: E402

if hasattr(_raw_api, "cached_architecture"):
    _raw_api.cached_architecture[:] = ["64bit", "Mach-O"]


# Readings run in a child process (backend/reading.py). Its command is allowed, whole, for the
# session; every other command, another Python run included, stays refused.
import contextlib  # noqa: E402
import sys  # noqa: E402
from pathlib import Path  # noqa: E402

import pytest  # noqa: E402


class _Started(set):
    """reading.LIVE, which also keeps the pid of each child started in the test, as it starts."""

    def __init__(self):
        super().__init__()
        self.started = []

    def add(self, pid):
        self.started.append(pid)
        super().add(pid)


@pytest.fixture(autouse=True, scope="session")
def _reading_child():
    from backend import reading

    reading.LIVE = _Started()  # empty: no child has started yet
    with network_guard.allow_command(*reading.command()):
        yield


def _members(group):
    """The pids in a process group (proc_listpids), read without signalling any."""
    import ctypes

    from backend import reading

    lib = reading._lib()
    lib.proc_listpids.argtypes = [ctypes.c_uint32, ctypes.c_uint32, ctypes.c_void_p, ctypes.c_int]
    members = (ctypes.c_int * 64)()
    size = lib.proc_listpids(2, group, members, ctypes.sizeof(members))  # 2: PROC_PGRP_ONLY
    return [pid for pid in members[:max(size, 0) // ctypes.sizeof(ctypes.c_int)] if pid]


def _stopped_left(groups):
    """The stopped processes, their parent gone (launchd's now), in the process groups named: a reading
    child's group is its pid's (backend/reading.py), so each is found by a pid the test recorded."""
    from backend import reading

    return [pid for group in groups for pid in _members(group) if (info := reading._info(pid)) is not None
            and info.status == reading._SSTOP and info.ppid == 1 and info.pgid == group]


@pytest.fixture(autouse=True)
def _no_reading_child_left_stopped():
    """A test leaves no stopped process in the group of a reading child it started: a sentinel its child
    did not outlive (reading._end ends one, whatever the order). Each child's pid is recorded as it starts
    (reading.LIVE, and test_reading's seen() for a stand-in app's child). Once the test ends, those groups
    are given 2 s to end; a stopped process left in one then fails the test, named. Nothing is signalled:
    once a group has ended, its id may be another process's."""
    from backend import reading

    reading.LIVE.started.clear()
    yield
    import time

    groups = set(reading.LIVE.started)
    deadline = time.monotonic() + 2
    while any(_members(group) for group in groups) and time.monotonic() < deadline:
        time.sleep(0.02)
    left = _stopped_left(groups)
    assert not left, f"stopped processes left in a reading child's group, not ended: {left}"


def _argv(pid):
    """A process's arguments as the kernel holds them (sysctl KERN_PROCARGS2), or None once it cannot be read."""
    import ctypes

    libc = ctypes.CDLL(None, use_errno=True)
    libc.sysctl.argtypes = [ctypes.POINTER(ctypes.c_int), ctypes.c_uint, ctypes.c_void_p, ctypes.POINTER(ctypes.c_size_t),
                            ctypes.c_void_p, ctypes.c_size_t]
    mib = (ctypes.c_int * 3)(1, 49, pid)  # CTL_KERN, KERN_PROCARGS2
    size = ctypes.c_size_t(0)
    if libc.sysctl(mib, 3, None, ctypes.byref(size), None, 0) != 0:
        return None
    buffer = ctypes.create_string_buffer(size.value)
    if libc.sysctl(mib, 3, buffer, ctypes.byref(size), None, 0) != 0:
        return None
    raw = buffer.raw[:size.value]
    argc = int.from_bytes(raw[:4], "little")
    parts = [part for part in raw[4:].split(b"\0") if part]  # the executable's path, then its arguments
    return [part.decode(errors="replace") for part in parts[1:1 + argc]]


def _stand_in(pid, root, since):
    """Whether pid is, now, a stand-in helper this test started: a process started at or after since (the test's
    start, seconds since the epoch) running the stand-in binary (tests/test_local_helper.py Fake) under root. Any
    other process, a pid another process took since included, is not."""
    import os

    from backend import reading

    info = reading._info(pid)  # none once it has ended (a zombie has no record)
    if info is None or info.start[0] + info.start[1] / 1e6 < since - 1:
        return False
    folder = os.path.realpath(root) + os.sep
    return any(os.path.realpath(arg).startswith(folder) and arg.endswith("/Contents/MacOS/llama-server")
               for arg in _argv(pid) or ())


def _helpers_left(root, grace=2.0, since=0.0):
    """The stand-in helpers started under root (since then) that still run, given grace seconds to end: those its
    launch records name (control/launches.jsonl, written as each starts) and this process's children still starting,
    read again at every look, each confirmed a stand-in of this test (_stand_in) before it is named."""
    import json
    import os
    import time

    from backend import reading

    deadline = time.monotonic() + grace
    while True:
        recorded = [json.loads(line)["pid"] for path in Path(root).glob("**/control/launches.jsonl")
                    for line in path.read_text().splitlines()]
        left = sorted({pid for pid in (*recorded, *reading._children(os.getpid())) if _stand_in(pid, root, since)})
        if not left or time.monotonic() >= deadline:
            return left
        time.sleep(0.02)


@pytest.fixture(autouse=True)
def _no_helper_left_running(request):
    """A test leaves no stand-in helper running: its app stops the helpers it started when it closes. One left fails
    the test, named, and is then ended, each only once confirmed this test's stand-in (_stand_in) just before."""
    import time

    root = request.getfixturevalue("tmp_path") if "tmp_path" in request.fixturenames else None  # torn down after this
    since = time.time()
    yield
    if root is None:
        return
    import os
    import signal

    left = _helpers_left(root, since=since)
    for pid in left:
        if _stand_in(pid, root, since):
            with contextlib.suppress(ProcessLookupError):
                os.kill(pid, signal.SIGKILL)
    assert not left, f"stand-in helpers left running after the test: {left}"


@pytest.fixture
def reading_stub(monkeypatch):
    """Read in tests/reading_stub.py's child for the rest of the test: reading_stub(mode, *args)
    allows its whole command and makes it the readings' (and page images') child."""
    from backend import reading

    with contextlib.ExitStack() as stack:
        def use(*args):
            command = [sys.executable, str(Path(__file__).with_name("reading_stub.py")), *map(str, args)]
            stack.enter_context(network_guard.allow_command(*command))
            monkeypatch.setattr(reading, "command", lambda: command)
            return command

        yield use


@pytest.fixture(autouse=True)
def _close_failed_cleared():
    """A failed close holds file replacement off for the rest of its process (backend.db.close_failed), never
    cleared but by a restart: each test is its own app, so it is cleared after each, as a restart would."""
    yield
    from backend.db import database
    database._close_failed.clear()
