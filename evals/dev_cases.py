"""Chinese and cross-language development cases (S1-19; Harold's decision of 2026-10-17, status.md S1-18
item (9); tickets 70 and 72 E3): retrieval over them with tier 1's arms, each language reported apart.

    sandbox-exec -f tests/loopback-only.sb uv run --no-sync python -I evals/dev_cases.py run --sets <sets> \\
        --helper <Scholia.app>/Contents/MacOS/llama-server --model <Qwen3-Embedding-0.6B-Q8_0.gguf> \\
        [--reranker <qwen3-reranker-0.6b-q8_0.gguf>] --out FILE

Two sets, in evals/dev_cases/:
- `cross_language.json`: thirty QASPER test questions from tier 1's small fixed subset, translated into
  Chinese (Chinese questions over English papers). Each runs twice, in English as QASPER asks it and in
  Chinese, against the same paper and the same annotators' evidence (tier1.qasper_question), so the
  Chinese wording's cost is a paired difference.
- `chinese_documents.json`: thirty synthetic Chinese documents (invented facts) in one project, one
  Chinese question each, whose answer is the paragraph named (Chinese questions over Chinese documents).

Each set gets its own app on a new temporary data folder, built as tier 1 builds a benchmark (evals/tier1.py:
the app's own reading and index, the model imported through the API, review-locked projects). Scored with
QASPER's metrics (recall@8 the primary). Synthetic and translated cases do not establish the Chinese
result: the rerank comparison records them beside tier 1's English result, never as rule E's evidence.
"""

import argparse
import asyncio
import json
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT), str(ROOT / "tools"), str(ROOT / "evals")]

import metrics  # noqa: E402
from evals import tier1  # noqa: E402  (one module, as the tests import it)

FOLDER = Path(__file__).with_name("dev_cases")


def load(name):
    return json.loads((FOLDER / name).read_text(encoding="utf-8"))


def _summary(specs, boot, scores, outcomes, groups):
    """Each metric's mean, n and interval, and the arm's (mode, reason) outcomes counted."""
    out = {"fallbacks": dict(tier1.Counter(o[1] or o[0] for o in outcomes if tuple(o) != ("hybrid", None)
                                           and tuple(o) != ("keyword_only", "reference_arm")))}
    for spec in specs:
        values = [s[spec["name"]] for s in scores]
        interval = metrics.bootstrap(values, groups, boot["resamples"], boot["seed"], boot["level"])
        out[spec["name"]] = {"mean": tier1._round(metrics.mean(values)), "n": len(values),
                             "interval": [tier1._round(v) for v in interval] if interval else None}
    return out


async def cross_language(client, state, install, files, specs, boot, arms):
    cases = load("cross_language.json")["cases"]
    limit = (await client.get("/api/settings")).json()["values"]["retrieval"]["passage_chars"]
    papers = tier1.read_qasper(files, sorted({c["paper"] for c in cases}))
    queries, record, _ = await tier1.build_qasper(client, state, install, papers, limit)
    by_key = {q["key"]: q for q in queries}
    if missing := [c["id"] for c in cases if c["question_id"] not in by_key]:
        raise tier1.Refused(f"cross-language cases whose question has no usable evidence: {missing}")
    english = [by_key[c["question_id"]] for c in cases]
    chinese = [{**by_key[c["question_id"]], "text": c["question_zh"]} for c in cases]
    await tier1.warm(state, english[0]["project"], english[0]["text"])

    def rank(answer, query):
        return [r["passage_id"] for r in answer["results"]]
    groups = [q["group"] for q in english]
    record = {"build": record["build"], "papers": record["papers"], "cases": len(cases), "document_language": "en",
              "arms": {}}
    for language, asked in (("en", english), ("zh", chinese)):
        out = await tier1.run_arms(state, arms, asked, rank)
        for arm, result in out.items():
            scores = [tier1.score_qasper(r, q["annotators"], specs) for r, q in zip(result["rankings"], asked)]
            record["arms"].setdefault(arm, {})[language] = {
                **_summary(specs, boot, scores, result["outcomes"], groups), "scores": scores}
    for arm, entry in record["arms"].items():  # the Chinese wording's paired cost, question by question
        entry["zh_minus_en"] = {}
        for spec in specs:
            diffs = [z[spec["name"]] - e[spec["name"]] for z, e in zip(entry["zh"]["scores"], entry["en"]["scores"])]
            interval = metrics.bootstrap(diffs, groups, boot["resamples"], boot["seed"], boot["level"])
            entry["zh_minus_en"][spec["name"]] = {"difference": tier1._round(metrics.mean(diffs)), "n": len(diffs),
                                                  "interval": [tier1._round(v) for v in interval] if interval else None}
        for language in ("en", "zh"):
            del entry[language]["scores"]
    return record


async def chinese_documents(client, state, install, specs, boot, arms):
    from backend.search_index import readings
    data = load("chinese_documents.json")
    began = time.perf_counter()
    project = await tier1.new_project(client, "Chinese development cases")
    made = [tier1.document_file(d["title"], "\n\n".join(d["paragraphs"])) for d in data["documents"]]
    materials = dict(zip((d["id"] for d in data["documents"]), await tier1.add_files(client, project, made), strict=True))
    build = await tier1.built(state, install, began)
    found = await asyncio.to_thread(state["db"].read, lambda c: readings(c, project, references=False))
    paragraphs = {d["id"]: d["paragraphs"] for d in data["documents"]}
    queries = []
    for i, case in enumerate(data["cases"]):
        wanted = paragraphs[case["document"]][case["paragraph"]]
        gold = {pid for pid, r in found.items() if r[0] == materials[case["document"]] and wanted in r[4]}
        if not gold:
            raise tier1.Refused(f"{case['id']}: its paragraph is in no passage")
        queries.append({"key": case["id"], "text": case["question"], "project": project, "annotators": [[sorted(gold)]],
                        "group": i})
    await tier1.warm(state, project, queries[0]["text"])
    out = await tier1.run_arms(state, arms, queries, lambda answer, query: [r["passage_id"] for r in answer["results"]])
    record = {"build": build, "documents": len(data["documents"]), "cases": len(queries), "document_language": "zh",
              "arms": {}}
    for arm, result in out.items():
        scores = [tier1.score_qasper(r, q["annotators"], specs) for r, q in zip(result["rankings"], queries)]
        record["arms"][arm] = {"zh": _summary(specs, boot, scores, result["outcomes"], [q["group"] for q in queries])}
    return record


async def run(manifest, files, out, open_app, arms):
    """Both sets, each in an app of its own; the results file says incomplete until both have run."""
    specs, boot = manifest["benchmarks"]["qasper"]["metrics"], manifest["bootstrap"]
    results = {"status": "running", "commit": tier1.commit(), "machine": tier1.machine(), "arms": list(arms),
               "started_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"), "load_average_start": tier1.os.getloadavg(),
               "note": "Synthetic and translated development cases: they do not establish the Chinese result "
                       "(ticket 72 E3)."}
    tier1.write_atomic(out, {"status": "incomplete", "reason": "running"})
    try:
        with tempfile.TemporaryDirectory(prefix="scholia-dev-cases-") as folder:
            async with open_app(Path(folder) / "cross_language") as (client, state, install):
                results["cross_language"] = await cross_language(client, state, install, files, specs, boot, arms)
            async with open_app(Path(folder) / "chinese_documents") as (client, state, install):
                results["chinese_documents"] = await chinese_documents(client, state, install, specs, boot, arms)
    except BaseException as error:
        tier1.write_atomic(out, {"status": "incomplete", "reason": f"{type(error).__name__}: {str(error)[:300]}"})
        raise
    results.update(status="finished", finished_at=time.strftime("%Y-%m-%dT%H:%M:%S%z"),
                   load_average_end=tier1.os.getloadavg())
    tier1.write_atomic(out, results)
    return results


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("command", choices=("run",))
    parser.add_argument("--sets", required=True)
    parser.add_argument("--helper", required=True)
    parser.add_argument("--model", required=True)
    parser.add_argument("--reranker")
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args(argv)
    manifest = tier1.load_manifest()
    try:
        files = tier1.verify(manifest, args.sets, ["qasper"])["qasper"]
        tier1.check_models(manifest, args.helper, args.model, args.reranker)
    except (tier1.Refused, OSError) as refused:
        sys.exit(f"refused: {refused}")
    results = asyncio.run(run(manifest, files, args.out,
                              lambda data: tier1.desktop_app(data, args.helper, args.model, args.reranker),
                              tier1.RERANK_ARMS if args.reranker else tier1.ARMS))
    print(json.dumps({part: {arm: entry for arm, entry in results[part]["arms"].items()}
                      for part in ("cross_language", "chinese_documents")}, indent=1, ensure_ascii=False))


if __name__ == "__main__":
    main()
