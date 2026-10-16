# Tier 1 small subset (S1-18)

The small fixed subset since Harold's decision of 2026-10-17: 100 QASPER test papers (listed in `evals/tier1_manifest.json`, drawn with seed 20261015), all their questions; SciFact, NFCorpus, SCIDOCS and LitSearch run only in full tier 1. `tier1-subset.json` is the reference run a later subset run is compared with (`evals/tier1.py compare evals/baseline/tier1-subset.json <run>`). The full baseline (`tier1-baseline.*`) stays as run.

## Two runs on the reference Mac (Apple M5, Mac17,4, 24 GB, macOS 27.0.1)

| Run | Commit | Start, end (CST) | Wall time | Build (read, embedding) | Load average at start, end (1, 5, 15 min) |
|---|---|---|---|---|---|
| a | `de3da19` | 2026-10-17 00:26:02, 00:37:25 | 682 s | 643 s (13 s, 629 s; 9.3 passages/s) | 4.34 4.04 3.07; 15.91 8.04 5.70 |
| b (`tier1-subset.json`) | `bc88c7c` | 00:37:26, 00:45:20 | 474 s | 457 s (11 s, 444 s; 13.2 passages/s) | 15.91 8.04 5.70; 4.56 7.81 6.94 |

The two commits run the same code and the same manifest rules (`manifest_rules_sha256` 692683fd…; they differ only in the manifest's `rules_unchanged_since` record and its test). Other writers' work ran on the Mac meanwhile (load up to 15.9). Both finished with no gaps, no keyword-only fallbacks of the hybrid arm and no refused passages: 5,884 passages, 328 questions scored (excluded: all unanswerable 19, figure or table only 17, no evidence 4; unmatched evidence items: heading 64, other 2; figure or table items dropped 60).

**Agreement.** Every metric is identical between the two runs. Rankings are identical for all 328 questions in the keyword-only arm and for 327 of 328 in the hybrid arm.

| Arm | recall@8 [95%] | nDCG@10 | MRR@10 | Search median, p95 (run b) |
|---|---|---|---|---|
| hybrid | 0.737 [0.690, 0.781] | 0.562 | 0.518 | 41 ms, 52 ms |
| keyword_only | 0.627 [0.573, 0.679] | 0.451 | 0.405 | 6 ms, 10 ms |

**Against the full baseline** (run a, its 328 questions paired by id): keyword-only rankings identical for 51, hybrid for 73; recall@8 differences +0.0035 [−0.0096, +0.0175] (keyword-only) and −0.0013 [−0.0088, +0.0037] (hybrid), no drop flagged. The differences come from the search index itself, not from the runner: its keyword scores (FTS5's BM25) take term statistics over every passage in the index, which here are 100 papers' instead of 416 papers', so the same question in the same paper can rank passages differently. Hence the reference run for subset comparisons is a subset run.
