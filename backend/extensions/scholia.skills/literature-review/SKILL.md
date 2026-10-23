---
name: literature-review
description: Screen and synthesize the papers in the researcher's library into a literature review. Use when the researcher asks for a literature review, a synthesis across papers, what the papers in their library say about a question, or which of their papers meet given criteria.
---

# Literature review over the library

Adapted from K-Dense's literature-review skill (MIT License, Copyright (c) 2025 K-Dense Inc.), version 1.11; see NOTICE.md. This version works only over the papers already in the researcher's library: finding new papers is not available yet, so say so if the researcher asks for it.

## How to work

1. **Settle the question and the criteria.** Restate the review question in one sentence. If inclusion criteria matter (populations, designs, dates, outcomes) and the researcher has not given them, ask with the `ask` tool, offering at most three options. Write the criteria down before screening.
2. **Screen the library.** Use `list_materials` to see the papers. Screen titles first, then read what you need with `search_passages` or `read_passage`. Keep a count: papers listed, excluded at the title, excluded after reading (with the reason), and included. An abstract or metadata record is not the paper's text: screen on it only as far as it goes, and say so.
3. **Extract.** For each included paper, note its design, population or data, main findings with their conditions, and the limitations it states. Use `answer_from_library` for a focused question across the library; it retrieves, answers with citations and checks them.
4. **Synthesize by theme, not paper by paper.** Group findings into a few themes. Within each, compare and contrast the papers, say where they agree, where they conflict and how strong the evidence is. Name gaps: what no included paper addresses.
5. **Keep the project's state.** After screening, and whenever you stop before the review is done, note what is left with `update_progress`: each open question, and the one next step you suggest, one note per call. These are your notes, never the researcher's decisions.
6. **Report.** Give the question, the criteria, the screening counts, the themes with their evidence, the gaps and the limits of this review (it covers only the library; screening was done by one reader). See `references/screening.md` and `references/synthesis.md` for more detail.

## Rules

- Cite every claim drawn from a paper with its marker, such as [S1.2], exactly as the tools give it. Phrase it as what the paper reports, under the conditions it states.
- Never cite a paper you have not read in this conversation, and never invent counts, effect sizes or studies.
- A paper marked retracted is reported as retracted and not used as support.
- Where the library cannot answer part of the question, say so plainly.
