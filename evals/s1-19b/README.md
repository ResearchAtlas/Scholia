# S1-19b measurements (reference Mac, 2026-10-21)

Measurement files only, from `m2-reviews/s119b-runs/run-all.sh`, each run under the loopback-only sandbox with the pinned embedding model and the pinned Qwen3-Reranker-0.6B Q8_0. Each file records its own commit where the tool records one.

| File | What | Commit |
|---|---|---|
| `full.json`, `full.md` | Tier 1 full, arms keyword_only, rerank (500 ms) and hybrid; `full.md` is `tier1.py report full.json` | 6709ac2 |
| `full-compare.txt` | `tier1.py compare evals/baseline/tier1-baseline.json full.json` | |
| `subset.json`, `subset-compare.txt` | Tier 1 subset (QASPER), no reranker, against `baseline/tier1-subset.json` | f521a55 |
| `subset-rerank-unbounded.json` | Tier 1 subset with `--rerank-ms 30000`: the reranker's quality apart from its 500 ms budget | 6709ac2 |
| `dev-cases.json` | Chinese and cross-language development cases (synthetic and translated; they do not establish the Chinese result, ticket 72 E3) | d16945e |
| `timings-10k.json`, `timings-100k.json` | `tools/search_timings.py` with evidence preparation, with and without reranking | not recorded by the tool (the worktree was between d16945e and 6709ac2) |

## Results

- Hybrid and keyword-only against the committed baseline: no difference on any benchmark, arm or metric (`full-compare.txt`: every paired difference +0.0000, interval [0, 0]); no keyword-only fallback in the hybrid arm.
- The rerank arm at its 500 ms budget fell back on every query: `rerank_deadline` 3528 of 3528 (QASPER 1308, SciFact 300, NFCorpus 323, SCIDOCS 1000, LitSearch 597); the hybrid `deadline` fallback count is 0 in both the hybrid and the rerank arm. Its scores are therefore the hybrid arm's, and its "without fallbacks" metrics have n = 0. Its search seconds were median 1.35 to 1.72 s and p95 1.74 to 2.14 s per benchmark, against the hybrid arm's 0.05 to 0.23 s and 0.08 to 0.31 s.
- With the deadline lifted (`subset-rerank-unbounded.json`, QASPER subset, 328 queries), reranking minus hybrid: recall@8 +0.037 [-0.001, +0.075], ndcg@10 +0.041 [+0.011, +0.071], mrr@10 +0.070 [+0.029, +0.109]; search median 2.37 s, p95 2.97 s.
- Evidence preparation (`timings-*.json`, target 2 s): without reranking median 0.058 s (10k) and 0.172 s (100k), none over target; with reranking median 1.36 s and 1.42 s, p95 1.71 s and 1.78 s, max 2.26 s and 18.19 s.
- Development cases at the 500 ms budget: the rerank arm fell back on every case (`rerank_deadline` 30 of 30 in each set), so its scores equal hybrid's. Cross-language (30 cases): hybrid recall@8 0.733 with English queries and 0.600 with Chinese queries; keyword-only 0.706 and 0.000. Chinese documents (30 cases): hybrid recall@8 1.000, keyword-only 0.967.
