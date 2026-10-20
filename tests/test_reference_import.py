"""Reference file import (slice-1 spec F3b, sections 5, 6.2, 10 and 13; S1-14): the import endpoint, the
duplicate check, the merge confirmation and its run, and the lookups of imported papers, with
synthetic reference files and test-owned OpenAlex, Crossref and arXiv stand-ins behind the gate.
Lifecycle cases first: cancellation, deletion while work runs, a change of level or lock, and a
commit followed by a failed report or a crash."""

import asyncio
import base64
import json

import pytest

import backend.lookup as lookup
import backend.materials as materials_module
import backend.references as references
from backend import asks, reading, reference_files
import synthetic_materials as synthetic
from scholia_app import (MockProvider, MockScholarly, background_idle, crossref_work, openalex_work, run_finished,
                         started)
from test_materials import added, listing, project_of, rows, settled

pytestmark = pytest.mark.asyncio

DOI = "10.5555/scholia.import.001"


@pytest.fixture(autouse=True)
def quick(monkeypatch):
    monkeypatch.setattr(lookup, "SPACING", {source: 0.0 for source in lookup.SPACING})
    monkeypatch.setattr(lookup, "RETRIES", (0.01, 0.02))
    monkeypatch.setattr(materials_module, "WAIT_SECONDS", 0.01)


def scholarly(**records):
    return MockProvider(scholarly=MockScholarly(**records))


def ris(*records):
    """A RIS file of records, each (title, extra lines)."""
    return "".join(f"TY  - JOUR\nTI  - {title}\n{extra}ER  - \n" for title, extra in records).encode()


async def post(client, project, data, name="library.ris"):
    return await client.post(f"/api/projects/{project}/materials/import-references",
                             json={"name": name, "data": base64.b64encode(data).decode()})


async def imported(client, project, data, name="library.ris"):
    response = await post(client, project, data, name)
    assert response.status_code == 201, response.text
    return response.json()


async def asks_of(client, project, count=1, kind=None):
    """The project's open questions (of kind), once there are count of them."""
    deadline = asyncio.get_running_loop().time() + 10
    while len(found := [a for a in (await listing(client, project))["asks"] if kind in (None, a["kind"])]) < count:
        assert asyncio.get_running_loop().time() < deadline, found
        await asyncio.sleep(0.02)
    return found


async def merge_asks(client, project, count=1):
    return await asks_of(client, project, count, "merge")


async def answer(client, ask, option):
    return await client.post(f"/api/runs/{ask['run_id']}/asks/{ask['ask_id']}", json={"option": option})


async def papers(client, project):
    return {m["title"]: m for m in (await listing(client, project))["materials"]}


async def with_duplicate(client, project, extra="AB  - The record's abstract.\n"):
    """A paper imported first, then a record of its DOI that would add to it: (paper id, the merge's ask)."""
    first = await imported(client, project, ris(("A Paper Already Here", f"DO  - {DOI}\nPY  - 2020\n")))
    second = await imported(client, project, ris(("A Paper Already Here", f"DO  - {DOI}\nPY  - 2020\n{extra}")))
    assert second["imported"] == [] and [w["material_id"] for w in second["waiting"]] == first["imported"]
    [ask] = await merge_asks(client, project)
    return first["imported"][0], ask


async def held(client, run_id):
    (inputs,) = (await rows(client, "SELECT inputs FROM runs WHERE id = ?", run_id))[0]
    return json.loads(inputs).get("held")


# Lifecycle: cancellation


async def test_a_merge_cancelled_while_its_question_waits_writes_nothing_and_closes_it(tmp_path):
    async with started(tmp_path / "data") as client:
        project = await project_of(client)
        paper, ask = await with_duplicate(client, project)
        before = await rows(client, "SELECT csl, updated_at FROM materials")
        for _ in range(2):  # Cancel twice is safe
            assert (await client.post(f"/api/runs/{ask['run_id']}/cancel")).status_code == 200
        run = await run_finished(client, ask["run_id"])
        assert (run["status"], run["cancel_reason"], run["ask"]) == ("cancelled", "researcher", None)
        assert (await answer(client, ask, "merge")).status_code == 409  # its question closed with it
        assert await rows(client, "SELECT csl, updated_at FROM materials") == before
        assert await held(client, ask["run_id"]) is None  # the record left out, kept nowhere
        assert (await listing(client, project))["asks"] == []
        assert run["cost_usd"] == 0  # no model call: nothing to settle


async def test_the_imports_lookup_tried_again_takes_its_identifiers_from_the_records_and_asks_again(tmp_path):
    async with started(tmp_path / "data", scholarly(openalex={DOI: openalex_work(DOI, "Resolved")})) as client:
        project = await project_of(client, level="local_only")
        result = await imported(client, project, ris(("An Imported Paper", f"DO  - {DOI}\n")))
        [ask] = await asks_of(client, project)
        assert ask["kind"] == "identifier_lookup" and ask["params"]["batch"] == "reference_file"
        assert (await client.post(f"/api/runs/{result['lookup_run_id']}/cancel")).status_code == 200
        await run_finished(client, result["lookup_run_id"])
        retried = await client.post(f"/api/runs/{result['lookup_run_id']}/retry")
        assert retried.status_code == 201
        [again] = await asks_of(client, project)
        assert again["run_id"] == retried.json()["run_id"]
        assert again["params"] == {"services": ["crossref", "openalex"], "identifiers": 1, "batch": "reference_file"}
        assert client.provider.scholarly.requests == []  # nothing before the answer
        assert (await answer(client, again, "lookup")).status_code == 200
        assert (await run_finished(client, again["run_id"]))["status"] == "succeeded"
        [paper] = await settled(client, project)
        assert paper["source_key"] == "openalex:W0000000001" and paper["title"] == "An Imported Paper"


# Lifecycle: deletion while work runs


async def test_deleting_the_paper_a_merge_waits_on_revokes_it_and_writes_nothing(tmp_path):
    async with started(tmp_path / "data") as client:
        project = await project_of(client)
        paper, ask = await with_duplicate(client, project)
        assert (await client.delete(f"/api/materials/{paper}")).status_code == 200
        run = await run_finished(client, ask["run_id"])
        assert (run["status"], run["cancel_reason"]) == ("cancelled", "revoked")
        assert (await answer(client, ask, "keep_both")).status_code == 409
        assert await rows(client, "SELECT count(*) FROM materials") == [(0,)]  # the record not imported
        assert await held(client, ask["run_id"]) is None
        assert (await listing(client, project))["asks"] == []


async def test_an_answer_recorded_before_the_paper_is_deleted_is_never_applied(tmp_path, monkeypatch):
    monkeypatch.setattr(references, "POLICY_SECONDS", 60)  # it wakes only when told, or at its revocation
    async with started(tmp_path / "data") as client:
        project = await project_of(client)
        paper, ask = await with_duplicate(client, project)
        registry = client.state["harness"].registry
        await asyncio.to_thread(client.state["db"].write, lambda conn: asks.answer(  # recorded, not yet applied
            conn, ask["run_id"], ask["ask_id"], "keep_both", registry=registry))
        assert (await client.delete(f"/api/materials/{paper}")).status_code == 200
        run = await run_finished(client, ask["run_id"])
        assert (run["status"], run["cancel_reason"]) == ("cancelled", "revoked")
        assert await rows(client, "SELECT count(*) FROM materials") == [(0,)]  # Keep both dropped with it


async def test_deleting_an_imported_paper_while_its_local_only_lookup_asks_sends_nothing(tmp_path):
    async with started(tmp_path / "data", scholarly(openalex={DOI: openalex_work(DOI, "Resolved")})) as client:
        project = await project_of(client, level="local_only")
        result = await imported(client, project, ris(("An Imported Paper", f"DO  - {DOI}\n")))
        asked = await asks_of(client, project)
        assert (await client.delete(f"/api/materials/{result['imported'][0]}")).status_code == 200
        run = await run_finished(client, result["lookup_run_id"])
        assert (run["status"], run["cancel_reason"]) == ("cancelled", "revoked")
        assert (await answer(client, asked[0], "lookup")).status_code == 409
        assert client.provider.scholarly.requests == []
        assert await rows(client, "SELECT count(*) FROM index_queue") == [(0,)]  # it never had passages


async def test_a_project_deleted_while_its_file_is_read_gets_nothing_written(tmp_path, monkeypatch):
    reached, go = asyncio.Event(), asyncio.Event()
    read = reading.references

    def held_read(data, stop):
        loop.call_soon_threadsafe(reached.set)
        asyncio.run_coroutine_threadsafe(go.wait(), loop).result(10)
        return read(data, stop)

    monkeypatch.setattr(reading, "references", held_read)
    async with started(tmp_path / "data") as client:
        loop = asyncio.get_running_loop()
        project = await project_of(client)
        importing = asyncio.create_task(post(client, project, synthetic.references_ris()))
        await asyncio.wait_for(reached.wait(), 10)
        assert (await client.delete(f"/api/projects/{project}")).status_code == 200
        go.set()
        response = await importing
        assert (response.status_code, response.json()["code"]) == (404, "not_found")
        assert await rows(client, "SELECT count(*) FROM materials") == [(0,)]
        assert await rows(client, "SELECT count(*) FROM runs WHERE workflow IN ('merge', 'lookup')") == [(0,)]


async def test_deleting_the_project_deletes_its_merge_runs_and_their_questions(tmp_path):
    async with started(tmp_path / "data") as client:
        project = await project_of(client)
        _, ask = await with_duplicate(client, project)
        assert (await client.delete(f"/api/projects/{project}")).status_code == 200
        await background_idle(client)
        assert await rows(client, "SELECT count(*) FROM runs WHERE id = ?", ask["run_id"]) == [(0,)]
        assert (await client.get("/api/asks")).json()["asks"] == []


# Lifecycle: a change of level or lock between the import and a dispatch


@pytest.mark.parametrize("change", ["local_only", "lock"])
async def test_a_tightening_or_the_lock_before_the_lookup_sends_stops_it(tmp_path, change):
    async with started(tmp_path / "data", scholarly(openalex={DOI: openalex_work(DOI, "Resolved")})) as client:
        project = await project_of(client)
        mock = client.provider.scholarly
        mock.hold = asyncio.Event()
        result = await imported(client, project, ris(("First", f"DO  - {DOI}\n"), ("Second", "DO  - 10.5555/second\n")))
        await asyncio.wait_for(mock.started.wait(), 10)
        response = await (client.post(f"/api/projects/{project}/sensitivity", json={"level": "local_only"})
                          if change == "local_only" else
                          client.post(f"/api/projects/{project}/review-lock", json={"locked": True}))
        assert response.status_code == 200, response.text
        mock.hold.set()
        run = await run_finished(client, result["lookup_run_id"])
        assert (run["status"], run["cancel_reason"]) == ("cancelled", "revoked")
        await background_idle(client)
        assert len(mock.requests) == 1  # the one in flight; nothing after the change


async def test_an_import_into_a_locked_project_makes_no_lookup_but_still_asks_its_merges(tmp_path):
    async with started(tmp_path / "data") as client:
        project = (await client.post("/api/projects", json={"name": "Review", "sensitivity": "local_only", "review_lock": True})).json()["id"]
        paper, ask = await with_duplicate(client, project)
        assert await rows(client, "SELECT count(*) FROM runs WHERE workflow = 'lookup'") == [(0,)]
        assert (await answer(client, ask, "merge")).status_code == 200
        assert (await run_finished(client, ask["run_id"]))["status"] == "succeeded"
        assert client.provider.scholarly.requests == []


async def test_a_local_only_import_loosened_while_its_lookup_asks_looks_up_without_asking(tmp_path):
    async with started(tmp_path / "data", scholarly(openalex={DOI: openalex_work(DOI, "Resolved")})) as client:
        project = await project_of(client, level="local_only")
        result = await imported(client, project, ris(("An Imported Paper", f"DO  - {DOI}\n")))
        await asks_of(client, project)
        token = (await client.post(f"/api/projects/{project}/sensitivity", json={"level": "normal"})).json()["token"]
        assert (await client.post(f"/api/projects/{project}/sensitivity",
                                  json={"level": "normal", "token": token})).status_code == 200
        assert (await run_finished(client, result["lookup_run_id"]))["status"] == "succeeded"


@pytest.mark.parametrize("change", ["level", "lock"])
async def test_a_change_of_level_or_lock_while_a_merge_waits_asks_it_again_under_the_new_policy(tmp_path, monkeypatch,
                                                                                               change):
    monkeypatch.setattr(references, "POLICY_SECONDS", 0.05)
    async with started(tmp_path / "data") as client:
        project = await project_of(client)
        paper, ask = await with_duplicate(client, project)
        response = await (client.post(f"/api/projects/{project}/sensitivity", json={"level": "private"})
                          if change == "level" else client.post(f"/api/projects/{project}/review-lock", json={"locked": True}))
        assert response.status_code == 200, response.text
        late = await answer(client, ask, "merge")  # asked under the earlier policy: refused
        assert late.status_code in (404, 409) and late.json()["code"] in ("ask_invalid", "not_found")
        deadline = asyncio.get_running_loop().time() + 10
        while not (again := [a for a in (await listing(client, project))["asks"] if a["ask_id"] != ask["ask_id"]]):
            assert asyncio.get_running_loop().time() < deadline
            await asyncio.sleep(0.02)
        assert again[0]["run_id"] == ask["run_id"]  # the same run, not revoked: a merge sends nothing
        assert (await answer(client, again[0], "merge")).status_code == 200
        assert (await run_finished(client, ask["run_id"]))["status"] == "succeeded"
        found = await papers(client, project)
        assert found["A Paper Already Here"]["csl"]["abstract"] == "The record's abstract."


# Lifecycle: a commit followed by a failed report, and crashes


async def test_an_import_whose_response_is_never_delivered_stands_and_its_runs_run(tmp_path, monkeypatch):
    writing, go = asyncio.Event(), asyncio.Event()
    make = references.new_material

    def slow(conn, project_id, csl, source="reference_file"):  # the transaction is under way
        loop.call_soon_threadsafe(writing.set)
        asyncio.run_coroutine_threadsafe(go.wait(), loop).result(10)
        return make(conn, project_id, csl, source)

    monkeypatch.setattr(references, "new_material", slow)
    async with started(tmp_path / "data", scholarly(openalex={DOI: openalex_work(DOI, "Resolved")})) as client:
        loop = asyncio.get_running_loop()
        project = await project_of(client)
        request = asyncio.create_task(post(client, project, ris(("Kept on", f"DO  - {DOI}\n"))))
        await asyncio.wait_for(writing.wait(), 10)
        request.cancel()  # the client went away while the import committed
        go.set()
        with pytest.raises(asyncio.CancelledError):
            await request
        [paper] = await settled(client, project)
        assert paper["title"] == "Kept on" and paper["lookup"]["status"] == "succeeded"


async def test_a_merge_answered_before_a_crash_is_applied_once_at_the_next_launch(tmp_path, monkeypatch):
    monkeypatch.setattr(references, "POLICY_SECONDS", 60)
    data = tmp_path / "data"
    async with started(data) as client:
        project = await project_of(client)
        paper, ask = await with_duplicate(client, project)
        registry = client.state["harness"].registry
        await asyncio.to_thread(client.state["db"].write, lambda conn: asks.answer(  # answered, the app then stops
            conn, ask["run_id"], ask["ask_id"], "keep_both", registry=registry))
    for _ in range(2):  # the launch after applies it; the one after that finds it finished
        async with started(data, setup=False) as client:
            assert (await run_finished(client, ask["run_id"]))["status"] == "succeeded"
            assert await rows(client, "SELECT count(*) FROM materials") == [(2,)]  # one paper more, never two
            assert await rows(client, "SELECT count(*) FROM run_events WHERE run_id = ? AND type = 'ask'",
                              ask["run_id"]) == [(1,)]


async def test_a_merge_waiting_at_shutdown_waits_at_the_next_launch_asked_once(tmp_path):
    data = tmp_path / "data"
    async with started(data) as client:
        project = await project_of(client)
        _, ask = await with_duplicate(client, project)
        assert await rows(client, "SELECT waiting FROM runs WHERE id = ?", ask["run_id"]) == [("ask",)]  # no hold on idle backups
    async with started(data, setup=False) as client:
        [again] = await merge_asks(client, project)
        assert again["ask_id"] == ask["ask_id"]
        assert await rows(client, "SELECT count(*) FROM run_events WHERE run_id = ? AND type = 'ask'",
                          ask["run_id"]) == [(1,)]
        assert (await answer(client, again, "merge")).status_code == 200
        assert (await run_finished(client, ask["run_id"]))["status"] == "succeeded"


# The endpoint


@pytest.mark.parametrize("data, status, code", [
    (b"plain text, no reference format", 400, "unsupported_reference_file"),
    (b"[1, 2", 400, "unreadable_file"),
    (b"  ", 400, "no_records"),
    (ris(("T", "")) * (reference_files.MAX_RECORDS + 1), 413, "too_many_records"),
    (b"TY  - JOUR\n" + b"x" * reference_files.MAX_BYTES, 413, "reference_file_too_large"),
])
async def test_a_file_that_cannot_be_imported_is_refused_by_code_and_writes_nothing(tmp_path, data, status, code):
    async with started(tmp_path / "data") as client:
        project = await project_of(client)
        response = await post(client, project, data)
        assert (response.status_code, response.json()["code"]) == (status, code)
        assert await rows(client, "SELECT count(*) FROM materials") == [(0,)]
        assert await rows(client, "SELECT count(*) FROM runs WHERE kind = 'background'") == [(0,)]


async def test_a_request_that_fails_validation_writes_nothing(tmp_path):
    async with started(tmp_path / "data") as client:
        project = await project_of(client)
        for body in ({"name": "​", "data": "VFkgIC0="}, {"name": "a.ris", "data": "not base64!"}, {"name": "a.ris"}):
            response = await client.post(f"/api/projects/{project}/materials/import-references", json=body)
            assert (response.status_code, response.json()["code"]) == (400, "invalid_request")
        response = await post(client, "no-such-project", synthetic.references_ris())
        assert (response.status_code, response.json()["code"]) == (404, "not_found")
        assert await rows(client, "SELECT count(*) FROM materials") == [(0,)]


async def test_each_record_becomes_a_metadata_only_paper_with_no_file_reading_or_index_row(tmp_path):
    async with started(tmp_path / "data") as client:
        project = await project_of(client)
        result = await imported(client, project, synthetic.references_ris())
        assert (result["format"], result["skipped"], len(result["imported"])) == ("ris", {"invalid_field": 1}, 3)  # its file: URL
        await background_idle(client)
        found = await papers(client, project)
        english = found["Synthetic Evidence from Reference Files"]
        assert (english["source"], english["evidence_type"], english["state"], english["extraction"]) == (
            "reference_file", "abstract", "ready", None)
        assert english["version"] == {"id": english["version"]["id"], "seq": 0, "media_type": None, "size": None,
                                      "created_at": english["version"]["created_at"]}
        assert english["csl"]["DOI"] == synthetic.REF_DOI and english["readable"] is False
        assert found["A Chapter without an Identifier"]["evidence_type"] == "metadata"
        assert await rows(client, "SELECT count(*) FROM content_files") == [(0,)]  # the file is never stored
        assert await rows(client, "SELECT count(*) FROM runs WHERE workflow = 'extract'") == [(0,)]
        assert await rows(client, "SELECT count(*) FROM index_queue") == [(0,)]
        assert await rows(client, "SELECT DISTINCT proposed_by FROM materials") == [("researcher",)]
        versions = (await client.get(f"/api/materials/{english['id']}/versions")).json()["versions"]
        assert [(v["seq"], v["is_current"], v["media_type"], v["extraction"]) for v in versions] == [(0, True, None, None)]


async def test_records_skipped_are_counted_by_reason(tmp_path):
    async with started(tmp_path / "data") as client:
        project = await project_of(client)
        bibtex = b"@article{a, title={Fine}}\n@article{b, title={}}\n@article{bad, title={unbalanced\n@misc{c, title={Also}}\n"
        result = await imported(client, project, bibtex, "export.bib")
        assert result["skipped"] == {"no_title": 1, "unreadable": 1} and len(result["imported"]) == 2


async def test_two_imports_of_one_file_at_once_never_both_create_its_papers(tmp_path):
    async with started(tmp_path / "data") as client:
        project = await project_of(client)
        first, second = await asyncio.gather(*(imported(client, project, synthetic.references_ris()) for _ in range(2)))
        assert sorted([len(first["imported"]), len(second["imported"])]) == [0, 3]
        assert sorted([len(first["already"]), len(second["already"])]) == [0, 3]
        assert await rows(client, "SELECT count(*) FROM materials") == [(3,)]


# Duplicates


@pytest.mark.parametrize("theirs, ours, matched", [
    (f"DO  - {DOI}\n", f"DO  - {DOI.upper()}\n", "doi"),
    (f"DO  - {DOI}\n", f"DO  - doi:{DOI}\n", "doi"),  # one of the approved prefixes (Harold, 2026-10-17)
    (f"DO  - {DOI}\n", f"DO  - https://doi.org/{DOI.upper()}\n", "doi"),
    (f"DO  - {DOI}\n", f"DO  - doi.org/{DOI}\n", "title"),  # no other: this one is left out
    ("PY  - 2020\n", "PY  - 2020\n", "title"),
    ("", "", "title"),  # neither has a year: the title alone
])
async def test_a_duplicate_matches_by_doi_then_by_normalized_title_and_year(tmp_path, theirs, ours, matched):
    async with started(tmp_path / "data") as client:
        project = await project_of(client)
        await imported(client, project, ris(("Wages, Workers & Firms: A {Synthetic} Study", theirs)))
        result = await imported(client, project, ris(("WAGES WORKERS AND FIRMS A SYNTHETIC STUDY".replace(" AND", ""),
                                                      ours + "AB  - New abstract.\n")))
        [waiting] = result["waiting"]
        [ask] = await merge_asks(client, project)
        assert ask["params"]["matched"] == matched and ask["run_id"] == waiting["run_id"]


@pytest.mark.parametrize("theirs, ours", [
    (f"DO  - {DOI}\nPY  - 2020\n", "DO  - 10.5555/another.work\nPY  - 2020\n"),  # different DOIs never match
    ("PY  - 2020\n", "PY  - 2021\n"),
    ("PY  - 2020\n", ""),  # a year on one side only
])
async def test_records_that_are_not_duplicates_become_papers_of_their_own(tmp_path, theirs, ours):
    async with started(tmp_path / "data") as client:
        project = await project_of(client)
        await imported(client, project, ris(("Same Title", theirs)))
        result = await imported(client, project, ris(("Same Title", ours)))
        assert len(result["imported"]) == 1 and result["waiting"] == [] and result["already"] == []


async def test_titles_compare_in_their_normalized_form():
    key = references.title_key
    assert key("Ｗａｇｅｓ：最低工资，研究") == key("wages 最低工资 研究") == "wages最低工资研究"
    assert key("Café — Studies!") == key("café studies") and key("​ ,.;") is None and key(None) is None


async def test_a_record_with_nothing_to_add_is_already_here_with_no_question(tmp_path):
    async with started(tmp_path / "data") as client:
        project = await project_of(client)
        first = await imported(client, project, synthetic.references_ris())
        again = await imported(client, project, synthetic.references_ris())
        assert again["imported"] == [] and again["waiting"] == [] and sorted(again["already"]) == sorted(first["imported"])
        await background_idle(client)
        assert await rows(client, "SELECT count(*) FROM runs WHERE workflow = 'merge'") == [(0,)]


async def test_records_within_one_file_are_checked_against_each_other(tmp_path):
    async with started(tmp_path / "data") as client:
        project = await project_of(client)
        result = await imported(client, project, ris(("Twice in the File", f"DO  - {DOI}\n"),
                                                     ("Twice in the File", f"DO  - {DOI}\nVL  - 3\n")))
        assert len(result["imported"]) == 1 and len(result["waiting"]) == 1


async def test_a_paper_from_a_file_matches_by_the_doi_its_lookup_found(tmp_path):
    async with started(tmp_path / "data", scholarly(openalex={synthetic.DOI: openalex_work(synthetic.DOI, "Looked Up")})) as client:
        project = await project_of(client)
        await added(client, project, ("paper.pdf", synthetic.paper_pdf()))
        [paper] = await settled(client, project)
        result = await imported(client, project, ris(("Some Other Title", f"DO  - {synthetic.DOI}\nAB  - Its abstract.\n")))
        assert [w["material_id"] for w in result["waiting"]] == [paper["id"]]


# Merge and Keep both


async def test_merge_fills_only_what_the_paper_lacks_and_shows_both_records_first(tmp_path):
    async with started(tmp_path / "data") as client:
        project = await project_of(client)
        await imported(client, project, ris(("Kept Title", f"DO  - {DOI}\nPY  - 2020\nAU  - Kept, Author\nVL  - 1\n")))
        await imported(client, project, ris(("kept title", f"DO  - {DOI}\nPY  - 1999\nAU  - Other, Name\nVL  - 9\n"
                                                           "AB  - Added abstract.\nIS  - 4\n")))
        [ask] = await merge_asks(client, project)
        assert ask["options"] == ["merge", "keep_both"] and ask["text_box"] is False
        assert ask["params"]["paper"]["title"] == "Kept Title" and ask["params"]["record"]["title"] == "kept title"
        assert ask["params"]["record"]["authors"] == ["Other, Name"] and ask["params"]["paper"]["year"] == 2020
        text = await client.post(f"/api/runs/{ask['run_id']}/asks/{ask['ask_id']}", json={"text": "merge them"})
        assert text.status_code == 400  # a merge takes one of its options
        assert (await answer(client, ask, "merge")).status_code == 200
        assert (await run_finished(client, ask["run_id"]))["result"] == {"option": "merge"}
        [paper] = (await listing(client, project))["materials"]
        assert paper["title"] == "Kept Title" and paper["csl"]["volume"] == "1" and paper["csl"]["issue"] == "4"
        assert paper["csl"]["issued"] == {"date-parts": [[2020]]} and paper["csl"]["author"] == [{"family": "Kept", "given": "Author"}]
        assert paper["csl"]["abstract"] == "Added abstract." and paper["evidence_type"] == "abstract"
        assert await held(client, ask["run_id"]) is None


async def test_merge_into_an_edited_paper_never_fills_its_form_fields(tmp_path):
    async with started(tmp_path / "data") as client:
        project = await project_of(client)
        first = await imported(client, project, ris(("Edited", f"DO  - {DOI}\n")))
        [paper] = first["imported"]
        cleared = await client.patch(f"/api/materials/{paper}", json={"authors": [], "year": None, "venue": ""})
        assert cleared.status_code == 200
        await imported(client, project, ris(("Edited", f"DO  - {DOI}\nAU  - New, Author\nPY  - 2001\nT2  - A Venue\n"
                                                       "AB  - Abstract.\n")))
        [ask] = await merge_asks(client, project)
        assert (await answer(client, ask, "merge")).status_code == 200
        await run_finished(client, ask["run_id"])
        [found] = (await listing(client, project))["materials"]
        assert "author" not in found["csl"] and "issued" not in found["csl"] and "container-title" not in found["csl"]
        assert found["csl"]["abstract"] == "Abstract." and found["checked_by"] == "researcher"


async def test_merge_into_a_paper_with_a_file_keeps_it_full_text(tmp_path):
    async with started(tmp_path / "data", scholarly(openalex={synthetic.DOI: openalex_work(synthetic.DOI, "Looked Up")})) as client:
        project = await project_of(client)
        await added(client, project, ("paper.pdf", synthetic.paper_pdf()))
        await settled(client, project)
        result = await imported(client, project, ris(("Looked Up", f"DO  - {synthetic.DOI}\nAB  - Record abstract.\n")))
        [ask] = await merge_asks(client, project)
        assert (await answer(client, ask, "merge")).status_code == 200
        await run_finished(client, result["waiting"][0]["run_id"])
        [paper] = (await listing(client, project))["materials"]
        assert paper["evidence_type"] == "full_text" and paper["csl"]["abstract"] == "Record abstract."
        assert paper["source"] == "upload"


async def test_keep_both_makes_the_record_its_own_paper_not_looked_up(tmp_path):
    async with started(tmp_path / "data", scholarly(openalex={DOI: openalex_work(DOI, "Resolved")})) as client:
        project = await project_of(client)
        await imported(client, project, ris(("Original", f"DO  - {DOI}\n")))
        await settled(client, project)
        requests = len(client.provider.scholarly.requests)
        await imported(client, project, ris(("Original", f"DO  - {DOI}\nAB  - Abstract.\n")))
        [ask] = await merge_asks(client, project)
        assert (await answer(client, ask, "keep_both")).status_code == 200
        await run_finished(client, ask["run_id"])
        await background_idle(client)
        kept = [m for m in (await listing(client, project))["materials"] if m["csl"].get("abstract")]
        assert len(kept) == 1 and kept[0]["source"] == "reference_file" and kept[0]["lookup"] is None
        assert len(client.provider.scholarly.requests) == requests


async def first_project(client):
    """The project listed first (after the general one): the one changed last."""
    return [p["id"] for p in (await client.get("/api/projects")).json()["projects"] if p["kind"] != "general"][0]


@pytest.mark.parametrize("option", ["merge", "keep_both"])
async def test_an_answer_that_changes_the_papers_puts_the_project_first(tmp_path, option):
    async with started(tmp_path / "data") as client:
        project = await project_of(client)
        _, ask = await with_duplicate(client, project)
        other = await project_of(client, "Changed since")
        assert await first_project(client) == other
        assert (await answer(client, ask, option)).status_code == 200
        await run_finished(client, ask["run_id"])
        assert await first_project(client) == project


async def test_a_merge_that_adds_nothing_leaves_the_projects_order(tmp_path):
    async with started(tmp_path / "data") as client:
        project = await project_of(client)
        paper, ask = await with_duplicate(client, project, extra="T2  - A Venue\n")
        # a venue given meanwhile: the merge has nothing left to add
        assert (await client.patch(f"/api/materials/{paper}", json={"venue": "Its Own Venue"})).status_code == 200
        other = await project_of(client, "Changed since")
        assert (await answer(client, ask, "merge")).status_code == 200
        assert (await run_finished(client, ask["run_id"]))["status"] == "succeeded"
        assert await first_project(client) == other


async def test_merge_runs_are_listed_with_their_paper_and_question_and_no_retry(tmp_path):
    async with started(tmp_path / "data") as client:
        project = await project_of(client)
        _, ask = await with_duplicate(client, project)
        [row] = [r for r in (await client.get("/api/activity")).json()["runs"] if r["run_id"] == ask["run_id"]]
        assert (row["workflow"], row["status"], row["materials"]["titles"]) == ("merge", "running", ["A Paper Already Here"])
        assert row["ask"]["ask_id"] == ask["ask_id"] and row["ask"]["params"]["record"]["title"] == "A Paper Already Here"
        assert row["retryable"] is False
        assert (await answer(client, row["ask"], "merge")).status_code == 200
        done = await run_finished(client, ask["run_id"])
        assert done["retryable"] is False and (await client.post(f"/api/runs/{ask['run_id']}/retry")).status_code == 409


# Lookups of imported papers


async def test_an_imported_papers_lookup_fills_only_what_its_record_lacks(tmp_path):
    record = {**openalex_work(DOI, "OpenAlex's Own Title", authors=("Someone Else",), year=1999, retracted=True)}
    async with started(tmp_path / "data", scholarly(openalex={DOI: record})) as client:
        project = await project_of(client)
        result = await imported(client, project, ris(("The Record's Title", f"DO  - {DOI}\nPY  - 2021\nAU  - Kept, Author\n")))
        run = await run_finished(client, result["lookup_run_id"])
        assert run["status"] == "succeeded"
        [paper] = (await listing(client, project))["materials"]
        assert paper["title"] == "The Record's Title" and paper["csl"]["author"] == [{"family": "Kept", "given": "Author"}]
        assert paper["csl"]["issued"] == {"date-parts": [[2021]]} and paper["csl"]["container-title"] == "Journal of Synthetic Studies"
        assert (paper["checked_by"], paper["source_key"]) == (None, "openalex:W0000000001") and paper["resolved_at"]
        assert paper["retraction"] == "retracted" and paper["retraction_checked_at"]
        assert paper["lookup"]["identifier"] == f"doi:{DOI}"
        [(_, path, headers)] = client.provider.scholarly.requests
        assert path == f"/works/doi:{DOI}" and "authorization" not in headers and "mailto" not in path


async def test_crossref_answers_an_imported_doi_and_arxiv_ids_from_bibtex_go_to_arxiv(tmp_path):
    crossref = {DOI: crossref_work(DOI, "Crossref Title")}
    async with started(tmp_path / "data", scholarly(crossref=crossref, arxiv={synthetic.ARXIV: "A Synthetic Preprint"})) as client:
        project = await project_of(client)
        await imported(client, project, ris(("From Crossref", f"DO  - {DOI}\n")))
        await imported(client, project, synthetic.references_bibtex(), "export.bib")
        found = {m["title"]: m for m in await settled(client, project)}
        assert found["From Crossref"]["source_key"] == f"doi:{DOI}"
        assert found["A Synthetic Preprint"]["source_key"] == f"arxiv:{synthetic.ARXIV}"
        hosts = client.provider.scholarly.hosts
        assert "export.arxiv.org" in hosts and "doi.org" not in hosts


async def test_a_failed_lookup_leaves_the_imported_paper_as_the_file_gave_it(tmp_path):
    async with started(tmp_path / "data") as client:
        project = await project_of(client)
        client.provider.scholarly.answers = {"api.openalex.org": [503] * 3, "api.crossref.org": [503] * 3}
        result = await imported(client, project, ris(("As Given", f"DO  - {DOI}\nVL  - 2\n")))
        run = await run_finished(client, result["lookup_run_id"])
        assert (run["status"], run["result"]) == ("failed", {"reason": "unavailable"})
        [paper] = (await listing(client, project))["materials"]
        assert paper["csl"] == {"type": "article-journal", "title": "As Given", "volume": "2", "DOI": DOI}
        assert (paper["source_key"], paper["resolved_at"], paper["checked_by"]) == (None, None, None)


async def test_a_local_only_import_asks_once_for_its_papers_naming_the_services_and_the_count(tmp_path):
    async with started(tmp_path / "data", scholarly(arxiv={synthetic.ARXIV: "A Synthetic Preprint"})) as client:
        project = await project_of(client, level="local_only")
        result = await imported(client, project, synthetic.references_ris() + b"\n")
        result_bib = await imported(client, project, synthetic.references_bibtex(), "export.bib")
        asked = await asks_of(client, project, 2)
        by_run = {a["run_id"]: a for a in asked}
        assert by_run[result["lookup_run_id"]]["params"] == {"services": ["crossref", "openalex"], "identifiers": 2,
                                                             "batch": "reference_file"}
        assert by_run[result_bib["lookup_run_id"]]["params"] == {
            "services": ["arxiv", "crossref", "openalex"], "identifiers": 2, "batch": "reference_file"}
        assert client.provider.scholarly.requests == []
        audit = await rows(client, "SELECT count(*) FROM audit_log WHERE event = 'outbound'")
        assert audit == [(0,)]


# Attaching a file later


async def test_a_file_attached_to_an_imported_paper_is_a_new_version_read_as_full_text(tmp_path):
    async with started(tmp_path / "data") as client:
        project = await project_of(client, level="local_only")  # its lookup asks, and is left waiting
        result = await imported(client, project, ris(("Attach To Me", "PY  - 2020\n")))
        [paper] = result["imported"]
        attached = await added(client, project, ("full.md", b"# Attach To Me\n\nThe full text of the paper.\n"),
                               material_id=paper)
        assert attached["materials"][0]["id"] == paper
        [found] = await settled(client, project)
        assert (found["evidence_type"], found["state"], found["source"]) == ("full_text", "ready", "reference_file")
        assert found["version"]["seq"] == 1 and found["version"]["media_type"] == "text/markdown"
        versions = (await client.get(f"/api/materials/{paper}/versions")).json()["versions"]
        assert [(v["seq"], v["is_current"], v["media_type"]) for v in versions] == [(0, False, None), (1, True, "text/markdown")]
        assert found["csl"]["issued"] == {"date-parts": [[2020]]}  # its record's details stay


@pytest.mark.parametrize("edited", [False, True])
async def test_a_file_attached_later_is_looked_up_by_the_records_doi_never_by_one_the_researcher_typed(tmp_path, edited):
    typed = "10.5555/typed.by.hand"
    records = {DOI: openalex_work(DOI, "Record's Work"), typed: openalex_work(typed, "Typed Work")}
    async with started(tmp_path / "data", scholarly(openalex=records)) as client:
        project = await project_of(client)
        mock = client.provider.scholarly
        result = await imported(client, project, ris(("Attach Later", f"DO  - {DOI}\n")))
        await run_finished(client, result["lookup_run_id"])
        [paper] = result["imported"]
        if edited:
            assert (await client.patch(f"/api/materials/{paper}", json={"doi": typed})).status_code == 200
        sent = len(mock.requests)
        attached = await added(client, project, ("attach.md", b"# Attach Later\n\nNo identifier in the text.\n"),
                               material_id=paper)
        await run_finished(client, attached["lookup_run_id"])
        [found] = await settled(client, project)
        paths = [path for _, path, _ in mock.requests[sent:]]
        if edited:  # the file's text gives none, and the typed DOI is never sent
            assert paths == [] and found["lookup"]["outcome"] == "no_identifier"
        else:  # the record's DOI comes first, whatever the attached file holds
            assert paths == [f"/works/doi:{DOI}"] and found["lookup"]["identifier"] == f"doi:{DOI}"


# From the writer's first review (2026-10-10)


async def test_half_a_surrogate_pair_in_a_csl_json_record_never_breaks_the_librarys_answers(tmp_path):
    async with started(tmp_path / "data") as client:
        project = await project_of(client)
        await imported(client, project, ris(("Paper Here", f"DO  - {DOI}\n")))
        text = json.dumps([{"title": "New \ud83d Paper", "abstract": "Cut \udc00"},
                           {"title": "Paper Here", "DOI": DOI, "abstract": "Half \ud800 pair"}])
        result = await imported(client, project, text.encode(), "export.json")
        assert len(result["imported"]) == 1 and len(result["waiting"]) == 1
        [ask] = await merge_asks(client, project)
        assert ask["params"]["record"]["title"] == "Paper Here"
        assert (await client.get("/api/activity")).status_code == 200 and (await client.get("/api/asks")).status_code == 200
        assert (await answer(client, ask, "keep_both")).status_code == 200
        assert (await run_finished(client, ask["run_id"]))["status"] == "succeeded"
        found = {m["csl"].get("abstract"): m for m in (await listing(client, project))["materials"]}
        assert {"Cut �", "Half � pair"} <= set(found)


async def test_a_doi_corrected_on_a_reference_files_paper_keeps_its_records_fields_and_drops_the_lookups(tmp_path):
    record = openalex_work(DOI, "Wrong Work")  # the lookup adds a venue, a volume and a page the record lacks
    async with started(tmp_path / "data", scholarly(openalex={DOI: record})) as client:
        project = await project_of(client)
        result = await imported(client, project, ris(("Corrected", f"DO  - {DOI}\nAB  - Its abstract.\nKW  - wages\n"
                                                                   "LA  - en\nIS  - 7\n")))
        await run_finished(client, result["lookup_run_id"])
        [paper] = result["imported"]
        before = (await client.get(f"/api/materials/{paper}")).json()["csl"]
        assert before["volume"] == "3" and before["container-title"] == "Journal of Synthetic Studies"
        moved = await client.patch(f"/api/materials/{paper}", json={"doi": "10.5555/the.right.doi"})
        assert moved.status_code == 200
        after = moved.json()
        assert after["csl"] == {"type": "article-journal", "title": "Corrected", "DOI": "10.5555/the.right.doi",
                                "abstract": "Its abstract.", "keyword": "wages", "language": "en", "issue": "7",
                                # the form's fields stay as saved, whoever gave them
                                "container-title": "Journal of Synthetic Studies", "author": [{"literal": "A. Researcher"}],
                                "issued": {"date-parts": [[2024]]}}
        assert "volume" not in after["csl"] and "page" not in after["csl"]  # the old DOI's lookup's, gone with it
        assert (after["evidence_type"], after["source_key"], after["resolved_at"]) == ("abstract", None, None)


async def test_a_doi_a_lookup_gave_an_arxiv_record_is_never_sent_as_the_records_own(tmp_path, monkeypatch):
    import scholia_app

    def with_a_doi(identifier, title):  # arXiv's answer, naming the work's DOI
        return scholia_app_feed(identifier, title).replace(b"</entry>", b"<arxiv:doi>10.5555/from.arxiv</arxiv:doi></entry>")

    scholia_app_feed = scholia_app.arxiv_feed
    monkeypatch.setattr(scholia_app, "arxiv_feed", with_a_doi)
    feed = MockScholarly(arxiv={"2401.05555": "A Preprint"})
    async with started(tmp_path / "data", MockProvider(scholarly=feed)) as client:
        project = await project_of(client)
        result = await imported(client, project, b"@misc{k, title={A Preprint}, eprint={2401.05555}, archivePrefix={arXiv}}",
                                "export.bib")
        await run_finished(client, result["lookup_run_id"])
        [paper] = result["imported"]
        assert (await client.get(f"/api/materials/{paper}")).json()["csl"]["DOI"] == "10.5555/from.arxiv"  # filled
        sent = len(feed.requests)
        attached = await added(client, project, ("p.md", b"# A Preprint\n\nText.\n"), material_id=paper)
        await run_finished(client, attached["lookup_run_id"])
        assert [(host, path) for host, path, _ in feed.requests[sent:]] == [
            ("export.arxiv.org", "/api/query?id_list=2401.05555&max_results=1")]  # the record's arXiv ID, not that DOI


async def test_a_lookup_fills_nothing_in_a_reference_files_paper_the_researcher_edited(tmp_path):
    async with started(tmp_path / "data", scholarly(openalex={DOI: openalex_work(DOI, "Resolved", retracted=True)})) as client:
        project = await project_of(client, level="local_only")  # its lookup waits on the question
        result = await imported(client, project, ris(("Edited First", f"DO  - {DOI}\n")))
        [ask] = await asks_of(client, project)
        [paper] = result["imported"]
        assert (await client.patch(f"/api/materials/{paper}", json={"title": "Edited First, by me"})).status_code == 200
        assert (await answer(client, ask, "lookup")).status_code == 200
        await run_finished(client, result["lookup_run_id"])
        found = (await client.get(f"/api/materials/{paper}")).json()
        assert found["csl"] == {"type": "article-journal", "title": "Edited First, by me", "DOI": DOI}
        assert (found["source_key"], found["resolved_at"], found["retraction"]) == (None, None, "retracted")


async def test_the_same_record_imported_again_while_its_merge_waits_asks_once(tmp_path):
    async with started(tmp_path / "data") as client:
        project = await project_of(client)
        _, ask = await with_duplicate(client, project)
        again = await imported(client, project, ris(("A Paper Already Here", f"DO  - {DOI}\nPY  - 2020\n"
                                                                             "AB  - The record's abstract.\n")))
        assert again["waiting"] == [{"run_id": ask["run_id"], "material_id": again["waiting"][0]["material_id"]}]
        assert await rows(client, "SELECT count(*) FROM runs WHERE workflow = 'merge'") == [(1,)]


async def test_the_library_looks_again_while_a_merge_applies_its_answer(tmp_path, monkeypatch):
    monkeypatch.setattr(references, "POLICY_SECONDS", 60)
    async with started(tmp_path / "data") as client:
        project = await project_of(client)
        _, ask = await with_duplicate(client, project)
        assert (await listing(client, project))["merging"] == 0  # it waits on its question, which is listed
        registry = client.state["harness"].registry
        await asyncio.to_thread(client.state["db"].write, lambda conn: asks.answer(
            conn, ask["run_id"], ask["ask_id"], "keep_both", registry=registry))
        assert (await listing(client, project))["merging"] == 1  # answered, its effect still to come
        registry.runs[ask["run_id"]].answered.set()
        await run_finished(client, ask["run_id"])
        assert (await listing(client, project))["merging"] == 0


async def test_the_library_looks_again_while_a_merge_waits_to_ask_again_under_a_changed_policy(tmp_path, monkeypatch):
    monkeypatch.setattr(references, "POLICY_SECONDS", 60)  # it asks again only once it next looks, not during the check
    async with started(tmp_path / "data") as client:
        project = await project_of(client)
        _, ask = await with_duplicate(client, project)
        assert (await client.post(f"/api/projects/{project}/review-lock", json={"locked": True})).status_code == 200
        found = await listing(client, project)
        assert found["asks"] == [] and found["merging"] == 1  # its question no longer stands, the next not asked yet
        client.state["harness"].registry.runs[ask["run_id"]].answered.set()  # it looks again
        [again] = await merge_asks(client, project)
        assert again["ask_id"] != ask["ask_id"] and (await listing(client, project))["merging"] == 0


async def test_a_reference_file_is_read_in_one_of_the_readings_turns(tmp_path):
    async with started(tmp_path / "data") as client:
        project = await project_of(client)
        turns = client.state["harness"].readings
        held = [await turns.acquire() for _ in range(materials_module.READINGS)]
        importing = asyncio.create_task(post(client, project, synthetic.references_ris()))
        await asyncio.sleep(0.5)
        assert not importing.done()  # waits for a turn
        turns.release()
        assert (await importing).status_code == 201
        for _ in held[1:]:
            turns.release()


async def test_an_import_waits_a_bounded_time_for_a_readings_turn_and_is_refused_after(tmp_path, monkeypatch):
    monkeypatch.setattr(references, "TURN_SECONDS", 0.3)
    async with started(tmp_path / "data") as client:
        project = await project_of(client)
        turns = client.state["harness"].readings
        for _ in range(materials_module.READINGS):
            await turns.acquire()
        response = await post(client, project, synthetic.references_ris())
        assert (response.status_code, response.json()["code"]) == (409, "readings_busy")
        assert await rows(client, "SELECT count(*) FROM materials") == [(0,)]
        for _ in range(materials_module.READINGS):
            turns.release()
        assert (await post(client, project, synthetic.references_ris())).status_code == 201
        assert turns._value == materials_module.READINGS  # every turn back, none kept by the refusal or the import


async def test_a_second_doi_change_keeps_a_field_a_merged_record_gave(tmp_path):
    other = "10.5555/the.second.doi"
    async with started(tmp_path / "data", scholarly(openalex={DOI: openalex_work(DOI, "First Work")})) as client:
        project = await project_of(client)
        result = await imported(client, project, ris(("Twice Moved", f"DO  - {DOI}\n")))
        await run_finished(client, result["lookup_run_id"])
        [paper] = result["imported"]
        assert (await client.patch(f"/api/materials/{paper}", json={"doi": other})).json()["csl"].get("volume") is None
        await imported(client, project, ris(("Twice Moved", f"DO  - {other}\nVL  - 9\n")))
        [ask] = await merge_asks(client, project)
        assert (await answer(client, ask, "merge")).status_code == 200
        await run_finished(client, ask["run_id"])
        moved = await client.patch(f"/api/materials/{paper}", json={"doi": "10.5555/the.third.doi"})
        assert moved.json()["csl"]["volume"] == "9"  # the record's, not the first DOI's lookup's


async def test_twin_records_of_a_paper_in_one_file_are_one_waiting_question(tmp_path):
    async with started(tmp_path / "data") as client:
        project = await project_of(client)
        await imported(client, project, ris(("Here Already", f"DO  - {DOI}\n")))
        twin = ("Here Already", f"DO  - {DOI}\nAB  - Twice.\n")
        result = await imported(client, project, ris(twin, twin, twin))
        assert len(result["waiting"]) == 1 and result["imported"] == []
        assert await rows(client, "SELECT count(*) FROM runs WHERE workflow = 'merge'") == [(1,)]


async def test_a_doi_change_drops_what_the_lookup_that_gave_the_doi_filled(tmp_path, monkeypatch):
    import scholia_app

    def with_a_doi(identifier, title):
        return scholia_app_feed(identifier, title).replace(b"</entry>", b"<arxiv:doi>10.5555/from.arxiv</arxiv:doi></entry>")

    scholia_app_feed = scholia_app.arxiv_feed
    monkeypatch.setattr(scholia_app, "arxiv_feed", with_a_doi)
    async with started(tmp_path / "data", MockProvider(scholarly=MockScholarly(arxiv={"2401.05555": "A Preprint"}))) as client:
        project = await project_of(client)
        result = await imported(client, project, b"@misc{k, title={A Preprint}, eprint={2401.05555}, archivePrefix={arXiv},"
                                                  b" note={kept}, volume={2}}", "export.bib")
        await run_finished(client, result["lookup_run_id"])
        [paper] = result["imported"]
        filled = (await client.get(f"/api/materials/{paper}")).json()["csl"]
        assert filled["DOI"] == "10.5555/from.arxiv" and filled["publisher"] == "arXiv"  # both the arXiv lookup's
        moved = (await client.patch(f"/api/materials/{paper}", json={"doi": "10.5555/the.right.doi"})).json()["csl"]
        assert "publisher" not in moved and moved["volume"] == "2" and moved["arxiv"] == "2401.05555"


async def test_a_doi_changed_twice_keeps_a_field_a_merge_gave_after_the_first(tmp_path, monkeypatch):
    import scholia_app

    def with_a_doi(identifier, title):
        return scholia_app_feed(identifier, title).replace(b"</entry>", b"<arxiv:doi>10.5555/from.arxiv</arxiv:doi></entry>")

    scholia_app_feed = scholia_app.arxiv_feed
    monkeypatch.setattr(scholia_app, "arxiv_feed", with_a_doi)
    async with started(tmp_path / "data", MockProvider(scholarly=MockScholarly(arxiv={"2401.05555": "A Preprint"}))) as client:
        project = await project_of(client)
        result = await imported(client, project, b"@misc{k, title={A Preprint}, eprint={2401.05555}, archivePrefix={arXiv}}",
                                "export.bib")
        await run_finished(client, result["lookup_run_id"])
        [paper] = result["imported"]
        first = (await client.patch(f"/api/materials/{paper}", json={"doi": "10.5555/second"})).json()["csl"]
        assert "publisher" not in first  # the arXiv lookup's, gone with the DOI it gave
        await imported(client, project, ris(("A Preprint", "DO  - 10.5555/second\nPB  - Merged Press\n")))
        [ask] = await merge_asks(client, project)
        assert (await answer(client, ask, "merge")).status_code == 200
        await run_finished(client, ask["run_id"])
        second = (await client.patch(f"/api/materials/{paper}", json={"doi": "10.5555/third"})).json()["csl"]
        assert second["publisher"] == "Merged Press"  # the merged record's own


# Finding 1's structural fix (Harold, 2026-10-15): the paper records the fields a lookup filled


async def lookup_filled(client, paper):
    [(filled,)] = await rows(client, "SELECT lookup_filled FROM materials WHERE id = ?", paper)
    return json.loads(filled) if filled else None


async def test_returning_to_an_earlier_doi_never_drops_a_field_a_merge_gave(tmp_path):
    # review 1's finding 1: lookup of A fills volume 3; to B; a merge gives volume 9; back to A, then to C
    async with started(tmp_path / "data", scholarly(openalex={DOI: openalex_work(DOI, "First Work")})) as client:
        project = await project_of(client)
        result = await imported(client, project, ris(("Moved About", f"DO  - {DOI}\n")))
        await run_finished(client, result["lookup_run_id"])
        [paper] = result["imported"]
        assert {"volume", "page"} <= set(await lookup_filled(client, paper))  # written with the fill
        other = "10.5555/the.b.doi"
        assert (await client.patch(f"/api/materials/{paper}", json={"doi": other})).json()["csl"].get("volume") is None
        assert await lookup_filled(client, paper) is None  # dropped with them, and cleared
        await imported(client, project, ris(("Moved About", f"DO  - {other}\nVL  - 9\n")))
        [ask] = await merge_asks(client, project)
        assert (await answer(client, ask, "merge")).status_code == 200
        await run_finished(client, ask["run_id"])
        assert await lookup_filled(client, paper) is None  # a merge never adds to it
        assert (await client.patch(f"/api/materials/{paper}", json={"doi": DOI})).json()["csl"]["volume"] == "9"
        moved = await client.patch(f"/api/materials/{paper}", json={"doi": "10.5555/the.c.doi"})
        assert moved.json()["csl"]["volume"] == "9"


async def test_a_doi_change_that_retitles_a_paper_reindexes_its_passages_by_the_new_title(tmp_path):
    from test_search import app, idle  # S1-17's index with its stand-in helper

    async with app(tmp_path) as client:
        project = await project_of(client)
        [paper] = (await imported(client, project, ris(("Quixotic Ledger", f"DO  - {DOI}\n"))))["imported"]
        await added(client, project, ("r.md", b"# Plain Heading\n\nPlain text with nothing special.\n"), material_id=paper)
        await idle(client, project)
        keyword = client.state["index"].keyword
        assert len(await asyncio.to_thread(keyword, project, "quixotic", 50)) > 0  # its passages, by their title
        moved = await client.patch(f"/api/materials/{paper}", json={"doi": "10.5555/new.doi", "title": "Zephyr Accounts"})
        assert moved.status_code == 200 and moved.json()["csl"]["DOI"] == "10.5555/new.doi"
        await idle(client, project)
        assert len(await asyncio.to_thread(keyword, project, "zephyr", 50)) > 0  # indexed again under the new title
        assert await asyncio.to_thread(keyword, project, "quixotic", 50) == []  # and no longer under the old


async def test_each_lookups_fill_joins_the_fields_a_lookup_filled(tmp_path):
    from types import SimpleNamespace

    async with started(tmp_path / "data") as client:
        project = await project_of(client)
        [paper] = (await imported(client, project, ris(("Filled Twice", "")), "a.ris"))["imported"]

        def fill(csl):
            return lambda conn: materials_module._fill_imported(conn, paper, SimpleNamespace(csl=csl, source_key="openalex:W1"))

        assert await asyncio.to_thread(client.state["db"].write, fill({"volume": "3"})) == ["volume"]
        assert await asyncio.to_thread(client.state["db"].write, fill({"volume": "4", "page": "5"})) == ["page"]
        assert await lookup_filled(client, paper) == ["page", "volume"]  # the first fill's stays with the second's


# The S1-14 follow-up (the connector's threads on PR 28)


async def test_an_empty_reference_file_holds_no_records(tmp_path):
    async with started(tmp_path / "data") as client:
        project = await project_of(client)
        response = await post(client, project, b"")
        assert (response.status_code, response.json()["code"]) == (400, "no_records")  # the parser's, not the request's


async def test_base64_longer_than_the_largest_file_read_is_refused_before_it_is_decoded(tmp_path, monkeypatch):
    async with started(tmp_path / "data") as client:
        project = await project_of(client)
        calls = []
        real = base64.b64decode
        monkeypatch.setattr(references.base64, "b64decode", lambda data, **kw: calls.append(len(data)) or real(data, **kw))
        data = "A" * (references._ENCODED + 4)  # valid base64, a little more than MAX_BYTES once decoded
        response = await client.post(f"/api/projects/{project}/materials/import-references",
                                     json={"name": "big.ris", "data": data})
        assert (response.status_code, response.json()["code"]) == (413, "reference_file_too_large")
        assert calls == []  # never decoded in the app's process
        within = base64.b64encode(b"x" * reference_files.MAX_BYTES).decode()
        assert len(within) <= references._ENCODED  # the largest file read still reaches the parser


async def test_a_merge_that_gives_a_paper_its_doi_looks_it_up(tmp_path):
    async with started(tmp_path / "data", scholarly(openalex={DOI: openalex_work(DOI, "Its Work", retracted=True)})) as client:
        project = await project_of(client)
        [paper] = (await imported(client, project, ris(("Without A DOI", "PY  - 2020\n"))))["imported"]
        second = await imported(client, project, ris(("Without A DOI", f"PY  - 2020\nDO  - {DOI}\n")))
        assert second["lookup_run_id"] is None and second["imported"] == []
        [ask] = await merge_asks(client, project)
        assert (await answer(client, ask, "merge")).status_code == 200
        await run_finished(client, ask["run_id"])
        [(run,)] = await rows(client, "SELECT id FROM runs WHERE workflow = 'lookup' AND project_id = ?", project)
        assert (await run_finished(client, run))["status"] == "succeeded"
        found = (await client.get(f"/api/materials/{paper}")).json()
        assert (found["csl"]["DOI"], found["retraction"]) == (DOI, "retracted")  # looked up and checked


@pytest.mark.parametrize("protection", ["lock", "local_only"])
async def test_a_merges_lookup_keeps_the_projects_protection(tmp_path, protection):
    async with started(tmp_path / "data", scholarly(openalex={DOI: openalex_work(DOI, "Its Work")})) as client:
        project = await project_of(client, level="local_only" if protection == "local_only" else "normal")
        await imported(client, project, ris(("Without A DOI", "PY  - 2020\n")))
        if protection == "lock":
            assert (await client.post(f"/api/projects/{project}/review-lock", json={"locked": True})).status_code == 200
        await imported(client, project, ris(("Without A DOI", f"PY  - 2020\nDO  - {DOI}\n")))
        [ask] = await merge_asks(client, project)
        sent = len(client.provider.scholarly.requests)
        assert (await answer(client, ask, "merge")).status_code == 200
        await run_finished(client, ask["run_id"])
        lookups = await rows(client, "SELECT id FROM runs WHERE workflow = 'lookup' AND project_id = ?", project)
        if protection == "lock":
            assert lookups == []  # a review-locked project never looks one up
        else:
            [question] = await asks_of(client, project, kind="identifier_lookup")  # asked first, as the import's
            assert question["params"].get("batch") == "reference_file"
        assert len(client.provider.scholarly.requests) == sent


async def test_a_merge_whose_paper_changed_meanwhile_applies_nothing(tmp_path):
    async with started(tmp_path / "data") as client:
        project = await project_of(client)
        paper, ask = await with_duplicate(client, project)  # a record of the paper's DOI, with an abstract
        moved = await client.patch(f"/api/materials/{paper}", json={"doi": "10.5555/another.work"})
        assert moved.status_code == 200
        [ask] = await merge_asks(client, project)  # its question stands (asked under the same policy)
        assert (await answer(client, ask, "merge")).status_code == 200
        run = await run_finished(client, ask["run_id"])
        assert (run["status"], run["result"]) == ("failed", {"reason": "no_longer_duplicate"})
        assert "abstract" not in (await client.get(f"/api/materials/{paper}")).json()["csl"]


async def test_the_merges_recheck_is_the_duplicate_checks_own_rule(tmp_path):
    def paper(title, year=None, doi=None):
        return {**({"title": title} if title else {}), **({"issued": {"date-parts": [[year]]}} if year else {}),
                **({"DOI": doi} if doi else {})}

    records = [paper("Wages"), paper("Wages", 2020), paper("WAGES!", 2020), paper("Wages", 2021), paper(None, 2020),
               paper("Other", 2020, DOI), paper("Wages", 2020, DOI), paper("Wages", 2020, DOI.upper()),
               paper("Wages", 2020, "10.5555/other")]
    async with started(tmp_path / "data") as client:
        project = await project_of(client)

        def check(conn):  # each paper alone in the project, as Duplicates reads it from the database
            for existing in records:
                conn.execute("DELETE FROM material_versions")
                conn.execute("DELETE FROM materials")
                references.new_material(conn, project, {"title": "x", **existing})
                conn.execute("UPDATE materials SET csl = ?", (json.dumps(existing),))
                for record in records:
                    found = references.Duplicates(conn, project).find(record) is not None
                    assert references._matches(existing, record) == found, (existing, record)
            raise LookupError  # roll it all back

        with pytest.raises(LookupError):
            await asyncio.to_thread(client.state["db"].write, check)


async def test_a_merge_into_a_paper_not_from_a_reference_file_looks_nothing_up(tmp_path):
    from types import SimpleNamespace

    async with started(tmp_path / "data") as client:
        project = await project_of(client)
        [paper] = (await imported(client, project, ris(("From A File", "PY  - 2020\n"))))["imported"]
        record = {"type": "article", "title": "From A File", "issued": {"date-parts": [[2020]]}, "DOI": DOI}
        active = SimpleNamespace(follows=[])

        def merge(conn):
            conn.execute("UPDATE materials SET source = 'upload' WHERE id = ?", (paper,))  # as an upload's paper is
            references._answered(conn, project, paper, record, "merge", client.state["harness"], active)

        await asyncio.to_thread(client.state["db"].write, merge)
        assert active.follows == [] and await rows(client, "SELECT count(*) FROM runs WHERE workflow = 'lookup'") == [(0,)]
        assert (await client.get(f"/api/materials/{paper}")).json()["csl"]["DOI"] == DOI  # merged, as details


async def test_a_merge_that_gives_a_paper_its_arxiv_id_looks_it_up(tmp_path):
    feed = MockScholarly(arxiv={"2401.05555": "A Preprint"})
    async with started(tmp_path / "data", MockProvider(scholarly=feed)) as client:
        project = await project_of(client)
        await imported(client, project, b"@misc{k, title={A Preprint}, year={2024}}", "a.bib")
        await imported(client, project, b"@misc{k, title={A Preprint}, year={2024}, eprint={2401.05555},"
                                        b" archivePrefix={arXiv}}", "b.bib")
        [ask] = await merge_asks(client, project)
        assert (await answer(client, ask, "merge")).status_code == 200
        await run_finished(client, ask["run_id"])
        [(run,)] = await rows(client, "SELECT id FROM runs WHERE workflow = 'lookup' AND project_id = ?", project)
        assert (await run_finished(client, run))["status"] == "succeeded"
        assert [(host, path) for host, path, _ in feed.requests] == [
            ("export.arxiv.org", "/api/query?id_list=2401.05555&max_results=1")]


@pytest.mark.parametrize("case", ["had_a_doi", "edited"])
async def test_a_merge_that_leaves_the_lookups_identifier_as_it_was_looks_nothing_up(tmp_path, case):
    # recheck 2 of PR 35: an arXiv ID added to a paper with a DOI leaves the DOI the lookup's identifier, looked
    # up already: nothing is asked again; and the researcher's details are never looked up
    feed = MockScholarly(arxiv={"2401.05555": "A Preprint"}, openalex={DOI: openalex_work(DOI, "A Preprint")})
    async with started(tmp_path / "data", MockProvider(scholarly=feed)) as client:
        project = await project_of(client)
        doi = f", doi={{{DOI}}}" if case == "had_a_doi" else ""
        first = await imported(client, project, f"@misc{{k, title={{A Preprint}}, year={{2024}}{doi}}}".encode(), "a.bib")
        [paper] = first["imported"]
        if first["lookup_run_id"]:
            await run_finished(client, first["lookup_run_id"])
        if case == "edited":
            assert (await client.patch(f"/api/materials/{paper}", json={"title": "A Preprint"})).status_code == 200
        before = await rows(client, "SELECT count(*) FROM runs WHERE workflow = 'lookup' AND project_id = ?", project)
        sent = len(feed.requests)
        await imported(client, project, f"@misc{{k, title={{A Preprint}}, year={{2024}}, eprint={{2401.05555}},"
                                        f" archivePrefix={{arXiv}}{doi}}}".encode(), "b.bib")
        [ask] = await merge_asks(client, project)
        assert (await answer(client, ask, "merge")).status_code == 200
        assert (await run_finished(client, ask["run_id"]))["status"] == "succeeded"
        assert "arxiv" in (await client.get(f"/api/materials/{paper}")).json()["csl"]  # merged, as its details
        await background_idle(client)
        assert await rows(client, "SELECT count(*) FROM runs WHERE workflow = 'lookup' AND project_id = ?",
                          project) == before
        assert feed.requests[sent:] == []  # neither the old DOI again nor the arXiv ID


@pytest.mark.parametrize("given, path, services", [
    (b"eprint={2401.05555}, archivePrefix={arXiv}", "/api/query?id_list=2401.05555&max_results=1", ["arxiv"]),
    (f"doi={{{DOI}}}".encode(), f"/works/doi:{DOI}", ["crossref", "openalex"]),
])
async def test_a_merge_giving_a_paper_with_no_identifier_one_asks_for_that_one(tmp_path, given, path, services):
    feed = MockScholarly(arxiv={"2401.05555": "A Preprint"}, openalex={DOI: openalex_work(DOI, "A Preprint")})
    async with started(tmp_path / "data", MockProvider(scholarly=feed)) as client:
        project = await project_of(client, level="local_only")
        await imported(client, project, b"@misc{k, title={A Preprint}, year={2024}}", "a.bib")
        await imported(client, project, b"@misc{k, title={A Preprint}, year={2024}, " + given + b"}", "b.bib")
        [ask] = await merge_asks(client, project)
        assert (await answer(client, ask, "merge")).status_code == 200
        await run_finished(client, ask["run_id"])
        [question] = await asks_of(client, project, kind="identifier_lookup")
        assert question["params"]["services"] == services  # Local only names the services this identifier goes to
        assert (await answer(client, question, "lookup")).status_code == 200
        await run_finished(client, question["run_id"])
        assert [p for _, p, _ in feed.requests] == [path]


async def test_a_merge_that_gives_an_arxiv_papers_its_doi_looks_the_doi_up(tmp_path):
    # review 1 of PR 35: the paper had an arXiv ID (looked up); the DOI a merge adds changes the identifier its lookup takes
    feed = MockScholarly(arxiv={"2401.05555": "A Preprint"}, openalex={DOI: openalex_work(DOI, "A Preprint", retracted=True)})
    async with started(tmp_path / "data", MockProvider(scholarly=feed)) as client:
        project = await project_of(client)
        first = await imported(client, project, b"@misc{k, title={A Preprint}, year={2024}, eprint={2401.05555},"
                                                b" archivePrefix={arXiv}}", "a.bib")
        [paper] = first["imported"]
        await run_finished(client, first["lookup_run_id"])
        await imported(client, project, ris(("A Preprint", f"PY  - 2024\nDO  - {DOI}\n")))
        [ask] = await merge_asks(client, project)
        assert (await answer(client, ask, "merge")).status_code == 200
        await run_finished(client, ask["run_id"])
        runs = await rows(client, "SELECT id FROM runs WHERE workflow = 'lookup' AND project_id = ? ORDER BY started_at",
                          project)
        assert len(runs) == 2 and (await run_finished(client, runs[1][0]))["status"] == "succeeded"
        assert [p for _, p, _ in feed.requests] == [  # the arXiv ID at import, then the DOI the merge gave
            "/api/query?id_list=2401.05555&max_results=1", f"/works/doi:{DOI}"]
        found = (await client.get(f"/api/materials/{paper}")).json()
        assert (found["csl"]["DOI"], found["retraction"]) == (DOI, "retracted")
