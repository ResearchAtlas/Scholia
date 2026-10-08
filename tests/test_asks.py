"""The shared confirmation (slice-1 spec section 6.2; ticket 71): one answer, only through the ask
endpoint, refused once its run has ended or its project's protection has changed."""

import asyncio
import json
import re
from pathlib import Path

import pytest

from backend import asks
from backend.db import new_id
from scholia_app import started
from test_materials import project_of, rows

pytestmark = pytest.mark.asyncio


async def asking(client, project, kind="choice", options=("yes", "no"), origin=None, workflow="test"):
    """A running background run of the project, waiting on an ask: (run id, ask id)."""
    run = new_id()

    def ask(conn):
        conn.execute("INSERT INTO runs (id, project_id, kind, workflow) VALUES (?, ?, 'background', ?)",
                     (run, project, workflow))
        return asks.raise_ask(conn, run, kind, list(options), {"n": 1}, origin)

    return run, await asyncio.to_thread(client.state["db"].write, ask)


async def answer(client, run, ask, **body):
    return await client.post(f"/api/runs/{run}/asks/{ask}", json=body)


async def test_an_ask_takes_one_answer_and_records_it_without_its_text(tmp_path):
    async with started(tmp_path / "data") as client:
        project = await project_of(client)
        run, ask = await asking(client, project)
        [listed] = (await client.get("/api/asks", params={"project_id": project})).json()["asks"]
        assert (listed["run_id"], listed["ask_id"], listed["options"], listed["text_box"]) == (run, ask, ["yes", "no"], True)
        response = await answer(client, run, ask, text="  Participant seven's own words  ")
        assert response.status_code == 200
        again = await answer(client, run, ask, option="yes")
        assert (again.status_code, again.json()["code"]) == (409, "ask_closed")
        events = [json.loads(d) for (d,) in await rows(client, "SELECT data FROM run_events WHERE run_id = ? AND type"
                                                       " = 'ask_answered'", run)]
        assert events == [{"ask_id": ask, "by": "researcher", "text": "Participant seven's own words"}]
        assert await rows(client, "SELECT waiting FROM runs WHERE id = ?", run) == [(None,)]
        [(audited,)] = await rows(client, "SELECT data FROM audit_log WHERE event = 'ask_answered'")
        assert json.loads(audited) == {"question": "choice", "answer": "text"}  # never the text itself
        assert (await client.get("/api/asks", params={"project_id": project})).json()["asks"] == []


@pytest.mark.parametrize("body, code", [
    ({"text": " \u200b\u2060 "}, "invalid_answer"),  # nothing visible
    ({"text": ""}, "invalid_answer"),
    ({"option": "maybe"}, "invalid_answer"),  # not one it offers
    ({"option": "yes", "text": "and this"}, "invalid_answer"),
    ({}, "invalid_answer"),
])
async def test_an_answer_that_is_not_one_is_refused_and_changes_nothing(tmp_path, body, code):
    async with started(tmp_path / "data") as client:
        project = await project_of(client)
        run, ask = await asking(client, project)
        response = await answer(client, run, ask, **body)
        assert (response.status_code, response.json()["code"]) == (400, code)
        assert await rows(client, "SELECT count(*) FROM run_events WHERE type = 'ask_answered'") == [(0,)]
        assert await rows(client, "SELECT waiting FROM runs WHERE id = ?", run) == [("ask",)]


async def test_a_kind_without_a_text_box_takes_only_its_options(tmp_path):
    async with started(tmp_path / "data") as client:
        project = await project_of(client)
        run, ask = await asking(client, project, kind="identifier_lookup", options=("lookup", "skip"))
        refused = await answer(client, run, ask, text="please do")
        assert (refused.status_code, refused.json()["code"]) == (400, "invalid_answer")
        assert (await answer(client, run, ask, option="lookup")).status_code == 200


@pytest.mark.parametrize("change", ["cancelled", "revoked", "finished", "project deleted", "level changed",
                                    "locked", "unknown ask"])
async def test_an_ask_is_invalid_after_its_run_ends_or_its_project_changes(tmp_path, change):
    async with started(tmp_path / "data") as client:
        project = await project_of(client)
        run, ask = await asking(client, project)
        db = client.state["db"]
        if change in ("cancelled", "revoked", "finished"):
            status, reason = {"cancelled": ("cancelled", "researcher"), "revoked": ("running", "revoked"),
                              "finished": ("succeeded", None)}[change]
            await asyncio.to_thread(db.write, lambda conn: conn.execute(
                "UPDATE runs SET status = ?, cancel_reason = ? WHERE id = ?", (status, reason, run)))
        elif change == "project deleted":
            assert (await client.delete(f"/api/projects/{project}")).status_code == 200
        elif change == "level changed":
            await client.post(f"/api/projects/{project}/sensitivity", json={"level": "private"})
            await asyncio.to_thread(db.write, lambda conn: conn.execute(  # tightening revokes; keep it running here
                "UPDATE runs SET cancel_reason = NULL WHERE id = ?", (run,)))
        elif change == "locked":
            await asyncio.to_thread(db.write, lambda conn: conn.execute(
                "UPDATE projects SET sensitivity = 'local_only', review_lock = 1 WHERE id = ?", (project,)))
        response = await answer(client, run, new_id() if change == "unknown ask" else ask, option="yes")
        expected = {"project deleted": (404, "not_found"), "unknown ask": (404, "not_found"),
                    "level changed": (409, "ask_invalid"), "locked": (409, "ask_invalid")}.get(change, (409, "ask_closed"))
        assert (response.status_code, response.json()["code"]) == expected
        assert await rows(client, "SELECT count(*) FROM run_events WHERE type = 'ask_answered'") == [(0,)]
        if change != "unknown ask":
            assert (await client.get("/api/asks")).json()["asks"] == []  # not shown where it cannot be answered


async def test_asks_are_listed_where_their_work_started(tmp_path):
    async with started(tmp_path / "data") as client:
        project = await project_of(client)
        conversation = (await client.post("/api/conversations", json={"project_id": project})).json()["id"]
        _, here = await asking(client, project, origin={"conversation_id": conversation})
        _, library = await asking(client, project, workflow="lookup")  # the background-run list shows its ask
        listed = (await client.get("/api/asks", params={"conversation_id": conversation})).json()["asks"]
        assert [a["ask_id"] for a in listed] == [here]
        assert {a["ask_id"] for a in (await client.get("/api/asks", params={"project_id": project})).json()["asks"]} == {
            here, library}
        # Wherever an ask is shown, it names its project: the conversation's, the Library's and the list's.
        materials = (await client.get(f"/api/projects/{project}/materials")).json()["asks"]
        runs = [r["ask"] for r in (await client.get("/api/activity")).json()["runs"] if r.get("ask")]
        for shown in (listed, materials, runs):
            assert shown and all((a["project_name"], a["project_kind"]) == ("Thesis", "research") for a in shown)


async def test_an_ask_offers_one_to_three_options():
    for options in ([], ["a", "b", "c", "d"]):
        with pytest.raises(ValueError):
            asks.raise_ask(None, "run", "choice", options)


async def test_only_the_ask_module_writes_an_answer():
    backend = Path(__file__).resolve().parents[1] / "backend"
    writers = sorted(path.name for path in backend.rglob("*.py")
                     if re.search(r"""['"]ask_answered['"]""", path.read_text(encoding="utf-8"))
                     and path.name != "migrations.py")
    assert writers == ["asks.py"]
