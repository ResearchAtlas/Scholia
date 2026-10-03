"""Moving a conversation between projects (slice-1 spec section 5; ticket 14): only to a
project at an equal or stricter level, with its history, never while it runs a turn or
its title run."""

import asyncio

import pytest

from scholia_app import MockProvider, background_idle, send, started

pytestmark = pytest.mark.asyncio


async def level(client, project, sensitivity):
    await asyncio.to_thread(client.state["db"].write, lambda conn: conn.execute(
        "UPDATE projects SET sensitivity = ? WHERE id = ?", (sensitivity, project)))


async def rows(client, sql, *args):
    return await asyncio.to_thread(client.state["db"].read, lambda conn: conn.execute(sql, args).fetchall())


async def test_a_conversation_moves_with_its_history_and_its_spending_stays_where_it_was_spent(tmp_path):
    async with started(tmp_path / "data") as client:
        first = (await client.post("/api/projects", json={"name": "First"})).json()["id"]
        second = (await client.post("/api/projects", json={"name": "Second"})).json()["id"]
        conversation = (await client.post("/api/conversations", json={"project_id": first})).json()["id"]
        await send(client, conversation)
        await background_idle(client)
        moved = await client.post(f"/api/conversations/{conversation}/move", json={"project_id": second})
        assert moved.status_code == 200 and moved.json()["project_id"] == second
        assert [t["answer"] is not None for t in moved.json()["turns"]] == [True]
        assert await rows(client, "SELECT DISTINCT project_id FROM runs WHERE conversation_id = ?"
                                  " OR source_turn_id IN (SELECT run_id FROM turns WHERE conversation_id = ?)",
                          conversation, conversation) == [(second,)]
        assert await rows(client, "SELECT DISTINCT project_id FROM budget_reservations") == [(first,)]
        # Deleting the first project leaves the moved conversation whole.
        assert (await client.delete(f"/api/projects/{first}")).status_code == 200
        assert (await client.get(f"/api/conversations/{conversation}")).json()["turns"][0]["answer"]


@pytest.mark.parametrize("source, target, allowed", [
    ("normal", "private", True), ("private", "local_only", True), ("normal", "normal", True),
    ("private", "normal", False), ("local_only", "private", False),
])
async def test_a_conversation_moves_only_to_a_project_as_strict_or_stricter(tmp_path, source, target, allowed):
    async with started(tmp_path / "data") as client:
        a = (await client.post("/api/projects", json={"name": "A"})).json()["id"]
        b = (await client.post("/api/projects", json={"name": "B"})).json()["id"]
        await level(client, a, source)
        await level(client, b, target)
        conversation = (await client.post("/api/conversations", json={"project_id": a})).json()["id"]
        response = await client.post(f"/api/conversations/{conversation}/move", json={"project_id": b})
        if allowed:
            assert response.json()["project_id"] == b
        else:
            assert (response.status_code, response.json()["code"]) == (409, "less_strict_project")
            assert (await client.get(f"/api/conversations/{conversation}")).json()["project_id"] == a


async def test_a_running_conversation_is_not_moved_and_unknown_ones_are_404(tmp_path):
    provider = MockProvider()
    release = asyncio.Event()

    async def held(body):
        await release.wait()
        return provider.answer("Later.")

    provider.replies.append(held)
    async with started(tmp_path / "data", provider) as client:
        other = (await client.post("/api/projects", json={"name": "Other"})).json()["id"]
        conversation = (await client.post("/api/conversations", json={"title": "t"})).json()["id"]
        streaming = asyncio.create_task(client.post(f"/api/conversations/{conversation}/message/stream",
                                                    json={"content": "hi"}))
        while not provider.answers:
            await asyncio.sleep(0.01)
        response = await client.post(f"/api/conversations/{conversation}/move", json={"project_id": other})
        assert (response.status_code, response.json()["code"]) == (409, "active_run")
        release.set()
        await streaming
        missing = await client.post("/api/conversations/nope/move", json={"project_id": other})
        assert missing.status_code == 404
        await background_idle(client)


async def test_a_conversation_is_not_moved_while_its_title_run_is_pending(tmp_path):
    provider = MockProvider()
    release = asyncio.Event()

    async def held(body):
        await release.wait()
        return provider.answer("A title")

    provider.title_replies.append(held)
    async with started(tmp_path / "data", provider) as client:
        stricter = (await client.post("/api/projects", json={"name": "Stricter"})).json()["id"]
        await level(client, stricter, "local_only")
        conversation = (await client.post("/api/conversations", json={})).json()["id"]
        await send(client, conversation)  # the turn ends; its title run waits on the provider
        while not provider.titles:
            await asyncio.sleep(0.01)
        response = await client.post(f"/api/conversations/{conversation}/move", json={"project_id": stricter})
        assert (response.status_code, response.json()["code"]) == (409, "active_run")
        release.set()
        await background_idle(client)
        moved = await client.post(f"/api/conversations/{conversation}/move", json={"project_id": stricter})
        assert moved.json()["project_id"] == stricter


async def test_a_turn_admitted_across_a_move_is_refused_rather_than_run_under_the_old_project(tmp_path, monkeypatch):
    import threading
    from backend import app as app_module
    from backend import runs as runs_module
    async with started(tmp_path / "data") as client:
        other = (await client.post("/api/projects", json={"name": "Other"})).json()["id"]
        conversation = (await client.post("/api/conversations", json={"title": "t"})).json()["id"]
        moving, admitted_read = threading.Event(), threading.Event()
        real_now, real_instructions = app_module.utc_now, runs_module.load_instructions

        def held_now():  # the move is in its transaction, past the check for a running turn
            moving.set()
            admitted_read.wait(5)
            return real_now()

        def instructions(*args):  # the admission has read the conversation's (old) project
            admitted_read.set()
            return real_instructions(*args)

        monkeypatch.setattr(app_module, "utc_now", held_now)
        monkeypatch.setattr(runs_module, "load_instructions", instructions)
        move = asyncio.create_task(client.post(f"/api/conversations/{conversation}/move", json={"project_id": other}))
        await asyncio.to_thread(moving.wait, 5)
        with pytest.raises(runs_module.AdmissionError, match="moved"):
            await client.state["harness"].admit_turn(conversation, "hi")
        assert (await move).status_code == 200
        assert await rows(client, "SELECT count(*) FROM runs WHERE kind = 'turn'") == [(0,)]
