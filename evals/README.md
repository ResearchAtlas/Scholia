# Evaluations

Evaluation sets and their runners. Real research data is never committed here.

## Tier 1: public retrieval benchmarks (S1-18)

Tier 1 measures Scholia's own search on five public benchmarks, each reported on its own with no combined score (slice 1 section 14; tickets 17, 70 and 72 E): SciFact, NFCorpus and SCIDOCS in BEIR's form, LitSearch, and QASPER evidence. Every query and document is English; results still give the query language and the document language apart. Chinese and cross-language development cases are not part of tier 1: S1-19 builds them (Harold, 2026-10-17), and as synthetic cases they do not establish the Chinese result (ticket 72 E3).

- `tier1.py`: the runner. It builds each benchmark through the app's own API and reader (section 7.1 cuts the passages, one document or paper per material, under its own title), imports the search model once the files are read so the app's index runs embed them, searches with `backend.search.search` as the app runs it (the `hybrid` arm) and with dense search unavailable (the `keyword_only` reference arm), scores, and writes one results file atomically when the run ends. Arms are plain named functions in `ARMS`.
- `metrics.py`: recall@k, nDCG@k, MRR@k, the passage-to-document collapse (a document at its best passage's rank) and the paired percentile bootstrap, in the standard library.
- `tier1_manifest.json`: the pins. Each set's official source, license as its authors state it, archive hashes, and the size and SHA-256 of every file the runner reads; the small fixed subset; the model settings a run must match to be like for like; the QASPER rules (evidence to passages, annotators, exclusions); and ticket 72's rule E, word for word, with each benchmark's own metric and the bootstrap's count and seed.
- `baseline/`: the committed baseline, a full tier 1 run on the reference Mac, as JSON with a Markdown summary (`tier1-baseline.*`), and the small subset's reference run with the record of its two runs beside it (`tier1-subset.*`).
- `litsearch_to_jsonl.py`: a one-off preparation step (below).

The runner never downloads anything and refuses a file whose size or SHA-256 differs from its pin. The sets are prepared once, outside git, under the M2 charter's section 6, each in its own folder with a `PROVENANCE.md`; the folder names are the manifest's `folder` values. Benchmark text never enters git: results carry scores, counts and hashes, and query ids only for QASPER and SciFact, whose licenses allow them.

LitSearch ships only as parquet and the project has no parquet reader, so its query and `corpus_clean` files are converted to JSONL once, with pyarrow 25.0.1 installed by uv for that command alone (the project gains no dependency):

    uv run --no-project --with pyarrow==25.0.1 python -I evals/litsearch_to_jsonl.py <sets>/litsearch/archives <sets>/litsearch/converted

The script checks the parquet files' SHA-256 before converting; the manifest pins both the parquet hashes and the converted files'.

### When each level runs

Commands run from the repository root. `<sets>` is the folder holding the prepared sets; `<bundle>` a built `Scholia.app` whose `llama-server` matches its SHA-256 manifest; `<model>` the pinned `Qwen3-Embedding-0.6B-Q8_0.gguf`.

1. **Every PR** runs its tests: `uv run pytest -q` (this runner's tests use synthetic data and the test-owned stand-in helper).
2. **A PR that touches retrieval** also runs the small fixed subset on the reference Mac (100 QASPER test papers drawn with a recorded seed and listed by id in the manifest, all their questions; about 8 to 11 minutes), then compares it with the subset's reference run, built from the same papers:

       sandbox-exec -f tests/loopback-only.sb uv run --no-sync python -I evals/tier1.py run --level subset \
           --sets <sets> --helper <bundle>/Contents/MacOS/llama-server --model <model> --out subset.json
       uv run --no-sync python -I evals/tier1.py compare evals/baseline/tier1-subset.json subset.json

   Compared with the full baseline instead (its questions pair by id), the keyword rankings differ even with nothing changed: the index's BM25 statistics cover every passage in it, so 100 papers and 416 rank some passages differently (`tier1-subset.md`).

3. **A change to a model, the parser, the passages or the pipeline**, and the preview gate, run all of tier 1 (`--level full`, the same command; SciFact, NFCorpus, SCIDOCS and LitSearch run only here), then `compare`.

`compare` prints each benchmark's paired difference from the baseline per arm and metric, with its 95% interval, and flags a drop whose interval lies wholly below zero for investigation. It blocks nothing by itself: a reproducible regression needs investigation and a recorded disposition. A run on another machine, with model settings other than the manifest's, or under different pins, rules or metrics (`manifest_rules_sha256`: the manifest without its levels) is said not to be like for like. `report <results>` prints a results file's Markdown summary.

Each run gets new temporary data folders, an in-memory credential store and review-locked projects, so nothing is looked up; run it under the loopback-only sandbox, as above. The reference Mac is shared: each results file records the load average at its start and end.

## Tiers 2 and 3: the gold sets (S1-36)

The gold sets measure retrieval on two research fields' papers (tier 2) and the support judge on labeled citation pairs (tier 3). `gold.py` holds their formats and checks, the passage worksheet a drafter writes them from, the local review pages Harold corrects and labels them with, the second rater's sample and the agreement. `gold/README.md` describes them. They are held out: no other module reads `gold/`, and tier 2 is never used for tuning. Every drafted item is provisional until a rater labels it. Field 2's sets, drawn from arXiv paper versions licensed CC BY 4.0 or CC0 1.0, are committed under `gold/field2/` without the PDFs. Field 1's come from Harold's library and stay in a local folder git ignores (`evals/local/`, or outside the repository). Generated worksheets and reports go to the git-ignored `evals/out/`.
