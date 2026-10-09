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

import httpx
import pytest

from backend import local_helper
from backend.local_helper import EMBEDDING, Config
from backend.search_index import SearchIndex
import synthetic_materials as synthetic
from network_guard import allow_subprocess
from scholia_app import run_finished, started
from test_local_helper import PIN, TIMINGS, WEIGHTS, Fake
from test_materials import added, project_of, rows

pytestmark = pytest.mark.asyncio


vector = synthetic.embedding


class Remote:
    """Everything the gate lets out: the stand-in helper's HTTP side (health and embeddings, with the key
    of the server launched last), the model download source (`files`), and 404 for anything else (the
    scholarly sources). `embedded` keeps each embedding request's texts in order; while `hold` is set,
    indexing requests after the first `free` wait for it (`reached` set as one arrives); `failing`
    answers them 500; `slow_queries` delays a query's answer by that many seconds."""

    def __init__(self, fake):
        self.fake = fake
        self.embedded, self.sources, self.files = [], [], {}
        self.hold, self.reached, self.free = None, asyncio.Event(), 0
        self.failing = False
        self.slow_queries = 0

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
        texts = json.loads(request.content)["input"]
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
        assert run["materials"]["titles"] == ["Wage Floors"]
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
        assert found["coverage"] == {"embedded": 0, "total": 4}
        client.remote.failing = False
        again = await client.post(f"/api/runs/{run['run_id']}/retry")
        assert (await run_finished(client, again.json()["run_id"]))["status"] == "succeeded"
        assert (await idle(client, project))["passages"]["embedded"] == 4


# Lifecycle: deletion while indexing runs


@pytest.mark.parametrize("deleted", ["material", "project"])
async def test_deleting_mid_embedding_revokes_the_run_and_nothing_of_it_is_sent_again_or_kept(tmp_path, deleted):
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
            assert (ended["status"], ended["cancel_reason"]) == ("cancelled", "revoked")
            assert (await find(client, project, "earnings"))["results"] == []


async def test_a_deleted_papers_index_run_leaves_its_other_papers_to_a_new_run(tmp_path):
    async with app(tmp_path, batch=1) as client:
        project = await project_of(client)
        first = (await added(client, project, WAGES))["materials"][0]
        second = (await added(client, project, paper("Second Paper", "A paragraph about synthetic cities.")))["materials"][0]
        await idle(client, project)
        client.remote.hold = asyncio.Event()
        response = await client.post(f"/api/projects/{project}/index/rebuild")  # names both papers
        assert response.status_code == 202
        rebuild = response.json()["run_id"]
        await asyncio.wait_for(client.remote.reached.wait(), 10)
        assert (await client.delete(f"/api/materials/{first['id']}")).status_code == 200
        ended = await run_finished(client, rebuild)
        assert (ended["status"], ended["cancel_reason"]) == ("cancelled", "revoked")
        follow = [r for r in await runs_of(client, "index", project) if r["status"] == "running"]
        assert [r["materials"]["titles"] for r in follow] == [["Second Paper"]]
        client.remote.hold.set()
        status = await idle(client, project)
        assert list(status["materials"]) == [second["id"]]
        assert status["passages"]["embedded"] == status["passages"]["embeddable"] > 0


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
    async with app(tmp_path) as client:
        project = await project_of(client)
        [material] = (await added(client, project, WAGES))["materials"]
        await idle(client, project)
        real = SearchIndex._verify
        monkeypatch.setattr(SearchIndex, "_verify", lambda self, rowids: (_ for _ in ()).throw(RuntimeError("disk")))
        assert (await client.delete(f"/api/materials/{material['id']}")).status_code == 200
        assert len(await index_rows(client, project)) == 4  # the pass rolled back: still there
        assert await rows(client, "SELECT count(*) FROM index_queue WHERE op = 'remove'") == [(4,)]  # still queued
        assert (await find(client, project, "earnings"))["results"] == []  # the access check keeps it out
        monkeypatch.setattr(SearchIndex, "_verify", real)
        await asyncio.to_thread(client.state["index"].apply)
        assert await index_rows(client, project) == []
        assert await rows(client, "SELECT count(*) FROM index_queue") == [(0,)]


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
    async with app(tmp_path) as client:  # the next launch starts it again: it finds its work done
        status = await idle(client, project)
        assert await index_rows(client, project) == before and status["passages"]["embedded"] == 4
        assert client.remote.indexing == [] and sent == 4
        [run] = await runs_of(client, "index", project)
        assert run["status"] == "succeeded"


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
        assert (run["status"], run["result"]) == ("succeeded", {"mode": "keyword_only", "reason": "model_missing"})


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
        assert status["passages"] == {"indexed": 4, "embedded": 4, "embeddable": 4}
        assert status["materials"] == {material["id"]: {"indexed": 4, "embedded": 4, "embeddable": 4}}
        before = len(client.remote.indexing)
        response = await client.post(f"/api/projects/{project}/index/rebuild")
        assert response.status_code == 202
        assert (await run_finished(client, response.json()["run_id"]))["status"] == "succeeded"
        status = await idle(client, project)
        assert status["run"] == {"run_id": response.json()["run_id"], "status": "succeeded", "rebuild": True,
                                 "progress": None}
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
    async with app(tmp_path, batch=1) as client:  # the run is still running in the record: started again
        status = await idle(client, project)
        assert status["passages"]["embedded"] == 4 and len(client.remote.indexing) == 2
        assert (await run_finished(client, run["run_id"]))["status"] == "succeeded"


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
    import backend.extraction as extraction
    monkeypatch.setitem(extraction.EXTRACTORS, extraction.MARKDOWN, ("markdown", "markdown-0"))
    async with app(tmp_path) as client:
        project = await project_of(client)
        real = SearchIndex._apply
        monkeypatch.setattr(SearchIndex, "_apply", lambda self: 0)  # the first reading's additions wait in the queue
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
        await asyncio.to_thread(client.state["index"].apply)
        newer = {p for (p,) in await rows(client, "SELECT id FROM passages")}
        assert {r[0] for r in await index_rows(client, project)} == newer and not newer & older
        assert await rows(client, "SELECT count(*) FROM index_queue") == [(0,)]
