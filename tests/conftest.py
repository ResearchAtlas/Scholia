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


@pytest.fixture(autouse=True, scope="session")
def _reading_child():
    from backend import reading

    with network_guard.allow_command(*reading.command()):
        yield


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
