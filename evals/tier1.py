"""Tier 1 of the retrieval evaluation (S1-18; slice-1 spec section 14, tickets 17, 70 and 72 E): public
benchmarks built through the app's own reading and index, searched with the product's search, scored
and written to one results file.

    sandbox-exec -f tests/loopback-only.sb uv run --no-sync python -I evals/tier1.py run \\
        --level subset|full --sets <folder holding the prepared sets> \\
        --helper <Scholia.app>/Contents/MacOS/llama-server --model <Qwen3-Embedding-0.6B-Q8_0.gguf> --out FILE
    uv run --no-sync python -I evals/tier1.py compare BASELINE RUN

Everything a run reads is pinned in evals/tier1_manifest.json: each set's files (size and SHA-256,
checked before anything is built; a file that differs or is missing is refused, and nothing is ever
downloaded), the small fixed subset, the model settings and ticket 72's rule E. Each benchmark gets a
new temporary data folder and the app as the desktop runs it (create_app, an in-memory credential
store, never the Keychain); the embedding model is imported through the API and the app's helper embeds
through it. Every project is review-locked, so nothing is looked up and nothing leaves the Mac. Run it
under the loopback-only sandbox; the helper's sockets are listed with lsof.

The arms (ARMS) are plain named async functions of (state, project id, query) returning search's
answer, so S1-19 adds a rerank arm without changing the runner. The results file is written whole,
when the run ends (an `incomplete` marker stands in its place meanwhile): `status: finished` only when
every benchmark ran to its end with every arm run as named, every reading succeeded and no socket off
loopback, `status: incomplete` with the reason or the gaps otherwise (an interrupted run, a refusal, a
failure), never a file that looks complete. A hybrid arm that fell back to keyword-only for any reason
but the deadline gives no score: its queries are marked with the reason, never scored as hybrid. Deadline
fallbacks are the product's own: scored as the app returned them, counted, marked, and the hybrid
metrics are also given without them. Benchmark text never enters the
results: scores, counts and hashes, and query ids only for sets whose terms allow them
(`ids_committable`).
"""

import argparse
import asyncio
import base64
import contextlib
import hashlib
import json
import os
import platform
import signal
import subprocess
import sys
import tempfile
import time
import unicodedata
from collections import Counter, defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT), str(ROOT / "tools"), str(ROOT / "evals")]

import metrics  # noqa: E402

MANIFEST = Path(__file__).with_name("tier1_manifest.json")
ORIGIN = "http://127.0.0.1:1"
ADD_BATCH = 20  # files per request (backend.materials.MAX_FILES)
POLL_SECONDS = 1.0  # how often a build is looked at


class Refused(Exception):
    """A pin, file or input the run will not use."""


def clean(text):
    return " ".join(str(text or "").split())


def load_manifest(path=MANIFEST):
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def verify(manifest, sets, names):
    """{benchmark: {role: path}} for the files the named benchmarks read, each checked against its pin
    (size, then SHA-256) before any is used."""
    from backend.local_helper import mismatch
    found = {}
    for name in names:
        bench = manifest["benchmarks"][name]
        found[name] = {}
        for role, pin in bench["files"].items():
            path = Path(sets) / bench["folder"] / pin["path"]
            if not path.is_file():
                raise Refused(f"{name}: {pin['path']} is missing under {Path(sets) / bench['folder']}; prepare the "
                              "set first (the runner never downloads)")
            if problem := mismatch(path, pin):
                raise Refused(f"{name}: {problem}")
            found[name][role] = path
    return found


def check_models(manifest, helper, model):
    """The helper bundle (its SHA-256 manifest) and the embedding model, checked before use."""
    from backend.local_helper import EMBEDDING_MODEL, binary_problem, mismatch
    if EMBEDDING_MODEL["sha256"] != manifest["model_settings"]["embedding_model"]["sha256"]:
        raise Refused("the app's embedding pin differs from the manifest's")
    if problem := mismatch(Path(model), EMBEDDING_MODEL):
        raise Refused(f"the embedding model: {problem}")
    if problem := binary_problem(Path(helper)):
        raise Refused(f"the helper: {problem}")


# Reading the sets (untrusted data: read as data, never run)


def _jsonl(path):
    with open(path, encoding="utf-8") as f:
        return [json.loads(line) for line in f if line.strip()]


def read_qasper(files, papers=None):
    """{paper id: paper} of the test split, all of it or the listed papers (each must be there)."""
    with open(files["test"], encoding="utf-8") as f:
        found = json.load(f)
    if papers is None:
        return dict(sorted(found.items()))
    if missing := [p for p in papers if p not in found]:
        raise Refused(f"qasper: subset papers not in the test split: {missing[:5]}")
    return {p: found[p] for p in sorted(papers)}


def read_beir(files):
    """(documents [(id, title, text)], queries [(id, text, {doc id: grade})]): the judged test queries."""
    docs = [(str(d["_id"]), clean(d.get("title")), clean(d.get("text"))) for d in _jsonl(files["corpus"])]
    judged = defaultdict(dict)
    with open(files["qrels"], encoding="utf-8") as f:
        next(f)  # the header
        for line in f:
            if line.strip():
                qid, doc, grade = line.rstrip("\n").split("\t")
                judged[qid][doc] = int(grade)
    texts = {str(q["_id"]): clean(q.get("text")) for q in _jsonl(files["queries"])}
    return docs, [(qid, texts.get(qid, ""), judged[qid]) for qid in sorted(judged)]


def read_litsearch(files):
    """(documents, queries [(position, text, {gold id: 1}, specific)]): LitSearch's queries have no ids."""
    docs = [(str(d["corpusid"]), clean(d.get("title")), clean(d.get("abstract"))) for d in _jsonl(files["corpus"])]
    return docs, [(str(i), clean(q["query"]), {str(c): 1 for c in q["corpusids"]}, q["specificity"] == 1)
                  for i, q in enumerate(_jsonl(files["queries"]))]


def file_name(title):
    """A material's file name from a title: the app takes a material's title from its file's name."""
    stem = "".join(" " if ch in "/\\" or unicodedata.category(ch)[0] == "C" else ch for ch in clean(title))
    return (clean(stem).lstrip(".")[:250] or "Untitled") + ".md"


def document_file(title, text):
    """One document as a Markdown file of its text (its title when it has none); None when both are empty."""
    body = clean(text) or clean(title)
    return (file_name(title), (body + "\n").encode()) if body else None


# QASPER evidence (ticket 72 E1; the manifest's rules)


def _key(text):
    return "".join(ch for ch in unicodedata.normalize("NFKC", text).casefold() if ch.isalnum())


def evidence_passages(evidence, passages, limit=2000):
    """The ids of the passages (id, text), in reading order, that an evidence paragraph maps to: those
    whose text equals it or contains it; or, for a paragraph longer than the passage limit (split by
    section 7.1), the consecutive passages whose texts together make it up. Letters and digits only,
    case-folded, so whitespace and Markdown punctuation do not count."""
    wanted = _key(evidence)
    if not wanted:
        return []
    keys = [(pid, _key(text)) for pid, text in passages]
    found = [pid for pid, have in keys if have and (wanted == have or wanted in have)]
    if found or len(clean(evidence)) <= limit:
        return found
    for start in range(len(keys)):
        made, run = "", []
        for pid, have in keys[start:]:
            if not have or not wanted.startswith(made + have):
                break
            made += have
            run.append(pid)
            if made == wanted:
                return run
    return []


def qasper_question(qa, passages, headings, limit=2000):
    """(annotators, exclusion, unmatched): each usable annotator as a list of evidence items, each the
    passage ids it maps to; the exclusion reason when none is usable; the evidence left out of a usable
    or unusable annotator's set, as (kind, text): text that matched no passage (heading, other), and an
    annotator's figure or table items beside text evidence (figure_or_table)."""
    kinds, annotators, unmatched = Counter(), [], []
    for answer in qa.get("answers") or []:
        given = answer.get("answer") or {}
        if given.get("unanswerable"):
            kinds["unanswerable"] += 1
            continue
        evidence = [clean(e) for e in given.get("evidence") or [] if clean(e)]
        text = [e for e in evidence if not e.startswith("FLOAT SELECTED")]
        if not evidence:
            kinds["no_evidence"] += 1
        elif not text:
            kinds["figure_or_table"] += 1
        else:
            items = []
            unmatched += [("figure_or_table", item) for item in evidence if item not in text]
            for item in text:
                if hits := evidence_passages(item, passages, limit):
                    items.append(hits)
                else:
                    unmatched.append(("heading" if item in headings else "other", item))
            if items:
                annotators.append(items)
            else:
                kinds["unmatched"] += 1
    if annotators:
        return annotators, None, unmatched
    answers = sum(kinds.values())
    reason = ("all_unanswerable" if answers and kinds["unanswerable"] == answers else
              "evidence_matching_no_passage" if kinds["unmatched"] else
              "figure_or_table_only" if kinds["figure_or_table"] else "no_evidence")
    return [], reason, unmatched


def score_qasper(ranking, annotators, specs):
    """Each metric for one question, the best annotator's (as QASPER's own evaluator takes the best
    reference). recall@k: the share of evidence items with a passage in the top k; nDCG and MRR: binary
    relevance over the passages the evidence maps to."""
    best = {}
    for items in annotators:
        relevant = {pid for item in items for pid in item}
        for spec in specs:
            k = spec["k"]
            if spec["kind"] == "recall":
                top = set(ranking[:k])
                value = sum(1 for item in items if top & set(item)) / len(items)
            elif spec["kind"] == "ndcg":
                value = metrics.ndcg_at(ranking, relevant, k)
            else:
                value = metrics.mrr_at(ranking, relevant, k)
            best[spec["name"]] = max(best.get(spec["name"], 0.0), value)
    return best


def score_documents(ranking, relevant, specs):
    fns = {"recall": metrics.recall_at, "ndcg": metrics.ndcg_at, "mrr": metrics.mrr_at}
    return {spec["name"]: fns[spec["kind"]](ranking, relevant, spec["k"]) for spec in specs}


# The arms


class _NoDense:
    """The keyword-only arm's stand-in for the helper's status: search sees dense search unavailable."""

    def status(self):
        return {"search": {"mode": "keyword_only", "reason": "reference_arm"}}


async def hybrid(state, project_id, query):
    from backend.search import MAX_LIMIT, search
    return await search(state, project_id, query, MAX_LIMIT)


async def keyword_only(state, project_id, query):
    from backend.search import MAX_LIMIT, search
    return await search({**state, "local_helper": _NoDense()}, project_id, query, MAX_LIMIT)


ARMS = {"hybrid": hybrid, "keyword_only": keyword_only}
# What each arm's answers must say (mode, reason) to count as that arm; an arm S1-19 adds names its own.
EXPECTED = {"hybrid": ("hybrid", None), "keyword_only": ("keyword_only", "reference_arm")}


# Building a benchmark through the app


async def _post(client, path, body, status):
    response = await client.post(path, json=body)
    if response.status_code != status:
        raise RuntimeError(f"{path}: {response.status_code} {response.text[:200]}")
    return response.json()


async def new_project(client, name):
    project = (await _post(client, "/api/projects", {"name": name}, 201))["id"]
    await _post(client, f"/api/projects/{project}/review-lock", {"locked": True, "venue": "APA"}, 200)  # no lookups
    return project


async def add_files(client, project, files):
    """The material id of each (name, bytes), in order (a file the project already has gives its material)."""
    ids = []
    for start in range(0, len(files), ADD_BATCH):
        found = await _post(client, f"/api/projects/{project}/materials", {"files": [
            {"name": name, "data": base64.b64encode(data).decode()} for name, data in files[start:start + ADD_BATCH]]}, 201)
        ids += [m["id"] for m in found["materials"]]
    return ids


async def until_built(state):
    """Wait until every reading and index run has ended, the index queue is empty and every passage has
    its vector; RuntimeError when the runs ended with passages left unembedded."""
    db, index = state["db"], state["index"]

    def busy(conn):
        return bool(conn.execute("SELECT 1 FROM runs WHERE workflow IN ('extract', 'index') AND status = 'running'"
                                 " LIMIT 1").fetchone() or conn.execute("SELECT 1 FROM index_queue LIMIT 1").fetchone())
    settled = 0
    while True:
        await asyncio.sleep(POLL_SECONDS)
        if await asyncio.to_thread(db.read, busy):
            settled = 0
            continue
        if not await asyncio.to_thread(index.unembedded):
            return
        settled += 1  # nothing running and passages without vectors: a run just requested, or the runs failed
        if settled >= 3:
            failed = await asyncio.to_thread(db.read, lambda c: c.execute(
                "SELECT summary FROM runs WHERE workflow = 'index' AND status != 'succeeded' ORDER BY rowid DESC"
                " LIMIT 1").fetchone())
            raise RuntimeError(f"the index runs ended with passages unembedded ({failed[0] if failed else 'none failed'})")


async def until_read(state):
    """Wait until every reading has ended and its passages are in the keyword index (no extract or index
    run running and the queue empty, twice in a row)."""
    db = state["db"]

    def busy(conn):
        return bool(conn.execute("SELECT 1 FROM runs WHERE workflow IN ('extract', 'index') AND status = 'running'"
                                 " LIMIT 1").fetchone() or conn.execute("SELECT 1 FROM index_queue LIMIT 1").fetchone())
    quiet = 0
    while quiet < 2:
        await asyncio.sleep(POLL_SECONDS)
        quiet = 0 if await asyncio.to_thread(db.read, busy) else quiet + 1


async def built(state, install, began):
    """The build's second half and its record: once the files are read and keyword-indexed, the search
    model is installed (imported through the API), and one index run per project embeds its passages, as
    when a researcher installs the model after adding papers; then the build's figures."""
    await until_read(state)
    read = time.perf_counter()
    await install()
    await until_built(state)
    stats = await build_stats(state, began)
    stats["read_and_keyword_seconds"] = round(read - began, 1)
    stats["embedding_seconds"] = round(time.perf_counter() - read, 1)
    stats["embedded_per_second"] = round(stats["passages"] / max(stats["embedding_seconds"], 0.001), 1)
    return stats


async def build_stats(state, began):
    from helper_timings import summary
    db = state["db"]
    lengths = await asyncio.to_thread(db.read, lambda c: [n for (n,) in c.execute("SELECT length(text) FROM passages")])
    failed = await asyncio.to_thread(db.read, lambda c: c.execute(
        "SELECT count(*) FROM runs WHERE workflow = 'extract' AND status != 'succeeded'").fetchone()[0])
    index_file = Path(state["data_dir"]) / "index" / "search.sqlite3"
    return {"seconds": round(time.perf_counter() - began, 1), "passages": len(lengths),
            "passage_chars": summary(lengths), "passages_over_2000_chars": sum(1 for n in lengths if n > 2000),
            "readings_not_succeeded": failed,
            "index_bytes": sum(p.stat().st_size for p in index_file.parent.glob(index_file.name + "*") if p.is_file())}


async def warm(state, project, text):
    """One unscored search, then the helper running, before the scored searches (a first start after its
    idle stop goes past the deadline, as in the app)."""
    from backend.local_helper import EMBEDDING
    await hybrid(state, project, text)
    local = state.get("local_helper")
    helper = local.helpers.get(EMBEDDING) if local is not None else None
    for _ in range(600):
        if helper is None or helper.state == "running":
            return
        await asyncio.sleep(0.1)


async def run_arms(state, arms, queries, rank):
    """Each arm over the queries in order: each query's ranking (rank(answer, query)), (mode, reason)
    and the search's seconds."""
    from helper_timings import summary
    out = {}
    for name, arm in arms.items():
        rankings, outcomes, seconds = [], [], []
        for query in queries:
            began = time.perf_counter()
            answer = await arm(state, query["project"], query["text"])
            seconds.append(time.perf_counter() - began)
            rankings.append(rank(answer, query))
            reason = answer["reason"]
            if answer.get("index") != "ready":  # being rebuilt or unavailable: not the built index
                reason = "index_not_ready"
            elif answer["mode"] == "hybrid" and (answer.get("coverage") or {}).get("embedded") != \
                    (answer.get("coverage") or {}).get("total"):  # meaning search over part of the passages
                reason = "coverage_partial"
            outcomes.append((answer["mode"], reason))
        out[name] = {"rankings": rankings, "outcomes": outcomes, "seconds": summary(seconds)}
    return out


async def build_qasper(client, state, install, papers, limit):
    """One project per paper (retrieval within the question's own paper), its questions mapped."""
    from search_timings import markdown
    from backend.search_index import readings
    began = time.perf_counter()
    projects = {}
    for pid, paper in papers.items():
        project = await new_project(client, f"QASPER {pid}")
        [material] = await add_files(client, project, [(file_name(paper.get("title")), markdown(paper))])
        projects[pid] = (project, material)
    record = {"build": await built(state, install, began), "papers": len(papers)}
    queries, exclusions, unmatched, items, stable, sample = [], Counter(), Counter(), 0, {}, []
    for group, (pid, paper) in enumerate(papers.items()):
        project, material = projects[pid]
        found = await asyncio.to_thread(state["db"].read, lambda c, p=project: readings(c, p, references=False))
        passages = [(passage, r[4]) for passage, r in found.items() if r[0] == material]
        stable.update({passage: f"{pid}:{r[6]}" for passage, r in found.items()})  # comparable between any two runs
        headings = {clean(s.get("section_name")) for s in paper.get("full_text") or []}
        for qa in paper.get("qas") or []:
            if not passages:
                exclusions["paper_not_read"] += 1
                continue
            annotators, reason, missed = qasper_question(qa, passages, headings, limit)
            unmatched.update(kind for kind, _ in missed)
            sample += [(kind, pid, text[:160]) for kind, text in missed if kind != "figure_or_table"][:max(0, 40 - len(sample))]
            if reason:
                exclusions[reason] += 1
                continue
            items += sum(len(a) for a in annotators)
            queries.append({"key": str(qa.get("question_id")), "text": clean(qa.get("question")), "group": group,
                            "project": project, "annotators": annotators})
    dropped = unmatched.pop("figure_or_table", 0)
    record.update(exclusions=dict(exclusions), evidence_items_mapped=items, evidence_items_unmatched=dict(unmatched),
                  figure_or_table_items_dropped=dropped)
    # For the person running it, never in the results (benchmark text): a sample of what matched nothing.
    print(json.dumps({"unmatched_evidence_sample": sample}, ensure_ascii=False), file=sys.stderr)
    return queries, record, stable


async def build_documents(client, state, install, docs, name):
    """One project of one material per document; {material id: [document ids]} (documents with the
    same file share a material)."""
    began = time.perf_counter()
    project = await new_project(client, name)
    files, kept = [], []
    for doc_id, title, text in docs:
        if made := document_file(title, text):
            files.append(made)
            kept.append(doc_id)
    documents_of = defaultdict(list)
    for doc_id, material in zip(kept, await add_files(client, project, files), strict=True):
        documents_of[material].append(doc_id)
    return project, documents_of, {"build": await built(state, install, began), "documents": len(docs),
                                   "documents_empty": len(docs) - len(kept),
                                   "documents_sharing_a_file": sum(len(d) - 1 for d in documents_of.values())}


# Scoring and summaries


def _digest(items):
    return hashlib.sha256(json.dumps(items, ensure_ascii=False).encode()).hexdigest()[:16]


def _round(value):
    return None if value is None else round(value, 4)


def _selected(spec, specific):
    want = spec.get("queries")
    return [i for i, s in enumerate(specific) if want is None or s == (want == "specific")]


def summarize(bench, boot, queries, arms_out, scores):
    """The benchmark's record: per arm, whether it ran as named, its fallbacks, its metrics with intervals
    and per-query scores; each other arm's paired difference from the hybrid arm."""
    groups = [q.get("group", i) for i, q in enumerate(queries)]
    keys = [q["key"] for q in queries]
    specific = [q.get("specific") for q in queries]
    record = {"queries": len(queries), "order_sha256": hashlib.sha256("\n".join(keys).encode()).hexdigest(),
              "keys": keys if bench["ids_committable"] else None,
              "groups": groups if bench["resample"] == "paper" else None,
              "specific": specific if any(s is not None for s in specific) else None, "arms": {}}
    for arm, out in arms_out.items():
        expected = EXPECTED.get(arm, ("hybrid", None))
        marked = [i for i, outcome in enumerate(out["outcomes"]) if tuple(outcome) != expected]
        reasons = Counter(out["outcomes"][i][1] or out["outcomes"][i][0] for i in marked)
        # Only the hybrid deadline is the product's own fallback: scored as returned, and counted. Any other
        # reason means the arm did not run as named: it gets no score, and its queries are marked.
        complete = set(reasons) <= {"deadline"} if arm == "hybrid" else not marked
        entry = {"complete": complete, "fallbacks": dict(reasons), "fallback_queries": marked,
                 "search_seconds": out["seconds"], "ranking_digests": out["digests"]}
        if complete:
            entry["scores"] = {s["name"]: [None if row[s["name"]] is None else round(row[s["name"]], 6)
                                           for row in scores[arm]] for s in bench["metrics"]}
            entry["metrics"] = metric_summary(bench, boot, entry["scores"], groups, specific)
            if marked:  # the deadline's keyword-only answers, counted above, also left out
                entry["metrics_without_fallbacks"] = metric_summary(bench, boot, entry["scores"], groups, specific,
                                                                    set(marked))
        record["arms"][arm] = entry
    base = record["arms"].get("hybrid", {}).get("scores")
    for arm, entry in record["arms"].items():
        if arm != "hybrid" and base and entry.get("scores"):
            entry["minus_hybrid"] = paired(bench, boot, entry["scores"], base, groups, specific)
    return record


def metric_summary(bench, boot, scores, groups, specific, skip=frozenset()):
    """Each metric's mean, n and interval over the queries it covers, less those in skip."""
    out = {}
    for spec in bench["metrics"]:
        chosen = [i for i in _selected(spec, specific) if i not in skip and scores[spec["name"]][i] is not None]
        values = [scores[spec["name"]][i] for i in chosen]
        interval = metrics.bootstrap(values, [groups[i] for i in chosen], boot["resamples"], boot["seed"], boot["level"])
        out[spec["name"]] = {"mean": _round(metrics.mean(values)), "n": len(values),
                             "interval": [_round(v) for v in interval] if interval else None}
    return out


def gaps(results):
    """What keeps a run that ran to its end from being finished: an arm that did not run as named, a
    reading that failed (a document missing from its corpus), a socket off loopback."""
    found = []
    for name, bench in results["benchmarks"].items():
        found += [f"{name} {arm}: did not run as named ({entry['fallbacks']})"
                  for arm, entry in bench["arms"].items() if not entry["complete"]]
        if bench["build"]["readings_not_succeeded"]:
            found.append(f"{name}: {bench['build']['readings_not_succeeded']} readings did not succeed")
        if bench.get("helper_sockets") is None:
            found.append(f"{name}: the helper's sockets were not sampled (it was not running)")
        if bench.get("not_loopback_sockets"):
            found.append(f"{name}: sockets off loopback {bench['not_loopback_sockets']}")
    return found


def paired(bench, boot, mine, other, groups, specific, chosen_pairs=None):
    """mine minus other, query by query (positions in chosen_pairs: (mine's, other's), default the same),
    with the paired percentile interval; whole groups resampled when groups repeat (QASPER's papers)."""
    out = {}
    for spec in bench["metrics"]:
        name = spec["name"]
        pairs = chosen_pairs or [(i, i) for i in range(len(mine[name]))]
        allowed = set(_selected(spec, specific))
        kept = [(i, j) for i, j in pairs if i in allowed and mine[name][i] is not None and other[name][j] is not None]
        diffs = [mine[name][i] - other[name][j] for i, j in kept]
        interval = metrics.bootstrap(diffs, [groups[i] for i, _ in kept], boot["resamples"], boot["seed"], boot["level"])
        out[name] = {"difference": _round(metrics.mean(diffs)), "n": len(diffs),
                     "interval": [_round(v) for v in interval] if interval else None}
    return out


async def evaluate(name, bench, boot, files, chosen, client, state, install, arms):
    """Build one benchmark in the app given, run the arms over its queries and score them."""
    from backend.search import QUERY_CHARS
    limit = (await client.get("/api/settings")).json()["values"]["retrieval"]["passage_chars"]
    if name == "qasper":
        papers = read_qasper(files, chosen.get("papers") if isinstance(chosen, dict) else None)
        queries, record, stable = await build_qasper(client, state, install, papers, limit)

        def rank(answer, query):
            return [r["passage_id"] for r in answer["results"]]

        def comparable(ranking):
            return [stable.get(p, p) for p in ranking]
    else:
        docs, raw = read_litsearch(files) if name == "litsearch" else read_beir(files)
        project, documents_of, record = await build_documents(client, state, install, docs, name)
        queries, skipped = [], 0
        for qid, text, relevant, *specific in raw:
            if not text or len(text) > QUERY_CHARS:  # the search API's own limit
                skipped += 1
                continue
            queries.append({"key": qid, "text": text, "relevant": relevant, "project": project,
                            # BEIR's evaluate drops a document whose id is the query's; LitSearch's keys are positions
                            **({"specific": specific[0]} if specific else {"drop": qid})})
        record["exclusions"] = {"query_empty_or_too_long": skipped}

        def rank(answer, query):  # a document at its best passage's rank
            ranked = metrics.documents([r["material_id"] for r in answer["results"]], lambda m: documents_of.get(m, ()))
            return [d for d in ranked if d != query.get("drop")]

        def comparable(ranking):
            return ranking
    if not queries:
        raise Refused(f"{name}: no query left to score")
    await warm(state, queries[0]["project"], queries[0]["text"])
    out = await run_arms(state, arms, queries, rank)
    scores = {}
    for arm, result in out.items():
        result["digests"] = [_digest(comparable(r)) for r in result["rankings"]]
        if name == "qasper":
            scores[arm] = [score_qasper(r, q["annotators"], bench["metrics"]) for r, q in zip(result["rankings"], queries)]
        else:
            scores[arm] = [score_documents(r, q["relevant"], bench["metrics"]) for r, q in zip(result["rankings"], queries)]
    if name != "qasper":
        largest = max(s["k"] for s in bench["metrics"])
        record["queries_with_fewer_documents_than_largest_cutoff"] = {
            arm: sum(1 for r in result["rankings"] if len(r) < largest) for arm, result in out.items()}
    return {**record, **summarize(bench, boot, queries, out, scores), "query_language": "en", "document_language": "en"}


# The run


def machine():
    def sysctl(name):
        try:
            return subprocess.run(["sysctl", "-n", name], capture_output=True, text=True, timeout=10).stdout.strip()
        except OSError:
            return None
    memory = sysctl("hw.memsize")
    return {"chip": sysctl("machdep.cpu.brand_string"), "model": sysctl("hw.model"),
            "memory_gb": round(int(memory) / 2**30) if memory and memory.isdigit() else None,
            "macos": platform.mac_ver()[0], "python": platform.python_version()}


def commit():
    def git(*args):
        try:
            return subprocess.run(["git", "-C", str(ROOT), *args], capture_output=True, text=True, timeout=30).stdout.strip()
        except OSError:
            return None
    return {"sha": git("rev-parse", "HEAD"), "modified": bool(git("status", "--porcelain", "--untracked-files=no"))}


def model_settings(state, values):
    """What the run used: the embedding pin, the llama.cpp build pinned for the helper, the bundle's
    runtime digest, the helper's flags, the query instruction and the [retrieval] values in effect."""
    from backend import local_helper
    from backend.search import QUERY_INSTRUCTION, identity
    from fetch import PINS
    pin = local_helper.EMBEDDING_MODEL
    return {"embedding_model": {k: pin[k] for k in ("id", "file", "size", "sha256")},
            "llama_cpp": {k: PINS["llama.cpp"][k] for k in ("file", "size", "sha256")},
            "helper_runtime_sha256": identity(state.get("helper_config") or local_helper.Config())["runtime"],
            "helper_flags": [*local_helper.FLAGS["embedding"], *local_helper.LIMITS],
            "query_instruction": QUERY_INSTRUCTION,
            "retrieval": {k: values[k] for k in ("passage_chars", "bm25_candidates", "dense_candidates", "rrf_k", "keep",
                                                 "embedding_batch", "hybrid_ms")},
            "reranker": None}


@contextlib.asynccontextmanager
async def desktop_app(data_dir, helper, model):
    """The app on a new data folder as the desktop runs it; yields (client, state, install), install
    importing the embedding model through the API."""
    import httpx
    from backend import local_helper
    from backend.app import create_app
    from backend.self_test import _Keys
    app = create_app(data_dir, origin=ORIGIN, keyring_backend=_Keys(), helper=local_helper.Config(binary=Path(helper)))
    async with app.app.router.lifespan_context(app.app):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url=ORIGIN,
                                     headers={"X-Scholia-Client": "local"}, timeout=3600) as client:
            async def install():
                await _post(client, "/api/helper/models/import",
                            {"model": local_helper.EMBEDDING, "path": str(Path(model).resolve())}, 200)
            yield client, app.app.state.scholia, install


def write_atomic(path, results):
    path = Path(path)
    part = path.with_name(path.name + ".part")
    with open(part, "w", encoding="utf-8") as f:
        f.write(json.dumps(results, ensure_ascii=False, indent=1) + "\n")
        f.flush()
        os.fsync(f.fileno())
    os.replace(part, path)


async def run(manifest, level_name, names, files, out, open_app, arms=None):
    """Every named benchmark, each in an app of its own on a new temporary data folder; the results file
    first an incomplete marker, then once more at the end: finished, or incomplete with the reason the run
    stopped or the gaps that keep it from being finished."""
    from helper_timings import not_loopback, sockets
    arms = arms or ARMS
    level = manifest["levels"][level_name]
    results = {"status": "running", "level": level_name, "benchmarks_asked": names, "manifest_sha256":
               hashlib.sha256(MANIFEST.read_bytes()).hexdigest() if MANIFEST.exists() else None,
               "commit": commit(), "machine": machine(), "reference_machine": manifest["reference_machine"],
               "started_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"), "load_average_start": os.getloadavg(),
               "arms": list(arms), "bootstrap": manifest["bootstrap"], "benchmarks": {}}
    began = time.perf_counter()
    # Whatever was at out before (a run killed outright leaves this marker, never an older finished file)
    write_atomic(out, {**{k: results[k] for k in ("level", "benchmarks_asked", "commit", "started_at")},
                       "status": "incomplete", "reason": "running"})
    try:
        with tempfile.TemporaryDirectory(prefix="scholia-tier1-") as folder:
            for name in names:
                data = Path(folder) / name
                async with open_app(data) as (client, state, install):
                    values = (await client.get("/api/settings")).json()["values"]["retrieval"]
                    settings = model_settings(state, values)
                    if "model_settings" not in results:
                        results["model_settings"] = settings
                        results["like_for_like"] = settings == manifest["model_settings"]
                    elif settings != results["model_settings"]:
                        raise Refused(f"{name}: the model settings changed during the run")
                    record = await evaluate(name, manifest["benchmarks"][name], manifest["bootstrap"], files[name],
                                            level["benchmarks"][name], client, state, install, arms)
                    local = state.get("local_helper")
                    helper = next(iter(local.helpers.values()), None) if local is not None else None
                    pid = getattr(getattr(helper, "_process", None), "pid", None)
                    record["helper_sockets"] = sockets(pid) if pid else None
                    record["not_loopback_sockets"] = not_loopback([os.getpid(), pid] if pid else [os.getpid()])
                results["benchmarks"][name] = record
    except BaseException as error:  # interrupted, refused or failed: said so, never a finished-looking file
        write_atomic(out, {**{k: results[k] for k in ("level", "benchmarks_asked", "commit", "started_at")},
                           "status": "incomplete", "reason": f"{type(error).__name__}: {str(error)[:300]}",
                           "benchmarks_finished": list(results["benchmarks"])})
        raise
    found = gaps(results)
    results.update(status="incomplete" if found else "finished", gaps=found, seconds=round(time.perf_counter() - began, 1),
                   load_average_end=os.getloadavg(), finished_at=time.strftime("%Y-%m-%dT%H:%M:%S%z"))
    write_atomic(out, results)
    return results


# Comparing a run with the baseline


def compare(manifest, baseline, current):
    """Lines giving each benchmark's paired difference (current minus baseline) per arm and metric with
    its interval, flagging a drop whose interval lies wholly below zero for investigation. Nothing here
    blocks a merge (slice 1 section 14)."""
    for which, results in (("baseline", baseline), ("run", current)):
        if results.get("status") != "finished":
            raise Refused(f"the {which} is not a finished run (status {results.get('status')})")
    lines = []
    if not current.get("like_for_like") or current.get("model_settings") != baseline.get("model_settings"):
        lines.append("NOT LIKE FOR LIKE: the model settings differ from the baseline's or the manifest's")
    if current.get("manifest_sha256") != baseline.get("manifest_sha256"):
        lines.append("NOT LIKE FOR LIKE: the manifest (pins, rules, subset) differs from the baseline's")
    if current.get("bootstrap") != baseline.get("bootstrap"):
        lines.append("the bootstrap settings differ from the baseline's; the intervals below use the manifest's")
    if current.get("machine") != baseline.get("machine"):
        lines.append(f"NOT LIKE FOR LIKE: machine {current.get('machine')} differs from the baseline's")
    for name, run_bench in current["benchmarks"].items():
        base_bench = baseline["benchmarks"].get(name)
        if base_bench is None:
            lines.append(f"{name}: not in the baseline")
            continue
        if run_bench["keys"] is not None and base_bench["keys"] is not None:
            where = {k: j for j, k in enumerate(base_bench["keys"])}
            pairs = [(i, where[k]) for i, k in enumerate(run_bench["keys"]) if k in where]
        elif run_bench["order_sha256"] == base_bench["order_sha256"]:
            pairs = [(i, i) for i in range(run_bench["queries"])]
        else:
            lines.append(f"{name}: its queries differ from the baseline's; not compared")
            continue
        groups = run_bench["groups"] or list(range(run_bench["queries"]))
        specific = run_bench["specific"] or [None] * run_bench["queries"]
        bench = manifest["benchmarks"][name]
        for arm, entry in run_bench["arms"].items():
            base = base_bench["arms"].get(arm)
            if not entry.get("scores") or not base or not base.get("scores"):
                lines.append(f"{name} {arm}: not compared (an arm that did not run as named, or none in the baseline)")
                continue
            same = sum(1 for i, j in pairs if entry["ranking_digests"][i] == base["ranking_digests"][j])
            lines.append(f"{name} {arm}: {len(pairs)} queries paired, rankings identical for {same}; fallbacks "
                         f"{entry['fallbacks'] or 'none'}")
            for metric, result in paired(bench, manifest["bootstrap"], entry["scores"], base["scores"], groups,
                                         specific, pairs).items():
                low, high = result["interval"] or (None, None)
                flag = "  DROP: investigate and record a disposition" if high is not None and high < 0 else ""
                lines.append(f"  {metric}: difference {result['difference']:+.4f} [{low:+.4f}, {high:+.4f}] "
                             f"n={result['n']}{flag}" if result["interval"] else f"  {metric}: no pairs")
    return lines


def report(results):
    """A short Markdown summary of a results file: per benchmark and arm, each metric with its interval."""
    def interval(found):
        return f"[{found[0]:.3f}, {found[1]:.3f}]" if found else "none"
    commit_ = results.get("commit") or {}
    lines = [f"# Tier 1 results ({results.get('level')}, {results.get('status')})", ""]
    if results.get("status") != "finished":
        lines += ["Not finished: " + "; ".join([*([results["reason"]] if results.get("reason") else []),
                                                *(results.get("gaps") or [])]), ""]
    lines += [f"Commit `{commit_.get('sha')}`{' (modified)' if commit_.get('modified') else ''}; "
             f"{results.get('started_at')} to {results.get('finished_at')}; {results.get('seconds')} s; machine "
             f"{results.get('machine')}; load average {results.get('load_average_start')} at the start and "
             f"{results.get('load_average_end')} at the end; model settings like the manifest's: "
             f"{results.get('like_for_like')}. Queries and documents are English (query language en, document "
             "language en). Each benchmark is reported on its own; there is no combined score.", ""]
    for name, bench in (results.get("benchmarks") or {}).items():
        build = bench["build"]
        chars = build["passage_chars"] or {}
        lines += [f"## {name}", "",
                  f"{bench['queries']} queries run; excluded {bench.get('exclusions') or 'none'}. Build "
                  f"{build['seconds']} s, {build['passages']} passages (characters median {chars.get('median')}, p95 "
                  f"{chars.get('p95')}, max {chars.get('max')}; {build['passages_over_2000_chars']} over 2,000), index "
                  f"{build['index_bytes'] / 2**20:.1f} MiB, readings not succeeded {build['readings_not_succeeded']}.", "",
                  "| Arm | Metric | Mean | 95% interval | n | Minus hybrid (95% interval) |", "|---|---|---|---|---|---|"]
        for arm, entry in bench["arms"].items():
            if not entry.get("metrics"):
                lines.append(f"| {arm} | not scored: fallbacks {entry['fallbacks']} | | | | |")
                continue
            for metric, found in entry["metrics"].items():
                diff = (entry.get("minus_hybrid") or {}).get(metric)
                mean = "none" if found["mean"] is None else f"{found['mean']:.3f}"
                lines.append(f"| {arm} | {metric} | {mean} | {interval(found['interval'])} | {found['n']} | "
                             + (f"{diff['difference']:+.3f} {interval(diff['interval'])}"
                                if diff and diff["difference"] is not None else "") + " |")
        for arm, entry in bench["arms"].items():
            if entry.get("metrics_without_fallbacks"):
                lines.append("")
                lines.append(f"{arm} without its {len(entry['fallback_queries'])} fallback queries: " + "; ".join(
                    f"{m} {'none' if f['mean'] is None else format(f['mean'], '.3f')} {interval(f['interval'])} n={f['n']}"
                    for m, f in entry["metrics_without_fallbacks"].items()))
        lines += ["", "Keyword-only fallbacks of the hybrid arm: "
                  f"{bench['arms'].get('hybrid', {}).get('fallbacks') or 'none'}; search seconds (median, p95): "
                  + "; ".join(f"{arm} {e['search_seconds']['median']}, {e['search_seconds']['p95']}"
                              for arm, e in bench["arms"].items() if e.get("search_seconds")), ""]
        for key in ("evidence_items_mapped", "evidence_items_unmatched", "figure_or_table_items_dropped", "documents",
                    "documents_empty",
                    "documents_sharing_a_file", "queries_with_fewer_documents_than_largest_cutoff"):
            if key in bench:
                lines.append(f"- {key.replace('_', ' ')}: {bench[key]}")
        lines.append("")
    return lines


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    sub = parser.add_subparsers(dest="command", required=True)
    go = sub.add_parser("run")
    go.add_argument("--level", choices=("subset", "full"), required=True)
    go.add_argument("--sets", required=True, help="the folder holding the prepared sets (the manifest's folders)")
    go.add_argument("--helper", required=True, help="the bundle's llama-server")
    go.add_argument("--model", required=True, help="Qwen3-Embedding-0.6B-Q8_0.gguf")
    go.add_argument("--benchmarks", nargs="+", help="only these of the level's benchmarks")
    go.add_argument("--out", type=Path, required=True)
    against = sub.add_parser("compare")
    against.add_argument("baseline", type=Path)
    against.add_argument("current", type=Path)
    sub.add_parser("report").add_argument("results", type=Path)
    args = parser.parse_args(argv)
    manifest = load_manifest()
    if args.command == "report":
        print("\n".join(report(load_manifest(args.results))))
        return
    if args.command == "compare":
        try:
            lines = compare(manifest, load_manifest(args.baseline), load_manifest(args.current))
        except Refused as refused:
            sys.exit(f"refused: {refused}")
        print("\n".join(lines))
        return
    level = list(manifest["levels"][args.level]["benchmarks"])
    names = args.benchmarks or level
    if unknown := [n for n in names if n not in level]:
        parser.error(f"not in the {args.level} level: {unknown}")
    try:
        files = verify(manifest, args.sets, names)
        check_models(manifest, args.helper, args.model)
    except Refused as refused:
        write_atomic(args.out, {"status": "incomplete", "reason": f"refused: {refused}", "level": args.level})
        sys.exit(f"refused: {refused}")

    async def main_task():
        task = asyncio.current_task()
        asyncio.get_running_loop().add_signal_handler(signal.SIGTERM, task.cancel)  # cleaned up as for Ctrl-C
        return await run(manifest, args.level, names, files, args.out,
                         lambda data: desktop_app(data, args.helper, args.model))
    results = asyncio.run(main_task())
    print(json.dumps({name: {arm: entry.get("metrics") for arm, entry in bench["arms"].items()}
                      for name, bench in results["benchmarks"].items()}, indent=1))
    if results["status"] != "finished":
        sys.exit(f"{results['status']}: " + "; ".join(results["gaps"]))


if __name__ == "__main__":
    main()
