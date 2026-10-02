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
import fcntl
import json
import logging
import os
import secrets
import socket
import stat
import sys
import threading
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
    the app runs, or None when another instance holds it."""
    data_dir = Path(data_dir)
    data_dir.parent.mkdir(parents=True, exist_ok=True)
    try:
        os.mkdir(data_dir, 0o700)
    except FileExistsError:
        pass
    fd = os.open(data_dir / LOCK_FILE, os.O_RDWR | os.O_CREAT, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        os.close(fd)
        return None
    return fd


def narrow_tree(data_dir) -> None:
    """Narrow an existing data folder to owner-only: folders to at most 0700 and files to at
    most 0600, never broadening a mode. A folder copied or restored with wider modes is
    closed to other accounts before anything is opened.

    The walk starts at the data folder's real path (the folder itself may be a link the
    researcher made; the app opens everything through it). The app never makes a link
    inside it, so a folder holding one is refused (UnsafeDataFolderError) rather than
    followed later by a reader of settings, keys or instructions; so is a folder holding
    a folder that cannot be listed, since what it holds cannot be checked. Each folder is
    narrowed before it is listed, and the walk narrows everything it can reach before it
    refuses, so whatever the outcome nothing is left open to other accounts."""
    pending, refused = [os.path.realpath(data_dir)], None
    while pending:
        folder = pending.pop()
        try:
            if not _narrow(folder, 0o700):
                continue
            entries = list(os.scandir(folder))
        except FileNotFoundError:  # removed meanwhile
            continue
        except PermissionError:
            refused = refused or UnsafeDataFolderError("a folder in the data folder cannot be listed")
            continue
        except UnsafeDataFolderError as error:
            refused = refused or error
            continue
        for entry in entries:
            if entry.is_symlink():
                refused = refused or UnsafeDataFolderError("the data folder holds a link, which Scholia does not follow")
            elif entry.is_dir(follow_symlinks=False):
                pending.append(entry.path)
            else:
                try:
                    _narrow(entry.path, 0o600)
                except UnsafeDataFolderError as error:
                    refused = refused or error
    if refused is not None:
        raise refused


def _narrow(path, mask) -> bool:
    """Narrow path's mode to mask, never broadening it. Returns whether path is a real
    folder or file that is still there; a link (put there meanwhile) is refused.
    ponytail: lstat, then chmod; the data folder is owner-only, so nobody else can swap
    in a link between the two."""
    try:
        info = os.lstat(path)
        if stat.S_ISLNK(info.st_mode):
            raise UnsafeDataFolderError("the data folder holds a link, which Scholia does not follow")
        if stat.S_IMODE(info.st_mode) & ~mask:
            os.chmod(path, stat.S_IMODE(info.st_mode) & mask, follow_symlinks=False)
        return True
    except FileNotFoundError:  # removed meanwhile
        return False


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
    lock = take_lock(data_dir)
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
        if not _wait_started(server, thread):
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


def _wait_started(server, thread):
    for _ in range(START_SECONDS * 20):
        if server.started:
            return True
        if not thread.is_alive():
            return False
        thread.join(0.05)
    return server.started


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
