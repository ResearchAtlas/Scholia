# Tier 1 results (full, finished)

Commit `6709ac27765dcefddb52a1b63a2e2856a9d8d0b5`; 2026-10-21T13:42:20+0800 to 2026-10-21T20:32:21+0800; 24601.5 s; machine {'chip': 'Apple M5', 'model': 'Mac17,4', 'memory_gb': 24, 'macos': '27.0.1', 'python': '3.13.14'}; load average [3.01416015625, 3.33544921875, 3.712890625] at the start and [3.7900390625, 3.607421875, 3.10107421875] at the end; model settings like the manifest's: True. Queries and documents are English (query language en, document language en). Each benchmark is reported on its own; there is no combined score.

## qasper

1308 queries run; excluded {'figure_or_table_only': 42, 'all_unanswerable': 79, 'no_evidence': 20, 'evidence_matching_no_passage': 2}. Build 2056.6 s, 23580 passages (0 refused by the helper; characters median 344.0, p95 1098, max 1994; 0 over 2,000), index 1707.9 MiB, readings not succeeded 0.

| Arm | Metric | Mean | 95% interval | n | Minus hybrid (95% interval) |
|---|---|---|---|---|---|
| keyword_only | recall@8 | 0.609 | [0.583, 0.634] | 1308 | -0.106 [-0.125, -0.086] |
| keyword_only | ndcg@10 | 0.421 | [0.402, 0.441] | 1308 | -0.115 [-0.127, -0.102] |
| keyword_only | mrr@10 | 0.375 | [0.353, 0.397] | 1308 | -0.119 [-0.134, -0.103] |
| rerank | recall@8 | 0.715 | [0.691, 0.737] | 1308 | +0.000 [0.000, 0.000] |
| rerank | ndcg@10 | 0.536 | [0.516, 0.556] | 1308 | +0.000 [0.000, 0.000] |
| rerank | mrr@10 | 0.494 | [0.471, 0.517] | 1308 | +0.000 [0.000, 0.000] |
| hybrid | recall@8 | 0.715 | [0.691, 0.737] | 1308 |  |
| hybrid | ndcg@10 | 0.536 | [0.516, 0.556] | 1308 |  |
| hybrid | mrr@10 | 0.494 | [0.471, 0.517] | 1308 |  |

rerank without its 1308 fallback queries: recall@8 none none n=0; ndcg@10 none none n=0; mrr@10 none none n=0

Keyword-only fallbacks of the hybrid arm: none; search seconds (median, p95): keyword_only 0.0265, 0.045; rerank 1.6184, 2.0061; hybrid 0.0503, 0.083

- evidence items mapped: 5075
- evidence items unmatched: {'heading': 190, 'other': 20}
- figure or table items dropped: 218

## scifact

300 queries run; excluded {'query_empty_or_too_long': 0}. Build 1165.0 s, 10920 passages (0 refused by the helper; characters median 373.5, p95 1896, max 1999; 0 over 2,000), index 71.2 MiB, readings not succeeded 0.

| Arm | Metric | Mean | 95% interval | n | Minus hybrid (95% interval) |
|---|---|---|---|---|---|
| keyword_only | ndcg@10 | 0.657 | [0.610, 0.702] | 300 | -0.055 [-0.079, -0.032] |
| keyword_only | recall@10 | 0.784 | [0.737, 0.828] | 300 | -0.058 [-0.096, -0.022] |
| keyword_only | mrr@10 | 0.623 | [0.573, 0.670] | 300 | -0.054 [-0.081, -0.028] |
| rerank | ndcg@10 | 0.712 | [0.669, 0.754] | 300 | +0.000 [0.000, 0.000] |
| rerank | recall@10 | 0.842 | [0.800, 0.881] | 300 | +0.000 [0.000, 0.000] |
| rerank | mrr@10 | 0.677 | [0.629, 0.723] | 300 | +0.000 [0.000, 0.000] |
| hybrid | ndcg@10 | 0.712 | [0.669, 0.754] | 300 |  |
| hybrid | recall@10 | 0.842 | [0.800, 0.881] | 300 |  |
| hybrid | mrr@10 | 0.677 | [0.629, 0.723] | 300 |  |

rerank without its 300 fallback queries: ndcg@10 none none n=0; recall@10 none none n=0; mrr@10 none none n=0

Keyword-only fallbacks of the hybrid arm: none; search seconds (median, p95): keyword_only 0.0082, 0.0145; rerank 1.4322, 1.923; hybrid 0.0679, 0.0858

- documents: 5183
- documents empty: 0
- documents sharing a file: 0
- relevant documents not indexed: 0
- queries with fewer documents than largest cutoff: {'keyword_only': 0, 'rerank': 0, 'hybrid': 0}

## nfcorpus

323 queries run; excluded {'query_empty_or_too_long': 0}. Build 826.0 s, 7593 passages (0 refused by the helper; characters median 291, p95 1902, max 1999; 0 over 2,000), index 53.8 MiB, readings not succeeded 0.

| Arm | Metric | Mean | 95% interval | n | Minus hybrid (95% interval) |
|---|---|---|---|---|---|
| keyword_only | ndcg@10 | 0.301 | [0.268, 0.335] | 323 | -0.052 [-0.065, -0.040] |
| keyword_only | recall@10 | 0.148 | [0.123, 0.174] | 323 | -0.026 [-0.037, -0.016] |
| keyword_only | mrr@10 | 0.501 | [0.453, 0.550] | 323 | -0.073 [-0.099, -0.049] |
| rerank | ndcg@10 | 0.353 | [0.320, 0.389] | 323 | +0.000 [0.000, 0.000] |
| rerank | recall@10 | 0.174 | [0.147, 0.202] | 323 | +0.000 [0.000, 0.000] |
| rerank | mrr@10 | 0.575 | [0.525, 0.623] | 323 | +0.000 [0.000, 0.000] |
| hybrid | ndcg@10 | 0.353 | [0.320, 0.389] | 323 |  |
| hybrid | recall@10 | 0.174 | [0.147, 0.202] | 323 |  |
| hybrid | mrr@10 | 0.575 | [0.525, 0.623] | 323 |  |

rerank without its 323 fallback queries: ndcg@10 none none n=0; recall@10 none none n=0; mrr@10 none none n=0

Keyword-only fallbacks of the hybrid arm: none; search seconds (median, p95): keyword_only 0.0051, 0.0087; rerank 1.5732, 1.9352; hybrid 0.0546, 0.078

- documents: 3633
- documents empty: 0
- documents sharing a file: 40
- relevant documents not indexed: 0
- queries with fewer documents than largest cutoff: {'keyword_only': 79, 'rerank': 0, 'hybrid': 0}

## scidocs

1000 queries run; excluded {'query_empty_or_too_long': 0}. Build 4858.8 s, 52655 passages (0 refused by the helper; characters median 307, p95 1743, max 2000; 0 over 2,000), index 296.0 MiB, readings not succeeded 0.

| Arm | Metric | Mean | 95% interval | n | Minus hybrid (95% interval) |
|---|---|---|---|---|---|
| keyword_only | ndcg@10 | 0.155 | [0.142, 0.168] | 1000 | -0.044 [-0.050, -0.038] |
| keyword_only | recall@10 | 0.162 | [0.149, 0.175] | 1000 | -0.044 [-0.052, -0.037] |
| keyword_only | mrr@10 | 0.271 | [0.249, 0.295] | 1000 | -0.070 [-0.084, -0.056] |
| rerank | ndcg@10 | 0.199 | [0.185, 0.213] | 1000 | +0.000 [0.000, 0.000] |
| rerank | recall@10 | 0.206 | [0.192, 0.221] | 1000 | +0.000 [0.000, 0.000] |
| rerank | mrr@10 | 0.341 | [0.317, 0.366] | 1000 | +0.000 [0.000, 0.000] |
| hybrid | ndcg@10 | 0.199 | [0.185, 0.213] | 1000 |  |
| hybrid | recall@10 | 0.206 | [0.192, 0.221] | 1000 |  |
| hybrid | mrr@10 | 0.341 | [0.317, 0.366] | 1000 |  |

rerank without its 1000 fallback queries: ndcg@10 none none n=0; recall@10 none none n=0; mrr@10 none none n=0

Keyword-only fallbacks of the hybrid arm: none; search seconds (median, p95): keyword_only 0.0308, 0.0628; rerank 1.7204, 2.1449; hybrid 0.1446, 0.1872

- documents: 25657
- documents empty: 0
- documents sharing a file: 1
- relevant documents not indexed: 0
- queries with fewer documents than largest cutoff: {'keyword_only': 0, 'rerank': 0, 'hybrid': 0}

## litsearch

597 queries run; excluded {'query_empty_or_too_long': 0}. Build 9388.2 s, 115164 passages (0 refused by the helper; characters median 199.5, p95 1432, max 2000; 0 over 2,000), index 619.1 MiB, readings not succeeded 0.

| Arm | Metric | Mean | 95% interval | n | Minus hybrid (95% interval) |
|---|---|---|---|---|---|
| keyword_only | recall@5 (specific) | 0.504 | [0.459, 0.551] | 442 | -0.140 [-0.176, -0.105] |
| keyword_only | recall@20 (broad) | 0.427 | [0.352, 0.504] | 155 | -0.177 [-0.247, -0.113] |
| keyword_only | recall@20 (specific) | 0.652 | [0.608, 0.696] | 442 | -0.119 [-0.153, -0.086] |
| keyword_only | recall@5 | 0.449 | [0.410, 0.488] | 597 | -0.142 [-0.172, -0.112] |
| keyword_only | recall@20 | 0.593 | [0.554, 0.632] | 597 | -0.134 [-0.164, -0.105] |
| rerank | recall@5 (specific) | 0.645 | [0.601, 0.688] | 442 | +0.000 [0.000, 0.000] |
| rerank | recall@20 (broad) | 0.605 | [0.529, 0.680] | 155 | +0.000 [0.000, 0.000] |
| rerank | recall@20 (specific) | 0.770 | [0.731, 0.809] | 442 | +0.000 [0.000, 0.000] |
| rerank | recall@5 | 0.590 | [0.551, 0.629] | 597 | +0.000 [0.000, 0.000] |
| rerank | recall@20 | 0.727 | [0.693, 0.762] | 597 | +0.000 [0.000, 0.000] |
| hybrid | recall@5 (specific) | 0.645 | [0.601, 0.688] | 442 |  |
| hybrid | recall@20 (broad) | 0.605 | [0.529, 0.680] | 155 |  |
| hybrid | recall@20 (specific) | 0.770 | [0.731, 0.809] | 442 |  |
| hybrid | recall@5 | 0.590 | [0.551, 0.629] | 597 |  |
| hybrid | recall@20 | 0.727 | [0.693, 0.762] | 597 |  |

rerank without its 597 fallback queries: recall@5 (specific) none none n=0; recall@20 (broad) none none n=0; recall@20 (specific) none none n=0; recall@5 none none n=0; recall@20 none none n=0

Keyword-only fallbacks of the hybrid arm: none; search seconds (median, p95): keyword_only 0.0967, 0.1648; rerank 1.3514, 1.7352; hybrid 0.2272, 0.3087

- documents: 64183
- documents empty: 6197
- documents sharing a file: 330
- relevant documents not indexed: 5
- queries with fewer documents than largest cutoff: {'keyword_only': 0, 'rerank': 0, 'hybrid': 0}

