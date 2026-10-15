"""Tier 1's retrieval evaluation (S1-18; slice-1 spec section 14, tickets 17, 70 and 72 E): the runner
in evals/tier1.py and its metrics in evals/metrics.py, on small synthetic fixtures in each benchmark's
file shape. No model, download or benchmark file: the app runs with S1-16's test-owned stand-in helper
(tests/test_search.py's `app`), whose vectors are synthetic. Lifecycle cases first.
"""

import asyncio
import contextlib
import hashlib
import json
from pathlib import Path

import pytest

from evals import metrics, tier1
from backend.local_helper import EMBEDDING
from test_local_helper import WEIGHTS
from test_search import app as search_app

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture(autouse=True)
def quick(monkeypatch):
    """Builds looked at often; the machine, the commit and the helper's sockets read without the
    commands (sysctl, git, lsof) the network block refuses."""
    import helper_timings  # the module tier1 uses (tools/ is on its path)
    monkeypatch.setattr(tier1, "POLL_SECONDS", 0.05)
    monkeypatch.setattr(tier1, "machine", lambda: {"chip": "test"})
    monkeypatch.setattr(tier1, "commit", lambda: {"sha": "test", "modified": False})
    monkeypatch.setattr(helper_timings, "sockets", lambda pid: [])
    monkeypatch.setattr(helper_timings, "not_loopback", lambda pids: [])


# Synthetic sets, in each benchmark's own file shapes


def _pin(folder, path):
    data = (folder / path).read_bytes()
    return {"path": path, "size": len(data), "sha256": hashlib.sha256(data).hexdigest()}


def _write(folder, path, text):
    (folder / path).parent.mkdir(parents=True, exist_ok=True)
    (folder / path).write_text(text, encoding="utf-8")


LONG = " ".join(f"Sentence {i} of the long paragraph about tidal wetlands and their carbon burial." for i in range(40))
QASPER = {
    "0000.00001": {"title": "An Earlier Paper", "abstract": "Nothing is asked about it.", "full_text": [
        {"section_name": "Intro", "paragraphs": ["Mangroves store carbon in their roots."]}], "figures_and_tables": [],
        "qas": []},
    "0001.00001": {
        "title": "Tidal Wetlands", "abstract": "We measure carbon burial in tidal wetlands.",
        "full_text": [{"section_name": "Methods", "paragraphs": [
            "Cores were taken from twelve salt marshes along the estuary.", LONG]},
            {"section_name": "Results", "paragraphs": ["Burial rates doubled where sediment supply was high."]}],
        "figures_and_tables": [{"caption": "Table 1: Burial rates by site.", "file": "x.png"}],
        "qas": [
            {"question": "Where were cores taken?", "question_id": "q-cores", "answers": [
                {"answer": {"unanswerable": False, "evidence": ["Cores were taken from twelve  salt marshes along the estuary."]}},
                {"answer": {"unanswerable": False, "evidence": ["Methods", "Something nobody wrote down."]}}]},
            {"question": "What did burial rates do with sediment supply?", "question_id": "q-burial", "answers": [
                {"answer": {"unanswerable": False, "evidence": ["Burial rates doubled where sediment supply was high."]}}]},
            {"question": "How long was the long paragraph?", "question_id": "q-long", "answers": [
                {"answer": {"unanswerable": False, "evidence": [LONG]}}]},
            {"question": "Who funded it?", "question_id": "q-none", "answers": [
                {"answer": {"unanswerable": True, "evidence": []}}, {"answer": {"unanswerable": True, "evidence": []}}]},
            {"question": "What is in table 1?", "question_id": "q-table", "answers": [
                {"answer": {"unanswerable": False, "evidence": ["FLOAT SELECTED: Table 1: Burial rates by site."]}}]},
            {"question": "Any evidence?", "question_id": "q-empty", "answers": [
                {"answer": {"unanswerable": False, "evidence": []}}]},
            {"question": "What about pigs?", "question_id": "q-pigs", "answers": [
                {"answer": {"unanswerable": False, "evidence": ["Pigs were never studied here at all."]}}]},
        ]},
}
DOCS = [("d1", "Coral bleaching", "Coral reefs bleach when ocean temperatures rise for weeks."),
        ("d2", "Wheat yields", "Drought lowers wheat yields across dry farming regions."),
        ("d3", "Bird migration", "Songbirds migrate at night using the stars for direction."),
        ("d4", "Same text", "Drought lowers wheat yields across dry farming regions."),
        ("d5", "", "")]
QUERIES = [("q1", "coral reefs bleach ocean temperatures", {"d1": 2}),
           ("q2", "drought wheat yields", {"d2": 1, "d4": 1, "d3": 0}),
           ("q3", "songbirds migrate stars", {"d3": 1, "d5": 1})]


def _sets(folder):
    _write(folder, "qasper/test.json", json.dumps(QASPER))
    _write(folder, "beir/corpus.jsonl", "".join(json.dumps({"_id": d, "title": t, "text": x}) + "\n" for d, t, x in DOCS))
    _write(folder, "beir/queries.jsonl", "".join(json.dumps({"_id": q, "text": t}) + "\n" for q, t, _ in QUERIES))
    _write(folder, "beir/qrels/test.tsv", "query-id\tcorpus-id\tscore\n" + "".join(
        f"{q}\t{d}\t{g}\n" for q, _, rel in QUERIES for d, g in rel.items()))
    _write(folder, "lit/corpus.jsonl", "".join(json.dumps({"corpusid": i, "title": t, "abstract": x}) + "\n"
                                               for i, (_, t, x) in enumerate(DOCS[:3])))
    _write(folder, "lit/queries.jsonl", "".join(json.dumps(q) + "\n" for q in [
        {"query": "papers on coral reefs bleaching", "corpusids": [0], "specificity": 1, "query_set": "x", "quality": 1},
        {"query": "work about migrating songbirds", "corpusids": [2], "specificity": 0, "query_set": "x", "quality": 1}]))


def manifest_for(folder):
    real = tier1.load_manifest()
    beir = {"folder": "beir", "ids_committable": False, "resample": "query", "unit": "document",
            "metrics": real["benchmarks"]["scifact"]["metrics"], "primary": "ndcg@10",
            "files": {r: _pin(folder / "beir", p) for r, p in (("corpus", "corpus.jsonl"), ("queries", "queries.jsonl"),
                                                               ("qrels", "qrels/test.tsv"))}}
    return {**real, "bootstrap": {**real["bootstrap"], "resamples": 200},
            "benchmarks": {
                "qasper": {**real["benchmarks"]["qasper"], "folder": "qasper", "files": {"test": _pin(folder / "qasper", "test.json")}},
                "scifact": beir,
                "litsearch": {**real["benchmarks"]["litsearch"], "folder": "lit",
                              "files": {r: _pin(folder / "lit", p) for r, p in (("corpus", "corpus.jsonl"), ("queries", "queries.jsonl"))}}},
            "levels": {"full": {"benchmarks": {"qasper": "all test papers", "scifact": "all", "litsearch": "all"}},
                       "subset": {"benchmarks": {"qasper": {"papers": ["0001.00001"]}}}}}


def opener(seen=None, before_yield=None, refuse_import=False):
    """The runner's app: the test app with the stand-in helper, the model imported through the API when
    the runner installs it (as the runner does with the real model)."""
    @contextlib.asynccontextmanager
    async def open_app(data):
        if seen is not None:
            seen.append(data)
        async with search_app(data.parent, data=data, install=False) as client:
            if before_yield:
                await before_yield(client)

            async def install():
                weights = data.parent / "Qwen3-Embedding-0.6B-Q8_0.gguf"
                weights.write_bytes(WEIGHTS[:-1] if refuse_import else WEIGHTS)
                passages = await asyncio.to_thread(client.state["db"].read, lambda c: c.execute(
                    "SELECT count(*) FROM passages").fetchone()[0])
                read = await asyncio.to_thread(client.state["db"].read, lambda c: c.execute(
                    "SELECT count(*) FROM runs WHERE workflow = 'extract' AND status = 'running'").fetchone()[0])
                installs.append((data.name, passages, read, len(client.remote.indexing)))
                await tier1._post(
                    client, "/api/helper/models/import", {"model": EMBEDDING, "path": str(weights)}, 200)
            yield client, client.state, install
    return open_app


installs = []


async def _run(tmp_path, names, level="full", **options):
    sets = tmp_path / "sets"
    _sets(sets)
    manifest = manifest_for(sets)
    files = tier1.verify(manifest, sets, names)
    out = tmp_path / "results.json"
    return manifest, out, tier1.run(manifest, level, names, files, out, options.pop("open_app", opener()), **options)


# Lifecycle


@pytest.mark.asyncio
@pytest.mark.parametrize("stage", ["build", "search"])
async def test_an_interrupted_run_leaves_an_incomplete_file_and_no_data_folder(tmp_path, monkeypatch, stage):
    seen = []
    reached = asyncio.Event()
    if stage == "build":
        real = tier1.until_built

        async def held(state):
            reached.set()
            await asyncio.sleep(3600)
            await real(state)
        monkeypatch.setattr(tier1, "until_built", held)
        arms = None
    else:
        async def stalled(state, project_id, query):
            reached.set()
            await asyncio.sleep(3600)
        arms = {"hybrid": tier1.hybrid, "keyword_only": stalled}
    _, out, running = await _run(tmp_path, ["scifact"], open_app=opener(seen), arms=arms)
    task = asyncio.ensure_future(running)
    await asyncio.wait_for(reached.wait(), 60)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    written = json.loads(out.read_text())
    assert written["status"] == "incomplete" and written["reason"].startswith("CancelledError")
    assert written["benchmarks_finished"] == [] and "benchmarks" not in written
    assert seen and not seen[0].parent.exists()  # the temporary data folder is gone
    assert not list(tmp_path.glob("*.part"))


@pytest.mark.asyncio
async def test_the_results_file_says_incomplete_until_the_run_has_finished_and_is_written_whole(tmp_path, monkeypatch):
    writes = []
    real = tier1.write_atomic
    monkeypatch.setattr(tier1, "write_atomic", lambda path, results: writes.append(
        (results["status"], results.get("reason"))) or real(path, results))
    (tmp_path / "results.json").write_text(json.dumps({"status": "finished"}))  # an earlier run's file at --out
    _, out, running = await _run(tmp_path, ["scifact"])
    task = asyncio.ensure_future(running)
    while not writes:
        await asyncio.sleep(0.01)
    assert json.loads(out.read_text())["status"] == "incomplete"  # the earlier finished file is gone at once
    results = await task
    assert writes == [("incomplete", "running"), ("finished", None)] and json.loads(out.read_text())["status"] == "finished"
    assert results["gaps"] == []


@pytest.mark.asyncio
async def test_a_failure_during_the_build_leaves_an_incomplete_file_and_no_data_folder(tmp_path, monkeypatch):
    seen = []

    async def failed(state):
        raise RuntimeError("the index runs ended with passages unembedded (request_failed)")
    monkeypatch.setattr(tier1, "until_built", failed)
    _, out, running = await _run(tmp_path, ["scifact"], open_app=opener(seen))
    with pytest.raises(RuntimeError):
        await running
    written = json.loads(out.read_text())
    assert written["status"] == "incomplete" and written["reason"].startswith("RuntimeError: the index runs ended")
    assert not seen[0].parent.exists()


@pytest.mark.asyncio
async def test_a_refused_model_import_leaves_an_incomplete_file(tmp_path):
    _, out, running = await _run(tmp_path, ["scifact"], open_app=opener(refuse_import=True))
    with pytest.raises(RuntimeError, match="/api/helper/models/import: 400"):
        await running
    assert json.loads(out.read_text())["status"] == "incomplete"


def test_the_command_exits_nonzero_when_the_run_is_not_finished(tmp_path, monkeypatch):
    async def gapped(*args, **kwargs):
        return {"status": "incomplete", "gaps": ["scifact hybrid: did not run as named"], "benchmarks": {}}
    monkeypatch.setattr(tier1, "verify", lambda *args: {})
    monkeypatch.setattr(tier1, "check_models", lambda *args: None)
    monkeypatch.setattr(tier1, "run", gapped)
    with pytest.raises(SystemExit, match="incomplete: scifact hybrid: did not run as named"):
        tier1.main(["run", "--level", "full", "--benchmarks", "scifact", "--sets", str(tmp_path), "--helper", "h",
                    "--model", "m", "--out", str(tmp_path / "out.json")])


@pytest.mark.asyncio
async def test_a_failing_helper_marks_its_queries_and_its_keyword_results_are_never_scored_as_hybrid(tmp_path, monkeypatch):
    clients, real = [], tier1.until_built

    async def then_fail(state):  # the helper fails once the build is done, before any search
        await real(state)
        clients[-1].remote.failing = True
    monkeypatch.setattr(tier1, "until_built", then_fail)

    async def keep(client):
        clients.append(client)
    _, out, running = await _run(tmp_path, ["scifact"], open_app=opener(before_yield=keep))
    results = await running
    arms = results["benchmarks"]["scifact"]["arms"]
    hybrid = arms["hybrid"]
    assert hybrid["complete"] is False and "scores" not in hybrid and "metrics" not in hybrid
    assert hybrid["fallback_queries"] == [0, 1, 2] and set(hybrid["fallbacks"]) - {"deadline"}
    assert arms["keyword_only"]["complete"] is True and "minus_hybrid" not in arms["keyword_only"]
    # the run ran to its end, but an arm that did not run as named keeps it from being finished
    assert results["status"] == "incomplete" and results["gaps"] == [
        f"scifact hybrid: did not run as named ({hybrid['fallbacks']})"]
    with pytest.raises(tier1.Refused, match="not a finished run"):
        tier1.compare(tier1.load_manifest(), results, results)
    assert "Not finished:" in "\n".join(tier1.report(results))


@pytest.mark.asyncio
async def test_a_deadline_fallback_is_counted_and_marked_but_scored_as_the_app_returned_it(tmp_path):
    from test_search import setting

    async def slow(client):
        await setting(client, "retrieval.hybrid_ms", 50)
        client.remote.slow_queries = 0.3
    _, out, running = await _run(tmp_path, ["scifact"], open_app=opener(before_yield=slow))
    results = await running
    hybrid = results["benchmarks"]["scifact"]["arms"]["hybrid"]
    assert hybrid["complete"] is True and hybrid["fallbacks"] == {"deadline": 3} and hybrid["fallback_queries"] == [0, 1, 2]
    assert hybrid["metrics"]["ndcg@10"]["n"] == 3 and results["model_settings"]["retrieval"]["hybrid_ms"] == 50
    assert hybrid["metrics_without_fallbacks"]["ndcg@10"]["n"] == 0 and results["status"] == "finished"


# Success paths


@pytest.mark.asyncio
async def test_a_full_run_builds_through_the_app_scores_each_benchmark_apart_and_pins_what_it_used(tmp_path):
    manifest, out, running = await _run(tmp_path, ["qasper", "scifact", "litsearch"])
    results = await running
    assert results["status"] == "finished" and json.loads(out.read_text()) == json.loads(json.dumps(results))
    # each benchmark's model imported once its files were read, before any passage was sent to be embedded
    assert [(name, passages > 0, running, sent) for name, passages, running, sent in installs[-3:]] == [
        ("qasper", True, 0, 0), ("scifact", True, 0, 0), ("litsearch", True, 0, 0)]
    assert results["benchmarks"]["scifact"]["build"]["embedding_seconds"] >= 0
    assert results["model_settings"]["retrieval"] == manifest["model_settings"]["retrieval"]
    assert results["model_settings"]["helper_flags"] == manifest["model_settings"]["helper_flags"]
    assert results["like_for_like"] is False  # the stand-in helper's runtime is not the pinned bundle's
    assert "combined" not in json.dumps(results)

    beir = results["benchmarks"]["scifact"]
    assert beir["documents"] == 5 and beir["documents_empty"] == 1 and beir["documents_sharing_a_file"] == 1
    assert beir["keys"] is None and beir["query_language"] == beir["document_language"] == "en"
    hybrid = beir["arms"]["hybrid"]
    assert hybrid["complete"] and hybrid["fallbacks"] == {}
    assert hybrid["metrics"]["ndcg@10"]["n"] == 3 and hybrid["metrics"]["ndcg@10"]["mean"] > 0.8
    assert beir["relevant_documents_not_indexed"] == 1  # d5 has no title or text: judged, never searchable
    assert beir["arms"]["keyword_only"]["minus_hybrid"]["ndcg@10"]["n"] == 3
    assert beir["build"]["passages"] == 3 and beir["build"]["passage_chars"]["n"] == 3
    assert beir["commit"] == {"sha": "test", "modified": False} and beir["started_at"] <= beir["finished_at"]

    qasper = results["benchmarks"]["qasper"]
    assert qasper["exclusions"] == {"all_unanswerable": 1, "figure_or_table_only": 1, "no_evidence": 1,
                                    "evidence_matching_no_passage": 1}
    assert qasper["evidence_items_unmatched"] == {"heading": 1, "other": 2}
    assert qasper["keys"] == ["q-cores", "q-burial", "q-long"] and qasper["groups"] == [1, 1, 1]
    assert qasper["papers"] == 2 and qasper["figure_or_table_items_dropped"] == 0
    assert qasper["arms"]["hybrid"]["metrics"]["recall@8"]["mean"] == 1.0

    lit = results["benchmarks"]["litsearch"]
    assert lit["specific"] == [True, False]
    # found, though its gold paper's id ("0") equals the query's position: only BEIR drops a query's own id
    assert lit["arms"]["hybrid"]["metrics"]["recall@5 (specific)"] == {"mean": 1.0, "n": 1, "interval": [1.0, 1.0]}
    assert lit["arms"]["hybrid"]["metrics"]["recall@20 (broad)"]["n"] == 1
    for text in ("Coral reefs bleach", "Tidal Wetlands", '"q1"', '"d1"'):  # no benchmark text or uncommittable id
        assert text not in json.dumps(lit) + json.dumps(beir)
    summary = "\n".join(tier1.report(results))
    assert "## qasper" in summary and "| hybrid | recall@8 | 1.000 |" in summary and "no combined score" in summary


@pytest.mark.asyncio
async def test_the_subset_runs_its_listed_papers_and_compare_pairs_it_with_the_baseline(tmp_path):
    manifest, out, running = await _run(tmp_path / "full", ["qasper"])
    baseline = await running
    manifest, out, running = await _run(tmp_path / "subset", ["qasper"], level="subset")
    run = await running
    assert run["benchmarks"]["qasper"]["papers"] == 1 and baseline["benchmarks"]["qasper"]["papers"] == 2
    assert run["benchmarks"]["qasper"]["groups"] == [0, 0, 0]  # the paper's place differs; its questions pair by id
    lines = tier1.compare(manifest, baseline, run)
    assert "qasper hybrid: 3 queries paired, rankings identical for 3; fallbacks none" in lines
    assert any(line.startswith("  recall@8: difference +0.0000 [+0.0000, +0.0000] n=3") for line in lines)


# The parts


def test_metrics_on_hand_computed_cases():
    assert metrics.recall_at(["a", "b", "c"], {"a", "x"}, 2) == 0.5
    assert metrics.recall_at(["a"], set(), 2) is None
    assert metrics.mrr_at(["x", "y", "a"], {"a"}, 10) == pytest.approx(1 / 3)
    assert metrics.mrr_at(["x"] * 10 + ["a"], {"a"}, 10) == 0.0
    # graded, linear gain: DCG = 1/log2(2) + 2/log2(3); ideal = 2/1 + 1/log2(3)
    assert metrics.ndcg_at(["b", "a"], {"a": 2, "b": 1, "c": 0}) == pytest.approx(
        (1 + 2 / 1.5849625007211562) / (2 + 1 / 1.5849625007211562))
    # a document ranked by its best passage; documents sharing a file ranked together
    assert metrics.documents(["p1", "p2", "p3", "p4"], {"p1": ["A"], "p2": ["B", "C"], "p3": ["A"], "p4": []}.get) == \
        ["A", "B", "C"]


def test_the_bootstrap_is_deterministic_for_its_seed_and_resamples_whole_papers():
    values = [0.1, 0.5, 0.9, 0.3, 0.7, 0.2]
    assert metrics.bootstrap(values, seed=7, resamples=500) == metrics.bootstrap(values, seed=7, resamples=500)
    # two papers of three questions each: a resample holds whole papers, so its mean is 0, 0.5 or 1
    low, high = metrics.bootstrap([1, 1, 1, 0, 0, 0], ["a", "a", "a", "b", "b", "b"], resamples=500, seed=1)
    assert {low, high} <= {0.0, 0.5, 1.0}
    by_query = metrics.bootstrap([1, 1, 1, 0, 0, 0], resamples=500, seed=1)
    assert by_query != (low, high)
    assert metrics.bootstrap([], resamples=10) is None


def test_evidence_maps_to_every_passage_of_a_split_paragraph_and_nothing_else():
    passages = [("p1", "Cores were taken from twelve salt marshes."), ("p2", LONG[:1000]), ("p3", LONG[1000:]),
                ("p4", "Sentence 1 of the long paragraph")]
    assert tier1.evidence_passages(LONG, passages) == ["p2", "p3"]  # long: split by section 7.1, both pieces
    assert tier1.evidence_passages(LONG, [("p2", LONG[:1000]), ("p9", "Elsewhere."), ("p3", LONG[1000:])]) == []
    assert tier1.evidence_passages("Cores were taken from *twelve*  salt marshes.", passages) == ["p1"]
    assert tier1.evidence_passages("twelve salt marshes", passages) == ["p1"]  # an edited part of a paragraph
    assert tier1.evidence_passages("Nothing like it.", passages) == []
    assert tier1.evidence_passages("", passages) == []


def test_each_exclusion_is_its_own_and_the_best_annotator_scores_the_question():
    passages = [("p1", "Alpha beta."), ("p2", "Gamma delta.")]
    ask = lambda *answers: {"answers": [{"answer": a} for a in answers]}  # noqa: E731
    yes = lambda *e: {"unanswerable": False, "evidence": list(e)}  # noqa: E731
    assert tier1.qasper_question(ask({"unanswerable": True}), passages, set())[1] == "all_unanswerable"
    assert tier1.qasper_question(ask({"unanswerable": True}, yes("FLOAT SELECTED: Fig 1")), passages, set())[1] == \
        "figure_or_table_only"
    assert tier1.qasper_question(ask(yes()), passages, set())[1] == "no_evidence"
    annotators, reason, unmatched = tier1.qasper_question(ask(yes("Heading"), yes("Zeta.")), passages, {"Heading"})
    assert (annotators, reason, unmatched) == ([], "evidence_matching_no_passage", [("heading", "Heading"), ("other", "Zeta.")])
    annotators, reason, left = tier1.qasper_question(
        ask(yes("Alpha beta.", "Gamma delta."), yes("Gamma delta.", "FLOAT SELECTED: Table 2")), passages, set())
    assert reason is None and annotators == [[["p1"], ["p2"]], [["p2"]]]
    assert left == [("figure_or_table", "FLOAT SELECTED: Table 2")]  # left out of the set, and counted
    specs = tier1.load_manifest()["benchmarks"]["qasper"]["metrics"]
    scored = tier1.score_qasper(["p2", "x"], annotators, specs)
    assert scored["recall@8"] == 1.0 and scored["mrr@10"] == 1.0  # the second annotator, fully found


def test_the_runner_refuses_a_file_of_another_size_or_hash_or_missing_and_downloads_nothing(tmp_path):
    sets = tmp_path / "sets"
    _sets(sets)
    manifest = manifest_for(sets)
    assert tier1.verify(manifest, sets, ["scifact"])["scifact"]["qrels"].name == "test.tsv"
    qrels = sets / "beir" / "qrels" / "test.tsv"
    original = qrels.read_bytes()
    qrels.write_bytes(original.replace(b"q1\td1\t2", b"q1\td1\t1"))  # the same size, another hash
    with pytest.raises(tier1.Refused, match="SHA-256"):
        tier1.verify(manifest, sets, ["scifact"])
    qrels.write_bytes(original + b"\n")
    with pytest.raises(tier1.Refused, match="bytes, expected"):
        tier1.verify(manifest, sets, ["scifact"])
    qrels.unlink()
    with pytest.raises(tier1.Refused, match="missing.*never downloads"):
        tier1.verify(manifest, sets, ["scifact"])
    source = (ROOT / "evals" / "tier1.py").read_text()
    assert "urllib" not in source and "httpx.AsyncClient(transport=httpx.ASGITransport" in source


def test_compare_flags_a_drop_and_refuses_an_unfinished_run():
    bench = {"queries": 4, "keys": None, "order_sha256": "o", "groups": None, "specific": None,
             "arms": {"hybrid": {"scores": {"ndcg@10": [0.9, 0.8, 0.9, 0.7]}, "ranking_digests": ["a", "b", "c", "d"],
                                 "fallbacks": {}}}}
    worse = {**bench, "arms": {"hybrid": {"scores": {"ndcg@10": [0.5, 0.4, 0.6, 0.3]},
                                          "ranking_digests": ["a", "x", "y", "z"], "fallbacks": {}}}}
    manifest = {"benchmarks": {"scifact": {"metrics": [{"name": "ndcg@10", "kind": "ndcg", "k": 10}]}},
                "bootstrap": {"resamples": 200, "seed": 1, "level": 0.95}}
    base = {"status": "finished", "like_for_like": True, "model_settings": {}, "machine": {}, "benchmarks": {"scifact": bench}}
    lines = tier1.compare(manifest, base, {**base, "benchmarks": {"scifact": worse}})
    assert "scifact hybrid: 4 queries paired, rankings identical for 1; fallbacks none" in lines
    assert any("difference -0.3750" in line and "DROP: investigate" in line for line in lines)
    other = {**base, "benchmarks": {"scifact": {**worse, "order_sha256": "other"}}}
    assert "scifact: its queries differ from the baseline's; not compared" in tier1.compare(manifest, base, other)
    with pytest.raises(tier1.Refused, match="not a finished run"):
        tier1.compare(manifest, base, {"status": "incomplete"})


def test_the_manifest_records_rule_e_word_for_word_with_each_benchmarks_own_metric():
    manifest = tier1.load_manifest()
    rule = manifest["rule_e"]
    text = "\n".join(rule["text"])
    assert text.startswith("**E. The reranker comparison on tier 1 (decided).**")
    assert "the gain is at least 2 points" in text and "lower end is above −3 points" in text
    assert text.endswith("the offer is decided at the preview.")
    assert set(rule["own_metric"]) == set(manifest["benchmarks"]) == {"qasper", "scifact", "nfcorpus", "scidocs", "litsearch"}
    for name, bench in manifest["benchmarks"].items():
        assert bench["primary"] in {m["name"] for m in bench["metrics"]}
        assert rule["own_metric"][name].startswith(bench["primary"])
    assert "no reranker can qualify in M2" in rule["chinese"]
    assert manifest["model_settings"]["reranker"] is None and set(tier1.ARMS) == {"hybrid", "keyword_only"}
    assert list(tier1.ARMS)[-1] == "hybrid"  # last, so the helper still runs when its sockets are listed
    from backend.search import MAX_LIMIT, QUERY_INSTRUCTION
    assert manifest["search_limit"] == MAX_LIMIT and manifest["model_settings"]["query_instruction"] == QUERY_INSTRUCTION
    assert len(manifest["levels"]["subset"]["benchmarks"]["qasper"]["papers"]) == 50


def test_the_models_and_the_conversion_refuse_files_that_differ_from_their_pins(tmp_path):
    model = tmp_path / "model.gguf"
    model.write_bytes(b"not the model")
    with pytest.raises(tier1.Refused, match="the embedding model: model.gguf: 13 bytes"):
        tier1.check_models(tier1.load_manifest(), tmp_path / "llama-server", model)
    from evals import litsearch_to_jsonl
    for name in litsearch_to_jsonl.INPUTS:
        (tmp_path / "in" / name).parent.mkdir(parents=True, exist_ok=True)
        (tmp_path / "in" / name).write_bytes(b"x")
    with pytest.raises(SystemExit, match="nothing converted"):  # before pyarrow is imported or anything written
        litsearch_to_jsonl.main(tmp_path / "in", tmp_path / "out")
    assert not (tmp_path / "out").exists()


@pytest.mark.asyncio
async def test_answers_over_a_partial_or_unready_index_are_marked_and_the_others_are_not():
    answers = iter([
        {"mode": "hybrid", "reason": None, "index": "ready", "coverage": {"embedded": 5, "total": 5}, "results": []},
        {"mode": "hybrid", "reason": None, "index": "ready", "coverage": {"embedded": 3, "total": 5}, "results": []},
        {"mode": "hybrid", "reason": None, "index": "building", "coverage": {"embedded": 5, "total": 5}, "results": []},
        {"mode": "keyword_only", "reason": "deadline", "index": "ready", "coverage": {"embedded": 5, "total": 5},
         "results": []},
        {"mode": "keyword_only", "reason": "reference_arm", "index": "ready", "coverage": {"embedded": 0, "total": 5},
         "results": []}])

    async def arm(state, project, text):
        return next(answers)
    out = await tier1.run_arms({}, {"hybrid": arm}, [{"project": "p", "text": "t"}] * 5, lambda answer, query: [])
    assert out["hybrid"]["outcomes"] == [("hybrid", None), ("hybrid", "coverage_partial"), ("hybrid", "index_not_ready"),
                                         ("keyword_only", "deadline"), ("keyword_only", "reference_arm")]


def test_gaps_keep_a_run_from_finishing_and_metrics_without_fallbacks_leave_out_only_those():
    bench = {"build": {"readings_not_succeeded": 2}, "helper_sockets": None, "not_loopback_sockets": ["1 10.0.0.1:443"],
             "arms": {"hybrid": {"complete": True}, "keyword_only": {"complete": False, "fallbacks": {"x": 1}}}}
    assert tier1.gaps({"benchmarks": {"b": bench}}) == [
        "b keyword_only: did not run as named ({'x': 1})", "b: 2 readings did not succeed",
        "b: the helper's sockets were not sampled (it was not running)", "b: sockets off loopback ['1 10.0.0.1:443']"]
    spec = {"metrics": [{"name": "ndcg@10", "kind": "ndcg", "k": 10}], "ids_committable": False, "resample": "query"}
    queries = [{"key": str(i)} for i in range(4)]
    scores = {"hybrid": [{"ndcg@10": v} for v in (1.0, 0.0, 0.5, 0.25)]}
    out = {"hybrid": {"outcomes": [("hybrid", None), ("keyword_only", "deadline"), ("hybrid", None), ("hybrid", None)],
                      "seconds": None, "digests": ["a"] * 4}}
    record = tier1.summarize(spec, {"resamples": 50, "seed": 1, "level": 0.95}, queries, out, scores)
    hybrid = record["arms"]["hybrid"]
    assert hybrid["complete"] and hybrid["fallback_queries"] == [1]
    assert hybrid["metrics"]["ndcg@10"]["n"] == 4 and hybrid["metrics_without_fallbacks"]["ndcg@10"] == {
        "mean": 0.5833, "n": 3, "interval": hybrid["metrics_without_fallbacks"]["ndcg@10"]["interval"]}
    finished = {"status": "finished", "like_for_like": True, "model_settings": {}, "machine": {}, "benchmarks": {},
                "manifest_sha256": "a", "bootstrap": {}}
    lines = tier1.compare({}, finished, {**finished, "manifest_sha256": "b"})
    assert lines == ["NOT LIKE FOR LIKE: the manifest (pins, rules, subset) differs from the baseline's"]
    assert "Not finished: running" in "\n".join(tier1.report({"status": "incomplete", "reason": "running"}))



@pytest.mark.asyncio
async def test_a_passage_the_helper_refuses_is_counted_and_the_hybrid_arm_still_scores(tmp_path):
    async def refusing(client):
        client.remote.refuse = "Songbirds migrate"  # the stand-in refuses a text containing this (HTTP 400)
    _, _, running = await _run(tmp_path, ["scifact"], open_app=opener(before_yield=refusing))
    results = await running
    beir = results["benchmarks"]["scifact"]
    assert beir["build"]["passages_refused"] == 1 and results["status"] == "finished"
    hybrid = beir["arms"]["hybrid"]
    assert hybrid["complete"] and hybrid["fallbacks"] == {} and hybrid["metrics"]["ndcg@10"]["n"] == 3
    assert "(1 refused by the helper;" in "\n".join(tier1.report(results))


def test_the_committed_baseline_is_a_finished_full_run_of_all_five_with_ids_only_where_allowed():
    manifest = tier1.load_manifest()
    baseline = tier1.load_manifest(ROOT / "evals" / "baseline" / "tier1-baseline.json")
    assert baseline["status"] == "finished" and baseline["gaps"] == [] and baseline["level"] == "full"
    assert baseline["like_for_like"] and baseline["model_settings"] == manifest["model_settings"]
    assert set(baseline["benchmarks"]) == set(manifest["benchmarks"])
    for name, bench in baseline["benchmarks"].items():
        assert (bench["keys"] is not None) == manifest["benchmarks"][name]["ids_committable"], name
        assert all(entry["complete"] and entry["metrics"] for entry in bench["arms"].values()), name
        assert bench["query_language"] == bench["document_language"] == "en"
    assert all(line.startswith(("qasper", "scifact", "nfcorpus", "scidocs", "litsearch", "  "))
               for line in tier1.compare(manifest, baseline, baseline))
