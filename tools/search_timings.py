"""Search timings on this Mac (ticket 70's measurement rules; slice 1 sections 7.4 and 13's calibration
rule), for S1-17.

    sandbox-exec -f tests/loopback-only.sb uv run --no-sync python -I tools/search_timings.py \\
        --helper <app>/Contents/MacOS/llama-server --model <Qwen3-Embedding-0.6B-Q8_0.gguf> \\
        [--corpus qasper --qasper <folder holding qasper-{train,dev,test}-v0.3.json> | --corpus synthetic] \\
        --size 10k|100k [--vectors helper|synthetic] [--sustained SECONDS] [--desktop] [--out FILE]

The workload is named before anything runs (WORKLOAD, printed first and with the results): QASPER
v0.3, AllenAI's public papers (CC BY 4.0), read as untrusted data from the folder given (downloaded
once, outside git, as a separate preparation step under the M2 charter's section 6; this tool never
downloads anything). Each paper becomes one Markdown file (its title, abstract, sections and figure
and table captions) added through the API, so the app's own reading cuts it by section 7.1 (one
paragraph, up to 2,000 characters, indexed with its title and section path). 10k: the papers in
order of their ids until about 10,000 passages; 100k: every paper (about 85,000 passages, all QASPER
has). Queries: QASPER's own questions on those papers (the first 200, in the same order), and 20
Chinese development queries written for this run about the same kind of papers (cross-language:
Chinese queries over English papers).

With --corpus synthetic the papers are generated instead, from S1-16's synthetic vocabulary and
seed (tools/helper_timings.py; SYNTHETIC): English and Chinese papers of four sections of six
paragraphs, to about 10,000 or 100,000 passages, and S1-16's synthetic questions; no real or public data.

The backend runs in this process on a new temporary data folder, with an in-memory credential store
(never the Keychain); the model is imported through the API, verified like any import, and the app
starts its helper (the bundle's, checked against its SHA-256 manifest) and indexes through it, as it
always does. The project is review-locked, so no identifier is looked up and nothing is sent anywhere
(indexing and search are local and keep working there). Run it under the loopback-only sandbox; the
helper's sockets are listed with lsof.

With --vectors synthetic (for 100k, whose embedding through the helper would take about three hours),
every passage's vector is a seeded random unit vector written to the index directly: dense search's
cost does not depend on the values, and the query's embedding still goes through the helper. The
embedding throughput is then the one measured at 10k.

Recorded: the index build (keyword part, embeddings, its file's size, the passage-length
distribution, the audit rows indexing wrote and the main database's growth); warm search latency
end to end through the search API (median and p95), with the keyword and dense parts timed apart; the
first search after a cold helper (stopped, as after its idle stop or a launch); searches while a
rebuild embeds (sustained); the share of searches past the hybrid deadline; peak memory, memory
pressure and swap. Without --desktop, memory covers this process (the backend) and its helper
processes only. With --desktop the whole application runs as the desktop entry runs it (S1-16's
helper_timings.run_desktop: the backend, its window and the interface in it, on a new temporary data
folder), the same workload is driven inside its backend, and memory covers the whole app: its
process, its helper processes and its window's WebKit processes. The sandbox profile confines the
Python process tree only; the results say what stands for the WebKit processes (network_isolation).
The synthetic papers are made as they are added, so the tool holds no corpus in the measured
process; QASPER's files are read whole into it first, which the results say (memory.includes).
"""

import argparse
import asyncio
import base64
import itertools
import json
import os
import random
import sqlite3
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT), str(ROOT / "tools")]
import httpx  # noqa: E402

from backend import local_helper  # noqa: E402
from backend.app import create_app  # noqa: E402
from backend.local_helper import EMBEDDING  # noqa: E402
from backend.search import QUERY_INSTRUCTION  # noqa: E402
from backend.search_index import DIMENSIONS, readings  # noqa: E402
from backend.self_test import _Keys  # noqa: E402
from helper_timings import (EN_SENTENCES, EN_WORDS, SECTIONS_EN, SECTIONS_ZH, TITLES_EN, TITLES_ZH,  # noqa: E402
                            ZH_SENTENCES, ZH_WORDS, Memory, _length, _paragraph, helpers_of, not_loopback, questions,
                            run_desktop, sockets, summary, uncached_copy, vm_counters)

ORIGIN = "http://127.0.0.1:1"
SEED = 20261009
FILES = ("qasper-train-v0.3.json", "qasper-dev-v0.3.json", "qasper-test-v0.3.json")
WORKLOAD = {
    "name": "S1-17 search timings on QASPER v0.3",
    "corpus": "QASPER v0.3 (Dasigi et al., 2021), AllenAI, CC BY 4.0: qasper-train-dev-v0.3.tgz SHA-256 "
              "a28fdf966db827bcee3d873107d6b6669864fb7ca8fbf73a192f5e39191bdb5a and qasper-test-and-evaluator-v0.3.tgz "
              "SHA-256 72a52a41193e2838b8074f80ac074b94f956b84886c36a61c58a7df4171bdd72, from "
              "https://qasper-dataset.s3.us-west-2.amazonaws.com/ (the URLs AllenAI's dataset script names)",
    "cut": "one Markdown file per paper (title, abstract, sections with their paragraphs, figure and table captions), "
           "read by the app into passages by section 7.1: one paragraph, up to 2,000 characters, split at a "
           "sentence; indexed with the title and section path",
    "sizes": {"10k": "papers in order of their ids until about 10,000 passages",
              "100k": "all 1,585 papers (about 85,000 passages: all QASPER has)"},
    "queries": "the first 200 of QASPER's questions on the papers indexed (by paper id, then question order), "
               "English; 20 Chinese development queries written for this run (cross-language: Chinese queries "
               "over English papers)",
    "search": "POST /api/projects/{id}/search, limit 8 (keep), BM25 50, dense 50, RRF k 60, [retrieval] hybrid_ms "
              "as set; 10 warm-up searches first",
    "machine": "the reference Mac: Apple M5, 24 GB, macOS 27.0.1 (ticket 70)",
}
ZH_QUERIES = [
    "这篇论文用什么数据集评估模型？", "机器翻译的质量如何评估？", "模型在低资源语言上的表现如何？", "情感分析使用了哪些特征？",
    "命名实体识别的基线模型是什么？", "注意力机制如何提高问答准确率？", "作者如何处理数据中的噪声标签？",
    "预训练语言模型在本研究中起什么作用？", "实验比较了哪些神经网络结构？", "数据集是如何标注的，标注者一致性如何？",
    "本文提出的方法有哪些局限？", "如何检测社交媒体上的仇恨言论？", "对话系统的评价指标有哪些？",
    "跨语言迁移学习的效果如何？", "文本摘要模型如何避免重复？", "知识图谱如何用于问答？", "模型的训练时间和计算成本是多少？",
    "词向量是如何训练的？", "阅读理解任务中的错误分析发现了什么？", "多任务学习是否提高了性能？"]


SYNTHETIC = {
    "name": "S1-17 search timings on synthetic papers, seed 20261009",
    "corpus": "generated papers, alternately English and Chinese, from S1-16's synthetic vocabulary and sentence "
              "forms (tools/helper_timings.py): a title and four sections of six paragraphs each, every paragraph "
              "up to 2,000 characters with S1-16's length mix; no real or public data",
    "cut": WORKLOAD["cut"],
    "sizes": {"10k": "papers until about 10,000 passages", "100k": "papers until about 100,000 passages"},
    "queries": "100 English and 100 Chinese synthetic questions in S1-16's question forms",
    "search": WORKLOAD["search"],
    "machine": WORKLOAD["machine"],
}


def synthetic_papers(size, rng):
    """Generated papers until about 10,000 or 100,000 passages (SYNTHETIC), one at a time, as they are
    added: (file name, Markdown)."""
    count, made = 0, 0
    while count < (10_000 if size == "10k" else 100_000):
        english = made % 2 == 0
        words, sentences, joiner = (EN_WORDS, EN_SENTENCES, " ") if english else (ZH_WORDS, ZH_SENTENCES, "")
        made += 1
        parts = [f"# {rng.choice(TITLES_EN if english else TITLES_ZH)} {made}"]
        for section in SECTIONS_EN if english else SECTIONS_ZH:
            parts.append(f"## {section.replace(' > ', ': ')}")
            parts += [_paragraph(rng, words, sentences, _length(rng), joiner) for _ in range(6)]
            count += 6
        yield f"synthetic-{made:05d}.md", "\n\n".join(parts).encode()


def corpus(args):
    """(workload, the papers as an iterator of (file name, Markdown) made as they are added, so the tool
    holds no corpus while memory is measured, [(language, query)]) for --corpus. QASPER's files are read
    whole first, into this process (see measure)."""
    if args.corpus == "synthetic":
        asked = random.Random(SEED + 1)
        queries = [("en", q) for q in questions("en", 100, asked)] + [("zh", q) for q in questions("zh", 100, asked)]
        return SYNTHETIC, itertools.islice(synthetic_papers(args.size, random.Random(SEED)), args.papers), queries
    picked = chosen(papers(args.qasper), args.size)
    if args.papers:  # a quick check of the tool itself, not a measurement
        picked = dict(list(picked.items())[:args.papers])
    queries = [("en", q) for q in english_queries(picked)] + [("zh", q) for q in ZH_QUERIES]
    return WORKLOAD, ((f"{pid}.md", markdown(paper)) for pid, paper in picked.items()), queries


def papers(folder):
    """{paper id: paper} from the three QASPER files, read as data."""
    found = {}
    for name in FILES:
        with open(Path(folder) / name, encoding="utf-8") as f:
            found.update(json.load(f))
    return dict(sorted(found.items()))


def markdown(paper):
    """One paper as a Markdown file the app reads: its title, abstract, sections and captions."""
    def clean(text):
        return " ".join(str(text or "").split())
    parts = [f"# {clean(paper.get('title')) or 'Untitled'}", "## Abstract", clean(paper.get("abstract"))]
    for section in paper.get("full_text") or []:
        name = clean(section.get("section_name")).replace("#", "") or "Section"
        paragraphs = [clean(p) for p in section.get("paragraphs") or [] if clean(p)]
        if paragraphs:
            parts += [f"## {name}", *paragraphs]
    captions = [clean(item.get("caption")) for item in paper.get("figures_and_tables") or [] if clean(item.get("caption"))]
    if captions:
        parts += ["## Figures and tables", *captions]
    return "\n\n".join(part for part in parts if part).encode()


def estimate(paper):
    return 2 + sum(len([p for p in s.get("paragraphs") or [] if str(p).strip()]) for s in paper.get("full_text") or []) \
        + len(paper.get("figures_and_tables") or [])


def chosen(all_papers, size):
    if size == "100k":
        return all_papers
    picked, count = {}, 0
    for pid, paper in all_papers.items():
        if count >= 10_000:
            break
        picked[pid] = paper
        count += estimate(paper)
    return picked


def english_queries(picked, count=200):
    found = []
    for paper in picked.values():
        for qa in paper.get("qas") or []:
            question = " ".join(str(qa.get("question") or "").split())
            if question:
                found.append(question)
    return found[:count]


async def timed(awaitable):
    began = time.perf_counter()
    result = await awaitable
    return time.perf_counter() - began, result


def sizes(data):
    def size(path):
        return sum(p.stat().st_size for p in path.parent.glob(path.name + "*") if p.is_file())
    return {"main_database_bytes": size(data / "scholia.sqlite3"), "index_bytes": size(data / "index" / "search.sqlite3")}


async def run(args):
    with tempfile.TemporaryDirectory(prefix="scholia-search-timings-") as folder:
        data = Path(folder)
        app = create_app(data, origin=ORIGIN, keyring_backend=_Keys(), helper=local_helper.Config(binary=Path(args.helper)))
        async with app.app.router.lifespan_context(app.app):
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url=ORIGIN,
                                         headers={"X-Scholia-Client": "local"}, timeout=3600) as client:
                return await measure(args, app.app.state.scholia, client, data,
                                     lambda: [os.getpid(), *helpers_of(os.getpid())])


async def until(check, every=0.5):
    while not await check():
        await asyncio.sleep(every)


async def measure(args, state, client, data, processes):
    db = state["db"]

    async def read(fn):
        return await asyncio.to_thread(db.read, fn)

    workload, to_add, queries = corpus(args)
    results = {"workload": workload, "size": args.size, "vectors": args.vectors,
               "started_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"), "load_average_start": os.getloadavg()}
    memory = Memory(processes)
    vm_before = vm_counters()
    memory.thread.start()

    async def install():
        seconds, response = await timed(client.post("/api/helper/models/import",
                                                    json={"model": EMBEDDING, "path": str(Path(args.model).resolve())}))
        assert response.status_code == 200, response.text
        installed = data / "models" / EMBEDDING / local_helper.EMBEDDING_MODEL["file"]
        uncached_copy(Path(args.model), installed)  # its first start reads it from disk, as after a restart
        return round(seconds, 2)

    if args.vectors == "helper":
        results["import_seconds"] = await install()
    project = (await client.post("/api/projects", json={"name": "QASPER timings"})).json()["id"]
    locked = await client.post(f"/api/projects/{project}/review-lock", json={"locked": True, "venue": "APA"})
    assert locked.status_code == 200, locked.text  # no lookup: nothing leaves the Mac
    before = {**sizes(data), "audit_rows": await read(lambda c: c.execute("SELECT count(*) FROM audit_log").fetchone()[0])}

    # The build: every paper added (made and encoded 20 at a time), read, keyword-indexed, then embedded.
    added = 0
    began = time.perf_counter()
    while batch := list(itertools.islice(to_add, 20)):
        response = await client.post(f"/api/projects/{project}/materials", json={
            "files": [{"name": name, "data": base64.b64encode(text).decode()} for name, text in batch]})
        assert response.status_code == 201, response.text
        added += len(batch)
    del batch
    results["papers"] = added
    index = state["index"]

    async def read_done():
        return not await read(lambda c: c.execute("SELECT 1 FROM runs WHERE workflow = 'extract' AND status = 'running'"
                                                  " LIMIT 1").fetchone())
    await until(read_done)
    read_seconds = time.perf_counter() - began

    async def keyword_done():
        return not await read(lambda c: c.execute("SELECT 1 FROM index_queue LIMIT 1").fetchone())
    await until(keyword_done)
    keyword_seconds = time.perf_counter() - began
    counts = await asyncio.to_thread(index.counts, project)
    passages = sum(c[0] for c in counts.values())
    lengths = await read(lambda c: [n for (n,) in c.execute("SELECT length(text) FROM passages")])
    build = {"papers": added, "passages": passages, "embeddable": sum(c[2] for c in counts.values()),
             "passage_chars": summary(lengths), "passages_over_2000_chars": sum(1 for n in lengths if n > 2000),
             "read_seconds": round(read_seconds, 1), "keyword_indexed_seconds": round(keyword_seconds, 1)}

    async def embedded():
        found = await asyncio.to_thread(index.counts, project)
        running = await read(lambda c: c.execute("SELECT 1 FROM runs WHERE workflow = 'index' AND status = 'running'"
                                                 " LIMIT 1").fetchone())
        return not running and sum(c[1] for c in found.values()) >= sum(c[2] for c in found.values())

    if args.vectors == "helper":
        await until(embedded, every=2)
        build["embedded_seconds"] = round(time.perf_counter() - began, 1)
        build["embedding_seconds"] = round(build["embedded_seconds"] - keyword_seconds, 1)
        build["passages_per_second"] = round(build["embeddable"] / max(build["embedding_seconds"], 0.001), 2)
    else:  # seeded random unit vectors, written to the index directly (see the module's docstring)
        rng = random.Random(SEED)
        materials = list(counts)
        written_at = time.perf_counter()
        while rows := await asyncio.to_thread(index.missing, project, materials, 2000):
            batch = []
            for rowid, pid, mark in rows:
                vector = [rng.gauss(0, 1) for _ in range(DIMENSIONS)]
                norm = sum(v * v for v in vector) ** 0.5
                batch.append((rowid, pid, mark, [v / norm for v in vector]))
            await asyncio.to_thread(index.store, project, batch)
        build["synthetic_vectors_written_seconds"] = round(time.perf_counter() - written_at, 1)
        results["import_seconds"] = await install()
        await asyncio.sleep(1)
        await until(embedded, every=1)
    after = {**sizes(data), "audit_rows": await read(lambda c: c.execute("SELECT count(*) FROM audit_log").fetchone()[0])}
    build["sizes_before"], build["sizes_after"] = before, after
    build["audit_rows_by_kind"] = await read(lambda c: c.execute(
        "SELECT data ->> 'kind', data ->> 'decision', count(*) FROM audit_log WHERE event = 'outbound' GROUP BY 1, 2").fetchall())
    build["index_queue_rows_left"] = await read(lambda c: c.execute("SELECT count(*) FROM index_queue").fetchone()[0])
    results["build"] = build
    helper = state["local_helper"].helpers[EMBEDDING]
    deadline_ms = (await client.get("/api/settings")).json()["values"]["retrieval"]["hybrid_ms"]
    results["hybrid_ms"] = deadline_ms

    async def search(query):
        seconds, response = await timed(client.post(f"/api/projects/{project}/search", json={"query": query}))
        found = response.json()
        assert response.status_code == 200, response.text
        return seconds, found

    # Cold: the first search with the helper stopped (after its idle stop, or the launch), three times.
    cold = []
    for lang, query in (queries[0], queries[1], queries[-1]):
        await helper.stop()
        seconds, found = await search(query)
        next_seconds, next_found = await search(query)
        cold.append({"lang": lang, "seconds": round(seconds, 3), "mode": found["mode"], "reason": found["reason"],
                     "next_seconds": round(next_seconds, 3), "next_mode": next_found["mode"]})
        while helper.state != "running":  # its start goes on past the deadline: the next search is warm
            await asyncio.sleep(0.2)
    results["cold"] = cold
    results["helper_ready_seconds_last_start"] = helper.ready_seconds

    # Warm: 10 warm-up searches, then every query once through the API; then its parts apart.
    for _, query in queries[:10]:
        await search(query)
    by_lang = {"en": [], "zh": []}
    reasons = {}
    for lang, query in queries:
        seconds, found = await search(query)
        by_lang[lang].append(seconds)
        reasons[found["reason"] or found["mode"]] = reasons.get(found["reason"] or found["mode"], 0) + 1
    results["warm_search_seconds"] = {lang: summary(values) for lang, values in by_lang.items()}
    results["warm_search_seconds_all"] = summary(by_lang["en"] + by_lang["zh"])
    results["warm_outcomes"] = reasons
    results["warm_past_deadline"] = reasons.get("deadline", 0)
    parts = {"keyword": [], "query_embedding": [], "dense_scan": [], "access_check_and_fetch": []}
    for _, query in queries:
        seconds, found = await timed(asyncio.to_thread(index.keyword, project, query, 50))
        parts["keyword"].append(seconds)
        seconds, [vector] = await timed(local_helper.embed(state, [QUERY_INSTRUCTION + query], project_id=project, query=True))
        parts["query_embedding"].append(seconds)
        seconds, dense = await timed(asyncio.to_thread(index.dense, project, vector, 50))
        parts["dense_scan"].append(seconds)
        ids = {*found, *dense}
        seconds, _ = await timed(read(lambda c: readings(c, project, ids, references=False)))
        parts["access_check_and_fetch"].append(seconds)
    results["warm_part_seconds"] = {name: summary(values) for name, values in parts.items()}
    results["footprint_after_warm"] = memory.peak()

    # Sustained: searches back to back while a rebuild of the project embeds (helper vectors only).
    if args.vectors == "helper" and args.sustained:
        response = await client.post(f"/api/projects/{project}/index/rebuild")
        rebuild = response.json()["run_id"]
        await asyncio.sleep(5)  # its keyword part done, its embeddings under way
        sustained, outcomes, i = [], {}, 0
        stop = time.monotonic() + args.sustained
        while time.monotonic() < stop:
            seconds, found = await search(queries[i % len(queries)][1])
            sustained.append(seconds)
            outcomes[found["reason"] or found["mode"]] = outcomes.get(found["reason"] or found["mode"], 0) + 1
            i += 1
            await asyncio.sleep(0.25)
        progress = state["harness"].registry.runs.get(rebuild)
        await client.post(f"/api/runs/{rebuild}/cancel")
        results["sustained"] = {"seconds": args.sustained, "searches": summary(sustained), "outcomes": outcomes,
                                "past_deadline": outcomes.get("deadline", 0),
                                "rebuild_progress_at_end": progress.progress if progress else None}
    memory.stop.set()
    memory.thread.join()
    results["memory"] = memory.peak()
    # The backend's and its helpers' figures only; with --desktop, run_desktop says it covers the whole app.
    results["memory"]["covers"] = "this backend process and its helper processes, from their launch"
    if args.corpus == "qasper":  # held in the process measured, with the backend: not separated
        results["memory"]["includes"] = "the QASPER files this tool read whole into the same Python process"
    results["vm_before"], results["vm_after"] = vm_before, vm_counters()
    results["helper_sockets"] = sockets(helper._process.pid) if helper._process else None
    results["not_loopback_sockets"] = not_loopback(processes())
    results["load_average_end"] = os.getloadavg()
    results["sqlite"] = {"main": sqlite3.sqlite_version}
    return results


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--helper", required=True)
    parser.add_argument("--model", required=True)
    parser.add_argument("--corpus", choices=("qasper", "synthetic"), default="qasper")
    parser.add_argument("--qasper", help="the folder holding QASPER's files (--corpus qasper)")
    parser.add_argument("--size", choices=("10k", "100k"), default="10k")
    parser.add_argument("--vectors", choices=("helper", "synthetic"), default="helper")
    parser.add_argument("--sustained", type=int, default=60)
    parser.add_argument("--papers", type=int, help="only this many papers: a quick check of the tool, not a measurement")
    parser.add_argument("--desktop", action="store_true", help="run the workload inside the desktop app")
    parser.add_argument("--out", type=Path)
    args = parser.parse_args(argv)
    if args.corpus == "qasper" and not args.qasper:
        parser.error("--corpus qasper needs --qasper")
    print(json.dumps({"workload": SYNTHETIC if args.corpus == "synthetic" else WORKLOAD}, ensure_ascii=False, indent=2),
          flush=True)
    results = run_desktop(args, measure) if args.desktop else asyncio.run(run(args))
    text = json.dumps(results, ensure_ascii=False, indent=2)
    print(text)
    if args.out:
        args.out.write_text(text + "\n")


if __name__ == "__main__":
    main()
