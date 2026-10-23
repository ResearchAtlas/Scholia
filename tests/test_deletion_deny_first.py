"""Deleted sources stay out (slice-1 spec section 14, test_deletion_deny_first.py), for what S1-13
builds: once a material is deleted, nothing of it is read back (its record, versions, passages and
page images), its passages leave its project's index after they were added, and a file shared with
another project stays there. Deletion while a material is read or looked up is in test_materials.py
and test_identifier_lookup.py; the index's own checks come with S1-17, memory's with S1-21a (below), and compaction
summaries' with S1-21b (the last test)."""

import asyncio

import pytest

import synthetic_materials as synthetic
from scholia_app import started
from test_materials import added, kept_queue, project_of, rows, settled  # noqa: F401

pytestmark = pytest.mark.asyncio


async def test_a_deleted_material_is_read_back_nowhere_and_leaves_its_projects_index(tmp_path, kept_queue):
    async with started(tmp_path / "data") as client:
        mine, theirs = await project_of(client, "Mine"), await project_of(client, "Theirs")
        file = ("paper.pdf", synthetic.paper_pdf())  # one file: each PDF made is a new document
        [paper] = (await added(client, mine, file))["materials"]
        await added(client, theirs, file)
        [ready] = await settled(client, mine)
        [kept] = await settled(client, theirs)
        version = ready["version"]["id"]
        passages = (await client.get(f"/api/material-versions/{version}/passages")).json()["passages"]
        assert (await client.delete(f"/api/materials/{paper['id']}")).status_code == 200
        for path in (f"/api/materials/{paper['id']}", f"/api/materials/{paper['id']}/versions",
                     f"/api/material-versions/{version}/passages", f"/api/material-versions/{version}/pages/1"):
            assert (await client.get(path)).status_code == 404, path
        listed = await client.get(f"/api/projects/{mine}/materials")
        assert listed.json()["materials"] == [], listed.text
        removed = await rows(client, "SELECT project_id, count(*) FROM index_queue WHERE op = 'remove'"
                                     " AND target = 'passage' GROUP BY 1")  # S1-21a: its progress record's too, apart
        assert removed == [(mine, len(passages))]  # only its own project's rows; the other keeps the shared file
        # The shared extraction is the other project's still: readable there.
        assert (await client.get(f"/api/material-versions/{kept['version']['id']}/passages")).json()["total"] == len(passages)
        one = (await client.get(f"/api/passages/{passages[0]['id']}")).json()
        assert [m["project_id"] for m in one["materials"]] == [theirs]



@pytest.mark.parametrize("deleted", ["markdown", "latex"])
async def test_deleting_one_reading_of_a_file_read_two_ways_removes_its_passages_only(tmp_path, deleted, kept_queue):
    source = b"\\section{Method}\n\nText with \\emph{emphasis} here.\n"  # Markdown and LaTeX alike
    async with started(tmp_path / "data") as client:
        project = await project_of(client)
        [as_markdown] = (await added(client, project, ("notes.md", source)))["materials"]
        [as_latex] = (await added(client, project, ("other.md", b"# Other\n\nAnother file entirely.\n")))["materials"]
        await settled(client, project)
        await added(client, project, ("notes.tex", source), material_id=as_latex["id"])  # Replace file: the same bytes
        await settled(client, project)
        readings = dict(await rows(client, "SELECT e.extractor, e.id FROM extractions e JOIN material_versions v"
                                           " ON v.file_sha256 = e.file_sha256 WHERE v.material_id = ?", as_markdown["id"]))
        assert set(readings) == {"markdown", "latex"}  # one stored file, read two ways
        passages = {name: {p for (p,) in await rows(client, "SELECT id FROM passages WHERE extraction_id = ?", extraction)}
                    for name, extraction in readings.items()}
        kept_reading = "latex" if deleted == "markdown" else "markdown"
        survivor = as_latex if deleted == "markdown" else as_markdown
        target = as_markdown if deleted == "markdown" else as_latex
        assert (await client.delete(f"/api/materials/{target['id']}")).status_code == 200
        removed = {t for (t,) in await rows(client, "SELECT target_id FROM index_queue WHERE op = 'remove'")}
        # By what was deleted, not by the file: the deleted reading's passages leave the index, the other's stay.
        assert passages[deleted] <= removed and not passages[kept_reading] & removed
        # Its extraction goes with its last reader; the other reading is the survivor's still.
        assert await rows(client, "SELECT extractor FROM extractions WHERE id IN (?, ?)", *readings.values()) == [
            (kept_reading,)]
        assert await rows(client, "SELECT count(*) FROM passages WHERE extraction_id = ?", readings[deleted]) == [(0,)]
        [current] = (await client.get(f"/api/projects/{project}/materials")).json()["materials"]
        texts = (await client.get(f"/api/material-versions/{current['version']['id']}/passages")).json()["passages"]
        assert current["id"] == survivor["id"] and {p["id"] for p in texts} == passages[kept_reading]


async def test_a_stale_index_never_surfaces_a_deleted_or_replaced_paper(tmp_path, monkeypatch):
    """S1-17: every search checks access again in the main database (slice-1 spec 4.3): passages the index
    still holds, its removals not applied yet, are never returned."""
    from backend.search_index import SearchIndex
    from test_search import app, find, idle, index_rows, paper

    async with app(tmp_path, install=False) as client:
        project = await project_of(client)
        [deleted] = (await added(client, project, paper("Deleted", "The aubergine finding.")))["materials"]
        [replaced] = (await added(client, project, paper("Replaced", "The obsolete quince finding.")))["materials"]
        await idle(client, project)
        monkeypatch.setattr(SearchIndex, "_apply", lambda self, stop=None: 0)  # the index applies nothing from here
        monkeypatch.setattr(SearchIndex, "_reconcile", lambda self, project_id, stop=None: (0, 0))  # nor makes its drift good
        assert (await client.delete(f"/api/materials/{deleted['id']}")).status_code == 200
        await added(client, project, paper("Replaced", "The current finding."), material_id=replaced["id"])
        await idle(client, project)
        texts = {r[0] for r in await index_rows(client, project)}
        assert len(texts) == 4  # stale: still the old rows
        for query in ("aubergine", "quince", "deleted", "obsolete"):
            assert (await find(client, project, query))["results"] == [], query


async def test_a_deleted_scanned_papers_recognized_passages_are_read_back_nowhere_and_leave_its_index(tmp_path, monkeypatch,
                                                                                                         kept_queue):
    from test_ocr import Engine, scan, use

    use(monkeypatch, Engine())  # S1-20: its page read by a test-owned engine (in this process)
    async with started(tmp_path / "data") as client:
        project = await project_of(client)
        [paper] = (await added(client, project, ("scan.pdf", scan())))["materials"]
        [ready] = await settled(client, project)
        version = ready["version"]["id"]
        [passage] = (await client.get(f"/api/material-versions/{version}/passages")).json()["passages"]
        assert passage["boxes"]["ocr"]
        await asyncio.to_thread(client.state["index"].apply)
        assert await searched(client, project, "Recognized text of a scanned page") == {passage["id"]}  # found before
        assert (await client.delete(f"/api/materials/{paper['id']}")).status_code == 200
        for path in (f"/api/material-versions/{version}/passages", f"/api/material-versions/{version}/pages/1",
                     f"/api/passages/{passage['id']}"):
            assert (await client.get(path)).status_code == 404, path
        assert await rows(client, "SELECT op FROM index_queue WHERE target_id = ? ORDER BY seq", passage["id"]) == [
            ("add",), ("remove",)]
        assert await rows(client, "SELECT count(*) FROM passages") == [(0,)]
        # S1-17: search reads it back nowhere either, once the index has applied its queue (keyword search:
        # this test has no helper)
        await asyncio.to_thread(client.state["index"].apply)
        assert await searched(client, project, "Recognized text of a scanned page") == set()


async def searched(client, project, query):
    """The passage ids the project's search returns for query (S1-17's API)."""
    response = await client.post(f"/api/projects/{project}/search", json={"query": query, "limit": 50})
    assert response.status_code == 200, response.text
    return {r["passage_id"] for r in response.json()["results"]}


async def test_a_superseded_reading_is_removed_through_the_deletion_service_and_read_back_nowhere(tmp_path, monkeypatch,
                                                                                                    kept_queue):
    """S1-20: a reading another reading of its file replaced leaves each index it was in, its passages
    are read back nowhere, and its removal leaves a tombstone (section 4.2) and no dangling reference."""
    import backend.extraction as extraction
    from test_ocr import cite
    from scholia_app import read_in_process, run_finished

    read_in_process(monkeypatch)  # an earlier extractor version, as this process sees it
    notes = b"# Notes\n\nA paragraph of synthetic text.\n"
    async with started(tmp_path / "data") as client:
        mine, theirs = await project_of(client, "Mine"), await project_of(client, "Theirs")
        monkeypatch.setitem(extraction.EXTRACTORS, extraction.MARKDOWN, ("markdown", "markdown-0"))
        [paper] = (await added(client, mine, ("notes.md", notes)))["materials"]
        [read] = await settled(client, mine)
        await added(client, theirs, ("notes.md", notes))
        await settled(client, theirs)
        [(older,)] = await rows(client, "SELECT id FROM extractions")
        earlier = [p for (p,) in await rows(client, "SELECT id FROM passages WHERE extraction_id = ?", older)]
        await cite(client, paper["id"], earlier[0], "synthetic text")
        monkeypatch.setitem(extraction.EXTRACTORS, extraction.MARKDOWN, ("markdown", "markdown-1"))
        again = await client.post(f"/api/material-versions/{read['version']['id']}/read")
        assert (await run_finished(client, again.json()["run_id"]))["status"] == "succeeded"
        for passage in earlier:
            assert (await client.get(f"/api/passages/{passage}")).status_code == 404
            assert {p for (_, p, op) in await rows(client, "SELECT target_id, project_id, op FROM index_queue"
                                                          " WHERE target_id = ? AND op = 'remove'", passage)} == {mine, theirs}
        assert await rows(client, "SELECT count(*) FROM passages WHERE extraction_id = ?", older) == [(0,)]
        assert await rows(client, "SELECT object_id, kind, title FROM tombstones") == [(older, "reading", None)]
        assert await rows(client, "SELECT count(*) FROM audit_log WHERE event = 'deletion'") == [(0,)]
        assert await rows(client, "SELECT count(*) FROM citations c LEFT JOIN passages p ON p.id = c.passage_id"
                                  " WHERE c.passage_id IS NOT NULL AND p.id IS NULL") == [(0,)]
        # S1-17: search reads the superseded passages back nowhere, once the index has applied its queue
        # (keyword search: this test has no helper); each project finds the newer reading's instead.
        await asyncio.to_thread(client.state["index"].apply)
        newer = {p for (p,) in await rows(client, "SELECT id FROM passages")}
        for project in (mine, theirs):
            found = await searched(client, project, "A paragraph of synthetic text")
            assert found and not found & set(earlier) and found <= newer



async def test_a_deleted_reference_files_paper_is_read_back_nowhere_and_its_merge_writes_nothing(tmp_path):
    # S1-14: a metadata-only paper, never indexed, deleted while a merge into it waits.
    from test_reference_import import answer, run_finished, with_duplicate
    async with started(tmp_path / "data") as client:
        project = await project_of(client)
        paper, ask = await with_duplicate(client, project)
        version = (await client.get(f"/api/materials/{paper}")).json()["version"]["id"]
        assert (await client.delete(f"/api/materials/{paper}", params={"remove_all_trace": "true"})).status_code == 200
        for path in (f"/api/materials/{paper}", f"/api/materials/{paper}/versions", f"/api/material-versions/{version}/passages"):
            assert (await client.get(path)).status_code == 404, path
        assert (await run_finished(client, ask["run_id"]))["cancel_reason"] == "revoked"
        assert (await answer(client, ask, "merge")).status_code == 409
        assert await rows(client, "SELECT count(*) FROM materials") == [(0,)]
        assert await rows(client, "SELECT count(*) FROM index_queue") == [(0,)]  # it never had passages
        # Nothing of its title or its record is kept by the run that waited on it.
        kept = await rows(client, "SELECT inputs FROM runs UNION ALL SELECT data FROM run_events")
        assert not any("A Paper Already Here" in (text or "") for (text,) in kept)
        assert (await client.get(f"/api/projects/{project}/materials")).json() == {"materials": [], "asks": [], "merging": 0}


# S1-19: a turn's evidence (L3)

@pytest.mark.parametrize("when", ["after_retrieval", "after_registration"])
async def test_a_paper_deleted_between_retrieval_and_dispatch_reaches_no_request(tmp_path, monkeypatch, when):
    # The turn's evidence names its papers and passages on its run before the answer is sent: deleting a
    # paper once they are named revokes the run (deletion's scope links), and one deleted before they are
    # named is refused by the dispatch check (its passages are gone); either way nothing deleted is sent.
    from backend import pipeline, runs as runs_module
    from cited import ANSWER, cited, conversation_in, with_papers
    from scholia_app import send
    from test_search import WAGES
    async with cited(tmp_path) as client:
        project, [material] = await with_papers(client, WAGES)
        conversation = await conversation_in(client, project)
        if when == "after_retrieval":
            real = pipeline.evidence

            async def then_delete(*args, **kwargs):
                found = await real(*args, **kwargs)
                assert (await client.delete(f"/api/materials/{material}")).status_code == 200
                return found
            monkeypatch.setattr(pipeline, "evidence", then_delete)
        else:
            real = runs_module.Harness._register_context

            async def then_delete(self, claim, numbered):
                await real(self, claim, numbered)
                assert (await client.delete(f"/api/materials/{material}")).status_code == 200
            monkeypatch.setattr(runs_module.Harness, "_register_context", then_delete)
        stream = await send(client, conversation, "What do minimum wages do to earnings?")
        assert client.provider.answers == []  # nothing deleted reached a request
        assert stream[-1]["status"] in ("cancelled", "failed")
        assert await rows(client, "SELECT count(*) FROM citations") == [(0,)]
        assert ANSWER not in str(await rows(client, "SELECT answer FROM turns"))


# S1-21a: memory (slice-1 spec sections 10 and 14: "Deleted or inaccessible sources stay out of context and memory")


async def test_a_record_forgotten_while_a_request_holds_it_revokes_the_run_and_is_never_sent_again(tmp_path):
    # L3 (c): the dispatch check covers every memory record in the request's context; the run ends revoked, and
    # Continue builds without it.
    import json as _json

    from scholia_app import MockProvider, send
    from test_memory import conversation_in
    provider = MockProvider()
    held = asyncio.Event()
    forgotten = asyncio.Event()

    async def answering(body):
        held.set()
        await forgotten.wait()
        return provider.answer("An answer.")
    async with started(tmp_path / "data", provider) as client:
        project = await project_of(client)
        record = (await client.post("/api/memory", json={"project_id": project, "type": "decision",
                                                          "content": "We dropped the pilot study"})).json()
        conversation = await conversation_in(client, project)
        provider.replies.append(answering)
        provider.replies.append(provider.answer("A second answer."))
        provider.hold = None
        sending = asyncio.create_task(send(client, conversation, "What did we decide last time?"))
        await held.wait()  # the first attempt holds the record; its retry must not
        assert (await client.delete(f"/api/memory/{record['id']}")).json() == {"ok": True}
        forgotten.set()
        stream = await sending
        assert stream[-1]["status"] == "cancelled" and stream[-1]["cancel_reason"] == "revoked"
        again = await client.post(f"/api/runs/{stream[0]['run_id']}/continue")
        assert again.status_code == 200
        for body in provider.answers[1:]:
            assert "We dropped the pilot study" not in _json.dumps(body)


async def test_a_deleted_papers_memory_and_its_extractions_held_reply_go_in_the_deletions_transaction(tmp_path):
    # L3 (b): a record that may carry a paper's text goes with the paper (any one of them), its vector is queued
    # for removal; an extraction holding a reply drawn on it loses that reply at once.
    from backend import memory
    async with started(tmp_path / "data") as client:
        project = await project_of(client)
        [paper] = (await added(client, project, ("notes.md", b"# Notes\n\nWages rose.\n")))["materials"]
        [other] = (await added(client, project, ("more.md", b"# More\n\nPrices fell.\n")))["materials"]

        def seeded(conn):
            record = memory.write(conn, project_id=project, scope="project", kind="event", content="Wages rose, it says",
                                  status="confirmed", sources=[("material", paper["id"]), ("material", other["id"])])
            run = memory.new_id()
            conn.execute("INSERT INTO runs (id, project_id, kind, workflow, inputs) VALUES (?, ?, 'background', 'x', ?)",
                         (run, project, '{"context_materials": ["%s"], "held": {"output": [{"content": "Wages rose"}]}}'
                          % paper["id"]))
            return record, run
        record, run = await asyncio.to_thread(client.state["db"].write, seeded)
        assert (await client.delete(f"/api/materials/{other['id']}")).status_code == 200  # any one of its papers
        assert await rows(client, "SELECT count(*) FROM memory_records WHERE id = ?", record) == [(0,)]
        assert (await client.delete(f"/api/materials/{paper['id']}")).status_code == 200
        assert await rows(client, "SELECT json_extract(inputs, '$.held.output') FROM runs WHERE id = ?", run) == [(None,)]


async def test_the_dispatch_check_refuses_a_request_holding_a_memory_record_that_is_gone(tmp_path):
    import json as _json

    from backend import memory, runs
    from test_memory import conversation_in
    async with started(tmp_path / "data") as client:
        project = await project_of(client)
        conversation = await conversation_in(client, project)
        record = (await client.post("/api/memory", json={"project_id": project, "type": "decision",
                                                          "content": "Held"})).json()["id"]
        run = memory.new_id()
        db = client.state["db"]
        await asyncio.to_thread(db.write, lambda conn: conn.execute(
            "INSERT INTO runs (id, project_id, conversation_id, kind, workflow, inputs) VALUES (?, ?, ?, 'turn', 'agent', ?)",
            (run, project, conversation, _json.dumps({"memory_ids": [record]}))))
        assert await asyncio.to_thread(db.read, lambda conn: runs.may_dispatch(conn, run)) is True
        assert (await client.delete(f"/api/memory/{record}")).json() == {"ok": True}
        assert await rows(client, "SELECT cancel_reason FROM runs WHERE id = ?", run) == [("revoked",)]  # SCOPE_LINKS
        await asyncio.to_thread(db.write, lambda conn: conn.execute("UPDATE runs SET cancel_reason = NULL WHERE id = ?",
                                                                    (run,)))
        assert await asyncio.to_thread(db.read, lambda conn: runs.may_dispatch(conn, run)) is False  # gone, whatever else


async def test_l8_a_summary_of_a_turn_citing_a_deleted_paper_is_not_reused(tmp_path):
    # S1-21b, checklist L8 (SPEC:1191, HS:761): deleted sources stay out of context, caches and summaries included.
    from cited import cited, conversation_in, rows, with_papers
    from scholia_app import send
    from test_compaction import SUMMARY, Long, small_history, summary_rows, text_of
    from test_search import WAGES
    provider = Long(summary=SUMMARY + " QUOTE-A: minimum wages raise the earnings of low-paid workers.")
    async with cited(tmp_path, provider) as client:
        await small_history(client)
        project, [material] = await with_papers(client, WAGES)
        chat = await conversation_in(client, project)
        await send(client, chat, "What do minimum wages do to earnings?")
        for i in range(1, 4):
            await send(client, chat, f"QUESTION-{i} about cohorts")
        [(_, _, sha)] = await summary_rows(client)
        assert (await client.delete(f"/api/materials/{material}")).status_code == 200
        assert await summary_rows(client) == []  # in the deletion's own transaction
        assert await rows(client, "SELECT count(*) FROM run_body_materials WHERE sha256 = ?", sha) == [(0,)]
        await send(client, chat, "QUESTION-4 about cohorts")
        assert "QUOTE-A" not in text_of(provider.turns[-1]) and "SUMMARY-MARK" not in text_of(provider.turns[-1])
