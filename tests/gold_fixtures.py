"""Synthetic gold sets for tests and the walkthrough (S1-36): a field's folder in evals/gold.py's formats,
with a paper list, questions, citation pairs and the passage worksheet the review page reads, all
made up here (no real paper, no PDF). Field 2's papers carry made-up arXiv ids (2601.0000xv1) and the
attribution and license fields the format requires; field 1's are local, one of them in Chinese.

    uv run --no-sync python tests/gold_fixtures.py FOLDER [--field 1|2] [--sample]
"""

import argparse
import hashlib
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT)]

from evals import gold  # noqa: E402

LONG = " ".join(f"Sentence {i} of a long synthetic passage about retrieval depth and evidence recall." for i in range(23))
CANARY = "CANARY-PASSAGE-7f3a"  # a passage text that must never reach a log
HOSTILE = '<script>window.hostile = 1</script><img src="https://example.invalid/x.png"> is how markup looks in text.'

# (id, title, document language, passages as (page, kind, section, text))
PAPERS = {
    2: [("2601.00001v1", "A Synthetic Study of Dense Retrieval", "en", [
            (1, "title", [], "A Synthetic Study of Dense Retrieval"),
            (1, "abstract", [], "We compare dense and keyword retrieval on a made-up corpus of synthetic papers."),
            (1, "paragraph", ["1 Introduction"], "Dense retrieval found the evidence passage for 71 of 100 synthetic questions."),
            (2, "paragraph", ["2 Results"], LONG),
            (2, "table", ["2 Results"], "Table 1. Recall at eight: dense 0.71, keyword 0.64."),
            (2, "paragraph", ["2 Results"], f"{CANARY} Keyword retrieval missed paraphrased questions most often."),
            (3, "reference", ["References"], "Doe, A. (2020). An earlier synthetic paper."),
        ]),
        ("2601.00002v2", "Synthetic Reranking Notes", "en", [
            (1, "paragraph", ["1 Method"], "A reranker reordered the top fifty passages for each synthetic query."),
            (1, "paragraph", ["2 Findings"], "Reranking raised recall at eight by four points in the synthetic runs,"),
            (1, "paragraph", ["2 Findings"], "and the gain was largest for questions written in another language."),
            (2, "caption", ["2 Findings"], "Figure 2. Gains by query language in the synthetic runs."),
            (2, "paragraph", ["3 Notes"], HOSTILE),
        ])],
    1: [("p-wages", "A Synthetic Study of Minimum Wages", "en", [
            (1, "paragraph", ["1 Introduction"], "Minimum wages raise the earnings of low-paid workers in the synthetic panel."),
            (1, "paragraph", ["2 Results"], "Employment effects are small in most specifications of the synthetic panel."),
            (2, "paragraph", ["2 Results"], f"{CANARY} Regional prices adjust slowly after a wage floor is raised."),
            (3, "reference", ["References"], "Smith, J. (2020). An earlier synthetic paper."),
        ]),
        ("p-zh", "最低工资的合成研究", "zh-CN", [
            (1, "paragraph", ["研究发现"], "最低工资提高了合成样本中低收入工人的收入。"),
            (1, "paragraph", ["研究发现"], "就业效应在大多数设定中很小。"),
            (2, "paragraph", ["讨论"], HOSTILE),
        ])],
}


def paper_record(field, pid, title, language):
    data = f"synthetic PDF of {pid}".encode()
    record = {"id": pid, "title": title, "file": f"{pid}.pdf", "size": len(data),
              "sha256": hashlib.sha256(data).hexdigest(), "document_language": language}
    if field == 2:
        record.update(authors=["A. Synthetic", "B. Example"], primary_category="cs.CL", submitted="2026-02-03", topic="retrieval",
                      abstract_url=f"https://arxiv.org/abs/{pid}", pdf_url=f"https://arxiv.org/pdf/{pid}",
                      license=gold.LICENSES[0], license_page=f"https://arxiv.org/abs/{pid}", license_checked="2026-10-17")
    return record


def papers_doc(field):
    doc = {"field": field, "extractor": {"name": "synthetic", "version": "1"},
           "papers": [paper_record(field, pid, title, lang) for pid, title, lang, _ in PAPERS[field]]}
    if field == 2:
        doc.update(criteria={"note": "synthetic"}, seed=1)
    return doc


def locator(field, papers, pid, ordinal):
    paper = next(p for p in PAPERS[field] if p[0] == pid)
    page, kind, section, text = paper[3][ordinal]
    loc = {"paper": pid, "ordinal": ordinal, "page": page, "kind": kind, "section_path": section, "quote": text}
    if field == 2:
        record = next(p for p in papers["papers"] if p["id"] == pid)
        loc.update(attribution=gold.attribution(record), license=record["license"])
    return loc


def sets(field):
    papers = papers_doc(field)
    a, b = (p[0] for p in PAPERS[field])
    doc_lang = {pid: lang for pid, _, lang, _ in PAPERS[field]}

    def loc(pid, ordinal):
        return locator(field, papers, pid, ordinal)
    if field == 2:
        questions = [
            {"id": "q01", "text": "How many synthetic questions did dense retrieval find evidence for?",
             "query_language": "en", "document_language": "en", "answerable": True, "short_answer": "71 of 100",
             "evidence": [[loc(a, 2)]]},
            {"id": "q02", "text": "Which retrieval method had the higher recall at eight, and did reranking help?",
             "query_language": "en", "document_language": "en", "answerable": True,
             "short_answer": "Dense retrieval; reranking added four points", "evidence": [[loc(a, 4)], [loc(b, 1), loc(b, 2)]]},
            {"id": "q03", "text": "What hardware did the synthetic runs use?", "query_language": "en",
             "document_language": "en", "answerable": False, "evidence": []},
            {"id": "q04", "text": "长段落讨论了什么？", "query_language": "zh-CN", "document_language": "en",
             "answerable": True, "short_answer": "检索深度与证据召回", "evidence": [[loc(a, 3)]]},
            {"id": "q05", "text": "这些合成实验的经费来自哪里？", "query_language": "zh-CN", "document_language": "en",
             "answerable": False, "evidence": []},
            {"id": "q06", "text": "关键词检索最常漏掉哪类问题？", "query_language": "zh-CN", "document_language": "en",
             "answerable": True, "short_answer": "改写过的问题", "evidence": [[loc(a, 5)]]},
        ]
        pairs = [
            {"id": "p01", "sentence": "Dense retrieval located evidence for most of the synthetic questions.",
             "sentence_language": "en", "passage": loc(a, 2), "drafted_category": "supported"},
            {"id": "p02", "sentence": "Dense retrieval located evidence for every synthetic question.",
             "sentence_language": "en", "passage": loc(a, 2), "drafted_category": "unsupported"},
            {"id": "p03", "sentence": "重排序提高了召回率，且对其他语言的问题提升最大。", "sentence_language": "zh-CN",
             "passage": loc(b, 1), "drafted_category": "partly"},
            {"id": "p04", "sentence": "重排序器对每个查询的前五十个段落重新排序。", "sentence_language": "zh-CN",
             "passage": loc(b, 0), "drafted_category": "supported"},
            {"id": "p05", "sentence": "Keyword retrieval beat dense retrieval at eight.", "sentence_language": "en",
             "passage": loc(a, 4), "drafted_category": "unsupported"},
            {"id": "p06", "sentence": "关键词检索在改写问题上最常失误。", "sentence_language": "zh-CN",
             "passage": loc(a, 5), "drafted_category": "partly"},
        ]
    else:
        questions = [
            {"id": "q01", "text": "What do minimum wages do to the earnings of low-paid workers?", "query_language": "en",
             "document_language": doc_lang[a], "answerable": True, "short_answer": "They raise them",
             "evidence": [[loc(a, 0)]]},
            {"id": "q02", "text": "最低工资对就业有什么影响？", "query_language": "zh-CN", "document_language": doc_lang[b],
             "answerable": True, "short_answer": "影响很小", "evidence": [[loc(b, 1)]]},
            {"id": "q03", "text": "这项研究在哪些国家进行？", "query_language": "zh-CN", "document_language": doc_lang[b],
             "answerable": False, "evidence": []},
        ]
        pairs = [
            {"id": "p01", "sentence": "Wage floors lift the pay of low earners in the synthetic panel.",
             "sentence_language": "en", "passage": loc(a, 0), "drafted_category": "supported"},
            {"id": "p02", "sentence": "最低工资大幅减少了就业。", "sentence_language": "zh-CN", "passage": loc(b, 1),
             "drafted_category": "unsupported"},
            {"id": "p03", "sentence": "Prices adjust at once after a wage floor rises.", "sentence_language": "en",
             "passage": loc(a, 2), "drafted_category": "unsupported"},
        ]
    return (papers, {"id": f"field{field}-questions", "kind": "questions", "field": field, "provisional": True,
                     "items": questions},
            {"id": f"field{field}-pairs", "kind": "pairs", "field": field, "provisional": True, "items": pairs})


def worksheet_doc(field, papers, papers_sha256):
    read = {pid: {"state": "ready", "reason": None, "extractor": ["synthetic", "1"],
                  "passages": [{"id": f"{pid}:{i}", "ordinal": i, "page": page, "kind": kind, "section_path": section,
                                "text": text} for i, (page, kind, section, text) in enumerate(passages)]}
            for pid, _, _, passages in PAPERS[field]}
    return gold.worksheet(field, papers, papers_sha256, read)


def write_field(folder, field=2, sample=False):
    """A field's folder with its paper list, both sets and the worksheet; with sample, both samples."""
    folder = Path(folder)
    folder.mkdir(parents=True, exist_ok=True)
    papers, questions, pairs = sets(field)
    for name, doc in (("papers", papers), ("questions", questions), ("pairs", pairs)):
        (folder / f"{name}.json").write_text(json.dumps(doc, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
    root = folder.resolve()
    gold.write_json(root, gold.outputs(root) / "worksheet.json",
                    worksheet_doc(field, papers, gold.sha256_of(root, root / "papers.json")))
    if sample:
        settings = gold.load_settings()
        for doc in (questions, pairs):
            gold.write_json(root, root / f"sample-{doc['kind']}.json", gold.draw(doc, settings))
    return folder


def git(command, repo):
    """A real git repository made, staged or its status read (sol's reproductions use one): the init's path,
    else git's porcelain output. Git is allowed past the tests' network block for this; it makes no connection."""
    import shutil
    import subprocess
    from network_guard import allow_subprocess
    program = shutil.which("git")
    argv = {"init": ["-c", "init.defaultBranch=main", "init", "-q", str(repo)],
            "add": ["-C", str(repo), "add", "-A"],
            "status": ["-C", str(repo), "status", "--porcelain", "--untracked-files=all"]}[command]
    with allow_subprocess(program):
        out = subprocess.run([program, *argv], capture_output=True, text=True, check=True).stdout
    return repo if command == "init" else out


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Write a synthetic gold-set folder.")
    parser.add_argument("folder")
    parser.add_argument("--field", type=int, choices=(1, 2), default=1)
    parser.add_argument("--sample", action="store_true")
    args = parser.parse_args()
    print(write_field(args.folder, args.field, args.sample))
