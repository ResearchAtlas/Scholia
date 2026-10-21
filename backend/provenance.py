"""What text a request may carry, and a stored row may keep, once its source is deleted (S1-19b; slice-1
spec section 4.2: a deletion reaches every copy the platform holds).

The rule: text drawn from a paper (its passages, quotes of them, and the answers and sentences that drew
on them) is sent or kept only while that paper exists. Each turn records the papers its request's text
depends on, `inputs.context_materials`: its evidence's papers and those of every earlier answer its history
carries, so the set is transitive. That one set is used everywhere:

- History: an earlier answer is carried in a later request only while every paper its turn depended on
  exists (`carried`); one that is not is left out, and the step's trace says how many were.
- Revocation: the set is the run's scope (`inputs.material_ids`, deletion's scope links) and the dispatch
  check refuses a run any of whose papers is gone (runs.may_dispatch).
- Stored request bodies (the trace's exact outbound text) are linked to it (run_body_materials) when they
  are stored, before they are sent, and go with any of its papers (db/deletion.py RULES).
- Stored rows holding such text name their paper and lose the text with it: a citation's quote and
  sentence (db/deletion.py _SOURCE_REMOVED). Append-only events never hold it: they name citations and
  passages by id, and a citation check binds a verdict to its sentence by the sentence's hash.
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
