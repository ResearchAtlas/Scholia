"""Zotero's library, read only (slice-1 spec F3c, S11, sections 4, 5 and 10; S1-15): the status and
browse endpoints, the import run, re-imports, duplicates and lookups, with a test-owned Zotero stand-in
(tests/scholia_app.py MockZotero) holding made-up items and synthetic PDFs, behind the gate's mock
transport. Lifecycle cases first: cancellation and retries, deletion while the import runs, a change of
level or lock, and a commit followed by a failure or a crash."""

import asyncio
import json
import re

import pytest

import backend.lookup as lookup
import backend.materials as materials_module
import backend.zotero as zotero
from backend.outbound_gate import _ZOTERO_CALLS
import synthetic_materials as synthetic
from scholia_app import MockProvider, MockScholarly, MockZotero, background_idle, openalex_work, run_finished, started
from test_materials import listing, project_of, rows, settled
from test_reference_import import answer, asks_of, imported as imported_references, merge_asks, ris

pytestmark = pytest.mark.asyncio

DOI = "10.5555/scholia.zotero.001"
KEYS = ["AAAA2222", "BBBB3333", "CCCC4444"]


@pytest.fixture(autouse=True)
def quick(monkeypatch):
    monkeypatch.setattr(zotero, "RETRIES", (0.01, 0.02))
    monkeypatch.setattr(lookup, "SPACING", {source: 0.0 for source in lookup.SPACING})
    monkeypatch.setattr(lookup, "RETRIES", (0.01, 0.02))
    monkeypatch.setattr(materials_module, "WAIT_SECONDS", 0.01)


def pdf_file(tmp_path, name, title, doi=DOI):
    path = tmp_path / "zotero-files" / name
    path.parent.mkdir(exist_ok=True)
    path.write_bytes(synthetic.paper_pdf(title, doi=doi))
    return path


def library(tmp_path, *, pdfs=True, **options):
    """A Zotero with three made-up items: two with a PDF (one with a DOI in its record), one without."""
    mock = MockZotero(**options)
    mock.item(KEYS[0], "Synthetic Wages in Small Towns", doi=DOI,
              pdf=pdf_file(tmp_path, "one.pdf", "Synthetic Wages in Small Towns") if pdfs else None)
    mock.item(KEYS[1], "合成数据中的最低工资", pdf=pdf_file(tmp_path, "two.pdf", "合成数据中的最低工资", doi=None) if pdfs else None)
    mock.item(KEYS[2], "A Record Without a File")
    return mock


def provider(mock, **records):
    return MockProvider(zotero=mock, scholarly=MockScholarly(**records))


async def start(client, project, keys=KEYS, library_id="users/0", server_id=None):
    response = await client.post(f"/api/projects/{project}/materials/import-zotero",
                                 json={"library": library_id, "items": keys, "server_id": server_id})
    assert response.status_code == 201, response.text
    return response.json()["run_id"]


async def imported(client, project, keys=KEYS, **options):
    """An import run to its end: its row in the background-run list."""
    return await run_finished(client, await start(client, project, keys, **options))


async def papers(client, project):
    return {m["source_key"]: m for m in await settled(client, project) if m["source"] == "zotero"}


def allowlisted(mock):
    """Every request Zotero got was a GET of an allowlisted call."""
    return all(method == "GET" and _ZOTERO_CALLS.fullmatch(path.encode()) for method, path, _ in mock.requests)


# Lifecycle: cancellation and retries


async def test_cancel_keeps_the_items_committed_and_sends_nothing_after(tmp_path):
    mock = library(tmp_path)
    mock.hold, mock.held = asyncio.Event(), f"items/{KEYS[1]}?"
    async with started(tmp_path / "data", provider(mock)) as client:
        project = await project_of(client)
        run = await start(client, project)
        await asyncio.wait_for(mock.started.wait(), 10)
        sent = len(mock.requests)
        cancelling = asyncio.create_task(client.post(f"/api/runs/{run}/cancel"))
        await asyncio.sleep(0.05)
        mock.hold.set()
        assert (await cancelling).status_code == 200
        row = await run_finished(client, run)
        assert (row["status"], row["cancel_reason"]) == ("cancelled", "researcher")
        assert len(mock.requests) == sent  # nothing after the cancel
        assert list(await papers(client, project)) == [f"users/0/items/{KEYS[0]}"]
        assert await rows(client, "SELECT count(*) FROM budget_reservations") == [(0,)]
        assert row["cost_usd"] == 0


async def test_an_unanswered_request_is_tried_twice_more_then_the_run_fails_keeping_what_committed(tmp_path):
    mock = library(tmp_path, pdfs=False)
    async with started(tmp_path / "data", provider(mock)) as client:
        project = await project_of(client)
        mock.answers = [503, 503]  # the first item's first read, twice: then answered
        row = await imported(client, project, KEYS[:1])
        assert row["status"] == "succeeded" and len(await papers(client, project)) == 1
        mock.answers = [503, 503, 503]  # the next item's, three times: given up
        row = await imported(client, project, KEYS[1:2])
        assert (row["status"], row["result"]["reason"]) == ("failed", "zotero_unavailable")
        assert len(await papers(client, project)) == 1
        mock.answers = [404]  # an item gone since it was browsed: reported, the others imported
        row = await imported(client, project, KEYS[1:])
        assert row["status"] == "succeeded" and (row["result"]["not_found"], row["result"]["imported"]) == (1, 1)


@pytest.mark.parametrize("state, reason", [("not_running", "zotero_not_running"), ("api_off", "zotero_api_off")])
async def test_zotero_closed_or_its_api_off_fails_the_import_with_nothing_written(tmp_path, state, reason):
    async with started(tmp_path / "data", provider(library(tmp_path, state=state))) as client:
        project = await project_of(client)
        row = await imported(client, project)
        assert (row["status"], row["result"]["reason"]) == ("failed", reason)
        assert await rows(client, "SELECT count(*) FROM materials") == [(0,)]


async def test_another_zotero_than_the_one_browsed_stops_the_import(tmp_path):
    mock = library(tmp_path, server_id="server-b")
    async with started(tmp_path / "data", provider(mock)) as client:
        project = await project_of(client)
        row = await imported(client, project, server_id="server-a")
        assert (row["status"], row["result"]["reason"]) == ("failed", "zotero_changed")
        assert await rows(client, "SELECT count(*) FROM materials") == [(0,)]
        row = await imported(client, project, server_id="server-b")
        assert row["status"] == "succeeded"
        assert {headers.get("zotero-server-id") for _, _, headers in mock.requests} == {"server-a", "server-b"}


# Lifecycle: deletion while the import runs


async def test_deleting_the_project_mid_import_stops_it_and_nothing_is_written_after(tmp_path):
    mock = library(tmp_path)
    mock.hold, mock.held = asyncio.Event(), f"items/{KEYS[1]}?"
    async with started(tmp_path / "data", provider(mock)) as client:
        project = await project_of(client)
        run = await start(client, project)
        await asyncio.wait_for(mock.started.wait(), 10)
        assert (await client.delete(f"/api/projects/{project}")).status_code == 200
        sent = len(mock.requests)
        mock.hold.set()
        await background_idle(client, 15)
        assert len(mock.requests) == sent  # the held answer is the last: nothing more is asked
        assert await rows(client, "SELECT count(*) FROM materials WHERE project_id = ?", project) == [(0,)]
        assert await rows(client, "SELECT count(*) FROM index_queue WHERE project_id = ?", project) == [(0,)]
        assert await rows(client, "SELECT status FROM runs WHERE id = ?", run) in ([], [("cancelled",)])


async def test_deleting_a_paper_the_import_updates_revokes_it_and_it_is_not_made_again(tmp_path):
    mock = library(tmp_path, pdfs=False)
    async with started(tmp_path / "data", provider(mock)) as client:
        project = await project_of(client)
        await imported(client, project, KEYS[:1])
        [paper] = (await papers(client, project)).values()
        mock.hold, mock.held = asyncio.Event(), f"items/{KEYS[0]}/children"
        run = await start(client, project, KEYS[:1])
        await asyncio.wait_for(mock.started.wait(), 10)
        assert (await client.delete(f"/api/materials/{paper['id']}")).status_code == 200
        mock.hold.set()
        row = await run_finished(client, run)
        assert (row["status"], row["cancel_reason"]) == ("cancelled", "revoked")
        assert await papers(client, project) == {}


# Lifecycle: a change of level or lock between the start and a dispatch


async def test_tightening_and_locking_mid_import_lets_it_finish_with_no_lookup(tmp_path):
    mock = library(tmp_path)
    mock.hold, mock.held = asyncio.Event(), f"items/{KEYS[1]}?"
    async with started(tmp_path / "data", provider(mock, openalex={DOI: openalex_work(DOI, "Resolved")})) as client:
        project = await project_of(client)
        run = await start(client, project, KEYS[1:])
        await asyncio.wait_for(mock.started.wait(), 10)
        assert (await client.post(f"/api/projects/{project}/sensitivity", json={"level": "private"})).status_code == 200
        assert (await client.post(f"/api/projects/{project}/review-lock", json={"locked": True})).status_code == 200
        mock.hold.set()
        row = await run_finished(client, run)
        assert row["status"] == "succeeded" and row["result"]["imported"] == 2
        levels = [json.loads(data) for (data,) in await rows(
            client, "SELECT data FROM audit_log WHERE event = 'outbound' AND project_id = ? ORDER BY seq", project)]
        assert {(r["kind"], r["decision"]) for r in levels} == {("zotero_local_api", "allow")}
        assert levels[-1]["sensitivity"] == "local_only"
        await background_idle(client, 30)
        assert await rows(client, "SELECT count(*) FROM runs WHERE workflow = 'lookup'") == [(0,)]
        assert client.provider.scholarly.requests == []


# Lifecycle: a commit followed by a failure, a crash or a restart


async def test_an_item_whose_transaction_fails_writes_nothing_and_ends_the_run(tmp_path, monkeypatch):
    mock = library(tmp_path)
    real = zotero._new
    calls = []

    def failing(conn, *args, **kwargs):
        calls.append(1)
        if len(calls) == 2:
            raise RuntimeError("the second item's write fails")
        return real(conn, *args, **kwargs)

    monkeypatch.setattr(zotero, "_new", failing)
    async with started(tmp_path / "data", provider(mock)) as client:
        project = await project_of(client)
        row = await imported(client, project)
        assert (row["status"], row["result"]["reason"]) == ("failed", "internal")
        assert list(await papers(client, project)) == [f"users/0/items/{KEYS[0]}"]
        assert await rows(client, "SELECT count(*) FROM material_versions") == [(1,)]


async def test_a_restart_goes_on_from_the_next_item(tmp_path):
    mock = library(tmp_path)
    mock.hold, mock.held = asyncio.Event(), f"items/{KEYS[1]}?"
    async with started(tmp_path / "data", provider(mock)) as client:
        project = await project_of(client)
        run = await start(client, project)
        await asyncio.wait_for(mock.started.wait(), 10)
    again = library(tmp_path)
    async with started(tmp_path / "data", provider(again), setup=False) as client:
        row = await run_finished(client, run, 30)
        assert row["status"] == "succeeded"
        assert sorted(await papers(client, project)) == [f"users/0/items/{k}" for k in KEYS]
        assert not any(KEYS[0] in path for path in again.paths)  # done before: not asked again
        assert await rows(client, "SELECT count(*) FROM runs WHERE workflow = 'extract'") == [(2,)]


async def test_deleting_a_paper_an_import_made_stops_it_and_a_restart_never_makes_it_again(tmp_path):
    mock = library(tmp_path)
    mock.hold, mock.held = asyncio.Event(), f"items/{KEYS[1]}?"
    async with started(tmp_path / "data", provider(mock)) as client:
        project = await project_of(client)
        run = await start(client, project)
        await asyncio.wait_for(mock.started.wait(), 10)
        [(first,)] = await rows(client, "SELECT id FROM materials WHERE source_key = ?", f"users/0/items/{KEYS[0]}")
        assert (await client.delete(f"/api/materials/{first}")).status_code == 200
        mock.hold.set()
        row = await run_finished(client, run)
        assert (row["status"], row["cancel_reason"]) == ("cancelled", "revoked")
    async with started(tmp_path / "data", provider(library(tmp_path)), setup=False) as client:
        await background_idle(client, 30)
        assert await papers(client, project) == {}


async def test_a_restarted_import_whose_zotero_is_gone_fails(tmp_path):
    mock = library(tmp_path)
    mock.hold, mock.held = asyncio.Event(), f"items/{KEYS[0]}?"
    async with started(tmp_path / "data", provider(mock)) as client:
        project = await project_of(client)
        run = await start(client, project)
        await asyncio.wait_for(mock.started.wait(), 10)
    async with started(tmp_path / "data", provider(MockZotero(state="not_running")), setup=False) as client:
        row = await run_finished(client, run, 30)
        assert (row["status"], row["result"]["reason"]) == ("failed", "zotero_not_running")
        assert await rows(client, "SELECT count(*) FROM materials") == [(0,)]


# The browser


async def test_status_says_whether_zotero_can_be_read_and_lists_its_libraries(tmp_path):
    mock = MockZotero(server_id="srv-1", groups=[(4711, "合成课题组"), (12, "Synthetic Lab")])
    async with started(tmp_path / "data", provider(mock)) as client:
        status = (await client.get("/api/zotero/status")).json()
        assert status == {"state": "ready", "server_id": "srv-1", "libraries": [
            {"id": "users/0", "name": None}, {"id": "groups/4711", "name": "合成课题组"},
            {"id": "groups/12", "name": "Synthetic Lab"}]}
        for state, shown in (("api_off", "api_off"), ("not_running", "not_running"), ("old", "unsupported")):
            mock.state = state
            assert (await client.get("/api/zotero/status")).json() == {"state": shown}
        mock.state = "ready"
        mock.answers = [lambda request: zotero_answer(200, [], {"Zotero-API-Version": "2"})]
        assert (await client.get("/api/zotero/status")).json() == {"state": "unsupported"}
        sent = len(mock.requests)
        assert (await zotero_off(client)).status_code == 200
        assert (await client.get("/api/zotero/status")).json() == {"state": "off"}
        assert (await client.get("/api/zotero/collections", params={"library": "users/0"})).json()["code"] == "zotero_off"
        assert len(mock.requests) == sent  # off: nothing is sent
        assert allowlisted(mock)


async def zotero_off(client):
    current = (await client.get("/api/settings")).json()
    return await client.put("/api/settings", json={"hash": current["hash"], "updates": {"zotero.enabled": False}})


def zotero_answer(status, body, headers):
    import httpx
    return httpx.Response(status, json=body, headers=headers)


async def test_collections_are_read_page_by_page_and_items_a_page_at_a_time(tmp_path):
    mock = MockZotero()
    mock.collections["users/0"] = [{"key": f"COL{n:05d}", "name": f"Collection {n}",
                                    "parent": "COL00000" if n else None} for n in range(150)]
    for n in range(60):
        mock.item(f"ITM{n:05d}", f"Item {n}", collections=["COL00001"] if n < 3 else [])
    mock.item("NOTE0000", "A note", item_type="note")
    async with started(tmp_path / "data", provider(mock)) as client:
        found = (await client.get("/api/zotero/collections", params={"library": "users/0"})).json()["collections"]
        assert len(found) == 150 and found[1] == {"key": "COL00001", "name": "Collection 1", "parent": "COL00000"}
        page = (await client.get("/api/zotero/items", params={"library": "users/0"})).json()
        assert (len(page["items"]), page["total"], page["next"]) == (50, 61, 50)
        assert page["items"][0] == {"key": "ITM00000", "title": "Item 0", "creators": "Example", "year": 2024,
                                    "type": "journalArticle"}
        rest = (await client.get("/api/zotero/items", params={"library": "users/0", "start": 50})).json()
        assert [i["key"] for i in rest["items"]] == [f"ITM{n:05d}" for n in range(50, 60)] and rest["next"] is None
        inside = (await client.get("/api/zotero/items", params={"library": "users/0", "collection": "COL00001"})).json()
        assert [i["key"] for i in inside["items"]] == ["ITM00000", "ITM00001", "ITM00002"]
        for params in ({"library": "users/1"}, {"library": "groups/0"}, {"library": "users/0", "collection": "bad"}):
            assert (await client.get("/api/zotero/items", params=params)).status_code == 400
        mock.state = "not_running"
        assert (await client.get("/api/zotero/items", params={"library": "users/0"})).json()["code"] == "zotero_not_running"
        assert allowlisted(mock)


# Importing


async def test_an_import_makes_one_paper_per_item_with_its_pdf_read(tmp_path):
    mock = library(tmp_path)
    mock.item("DDDD5555", "A Linked File", pdf=pdf_file(tmp_path, "linked.pdf", "A Linked File", doi=None), linked=True)
    mock.item("EEEE6666", "A File on a Share", pdf="file://nas.example/share/paper.pdf")
    keys = [*KEYS, "DDDD5555", "EEEE6666"]
    async with started(tmp_path / "data", provider(mock, openalex={DOI: openalex_work(DOI, "Resolved")})) as client:
        project = await project_of(client)
        row = await imported(client, project, keys)
        assert row["status"] == "succeeded" and row["retryable"] is False
        assert {k: row["result"][k] for k in ("imported", "updated", "not_found", "already")} == {
            "imported": 5, "updated": 0, "not_found": 0, "already": 0}
        assert row["result"]["pdf"] == {"pdf_not_local": 1}
        await background_idle(client, 60)
        found = await papers(client, project)
        assert set(found) == {f"users/0/items/{k}" for k in keys}
        with_file = found[f"users/0/items/{KEYS[1]}"]
        assert (with_file["title"], with_file["state"], with_file["evidence_type"]) == ("合成数据中的最低工资", "ready", "full_text")
        assert with_file["version"]["media_type"] == "application/pdf"
        assert found["users/0/items/DDDD5555"]["state"] == "ready"
        details_only = found[f"users/0/items/{KEYS[2]}"]
        assert (details_only["state"], details_only["evidence_type"], details_only["version"]["media_type"]) == (
            "ready", "metadata", None)
        share = found["users/0/items/EEEE6666"]
        assert (share["state"], share["reason"]) == ("needs_attention", "pdf_not_local")
        # The record keeps S1-14's fields only, and its DOI was looked up (it completes, never replaces, it).
        doi_paper = found[f"users/0/items/{KEYS[0]}"]
        assert "id" not in doi_paper["csl"] and doi_paper["csl"]["DOI"] == DOI
        assert doi_paper["title"] == "Synthetic Wages in Small Towns" and doi_paper["source_key"] == f"users/0/items/{KEYS[0]}"
        assert doi_paper["resolved_at"] is not None and doi_paper["lookup"]["source"] == "openalex"
        assert allowlisted(mock)
        assert not any(path.endswith(("/file", "/file/view")) or "authorize" in path for path in mock.paths)
        assert await rows(client, "SELECT DISTINCT json_extract(data, '$.kind') FROM audit_log WHERE event = 'outbound'"
                                  " AND json_extract(data, '$.destination') = 'http://127.0.0.1:23119'") == [
            ("zotero_local_api",)]


async def test_a_reimport_updates_details_and_a_changed_pdf_becomes_the_next_version(tmp_path):
    mock = library(tmp_path)
    async with started(tmp_path / "data", provider(mock)) as client:
        project = await project_of(client)
        await imported(client, project, KEYS[1:2])
        await background_idle(client, 60)
        row = await imported(client, project, KEYS[1:2])  # nothing changed in Zotero
        assert (row["result"]["unchanged"], row["result"]["versions"]) == (1, 0)
        assert await rows(client, "SELECT count(*) FROM runs WHERE workflow = 'extract'") == [(1,)]
        mock.items["users/0", KEYS[1]]["csljson"]["title"] = "A Retitled Record"
        pdf_file(tmp_path, "two.pdf", "A Retitled Record", doi=None)  # the same attachment, its file changed
        row = await imported(client, project, KEYS[1:2])
        assert (row["result"]["updated"], row["result"]["versions"]) == (1, 1)
        await background_idle(client, 60)
        [paper] = (await papers(client, project)).values()
        assert paper["title"] == "A Retitled Record" and paper["version"]["seq"] == 1
        # Details the researcher edited stay as saved; the file still follows Zotero.
        assert (await client.patch(f"/api/materials/{paper['id']}", json={"title": "My Own Title"})).status_code == 200
        mock.items["users/0", KEYS[1]]["csljson"]["title"] = "Zotero's Newer Title"
        await imported(client, project, KEYS[1:2])
        assert (await papers(client, project))[f"users/0/items/{KEYS[1]}"]["title"] == "My Own Title"
        # A PDF no longer readable leaves the current file as it is.
        mock.files["users/0", KEYS[1][:7] + "Z"] = "file:///nowhere/gone.pdf"
        row = await imported(client, project, KEYS[1:2])
        assert row["result"]["pdf"] == {"pdf_missing": 1}
        paper = (await papers(client, project))[f"users/0/items/{KEYS[1]}"]
        assert (paper["version"]["seq"], paper["state"]) == (1, "ready")


async def test_identifiers_follow_s114s_rule_and_a_local_only_import_asks_once(tmp_path):
    mock = library(tmp_path, pdfs=False)
    mock.item("FFFF7777", "A DOI Written as a Link", doi=f"https://doi.org/{DOI}")
    mock.item("GGGG8888", "A Second DOI", doi=" 10.5555/scholia.zotero.002\n")
    async with started(tmp_path / "data", provider(mock, openalex={DOI: openalex_work(DOI, "Resolved")})) as client:
        project = await project_of(client, level="local_only")
        row = await imported(client, project, [KEYS[0], "FFFF7777", "GGGG8888"])
        assert row["result"]["left_out"] == 1  # the link is no DOI: left out and counted
        [ask] = await asks_of(client, project)
        assert ask["kind"] == "identifier_lookup"
        assert ask["params"] == {"services": ["crossref", "openalex"], "identifiers": 2, "batch": "zotero"}
        assert client.provider.scholarly.requests == []  # nothing before the answer
        assert (await answer(client, ask, "lookup")).status_code == 200
        await background_idle(client, 30)
        assert {path for _, path, _ in client.provider.scholarly.requests} >= {
            f"/works/doi:{DOI}"}
        locked = await project_of(client, "Locked")
        assert (await client.post(f"/api/projects/{locked}/review-lock", json={"locked": True})).status_code == 200
        before = len(client.provider.scholarly.requests)
        assert (await imported(client, locked, [KEYS[0]]))["status"] == "succeeded"
        await background_idle(client, 30)
        assert len(client.provider.scholarly.requests) == before
        assert await rows(client, "SELECT count(*) FROM runs WHERE workflow = 'lookup' AND project_id = ?", locked) == [(0,)]


async def test_duplicates_are_asked_about_and_keep_both_imports_the_item_with_its_pdf(tmp_path):
    mock = library(tmp_path)
    async with started(tmp_path / "data", provider(mock)) as client:
        project = await project_of(client)
        await imported_references(client, project, ris(("Synthetic Wages in Small Towns", f"DO  - {DOI}\n")))
        row = await imported(client, project, KEYS[:1])
        assert row["result"]["waiting"] == 1
        [ask] = await merge_asks(client, project)
        again = await imported(client, project, KEYS[:1])  # imported again meanwhile: asked once
        assert again["result"]["waiting"] == 1 and len(await merge_asks(client, project)) == 1
        assert (await answer(client, ask, "keep_both")).status_code == 200
        await run_finished(client, ask["run_id"])
        await background_idle(client, 60)
        found = await papers(client, project)
        assert found[f"users/0/items/{KEYS[0]}"]["version"]["media_type"] == "application/pdf"
        assert len((await listing(client, project))["materials"]) == 2
        # Nothing to add: already in the project, no question.
        other = await project_of(client, "Other")
        await imported_references(client, other, ris(("Synthetic Wages in Small Towns",
                                                       f"DO  - {DOI}\nAU  - Example, Ana\nPY  - 2024\n")))
        row = await imported(client, other, KEYS[:1])
        assert (row["result"]["already"], row["result"]["waiting"]) == (1, 0)


async def test_merge_fills_the_paper_from_the_zotero_record(tmp_path):
    mock = library(tmp_path, pdfs=False)
    async with started(tmp_path / "data", provider(mock)) as client:
        project = await project_of(client)
        await imported_references(client, project, ris(("Synthetic Wages in Small Towns", f"DO  - {DOI}\n")))
        await imported(client, project, KEYS[:1])
        [ask] = await merge_asks(client, project)
        assert (await answer(client, ask, "merge")).status_code == 200
        await run_finished(client, ask["run_id"])
        [paper] = (await listing(client, project))["materials"]
        assert paper["source"] == "reference_file" and paper["csl"]["author"] == [{"family": "Example", "given": "Ana"}]


async def test_a_pdf_already_in_the_project_is_reported_already_there(tmp_path):
    mock = library(tmp_path)
    async with started(tmp_path / "data", provider(mock)) as client:
        project = await project_of(client)
        data = (tmp_path / "zotero-files" / "two.pdf").read_bytes()
        import base64
        response = await client.post(f"/api/projects/{project}/materials", json={"files": [
            {"name": "two.pdf", "data": base64.b64encode(data).decode()}]})
        assert response.status_code == 201
        row = await imported(client, project, KEYS[1:2])
        assert (row["result"]["already"], row["result"]["imported"]) == (1, 0)


@pytest.mark.parametrize("body, code", [
    ({"library": "users/1", "items": KEYS}, "invalid_request"),
    ({"library": "../x", "items": KEYS}, "invalid_request"),
    ({"library": "users/0", "items": ["aaaa2222"]}, "invalid_request"),
    ({"library": "users/0", "items": ["AAAA22223"]}, "invalid_request"),
    ({"library": "users/0", "items": []}, "invalid_request"),
    ({"library": "users/0", "items": [f"A{n:07d}" for n in range(101)]}, "invalid_request"),
    ({"library": "users/0", "items": KEYS, "server_id": "a b"}, "invalid_request"),
])
async def test_a_request_that_fails_validation_writes_and_sends_nothing(tmp_path, body, code):
    mock = library(tmp_path)
    async with started(tmp_path / "data", provider(mock)) as client:
        project = await project_of(client)
        response = await client.post(f"/api/projects/{project}/materials/import-zotero", json=body)
        assert (response.status_code, response.json()["code"]) == (400, code)
        assert await rows(client, "SELECT count(*) FROM runs WHERE workflow = 'zotero_import'") == [(0,)]
        assert mock.requests == []


async def test_an_unknown_project_or_zotero_off_writes_nothing(tmp_path):
    mock = library(tmp_path)
    async with started(tmp_path / "data", provider(mock)) as client:
        response = await client.post("/api/projects/00000000-0000-4000-8000-000000000000/materials/import-zotero",
                                     json={"library": "users/0", "items": KEYS})
        assert response.status_code == 404
        project = await project_of(client)
        assert (await zotero_off(client)).status_code == 200
        response = await client.post(f"/api/projects/{project}/materials/import-zotero",
                                     json={"library": "users/0", "items": KEYS})
        assert (response.status_code, response.json()["code"]) == (409, "zotero_off")
        assert await rows(client, "SELECT count(*) FROM runs WHERE workflow = 'zotero_import'") == [(0,)]
        assert mock.requests == []


async def test_keys_are_counted_once_and_the_run_list_names_its_papers(tmp_path):
    mock = library(tmp_path, pdfs=False)
    async with started(tmp_path / "data", provider(mock)) as client:
        project = await project_of(client)
        row = await imported(client, project, [KEYS[0], KEYS[0], KEYS[1]])
        assert row["result"]["imported"] == 2 and row["workflow"] == "zotero_import"
        assert row["materials"]["count"] == 2 and row["ask"] is None
        assert len([p for p in mock.paths if "include=data,csljson" in p]) == 2


async def test_a_record_with_no_title_or_a_note_is_not_a_paper(tmp_path):
    mock = MockZotero()
    mock.item("HHHH9999", "   ")
    mock.item("JJJJ2222", "A note", item_type="note")
    async with started(tmp_path / "data", provider(mock)) as client:
        project = await project_of(client)
        row = await imported(client, project, ["HHHH9999", "JJJJ2222"])
        assert (row["result"]["no_title"], row["result"]["not_found"]) == (1, 1)
        assert await rows(client, "SELECT count(*) FROM materials") == [(0,)]


async def test_logs_hold_no_zotero_content(tmp_path, caplog):
    mock = MockZotero()
    mock.item("KKKK3333", "CANARY-TITLE-ZOTERO", pdf="file:///CANARY-PATH/paper.pdf")
    mock.answers = [500]
    async with started(tmp_path / "data", provider(mock)) as client:
        project = await project_of(client)
        await imported(client, project, ["KKKK3333"])
        mock.items["users/0", "KKKK3333"]["csljson"] = "not a record"
        await imported(client, project, ["KKKK3333"])
    text = caplog.text
    assert not re.search(r"CANARY|KKKK3333", text)


async def test_the_zotero_endpoints_answer_only_the_apps_own_window(tmp_path):
    mock = library(tmp_path)
    async with started(tmp_path / "data", provider(mock)) as client:
        project = await project_of(client)
        for method, path, body in (("GET", "/api/zotero/status", None),
                                   ("GET", "/api/zotero/collections?library=users/0", None),
                                   ("GET", "/api/zotero/items?library=users/0", None),
                                   ("POST", f"/api/projects/{project}/materials/import-zotero",
                                    {"library": "users/0", "items": KEYS})):
            response = await client.request(method, path, json=body, headers={"Origin": "http://evil.example"})
            assert response.status_code == 403, path
        assert mock.requests == []
        assert await rows(client, "SELECT count(*) FROM runs WHERE workflow = 'zotero_import'") == [(0,)]


async def test_an_abstract_is_kept_as_the_records_never_as_text(tmp_path):
    mock = MockZotero()
    mock.item("LLLL4444", "A Record With an Abstract", extra={"abstract": "A synthetic abstract, never full text."})
    async with started(tmp_path / "data", provider(mock)) as client:
        project = await project_of(client)
        await imported(client, project, ["LLLL4444"])
        [paper] = (await papers(client, project)).values()
        assert (paper["evidence_type"], paper["csl"]["abstract"]) == ("abstract", "A synthetic abstract, never full text.")
        assert await rows(client, "SELECT count(*) FROM passages") == [(0,)]
        assert await rows(client, "SELECT count(*) FROM index_queue") == [(0,)]


# Review 1's cases


async def test_a_doi_edited_in_scholia_keeps_the_paper_keyed_to_its_zotero_item(tmp_path):
    mock = library(tmp_path)
    async with started(tmp_path / "data", provider(mock)) as client:
        project = await project_of(client)
        await imported(client, project, KEYS[1:2])
        [paper] = (await papers(client, project)).values()
        assert (await client.patch(f"/api/materials/{paper['id']}", json={"doi": "10.5555/scholia.edited"})).status_code == 200
        pdf_file(tmp_path, "two.pdf", "A Changed File", doi=None)
        row = await imported(client, project, KEYS[1:2])
        assert (row["result"]["updated"], row["result"]["versions"], row["result"]["imported"]) == (1, 1, 0)
        [again] = (await papers(client, project)).values()
        assert again["id"] == paper["id"] and again["csl"]["DOI"] == "10.5555/scholia.edited"


async def test_a_zotero_pdf_matching_an_earlier_version_never_displaces_the_researchers_file(tmp_path):
    import base64
    mock = library(tmp_path)
    async with started(tmp_path / "data", provider(mock)) as client:
        project = await project_of(client)
        await imported(client, project, KEYS[1:2])
        await background_idle(client, 60)
        [paper] = (await papers(client, project)).values()
        mine = synthetic.paper_pdf("The Researcher's Own File", doi=None)
        response = await client.post(f"/api/projects/{project}/materials", json={
            "files": [{"name": "mine.pdf", "data": base64.b64encode(mine).decode()}], "material_id": paper["id"],
            "replaces": paper["version"]["id"]})
        assert response.status_code == 201
        row = await imported(client, project, KEYS[1:2])
        assert (row["result"]["unchanged"], row["result"]["versions"]) == (1, 0)
        assert (await papers(client, project))[f"users/0/items/{KEYS[1]}"]["version"]["seq"] == 1


async def test_a_redirect_from_zotero_fails_as_unavailable_never_as_a_changed_project(tmp_path):
    import httpx
    mock = library(tmp_path, pdfs=False)
    moved = lambda request: httpx.Response(302, headers={"Location": "file:///etc/passwd"})  # noqa: E731
    async with started(tmp_path / "data", provider(mock)) as client:
        project = await project_of(client)
        mock.answers = [moved] * 3  # its first request, and both tries again
        row = await imported(client, project, KEYS[:1])
        assert (row["status"], row["cancel_reason"], row["result"]["reason"]) == ("failed", None, "zotero_unavailable")
        mock.answers = [moved]
        assert (await client.get("/api/zotero/status")).json() == {"state": "error"}
        mock.answers = [moved]
        response = await client.get("/api/zotero/items", params={"library": "users/0"})
        assert (response.status_code, response.json()["code"]) == (502, "zotero_unavailable")


async def test_malformed_answers_fail_as_unavailable(tmp_path):
    import httpx
    mock = library(tmp_path, pdfs=False)
    answer = lambda body, **headers: lambda request: httpx.Response(  # noqa: E731
        200, content=body, headers={"Zotero-API-Version": "3", **headers})
    async with started(tmp_path / "data", provider(mock)) as client:
        project = await project_of(client)
        for groups in (b'["x", 3, {"id": "1"}]', b'{"id": 1}', b'[{"id": "1", "data": {}}]'):
            mock.answers = [answer(groups)]
            assert (await client.get("/api/zotero/status")).json() == {"state": "error"}
        mock.answers = [answer(b"[]", **{"Total-Results": b"\xb2"})]
        assert (await client.get("/api/zotero/items", params={"library": "users/0"})).json()["total"] is None
        for body, headers in ((b"[" * 100_000, {}), (b"{}", {"Content-Encoding": "gzip"}),
                              (b" " * (zotero.MAX_ANSWER + 1), {}), (b"not json", {})):
            mock.answers = [answer(body, **headers)]
            response = await client.get("/api/zotero/collections", params={"library": "users/0"})
            assert (response.status_code, response.json()["code"]) == (502, "zotero_unavailable")
        for items in (b'{"x": 1}', b'[{"data": {"key": "not a key"}}]', b'[{"key": "AAAA2222"}]'):
            mock.answers = [answer(items)]
            response = await client.get("/api/zotero/items", params={"library": "users/0"})
            assert (response.status_code, response.json()["code"]) == (502, "zotero_unavailable")
        # An item, or its attachments, of another shape: the import fails, never "no attachments"
        for first, second in ((b'{"data": 1}', None), (b'{"data": {"key": "BBBB3333"}}', None),
                              (None, b'{"data": []}'), (None, b'[{"data": "x"}]'), (None, b'[{"key": "AAAA222Z"}]')):
            mock.answers = [answer(first)] if first else [None, answer(second)]  # or the item as it is, then its children
            row = await imported(client, project, KEYS[:1])
            assert (row["status"], row["result"]["reason"]) == ("failed", "zotero_unavailable"), (first, second)
        assert await rows(client, "SELECT count(*) FROM materials") == [(0,)]


async def test_a_trashed_item_is_not_imported(tmp_path):
    mock = library(tmp_path, pdfs=False)
    mock.items["users/0", KEYS[0]]["data"]["deleted"] = True
    async with started(tmp_path / "data", provider(mock)) as client:
        project = await project_of(client)
        row = await imported(client, project, KEYS[:2])
        assert (row["result"]["not_found"], row["result"]["imported"]) == (1, 1)


async def test_cancel_while_a_pdf_is_read_writes_nothing_for_it(tmp_path, monkeypatch):
    mock = library(tmp_path)
    reading, release = asyncio.Event(), asyncio.Event()
    loop = asyncio.get_running_loop()
    real = zotero.read_attachment

    def slow(text, **kwargs):
        loop.call_soon_threadsafe(reading.set)
        asyncio.run_coroutine_threadsafe(release.wait(), loop).result(10)
        return real(text, **kwargs)

    monkeypatch.setattr(zotero, "read_attachment", slow)
    async with started(tmp_path / "data", provider(mock)) as client:
        project = await project_of(client)
        run = await start(client, project, KEYS[:1])
        await asyncio.wait_for(reading.wait(), 10)
        cancelling = asyncio.create_task(client.post(f"/api/runs/{run}/cancel"))
        await asyncio.sleep(0.3)
        # The read is still going: the run does not end before it (nothing of it outlasts the run's record).
        assert (await client.get("/api/activity", params={"run_id": run})).json()["runs"][0]["status"] == "running"
        release.set()
        assert (await cancelling).status_code == 200
        row = await run_finished(client, run)
        assert (row["status"], row["cancel_reason"]) == ("cancelled", "researcher")
        assert await rows(client, "SELECT count(*) FROM materials") == [(0,)]
        assert await rows(client, "SELECT count(*) FROM content_files") == [(0,)]  # read to its end, never stored


# Harold's decision of 2026-10-17: an arXiv ID from Zotero's archiveID, in its exact form only


async def test_an_archive_id_of_the_exact_form_is_looked_up_in_the_imports_one_batch(tmp_path):
    mock = library(tmp_path, pdfs=False)
    preprint = mock.item("PPPP2222", "A Synthetic Preprint", extra={"number": "arXiv:2401.99999"})
    preprint["data"]["archiveID"] = " arXiv:2401.00002v3\n"
    for key, near in (("QQQQ2222", "arXiv: 2401.00003"), ("RRRR2222", "arxiv:2401.00004"), ("SSSS2222", "2401.00005"),
                      ("TTTT2222", "arXiv:2401.00006 (v2)"), ("UUUU2222", "arXiv:https://arxiv.org/abs/2401.00007")):
        mock.item(key, f"A Near Miss {key}")["data"]["archiveID"] = near
    async with started(tmp_path / "data", provider(mock, openalex={DOI: openalex_work(DOI, "Resolved")},
                                                    arxiv={"2401.00002": "A Synthetic Preprint, as arXiv has it"})) as client:
        project = await project_of(client, level="local_only")
        keys = [KEYS[0], "PPPP2222", "QQQQ2222", "RRRR2222", "SSSS2222", "TTTT2222", "UUUU2222"]
        row = await imported(client, project, keys)
        assert row["result"]["imported"] == 7 and row["result"]["left_out"] == 0  # a near miss is ignored, not counted
        [ask] = await asks_of(client, project)  # one question for the import: the DOI and the arXiv ID
        assert ask["params"] == {"services": ["arxiv", "crossref", "openalex"], "identifiers": 2, "batch": "zotero"}
        found = await rows(client, "SELECT source_key, json_extract(csl, '$.arxiv') FROM materials WHERE source = 'zotero'")
        assert dict(found) == {f"users/0/items/{KEYS[0]}": None, "users/0/items/PPPP2222": "2401.00002v3",
                               **{f"users/0/items/{k}": None for k in keys[2:]}}
        assert (await answer(client, ask, "lookup")).status_code == 200
        await background_idle(client, 30)
        sent = [(host, path) for host, path, _ in client.provider.scholarly.requests]
        assert ("export.arxiv.org", "/api/query?id_list=2401.00002&max_results=1") in sent
        assert not any(near in path for _, path in sent for near in ("2401.00003", "2401.00004", "2401.00005",
                                                                      "2401.00006", "2401.00007", "2401.99999"))
        [preprint_paper] = [m for m in await settled(client, project) if m["source_key"] == "users/0/items/PPPP2222"]
        assert preprint_paper["title"] == "A Synthetic Preprint" and preprint_paper["lookup"]["source"] == "arxiv"


# gpt-6-astra review 1 (PR 33)


async def test_a_lookup_for_a_doi_the_record_no_longer_gives_applies_nothing(tmp_path):
    other = "10.5555/scholia.zotero.other"
    mock = library(tmp_path, pdfs=False)
    scholarly = MockScholarly(openalex={DOI: openalex_work(DOI, "Record A", venue="Journal of A", retracted=True)})
    scholarly.hold = asyncio.Event()
    async with started(tmp_path / "data", MockProvider(zotero=mock, scholarly=scholarly)) as client:
        project = await project_of(client)
        await imported(client, project, KEYS[:1])
        await asyncio.wait_for(scholarly.started.wait(), 10)  # A's lookup is under way, its answer held
        mock.items["users/0", KEYS[0]]["csljson"]["DOI"] = other  # Zotero's record now names B
        row = await imported(client, project, KEYS[:1])
        assert row["result"]["updated"] == 1
        scholarly.hold.set()
        await background_idle(client, 30)
        [paper] = (await papers(client, project)).values()
        assert paper["csl"]["DOI"] == other
        assert "container-title" not in paper["csl"] and paper["retraction"] != "retracted" and paper["resolved_at"] is None
        steps = [json.loads(d) for (d,) in await rows(client, "SELECT data FROM run_events e JOIN runs r ON r.id = e.run_id"
                                                         " WHERE r.workflow = 'lookup' AND e.type = 'step_finished'")]
        assert {(s["identifier"], s["outcome"]) for s in steps} == {(f"doi:{DOI}", "replaced"), (f"doi:{other}", "not_found")}


async def test_deleting_a_paper_revokes_every_import_of_its_item_even_one_admitted_before_it_existed(tmp_path,
                                                                                                    monkeypatch):
    mock = library(tmp_path, pdfs=False)
    real, calls, both, later = zotero._fetch_item, [], asyncio.Event(), asyncio.Event()

    async def fetch(harness, active, *args):
        calls.append(active.run_id)
        await (both if len(calls) == 1 else later).wait()
        return await real(harness, active, *args)

    monkeypatch.setattr(zotero, "_fetch_item", fetch)
    async with started(tmp_path / "data", provider(mock)) as client:
        project = await project_of(client)
        runs = [await start(client, project, KEYS[:1]), await start(client, project, KEYS[:1])]
        both.set()  # both admitted while the paper did not exist: the first goes on
        first = await run_finished(client, calls[0])
        assert first["status"] == "succeeded"
        [(paper,)] = await rows(client, "SELECT id FROM materials")
        assert (await client.delete(f"/api/materials/{paper}")).status_code == 200
        later.set()
        second = await run_finished(client, next(r for r in runs if r != calls[0]))
        assert (second["status"], second["cancel_reason"]) == ("cancelled", "revoked")
        assert await rows(client, "SELECT count(*) FROM materials") == [(0,)]


async def test_an_import_has_one_lookup_batch_and_one_question_however_long_it_takes(tmp_path, monkeypatch):
    monkeypatch.setattr(materials_module, "BATCH_IDLE_SECONDS", 0.2)
    mock = library(tmp_path, pdfs=False)
    for n, key in enumerate(KEYS[1:], start=2):
        mock.items["users/0", key]["csljson"]["DOI"] = f"10.5555/scholia.zotero.00{n}"
    real = zotero._fetch_item

    async def slow(harness, active, client, library_id, key, *args):
        if key != KEYS[0]:
            await asyncio.sleep(0.8)  # each later item past the drop batch's idle time
        return await real(harness, active, client, library_id, key, *args)

    monkeypatch.setattr(zotero, "_fetch_item", slow)
    async with started(tmp_path / "data", provider(mock)) as client:
        project = await project_of(client, level="local_only")
        row = await imported(client, project)
        assert row["result"]["imported"] == 3
        [ask] = await asks_of(client, project)
        assert ask["params"]["identifiers"] == 3 and ask["params"]["batch"] == "zotero"
        assert await rows(client, "SELECT count(*) FROM runs WHERE workflow = 'lookup'") == [(1,)]
