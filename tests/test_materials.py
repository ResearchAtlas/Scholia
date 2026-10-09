"""Adding files, reading them into passages and the Library's API (slice-1 spec F3a, S7, sections
4.2, 5, 7.1 and 13), on synthetic files made by the tests. Lifecycle cases first."""

import asyncio
import base64
import json
import logging
import stat
import threading

import pytest

import backend.extraction as extraction
import backend.materials as materials_module
from backend.db.content import ContentStore
import synthetic_materials as synthetic
from scholia_app import MockProvider, MockScholarly, background_idle, openalex_work, run_finished, started

pytestmark = pytest.mark.asyncio


def b64(data):
    return base64.b64encode(data).decode()


async def rows(client, sql, *args):
    return await asyncio.to_thread(client.state["db"].read, lambda conn: conn.execute(sql, args).fetchall())


async def project_of(client, name="Thesis", level="normal"):
    project = (await client.post("/api/projects", json={"name": name})).json()["id"]
    if level != "normal":
        response = await client.post(f"/api/projects/{project}/sensitivity", json={"level": level})
        assert response.status_code == 200, response.text
    return project


async def add(client, project, *files, **extra):
    response = await client.post(f"/api/projects/{project}/materials",
                                 json={"files": [{"name": n, "data": b64(d)} for n, d in files], **extra})
    return response


async def added(client, project, *files, **extra):
    response = await add(client, project, *files, **extra)
    assert response.status_code == 201, response.text
    return response.json()


async def listing(client, project):
    return (await client.get(f"/api/projects/{project}/materials")).json()


async def settled(client, project, timeout=15.0):
    """The project's papers once none is being read and no lookup runs."""
    deadline = asyncio.get_running_loop().time() + timeout
    while True:
        listed = await listing(client, project)
        if all(m["state"] != "reading" and (m["lookup"] or {}).get("status") != "running"
               for m in listed["materials"]):
            return listed["materials"]
        assert asyncio.get_running_loop().time() < deadline, listed
        await asyncio.sleep(0.02)


def hold_extraction(monkeypatch):
    """Hold every extraction inside its thread, checking stop() as it waits, until go is set."""
    reached, go, real = threading.Event(), threading.Event(), extraction.extract

    def held(data, kind, stop=lambda: None, progress=lambda d, t: None):
        progress(0, 1)
        reached.set()
        while not go.wait(0.01):
            stop()
        return real(data, kind, stop, progress)

    monkeypatch.setattr(extraction, "extract", held)
    return reached, go


PDF = ("paper.pdf", synthetic.paper_pdf())


# Lifecycle: cancellation, deletion, limits, restarts


async def test_a_cancelled_reading_writes_nothing_and_can_be_tried_again(tmp_path, monkeypatch):
    reached, go = hold_extraction(monkeypatch)
    async with started(tmp_path / "data") as client:
        project = await project_of(client)
        [paper] = (await added(client, project, PDF))["materials"]
        await asyncio.to_thread(reached.wait, 10)
        [reading] = (await listing(client, project))["materials"]
        assert reading["state"] == "reading" and reading["progress"] == {"done": 0, "total": 1}
        cancelled = await client.post(f"/api/runs/{paper['run_id']}/cancel")
        assert cancelled.json()["status"] == "cancelled"
        assert (await client.post(f"/api/runs/{paper['run_id']}/cancel")).json()["status"] == "cancelled"  # again: safe
        assert await rows(client, "SELECT count(*) FROM extractions") == [(0,)]
        assert await rows(client, "SELECT count(*) FROM index_queue") == [(0,)]
        [stopped] = await settled(client, project)
        assert (stopped["state"], stopped["reason"]) == ("needs_attention", "stopped")
        run = await run_finished(client, paper["run_id"])
        assert run["retryable"] and run["materials"] == {"titles": ["paper"], "count": 1}
        go.set()
        again = await client.post(f"/api/runs/{paper['run_id']}/retry")
        assert again.status_code == 201
        assert (await run_finished(client, again.json()["run_id"]))["status"] == "succeeded"
        twice = await client.post(f"/api/runs/{paper['run_id']}/retry")  # a second press: the first's run, no error
        assert (twice.status_code, twice.json()) == (201, again.json())
        [ready] = await settled(client, project)
        assert ready["state"] == "ready" and ready["extraction"]["passages"] > 0
        assert (await client.post(f"/api/runs/{again.json()['run_id']}/retry")).json()["code"] == "not_retryable"


async def test_deleting_a_material_while_it_is_read_revokes_its_run_and_nothing_reaches_the_index(
        tmp_path, monkeypatch):
    reached, go = hold_extraction(monkeypatch)
    async with started(tmp_path / "data") as client:
        project = await project_of(client)
        [paper] = (await added(client, project, PDF))["materials"]
        await asyncio.to_thread(reached.wait, 10)
        assert (await client.delete(f"/api/materials/{paper['id']}")).status_code == 200
        go.set()
        await background_idle(client)
        assert await rows(client, "SELECT status, cancel_reason FROM runs WHERE id = ?", paper["run_id"]) == [
            ("cancelled", "revoked")]
        assert await rows(client, "SELECT count(*) FROM extractions") == [(0,)]
        assert await rows(client, "SELECT count(*) FROM passages") == [(0,)]
        assert await rows(client, "SELECT count(*) FROM index_queue WHERE op = 'add'") == [(0,)]


async def test_one_projects_deletion_revokes_only_its_own_reading_of_a_file_another_project_reads(
        tmp_path, monkeypatch):
    reached, go = hold_extraction(monkeypatch)
    async with started(tmp_path / "data") as client:
        mine, theirs = await project_of(client, "Mine"), await project_of(client, "Theirs")
        [paper] = (await added(client, mine, PDF))["materials"]
        [other] = (await added(client, theirs, PDF))["materials"]
        assert other["run_id"] and other["run_id"] != paper["run_id"]  # not read yet: each its own run
        await asyncio.to_thread(reached.wait, 10)
        assert (await client.delete(f"/api/materials/{paper['id']}")).status_code == 200
        go.set()
        assert (await run_finished(client, other["run_id"]))["status"] == "succeeded"
        assert (await run_finished(client, paper["run_id"]))["cancel_reason"] == "revoked"
        [ready] = await settled(client, theirs)
        assert ready["state"] == "ready"
        assert await rows(client, "SELECT count(*) FROM extractions") == [(1,)]
        assert {p for (p,) in await rows(client, "SELECT DISTINCT project_id FROM index_queue")} == {theirs}


async def test_deleting_the_project_while_its_material_is_read_leaves_nothing_of_it(tmp_path, monkeypatch):
    reached, go = hold_extraction(monkeypatch)
    async with started(tmp_path / "data") as client:
        project = await project_of(client)
        [paper] = (await added(client, project, PDF))["materials"]
        await asyncio.to_thread(reached.wait, 10)
        assert (await client.delete(f"/api/projects/{project}")).status_code == 200
        go.set()
        await background_idle(client)
        assert await rows(client, "SELECT count(*) FROM runs WHERE id = ?", paper["run_id"]) == [(0,)]
        assert await rows(client, "SELECT count(*) FROM extractions") == [(0,)]
        assert await rows(client, "SELECT count(*) FROM index_queue") == [(0,)]


async def test_deleting_a_read_material_queues_its_removals_after_its_additions(tmp_path):
    async with started(tmp_path / "data") as client:
        project = await project_of(client)
        [paper] = (await added(client, project, PDF))["materials"]
        [ready] = await settled(client, project)
        version = ready["version"]["id"]
        passages = (await client.get(f"/api/material-versions/{version}/passages")).json()["passages"]
        assert (await client.delete(f"/api/materials/{paper['id']}")).status_code == 200
        queued = await rows(client, "SELECT target_id, op, project_id FROM index_queue ORDER BY seq")
        ids = sorted(p["id"] for p in passages)
        assert sorted(t for t, op, _ in queued if op == "add") == ids == sorted(t for t, op, _ in queued if op == "remove")
        assert [op for _, op, _ in queued] == ["add"] * len(ids) + ["remove"] * len(ids)  # removals come after
        assert {p for _, _, p in queued} == {project}
        assert (await client.get(f"/api/material-versions/{version}/passages")).status_code == 404
        assert (await client.get(f"/api/material-versions/{version}/pages/1")).status_code == 404
        assert (await client.get(f"/api/passages/{passages[0]['id']}")).status_code == 404


async def test_at_most_two_readings_hold_their_files_and_a_third_waits_unread(tmp_path, monkeypatch):
    go, reading, reads = threading.Event(), [], []
    real_extract, real_read = extraction.extract, ContentStore.read

    def held(data, kind, stop=lambda: None, progress=lambda d, t: None):
        progress(0, 1)
        reading.append(kind)
        while not go.wait(0.01):
            stop()
        return real_extract(data, kind, stop, progress)

    def read(store, sha256):
        reads.append(sha256)
        return real_read(store, sha256)

    monkeypatch.setattr(extraction, "extract", held)
    monkeypatch.setattr(ContentStore, "read", read)
    async with started(tmp_path / "data") as client:
        project = await project_of(client)
        await added(client, project, *[(f"notes{i}.md", synthetic.paper_markdown(f"Notes {i}", arxiv="")) for i in range(4)])
        while len(reading) < 2:
            await asyncio.sleep(0.01)
        await asyncio.sleep(0.3)
        assert (len(reading), len(reads)) == (2, 2)  # the others wait for a turn without their files' bytes
        listed = (await listing(client, project))["materials"]
        assert {m["state"] for m in listed} == {"reading"}
        waiting = [m for m in listed if m["progress"] is None]
        assert len(waiting) == 2
        cancelled = await client.post(f"/api/runs/{waiting[0]['reading']['run_id']}/cancel")  # while it waits
        assert cancelled.json()["status"] == "cancelled"
        go.set()
        ready = await settled(client, project)
        assert sorted(m["state"] for m in ready) == ["needs_attention"] + ["ready"] * 3
        assert len(reads) == 3  # the cancelled one never read its file


async def test_a_reading_past_its_limit_stops_and_says_so(tmp_path, monkeypatch):
    hold_extraction(monkeypatch)  # never let go: only the limit ends it
    monkeypatch.setattr(materials_module, "EXTRACTION_SECONDS", 0.2)
    async with started(tmp_path / "data") as client:
        project = await project_of(client)
        [paper] = (await added(client, project, PDF))["materials"]
        run = await run_finished(client, paper["run_id"])
        assert (run["status"], run["cancel_reason"], run["result"]) == ("cancelled", "limit", {"reason": "time_limit"})
        [limited] = await settled(client, project)
        assert (limited["state"], limited["reason"]) == ("needs_attention", "time_limit")


async def test_a_reading_stopped_by_a_restart_starts_again_from_its_inputs(tmp_path, monkeypatch):
    data = tmp_path / "data"
    reached, _ = hold_extraction(monkeypatch)
    async with started(data) as client:
        project = await project_of(client)
        [paper] = (await added(client, project, PDF))["materials"]
        await asyncio.to_thread(reached.wait, 10)
    assert reached.is_set()
    monkeypatch.undo()
    async with started(data, setup=False) as client:
        assert (await run_finished(client, paper["run_id"]))["status"] == "succeeded"
        [ready] = await settled(client, project)
        assert ready["state"] == "ready"


async def test_the_runs_of_added_files_start_with_their_commit_and_never_read_interrupted(tmp_path, monkeypatch):
    async with started(tmp_path / "data") as client:
        project = await project_of(client)
        harness, db = client.state["harness"], client.state["db"]

        async def failing():
            raise RuntimeError("no later start of background runs")

        monkeypatch.setattr(harness, "kick_background", failing)  # not needed: they start with the commit
        committed, go, real_write = threading.Event(), threading.Event(), db.write

        def write(fn):  # held right after the transaction that records the added files commits
            result = real_write(fn)
            if "add_files" in fn.__qualname__ or "record_background" in fn.__qualname__:
                committed.set()
                go.wait(10)
            return result

        monkeypatch.setattr(db, "write", write)
        adding = asyncio.create_task(add(client, project, PDF))
        await asyncio.to_thread(committed.wait, 10)
        recorded = await rows(client, "SELECT id FROM runs WHERE kind = 'background' AND status = 'running'")
        assert len(recorded) == 2  # its reading and its lookup, committed
        for (run_id,) in recorded:  # held from before the commit: running, never interrupted
            [row] = (await client.get("/api/activity", params={"run_id": run_id})).json()["runs"]
            assert row["status"] == "running", row
        go.set()
        assert (await adding).status_code == 201
        [ready] = await settled(client, project)
        assert ready["state"] == "ready"


# Adding files


@pytest.mark.parametrize("name, data, kinds", [
    ("paper.pdf", synthetic.paper_pdf(), {"title", "abstract", "paragraph", "caption", "table", "reference"}),
    ("paper.docx", synthetic.paper_docx(), {"title", "paragraph", "caption", "table", "reference"}),
    ("paper.html", synthetic.paper_html(), {"title", "paragraph", "caption", "table", "reference"}),
    ("notes.md", synthetic.paper_markdown(), {"title", "paragraph", "table", "reference"}),
    ("model.tex", synthetic.paper_latex(), {"title", "abstract", "paragraph", "caption", "table", "reference"}),
])
async def test_each_format_is_stored_once_and_read_into_passages(tmp_path, name, data, kinds):
    async with started(tmp_path / "data") as client:
        project = await project_of(client)
        [paper] = (await added(client, project, (name, data)))["materials"]
        [ready] = await settled(client, project)
        assert ready["state"] == "ready" and ready["title"] == name.rsplit(".", 1)[0]
        assert (ready["source"], ready["evidence_type"]) == ("upload", "full_text")
        assert ready["version"]["seq"] == 0 and ready["version"]["media_type"] == extraction.media_type(name, data)
        passages = (await client.get(f"/api/material-versions/{ready['version']['id']}/passages")).json()["passages"]
        assert {p["kind"] for p in passages} == kinds
        assert [p["ordinal"] for p in passages] == list(range(len(passages)))
        assert all(len(p["text"]) <= extraction.MAX_PASSAGE for p in passages)
        assert await rows(client, "SELECT count(*) FROM content_files") == [(1,)]
        sha = (await rows(client, "SELECT sha256 FROM content_files"))[0][0]
        stored = tmp_path / "data" / "content" / sha[:2] / sha
        assert stored.read_bytes() == data and stat.S_IMODE(stored.stat().st_mode) == 0o600  # owner-only
        # Its passages are queued for the project's index with them (Ready: the extraction is committed).
        assert sorted(t for (t,) in await rows(client, "SELECT target_id FROM index_queue WHERE op = 'add'")) == sorted(
            p["id"] for p in passages)


async def test_the_same_file_is_one_paper_in_a_project_and_shares_its_reading_with_another(tmp_path):
    async with started(tmp_path / "data") as client:
        first, second = await project_of(client, "First"), await project_of(client, "Second")
        [paper] = (await added(client, first, PDF))["materials"]
        await settled(client, first)
        [again] = (await added(client, first, ("copy.pdf", PDF[1])))["materials"]
        assert again == {"id": paper["id"], "existing": True}
        [elsewhere] = (await added(client, second, PDF))["materials"]
        assert elsewhere["run_id"] is None and elsewhere["id"] != paper["id"]  # read once, shared
        [shared] = await settled(client, second)
        assert shared["state"] == "ready"
        assert await rows(client, "SELECT count(*) FROM extractions") == [(1,)]
        assert await rows(client, "SELECT count(*) FROM content_files") == [(1,)]
        (count,) = (await rows(client, "SELECT count(*) FROM passages"))[0]
        assert await rows(client, "SELECT project_id, count(*) FROM index_queue GROUP BY project_id ORDER BY 1") == sorted(
            [(first, count), (second, count)])


async def test_two_projects_reading_the_same_file_at_once_queue_each_passage_once_for_each(tmp_path, monkeypatch):
    reached, go = hold_extraction(monkeypatch)
    async with started(tmp_path / "data") as client:
        first, second = await project_of(client, "First"), await project_of(client, "Second")
        [one] = (await added(client, first, PDF))["materials"]
        [two] = (await added(client, second, PDF))["materials"]  # neither read yet: each its own reading
        assert one["run_id"] and two["run_id"]
        await asyncio.to_thread(reached.wait, 10)
        go.set()
        for run in (one["run_id"], two["run_id"]):
            assert (await run_finished(client, run))["status"] == "succeeded"
        assert await rows(client, "SELECT count(*) FROM extractions") == [(1,)]  # the second shares the first's
        (passages,) = (await rows(client, "SELECT count(*) FROM passages"))[0]
        assert await rows(client, "SELECT project_id, count(*), count(DISTINCT target_id) FROM index_queue"
                                  " WHERE op = 'add' GROUP BY project_id ORDER BY project_id") == sorted(
            [(first, passages, passages), (second, passages, passages)])


async def test_a_reading_by_an_earlier_extractor_version_is_never_this_versions(tmp_path, monkeypatch):
    doi = synthetic.DOI
    notes = f"# Notes\n\ndoi:{doi}\n\nSynthetic text.\n".encode()
    real = extraction.extract
    scholarly = MockScholarly(openalex={doi: openalex_work(doi, "Notes, Resolved")})
    async with started(tmp_path / "data", MockProvider(scholarly=scholarly)) as client:
        project = await project_of(client)
        monkeypatch.setitem(extraction.EXTRACTORS, extraction.MARKDOWN, ("markdown", "markdown-0"))  # an earlier Scholia
        result = await added(client, project, ("notes.md", notes))
        [paper] = result["materials"]
        [earlier] = await settled(client, project)
        assert earlier["state"] == "ready" and earlier["extraction"]["version"] == "markdown-0"
        monkeypatch.setitem(extraction.EXTRACTORS, extraction.MARKDOWN, ("markdown", "markdown-1"))  # this one
        [outdated] = await settled(client, project)  # read by the earlier version only: not Ready, and Retry reads it
        assert (outdated["state"], outdated["reason"], outdated["extraction"]) == ("needs_attention", "outdated", None)
        assert (await run_finished(client, paper["run_id"]))["retryable"] is True
        version = outdated["version"]["id"]
        assert (await client.get(f"/api/material-versions/{version}/passages")).json()["passages"] == []
        # This version's reading fails: the paper needs attention for it, and is not Ready from the earlier one.
        def unreadable(*args, **kwargs):
            raise extraction.Unreadable()
        monkeypatch.setattr(extraction, "extract", unreadable)
        again = await client.post(f"/api/runs/{paper['run_id']}/retry")
        assert (await run_finished(client, again.json()["run_id"]))["status"] == "failed"
        [failed] = await settled(client, project)
        assert (failed["state"], failed["reason"], failed["extraction"]) == ("needs_attention", "unreadable_file", None)
        # Its identifiers are not taken from the earlier reading's passages: nothing is sent.
        sent = len(scholarly.requests)
        await asyncio.to_thread(client.state["db"].write, lambda conn: conn.execute(
            "UPDATE runs SET status = 'failed' WHERE id = ?", (result["lookup_run_id"],)))
        looked = await client.post(f"/api/runs/{result['lookup_run_id']}/retry")
        assert (await run_finished(client, looked.json()["run_id"]))["result"] == {"reason": "not_read"}
        assert len(scholarly.requests) == sent
        [passage] = (await rows(client, "SELECT id FROM passages ORDER BY ordinal LIMIT 1"))[0]
        assert (await client.get(f"/api/passages/{passage}")).json()["materials"] == []  # the earlier reading's: no owner
        # A reading by this version is shared as before, and only it.
        monkeypatch.setattr(extraction, "extract", real)
        other = await project_of(client, "Other")
        [theirs] = (await added(client, other, ("notes.md", notes)))["materials"]
        assert theirs["run_id"]  # not the earlier version's: a reading of its own
        assert (await run_finished(client, theirs["run_id"]))["status"] == "succeeded"
        [shared] = (await added(client, await project_of(client, "Third"), ("notes.md", notes)))["materials"]
        assert shared["run_id"] is None  # this version's reading, shared
        [ready] = await settled(client, project)  # and the first paper reads Ready from it too
        assert ready["state"] == "ready" and ready["extraction"]["version"] == "markdown-1"


async def test_a_paper_sharing_a_reading_by_an_earlier_version_is_read_again_without_a_run_of_its_own(tmp_path,
                                                                                                    monkeypatch):
    notes = b"# Notes\n\nA paragraph of synthetic text.\n"
    async with started(tmp_path / "data") as client:
        first, second = await project_of(client, "First"), await project_of(client, "Second")
        monkeypatch.setitem(extraction.EXTRACTORS, extraction.MARKDOWN, ("markdown", "markdown-0"))  # an earlier Scholia
        [importer] = (await added(client, first, ("notes.md", notes)))["materials"]
        await settled(client, first)
        [sharer] = (await added(client, second, ("notes.md", notes)))["materials"]
        assert sharer["run_id"] is None  # the reading shared: no run of its own
        [ready] = await settled(client, second)
        assert ready["state"] == "ready" and ready["readable"] is False
        monkeypatch.setitem(extraction.EXTRACTORS, extraction.MARKDOWN, ("markdown", "markdown-1"))  # this one
        assert (await client.delete(f"/api/materials/{importer['id']}")).status_code == 200  # the importer is gone
        [outdated] = await settled(client, second)
        assert (outdated["state"], outdated["reason"], outdated["readable"]) == ("needs_attention", "outdated", True)
        read = await client.post(f"/api/material-versions/{outdated['version']['id']}/read")
        assert read.status_code == 201, read.text
        assert (await run_finished(client, read.json()["run_id"]))["status"] == "succeeded"
        [again] = await settled(client, second)
        assert (again["state"], again["readable"], again["extraction"]["version"]) == ("ready", False, "markdown-1")
        refused = await client.post(f"/api/material-versions/{outdated['version']['id']}/read")
        assert (refused.status_code, refused.json()["code"]) == (409, "not_retryable")  # read already
        missing = await client.post("/api/material-versions/no-such-version/read")
        assert missing.status_code == 404


async def ops(client, extraction_id):
    """{project id: [its queued operations on the extraction's passages in queue order, each run of the
    same operation with how many rows it has]}."""
    found = {}
    for project, op in await rows(
            client, "SELECT q.project_id, q.op FROM index_queue q JOIN passages p ON p.id = q.target_id"
                    " WHERE q.target = 'passage' AND p.extraction_id = ? ORDER BY q.seq", extraction_id):
        runs = found.setdefault(project, [])
        if runs and runs[-1][0] == op:
            runs[-1] = (op, runs[-1][1] + 1)
        else:
            runs.append((op, 1))
    return found


async def test_a_newer_reading_takes_the_earlier_readings_passages_out_of_every_index_that_has_them(tmp_path, monkeypatch):
    notes = b"# Notes\n\nA first paragraph.\n\nA second paragraph.\n"
    async with started(tmp_path / "data") as client:
        first, second = await project_of(client, "First"), await project_of(client, "Second")
        monkeypatch.setitem(extraction.EXTRACTORS, extraction.MARKDOWN, ("markdown", "markdown-0"))  # an earlier Scholia
        [paper] = (await added(client, first, ("notes.md", notes)))["materials"]
        await settled(client, first)
        await added(client, second, ("notes.md", notes))  # the earlier reading, shared
        await settled(client, second)
        [(older,)] = await rows(client, "SELECT id FROM extractions")
        monkeypatch.setitem(extraction.EXTRACTORS, extraction.MARKDOWN, ("markdown", "markdown-1"))  # this one
        again = await client.post(f"/api/runs/{paper['run_id']}/retry")  # read again by this version
        assert (await run_finished(client, again.json()["run_id"]))["status"] == "succeeded"
        [(newer,)] = await rows(client, "SELECT id FROM extractions WHERE id != ?", older)
        (n,) = (await rows(client, "SELECT count(*) FROM passages WHERE extraction_id = ?", newer))[0]
        assert await ops(client, older) == {first: [("add", n), ("remove", n)], second: [("add", n), ("remove", n)]}
        assert await ops(client, newer) == {first: [("add", n)], second: [("add", n)]}
        assert await rows(client, "SELECT count(*) FROM passages WHERE extraction_id = ?", older) == [(n,)]  # kept


async def test_a_replaced_file_leaves_its_projects_index_unless_another_paper_there_still_reads_it(tmp_path):
    one, two, three = (f"# Paper {n}\n\nIts own text, {n}.\n".encode() for n in ("one", "two", "three"))
    async with started(tmp_path / "data") as client:
        project = await project_of(client)
        [a] = (await added(client, project, ("a.md", one)))["materials"]
        [b] = (await added(client, project, ("b.md", two)))["materials"]
        await settled(client, project)
        by_file = dict(await rows(client, "SELECT v.file_sha256, e.id FROM material_versions v JOIN extractions e"
                                         " ON e.file_sha256 = v.file_sha256"))
        [read_one, read_two] = [by_file[sha] for (sha,) in await rows(
            client, "SELECT file_sha256 FROM material_versions WHERE material_id IN (?, ?) ORDER BY material_id = ?",
            a["id"], b["id"], b["id"])]
        n = dict(await rows(client, "SELECT extraction_id, count(*) FROM passages GROUP BY extraction_id"))
        await added(client, project, ("b-again.md", one), material_id=b["id"])  # b now reads a's file too
        await settled(client, project)
        assert await ops(client, read_two) == {project: [("add", n[read_two]), ("remove", n[read_two])]}  # no paper reads it
        assert await ops(client, read_one) == {project: [("add", n[read_one])]}  # a's file, now b's too: once
        await added(client, project, ("a-new.md", three), material_id=a["id"])
        await settled(client, project)
        assert await ops(client, read_one) == {project: [("add", n[read_one])]}  # still b's reading: it stays


@pytest.mark.parametrize("change", ["replaced", "read by a newer version"])
async def test_passages_the_index_has_applied_still_leave_it(tmp_path, monkeypatch, change):
    notes = b"# Notes\n\nA paragraph of synthetic text.\n"
    async with started(tmp_path / "data") as client:
        project = await project_of(client)
        monkeypatch.setitem(extraction.EXTRACTORS, extraction.MARKDOWN, ("markdown", "markdown-0"))
        [paper] = (await added(client, project, ("notes.md", notes)))["materials"]
        await settled(client, project)
        [(reading,)] = await rows(client, "SELECT id FROM extractions")
        (n,) = (await rows(client, "SELECT count(*) FROM passages WHERE extraction_id = ?", reading))[0]
        await asyncio.to_thread(client.state["db"].write, lambda conn: conn.execute("DELETE FROM index_queue"))  # applied
        if change == "replaced":
            await added(client, project, ("other.md", b"# Other\n\nAnother text.\n"), material_id=paper["id"])
        else:
            monkeypatch.setitem(extraction.EXTRACTORS, extraction.MARKDOWN, ("markdown", "markdown-1"))
            read = await client.post(f"/api/runs/{paper['run_id']}/retry")
            assert (await run_finished(client, read.json()["run_id"]))["status"] == "succeeded"
        await settled(client, project)
        assert await ops(client, reading) == {project: [("remove", n)]}  # once, though no add is left in the queue


async def test_a_removal_already_queued_is_not_queued_again_and_one_for_passages_never_added_is_harmless(tmp_path):
    async with started(tmp_path / "data") as client:
        project, other = await project_of(client), await project_of(client, "Other")
        await added(client, project, PDF)
        await settled(client, project)
        [(reading,)] = await rows(client, "SELECT id FROM extractions")
        (n,) = (await rows(client, "SELECT count(*) FROM passages WHERE extraction_id = ?", reading))[0]
        db = client.state["db"]
        for _ in range(2):
            for target in (project, other):  # its own project's, and one that never had them
                await asyncio.to_thread(db.write, lambda conn: materials_module._queue_removes(conn, reading, target))
        assert await ops(client, reading) == {project: [("add", n), ("remove", n)], other: [("remove", n)]}
        await asyncio.to_thread(db.write, lambda conn: materials_module._queue_adds(conn, reading, project))
        await asyncio.to_thread(db.write, lambda conn: materials_module._queue_adds(conn, reading, project))
        assert await ops(client, reading) == {project: [("add", n), ("remove", n), ("add", n)], other: [("remove", n)]}


async def test_deleting_the_paper_that_reads_a_file_takes_it_out_of_the_index_though_another_once_had_it(tmp_path):
    x, y = b"# File X\n\nIts own text.\n", b"# File Y\n\nAnother text.\n"
    async with started(tmp_path / "data") as client:
        project = await project_of(client)
        [a] = (await added(client, project, ("x.md", x)))["materials"]
        await settled(client, project)
        await added(client, project, ("y.md", y), material_id=a["id"])  # A keeps X only as an earlier version
        [b] = (await added(client, project, ("x-again.md", x)))["materials"]  # B reads X now
        await settled(client, project)
        [(read_x,)] = await rows(client, "SELECT e.id FROM extractions e JOIN material_versions v"
                                         " ON v.file_sha256 = e.file_sha256 WHERE v.material_id = ?", b["id"])
        (n,) = (await rows(client, "SELECT count(*) FROM passages WHERE extraction_id = ?", read_x))[0]
        assert (await ops(client, read_x))[project][-1] == ("add", n)  # in the index, as B's
        assert (await client.delete(f"/api/materials/{b['id']}")).status_code == 200
        assert (await ops(client, read_x))[project][-1] == ("remove", n)  # A's earlier version does not keep it there
        assert await rows(client, "SELECT count(*) FROM passages WHERE extraction_id = ?", read_x) == [(n,)]  # A's version's


async def test_a_reading_of_a_file_replaced_while_it_was_read_reaches_no_index(tmp_path, monkeypatch):
    real, reached, release = extraction.extract, threading.Event(), threading.Event()

    def held(data, kind, stop=lambda: None, progress=lambda d, t: None):  # only the PDF's reading waits
        if data == PDF[1]:
            reached.set()
            release.wait(20)
        return real(data, kind, lambda: None, progress)

    monkeypatch.setattr(extraction, "extract", held)
    async with started(tmp_path / "data") as client:
        project = await project_of(client)
        [paper] = (await added(client, project, PDF))["materials"]
        await asyncio.to_thread(reached.wait, 10)
        await added(client, project, ("v2.md", b"# Version two\n\nIts text.\n"), material_id=paper["id"])
        release.set()
        assert (await run_finished(client, paper["run_id"]))["status"] == "succeeded"  # read, and kept
        await settled(client, project)
        [(pdf_reading,)] = await rows(client, "SELECT id FROM extractions WHERE extractor = 'pdf'")
        assert await ops(client, pdf_reading) == {}  # but no current version reads it: not in the index


async def test_the_same_bytes_added_as_another_type_are_read_as_that_type(tmp_path):
    source = b"\\section{Method}\n\nText with \\emph{emphasis} here.\n"  # Markdown and LaTeX alike
    async with started(tmp_path / "data") as client:
        first, second = await project_of(client, "First"), await project_of(client, "Second")
        await added(client, first, ("notes.md", source))
        [markdown] = await settled(client, first)
        [latex_paper] = (await added(client, second, ("notes.tex", source)))["materials"]
        assert latex_paper["run_id"]  # not the Markdown reading, shared: a reading of its own
        [latex] = await settled(client, second)
        assert await rows(client, "SELECT count(*) FROM content_files") == [(1,)]  # one stored file
        assert {e for (e,) in await rows(client, "SELECT extractor FROM extractions")} == {"markdown", "latex"}

        async def texts(paper):
            return [p["text"] for p in (await client.get(
                f"/api/material-versions/{paper['version']['id']}/passages")).json()["passages"]]

        assert (markdown["version"]["media_type"], latex["version"]["media_type"]) == (extraction.MARKDOWN, extraction.LATEX)
        assert await texts(latex) == ["Text with emphasis here."] and "\\section{Method}" in await texts(markdown)
        # A new version with the same bytes as another type is read as that type too.
        [again] = (await added(client, first, ("notes.html", source), material_id=markdown["id"]))["materials"]
        [html] = await settled(client, first)
        assert again["run_id"] and html["version"]["media_type"] == extraction.HTML and html["state"] == "ready"
        versions = (await client.get(f"/api/materials/{markdown['id']}/versions")).json()["versions"]
        assert [v["media_type"] for v in versions] == [extraction.MARKDOWN, extraction.HTML]


async def test_a_replaced_file_is_a_new_version_read_again(tmp_path):
    async with started(tmp_path / "data") as client:
        project = await project_of(client)
        [paper] = (await added(client, project, PDF))["materials"]
        await settled(client, project)
        replacement = ("paper-v2.md", synthetic.paper_markdown())
        [replaced] = (await added(client, project, replacement, material_id=paper["id"]))["materials"]
        assert replaced["id"] == paper["id"] and replaced["run_id"]
        [ready] = await settled(client, project)
        assert ready["version"]["seq"] == 1 and ready["version"]["media_type"] == extraction.MARKDOWN
        versions = (await client.get(f"/api/materials/{paper['id']}/versions")).json()["versions"]
        assert [(v["seq"], v["is_current"]) for v in versions] == [(0, False), (1, True)]
        too_many = await add(client, project, PDF, replacement, material_id=paper["id"])
        assert too_many.json()["code"] == "invalid_request"


async def test_a_passage_names_only_the_papers_whose_current_file_it_comes_from(tmp_path):
    async with started(tmp_path / "data") as client:
        first, second = await project_of(client, "First"), await project_of(client, "Second")
        [paper] = (await added(client, first, PDF))["materials"]
        [ready] = await settled(client, first)
        [elsewhere] = (await added(client, second, PDF))["materials"]  # the same file, its reading shared
        [passage, *_] = (await client.get(f"/api/material-versions/{ready['version']['id']}/passages")).json()["passages"]
        owners = (await client.get(f"/api/passages/{passage['id']}")).json()["materials"]
        assert {o["id"] for o in owners} == {paper["id"], elsewhere["id"]}
        await added(client, first, ("paper-v2.md", synthetic.paper_markdown()), material_id=paper["id"])
        await settled(client, first)
        owners = (await client.get(f"/api/passages/{passage['id']}")).json()["materials"]
        assert [(o["id"], o["project_id"]) for o in owners] == [(elsewhere["id"], second)]  # not the replaced one


@pytest.mark.parametrize("files, code", [
    ([("notes.txt", b"plain text")], "unsupported_file"),
    ([("fake.pdf", b"not a pdf at all")], "unsupported_file"),
    ([("fake.docx", b"PK not a zip")], "unsupported_file"),
    ([("empty.md", b"")], "invalid_request"),
    ([("\u200b\u200b", b"# A title")], "invalid_request"),
    ([("good.md", b"# A title"), ("bad.txt", b"text")], "unsupported_file"),  # nothing of the batch is written
])
async def test_a_request_that_fails_validation_writes_nothing(tmp_path, files, code):
    async with started(tmp_path / "data") as client:
        project = await project_of(client)
        response = await client.post(f"/api/projects/{project}/materials", json={
            "files": [{"name": n, "data": b64(d)} for n, d in files]})
        assert (response.status_code, response.json()["code"]) == (400, code)
        for table in ("materials", "material_versions", "content_files", "runs WHERE kind = 'background'"):
            assert await rows(client, f"SELECT count(*) FROM {table}") == [(0,)], table


async def test_oversized_unknown_and_garbled_uploads_are_refused(tmp_path, monkeypatch):
    monkeypatch.setattr(extraction, "MAX_FILE_BYTES", 10)
    async with started(tmp_path / "data") as client:
        project = await project_of(client)
        assert (await add(client, project, ("big.md", b"# more than ten bytes"))).json()["code"] == "file_too_large"
        unknown = await client.post("/api/projects/00000000-0000-4000-8000-000000000000/materials",
                                    json={"files": [{"name": "a.md", "data": b64(b"# A")}]})
        assert unknown.status_code == 404
        garbled = await client.post(f"/api/projects/{project}/materials", json={"files": [{"name": "a.md", "data": "%%%"}]})
        assert garbled.json()["code"] == "invalid_request"
        assert await rows(client, "SELECT count(*) FROM materials") == [(0,)]


async def test_scanned_pages_wait_for_ocr_and_the_text_pages_are_kept(tmp_path):
    async with started(tmp_path / "data") as client:
        project = await project_of(client)
        await added(client, project, ("scan.pdf", synthetic.paper_pdf(scanned=2)))
        [paper] = await settled(client, project)
        assert (paper["state"], paper["reason"]) == ("needs_attention", "ocr_waiting")
        assert (paper["extraction"]["pages"], paper["extraction"]["ocr_pages"]) == (4, 2)
        assert paper["extraction"]["passages"] > 0 and paper["extraction"]["status"] == "ocr_needed"


async def test_a_damaged_file_needs_attention_with_its_reason(tmp_path):
    async with started(tmp_path / "data") as client:
        project = await project_of(client)
        [paper] = (await added(client, project, ("broken.pdf", b"%PDF-1.7\n" + b"\x00garbage" * 50)))["materials"]
        run = await run_finished(client, paper["run_id"])
        assert (run["status"], run["result"], run["retryable"]) == ("failed", {"reason": "unreadable_file"}, True)
        [broken] = await settled(client, project)
        assert (broken["state"], broken["reason"]) == ("needs_attention", "unreadable_file")


@pytest.mark.parametrize("entity", ["billion laughs", "external"])
async def test_a_docx_declaring_an_entity_is_refused_and_nothing_of_it_is_written(tmp_path, entity):
    from test_extraction import EXTERNAL, LAUGHS, zipped
    async with started(tmp_path / "data") as client:
        project = await project_of(client)
        data = zipped({"word/document.xml": LAUGHS if entity == "billion laughs" else EXTERNAL})
        [paper] = (await added(client, project, ("entity.docx", data)))["materials"]
        run = await run_finished(client, paper["run_id"])
        assert (run["status"], run["result"]) == ("failed", {"reason": "unreadable_file"})
        for table in ("extractions", "passages", "index_queue"):
            assert await rows(client, f"SELECT count(*) FROM {table}") == [(0,)], table
        [refused] = await settled(client, project)
        assert (refused["state"], refused["reason"]) == ("needs_attention", "unreadable_file")


@pytest.mark.parametrize("held", ["its file's read", "its render"])
@pytest.mark.parametrize("deleted", ["material", "project"])
async def test_a_page_image_whose_paper_is_deleted_while_it_renders_is_not_given(tmp_path, monkeypatch, deleted, held):
    reached, go = threading.Event(), threading.Event()

    def holding(real):
        def wait_then(*args):
            reached.set()
            go.wait(10)
            return real(*args)
        return wait_then

    async with started(tmp_path / "data") as client:
        project = await project_of(client)
        await added(client, project, PDF)
        [paper] = await settled(client, project)
        if held == "its render":
            monkeypatch.setattr(extraction, "render_page", holding(extraction.render_page))
        else:
            monkeypatch.setattr(ContentStore, "read", holding(ContentStore.read))
        page = asyncio.ensure_future(client.get(f"/api/material-versions/{paper['version']['id']}/pages/1"))
        await asyncio.to_thread(reached.wait, 10)
        url = f"/api/materials/{paper['id']}" if deleted == "material" else f"/api/projects/{project}"
        assert (await client.delete(url)).status_code == 200
        go.set()
        assert (await page).status_code == 404


async def test_at_most_two_page_images_hold_their_files_at_once(tmp_path, monkeypatch):
    lock, holding, most = threading.Lock(), [0], [0]
    real_read, real_render = ContentStore.read, extraction.render_page

    def read(store, sha256):  # a page image's bytes are held from here to its render's end
        with lock:
            holding[0] += 1
            most[0] = max(most[0], holding[0])
        return real_read(store, sha256)

    def render(data, number, scale=2.0):
        try:
            threading.Event().wait(0.1)
            return real_render(data, number, scale)
        finally:
            with lock:
                holding[0] -= 1

    async with started(tmp_path / "data") as client:
        project = await project_of(client)
        await added(client, project, PDF)
        [paper] = await settled(client, project)
        monkeypatch.setattr(ContentStore, "read", read)
        monkeypatch.setattr(extraction, "render_page", render)
        pages = await asyncio.gather(*[client.get(f"/api/material-versions/{paper['version']['id']}/pages/{1 + i % 2}")
                                       for i in range(6)])
        assert [page.status_code for page in pages] == [200] * 6
        assert most[0] == 2


async def test_a_pdf_page_is_rendered_as_a_png_and_only_a_pdf_has_pages(tmp_path):
    async with started(tmp_path / "data") as client:
        project = await project_of(client)
        await added(client, project, PDF, ("notes.md", synthetic.paper_markdown()))
        papers = {m["version"]["media_type"]: m for m in await settled(client, project)}
        version = papers[extraction.PDF]["version"]["id"]
        page = await client.get(f"/api/material-versions/{version}/pages/1", params={"scale": 1})
        assert page.status_code == 200 and page.headers["content-type"] == "image/png"
        assert page.content[:8] == b"\x89PNG\r\n\x1a\n"
        assert int.from_bytes(page.content[16:20], "big") == 612  # the page's width at scale 1
        assert (await client.get(f"/api/material-versions/{version}/pages/3")).status_code == 404
        other = papers[extraction.MARKDOWN]["version"]["id"]
        assert (await client.get(f"/api/material-versions/{other}/pages/1")).json()["code"] == "not_a_pdf"
        passages = (await client.get(f"/api/material-versions/{version}/passages")).json()["passages"]
        boxed = [p for p in passages if p["boxes"]]
        assert boxed and all(0 <= v <= 1 for p in boxed for rect in p["boxes"]["rects"] for v in rect)
        one = (await client.get(f"/api/passages/{passages[2]['id']}")).json()
        assert one["selector"]["exact"] == passages[2]["text"] and one["selector"]["prefix"]
        assert one["materials"][0]["project_id"] == project


async def test_no_answer_about_a_material_is_kept_in_the_browser_cache(tmp_path):
    async with started(tmp_path / "data") as client:
        project = await project_of(client)
        [paper] = (await added(client, project, PDF))["materials"]
        [ready] = await settled(client, project)
        version = ready["version"]["id"]
        passages = (await client.get(f"/api/material-versions/{version}/passages")).json()["passages"]
        for url in (f"/api/material-versions/{version}/pages/1", f"/api/material-versions/{version}/passages",
                    f"/api/passages/{passages[0]['id']}", f"/api/materials/{paper['id']}",
                    f"/api/materials/{paper['id']}/versions", f"/api/projects/{project}/materials",
                    f"/api/activity?run_id={paper['run_id']}"):
            response = await client.get(url)
            assert (response.status_code, response.headers.get("cache-control")) == (200, "no-store"), url


async def test_the_researchers_edit_of_a_papers_details_is_checked_and_kept(tmp_path):
    async with started(tmp_path / "data") as client:
        project = await project_of(client)
        [paper] = (await added(client, project, ("notes.md", synthetic.paper_markdown(arxiv=""))))["materials"]
        await settled(client, project)
        url = f"/api/materials/{paper['id']}"
        for body, code in (({"title": " \u200b "}, "title_needed"), ({"title": ""}, "title_needed"),
                           ({"doi": "not a doi"}, "invalid_doi"), ({"doi": "10.1234/../../works"}, "invalid_doi")):
            refused = await client.patch(url, json=body)
            assert (refused.status_code, refused.json()["code"]) == (400, code)
        assert (await rows(client, "SELECT checked_by FROM materials"))[0] == (None,)
        saved = await client.patch(url, json={"title": "  A Revised Title ", "authors": ["Example, Ana", "Bo", "\u200b"],
                                              "year": 2021, "venue": "Synthetic Review", "doi": "https://doi.org/10.5555/X.1"})
        assert saved.status_code == 200, saved.text
        material = saved.json()
        assert material["title"] == "A Revised Title" and material["checked_by"] == "researcher"
        assert material["csl"]["author"] == [{"family": "Example", "given": "Ana"}, {"literal": "Bo"}]
        assert material["csl"]["DOI"] == "10.5555/x.1" and material["csl"]["issued"] == {"date-parts": [[2021]]}
        cleared = (await client.patch(url, json={"venue": "", "year": None, "doi": ""})).json()
        assert not {"container-title", "issued", "DOI"} & cleared["csl"].keys()


async def test_an_author_name_past_its_length_is_refused_and_nothing_is_written(tmp_path):
    async with started(tmp_path / "data") as client:
        project = await project_of(client)
        [paper] = (await added(client, project, ("notes.md", synthetic.paper_markdown(arxiv=""))))["materials"]
        before = (await settled(client, project))[0]
        url = f"/api/materials/{paper['id']}"
        too_long = materials_module.AUTHOR_CHARS + 1  # past "Family, Given" with each part as long as a lookup keeps
        refused = await client.patch(url, json={"title": "A Title", "authors": ["Example, Ana", "x" * too_long]})
        assert (refused.status_code, refused.json()["code"]) == (400, "invalid_request")
        [after] = await settled(client, project)
        assert (after["title"], after["csl"], after["checked_by"]) == (before["title"], before["csl"], before["checked_by"])
        longest = "x" * materials_module.AUTHOR_CHARS  # a name as long as a looked-up one may be sent back
        kept = await client.patch(url, json={"authors": [longest]})
        assert kept.status_code == 200 and kept.json()["csl"]["author"] == [{"literal": longest}]


async def test_reading_and_its_failures_log_no_file_name_or_text(tmp_path, caplog):
    caplog.set_level(logging.DEBUG)
    secret = "Participant-Seven-Canary"
    async with started(tmp_path / "data") as client:
        project = await project_of(client)
        await added(client, project, (f"{secret}.md", f"# {secret}\n\n{secret} said this.".encode()),
                    (f"{secret}.pdf", b"%PDF-1.7 " + secret.encode()),
                    # Malformed LaTeX: pylatexenc's tolerant parser would log the mismatched names.
                    (f"{secret}.tex", f"\\begin{{document}}\n{secret} \\end{{ParticipantSevenCanary}} and"
                                      f" \\textbf{{{secret}\n\\end{{document}}\n".encode()))
        latex = {m["version"]["media_type"]: m for m in await settled(client, project)}[extraction.LATEX]
        assert latex["state"] == "ready"  # read, the error tolerated
    assert secret not in caplog.text and "ParticipantSevenCanary" not in caplog.text


async def test_attached_in_a_conversation_names_it_and_only_its_own_project(tmp_path):
    async with started(tmp_path / "data") as client:
        project, other = await project_of(client), await project_of(client, "Other")
        conversation = (await client.post("/api/conversations", json={"project_id": other})).json()["id"]
        refused = await add(client, project, PDF, conversation_id=conversation)
        assert refused.status_code == 404
        mine = (await client.post("/api/conversations", json={"project_id": project})).json()["id"]
        result = await added(client, project, PDF, conversation_id=mine)
        [(inputs,)] = await rows(client, "SELECT inputs FROM runs WHERE id = ?", result["lookup_run_id"])
        assert json.loads(inputs)["origin"] == {"conversation_id": mine}
        await settled(client, project)
