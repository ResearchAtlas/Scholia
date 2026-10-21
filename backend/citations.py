"""Cited answers: the evidence a request carries, the citations an answer makes, and their checks
(slice-1 spec F4 and sections 6.2, 7.5 and 14; tickets 17 and 71; final review F02).

The evidence goes into the request's RESEARCH EVIDENCE section as numbered records: each paper a
source (S1, S2, ...) in the order its first passage ranks, each of its passages a number within it, so
`[S2.3]` names the third passage of the second paper. Each record says its paper, page, evidence type
and, for a retracted paper, that it is retracted, so an abstract or metadata is never taken for
full-text support. When the bundle holds nothing the request says so, in the conflicts and missing
information section, and the system rules ask for missing evidence to be stated (abstention wording,
not filler).

Every delivered answer is checked before its primary commit:
- Existence and mapping, locally: each marker must parse, name a record of this run's evidence (one
  read in the run), and point at a passage that still exists. Otherwise `not_found` (unparsed, or no
  such record), `not_read_in_run`, or `source_removed` (its paper deleted or re-read since, and no
  current reading holds its quote).
- Support, by the judge: each sentence with a citation that exists is paired with each passage it
  cites; up to `[retrieval] judge_pairs` pairs go in one call. Verdicts: supported, partly,
  unsupported; anything the judge does not answer, and every pair when it fails, times out or
  refuses, is `unchecked`, and the answer says so. A sentence is never removed or rewritten.

The answer's citations are `citations` rows (owner_kind `answer`, owner the turn's run), written in the
primary commit with the answer.
"""

import json
import re

from backend.evidence import CITATION
from backend.search_index import readings

# A marker that starts like a citation but does not parse ([S1], [S1.x], [S 2.3]): kept as not_found.
_MALFORMED = re.compile(r"\[S[^\]\n]{0,12}\]")
# A sentence ends at . ! ? before a space or the end, at 。！？, or at a line's end; a point inside a
# marker or a number ([S1.2], 3.5) does not end it.
_SENTENCE = re.compile(r"(?:[^.!?。！？\n]|[.!?]+(?=[^\s.!?]))+(?:[.!?]+(?=\s|$)|[。！？]+|\n|$)")
VERDICTS = ("supported", "partly", "unsupported")
QUOTE_CHARS = 500  # the part of a passage a citation keeps as its quote


def sources(records):
    """{(source, passage): record}, numbering papers in the order their first record ranks."""
    papers, numbered = {}, {}
    for record in records:
        source = papers.setdefault(record["material_id"], len(papers) + 1)
        within = sum(1 for key in numbered if key[0] == source) + 1
        numbered[(source, within)] = record
    return numbered


def bundle(numbered):
    """The RESEARCH EVIDENCE section's records, one per passage, best first by source."""
    lines = []
    for (source, passage), r in numbered.items():
        facts = [f"p. {r['page']}" if r.get("page") else None, r["evidence_type"].replace("_", " "),
                 "RETRACTED" if r.get("retraction") == "retracted" else None]
        lines.append(f"[S{source}.{passage}] {r['title']} ({'; '.join(f for f in facts if f)})\n{r['text']}")
    return lines


def missing(numbered, *, mode, route):
    """The conflicts and missing information section's records for this evidence."""
    notes = []
    if route in ("literature", "memory_literature") and not numbered:
        notes.append("No passage in the researcher's library matched this question. Say that the library holds no "
                     "evidence for it; do not answer from general knowledge as if it were cited.")
    if mode == "keyword_only":
        notes.append("Meaning search was unavailable, so the evidence came from keyword search only.")
    if route in ("memory", "find_papers"):
        notes.append("Earlier decisions, project state and new-paper search are not available yet; if the question "
                     "depends on them, say they could not be retrieved rather than inventing them.")
    return notes


def sentences(text):
    """(start offset, sentence) for each sentence of an answer, as written."""
    return [(m.start(), m.group()) for m in _SENTENCE.finditer(text) if m.group().strip()]


def markers(text):
    """[(offset, marker, (source, passage) or None)] for every citation-like marker, in order."""
    found = {m.start(): (m.start(), m.group(), (int(m[1]), int(m[2]))) for m in CITATION.finditer(text)}
    for m in _MALFORMED.finditer(text):
        found.setdefault(m.start(), (m.start(), m.group(), None))
    return [found[k] for k in sorted(found)]


def check_existence(conn, project_id, text, numbered):
    """The answer's citations, each with its existence, read in the transaction given: a list of dicts
    for citation rows, each with the sentence it sits in."""
    spans = sentences(text)
    cited = [n for _, _, n in markers(text) if n in numbered]
    current = readings(conn, project_id, {numbered[n]["passage_id"] for n in cited}, references=False) if cited else {}
    by_material = {}
    out = []
    for offset, marker, number in markers(text):
        sentence = next((s for start, s in spans if start <= offset < start + len(s)), "")
        row = {"marker": marker, "offset": offset, "sentence": sentence.strip(), "number": number}
        record = numbered.get(number) if number else None
        if record is None:
            out.append({**row, "existence": "not_found"})
            continue
        quote = record["text"][:QUOTE_CHARS]
        found = {"material_id": record["material_id"], "material_version_id": record["version_id"],
                 "passage_id": record["passage_id"], "quote": quote, "page": record.get("page"),
                 "evidence_type": record["evidence_type"], "retraction": record.get("retraction")}
        if record["passage_id"] in current:
            out.append({**row, **found, "existence": "ok"})
            continue
        # Re-read since: a current reading of its paper that holds its quote still supports it (status.md,
        # decided 2026-10-17 (8)); otherwise the source was removed.
        if record["material_id"] not in by_material:
            by_material[record["material_id"]] = readings(conn, project_id, references=False,
                                                          materials=[record["material_id"]])
        holder = next((pid for pid, r in by_material[record["material_id"]].items() if quote and quote in r[4]), None)
        if holder is not None:
            r = by_material[record["material_id"]][holder]
            out.append({**row, **found, "passage_id": holder, "material_version_id": r[7], "page": r[5], "existence": "ok"})
        else:  # its paper or passage may be gone: the row keeps only what still exists (foreign keys)
            kept = {k: v for k, v, table in (("material_id", found["material_id"], "materials"),
                                             ("material_version_id", found["material_version_id"], "material_versions"),
                                             ("passage_id", found["passage_id"], "passages"))
                    if conn.execute(f"SELECT 1 FROM {table} WHERE id = ?", (v,)).fetchone()}
            out.append({**row, **found, "material_id": None, "material_version_id": None, "passage_id": None, **kept,
                        "existence": "source_removed"})
    return out


def pairs(checked):
    """The (sentence, passage id) pairs the judge checks: each sentence with each passage it cites that
    exists, once."""
    seen = []
    for c in checked:
        if c["existence"] == "ok" and (c["sentence"], c["passage_id"]) not in seen:
            seen.append((c["sentence"], c["passage_id"]))
    return seen


JUDGE_RULES = (
    "You check citations in a research assistant's answer. For each numbered pair, say whether the passage "
    "supports the sentence: supported (the passage states what the sentence claims), partly (it supports some "
    "of it, or with conditions the sentence leaves out) or unsupported. Judge only from the passage. Reply with "
    "one line per pair, exactly in the form `N: verdict`, and nothing else."
)


def judge_request(batch, texts):
    """The judge's request text for a batch of pairs; texts maps a passage id to its text."""
    lines = []
    for i, (sentence, pid) in enumerate(batch, start=1):
        lines.append(f"Pair {i}\nSentence: {sentence}\nPassage: {texts[pid]}")
    return "\n\n".join(lines)


def parse_verdicts(reply, count):
    """{pair number: verdict} from the judge's reply; a pair it does not answer clearly is left out."""
    found = {}
    for line in (reply or "").splitlines():
        m = re.match(r"\s*(?:pair\s*)?(\d+)\s*[:.)-]\s*(supported|partly|unsupported)\b", line, re.IGNORECASE)
        if m and 1 <= int(m[1]) <= count:
            found.setdefault(int(m[1]), m[2].lower())
    return found


def batches(items, size):
    return [items[i:i + size] for i in range(0, len(items), size)]


def apply_verdicts(checked, verdicts):
    """Each citation's support, from its (sentence, passage) pair's verdict, or unchecked."""
    return [{**c, "support": verdicts.get((c["sentence"], c.get("passage_id")), "unchecked")
             if c["existence"] == "ok" else "unchecked"} for c in checked]


def write_rows(conn, run_id, checked, checked_at, new_id):
    """The answer's citation rows, in the primary commit."""
    for c in checked:
        conn.execute(
            "INSERT INTO citations (id, owner_kind, owner_id, material_id, material_version_id, passage_id, quote,"
            " context, page, evidence_type, existence, support, support_run_id, checked_at)"
            " VALUES (?, 'answer', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (new_id(), run_id, c.get("material_id"), c.get("material_version_id"), c.get("passage_id"), c.get("quote"),
             json.dumps({"marker": c["marker"], "offset": c["offset"], "sentence": c["sentence"],
                         "retraction": c.get("retraction")}),
             c.get("page"), c.get("evidence_type"), c["existence"], c.get("support", "unchecked"),
             run_id if c.get("support", "unchecked") != "unchecked" else None, checked_at))
