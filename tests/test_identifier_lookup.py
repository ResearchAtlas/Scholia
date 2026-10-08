"""Identifier lookups (slice-1 spec F3a step 3, sections 7.5, 10 and 13; ticket 71), with test-owned
OpenAlex, Crossref and arXiv stand-ins behind the outbound gate's mock transport and synthetic files.
Lifecycle cases first: a change of level, a lock or a deletion between a lookup's start and its
dispatch."""

import asyncio
import contextlib
import gzip
import json

import httpx
import pytest

import backend.lookup as lookup
from backend.db import new_id
import backend.materials as materials_module
import synthetic_materials as synthetic
from scholia_app import (Chunks, MockProvider, MockScholarly, arxiv_feed, background_idle, crossref_work, openalex_work,
                         run_finished, started, streamed)
from test_materials import added, hold_extraction, listing, project_of, rows, settled

pytestmark = pytest.mark.asyncio

DOI = synthetic.DOI
DEFAULTS = (dict(lookup.SPACING), lookup.RETRIES, lookup.TIMEOUT)  # as the module sets them, before the fixture
TITLE = "A Synthetic Study of Minimum Wages, Resolved"


@pytest.fixture(autouse=True)
def quick(monkeypatch):
    monkeypatch.setattr(lookup, "SPACING", {source: 0.0 for source in lookup.SPACING})
    monkeypatch.setattr(lookup, "RETRIES", (0.01, 0.02))
    monkeypatch.setattr(materials_module, "WAIT_SECONDS", 0.01)


def scholarly(**records):
    return MockProvider(scholarly=MockScholarly(**records))


async def gate_log(client):
    return [json.loads(data) for (data,) in await rows(
        client, "SELECT data FROM audit_log WHERE event = 'outbound' AND data ->> 'kind' = 'scholarly_api' ORDER BY seq")]


async def ask_of(client, project):
    deadline = asyncio.get_running_loop().time() + 10
    while not (asks := (await listing(client, project))["asks"]):
        assert asyncio.get_running_loop().time() < deadline
        await asyncio.sleep(0.02)
    return asks


# Lifecycle


async def test_a_project_made_local_only_before_dispatch_sends_nothing(tmp_path, monkeypatch):
    reached, go = hold_extraction(monkeypatch)  # the lookup waits for its material to be read
    async with started(tmp_path / "data", scholarly(openalex={DOI: openalex_work(DOI, TITLE)})) as client:
        project = await project_of(client)
        result = await added(client, project, ("paper.pdf", synthetic.paper_pdf()))
        await asyncio.to_thread(reached.wait, 10)
        tightened = await client.post(f"/api/projects/{project}/sensitivity", json={"level": "local_only"})
        assert tightened.status_code == 200
        go.set()
        run = await run_finished(client, result["lookup_run_id"])
        assert (run["status"], run["cancel_reason"]) == ("cancelled", "revoked")
        await background_idle(client)
        assert client.provider.scholarly.requests == [] and await gate_log(client) == []
        [paper] = await settled(client, project)
        assert paper["checked_by"] is None and paper["title"] == "paper"  # left as it was


@pytest.mark.parametrize("change", ["lock", "delete the material", "delete the project", "made private"])
async def test_a_change_while_a_lookup_is_in_flight_sends_nothing_more_and_writes_nothing(tmp_path, change):
    records = {DOI: openalex_work(DOI, TITLE), "10.5555/second.paper": openalex_work("10.5555/second.paper", "Second")}
    async with started(tmp_path / "data", scholarly(openalex=records)) as client:
        project = await project_of(client)
        mock = client.provider.scholarly
        mock.hold = asyncio.Event()
        result = await added(client, project, ("paper.pdf", synthetic.paper_pdf()),
                             ("second.md", f"# Second\n\ndoi:10.5555/second.paper\n".encode()))
        await asyncio.wait_for(mock.started.wait(), 10)
        first = result["materials"][0]["id"]
        if change == "lock":
            response = await client.post(f"/api/projects/{project}/review-lock", json={"locked": True})
        elif change == "delete the material":
            response = await client.delete(f"/api/materials/{first}")
        elif change == "delete the project":
            response = await client.delete(f"/api/projects/{project}")
        else:
            response = await client.post(f"/api/projects/{project}/sensitivity", json={"level": "private"})
        assert response.status_code == 200, response.text
        mock.hold.set()
        await background_idle(client)
        assert len(mock.requests) == 1  # the one in flight; nothing after the change
        if change != "delete the project":
            assert await rows(client, "SELECT status, cancel_reason FROM runs WHERE id = ?", result["lookup_run_id"]) == [
                ("cancelled", "revoked")]
            assert await rows(client, "SELECT count(*) FROM materials WHERE checked_by = 'lookup'") == [(0,)]


async def test_a_local_only_projects_ask_is_withdrawn_when_it_no_longer_needs_one(tmp_path):
    async with started(tmp_path / "data", scholarly(openalex={DOI: openalex_work(DOI, TITLE)})) as client:
        project = await project_of(client, level="local_only")
        result = await added(client, project, ("paper.pdf", synthetic.paper_pdf()))
        [ask] = await ask_of(client, project)
        token = (await client.post(f"/api/projects/{project}/sensitivity", json={"level": "normal"})).json()["token"]
        assert (await client.post(f"/api/projects/{project}/sensitivity",
                                  json={"level": "normal", "token": token})).status_code == 200
        run = await run_finished(client, result["lookup_run_id"])
        assert run["status"] == "succeeded"
        late = await client.post(f"/api/runs/{ask['run_id']}/asks/{ask['ask_id']}", json={"option": "lookup"})
        assert late.status_code == 409  # closed: the project changed
        [paper] = await settled(client, project)
        assert paper["title"] == TITLE and paper["checked_by"] == "lookup"
        assert [entry["approved"] for entry in await gate_log(client)] == [False]  # Normal needs no approval


async def test_a_lookup_resumed_after_a_restart_keeps_its_answer(tmp_path, monkeypatch):
    data = tmp_path / "data"
    async with started(data, scholarly(openalex={DOI: openalex_work(DOI, TITLE)})) as client:
        project = await project_of(client, level="local_only")
        mock = client.provider.scholarly
        mock.hold = asyncio.Event()
        result = await added(client, project, ("paper.pdf", synthetic.paper_pdf()))
        [ask] = await ask_of(client, project)
        assert (await client.post(f"/api/runs/{ask['run_id']}/asks/{ask['ask_id']}",
                                  json={"option": "lookup"})).status_code == 200
        await asyncio.wait_for(mock.started.wait(), 10)
    async with started(data, scholarly(openalex={DOI: openalex_work(DOI, TITLE)}), setup=False) as client:
        assert (await run_finished(client, result["lookup_run_id"]))["status"] == "succeeded"
        assert (await listing(client, project))["asks"] == []  # not asked again
        [paper] = await settled(client, project)
        assert paper["title"] == TITLE


async def test_a_restarted_lookup_whose_identifiers_changed_asks_again(tmp_path):
    data, other = tmp_path / "data", "10.5555/read.again"
    records = {DOI: openalex_work(DOI, TITLE), other: openalex_work(other, "As Read Again")}
    async with started(data, scholarly(openalex=records)) as client:
        project = await project_of(client, level="local_only")
        mock = client.provider.scholarly
        mock.hold = asyncio.Event()
        result = await added(client, project, ("paper.pdf", synthetic.paper_pdf()))
        [ask] = await ask_of(client, project)
        assert (await client.post(f"/api/runs/{ask['run_id']}/asks/{ask['ask_id']}",
                                  json={"option": "lookup"})).status_code == 200  # for the PDF's DOI alone
        await asyncio.wait_for(mock.started.wait(), 10)
        version = result["materials"][0]["version_id"]

        def read_again(conn):  # the same version read again (a newer extractor, say), giving another DOI
            (sha,) = conn.execute("SELECT file_sha256 FROM material_versions WHERE id = ?", (version,)).fetchone()
            extraction_id = new_id()
            conn.execute("INSERT INTO extractions (id, file_sha256, extractor, extractor_version, status, pages,"
                         " ocr_pages) VALUES (?, ?, 'pdf', 'pdf-later', 'complete', 1, 0)", (extraction_id, sha))
            conn.execute("INSERT INTO passages (id, extraction_id, ordinal, page, kind, text) VALUES (?, ?, 0, 1,"
                         " 'paragraph', ?)", (new_id(), extraction_id, f"doi:{other}"))

        await asyncio.to_thread(client.state["db"].write, read_again)
    first = result["lookup_run_id"]
    async with started(data, scholarly(openalex=records), setup=False) as client:
        mock = client.provider.scholarly
        deadline = asyncio.get_running_loop().time() + 10
        while not (asks := (await listing(client, project))["asks"]):
            assert asyncio.get_running_loop().time() < deadline
            await asyncio.sleep(0.02)
        [again] = asks
        assert again["run_id"] == first and again["ask_id"] != ask["ask_id"] and again["params"]["identifiers"] == 1
        assert mock.requests == []  # the earlier answer covered the earlier DOI only: nothing went out
        assert (await client.post(f"/api/runs/{first}/asks/{ask['ask_id']}", json={"option": "lookup"})).status_code == 404
        assert (await client.post(f"/api/runs/{first}/asks/{again['ask_id']}", json={"option": "lookup"})).status_code == 200
        assert (await run_finished(client, first))["status"] == "succeeded"
        assert [path for _, path, _ in mock.requests] == [f"/works/doi:{other}"]


async def test_a_lookup_is_for_the_version_it_was_made_for_and_a_replaced_ones_ask_closes(tmp_path):
    other = "10.5555/replacement.version"
    records = {DOI: openalex_work(DOI, TITLE), other: openalex_work(other, "The Replacement")}
    async with started(tmp_path / "data", scholarly(openalex=records)) as client:
        project = await project_of(client, level="local_only")
        result = await added(client, project, ("paper.pdf", synthetic.paper_pdf()))
        [first] = await ask_of(client, project)  # version A's, left unanswered
        material = result["materials"][0]["id"]
        replaced = await added(client, project, ("v2.md", f"# Version two\n\ndoi:{other}\n".encode()), material_id=material)
        deadline = asyncio.get_running_loop().time() + 10
        while [a["run_id"] for a in (await listing(client, project))["asks"]] != [replaced["lookup_run_id"]]:
            assert asyncio.get_running_loop().time() < deadline  # A's ask is closed, B's is open
            await asyncio.sleep(0.02)
        [second] = (await listing(client, project))["asks"]
        assert (await client.post(f"/api/runs/{second['run_id']}/asks/{second['ask_id']}",
                                  json={"option": "lookup"})).status_code == 200
        assert (await run_finished(client, replaced["lookup_run_id"]))["status"] == "succeeded"
        late = await client.post(f"/api/runs/{first['run_id']}/asks/{first['ask_id']}", json={"option": "lookup"})
        assert (late.status_code, late.json()["code"]) == (409, "ask_closed")  # A approved too late: refused
        assert (await run_finished(client, result["lookup_run_id"]))["status"] == "succeeded"
        [paper] = await settled(client, project)
        assert (paper["title"], paper["source_key"]) == ("The Replacement", f"doi:{other}")
        assert [path for _, path, _ in client.provider.scholarly.requests] == [f"/works/doi:{other}"]  # A's: never sent


async def test_a_file_replaced_while_its_identifier_waits_for_its_turn_keeps_it_from_being_sent(tmp_path, monkeypatch):
    class Watched(lookup.Pace):  # says when a request waits for a source another request holds
        waiting = asyncio.Event()

        @contextlib.asynccontextmanager
        async def turn(self, source):
            if source in self.locks and self.locks[source].locked():
                Watched.waiting.set()
            async with super().turn(source):
                yield

    monkeypatch.setattr(lookup, "Pace", Watched)
    first = "10.5555/first.in.line"
    mock = MockScholarly(openalex={first: openalex_work(first, "First In Line"), DOI: openalex_work(DOI, TITLE)})
    async with started(tmp_path / "data", MockProvider(scholarly=mock)) as client:
        project = await project_of(client)
        mock.hold = asyncio.Event()
        await added(client, project, ("first.md", f"# First\n\ndoi:{first}\n".encode()))
        await asyncio.wait_for(mock.started.wait(), 10)  # its request holds OpenAlex's turn
        result = await added(client, project, ("paper.pdf", synthetic.paper_pdf()))
        await asyncio.wait_for(Watched.waiting.wait(), 10)  # the paper's DOI, checked current, waits for the turn
        await added(client, project, ("v2.md", b"# Version two\n\nNo identifier in this one.\n"),
                    material_id=result["materials"][0]["id"])
        mock.hold.set()
        assert (await run_finished(client, result["lookup_run_id"]))["status"] == "succeeded"
        assert [path for _, path, _ in mock.requests] == [f"/works/doi:{first}"]  # the replaced file's DOI: never sent
        [(data,)] = await rows(client, "SELECT data FROM run_events WHERE run_id = ? AND type = 'step_finished'",
                               result["lookup_run_id"])
        assert json.loads(data)["outcome"] == "replaced"


async def test_an_older_lookup_answered_after_its_file_was_replaced_writes_nothing(tmp_path):
    mock = MockScholarly(openalex={DOI: openalex_work(DOI, TITLE)}, arxiv={synthetic.ARXIV: "The Replacement Preprint"})
    async with started(tmp_path / "data", MockProvider(scholarly=mock)) as client:
        project = await project_of(client)
        mock.hold, mock.held = asyncio.Event(), {"api.openalex.org"}  # the DOI's answer is slow
        result = await added(client, project, ("paper.pdf", synthetic.paper_pdf()))
        await asyncio.wait_for(mock.started.wait(), 10)  # version A's DOI is in flight
        replaced = await added(client, project, ("v2.md", synthetic.paper_markdown()),
                               material_id=result["materials"][0]["id"])
        assert (await run_finished(client, replaced["lookup_run_id"]))["status"] == "succeeded"  # B's arXiv ID first
        mock.hold.set()
        assert (await run_finished(client, result["lookup_run_id"]))["status"] == "succeeded"
        [paper] = await settled(client, project)
        assert (paper["title"], paper["source_key"]) == ("The Replacement Preprint", f"arxiv:{synthetic.ARXIV}")
        assert paper["lookup"]["run_id"] == replaced["lookup_run_id"] and paper["lookup"]["outcome"] == "resolved"
        [(data,)] = await rows(client, "SELECT data FROM run_events WHERE run_id = ? AND type = 'step_finished'",
                               result["lookup_run_id"])
        assert json.loads(data)["outcome"] == "replaced"  # A's answer came, and was not applied


# What is sent, and where


async def test_a_doi_from_the_file_is_sent_alone_and_anonymously_to_openalex(tmp_path):
    async with started(tmp_path / "data", scholarly(openalex={DOI: openalex_work(DOI, TITLE)})) as client:
        project = await project_of(client)
        await added(client, project, ("paper.pdf", synthetic.paper_pdf()))
        [paper] = await settled(client, project)
        [(host, path, headers)] = client.provider.scholarly.requests
        assert (host, path) == ("api.openalex.org", f"/works/doi:{DOI}")  # the identifier alone
        assert not {"authorization", "cookie", "x-api-key", "mailto"} & {h.lower() for h in headers}
        assert "mailto" not in path and "api_key" not in path
        assert paper["title"] == TITLE and paper["checked_by"] == "lookup" and paper["resolved_at"]
        assert paper["csl"]["DOI"] == DOI and paper["csl"]["author"] == [{"literal": "A. Researcher"}]
        assert (paper["source_key"], paper["retraction"]) == (f"doi:{DOI}", "none")
        assert paper["retraction_checked_at"] == paper["checked_at"]
        assert paper["lookup"]["outcome"] == "resolved" and paper["lookup"]["source"] == "openalex"
        [decision] = await gate_log(client)
        assert decision["decision"] == "allow" and decision["approved"] is False and decision["sensitivity"] == "normal"
        assert await rows(client, "SELECT count(*) FROM materials WHERE checked_by = 'lookup'") == [(1,)]


async def test_crossref_answers_when_openalex_has_no_record_and_arxiv_ids_go_to_arxiv(tmp_path):
    other = "10.5555/crossref.only"
    mock = MockScholarly(crossref={other: crossref_work(other, "Only In Crossref")},
                         arxiv={synthetic.ARXIV: "An arXiv Preprint"})
    async with started(tmp_path / "data", MockProvider(scholarly=mock)) as client:
        project = await project_of(client, level="private")  # Private looks up without asking
        await added(client, project, ("a.md", f"# A\n\ndoi:{other}\n".encode()),
                    ("b.md", synthetic.paper_markdown()))
        papers = {p["title"]: p for p in await settled(client, project)}
        assert set(papers) == {"Only In Crossref", "An arXiv Preprint"}
        assert papers["Only In Crossref"]["csl"]["author"] == [{"family": "Example", "given": "Ana"}]
        assert papers["An arXiv Preprint"]["retraction"] == "unknown"  # arXiv says nothing on retraction
        assert papers["An arXiv Preprint"]["source_key"] == f"arxiv:{synthetic.ARXIV}"
        assert sorted(mock.hosts) == ["api.crossref.org", "api.openalex.org", "export.arxiv.org"]
        assert {p["lookup"]["source"] for p in papers.values()} == {"crossref", "arxiv"}


async def test_a_doi_after_many_short_passages_but_within_the_first_characters_is_found(tmp_path):
    late = "10.5555/after.many.notes"
    notes = "".join(f"Note {n}.\n\n" for n in range(250))  # 250 short passages, about 2,400 characters
    async with started(tmp_path / "data", scholarly(openalex={late: openalex_work(late, "Found Late")})) as client:
        project = await project_of(client)
        await added(client, project, ("notes.md", f"# Many Notes\n\n{notes}doi:{late}\n".encode()))
        [paper] = await settled(client, project)
        assert paper["extraction"]["passages"] == 252  # the title, the notes and the DOI's
        assert (paper["title"], paper["lookup"]["identifier"]) == ("Found Late", f"doi:{late}")


async def test_a_doi_only_in_the_reference_list_or_with_none_sends_nothing(tmp_path):
    references_only = b"# A Paper\n\nNo identifier here.\n\n## References\n\n- Smith (2020). doi:10.5555/cited.paper.002\n"
    async with started(tmp_path / "data") as client:
        project = await project_of(client)
        await added(client, project, ("a.md", references_only))
        [paper] = await settled(client, project)
        assert client.provider.scholarly.requests == []
        assert paper["lookup"]["outcome"] == "no_identifier" and paper["checked_by"] is None


async def test_a_failed_lookup_leaves_the_material_imported_with_incomplete_metadata(tmp_path):
    mock = MockScholarly()
    mock.answers = {"api.openalex.org": [503, 503, 503], "api.crossref.org": [500, 500, 500]}
    async with started(tmp_path / "data", MockProvider(scholarly=mock)) as client:
        project = await project_of(client)
        result = await added(client, project, ("paper.pdf", synthetic.paper_pdf()))
        [paper] = await settled(client, project)
        assert paper["state"] == "ready" and paper["title"] == "paper" and paper["checked_by"] is None
        assert paper["lookup"]["outcome"] == "unavailable" and paper["retraction"] == "unknown"
        assert mock.hosts == ["api.openalex.org"] * 3 + ["api.crossref.org"] * 3  # each retried twice, no more
        # The run failed with its reason, so Retry is offered, and taken once the services answer again.
        run = await run_finished(client, result["lookup_run_id"])
        assert (run["status"], run["result"], run["retryable"]) == ("failed", {"reason": "unavailable"}, True)
        mock.openalex[DOI] = openalex_work(DOI, TITLE)
        again = await client.post(f"/api/runs/{result['lookup_run_id']}/retry")
        assert again.status_code == 201
        assert (await run_finished(client, again.json()["run_id"]))["status"] == "succeeded"
        [paper] = await settled(client, project)
        assert paper["title"] == TITLE and paper["checked_by"] == "lookup"


async def test_openalex_unavailable_and_crossref_without_the_record_is_unavailable_not_not_found(tmp_path):
    mock = MockScholarly()  # Crossref holds no record for it
    mock.answers = {"api.openalex.org": [503, 503, 503]}  # OpenAlex never answers
    async with started(tmp_path / "data", MockProvider(scholarly=mock)) as client:
        project = await project_of(client)
        result = await added(client, project, ("paper.pdf", synthetic.paper_pdf()))
        [paper] = await settled(client, project)
        assert paper["lookup"]["outcome"] == "unavailable"  # OpenAlex may hold it: not known to be missing
        run = await run_finished(client, result["lookup_run_id"])
        assert (run["status"], run["result"], run["retryable"]) == ("failed", {"reason": "unavailable"}, True)
        assert mock.hosts == ["api.openalex.org"] * 3 + ["api.crossref.org"]


@pytest.mark.parametrize("body", ["long", "compressed"])
async def test_an_answer_past_the_body_limit_is_given_up_as_it_streams_in(tmp_path, monkeypatch, body):
    monkeypatch.setattr(lookup, "MAX_BODY", 5000)
    if body == "long":  # 50,000 bytes in 50 chunks
        chunks, headers = [b" " * 1000] * 50, {}
    else:  # 10 MB of spaces as about 10 kB of gzip, in 1 kB chunks
        packed = gzip.compress(b" " * 10_000_000)
        chunks, headers = [packed[i:i + 1000] for i in range(0, len(packed), 1000)], {"content-encoding": "gzip"}
    streams = []

    async def answer(request):
        streams.append(Chunks(chunks))
        return httpx.Response(200, headers=headers, stream=streams[-1])

    async with started(tmp_path / "data", MockProvider(scholarly=answer)) as client:
        project = await project_of(client)
        await added(client, project, ("paper.pdf", synthetic.paper_pdf()))
        [paper] = await settled(client, project)
        assert paper["lookup"]["outcome"] == "unavailable" and paper["checked_by"] is None
    assert len(streams) == 2  # OpenAlex's answer, then Crossref's: neither is tried again
    assert all(stream.read <= 7 for stream in streams)  # each stopped once past the limit, not read to its end


async def test_a_compressed_answer_within_the_limit_is_read(tmp_path):
    record = json.dumps(openalex_work(DOI, TITLE)).encode()

    async def answer(request):
        return httpx.Response(200, headers={"content-encoding": "gzip"}, stream=Chunks([gzip.compress(record)]))

    async with started(tmp_path / "data", MockProvider(scholarly=answer)) as client:
        project = await project_of(client)
        await added(client, project, ("paper.pdf", synthetic.paper_pdf()))
        [paper] = await settled(client, project)
        assert paper["title"] == TITLE


async def test_an_identifier_no_service_knows_is_a_finished_lookup_not_a_failed_one(tmp_path):
    async with started(tmp_path / "data") as client:  # neither stand-in holds a record for it
        project = await project_of(client)
        result = await added(client, project, ("paper.pdf", synthetic.paper_pdf()))
        [paper] = await settled(client, project)
        assert paper["lookup"]["outcome"] == "not_found" and paper["checked_by"] is None
        run = await run_finished(client, result["lookup_run_id"])
        assert (run["status"], run["retryable"]) == ("succeeded", False)


async def test_a_rate_limited_request_is_retried_and_resolves(tmp_path):
    mock = MockScholarly(openalex={DOI: openalex_work(DOI, TITLE)})
    mock.answers = {"api.openalex.org": [(429, {"retry-after": "0"})]}
    async with started(tmp_path / "data", MockProvider(scholarly=mock)) as client:
        project = await project_of(client)
        await added(client, project, ("paper.pdf", synthetic.paper_pdf()))
        [paper] = await settled(client, project)
        assert paper["title"] == TITLE and mock.hosts == ["api.openalex.org"] * 2


async def test_the_starting_values_for_pacing_retries_and_time():
    spacing, retries, timeout = DEFAULTS  # sections 7.6 and 13
    assert (spacing["arxiv"], retries, timeout) == (3.0, (1.0, 4.0), 20.0)


async def test_pacing_spaces_one_sources_requests():
    pace = lookup.Pace()
    lookup.SPACING["arxiv"] = 0.15
    loop = asyncio.get_running_loop()
    times = []
    for _ in range(3):
        async with pace.turn("arxiv"):
            times.append(loop.time())
    assert all(b - a >= 0.14 for a, b in zip(times, times[1:]))


async def test_a_sources_next_request_waits_for_a_slow_one_to_be_answered():
    seen = {"in_flight": 0, "most": 0, "paths": []}

    async def slow(request):  # each answer takes longer than the source's spacing
        seen["in_flight"] += 1
        seen["most"] = max(seen["most"], seen["in_flight"])
        seen["paths"].append(request.url.path)
        await asyncio.sleep(0.2)
        seen["in_flight"] -= 1
        return streamed(httpx.Response(200, content=arxiv_feed("2401.00001", "A Synthetic Preprint")))

    pace = lookup.Pace()
    lookup.SPACING["arxiv"] = 0.05  # shorter than an answer takes
    async with httpx.AsyncClient(transport=httpx.MockTransport(slow)) as client:
        found = await asyncio.gather(*(lookup.resolve(client, "arxiv", "2401.00001", pace) for _ in range(3)))
    assert [f.source for f in found] == ["arxiv"] * 3 and len(seen["paths"]) == 3
    assert seen["most"] == 1  # never two requests to arXiv at once, across lookups


async def test_a_researchers_edit_is_kept_and_its_retraction_still_checked(tmp_path):
    mock = MockScholarly(openalex={DOI: openalex_work(DOI, TITLE, retracted=True)})
    async with started(tmp_path / "data", MockProvider(scholarly=mock)) as client:
        project = await project_of(client)
        result = await added(client, project, ("paper.pdf", synthetic.paper_pdf()))
        await settled(client, project)
        material = result["materials"][0]["id"]
        await client.patch(f"/api/materials/{material}", json={"title": "My Own Title"})
        again = await client.post(f"/api/runs/{result['lookup_run_id']}/retry")
        assert again.status_code == 409  # a lookup that succeeded is not tried again
        await asyncio.to_thread(client.state["db"].write, lambda conn: conn.execute(
            "UPDATE runs SET status = 'failed' WHERE id = ?", (result["lookup_run_id"],)))
        again = await client.post(f"/api/runs/{result['lookup_run_id']}/retry")
        assert (await run_finished(client, again.json()["run_id"]))["status"] == "succeeded"
        [paper] = await settled(client, project)
        assert (paper["title"], paper["checked_by"], paper["retraction"]) == ("My Own Title", "researcher", "retracted")


# By level


async def test_a_review_locked_project_never_looks_up(tmp_path):
    async with started(tmp_path / "data") as client:
        project = (await client.post("/api/projects", json={"name": "Review", "sensitivity": "local_only",
                                                           "review_lock": True})).json()["id"]
        result = await added(client, project, ("paper.pdf", synthetic.paper_pdf()))
        assert result["lookup_run_id"] is None
        await settled(client, project)
        assert client.provider.scholarly.requests == [] and await gate_log(client) == []
        assert await rows(client, "SELECT count(*) FROM runs WHERE workflow = 'lookup'") == [(0,)]


async def test_a_local_only_project_asks_once_per_batch_and_the_answer_covers_that_batch_only(tmp_path):
    records = {DOI: openalex_work(DOI, TITLE)}
    async with started(tmp_path / "data", scholarly(openalex=records, arxiv={synthetic.ARXIV: "Preprint"})) as client:
        project = await project_of(client, level="local_only")
        result = await added(client, project, ("paper.pdf", synthetic.paper_pdf()),
                             ("same.docx", synthetic.paper_docx()),  # the same DOI: counted once
                             ("notes.md", synthetic.paper_markdown()))
        [ask] = await ask_of(client, project)
        assert ask["run_id"] == result["lookup_run_id"] and ask["kind"] == "identifier_lookup"
        assert ask["params"] == {"services": ["arxiv", "crossref", "openalex"], "identifiers": 2}
        assert ask["options"] == ["lookup", "skip"] and ask["text_box"] is False and ask["project_id"] == project
        assert client.provider.scholarly.requests == []  # nothing before the answer
        answered = await client.post(f"/api/runs/{ask['run_id']}/asks/{ask['ask_id']}", json={"option": "lookup"})
        assert answered.status_code == 200
        papers = await settled(client, project)
        assert sum(p["checked_by"] == "lookup" for p in papers) == 3
        assert {e["approved"] for e in await gate_log(client)} == {True}
        assert len(client.provider.scholarly.requests) == 2  # each distinct identifier once
        # A later import asks again: the answer covered its own batch.
        later = await added(client, project, ("later.md", f"# Later\n\ndoi:{DOI}\n".encode()))
        [again] = await ask_of(client, project)
        assert again["run_id"] == later["lookup_run_id"] and again["params"]["identifiers"] == 1
        declined = await client.post(f"/api/runs/{again['run_id']}/asks/{again['ask_id']}", json={"option": "skip"})
        assert declined.status_code == 200
        run = await run_finished(client, later["lookup_run_id"])
        assert (run["status"], run["cancel_reason"], run["result"]) == ("cancelled", "researcher", {"reason": "declined"})
        assert len(client.provider.scholarly.requests) == 2  # nothing sent for it
        retried = await client.post(f"/api/runs/{later['lookup_run_id']}/retry")
        assert retried.status_code == 201
        [asked_again] = await ask_of(client, project)  # a retry from the list asks again
        assert asked_again["run_id"] == retried.json()["run_id"]


async def test_a_local_only_lookup_cancelled_while_it_asks_closes_its_ask(tmp_path):
    async with started(tmp_path / "data") as client:
        project = await project_of(client, level="local_only")
        result = await added(client, project, ("paper.pdf", synthetic.paper_pdf()))
        [ask] = await ask_of(client, project)
        assert (await client.post(f"/api/runs/{result['lookup_run_id']}/cancel")).json()["status"] == "cancelled"
        late = await client.post(f"/api/runs/{ask['run_id']}/asks/{ask['ask_id']}", json={"option": "lookup"})
        assert (late.status_code, late.json()["code"]) == (409, "ask_closed")
        assert (await listing(client, project))["asks"] == [] and client.provider.scholarly.requests == []
