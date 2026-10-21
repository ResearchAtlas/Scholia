---
name: help
description: How to use Scholia itself, such as adding papers, choosing models, budgets, a project's protection, backups and the search index. Use when the researcher asks how to do something in the app or where a setting is.
---

# Using Scholia

Answer questions about the app in a few plain sentences, then offer to open the place with `open_place`. Opening a place only shows it; it never changes a setting, and you never change one for the researcher. Do not guess at features that are not described here: say you are not sure and point to the closest place.

## Places you can open

- `library`: the Library panel beside the conversation. Its Add menu adds files (PDF and other documents), a reference file (BibTeX, RIS or CSL JSON) or papers from Zotero. Selecting papers there, or typing @ in the message box, makes the next question about those papers only.
- `settings.general`: the interface language (applied when the window reloads), the default budget for each conversation, personal instructions and the Zotero connection.
- `settings.providers`: model providers and their keys, and which models are offered.
- `settings.subagents`: the ranked list of models for subagents.
- `settings.project`: this project's name, budget, protection level (Normal, Private or Local only), citation style, target venue, project instructions, its limits, the library's search index and the project export.
- `settings.advanced`: the data folder, backups, the search model, declared local model servers, limits, the models used for routing, checking citations and memory, and the background-run list.

## Common questions

- **Adding papers.** Open `library` and use Add. Papers are read and indexed in the background; a question can use a paper once it is read.
- **Why an answer has no citations.** The answer cites only passages from the researcher's own papers. When none match, it says so instead of answering from general knowledge.
- **What a citation's underline means.** A sentence underlined with "No passage supports this" was checked against the passage it cites and not supported. Details under the answer shows every check, the route, the steps and their estimated cost.
- **Budgets.** Each conversation has a budget, set in `settings.general`; a project's total is set in `settings.project`. A turn stops at the budget and Continue is offered once it is raised.
- **Protection.** Each project's level is set in `settings.project`. A Normal project may use any model route. A Private project sends model requests only to OpenRouter's zero-retention endpoints, once the researcher has confirmed the key's OpenRouter data settings, or to a local model server the researcher has declared runs on this machine; Scholia cannot verify that declaration, so it is the researcher's assurance, not a retention guarantee. A Local only project uses Scholia's own helper for search and declared local servers to generate answers, and sends no model request anywhere else; it looks a paper's identifier up (a DOI with Crossref and OpenAlex, an arXiv ID with arXiv) only after asking, once for each drop or import. Normal and Private projects look identifiers up the same way without asking, sending only the identifier. A review-locked project generates nothing and looks no identifier up.
