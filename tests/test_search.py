"""Search (S1-17; slice-1 spec F3a step 4, sections 4.3, 5, 7.2, 7.4, 10 and 14; tickets 17 and 71):
index runs, the search model's offer at a project's first material, hybrid search with its access
check, and the index's status and rebuild, on synthetic Markdown papers. Lifecycle cases first.

The helper is S1-16's test-owned stand-in for llama-server (test_local_helper.Fake: a small program,
allowed by the network block, that opens no socket). Its HTTP side and the model download source are
answered in process through the outbound gate's mock transport, with synthetic vectors (`vector`: a
text's tokens hashed into 1024 dimensions, so texts sharing words are near), never a model's.
"""

import asyncio
import contextlib
import json
from pathlib import Path

import httpx
import pytest

from backend import local_helper, search, search_index
from backend.local_helper import EMBEDDING, Config
from backend.search_index import SearchIndex
import synthetic_materials as synthetic
from network_guard import allow_subprocess
from scholia_app import read_in_process, run_finished, started
from test_local_helper import PIN, SLOT, TIMINGS, WEIGHTS, Fake, tokenizer
from test_materials import added, project_of, rows

pytestmark = pytest.mark.asyncio


vector = synthetic.embedding


class Remote:
    """Everything the gate lets out: the stand-in helper's HTTP side (health and embeddings, with the key
    of the server launched last), the model download source (`files`), and 404 for anything else (the
    scholarly sources). `embedded` keeps each embedding request's texts in order; while `hold` is set,
    indexing requests after the first `free` wait for it (`reached` set as one arrives); `failing`
    answers them 500; `slow_queries` delays a query's answer by that many seconds. Its tokenizer and
    slot are test_local_helper.tokenizer's: an input over the slot, or one containing `refuse`, is
    refused with HTTP 400 (`refused` keeps those texts); `tokenizing` answers the tokenizer 503."""

    def __init__(self, fake):
        self.fake = fake
        self.embedded, self.sources, self.files = [], [], {}
        self.hold, self.reached, self.free = None, asyncio.Event(), 0
        self.failing = False
        self.slow_queries = 0
        self.refuse, self.refused, self.counted, self.tokenizing, self.fail_on = None, [], [], None, None

    def key(self, port):
        launches = self.fake.launches
        return launches[port - 50001]["env"]["LLAMA_API_KEY"] if 0 < port - 50000 <= len(launches) else None

    @property
    def indexing(self):
        return [texts for texts in self.embedded if not any("Query:" in t for t in texts)]

    async def __call__(self, request):
        if request.url.host != "127.0.0.1":
            self.sources.append(str(request.url))
            status, body = self.files.get(str(request.url), (404, b""))
            return httpx.Response(status, content=body)
        if request.url.path == "/health":
            return httpx.Response(200, json={"status": "ok"})
        if request.headers.get("authorization") != f"Bearer {self.key(request.url.port)}":
            return httpx.Response(401, json={"error": "invalid api key"})
        body = json.loads(request.content)
        if request.url.path == "/tokenize":
            self.counted.append(body.get("content"))
            return httpx.Response(self.tokenizing) if self.tokenizing else tokenizer(request.url.path, body)
        if (refused := tokenizer(request.url.path, body, refuse=self.refuse)) is not None:
            self.refused.append(body["input"])
            return refused
        texts = body["input"]
        if self.fail_on is not None and any(self.fail_on in text for text in texts):
            return httpx.Response(500, json={"error": "failed"})
        self.embedded.append(texts)
        query = any("Query:" in t for t in texts)
        if query and self.slow_queries:
            await asyncio.sleep(self.slow_queries)
        if not query and self.hold is not None and len(self.indexing) > self.free:
            self.reached.set()
            await self.hold.wait()
        if self.failing:
            return httpx.Response(500, json={"error": "failed"})
        return httpx.Response(200, json={"data": [{"index": i, "embedding": synthetic.embedding(t)}
                                                  for i, t in enumerate(texts)]})


@pytest.fixture(autouse=True)
def timings(monkeypatch):
    monkeypatch.setattr(local_helper.Local, "timings", lambda self: dict(TIMINGS))


@contextlib.asynccontextmanager
async def app(tmp_path, *, install=True, batch=None, data=None):
    """The app with the stand-in helper and, with install, the search model's file in place."""
    data = data or tmp_path / "data"
    path = data / "models" / EMBEDDING / PIN["file"]
    if install and not path.exists():
        path.parent.mkdir(parents=True)
        path.write_bytes(WEIGHTS)
    bundle = tmp_path / "bundle"
    fake = Fake(bundle) if not bundle.exists() else _existing(bundle)
    remote = Remote(fake)
    with allow_subprocess(str(fake.binary)):
        async with started(data, remote, setup=False, helper=Config(binary=fake.binary, models={EMBEDDING: PIN})) as client:
            client.remote = remote
            if batch is not None:
                await setting(client, "retrieval.embedding_batch", batch)
            yield client


def _existing(bundle):
    fake = Fake.__new__(Fake)
    fake.contents = bundle / "Scholia.app" / "Contents"
    fake.binary = fake.contents / "MacOS" / "llama-server"
    fake.control = bundle / "control"
    return fake


async def setting(client, key, value):
    current = (await client.get("/api/settings")).json()
    response = await client.put("/api/settings", json={"hash": current["hash"], "updates": {key: value}})
    assert response.status_code == 200, response.text


def paper(title, *paragraphs, section="Findings"):
    """A synthetic Markdown paper with no identifier: its title, one section and its paragraphs."""
    return f"{title}.md", (f"# {title}\n\n## {section}\n\n" + "\n\n".join(paragraphs) + "\n").encode()


WAGES = paper("Wage Floors", "Minimum wages raise the earnings of low-paid workers in the synthetic panel.",
              "Employment effects are small in most specifications of the synthetic panel.",
              "Regional prices adjust slowly after a wage floor is raised.")
CHINESE = paper("最低工资研究", "最低工资提高了低收入工人的收入。", "就业效应在大多数设定中很小。", section="研究发现")


async def idle(client, project, timeout=20.0):
    """The project's index status once none of its readings or index runs is running."""
    deadline = asyncio.get_running_loop().time() + timeout
    while True:
        running = [r for r in (await client.get("/api/activity")).json()["runs"]
                   if r["project_id"] == project and r["status"] == "running" and r["workflow"] in ("extract", "index")]
        if not running:
            return (await client.get(f"/api/projects/{project}/index")).json()
        assert asyncio.get_running_loop().time() < deadline, running
        await asyncio.sleep(0.02)


async def find(client, project, query, **extra):
    response = await client.post(f"/api/projects/{project}/search", json={"query": query, **extra})
    assert response.status_code == 200, response.text
    return response.json()


async def index_rows(client, project=None):
    index = client.state["index"]
    return await asyncio.to_thread(index._read, lambda conn: conn.execute(
        "SELECT passage_id, material_id, kind, embedded FROM index_rows WHERE ?1 IS NULL OR project_id = ?1 ORDER BY id",
        (project,)).fetchall())


async def offers(client, project):
    return [a for a in (await client.get("/api/asks", params={"project_id": project})).json()["asks"]
            if a["kind"] == "model_download"]


async def runs_of(client, workflow, project=None):
    return [r for r in (await client.get("/api/activity", params={"limit": 200})).json()["runs"]
            if r["workflow"] == workflow and (project is None or r["project_id"] == project)]


# Lifecycle: cancellation and retries


async def test_cancelling_an_index_run_keeps_its_committed_batches_and_retry_embeds_only_the_rest(tmp_path):
    async with app(tmp_path, batch=2) as client:
        client.remote.hold, client.remote.free = asyncio.Event(), 3  # the first batch of 2 commits; the 4th waits
        project = await project_of(client)
        [material] = (await added(client, project, WAGES))["materials"]
        await asyncio.wait_for(client.remote.reached.wait(), 10)
        [run] = [r for r in await runs_of(client, "index", project) if r["status"] == "running"]
        assert run["materials"] == {"titles": [], "count": 0}  # a project's run: it names no papers
        assert (await client.post(f"/api/runs/{run['run_id']}/cancel")).json()["status"] == "cancelled"
        assert (await client.post(f"/api/runs/{run['run_id']}/cancel")).json()["status"] == "cancelled"  # again: safe
        stored = [r for r in await index_rows(client, project) if r[3]]
        assert len(stored) == 2  # the committed batch stays; the partial one is not written
        ended = await run_finished(client, run["run_id"])
        assert ended["retryable"] and ended["cost_usd"] in (0, None)
        sent = len(client.remote.indexing)
        client.remote.hold.set()
        again = await client.post(f"/api/runs/{run['run_id']}/retry")
        assert again.status_code == 201
        assert (await run_finished(client, again.json()["run_id"]))["status"] == "succeeded"
        status = await idle(client, project)
        embeddable = status["passages"]["embeddable"]
        assert status["passages"]["embedded"] == embeddable == status["materials"][material["id"]]["embeddable"]
        assert len(client.remote.indexing) - sent == embeddable - 2  # only what was missing


async def test_a_helper_that_fails_ends_the_run_with_its_reason_and_the_paper_stays_keyword_searchable(tmp_path):
    async with app(tmp_path) as client:
        client.remote.failing = True
        project = await project_of(client)
        await added(client, project, WAGES)
        await idle(client, project)
        [run] = await runs_of(client, "index", project)
        assert (run["status"], run["result"]["reason"], run["retryable"]) == ("failed", "request_failed", True)
        found = await find(client, project, "earnings")
        assert [r["title"] for r in found["results"]] == ["Wage Floors"]
        assert found["coverage"] == {"embedded": 0, "total": 4, "refused": 0}
        client.remote.failing = False
        again = await client.post(f"/api/runs/{run['run_id']}/retry")
        assert (await run_finished(client, again.json()["run_id"]))["status"] == "succeeded"
        assert (await idle(client, project))["passages"]["embedded"] == 4


# Lifecycle: deletion while indexing runs


@pytest.mark.parametrize("deleted", ["material", "project"])
async def test_deleting_mid_embedding_sends_nothing_more_of_it_and_keeps_nothing(tmp_path, deleted):
    """A paper's deletion leaves its project's run running: its passages are refused at their requests
    and its rows go with the deletion's cleanup. A project's deletion takes its run with it."""
    async with app(tmp_path, batch=1) as client:
        client.remote.hold = asyncio.Event()
        project = await project_of(client)
        [material] = (await added(client, project, WAGES))["materials"]
        await asyncio.wait_for(client.remote.reached.wait(), 10)
        [run] = [r for r in await runs_of(client, "index", project) if r["status"] == "running"]
        held = len(client.remote.indexing)
        path = f"/api/materials/{material['id']}" if deleted == "material" else f"/api/projects/{project}"
        assert (await client.delete(path)).status_code == 200
        client.remote.hold.set()
        await asyncio.sleep(0.2)
        assert len(client.remote.indexing) == held  # no request after the deletion: the held one was in flight
        assert await index_rows(client) == []  # its rows and vectors went with the deletion's cleanup
        if deleted == "material":
            ended = await run_finished(client, run["run_id"])
            assert ended["status"] == "succeeded" and ended["result"]["passages"] == {"indexed": 0, "embedded": 0,
                                                                                    "embeddable": 0, "refused": 0}
            assert (await find(client, project, "earnings"))["results"] == []


async def test_a_paper_deleted_while_its_project_rebuilds_leaves_the_rebuild_to_embed_the_rest(tmp_path):
    async with app(tmp_path, batch=1) as client:
        project = await project_of(client)
        first = (await added(client, project, WAGES))["materials"][0]
        second = (await added(client, project, paper("Second Paper", "A paragraph about synthetic cities.")))["materials"][0]
        await idle(client, project)
        client.remote.hold, client.remote.free = asyncio.Event(), len(client.remote.indexing)
        response = await client.post(f"/api/projects/{project}/index/rebuild")
        assert response.status_code == 202
        rebuild = response.json()["run_id"]
        await asyncio.wait_for(client.remote.reached.wait(), 10)
        assert (await client.delete(f"/api/materials/{first['id']}")).status_code == 200
        client.remote.hold.set()
        ended = await run_finished(client, rebuild)
        assert (ended["status"], ended["result"]["rebuilt"]) == ("succeeded", True)
        status = await idle(client, project)
        assert list(status["materials"]) == [second["id"]]
        assert status["passages"]["embedded"] == status["passages"]["embeddable"] > 0
        after = [r for r in await runs_of(client, "index", project) if r["started_at"] >= ended["started_at"]]
        assert len(after) <= 2 and all(r["status"] == "succeeded" for r in after)  # the deletion's request: one rerun at most


async def test_a_passage_deleted_between_retrieval_and_fetch_is_dropped(tmp_path, monkeypatch):
    async with app(tmp_path) as client:
        project = await project_of(client)
        [material] = (await added(client, project, WAGES))["materials"]
        await idle(client, project)
        [hit] = [r["passage_id"] for r in (await find(client, project, "regional prices"))["results"]][:1]
        real = SearchIndex.keyword

        def keyword(self, project_id, query, limit):
            found = real(self, project_id, query, limit)
            client.state["db"].write(lambda conn: conn.execute(  # deleted after retrieval, before the access check
                "UPDATE material_versions SET is_current = 0 WHERE material_id = ?", (material["id"],)))
            return found
        monkeypatch.setattr(SearchIndex, "keyword", keyword)
        found = await find(client, project, "regional prices")
        assert hit not in [r["passage_id"] for r in found["results"]] and found["results"] == []


# Lifecycle: a change of level or lock between start and dispatch


@pytest.mark.parametrize("change", ["local_only", "review_lock"])
async def test_a_stricter_level_or_the_review_lock_leaves_indexing_running_and_local(tmp_path, change):
    async with app(tmp_path, batch=1) as client:
        client.remote.hold = asyncio.Event()
        project = await project_of(client)
        await added(client, project, WAGES)
        await asyncio.wait_for(client.remote.reached.wait(), 10)
        if change == "local_only":
            response = await client.post(f"/api/projects/{project}/sensitivity", json={"level": "local_only"})
        else:
            response = await client.post(f"/api/projects/{project}/review-lock", json={"locked": True, "venue": "APA"})
        assert response.status_code == 200, response.text
        client.remote.hold.set()
        status = await idle(client, project)
        assert status["passages"]["embedded"] == status["passages"]["embeddable"] == 4
        [run] = await runs_of(client, "index", project)
        assert run["status"] == "succeeded"
        decisions = await rows(client, "SELECT json_extract(data, '$.kind'), json_extract(data, '$.decision'),"
                                       " json_extract(data, '$.sensitivity') FROM audit_log WHERE event = 'outbound'"
                                       " AND project_id = ?", project)
        assert {kind for kind, _, _ in decisions} == {"local_helper"} and all(d == "allow" for _, d, _ in decisions)
        assert "local_only" in {level for _, _, level in decisions}


async def test_a_stricter_level_revokes_an_unanswered_offer_and_nothing_is_downloaded(tmp_path):
    async with app(tmp_path, install=False) as client:
        project = await project_of(client)
        await added(client, project, WAGES)
        [ask] = await until_offer(client, project)
        response = await client.post(f"/api/projects/{project}/sensitivity", json={"level": "local_only"})
        assert response.status_code == 200
        refused = await client.post(f"/api/runs/{ask['run_id']}/asks/{ask['ask_id']}", json={"option": "modelscope"})
        assert refused.json()["code"] in ("ask_closed", "ask_invalid")
        ended = await run_finished(client, ask["run_id"])
        assert (ended["status"], ended["cancel_reason"]) == ("cancelled", "revoked")
        assert client.remote.sources == [] and client.state["local_helper"].download is None


async def until_offer(client, project, timeout=15.0):
    deadline = asyncio.get_running_loop().time() + timeout
    while not (found := await offers(client, project)):
        assert asyncio.get_running_loop().time() < deadline
        await asyncio.sleep(0.02)
    return found


# Lifecycle: a commit followed by a failed cleanup or report


async def test_a_deletion_whose_index_cleanup_fails_keeps_its_removals_and_search_excludes_the_paper(tmp_path, monkeypatch):
    """The pass rolls back and its removals stay queued; it is tried again soon, with no other work, until
    they are applied."""
    import backend.search_index as search_index
    monkeypatch.setattr(search_index, "RETRY_SECONDS", (0.05, 0.05))
    async with app(tmp_path) as client:
        project = await project_of(client)
        [material] = (await added(client, project, WAGES))["materials"]
        await idle(client, project)
        real = SearchIndex._verify
        monkeypatch.setattr(SearchIndex, "_verify", lambda self, rowids: (_ for _ in ()).throw(RuntimeError("disk")))
        assert (await client.delete(f"/api/materials/{material['id']}")).status_code == 200
        await asyncio.sleep(0.2)  # tried again meanwhile, and failing still
        assert len(await index_rows(client, project)) == 4  # the pass rolled back: still there
        assert await rows(client, "SELECT count(*) FROM index_queue WHERE op = 'remove'") == [(4,)]  # still queued
        assert (await find(client, project, "earnings"))["results"] == []  # the access check keeps it out
        monkeypatch.setattr(SearchIndex, "_verify", real)
        deadline = asyncio.get_running_loop().time() + 5
        while await index_rows(client, project) or await rows(client, "SELECT count(*) FROM index_queue") != [(0,)]:
            assert asyncio.get_running_loop().time() < deadline  # no pass, deletion or launch called for
            await asyncio.sleep(0.02)


async def test_an_index_commit_whose_terminal_write_fails_reads_interrupted_and_a_restart_adds_no_duplicate(tmp_path,
                                                                                                         monkeypatch):
    from backend.runs import Harness
    real = Harness._finish_local

    def failing(self, conn, active, status, cancel_reason, summary, effect=None):
        if conn.execute("SELECT workflow FROM runs WHERE id = ?", (active.run_id,)).fetchone()[0] == "index":
            raise OSError("the disk is full")
        return real(self, conn, active, status, cancel_reason, summary, effect)

    async with app(tmp_path) as client:
        project = await project_of(client)
        monkeypatch.setattr(Harness, "_finish_local", failing)
        await added(client, project, WAGES)
        deadline = asyncio.get_running_loop().time() + 10
        while not [r for r in await runs_of(client, "index", project) if r["status"] == "interrupted"]:
            assert asyncio.get_running_loop().time() < deadline
            await asyncio.sleep(0.02)
        before = await index_rows(client, project)
        assert len(before) == 4 and all(r[3] for r in before if r[2] != "reference")
        monkeypatch.setattr(Harness, "_finish_local", real)
        sent = len(client.remote.indexing)
    async with app(tmp_path) as client:  # the next launch records it interrupted and runs afresh: its work was done
        status = await idle(client, project)
        assert await index_rows(client, project) == before and status["passages"]["embedded"] == 4
        assert client.remote.indexing == [] and sent == 4
        assert sorted(r["status"] for r in await runs_of(client, "index", project)) == ["interrupted", "succeeded"]


async def test_an_answered_offer_that_crashed_after_starting_its_download_never_starts_a_second(tmp_path):
    async with app(tmp_path, install=False) as client:
        project = await project_of(client)
        await added(client, project, WAGES)
        [ask] = await until_offer(client, project)
        run_id = ask["run_id"]
        harness = client.state["harness"]
        active = harness.registry.runs[run_id]
        harness._request_cancel(active, "shutdown")  # stopped as at a crash: still running in the record
        await asyncio.wait({active.task})

        def answered_and_started(conn):  # as a crash right after the start was recorded leaves it
            conn.execute("INSERT INTO run_events (run_id, seq, type, data) VALUES (?, 90, 'ask_answered', ?)",
                         (run_id, json.dumps({"ask_id": ask["ask_id"], "by": "researcher", "option": "modelscope"})))
            conn.execute("INSERT INTO run_events (run_id, seq, type, data) VALUES (?, 91, 'step_started', ?)",
                         (run_id, json.dumps({"download": "modelscope"})))
            conn.execute("UPDATE runs SET waiting = NULL WHERE id = ?", (run_id,))
        await asyncio.to_thread(client.state["db"].write, answered_and_started)
    async with app(tmp_path, install=False) as client:
        ended = await run_finished(client, run_id)  # its download never ran: it says so, and starts none
        assert (ended["status"], ended["result"]) == ("succeeded", {"answer": "modelscope", "outcome": "start_interrupted"})
        assert client.remote.sources == [] and client.state["local_helper"].download is None


# The offer at a project's first material (ticket 71's acceptance check)


@pytest.mark.parametrize("level", ["normal", "private"])
async def test_the_first_material_raises_one_offer_and_nothing_is_sent_before_the_answer(tmp_path, level):
    async with app(tmp_path, install=False) as client:
        project = await project_of(client, level=level)
        await added(client, project, WAGES, paper("Two", "Another paragraph."), paper("Three", "A third one."))
        [ask] = await until_offer(client, project)
        assert ask["options"] == ["huggingface", "modelscope", "later"] and ask["text_box"] is False
        assert (ask["project_id"], ask["params"]) == (project, {"model": EMBEDDING})
        await idle(client, project)
        await added(client, project, paper("Four", "A fourth paragraph."))
        await idle(client, project)
        assert len(await offers(client, project)) == 1 and len(await runs_of(client, "model_offer", project)) == 1
        assert client.remote.sources == []  # nothing probed before the answer
        assert await rows(client, "SELECT count(*) FROM audit_log WHERE event = 'outbound'"
                                  " AND json_extract(data, '$.kind') = 'model_download'") == [(0,)]
        found = await find(client, project, "earnings")
        assert (found["mode"], found["reason"]) == ("keyword_only", "model_missing") and found["results"]


async def test_an_offer_shows_in_the_conversation_its_file_was_attached_in(tmp_path):
    async with app(tmp_path, install=False) as client:
        project = await project_of(client)
        conversation = (await client.post("/api/conversations", json={"project_id": project})).json()["id"]
        await added(client, project, WAGES, conversation_id=conversation)
        await until_offer(client, project)
        shown = (await client.get("/api/asks", params={"conversation_id": conversation})).json()["asks"]
        assert [a["kind"] for a in shown] == ["model_download"]


async def test_a_local_only_project_gets_no_offer_and_searches_by_keyword(tmp_path):
    async with app(tmp_path, install=False) as client:
        project = await project_of(client, level="local_only")
        await added(client, project, WAGES)
        await idle(client, project)
        await asyncio.sleep(0.1)
        assert await runs_of(client, "model_offer") == [] and await offers(client, project) == []
        found = await find(client, project, "earnings")
        assert (found["mode"], found["reason"]) == ("keyword_only", "model_missing") and found["results"]
        [run] = await runs_of(client, "index", project)
        assert (run["status"], run["result"]["mode"], run["result"]["reason"]) == ("succeeded", "keyword_only", "model_missing")
        assert run["result"]["passages"] == {"indexed": 4, "embedded": 0, "embeddable": 4, "refused": 0}


async def test_later_sends_nothing_and_is_not_asked_again_in_that_project(tmp_path):
    async with app(tmp_path, install=False) as client:
        project, other = await project_of(client, "One"), await project_of(client, "Two")
        await added(client, project, WAGES)
        [ask] = await until_offer(client, project)
        answered = await client.post(f"/api/runs/{ask['run_id']}/asks/{ask['ask_id']}", json={"option": "later"})
        assert answered.status_code == 200
        assert (await run_finished(client, ask["run_id"]))["result"] == {"answer": "later"}
        twice = await client.post(f"/api/runs/{ask['run_id']}/asks/{ask['ask_id']}", json={"option": "modelscope"})
        assert twice.json()["code"] == "ask_closed"
        await added(client, project, paper("Later Paper", "Text."))
        await idle(client, project)
        assert await offers(client, project) == [] and len(await runs_of(client, "model_offer", project)) == 1
        await added(client, other, paper("Other Paper", "Text."))
        await until_offer(client, other)  # another project is asked on its own
        assert client.remote.sources == []
        audit = await rows(client, "SELECT data FROM audit_log WHERE event = 'ask_answered'")
        assert [json.loads(d) for (d,) in audit] == [{"question": "model_download", "answer": "later"}]


async def test_a_download_answer_starts_one_download_and_the_index_embeds_once_the_model_is_installed(tmp_path):
    async with app(tmp_path, install=False) as client:
        url = PIN["sources"]["modelscope"]["url"]
        client.remote.files[url] = (200, WEIGHTS)
        project, other = await project_of(client, "One"), await project_of(client, "Two")
        await added(client, project, WAGES)
        await added(client, other, CHINESE)
        [ask] = await until_offer(client, project)
        [their_ask] = await until_offer(client, other)
        response = await client.post(f"/api/runs/{ask['run_id']}/asks/{ask['ask_id']}", json={"option": "modelscope"})
        assert response.status_code == 200
        ended = await run_finished(client, ask["run_id"])
        assert ended["result"] == {"answer": "modelscope", "outcome": "started"}
        # The other project's offer, no longer needed, closes without a second download.
        theirs = await run_finished(client, their_ask["run_id"])
        assert theirs["status"] == "succeeded" and theirs["result"]["outcome"] == "not_needed"
        deadline = asyncio.get_running_loop().time() + 10
        while not (await client.get("/api/helper")).json()["models"][0]["installed"]:
            assert asyncio.get_running_loop().time() < deadline
            await asyncio.sleep(0.02)
        assert client.remote.sources == [url]  # one download
        assert (await client.get("/api/helper")).json()["model_source"] == "modelscope"
        for each in (project, other):
            deadline = asyncio.get_running_loop().time() + 10
            while (status := await idle(client, each))["passages"]["embedded"] < status["passages"]["embeddable"]:
                assert asyncio.get_running_loop().time() < deadline
                await asyncio.sleep(0.02)
        downloads = await rows(client, "SELECT p.kind FROM audit_log a JOIN projects p ON p.id = a.project_id"
                                       " WHERE a.event = 'outbound' AND json_extract(a.data, '$.kind') = 'model_download'")
        assert downloads == [("general",)]


async def test_an_answer_while_a_download_runs_starts_nothing_and_says_why(tmp_path, monkeypatch):
    async with app(tmp_path, install=False) as client:
        project = await project_of(client)
        await added(client, project, WAGES)
        [ask] = await until_offer(client, project)
        local = client.state["local_helper"]
        monkeypatch.setattr(local, "start_download", _refusing("download_running"))
        await client.post(f"/api/runs/{ask['run_id']}/asks/{ask['ask_id']}", json={"option": "huggingface"})
        ended = await run_finished(client, ask["run_id"])
        assert ended["result"] == {"answer": "huggingface", "outcome": "download_running"}


def _refusing(code):
    async def refuse(*args, **kwargs):
        raise local_helper.Refused(409, code, "refused")
    return refuse


async def test_cancelling_the_offer_closes_its_question_and_it_may_be_asked_again_later(tmp_path):
    async with app(tmp_path, install=False) as client:
        project = await project_of(client)
        await added(client, project, WAGES)
        [ask] = await until_offer(client, project)
        assert (await client.post(f"/api/runs/{ask['run_id']}/cancel")).json()["status"] == "cancelled"
        assert await offers(client, project) == []
        ended = await run_finished(client, ask["run_id"])
        assert not ended["retryable"]
        await added(client, project, paper("Next", "Text."))
        await until_offer(client, project)
        assert client.remote.sources == []


# Search


async def test_search_finds_english_and_chinese_passages_by_keyword_and_meaning_in_rank_order(tmp_path):
    async with app(tmp_path) as client:
        project = await project_of(client)
        [wages] = (await added(client, project, WAGES))["materials"]
        await added(client, project, CHINESE)
        await idle(client, project)
        found = await find(client, project, "low-paid workers earnings")
        assert found["mode"] == "hybrid" and found["coverage"]["embedded"] == found["coverage"]["total"]
        top = found["results"][0]
        assert top["text"].startswith("Minimum wages raise") and top["material_id"] == wages["id"]
        assert top["section_path"] == ["Findings"] and top["title"] == "Wage Floors" and "score" not in top
        assert {"passage_id", "version_id", "ordinal", "page", "kind"} <= set(top)
        chinese = await find(client, project, "低收入工人")
        assert chinese["results"][0]["text"] == "最低工资提高了低收入工人的收入。"
        keyword = client.state["index"].keyword
        assert len(await asyncio.to_thread(keyword, project, "就", 50)) == 1  # a lone character: the bigram it starts


async def test_a_title_word_finds_its_papers_passages_and_a_new_title_replaces_the_old(tmp_path):
    async with app(tmp_path) as client:
        project = await project_of(client)
        file = ("quixotic ledger.md", b"# Plain Heading\n\n## Part\n\nPlain text with nothing special.\n")
        [material] = (await added(client, project, file))["materials"]
        await idle(client, project)
        keyword = client.state["index"].keyword
        assert len(await asyncio.to_thread(keyword, project, "quixotic", 50)) == 2  # every passage, by its title
        sent = len(client.remote.indexing)
        response = await client.patch(f"/api/materials/{material['id']}", json={"title": "Zephyr Accounts"})
        assert response.status_code == 200
        status = await idle(client, project)
        assert status["passages"]["embedded"] == status["passages"]["embeddable"] == 2
        assert len(client.remote.indexing) - sent == 2  # embedded again with the new title
        assert len(await asyncio.to_thread(keyword, project, "zephyr", 50)) == 2
        assert await asyncio.to_thread(keyword, project, "quixotic", 50) == []
        assert [r["title"] for r in (await find(client, project, "zephyr"))["results"]] == ["Zephyr Accounts"] * 2


async def test_reference_passages_are_indexed_but_never_found_nor_embedded(tmp_path):
    references = "\n\n".join(f"- Author {i}. (2020). A synthetic reference about wages, number {i}." for i in range(80))
    file = ("refs.md", (f"# Wage Notes\n\n## Results\n\nWages rose in the synthetic panel.\n\n"
                        f"## References\n\n{references}\n").encode())
    async with app(tmp_path) as client:
        project = await project_of(client)
        await added(client, project, file)
        await idle(client, project)
        kinds = [r[2] for r in await index_rows(client, project)]
        assert kinds.count("reference") >= 1 and all(not r[3] for r in await index_rows(client, project) if r[2] == "reference")
        found = await find(client, project, "wages synthetic", limit=50)
        assert found["results"] and all(r["kind"] != "reference" for r in found["results"])
        assert found["results"][0]["text"] == "Wages rose in the synthetic panel."


async def test_replacing_a_file_takes_the_old_versions_text_out_of_search_and_the_index(tmp_path):
    async with app(tmp_path, install=False) as client:  # keyword search only: nearest neighbours always answer
        project = await project_of(client)
        [material] = (await added(client, project, paper("Report", "The obsolete marmalade finding.")))["materials"]
        await idle(client, project)
        assert (await find(client, project, "marmalade"))["results"]
        await added(client, project, paper("Report", "The current finding about bread."), material_id=material["id"])
        await idle(client, project)
        assert (await find(client, project, "marmalade"))["results"] == []
        assert [r["text"] for r in (await find(client, project, "bread"))["results"]] == ["The current finding about bread."]
        texts = await asyncio.to_thread(client.state["index"]._read, lambda conn: conn.execute(
            "SELECT text FROM fts_passages").fetchall())
        assert not any("marmalade" in text for (text,) in texts)


async def test_a_file_shared_by_two_projects_has_rows_in_each_and_deleting_one_leaves_the_other(tmp_path):
    async with app(tmp_path) as client:
        mine, theirs = await project_of(client, "Mine"), await project_of(client, "Theirs")
        [paper_mine] = (await added(client, mine, WAGES))["materials"]
        await added(client, theirs, WAGES)
        await idle(client, mine)
        await idle(client, theirs)
        assert len(await index_rows(client, mine)) == len(await index_rows(client, theirs)) == 4
        assert (await client.delete(f"/api/materials/{paper_mine['id']}")).status_code == 200
        assert await index_rows(client, mine) == [] and len(await index_rows(client, theirs)) == 4
        assert (await find(client, theirs, "earnings"))["results"]


async def test_search_refuses_blank_input_an_unknown_project_and_limits_out_of_bounds(tmp_path):
    async with app(tmp_path) as client:
        project = await project_of(client)
        for query in ("", "   ", "​⁠"):
            response = await client.post(f"/api/projects/{project}/search", json={"query": query})
            assert (response.status_code, response.json()["code"]) == (400, "query_needed")
        missing = await client.post("/api/projects/00000000-0000-4000-8000-000000000000/search", json={"query": "x"})
        assert (missing.status_code, missing.json()["code"]) == (404, "not_found")
        for limit in (0, 51):
            response = await client.post(f"/api/projects/{project}/search", json={"query": "x", "limit": limit})
            assert (response.status_code, response.json()["code"]) == (400, "invalid_request")
        long = await client.post(f"/api/projects/{project}/search", json={"query": "x" * 1001})
        assert (long.status_code, long.json()["code"]) == (400, "invalid_request")


async def test_typed_fts_syntax_is_only_text(tmp_path):
    async with app(tmp_path) as client:
        project = await project_of(client)
        await added(client, project, paper("Operators", 'Text with NEAR and OR and "quotes" in it.'))
        await idle(client, project)
        for query in ('NEAR(text', '"unbalanced', "text*", "a OR", "-text", "(", "^text", "col:text"):
            assert (await find(client, project, query))["results"], query


# The index's status and rebuild


async def test_the_index_status_and_a_rebuild_read_back(tmp_path):
    async with app(tmp_path) as client:
        project = await project_of(client)
        [material] = (await added(client, project, WAGES))["materials"]
        status = await idle(client, project)
        assert (status["state"], status["mode"], status["reason"]) == ("ready", "hybrid", None)
        assert status["passages"] == {"indexed": 4, "embedded": 4, "embeddable": 4, "refused": 0}
        assert status["materials"] == {material["id"]: {"indexed": 4, "embedded": 4, "embeddable": 4, "refused": 0}}
        before = len(client.remote.indexing)
        response = await client.post(f"/api/projects/{project}/index/rebuild")
        assert response.status_code == 202
        assert (await run_finished(client, response.json()["run_id"]))["status"] == "succeeded"
        status = await idle(client, project)
        assert status["run"] == {"run_id": response.json()["run_id"], "status": "succeeded", "rebuild": True,
                                 "rebuild_pending": False, "progress": None}
        assert status["passages"]["embedded"] == 4 and len(client.remote.indexing) - before == 4
        assert (await find(client, project, "earnings"))["results"]
        assert (await client.post("/api/projects/00000000-0000-4000-8000-000000000000/index/rebuild")).status_code == 404


async def test_a_shutdown_during_embedding_resumes_at_the_next_launch_with_only_what_is_missing(tmp_path):
    async with app(tmp_path, batch=1) as client:
        client.remote.hold, client.remote.free = asyncio.Event(), 2
        project = await project_of(client)
        await added(client, project, WAGES)
        await asyncio.wait_for(client.remote.reached.wait(), 10)
        [run] = [r for r in await runs_of(client, "index", project) if r["status"] == "running"]
        embedded = sum(1 for r in await index_rows(client, project) if r[3])
        assert embedded == 2
    async with app(tmp_path, batch=1) as client:  # recorded interrupted, and a fresh run embeds only what is missing
        status = await idle(client, project)
        assert status["passages"]["embedded"] == 4 and len(client.remote.indexing) == 2
        assert (await run_finished(client, run["run_id"]))["status"] == "interrupted"
        assert [r["status"] for r in await runs_of(client, "index", project) if r["run_id"] != run["run_id"]] == ["succeeded"]


# From the independent review


async def test_an_empty_reading_raises_no_offer_and_no_index_run(tmp_path):
    async with app(tmp_path, install=False) as client:
        project = await project_of(client)
        await added(client, project, ("blank.md", b"\n\n"), ("empty.html", b"<html><body></body></html>"))
        await idle(client, project)
        await asyncio.sleep(0.2)
        assert await runs_of(client, "model_offer", project) == [] and await runs_of(client, "index", project) == []
        await added(client, project, WAGES)
        assert len(await until_offer(client, project)) == 1  # the first reading with passages is the first material


async def test_deleting_one_of_two_papers_reading_one_file_leaves_the_other_its_own_rows(tmp_path):
    async with app(tmp_path) as client:
        project = await project_of(client)
        [first] = (await added(client, project, paper("Alpha Deleted Title", "A shared paragraph about wages.")))[
            "materials"]
        [second] = (await added(client, project, paper("Beta Kept", "Its own first text.")))["materials"]
        await idle(client, project)
        await added(client, project, paper("Alpha Deleted Title", "A shared paragraph about wages."),
                    material_id=second["id"])  # the second now reads the first's file
        await idle(client, project)
        assert (await client.delete(f"/api/materials/{first['id']}")).status_code == 200
        status = await idle(client, project)
        deadline = asyncio.get_running_loop().time() + 10
        while (status := await idle(client, project))["passages"]["embedded"] < status["passages"]["embeddable"]:
            assert asyncio.get_running_loop().time() < deadline
            await asyncio.sleep(0.05)
        assert {r[1] for r in await index_rows(client, project)} == {second["id"]}
        assert list(status["materials"]) == [second["id"]]
        texts = await asyncio.to_thread(client.state["index"]._read, lambda conn: conn.execute(
            "SELECT text FROM fts_passages").fetchall())
        assert texts and all(text.startswith("Beta Kept") for (text,) in texts)  # the deleted title is gone


async def test_papers_left_unembedded_get_index_runs_at_the_next_launch_with_the_model(tmp_path):
    async with app(tmp_path, install=False) as client:
        project = await project_of(client)
        await added(client, project, WAGES)
        status = await idle(client, project)
        assert status["passages"]["embedded"] == 0
    async with app(tmp_path) as client:  # the model is in place now: the launch embeds what waited
        deadline = asyncio.get_running_loop().time() + 10
        while (await idle(client, project))["passages"]["embedded"] < 4:
            assert asyncio.get_running_loop().time() < deadline
            await asyncio.sleep(0.05)


# Embedding inputs over the helper's slot (found by S1-18): bounded before they are sent; one the helper
# still refuses is recorded, counted and passed over until its text or the model changes


GLYPHS = "\ue000\ue0a1\uf8ff" * 150  # private-use characters, as PDF extraction leaves unmapped glyphs
LONG = ("long-passages.md", paper("Long Passages " + GLYPHS, "Minimum wages raise the earnings of low-paid workers.",
                                  "最低工资" * 497 + "最", "Regional prices adjust slowly after a wage floor is raised.")[1])


async def test_an_over_long_passage_among_others_is_embedded_on_its_bounded_prefix_and_the_rest_with_it(tmp_path):
    async with app(tmp_path, batch=2) as client:
        project = await project_of(client)
        await added(client, project, LONG)
        status = await idle(client, project)
        [run] = await runs_of(client, "index", project)
        assert (run["status"], run["result"]["passages"]["refused"]) == ("succeeded", 0)
        assert status["passages"]["embedded"] == status["passages"]["embeddable"] > 2
        assert status["passages"]["refused"] == 0 and client.remote.refused == [] and client.remote.counted
        sent = [text for texts in client.remote.indexing for text in texts]
        assert all(len(text.encode()) + 1 <= SLOT for text in sent)  # every input within the slot
        readings = await asyncio.to_thread(client.state["db"].read, lambda conn: search_index.readings(conn, project))
        [whole] = [search._text(r) for r in readings.values() if r[4].startswith("最低工资")]
        [bounded] = [t for t in sent if "最低工资" in t]
        assert whole.startswith(bounded) and len(bounded) < len(whole)  # its longest prefix within the slot
        assert len(bounded.encode()) > local_helper.INPUT_TOKENS - 3 and len(client.remote.counted) <= 14
        found = (await find(client, project, "最低工资"))["results"][0]
        assert found["text"].startswith("最低工资") and found["text"] == readings[found["passage_id"]][4]  # shown whole


async def test_a_question_over_the_slot_is_embedded_on_its_bounded_prefix_and_a_refused_one_searches_by_keyword(tmp_path):
    async with app(tmp_path) as client:
        project = await project_of(client)
        await added(client, project, WAGES)
        await idle(client, project)
        question = "earnings " + GLYPHS[:990]
        found = await find(client, project, question)
        assert (found["mode"], found["reason"]) == ("hybrid", None) and found["results"]
        [query] = [t for texts in client.remote.embedded for t in texts if "Query:" in t]
        assert query.startswith(search.QUERY_INSTRUCTION + "earnings") and len(query.encode()) + 1 <= SLOT
        client.remote.refuse = "earnings"
        found = await find(client, project, "earnings")
        assert (found["mode"], found["reason"]) == ("keyword_only", "input_refused")
        assert [r["title"] for r in found["results"]] == ["Wage Floors"] * 1


async def test_a_passage_the_helper_refuses_is_counted_and_passed_over_until_its_text_changes(tmp_path):
    async with app(tmp_path, batch=1) as client:
        client.remote.refuse = "Employment effects"
        project = await project_of(client)
        [material] = (await added(client, project, WAGES))["materials"]
        status = await idle(client, project)
        [run] = await runs_of(client, "index", project)
        assert run["status"] == "succeeded"  # the project's refused passages, from the index, in its result
        assert run["result"]["passages"] == {"indexed": 4, "embedded": 3, "embeddable": 4, "refused": 1}
        assert status["passages"] == run["result"]["passages"]
        assert status["materials"][material["id"]] == {"indexed": 4, "embedded": 3, "embeddable": 4, "refused": 1}
        assert len(client.remote.refused) == 1
        found = await find(client, project, "employment effects")
        assert found["coverage"] == {"embedded": 3, "total": 4, "refused": 1}
        assert found["results"][0]["text"].startswith("Employment effects")  # still found by its keywords
        await added(client, project, CHINESE)  # the project's next run: the refused passage is not sent again
        await idle(client, project)
        assert len(client.remote.refused) == 1
        ran = await runs_of(client, "index", project)
        assert ran[0]["result"]["passages"]["refused"] == 1  # still the one: not asked for again
    async with app(tmp_path, batch=1) as client:  # nor at the next launch, which asks for no run for it
        client.remote.refuse = "Employment effects"
        await asyncio.sleep(0.3)
        status = await idle(client, project)
        assert client.remote.refused == [] and len(await runs_of(client, "index", project)) == len(ran)
        assert status["passages"]["refused"] == 1
        response = await client.patch(f"/api/materials/{material['id']}", json={"title": "Wage Floors, Revised"})
        assert response.status_code == 200
        status = await idle(client, project)  # its text changed: asked for once more, and refused once more
        assert len(client.remote.refused) == 1 and status["passages"]["refused"] == 1
        other = (await added(client, project, paper("Doomed", "Employment effects of a doomed paper.")))["materials"][0]
        assert (await idle(client, project))["materials"][other["id"]]["refused"] == 1
        assert (await client.delete(f"/api/materials/{other['id']}")).status_code == 200  # a refused row goes too
        assert other["id"] not in (await idle(client, project))["materials"]
        assert [r for r in await index_rows(client, project) if r[1] == other["id"]] == []
        client.remote.refuse = None
        index = client.state["index"]
        await asyncio.to_thread(index._write, index._drop_vectors)  # another model: every vector asked for again
        await search._embed_all(client.state)
        status = await idle(client, project)
        assert status["passages"]["refused"] == 0 and status["passages"]["embedded"] == status["passages"]["embeddable"]


async def test_a_refusal_is_kept_when_a_later_passage_of_its_batch_fails_and_retry_does_not_send_it_again(tmp_path):
    async with app(tmp_path, batch=10) as client:  # one batch for the paper's passages
        client.remote.refuse, client.remote.fail_on = "Minimum wages", "Employment effects"  # the 2nd, then the 3rd
        project = await project_of(client)
        await added(client, project, WAGES)
        status = await idle(client, project)
        [run] = await runs_of(client, "index", project)
        assert (run["status"], run["result"]) == ("failed", {"reason": "request_failed"})  # its reason only
        assert status["passages"]["refused"] == 1 and len(client.remote.refused) == 1  # kept, though the batch failed
        client.remote.fail_on = None
        again = await client.post(f"/api/runs/{run['run_id']}/retry")
        assert (await run_finished(client, again.json()["run_id"]))["status"] == "succeeded"
        status = await idle(client, project)
        assert len(client.remote.refused) == 1  # not sent again
        assert status["passages"] == {"indexed": 4, "embedded": 3, "embeddable": 4, "refused": 1}


async def test_a_rebuild_and_its_retry_keep_a_refusal_while_its_text_is_unchanged(tmp_path):
    async with app(tmp_path, batch=10) as client:
        client.remote.refuse, client.remote.fail_on = "Minimum wages", "Employment effects"
        project = await project_of(client)
        await added(client, project, WAGES)
        await idle(client, project)  # refused, then failed: the refusal kept (above)
        response = await client.post(f"/api/projects/{project}/index/rebuild")
        rebuilt = await run_finished(client, response.json()["run_id"])
        assert (rebuilt["status"], rebuilt["result"]["reason"]) == ("failed", "request_failed")
        assert len(client.remote.refused) == 1  # the rebuild kept the refusal: the text was not sent again
        assert (await idle(client, project))["passages"]["refused"] == 1
        client.remote.fail_on = None
        again = await client.post(f"/api/runs/{rebuilt['run_id']}/retry")
        ended = await run_finished(client, again.json()["run_id"])
        assert (ended["status"], ended["result"]["rebuilt"]) == ("succeeded", True)  # a rebuild again
        assert len(client.remote.refused) == 1
        assert (await idle(client, project))["passages"] == {"indexed": 4, "embedded": 3, "embeddable": 4, "refused": 1}
        index = client.state["index"]  # a mark whose digest is not its passage's text's is not kept by a rebuild
        await asyncio.to_thread(index._write, lambda: index._conn.execute(
            "UPDATE index_rows SET digest = 'another text' WHERE embedded = -1"))
        response = await client.post(f"/api/projects/{project}/index/rebuild")
        assert (await run_finished(client, response.json()["run_id"]))["status"] == "succeeded"
        assert len(client.remote.refused) == 2  # asked for again, and refused again


@pytest.mark.parametrize("status", [503, 500, 429])
async def test_a_helper_that_cannot_count_an_over_long_passage_fails_the_run_as_before(tmp_path, status):
    async with app(tmp_path) as client:
        client.remote.tokenizing = status
        project = await project_of(client)
        await added(client, project, LONG)
        await idle(client, project)
        [run] = await runs_of(client, "index", project)
        assert (run["status"], run["result"]["reason"], run["retryable"]) == ("failed", "request_failed", True)
        client.remote.tokenizing = None
        again = await client.post(f"/api/runs/{run['run_id']}/retry")
        assert (await run_finished(client, again.json()["run_id"]))["status"] == "succeeded"
        status = await idle(client, project)
        assert status["passages"]["embedded"] == status["passages"]["embeddable"] and status["passages"]["refused"] == 0


async def test_a_helper_answering_vectors_of_another_dimension_fails_the_run_after_one_request(tmp_path, monkeypatch):
    monkeypatch.setattr(synthetic, "embedding", lambda text, dimensions=512: [1.0] * dimensions)
    async with app(tmp_path) as client:
        project = await project_of(client)
        await added(client, project, WAGES)
        await idle(client, project)
        [run] = await runs_of(client, "index", project)
        assert (run["status"], run["result"]["reason"]) == ("failed", "request_failed")
        assert len(client.remote.indexing) == 1


async def test_a_dense_depth_above_what_vec0_takes_is_bounded(tmp_path):
    async with app(tmp_path) as client:
        project = await project_of(client)
        await added(client, project, WAGES)
        await idle(client, project)
        await setting(client, "retrieval.dense_candidates", 5000)
        found = await find(client, project, "earnings")
        assert found["mode"] == "hybrid" and found["results"]


# A newer reading of a file supersedes the earlier one (S1-20): the index follows


async def test_a_superseded_readings_passages_leave_the_index_for_the_newer_ones(tmp_path, monkeypatch):
    read_in_process(monkeypatch)  # an earlier extractor version, as this process sees it
    import backend.extraction as extraction
    monkeypatch.setitem(extraction.EXTRACTORS, extraction.MARKDOWN, ("markdown", "markdown-0"))
    async with app(tmp_path) as client:
        mine, theirs = await project_of(client, "Mine"), await project_of(client, "Theirs")
        [material] = (await added(client, mine, WAGES))["materials"]
        await added(client, theirs, WAGES)
        await idle(client, mine)
        await idle(client, theirs)
        older = {r[0] for r in await index_rows(client)}
        assert len(older) == 4 and len(await index_rows(client)) == 8  # the same passages, a row in each project
        monkeypatch.setitem(extraction.EXTRACTORS, extraction.MARKDOWN, ("markdown", "markdown-1"))
        [read] = (await client.get(f"/api/projects/{mine}/materials")).json()["materials"]
        again = await client.post(f"/api/material-versions/{read['version']['id']}/read")
        assert (await run_finished(client, again.json()["run_id"]))["status"] == "succeeded"
        for project in (mine, theirs):
            deadline = asyncio.get_running_loop().time() + 10
            while (status := await idle(client, project))["passages"]["embedded"] < 4:
                assert asyncio.get_running_loop().time() < deadline
                await asyncio.sleep(0.05)
        rows_now = await index_rows(client)
        newer = {r[0] for r in rows_now}
        assert len(rows_now) == 8 and not newer & older  # each project now holds the newer reading's passages only
        assert newer == {p for (p,) in await rows(client, "SELECT id FROM passages")}
        fts = {p for (p,) in await asyncio.to_thread(client.state["index"]._read, lambda conn: conn.execute(
            "SELECT passage_id FROM fts_passages").fetchall())}
        assert fts == newer and await rows(client, "SELECT count(*) FROM index_queue") == [(0,)]
        assert (await find(client, mine, "earnings"))["results"][0]["material_id"] == material["id"]


async def test_queued_additions_whose_passages_a_newer_reading_removed_are_passed_over(tmp_path, monkeypatch):
    read_in_process(monkeypatch)  # an earlier extractor version, as this process sees it
    import backend.extraction as extraction
    monkeypatch.setitem(extraction.EXTRACTORS, extraction.MARKDOWN, ("markdown", "markdown-0"))
    async with app(tmp_path) as client:
        project = await project_of(client)
        real, reconcile = SearchIndex._apply, SearchIndex._reconcile
        monkeypatch.setattr(SearchIndex, "_apply", lambda self, stop=None: 0)  # the first reading's additions wait in the queue
        monkeypatch.setattr(SearchIndex, "_reconcile", lambda self, project_id, stop=None: (0, 0))  # and nothing makes them good
        await added(client, project, WAGES)
        await idle(client, project)
        older = {p for (p,) in await rows(client, "SELECT id FROM passages")}
        assert await index_rows(client, project) == [] and len(older) == 4
        monkeypatch.setitem(extraction.EXTRACTORS, extraction.MARKDOWN, ("markdown", "markdown-1"))
        [read] = (await client.get(f"/api/projects/{project}/materials")).json()["materials"]
        again = await client.post(f"/api/material-versions/{read['version']['id']}/read")
        assert (await run_finished(client, again.json()["run_id"]))["status"] == "succeeded"
        queued = await rows(client, "SELECT target_id, op FROM index_queue ORDER BY seq")
        assert {p for p, op in queued if op == "add"} >= older  # the earlier additions are still queued
        assert await rows(client, "SELECT count(*) FROM passages WHERE id IN (SELECT value FROM json_each(?))",
                          json.dumps(sorted(older))) == [(0,)]  # but the newer reading removed their passages
        monkeypatch.setattr(SearchIndex, "_apply", real)
        monkeypatch.setattr(SearchIndex, "_reconcile", reconcile)
        await asyncio.to_thread(client.state["index"].apply)
        newer = {p for (p,) in await rows(client, "SELECT id FROM passages")}
        assert {r[0] for r in await index_rows(client, project)} == newer and not newer & older
        assert await rows(client, "SELECT count(*) FROM index_queue") == [(0,)]


# From review 1 (fix batch 1)


async def _held_in_the_writer(client, monkeypatch, name):
    """Hold SearchIndex.name inside the index's writer until the returned event is set; reached is set as it
    is entered."""
    import threading
    reached, go, real = threading.Event(), threading.Event(), getattr(SearchIndex, name)

    def held(self, *args):
        reached.set()
        go.wait(20)
        return real(self, *args)
    monkeypatch.setattr(SearchIndex, name, held)
    return reached, go


async def _status_of(client, run_id):
    return (await rows(client, "SELECT status FROM runs WHERE id = ?", run_id))[0][0]


async def _cancelling(client, run_id):
    """Ask to cancel the run, without waiting for the reply (it comes once the run has ended): returns the
    request once the harness holds the cancellation."""
    active = client.state["harness"].registry.runs[run_id]
    cancel = asyncio.ensure_future(client.post(f"/api/runs/{run_id}/cancel"))
    deadline = asyncio.get_running_loop().time() + 10
    while not active.cancel_requested.is_set():
        assert asyncio.get_running_loop().time() < deadline
        await asyncio.sleep(0.01)
    return cancel


@pytest.mark.parametrize("change", ["store", "rebuild"])
async def test_a_cancelled_run_ends_only_after_its_index_change_has(tmp_path, monkeypatch, change):
    """Cancel while the run's change waits in the index's writer (vectors stored, or a project's rows
    rebuilt): the run's end is recorded only once that change has returned, as the harness drains
    blocking work (Harness.work), and nothing changes after it. A rebuild that sees the cancel rolls back."""
    async with app(tmp_path) as client:
        project = await project_of(client)
        if change == "rebuild":
            await added(client, project, WAGES)
            await idle(client, project)
        reached, go = await _held_in_the_writer(client, monkeypatch, "_store" if change == "store" else "_rebuild_project")
        if change == "store":
            await added(client, project, WAGES)
        else:
            await client.post(f"/api/projects/{project}/index/rebuild")
        assert await asyncio.to_thread(reached.wait, 10)
        [run] = [r for r in await runs_of(client, "index", project) if r["status"] == "running"]
        cancel = await _cancelling(client, run["run_id"])
        assert await _status_of(client, run["run_id"]) == "running"  # its change is still to come
        go.set()
        ended = await run_finished(client, run["run_id"])
        assert (ended["status"], ended["cancel_reason"]) == ("cancelled", "researcher")
        embedded = sum(1 for r in await index_rows(client, project) if r[3])
        assert embedded == 4  # stored before its end; or, the rebuild rolled back, the rows as they were
        await cancel
        await asyncio.sleep(0.2)
        assert sum(1 for r in await index_rows(client, project) if r[3]) == embedded  # nothing after its end


@pytest.mark.parametrize("then", ["cancelled", "revoked"])
async def test_an_offer_answered_later_is_not_asked_again_whatever_ends_its_run(tmp_path, monkeypatch, then):
    import backend.search as search
    monkeypatch.setattr(search, "WAIT_SECONDS", 30)  # the run reads its answer only after this: it ends first
    async with app(tmp_path, install=False) as client:
        project = await project_of(client)
        await added(client, project, WAGES)
        [ask] = await until_offer(client, project)
        answered = await client.post(f"/api/runs/{ask['run_id']}/asks/{ask['ask_id']}", json={"option": "later"})
        assert answered.status_code == 200
        if then == "cancelled":
            await client.post(f"/api/runs/{ask['run_id']}/cancel")
        else:  # a stricter level revokes it (a Private project is still offered the model)
            response = await client.post(f"/api/projects/{project}/sensitivity", json={"level": "private"})
            assert response.status_code == 200, response.text
        ended = await run_finished(client, ask["run_id"])
        assert ended["status"] == "cancelled"
        await added(client, project, paper("Next Paper", "Text."))
        await idle(client, project)
        await asyncio.sleep(0.2)
        assert await offers(client, project) == [] and len(await runs_of(client, "model_offer", project)) == 1
        assert client.remote.sources == []


@pytest.mark.parametrize("how", ["an empty reading", "a blank file", "a file that cannot be read"])
async def test_text_that_gives_way_to_none_leaves_the_index_file_without_other_work(tmp_path, monkeypatch, how):
    """An indexed reading superseded by one without passages, or a file replaced by one that gives none (blank,
    or failing to be read): an index run takes the removals now, so the old text leaves the index file and its
    WAL, not only search (whose access check already hides it)."""
    import backend.extraction as extraction
    read_in_process(monkeypatch)  # its extractor's version and parser changed in this process
    english = "Narwhals barter vermilion abacuses"
    monkeypatch.setitem(extraction.EXTRACTORS, extraction.MARKDOWN, ("markdown", "markdown-0"))
    async with app(tmp_path) as client:
        project = await project_of(client)
        [material] = (await added(client, project, paper("Narwhal Ledger", english)))["materials"]
        await idle(client, project)
        index = client.state["index"]
        files = [index.path, Path(f"{index.path}-wal")]
        assert english.encode() in b"".join(f.read_bytes() for f in files if f.exists())
        if how == "an empty reading":
            real = extraction.extract

            def empty(*args, **kwargs):
                found = real(*args, **kwargs)
                return extraction.Extracted(found.extractor, found.version, [], found.pages)
            monkeypatch.setattr(extraction, "extract", empty)
            monkeypatch.setitem(extraction.EXTRACTORS, extraction.MARKDOWN, ("markdown", "markdown-1"))
            [read] = (await client.get(f"/api/projects/{project}/materials")).json()["materials"]
            again = await client.post(f"/api/material-versions/{read['version']['id']}/read")
            assert (await run_finished(client, again.json()["run_id"]))["status"] == "succeeded"
        else:
            replacement = ("blank.md", b"\n\n") if how == "a blank file" else ("broken.pdf", b"%PDF-1.7\nnot a pdf")
            await added(client, project, replacement, material_id=material["id"])
        await idle(client, project)
        assert await index_rows(client, project) == []
        assert await rows(client, "SELECT count(*) FROM index_queue") == [(0,)]
        assert not files[1].exists() or files[1].stat().st_size == 0
        left = b"".join(f.read_bytes() for f in files if f.exists())
        assert english.encode() not in left and b"narwhals" not in left and b"vermilion" not in left


async def test_a_default_result_count_above_the_apis_bound_is_held_to_it(tmp_path):
    async with app(tmp_path, install=False) as client:
        project = await project_of(client)
        await added(client, project, paper("Many", *[f"Common finding number {i}." for i in range(60)]))
        await idle(client, project)
        await setting(client, "retrieval.keep", 100)
        await setting(client, "retrieval.bm25_candidates", 100)
        assert len((await find(client, project, "common"))["results"]) == 50
        assert len((await find(client, project, "common", limit=10))["results"]) == 10


async def test_a_model_installed_as_a_run_finds_it_missing_gets_that_runs_papers_embedded(tmp_path, monkeypatch):
    """The install comes as an index run has found the model missing and is ending: the install's runs
    take every paper still without embeddings, that run's too, so they are embedded without a restart."""
    import sys
    import backend.search as search
    real = search.search_mode

    def installed_as_it_looks(state):  # the first index run looks just before the model is in place
        if sys._getframe(1).f_code.co_name == "_index_run" and not getattr(installed_as_it_looks, "seen", False):
            installed_as_it_looks.seen = True
            state["local_helper"].helpers[EMBEDDING].model_installed()  # what a download or an import calls
            return "keyword_only", "model_missing"
        return real(state)
    monkeypatch.setattr(search, "search_mode", installed_as_it_looks)
    async with app(tmp_path) as client:
        project = await project_of(client)
        await added(client, project, WAGES)
        deadline = asyncio.get_running_loop().time() + 10
        while (status := await idle(client, project))["passages"]["embedded"] < 4:
            assert asyncio.get_running_loop().time() < deadline, status
            await asyncio.sleep(0.05)
        first, *_ = sorted(await runs_of(client, "index", project), key=lambda r: r["started_at"])
        assert (first["result"]["mode"], first["result"]["reason"]) == ("keyword_only", "model_missing")


async def test_the_installs_request_while_a_run_runs_is_one_rerun_after_it(tmp_path):
    """No window in which an ending run's work is left to no one: the install's request, while the
    project's run runs, marks it to run once more when it ends (that rerun finds nothing missing)."""
    import backend.search as search
    async with app(tmp_path, batch=1) as client:
        client.remote.hold, client.remote.free = asyncio.Event(), 1  # the run embeds one passage, then waits
        project = await project_of(client)
        [material] = (await added(client, project, WAGES))["materials"]
        await asyncio.wait_for(client.remote.reached.wait(), 10)
        [running] = [r for r in await runs_of(client, "index", project) if r["status"] == "running"]
        await search._embed_all(client.state)  # as the model's install calls it
        await search._embed_all(client.state)  # twice: still one rerun
        assert [r["run_id"] for r in await runs_of(client, "index", project)] == [running["run_id"]]
        [(inputs,)] = await rows(client, "SELECT inputs FROM runs WHERE id = ?", running["run_id"])
        assert json.loads(inputs)["again"] == {"rebuild": False}
        client.remote.hold.set()
        assert (await run_finished(client, running["run_id"]))["status"] == "succeeded"
        [rerun] = [r["run_id"] for r in await runs_of(client, "index", project) if r["run_id"] != running["run_id"]]
        assert (await run_finished(client, rerun))["status"] == "succeeded"
        status = await idle(client, project)
        assert status["passages"]["embedded"] == 4 and status["materials"][material["id"]]["embedded"] == 4
        assert len(await runs_of(client, "index", project)) == 2


async def test_a_helper_still_saying_the_model_is_missing_records_no_chain_of_runs(tmp_path):
    """The file in place but the helper's last check still saying model_missing (until an install or a
    start says otherwise): the run ends keyword-only, and no run follows it."""
    async with app(tmp_path) as client:
        helper = client.state["local_helper"].helpers[EMBEDDING]
        helper.state, helper.problem = "stopped", "model_missing"
        project = await project_of(client)
        await added(client, project, WAGES)
        await idle(client, project)
        await asyncio.sleep(0.5)
        await idle(client, project)
        [run] = await runs_of(client, "index", project)
        assert (run["result"]["mode"], run["result"]["reason"]) == ("keyword_only", "model_missing")


async def test_a_reading_during_a_rebuild_runs_once_more_after_it_and_the_status_shows_the_rebuild(tmp_path):
    async with app(tmp_path, batch=1) as client:
        project = await project_of(client)
        await added(client, project, WAGES)
        await idle(client, project)
        client.remote.hold = asyncio.Event()
        client.remote.free = len(client.remote.indexing)
        rebuild = (await client.post(f"/api/projects/{project}/index/rebuild")).json()["run_id"]
        await asyncio.wait_for(client.remote.reached.wait(), 10)
        [newer] = (await added(client, project, paper("Newer Paper", "Text after the rebuild began.")))["materials"]
        deadline = asyncio.get_running_loop().time() + 10
        while not (await rows(client, "SELECT json_extract(inputs, '$.again') FROM runs WHERE id = ?", rebuild))[0][0]:
            assert asyncio.get_running_loop().time() < deadline  # the reading's commit asked for the project's run
            await asyncio.sleep(0.02)
        assert [r["run_id"] for r in await runs_of(client, "index", project) if r["status"] == "running"] == [rebuild]
        status = (await client.get(f"/api/projects/{project}/index")).json()
        assert (status["run"]["run_id"], status["run"]["rebuild"], status["run"]["status"]) == (rebuild, True, "running")
        again = await client.post(f"/api/projects/{project}/index/rebuild")  # Rebuild while a rebuild runs: that one
        assert again.json()["run_id"] == rebuild
        client.remote.hold.set()
        assert (await run_finished(client, rebuild))["status"] == "succeeded"
        status = await idle(client, project)
        assert status["materials"][newer["id"]]["embedded"] == status["materials"][newer["id"]]["embeddable"] > 0


async def test_each_index_run_in_the_activity_says_whether_its_own_project_offers_the_model(tmp_path):
    async with app(tmp_path, install=False) as client:
        normal, local = await project_of(client, "Normal"), await project_of(client, "Local", level="local_only")
        await added(client, normal, WAGES)
        await added(client, local, paper("Local Paper", "Text kept on this Mac."))
        await idle(client, normal)
        await idle(client, local)
        [in_normal] = await runs_of(client, "index", normal)
        [in_local] = await runs_of(client, "index", local)
        assert in_normal["result"]["reason"] == in_local["result"]["reason"] == "model_missing"
        assert (in_normal["download_offered"], in_local["download_offered"]) == (True, False)


async def test_a_cancelled_run_stops_its_pass_between_pages_and_leaves_the_rest_queued(tmp_path, monkeypatch):
    """Cancel and shutdown wait for at most the page under way (QUEUE_PAGE rows), not the whole queue."""
    import threading
    import backend.search_index as search_index
    monkeypatch.setattr(search_index, "QUEUE_PAGE", 2)
    reached, go, real = threading.Event(), threading.Event(), SearchIndex._add

    def held(self, *args):
        reached.set()
        go.wait(20)
        return real(self, *args)
    async with app(tmp_path, install=False) as client:
        project = await project_of(client)
        monkeypatch.setattr(SearchIndex, "_add", held)
        await added(client, project, paper("Pages", *[f"Paragraph number {i}." for i in range(10)]))
        assert await asyncio.to_thread(reached.wait, 10)
        [run] = [r for r in await runs_of(client, "index", project) if r["status"] == "running"]
        cancel = await _cancelling(client, run["run_id"])
        go.set()
        ended = await run_finished(client, run["run_id"])
        assert ended["status"] == "cancelled"
        [(passages,)] = await rows(client, "SELECT count(*) FROM passages")
        assert len(await index_rows(client, project)) == 2  # the page under way, and no more
        assert await rows(client, "SELECT count(*) FROM index_queue") == [(passages - 2,)]  # left to the next pass
        await cancel
        monkeypatch.setattr(SearchIndex, "_add", real)
        await asyncio.to_thread(client.state["index"].apply)
        assert len(await index_rows(client, project)) == passages >= 10


async def test_a_cancelled_rebuild_rolls_back_without_finishing_and_leaves_the_rows_as_they_were(tmp_path, monkeypatch):
    import threading
    import backend.search_index as search_index
    monkeypatch.setattr(search_index, "STOP_EVERY", 1)
    async with app(tmp_path) as client:
        project = await project_of(client)
        await added(client, project, WAGES)
        before = await idle(client, project)
        assert before["passages"]["embedded"] == 4
        reached, go, real = threading.Event(), threading.Event(), SearchIndex._add

        def held(self, *args):  # the rebuild's first new row: its old rows already removed in its transaction
            reached.set()
            go.wait(20)
            return real(self, *args)
        monkeypatch.setattr(SearchIndex, "_add", held)
        rebuild = (await client.post(f"/api/projects/{project}/index/rebuild")).json()["run_id"]
        assert await asyncio.to_thread(reached.wait, 10)
        cancel = await _cancelling(client, rebuild)
        go.set()
        assert (await run_finished(client, rebuild))["status"] == "cancelled"
        await cancel
        status = await idle(client, project)
        assert status["passages"] == before["passages"]  # rolled back: every row and its embedding as before
        meta = await asyncio.to_thread(client.state["index"]._read, lambda conn: conn.execute(
            "SELECT key FROM index_meta WHERE key LIKE 'rebuilt:%'").fetchall())
        assert meta == []  # not marked rebuilt: a Rebuild later does it whole


async def test_a_cancelled_runs_index_change_still_waiting_its_turn_is_withdrawn(tmp_path):
    """Cancel while the run's pass waits behind other work in the index's writer (a whole rebuild, say): it is
    taken off the writer's queue, the run ends at once, and the queue keeps its rows for the next pass."""
    import threading
    async with app(tmp_path, install=False) as client:
        project = await project_of(client)
        index, go = client.state["index"], threading.Event()
        index._writer.submit(go.wait, 20)  # the writer busy, as with a damaged file's rebuild
        try:
            await added(client, project, WAGES)
            deadline = asyncio.get_running_loop().time() + 10
            while not (running := [r for r in await runs_of(client, "index", project) if r["status"] == "running"]):
                assert asyncio.get_running_loop().time() < deadline
                await asyncio.sleep(0.02)
            cancel = await _cancelling(client, running[0]["run_id"])
            ended = await asyncio.wait_for(run_finished(client, running[0]["run_id"]), 5)  # the writer still busy
            assert ended["status"] == "cancelled"
            await cancel
        finally:
            go.set()
        assert await index_rows(client, project) == []
        assert await rows(client, "SELECT count(*) FROM index_queue") == [(4,)]  # left to the next pass
        await asyncio.to_thread(index.apply)
        assert len(await index_rows(client, project)) == 4


async def test_retrying_a_stopped_rebuild_rebuilds_again(tmp_path):
    """Retry of a cancelled project rebuild is a rebuild (its flag kept), not an index run that finds nothing
    to embed and succeeds without rebuilding."""
    async with app(tmp_path) as client:
        project = await project_of(client)
        await added(client, project, WAGES)
        await idle(client, project)
        client.remote.hold, client.remote.free = asyncio.Event(), len(client.remote.indexing)
        rebuild = (await client.post(f"/api/projects/{project}/index/rebuild")).json()["run_id"]
        await asyncio.wait_for(client.remote.reached.wait(), 10)  # its rows rebuilt, its embeddings under way
        cancel = await _cancelling(client, rebuild)
        client.remote.hold.set()
        assert (await run_finished(client, rebuild))["status"] == "cancelled"
        await cancel
        await idle(client, project)
        before = len(client.remote.indexing)
        again = await client.post(f"/api/runs/{rebuild}/retry")
        assert again.status_code == 201, again.text
        retried = again.json()["run_id"]
        [(inputs,)] = await rows(client, "SELECT inputs FROM runs WHERE id = ?", retried)
        assert json.loads(inputs)["rebuild"] is True
        assert (await run_finished(client, retried))["status"] == "succeeded"
        meta = await asyncio.to_thread(client.state["index"]._read, lambda conn: conn.execute(
            "SELECT key FROM index_meta WHERE key LIKE 'rebuilt:%'").fetchall())
        assert meta == [(f"rebuilt:{retried}",)]  # rebuilt by the retry
        assert len(client.remote.indexing) - before == 4  # and every passage embedded again
        status = await idle(client, project)
        assert status["run"]["rebuild"] is True and status["passages"]["embedded"] == 4


# The structural fix (fix batch 3): a project's index run, reconciling from the main database, at most
# one running and one rerun pending per project; every trigger only a request.


async def _pending(client, run_id):
    [(again,)] = await rows(client, "SELECT json_extract(inputs, '$.again') FROM runs WHERE id = ?", run_id)
    return json.loads(again) if again else None


async def test_every_trigger_while_the_projects_run_runs_is_one_rerun_after_it(tmp_path):
    """A burst of readings, a title change, a deletion, the model's install, Retry and Rebuild while the
    project's run runs: still one running run, and one rerun pending (a rebuild, since one asked for it)."""
    import backend.search as search
    async with app(tmp_path, batch=1) as client:
        project = await project_of(client)
        [old] = (await added(client, project, paper("Old Paper", "An earlier paragraph about prices.")))["materials"]
        await idle(client, project)
        cancelled = (await client.post(f"/api/projects/{project}/index/rebuild")).json()["run_id"]
        await client.post(f"/api/runs/{cancelled}/cancel")
        await run_finished(client, cancelled)
        client.remote.hold, client.remote.free = asyncio.Event(), len(client.remote.indexing)
        [first] = (await added(client, project, WAGES))["materials"]
        await asyncio.wait_for(client.remote.reached.wait(), 10)
        [running] = [r["run_id"] for r in await runs_of(client, "index", project) if r["status"] == "running"]
        for i in range(4):  # a burst of readings
            await added(client, project, paper(f"Burst {i}", f"Paragraph {i} of a burst."))
        deadline = asyncio.get_running_loop().time() + 10
        while [r for r in await runs_of(client, "extract", project) if r["status"] == "running"]:
            assert asyncio.get_running_loop().time() < deadline
            await asyncio.sleep(0.02)
        patched = await client.patch(f"/api/materials/{first['id']}", json={"title": "Wage Floors, Retitled"})
        assert patched.status_code == 200, patched.text
        assert (await client.delete(f"/api/materials/{old['id']}")).status_code == 200
        await search._embed_all(client.state)
        retried = (await client.post(f"/api/runs/{cancelled}/retry")).json()["run_id"]
        rebuilt = (await client.post(f"/api/projects/{project}/index/rebuild")).json()
        assert retried == rebuilt["run_id"] == running and rebuilt["rebuild_pending"] is True  # the running run's id
        assert await _pending(client, running) == {"rebuild": True}
        assert [r["run_id"] for r in await runs_of(client, "index", project) if r["status"] == "running"] == [running]
        client.remote.hold.set()
        assert (await run_finished(client, running))["status"] == "succeeded"
        [retried] = [r["run_id"] for r in await runs_of(client, "index", project)
                     if r["run_id"] not in (cancelled, running) and r["rebuild"]]
        ended = await run_finished(client, retried)
        assert (ended["status"], ended["rebuild"], ended["result"]["rebuilt"]) == ("succeeded", True, True)
        status = await idle(client, project)
        assert status["passages"]["embedded"] == status["passages"]["embeddable"] == status["passages"]["indexed"] > 4
        assert old["id"] not in status["materials"]
        runs = {r["run_id"] for r in await runs_of(client, "index", project)}
        assert {cancelled, running, retried} <= runs and len(runs) == 4  # and the first paper's: no other run


async def test_retrying_an_old_rebuild_embeds_the_papers_added_since(tmp_path):
    """Finding 16: Retry of a rebuild stopped before papers were added rebuilds the project's rows and
    embeds every one of its passages, theirs too, not only the papers the old run knew."""
    async with app(tmp_path, batch=1) as client:
        project = await project_of(client)
        await added(client, project, WAGES)
        await idle(client, project)
        client.remote.hold, client.remote.free = asyncio.Event(), len(client.remote.indexing)
        old = (await client.post(f"/api/projects/{project}/index/rebuild")).json()["run_id"]
        await asyncio.wait_for(client.remote.reached.wait(), 10)
        await client.post(f"/api/runs/{old}/cancel")
        client.remote.hold.set()
        assert (await run_finished(client, old))["status"] == "cancelled"
        [later] = (await added(client, project, paper("Later Paper", "A paragraph added after the rebuild.")))["materials"]
        await idle(client, project)
        again = (await client.post(f"/api/runs/{old}/retry")).json()["run_id"]
        ended = await run_finished(client, again)
        assert (ended["status"], ended["result"]["rebuilt"]) == ("succeeded", True)
        status = await idle(client, project)
        assert status["materials"][later["id"]]["embedded"] == status["materials"][later["id"]]["embeddable"] > 0
        assert status["passages"]["embedded"] == status["passages"]["embeddable"]


async def test_a_revoked_run_left_running_by_a_crash_never_stands_for_the_projects_run(tmp_path):
    """Finding 18: a revoked index run still running in the record at a launch is not the project's run:
    the launch's request records one that embeds what waits; the revoked one ends cancelled."""
    async with app(tmp_path, install=False) as client:
        project = await project_of(client)
        [material] = (await added(client, project, WAGES))["materials"]
        assert (await idle(client, project))["passages"]["embedded"] == 0

        def left(conn):  # as a crash after the revocation's commit leaves it (an older run named its papers)
            conn.execute("INSERT INTO runs (id, project_id, kind, workflow, inputs, cancel_reason) VALUES"
                         " ('00000000-0000-4000-8000-0000000000aa', ?, 'background', 'index', ?, 'revoked')",
                         (project, json.dumps({"material_ids": [material["id"]]})))
        await asyncio.to_thread(client.state["db"].write, left)
    async with app(tmp_path) as client:
        deadline = asyncio.get_running_loop().time() + 10
        while (await idle(client, project))["passages"]["embedded"] < 4:
            assert asyncio.get_running_loop().time() < deadline
            await asyncio.sleep(0.05)
        revoked = await run_finished(client, "00000000-0000-4000-8000-0000000000aa")
        assert (revoked["status"], revoked["cancel_reason"]) == ("cancelled", "revoked")


@pytest.mark.parametrize("ended", ["cancelled", "failed"])
async def test_a_run_that_ends_without_its_work_leaves_it_to_the_next_request(tmp_path, ended):
    """Whatever ends the project's run (Cancel, a helper that fails), the next request's run does what it
    left: every passage of the project embedded."""
    async with app(tmp_path, batch=1) as client:
        project = await project_of(client)
        client.remote.hold, client.remote.free = asyncio.Event(), 1
        if ended == "failed":
            client.remote.failing = True
        [first] = (await added(client, project, WAGES))["materials"]
        if ended == "cancelled":
            await asyncio.wait_for(client.remote.reached.wait(), 10)
            [run] = [r for r in await runs_of(client, "index", project) if r["status"] == "running"]
            await client.post(f"/api/runs/{run['run_id']}/cancel")
        client.remote.hold.set()
        assert (await idle(client, project))["passages"]["embedded"] < 4
        assert [r["status"] for r in await runs_of(client, "index", project)] == [ended]
        client.remote.failing, client.remote.hold = False, None
        [second] = (await added(client, project, paper("Second Paper", "Another paragraph.")))["materials"]
        status = await idle(client, project)
        for material in (first, second):
            assert status["materials"][material["id"]]["embedded"] == status["materials"][material["id"]]["embeddable"]


# Fix batch 4


async def _readings_done(client, project, timeout=10.0):
    """Once none of the project's readings runs (each has committed, asking for the project's index run)."""
    deadline = asyncio.get_running_loop().time() + timeout
    while [r for r in await runs_of(client, "extract", project) if r["status"] == "running"]:
        assert asyncio.get_running_loop().time() < deadline
        await asyncio.sleep(0.02)


async def _running_index_runs(client, project):
    return [r for (r,) in await rows(client, "SELECT id FROM runs WHERE project_id = ? AND workflow = 'index'"
                                             " AND status = 'running'", project)]


async def test_a_trigger_while_a_cancel_drains_is_that_runs_and_the_cancel_drops_it(tmp_path, monkeypatch):
    """Review 4, items 2 and 6: a run is its project's until its terminal transaction, a Cancel draining
    included, so a reading then marks it (never a second run); and Cancel stops indexing for now (reading
    R29): the pending rerun is dropped, and nothing starts until a later trigger."""
    async with app(tmp_path) as client:
        project = await project_of(client)
        reached, go = await _held_in_the_writer(client, monkeypatch, "_store")
        await added(client, project, WAGES)
        assert await asyncio.to_thread(reached.wait, 10)
        [run] = await _running_index_runs(client, project)
        cancel = await _cancelling(client, run)
        [(newer,)] = [(m["id"],) for m in (await added(client, project, paper("Newer", "Text added meanwhile.")))["materials"]]
        deadline = asyncio.get_running_loop().time() + 10
        while not await _pending(client, run):  # the reading's commit marked the draining run
            assert asyncio.get_running_loop().time() < deadline
            assert await _running_index_runs(client, project) == [run]  # never a second run
            await asyncio.sleep(0.02)
        assert await _pending(client, run) == {"rebuild": False}
        go.set()
        assert (await run_finished(client, run))["status"] == "cancelled"
        await cancel
        await asyncio.sleep(0.3)
        assert [r["run_id"] for r in await runs_of(client, "index", project)] == [run]  # its rerun dropped, nothing started
        [later] = (await added(client, project, paper("Later", "A later paragraph.")))["materials"]
        status = await idle(client, project)  # a later trigger's run reconciles all of it
        for material in (newer, later["id"]):
            assert status["materials"][material]["embedded"] == status["materials"][material]["embeddable"] > 0


async def test_a_launch_records_runs_left_running_interrupted_and_one_run_for_their_project(tmp_path, monkeypatch):
    """PR 29 finding 38, Harold's simplification (3): two index runs of one project recorded running (as finding 33's
    bug could leave them), one with a Rebuild pending: the next launch records both interrupted and one fresh run
    for the project, the rebuild, which does it; no second run, and nothing of the old ones runs."""
    from backend.runs import Harness
    from backend.search import _insert_run
    async with app(tmp_path, batch=1) as client:
        project = await project_of(client)
        client.remote.hold, client.remote.free = asyncio.Event(), 1
        await added(client, project, WAGES)
        await asyncio.wait_for(client.remote.reached.wait(), 10)
        [run] = await _running_index_runs(client, project)
        assert (await client.post(f"/api/projects/{project}/index/rebuild")).json() == {"run_id": run, "rebuild_pending": True}
        stale = await asyncio.to_thread(client.state["db"].write, lambda conn: _insert_run(conn, None, project, {}))
    started_for, real = [], Harness._background

    async def background(self, active):
        started_for.append(active.run_id)
        return await real(self, active)
    monkeypatch.setattr(Harness, "_background", background)
    async with app(tmp_path, batch=1) as client:
        status = await idle(client, project)
        old = {run, stale}
        for run_id in old:
            assert (await run_finished(client, run_id))["status"] == "interrupted"
        [fresh] = [r for r in await runs_of(client, "index", project) if r["run_id"] not in old]
        assert (fresh["status"], fresh["rebuild"], fresh["result"]["rebuilt"]) == ("succeeded", True, True)
        assert not old & set(started_for) and status["passages"]["embedded"] == 4


async def test_retry_after_a_failed_terminal_write_requests_the_projects_run_with_its_pending_rebuild(tmp_path,
                                                                                                      monkeypatch):
    """Review 4, item 3, as Harold's simplification (2) has it: a run whose terminal write failed (interrupted)
    had a Rebuild pending; Retry records it interrupted and requests the project's run, a rebuild, which runs.
    The Retry comes once the run is settled: its one start again in the session has failed too and no process
    holds it. A Retry while that start holds it answers 409 (CI on c9f1cd6, before PR 39, where a poll could
    read the run interrupted in the moment before that start held it)."""
    async with app(tmp_path, batch=1) as client:
        project, run = await _interrupted_with_a_rebuild_pending(client, monkeypatch)
        assert (await run_finished(client, run))["retryable"] is True
        again = await client.post(f"/api/runs/{run}/retry")
        assert again.status_code == 201 and again.json()["run_id"] != run
        ended = await run_finished(client, again.json()["run_id"])
        assert (ended["status"], ended["rebuild"], ended["result"]["rebuilt"]) == ("succeeded", True, True)
        assert await rows(client, "SELECT status FROM runs WHERE id = ?", run) == [("interrupted",)]


async def test_startup_reads_what_waits_once_before_any_pass_applies_the_queue(tmp_path, monkeypatch):
    """Review 4, item 4: additions left queued (a run cancelled before applying them) are seen at the next
    launch whichever way its reads and the queue's pass interleave: its requests come from reads made
    before the pass starts."""
    real_apply, real_reconcile = SearchIndex._apply, SearchIndex._reconcile
    async with app(tmp_path, install=False) as client:
        project = await project_of(client)
        monkeypatch.setattr(SearchIndex, "_apply", lambda self, stop=None: 0)  # the additions stay queued
        monkeypatch.setattr(SearchIndex, "_reconcile", lambda self, project_id, stop=None: (0, 0))
        await added(client, project, WAGES)
        await idle(client, project)
        assert await index_rows(client, project) == []
    monkeypatch.setattr(SearchIndex, "_apply", real_apply)
    monkeypatch.setattr(SearchIndex, "_reconcile", real_reconcile)
    real_unembedded = SearchIndex.unembedded

    def late_reader(self):  # reads, then lets a pass apply and prune the queue (if one runs) before answering
        found = real_unembedded(self)
        import sqlite3
        import time
        deadline = time.monotonic() + 2
        while time.monotonic() < deadline:
            with sqlite3.connect(self.db.path) as conn:
                if conn.execute("SELECT count(*) FROM index_queue").fetchone()[0] == 0:
                    break
            time.sleep(0.02)
        return found
    monkeypatch.setattr(SearchIndex, "unembedded", late_reader)
    async with app(tmp_path) as client:
        deadline = asyncio.get_running_loop().time() + 15
        while (await idle(client, project))["passages"]["embedded"] < 4:
            assert asyncio.get_running_loop().time() < deadline
            await asyncio.sleep(0.05)


async def test_retry_of_a_rebuild_while_another_rebuild_runs_is_that_rebuild(tmp_path):
    """Finding 23: one rebuild at a time per project, Retry included."""
    async with app(tmp_path, batch=1) as client:
        project = await project_of(client)
        await added(client, project, WAGES)
        await idle(client, project)
        old = (await client.post(f"/api/projects/{project}/index/rebuild")).json()["run_id"]
        await client.post(f"/api/runs/{old}/cancel")
        assert (await run_finished(client, old))["status"] == "cancelled"
        client.remote.hold, client.remote.free = asyncio.Event(), len(client.remote.indexing)
        running = (await client.post(f"/api/projects/{project}/index/rebuild")).json()["run_id"]
        await asyncio.wait_for(client.remote.reached.wait(), 10)
        retried = (await client.post(f"/api/runs/{old}/retry")).json()["run_id"]
        assert retried == running and await _running_index_runs(client, project) == [running]
        assert await _pending(client, running) is None
        client.remote.hold.set()
        assert (await run_finished(client, running))["status"] == "succeeded"
        await idle(client, project)
        assert await _running_index_runs(client, project) == []


async def test_an_index_run_a_lookup_records_starts_at_once_and_never_outlives_a_cancel(tmp_path, monkeypatch):
    """The internal review of 68c4cdd, P2: a lookup's title change asks for its project's index run mid-run;
    that run starts once the change commits (not at the lookup's end), so papers added while the lookup
    goes on are indexed, and a Cancel of the lookup leaves no run held that never starts."""
    from backend import lookup
    blocked, go = asyncio.Event(), asyncio.Event()

    async def resolve(client, scheme, identifier, pace):
        if identifier.endswith(".1"):
            await go.wait()  # once the readings' index run has ended: the title change records the project's run
            return lookup.Found("openalex", {"title": "Wage Floors, as Published", "DOI": identifier}, None,
                                "openalex:W1")
        await blocked.wait()  # the second identifier: the lookup goes on
        raise lookup.Failed("unavailable")
    monkeypatch.setattr(lookup, "resolve", resolve)
    async with app(tmp_path) as client:
        project = await project_of(client)
        one = ("one.md", b"# Wage Floors\n\ndoi:10.5555/s117.1\n\n## Findings\n\nMinimum wages raise earnings.\n")
        two = ("two.md", b"# Price Notes\n\ndoi:10.5555/s117.2\n\n## Findings\n\nRegional prices adjust slowly.\n")
        result = await added(client, project, one, two)
        await idle(client, project)
        go.set()
        deadline = asyncio.get_running_loop().time() + 15
        while not await rows(client, "SELECT 1 FROM materials WHERE title = 'Wage Floors, as Published'"):
            assert asyncio.get_running_loop().time() < deadline  # the first identifier's title applied
            await asyncio.sleep(0.05)
        [third] = (await added(client, project, paper("Third Paper", "A paragraph added during the lookup.")))["materials"]
        deadline = asyncio.get_running_loop().time() + 10
        while not [r for r in await index_rows(client, project) if r[1] == third["id"]]:
            assert asyncio.get_running_loop().time() < deadline  # indexed while the lookup goes on
            await asyncio.sleep(0.05)
        await client.post(f"/api/runs/{result['lookup_run_id']}/cancel")
        assert (await run_finished(client, result["lookup_run_id"]))["status"] == "cancelled"
        await idle(client, project)
        registry = client.state["harness"].registry
        assert [r for r in registry.runs.values() if r.kind == "background" and r.task is None] == []
        [fourth] = (await added(client, project, paper("Fourth Paper", "A paragraph after the Cancel.")))["materials"]
        status = await idle(client, project)
        assert status["materials"][fourth["id"]]["embedded"] == status["materials"][fourth["id"]]["embeddable"] > 0


async def test_an_index_run_a_lookup_records_starts_though_the_lookup_is_cancelled_as_that_write_commits(
        tmp_path, monkeypatch):
    """The internal review of b682b9b, P2-3: a Cancel reaching the lookup at the await of a write that has
    committed (and recorded its project's index run) never reaches the lookup's own start of that run; the
    run's end starts it all the same, so it never stays held with no task and later papers are indexed."""
    from backend import lookup, materials
    blocked, go = asyncio.Event(), asyncio.Event()

    async def resolve(client, scheme, identifier, pace):
        if identifier.endswith(".1"):
            await go.wait()  # once the readings' index run has ended: the title change records the project's run
            return lookup.Found("openalex", {"title": "Wage Floors, as Published", "DOI": identifier}, None,
                                "openalex:W1")
        await blocked.wait()
        raise lookup.Failed("unavailable")
    monkeypatch.setattr(lookup, "resolve", resolve)
    real, fired = materials._write_if_running, []

    async def cancelled_as_it_commits(write, run_id, fn):
        made = await real(write, run_id, fn)
        if made and not fired:
            fired.append(True)
            raise asyncio.CancelledError()  # as a Cancel arriving at this await once the write has committed
        return made
    monkeypatch.setattr(materials, "_write_if_running", cancelled_as_it_commits)
    async with app(tmp_path) as client:
        project = await project_of(client)
        one = ("one.md", b"# Wage Floors\n\ndoi:10.5555/s117.1\n\n## Findings\n\nMinimum wages raise earnings.\n")
        two = ("two.md", b"# Price Notes\n\ndoi:10.5555/s117.2\n\n## Findings\n\nRegional prices adjust slowly.\n")
        result = await added(client, project, one, two)
        await idle(client, project)
        go.set()
        await run_finished(client, result["lookup_run_id"])
        assert fired and await rows(client, "SELECT 1 FROM materials WHERE title = 'Wage Floors, as Published'")
        await idle(client, project)
        assert {r["status"] for r in await runs_of(client, "index", project)} == {"succeeded"}  # the one recorded ran
        registry = client.state["harness"].registry
        assert [r for r in registry.runs.values() if r.kind == "background" and r.task is None] == []
        [later] = (await added(client, project, paper("Later Paper", "A paragraph after the Cancel.")))["materials"]
        status = await idle(client, project)
        assert status["materials"][later["id"]]["embedded"] == status["materials"][later["id"]]["embeddable"] > 0


# From the internal review of 68c4cdd


async def test_a_launch_records_one_fresh_run_and_no_rerun_after_it(tmp_path):
    """Finding 4(b), as Harold's simplification (3) has it: the run a shutdown left running is recorded
    interrupted at the next launch, and the project gets one fresh run, unmarked (it applies the queue, reconciles
    and embeds what is missing, all a plain run does): no second run follows."""
    async with app(tmp_path, batch=1) as client:
        client.remote.hold, client.remote.free = asyncio.Event(), 2
        project = await project_of(client)
        await added(client, project, WAGES)
        await asyncio.wait_for(client.remote.reached.wait(), 10)  # shut down with the paper half embedded
        [run] = [r["run_id"] for r in await runs_of(client, "index", project) if r["status"] == "running"]
        before = {r["run_id"] for r in await runs_of(client, "index", project)}
    async with app(tmp_path, batch=1) as client:  # its unembedded passages, and the run left, ask for one run
        assert (await idle(client, project))["passages"]["embedded"] == 4
        assert (await run_finished(client, run))["status"] == "interrupted"
        [fresh] = [r for r in await runs_of(client, "index", project) if r["run_id"] not in before]
        assert fresh["status"] == "succeeded" and await _pending(client, fresh["run_id"]) is None


async def test_the_index_status_shows_the_latest_run_over_an_older_one_left_running_in_its_record(tmp_path):
    """Finding 6: a run whose terminal write failed stays running in its record (it reads interrupted) and
    is still its project's run: a reading marks it to run once more; a kick (a turn's end) starts it, and the
    index status then shows the project's latest run, the rerun that finished, not that one."""
    from backend.search import _insert_run
    async with app(tmp_path) as client:
        project = await project_of(client)
        stale = await asyncio.to_thread(client.state["db"].write, lambda conn: _insert_run(conn, None, project, {}))
        await added(client, project, WAGES)
        await _readings_done(client, project)
        assert await _running_index_runs(client, project) == [stale] and await _pending(client, stale) == {"rebuild": False}
        await client.state["harness"].kick_background()
        await run_finished(client, stale)
        status = await idle(client, project)
        [latest] = [r["run_id"] for r in await runs_of(client, "index", project) if r["run_id"] != stale]
        assert (status["run"]["run_id"], status["run"]["status"]) == (latest, "succeeded")
        assert status["passages"]["embedded"] == 4


# Harold's simplification of the pending rerun (2026-10-15): a request while the project's run runs marks it and
# gets its id; a Retry records the old run interrupted and requests; a launch records every run left running
# interrupted and one fresh run per project with work left. No pending run has an id before it is recorded.


async def _interrupted_with_a_rebuild_pending(client, monkeypatch):
    """A project's run whose terminal write failed (left running, held by no process: it reads interrupted) with a
    Rebuild pending (the app with batch=1): (project, run). Its one start again in the session (Harness, after a
    failed terminal write) fails the same way, so it is left so."""
    from backend.runs import Harness
    real = Harness._finish_local
    project = await project_of(client)
    client.remote.hold, client.remote.free = asyncio.Event(), 1
    await added(client, project, WAGES)
    await asyncio.wait_for(client.remote.reached.wait(), 10)
    [run] = await _running_index_runs(client, project)
    assert (await client.post(f"/api/projects/{project}/index/rebuild")).json() == {"run_id": run, "rebuild_pending": True}

    failures = []

    def failing(self, conn, active, status, cancel_reason, summary, effect=None):
        if active.run_id == run:
            failures.append(True)
            raise OSError("the disk is full")
        return real(self, conn, active, status, cancel_reason, summary, effect)
    monkeypatch.setattr(Harness, "_finish_local", failing)
    client.remote.hold.set()
    harness = client.state["harness"]
    deadline = asyncio.get_running_loop().time() + 20
    while not (len(failures) == 2 and not harness.registry.is_active(run)):  # its one start again has failed too
        assert asyncio.get_running_loop().time() < deadline
        await asyncio.sleep(0.02)
    assert (await run_finished(client, run))["status"] == "interrupted"
    monkeypatch.setattr(Harness, "_finish_local", real)
    client.remote.hold = None
    return project, run


def _counting_starts(monkeypatch):
    from backend.runs import Harness
    real, started_for = Harness._background, []

    async def background(self, active):
        started_for.append(active.run_id)
        return await real(self, active)
    monkeypatch.setattr(Harness, "_background", background)
    return started_for


async def test_a_request_after_a_failed_terminal_write_marks_that_run_and_a_kick_runs_it_once(tmp_path, monkeypatch):
    """Recheck 5, finding 33, under Harold's simplification: the run whose terminal write failed is still its
    project's run, so a later request (a reading) records no second run: it marks that run (its Rebuild still
    pending) and gets its id. A kick (a turn's end) starts it, alone; its end records the rebuild once."""
    started_for = _counting_starts(monkeypatch)
    async with app(tmp_path, batch=1) as client:
        project, run = await _interrupted_with_a_rebuild_pending(client, monkeypatch)
        await added(client, project, paper("Later", "A later paragraph."))
        await _readings_done(client, project)
        assert await _running_index_runs(client, project) == [run] and await _pending(client, run) == {"rebuild": True}
        harness, starts = client.state["harness"], started_for.count(run)
        await harness.kick_background()
        await harness.kick_background()  # a second kick holds nothing more
        assert await _running_index_runs(client, project) == [run] and started_for.count(run) == starts + 1
        assert (await run_finished(client, run))["status"] == "succeeded"
        [rerun] = [r for r in await runs_of(client, "index", project) if r["run_id"] != run]
        assert (rerun["rebuild"], (await run_finished(client, rerun["run_id"]))["status"]) == (True, "succeeded")
        status = await idle(client, project)
        assert status["passages"]["embedded"] == status["passages"]["embeddable"] > 4
        assert len(await runs_of(client, "index", project)) == 2


async def test_a_retry_and_a_request_at_once_record_one_run_whose_end_commits(tmp_path, monkeypatch):
    """PR 29 finding 36 cannot occur: a Retry of the run left running, its write held while a reading's request
    marks that run, then committing: the old run is recorded interrupted and the Retry's request records one run,
    the rebuild (the pending rebuild carried), whose terminal transaction commits; no other run, and no id given
    out without a run."""
    async with app(tmp_path, batch=1) as client:
        project, run = await _interrupted_with_a_rebuild_pending(client, monkeypatch)
        entered, go = _held_retry_write(monkeypatch)
        retry = asyncio.ensure_future(client.post(f"/api/runs/{run}/retry"))
        assert await asyncio.to_thread(entered.wait, 10)
        await added(client, project, paper("Meanwhile", "A paragraph read while the Retry waited."))
        await _readings_done(client, project)
        assert await _running_index_runs(client, project) == [run]  # marked, never a second run
        go.set()
        again = (await retry).json()["run_id"]
        assert await rows(client, "SELECT status FROM runs WHERE id = ?", run) == [("interrupted",)]
        ended = await run_finished(client, again)
        assert (ended["status"], ended["rebuild"], ended["result"]["rebuilt"]) == ("succeeded", True, True)
        await idle(client, project)
        assert {r["run_id"] for r in await runs_of(client, "index", project)} == {run, again}
        assert await rows(client, "SELECT count(*) FROM runs WHERE workflow = 'index' AND status = 'cancelled'") == [(0,)]


async def test_two_retries_at_once_are_answered_with_one_run(tmp_path, monkeypatch):
    """A second Retry pressed while the first's write waits reads what the first wrote: both answers are the one
    run the first recorded."""
    async with app(tmp_path, batch=1) as client:
        project, run = await _interrupted_with_a_rebuild_pending(client, monkeypatch)
        entered, go = _held_retry_write(monkeypatch)
        first = asyncio.ensure_future(client.post(f"/api/runs/{run}/retry"))
        assert await asyncio.to_thread(entered.wait, 10)
        second = asyncio.ensure_future(client.post(f"/api/runs/{run}/retry"))
        await asyncio.sleep(0.3)
        go.set()
        answers = [await first, await second]
        assert [a.status_code for a in answers] == [201, 201] and answers[0].json() == answers[1].json()
        assert (await run_finished(client, answers[0].json()["run_id"]))["status"] == "succeeded"
        assert len(await runs_of(client, "index", project)) == 2


async def test_a_retry_of_the_run_still_running_is_refused_and_its_end_records_its_rerun(tmp_path, monkeypatch):
    """Retry pressed on the project's run while it runs with a Rebuild pending, the run ending before the Retry's
    write: the Retry is refused (it ended, succeeded), and the run's end recorded and started the rebuild."""
    async with app(tmp_path, batch=1) as client:
        project = await project_of(client)
        client.remote.hold, client.remote.free = asyncio.Event(), 1
        await added(client, project, WAGES)
        await asyncio.wait_for(client.remote.reached.wait(), 10)
        [run] = await _running_index_runs(client, project)
        await client.post(f"/api/projects/{project}/index/rebuild")
        entered, go = _held_retry_write(monkeypatch)
        retry = asyncio.ensure_future(client.post(f"/api/runs/{run}/retry"))
        assert await asyncio.to_thread(entered.wait, 10)
        client.remote.hold.set()
        assert (await run_finished(client, run))["status"] == "succeeded"
        go.set()
        assert (await retry).status_code == 409
        [rerun] = [r["run_id"] for r in await runs_of(client, "index", project) if r["run_id"] != run]
        ended = await run_finished(client, rerun)
        assert (ended["status"], ended["rebuild"]) == ("succeeded", True)


def _held_retry_write(monkeypatch):
    """The next Retry's write waits to begin until go is set; entered is set as it waits."""
    import threading
    from backend.db import Database
    real, armed, entered, go = Database.write, [True], threading.Event(), threading.Event()

    def write(self, fn):
        if armed and "record_background" in fn.__qualname__:
            armed.clear()
            entered.set()
            go.wait(10)
        return real(self, fn)
    monkeypatch.setattr(Database, "write", write)
    return entered, go


async def test_a_kick_while_a_retry_waits_runs_the_run_and_the_retry_is_refused(tmp_path, monkeypatch):
    """A kick (a turn's end) while a Retry's write waits holds the run left running and starts it: the Retry,
    finding it held, is refused (it runs); its end records its pending rebuild. One run at a time."""
    async with app(tmp_path, batch=1) as client:
        project, run = await _interrupted_with_a_rebuild_pending(client, monkeypatch)
        entered, go = _held_retry_write(monkeypatch)
        retry = asyncio.ensure_future(client.post(f"/api/runs/{run}/retry"))
        assert await asyncio.to_thread(entered.wait, 10)
        await client.state["harness"].kick_background()
        assert run in client.state["harness"].registry.runs
        go.set()
        assert (await retry).status_code == 409
        assert (await run_finished(client, run))["status"] == "succeeded"
        [rerun] = [r["run_id"] for r in await runs_of(client, "index", project) if r["run_id"] != run]
        assert ((await run_finished(client, rerun))["status"], len(await runs_of(client, "index", project))) == ("succeeded", 2)


@pytest.mark.parametrize("when", ["before", "during_its_write"])
async def test_a_retry_while_the_app_closes_is_refused_and_leaves_the_run_to_the_resume(tmp_path, monkeypatch, when):
    """A Retry while the harness is closed (a restore starting, that then does not go ahead), or that the closing
    overtakes while its write waits, is refused as the app closing, and leaves the run as it was: the resume
    starts it again, and its end records its pending rebuild."""
    async with app(tmp_path, batch=1) as client:
        project, run = await _interrupted_with_a_rebuild_pending(client, monkeypatch)
        harness = client.state["harness"]
        if when == "before":
            harness.registry.closed = True
            assert (await client.post(f"/api/runs/{run}/retry")).status_code == 503
        else:
            entered, go = _held_retry_write(monkeypatch)
            retry = asyncio.ensure_future(client.post(f"/api/runs/{run}/retry"))
            assert await asyncio.to_thread(entered.wait, 10)
            harness.registry.closed = True
            go.set()
            assert (await retry).status_code == 503
        harness.registry.closed = False
        await harness.kick_background()
        assert (await run_finished(client, run))["status"] == "succeeded"
        [rerun] = [r["run_id"] for r in await runs_of(client, "index", project) if r["run_id"] != run]
        ended = await run_finished(client, rerun)
        assert (ended["status"], ended["rebuild"]) == ("succeeded", True)


async def test_a_request_while_the_app_closes_marks_the_run_left_running(tmp_path, monkeypatch):
    """A request while the app closes (a restore starting, or shutdown) records no second run: it marks the project's
    run, left running, and gets its id. The resume starts that run, whose end records the rerun: one at a time."""
    import backend.search as search
    async with app(tmp_path, batch=1) as client:
        project, run = await _interrupted_with_a_rebuild_pending(client, monkeypatch)
        harness = client.state["harness"]
        harness.registry.closed = True
        asked = await asyncio.to_thread(client.state["db"].write, lambda conn: search.request_run(
            conn, project, lambda conn, project, inputs: search._insert_run(conn, None, project, inputs)))
        assert asked == run and await _running_index_runs(client, project) == [run]
        harness.registry.closed = False
        await harness.kick_background()
        assert (await run_finished(client, run))["status"] == "succeeded"
        [rerun] = [r["run_id"] for r in await runs_of(client, "index", project) if r["run_id"] != run]
        ended = await run_finished(client, rerun)
        assert (ended["status"], ended["rebuild"]) == ("succeeded", True)


async def test_a_rebuild_whose_projects_run_no_process_holds_starts_that_run_and_then_the_rebuild(tmp_path, monkeypatch):
    """Review 4, F1: a Rebuild while the project's run is one a failed terminal write left running (held by no
    process): the answer is that run, which the Rebuild starts at once (no dead end until a turn's end), and the
    rebuild its end records follows it, as the interface follows them."""
    async with app(tmp_path, batch=1) as client:
        project = await project_of(client)
        client.remote.hold, client.remote.free = asyncio.Event(), 1
        await added(client, project, WAGES)
        await asyncio.wait_for(client.remote.reached.wait(), 10)
        [run] = await _running_index_runs(client, project)
        from backend.runs import Harness
        real = Harness._finish_local

        def failing(self, conn, active, status, cancel_reason, summary, effect=None):
            if active.run_id == run:
                raise OSError("the disk is full")
            return real(self, conn, active, status, cancel_reason, summary, effect)
        monkeypatch.setattr(Harness, "_finish_local", failing)
        client.remote.hold.set()
        assert (await run_finished(client, run))["status"] == "interrupted"
        monkeypatch.setattr(Harness, "_finish_local", real)
        answer = (await client.post(f"/api/projects/{project}/index/rebuild")).json()
        assert answer == {"run_id": run, "rebuild_pending": True}
        assert client.state["harness"].registry.is_active(run)  # started by the Rebuild
        assert (await run_finished(client, run))["status"] == "succeeded"
        [rebuild] = [r for r in await runs_of(client, "index", project) if r["run_id"] != run]
        ended = await run_finished(client, rebuild["run_id"])
        assert (ended["status"], ended["rebuild"], ended["result"]["rebuilt"]) == ("succeeded", True, True)


async def test_a_retry_answered_with_a_newer_run_no_process_holds_starts_it(tmp_path):
    """PR 29 finding 52: a Retry of an older failed index run while a newer one is left running in its record (a failed
    terminal write; held by no process) is answered with that newer run, which the Retry starts at once, as a
    Rebuild or any request does."""
    from backend.search import _insert_run
    async with app(tmp_path) as client:
        project = await project_of(client)
        await added(client, project, WAGES)
        await idle(client, project)
        db = client.state["db"]

        def old_and_new(conn):
            old = _insert_run(conn, None, project, {})
            conn.execute("UPDATE runs SET status = 'failed', finished_at = ? WHERE id = ?", ("2026-10-15T00:00:00.000Z", old))
            return old, _insert_run(conn, None, project, {})
        old, newer = await asyncio.to_thread(db.write, old_and_new)
        answer = await client.post(f"/api/runs/{old}/retry")
        assert answer.status_code == 201 and answer.json()["run_id"] == newer
        assert client.state["harness"].registry.is_active(newer)  # started by the Retry
        assert (await run_finished(client, newer))["status"] == "succeeded"


async def test_the_index_status_names_the_rebuild_a_run_recorded_and_the_run_it_follows(tmp_path):
    """PR 29 finding 53: the rebuild a run's end records says which run it follows, and the index status names the
    project's latest rebuild with it, so the interface follows that rebuild to its end whatever runs after it."""
    async with app(tmp_path, batch=1) as client:
        project = await project_of(client)
        client.remote.hold, client.remote.free = asyncio.Event(), 1
        await added(client, project, WAGES)
        await asyncio.wait_for(client.remote.reached.wait(), 10)
        [run] = await _running_index_runs(client, project)
        assert (await client.post(f"/api/projects/{project}/index/rebuild")).json() == {"run_id": run, "rebuild_pending": True}
        client.remote.hold.set()
        assert (await run_finished(client, run))["status"] == "succeeded"
        last = (await client.get(f"/api/projects/{project}/index")).json()["last_rebuild"]
        assert last["after"] == run and last["run_id"] != run
        ended = await run_finished(client, last["run_id"])
        assert (ended["status"], ended["rebuild"]) == ("succeeded", True)
        # a later rebuild recorded since (another tab's): the one recorded as that run ended is still found by it
        later = (await client.post(f"/api/projects/{project}/index/rebuild")).json()["run_id"]
        await run_finished(client, later)
        assert (await client.get(f"/api/projects/{project}/index")).json()["last_rebuild"]["run_id"] == later
        found = (await client.get(f"/api/projects/{project}/index", params={"after": run})).json()["last_rebuild"]
        assert (found["run_id"], found["after"]) == (last["run_id"], run)


# PR 29 recheck 7 and connector threads 4283763992, 4283764003, 4296864820: a run left running is always started again


async def test_an_index_run_whose_terminal_write_fails_once_is_started_again_and_runs_its_rerun(tmp_path, monkeypatch):
    """Thread 4283763992: a request marks the project's run just before that run's terminal write fails. The harness,
    releasing the run it no longer holds, starts it again in the session (once), with no other trigger, so the rerun
    the request asked for runs."""
    from backend.runs import Harness
    real, failed = Harness._finish_local, []
    async with app(tmp_path, batch=1) as client:
        project = await project_of(client)
        client.remote.hold, client.remote.free = asyncio.Event(), 1
        await added(client, project, WAGES)
        await asyncio.wait_for(client.remote.reached.wait(), 10)
        [run] = await _running_index_runs(client, project)
        assert (await client.post(f"/api/projects/{project}/index/rebuild")).json()["run_id"] == run  # marked

        def failing_once(self, conn, active, status, cancel_reason, summary, effect=None):
            if active.run_id == run and not failed:
                failed.append(True)
                raise OSError("the disk is full")
            return real(self, conn, active, status, cancel_reason, summary, effect)
        monkeypatch.setattr(Harness, "_finish_local", failing_once)
        client.remote.hold.set()
        deadline = asyncio.get_running_loop().time() + 20
        while (await rows(client, "SELECT status FROM runs WHERE id = ?", run)) != [("succeeded",)]:
            assert asyncio.get_running_loop().time() < deadline  # started again on its own: no kick, no trigger
            await asyncio.sleep(0.05)
        assert failed
        [rerun] = [r["run_id"] for r in await runs_of(client, "index", project) if r["run_id"] != run]
        ended = await run_finished(client, rerun)
        assert (ended["status"], ended["rebuild"]) == ("succeeded", True)


async def test_an_index_run_started_again_after_its_terminal_write_fails_never_reads_interrupted_meanwhile(
        tmp_path, monkeypatch):
    """Connector thread 4300055389 (finding 69): an index run whose terminal write fails once is started again, and it
    stays held across that restart: a poll in the handoff (its old hold released) reads it running, not interrupted,
    so the interface's follow of the asked-for Rebuild goes on."""
    from backend.runs import Harness, Registry, derived_status
    real, real_release, failed, seen = Harness._finish_local, Registry.release, [], []
    async with app(tmp_path, batch=1) as client:
        project = await project_of(client)
        client.remote.hold, client.remote.free = asyncio.Event(), 1
        await added(client, project, WAGES)
        await asyncio.wait_for(client.remote.reached.wait(), 10)
        [run] = await _running_index_runs(client, project)
        assert (await client.post(f"/api/projects/{project}/index/rebuild")).json()["run_id"] == run  # marked

        def failing_once(self, conn, active, status, cancel_reason, summary, effect=None):
            if active.run_id == run and not failed:
                failed.append(True)
                raise OSError("the disk is full")
            return real(self, conn, active, status, cancel_reason, summary, effect)

        def release(self, active):  # each moment after the failed run's hold is released, as a poll would read it
            real_release(self, active)
            if active.run_id == run and failed and not seen:
                seen.append(derived_status("running", run, self))
        monkeypatch.setattr(Harness, "_finish_local", failing_once)
        monkeypatch.setattr(Registry, "release", release)
        client.remote.hold.set()
        deadline = asyncio.get_running_loop().time() + 20
        while (await rows(client, "SELECT status FROM runs WHERE id = ?", run)) != [("succeeded",)]:
            assert asyncio.get_running_loop().time() < deadline
            await asyncio.sleep(0.05)
        assert failed and seen == ["running"]  # held across the restart: never interrupted
        [rerun] = [r["run_id"] for r in await runs_of(client, "index", project) if r["run_id"] != run]
        assert (await run_finished(client, rerun))["status"] == "succeeded"

async def test_a_request_cancelled_after_its_commit_still_starts_the_run_it_was_answered_with(tmp_path):
    """Threads 4283764003 and 4296864820: a request (as a Rebuild's, a Retry's or a trigger's) whose caller is
    cancelled after its transaction commits: the run it was answered with, held by no process, is started all the
    same, since the start runs once the write has committed, whatever becomes of the request."""
    import threading
    import backend.search as search
    from backend.db import Database
    from backend.search import _insert_run
    async with app(tmp_path) as client:
        project = await project_of(client)
        await added(client, project, WAGES)
        await idle(client, project)
        stale = await asyncio.to_thread(client.state["db"].write, lambda conn: _insert_run(conn, None, project, {}))
        harness = client.state["harness"]
        real_write, entered, go = Database.write, threading.Event(), threading.Event()

        def write(self, fn):
            if "record_background" in fn.__qualname__ and not entered.is_set():
                entered.set()
                go.wait(10)
            return real_write(self, fn)
        Database.write = write
        try:
            request = asyncio.ensure_future(search._request(harness, project))
            assert await asyncio.to_thread(entered.wait, 10)
            request.cancel()  # the caller goes before its write has run; the write still commits
            go.set()
            with pytest.raises(asyncio.CancelledError):
                await request
        finally:
            Database.write = real_write
        deadline = asyncio.get_running_loop().time() + 20
        while await rows(client, "SELECT status FROM runs WHERE id = ?", stale) != [("succeeded",)]:
            assert asyncio.get_running_loop().time() < deadline  # started all the same, and ran to its end
            await asyncio.sleep(0.02)


async def test_a_retry_pressed_again_starts_the_run_it_was_answered_with_if_no_process_holds_it(tmp_path):
    """Thread 4283764003: a second Retry of a run that was tried again already answers with the stored run, and
    starts it if no process holds it (its first start was lost), instead of leaving it waiting."""
    from backend.search import _insert_run
    async with app(tmp_path) as client:
        project = await project_of(client)
        await added(client, project, WAGES)
        await idle(client, project)

        def tried(conn):
            newer = _insert_run(conn, None, project, {})
            old = _insert_run(conn, None, project, {})
            conn.execute("UPDATE runs SET status = 'failed', finished_at = ?, inputs = json_set(inputs, '$.retried_by', ?)"
                         " WHERE id = ?", ("2026-10-17T00:00:00.000Z", newer, old))
            return old, newer
        old, newer = await asyncio.to_thread(client.state["db"].write, tried)
        assert not client.state["harness"].registry.is_active(newer)
        answer = await client.post(f"/api/runs/{old}/retry")
        assert answer.status_code == 201 and answer.json()["run_id"] == newer
        assert client.state["harness"].registry.is_active(newer)
        assert (await run_finished(client, newer))["status"] == "succeeded"


async def test_starting_a_run_left_running_starts_that_run_only(tmp_path, monkeypatch):
    """Review 9, P3-2: the start of a run a request was answered with (or of an index run left running) starts that
    run only: another run left running, held by no process (a reading whose terminal write failed, which reads
    interrupted with Retry), is left for its own rules, not started by it."""
    from backend.search import _insert_run
    started_for = _counting_starts(monkeypatch)
    async with app(tmp_path) as client:
        project = await project_of(client)
        await added(client, project, WAGES)
        await idle(client, project)

        def left(conn):
            stale = _insert_run(conn, None, project, {})
            reading = "00000000-0000-4000-8000-0000000000bb"
            conn.execute("INSERT INTO runs (id, project_id, kind, workflow, inputs) VALUES (?, ?, 'background', 'extract',"
                         " ?)", (reading, project, json.dumps({"material_ids": [], "version_id": None})))
            return stale, reading
        stale, reading = await asyncio.to_thread(client.state["db"].write, left)
        answer = (await client.post(f"/api/projects/{project}/index/rebuild")).json()
        assert answer["run_id"] == stale and stale in started_for
        assert (await run_finished(client, stale))["status"] == "succeeded"
        assert reading not in started_for  # left as it was
