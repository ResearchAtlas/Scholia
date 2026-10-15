"""Degradation (slice-1 spec sections 7.2, 14 and F3a; test_degradation.py), for what S1-17 builds:
when the search model is missing, the helper cannot serve, sqlite-vec cannot load or the index cannot
open, search is keyword-only and says why; the papers stay searchable by keyword. sqlite-vec and the
index itself are in test_search_index.py, the hybrid deadline in test_retrieval_bounds.py."""

import asyncio

import pytest

from backend import local_helper
from backend.local_helper import EMBEDDING
from test_local_helper import PIN
from test_materials import added, project_of
from test_search import WAGES, app, find, idle

pytestmark = pytest.mark.asyncio


async def test_without_the_model_search_is_keyword_only_and_indexing_waits_for_it(tmp_path):
    async with app(tmp_path, install=False) as client:
        project = await project_of(client)
        await added(client, project, WAGES)
        status = await idle(client, project)
        assert (status["mode"], status["reason"]) == ("keyword_only", "model_missing")
        assert status["passages"] == {"indexed": 4, "embedded": 0, "embeddable": 4}
        found = await find(client, project, "earnings")
        assert (found["mode"], found["reason"]) == ("keyword_only", "model_missing") and found["results"]
        assert client.remote.embedded == []


async def test_a_helper_that_cannot_start_leaves_keyword_search_and_says_why(tmp_path):
    async with app(tmp_path) as client:
        project = await project_of(client)
        (client.remote.fake.control / "behavior").write_text("exit")  # its start fails
        await added(client, project, WAGES)
        await idle(client, project)
        [run] = [r for r in (await client.get("/api/activity")).json()["runs"] if r["workflow"] == "index"]
        assert (run["status"], run["result"]["reason"]) == ("failed", "start_failed")
        helper = client.state["local_helper"].helpers[EMBEDDING]
        helper.state, helper.problem = "failed", "start_failed"  # after its last restart (S1-16's notice)
        found = await find(client, project, "earnings")
        assert (found["mode"], found["reason"]) == ("keyword_only", "helper_failed") and found["results"]
        status = (await client.get(f"/api/projects/{project}/index")).json()
        assert (status["mode"], status["reason"]) == ("keyword_only", "helper_failed")


async def test_a_query_the_helper_fails_to_embed_gets_keyword_results_said(tmp_path):
    async with app(tmp_path) as client:
        project = await project_of(client)
        await added(client, project, WAGES)
        await idle(client, project)
        client.remote.failing = True
        found = await find(client, project, "earnings")
        assert (found["mode"], found["reason"]) == ("keyword_only", "request_failed") and found["results"]


async def test_a_build_without_its_helper_is_keyword_only_with_its_reason(tmp_path):
    async with app(tmp_path) as client:
        client.state["local_helper"].binary_problem = "binary_missing"
        project = await project_of(client)
        await added(client, project, WAGES)
        status = await idle(client, project)
        assert (status["mode"], status["reason"]) == ("keyword_only", "binary_missing")
        assert (await find(client, project, "earnings"))["results"]


async def test_a_partly_embedded_library_says_how_much_meaning_search_covers(tmp_path):
    async with app(tmp_path) as client:
        project = await project_of(client)
        await added(client, project, WAGES)
        await idle(client, project)
        index = client.state["index"]
        await asyncio.to_thread(index._write, lambda: index._conn.execute(
            "UPDATE index_rows SET embedded = 0 WHERE id = (SELECT min(id) FROM index_rows)"))
        found = await find(client, project, "earnings")
        assert found["mode"] == "hybrid" and found["coverage"] == {"embedded": 3, "total": 4}


async def test_the_search_model_pin_is_the_one_the_index_records():
    from backend.search import identity
    found = identity(local_helper.Config(binary=None, models={EMBEDDING: PIN}))
    assert found == {"model": EMBEDDING, "revision": PIN["sha256"], "quantization": "Q8_0", "runtime": "none",
                     "dimensions": 1024}
