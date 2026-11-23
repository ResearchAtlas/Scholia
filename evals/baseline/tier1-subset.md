# Tier 1 small subset (S1-18)

The small fixed subset since Harold's decision of 2026-10-17: 100 QASPER test papers (listed in `evals/tier1_manifest.json`, drawn with seed 20261015), all their questions; SciFact, NFCorpus, SCIDOCS and LitSearch run only in full tier 1. `tier1-subset.json` is the reference run a later subset run is compared with (`evals/tier1.py compare evals/baseline/tier1-subset.json <run>`). The full baseline is `tier1-baseline.*`.

## Runs on the reference Mac (Apple M5, Mac17,4, 24 GB, macOS 27.0.1)

| Run | Commit | Start, end (CST) | Wall time | Build (read, embedding) | Load average at start, end (1, 5, 15 min) |
|---|---|---|---|---|---|
| a | `de3da19` | 2026-10-17 00:26:02, 00:37:25 | 682 s | 643 s (13 s, 629 s; 9.3 passages/s) | 4.34 4.04 3.07; 15.91 8.04 5.70 |
| b | `bc88c7c` | 00:37:26, 00:45:20 | 474 s | 457 s (11 s, 444 s; 13.2 passages/s) | 15.91 8.04 5.70; 4.56 7.81 6.94 |
| c (`tier1-subset.json`) | `bba313f` | 07:28:32, 07:33:17 | 285 s | 272 s (22.1 passages/s) | 2.56 2.65 3.10; 3.13 3.01 3.13 |

Runs a and b were made under the manifest's earlier rules (rules hash 692683fd…); run c under the current ones (fe0d5570…), which changed only how the document benchmarks' files are written (each carries its title), not QASPER's papers. Other writers' work ran on the Mac during a and b (load up to 15.9). All three finished with no gaps, no keyword-only fallbacks of the hybrid arm and no refused passages: 5,884 passages, 328 questions scored (excluded: all unanswerable 19, figure or table only 17, no evidence 4; unmatched evidence items: heading 64, other 2; figure or table items dropped 60).

**Agreement.** Every metric is identical in all three runs. Rankings are identical for all 328 questions in the keyword-only arm and for 327 of 328 in the hybrid arm, between a and b and between b and c.

| Arm | recall@8 [95%] | nDCG@10 | MRR@10 | Search median, p95 (run c) |
|---|---|---|---|---|
| hybrid | 0.737 [0.690, 0.781] | 0.562 | 0.518 | 32 ms, 41 ms |
| keyword_only | 0.627 [0.573, 0.679] | 0.451 | 0.405 | 5 ms, 8 ms |

**Against the full baseline** (run c, its 328 questions paired by id): keyword-only rankings identical for 51, hybrid for 73; recall@8 differences +0.0035 [−0.0096, +0.0175] (keyword-only) and −0.0013 [−0.0088, +0.0037] (hybrid), no drop flagged. The differences come from the search index itself, not from the runner: its keyword scores (FTS5's BM25) take term statistics over every passage in the index, which here are 100 papers' instead of 416 papers', so the same question in the same paper can rank passages differently. Hence the reference run for subset comparisons is a subset run.

## A PDF-only parser change (`pdf-4`, 2026-11-23)

The PDF follow-up PR changes only PDF reading (`backend/extraction.py`'s PDF path, the `pdf-4` version) and how citations compare quotes. Tier 1's materials, QASPER's papers and the document benchmarks alike, are written and read as Markdown (`evals/tier1.py`), so no tier 1 number can move with it, and the full run that `evals/README.md` asks of "a change to … the parser" would repeat the baseline. On Harold's word (2026-11-23) the subset was run as the check instead, at `8e3e70e` on the reference Mac (macOS 27.2.1, so `compare` says not like for like; load 1.75 at the start, 2.19 at the end): 258.5 s, 5,884 passages, 328 questions; every metric's paired difference from `tier1-subset.json` is +0.0000 in both arms, with rankings identical for 328 of 328 (keyword only) and 327 of 328 (hybrid), as between the recorded runs above.

The same PR's fix batch 1 (2026-11-23) also changed two patterns every reader shares, so it reaches Markdown:
- `_ABSTRACT`: "Abstract" is no longer the start of a longer word.
- `_CAPTION`: a Roman numeral must stand alone, so prose such as "Table is ..." is not a caption.

Every tier 1 material of all five benchmarks was read by the `82f2031` and the final code (`06a6c82`): 210,623 passages both times, and 37 materials differ.
- In 35, a passage beginning with a word such as "Abstractive", "Abstraction" or "Abstracts" was typed `abstract` with its first eight letters cut off. It is now a `paragraph`, whole.
- In 2, prose beginning "Table is" was typed `caption`. It is now a `paragraph`.

One of these, QASPER's 1911.06919, is a subset paper, so the subset was run again at `06a6c82` on the reference Mac (macOS 27.2.1, so `compare` says not like for like; load 1.94 at the start, 1.89 at the end; 244.5 s; 5,884 passages, 328 questions).
- Every metric's paired difference from `tier1-subset.json` is again +0.0000.
- Rankings are identical for 328 of 328 questions (keyword only) and 326 of 328 (hybrid).
- The two hybrid rankings that differ are in other papers (1605.04278, 1903.04329). Each paper is built in its own project, and both versions read these two identically, so the differences are run to run in the meaning search, not this change.
- Two earlier runs during the batch, at `6e9d2cf` and `3ee1ae8`, had 6 and 3 such differences, also in unchanged papers.
