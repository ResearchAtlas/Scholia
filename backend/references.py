"""Reference file import (slice-1 spec F3b, sections 5, 6.2, 10 and 13; ticket 06's answer; S1-14).

`POST /api/projects/{id}/materials/import-references` takes one reference file ({name, data}, its
bytes in base64, as every upload) and answers with what the import did. The file is read in a child
process under its own memory and time ceilings (backend/reading.py `references`, parsing with
backend/reference_files.py) and is never stored: each record becomes CSL JSON, kept as its paper's
details. One transaction, which checks the project still exists, then writes:
- for each record that matches none of the project's papers (nor one this import made before it), a
  metadata-only paper (`new_material`): source reference_file, proposed by the researcher, its first
  version with no file (version 0, current), its evidence an abstract when the record has one and
  metadata otherwise. No reading is made, and nothing of it is indexed: an abstract is never a passage.
- for each record that matches a paper (`Duplicates`: by DOI, else by normalized title and year; two
  different DOIs never match) and would add a field that paper lacks (`fills`), a `merge` background
  run, which asks through the shared confirmation (backend/asks.py): Merge, or Keep both. A record
  that would add nothing is reported as already in the project, with no question.
- one `lookup` run (backend/materials.py) for the papers it made that carry a DOI or an arXiv ID, as
  for files added (a Local only project asks first; none in a review-locked project). Its question
  says it covers the papers this reference file added (inputs `batch`).
A request that fails validation, or a file that cannot be read, writes nothing.

A `merge` run holds its record in its inputs (`held`) only while it runs: the harness takes it out
with the run's terminal record, however the run ends (backend/runs.py), and its question shows the
two papers as they are when it is listed (asks.SHOWN), so no record is kept in the question itself.
The run names the paper it would merge into in `inputs.material_ids`, the deletion service's scope
link, so deleting that paper revokes it and its question goes with it. It sends nothing, so a
stricter level or the review lock does not stop it (governance.UNSENT_WORKFLOWS); its question, asked
under the earlier policy, is withdrawn and asked again under the current one. The answer's effect
is applied with the run's terminal record, once: Merge fills the paper's missing fields from the
record (`fills`; a paper whose details the researcher edited keeps the details form's fields as they
are) and its evidence follows what it now holds (a file stays full text); Keep both makes the record
its own paper, as at import, not looked up. Cancel leaves the record out.
"""

import asyncio
import base64
import binascii
import contextlib
import json
import unicodedata

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel, Field

from backend import asks, extraction, reading, reference_files
from backend.db import new_id, utc_now
from backend.local_guard import MAX_BODY
from backend.materials import _FORM_FIELDS, _no_store, _refused, _to_end
from backend.runs import RunOutcome, _running, _revoked
from backend.settings import visible


OPTIONS = ("merge", "keep_both")
# A waiting merge wakes when its question is answered (ActiveRun.answered), and otherwise looks this
# often at whether the project's protection changed, so its question is asked again under the new one.
# ponytail: one small read for each waiting merge each time; a notice from the protection change if
# imports leave thousands waiting at once.
POLICY_SECONDS = 10.0
# An import waits this long at most for one of the readings' turns (backend/materials.py READINGS), so the
# children's memory budget holds; past it, it is refused (readings_busy) rather than left hanging.
TURN_SECONDS = 30.0
_STATUS = {"too_many_records": 413, "reference_file_too_large": 413, "memory_limit": 409, "step_limit": 409}
_MESSAGES = {"unsupported_reference_file": "Scholia reads RIS, BibTeX, CSL JSON, NoteExpress and EndNote tagged files",
             "unreadable_file": "The reference file could not be read", "no_records": "The file holds no records",
             "too_many_records": f"The file holds more than {reference_files.MAX_RECORDS} records",
             "reference_file_too_large": "The reference file is larger than Scholia reads",
             "memory_limit": "Reading the file needed more memory than allowed",
             "step_limit": "Reading the file made no progress for a minute"}

router = APIRouter(dependencies=[Depends(_no_store)])


class ReferenceFile(BaseModel):
    name: str = Field(min_length=1, max_length=255)
    data: str = Field(min_length=1, max_length=MAX_BODY)  # base64; its decoded size is checked after


class _Stop(Exception):
    pass


@router.post("/api/projects/{project_id}/materials/import-references", status_code=201)
async def import_references(project_id: str, body: ReferenceFile, request: Request):
    state = request.app.state.scholia
    db, harness = state["db"], state["harness"]
    if visible(body.name) is None:
        raise _refused(400, "invalid_request", "A file needs a name")
    try:
        data = base64.b64decode(body.data, validate=True)
    except (binascii.Error, ValueError):
        raise _refused(400, "invalid_request", "A file's data is not base64") from None
    if len(data) > reference_files.MAX_BYTES:
        raise _refused(413, "reference_file_too_large", _MESSAGES["reference_file_too_large"])

    def exists(conn):
        return conn.execute("SELECT 1 FROM projects WHERE id = ?", (project_id,)).fetchone() is not None

    if not await asyncio.to_thread(db.read, exists):
        raise _refused(404, "not_found", "No such project")

    def stop():
        if harness.registry.closed:
            raise _Stop()

    async def parse():
        # One of the readings' turns (materials.READINGS), so the children's memory stays within the
        # budget backend/reading.py sets out, waited for TURN_SECONDS at most.
        async with asyncio.timeout(TURN_SECONDS):
            await harness.readings.acquire()
        try:
            return await asyncio.to_thread(reading.references, data, stop)
        finally:
            harness.readings.release()

    try:
        # Held by the harness as its detached work, so a shutdown waits for its child to end and be
        # reaped; followed to its end when the request goes away (nothing is written then).
        records, skipped, kind = await _to_end(harness._detach(parse()))
    except _Stop:
        raise _refused(503, "shutting_down", "The app is closing") from None
    except TimeoutError:  # every reading turn stayed taken: nothing was read or written
        raise _refused(409, "readings_busy", "Scholia is reading other files; import this one once they are read") from None
    except extraction.Unreadable as unreadable:
        code = unreadable.code if unreadable.code in _MESSAGES else "unreadable_file"
        raise _refused(_STATUS.get(code, 400), code, _MESSAGES[code]) from None
    except (reading.ChildError, MemoryError):  # logged there, as its type and place
        raise _refused(500, "internal", "The reference file could not be read") from None
    del data

    def record(conn, ids):  # ids: one for each record's merge run and one for the lookup, each used only if needed
        if not exists(conn):
            raise _refused(404, "not_found", "No such project")
        ids, used = iter(ids), []
        locked = conn.execute("SELECT review_lock FROM projects WHERE id = ?", (project_id,)).fetchone()[0]
        papers = Duplicates(conn, project_id)
        asking = {}  # the merges waiting already, by their paper and record: the same record is asked about once
        for run, inputs in conn.execute("SELECT id, inputs FROM runs WHERE project_id = ? AND workflow = 'merge'"
                                        " AND status = 'running'", (project_id,)).fetchall():
            inputs = json.loads(inputs or "{}")
            if (held := (inputs.get("held") or {}).get("record")) is not None:
                asking[inputs["material_ids"][0], json.dumps(held, sort_keys=True)] = run
        imported, waiting, already, looked_up, reported = [], [], [], {}, set()
        for csl in records:
            if (found := papers.find(csl)) is not None:
                material, matched = found
                if not fills(*papers.details(material), csl):
                    already.append(material)
                    continue
                if (run := asking.get((material, json.dumps(csl, sort_keys=True)))) is not None:
                    if run not in reported:  # its twin in this file is counted there already
                        reported.add(run)
                        waiting.append({"run_id": run, "material_id": material})
                    continue
                run = asking[material, json.dumps(csl, sort_keys=True)] = next(ids)
                used.append(run)
                conn.execute("INSERT INTO runs (id, project_id, kind, workflow, inputs) VALUES (?, ?, 'background',"
                             " 'merge', ?)", (run, project_id, json.dumps({
                                 "material_ids": [material], "matched": matched, "origin": None,
                                 "held": {"record": csl}})))
                reported.add(run)
                waiting.append({"run_id": run, "material_id": material})
                continue
            material, version = new_material(conn, project_id, csl)
            papers.add(material, csl)
            imported.append(material)
            if reference_files.identifier(csl) is not None:
                looked_up[material] = version
        lookup_run = None  # a review-locked project never looks identifiers up
        if not locked and looked_up:
            lookup_run = next(ids)
            used.append(lookup_run)
            conn.execute("INSERT INTO runs (id, project_id, kind, workflow, inputs) VALUES (?, ?, 'background',"
                         " 'lookup', ?)", (lookup_run, project_id, json.dumps({
                             "material_ids": list(looked_up), "versions": looked_up, "origin": None,
                             "batch": "reference_file"})))
        conn.execute("UPDATE projects SET updated_at = ? WHERE id = ?", (utc_now(), project_id))
        return {"format": kind, "imported": imported, "waiting": waiting, "already": already,
                "skipped": skipped, "lookup_run_id": lookup_run}, used

    # The runs start with the commit (record_background): none is ever running in the record and not held.
    return await harness.record_background(len(records) + 1, record)


def new_material(conn, project_id, csl, source="reference_file"):
    """A metadata-only paper of the project from a record's CSL JSON, in the caller's transaction:
    the material and its first version, with no file. Returns (material id, version id)."""
    material, version = new_id(), new_id()
    conn.execute("INSERT INTO materials (id, project_id, title, csl, source, evidence_type, proposed_by)"
                 " VALUES (?, ?, ?, ?, ?, ?, 'researcher')",
                 (material, project_id, csl["title"], json.dumps(csl), source, _evidence(csl)))
    conn.execute("INSERT INTO material_versions (id, material_id, seq, file_sha256, is_current) VALUES (?, ?, 0, NULL, 1)",
                 (version, material))
    return material, version


def _evidence(csl, current=None):
    """What a paper's text is (section 4.2): full text once it has a file, else its record's abstract,
    else its metadata. An abstract is never full text."""
    return "full_text" if current == "full_text" else "abstract" if csl.get("abstract") else "metadata"


# Duplicates


def title_key(title):
    """A title as duplicates compare it: its compatibility form, case folded, letters and digits only
    (punctuation, spaces and Chinese punctuation dropped), or None."""
    if not isinstance(title, str):
        return None
    key = "".join(c for c in unicodedata.normalize("NFKC", title).casefold() if c.isalnum())
    return key or None


def _year(csl):
    parts = (csl.get("issued") or {}).get("date-parts") if isinstance(csl.get("issued"), dict) else None
    year = parts[0][0] if isinstance(parts, list) and parts and isinstance(parts[0], list) and parts[0] else None
    return year if isinstance(year, int) else None


def _doi(csl):
    return extraction.clean_doi(csl["DOI"]) if isinstance(csl.get("DOI"), str) else None


class Duplicates:
    """A project's papers as the duplicate check reads them (slice-1 spec F3b): by DOI, then by
    normalized title and year (both without a year match on the title). Two papers with different
    DOIs never match, since a DOI names one work. Papers without details (a file not looked up) match
    nothing. Built once from the project's papers in the caller's transaction; add() takes in the
    papers made since, so a file's records are checked against each other too."""

    def __init__(self, conn, project_id):
        self.dois, self.titles, self.papers = {}, {}, {}
        for material, csl, checked_by in conn.execute(
                "SELECT id, csl, checked_by FROM materials WHERE project_id = ? ORDER BY created_at, id", (project_id,)):
            self.add(material, json.loads(csl) if csl else {}, checked_by == "researcher")

    def add(self, material, csl, edited=False):
        self.papers[material] = (csl, edited)
        if doi := _doi(csl):
            self.dois.setdefault(doi, material)
        if key := title_key(csl.get("title")):
            self.titles.setdefault((key, _year(csl)), []).append(material)

    def details(self, material):
        """(its CSL JSON, whether the researcher edited its details)."""
        return self.papers[material]

    def find(self, csl):
        """(the paper the record duplicates, "doi" or "title"), or None."""
        doi = _doi(csl)
        if doi and doi in self.dois:
            return self.dois[doi], "doi"
        for material in self.titles.get((title_key(csl.get("title")), _year(csl)), []):
            other = _doi(self.papers[material][0])
            if not (doi and other and doi != other):
                return material, "title"
        return None


def duplicate_of(conn, project_id, csl):
    """The project's paper a record duplicates, (material id, "doi" or "title"), or None (S1-15)."""
    return Duplicates(conn, project_id).find(csl)


def fills(existing, edited, record):
    """The fields a record would give a paper: those its details lack, but for the details form's
    fields (title, authors, year, venue, DOI) of a paper the researcher edited, which stay as saved."""
    return {key: value for key, value in record.items()
            if key not in existing and not (edited and key in _FORM_FIELDS)}


# The merge run


def register(harness):
    """The merge workflow, as the harness's local background work; and its question's papers, read
    when it is listed (asks.SHOWN)."""
    harness.workflows["merge"] = _merge_run
    asks.SHOWN["merge"] = _shown


async def _merge_run(harness, active, project_id, inputs):
    run_id, db = active.run_id, harness.db
    record = (inputs.get("held") or {}).get("record")
    if record is None:  # never so while it runs: its record leaves with its terminal record
        raise RunOutcome("failed", "not_found")

    def look(conn):
        """The researcher's option, or None while its question waits under the project's current
        policy, or "ask" when it is to be asked (first, or again under a changed policy)."""
        ask, answer = asks.asked(conn, run_id)
        if answer is not None and answer.get("by") == "researcher":
            return answer["option"]
        if ask is not None and answer is None and asks._current_policy(conn, project_id) == ask["policy"]:
            return None
        return "ask"

    def ask(conn):  # in its transaction: an answer that came meanwhile is taken, not asked over
        if not _running(conn, run_id) or _revoked(conn, run_id):
            raise RunOutcome("cancelled", "project_changed", "revoked")
        if (found := look(conn)) != "ask":
            return found
        asked, answer = asks.asked(conn, run_id)
        if asked is not None and answer is None:  # asked under a policy the project no longer has
            asks.withdraw(conn, run_id, asked["ask_id"], "policy_changed")
        asks.raise_ask(conn, run_id, "merge", list(OPTIONS), {"matched": inputs.get("matched")})
        return None

    while True:
        active.answered.clear()
        option = await asyncio.to_thread(db.read, look)
        if option == "ask":
            option = await asyncio.to_thread(db.write, ask)
        if option in OPTIONS:
            break
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(active.answered.wait(), POLICY_SECONDS)
    [material] = inputs["material_ids"]
    return {"option": option}, lambda conn: _answered(conn, project_id, material, record, option)


def _answered(conn, project_id, material, record, option):
    """The answer's effect, in the run's terminal transaction (which drops it when the run was
    revoked or cancelled before it): Merge fills the paper from the record, Keep both makes it a paper."""
    if option == "keep_both":
        new_material(conn, project_id, record)
        return
    row = conn.execute("SELECT csl, checked_by, evidence_type, title FROM materials WHERE id = ? AND project_id = ?",
                       (material, project_id)).fetchone()
    if row is None:  # deleted meanwhile, which revokes the run first: nothing to merge into
        return
    csl = json.loads(row[0]) if row[0] else {}
    added = fills(csl, row[1] == "researcher", record)
    csl.update(added)
    conn.execute("UPDATE materials SET csl = ?, title = ?, evidence_type = ?, updated_at = ? WHERE id = ?",
                 (json.dumps(csl), added.get("title", row[3]), _evidence(csl, row[2]), utc_now(), material))


def _summary(csl):
    """A paper's details as a merge's question shows them."""
    names = [a.get("literal") or ", ".join(p for p in (a.get("family"), a.get("given")) if p)
             for a in csl.get("author") or [] if isinstance(a, dict)]
    return {"title": csl.get("title"), "authors": [n for n in names if n][:3], "author_count": len(names),
            "year": _year(csl), "venue": csl.get("container-title"), "doi": csl.get("DOI")}


def _shown(conn, run_id):
    """A merge question's papers as they are now: the project's paper and the record (asks.SHOWN)."""
    row = conn.execute("SELECT r.inputs, m.csl, m.title FROM runs r JOIN materials m"
                       " ON m.id = json_extract(r.inputs, '$.material_ids[0]') WHERE r.id = ?", (run_id,)).fetchone()
    if row is None:
        return {}
    inputs, paper = json.loads(row[0] or "{}"), json.loads(row[1]) if row[1] else {}
    record = (inputs.get("held") or {}).get("record")
    if record is None:
        return {}
    return {"paper": {**_summary(paper), "title": paper.get("title") or row[2]}, "record": _summary(record)}


def run_details(conn, run_id, status):
    """What the background-run list shows of a merge run: its paper's title, its open question, and
    no Retry (backend/materials.py run_details)."""
    titles = [title for (title,) in conn.execute(
        "SELECT m.title FROM runs r JOIN materials m ON m.id = json_extract(r.inputs, '$.material_ids[0]')"
        " WHERE r.id = ?", (run_id,))]
    ask = asks.open_asks(conn, run_id=run_id) if status == "running" else []
    return {"materials": {"titles": titles, "count": len(titles)}, "ask": ask[0] if ask else None, "retryable": False}

