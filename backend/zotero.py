"""Zotero's library, read only (slice-1 spec F3c, S11, sections 4, 5 and 10; ticket 19's Zotero client;
ticket 71; S1-15).

Scholia reads Zotero through Zotero's local API (Zotero 7 and later, at http://127.0.0.1:23119, once
"Allow other applications on this computer to communicate with Zotero" is on), only with the outbound
gate's Zotero clients: GETs of the calls the gate allowlists (backend/outbound_gate.py, the Zotero
block), with no redirect followed. It never writes to Zotero, never calls local/authorize and never
opens Zotero's database (zotero.sqlite).

- `GET /api/zotero/status`: whether Zotero can be read (off, not_running, api_off, not_yours,
  unsupported or ready) and, when ready, its libraries (My Library and each group) and the server ID
  Zotero 10 and later send, which an import sends back on its reads (a different Zotero answers 412).
- `GET /api/zotero/collections?library=`: the library's collections (key, name, parent).
- `GET /api/zotero/items?library=[&collection=][&start=]`: a page of the library's (or collection's)
  top-level items, notes and attachments left out.
- `POST /api/projects/{id}/materials/import-zotero`: imports up to MAX_ITEMS items as a background
  run (`zotero_import`), answering its run id.
Browsing goes through the General project's client (it carries no project's content); an import
through its project's, with a dispatch check that needs the run running, not revoked, in a project that
still exists. The review lock is not part of it: nothing leaves this Mac, so every level allows it.

An import reads, for each item in turn: the item with its CSL JSON (one request), its children, and,
for its PDF attachment (a stored file before a linked one, then the earliest added), the attachment's
file URL. Zotero's local API serves no file bytes: it names the file on disk (a file:// URL, read only
in the whole form Zotero writes, else the import fails: file_path). Scholia reads that local file itself (read_attachment), only when it is on this Mac's own disk, reached through
no link, a regular PDF of at most extraction.MAX_FILE_BYTES; otherwise the item is imported with its
details only, and the reason recorded. Every answer of Zotero's is checked for its shape before it is
used; one of another shape fails the import (zotero_unavailable), never read as an empty answer. The
record is read with S1-14's CSL JSON reader and bounds (backend/reference_files.py), so its identifier
follows S1-14's rule: a DOI only from the record's DOI field, whole, else an arXiv ID only from Zotero's
archiveID field when it is, whole, "arXiv:" and an ID (Harold, 2026-10-17); never from a URL or
`number`. Each item is written in its own transaction, while the run is running, not revoked and not
cancelled:
- an item already imported into the project (keyed by library and item key, `source_key`
  "<library>/items/<KEY>") is updated: its details replaced by the record when they changed and the
  researcher has not edited them, and a PDF that is none of its files becomes its next version;
- a new item that duplicates a paper of the project (S1-14's `references.Duplicates`) is reported
  already there when it would add nothing, else asked about through S1-14's merge run (Keep both
  imports it as its own paper, through a one-item import of its own); a PDF that is already a paper's
  current file in the project is reported already there, as an upload is;
- otherwise it becomes the project's paper, source zotero, with its first version (its PDF, read in
  a reading run, or none).
Papers it made or whose details or file it replaced, carrying an identifier in their record or a file
whose text may give one, join one lookup batch for the import (S1-13's, batch `zotero`), bound to the
import run: it stays open while the import runs, however long and across a restart, and closes when the
import ends, so a Local only project asks once per import; a review-locked one never looks up. A
lookup applies only while the paper still gives the identifier it was made for, as the lookup worked it
out (its record, else its file's text; materials._gives). Its step events record each item's outcome, from
which the summary is counted. Its inputs name its items and the items done, so a restart goes on from
the next item; deleting a Zotero paper revokes every running import of its item into its project
(backend/db/deletion.py SCOPE_LINKS), so none makes it again.
"""

import asyncio
import ctypes
import errno
import json
import os
import platform
import re
import stat
import sys
import time
import unicodedata
from urllib.parse import unquote

import httpx
from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel, Field

from backend import extraction, reference_files, references, search
from backend.data_folder import _StatFS
from backend.db import new_id, utc_now
from backend.materials import _add_to_batch, _no_store, _refused, add_version
from backend.outbound_gate import ZOTERO, OutboundDenied, _show
from backend.runs import AdmissionError, RunOutcome, _event, _revoked, _running
from backend.settings import load_settings

ORIGIN = _show(ZOTERO)
MAX_ITEMS = 100  # per import (bounded work; the browser says so at the cap)
PAGE = 50  # items per page of the browser
COLLECTION_PAGES = 50  # of 100 collections each, at most
MAX_ANSWER = 8 * 1024 * 1024  # one answer's bytes
TIMEOUT = 15.0  # one request, connect to last byte
RETRIES = (1.0, 4.0)  # an import's request that found Zotero unreachable or failing (5xx), tried again after these
TITLE_CHARS = reference_files.TITLE_CHARS
CREATORS_CHARS = 300
SF_DATALESS = 0x40000000  # a file whose contents are not on this Mac (a cloud placeholder), macOS's st_flags
_LIBRARY = re.compile(r"users/0|groups/[1-9][0-9]{0,9}")
_KEY = re.compile(r"[A-Z0-9]{8}")
_SERVER_ID = re.compile(r"[A-Za-z0-9_-]{1,64}")
# A file URL as Zotero's local API gives one (Zotero.File.pathToFileURI, Mozilla's newFileURI): file:/// and
# a path of RFC 3986's path characters (unreserved, sub-delims, ":", "@", "/") and %XX escapes, plus "[",
# "]" and "|", which newFileURI leaves unescaped (its esc_Directory mask, xpcom/io/nsEscape.cpp); a space, a
# letter outside ASCII, "#", "?", "%" and every control character always come escaped. Nothing else is read.
_FILE_URL = re.compile(r"file:///(?:[A-Za-z0-9\-._~!$&'()*+,;=:@/\[\]|]|%[0-9A-Fa-f]{2})*")
_CONTROL_CHARS = re.compile("[\x00-\x1f\x7f-\x9f]")
_NOT_ITEMS = ("note", "attachment", "annotation")  # never offered or imported as a paper
_DATABASES = (".sqlite", ".sqlite3", ".sqlite-wal", ".sqlite-shm", "-journal", ".sqlite.bak")  # file names never read
MNT_NOWAIT = 2  # getfsstat(2): the kernel's table as it is, mounting nothing and asking no file system
_FIRMLINK = ["system", "volumes", "data"]  # macOS's data volume, also seen at / (a firmlink)
# The file systems an attachment's folders and file may be on: this Mac's own disks (APFS, HFS+) and
# drives attached to it (exFAT, FAT). Anything else (smbfs, afpfs, nfs, webdav, autofs, a FUSE file
# system, one whose type cannot be read) is refused on the folder's own descriptor before anything below
# it is opened, so no automount, network share or remote file system is ever descended into.
LOCAL_FILE_SYSTEMS = frozenset({"apfs", "hfs", "exfat", "msdos"})
_LINKS = ("imported_file", "imported_url", "linked_file")  # attachments with a file on disk, stored ones first
# What a paper's import outcome is, counted in the run's summary.
OUTCOMES = ("imported", "updated", "unchanged", "already", "waiting", "not_found", "no_title")

router = APIRouter(dependencies=[Depends(_no_store)])


class Unreachable(Exception):
    """Zotero could not be read: code zotero_not_running, zotero_api_off, zotero_unsupported,
    zotero_not_yours or zotero_unavailable."""

    def __init__(self, code):
        super().__init__(code)
        self.code = code


def enabled(data_dir):
    """The [zotero] switch (slice-1 spec section 4): on unless `enabled = false`. The section keeps the
    open shape settings give it until its other switches are defined."""
    section = load_settings(data_dir).values.get("zotero")
    return not (isinstance(section, dict) and section.get("enabled") is False)


# Reading Zotero's answers


async def _get(client, path, server_id=None):
    """(status, headers, body) of one GET of Zotero's local API: a 200 answer's body read as it
    streams in, refused (zotero_unavailable) past MAX_ANSWER or in any encoding but identity; another
    status with no body. Raises Unreachable (no answer, the port is another account's, or another
    refusal by the gate, such as a redirect) or OutboundDenied revoked (the run may send nothing more)."""
    headers = {"Accept-Encoding": "identity", **({"Zotero-Server-ID": server_id} if server_id else {})}
    try:
        async with asyncio.timeout(TIMEOUT):
            async with client.stream("GET", ORIGIN + path, headers=headers) as response:
                if response.status_code != 200:
                    return response.status_code, response.headers, None
                if response.headers.get("content-encoding", "identity").strip().lower() != "identity":
                    raise Unreachable("zotero_unavailable")
                body = bytearray()
                async for raw in response.aiter_raw():
                    body += raw
                    if len(body) > MAX_ANSWER:
                        raise Unreachable("zotero_unavailable")
                return 200, response.headers, bytes(body)
    except OutboundDenied as denied:
        if denied.reason == "revoked":  # the run may send nothing more
            raise
        # Another account's listener, or anything else the gate refused (a redirect from Zotero, say).
        raise Unreachable("zotero_not_yours" if denied.reason == "local_server_not_yours" else "zotero_unavailable") \
            from None
    except (httpx.ConnectError, httpx.ConnectTimeout, TimeoutError):
        raise Unreachable("zotero_not_running") from None
    except httpx.HTTPError:
        raise Unreachable("zotero_unavailable") from None


class _NotFound:
    """What a status in _read's none answers (404: no such item), never a 200 answer's value (JSON null
    included, which is an answer of another shape)."""


NOT_FOUND = _NotFound()


async def _read(client, path, server_id=None, retries=(), none=(404,)):
    """The body of a 200 answer, parsed as JSON when it is JSON (text otherwise), and its headers; a
    status in none (404: no such item) is NOT_FOUND. Unreachable otherwise: 403 api_off, 412 zotero_changed
    (another Zotero than the one browsed), anything else unavailable. retries: delays to try again after
    when Zotero gave no answer or failed (5xx)."""
    for delay in (*retries, None):
        try:
            status, headers, body = await _get(client, path, server_id)
        except Unreachable as unreachable:
            if delay is None or unreachable.code not in ("zotero_not_running", "zotero_unavailable"):
                raise
            status = None
        if status == 200:
            if path.endswith("/file/view/url"):
                return body, headers
            try:
                return json.loads(body), headers
            except (ValueError, RecursionError):
                raise Unreachable("zotero_unavailable") from None
        if status in none:
            return NOT_FOUND, headers
        if status == 403:
            raise Unreachable("zotero_api_off")
        if status == 412:
            raise Unreachable("zotero_changed")
        if status is not None and status < 500 or delay is None:
            raise Unreachable("zotero_unavailable")
        await asyncio.sleep(delay)


def _text(value, limit):
    return reference_files._text(value, limit)


# The one checked reader (Harold, 2026-10-17): every value Scholia reads from a Zotero answer is read by
# its expected type, or as absent where Zotero may leave it out; any other value is no answer to go by and
# fails what asked for it (Unreachable zotero_unavailable), never read as empty or as nothing. The fields
# of an item's CSL JSON are S1-14's reader's (backend/reference_files.py), which leaves out and counts a
# value of the wrong kind.
OBJECT, LIST, TEXT, INTEGER, FLAG = dict, list, str, int, bool


def _as(value, kind, optional=False):
    """value, when its type is kind (or one of a tuple of kinds; a flag is never an integer); None when
    optional and absent (or JSON null)."""
    if value is None and optional:
        return None
    if type(value) in (kind if isinstance(kind, tuple) else (kind,)):
        return value
    raise Unreachable("zotero_unavailable")


def _field(container, name, kind, optional=False):
    """A checked object's field, read by _as."""
    return _as(container.get(name), kind, optional)


def _key(container, name="key", optional=False):
    """An item's or a collection's key field: text of Zotero's key form."""
    value = _field(container, name, TEXT, optional)
    if value is not None and not _KEY.fullmatch(value):
        raise Unreachable("zotero_unavailable")
    return value


def _rows(answer, keyed=True):
    """A list answer's rows' data objects: a list of objects, each with its data object (keyed: with
    its key)."""
    found = []
    for row in _as(answer, LIST):
        data = _field(_as(row, OBJECT), "data", OBJECT)
        if keyed:
            _key(data)
        found.append(data)
    return found


def _libraries(groups):
    """My Library and each group, as the browser lists them: {id, name} (name None for My Library)."""
    found = [{"id": "users/0", "name": None}]
    for group in _as(groups, LIST):
        number = _field(_as(group, OBJECT), "id", INTEGER)
        name = _field(_field(group, "data", OBJECT), "name", TEXT, optional=True)
        if not _LIBRARY.fullmatch(f"groups/{number}"):
            raise Unreachable("zotero_unavailable")
        found.append({"id": f"groups/{number}", "name": _text(name, TITLE_CHARS) or f"{number}"})
    return found


async def _general(state):
    return await asyncio.to_thread(state["db"].read, lambda conn: conn.execute(
        "SELECT id FROM projects WHERE kind = 'general'").fetchone()[0])


async def _browse(state, path):
    """A browse request's JSON answer, through the General project's Zotero client; refused when the
    switch is off or Zotero cannot be read."""
    if not await asyncio.to_thread(enabled, state["data_dir"]):
        raise _refused(409, "zotero_off", "Zotero is turned off in Settings")
    async with state["gate"].async_client(await _general(state), zotero=True, follow_redirects=False) as client:
        try:
            return await _read(client, path)
        except Unreachable as unreachable:
            raise _refused(502, unreachable.code, "Zotero could not be read") from None


def _library(library):
    if not isinstance(library, str) or not _LIBRARY.fullmatch(library):
        raise _refused(400, "invalid_request", "No such Zotero library")
    return library


# The browser


@router.get("/api/zotero/status")
async def status(request: Request):
    """Whether Zotero can be read now, and its libraries when it can."""
    state = request.app.state.scholia
    if not await asyncio.to_thread(enabled, state["data_dir"]):
        return {"state": "off"}
    async with state["gate"].async_client(await _general(state), zotero=True, follow_redirects=False) as client:
        try:
            groups, headers = await _read(client, "/api/users/0/groups?format=json")
        except Unreachable as unreachable:
            return {"state": {"zotero_not_running": "not_running", "zotero_api_off": "api_off",
                              "zotero_not_yours": "not_yours"}.get(unreachable.code, "error")}
    if groups is NOT_FOUND or headers.get("zotero-api-version") != "3":  # Zotero before 7, or another API
        return {"state": "unsupported"}
    server_id = headers.get("zotero-server-id")
    try:
        libraries = _libraries(groups)
    except Unreachable:  # an answer of another shape
        return {"state": "error"}
    return {"state": "ready", "libraries": libraries,
            "server_id": server_id if server_id and _SERVER_ID.fullmatch(server_id) else None}


@router.get("/api/zotero/collections")
async def collections(request: Request, library: str = ""):
    """The library's collections, read 100 at a time: {key, name, parent} (parent None at the top)."""
    state, library = request.app.state.scholia, _library(library)
    found = []
    for page in range(COLLECTION_PAGES):
        answer, headers = await _browse(state, f"/api/{library}/collections?format=json&limit=100&start={page * 100}")
        if answer is NOT_FOUND:
            raise _refused(404, "not_found", "No such Zotero library")
        try:
            rows = _rows(answer)
            for data in rows:
                parent = _field(data, "parentCollection", (TEXT, FLAG), optional=True)  # a key, or false at the top
                if parent is True:
                    raise Unreachable("zotero_unavailable")
                found.append({"key": data["key"], "name": _text(_field(data, "name", TEXT, optional=True), TITLE_CHARS)
                              or data["key"], "parent": _key(data, "parentCollection") if isinstance(parent, str) else None})
        except Unreachable as unreachable:
            raise _refused(502, unreachable.code, "Zotero could not be read") from None
        if len(rows) < 100:
            return {"collections": found}
    raise _refused(502, "zotero_unavailable", "The library has more collections than Scholia reads")


def _row(item):
    """An item as the browser lists it (its row's data and meta read by the checked reader), or None for
    a note or an attachment."""
    data = _field(item, "data", OBJECT)
    kind = _field(data, "itemType", TEXT)
    if kind in _NOT_ITEMS:
        return None
    meta = _field(item, "meta", OBJECT, optional=True) or {}
    title = next((t for t in (_text(_field(data, f, TEXT, optional=True), TITLE_CHARS)
                              for f in ("title", "caseName", "subject", "nameOfAct")) if t), None)
    date = _field(meta, "parsedDate", TEXT, optional=True)
    return {"key": data["key"], "title": title,
            "creators": _text(_field(meta, "creatorSummary", TEXT, optional=True), CREATORS_CHARS),
            "year": int(date[:4]) if date and re.match(r"[0-9]{4}", date) else None,
            "type": kind if len(kind) <= 40 else None}


@router.get("/api/zotero/items")
async def items(request: Request, library: str = "", collection: str | None = None, start: int = 0):
    """A page of the library's top-level items (or a collection's), with the total and the next page's start."""
    state, library = request.app.state.scholia, _library(library)
    if collection is not None and not _KEY.fullmatch(collection):
        raise _refused(400, "invalid_request", "No such Zotero collection")
    start = max(0, min(start, 999_950))
    where = f"collections/{collection}/" if collection else ""
    answer, headers = await _browse(state, f"/api/{library}/{where}items/top?format=json&limit={PAGE}&start={start}")
    if answer is NOT_FOUND:
        raise _refused(404, "not_found", "No such Zotero library or collection")
    try:
        _rows(answer)
        listed = [row for row in map(_row, answer) if row]
    except Unreachable as unreachable:
        raise _refused(502, unreachable.code, "Zotero could not be read") from None
    total = headers.get("total-results", "")
    total = int(total) if re.fullmatch(r"[0-9]{1,9}", total) else None
    more = total > start + PAGE if total is not None else len(answer) == PAGE
    return {"items": listed, "total": total, "next": start + PAGE if more else None}


# Importing


class ZoteroImport(BaseModel):
    library: str = Field(max_length=20)
    items: list[str] = Field(min_length=1, max_length=MAX_ITEMS)
    server_id: str | None = Field(default=None, max_length=64)


@router.post("/api/projects/{project_id}/materials/import-zotero", status_code=201)
async def import_zotero(project_id: str, body: ZoteroImport, request: Request):
    """Admit an import of the chosen items as a background run; its id. Nothing is written when the
    request fails validation, the project does not exist or Zotero is turned off."""
    state = request.app.state.scholia
    library = _library(body.library)
    if not all(_KEY.fullmatch(key) for key in body.items):
        raise _refused(400, "invalid_request", "No such Zotero item")
    if body.server_id is not None and not _SERVER_ID.fullmatch(body.server_id):
        raise _refused(400, "invalid_request", "Not a Zotero server ID")
    if not await asyncio.to_thread(enabled, state["data_dir"]):
        raise _refused(409, "zotero_off", "Zotero is turned off in Settings")
    keys = list(dict.fromkeys(body.items))

    def admit(conn, ids):
        if conn.execute("SELECT 1 FROM projects WHERE id = ?", (project_id,)).fetchone() is None:
            raise _refused(404, "not_found", "No such project")
        existing = [m for (m,) in conn.execute(  # the papers it updates: deleting one revokes it
            "SELECT id FROM materials WHERE project_id = ? AND source = 'zotero' AND source_key IN"
            " (SELECT value FROM json_each(?))", (project_id, json.dumps([f"{library}/items/{k}" for k in keys])))]
        conn.execute("INSERT INTO runs (id, project_id, kind, workflow, inputs) VALUES (?, ?, 'background',"
                     " 'zotero_import', ?)", (ids[0], project_id, json.dumps({
                         "library": library, "items": keys, "server_id": body.server_id, "material_ids": existing,
                         "done": [], "origin": None})))
        return ids[0], ids

    return {"run_id": await state["harness"].record_background(1, admit)}


def file_path(text):
    """The local path a file URL from Zotero names (Harold, 2026-10-20): the whole URL matches _FILE_URL,
    and its path, percent-decoded as UTF-8 (strictly), is absolute and holds no control character (NUL
    included). ValueError for anything else. Nothing normalizes it on the way (no URL parser)."""
    if not isinstance(text, str) or not _FILE_URL.fullmatch(text):
        raise ValueError("not a file URL Zotero gives")
    path = unquote(text[len("file://"):], errors="strict")  # a byte sequence not UTF-8: UnicodeDecodeError
    if not path.startswith("/") or _CONTROL_CHARS.search(path):
        raise ValueError("not an absolute path")
    return path


def read_attachment(path, fs_type=None, mounts=None):
    """(the PDF's bytes, None) or (None, why not) for the local path a file URL from Zotero names
    (file_path), or None for an attachment with no file (pdf_missing).
    Only a file on this Mac's own disk is read, and no link is ever followed: the path (with no "." or
    ".." in it) is opened from / one folder at a time, each relative to the
    folder opened before it and never by name again, refusing a symbolic link at any step (pdf_link: an
    attachment reached through one imports with its details only; a Finder alias is a file, not a PDF). Before any of
    it is opened, a path at or under any mount point of the Mac's mount table (read with getfsstat and
    MNT_NOWAIT, which mounts nothing; an autofs trigger, a direct-map one included, is listed) whose type
    is not one of LOCAL_FILE_SYSTEMS is refused by its name (pdf_on_network; a table that cannot be read
    refuses every path: pdf_unreadable), and a file named
    like a database (zotero.sqlite and its kin); each folder opened, / first, is checked on its own
    descriptor to be on one of LOCAL_FILE_SYSTEMS and not a cloud placeholder before anything in it is
    opened (any other type, autofs included, or one that cannot be read, is refused); the file
    is opened read-only and without waiting (a FIFO), then checked on its descriptor: a regular file on
    this Mac's disk, its contents here, of at most MAX_FILE_BYTES (at most one byte more is read), that
    starts as a PDF (only its first 5 bytes are read from one that does not). Nothing is written.
    fs_type(fd), the file system type of an open descriptor, and mounts(), the mount table as [(mount
    point, type)], are for tests. ponytail: a trigger mounted between the table's reading and the open is
    not seen by name (the per-descriptor check still refuses what is below it once opened); it needs an
    administrator to run automount meanwhile."""
    fs_type, mounts = fs_type or _file_system_type, mounts or _mount_table
    if path is None:
        return None, "pdf_missing"
    names = [name for name in path.split("/") if name]
    if not path.startswith("/") or "\x00" in path or not names or {".", ".."} & set(names):
        return None, "pdf_not_local"
    try:
        table = mounts()
    except (OSError, AttributeError, ctypes.ArgumentError):  # the Mac's mounts cannot be told: nothing is opened
        return None, "pdf_unreadable"
    if table is not None and _on_other_mount(names, table):  # never opened: opening it could mount a share
        return None, "pdf_on_network"
    if names[-1].lower().endswith(_DATABASES):
        return None, "pdf_not_a_pdf"  # never Zotero's database or its kin
    try:
        folder = os.open("/", os.O_RDONLY | os.O_DIRECTORY)
    except OSError:
        return None, "pdf_unreadable"
    try:
        try:  # / too is checked before anything in it is opened
            problem = _place(os.fstat(folder), fs_type(folder))
        except OSError:
            return None, "pdf_unreadable"
        if problem is not None:
            return None, problem
        for name in names[:-1]:
            try:
                inner = os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=folder)
            except OSError as error:
                return None, _open_problem(error, name, folder)
            os.close(folder)
            folder = inner
            try:
                problem = _place(os.fstat(folder), fs_type(folder))
            except OSError:
                return None, "pdf_unreadable"
            if problem is not None:
                return None, problem
        try:  # O_NONBLOCK: a FIFO never waits for a writer; O_NOFOLLOW: a link here is refused
            fd = os.open(names[-1], os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW, dir_fd=folder)
        except OSError as error:
            return None, _open_problem(error, names[-1], folder)
    finally:
        os.close(folder)
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode):
            return None, "pdf_not_a_file"
        if (problem := _place(info, fs_type(fd))) is not None:  # an OSError here is pdf_unreadable (below)
            return None, problem
        if info.st_size > extraction.MAX_FILE_BYTES:
            return None, "pdf_too_large"
        data = bytearray(os.read(fd, 5))
        if data != b"%PDF-":  # nothing more of a file that is not a PDF is read
            return None, "pdf_not_a_pdf"
        while len(data) <= extraction.MAX_FILE_BYTES:
            block = os.read(fd, min(1 << 20, extraction.MAX_FILE_BYTES + 1 - len(data)))
            if not block:
                break
            data += block
    except OSError:
        return None, "pdf_unreadable"
    finally:
        os.close(fd)
    if len(data) > extraction.MAX_FILE_BYTES:  # it grew as it was read
        return None, "pdf_too_large"
    return bytes(data), None


def _spelled(names):
    """A path's names as compared with mount points: in one Unicode form, case folded (APFS's default
    ignores both), and without the data volume's firmlinked prefix, so both of its spellings are one path."""
    lowered = [unicodedata.normalize("NFD", name).casefold() for name in names]
    return lowered[3:] if lowered[:3] == _FIRMLINK else lowered


def _on_other_mount(names, table):
    """Whether a path is at or under any mount point whose file system is not one of LOCAL_FILE_SYSTEMS."""
    path = _spelled(names)
    for point, kind in table:
        if kind not in LOCAL_FILE_SYSTEMS:
            at = _spelled([name for name in point.split("/") if name])
            if path[:len(at)] == at:
                return True
    return False


def _mount_table():
    """The Mac's mount table, [(mount point, file system type)], from getfsstat(MNT_NOWAIT); None off
    macOS (no table is checked there). OSError when it cannot be read."""
    if sys.platform != "darwin":
        return None
    libc = ctypes.CDLL(None, use_errno=True)
    getfsstat = libc["getfsstat$INODE64"] if platform.machine() == "x86_64" else libc.getfsstat
    getfsstat.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_int]
    for _ in range(3):  # a mount added between the count and the reading: read again
        count = getfsstat(None, 0, MNT_NOWAIT)
        if count < 0:
            raise OSError(ctypes.get_errno(), "getfsstat failed")
        table = (_StatFS * (count + 1))()
        read = getfsstat(table, ctypes.sizeof(table), MNT_NOWAIT)
        if read < 0:
            raise OSError(ctypes.get_errno(), "getfsstat failed")
        if read <= count:
            return [(os.fsdecode(table[i].f_mntonname), table[i].f_fstypename.decode(errors="replace"))
                    for i in range(read)]
    raise OSError(errno.EAGAIN, "the mount table kept changing")


def _open_problem(error, name, folder):
    """Why a step of the walk could not be opened: a link (ELOOP under O_NOFOLLOW, or a link where a
    folder was opened, looked at without following it), nothing there, or anything else."""
    try:
        link = error.errno == errno.ELOOP or stat.S_ISLNK(os.stat(name, dir_fd=folder, follow_symlinks=False).st_mode)
    except OSError:
        link = False
    if link:
        return "pdf_link"
    if isinstance(error, (FileNotFoundError, NotADirectoryError)):
        return "pdf_missing"
    return "pdf_unreadable"


def _place(info, kind):
    """Why an opened folder or file is not on this Mac's own disk (its stat and its file system's type:
    only LOCAL_FILE_SYSTEMS are), or None."""
    if kind not in LOCAL_FILE_SYSTEMS:
        return "pdf_on_network"
    if getattr(info, "st_flags", 0) & SF_DATALESS:
        return "pdf_not_downloaded"
    return None


def _file_system_type(fd):
    """The type name of the file system an open descriptor is on (apfs, smbfs, ...), or None off macOS:
    data_folder.file_system_type, by descriptor. A descriptor whose file system cannot be read is never
    taken for local: OSError, which the walk reads as pdf_unreadable."""
    if sys.platform != "darwin":
        return None
    libc = ctypes.CDLL(None, use_errno=True)
    fstatfs = libc["fstatfs$INODE64"] if platform.machine() == "x86_64" else libc.fstatfs
    fstatfs.argtypes = [ctypes.c_int, ctypes.POINTER(_StatFS)]
    info = _StatFS()
    if fstatfs(fd, ctypes.byref(info)) != 0:
        raise OSError(ctypes.get_errno(), "fstatfs failed")
    return info.f_fstypename.decode(errors="replace")


def _pdf_child(children):
    """The item's PDF attachment's key: a stored file before a linked one, then the earliest added."""
    found = []
    for data in _rows(children):  # each read by the checked reader: any other shape fails the import
        kind = _field(data, "itemType", TEXT)
        content = _field(data, "contentType", TEXT, optional=True)
        link = _field(data, "linkMode", TEXT, optional=True)
        added = _field(data, "dateAdded", TEXT, optional=True) or ""
        if kind == "attachment" and content == "application/pdf" and link in _LINKS:
            found.append((link == "linked_file", added, data["key"]))
    return min(found)[2] if found else None


def _record(item):
    """(the item's CSL JSON as S1-14 keeps a record, or None without a visible title; how many of its
    values were left out as not what their field takes). Its DOI is kept only when the DOI field is,
    whole, a DOI (S1-14's rule); its arXiv ID only from Zotero's archiveID (_archive_id)."""
    csl = _field(item, "csljson", OBJECT)  # its fields: S1-14's reader's
    found = next(reference_files._csl_json(json.dumps([csl]), lambda: None), None)
    if found is None:
        return None, 0
    if (arxiv := _archive_id(_field(item["data"], "archiveID", TEXT, optional=True))) is not None:
        found["arxiv"] = arxiv  # kept as S1-14 keeps a BibTeX eprint, so identifier() reads it
    return reference_files._finish(found)


ARXIV_PREFIX = "arXiv:"


def _archive_id(value):
    """The arXiv ID a Zotero record gives in its archiveID field (Harold, 2026-10-17): its whole value,
    only ASCII spaces, tabs and line ends taken from its ends, is exactly "arXiv:" followed by an ID that
    S1-14's strict pattern takes whole (a version allowed). Anything else there is not an identifier and
    is ignored; the CSL `number` Zotero exports it as is never read for one."""
    if not isinstance(value, str):
        return None
    whole = value.strip(" \t\r\n")
    rest = whole.removeprefix(ARXIV_PREFIX)
    if rest == whole or rest != rest.strip(" \t\r\n"):  # no prefix, or space after it
        return None
    return reference_files._arxiv(rest)[0]


async def _fetch_item(harness, active, client, library, key, server_id, content, hold=None):
    """What Zotero holds of one item: None when it is gone (or a note, an attachment, in the trash),
    else (its record, values left out, its PDF's stored SHA-256 or None, why its PDF was not read or None)."""
    item, _ = await _read(client, f"/api/{library}/items/{key}?format=json&include=data,csljson", server_id, RETRIES)
    if item is NOT_FOUND:  # 404: gone since it was browsed
        return None
    data = _field(_as(item, OBJECT), "data", OBJECT)
    if _key(data) != key:
        raise Unreachable("zotero_unavailable")  # not the item asked for: no answer to go by
    if _field(data, "itemType", TEXT) in _NOT_ITEMS or _field(data, "deleted", (FLAG, INTEGER), optional=True):
        return None
    csl, dropped = _record(item)
    children, _ = await _read(client, f"/api/{library}/items/{key}/children?format=json", server_id, RETRIES)
    child = _pdf_child([] if children is NOT_FOUND else children)  # a 404: none; null or any other shape fails
    if child is None:
        return csl, dropped, None, None
    # Zotero's own answers that the attachment has no file: 400 (not a file attachment), 404 (no such
    # attachment), or a 200 with no file URL ("false"), each read as pdf_missing. Any other failure (no
    # answer after the tries, a redirect, an answer past the bound) is no answer: the import stops
    # (zotero_unavailable) before the item is written or done, so a resume or a new import reads it again.
    url, _ = await _read(client, f"/api/{library}/items/{child}/file/view/url", server_id, RETRIES, none=(400, 404))
    try:
        text = None if url is NOT_FOUND else url.decode("utf-8")  # as sent: nothing trimmed
    except UnicodeDecodeError:
        raise Unreachable("zotero_unavailable") from None
    # Its answer is text: a URL, or, for an attachment with no file, getLocalFileURL's false, which Zotero's
    # server sends as an empty body (it writes a body only when there is one), or as "false". Any other
    # answer (null, {}, a word) is no answer to go by, never "no file".
    if text in ("", "false"):
        text = None
    try:  # anything but a file URL of the form Zotero gives fails the import (Harold, 2026-10-20)
        path = None if text is None else file_path(text)
    except ValueError:
        raise Unreachable("zotero_unavailable") from None
    def read_and_store():  # run to its end (Harness.work), so nothing of it outlasts the run's record
        data, why = read_attachment(path)
        if data is None or active.cancel_requested.is_set():
            return None, why
        return content.put(data, extraction.PDF, hold), None  # held until its item commits; else collected once idle

    sha256, why = await harness.work(active, read_and_store)
    return csl, dropped, sha256, why


def register(harness, content):
    """The zotero_import workflow, as the harness's local background work."""
    async def import_run(harness, active, project_id, inputs):
        return await _import(harness, active, project_id, inputs, content)

    harness.workflows["zotero_import"] = import_run
    # Its lookup batch closes with its terminal record, however it ends, and is looked up then.
    harness.endings["zotero_import"] = lambda conn, active, project_id: _close_batch(conn, harness, project_id,
                                                                                      active.run_id)


class _Ended(Exception):
    """The run was revoked, or ended, before an item's transaction."""


class _Stopped(Exception):
    """A cancellation was requested before an item's transaction: it writes nothing."""


async def _import(harness, active, project_id, inputs, content):
    run_id, db = active.run_id, harness.db
    library, server_id = inputs["library"], inputs.get("server_id")
    if not await asyncio.to_thread(enabled, harness.data_dir):
        raise RunOutcome("failed", "zotero_off")
    done = list(inputs.get("done") or [])
    active.progress = {"done": len(done), "total": len(inputs["items"])}

    def admit(conn):
        """The gate's check of each request: the run may still send. No review lock: nothing leaves this Mac."""
        return _running(conn, run_id) and not _revoked(conn, run_id) and conn.execute(
            "SELECT 1 FROM projects WHERE id = ?", (project_id,)).fetchone() is not None

    try:
        async with harness.gate.async_client(project_id, zotero=True, admit=admit, follow_redirects=False) as client:
            for key in [k for k in inputs["items"] if k not in done]:
                with content.hold() as held:  # its file kept from collection until its transaction commits
                    fetched = await _fetch_item(harness, active, client, library, key, server_id, content, held)
                    try:
                        await harness.record_background(8, lambda conn, ids: _commit(
                            conn, ids, harness, active, project_id, inputs, key, fetched))
                    except _Ended:
                        raise RunOutcome("cancelled", "project_changed", "revoked") from None
                    except _Stopped:
                        raise asyncio.CancelledError() from None
                    except AdmissionError:  # the app is closing: it stays as it is, read interrupted
                        raise RunOutcome("interrupted") from None
                done.append(key)
                active.progress = {"done": len(done), "total": len(inputs["items"])}
    except Unreachable as unreachable:
        raise RunOutcome("failed", unreachable.code) from None
    except OutboundDenied:  # revoked meanwhile (any other refusal is Unreachable): nothing more is sent
        raise RunOutcome("cancelled", "project_changed", "revoked") from None
    return await asyncio.to_thread(db.read, lambda conn: _summary(conn, run_id)), None


def _commit(conn, ids, harness, active, project_id, inputs, key, fetched):
    """One item's effect, in its own transaction, while the run is running, not revoked and not
    cancelled; its outcome recorded as a step event, and its key added to the items done."""
    run_id = active.run_id
    if not _running(conn, run_id) or _revoked(conn, run_id):
        raise _Ended()
    if active.cancel_requested.is_set():
        raise _Stopped()  # rolled back; the run ends cancelled
    ids, used = iter(ids), []

    def recorded(conn, project, workflow, run_inputs):  # a run that starts with the commit
        run = next(ids)
        used.append(run)
        conn.execute("INSERT INTO runs (id, project_id, kind, workflow, inputs) VALUES (?, ?, 'background', ?, ?)",
                     (run, project, workflow, json.dumps(run_inputs)))
        return run

    library = inputs["library"]
    source_key = f"{library}/items/{key}"
    step = {"key": key, "material_id": None, "outcome": "not_found", "pdf": None, "version": False}
    looked_up = None  # (material, version, record, whether a file came) to look up
    if fetched is not None:
        csl, dropped, sha256, why = fetched
        step.update(pdf=why, left_out=dropped)
        found = conn.execute("SELECT id, csl, checked_by, lookup_filled, title, evidence_type FROM materials"
                             " WHERE project_id = ? AND source = 'zotero' AND source_key = ?",
                             (project_id, source_key)).fetchone()
        if found is not None:
            looked_up = _update(conn, harness, project_id, found, csl, sha256, step, recorded)
        elif csl is None:
            step["outcome"] = "no_title"
        else:
            looked_up = _new(conn, harness, project_id, inputs, key, source_key, csl, sha256, step, recorded)
    locked = conn.execute("SELECT review_lock FROM projects WHERE id = ?", (project_id,)).fetchone()[0]
    batch = conn.execute("SELECT json_extract(inputs, '$.batch') FROM runs WHERE id = ?", (run_id,)).fetchone()[0]
    # Looked up as a file added through Add is: by its record's identifier, else by its file's text once read
    # (the lookup waits for that reading), in the import's one batch.
    if looked_up is not None and not locked and (reference_files.identifier(looked_up[2]) is not None or looked_up[3]):
        material, version = looked_up[:2]
        if batch is None or _add_to_batch(conn, project_id, batch, {material: version}, True,
                                          harness.registry) is None:
            batch = recorded(conn, project_id, "lookup", {
                "material_ids": [material], "versions": {material: version}, "origin": None, "batch": "zotero",
                "open": True, "touched": time.time(), "import_run": run_id})  # open while the import runs
    if step["outcome"] in ("imported", "updated"):  # the project changed: it comes first in the list, as other imports do
        conn.execute("UPDATE projects SET updated_at = ? WHERE id = ?", (utc_now(), project_id))
    _event(conn, run_id, "step_finished", step)
    conn.execute("UPDATE runs SET inputs = json_set(json_insert(inputs, '$.done[#]', ?), '$.batch', ?) WHERE id = ?",
                 (key, batch, run_id))
    return step, used


def _update(conn, harness, project_id, found, csl, sha256, step, recorded):
    """An item imported before: its details replaced by its record when they changed (and the
    researcher has not edited them), and its PDF, when it is none of its files, its next version.
    Returns (material, version, its record or {}, whether its current version has a file) to look up
    when its details were replaced or a file added, else None."""
    material, stored, checked_by, filled, title, evidence = found
    step["material_id"] = material
    changed = False
    own = json.loads(stored or "{}")
    for field in json.loads(filled or "[]"):  # a lookup's fields are not the record's (S1-14's lookup_filled)
        own.pop(field, None)
    if csl is not None and checked_by != "researcher":
        if own != csl:
            changed = True
            # its retraction check and resolution were about the identifier it had: a DOI or an arXiv ID changed or gone
            moved = reference_files.identifier(own) != reference_files.identifier(csl)
            conn.execute("UPDATE materials SET title = ?, csl = ?, evidence_type = ?, lookup_filled = NULL, updated_at = ?"
                         + (", resolved_at = NULL, retraction = 'unknown', retraction_checked_at = NULL" if moved else "")
                         + " WHERE id = ?", (csl["title"], json.dumps(csl), references._evidence(csl, evidence),
                                            utc_now(), material))
            if csl["title"] != title:  # the index text carries the title (S1-17)
                search.retitled(conn, harness, project_id, material, recorded)
    current = conn.execute("SELECT id, file_sha256 FROM material_versions WHERE material_id = ? AND is_current = 1",
                           (material,)).fetchone()
    version = current[0] if current else None
    if sha256 is not None and conn.execute("SELECT 1 FROM material_versions WHERE material_id = ? AND file_sha256 = ?",
                                           (material, sha256)).fetchone() is None:  # one of its files already: none
        version, _ = add_version(conn, harness, project_id, material, sha256, extraction.PDF, None, recorded)
        step["version"] = True
        # what the old file's lookup gave it goes in add_version (materials._forget_file_lookup)
    elif sha256 is None and step.get("pdf") and current is not None and current[1] is not None:
        step["kept"] = True  # its PDF could not be read this time: the paper keeps the file it had
    step["outcome"] = "updated" if changed or step["version"] else "unchanged"
    if not (changed or step["version"]) or version is None:
        return None
    # Looked up again whenever its record or its file changed: by the record's identifier, else by the
    # text of the file it has (a lookup's fields were cleared with the record it completed).
    has_file = conn.execute("SELECT file_sha256 IS NOT NULL FROM material_versions WHERE id = ?", (version,)).fetchone()[0]
    return material, version, csl or {}, bool(has_file)


def _new(conn, harness, project_id, inputs, key, source_key, csl, sha256, step, recorded):
    """A new item: already in the project (its PDF a paper's current file, or a duplicate with nothing
    to add), asked about (a duplicate that would add something: S1-14's merge), or a new paper.
    Returns (material, version, record, whether it has a file) to look up for a new paper, else None."""
    if sha256 is not None and (same := conn.execute(
            "SELECT m.id FROM materials m JOIN material_versions v ON v.material_id = m.id AND v.is_current = 1"
            " WHERE m.project_id = ? AND v.file_sha256 = ?", (project_id, sha256)).fetchone()) is not None:
        step.update(outcome="already", material_id=same[0], pdf=None)
        return None
    if not inputs.get("keep_both"):  # Keep both, answered on its merge: its own paper, unasked
        papers = references.Duplicates(conn, project_id)
        if (duplicate := papers.find(csl)) is not None:
            material, matched = duplicate
            step.update(material_id=material, pdf=None)  # the reason is about this item, which has no paper of its own
            if not references.fills(*papers.details(material), csl):
                step["outcome"] = "already"
                return None
            step["outcome"] = "waiting"
            if conn.execute("SELECT 1 FROM runs WHERE project_id = ? AND workflow = 'merge' AND status = 'running'"
                            " AND json_extract(inputs, '$.held.zotero.library') = ?"
                            " AND json_extract(inputs, '$.held.zotero.items[0]') = ?",
                            (project_id, inputs["library"], key)).fetchone() is None:  # asked once
                recorded(conn, project_id, "merge", {
                    "material_ids": [material], "matched": matched, "origin": None,
                    "held": {"record": csl, "zotero": {"library": inputs["library"], "items": [key],
                                                       "server_id": inputs.get("server_id")}}})
            return None
    material = new_id()
    conn.execute("INSERT INTO materials (id, project_id, title, csl, source, source_key, evidence_type, proposed_by)"
                 " VALUES (?, ?, ?, ?, 'zotero', ?, ?, 'researcher')",
                 (material, project_id, csl["title"], json.dumps(csl), source_key,
                  "full_text" if sha256 else references._evidence(csl)))
    version, _ = add_version(conn, harness, project_id, material, sha256, extraction.PDF if sha256 else None, None,
                             recorded)
    step.update(outcome="imported", material_id=material, version=sha256 is not None)
    return material, version, csl, sha256 is not None


def _close_batch(conn, harness, project_id, run_id):
    """The import's lookup batch closes with its terminal record, however the import ends: its
    papers are looked up now. (One a crash interrupted closes itself, BATCH_IDLE_SECONDS later.)"""
    row = conn.execute("SELECT json_extract(inputs, '$.batch') FROM runs WHERE id = ?", (run_id,)).fetchone()
    if row is not None and (batch := row[0]) is not None:
        _add_to_batch(conn, project_id, batch, {}, False, harness.registry)


def _summary(conn, run_id):
    """What the import did, counted from its items' step events: each outcome, the new versions, the
    PDFs not read by reason (pdf: the paper has its details only; pdf_kept: it keeps the file it had),
    and the values left out of the records."""
    summary = {outcome: 0 for outcome in OUTCOMES} | {"versions": 0, "pdf": {}, "left_out": 0}
    for (data,) in conn.execute("SELECT data FROM run_events WHERE run_id = ? AND type = 'step_finished'", (run_id,)):
        step = json.loads(data)
        summary[step["outcome"]] += 1
        summary["versions"] += bool(step.get("version")) and step["outcome"] == "updated"
        summary["left_out"] += step.get("left_out") or 0
        if step.get("pdf"):  # not read: a paper with its details only, or one that keeps the file it had
            counted = summary.setdefault("pdf_kept", {}) if step.get("kept") else summary["pdf"]
            counted[step["pdf"]] = counted.get(step["pdf"], 0) + 1
    return summary


def run_details(conn, run_id):
    """What the background-run list shows of an import: the papers it made or updated (up to three
    titles, with their count), no question and no Retry (importing again from the browser does it)."""
    ids = [m for (m,) in conn.execute("SELECT DISTINCT json_extract(data, '$.material_id') FROM run_events"
                                      " WHERE run_id = ? AND type = 'step_finished'"
                                      " AND json_extract(data, '$.outcome') IN ('imported', 'updated')", (run_id,)) if m]
    titles = [t for (t,) in conn.execute("SELECT title FROM materials WHERE id IN (SELECT value FROM json_each(?))"
                                         " ORDER BY created_at LIMIT 3", (json.dumps(ids),))]
    (count,) = conn.execute("SELECT count(*) FROM materials WHERE id IN (SELECT value FROM json_each(?))",
                            (json.dumps(ids),)).fetchone()
    return {"materials": {"titles": titles, "count": count}, "ask": None, "retryable": False}
