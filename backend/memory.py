"""Memory and project state (slice-1 spec F10 and section 8; ticket 31; S1-21a).

Records (`memory_records`, with `memory_sources` for deletion): types profile and preference (personal unless the
project is Private or Local only, whose records stay its own), project_state, decision and event (the project's),
and the two project-state types written by code only: `progress` (from AAB's own records: papers added and removed)
and `agent_note` (the update_progress tool). Statuses confirmed, inferred, proposed and superseded; neither progress
nor an agent note is ever confirmed (a database check). An edit, update or correction writes a new version that
names the one it replaces (`supersedes_id`); the old one is superseded and leaves recall. Forget deletes through the
deletion service (backend/db/deletion.py), with the versions it replaced.

Where text comes from (backend/provenance.py's rule): a record that may carry text of the papers a turn's answer drew
on (an extraction's, a saved answer, an agent's note) names each of those papers as a `material` source, and goes
with any one of them; a record goes with its last turn or run too. Recall leaves out a record whose papers the
request may not carry.

The researcher's own words count (section 8, Words that count): explicit commands ("remember", "save", "update",
"correct", "forget"; 记住, 保存, 更新, 更正, 忘记) are recognized by rules (`command`) and applied by code before any
model call (backend/runs.py); a bare "save this" or "forget this" asks first (the shared confirmation), and only the
researcher's answer saves or forgets. Extraction (a background run after each answer, backend/runs.py) proposes; code
caps a candidate at proposed unless the words it rests on are in the researcher's own message (`cap`), and replaces
an existing record only under section 8's three conditions (`apply_extraction`).
"""

import json
import re
import unicodedata
from datetime import UTC, datetime, timedelta

from backend.db import new_id, utc_now

TYPES = ("profile", "project_state", "decision", "event", "preference", "progress", "agent_note")
PERSONAL_TYPES = ("profile", "preference")
RECALLED = ("profile", "preference", "decision", "event")  # USER MEMORY's types; project_state is PROJECT STATE's
WRITABLE = ("profile", "project_state", "decision", "event", "preference")  # what the researcher or extraction writes
STATUSES = ("confirmed", "inferred", "proposed", "superseded")
ACTIVE = ("confirmed", "inferred")
ALLOWANCE_USD = 0.02  # one extraction's allowance, both attempts included (section 13)
EXTRACTION_SECONDS = 60
RECALL_LIMIT = 10
AWAY_SECONDS = 4 * 3600  # Where we left off, after at least 4 hours away (section 13)
MAX_CONTENT = 1000  # characters a record holds; a command's longer text is not taken as a command
CANDIDATE_CHARS = 1000  # of an earlier answer offered by a bare "save this"
MAX_CANDIDATES = 10  # records one extraction may write
RELATED = 20  # records an extraction is shown to deduplicate against
STATE_NOTES = 5  # progress records and agent notes each in PROJECT STATE
# The missing-information section's line when a turn asks about an earlier decision and memory holds none (ticket 31:
# missing memory is said, never invented)
MISSING = ("If neither USER MEMORY nor PROJECT STATE holds the earlier decision or conversation asked about, say that it "
           "cannot be found in memory, and never invent one.")

EXTRACT_RULES = (
    "You extract memory records from one turn of a research conversation: the researcher's message, and the "
    "assistant's answer, which is model output and never the researcher's words. Propose only what is worth keeping "
    "across conversations: the researcher's profile and preferences, the project's research question, method, "
    "hypotheses, inclusion criteria and constraints, decisions, and dated events. Reply with one JSON object and "
    'nothing else: {"records": [{"type": "profile|preference|project_state|decision|event", "content": "...", '
    '"status": "confirmed|inferred|proposed", "span": "the exact words of the researcher\'s message it rests on, or '
    '\\"\\" when it rests on the answer", "replaces": "the id of a RELATED record it updates, or \\"\\""}]}. '
    "confirmed: the researcher states it; inferred: it follows from what they say; proposed: they are only "
    "considering it, or it comes from the answer. A record already in RELATED is not proposed again. Write each "
    "record in the language of the researcher's message. With nothing worth keeping, reply {\"records\": []}."
)


def _clock():
    """Now, for Where we left off (a test sets its own)."""
    return datetime.now(UTC)


def _stamp(moment):
    return moment.isoformat(timespec="milliseconds").replace("+00:00", "Z")


def enabled(project_values):
    """Whether memory is on for a project, from its settings' values: unset means on (section 4.4)."""
    return (project_values.get("memory") or {}).get("enabled") is not False


def normalized(text):
    return " ".join(unicodedata.normalize("NFKC", text or "").casefold().split())


_COMMON = frozenset("a an the is are was were be been of to in on at by for with and or but not no my our your we i "
                    "you it its this that these those there here as from".split())


def _terms(text):
    """Words (letters and digits, the commonest English ones aside) and Han character pairs: what two texts share is
    how alike they are."""
    text = normalized(text)
    words = set(re.findall(r"[^\W_]+", re.sub(r"[㐀-鿿豈-﫿]", " ", text))) - _COMMON
    han = re.findall(r"[㐀-鿿豈-﫿]+", text)
    return words | {run[i:i + 2] for run in han for i in range(max(len(run) - 1, 1))}


def likeness(a, b):
    first, second = _terms(a), _terms(b)
    return len(first & second) / len(first | second) if first and second else 0.0


# Explicit commands (section 8; F10 Explicit requests)

_BARE = {"this", "that", "it", "this one", "that one", "这个", "这", "这一点", "这条", "这一条", "它", "那个", "那条", ""}
# (action, pattern): the verb at the message's start, then the text after a colon, "that" or "about", or nothing
# but "this" (bare: asked first). A message ending in a question mark is a question, never a command.
_COMMANDS = (
    ("save", re.compile(r"^(?:please\s+)?(?:remember(?:\s*[:：]\s*|\s+that\s+)|save\s*[:：]\s*"
                        r"|(?:remember|save)\s+(?=(?:this|that|it)\W*$))(?P<text>.*)$", re.I | re.S)),
    ("correct", re.compile(r"^(?:please\s+)?correct(?:ion)?(?:\s*[:：]\s*|\s+(?=(?:this|that|it)\W*$))(?P<text>.*)$",
                           re.I | re.S)),
    ("update", re.compile(r"^(?:please\s+)?update(?:\s*[:：]\s*|\s+(?=(?:this|that|it)\W*$))(?P<text>.*)$", re.I | re.S)),
    ("forget", re.compile(r"^(?:please\s+)?forget(?:\s*[:：]\s*|\s+(?:that|about)\s+|\s+(?=(?:this|that|it)\W*$))(?P<text>.*)$",
                          re.I | re.S)),
    # Chinese as English: the verb, then a colon or comma and the text, or nothing but 这个 (asked first); never a
    # sentence that only starts with the verb (更新参考文献格式 is a request, not a command)
    ("save", re.compile(r"^请?(?:记住|保存|记下)(?:\s*[：:，,]\s*|(?=(?:这|那|它)(?:个|条|一点|一条)?[\s。.!！]*$))(?P<text>.*)$", re.S)),
    ("correct", re.compile(r"^请?(?:更正|纠正)(?:\s*[：:，,]\s*|(?=(?:这|那|它)(?:个|条|一点|一条)?[\s。.!！]*$))(?P<text>.*)$", re.S)),
    ("update", re.compile(r"^请?更新(?:\s*[：:，,]\s*|(?=(?:这|那|它)(?:个|条|一点|一条)?[\s。.!！]*$))(?P<text>.*)$", re.S)),
    ("forget", re.compile(r"^请?(?:忘记|忘掉)(?:\s*[：:，,]\s*|(?=(?:这|那|它)(?:个|条|一点|一条)?[\s。.!！]*$))(?P<text>.*)$", re.S)),
)


def command(message):
    """The explicit memory command a researcher's message makes, or None: {"action": save, update, correct or
    forget, "text": what it carries, or None for a bare "this"}. Rules only, in English and Chinese."""
    text = (message or "").strip()
    if not text or text.endswith(("?", "？")):
        return None
    for action, pattern in _COMMANDS:
        found = pattern.match(text)
        if found is None:
            continue
        rest = found.group("text").strip()
        if rest.strip(" .。!！,，:：").lower() in _BARE:
            return {"action": action, "text": None}
        if len(rest) > MAX_CONTENT:
            return None
        return {"action": action, "text": rest}
    return None


_TYPE_WORDS = (
    ("preference", re.compile(r"\bI (?:prefer|like|want you to|would like)\b|偏好|喜欢|希望你", re.I)),
    ("profile", re.compile(r"\b(?:I am|I'm|my (?:field|discipline|background|name|role|position))\b|我是|我的(?:专业|领域|背景|研究方向)", re.I)),
    ("project_state", re.compile(r"\b(?:research questions?|methods?|methodology|hypothes[ie]s|inclusion criteria|"
                                 r"exclusion criteria|constraints?|sample|design)\b|研究问题|方法|假设|纳入标准|排除标准|约束|样本|设计", re.I)),
    ("decision", re.compile(r"\b(?:we decided|decided|decision|we will|we'll|chose|chosen)\b|决定|选定|选择了", re.I)),
)


def kind_of(text):
    """The type of a record the researcher states in a command, by its words; else an event."""
    return next((kind for kind, pattern in _TYPE_WORDS if pattern.search(text or "")), "event")


def _normal(conn, project_id):
    row = conn.execute("SELECT sensitivity FROM projects WHERE id = ?", (project_id,)).fetchone()
    return row is not None and row[0] == "normal"


def personal_allowed(conn, project_id):
    """Whether a personal record may be written from project_id: only from a Normal, unlocked project (a review lock
    keeps its project Local only), so nothing of a Private or Local only project's reaches another project's request
    (scope notes reading 4; Harold's decision of 2026-10-23, point 1)."""
    return _normal(conn, project_id)


def allowed(conn, project_id, record_ids):
    """Whether every one of these records may still be in a request of project_id (section 10, at dispatch): it exists,
    is current (never superseded: an edit since it was assembled means its old text may not leave; a proposal's edit
    makes a new record, edit), and is valid there now (its scope, the request's project's lock, its papers)."""
    for record in record_ids:
        found = row(conn, record)
        if found is None or found["status"] == "superseded" or not valid(conn, project_id, found):
            return False
    return True


def scope_for(conn, project_id, kind):
    """Personal for profile and preference, unless the project is Private or Local only: its records stay its own
    (nothing from it reaches another project's request)."""
    if kind not in PERSONAL_TYPES:
        return "project"
    row = conn.execute("SELECT sensitivity FROM projects WHERE id = ?", (project_id,)).fetchone()
    return "personal" if row is not None and row[0] == "normal" else "project"


# Records


def _sensitivity(conn, project_id):
    row = conn.execute("SELECT sensitivity FROM projects WHERE id = ?", (project_id,)).fetchone()
    return row[0] if row else None


def write(conn, *, project_id, scope, kind, content, status, sources, run_id=None, replaces=None, structured=None):
    """A new record and its sources, in the caller's transaction; returns its id. replaces: an active record it
    replaces (its next version): that one is superseded."""
    record = new_id()
    now = utc_now()
    version = 1
    if replaces is not None:
        row = conn.execute("SELECT version FROM memory_records WHERE id = ?", (replaces,)).fetchone()
        version = row[0] + 1 if row else 1
        conn.execute("UPDATE memory_records SET status = 'superseded', valid_to = ?, updated_at = ? WHERE id = ?"
                     " AND status != 'superseded'", (now, now, replaces))
        conn.execute("INSERT INTO index_queue (target, target_id, project_id, op) VALUES ('memory', ?, ?, 'remove')",
                     (replaces, project_id))
    conn.execute(
        "INSERT INTO memory_records (id, scope, project_id, type, content, structured, status, valid_from, sensitivity,"
        " version, supersedes_id, created_by_run_id) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (record, scope, project_id, kind, content, json.dumps(structured) if structured else None, status, now,
         _sensitivity(conn, project_id), version, replaces, run_id))
    conn.executemany("INSERT OR IGNORE INTO memory_sources (memory_id, source_kind, source_id) VALUES (?, ?, ?)",
                     [(record, k, i) for k, i in dict.fromkeys(sources)] or [(record, "researcher", record)])
    if status in ACTIVE and kind in RECALLED:
        conn.execute("INSERT INTO index_queue (target, target_id, project_id, op) VALUES ('memory', ?, ?, 'add')",
                     (record, project_id))
    return record


def sources_of(conn, record):
    return [tuple(row) for row in conn.execute(
        "SELECT source_kind, source_id FROM memory_sources WHERE memory_id = ? ORDER BY source_kind, source_id", (record,))]


def papers(material_ids):
    return [("material", m) for m in sorted(set(material_ids or ()))]


def row(conn, record):
    found = conn.execute(f"SELECT {_COLUMNS} FROM memory_records WHERE id = ?", (record,)).fetchone()
    return _dict(conn, found) if found else None


_COLUMNS = ("id, scope, project_id, type, content, structured, status, valid_from, valid_to, version, supersedes_id,"
            " created_by_run_id, created_at, updated_at, sensitivity")


def _dict(conn, found, with_sources=True):
    record = dict(zip([c.strip() for c in _COLUMNS.split(",")], found))
    record["structured"] = json.loads(record["structured"]) if record["structured"] else {}
    if with_sources:
        record["sources"] = [{"kind": k, "id": i} for k, i in sources_of(conn, record["id"])]
    return record


def listing(conn, project_id=None, statuses=None):
    """{"personal": [...], "project": [...]}: the records the memory views show, newest first; project records of
    project_id only."""
    statuses = statuses or ("confirmed", "inferred", "proposed")
    rows = conn.execute(
        f"SELECT {_COLUMNS} FROM memory_records WHERE status IN (SELECT value FROM json_each(?))"
        " AND (scope = 'personal' OR project_id = ?) ORDER BY updated_at DESC, rowid DESC",
        (json.dumps(list(statuses)), project_id)).fetchall()
    found = {"personal": [], "project": []}
    for item in rows:
        record = _dict(conn, item)
        found[record["scope"]].append(record)
    return found


def edit(conn, record, version, *, content=None, confirm=False):
    """The researcher's change to a record at the version they saw (the memory views, Things to confirm). A pending
    proposal takes the new content and is kept (keep). An active record gets a new version that replaces it, with
    content (or the same content), confirmed; progress and agent notes stay inferred (never confirmed). Raises
    LookupError, or ValueError: "changed" when it is not at that version any more (or superseded), or "not_confirmable".
    None when a proposal is out of date (keep)."""
    old = row(conn, record)
    if old is None:
        raise LookupError(record)
    if old["version"] != version or old["status"] == "superseded":
        raise ValueError("changed")
    code_written = old["type"] in ("progress", "agent_note")
    if code_written and confirm:
        raise ValueError("not_confirmable")
    if old["status"] == "proposed":
        if content is not None and content != old["content"]:
            record = _renewed(conn, record, content)
        return keep(conn, record, None)
    structured = {k: v for k, v in old["structured"].items() if k in ("kind", "event", "count", "date")}
    return write(conn, project_id=old["project_id"], scope=old["scope"], kind=old["type"],
                 content=content if content is not None else old["content"],
                 status="inferred" if code_written else "confirmed",
                 sources=[*sources_of(conn, record), ("researcher", "views")], replaces=record,
                 structured=structured or None)


def _renewed(conn, proposal, content):
    """A pending proposal with new content, as a new record (its next version) that takes its place, its sources and
    the record it would replace: a request that held the old text names an id that no longer exists (allowed), and
    the old text is not kept (S1-21a fix batch 3, R3). Returns the new id."""
    record = new_id()
    conn.execute(
        "INSERT INTO memory_records (id, scope, project_id, type, content, structured, status, valid_from, sensitivity,"
        " version, supersedes_id, created_by_run_id, created_at) SELECT ?, scope, project_id, type, ?, structured,"
        " status, valid_from, sensitivity, version + 1, supersedes_id, created_by_run_id, created_at"
        " FROM memory_records WHERE id = ?", (record, content, proposal))
    conn.execute("UPDATE memory_sources SET memory_id = ? WHERE memory_id = ?", (record, proposal))
    conn.execute("DELETE FROM memory_records WHERE id = ?", (proposal,))
    return record


def keep(conn, proposal, version, edited=False):
    """Keep (confirm) a proposal: it replaces the record it names (supersedes_id) only if that record is still at
    the version the proposal saw; else it stays proposed and is marked out of date (section 8), in this transaction, and
    None is returned. Returns its id, or raises LookupError or ValueError("changed")."""
    found = row(conn, proposal)
    if found is None or found["status"] != "proposed":
        raise LookupError(proposal)
    if version is not None and found["version"] != version:
        raise ValueError("changed")
    target, seen = found["supersedes_id"], found["structured"].get("seen")
    now = utc_now()
    if target is not None:
        current = conn.execute("SELECT status, version FROM memory_records WHERE id = ?", (target,)).fetchone()
        if current is None or current[0] in ("superseded", "proposed") or (seen is not None and current[1] != seen):
            conn.execute("UPDATE memory_records SET structured = json_set(coalesce(structured, '{}'),"
                         " '$.out_of_date', json('true')), updated_at = ? WHERE id = ?", (now, proposal))
            return None
        conn.execute("UPDATE memory_records SET status = 'superseded', valid_to = ?, updated_at = ? WHERE id = ?",
                     (now, now, target))
        conn.execute("INSERT INTO index_queue (target, target_id, project_id, op) VALUES ('memory', ?, ?, 'remove')",
                     (target, found["project_id"]))
        conn.execute("UPDATE memory_records SET version = ? WHERE id = ?", (current[1] + 1, proposal))
    structured = {k: v for k, v in found["structured"].items() if k not in ("out_of_date", "older", "seen", "source")}
    conn.execute("UPDATE memory_records SET status = 'confirmed', structured = ?, updated_at = ? WHERE id = ?",
                 (json.dumps(structured) if structured else None, now, proposal))
    conn.execute("INSERT OR IGNORE INTO memory_sources (memory_id, source_kind, source_id) VALUES (?, 'researcher', 'views')",
                 (proposal,))
    if found["type"] in RECALLED:
        conn.execute("INSERT INTO index_queue (target, target_id, project_id, op) VALUES ('memory', ?, ?, 'add')",
                     (proposal, found["project_id"]))
    return proposal


def in_scope(conn, project_id, statuses=ACTIVE, kinds=WRITABLE):
    """The records of a project's scope (its own and the personal ones) of these statuses and types, newest first."""
    return [_dict(conn, item, with_sources=False) for item in conn.execute(
        f"SELECT {_COLUMNS} FROM memory_records WHERE status IN (SELECT value FROM json_each(?))"
        " AND type IN (SELECT value FROM json_each(?)) AND (project_id = ? AND scope = 'project' OR scope = 'personal')"
        " ORDER BY updated_at DESC, rowid DESC", (json.dumps(list(statuses)), json.dumps(list(kinds)), project_id)).fetchall()]


def matching(conn, project_id, text, limit=3, floor=0.4, statuses=ACTIVE):
    """Up to limit records of the project's scope (active, or of statuses) most like text, best first:
    [(likeness, record)]."""
    scored = [(likeness(text, r["content"]), r) for r in in_scope(conn, project_id, statuses=statuses)]
    return sorted([s for s in scored if s[0] >= floor or normalized(text) in normalized(s[1]["content"])],
                  key=lambda s: -s[0])[:limit]


def save_command(conn, *, project_id, run_id, action, text, replaces=None):
    """A command that carries its text, applied in the caller's transaction: save keeps it as the researcher's
    own words, confirmed; update and correct replace the record most like it (if any), or replaces (the one a bare
    "update this" asked about, while it is still active), with it. Returns (what it did, the record ids)."""
    kind = kind_of(text)
    scope = scope_for(conn, project_id, kind)
    if action in ("update", "correct"):
        found = [r for r in in_scope(conn, project_id) if r["id"] == replaces] if replaces else \
            [r for _, r in matching(conn, project_id, text, limit=1)]
        replaces = None
        # a personal record is replaced only from a Normal project; from another, the text stays the project's own
        if found and (found[0]["scope"] == "project" or personal_allowed(conn, project_id)):
            replaces, kind, scope = found[0]["id"], found[0]["type"], found[0]["scope"]
    if action == "save":  # a command sent again (Continue) or repeated is kept once, confirmed by these words
        same = [r for r in in_scope(conn, project_id, statuses=("confirmed", "inferred", "proposed"))
                if normalized(r["content"]) == normalized(text) and (r["scope"] == "project" or personal_allowed(conn, project_id))]
        if same and same[0]["status"] == "confirmed":
            return "saved", [same[0]["id"]]
        if same and same[0]["status"] == "proposed":  # the researcher's own words keep a pending proposal
            kept = keep(conn, same[0]["id"], None)
            if kept is not None:
                return "saved", [kept]
            same = []  # out of date: saved as its own record below
        if same:  # inferred: the researcher's own words confirm it, as a new version
            replaces, kind, scope = same[0]["id"], same[0]["type"], same[0]["scope"]
    sources = [("turn", run_id)] + (sources_of(conn, replaces) if replaces else [])
    record = write(conn, project_id=project_id, scope=scope, kind=kind, content=text, status="confirmed",
                   sources=sources, run_id=run_id, replaces=replaces, structured={"command": action})
    return ("updated" if replaces and action != "save" else "saved"), [record]


def forget_candidates(conn, project_id, text=None, previous=None):
    """Records a forget may mean: the ones most like text; for a bare "forget this", the active records the previous
    turn (previous: its run id) or its extraction made, else the one changed last."""
    if text is not None:
        return matching(conn, project_id, text, floor=0.2, statuses=("confirmed", "inferred", "proposed"))
    made = [r for r in in_scope(conn, project_id, kinds=TYPES) if previous is not None and (
        r["created_by_run_id"] == previous or conn.execute(
            "SELECT 1 FROM runs WHERE id = ? AND source_turn_id = ?", (r["created_by_run_id"], previous)).fetchone())]
    return [(1.0, r) for r in (made or in_scope(conn, project_id, kinds=TYPES)[:1])][:3]


def clear(scored):
    """Whether a forget with text names one record clearly: much like it, or the only one fairly like it."""
    return bool(scored) and (scored[0][0] >= 0.6 or (len(scored) == 1 and scored[0][0] >= 0.4))


def previous_answer(conn, conversation_id, run_id):
    """(run id, answer text) of the conversation's latest succeeded answer before this turn, while it may be kept
    (backend/provenance.py), else None."""
    from backend import provenance
    found = conn.execute(
        "SELECT t.run_id, t.answer FROM turns t JOIN runs r ON r.id = t.run_id WHERE t.conversation_id = ?"
        " AND t.run_id != ? AND r.status = 'succeeded' AND t.answer IS NOT NULL ORDER BY t.seq DESC LIMIT 1",
        (conversation_id, run_id)).fetchone()
    if found is None or not provenance.answer_carried(conn, found[0]):
        return None
    text = (json.loads(found[1]) or {}).get("text") or ""
    return found[0], (text[:CANDIDATE_CHARS] + ("…" if len(text) > CANDIDATE_CHARS else ""))


def save_answer(conn, *, project_id, run_id, previous, text=None):
    """A bare "save this" answered Save: the earlier answer (previous: its run id), confirmed by the researcher's
    click, carrying that answer's papers as its sources; or, answered with text, that text, the researcher's own."""
    from backend import provenance
    if text is not None:
        kind = kind_of(text)
        return write(conn, project_id=project_id, scope=scope_for(conn, project_id, kind), kind=kind, content=text,
                     status="confirmed", sources=[("turn", run_id)], run_id=run_id, structured={"command": "save"})
    found = conn.execute("SELECT answer FROM turns WHERE run_id = ?", (previous,)).fetchone()
    if found is None or not provenance.answer_carried(conn, previous):
        return None
    answer = (json.loads(found[0]) or {}).get("text") or ""
    answer = answer[:CANDIDATE_CHARS] + ("…" if len(answer) > CANDIDATE_CHARS else "")
    return write(conn, project_id=project_id, scope="project", kind="event", content=answer, status="confirmed",
                 sources=[("turn", run_id), ("turn", previous), *papers(provenance.dependencies(conn, previous))],
                 run_id=run_id, structured={"command": "save", "from": "answer"})


# Extraction


def extraction_input(conn, run_id):
    """What an extraction run's request holds, read now: (the researcher's message, the delivered answer, the turn's
    author, the related records [(id, type, status, content)], the papers they and the answer carry), or None when
    the source turn is gone."""
    from backend import provenance
    found = conn.execute(
        "SELECT t.user_message, t.answer, t.author, r.project_id, t.run_id FROM runs r JOIN turns t"
        " ON t.run_id = r.source_turn_id WHERE r.id = ?", (run_id,)).fetchone()
    if found is None:
        return None
    message, answer, author, project_id, turn = found
    related = [r for r in in_scope(conn, project_id, statuses=("confirmed", "inferred", "proposed"))
               if valid(conn, project_id, r)][:RELATED]  # as recall: none it may not be shown
    held = set(provenance.dependencies(conn, turn))
    for record in related:
        held |= {i for k, i in sources_of(conn, record["id"]) if k == "material"}
    return ((json.loads(message) or {}).get("text", ""), (json.loads(answer or "{}") or {}).get("text", ""), author,
            [(r["id"], r["type"], r["status"], r["content"]) for r in related], sorted(held))


def extraction_request(message, answer, related):
    lines = [f"- [{rid}] ({kind}, {status}) {content}" for rid, kind, status, content in related]
    return ("RESEARCHER'S MESSAGE:\n" + message + "\n\nASSISTANT'S ANSWER (model output, not the researcher's words):\n"
            + (answer or "(none)") + "\n\nRELATED RECORDS:\n" + ("\n".join(lines) if lines else "(none)"))


def parse(text):
    """The candidates of an extractor's reply, or None when it is not exactly the JSON asked for (the run then fails
    and writes nothing). A reply in one code fence is read inside it."""
    body = (text or "").strip()
    fenced = re.fullmatch(r"```(?:json)?\s*(.*?)\s*```", body, re.S)
    try:
        found = json.loads(fenced.group(1) if fenced else body)
    except (ValueError, TypeError):
        return None
    if not isinstance(found, dict) or set(found) != {"records"} or not isinstance(found["records"], list) \
            or len(found["records"]) > MAX_CANDIDATES:
        return None
    candidates = []
    for item in found["records"]:
        if not isinstance(item, dict) or not set(item) <= {"type", "content", "status", "span", "replaces"}:
            return None
        kind, content, status = item.get("type"), item.get("content"), item.get("status")
        span, replaces = item.get("span") or "", item.get("replaces") or ""
        if kind not in WRITABLE or status not in ("confirmed", "inferred", "proposed") or not isinstance(content, str) \
                or not isinstance(span, str) or not isinstance(replaces, str):
            return None
        from backend.settings import visible
        content = visible(content)
        if content is None or len(content) > MAX_CONTENT:
            return None
        candidates.append({"type": kind, "content": content, "status": status, "span": span[:MAX_CONTENT],
                           "replaces": replaces[:64]})
    return candidates


def _negations(text):
    """The negating words a text holds, which _terms leaves out as common: a record saying the opposite of the
    researcher's words is never theirs (S1-21a fix batch 3, internal review)."""
    return set(re.findall(r"[^\W_]+", normalized(text))) & {"not", "no"}


def cap(candidate, message, author):
    """The status a candidate may have: confirmed or inferred only when the words it rests on are in the
    researcher's own message (NFKC, case and spaces aside), are more than a word, and hold every term of the
    candidate (fix batch 2: a confirmed record never restates another record's text), and the turn is the researcher's; else proposed, whatever the extractor said (section 8,
    Where memory comes from: a decision planted in a paper and echoed in the answer never becomes active)."""
    span = normalized(candidate["span"])
    terms, said = _terms(candidate["content"]) | _negations(candidate["content"]), _terms(span) | _negations(span)
    own = (author == "researcher" and len(said) >= 2 and span in normalized(message)
           and terms <= said)  # every term it holds is the researcher's own (nothing restated from another record)
    return candidate["status"] if own else "proposed"


def _latest_turn(conn, record):
    """When the latest turn behind a record started (its turn sources, or its run's source turn), else when it was
    written."""
    found = conn.execute(
        "SELECT max(r.started_at) FROM memory_sources s JOIN runs r ON r.id = s.source_id"
        " WHERE s.memory_id = ? AND s.source_kind = 'turn'", (record["id"],)).fetchone()[0]
    return found or record["created_at"]


def _command_since(conn, project_id, started_at, anywhere=False):
    """Whether the researcher changed memory in the scope after a turn that started then: an explicit command applied
    (when it was applied: a bare command's ask may be answered long after its turn began), or a record forgotten in the
    views; in the project's scope, or anywhere (anywhere: the personal scope every project shares)."""
    return conn.execute(
        "SELECT 1 FROM run_events e JOIN runs r ON r.id = e.run_id WHERE e.type = 'memory_proposed'"
        " AND json_extract(e.data, '$.command') IS NOT NULL AND json_extract(e.data, '$.applied') = 1"
        " AND (?3 OR r.project_id = ?1) AND e.at > ?2"
        " UNION ALL SELECT 1 FROM audit_log WHERE event = 'deletion' AND json_extract(data, '$.kind') = 'memory'"
        " AND (?3 OR project_id = ?1) AND at > ?2 LIMIT 1", (project_id, started_at, int(anywhere))).fetchone() is not None


def _head(conn, record):
    """The active version a record is now: itself, or the version that replaced it (and so on); None when it was
    forgotten or is not active."""
    seen = set()
    while record is not None and record not in seen:
        seen.add(record)
        found = conn.execute("SELECT status FROM memory_records WHERE id = ?", (record,)).fetchone()
        if found is None:
            return None
        if found[0] in ACTIVE:
            return record
        newer = conn.execute("SELECT id FROM memory_records WHERE supersedes_id = ? AND status != 'proposed'"
                             " ORDER BY version DESC LIMIT 1", (record,)).fetchone()
        record = newer[0] if newer else None
    return None


def apply_extraction(conn, run_id, candidates):
    """An extraction's effect, in its terminal transaction (section 8): each candidate capped (cap), a duplicate of
    an active or pending record dropped, and one that names a record it replaces (or, confirmed, conflicts with)
    replacing it only when it is at least as authoritative, its source turn came after the latest turn behind that
    record, and the researcher has not changed the record since the extraction started; else it waits as a proposal
    naming the record (older when its turn came first). After a later explicit command in the scope it stores
    proposals only. Nothing when its source turn is gone. Returns the counts, or None."""
    from backend import provenance
    found = conn.execute(
        "SELECT t.user_message, t.author, r.project_id, t.run_id, s.started_at, r.started_at FROM runs r"
        " JOIN turns t ON t.run_id = r.source_turn_id JOIN runs s ON s.id = t.run_id WHERE r.id = ?",
        (run_id,)).fetchone()
    if found is None:
        return None
    message, author, project_id, turn, turn_started, run_started = found
    message = (json.loads(message) or {}).get("text", "")
    late_project = _command_since(conn, project_id, turn_started)
    late_anywhere = _command_since(conn, project_id, turn_started, anywhere=True)
    # every paper its request carried text of (the answer's, and the related records' it was shown), registered as
    # the run's dependencies before its dispatch (backend/runs.py _extraction_call)
    held = conn.execute("SELECT json_extract(inputs, '$.context_materials') FROM runs WHERE id = ?", (run_id,)).fetchone()
    carried = papers({*provenance.dependencies(conn, turn), *json.loads((held and held[0]) or "[]")})
    if not provenance.carried(conn, [i for _, i in carried]):  # a paper it drew on was deleted: nothing of it is kept
        return None
    # the records its request was shown: nothing is written when one of them was forgotten since (its tombstone; Forget
    # also takes an output held for it, backend/db/deletion.py); one replaced or edited since is section 8's case
    shown = conn.execute("SELECT json_extract(inputs, '$.memory_ids') FROM runs WHERE id = ?", (run_id,)).fetchone()
    if conn.execute("SELECT 1 FROM json_each(?) WHERE value IN (SELECT object_id FROM tombstones)",
                    ((shown and shown[0]) or "[]",)).fetchone() is not None:
        return None
    personal = personal_allowed(conn, project_id)
    counts = {"confirmed": 0, "inferred": 0, "proposed": 0, "replaced": 0, "duplicates": 0}
    written = []
    for candidate in candidates:
        status = cap(candidate, message, author)
        kind = candidate["type"]
        own = cap({**candidate, "status": "confirmed"}, message, author) == "confirmed"
        # a personal record holds only the researcher's own words, never text the model drew from the answer (Harold's
        # decision of 2026-10-23, point 1): such a candidate is the project's
        scope = scope_for(conn, project_id, kind) if own else "project"
        if scope == "personal" and status == "inferred":  # ticket 31: a personal inference waits to be confirmed
            status = "proposed"
        late = late_anywhere if scope == "personal" else late_project
        pending = in_scope(conn, project_id, statuses=("confirmed", "inferred", "proposed"))
        same = next((r for r in pending if normalized(r["content"]) == normalized(candidate["content"])), None)
        if same is not None:
            counts["duplicates"] += 1
            if same["status"] == "proposed" and status == "confirmed" and not late and not same["supersedes_id"] \
                    and (same["scope"] == "project" or personal):  # never a personal one from a stricter project
                keep(conn, same["id"], None)  # the researcher's own words confirm what was only proposed
            continue
        head = _head(conn, candidate["replaces"]) if candidate["replaces"] else None  # as the record stands now
        target = next((r for r in pending if r["id"] == head and r["status"] in ACTIVE and (
            r["scope"] == "project" or (personal and own))), None)  # a personal one: the researcher's own words only
        structured = {"source": "researcher" if own else "answer"}
        sources = [("turn", turn), ("run", run_id)]  # a personal record carries no paper (point 1)
        if target is not None:
            if target["scope"] == "project":  # the papers it may carry text of, and those it was written from
                sources += [*carried, *((k, i) for k, i in sources_of(conn, target["id"]) if k == "material")]
            if target["scope"] == "personal":
                late = late_anywhere
                if status == "inferred":
                    status = "proposed"
            authoritative = status == "confirmed" or (status == "inferred" and target["status"] in ("inferred", "proposed"))
            newer = turn_started > _latest_turn(conn, target)
            untouched = target["updated_at"] <= run_started
            if authoritative and newer and untouched and not late:
                written.append(write(conn, project_id=project_id, scope=target["scope"], kind=target["type"],
                                     content=candidate["content"], status=status, sources=sources, run_id=run_id,
                                     replaces=target["id"], structured=structured))
                counts["replaced"] += 1
                counts[status] += 1
                continue
            written.append(write(conn, project_id=project_id, scope=target["scope"], kind=target["type"],
                                 content=candidate["content"], status="proposed", sources=sources, run_id=run_id,
                                 replaces=None, structured={**structured, "seen": target["version"],
                                                            **({"older": True} if not newer else {})}))
            conn.execute("UPDATE memory_records SET supersedes_id = ? WHERE id = ?", (target["id"], written[-1]))
            counts["proposed"] += 1
            continue
        if late and status != "proposed":
            status = "proposed"
        written.append(write(conn, project_id=project_id, scope=scope, kind=kind, content=candidate["content"],
                             status=status, sources=sources + (carried if scope == "project" else []), run_id=run_id,
                             structured=structured))
        counts[status] += 1
    return {"counts": counts, "records": written}


# Project state, recall and Where we left off


def wants(decision):
    """(project state, user memory) a turn pre-loads by its route (section 7.3's table; T31:68): none for rewriting
    supplied text or a selection of papers; memory for a comparison; both for an earlier decision, finding papers,
    and a message no rule decides (an interest without a clear question)."""
    rule = decision.get("rule")
    if rule in ("rewrite", "selection"):
        return False, False
    if rule == "comparison":
        return False, True
    return True, True


def valid(conn, project_id, record):
    """Whether a record's text may be in a request of project_id now (S1-21a; Harold's decision of 2026-10-23: a
    record's scope and validity are fixed when it is written). The one rule every reader of memory applies (recall,
    PROJECT STATE, the memory tools, an extraction's related records) and the dispatch check (allowed):
    - its scope: a project record is project_id's own; a personal record any project's but a review-locked one's;
    - `material`: every paper whose text it may carry exists and passes the request's access check
      (provenance.sendable; a personal record carries none).
    A later tightening, lock or move of another project, or of the one it was written in, retracts nothing written."""
    from backend import provenance
    if record["scope"] == "project":
        if record["project_id"] != project_id:
            return False
    else:
        locked = conn.execute("SELECT review_lock FROM projects WHERE id = ?", (project_id,)).fetchone()
        if locked is None or locked[0]:
            return False
    return provenance.sendable(conn, project_id, materials_of(conn, [record["id"]]))


def recall_candidates(conn, project_id, locked=False, now=None):
    """USER MEMORY's eligible records for a turn of project_id, newest first: confirmed or inferred, of the recalled
    types, valid now; the project's own, and personal ones (none in a review-locked project, and a personal record
    only once confirmed); none whose papers the request may not carry."""
    now = now or utc_now()
    records = []
    for record in in_scope(conn, project_id, kinds=RECALLED):
        if record["scope"] == "personal" and (locked or record["status"] != "confirmed"):
            continue
        if record["valid_to"] is not None and record["valid_to"] <= now:
            continue
        if valid(conn, project_id, record):
            records.append(record)
    return records


def ranked(candidates, nearest=None, limit=RECALL_LIMIT, embedded=None):
    """At most limit records: those nearest in meaning first (nearest: ids, nearest first), then the newest; a record
    without a vector (embedded: the ids that have one) newer than every one with a vector (just written, not embedded
    yet) goes first, so it is never cut for lacking one."""
    if not nearest:
        return candidates[:limit]
    order = {rid: i for i, rid in enumerate(nearest)}
    embedded = set(order) if embedded is None else set(embedded)
    stamps = [r["updated_at"] for r in candidates if r["id"] in embedded]
    newest = max(stamps) if stamps else ""
    fresh = [r for r in candidates if r["id"] not in embedded and r["updated_at"] > newest]
    rest = sorted((r for r in candidates if r not in fresh), key=lambda r: order.get(r["id"], len(order)))
    return (fresh + rest)[:limit]


def memory_line(record):
    label = "" if record["status"] == "confirmed" else " (inferred)"
    return f"- [{record['type']}{label}] {record['content']}"


def project_state(conn, project_id):
    """PROJECT STATE's records: the project state itself (confirmed first, an inferred one labeled), then what's
    done (progress) and the agent's notes, newest first; and the records they are, [(id, materials)]."""
    rows = [r for r in in_scope(conn, project_id, kinds=("project_state",)) if valid(conn, project_id, r)]
    rows.sort(key=lambda r: r["status"] != "confirmed")
    done = [r for r in in_scope(conn, project_id, kinds=("progress",)) if valid(conn, project_id, r)][:STATE_NOTES]
    notes = [r for r in in_scope(conn, project_id, kinds=("agent_note",)) if valid(conn, project_id, r)][:STATE_NOTES]
    lines = [memory_line(r).replace("[project_state", "[state", 1) for r in rows]
    lines += [f"- [done] {r['content']}" for r in done]
    lines += [f"- [agent's note, {r['structured'].get('kind', 'note').replace('_', ' ')}] {r['content']}" for r in notes]
    return lines, [r["id"] for r in (*rows, *done, *notes)]


def materials_of(conn, record_ids):
    return sorted({i for rid in record_ids for k, i in sources_of(conn, rid) if k == "material"})


def note(conn, *, project_id, run_id, kind, text, materials):
    """An agent's note (update_progress): an open question or a next step, marked as the agent's, inferred, never
    confirmed; carrying the papers the turn's request held."""
    return write(conn, project_id=project_id, scope="project", kind="agent_note", content=text, status="inferred",
                 sources=[("run", run_id), *papers(materials)], run_id=run_id, structured={"kind": kind})


def _progress(conn, project_id, event, count, sources, wording):
    """One progress record per project, day and event, counted up: written by code from AAB's own records."""
    day = utc_now()[:10]
    found = conn.execute(
        "SELECT id, json_extract(structured, '$.count') FROM memory_records WHERE project_id = ? AND type = 'progress'"
        " AND json_extract(structured, '$.event') = ? AND json_extract(structured, '$.date') = ? AND status = 'inferred'",
        (project_id, event, day)).fetchone()
    if found is None:
        return write(conn, project_id=project_id, scope="project", kind="progress", content=wording(count, day),
                     status="inferred", sources=sources, structured={"event": event, "count": count, "date": day})
    total = found[1] + count
    conn.execute("UPDATE memory_records SET content = ?, structured = json_set(structured, '$.count', ?), updated_at = ?"
                 " WHERE id = ?", (wording(total, day), total, utc_now(), found[0]))
    conn.executemany("INSERT OR IGNORE INTO memory_sources (memory_id, source_kind, source_id) VALUES (?, ?, ?)",
                     [(found[0], k, i) for k, i in sources])
    return found[0]


def materials_added(conn, project_id, material_ids):
    """Progress: papers added to the project's library, each named as a `paper` source (it counts them, carrying none of
    their text): the record goes once they all have."""
    if material_ids:
        _progress(conn, project_id, "materials_added", len(material_ids), [("paper", m) for m in material_ids],
                  lambda n, day: f"Added {n} paper{'s' if n != 1 else ''} to the library ({day})")


def materials_removed(conn, project_id, count):
    """Progress: papers removed from the library (no title is kept; its source is the project)."""
    if count:
        _progress(conn, project_id, "materials_removed", count, [("project", project_id)],
                  lambda n, day: f"Removed {n} paper{'s' if n != 1 else ''} from the library ({day})")


def proposals(conn, project_id):
    """Things to confirm: the pending proposals of the project's scope, newest first, each with the record it would
    replace (its current content, when it is still there)."""
    found = []
    for record in in_scope(conn, project_id, statuses=("proposed",)):
        full = row(conn, record["id"])
        target = row(conn, full["supersedes_id"]) if full["supersedes_id"] else None
        full["replaces"] = {"id": target["id"], "content": target["content"], "status": target["status"],
                            "version": target["version"]} if target else None
        found.append(full)
    return found


def notice(conn, project_id):
    """Whether the project's quiet extraction-allowance notice is to be shown: recorded and not dismissed since."""
    shown = conn.execute("SELECT max(seq) FROM audit_log WHERE event = 'memory_allowance_notice' AND project_id = ?",
                         (project_id,)).fetchone()[0]
    dismissed = conn.execute("SELECT max(seq) FROM audit_log WHERE event = 'memory_notice_dismissed' AND project_id = ?",
                             (project_id,)).fetchone()[0]
    return shown is not None and (dismissed is None or dismissed < shown)


def record_notice(conn, project_id):
    """The allowance notice, once per project."""
    if conn.execute("SELECT 1 FROM audit_log WHERE event = 'memory_allowance_notice' AND project_id = ?",
                    (project_id,)).fetchone() is None:
        conn.execute("INSERT INTO audit_log (event, project_id, data) VALUES ('memory_allowance_notice', ?, '{}')",
                     (project_id,))


def left_off(conn, project_id, memory_on=True):
    """Where we left off (F10, S5): shown when the project's latest turn is at least 4 hours old, with the last
    things done, open questions, one suggested next step and Things to confirm (none while memory is off)."""
    latest = conn.execute("SELECT max(r.started_at) FROM turns t JOIN runs r ON r.id = t.run_id"
                          " WHERE r.project_id = ?", (project_id,)).fetchone()[0]
    threshold = _stamp(_clock() - timedelta(seconds=AWAY_SECONDS))
    show = latest is not None and latest <= threshold

    def shown(record):
        return {k: record[k] for k in ("id", "type", "status", "content", "version", "updated_at")} | {
            "structured": record["structured"]}
    done = in_scope(conn, project_id, kinds=("progress",))[:STATE_NOTES]
    notes = in_scope(conn, project_id, kinds=("agent_note",))
    questions = [r for r in notes if r["structured"].get("kind") == "open_question"][:3]
    step = next((r for r in notes if r["structured"].get("kind") == "next_step"), None)
    return {"show": show, "latest_turn_at": latest, "done": [shown(r) for r in done],
            "questions": [shown(r) for r in questions], "next_step": shown(step) if step else None,
            "to_confirm": proposals(conn, project_id) if memory_on else [], "memory_enabled": memory_on,
            "notice": notice(conn, project_id)}


# The API (slice-1 spec section 5: GET, POST /api/memory; PATCH, DELETE /api/memory/{id}; GET, POST
# /api/memory/review; GET /api/projects/{id}/left-off). Text through visible; a change names the version seen (409
# when it changed); Forget deletes through the deletion service. Every change is the researcher's, so it is
# confirmed (progress and agent notes stay inferred).

import asyncio  # noqa: E402
from typing import Literal  # noqa: E402

from fastapi import APIRouter, Request  # noqa: E402
from fastapi.responses import JSONResponse  # noqa: E402
from pydantic import BaseModel, Field  # noqa: E402

router = APIRouter()


class NewRecord(BaseModel):
    project_id: str = Field(max_length=64)
    scope: Literal["personal", "project"] = "project"
    type: Literal[WRITABLE]
    content: str = Field(max_length=MAX_CONTENT)


class Change(BaseModel):
    version: int
    content: str | None = Field(default=None, max_length=MAX_CONTENT)
    status: Literal["confirmed"] | None = None


class Review(BaseModel):
    project_id: str | None = Field(default=None, max_length=64)
    id: str | None = Field(default=None, max_length=64)
    action: Literal["keep", "edit", "forget"] | None = None
    version: int | None = None
    content: str | None = Field(default=None, max_length=MAX_CONTENT)
    dismiss: Literal["allowance_notice"] | None = None


class Refused(Exception):
    def __init__(self, status, code, message):
        super().__init__(message)
        self.status, self.code, self.message = status, code, message


def _refused(error):
    return JSONResponse({"code": error.code, "message": error.message}, status_code=error.status)


_CHANGE_ERRORS = {"changed": (409, "version_changed", "This record changed since it was shown; look again"),
                  "out_of_date": (409, "out_of_date", "The record this would replace changed; look again"),
                  "not_confirmable": (400, "not_confirmable", "Progress and the agent's notes are never confirmed")}


def _on(state, project_id):
    from backend.settings import load_settings
    return enabled(load_settings(state["data_dir"], project_id).values)


async def _embed_soon(state, project_id):
    """An index run for the project, when the search model is installed, so new records get their vectors."""
    from backend import search  # it imports backend.runs, which imports this module
    if state.get("harness") is not None and search._installed(state):
        try:
            await search._request(state["harness"], project_id)
        except Exception as error:  # the next index run embeds them
            import logging
            logging.getLogger(__name__).warning("an index run for memory could not be requested (%s)", type(error).__name__)


async def _forget(state, record):
    from backend.db import deletion
    revoked = await asyncio.to_thread(deletion.delete, state["db"], state["content"], "memory", record,
                                      on_committed=state["harness"].revoke)
    return revoked


@router.get("/api/memory")
async def list_memory(request: Request, project_id: str | None = None):
    """The memory views (Settings, then Advanced): personal and project records apart, with type, status, sources,
    validity and version; whether memory is on in the project."""
    state = request.app.state.scholia
    found = await asyncio.to_thread(state["db"].read, lambda conn: listing(conn, project_id))
    on = await asyncio.to_thread(_on, state, project_id) if project_id else True
    return {**found, "memory_enabled": on}


@router.post("/api/memory", status_code=201)
async def create_memory(body: NewRecord, request: Request):
    from backend.settings import visible
    state = request.app.state.scholia
    content = visible(body.content)
    if content is None:
        return _refused(Refused(400, "empty", "The record is empty"))
    if body.scope == "personal" and body.type not in PERSONAL_TYPES:
        return _refused(Refused(400, "invalid_scope", "Only profile and preference records are personal"))

    def create(conn):
        if conn.execute("SELECT 1 FROM projects WHERE id = ?", (body.project_id,)).fetchone() is None:
            raise Refused(404, "not_found", "No such project")
        if body.scope == "personal" and not personal_allowed(conn, body.project_id):  # reading 4
            raise Refused(400, "personal_from_strict", "A Private or Local only project's records stay its own")
        return write(conn, project_id=body.project_id, scope=body.scope, kind=body.type, content=content,
                     status="confirmed", sources=[("researcher", "views")], structured={"entered": True})
    try:
        record = await asyncio.to_thread(state["db"].write, create)
    except Refused as error:
        return _refused(error)
    await _embed_soon(state, body.project_id)
    return await asyncio.to_thread(state["db"].read, lambda conn: row(conn, record))


@router.patch("/api/memory/{record_id}")
async def change_memory(record_id: str, body: Change, request: Request):
    """Confirm, edit or supersede a record at the version seen: a new version that replaces it (a proposal is kept)."""
    from backend.settings import visible
    state = request.app.state.scholia
    content = None
    if body.content is not None:
        content = visible(body.content)
        if content is None:
            return _refused(Refused(400, "empty", "The record is empty"))

    def change(conn):
        return edit(conn, record_id, body.version, content=content, confirm=body.status == "confirmed")
    try:
        record = await asyncio.to_thread(state["db"].write, change)
    except LookupError:
        return _refused(Refused(404, "not_found", "No such record"))
    except ValueError as error:
        return _refused(Refused(*_CHANGE_ERRORS[str(error)]))
    if record is None:  # a proposal whose record changed: marked out of date, back in Things to confirm
        return _refused(Refused(*_CHANGE_ERRORS["out_of_date"]))
    found = await asyncio.to_thread(state["db"].read, lambda conn: row(conn, record))
    await _embed_soon(state, found["project_id"])
    return found


@router.delete("/api/memory/{record_id}")
async def delete_memory(record_id: str, request: Request):
    """Forget: the record's content is deleted (with the versions it replaced), its vector removed, its audit record
    written without content, and the runs whose request holds it revoked."""
    state = request.app.state.scholia
    try:
        await _forget(state, record_id)
    except LookupError:
        return _refused(Refused(404, "not_found", "No such record"))
    return {"ok": True}


@router.get("/api/memory/review")
async def review(request: Request, project_id: str):
    """Things to confirm: the pending proposals (none while memory is off), and the allowance notice."""
    state = request.app.state.scholia
    on = await asyncio.to_thread(_on, state, project_id)
    found = await asyncio.to_thread(state["db"].read, lambda conn: (proposals(conn, project_id) if on else [],
                                                                     notice(conn, project_id)))
    return {"proposals": found[0], "notice": found[1], "memory_enabled": on}


@router.post("/api/memory/review")
async def answer_review(body: Review, request: Request):
    """Keep, Edit or Forget a proposal at the version seen; or dismiss the allowance notice."""
    from backend.settings import visible
    state = request.app.state.scholia
    if body.dismiss is not None:
        if body.project_id is None:
            return _refused(Refused(400, "invalid_request", "Name the project"))
        await asyncio.to_thread(state["db"].write, lambda conn: conn.execute(
            "INSERT INTO audit_log (event, project_id, data) VALUES ('memory_notice_dismissed', ?, '{}')",
            (body.project_id,)))
        return {"ok": True}
    if body.id is None or body.action is None:
        return _refused(Refused(400, "invalid_request", "Name the proposal and what to do"))
    if body.action == "forget":
        try:
            await _forget(state, body.id)
        except LookupError:
            return _refused(Refused(404, "not_found", "No such record"))
        return {"ok": True}
    content = visible(body.content) if body.action == "edit" else None
    if body.action == "edit" and content is None:
        return _refused(Refused(400, "empty", "The record is empty"))

    def answer(conn):
        found = row(conn, body.id)
        if found is None or found["status"] != "proposed":
            raise LookupError(body.id)
        return edit(conn, body.id, body.version if body.version is not None else found["version"], content=content)
    try:
        record = await asyncio.to_thread(state["db"].write, answer)
    except LookupError:
        return _refused(Refused(404, "not_found", "No such proposal"))
    except ValueError as error:
        return _refused(Refused(*_CHANGE_ERRORS[str(error)]))
    if record is None:  # marked out of date in that commit: back in Things to confirm
        return _refused(Refused(*_CHANGE_ERRORS["out_of_date"]))
    found = await asyncio.to_thread(state["db"].read, lambda conn: row(conn, record))
    await _embed_soon(state, found["project_id"])
    return found


@router.get("/api/projects/{project_id}/left-off")
async def where_we_left_off(project_id: str, request: Request):
    state = request.app.state.scholia
    on = await asyncio.to_thread(_on, state, project_id)

    def read(conn):
        if conn.execute("SELECT 1 FROM projects WHERE id = ?", (project_id,)).fetchone() is None:
            return None
        return left_off(conn, project_id, on)
    found = await asyncio.to_thread(state["db"].read, read)
    if found is None:
        return _refused(Refused(404, "not_found", "No such project"))
    return found
