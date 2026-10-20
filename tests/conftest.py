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
import os  # noqa: E402
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


def _stopped_left(groups):
    """The stopped processes, their parent gone (launchd's now), in the process groups named: a reading
    child's group is its pid's (backend/reading.py), so each is found by a pid the test recorded."""
    import ctypes

    from backend import reading

    lib = reading._lib()
    lib.proc_listpids.argtypes = [ctypes.c_uint32, ctypes.c_uint32, ctypes.c_void_p, ctypes.c_int]
    left = []
    for group in groups:
        members = (ctypes.c_int * 64)()
        size = lib.proc_listpids(2, group, members, ctypes.sizeof(members))  # 2: PROC_PGRP_ONLY
        for pid in members[:max(size, 0) // ctypes.sizeof(ctypes.c_int)]:
            info = reading._info(pid) if pid else None
            if info is not None and info.status == reading._SSTOP and info.ppid == 1 and info.pgid == group:
                left.append(pid)
    return left


@pytest.fixture(autouse=True)
def _no_reading_child_left_stopped():
    """A test leaves no stopped process in the group of a reading child it started (its sentinel, made
    stopped, which the kernel ends with the child; one whose child was ended by its own pid before it
    stopped stays stopped for good). Each child's pid is recorded as it starts (reading.LIVE, and test_reading's
    seen() for a stand-in app's child); one found once the test ends is ended, by its pid, and the test fails."""
    from backend import reading

    reading.LIVE.started.clear()
    yield
    import signal
    import time

    deadline = time.monotonic() + 2  # a sentinel whose child has just ended is ended by the kernel
    while (left := _stopped_left(set(reading.LIVE.started))) and time.monotonic() < deadline:
        time.sleep(0.05)
    for pid in left:
        with contextlib.suppress(ProcessLookupError):
            os.kill(pid, signal.SIGKILL)
    assert not left, f"stopped processes left in a reading child's group: {left}"


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
