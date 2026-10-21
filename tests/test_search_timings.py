"""The search timing tool (tools/search_timings.py; S1-17, ticket 70's measurement rules): what it
holds in the process it measures, and what its desktop results say the network sandbox covers."""

import argparse
import inspect

import pytest


def test_the_synthetic_papers_are_made_one_at_a_time_as_they_are_added(monkeypatch):
    """No corpus is held in the measured process: a paper is made only when it is taken."""
    from tools import search_timings  # here, not at collection: it puts tools/ on sys.path
    made, real = [], search_timings._paragraph
    monkeypatch.setattr(search_timings, "_paragraph", lambda *args: made.append(1) or real(*args))
    _, papers, queries = search_timings.corpus(argparse.Namespace(corpus="synthetic", size="100k", papers=None))
    assert made == [] and len(queries) == 200
    name, text = next(papers)
    assert name == "synthetic-00001.md" and text.startswith(b"# ") and len(made) == 24  # four sections of six
    next(papers)
    assert len(made) == 48


def test_the_desktop_results_say_what_the_network_sandbox_covers(monkeypatch):
    from tools import helper_timings
    monkeypatch.setattr(helper_timings, "sandboxed", lambda: True)
    said = helper_timings.network_isolation()
    assert "confines this Python process and its children" in said and "WebKit" in said
    assert 'results["network_isolation"] = network_isolation()' in inspect.getsource(helper_timings._run_desktop)
    monkeypatch.setattr(helper_timings, "sandboxed", lambda: False)  # run without the profile: said so
    assert helper_timings.network_isolation().startswith("Not run under a sandbox profile")


@pytest.mark.asyncio
async def test_evidence_preparation_is_timed_without_and_with_reranking_per_language(tmp_path):
    """S1-19's evidence preparation (ticket 70): from the pipeline's entry to the assembled request, each
    query once, with the stand-in helper and reranker; English and Chinese apart, against the 2 s target."""
    from tools import search_timings
    from test_local_helper import RERANKER_PIN, RERANKER_WEIGHTS
    from test_materials import added, project_of
    from test_search import WAGES, app, idle
    async with app(tmp_path) as client:
        project = await project_of(client)
        await added(client, project, WAGES)
        await idle(client, project)
        path = tmp_path / "reranker.gguf"
        path.write_bytes(RERANKER_WEIGHTS)
        pins = search_timings.local_helper.RERANKER_MODEL
        search_timings.local_helper.RERANKER_MODEL = RERANKER_PIN  # the stand-in's weights, under the real id
        try:
            out = await search_timings.preparation(client.state, project, [("en", "minimum wages"), ("zh", "最低工资")],
                                                   path)
        finally:
            search_timings.local_helper.RERANKER_MODEL = pins
    assert out["target_seconds"] == 2.0 and out["reranker_first_start_seconds"] >= 0
    for arm in ("without_reranking", "with_reranking"):
        assert set(out[arm]["seconds"]) == {"en", "zh"} and out[arm]["seconds_all"]["n"] == 2
        assert sum(out[arm]["outcomes"].values()) == 2 and out[arm]["over_target"] == 0
    assert "rerank_deadline" not in out["with_reranking"]["outcomes"]
