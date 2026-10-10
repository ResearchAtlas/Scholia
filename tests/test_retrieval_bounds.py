"""Bounds on search (slice-1 spec sections 7.4, 13 and 14, test_retrieval_bounds.py), for what S1-17
builds: candidate depths, reciprocal rank fusion, the limit, the query's length and the hybrid
deadline, which covers the query's embedding. S1-19 adds the pipeline's own bounds."""

import asyncio

import pytest

from backend.search import fuse
from backend.search_index import SearchIndex
from test_materials import added, project_of
from test_search import WAGES, app, find, idle, paper, setting


def test_fusion_ranks_by_the_sum_of_reciprocal_ranks():
    # k = 60: a = 1/61 + 1/63, b = 1/62 + 1/61, c = 1/63, d = 1/62; b and a tie-break by score, not order.
    assert fuse([["a", "b", "c"], ["b", "d", "a"]], 60) == ["b", "a", "d", "c"]
    assert fuse([["x"], []], 60) == ["x"] and fuse([[], []], 60) == []
    assert fuse([["p", "q"], ["q", "p"]], 1) == ["p", "q"]  # a tie keeps the order first seen


@pytest.mark.asyncio
async def test_candidates_are_cut_at_their_depths_and_results_at_the_limit(tmp_path, monkeypatch):
    seen = {}
    real_keyword, real_dense = SearchIndex.keyword, SearchIndex.dense

    def keyword(self, project_id, query, limit):
        seen["bm25"] = limit
        return real_keyword(self, project_id, query, limit)

    def dense(self, project_id, vector, limit):
        seen["dense"] = limit
        return real_dense(self, project_id, vector, limit)
    monkeypatch.setattr(SearchIndex, "keyword", keyword)
    monkeypatch.setattr(SearchIndex, "dense", dense)
    many = paper("Many Paragraphs", *[f"Paragraph {i} on wages and synthetic panels." for i in range(30)])
    async with app(tmp_path) as client:
        project = await project_of(client)
        await added(client, project, many)
        await idle(client, project)
        found = await find(client, project, "wages")
        assert len(found["results"]) == 8 and seen == {"bm25": 50, "dense": 50}  # keep 8 by default
        assert len((await find(client, project, "wages", limit=20))["results"]) == 20
        await setting(client, "retrieval.bm25_candidates", 3)
        await setting(client, "retrieval.dense_candidates", 2)
        found = await find(client, project, "wages", limit=50)
        assert seen == {"bm25": 3, "dense": 2} and len(found["results"]) <= 5


@pytest.mark.asyncio
async def test_a_query_embedding_past_the_deadline_returns_keyword_results_and_says_so(tmp_path):
    async with app(tmp_path) as client:
        project = await project_of(client)
        await added(client, project, WAGES)
        await idle(client, project)
        await setting(client, "retrieval.hybrid_ms", 100)
        client.remote.slow_queries = 0.5
        started = asyncio.get_running_loop().time()
        found = await find(client, project, "earnings")
        assert (found["mode"], found["reason"]) == ("keyword_only", "deadline") and found["results"]
        assert asyncio.get_running_loop().time() - started < 0.45  # it did not wait for the embedding
        client.remote.slow_queries = 0
        again = await find(client, project, "earnings")
        assert (again["mode"], again["reason"]) == ("hybrid", None)


@pytest.mark.asyncio
async def test_a_long_query_is_bounded_and_its_terms_are_capped(tmp_path):
    async with app(tmp_path) as client:
        project = await project_of(client)
        await added(client, project, WAGES)
        await idle(client, project)
        query = " ".join(f"word{i}" for i in range(150))[:1000] + " earnings"
        found = await find(client, project, query[:1000])
        assert found["mode"] == "hybrid"
        too_long = await client.post(f"/api/projects/{project}/search", json={"query": "a" * 1001})
        assert too_long.status_code == 400
