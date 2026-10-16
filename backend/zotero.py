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
file URL. Zotero's local API serves no file bytes: it names the file on disk (a file:// URL). Scholia
reads that local file itself (read_attachment), only when it is on this Mac's own disk, a regular PDF
of at most extraction.MAX_FILE_BYTES; otherwise the item is imported with its details only, and the
reason recorded. The record is read with S1-14's CSL JSON reader and bounds (backend/reference_files.py),
so its identifier follows S1-14's rule: a DOI only from the record's DOI field, whole, else an arXiv ID
only from Zotero's archiveID field when it is, whole, "arXiv:" and an ID (Harold, 2026-10-17); never
from a URL or `number`. Each item is written in its own transaction, while the run is running, not revoked and not
cancelled:
- an item already imported into the project (keyed by library and item key, `source_key`
  "<library>/items/<KEY>") is updated: its details replaced by the record when they changed and the
  researcher has not edited them, and a PDF that is not its current file becomes its next version;
- a new item that duplicates a paper of the project (S1-14's `references.Duplicates`) is reported
  already there when it would add nothing, else asked about through S1-14's merge run (Keep both
  imports it as its own paper, through a one-item import of its own); a PDF that is already a paper's
  current file in the project is reported already there, as an upload is;
- otherwise it becomes the project's paper, source zotero, with its first version (its PDF, read in
  a reading run, or none).
Papers it made or whose details it replaced, carrying an identifier, join one lookup batch for the
import (S1-13's, batch `zotero`): a Local only project asks once, a review-locked one never looks up.
Its step events record each item's outcome, from which the summary is counted. Its inputs name the
papers it was admitted to update (`material_ids`: deleting one revokes it) and the items done, so a
restart goes on from the next item and never makes a paper deleted since again.
"""

import asyncio
import json
import os
import re
import stat
import time
from urllib.parse import unquote, urlsplit

import httpx
from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel, Field

from backend import extraction, reference_files, references, search
from backend.data_folder import NETWORK_FILE_SYSTEMS, file_system_type
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
_NOT_ITEMS = ("note", "attachment", "annotation")  # never offered or imported as a paper
_DATABASES = (".sqlite", ".sqlite3", ".sqlite-wal", ".sqlite-shm", "-journal", ".sqlite.bak")  # file names never read
# macOS's automounted network folders (and their firmlinked spelling under /System/Volumes/Data): a path under
# one is refused before it is resolved, since resolving it mounts the share. ponytail: a link elsewhere that
# points into one, or a ".." after a link, is resolved first (realpath mounts it), then refused by its file
# system's type; a no-follow walk of each component if that ever matters.
_AUTOMOUNTS = ("net", "home", "network")
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


async def _read(client, path, server_id=None, retries=()):
    """The body of a 200 answer, parsed as JSON when it is JSON (text otherwise), and its headers; a
    404 is None. Unreachable otherwise: 403 api_off, 412 zotero_changed (another Zotero than the one
    browsed), anything else unavailable. retries: delays to try again after when Zotero gave no answer
    or failed (5xx)."""
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
        if status == 404:
            return None, headers
        if status == 403:
            raise Unreachable("zotero_api_off")
        if status == 412:
            raise Unreachable("zotero_changed")
        if status is not None and status < 500 or delay is None:
            raise Unreachable("zotero_unavailable")
        await asyncio.sleep(delay)


def _text(value, limit):
    return reference_files._text(value, limit)


def _libraries(groups):
    """My Library and each group, as the browser lists them: {id, name} (name None for My Library)."""
    found = [{"id": "users/0", "name": None}]
    for group in groups if isinstance(groups, list) else []:
        if not isinstance(group, dict):
            continue
        number = group.get("id")
        name = group["data"].get("name") if isinstance(group.get("data"), dict) else None
        if type(number) is int and _LIBRARY.fullmatch(f"groups/{number}"):
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
    if groups is None or headers.get("zotero-api-version") != "3":  # Zotero before 7, or another API
        return {"state": "unsupported"}
    server_id = headers.get("zotero-server-id")
    return {"state": "ready", "libraries": _libraries(groups),
            "server_id": server_id if server_id and _SERVER_ID.fullmatch(server_id) else None}


@router.get("/api/zotero/collections")
async def collections(request: Request, library: str = ""):
    """The library's collections, read 100 at a time: {key, name, parent} (parent None at the top)."""
    state, library = request.app.state.scholia, _library(library)
    found = []
    for page in range(COLLECTION_PAGES):
        answer, headers = await _browse(state, f"/api/{library}/collections?format=json&limit=100&start={page * 100}")
        rows = answer if isinstance(answer, list) else []
        for row in rows:
            data = row.get("data") if isinstance(row, dict) else None
            if isinstance(data, dict) and isinstance(data.get("key"), str) and _KEY.fullmatch(data["key"]):
                parent = data.get("parentCollection")
                found.append({"key": data["key"], "name": _text(data.get("name"), TITLE_CHARS) or data["key"],
                              "parent": parent if isinstance(parent, str) and _KEY.fullmatch(parent) else None})
        if len(rows) < 100:
            return {"collections": found}
    raise _refused(502, "zotero_unavailable", "The library has more collections than Scholia reads")


def _row(item):
    """An item as the browser lists it, or None for a note, an attachment or anything unreadable."""
    data = item.get("data") if isinstance(item, dict) else None
    meta = item.get("meta") if isinstance(item, dict) and isinstance(item.get("meta"), dict) else {}
    if not isinstance(data, dict) or data.get("itemType") in _NOT_ITEMS or not isinstance(data.get("key"), str) \
            or not _KEY.fullmatch(data["key"]):
        return None
    title = next((t for t in (_text(data.get(f), TITLE_CHARS) for f in ("title", "caseName", "subject", "nameOfAct"))
                  if t), None)
    date = meta.get("parsedDate")
    return {"key": data["key"], "title": title, "creators": _text(meta.get("creatorSummary"), CREATORS_CHARS),
            "year": int(date[:4]) if isinstance(date, str) and re.match(r"[0-9]{4}", date) else None,
            "type": data["itemType"] if isinstance(data.get("itemType"), str) and len(data["itemType"]) <= 40 else None}


@router.get("/api/zotero/items")
async def items(request: Request, library: str = "", collection: str | None = None, start: int = 0):
    """A page of the library's top-level items (or a collection's), with the total and the next page's start."""
    state, library = request.app.state.scholia, _library(library)
    if collection is not None and not _KEY.fullmatch(collection):
        raise _refused(400, "invalid_request", "No such Zotero collection")
    start = max(0, min(start, 999_950))
    where = f"collections/{collection}/" if collection else ""
    answer, headers = await _browse(state, f"/api/{library}/{where}items/top?format=json&limit={PAGE}&start={start}")
    if answer is None:
        raise _refused(404, "not_found", "No such Zotero library or collection")
    rows = answer if isinstance(answer, list) else []
    total = headers.get("total-results", "")
    total = int(total) if re.fullmatch(r"[0-9]{1,9}", total) else None
    more = total > start + PAGE if total is not None else len(rows) == PAGE
    return {"items": [row for row in map(_row, rows) if row], "total": total, "next": start + PAGE if more else None}


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


def read_attachment(text, fs_type=file_system_type):
    """(the PDF's bytes, None) or (None, why not) for the file URL Zotero named for an attachment.
    Only a file on this Mac's own disk is read: a file: URL with no host, its path resolved whole,
    every link followed, before any check, and every check made on that final file (never on the name
    Zotero gave); not a file named like a database (zotero.sqlite and its kin), not on a network file
    system, not a cloud placeholder whose contents are elsewhere; a regular file, opened read-only
    without following a link put in its place since, checked again once open, of at most
    MAX_FILE_BYTES (at most one byte more is read), that is a PDF. Nothing is written."""
    if not isinstance(text, str):
        return None, "pdf_missing"
    try:
        parts = urlsplit(text.strip())
    except ValueError:  # a host it cannot read
        return None, "pdf_not_local"
    if parts.scheme != "file":
        return None, "pdf_missing" if not parts.scheme else "pdf_not_local"
    if parts.netloc or parts.query or parts.fragment:
        return None, "pdf_not_local"
    try:
        path = unquote(parts.path, errors="strict")
    except UnicodeDecodeError:
        return None, "pdf_not_local"
    if not path.startswith("/") or "\x00" in path:
        return None, "pdf_not_local"
    names = [name.lower() for name in os.path.normpath(re.sub("/+", "/", path)).split("/") if name]
    if names[:3] == ["system", "volumes", "data"]:
        names = names[3:]
    if names[:1] and names[0] in _AUTOMOUNTS:  # never even resolved: it would mount a share
        return None, "pdf_on_network"
    try:
        real = os.path.realpath(path, strict=True)
        info = os.stat(real)
    except FileNotFoundError:
        return None, "pdf_missing"
    except OSError:  # a loop of links, or a folder on the way that cannot be read
        return None, "pdf_unreadable"
    if os.path.basename(real).lower().endswith(_DATABASES):
        return None, "pdf_not_a_pdf"  # never Zotero's database or its kin, whatever a link names
    if getattr(info, "st_flags", 0) & SF_DATALESS:
        return None, "pdf_not_downloaded"
    if fs_type(real) in NETWORK_FILE_SYSTEMS:
        return None, "pdf_on_network"
    if not stat.S_ISREG(info.st_mode):
        return None, "pdf_not_a_file"
    if info.st_size > extraction.MAX_FILE_BYTES:
        return None, "pdf_too_large"
    try:
        fd = os.open(real, os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW)  # a FIFO never waits; a link put there since is refused
    except FileNotFoundError:
        return None, "pdf_missing"
    except OSError:
        return None, "pdf_unreadable"
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode):
            return None, "pdf_not_a_file"
        if getattr(info, "st_flags", 0) & SF_DATALESS:
            return None, "pdf_not_downloaded"
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
    if not data.startswith(b"%PDF-"):
        return None, "pdf_not_a_pdf"
    return bytes(data), None


def _pdf_child(children):
    """The item's PDF attachment's key: a stored file before a linked one, then the earliest added."""
    found = []
    for child in children if isinstance(children, list) else []:
        data = child.get("data") if isinstance(child, dict) else None
        if isinstance(data, dict) and data.get("itemType") == "attachment" \
                and data.get("contentType") == "application/pdf" and data.get("linkMode") in _LINKS \
                and isinstance(data.get("key"), str) and _KEY.fullmatch(data["key"]):
            found.append((data["linkMode"] == "linked_file", str(data.get("dateAdded") or ""), data["key"]))
    return min(found)[2] if found else None


def _record(item):
    """(the item's CSL JSON as S1-14 keeps a record, or None without a visible title; how many of its
    values were left out as not what their field takes). Its DOI is kept only when the DOI field is,
    whole, a DOI (S1-14's rule); its arXiv ID only from Zotero's archiveID (_archive_id)."""
    csl = item.get("csljson") if isinstance(item, dict) else None
    found = next(reference_files._csl_json(json.dumps([csl if isinstance(csl, dict) else {}]), lambda: None), None)
    if found is None:
        return None, 0
    data = item.get("data") if isinstance(item.get("data"), dict) else {}
    if (arxiv := _archive_id(data.get("archiveID"))) is not None:
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


async def _fetch_item(harness, active, client, library, key, server_id, content):
    """What Zotero holds of one item: None when it is gone (or a note, an attachment, in the trash),
    else (its record, values left out, its PDF's stored SHA-256 or None, why its PDF was not read or None)."""
    item, _ = await _read(client, f"/api/{library}/items/{key}?format=json&include=data,csljson", server_id, RETRIES)
    data = item.get("data") if isinstance(item, dict) else None
    if not isinstance(data, dict) or data.get("itemType") in _NOT_ITEMS or data.get("deleted"):
        return None
    csl, dropped = _record(item)
    children, _ = await _read(client, f"/api/{library}/items/{key}/children?format=json", server_id, RETRIES)
    child = _pdf_child(children)
    if child is None:
        return csl, dropped, None, None
    try:
        url, _ = await _read(client, f"/api/{library}/items/{child}/file/view/url", server_id, RETRIES)
    except Unreachable as unreachable:
        if unreachable.code != "zotero_unavailable":  # a 400 for a file it no longer has: no file
            raise
        url = None
    try:
        text = url.decode("utf-8") if url is not None else None
    except UnicodeDecodeError:
        text = None
    def read_and_store():  # run to its end (Harness.work), so nothing of it outlasts the run's record
        data, why = read_attachment(text)
        if data is None or active.cancel_requested.is_set():
            return None, why
        return content.put(data, extraction.PDF), None  # collected later if no record names it

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
                fetched = await _fetch_item(harness, active, client, library, key, server_id, content)
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
    looked_up = None  # (material, version) to look up
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
    if looked_up is not None and not locked and reference_files.identifier(looked_up[2]) is not None:
        material, version = looked_up[:2]
        if batch is None or _add_to_batch(conn, project_id, batch, {material: version}, True,
                                          harness.registry) is None:
            batch = recorded(conn, project_id, "lookup", {
                "material_ids": [material], "versions": {material: version}, "origin": None, "batch": "zotero",
                "open": True, "touched": time.time()})
    _event(conn, run_id, "step_finished", step)
    conn.execute("UPDATE runs SET inputs = json_set(json_insert(inputs, '$.done[#]', ?), '$.batch', ?) WHERE id = ?",
                 (key, batch, run_id))
    return step, used


def _update(conn, harness, project_id, found, csl, sha256, step, recorded):
    """An item imported before: its details replaced by its record when they changed (and the
    researcher has not edited them), and its PDF, when it is not its current file, its next version.
    Returns (material, version, record) to look up when its details were replaced, else None."""
    material, stored, checked_by, filled, title, evidence = found
    step["material_id"] = material
    changed = False
    if csl is not None and checked_by != "researcher":
        own = json.loads(stored or "{}")
        for field in json.loads(filled or "[]"):  # a lookup's fields are not the record's
            own.pop(field, None)
        if own != csl:
            changed = True
            moved = own.get("DOI") != csl.get("DOI")  # its retraction check and resolution were about the old one
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
    step["outcome"] = "updated" if changed or step["version"] else "unchanged"
    return (material, version, csl) if changed and version is not None else None


def _new(conn, harness, project_id, inputs, key, source_key, csl, sha256, step, recorded):
    """A new item: already in the project (its PDF a paper's current file, or a duplicate with nothing
    to add), asked about (a duplicate that would add something: S1-14's merge), or a new paper.
    Returns (material, version, record) to look up for a new paper, else None."""
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
    return material, version, csl


def _close_batch(conn, harness, project_id, run_id):
    """The import's lookup batch closes with its terminal record, however the import ends: its
    papers are looked up now. (One a crash interrupted closes itself, BATCH_IDLE_SECONDS later.)"""
    row = conn.execute("SELECT json_extract(inputs, '$.batch') FROM runs WHERE id = ?", (run_id,)).fetchone()
    if row is not None and (batch := row[0]) is not None:
        _add_to_batch(conn, project_id, batch, {}, False, harness.registry)


def _summary(conn, run_id):
    """What the import did, counted from its items' step events: each outcome, the new versions, the
    PDFs not read by reason, and the values left out of the records."""
    summary = {outcome: 0 for outcome in OUTCOMES} | {"versions": 0, "pdf": {}, "left_out": 0}
    for (data,) in conn.execute("SELECT data FROM run_events WHERE run_id = ? AND type = 'step_finished'", (run_id,)):
        step = json.loads(data)
        summary[step["outcome"]] += 1
        summary["versions"] += bool(step.get("version")) and step["outcome"] == "updated"
        summary["left_out"] += step.get("left_out") or 0
        if step.get("pdf"):
            summary["pdf"][step["pdf"]] = summary["pdf"].get(step["pdf"], 0) + 1
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
