# Gold sets (tiers 2 and 3)

The gold sets measure Scholia's retrieval on papers of two research fields, and its support judge on labeled citation pairs (slice 1 section 14; tickets 17, 22, 70 and 72). They are built by `evals/gold.py` (S1-36). Every set here is a draft until a rater has reviewed it.

- **Field 2** (AI and NLP): recent arXiv cs.CL and cs.AI papers whose specific version is licensed CC BY 4.0 or CC0 1.0, leaving out QASPER's papers. Its files are public and live in `field2/`.
- **Field 1** (social science, English and Chinese papers): Harold's own papers. Its files never enter this repository. They stay in a folder given with `--folder`, outside any git work tree (this repository's checkouts and worktrees included) or under the `evals/local/` of a checkout whose `.gitignore` ignores it, and are written owner-only. No link is followed in it or in the output folders: a link at or below the folder (its `labels/` or `out/`, a set, a written file) refuses the command. The one exception is a PDF in the PDFs folder, which may link to storage outside every git work tree (Zotero's, say).

## Held out and provisional

Tier 2 is held out, questions and provisional labels alike. Tuning uses tier 1 and made-up development cases only, and the candidate order and acceptance rules are frozen before any tier 2 result is examined. No module but `evals/gold.py` reads this folder; a static test checks that. Chinese development cases elsewhere in the repository are made up, never drawn from these drafts.

Every drafted item is provisional. A rater's labels make it gold. The corrected set (`gold.py apply`) keeps an unlabeled item, or one changed since it was labeled, marked `provisional: true`, and tier 2 never counts it.

## What a field's folder holds

- `papers.json`: the paper list. For field 2, each paper version records its arXiv id with version, title, authors, primary category, submission date, topic group, abstract page and PDF address, the license read from that version's own abstract page and when, and the PDF's size and SHA-256. The list also records the selection criteria and seed, and the extractor version the locators were drafted against. The PDFs stay out of git: commands read them from a prepared folder and refuse a file whose size or SHA-256 differs. Nothing is ever downloaded by these commands.
- `questions.json`: each question has its text, its query language and document language, whether it is answerable, and for an answerable one a short answer and its evidence. Evidence is a list of items. Each item is the passages that make up one span: one passage, or the consecutive passages of a split paragraph. An item is found when any of its passages is retrieved, as in tier 1.
- `pairs.json`: each citation pair has a sentence, its language, the cited passage and the category it was drafted as (supported, partly supported or unsupported). No judge verdict is stored.
- `sample-questions.json`, `sample-pairs.json`: the second rater's sample.
- `labels/<set id>.<rater>.json`: one rater's labels, written by the review page.

A locator names the paper, the passage's ordinal in the app's own reading (section 7.1), its page, kind and section, and quotes the passage whole. Character ranges are not part of a locator. Reference passages are never evidence. In field 2 every quote carries its attribution (authors, title, arXiv id with version) and that paper version's license (CC BY 4.0 or CC0 1.0). A pair's sentence is written in new words. The check refuses a sentence that shares more than 40 letters and digits in a row with its passage.

## Field 2's drafts

`field2/` holds the paper list and the drafts. The drafts are 100 questions and 100 citation pairs, as `settings.json` asks.
- **The papers.** 24 arXiv paper versions, six in each of four topic groups: retrieval-augmented generation; citation and attribution in LLM answers; long-context evaluation; hallucination detection.
  - The long-context group's query also returned method papers that test on long-context benchmarks, and the draw kept them.
  - They were prepared on 2026-10-20 under the M2 charter's section 6, from arXiv itself. Each group's candidates were filtered by primary category, first-submission date, QASPER and language, then drawn with the group's recorded seed.
  - **Recorded limits.**
    - Each license rests on that exact version's own abstract page alone. The arXiv API listings used carry no license field, and no other metadata source was queried; arXiv's license help page says a license is chosen per version.
    - Each group's candidates were also filtered by a title pattern, to keep the group coherent.
    - The retrieval-augmented generation group drew from the 200 most recent of 1,525 listing results.
  - `papers.json` records the criteria, queries, patterns and seeds.
- **The drafting.** Claude drafted the questions and pairs in its own session from the passage worksheet (`gold.py passages`). No tool called a model.
  - Each answerable question's evidence is the passages that answer it, and every quote is a whole passage of the app's own reading (without the noncharacter U+FFFE that PDF extraction leaves at a line-break hyphen). The check confirmed every locator against the pinned PDFs.
  - Each unanswerable question was checked by searching every paper's passages for its subject. No retrieval was run over the drafted questions, because they are held out.
  - Pair sentences are written in new words. Unsupported pairs are near misses: a wrong number, a reversed finding, an overclaim. Partly supported pairs join a supported claim to one the passage does not make.
- **The samples.** The second rater's samples (`sample-*.json`) were drawn once, before any labeling.

## The settings

`settings.json` holds the study-design numbers as data, as Harold approved them on 2026-10-20:
- Papers: 24 papers in 4 to 6 topic groups.
- Questions: 100, of which 50 are in English and 50 in Simplified Chinese about the English papers. Of these, 18 are unanswerable (9 per language) and 20 span several papers (10 per language).
- Citation pairs: 100, about 34 drafted supported, 33 partly supported and 33 unsupported. Their sentences are 50 English and 50 Chinese, and the unsupported ones are near misses.
- The review page: pairs come in a seeded shuffled order, and the page hides the drafted category and any judge verdict.
- The second rater's sample: a seeded 20% of each set, stratified by language and by drafted category (pairs) or answerable and multi-paper (questions).

The check reports where a set's mix differs from these targets; it refuses only what breaks a rule.

Which judge verdicts count as flagging a pair (`judge.flagged_verdicts`, now `unsupported` only) is not decided yet.

## Commands

Run from the repository root. `--folder` is required for field 1.

    uv run --no-sync python -I evals/gold.py check --field 2 [--pdfs <prepared PDFs> --qasper <qasper-v0.3>]
    sandbox-exec -f tests/loopback-only.sb uv run --no-sync python -I evals/gold.py passages --field 2 --pdfs <prepared PDFs>
    uv run --no-sync python -I evals/gold.py review --field 2 --set questions --rater harold [--lang zh-CN]
    uv run --no-sync python -I evals/gold.py sample --field 2 --set pairs
    uv run --no-sync python -I evals/gold.py review --field 2 --set pairs --rater <colleague> --sample
    uv run --no-sync python -I evals/gold.py agreement --field 2 --set pairs --raters harold <colleague>
    uv run --no-sync python -I evals/gold.py apply --field 2 --set questions --rater harold

- `check` checks the paper list, each set, each labels file and each sample, and reports the mix. With the PDFs, it reads every paper through the app and confirms every locator against that reading. Field 2's check with the PDFs also checks the papers against QASPER v0.3's three splits. When the extractor's version has changed since drafting, it reports each locator whose passage moved and refuses a quote that matches nothing. It never rewrites a set.
- `passages` writes the drafter's worksheet: every paper's passages as the app reads them, with what a locator needs. It runs the app as the desktop does: a temporary data folder, an in-memory credential store, a review-locked project, so nothing is looked up. The worksheet and the check's report hold whole papers' text, so they go to the git-ignored `evals/out/` (field 1: `<folder>/out/`). They say `incomplete` until finished.
- `review` serves one set's review page on 127.0.0.1 and prints its address with this launch's secret.
  - Questions: Harold keeps, edits or drops each one, and confirms or changes its evidence, shown with its text, page and section and chosen from the paper's passages.
  - Pairs: he marks each one supported, partly supported or unsupported.
  - Each decision is written to the labels file before the page says it is saved, so work can stop and resume.
  - A question is always saved whole, exactly as the page shows it (question, short answer, answerable, evidence), with the revision of its label the page loaded. It is stored as keep when it is the draft as drafted, and otherwise as an edit holding the whole question. Nothing is filled in from the draft or merged. Keep on an edited question keeps the edit as shown; Revert sends the draft.
  - A save from a page that loaded an older revision is refused, and the page keeps the rater's edit open with the saved question shown above it.
  - Before anything is written, the whole set as it would stand is checked with the drafts' own rules, including that no two questions have the same text. A save that would break one is refused, naming the rule. `check`, `apply` and `agreement` use the same check, and `check` with the PDFs also resolves a rater's chosen evidence.
  - Each passage chosen in the picker goes back with a key the server gave it, so a passage is never saved under another paper.
  - Every evidence passage of a saved question, a draft's own included, must be a passage of the current worksheet. A draft whose passage no longer resolves (`check` reports it moved) can be saved only once its evidence is chosen again.
  - A label applies to its item as it now is (its content hash matches), and only such a label is validated and applied. A label for an item changed or removed since is history: kept untouched in the file, never validated against the current set or papers, never applied, and its item is shown again for labeling.
  - The page loads nothing remote and logs nothing.
  - `--sample` serves only the sampled items, for the second rater, from their own labels file.
  - Field 1's sample holds Harold's papers' text, so its page is written only with `--colleague`, on Harold's decision.
- `sample` draws the second rater's sample once.
- `agreement` reports, on the items both raters labeled:
  - for pairs: percent agreement, Cohen's kappa over the three categories, and the confusion table;
  - for questions: agreement on keep or drop and on answerable, and on evidence (the same items, and the overlap of the passages chosen).
- `apply` writes the corrected set from the drafts and one rater's labels.

Field 2's labels files may be committed when Harold chooses. Field 1's never leave its folder.

## Tiers 2 and 3

Tier 2 (`evals/retrieval_gold` in section 14) is recall@8 on the answerable questions of a corrected set, with English and Chinese questions reported separately and the document language beside them. `gold.tier2_queries` and `gold.tier2_scores` shape a corrected set into the queries `evals/tier1.py` runs and score them by tier 1's item rule. Tier 3 scores the support judge against the labeled pairs (`gold.judge_scores`). Both run at the preview's gate, never on the drafts in M2. The judge and the answer model are paid, so only their scoring is built here, tested on synthetic inputs.
