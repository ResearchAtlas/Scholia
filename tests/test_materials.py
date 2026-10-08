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
import synthetic_materials as synthetic
from scholia_app import background_idle, run_finished, started

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


async def test_a_failed_start_of_the_runs_after_the_commit_still_reports_what_was_added(tmp_path, monkeypatch):
    async with started(tmp_path / "data") as client:
        project = await project_of(client)
        harness = client.state["harness"]
        real = harness.kick_background

        async def failing():
            raise RuntimeError("could not start")

        monkeypatch.setattr(harness, "kick_background", failing)
        response = await add(client, project, PDF)
        assert response.status_code == 201  # committed: reported as added
        monkeypatch.setattr(harness, "kick_background", real)
        await harness.kick_background()  # the next start of background runs reads it
        [ready] = await settled(client, project)
        assert ready["state"] == "ready"


# Adding files


@pytest.mark.parametrize("name, data, kinds", [
    ("paper.pdf", synthetic.paper_pdf(), {"title", "abstract", "paragraph", "caption", "reference"}),
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


async def test_the_researchers_edit_of_a_papers_details_is_checked_and_kept(tmp_path):
    async with started(tmp_path / "data") as client:
        project = await project_of(client)
        [paper] = (await added(client, project, ("notes.md", synthetic.paper_markdown(arxiv=""))))["materials"]
        await settled(client, project)
        url = f"/api/materials/{paper['id']}"
        for body, code in (({"title": " \u200b "}, "title_needed"), ({"title": ""}, "title_needed"),
                           ({"doi": "not a doi"}, "invalid_doi")):
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


async def test_reading_and_its_failures_log_no_file_name_or_text(tmp_path, caplog):
    caplog.set_level(logging.DEBUG)
    secret = "Participant-Seven-Canary"
    async with started(tmp_path / "data") as client:
        project = await project_of(client)
        await added(client, project, (f"{secret}.md", f"# {secret}\n\n{secret} said this.".encode()),
                    (f"{secret}.pdf", b"%PDF-1.7 " + secret.encode()))
        await settled(client, project)
    assert secret not in caplog.text


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
