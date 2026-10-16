<!-- The committed tier 1 baseline (S1-18): `uv run --no-sync python -I evals/tier1.py report evals/baseline/tier1-baseline.json`, below. It is one full run of all five benchmarks on the reference Mac, at commit 7f2d37e: each benchmark document's file carries its title as a paper's Markdown does, so documents with different titles are different papers (PR 36's connector finding); the run before it, at dbed9f1, is in git history. The Mac was shared: other writers' work ran during it (load average 2.0 to 7.8 in between), so build times and rates are this run's, not the Mac's best. Compare a later run with `evals/tier1.py compare evals/baseline/tier1-baseline.json <run>`. -->

# Tier 1 results (full, finished)

Commit `7f2d37ee41b85a7580f4d19fd38e49e2a93ae366`; 2026-10-17T02:57:45+0800 to 2026-10-17T07:28:31+0800; 16246.3 s; machine {'chip': 'Apple M5', 'model': 'Mac17,4', 'memory_gb': 24, 'macos': '27.0.1', 'python': '3.13.14'}; load average [2.51904296875, 2.859375, 2.9580078125] at the start and [2.56396484375, 2.65234375, 3.10302734375] at the end; model settings like the manifest's: True. Queries and documents are English (query language en, document language en). Each benchmark is reported on its own; there is no combined score.

## litsearch

597 queries run; excluded {'query_empty_or_too_long': 0}. Build 9027.6 s, 115164 passages (0 refused by the helper; characters median 199.5, p95 1432, max 2000; 0 over 2,000), index 618.4 MiB, readings not succeeded 0.

| Arm | Metric | Mean | 95% interval | n | Minus hybrid (95% interval) |
|---|---|---|---|---|---|
| keyword_only | recall@5 (specific) | 0.504 | [0.459, 0.551] | 442 | -0.140 [-0.176, -0.105] |
| keyword_only | recall@20 (broad) | 0.427 | [0.352, 0.504] | 155 | -0.177 [-0.247, -0.113] |
| keyword_only | recall@20 (specific) | 0.652 | [0.608, 0.696] | 442 | -0.119 [-0.153, -0.086] |
| keyword_only | recall@5 | 0.449 | [0.410, 0.488] | 597 | -0.142 [-0.172, -0.112] |
| keyword_only | recall@20 | 0.593 | [0.554, 0.632] | 597 | -0.134 [-0.164, -0.105] |
| hybrid | recall@5 (specific) | 0.645 | [0.601, 0.688] | 442 |  |
| hybrid | recall@20 (broad) | 0.605 | [0.529, 0.680] | 155 |  |
| hybrid | recall@20 (specific) | 0.770 | [0.731, 0.809] | 442 |  |
| hybrid | recall@5 | 0.590 | [0.551, 0.629] | 597 |  |
| hybrid | recall@20 | 0.727 | [0.693, 0.762] | 597 |  |

Keyword-only fallbacks of the hybrid arm: none; search seconds (median, p95): keyword_only 0.124, 0.2598; hybrid 0.2202, 0.2516

- documents: 64183
- documents empty: 6197
- documents sharing a file: 330
- relevant documents not indexed: 5
- queries with fewer documents than largest cutoff: {'keyword_only': 0, 'hybrid': 0}

## scidocs

1000 queries run; excluded {'query_empty_or_too_long': 0}. Build 3973.3 s, 52655 passages (0 refused by the helper; characters median 307, p95 1743, max 2000; 0 over 2,000), index 296.1 MiB, readings not succeeded 0.

| Arm | Metric | Mean | 95% interval | n | Minus hybrid (95% interval) |
|---|---|---|---|---|---|
| keyword_only | ndcg@10 | 0.155 | [0.142, 0.168] | 1000 | -0.044 [-0.050, -0.038] |
| keyword_only | recall@10 | 0.162 | [0.149, 0.175] | 1000 | -0.044 [-0.052, -0.037] |
| keyword_only | mrr@10 | 0.271 | [0.249, 0.295] | 1000 | -0.070 [-0.084, -0.056] |
| hybrid | ndcg@10 | 0.199 | [0.185, 0.213] | 1000 |  |
| hybrid | recall@10 | 0.206 | [0.192, 0.221] | 1000 |  |
| hybrid | mrr@10 | 0.341 | [0.317, 0.366] | 1000 |  |

Keyword-only fallbacks of the hybrid arm: none; search seconds (median, p95): keyword_only 0.0283, 0.1659; hybrid 0.125, 0.1535

- documents: 25657
- documents empty: 0
- documents sharing a file: 1
- relevant documents not indexed: 0
- queries with fewer documents than largest cutoff: {'keyword_only': 0, 'hybrid': 0}

## qasper

1308 queries run; excluded {'figure_or_table_only': 42, 'all_unanswerable': 79, 'no_evidence': 20, 'evidence_matching_no_passage': 2}. Build 1124.3 s, 23580 passages (0 refused by the helper; characters median 344.0, p95 1098, max 1994; 0 over 2,000), index 1706.1 MiB, readings not succeeded 0.

| Arm | Metric | Mean | 95% interval | n | Minus hybrid (95% interval) |
|---|---|---|---|---|---|
| keyword_only | recall@8 | 0.609 | [0.583, 0.634] | 1308 | -0.106 [-0.125, -0.086] |
| keyword_only | ndcg@10 | 0.421 | [0.402, 0.441] | 1308 | -0.115 [-0.127, -0.102] |
| keyword_only | mrr@10 | 0.375 | [0.353, 0.397] | 1308 | -0.119 [-0.134, -0.103] |
| hybrid | recall@8 | 0.715 | [0.691, 0.737] | 1308 |  |
| hybrid | ndcg@10 | 0.536 | [0.516, 0.556] | 1308 |  |
| hybrid | mrr@10 | 0.494 | [0.471, 0.517] | 1308 |  |

Keyword-only fallbacks of the hybrid arm: none; search seconds (median, p95): keyword_only 0.0215, 0.0342; hybrid 0.0405, 0.0552

- evidence items mapped: 5075
- evidence items unmatched: {'heading': 190, 'other': 20}
- figure or table items dropped: 218

## scifact

300 queries run; excluded {'query_empty_or_too_long': 0}. Build 954.4 s, 10920 passages (0 refused by the helper; characters median 373.5, p95 1896, max 1999; 0 over 2,000), index 71.1 MiB, readings not succeeded 0.

| Arm | Metric | Mean | 95% interval | n | Minus hybrid (95% interval) |
|---|---|---|---|---|---|
| keyword_only | ndcg@10 | 0.657 | [0.610, 0.702] | 300 | -0.055 [-0.079, -0.032] |
| keyword_only | recall@10 | 0.784 | [0.737, 0.828] | 300 | -0.058 [-0.096, -0.022] |
| keyword_only | mrr@10 | 0.623 | [0.573, 0.670] | 300 | -0.054 [-0.081, -0.028] |
| hybrid | ndcg@10 | 0.712 | [0.669, 0.754] | 300 |  |
| hybrid | recall@10 | 0.842 | [0.800, 0.881] | 300 |  |
| hybrid | mrr@10 | 0.677 | [0.629, 0.723] | 300 |  |

Keyword-only fallbacks of the hybrid arm: none; search seconds (median, p95): keyword_only 0.0087, 0.0146; hybrid 0.0495, 0.056

- documents: 5183
- documents empty: 0
- documents sharing a file: 0
- relevant documents not indexed: 0
- queries with fewer documents than largest cutoff: {'keyword_only': 0, 'hybrid': 0}

## nfcorpus

323 queries run; excluded {'query_empty_or_too_long': 0}. Build 630.1 s, 7593 passages (0 refused by the helper; characters median 291, p95 1902, max 1999; 0 over 2,000), index 53.9 MiB, readings not succeeded 0.

| Arm | Metric | Mean | 95% interval | n | Minus hybrid (95% interval) |
|---|---|---|---|---|---|
| keyword_only | ndcg@10 | 0.301 | [0.268, 0.335] | 323 | -0.052 [-0.065, -0.040] |
| keyword_only | recall@10 | 0.148 | [0.123, 0.174] | 323 | -0.026 [-0.037, -0.016] |
| keyword_only | mrr@10 | 0.501 | [0.453, 0.550] | 323 | -0.073 [-0.099, -0.049] |
| hybrid | ndcg@10 | 0.353 | [0.320, 0.389] | 323 |  |
| hybrid | recall@10 | 0.174 | [0.147, 0.202] | 323 |  |
| hybrid | mrr@10 | 0.575 | [0.525, 0.623] | 323 |  |

Keyword-only fallbacks of the hybrid arm: none; search seconds (median, p95): keyword_only 0.0027, 0.005; hybrid 0.0387, 0.063

- documents: 3633
- documents empty: 0
- documents sharing a file: 40
- relevant documents not indexed: 0
- queries with fewer documents than largest cutoff: {'keyword_only': 79, 'hybrid': 0}
