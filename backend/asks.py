"""The shared confirmation (slice-1 spec section 6.2; ticket 71): one question to the researcher,
built here before the agent exists; S1-19's ask tool raises it for the agent.

An ask belongs to a running run: an `ask` run event with the run waiting `ask`. It has the ask
tool's shape: a question, one to three options and a text box unless its kind rules one out; it
names its project and action (its kind) and the policy it was asked under (the project's level
and review lock). It takes one answer, only through `POST /api/runs/{id}/asks/{ask_id}`, recorded
as an `ask_answered` event and audited without content. It is invalid once its run has ended
(cancelled, finished, revoked or deleted with its project) or the project's level or lock has
changed since it was asked. The conversation, the Library panel and the background-run list all
show the open asks this module lists, and answer them through the one endpoint.
"""

import asyncio
import json

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from backend import governance
from backend.db import new_id
from backend.runs import _event
from backend.settings import visible

# The kinds that rule the text box out, and how many options an ask may offer.
NO_TEXT = {"identifier_lookup"}
MAX_OPTIONS = 3

router = APIRouter()


class AskRefused(Exception):
    def __init__(self, status, code, message):
        super().__init__(message)
        self.status, self.code, self.message = status, code, message


class Answer(BaseModel):
    option: str | None = Field(default=None, max_length=100)
    text: str | None = Field(default=None, max_length=4000)


def raise_ask(conn, run_id, kind, options, params=None, origin=None):
    """Ask, for a running run that is not revoked, in the caller's transaction. Returns its id."""
    if not 1 <= len(options) <= MAX_OPTIONS:
        raise ValueError("an ask offers one to three options")
    row = conn.execute("SELECT r.project_id, p.sensitivity, p.review_lock FROM runs r JOIN projects p"
                       " ON p.id = r.project_id WHERE r.id = ? AND r.status = 'running' AND r.cancel_reason IS NULL",
                       (run_id,)).fetchone()
    if row is None:
        raise AskRefused(409, "ask_closed", "The run has ended")
    ask_id = new_id()
    _event(conn, run_id, "ask", {
        "ask_id": ask_id, "kind": kind, "project_id": row[0], "options": list(options),
        "text_box": kind not in NO_TEXT, "params": params or {}, "origin": origin,
        "policy": {"level": row[1], "locked": bool(row[2])}})
    conn.execute("UPDATE runs SET waiting = 'ask' WHERE id = ?", (run_id,))
    return ask_id


def asked(conn, run_id):
    """The run's latest ask and its answer: (ask, answer), each a dict or None."""
    ask = answer = None
    for (data,) in conn.execute("SELECT data FROM run_events WHERE run_id = ? AND type IN ('ask', 'ask_answered')"
                                " ORDER BY seq", (run_id,)):
        record = json.loads(data)
        if "options" in record:
            ask, answer = record, None
        elif ask is not None and record.get("ask_id") == ask["ask_id"]:
            answer = record
    return ask, answer


def _current_policy(conn, project_id):
    row = conn.execute("SELECT sensitivity, review_lock FROM projects WHERE id = ?", (project_id,)).fetchone()
    return None if row is None else {"level": row[0], "locked": bool(row[1])}


def answer(conn, run_id, ask_id, option=None, text=None):
    """The researcher's answer, in the caller's transaction, or AskRefused having changed nothing."""
    if (option is None) == (text is None):
        raise AskRefused(400, "invalid_answer", "Answer with one option or with text")
    run = conn.execute("SELECT status, cancel_reason, waiting FROM runs WHERE id = ?", (run_id,)).fetchone()
    ask, answered = asked(conn, run_id) if run else (None, None)
    if ask is None or ask["ask_id"] != ask_id:
        raise AskRefused(404, "not_found", "No such question")
    if answered is not None or run[0] != "running" or run[1] is not None or run[2] != "ask":
        raise AskRefused(409, "ask_closed", "This question can no longer be answered")
    if _current_policy(conn, ask["project_id"]) != ask["policy"]:
        raise AskRefused(409, "ask_invalid", "The project's protection changed since this was asked")
    if option is not None and option not in ask["options"]:
        raise AskRefused(400, "invalid_answer", "That is not one of the options")
    if text is not None:
        if not ask["text_box"]:
            raise AskRefused(400, "invalid_answer", "This question takes one of its options")
        text = visible(text)
        if text is None:
            raise AskRefused(400, "invalid_answer", "The answer is empty")
    _event(conn, run_id, "ask_answered", {"ask_id": ask_id, "by": "researcher",
                                          **({"option": option} if option is not None else {"text": text})})
    conn.execute("UPDATE runs SET waiting = NULL WHERE id = ?", (run_id,))
    governance.record(conn, "ask_answered", ask["project_id"], question=ask["kind"],
                      answer=option if option is not None else "text")
    return {"ask_id": ask_id, "option": option, "answered": True}


def withdraw(conn, run_id, ask_id, reason):
    """Close an open ask that no longer applies (its project no longer needs it), in the caller's
    transaction. Returns whether it was open."""
    ask, answered = asked(conn, run_id)
    if ask is None or ask["ask_id"] != ask_id or answered is not None:
        return False
    _event(conn, run_id, "ask_answered", {"ask_id": ask_id, "withdrawn": reason})
    conn.execute("UPDATE runs SET waiting = NULL WHERE id = ?", (run_id,))
    return True


def open_asks(conn, project_id=None, conversation_id=None, run_id=None):
    """The asks that can be answered now: of running runs waiting on them, under the policy they were
    asked under; for a project, a conversation they were raised from, or a run."""
    rows = conn.execute(
        "SELECT r.id, r.workflow FROM runs r WHERE r.status = 'running' AND r.waiting = 'ask'"
        " AND r.cancel_reason IS NULL AND (?1 IS NULL OR r.project_id = ?1) AND (?2 IS NULL OR r.id = ?2)"
        " ORDER BY r.started_at", (project_id, run_id)).fetchall()
    found = []
    for run, workflow in rows:
        ask, answered = asked(conn, run)
        if ask is None or answered is not None or _current_policy(conn, ask["project_id"]) != ask["policy"]:
            continue
        if conversation_id is not None and (ask.get("origin") or {}).get("conversation_id") != conversation_id:
            continue
        found.append({"run_id": run, "workflow": workflow, **{key: ask[key] for key in (
            "ask_id", "kind", "project_id", "options", "text_box", "params")}})
    return found


@router.get("/api/asks")
async def list_asks(request: Request, project_id: str | None = None, conversation_id: str | None = None):
    db = request.app.state.scholia["db"]
    return {"asks": await asyncio.to_thread(db.read, lambda conn: open_asks(conn, project_id, conversation_id))}


@router.post("/api/runs/{run_id}/asks/{ask_id}")
async def answer_ask(run_id: str, ask_id: str, body: Answer, request: Request):
    """The one way an ask is answered: by the researcher, here."""
    db = request.app.state.scholia["db"]
    try:
        return await asyncio.to_thread(db.write, lambda conn: answer(conn, run_id, ask_id, body.option, body.text))
    except AskRefused as refused:
        return JSONResponse({"code": refused.code, "message": refused.message}, status_code=refused.status)
