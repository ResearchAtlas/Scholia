<!-- The committed tier 1 baseline (S1-18): `uv run --no-sync python -I evals/tier1.py report evals/baseline/tier1-baseline.json`, below. It is one full run of all five benchmarks on the reference Mac, at commit dbed9f1 (main 6cfcd23 merged: the access-check fix, PR 30, and the embedding-input bound, PR 31). The Mac was shared: other writers' test suites and walkthroughs ran during it (load average 2.0 to 9.0 in between), so build times and rates are this run's, not the Mac's best. Compare a later run with `evals/tier1.py compare evals/baseline/tier1-baseline.json <run>`. -->

# Tier 1 results (full, finished)

Commit `dbed9f1efadd6dac93d1993e0242737da48e1292`; 2026-10-15T20:35:22+0800 to 2026-10-16T00:59:47+0800; 15865.0 s; machine {'chip': 'Apple M5', 'model': 'Mac17,4', 'memory_gb': 24, 'macos': '27.0.1', 'python': '3.13.14'}; load average [2.43310546875, 2.47607421875, 2.8291015625] at the start and [3.005859375, 2.400390625, 2.4140625] at the end; model settings like the manifest's: True. Queries and documents are English (query language en, document language en). Each benchmark is reported on its own; there is no combined score.

## litsearch

597 queries run; excluded {'query_empty_or_too_long': 0}. Build 7392.8 s, 59782 passages (0 refused by the helper; characters median 868.0, p95 1708, max 2000; 0 over 2,000), index 364.4 MiB, readings not succeeded 0.

| Arm | Metric | Mean | 95% interval | n | Minus hybrid (95% interval) |
|---|---|---|---|---|---|
| keyword_only | recall@5 (specific) | 0.510 | [0.463, 0.557] | 442 | -0.117 [-0.153, -0.081] |
| keyword_only | recall@20 (broad) | 0.424 | [0.348, 0.500] | 155 | -0.163 [-0.227, -0.102] |
| keyword_only | recall@20 (specific) | 0.650 | [0.606, 0.695] | 442 | -0.132 [-0.169, -0.096] |
| keyword_only | recall@5 | 0.456 | [0.417, 0.495] | 597 | -0.120 [-0.150, -0.090] |
| keyword_only | recall@20 | 0.592 | [0.552, 0.631] | 597 | -0.140 [-0.172, -0.109] |
| hybrid | recall@5 (specific) | 0.627 | [0.583, 0.672] | 442 |  |
| hybrid | recall@20 (broad) | 0.588 | [0.512, 0.663] | 155 |  |
| hybrid | recall@20 (specific) | 0.783 | [0.744, 0.819] | 442 |  |
| hybrid | recall@5 | 0.576 | [0.537, 0.616] | 597 |  |
| hybrid | recall@20 | 0.732 | [0.697, 0.767] | 597 |  |

Keyword-only fallbacks of the hybrid arm: none; search seconds (median, p95): keyword_only 0.1019, 0.1732; hybrid 0.1646, 0.2088

- documents: 64183
- documents empty: 6197
- documents sharing a file: 345
- relevant documents not indexed: 5
- queries with fewer documents than largest cutoff: {'keyword_only': 0, 'hybrid': 0}

## scidocs

1000 queries run; excluded {'query_empty_or_too_long': 0}. Build 4125.4 s, 27295 passages (0 refused by the helper; characters median 1021, p95 1896, max 2000; 0 over 2,000), index 178.5 MiB, readings not succeeded 0.

| Arm | Metric | Mean | 95% interval | n | Minus hybrid (95% interval) |
|---|---|---|---|---|---|
| keyword_only | ndcg@10 | 0.154 | [0.141, 0.167] | 1000 | -0.044 [-0.050, -0.039] |
| keyword_only | recall@10 | 0.161 | [0.148, 0.174] | 1000 | -0.046 [-0.053, -0.038] |
| keyword_only | mrr@10 | 0.273 | [0.250, 0.296] | 1000 | -0.067 [-0.080, -0.054] |
| hybrid | ndcg@10 | 0.198 | [0.185, 0.213] | 1000 |  |
| hybrid | recall@10 | 0.206 | [0.193, 0.221] | 1000 |  |
| hybrid | mrr@10 | 0.340 | [0.316, 0.365] | 1000 |  |

Keyword-only fallbacks of the hybrid arm: none; search seconds (median, p95): keyword_only 0.0246, 0.037; hybrid 0.1102, 0.1531

- documents: 25657
- documents empty: 0
- documents sharing a file: 49
- relevant documents not indexed: 0
- queries with fewer documents than largest cutoff: {'keyword_only': 0, 'hybrid': 0}

## qasper

1308 queries run; excluded {'figure_or_table_only': 42, 'all_unanswerable': 79, 'no_evidence': 20, 'evidence_matching_no_passage': 2}. Build 1831.6 s, 23580 passages (0 refused by the helper; characters median 344.0, p95 1098, max 1994; 0 over 2,000), index 1706.1 MiB, readings not succeeded 0.

| Arm | Metric | Mean | 95% interval | n | Minus hybrid (95% interval) |
|---|---|---|---|---|---|
| keyword_only | recall@8 | 0.609 | [0.583, 0.634] | 1308 | -0.106 [-0.125, -0.086] |
| keyword_only | ndcg@10 | 0.421 | [0.402, 0.441] | 1308 | -0.115 [-0.127, -0.102] |
| keyword_only | mrr@10 | 0.375 | [0.353, 0.397] | 1308 | -0.119 [-0.134, -0.103] |
| hybrid | recall@8 | 0.715 | [0.691, 0.737] | 1308 |  |
| hybrid | ndcg@10 | 0.536 | [0.516, 0.556] | 1308 |  |
| hybrid | mrr@10 | 0.494 | [0.471, 0.517] | 1308 |  |

Keyword-only fallbacks of the hybrid arm: none; search seconds (median, p95): keyword_only 0.0232, 0.0364; hybrid 0.0732, 0.1054

- evidence items mapped: 5075
- evidence items unmatched: {'heading': 190, 'other': 20}
- figure or table items dropped: 218

## scifact

300 queries run; excluded {'query_empty_or_too_long': 0}. Build 1155.3 s, 5737 passages (0 refused by the helper; characters median 1268, p95 1946, max 1999; 0 over 2,000), index 45.5 MiB, readings not succeeded 0.

| Arm | Metric | Mean | 95% interval | n | Minus hybrid (95% interval) |
|---|---|---|---|---|---|
| keyword_only | ndcg@10 | 0.666 | [0.620, 0.710] | 300 | -0.056 [-0.079, -0.034] |
| keyword_only | recall@10 | 0.791 | [0.744, 0.833] | 300 | -0.056 [-0.088, -0.026] |
| keyword_only | mrr@10 | 0.633 | [0.584, 0.680] | 300 | -0.056 [-0.082, -0.031] |
| hybrid | ndcg@10 | 0.722 | [0.679, 0.764] | 300 |  |
| hybrid | recall@10 | 0.847 | [0.805, 0.886] | 300 |  |
| hybrid | mrr@10 | 0.689 | [0.641, 0.735] | 300 |  |

Keyword-only fallbacks of the hybrid arm: none; search seconds (median, p95): keyword_only 0.0056, 0.0099; hybrid 0.0826, 0.1276

- documents: 5183
- documents empty: 0
- documents sharing a file: 0
- relevant documents not indexed: 0
- queries with fewer documents than largest cutoff: {'keyword_only': 0, 'hybrid': 0}

## nfcorpus

323 queries run; excluded {'query_empty_or_too_long': 0}. Build 862.1 s, 4000 passages (0 refused by the helper; characters median 1451.0, p95 1947, max 1999; 0 over 2,000), index 32.9 MiB, readings not succeeded 0.

| Arm | Metric | Mean | 95% interval | n | Minus hybrid (95% interval) |
|---|---|---|---|---|---|
| keyword_only | ndcg@10 | 0.309 | [0.276, 0.343] | 323 | -0.048 [-0.062, -0.036] |
| keyword_only | recall@10 | 0.152 | [0.128, 0.178] | 323 | -0.019 [-0.028, -0.011] |
| keyword_only | mrr@10 | 0.518 | [0.469, 0.567] | 323 | -0.053 [-0.077, -0.029] |
| hybrid | ndcg@10 | 0.357 | [0.323, 0.394] | 323 |  |
| hybrid | recall@10 | 0.171 | [0.146, 0.198] | 323 |  |
| hybrid | mrr@10 | 0.571 | [0.522, 0.620] | 323 |  |

Keyword-only fallbacks of the hybrid arm: none; search seconds (median, p95): keyword_only 0.0024, 0.0034; hybrid 0.0425, 0.0933

- documents: 3633
- documents empty: 0
- documents sharing a file: 40
- relevant documents not indexed: 0
- queries with fewer documents than largest cutoff: {'keyword_only': 79, 'hybrid': 0}

