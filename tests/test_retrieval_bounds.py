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

    def keyword(self, project_id, query, limit, materials=None):
        seen["bm25"] = limit
        return real_keyword(self, project_id, query, limit, materials)

    def dense(self, project_id, vector, limit, materials=None):
        seen["dense"] = limit
        return real_dense(self, project_id, vector, limit, materials)
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


# S1-19: the answer pipeline's own bounds (backend/pipeline.py)

@pytest.mark.asyncio
async def test_the_pipeline_keeps_eight_by_fused_rank_and_reranks_only_when_a_reranker_is_passed(tmp_path):
    from backend import pipeline
    many = paper("Many Paragraphs", *[f"Paragraph {i} on wages and synthetic panels." for i in range(30)])
    async with app(tmp_path) as client:
        project = await project_of(client)
        await added(client, project, many)
        await idle(client, project)
        state = client.state
        plain = await pipeline.evidence(state, project, "wages")
        assert len(plain["records"]) == 8 and "rerank" not in plain["trace"]  # off by default: no reranker
        fused = plain["trace"]["fused"]
        seen = {}

        async def on_time(query, texts, deadline):
            seen.update(query=query, count=len(texts), deadline=deadline)
            return [float(i) for i in range(len(texts))]  # the last of the top 24 first

        reranked = await pipeline.evidence(state, project, "wages", rerank=on_time)
        assert seen == {"query": "wages", "count": 24, "deadline": 0.5}
        assert [r["passage_id"] for r in reranked["records"]] == list(reversed(fused[:24]))[:8]

        async def late(query, texts, deadline):  # local_helper.Helper.rerank's answer past its deadline
            return None

        fallback = await pipeline.evidence(state, project, "wages", rerank=late)
        assert [r["passage_id"] for r in fallback["records"]] == fused[:8]  # the fused order stands
        assert fallback["trace"]["rerank"]["fallback"] == "deadline"


@pytest.mark.asyncio
async def test_a_selection_that_fits_is_read_whole_in_order_and_a_larger_one_is_searched_within(tmp_path):
    from backend import pipeline
    many = paper("Many Paragraphs", *[f"Paragraph {i} on wages and synthetic panels." for i in range(30)])
    other = paper("Other", "Wages and synthetic panels elsewhere.")
    async with app(tmp_path) as client:
        project = await project_of(client)
        [selected] = [m["id"] for m in (await added(client, project, many))["materials"]]
        await added(client, project, other)
        await idle(client, project)
        whole = await pipeline.evidence(client.state, project, "wages", materials=[selected])
        assert whole["mode"] == "selection" and whole["trace"]["read_directly"] is True
        assert len(whole["records"]) > 8 and {r["material_id"] for r in whole["records"]} == {selected}
        await setting(client, "context.research_evidence", 50)  # now too large to read whole
        searched = await pipeline.evidence(client.state, project, "wages", materials=[selected])
        assert searched["trace"]["read_directly"] is False and len(searched["records"]) == 8
        assert {r["material_id"] for r in searched["records"]} == {selected}  # only the selection's passages
        assert set(searched["trace"]["keyword"]) | set(searched["trace"]["dense"]) <= {
            r["passage_id"] for r in whole["records"]}


def test_no_setting_turns_reranking_on_and_the_reranker_is_not_offered():
    from backend import local_helper, settings
    keys = [key for key in (*settings.PERSONAL, *settings.PROJECT) if any("rerank" in str(part) for part in key)]
    assert sorted(keys) == [("retrieval", "rerank_ms"), ("retrieval", "rerank_top")]  # bounds only, no switch
    assert local_helper.RERANKER_MODEL not in local_helper.MODELS.values() and list(local_helper.MODELS) == [local_helper.EMBEDDING]


def test_no_pin_fetch_fixture_or_arm_names_the_excluded_rerankers():
    # Slice 1 section 15 and ticket 70: Qwen3-Reranker-4B and 8B are excluded, as benchmark candidates too.
    from pathlib import Path
    root = Path(__file__).resolve().parents[1]
    found = [str(path.relative_to(root)) for folder in ("backend", "evals", "tools", "frontend/src", "tests/fixtures")
             for path in (root / folder).rglob("*") if path.is_file() and path.suffix in (".py", ".json", ".js", ".jsx", ".mjs", ".md")
             and any(name in path.read_text(errors="ignore") for name in ("Reranker-4B", "Reranker-8B"))]
    assert found == []


@pytest.mark.asyncio
async def test_a_selections_dense_search_is_its_own_nearest_whatever_the_rest_of_the_project_holds(tmp_path):
    # Review b1 (11): the selection's own top passages by the same distance, not a filter over the project's
    # nearest 4,096 (vec0's largest k), which other papers can fill entirely.
    import sqlite_vec
    from backend import search_index
    from test_search_index import until_embedded
    async with app(tmp_path) as client:
        project = await project_of(client)
        [selected] = [m["id"] for m in (await added(client, project, WAGES))["materials"]]
        await until_embedded(client, project)
        index = client.state["index"]
        query = [1.0] + [0.0] * (search_index.DIMENSIONS - 1)
        conn, _ = search_index.connect(index.path)
        try:  # 4,200 passages of another paper, each nearer the query than anything the selection holds
            with conn:
                for i in range(4200):
                    rowid = conn.execute("INSERT INTO index_rows (project_id, passage_id, material_id, kind, digest, embedded)"
                                         " VALUES (?, ?, 'other', 'paragraph', 'd', 1) RETURNING id",
                                         (project, f"near-{i}")).fetchone()[0]
                    conn.execute("INSERT INTO vec_passages (rowid, project_id, passage_id, embedding) VALUES (?, ?, ?, ?)",
                                 (rowid, project, f"near-{i}", sqlite_vec.serialize_float32(query)))
        finally:
            conn.close()
        mine = {pid for pid, in await asyncio.to_thread(index._read, lambda c: c.execute(
            "SELECT passage_id FROM index_rows WHERE material_id = ? AND embedded = 1", (selected,)).fetchall())}
        own = await asyncio.to_thread(index.dense, project, query, 50, mine)
        assert own and set(own) == mine  # every embedded passage of the selection, none of the others
        assert len(await asyncio.to_thread(index.dense, project, query, 50)) == 50  # the project's own nearest
