"""What text a request may carry, and a stored row may keep, once its source is deleted (S1-19b; slice-1
spec section 4.2: a deletion reaches every copy the platform holds).

The rule: text drawn from a paper (its passages, quotes of them, and the answers and sentences that drew
on them) is sent or kept only while that paper exists. Each turn records the papers its request's text
depends on, `inputs.context_materials`: its evidence's papers and those of every earlier answer its history
carries, so the set is transitive. That one set is used everywhere:

- History: an earlier answer is carried in a later request only while every paper its turn depended on
  exists and its project allows the request as its evidence would be allowed (`sendable`); one that is
  not is left out, and the step's trace says how many were.
- Revocation: the set is the run's scope (`inputs.material_ids`, deletion's scope links) and the dispatch
  check refuses a run any of whose papers is gone or no longer `sendable` (runs.may_dispatch).
- Stored request bodies (the trace's exact outbound text) are linked to it (run_body_materials) when they
  are stored, before they are sent, and go with any of its papers (db/deletion.py RULES).
- Stored rows holding such text name their paper and lose the text with it: a citation's quote and
  sentence (db/deletion.py _SOURCE_REMOVED). An agent's question and option labels, held in its run's inputs
  while it waits (`held.ask`, S1-19c), go in the deletion's own transaction (`forget`). Append-only events never hold it: they name citations and
  passages by id, and a citation check binds a verdict to its sentence by the sentence's hash.
- Memory (S1-21a, backend/memory.py): a record that may carry such text (an extraction's, a saved answer, an agent's
  note) names each paper as a `material` source and is deleted with any one of them (db/deletion.py RULES); recall
  leaves out one whose papers are not `sendable`, and a request holding records names their papers too.
- An answer stays the conversation's record (turns.answer), but every copy derived from it follows its
  whole dependency set (Harold, 2026-10-21, option A): once any of its papers is gone, every citation row
  of the answer loses its quote and sentence, whichever paper it names (`forget`, from the deletion
  service; `answer_carried` where the rows are written), and it is never sent again: not in history, and
  /check refuses it (backend/answers.py); a check run carries the answer's papers, so its bodies and its
  dispatch follow them too.
"""

import json


def existing(conn, material_ids):
    """The ones of material_ids that exist now, in the transaction given."""
    ids = sorted(set(material_ids))
    return {m for (m,) in conn.execute("SELECT id FROM materials WHERE id IN (SELECT value FROM json_each(?))",
                                       (json.dumps(ids),))} if ids else set()


def carried(conn, material_ids):
    """Whether text depending on these papers may be sent or kept: every one of them exists."""
    ids = set(material_ids)
    return existing(conn, ids) == ids


def sendable(conn, project_id, material_ids):
    """Whether text depending on these papers may go out in a request of project_id now: every paper exists
    (`carried`), and its project passes the current access check its evidence would (slice-1 spec section 10):
    not review-locked, and at a level no stricter than project_id's. The levels allow nested sets of routes
    (Local only within Private within Normal; governance.Policy.problem), so a route the gate allows
    project_id's request is allowed for every such paper too. A conversation moved to another project keeps
    its earlier answers' papers where they are; this is what keeps a tightened project's text in."""
    from backend.governance import LEVELS
    ids = set(material_ids)
    if not ids:
        return True
    rows = conn.execute("SELECT p.sensitivity, p.review_lock FROM materials m JOIN projects p ON p.id = m.project_id"
                        " WHERE m.id IN (SELECT value FROM json_each(?))", (json.dumps(sorted(ids)),)).fetchall()
    own = conn.execute("SELECT sensitivity FROM projects WHERE id = ?", (project_id,)).fetchone()
    return own is not None and len(rows) == len(ids) and all(
        not locked and LEVELS.index(level) <= LEVELS.index(own[0]) for level, locked in rows)


def dependencies(conn, run_id):
    """The papers run_id's request carried text of (inputs.context_materials), its history's included."""
    row = conn.execute("SELECT json_extract(inputs, '$.context_materials') FROM runs WHERE id = ?", (run_id,)).fetchone()
    return json.loads(row[0]) if row and row[0] else []


def answer_carried(conn, run_id):
    """Whether the answer of run_id, and every copy derived from it, may still be sent or kept."""
    return carried(conn, dependencies(conn, run_id))


def forget(conn, doomed):
    """Deletion's side of the rule (backend/db/deletion.py, in its transaction): every run whose dependency set
    holds a paper being deleted (doomed: an SQL query of their ids) loses every copy of model-written text it keeps:
    its answer's citation rows lose their text, whichever paper each row names, and an agent's question and option
    labels held while it waits (`inputs.held.ask`, S1-19c: the model's text, which may quote the paper) go at once,
    not at the run's own end, which a failed write could keep from happening; so does a memory extraction's reply held
    until its effect is written (`inputs.held.output`, S1-21a)."""
    # ponytail: scans every run's context_materials per deletion; an index table if runs grow very large
    depending = f"SELECT r.id FROM runs r, json_each(r.inputs, '$.context_materials') j WHERE j.value IN ({doomed})"
    conn.execute("UPDATE citations SET quote = NULL, context = json_remove(coalesce(context, '{}'), '$.sentence')"
                 f" WHERE owner_kind = 'answer' AND owner_id IN ({depending})"
                 " AND (quote IS NOT NULL OR json_extract(context, '$.sentence') IS NOT NULL)")
    conn.execute("UPDATE runs SET inputs = json_remove(inputs, '$.held.ask', '$.held.output')"
                 " WHERE (json_type(inputs, '$.held.ask') IS NOT NULL OR json_type(inputs, '$.held.output') IS NOT NULL)"
                 f" AND id IN ({depending})")
