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

import hashlib
import json
import re

from backend.context import MESSAGE_FRAMING, REPLY_FRAMING, count
from backend.evidence import CITATION
from backend.search_index import readings, still_read

# A marker that starts like a citation but does not parse ([S1], [S1.x], [S 2.3]): kept as not_found.
_MALFORMED = re.compile(r"\[S[^\]\n]{0,12}\]")
# A sentence ends at a run of . ! ? before a space or the end, at 。！？, or at a line's end; a point inside
# a marker or a number ([S1.2], 3.5) does not end it. The look-behind makes a long run of points cost what
# its length does.
_END = re.compile(r"(?<![.!?])[.!?]++(?=\s|$)|[。！？]++|\n")
# Code, where a marker is not a citation (the interface draws it as code): fenced blocks and inline spans.
_LEADING = re.compile(r"^(?:[\s|]*\[S[^\]\n]{0,12}\])+")
_EDGES = re.compile(r"^[\s|]+|[\s|]+$")  # a table row's cell bars and the space around a sentence
_CODE = re.compile(r"```.*?```|`[^`\n]+`", re.DOTALL)
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


def missing(numbered, *, mode, route, index="ready", cut=False):
    """The conflicts and missing information section's records for this evidence. index is the search
    index's state when it was searched: one being built or unavailable searched nothing, which is not the
    library holding no evidence. cut: passages matched, but the model's window left no room for any."""
    notes = []
    if route in ("literature", "memory_literature") and cut:
        notes.append("Passages in the researcher's library matched this question, but none fits this model's context "
                     "window, so none is included. Say that the evidence could not be included with this model; do not "
                     "say the library holds no evidence, and do not answer from general knowledge as if it were cited.")
    elif route in ("literature", "memory_literature") and index in ("building", "unavailable"):
        notes.append("The library's search index is being built or is unavailable, so the researcher's papers could "
                     "not be searched for this question. Say so; do not say the library holds no evidence, and do not "
                     "answer from general knowledge as if it were cited.")
    elif route in ("literature", "memory_literature") and not numbered:
        notes.append("No passage in the researcher's library matched this question. Say that the library holds no "
                     "evidence for it; do not answer from general knowledge as if it were cited.")
    if mode == "keyword_only":
        notes.append("Meaning search was unavailable, so the evidence came from keyword search only.")
    if route in ("memory", "find_papers", "memory_literature"):
        notes.append("Earlier decisions, project state and new-paper search are not available yet; if the question "
                     "depends on them, say they could not be retrieved rather than inventing them.")
    return notes


def sentences(text):
    """(start offset, sentence) for each sentence of an answer, as written."""
    out, start = [], 0
    for m in _END.finditer(text):
        out.append((start, text[start:m.end()]))
        start = m.end()
    out.append((start, text[start:]))
    return [(at, sentence) for at, sentence in out if sentence.strip()]


def markers(text):
    """[(offset, marker, (source, passage) or None)] for every citation-like marker outside code, in order."""
    code = [m.span() for m in _CODE.finditer(text)]
    found = {m.start(): (m.start(), m.group(), (int(m[1]), int(m[2]))) for m in CITATION.finditer(text)}
    for m in _MALFORMED.finditer(text):
        found.setdefault(m.start(), (m.start(), m.group(), None))
    return [found[k] for k in sorted(found) if not any(a <= k < b for a, b in code)]


def _sentence_of(spans, text, offset):
    """The sentence a marker at offset cites: the one it sits in, or, when only markers (or a table's cell
    bars) come before it in that one ("Wages rose. [S1.1]", "工资上涨。[S1.1]", "| Wages rose. | [S1.1] |"),
    the sentence before, as the interface underlines it (frontend/src/markdown.js)."""
    for i, (start, sentence) in enumerate(spans):
        if start <= offset < start + len(sentence):
            if i and not _EDGES.sub("", _MALFORMED.sub("", text[start:offset])):
                sentence = spans[i - 1][1]
            return _EDGES.sub("", _LEADING.sub("", sentence))  # without the markers that cite the sentence before it
    return ""


def check_existence(conn, project_id, text, numbered):
    """The answer's citations, each with its existence, read in the transaction given: a list of dicts
    for citation rows, each with the sentence it sits in. A citation exists while the paper it names still
    reads its passage (still_read): a passage another paper also reads does not keep a deleted paper's
    citation. A citation whose paper was deleted keeps no quote of its text."""
    spans = sentences(text)
    cited = [n for _, _, n in markers(text) if n in numbered]
    current = still_read(conn, project_id, {(numbered[n]["passage_id"], numbered[n]["material_id"]) for n in cited})
    by_material = {}
    out = []
    for offset, marker, number in markers(text):
        sentence = _sentence_of(spans, text, offset)
        row = {"marker": marker, "offset": offset, "sentence": sentence, "number": number}
        record = numbered.get(number) if number else None
        if record is None:
            out.append({**row, "existence": "not_found"})
            continue
        quote = record["text"][:QUOTE_CHARS]
        found = {"material_id": record["material_id"], "material_version_id": record["version_id"],
                 "passage_id": record["passage_id"], "quote": quote, "page": record.get("page"),
                 "evidence_type": record["evidence_type"], "retraction": record.get("retraction"),
                 "judged_text": record["text"]}  # what the answer was given, which the judge is shown too
        if (record["passage_id"], record["material_id"]) in current:
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
            # the judge is shown what the passage says now, never the old text (a verdict belongs to the text judged)
            out.append({**row, **found, "passage_id": holder, "material_version_id": r[7], "page": r[5],
                        "judged_text": r[4], "existence": "ok"})
        else:  # its paper or passage may be gone: the row keeps only what still exists (foreign keys), no quote
            kept = {k: v for k, v, table in (("material_id", found["material_id"], "materials"),
                                             ("material_version_id", found["material_version_id"], "material_versions"),
                                             ("passage_id", found["passage_id"], "passages"))
                    if conn.execute(f"SELECT 1 FROM {table} WHERE id = ?", (v,)).fetchone()}
            kept = kept if "material_id" in kept else {}  # a deleted paper's citation keeps no link to the text
            out.append({**row, **found, "material_id": None, "material_version_id": None, "passage_id": None, **kept,
                        # its quote only while its paper is there, so a later reading holding it can resolve it
                        **({} if kept else {"quote": None, "sentence": None}), "existence": "source_removed"})
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
    "You check citations in a research assistant's answer. The passages come first, each with its label; then "
    "numbered pairs, each a sentence and the label of the passage it cites. For each pair, say whether that "
    "passage supports the sentence: supported (the passage states what the sentence claims), partly (it supports "
    "some of it, or with conditions the sentence leaves out) or unsupported. Judge only from the passage. Reply "
    "with one line per pair, exactly in the form `N: verdict`, and nothing else."
)
# The judge's share reserved with an answer (Harold, decided 2026-10-21, option C): the judge cost of a
# typical answer's check, one call of judge_pairs pairs over every passage the request holds, each sentence
# at TYPICAL_SENTENCE counted units (about a 40-word sentence, counted conservatively). It covers an
# ordinary answer's check; a check that needs more draws on the answer's unspent reservation, then on the
# budget's room (runs.Harness._reserve), and only a call neither covers is left unchecked, for budget, said.
TYPICAL_SENTENCE = 120
_PASSAGE_FRAME = count("Passage P9999: ") + 2
_PAIR_FRAME = count("Pair 9999\nSentence: \nPassage: P9999") + 2


def judge_request(batch, texts):
    """The judge's request text for a batch of (sentence, passage id) pairs: each passage the batch cites once,
    labeled, then the pairs, each sentence as delivered (section 6.2: the wording actually delivered); texts
    maps a passage id to its text."""
    labels = {}
    for _, pid in batch:
        labels.setdefault(pid, f"P{len(labels) + 1}")
    parts = [f"Passage {label}: {texts[pid]}" for pid, label in labels.items()]
    parts += [f"Pair {i}\nSentence: {sentence}\nPassage: {labels[pid]}"
              for i, (sentence, pid) in enumerate(batch, start=1)]
    return "\n\n".join(parts)


def typical_judge_input(passages, pairs):
    """The counted input of a typical judge call for an answer whose evidence is these passage texts: the
    rules, every passage once with its label, `pairs` sentences of TYPICAL_SENTENCE units with their framing,
    and the messages' framing (the share's rule, above)."""
    return (count(JUDGE_RULES) + sum(count(text) + _PASSAGE_FRAME for text in passages)
            + pairs * (TYPICAL_SENTENCE + _PAIR_FRAME) + 2 * MESSAGE_FRAMING + REPLY_FRAMING)


def parse_verdicts(reply, count):
    """{pair number: verdict} from the judge's reply; a pair it does not answer clearly is left out."""
    found = {}
    for line in (reply or "").splitlines():
        m = re.match(r"\s*(?:pair\s*)?(\d+)\s*[:.)-]\s*(supported|partly|unsupported)\b", line, re.IGNORECASE)
        if m and 1 <= int(m[1]) <= count:
            found.setdefault(int(m[1]), m[2].lower())
    return found


def sentence_key(sentence):
    """What a citation check stores to bind a verdict to its sentence: the sentence's SHA-256, never the text
    (backend/provenance.py: rows and events that cannot lose text when its source is deleted never hold it)."""
    return hashlib.sha256((sentence or "").encode("utf-8")).hexdigest()


def batches(items, size):
    return [items[i:i + size] for i in range(0, len(items), size)]


def apply_verdicts(checked, verdicts):
    """Each citation's support, from its (sentence, passage) pair's verdict, or unchecked."""
    return [{**c, "support": verdicts.get((c["sentence"], c.get("passage_id")), "unchecked")
             if c["existence"] == "ok" else "unchecked"} for c in checked]


def write_rows(conn, run_id, checked, checked_at):
    """The answer's citation rows, each under its id (c["id"]), in the primary commit."""
    for c in checked:
        conn.execute(
            "INSERT INTO citations (id, owner_kind, owner_id, material_id, material_version_id, passage_id, quote,"
            " context, page, evidence_type, existence, support, support_run_id, checked_at)"
            " VALUES (?, 'answer', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (c["id"], run_id, c.get("material_id"), c.get("material_version_id"), c.get("passage_id"), c.get("quote"),
             json.dumps({"marker": c["marker"], "offset": c["offset"], "sentence": c["sentence"],
                         "retraction": c.get("retraction")}),
             c.get("page"), c.get("evidence_type"), c["existence"], c.get("support", "unchecked"),
             run_id if c.get("support", "unchecked") != "unchecked" else None, checked_at))
