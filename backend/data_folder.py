"""Where the data folder is, whether the live database may be opened there, and choosing another place.

The default folder is desktop.data_folder(). A place the researcher chose instead is recorded
in LOCATION_FILE inside the default folder, owner-only, where the desktop entry reads it before
it opens anything else. A data folder inside iCloud Drive, a File Provider sync folder
(~/Library/CloudStorage: OneDrive, Dropbox, Google Drive and others), ~/Dropbox, or on a network
file system is refused (synced): syncing copies a live database file by file and can corrupt it.
The desktop entry then serves only limited_app, which says why (the interface's data-folder
screen, S2) and records another place for the next launch.
"""

import ctypes
import json
import os
import platform
import stat
import sys
from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel, Field

from backend import APP_VERSION
from backend.desktop import UnsafeDataFolderError, _acl_problem, _check_ancestors, _check_folder
from backend.settings import write_private

LOCATION_FILE = "data-folder.json"  # in the default data folder: {"path": "<the chosen folder>"}
NETWORK_FILE_SYSTEMS = frozenset({"smbfs", "afpfs", "nfs", "webdav"})
SYNCED = {  # under the home folder
    "Library/Mobile Documents": "iCloud Drive",
    "Library/CloudStorage": "a cloud storage folder (OneDrive, Dropbox, Google Drive or another)",
    "Dropbox": "Dropbox",
}


def located(default) -> Path:
    """The data folder: the place recorded in the default folder's LOCATION_FILE, else the default.

    The record is read without following a link, and only if it is a regular file of this account's
    that no other account could have written; otherwise, or when it is unreadable or not an absolute
    path, UnsafeDataFolderError.
    """
    record = Path(default) / LOCATION_FILE
    try:
        fd = os.open(record, os.O_RDONLY | os.O_NOFOLLOW)
    except FileNotFoundError:
        return Path(default)
    except OSError:
        raise UnsafeDataFolderError("Scholia will not open its data folder: its location record cannot be read")
    try:
        info = os.fstat(fd)
        if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) & 0o022
                or _acl_problem(record)):
            raise UnsafeDataFolderError("Scholia will not open its data folder: its location record is not its own")
        with os.fdopen(os.dup(fd), "rb") as file:
            text = json.loads(file.read()).get("path")
    except (ValueError, AttributeError):
        raise UnsafeDataFolderError("Scholia will not open its data folder: its location record cannot be read")
    finally:
        os.close(fd)
    if not isinstance(text, str) or not os.path.isabs(text) or ".." in Path(text).parts:
        raise UnsafeDataFolderError("Scholia will not open its data folder: its location record cannot be read")
    return Path(text)


def synced(path, *, home=None, fs_type=None) -> str | None:
    """Why path may not hold the live database, or None: it is inside a synced folder, or on a
    network file system. home and fs_type (path -> file system type name) are for tests."""
    home = Path(home or Path.home())
    places = {Path(os.path.abspath(path)), Path(os.path.realpath(path))}  # as written, and with links followed
    for base in {home, Path(os.path.realpath(home))}:
        for folder, name in SYNCED.items():
            if any(place.is_relative_to(base / folder) for place in places):
                return f"it is inside {name}, which syncs its files"
    existing = Path(os.path.realpath(path))
    while not existing.exists():  # a folder not made yet sits on its nearest existing parent's file system
        existing = existing.parent
    kind = (fs_type or file_system_type)(existing)
    if kind in NETWORK_FILE_SYSTEMS:
        return f"it is on a network file system ({kind})"
    return None


class _StatFS(ctypes.Structure):  # macOS struct statfs, 64-bit inodes
    _fields_ = [("f_bsize", ctypes.c_uint32), ("f_iosize", ctypes.c_int32), ("f_blocks", ctypes.c_uint64),
                ("f_bfree", ctypes.c_uint64), ("f_bavail", ctypes.c_uint64), ("f_files", ctypes.c_uint64),
                ("f_ffree", ctypes.c_uint64), ("f_fsid", ctypes.c_int32 * 2), ("f_owner", ctypes.c_uint32),
                ("f_type", ctypes.c_uint32), ("f_flags", ctypes.c_uint32), ("f_fssubtype", ctypes.c_uint32),
                ("f_fstypename", ctypes.c_char * 16), ("f_mntonname", ctypes.c_char * 1024),
                ("f_mntfromname", ctypes.c_char * 1024), ("f_flags_ext", ctypes.c_uint32),
                ("f_reserved", ctypes.c_uint32 * 7)]


def file_system_type(path) -> str | None:
    """The type name of the file system holding path (apfs, smbfs, ...), or None off macOS or when unknown."""
    if sys.platform != "darwin":
        return None
    libc = ctypes.CDLL(None, use_errno=True)
    statfs = libc["statfs$INODE64"] if platform.machine() == "x86_64" else libc.statfs
    statfs.argtypes = [ctypes.c_char_p, ctypes.POINTER(_StatFS)]
    info = _StatFS()
    if statfs(os.fsencode(path), ctypes.byref(info)) != 0:
        return None
    return info.f_fstypename.decode(errors="replace")


def choose(default, path, *, home=None, fs_type=None) -> Path:
    """Record path as the data folder for the next launch, after checking it as the desktop entry
    will: an absolute path, not synced or on a network, a folder (made here, owner-only, under an
    existing one) that no other account can change. Choosing the default removes the record.
    Raises ValueError, or UnsafeDataFolderError, saying why it cannot be used."""
    if not os.path.isabs(path) or ".." in Path(path).parts:
        raise ValueError("Choose a folder by its full path")
    path, default = Path(path), Path(default)
    problem = synced(path, home=home, fs_type=fs_type)
    if problem:
        raise ValueError(f"Scholia cannot keep its data there: {problem}")
    _check_ancestors(path)
    if not path.parent.is_dir():
        raise ValueError("The folder above it does not exist")
    try:
        os.mkdir(path, 0o700)
    except FileExistsError:
        pass
    if path.is_symlink() or not path.is_dir():
        raise ValueError("That is not a folder")
    _check_folder(path, os.getuid())
    if default.is_symlink():
        raise UnsafeDataFolderError("Scholia will not record it: its default folder is a link")
    os.makedirs(default, 0o700, exist_ok=True)
    if path == default:
        (default / LOCATION_FILE).unlink(missing_ok=True)
    else:
        write_private(default / LOCATION_FILE, json.dumps({"path": str(path)}).encode())
    return path


class Chosen(BaseModel):
    path: str = Field(min_length=1, max_length=4096)


def limited_app(default, data_dir, problem, reason, *, origin, session, frontend_dir=None):
    """The app the desktop entry serves when it will not open the data folder: the interface,
    /api/health saying why (data_folder_problem: "synced", "unsafe" or "missing"), and
    POST /api/data-folder to choose another place, used from the next launch. It opens nothing
    in the data folder. Every other API request is refused (503 data_folder_problem)."""
    from backend.app import static_file
    from backend.local_guard import LocalRequestGuard

    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)
    app.state.scholia = {}  # nothing for the desktop entry to stop
    frontend = Path(frontend_dir).resolve() if frontend_dir else None

    def error(status, code, message):
        return JSONResponse({"code": code, "message": message}, status_code=status)

    @app.get("/api/health")
    async def health():
        return {"ok": True, "version": APP_VERSION, "data_folder": str(data_dir),
                "data_folder_problem": problem, "data_folder_reason": reason}

    @app.post("/api/data-folder")
    def choose_another(body: Chosen):  # a plain function: FastAPI runs it off the event loop
        try:
            chosen = choose(default, body.path)
        except (ValueError, UnsafeDataFolderError, OSError) as failure:
            reason = str(failure) if not isinstance(failure, OSError) else "That folder cannot be used"
            return error(400, "invalid_data_folder", reason)
        return {"ok": True, "data_folder": str(chosen), "restart": True}

    @app.api_route("/api/{rest:path}", methods=["GET", "POST", "PUT", "PATCH", "DELETE"])
    async def refused(rest: str):
        return error(503, "data_folder_problem", "Scholia has not opened its data folder")

    @app.get("/{path:path}")
    async def interface(path: str):
        file = static_file(frontend, path)
        return FileResponse(file, headers={"Cache-Control": "no-cache"}) if file else error(404, "not_found",
                                                                                              "Not found")

    return LocalRequestGuard(app, origin=origin, session=session)
