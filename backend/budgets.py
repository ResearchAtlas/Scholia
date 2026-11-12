"""S1-33: the budgets as the researcher meets them (slice-1 spec F11; section 6.2, Effects): the budget ask's answer,
the bar's notices and the spend it reads, and the evaluation project's conservative bound (SPEC:1223).

A turn's model call that does not fit a budget asks first (Harness._reserve): at the cap (the budget's settled spend
and open reservations have reached its limit) Continue, Set a new limit or Stop; when money is left but this one
step's estimate is more, Go ahead, Set a new limit or Not now (the scope notes' reading 1, approved 2026-10-21). The
answer's effect is applied here, in the answer's transaction (backend/asks.py): a conversation's limit is
`conversations.budget_usd`; the project's lives in its config.toml, which a database transaction cannot hold, so its
new value is returned for the ask endpoint to write once the answer has committed (Harold, 2026-10-21). Each decision
is a `budget_decision` run event and an audit record, content-free. An answer to an ask whose limit changed since it
was asked (another conversation's Continue, a Settings edit) applies nothing: its step is checked again.

Continue adds the budget's amount again (reading 3): the amount its last decision recorded while the limit is still
what that decision left, else the limit itself (a conversation without its own, the personal default), so $10
becomes $20 and then $30, never compounding; a typed limit becomes the amount from then on.
"""

import json
import math
import re
import unicodedata

from backend import governance, openrouter, spending
from backend.runs import _budget_unread, _budgets_now, _event
from backend.settings import PERSONAL, PROJECT, load_settings

_EPSILON = 1e-9
EVALUATION_ATTEMPTS = openrouter.MAX_ATTEMPTS  # the HTTP attempts one call may make, each charged if it goes out
STOPS = ("stop", "not_now")


class Refused(Exception):
    """A typed limit that cannot be applied: code invalid_limit or limit_too_low (needed_usd given). Nothing changed."""

    def __init__(self, code, message, needed_usd=None):
        super().__init__(message)
        self.code, self.message, self.needed_usd = code, message, needed_usd


def options(form):
    return ["continue", "stop"] if form == "cap" else ["go_ahead", "not_now"]


def bound(conn, project_id, estimate):
    """A call's reservation: its estimate, or in the evaluation project the conservative bound, the estimate (the counted
    request and its whole output limit at the highest reachable rate) times the attempts one call may make."""
    return round(estimate * EVALUATION_ATTEMPTS, 9) if evaluation(conn, project_id) else estimate


def evaluation(conn, project_id):
    row = conn.execute("SELECT evaluation FROM projects WHERE id = ?", (project_id,)).fetchone()
    return bool(row and row[0])


def totals(conn, *, project_id=None, conversation_id=None):
    """(settled, open) spend of a project, or of a paying conversation."""
    column, value = ("project_id", project_id) if conversation_id is None else ("paying_conversation_id", conversation_id)
    settled, held = conn.execute(
        "SELECT coalesce(sum(CASE status WHEN 'settled' THEN settled_usd ELSE 0 END), 0),"
        " coalesce(sum(CASE status WHEN 'open' THEN estimate_usd ELSE 0 END), 0)"
        f" FROM budget_reservations WHERE {column} = ?", (value,)).fetchone()
    return round(settled, 9), round(held, 9)


def bases(conn, *, project_id=None, conversation_id=None):
    """The settled spend of a project, or of a paying conversation, by basis: {reported_usd, estimated_usd}."""
    column, value = ("project_id", project_id) if conversation_id is None else ("paying_conversation_id", conversation_id)
    found = dict(conn.execute(f"SELECT basis, coalesce(sum(settled_usd), 0) FROM budget_reservations WHERE {column} = ?"
                              " AND status = 'settled' GROUP BY basis", (value,)).fetchall())
    return {"reported_usd": round(found.get("reported", 0), 9), "estimated_usd": round(found.get("estimated", 0), 9)}


def _limits(conn, data_dir, project_id, conversation_id):
    """{budget: (its limit now, whether it is the conversation's own)} for the project and, if given, the conversation."""
    project_limit, default = _budgets_now(data_dir, project_id)
    found = {"project": (project_limit, False)}
    if conversation_id is not None:
        own = conn.execute("SELECT budget_usd FROM conversations WHERE id = ?", (conversation_id,)).fetchone()
        if own is not None:
            found["conversation"] = (own[0], True) if own[0] is not None else (default, False)
    return found


def spend(conn, data_dir, project_id, conversation_id=None):
    """GET /api/projects/{id}/spend: settled spend (with its reported and estimated parts) and open spend against the
    project's limit and, given one, the conversation's; no content. None for a conversation not in this project."""
    if conversation_id is not None and conn.execute(
            "SELECT 1 FROM conversations WHERE id = ? AND project_id = ?", (conversation_id, project_id)).fetchone() is None:
        return None
    limits = _limits(conn, data_dir, project_id, conversation_id)
    settled, held = totals(conn, project_id=project_id)
    unread = _budget_unread(load_settings(data_dir, project_id))  # no limit shown that its file does not give (E4)
    found = {"project": {"limit_usd": None if unread else limits["project"][0], "settled_usd": settled, "open_usd": held,
                         **bases(conn, project_id=project_id)}, "conversation": None,
             "notice_percents": sorted(load_settings(data_dir).values["budget"]["notice_percents"])}
    if conversation_id is not None:
        settled, held = totals(conn, conversation_id=conversation_id)
        limit, own = limits["conversation"]
        found["conversation"] = {"limit_usd": limit, "settled_usd": settled, "open_usd": held, "own": own,
                                 **bases(conn, conversation_id=conversation_id)}
    return found


def notices(conn, run_id, data_dir):
    """After a settlement, in its transaction: a `budget_notice` event on this run for each notice threshold the settled
    spend of its budgets has reached and that is not yet recorded for that budget and limit (so one settlement crossing
    several records each, and a raised or lowered limit rearms them). A conversation's are known by its runs' events,
    deleted only with it; a project's by its own record (projects.budget_noticed), since its conversations are deleted
    and moved with their runs (PR 52 cycle 2), and each names its project. Returns what it recorded."""
    row = conn.execute("SELECT r.project_id, CASE WHEN r.kind IN ('turn', 'child') THEN r.conversation_id END,"
                       " p.budget_noticed FROM runs r JOIN projects p ON p.id = r.project_id WHERE r.id = ?",
                       (run_id,)).fetchone()
    if row is None:
        return []
    project_id, conversation_id, noticed = row[0], row[1], json.loads(row[2] or "[]")
    percents = sorted(load_settings(data_dir).values["budget"]["notice_percents"])
    recorded = []
    for budget, (limit, _) in _limits(conn, data_dir, project_id, conversation_id).items():
        if not limit or limit <= 0:
            continue
        settled = totals(conn, **({"conversation_id": conversation_id} if budget == "conversation"
                                  else {"project_id": project_id}))[0]
        for percent in percents:
            if settled * 100 < percent * limit - _EPSILON:
                continue
            if budget == "project":
                if [percent, limit] in noticed:
                    continue
                noticed.append([percent, limit])
            elif conn.execute(
                    "SELECT 1 FROM run_events e JOIN runs r ON r.id = e.run_id WHERE e.type = 'budget_notice'"
                    " AND r.conversation_id = ? AND json_extract(e.data, '$.budget') = 'conversation'"
                    " AND json_extract(e.data, '$.percent') = ? AND json_extract(e.data, '$.limit_usd') = ? LIMIT 1",
                    (conversation_id, percent, limit)).fetchone():
                continue
            notice = {"budget": budget, "percent": percent, "limit_usd": limit, "settled_usd": settled,
                      **({"project_id": project_id} if budget == "project" else {})}
            _event(conn, run_id, "budget_notice", notice)
            recorded.append(notice)
    if any(notice["budget"] == "project" for notice in recorded):
        conn.execute("UPDATE projects SET budget_noticed = json(?) WHERE id = ?", (json.dumps(noticed), project_id))
    return recorded


def _typed(text):
    """A typed limit in dollars ("25", "$2.50", " 10 ", full-width digits too), or Refused."""
    found = re.fullmatch(r"\s*(?:US\s*)?\$?\s*([0-9]+(?:\.[0-9]+)?)\s*", unicodedata.normalize("NFKC", text or ""))
    value = float(found.group(1)) if found else math.nan
    if not math.isfinite(value) or value <= 0:
        raise Refused("invalid_limit", "Type a budget in US dollars, more than zero")
    return round(value, 9)


def _amount(conn, data_dir, budget, scope_id, current, default):
    """What Continue adds: the amount the budget's last applied decision recorded while its limit is what that decision
    left, else the limit itself; for a limit of zero, the budget's default as configured (so Continue never adds nothing).
    A conversation's decisions are its runs' events, deleted only with it, and only an applied one counts (one stale or
    stopped changed nothing: PR 52 cycle 2). A project's are in its config.toml beside the limit (Harold, 2026-11-12,
    design review C's Option 2): `budget_continue_usd` while `budget_continue_at_usd` is the limit, written with it in
    one file replace (asks._save_project_budget), and taken out by any other save that changes the limit (Settings.save,
    design review C3's Option 2), so the limit set there is the amount from then on. A pair a hand edit made invalid is
    none."""
    if budget == "project":
        own = load_settings(data_dir, scope_id).values["project"]
        row = (own.get("budget_continue_at_usd"), own.get("budget_continue_usd"))
        row = row if None not in row else None
    else:
        row = conn.execute(
            "SELECT json_extract(e.data, '$.to_usd'), json_extract(e.data, '$.amount_usd') FROM run_events e"
            " JOIN runs r ON r.id = e.run_id WHERE e.type = 'budget_decision' AND r.conversation_id = ?"
            " AND json_extract(e.data, '$.budget') = ? AND json_extract(e.data, '$.amount_usd') IS NOT NULL"
            " AND json_extract(e.data, '$.applied') = 1"
            " ORDER BY e.rowid DESC LIMIT 1", (scope_id, budget)).fetchone()
    found = row[1] if row is not None and abs(row[0] - current) <= _EPSILON else current
    return found if found > 0 else default


def decide(conn, run_id, ask, option, text, data_dir):
    """A budget ask's answer, in its transaction: the decision recorded (a `budget_decision` event and an audit record)
    and, for a conversation, its new limit written. Returns (what the reply says of it, the project budget's decision
    still to apply, or None). A project budget's change is recorded here as decided but not applied: its config.toml is
    written once this commits, and only then is it recorded applied (or not saved) with its audit record (saved), so
    no record says the budget was raised before the file says so. Raises Refused for a typed limit it cannot apply."""
    params, project_id = ask["params"], ask["project_id"]
    budget, conversation_id = params["budget"], params.get("conversation_id")
    limits = _limits(conn, data_dir, project_id, conversation_id if budget == "conversation" else None)
    if budget not in limits:  # its conversation is gone; the run ends with it
        raise Refused("invalid_limit", "The conversation no longer exists")
    current = limits[budget][0]
    spent = spending.spent(conn, **({"conversation_id": conversation_id} if budget == "conversation"
                                    else {"project_id": project_id}))
    needed = round(spent + params["estimate_usd"], 9)
    # a zero limit's Continue adds the personal default (a conversation's) or the shipped project budget, never nothing
    default = _budgets_now(data_dir, project_id)[1] if budget == "conversation" else PROJECT[("project", "budget_usd")][0]
    amount = _amount(conn, data_dir, budget, conversation_id if budget == "conversation" else project_id, current,
                     default or PERSONAL[("budget", "conversation_usd")][0])
    if text is not None:
        typed = _typed(text)
        if typed + _EPSILON < needed:
            raise Refused("limit_too_low", f"This step needs a budget of at least ${needed:.2f}", needed)
        decision, wanted, amount = "set_limit", typed, typed
    else:
        decision = option
        wanted = {"continue": round(current + amount, 9), "go_ahead": max(current, needed)}.get(option, current)
    # An ask raised while the project's file did not give its budget (unread, PR 52 cycle 3, E4): only a typed limit
    # means anything; Continue and Go ahead have no amount to add to, so they apply nothing and the step is checked again
    stale = abs(current - params["limit_usd"]) > _EPSILON or (bool(params.get("unread")) and text is None)
    applied = not stale and decision not in STOPS and abs(wanted - current) > _EPSILON
    # what the records say the limit was: nothing when its file does not give it, never $0 (and no amount to add to)
    known = None if budget == "project" and _budget_unread(load_settings(data_dir, project_id)) else current
    amount = amount if known is not None or text is not None else None
    to = wanted if applied else known
    pending = applied and budget == "project"
    decided = {"ask_id": ask["ask_id"], "budget": budget, "decision": decision, "from_usd": known, "to_usd": to,
               "amount_usd": amount}
    _event(conn, run_id, "budget_decision", {
        **decided, **({"to_usd": known, "applied": False, "pending_usd": to} if pending else {"applied": applied}),
        **({"stale": True} if stale else {})})
    if not pending:
        governance.record(conn, "budget_decision", project_id, budget=budget, decision=decision, from_usd=known,
                          to_usd=to, applied=applied)
    if applied and budget == "conversation":
        conn.execute("UPDATE conversations SET budget_usd = ? WHERE id = ?", (to, conversation_id))
    return {"applied": applied, "budget": budget, "limit_usd": to}, ({**decided, "project_id": project_id} if pending else None)


def saved(conn, run_id, decided, ok):
    """After a project budget's decision committed and its config.toml write was tried: the decision recorded applied
    (or not saved, its limit unchanged) on the run's record, which a run deleted meanwhile no longer has, and audited."""
    record = {**decided, **({} if ok else {"to_usd": decided["from_usd"]}), "applied": ok, "saved": ok}
    record.pop("project_id")
    if conn.execute("SELECT 1 FROM runs WHERE id = ?", (run_id,)).fetchone() is not None:
        _event(conn, run_id, "budget_decision", record)
    governance.record(conn, "budget_decision", decided["project_id"], budget="project", decision=decided["decision"],
                      from_usd=decided["from_usd"], to_usd=record["to_usd"], applied=ok)


QUEUED_COLUMNS = "id, text, created_at, updated_at, material_ids"


def queued_row(row):
    """A waiting message as the API gives it: with material_ids, the papers it names, only when it names some."""
    found = dict(zip(("id", "text", "created_at", "updated_at"), row[:4]))
    return {**found, "material_ids": json.loads(row[4])} if row[4] else found


def queue(conn, conversation_id, text, material_ids=None):
    """A message waiting as the conversation's next turn, after those already waiting, with the papers it names (ids,
    checked when it starts, Harness.admit_turn). Returns its row."""
    from backend.db import new_id
    queued_id = new_id()
    conn.execute("INSERT INTO queued_messages (id, conversation_id, text, material_ids) VALUES (?, ?, ?, ?)",
                 (queued_id, conversation_id, text, json.dumps(material_ids) if material_ids else None))
    return queued_row(conn.execute(f"SELECT {QUEUED_COLUMNS} FROM queued_messages WHERE id = ?",
                                   (queued_id,)).fetchone())


def queued(conn, conversation_id):
    return [queued_row(r) for r in conn.execute(
        f"SELECT {QUEUED_COLUMNS} FROM queued_messages WHERE conversation_id = ? ORDER BY rowid", (conversation_id,))]


def carried_notes(conn, run_id):
    """The steering notes (their seq) a step of the run carried in a request that went out: read by the agent. Known by
    a recorded attempt of that step the gate let out (`model_attempt` dispatched true), never by its `dispatch` event,
    which is written before the gate's check and the connection (PR 52 review 1); an attempt whose fate a crash left
    unknown counts as not sent, so its note is queued rather than lost."""
    carried = set()
    for (data,) in conn.execute(
            "SELECT s.data FROM run_events s WHERE s.run_id = ? AND s.type = 'step_started'"
            " AND json_extract(s.data, '$.steering') IS NOT NULL AND EXISTS (SELECT 1 FROM run_events d"
            " WHERE d.run_id = s.run_id AND d.type = 'model_attempt' AND json_extract(d.data, '$.dispatched') = 1"
            " AND json_extract(d.data, '$.step') = json_extract(s.data, '$.step'))", (run_id,)):
        carried.update(json.loads(data)["steering"])
    return carried


def read_notes(conn, run_id):
    """The texts of the turn's steering notes a step read, in order: the researcher's own words in that turn, which
    later turns' history (and compaction) carry after its message (the connector's finding on PR 52; T15:84)."""
    carried = carried_notes(conn, run_id)
    return [text for seq, text in conn.execute("SELECT seq, json_extract(data, '$.text') FROM run_events WHERE run_id = ?"
                                               " AND type = 'steering_note' ORDER BY seq", (run_id,))
            if seq in carried and text]


def queue_unread_notes(conn, run_id):
    """As a turn ends, in its terminal transaction: each steering note no dispatched step carried waits as the
    conversation's next turn instead, in order, so a note is never lost and never both read and queued (reading 10).
    Returns how many it queued."""
    row = conn.execute("SELECT conversation_id FROM runs WHERE id = ?", (run_id,)).fetchone()
    if row is None or row[0] is None:
        return 0
    carried, queued = carried_notes(conn, run_id), 0
    for seq, text in conn.execute("SELECT seq, json_extract(data, '$.text') FROM run_events WHERE run_id = ?"
                                  " AND type = 'steering_note' ORDER BY seq", (run_id,)).fetchall():
        if seq not in carried and text:
            queue(conn, row[0], text)
            queued += 1
    return queued
