"""The desktop app: one window over the backend, served on a loopback port.

Order matters. The data folder's lock is taken first, before the log is opened
or anything else touches the folder, so a second instance stops without writing
there; the operating system releases the lock when the process ends, however it
ends, and nothing reads a process id from the file. Then the log, then the
server on a port of this machine only, then the window. Closing the window stops
admissions, cancels what is running with a bounded wait, and stops the server.

Launching the server directly (uvicorn) against a data folder the app also uses
is not supported: only this entry takes the lock.
"""

import asyncio
import contextlib
import ctypes
import errno
import fcntl
import functools
import json
import logging
import os
import secrets
import socket
import stat
import sys
import threading
import time
from pathlib import Path

APP_NAME = "Scholia"
LOCK_FILE = "scholia.lock"
SESSION_FILE = "session.json"  # owner-only: this launch's origin and session, for this account's native clients
START_SECONDS = 30
STOP_SECONDS = 15
ALREADY_OPEN = (
    "<!doctype html><meta charset=utf-8><title>Scholia</title>"
    "<body style='font:15px -apple-system,sans-serif;padding:24px'>"
    "<p>Scholia is already open.</p><p>Scholia 已经打开。</p>"
)

log = logging.getLogger(__name__)


class UnsafeDataFolderError(RuntimeError):
    """The data folder holds something Scholia will not open, such as a link."""


def data_folder() -> Path:
    """~/Library/Application Support/Scholia on macOS."""
    import platformdirs

    return Path(platformdirs.user_data_dir(APP_NAME, appauthor=False))


def take_lock(data_dir) -> int | None:
    """Take the data folder's lock. Returns its file descriptor, to keep open while
    the app runs, or None when another instance holds it. A lock file that is a link,
    not a regular file or not this account's is refused (UnsafeDataFolderError): it is
    opened before the folder is checked, so it is never followed."""
    data_dir = Path(data_dir)
    data_dir.parent.mkdir(parents=True, exist_ok=True)
    _check_ancestors(data_dir)
    try:
        os.mkdir(data_dir, 0o700)
    except FileExistsError:
        pass
    if os.path.islink(data_dir):  # its target could be swapped after the check: never used
        raise UnsafeDataFolderError("Scholia will not open its data folder: the folder itself is a link")
    try:
        fd = os.open(data_dir / LOCK_FILE, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_NONBLOCK, 0o600)
    except OSError as error:
        problem = _LINK if error.errno == errno.ELOOP else "its lock file cannot be opened"
        raise UnsafeDataFolderError(f"Scholia will not open its data folder: {problem}") from None
    info = os.fstat(fd)
    if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid():
        os.close(fd)
        raise UnsafeDataFolderError("Scholia will not open its data folder: its lock file is not its own")
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        os.close(fd)
        return None
    return fd


def _check_ancestors(data_dir) -> None:
    """Refuse a data folder another account could move or swap: every folder above it, as
    written and as resolved, must be this account's or the system's, writable by no other
    account unless sticky (as /tmp is: others cannot move what is not theirs), with no
    access rule letting others in. Then the path names the folder that was checked."""
    uid, path = os.getuid(), os.path.abspath(data_dir)
    for ancestor in sorted(set(Path(path).parents) | set(Path(os.path.realpath(path)).parents)):
        info = os.lstat(ancestor)
        if info.st_uid not in (uid, 0):
            raise UnsafeDataFolderError("Scholia will not open its data folder: a folder above it is another account's")
        if stat.S_ISLNK(info.st_mode):
            continue  # it is resolved: the folders it leads to are checked as resolved
        if stat.S_IMODE(info.st_mode) & 0o022 and not info.st_mode & stat.S_ISVTX:
            raise UnsafeDataFolderError("Scholia will not open its data folder: other accounts could move it")
        if _acl_problem(ancestor):
            raise UnsafeDataFolderError("Scholia will not open its data folder: an access rule above it lets others in")


def narrow_tree(data_dir) -> None:
    """Narrow an existing data folder to owner-only (folders to at most 0700, files to at
    most 0600, never broadening a mode) before anything in it is opened, or refuse it.

    The data folder itself must not be a link (its target could be swapped after this
    check; S1-12 provides choosing another place). The walk refuses the folder
    (UnsafeDataFolderError) when what it holds may have been changed by another account,
    since a reader of settings, keys or instructions would trust it: a link (the app never
    makes one there), an item another account owns, an item other accounts could write
    (group or other write; left as it is, so the folder stays refused until the
    researcher checks and narrows it), an access-control entry that allows someone in,
    or a folder that cannot be listed, whose contents cannot be checked. The walk narrows
    everything else it reaches before it refuses."""
    if os.path.islink(data_dir):
        raise UnsafeDataFolderError("Scholia will not open its data folder: the folder itself is a link")
    pending, problem = [os.fspath(data_dir)], None
    while pending:
        folder = pending.pop()
        found = _narrow(folder, 0o700)
        problem = problem or found
        if found == _LINK:  # put there meanwhile: never listed
            continue
        try:
            entries = list(os.scandir(folder))
        except FileNotFoundError:  # removed meanwhile
            continue
        except PermissionError:
            problem = problem or "a folder in it cannot be listed"
            continue
        for entry in entries:
            if entry.is_dir(follow_symlinks=False):
                pending.append(entry.path)
            else:
                found = _narrow(entry.path, 0o600)  # every file is narrowed, whatever was found before
                problem = problem or found
    if problem:
        raise UnsafeDataFolderError(f"Scholia will not open its data folder: {problem}")


_LINK = "it holds a link"


def _narrow(path, mask):
    """Narrow path's mode to mask, unless another account may have changed it. Returns why
    it is unsafe, or None. ponytail: lstat, then chmod; the folder above is checked first,
    so nobody else can swap in a link between the two."""
    try:
        info = os.lstat(path)
    except FileNotFoundError:  # removed meanwhile
        return None
    mode = stat.S_IMODE(info.st_mode)
    if stat.S_ISLNK(info.st_mode):
        return _LINK
    if info.st_uid != os.getuid():
        return "it holds an item another account owns"
    if mode & 0o022:
        return "other accounts could change what it holds"
    found = _acl_problem(path)
    if found:
        return found
    if mode & ~mask:
        os.chmod(path, mode & mask, follow_symlinks=False)
    return None


@functools.cache
def _libc():
    libc = ctypes.CDLL(None, use_errno=True)
    libc.acl_get_link_np.restype = ctypes.c_void_p
    libc.acl_get_link_np.argtypes = [ctypes.c_char_p, ctypes.c_int]
    libc.acl_to_text.restype = ctypes.c_void_p
    libc.acl_to_text.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
    libc.acl_free.argtypes = [ctypes.c_void_p]
    return libc


def _acl_problem(path):
    """Why path's macOS access-control list is unsafe, or None: an entry that allows
    something (inherited or inheritable ones included) can let another account in whatever
    the mode says, and a list that cannot be read cannot be checked. Deny entries are
    harmless."""
    if sys.platform != "darwin":
        return None
    acl = _libc().acl_get_link_np(os.fsencode(path), 0x100)  # ACL_TYPE_EXTENDED
    if not acl:
        return None if ctypes.get_errno() == errno.ENOENT else "its access rules cannot be checked"  # ENOENT: none
    try:
        text = _libc().acl_to_text(acl, None)
        if not text:
            return "its access rules cannot be checked"
        try:
            entries = ctypes.string_at(text).splitlines()[1:]  # after the "!#acl 1" header
        finally:
            _libc().acl_free(text)
    finally:
        _libc().acl_free(acl)
    # An entry is tag:qualifier...:id:kind[,flags]:permissions; kind is allow or deny.
    if any(len(fields) < 5 or fields[-2].split(b",")[0] != b"deny" for fields in (e.split(b":") for e in entries if e)):
        return "an access rule lets other accounts in"
    return None


def frontend_folder() -> Path:
    """The built interface: inside the app bundle, or frontend/dist in a checkout."""
    if getattr(sys, "frozen", False):
        return Path(sys._MEIPASS) / "frontend"
    return Path(__file__).resolve().parents[1] / "frontend" / "dist"


def run(data_dir, open_window, *, keyring_backend=None, transport=None, listening=lambda sock: None) -> int:
    """Run the app on data_dir until open_window(url) returns (the window closed). url
    carries this launch's session in its fragment (see local_guard).

    Returns 0, or 1 when another instance holds the data folder. The keyword
    arguments are for tests: a credential store, a transport for outbound requests,
    and a hook given the listening socket.
    """
    try:
        lock = take_lock(data_dir)
    except UnsafeDataFolderError as error:  # ponytail: said on stderr; S1-12's data-folder screen explains it
        print(f"Scholia: {error}", file=sys.stderr)
        return 1
    if lock is None:
        open_window(None)  # shows that the app is already open
        return 1
    try:
        narrow_tree(data_dir)
        return _serve(Path(data_dir), open_window, keyring_backend, transport, listening)
    except UnsafeDataFolderError as error:  # ponytail: said on stderr; S1-12's data-folder screen explains it
        print(f"Scholia: {error}", file=sys.stderr)
        return 1
    finally:
        os.close(lock)


def _serve(data_dir, open_window, keyring_backend, transport, listening):
    import uvicorn

    from backend import logs
    from backend.app import create_app
    from backend.settings import write_private

    handler = logs.configure(data_dir)
    try:
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.bind(("127.0.0.1", 0))
        sock.listen(64)
        listening(sock)
        origin = f"http://127.0.0.1:{sock.getsockname()[1]}"
        session = secrets.token_urlsafe(32)  # this launch's: the window and this account's native clients
        write_private(data_dir / SESSION_FILE, json.dumps({"origin": origin, "session": session}).encode())
        app = create_app(data_dir, origin=origin, session=session, frontend_dir=frontend_folder(),
                         keyring_backend=keyring_backend, transport=transport)
        server = uvicorn.Server(uvicorn.Config(
            app, lifespan="on", loop="asyncio", http="h11", ws="none", log_config=None, access_log=False,
            timeout_graceful_shutdown=5))
        loop = {}
        thread = threading.Thread(target=_run_server, args=(server, sock, loop), name="scholia-server", daemon=True)
        thread.start()
        if not _wait_started(server, thread, app.app.state.scholia):
            log.error("the backend did not start")
            server.should_exit = True
            thread.join(STOP_SECONDS)
            return 1
        try:
            open_window(f"{origin}/#session={session}")  # a fragment never reaches a server
        finally:
            _stop(app, server, thread, loop)
        return 0
    finally:
        with contextlib.suppress(FileNotFoundError):  # this launch's session ends with it
            os.unlink(data_dir / SESSION_FILE)
        logging.getLogger().removeHandler(handler)
        handler.close()


def _run_server(server, sock, loop):
    async def main():
        loop["loop"] = asyncio.get_running_loop()
        await server.serve(sockets=[sock])

    asyncio.run(main())


def _wait_started(server, thread, state):
    """Wait for the server to start, at most START_SECONDS, not counting the local maintenance
    of opening the database and the daily backup (the app's maintenance times in state): on
    a large folder it is progress, not a hang."""
    began = time.monotonic()
    while not server.started:
        if not thread.is_alive():
            return False
        with state.get("maintenance_lock") or contextlib.nullcontext():  # the clock and the times, as one reading
            now = time.monotonic()
            current = state.get("maintenance_started")
            maintained = state.get("maintenance_seconds", 0.0) + (now - current if current is not None else 0.0)
        if now - began - maintained > START_SECONDS:
            return False
        thread.join(0.05)
    return True


def _stop(app, server, thread, loop):
    """Cancel running work first, so it ends as interrupted by the shutdown, then stop the server."""
    harness = app.app.state.scholia.get("harness")
    if harness is not None and "loop" in loop:
        future = asyncio.run_coroutine_threadsafe(harness.shutdown(timeout=STOP_SECONDS - 5), loop["loop"])
        try:
            future.result(STOP_SECONDS)
        except Exception as error:
            log.warning("cancelling running work at shutdown failed (%s)", type(error).__name__)
    server.should_exit = True
    thread.join(STOP_SECONDS)
    if thread.is_alive():
        log.warning("the backend did not stop within %s s; the process exits anyway", STOP_SECONDS)


def _webview_window(url):
    import webview  # the window's own network use is loading this app's pages

    if url is None:
        webview.create_window(APP_NAME, html=ALREADY_OPEN, width=420, height=200)
    else:
        webview.create_window(APP_NAME, url, width=1280, height=820, min_size=(720, 520))
    webview.start()


def main(argv=None) -> int:
    return run(data_folder(), _webview_window)
