"""Where we left off (S1-21a; slice-1 spec F10 and S5, section 13; checklist H1, H2, G6): after at least 4 hours away
from the project, counted from its latest turn, the last things done, open questions, one suggested next step and
Things to confirm; nothing while memory is off but the record of what was done. The clock is the test's."""

from datetime import UTC, datetime, timedelta

import pytest

from backend import memory
from scholia_app import background_idle, send, started
from test_materials import project_of
from test_memory import conversation_in, memory_off, rows, write

pytestmark = pytest.mark.asyncio


async def left_off(client, project):
    response = await client.get(f"/api/projects/{project}/left-off")
    assert response.status_code == 200, response.text
    return response.json()


def at(monkeypatch, latest, after):
    moment = datetime.fromisoformat(latest.replace("Z", "+00:00")) + after
    monkeypatch.setattr(memory, "_clock", lambda: moment)


async def test_it_is_shown_after_four_hours_from_the_latest_turn_and_not_before(tmp_path, monkeypatch):
    async with started(tmp_path / "data") as client:
        project = await project_of(client)
        assert (await left_off(client, project))["show"] is False  # G6: no turn yet
        await send(client, await conversation_in(client, project), "We study wages")
        await background_idle(client)
        [(latest,)] = await rows(client, "SELECT max(started_at) FROM runs WHERE kind = 'turn'")
        at(monkeypatch, latest, timedelta(hours=3, minutes=59))
        assert (await left_off(client, project))["show"] is False
        at(monkeypatch, latest, timedelta(hours=4))
        found = await left_off(client, project)
        assert found["show"] is True and found["latest_turn_at"] == latest
        assert (found["done"], found["questions"], found["next_step"], found["to_confirm"]) == ([], [], None, [])
        assert (await client.get("/api/projects/not-a-project/left-off")).status_code == 404


async def test_it_reads_progress_the_agents_notes_and_the_pending_proposals(tmp_path, monkeypatch):
    async with started(tmp_path / "data") as client:
        project = await project_of(client)

        def seeded(conn):
            memory.materials_added(conn, project, [])  # none: no record
            memory.note(conn, project_id=project, run_id=None, kind="open_question", text="Is attrition random?",
                        materials=())
            memory.note(conn, project_id=project, run_id=None, kind="next_step", text="Screen the 2020 papers",
                        materials=())
            memory.note(conn, project_id=project, run_id=None, kind="next_step", text="Code the interviews",
                        materials=())
            memory._progress(conn, project, "materials_added", 3, [("project", project)],
                             lambda n, day: f"Added {n} papers to the library ({day})")
            memory.write(conn, project_id=project, scope="project", kind="decision", content="Maybe a survey",
                         status="proposed", sources=[("project", project)], structured={"source": "researcher"})
        await write(client, seeded)
        found = await left_off(client, project)
        assert [r["content"] for r in found["done"]] == [f"Added 3 papers to the library ({found['done'][0]['structured']['date']})"]
        assert [r["content"] for r in found["questions"]] == ["Is attrition random?"]
        assert found["next_step"]["content"] == "Code the interviews"  # one: the latest
        assert [r["content"] for r in found["to_confirm"]] == ["Maybe a survey"]
        await memory_off(client, project)
        found = await left_off(client, project)
        assert found["to_confirm"] == [] and found["memory_enabled"] is False and found["next_step"] is not None


async def test_keep_in_things_to_confirm_confirms_at_the_version_seen(tmp_path):
    async with started(tmp_path / "data") as client:
        project = await project_of(client)
        proposal = await write(client, lambda conn: memory.write(
            conn, project_id=project, scope="project", kind="decision", content="Maybe a survey", status="proposed",
            sources=[("project", project)]))
        [shown] = (await left_off(client, project))["to_confirm"]
        assert shown["id"] == proposal and shown["replaces"] is None
        kept = await client.post("/api/memory/review", json={"id": proposal, "action": "keep", "version": 1})
        assert kept.status_code == 200 and kept.json()["status"] == "confirmed"
        assert (await left_off(client, project))["to_confirm"] == []
        again = await client.post("/api/memory/review", json={"id": proposal, "action": "keep", "version": 1})
        assert again.status_code == 404  # no longer a proposal
