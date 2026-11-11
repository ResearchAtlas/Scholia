"""The agent loop's tools (slice-1 spec sections 6.2, 6.3 and 6.6; S1-19c).

Every turn on a route whose endpoints all take tool calls is an agent run (backend/runs.py drives the
loop, Harness._agent_step): the model answers, or calls tools, which the harness checks against the
project's `[tools]` rules (deny, then ask, then allow), runs, and answers in the turn's chain. The tools
S1-19 has:

- Read the library: `list_materials`, `search_passages` (the retrieval pipeline, backend/pipeline.py, with
  its access checks) and `read_passage` (the September `read_material` contract: whole passages of one
  paper in order up to the read bound, or one passage by its ordinal; what is left out is listed).
- Pipeline: `answer_from_library`, the retrieval pipeline and the support check whole inside one tool.
- Agent control: `use_skill`, `read_skill_file` (backend/skills.py), `ask` (the shared confirmation,
  backend/asks.py, kind `choice` only) and `open_place` (one place from a fixed list; it only opens it).
- S1-21a: `read_project_state` and `read_memory` (reading the library's class) and `update_progress` (an agent's note in
  project state, never confirmed; backend/memory.py).

Each passage a tool gives the model joins the turn's evidence (`Evidence`) under a marker, [S<source>.
<passage>], numbered after the router's pre-load, so the delivered answer's citations are checked
against every passage read in the run, and its papers are registered with the dispatch check and the
deletion links before the next dispatch (backend/provenance.py). The trace records each call by name,
its non-text arguments and its result's ids and counts, never the text (section 1.5, provenance).
"""

import asyncio
import hashlib
import itertools
import json

from fastapi import APIRouter

from backend import citations, compaction, memory, skills
from backend.settings import visible
from backend.tools.registry import ToolRegistry
from backend.tools.router import failed
from backend.tools.types import ToolResult

REGISTRY = ToolRegistry()
router = APIRouter()
# open_place's fixed list: the Library panel and each Settings page (scope notes, reading 11)
PLACES = ("library", "settings.general", "settings.providers", "settings.subagents", "settings.project",
          "settings.advanced")
READ_CHARS = 50_000  # the read tool's bound (section 13); its output is previewed within 8,000 (tools/router.py)
LIST_LIMIT = 500  # papers list_materials reads, before the preview bound
MAX_SELECTED = 50  # papers a tool call may restrict itself to, as a message may
ANSWERED_POLL_SECONDS = 2  # how often a waiting ask looks again without being woken

_IDS = {"type": "array", "items": {"type": "string", "maxLength": 64}, "maxItems": MAX_SELECTED}


class Evidence:
    """The turn's evidence: {(source, passage): record}, the router's pre-load first, then every passage a
    tool gave the model, each paper keeping its source number and each passage its marker once given. S1-21b: pending
    holds passages numbered in a paged output past its preview (read_passage), read in the run only once a page showing
    their marker is served (read_output), so their markers are never given to another passage meanwhile. Fix batch 4:
    an index of the markers given (by passage and paper), each paper's source and each source's last passage number,
    so numbering a passage, or sizing one before it is numbered (keys_for), costs the same however much the turn has
    read; it is built whole only when markers are taken back (keep)."""

    def __init__(self, numbered, pending=None):
        self.numbered = dict(numbered)
        self.pending = dict(pending or {})
        self.revealed = set()  # pending keys a later tool's add gave out (keep can return them to pending)
        self._index()

    def _index(self):
        self._keys, self._source, self._last, self._top = {}, {}, {}, 0
        for key, record in {**self.numbered, **self.pending}.items():
            self._note(key, record)

    def _note(self, key, record):
        self._keys[(record["passage_id"], record["material_id"])] = key
        self._source.setdefault(record["material_id"], key[0])  # its first source, as the evidence lists it
        self._last[key[0]] = max(self._last.get(key[0], 0), key[1])
        self._top = max(self._top, key[0])

    def keys_for(self, records):
        """The markers add(records) would give them, one at a time, numbering nothing: what read_passage sizes its
        candidates by before it numbers those it keeps."""
        keys, sources, last, top = {}, {}, {}, self._top
        for record in records:
            ident = (record["passage_id"], record["material_id"])
            key = self._keys.get(ident) or keys.get(ident)
            if key is None:
                source = self._source.get(record["material_id"]) or sources.get(record["material_id"])
                if source is None:
                    top += 1
                    source = sources[record["material_id"]] = top
                last[source] = max(self._last.get(source, 0), last.get(source, 0)) + 1
                key = keys[ident] = (source, last[source])
            yield key

    def add(self, records, shown=True):
        """The markers of records (given in order), numbering those not yet in the evidence (pending ones unless shown)."""
        keys = []
        for record in records:
            key = self._keys.get((record["passage_id"], record["material_id"]))
            if key is None:
                [key] = self.keys_for([record])
                (self.numbered if shown else self.pending)[key] = record
                self._note(key, record)
            elif shown and key in self.pending:
                self.show([key])
                self.revealed.add(key)
            keys.append(key)
        return keys

    def show(self, keys):
        """Pending passages a page now shows the model: read in the run from here."""
        for key in keys:
            if key in self.pending:
                self.numbered[key] = self.pending.pop(key)

    def record(self, key):
        return self.numbered.get(key) or self.pending[key]

    def lines(self, keys):
        return citations.bundle({k: self.record(k) for k in dict.fromkeys(keys)})

    def keep(self, keys, kept, before):
        """Take back the markers of keys that were new (not in before) and are not in kept: passages retrieved but
        never shown to a model are not read in the run (their numbers given again, as before the index)."""
        taken = False
        for key in keys:
            if key not in kept and key not in before:
                record = self.numbered.pop(key, None)
                if record is not None and key in self.revealed:  # still in a paged output: its marker stays its own
                    self.pending[key] = record
                taken = taken or record is not None
        if taken:
            self._index()


class Turn:
    """What a tool call runs with: the harness, the turn's claim and context, its evidence, and emit for the
    stream. listed: the papers whose metadata a tool gave the model (registered like evidence)."""

    def __init__(self, harness, claim, evidence, emit, step=0):
        self.harness, self.claim, self.ctx, self.evidence, self.emit = harness, claim, claim.context, evidence, emit
        self.step = step
        self.listed = set()
        self.activated = set()  # the skills activated in this turn
        self.outputs = {}  # S1-21b: handle -> a paged output of this turn (its run_bodies slot, length, pending markers)
        self.segments = {}  # call id -> [(character offset, pending passage key)] of an output past its preview

    async def read(self, fn):
        return await asyncio.to_thread(self.harness.db.read, fn)


def catalog(loaded):
    """The SKILL CATALOG section's records: each enabled skill's name and description."""
    return [f"- {s.name}: {s.description}" for s in loaded.values()]


def active(loaded, names):
    """The ACTIVE SKILLS section's records, most recently used first: each skill's body in tags naming it."""
    return [_wrapped(loaded[name]) for name in names if name in loaded]


def _wrapped(skill):
    return f'<skill name="{skill.name}">\n{skill.body.strip()}\n</skill>'


def traced_arguments(arguments):
    """A call's arguments as the trace keeps them: an ordinal (a number) and a place from the fixed list as given,
    every other value by its length only, since the model may put any text in any argument, a passage's included
    (backend/provenance.py: an append-only event never holds text drawn from a paper). The ids a call resolved are in
    its tool_result's meta, once validated."""
    def kept(key, value):
        if (key == "ordinal" and type(value) is int) or (key == "place" and value in PLACES):
            return value
        return {"chars": len(value) if isinstance(value, str) else len(json.dumps(value, ensure_ascii=False))}
    return {k if k in _ARGUMENT_NAMES else "other": kept(k, v) for k, v in arguments.items()}


async def _selection(turn, ids):
    """ids if every one is a paper of this project, else None."""
    if not ids:
        return []
    ids = list(dict.fromkeys(ids))
    found = await turn.read(lambda conn: {m for (m,) in conn.execute(
        "SELECT id FROM materials WHERE project_id = ? AND id IN (SELECT value FROM json_each(?))",
        (turn.ctx["project_id"], json.dumps(ids)))})
    return ids if found == set(ids) else None


# Read the library


@REGISTRY.register(
    "list_materials",
    "List the papers in the researcher's library for this project: each paper's id, title, year, what kind of "
    "record it is (full text, abstract or metadata only) and whether it is retracted.",
    {"type": "object", "properties": {}, "additionalProperties": False},
    read_only=True, repeat_safe=True, network=False, output_chars=8000, tool_class="read_library", spends=False)
async def list_materials(call, turn):
    rows = await turn.read(lambda conn: conn.execute(
        "SELECT m.id, m.title, json_extract(m.csl, '$.issued.\"date-parts\"[0][0]'), coalesce(m.evidence_type, 'full_text'),"
        " m.retraction, count(*) OVER () FROM materials m WHERE m.project_id = ? ORDER BY m.created_at, m.id LIMIT ?",
        (turn.ctx["project_id"], LIST_LIMIT)).fetchall())  # each row also counts every paper, past the limit too
    if not rows:
        return ToolResult(id=call.id, ok=True, text="The library has no papers yet.", meta={"materials": []})
    items = [(line, line[:LABEL_CHARS], "paper", material) for material, title, year, kind, retraction, _ in rows
             for line in [" | ".join(str(x) for x in (material, title or "(untitled)", year or "n.d.", kind.replace(
                 "_", " "), "RETRACTED" if retraction == "retracted" else None) if x)]]
    more = rows[0][5] - len(rows)
    text, shown, _ = _paged(turn, call.id, items, "\n", tail=f"\n({more} more papers not listed.)" if more > 0 else "")
    return ToolResult(id=call.id, ok=True, text=text, meta={"materials": [v for *_, v in shown]})


@REGISTRY.register(
    "search_passages",
    "Search the passages of the researcher's papers for a query (keyword and meaning search). Returns up to 8 "
    "passages, each with a citation marker such as [S2.1] to cite it with. material_ids restricts the search to "
    "those papers.",
    {"type": "object", "properties": {"query": {"type": "string", "maxLength": 2000}, "material_ids": _IDS},
     "required": ["query"], "additionalProperties": False},
    read_only=True, repeat_safe=True, network=False, output_chars=8000, tool_class="read_library", spends=False)
async def search_passages(call, turn):
    from backend import pipeline, search  # search imports backend.runs, which imports this module
    query = visible(call.arguments["query"])
    if query is None:
        return failed(call.id, "validation_error", "The query is empty.")
    selection = await _selection(turn, call.arguments.get("material_ids"))
    if selection is None:
        return failed(call.id, "access_denied", "A paper named is not in this project.")
    selection = selection or turn.ctx["selection"]  # the papers the message selected narrow it too (F4)
    state = search._STATES.get(turn.harness)
    if state is None:
        return failed(call.id, "tool_error", "The library's search index is unavailable.")
    # B40: a selection is read directly only when its passages fit this result's preview whole; else searched within
    found = await pipeline.evidence(state, turn.ctx["project_id"], query, materials=selection or None,
                                    whole=lambda records: _preview_fits(citations.bundle(citations.sources(records))))
    await turn.harness._trace_retrieval(turn.claim, found, call.id)
    keys = list(dict.fromkeys(turn.evidence.add(found["records"], shown=False)))
    if not keys:
        return ToolResult(id=call.id, ok=True, text="No passage matched.", meta={"passages": [], "mode": found["mode"]})
    # S1-21b: whole passages, best first; past the preview, kept and paged, each read once a page shows its marker
    head = "(Meaning search was unavailable: these come from keyword search only.)\n\n" \
        if found["mode"] == "keyword_only" else ""
    text, _, read = _paged(turn, call.id, _passage_items(turn, keys), "\n\n", head=head)
    return ToolResult(id=call.id, ok=True, text=text, meta={"passages": read, "mode": found["mode"]})


@REGISTRY.register(
    "read_passage",
    "Read one paper's passages in order, as many whole passages as fit, each with a citation marker; or, with "
    "ordinal, the one passage at that position. The passages left out are listed by ordinal.",
    {"type": "object", "properties": {"material_id": {"type": "string", "maxLength": 64},
                                      "ordinal": {"type": "integer", "minimum": 0, "maximum": 1_000_000},
                                      "version_id": {"type": "string", "maxLength": 64}},
     "required": ["material_id"], "additionalProperties": False},
    read_only=True, repeat_safe=True, network=False, output_chars=READ_CHARS, tool_class="read_library", spends=False)
async def read_passage(call, turn):
    from backend.pipeline import _records
    from backend.search_index import readings
    material, ordinal = call.arguments["material_id"], call.arguments.get("ordinal")

    def read(conn):
        rows = sorted(readings(conn, turn.ctx["project_id"], references=False, materials=[material]).items(),
                      key=lambda item: item[1][6])
        return rows, (_records(conn, rows) if rows else [])
    rows, records = await turn.read(read)
    if not rows:  # not this project's, deleted, not read yet, or a record with no text (an abstract, metadata)
        return failed(call.id, "not_found", "That paper has no readable text in this project.")
    if call.arguments.get("version_id") not in (None, rows[0][1][7]):
        return failed(call.id, "validation_error", "That paper's text changed; read its current version.")
    if ordinal is not None:
        records = [r for r in records if r["ordinal"] == ordinal]
        if not records:
            return failed(call.id, "not_found", "That paper has no passage at that ordinal.")
    # Whole passages in order within the read bound (S1-21b: past the 8,000-character preview, paged with read_output),
    # counted as they are emitted: each passage's line with its marker, title and facts, and the separators between them
    # (fix batch 3: the title repeats on every passage); when some are left out, their note, at most NOTE_MAX characters,
    # is reserved too (fix batch 4). The candidates are sized one at a time and only until the bound is passed, so the
    # work follows what is kept, never the whole paper; the probe numbers them as the turn will. A first passage longer
    # than the bound alone is cut at it, said so.
    sizes, used = [], 0
    for record, key in zip(records, turn.evidence.keys_for(records)):  # numbered as the turn will number them
        size = len(citations.bundle({key: record})[0]) + (2 if sizes else 0)
        if used + size > READ_CHARS:
            break
        sizes.append(size)
        used += size
    count = len(sizes)
    if count < len(records):  # some are left out: room for their note
        while count > 1 and sum(sizes[:count]) > READ_CHARS - NOTE_MAX:
            count -= 1
        count = max(count, 1)
    kept = records[:count]
    keys = list(dict.fromkeys(turn.evidence.add(kept, shown=False)))
    tail = _left_out(records, count)
    items = _passage_items(turn, keys)
    room = READ_CHARS - len(tail)
    if len(items[0][0]) > room:  # one passage longer than the bound: cut at it, said so (the note under CUT_NOTE)
        items[0] = (items[0][0][:room - CUT_NOTE]
                    + f"\n(Cut at {room - CUT_NOTE} characters: this passage is longer than the read bound.)",
                    *items[0][1:])
    text, _, read = _paged(turn, call.id, items, "\n\n", tail=tail)
    return ToolResult(id=call.id, ok=True, text=text, meta={
        "passages": read, "version_id": rows[0][1][7], "left_out": len(records) - count})


PREVIEW_ROOM = 7000  # characters of whole passages a selection read directly must fit (B40), within the 8,000 preview
NOTE_RANGES = 400  # characters of left-out ordinal ranges read_passage's note lists before it says "and more"
NOTE_MAX = NOTE_RANGES + 120  # the most characters that note takes (_left_out)
CUT_NOTE = 80  # room kept for the note on a passage cut at the read bound


def _left_out(records, count):
    """read_passage's note of the passages after the first count that it leaves out: how many, and their ordinals as
    ranges, at most NOTE_RANGES characters of them, then "and more" (fix batch 4: gaps left by reference passages made
    the list as long as the paper). At most NOTE_MAX characters; its work stops with the list."""
    left = len(records) - count
    if left <= 0:
        return ""
    listed, used, more = [], 0, False
    for part in _ranges_of(r["ordinal"] for r in itertools.islice(records, count, None)):
        if used + len(part) + 2 > NOTE_RANGES:
            more = True
            break
        listed.append(part)
        used += len(part) + 2
    return (f"\n\n({left} passage{'s' if left != 1 else ''} left out, by ordinal: {', '.join(listed)}"
            f"{', and more' if more else ''}. Read one with its ordinal.)")


def _ranges_of(numbers):
    """Runs of consecutive numbers as "a" or "a-b", one at a time."""
    start = previous = None
    for n in numbers:
        if start is not None and n == previous + 1:
            previous = n
            continue
        if start is not None:
            yield str(start) if start == previous else f"{start}-{previous}"
        start = previous = n
    if start is not None:
        yield str(start) if start == previous else f"{start}-{previous}"
LABEL_CHARS = 60  # S1-21b: a listed paper or memory record is shown once a page holds this much of its line (or all)


def _passage_items(turn, keys):
    return [(line, f"[S{key[0]}.{key[1]}]", "passage", key) for key, line in zip(keys, turn.evidence.lines(keys))]


def _paged(turn, call_id, items, sep, head="", tail=""):
    """S1-21b (section 8, Paging): a result's text of items [(line, label, kind, value)], kept as the call's segments
    [(start, length, label, kind, value)] for read_output (the harness keeps the whole text and pages it), and what the
    preview shows of them given to the run now (reveal). Returns (the text, the segments the preview shows any of)."""
    at, segments = len(head), []
    for line, label, kind, value in items:
        segments.append((at, len(line), label, kind, value))
        at += len(line) + len(sep)
    turn.segments[call_id] = segments
    text = head + sep.join(line for line, *_ in items) + tail
    # what the model is shown now: all of it, or a preview's part (the harness pages the rest, compaction.preview)
    return (text, *reveal(turn, segments, 0, len(text) if len(text) <= compaction.PREVIEW
                          else compaction.shown_of(compaction.PREVIEW)))


def reveal(turn, segments, offset, size=compaction.PREVIEW, served=()):
    """What a page from offset (the preview: 0) of size characters gives the run of the items it shows any character of,
    so nothing it shows is sent unregistered (fix batch 1 review, P1-2): a paper listed, its title in the request; a
    memory record held by the request; a passage's paper registered, and the passage read in the run (citable) only once
    this turn's served pages of the output, this one and those served before it (served: [(start, end)]), cover every
    character of its marker (fix batch 4, P9: a page may start inside a marker). Returns (the segments it shows, the
    passages it reads, by passage id)."""
    pages = [*served, (offset, offset + size)]
    shown = [s for s in segments if compaction.shows(s[0], s[1], offset, size)]
    complete = [s for s in shown if s[3] == "passage" and compaction.covered(pages, s[0], s[0] + len(s[2]))]
    for segment in shown:
        *_, kind, value = segment
        if kind == "passage":
            if segment in complete:
                turn.evidence.show([value])
            record = turn.evidence.numbered.get(value) or turn.evidence.pending.get(value)
            if record is not None:
                turn.listed.add(record["material_id"])
        elif kind == "paper":
            turn.listed.add(value)
    held = [v for _, _, _, k, v in shown if k == "memory"]
    if held:
        _remembered(turn, [record for record, _ in held], {m for _, materials in held for m in materials})
    return shown, [turn.evidence.numbered[v]["passage_id"] for *_, v in complete]
_MARKER_SLACK = 8  # a marker the turn numbers ([S12.30]) may be longer than one numbered from 1


def _preview_fits(lines):
    return sum(len(line) + 2 + _MARKER_SLACK for line in lines) <= PREVIEW_ROOM


def _ranges(numbers):
    out, start = [], None
    for i, n in enumerate(numbers):
        if start is None:
            start = n
        if i + 1 == len(numbers) or numbers[i + 1] != n + 1:
            out.append(str(start) if start == n else f"{start}-{n}")
            start = None
    return ", ".join(out)


# Pipelines


@REGISTRY.register(
    "answer_from_library",
    "Answer a question from the researcher's papers: retrieves the best passages, writes a cited answer and checks "
    "each citation against its passage. Returns the answer with its citation markers and each checked sentence's "
    "verdict. Deliver its sentences unchanged where you can: changed wording is checked again.",
    {"type": "object", "properties": {"question": {"type": "string", "maxLength": 4000}, "material_ids": _IDS},
     "required": ["question"], "additionalProperties": False},
    read_only=True, repeat_safe=True, network=True, output_chars=8000, tool_class="pipeline", spends=True)
async def answer_from_library(call, turn):
    question = visible(call.arguments["question"])
    if question is None:
        return failed(call.id, "validation_error", "The question is empty.")
    selection = await _selection(turn, call.arguments.get("material_ids"))
    if selection is None:
        return failed(call.id, "access_denied", "A paper named is not in this project.")
    return await turn.harness._answer_from_library(turn, call.id, question, selection or turn.ctx["selection"])


# Agent control


@REGISTRY.register(
    "use_skill",
    "Activate a skill from the SKILL CATALOG by its name. Returns the skill's instructions and the ids of its files, "
    "which read_skill_file reads.",
    {"type": "object", "properties": {"name": {"type": "string", "maxLength": 100}}, "required": ["name"],
     "additionalProperties": False},
    read_only=True, repeat_safe=True, network=False, output_chars=8000, tool_class="agent_control", spends=False)
async def use_skill(call, turn):
    loaded, _ = await asyncio.to_thread(skills.load)
    skill = loaded.get(call.arguments["name"])
    if skill is None:
        return failed(call.id, "validation_error", "No enabled skill has that name; use a name from the SKILL CATALOG.")
    # loaded once per conversation: in this turn's chain, or in what ACTIVE SKILLS still carried in the last request
    # (one cut for capacity is activated again, section 8)
    files = "\n".join(f"- {file_id}: {path}" for file_id, path, _ in skill.files)
    listed = f"\n\nFiles (read with read_skill_file):\n{files}" if files else ""
    if _active(turn, skill.name):
        # its body is already in the request; its files' ids are given again, since ACTIVE SKILLS holds the body only
        return ToolResult(id=call.id, ok=True, text=f"The {skill.name} skill is already active; its instructions are "
                                                    f"above.{listed}", meta={"skill": skill.name, "already_active": True,
                                                                             "files": [f[0] for f in skill.files]})
    await turn.harness._skill_activated(turn.claim, skill, turn.step)
    turn.activated.add(skill.name)  # active once its activation is recorded, never before (a failed write leaves it not)
    turn.emit({"type": "skill", "name": skill.name})
    return ToolResult(id=call.id, ok=True, text=_wrapped(skill) + listed,
                      meta={"skill": skill.name, "files": [f[0] for f in skill.files]})


@REGISTRY.register(
    "read_skill_file",
    "Read one file of an active skill by its file id, as use_skill listed it (never a path).",
    {"type": "object", "properties": {"file_id": {"type": "string", "maxLength": 120}}, "required": ["file_id"],
     "additionalProperties": False},
    read_only=True, repeat_safe=True, network=False, output_chars=8000, tool_class="agent_control", spends=False)
async def read_skill_file(call, turn):
    # only a skill active in the conversation (enabled, and use_skill recorded its activation): spec section 7.2
    if not _active(turn, str(call.arguments["file_id"]).partition("#")[0]):
        return failed(call.id, "validation_error", "Not a file of an active skill: activate the skill with use_skill, "
                                                   "then give a file id it listed.")
    try:
        text, sha = await asyncio.to_thread(skills.read_file, call.arguments["file_id"])
    except ValueError as error:
        message = "That file changed and cannot be read." if str(error) == "changed" else \
            "Not a file id use_skill listed; give the id, not a path."
        return failed(call.id, "validation_error" if str(error) != "changed" else "tool_error", message)
    return ToolResult(id=call.id, ok=True, text=text, meta={"file_id": call.arguments["file_id"], "sha256": sha})


def _active(turn, name):
    """Whether a skill is active in the conversation: activated in this turn, or carried in ACTIVE SKILLS (which holds
    only enabled skills; one cut for capacity is not)."""
    return name in turn.activated or name in turn.ctx.get("active_kept", ())


@REGISTRY.register(
    "ask",
    "Ask the researcher a question when you need a decision only they can make. Give one to three short options; "
    "they may also type an answer. Their answer is returned to you.",
    {"type": "object", "properties": {
        "question": {"type": "string", "maxLength": 1000},
        "options": {"type": "array", "items": {"type": "string", "maxLength": 100}, "minItems": 1, "maxItems": 3}},
     "required": ["question", "options"], "additionalProperties": False},
    read_only=True, repeat_safe=False, network=False, output_chars=4000, tool_class="agent_control", spends=False)
async def ask(call, turn):
    question = visible(call.arguments["question"])
    options = [visible(o) for o in call.arguments["options"]]
    if question is None or None in options or len(set(options)) != len(options):
        return failed(call.id, "validation_error", "The question and each option must have text, and the options differ.")
    answer = await turn.harness._ask(turn, "choice", options, {}, call.id, question=question)
    if answer is None:
        return failed(call.id, "tool_error", "The question was withdrawn: the project's protection changed.")
    said = answer.get("option") if "option" in answer else answer.get("text")
    return ToolResult(id=call.id, ok=True, text=f"The researcher answered: {said}",
                      meta={"answered": "option" if "option" in answer else "text"})


@REGISTRY.register(
    "open_place",
    "Open a place in the app for the researcher: the Library panel or a Settings page. It only opens it; it never "
    "changes a setting.",
    {"type": "object", "properties": {"place": {"type": "string", "enum": list(PLACES)}}, "required": ["place"],
     "additionalProperties": False},
    read_only=True, repeat_safe=True, network=False, output_chars=200, tool_class="agent_control", spends=False)
async def open_place(call, turn):
    # its project: the window opens it only while this conversation and project are shown (frontend/src/live.js)
    turn.emit({"type": "open_place", "place": call.arguments["place"], "project_id": turn.ctx["project_id"]})
    return ToolResult(id=call.id, ok=True, text=f"Opened {call.arguments['place']} for the researcher.",
                      meta={"place": call.arguments["place"]})


# S1-21a: project state and memory (section 6.3: read_project_state and read_memory read the library's class;
# update_progress is agent control). What they give the model joins the request's context (its records and the papers
# they may carry pass the dispatch check before the next step); with memory off in the project they read and write
# nothing.

def _remembered(turn, records, materials):
    turn.ctx["memory_ids"] = sorted({*turn.ctx.get("memory_ids", ()), *records})
    turn.ctx["memory_materials"] = sorted({*turn.ctx.get("memory_materials", ()), *materials})



@REGISTRY.register(
    "read_project_state",
    "Read this project's state: its research question, method, hypotheses, criteria and constraints as recorded, "
    "what has been done, and your earlier notes (open questions and next steps).",
    {"type": "object", "properties": {}, "additionalProperties": False},
    read_only=True, repeat_safe=True, network=False, output_chars=8000, tool_class="read_library", spends=False)
async def read_project_state(call, turn):
    if not await turn.harness.memory_on(turn.ctx["project_id"]):  # the switch as it is now
        return ToolResult(id=call.id, ok=True, text="Memory is off in this project: there is no project state to read.",
                          meta={"memory": []})

    def read(conn):
        lines, ids = memory.project_state(conn, turn.ctx["project_id"])
        return lines, [(i, tuple(memory.materials_of(conn, [i]))) for i in ids]
    lines, held = await turn.read(read)
    if not lines:
        return ToolResult(id=call.id, ok=True, text="The project state is empty so far.", meta={"memory": []})
    text, shown, _ = _paged(turn, call.id, [(line, line[:LABEL_CHARS], "memory", h) for line, h in zip(lines, held)], "\n")
    return ToolResult(id=call.id, ok=True, text=text, meta={"memory": [v[0] for *_, v in shown]})


@REGISTRY.register(
    "read_memory",
    "Read what the researcher's memory holds for this project: their profile and preferences, decisions and events, "
    "up to 10 records, those most like query first when one is given. Say plainly when nothing is recorded; never "
    "invent a decision.",
    {"type": "object", "properties": {"query": {"type": "string", "maxLength": 1000}}, "additionalProperties": False},
    read_only=True, repeat_safe=True, network=False, output_chars=8000, tool_class="read_library", spends=False)
async def read_memory(call, turn):
    if not await turn.harness.memory_on(turn.ctx["project_id"]):
        return ToolResult(id=call.id, ok=True, text="Memory is off in this project: nothing is recalled.",
                          meta={"memory": []})
    query = visible(call.arguments.get("query"))

    def read(conn):
        found = memory.recall_candidates(conn, turn.ctx["project_id"], locked=turn.ctx["policy"].locked)
        if query is not None:
            found.sort(key=lambda r: -memory.likeness(query, r["content"]))
        found = found[:memory.RECALL_LIMIT]
        return [(memory.memory_line(r), (r["id"], tuple(memory.materials_of(conn, [r["id"]])))) for r in found]
    found = await turn.read(read)
    if not found:
        return ToolResult(id=call.id, ok=True, text="No memory record is recorded for this.", meta={"memory": []})
    text, shown, _ = _paged(turn, call.id, [(line, line[:LABEL_CHARS], "memory", h) for line, h in found], "\n")
    return ToolResult(id=call.id, ok=True, text=text, meta={"memory": [v[0] for *_, v in shown]})


@REGISTRY.register(
    "update_progress",
    "Add a note to this project's state for later: an open question, or the next step you suggest. It is kept as your "
    "note, never as the researcher's decision. One note per call.",
    {"type": "object", "properties": {"kind": {"type": "string", "enum": ["open_question", "next_step"]},
                                      "text": {"type": "string", "maxLength": 500}},
     "required": ["kind", "text"], "additionalProperties": False},
    read_only=False, repeat_safe=False, network=False, output_chars=500, tool_class="agent_control", spends=False)
async def update_progress(call, turn):
    text = visible(call.arguments["text"])
    if text is None:
        return failed(call.id, "validation_error", "The note is empty.")
    if not await turn.harness.memory_on(turn.ctx["project_id"]):
        return ToolResult(id=call.id, ok=True, text="Memory is off in this project: the note was not kept.",
                          meta={"memory": []})
    written = []

    def keep(conn):
        written.append(memory.note(conn, project_id=turn.ctx["project_id"], run_id=turn.claim.run_id,
                                   kind=call.arguments["kind"], text=text, materials=turn.ctx.get("context_materials") or ()))
        from backend.runs import _event  # it imports this module
        _event(conn, turn.claim.run_id, "memory_proposed", {"tool": "update_progress", "records": list(written)})
    await turn.harness._record(turn.claim, None, None, also=keep)
    _remembered(turn, written, ())  # the note's text is in the chain now: forgetting it revokes this turn (MB5)
    turn.emit({"type": "memory", "action": "noted", "count": 1})
    return ToolResult(id=call.id, ok=True, text="Noted in the project state as your note.",
                      meta={"noted": written[0], "memory": list(written)})


# S1-21b: paged outputs (section 8, Paging; backend/compaction.py)


@REGISTRY.register(
    "read_output",
    "Read more of a tool result that was cut at its preview: give the handle and the offset the result named. Returns "
    "the next 8,000 characters. Handles are valid only within this turn.",
    {"type": "object", "properties": {"handle": {"type": "string", "maxLength": 64},
                                      "offset": {"type": "integer", "minimum": 0, "maximum": 1_000_000}},
     "required": ["handle", "offset"], "additionalProperties": False},
    read_only=True, repeat_safe=True, network=False, output_chars=compaction.PREVIEW + 200, tool_class="agent_control",
    spends=False)
async def read_output(call, turn):
    handle, offset = call.arguments["handle"], call.arguments["offset"]
    kept = turn.outputs.get(handle)  # this turn's own outputs only
    if kept is None:
        return failed(call.id, "not_found", "No output of this turn has that handle.")
    if offset >= kept["length"]:
        return failed(call.id, "validation_error", f"That output has {kept['length']} characters; give a smaller offset.")
    sha = await turn.read(lambda conn: compaction.kept_output(conn, turn.claim.run_id, kept["step"], kept["number"]))
    text = None
    if sha is not None and turn.harness.content is not None:
        try:
            text = (await asyncio.to_thread(turn.harness.content.read, sha)).decode("utf-8")
        except Exception:  # missing or damaged: as if not kept
            text = None
    if text is None:  # gone with a paper or memory record whose text it held, or never kept
        return failed(call.id, "not_found", "That output is no longer kept.")
    shown, read = reveal(turn, kept["segments"], offset, compaction.PREVIEW, kept["served"])
    kept["served"].append((offset, min(offset + compaction.PREVIEW, len(text))))  # what this turn has been shown of it
    return ToolResult(id=call.id, ok=True, text=compaction.page(text, offset, handle), meta={
        "passages": read, "materials": [v for *_, k, v in shown if k == "paper"],
        "memory": [v[0] for *_, k, v in shown if k == "memory"], "page": True})


def definitions():
    return REGISTRY.to_request_format()


def traced_id(call_id):
    """A tool call's id as the trace and the tools know it: always its hash, since a provider may let the model write
    the id, and even a plain token can spell words drawn from a paper (backend/provenance.py: no model text in an
    append-only event). The provider's own id stays in the turn's chain only."""
    return "call-" + hashlib.sha256(str(call_id).encode("utf-8", "surrogatepass")).hexdigest()[:24]


def known_name(name):
    """A tool name as the trace keeps it: one the registry knows, else None (the model's text is not kept)."""
    return name if REGISTRY.get_definition(name) is not None else None


# The argument names the tools declare: any other name the model sends is recorded as "other"
_ARGUMENT_NAMES = {name for tool in REGISTRY.list_tools() for name in tool.args_schema.get("properties", {})}


@router.get("/api/skills")
async def list_skills(language: str = "en"):
    """The skill catalog (section 5): each skill's name, description in the interface language, source and
    enablement. Both first-party skills are enabled in S1-19 (per-project enablement comes with S1-22); one
    whose files do not match the manifest is listed unavailable, with why."""
    loaded, problems = await asyncio.to_thread(skills.load)
    zh = language == "zh-CN"
    return {"skills": [{"name": s.name, "description": s.zh["description"] if zh else s.description,
                        "source": "scholia.skills", "enabled": True, "status": "ok"} for s in loaded.values()]
            + [{"name": name, "description": None, "source": "scholia.skills", "enabled": False, "status": problem}
               for name, problem in problems.items()]}
