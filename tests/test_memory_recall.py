"""Recall into the request (S1-21a; slice-1 spec sections 7.3, 8 and 13; ticket 31; checklist R1, R2, R4, R5, R6, P1,
G5): PROJECT STATE and USER MEMORY by the route, at most 10 records within their budgets, the nearest in meaning first
when the search model answers in time (vec_memory), else the newest, said in the trace; never a superseded, expired,
deleted or unconfirmed personal record, nor another project's. Checked on the final serialized requests."""

import asyncio
import json

import pytest

from backend import memory, search
from scholia_app import MockProvider, background_idle, run_finished, send, started
from test_materials import project_of
from test_memory import conversation_in, forget, rows, write

pytestmark = pytest.mark.asyncio


def system(body):
    return body["messages"][0]["content"]


def section(body, label):
    text = system(body)
    if f"## {label}\n" not in text:
        return []
    return text.split(f"## {label}\n", 1)[1].split("\n\n## ", 1)[0].split("\n\n")


async def entered(client, project, content, kind="decision", scope="project"):
    response = await client.post("/api/memory", json={"project_id": project, "type": kind, "content": content,
                                                      "scope": scope})
    assert response.status_code == 201, response.text
    return response.json()


async def test_user_memory_holds_at_most_ten_records_newest_first_and_never_a_superseded_or_expired_one(tmp_path):
    provider = MockProvider()
    async with started(tmp_path / "data", provider) as client:
        project = await project_of(client)
        made = [await entered(client, project, f"Decision number {i}") for i in range(12)]
        old = made[0]
        await client.patch(f"/api/memory/{made[11]['id']}", json={"version": 1, "content": "Decision eleven, revised"})
        await write(client, lambda conn: conn.execute(  # G5: past its validity
            "UPDATE memory_records SET valid_to = '2000-01-01T00:00:00.000Z' WHERE id = ?", (made[10]["id"],)))
        stream = await send(client, await conversation_in(client, project), "What did we decide last time?")
        lines = section(provider.answers[-1], "USER MEMORY")
        assert len(lines) == 10 and lines[0] == "- [decision] Decision eleven, revised"
        assert not any("Decision number 11" in line or "Decision number 10" in line for line in lines)
        assert "Decision number 0" not in "\n".join(lines) and old["content"] == "Decision number 0"  # the oldest left out
        [(trace,)] = await rows(client, "SELECT data FROM run_events WHERE run_id = ? AND type = 'retrieval_trace'",
                                stream[0]["run_id"])
        trace = json.loads(trace)["memory"]
        assert trace["mode"] == "recency" and len(trace["records"]) == 10  # ids only, never their text
        assert "Decision" not in json.dumps(trace)
        [(held,)] = await rows(client, "SELECT json_extract(inputs, '$.memory_ids') FROM runs WHERE id = ?",
                               stream[0]["run_id"])
        assert set(trace["records"]) <= set(json.loads(held))  # the dispatch check covers them


async def test_the_route_decides_what_is_pre_loaded(tmp_path):
    provider = MockProvider()
    async with started(tmp_path / "data", provider) as client:
        project = await project_of(client)
        await entered(client, project, "We use semi-structured interviews.", kind="project_state")
        await entered(client, project, "We dropped the pilot.")
        conversation = await conversation_in(client, project)
        cases = {"Translate this into Chinese: hello": (False, False),  # rewriting supplied text: neither
                 "Compare interviews versus surveys": (False, True),  # a comparison: memory
                 "Why did I drop the pilot? We decided last time": (True, True),  # an earlier decision: both
                 "I keep wondering about attrition": (True, True)}  # no rule decides: both
        for message, (state, remembered) in cases.items():
            await send(client, conversation, message)
            body = provider.answers[-1]
            assert bool(section(body, "PROJECT STATE")) == state, message
            assert bool(section(body, "USER MEMORY")) == remembered, message
        assert "[state] We use semi-structured interviews." in section(provider.answers[-1], "PROJECT STATE")[0]


async def test_project_state_holds_the_state_whats_done_and_the_agents_notes_within_its_budget(tmp_path):
    provider = MockProvider()
    async with started(tmp_path / "data", provider) as client:
        project = await project_of(client)
        await entered(client, project, "Research question: do wage floors raise earnings?", kind="project_state")

        def notes(conn):
            for i in range(8):
                memory.note(conn, project_id=project, run_id=None, kind="next_step", text=f"Next step {i}", materials=())
            memory.materials_added(conn, project, [])
        await write(client, notes)
        await write(client, lambda conn: conn.execute(  # an inferred project state, labeled
            "UPDATE memory_records SET status = 'inferred' WHERE type = 'project_state'"))
        await send(client, await conversation_in(client, project), "Where are we?")
        lines = section(provider.answers[-1], "PROJECT STATE")
        assert lines[0] == "- [state (inferred)] Research question: do wage floors raise earnings?"
        assert [line for line in lines if "agent's note" in line] == [
            f"- [agent's note, next step] Next step {i}" for i in (7, 6, 5, 4, 3)]  # the latest few
        from backend import context
        assert context.count("\n".join(lines)) <= 1500


async def test_another_projects_records_never_enter_a_request_and_personal_ones_only_once_confirmed(tmp_path):
    provider = MockProvider()
    async with started(tmp_path / "data", provider) as client:
        mine, other = await project_of(client, "Mine"), await project_of(client, "Other")
        await entered(client, other, "The other project's decision.")
        await entered(client, other, "I prefer tables.", kind="preference", scope="personal")
        personal = await entered(client, other, "I am a sociologist.", kind="profile", scope="personal")
        await write(client, lambda conn: conn.execute("UPDATE memory_records SET status = 'inferred' WHERE id = ?",
                                                      (personal["id"],)))
        await send(client, await conversation_in(client, mine), "What did we decide last time?")
        held = "\n".join(section(provider.answers[-1], "USER MEMORY"))
        assert "I prefer tables." in held  # a confirmed personal record reaches every project
        assert "The other project's decision." not in held and "sociologist" not in held


async def test_a_review_locked_project_recalls_no_personal_memory(tmp_path):
    async with started(tmp_path / "data") as client:
        project = await project_of(client)
        await entered(client, project, "I prefer tables.", kind="preference", scope="personal")
        await entered(client, project, "Our own decision.")
        found = await asyncio.to_thread(client.state["db"].read, lambda conn: [
            r["content"] for r in memory.recall_candidates(conn, project, locked=True)])
        assert found == ["Our own decision."]


async def test_a_record_whose_paper_is_deleted_is_gone_and_never_recalled(tmp_path):
    from test_materials import added
    provider = MockProvider()
    async with started(tmp_path / "data", provider) as client:
        project = await project_of(client)
        [paper] = (await added(client, project, ("notes.md", b"# Notes\n\nWages rose.\n")))["materials"]

        def saved(conn):  # a saved answer that carries the paper's text
            return memory.write(conn, project_id=project, scope="project", kind="event", content="Wages rose, it says.",
                                status="confirmed", sources=[("material", paper["id"])])
        record = await write(client, saved)
        await send(client, await conversation_in(client, project), "What did we decide last time?")
        assert "Wages rose, it says." in system(provider.answers[-1])
        assert (await client.delete(f"/api/materials/{paper['id']}")).status_code == 200
        assert await rows(client, "SELECT count(*) FROM memory_records WHERE id = ?", record) == [(0,)]
        await send(client, await conversation_in(client, project), "What did we decide last time?")
        assert "Wages rose" not in system(provider.answers[-1])


async def test_recall_ranks_by_meaning_when_the_search_model_answers_and_a_forgotten_vector_goes(tmp_path):
    from cited import cited
    from test_search import idle
    async with cited(tmp_path) as client:
        project = await project_of(client)
        for i in range(11):
            await entered(client, project, f"Unrelated item {i} about scheduling")
        wages = await entered(client, project, "Wage floors raise the earnings of low-paid workers")
        await idle(client, project)
        await background_idle(client, timeout=20)
        index = client.state["index"]
        held = await asyncio.to_thread(index.memory_embedded, [wages["id"]])
        assert held == {wages["id"]}
        stream = await send(client, await conversation_in(client, project),
                            "Last time we decided what about wage floors and earnings?")
        [(trace,)] = await rows(client, "SELECT data FROM run_events WHERE run_id = ? AND type = 'retrieval_trace'"
                                        " AND json_extract(data, '$.memory') IS NOT NULL", stream[0]["run_id"])
        trace = json.loads(trace)["memory"]
        assert trace["mode"] == "similarity" and trace["records"][0] == wages["id"]
        assert (await forget(client, wages['id'])).json() == {"ok": True}
        assert await asyncio.to_thread(index.memory_embedded, [wages["id"]]) == set()  # removed, checked gone


async def test_an_earlier_decision_memory_lacks_is_said_missing_never_invented(tmp_path):
    # R3: the missing-information section says so; that the answer never invents one is [PAID]
    provider = MockProvider()
    async with started(tmp_path / "data", provider) as client:
        project = await project_of(client)
        conversation = await conversation_in(client, project)
        await send(client, conversation, "Why did I drop the pilot? We decided last time.")
        assert memory.MISSING in section(provider.answers[-1], "CONFLICTS AND MISSING INFORMATION")
        await entered(client, project, "We dropped the pilot because of cost.")
        await send(client, conversation, "Why did I drop the pilot? We decided last time.")
        assert memory.MISSING in system(provider.answers[-1]) and "We dropped the pilot" in system(provider.answers[-1])



async def test_ranking_puts_a_record_written_since_the_last_vector_first():
    old = [{"id": f"r{i}", "updated_at": f"2026-10-22T10:00:{i:02d}.000Z"} for i in range(20)]
    fresh = {"id": "new", "updated_at": "2026-10-22T11:00:00.000Z"}
    candidates = [fresh, *reversed(old)]  # newest first, as in_scope gives them
    nearest = [f"r{i}" for i in range(12)]  # the oldest twelve are the nearest
    chosen = memory.ranked(candidates, nearest, embedded={r["id"] for r in old})
    assert [r["id"] for r in chosen] == ["new", *nearest[:9]]
    assert [r["id"] for r in memory.ranked(candidates, None)] == ["new", *[f"r{i}" for i in range(19, 10, -1)]]


async def test_a_personal_record_stays_shown_to_another_projects_extraction_after_its_project_tightens(tmp_path):
    # fix batch 3 (Harold's decision of 2026-10-23): a later tightening never retracts a personal record
    provider = MockProvider()
    async with started(tmp_path / "data", provider) as client:
        first, other = await project_of(client, "First"), await project_of(client, "Other")
        await entered(client, first, "I prefer answers about participant Alice", kind="preference", scope="personal")
        await client.post(f"/api/projects/{first}/sensitivity", json={"level": "private"})  # its own project tightened
        found = await asyncio.to_thread(client.state["db"].read, lambda conn: memory.recall_candidates(conn, other))
        assert [r["content"] for r in found] == ["I prefer answers about participant Alice"]
        await send(client, await conversation_in(client, other), "Hello")
        await background_idle(client)
        assert provider.extractions and "Alice" in json.dumps(provider.extractions[-1])


async def test_a_moved_conversations_record_is_found_by_meaning_in_its_new_project(tmp_path):
    # S1-21a follow-up MB2: its vector leaves the source project's partition and is made again under the destination's
    from cited import cited
    from test_search import idle
    async with cited(tmp_path) as client:
        first, second = await project_of(client, "First"), await project_of(client, "Second")
        conversation = await conversation_in(client, first)
        await send(client, conversation, "Remember: wage floors raise the earnings of low-paid workers")
        [(record,)] = await rows(client, "SELECT id FROM memory_records WHERE type != 'progress'")
        await idle(client, first)
        await background_idle(client, timeout=20)
        index = client.state["index"]
        scope = lambda: asyncio.to_thread(index._read, lambda conn: conn.execute(  # noqa: E731
            "SELECT scope FROM memory_rows WHERE memory_id = ?", (record,)).fetchall())
        assert await scope() == [(f"project:{first}",)]
        assert (await client.post(f"/api/conversations/{conversation}/move", json={"project_id": second})).status_code == 200
        await idle(client, second)
        await background_idle(client, timeout=20)
        assert await scope() == [(f"project:{second}",)]


async def test_a_record_a_command_or_an_extraction_writes_gets_its_vector(tmp_path):
    # S1-21a follow-up MB7: an index run follows a write (the queue never embeds a memory addition)
    from cited import Provider, cited
    from test_memory import candidate, extracted
    from test_search import idle
    provider = Provider()
    extracted(provider, candidate("our sample is thirty nurses", span="our sample is thirty nurses", kind="decision"))
    async with cited(tmp_path, provider) as client:
        project = await project_of(client)
        conversation = await conversation_in(client, project)
        await send(client, conversation, "Our sample is thirty nurses")  # the extraction writes it
        await background_idle(client, timeout=20)
        await send(client, conversation, "Remember: we decided on interviews")
        await idle(client, project)
        await background_idle(client, timeout=20)
        ids = [r for (r,) in await rows(client, "SELECT id FROM memory_records WHERE type IN ('decision', 'event')")]
        assert len(ids) == 2
        await idle(client, project)
        assert await asyncio.to_thread(client.state["index"].memory_embedded, ids) == set(ids)


async def test_a_proposal_an_extraction_confirms_gets_its_vector(tmp_path):
    # MB7, internal review: the index run follows any extraction that wrote or confirmed memory
    from cited import Provider, cited
    from test_memory import candidate, extracted
    from test_search import idle
    provider = Provider()
    extracted(provider, candidate("we sample thirty nurses", span="we sample thirty nurses"))
    async with cited(tmp_path, provider) as client:
        project = await project_of(client)
        proposal = await asyncio.to_thread(client.state["db"].write, lambda conn: memory.write(
            conn, project_id=project, scope="project", kind="decision", content="we sample thirty nurses",
            status="proposed", sources=[("project", project)]))
        await idle(client, project)
        await background_idle(client, timeout=20)
        await send(client, await conversation_in(client, project), "We sample thirty nurses")
        await background_idle(client, timeout=20)
        await idle(client, project)
        assert await rows(client, "SELECT status FROM memory_records WHERE id = ?", proposal) == [("confirmed",)]
        assert await asyncio.to_thread(client.state["index"].memory_embedded, [proposal]) == {proposal}


def _unindexed(project, content, scope="project"):
    """A record written as the queue alone knows of it (no index run asked for: a crash after the commit)."""
    return lambda conn: memory.write(conn, project_id=project, scope=scope, kind="decision", content=content,
                                     status="confirmed", sources=[("researcher", "views")])


async def _vectors(client, ids, timeout=20.0):
    """The ones of ids that have a vector, once no index run runs and they all have one, or the timeout passes."""
    from backend.search_index import Unavailable
    from test_search import runs_of
    deadline = asyncio.get_running_loop().time() + timeout
    while True:
        running = [r for r in await runs_of(client, "index") if r["status"] == "running"]
        try:
            held = await asyncio.to_thread(client.state["index"].memory_embedded, ids)
        except Unavailable:  # a launch's whole rebuild under way: no read is answered until it is done
            held = set()
        if (held == set(ids) and not running) or asyncio.get_running_loop().time() > deadline:
            return held
        await asyncio.sleep(0.05)


async def test_memory_in_a_project_without_papers_gets_its_vectors_once_the_model_is_installed(tmp_path):
    # M2 audit area 2: the install asks for the runs of the projects whose memory lacks vectors, not only of passages
    from backend.local_helper import EMBEDDING
    from test_local_helper import WEIGHTS
    from test_search import app
    async with app(tmp_path, install=False) as client:
        project = await project_of(client)
        record = await entered(client, project, "Wage floors raise the earnings of low-paid workers")
        personal = await entered(client, project, "I prefer short answers", kind="preference", scope="personal")
        await background_idle(client)
        ids = [record["id"], personal["id"]]
        assert await asyncio.to_thread(client.state["index"].memory_embedded, ids) == set()
        helper = client.state["local_helper"].helpers[EMBEDDING]
        helper.path.parent.mkdir(parents=True, exist_ok=True)
        helper.path.write_bytes(WEIGHTS)
        helper.model_installed()  # what a download or an import calls
        assert await _vectors(client, ids) == set(ids)


@pytest.mark.parametrize("lost", ["run_request", "index_file", "model_identity"])
async def test_a_launch_asks_for_the_runs_of_memory_the_index_lacks_vectors_for(tmp_path, monkeypatch, lost):
    # M2 audit area 2: a record whose index run was never asked for (the queue never embeds a memory addition, and its
    # opening pass prunes it), and every vector a whole rebuild or another model's identity drops, in a project with no
    # papers; a personal record whose project is gone is embedded by another project's run
    from test_search import app
    async with app(tmp_path) as client:
        project, other = await project_of(client, "First"), await project_of(client, "Other")
        if lost == "run_request":
            ids = [await write(client, _unindexed(project, "Wage floors raise the earnings of low-paid workers")),
                   await write(client, _unindexed(other, "I prefer short answers", scope="personal"))]
            await write(client, lambda conn: conn.execute(  # as a project's deletion leaves it
                "UPDATE memory_records SET project_id = NULL WHERE id = ?", (ids[1],)))
        else:
            ids = [(await entered(client, project, "Wage floors raise the earnings of low-paid workers"))["id"],
                   (await entered(client, other, "I prefer short answers", kind="preference", scope="personal"))["id"]]
            assert await _vectors(client, ids) == set(ids)
    if lost == "index_file":
        for found in (tmp_path / "data" / "index").glob("search.sqlite3*"):
            found.unlink()
    if lost == "model_identity":
        real = search.identity
        monkeypatch.setattr(search, "identity", lambda config: {**real(config), "revision": "another"})
    async with app(tmp_path) as client:
        assert await _vectors(client, ids) == set(ids)


async def test_a_launch_without_sqlite_vec_asks_for_no_memory_runs_and_the_next_with_it_does(tmp_path, monkeypatch):
    # internal review: no vector can be made while sqlite-vec does not load, so no run is asked for it at each launch
    from backend import search_index
    from test_search import app, runs_of
    real = search_index.sqlite_vec.loadable_path
    monkeypatch.setattr(search_index.sqlite_vec, "loadable_path", lambda: str(tmp_path / "missing" / "vec0"))
    async with app(tmp_path) as client:
        project = await project_of(client)
        ids = [await write(client, _unindexed(project, "Wage floors raise the earnings of low-paid workers"))]
    async with app(tmp_path) as client:
        await background_idle(client)
        assert await runs_of(client, "index", project) == []
    monkeypatch.setattr(search_index.sqlite_vec, "loadable_path", real)
    async with app(tmp_path) as client:
        assert await _vectors(client, ids) == set(ids)


@pytest.mark.parametrize("meanwhile", ["damaged_file", "damaged_file_rebuilt_before_the_harness", "model_installed"])
async def test_the_opening_rebuild_asks_for_the_runs_of_what_lacks_vectors_once_it_is_ready(tmp_path, monkeypatch,
                                                                                           meanwhile):
    # batch 1 (PR 60 rechecks): a launch's whole rebuild, held until the launch has asked for its runs, or (its internal
    # review) ending before the app holds its harness. A file damaged at open fails before sqlite-vec loads, which tells
    # nothing then; a model installed while it runs finds the index unreadable, and its end asks.
    import threading
    from backend.local_helper import EMBEDDING
    from backend.runs import Harness
    from backend.search_index import FILE, SearchIndex, Unavailable
    from test_local_helper import WEIGHTS
    from test_search import app, runs_of
    installed = meanwhile != "model_installed"
    async with app(tmp_path, install=installed) as client:
        project = await project_of(client)
        ids = [(await entered(client, project, "Wage floors raise the earnings of low-paid workers"))["id"]]
        await background_idle(client)
        await asyncio.to_thread(client.state["index"].apply)  # its addition has left the queue
        assert await asyncio.to_thread(client.state["index"].memory_embedded, ids) == (set(ids) if installed else set())
        before = len(await runs_of(client, "index", project))
    path = tmp_path / "data" / FILE
    for found in path.parent.glob(path.name + "*"):
        found.unlink()
    if meanwhile.startswith("damaged_file"):
        path.write_bytes(b"not a database " * 512)
    gate, real = threading.Event(), SearchIndex._rebuild_all

    def held(self):
        gate.wait(20)
        return real(self)
    monkeypatch.setattr(SearchIndex, "_rebuild_all", held)
    if meanwhile == "damaged_file_rebuilt_before_the_harness":
        recover = Harness.recover

        async def rebuilt_first(self, *args, **kwargs):  # start() stores the harness only after this
            gate.set()
            index, deadline = search._STATES[self]["index"], asyncio.get_running_loop().time() + 20
            while index.building:  # a failed rebuild leaves it set (its retry waiting): bounded, said
                assert asyncio.get_running_loop().time() < deadline, "the opening rebuild did not finish"
                await asyncio.sleep(0.01)
            return await recover(self, *args, **kwargs)
        monkeypatch.setattr(Harness, "recover", rebuilt_first)
    refused, unembedded = threading.Event(), SearchIndex.unembedded

    def read(self):  # the install's read of the index, refused while the rebuild runs
        try:
            return unembedded(self)
        except Unavailable:
            refused.set()
            raise
    monkeypatch.setattr(SearchIndex, "unembedded", read)
    try:
        async with app(tmp_path, install=installed) as client:  # the launch has asked for its runs
            if meanwhile == "model_installed":
                helper = client.state["local_helper"].helpers[EMBEDDING]
                helper.path.parent.mkdir(parents=True, exist_ok=True)
                helper.path.write_bytes(WEIGHTS)
                helper.model_installed()
                assert await asyncio.to_thread(refused.wait, 10)  # its request met the rebuild, before the release
            assert client.state["index"].building == (meanwhile != "damaged_file_rebuilt_before_the_harness")
            gate.set()
            assert await _vectors(client, ids) == set(ids)
            if installed:  # asked for once, at the launch: no rerun for the rebuild's end
                assert len(await runs_of(client, "index", project)) == before + 1
    finally:
        gate.set()


async def test_a_memory_record_the_helper_refuses_is_asked_for_again_only_once_its_text_or_the_model_changes(
        tmp_path, monkeypatch):
    # batch 1 (PR 60 recheck 2): its refusal is kept for its text, as a passage's is, so no launch asks for it again;
    # an edit (a new version) or another model asks once more. Recall reads it as without a vector throughout.
    from test_search import app, runs_of
    text = "Wage floors raise the earnings of low-paid workers"
    async with app(tmp_path) as client:
        client.remote.refuse = "Wage floors"
        project = await project_of(client)
        record = (await entered(client, project, text))["id"]
        await background_idle(client)
        assert [r["status"] for r in await runs_of(client, "index", project)] == ["succeeded"]
        assert await asyncio.to_thread(client.state["index"].memory_embedded, [record]) == set()
    for _ in range(2):
        async with app(tmp_path) as client:
            client.remote.refuse = "Wage floors"
            await background_idle(client)
            assert len(await runs_of(client, "index", project)) == 1
    real = search.identity
    monkeypatch.setattr(search, "identity", lambda config: {**real(config), "revision": "another"})
    async with app(tmp_path) as client:  # another model: asked for once more, and embedded this time
        assert await _vectors(client, [record]) == {record}
        assert len(await runs_of(client, "index", project)) == 2
        client.remote.refuse = "earnings"
        response = await client.patch(f"/api/memory/{record}", json={"version": 1, "content": text + " in 2024"})
        assert response.status_code == 200, response.text
        edited = response.json()["id"]
        await background_idle(client)
        assert await asyncio.to_thread(client.state["index"].memory_embedded, [edited]) == set()  # refused: no vector
    async with app(tmp_path) as client:  # the same model as the launch before
        client.remote.refuse = "earnings"
        await background_idle(client)
        assert len(await runs_of(client, "index", project)) == 3  # the edit's own run; the launch asks for none
        marks = lambda: asyncio.to_thread(client.state["index"]._read, lambda conn: conn.execute(  # noqa: E731
            "SELECT memory_id FROM memory_refused").fetchall())
        assert await marks() == [(edited,)]
        assert (await forget(client, edited)).json() == {"ok": True}
        assert await marks() == []  # the refusal's digest goes with the record


async def _general(client):
    return (await rows(client, "SELECT id FROM projects WHERE kind = 'general'"))[0][0]


@pytest.mark.parametrize("path", ["write", "install"])
async def test_a_personal_record_keeps_its_run_when_the_project_it_was_written_from_is_deleted(tmp_path, path):
    # batch 2 (PR 60 recheck round 2; design review E, Option 1): a personal record's run is the General project's,
    # which cannot be deleted; the project it was written from is deleted while the record's embedding is held
    from backend.local_helper import EMBEDDING
    from test_local_helper import WEIGHTS
    from test_search import app, runs_of
    async with app(tmp_path, install=path == "write") as client:
        client.remote.hold, client.remote.free = asyncio.Event(), 0
        project = await project_of(client)
        record = (await entered(client, project, "I prefer short answers", kind="preference", scope="personal"))["id"]
        if path == "install":
            helper = client.state["local_helper"].helpers[EMBEDDING]
            helper.path.parent.mkdir(parents=True, exist_ok=True)
            helper.path.write_bytes(WEIGHTS)
            helper.model_installed()
        await asyncio.wait_for(client.remote.reached.wait(), 10)  # its embedding asked for, and held
        assert (await client.delete(f"/api/projects/{project}")).status_code == 200
        client.remote.hold.set()
        assert await _vectors(client, [record]) == {record}
        assert [r["project_id"] for r in await runs_of(client, "index")] == [await _general(client)]


async def test_a_personal_record_keeps_its_run_when_its_project_goes_before_the_request(tmp_path, monkeypatch):
    # batch 2: the project it was written from is deleted between the derived question and the request (install path)
    from backend.local_helper import EMBEDDING
    from test_local_helper import WEIGHTS
    from test_search import app
    async with app(tmp_path, install=False) as client:
        project = await project_of(client)
        record = (await entered(client, project, "I prefer short answers", kind="preference", scope="personal"))["id"]
        request, deleted = search._request, []

        async def deleting_first(harness, project_id, rebuild=False):
            if not deleted:
                deleted.append(project_id)
                assert (await client.delete(f"/api/projects/{project}")).status_code == 200
            return await request(harness, project_id, rebuild)
        monkeypatch.setattr(search, "_request", deleting_first)
        helper = client.state["local_helper"].helpers[EMBEDDING]
        helper.path.parent.mkdir(parents=True, exist_ok=True)
        helper.path.write_bytes(WEIGHTS)
        helper.model_installed()
        assert await _vectors(client, [record]) == {record} and deleted


async def test_a_vector_counts_as_done_only_under_the_records_scope(tmp_path):
    # batch 2: a moved record keeps its vector under the old project's partition until a pass removes it
    from test_search import app
    async with app(tmp_path) as client:
        project = await project_of(client)
        record = await write(client, _unindexed(project, "Wage floors raise the earnings of low-paid workers"))
        index = client.state["index"]
        assert await asyncio.to_thread(index.store_memory, [(record, "project:another", [1.0] * 1024)]) == 1
        assert await asyncio.to_thread(index.memory_embedded, [record]) == {record}  # recall's reading, by id
        assert await asyncio.to_thread(index.memory_embedded, [record], {record: "project:another"}) == {record}
        assert await asyncio.to_thread(index.memory_embedded, [record], {record: f"project:{project}"}) == set()
        assert await asyncio.to_thread(search._memory_pending, index) == {project}


async def test_the_installs_requests_go_on_after_one_fails(tmp_path, monkeypatch):
    # batch 2: each project's request on its own
    from test_search import app
    async with app(tmp_path, install=False) as client:
        first, second = sorted([await project_of(client, "First"), await project_of(client, "Second")])
        for project in (first, second):
            await write(client, _unindexed(project, f"A decision of {project}"))
        asked = []

        async def failing_first(harness, project_id, rebuild=False):
            asked.append(project_id)
            if len(asked) == 1:
                raise RuntimeError("a request that fails")
        monkeypatch.setattr(search, "_request", failing_first)
        await search._embed_all(client.state)
        assert asked == [first, second]


async def test_a_record_moved_away_and_back_before_its_removal_applies_gets_its_vector_again(tmp_path):
    # batch 2, internal review F1: a queued removal deletes the vector by id, so a record moved back to its project
    # counts as lacking one until that removal applies; the move asks for its project's run
    from cited import cited
    from test_materials import added
    from test_search import WAGES, runs_of
    async with cited(tmp_path) as client:
        first, second = await project_of(client, "First"), await project_of(client, "Second")
        conversation = await conversation_in(client, first)
        await send(client, conversation, "Remember: wage floors raise the earnings of low-paid workers")
        [(record,)] = await rows(client, "SELECT id FROM memory_records WHERE type != 'progress'")
        await background_idle(client, timeout=20)
        assert await _vectors(client, [record]) == {record}
        client.remote.hold, client.remote.free = asyncio.Event(), len(client.remote.indexing)
        await added(client, second, WAGES)  # the second project's run, held on its paper's embedding
        await asyncio.wait_for(client.remote.reached.wait(), 10)
        for to in (second, first):  # its removals queued; the second project's run applies them only as it reruns
            response = await client.post(f"/api/conversations/{conversation}/move", json={"project_id": to})
            assert response.status_code == 200, response.text
        client.remote.hold.set()
        assert await _vectors(client, [record]) == {record}
        assert await asyncio.to_thread(client.state["index"]._read, lambda conn: conn.execute(
            "SELECT scope FROM memory_rows WHERE memory_id = ?", (record,)).fetchall()) == [(f"project:{first}",)]
        assert all(r["status"] == "succeeded" for r in await runs_of(client, "index"))


async def test_a_memory_write_asks_its_own_projects_run_and_the_general_projects_and_no_other(tmp_path):
    # batch 3 (design review E2, Option A; replaces batch 2's F2 test): another project's cancelled run stays so;
    # the General project's is asked at every memory write
    from test_search import app, runs_of
    async with app(tmp_path) as client:
        client.remote.hold, client.remote.free = asyncio.Event(), 0
        project, other = await project_of(client, "First"), await project_of(client, "Other")
        general = await _general(client)
        await entered(client, other, "We sample thirty nurses")
        await asyncio.wait_for(client.remote.reached.wait(), 10)
        [run] = [r for r in await runs_of(client, "index", other) if r["status"] == "running"]
        assert (await client.post(f"/api/runs/{run['run_id']}/cancel")).json()["status"] == "cancelled"
        client.remote.hold.set()
        await background_idle(client)
        asked = len(await runs_of(client, "index", general))
        record = (await entered(client, project, "We dropped the pilot"))["id"]
        assert await _vectors(client, [record]) == {record}
        assert len(await runs_of(client, "index", other)) == 1  # still the one cancelled
        assert len(await runs_of(client, "index", project)) == 1
        assert len(await runs_of(client, "index", general)) == asked + 1


@pytest.mark.parametrize("site", ["create", "edit", "keep", "move"])
async def test_a_memory_write_whose_request_goes_away_after_its_commit_still_asks_for_its_run(tmp_path, monkeypatch,
                                                                                              site):
    # batch 2, rider R1: "write, then ask" is followed through (as app._to_end), so a cancellation between the two
    # leaves the record's run asked for
    from cited import cited
    from test_search import runs_of
    async with cited(tmp_path) as client:
        project, other = await project_of(client, "First"), await project_of(client, "Second")
        conversation = await conversation_in(client, project)
        await send(client, conversation, "Remember: wage floors raise the earnings of low-paid workers")
        [(moved,)] = await rows(client, "SELECT id FROM memory_records WHERE type != 'progress'")
        await background_idle(client, timeout=20)
        proposal = await write(client, lambda conn: memory.write(
            conn, project_id=project, scope="project", kind="decision", content="We sample thirty nurses",
            status="proposed", sources=[("researcher", "views")]))
        before = len(await runs_of(client, "index"))
        real, outer = memory._embed_soon, {}

        async def going_away(state, project_id):
            outer["task"].cancel()  # the request goes away as its run is asked for
            await asyncio.sleep(0)
            await real(state, project_id)
        monkeypatch.setattr(memory, "_embed_soon", going_away)
        call = {"create": lambda: client.post("/api/memory", json={"project_id": project, "type": "decision",
                                                                    "content": "We dropped the pilot"}),
                "edit": lambda: client.patch(f"/api/memory/{moved}", json={"version": 1, "content": "Wage floors, revised"}),
                "keep": lambda: client.post("/api/memory/review", json={"id": proposal, "action": "keep", "version": 1}),
                "move": lambda: client.post(f"/api/conversations/{conversation}/move", json={"project_id": other})}[site]
        outer["task"] = asyncio.ensure_future(call())
        with pytest.raises(asyncio.CancelledError):
            await outer["task"]
        await asyncio.sleep(0.2)
        await background_idle(client, timeout=20)
        assert len(await runs_of(client, "index")) == before + 2  # the project's run and the General project's (batch 3)
        records = [r for (r,) in await rows(client, "SELECT id FROM memory_records WHERE status IN ('confirmed', 'inferred')"
                                                    " AND type = 'decision'")]
        assert await _vectors(client, records) == set(records)


def _personal_proposal(project, content="I prefer short answers"):
    return lambda conn: memory.write(conn, project_id=project, scope="personal", kind="preference", content=content,
                                     status="proposed", sources=[("researcher", "views")])


@pytest.mark.parametrize("by", ["command", "extraction"])
async def test_a_personal_proposal_confirmed_from_another_project_gets_its_vector(tmp_path, by):
    # batch 3 (recheck round 3, pass 2 finding 1): the record keeps the project it was proposed in; the write is in
    # another project, which asks its own run and the General project's
    from cited import Provider, cited
    from test_memory import candidate, extracted
    provider = Provider()
    if by == "extraction":
        extracted(provider, candidate("I prefer short answers", kind="preference", span="I prefer short answers"))
    async with cited(tmp_path, provider) as client:
        first, second = await project_of(client, "First"), await project_of(client, "Second")
        proposal = await write(client, _personal_proposal(first))
        await background_idle(client, timeout=20)
        text = "Remember: I prefer short answers" if by == "command" else "I prefer short answers"
        await send(client, await conversation_in(client, second), text)
        await background_idle(client, timeout=20)
        assert await rows(client, "SELECT status, project_id FROM memory_records WHERE id = ?", proposal) == [
            ("confirmed", first)]
        assert await _vectors(client, [proposal]) == {proposal}


def _deleting_on_ask(client, project):
    """memory._embed_soon, deleting the project first (a deletion between a write's commit and its request)."""
    real = memory._embed_soon

    async def ask(state, project_id):
        assert (await client.delete(f"/api/projects/{project}")).status_code == 200
        await real(state, project_id)
    return ask


@pytest.mark.parametrize("site", ["create", "edit", "keep"])
async def test_a_personal_record_whose_project_is_deleted_before_the_request_gets_its_vector(tmp_path, monkeypatch,
                                                                                              site):
    # batch 3 (recheck round 3, pass 1 finding 1 and pass 2 finding 1): the surviving record's project_id is NULL
    from test_search import app
    async with app(tmp_path) as client:
        project = await project_of(client)
        if site == "edit":
            record = (await entered(client, project, "I prefer short answers", kind="preference", scope="personal"))["id"]
            await background_idle(client)
        elif site == "keep":
            record = await write(client, _personal_proposal(project))
        monkeypatch.setattr(memory, "_embed_soon", _deleting_on_ask(client, project))
        response = {"create": lambda: client.post("/api/memory", json={"project_id": project, "type": "preference",
                                                                       "content": "I prefer short answers",
                                                                       "scope": "personal"}),
                    "edit": lambda: client.patch(f"/api/memory/{record}", json={"version": 1,
                                                                                "content": "I prefer brief answers"}),
                    "keep": lambda: client.post("/api/memory/review", json={"id": record, "action": "keep",
                                                                            "version": 1})}[site]()
        assert (await response).status_code in (200, 201)
        [(kept, scope, owner)] = await rows(client, "SELECT id, scope, project_id FROM memory_records"
                                                    " WHERE status = 'confirmed'")
        assert (scope, owner) == ("personal", None)
        assert await _vectors(client, [kept]) == {kept}


async def test_a_record_written_during_the_opening_rebuild_gets_its_vector(tmp_path, monkeypatch):
    # batch 3 (recheck round 3, both passes, finding 2): the file damaged at open (sqlite-vec not loaded yet), the model
    # installed, a record written while the rebuild runs, in a project with no run asked for
    import threading
    from backend.search_index import FILE, SearchIndex
    from test_search import app
    async with app(tmp_path):
        pass
    path = tmp_path / "data" / FILE
    for found in path.parent.glob(path.name + "*"):
        found.unlink()
    path.write_bytes(b"not a database " * 512)
    gate, real = threading.Event(), SearchIndex._rebuild_all

    def held(self):
        gate.wait(20)
        return real(self)
    monkeypatch.setattr(SearchIndex, "_rebuild_all", held)
    try:
        async with app(tmp_path) as client:
            project = await project_of(client)
            assert client.state["index"].building and not client.state["index"].vectors
            record = (await entered(client, project, "Wage floors raise the earnings of low-paid workers"))["id"]
            gate.set()
            assert await _vectors(client, [record]) == {record}
    finally:
        gate.set()


@pytest.mark.parametrize("site", ["edit", "keep"])
async def test_a_project_deleted_between_an_edit_and_its_reread_restarts_no_other_projects_cancelled_run(
        tmp_path, monkeypatch, site):
    # batch 3 (recheck round 3, both passes, finding 3): the reread finds project_id NULL; a write never names a third
    # project, whose cancelled run stays so
    from test_search import app, runs_of
    async with app(tmp_path) as client:
        project, other = await project_of(client, "First"), await project_of(client, "Other")
        if site == "edit":
            record = (await entered(client, project, "I prefer short answers", kind="preference", scope="personal"))["id"]
        else:
            record = await write(client, _personal_proposal(project))
        await background_idle(client)
        client.remote.hold, client.remote.free = asyncio.Event(), len(client.remote.indexing)
        await entered(client, other, "We sample thirty nurses")  # pending in Other, its run cancelled
        await asyncio.wait_for(client.remote.reached.wait(), 10)
        [run] = [r for r in await runs_of(client, "index", other) if r["status"] == "running"]
        assert (await client.post(f"/api/runs/{run['run_id']}/cancel")).json()["status"] == "cancelled"
        client.remote.hold.set()
        await background_idle(client)
        db, loop, real = client.state["db"], asyncio.get_running_loop(), client.state["db"].write

        def write_then_delete(fn, *args, **kwargs):  # the project deleted once the edit has committed, before the reread
            done = real(fn, *args, **kwargs)
            if getattr(fn, "__name__", "") in ("change", "answer"):
                asyncio.run_coroutine_threadsafe(client.delete(f"/api/projects/{project}"), loop).result(20)
            return done
        monkeypatch.setattr(db, "write", write_then_delete)
        response = await (client.patch(f"/api/memory/{record}", json={"version": 1, "content": "I prefer brief answers"})
                          if site == "edit" else
                          client.post("/api/memory/review", json={"id": record, "action": "keep", "version": 1}))
        monkeypatch.setattr(db, "write", real)
        assert response.status_code == 200, response.text
        await background_idle(client)
        assert len(await runs_of(client, "index", other)) == 1  # still the one cancelled
        [(kept,)] = await rows(client, "SELECT id FROM memory_records WHERE status = 'confirmed' AND scope = 'personal'")
        assert await _vectors(client, [kept]) == {kept}


async def test_a_memory_write_still_asks_the_general_projects_run_when_its_projects_request_fails(tmp_path, monkeypatch):
    # batch 3: each request on its own, the project's first
    from test_search import app
    async with app(tmp_path) as client:
        project, general = await project_of(client), await _general(client)
        asked = []

        async def failing_for_the_project(harness, project_id, rebuild=False):
            asked.append(project_id)
            if project_id == project:
                raise RuntimeError("a request that fails")
        monkeypatch.setattr(search, "_request", failing_for_the_project)
        await search.embed_memory_soon(client.state, project)
        assert asked == [project, general]
        asked.clear()
        await search.embed_memory_soon(client.state, general)  # the General project's own write: once
        assert asked == [general]


async def test_a_whole_rebuild_while_the_app_runs_asks_for_the_runs_of_memory_it_dropped(tmp_path):
    # M2 audit area 2: the rebuilt file's runs (index.rebuilt) take the projects whose memory lacks vectors too
    from test_search import app
    async with app(tmp_path) as client:
        project = await project_of(client)
        ids = [(await entered(client, project, "Wage floors raise the earnings of low-paid workers"))["id"]]
        assert await _vectors(client, ids) == set(ids)
        index = client.state["index"]
        await asyncio.to_thread(index._damaged)  # as a read that finds the file damaged
        deadline = asyncio.get_running_loop().time() + 10
        while index.building:
            assert asyncio.get_running_loop().time() < deadline
            await asyncio.sleep(0.02)
        assert await _vectors(client, ids) == set(ids)


async def test_a_helper_that_fails_on_a_memory_record_fails_the_run_and_retry_embeds_it(tmp_path):
    # M2 audit area 2: as with a passage, the run fails with the helper's reason, so Retry (or the next launch) embeds it
    from test_search import app, runs_of
    async with app(tmp_path) as client:
        client.remote.fail_on = "Wage floors"
        project = await project_of(client)
        record = await entered(client, project, "Wage floors raise the earnings of low-paid workers")
        await background_idle(client)
        [run] = await runs_of(client, "index", project)
        assert (run["status"], run["result"]["reason"], run["retryable"]) == ("failed", "request_failed", True)
        client.remote.fail_on = None
        again = await client.post(f"/api/runs/{run['run_id']}/retry")
        assert (await run_finished(client, again.json()["run_id"]))["status"] == "succeeded"
        assert await _vectors(client, [record["id"]]) == {record["id"]}


async def test_recall_with_no_record_embedded_yet_is_traced_as_the_newest(tmp_path):
    # M2 audit area 2: the nearest are none of the records (none has a vector yet): memory.ranked takes the newest,
    # and the trace says so
    from cited import cited
    from test_search import setting
    async with cited(tmp_path) as client:
        # recall's deadline covers the helper's start, cold here (no index run warms it): long enough for it, so the
        # reason is the one under test, never the deadline (batch 1)
        await setting(client, "retrieval.memory_ms", 30000)
        project = await project_of(client)
        await write(client, _unindexed(project, "We dropped the pilot because of cost."))
        stream = await send(client, await conversation_in(client, project), "What did we decide last time?")
        [(trace,)] = await rows(client, "SELECT data FROM run_events WHERE run_id = ? AND type = 'retrieval_trace'"
                                        " AND json_extract(data, '$.memory') IS NOT NULL", stream[0]["run_id"])
        trace = json.loads(trace)["memory"]
        assert (trace["mode"], trace.get("reason")) == ("recency", "unembedded") and len(trace["records"]) == 1


async def test_an_embedding_that_finishes_after_its_record_moved_never_writes_the_old_partition(tmp_path, monkeypatch):
    # PR 51 review 1 (P2 3): the vector is stored only while the record is still of the scope it was embedded for,
    # checked as it is written; the destination's own index run then embeds it under its partition
    from backend import local_helper
    from cited import cited
    from test_search import idle
    embed, moved = local_helper.embed, {}

    async def moving_meanwhile(state, texts, **kwargs):
        vectors = await embed(state, texts, **kwargs)
        if moved.get("conversation") and not moved.get("done") and "wage floors" in texts[0] \
                and not kwargs.get("query"):  # the record's own embedding, not a recall query
            moved["done"] = True  # the source project's run has its vector; the record moves before it is stored
            for _ in range(200):  # once the turn that saved it has ended (a running turn's conversation never moves)
                response = await moved["client"].post(f"/api/conversations/{moved['conversation']}/move",
                                                      json={"project_id": moved["to"]})
                if response.status_code != 409:
                    break
                await asyncio.sleep(0.05)
            assert response.status_code == 200, response.text
        return vectors
    async with cited(tmp_path) as client:
        first, second = await project_of(client, "First"), await project_of(client, "Second")
        conversation = await conversation_in(client, first)
        monkeypatch.setattr(local_helper, "embed", moving_meanwhile)
        moved.update(client=client, conversation=conversation, to=second)
        await send(client, conversation, "Remember: wage floors raise the earnings of low-paid workers")
        [(record,)] = await rows(client, "SELECT id FROM memory_records WHERE type != 'progress'")
        for _ in range(3):
            await idle(client, first)
            await idle(client, second)
            await background_idle(client, timeout=20)
        assert moved.get("done")
        index = client.state["index"]
        assert await asyncio.to_thread(index._read, lambda conn: conn.execute(
            "SELECT scope FROM memory_rows WHERE memory_id = ?", (record,)).fetchall()) == [(f"project:{second}",)]
