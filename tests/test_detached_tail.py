"""Detached post-answer work: title runs, their guard, cancel, and the recovery rules.

A title run is written in the turn's primary commit and runs outside the turn.
It writes the title only if the conversation's title is unchanged since the run
was queued (title_rev) and was not set by the researcher. On every start it
finishes from a recorded step with no model call, restarts while it has made
fewer than 2 attempts, or is marked interrupted; a finished run never re-runs.
Crash kill points are in test_durability_kill.py.
"""

import asyncio
import json

import pytest

from backend.db import new_id
from scholia_app import FakeKeyring, MockProvider, background_idle, send, started

pytestmark = pytest.mark.asyncio


async def new_conversation(client, **body):
    return (await client.post("/api/conversations", json=body)).json()["id"]


async def rows(client, sql, *args):
    return await asyncio.to_thread(client.state["db"].read, lambda conn: conn.execute(sql, args).fetchall())


async def title_run(client):
    [(run_id,)] = await rows(client, "SELECT id FROM runs WHERE kind = 'background' AND workflow = 'title'")
    return run_id


async def wait_for(predicate, timeout=5.0):
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while not predicate():
        if loop.time() > deadline:
            raise AssertionError("condition not reached")
        await asyncio.sleep(0.01)


def hold_titles(provider, text="A generated title"):
    release = asyncio.Event()

    async def reply(body):
        await release.wait()
        return provider.answer(text, cost=0.0003)

    provider.title_replies.append(reply)
    return release


async def conversation(client, conversation_id):
    return (await client.get(f"/api/conversations/{conversation_id}")).json()


async def test_the_first_answer_queues_one_title_run_that_counts_toward_the_project_only(tmp_path):
    provider = MockProvider()
    provider.title_replies.append(provider.answer("Cohort studies explained", cost=0.0003))
    async with started(tmp_path / "data", provider) as client:
        conversation_id = await new_conversation(client)
        await send(client, conversation_id)
        await background_idle(client)
        await send(client, conversation_id, "and a second question")
        await background_idle(client)

        found = await conversation(client, conversation_id)
        assert (found["title"], found["title_source"], found["title_rev"]) == ("Cohort studies explained", "generated", 1)
        assert len(provider.titles) == 1  # the second turn queued no title
        assert provider.titles[0]["messages"][1]["content"] == "What is a cohort study?"  # the researcher's words only
        run_id = await title_run(client)
        assert await rows(client, "SELECT status, attempts, settled_cost_usd, conversation_id, source_turn_id IS NOT NULL"
                                  " FROM runs WHERE id = ?", run_id) == [("succeeded", 1, 0.0003, None, 1)]
        assert await rows(client, "SELECT paying_conversation_id, settled_usd, basis FROM budget_reservations"
                                  " WHERE run_id = ?", run_id) == [(None, 0.0003, "reported")]
        activity = (await client.get("/api/activity")).json()["runs"]
        assert [(r["workflow"], r["status"], r["cost_usd"]) for r in activity] == [("title", "succeeded", 0.0003)]


async def test_a_conversation_named_by_the_researcher_gets_no_title_run(tmp_path):
    async with started(tmp_path / "data") as client:
        conversation_id = await new_conversation(client, title="My own name")
        await send(client, conversation_id)
        await background_idle(client)
        assert await rows(client, "SELECT count(*) FROM runs WHERE kind = 'background'") == [(0,)]
        assert (await conversation(client, conversation_id))["title"] == "My own name"


@pytest.mark.parametrize("new_title", ["Renamed by me", "A generated title"])
async def test_a_rename_while_the_title_run_waits_wins_even_with_the_same_text(tmp_path, new_title):
    provider = MockProvider()
    release = hold_titles(provider)
    async with started(tmp_path / "data", provider) as client:
        conversation_id = await new_conversation(client)
        await send(client, conversation_id)
        await wait_for(lambda: provider.titles)
        renamed = (await client.put(f"/api/conversations/{conversation_id}", json={"title": new_title})).json()
        assert (renamed["title_source"], renamed["title_rev"]) == ("researcher", 1)
        release.set()
        await background_idle(client)

        found = await conversation(client, conversation_id)
        assert (found["title"], found["title_source"], found["title_rev"]) == (new_title, "researcher", 1)
        assert await rows(client, "SELECT status FROM runs WHERE kind = 'background'") == [("succeeded",)]


async def test_the_next_message_is_accepted_while_the_title_runs(tmp_path):
    provider = MockProvider()
    release = hold_titles(provider)
    async with started(tmp_path / "data", provider) as client:
        conversation_id = await new_conversation(client)
        await send(client, conversation_id)
        await wait_for(lambda: provider.titles)
        assert (await send(client, conversation_id, "next"))[-1]["status"] == "succeeded"
        release.set()
        await background_idle(client)
        assert (await conversation(client, conversation_id))["title"] == "A generated title"


async def test_stop_on_the_turn_leaves_its_title_run_and_cancel_in_the_list_stops_it(tmp_path):
    provider = MockProvider()
    hold_titles(provider)  # never released
    async with started(tmp_path / "data", provider) as client:
        conversation_id = await new_conversation(client)
        turn = (await send(client, conversation_id))[0]["run_id"]
        await wait_for(lambda: provider.titles)
        title = await title_run(client)

        assert (await client.post(f"/api/runs/{turn}/cancel")).json()["status"] == "succeeded"
        assert client.state["harness"].registry.is_active(title)  # Stop on the turn left it running

        assert (await client.post(f"/api/runs/{title}/cancel")).json()["status"] == "cancelled"
        await background_idle(client)
        assert await rows(client, "SELECT status, cancel_reason FROM runs WHERE id = ?", title) == [
            ("cancelled", "researcher")]
        [(estimate, settled, basis)] = await rows(
            client, "SELECT estimate_usd, settled_usd, basis FROM budget_reservations WHERE run_id = ?", title)
        assert (settled, basis) == (estimate, "estimated")
        assert (await conversation(client, conversation_id))["title"] is None


async def test_deleting_the_conversation_while_its_title_run_waits_recreates_nothing(tmp_path):
    provider = MockProvider()
    release = hold_titles(provider)
    async with started(tmp_path / "data", provider) as client:
        conversation_id = await new_conversation(client)
        await send(client, conversation_id)
        await wait_for(lambda: provider.titles)
        title = await title_run(client)
        [(project,)] = await rows(client, "SELECT project_id FROM conversations")

        assert (await client.delete(f"/api/conversations/{conversation_id}")).json() == {"ok": True}
        release.set()
        await background_idle(client)

        assert await rows(client, "SELECT count(*) FROM conversations") == [(0,)]
        assert await rows(client, "SELECT count(*) FROM runs") == [(0,)]
        assert (await client.get(f"/api/conversations/{conversation_id}")).status_code == 404
        # Both calls' spending stays in the project, without links, settled once.
        spending = await rows(client, "SELECT run_id, paying_conversation_id, project_id, status FROM budget_reservations")
        assert sorted(spending) == [(None, None, project, "settled")] * 2
        assert not client.state["harness"].registry.is_active(title)


async def test_a_failed_title_call_fails_only_the_title_run(tmp_path):
    provider = MockProvider()
    provider.title_replies.append((500, {"error": {"message": "boom"}}))
    async with started(tmp_path / "data", provider) as client:
        conversation_id = await new_conversation(client)
        assert (await send(client, conversation_id))[-1]["status"] == "succeeded"
        await background_idle(client)
        assert await rows(client, "SELECT status FROM runs WHERE kind = 'background'") == [("failed",)]
        found = await conversation(client, conversation_id)
        assert (found["title"], found["turns"][0]["status"]) == (None, "succeeded")


# The recovery rules, applied when the app starts


async def seed_title_run(client, conversation_id, *, attempts, status="running", recorded=None, outcome="ok"):
    """A title run as a crash would leave it; recorded is the output of a finished step."""
    run_id = new_id()

    def write(conn):
        project, rev = conn.execute("SELECT project_id, title_rev FROM conversations WHERE id = ?",
                                    (conversation_id,)).fetchone()
        (turn,) = conn.execute("SELECT run_id FROM turns WHERE conversation_id = ?", (conversation_id,)).fetchone()
        conn.execute(
            "INSERT INTO runs (id, project_id, kind, workflow, source_turn_id, attempts, status, inputs)"
            " VALUES (?, ?, 'background', 'title', ?, ?, ?, ?)",
            (run_id, project, turn, attempts, status, json.dumps({
                "conversation_id": conversation_id, "title_rev": rev, "provider": "openrouter", "model": "test/model",
                "message": "What is a cohort study?"})))
        if recorded is not None:
            step = {"step": 0, "outcome": outcome, **({"output": recorded} if outcome == "ok" else {})}
            conn.execute("INSERT INTO run_events (run_id, seq, type, data) VALUES (?, 0, 'step_finished', ?)",
                         (run_id, json.dumps(step)))
    await asyncio.to_thread(client.state["db"].write, write)
    return run_id


@pytest.mark.parametrize("attempts, recorded, calls, status, title", [
    (1, "Recorded title", 0, "succeeded", "Recorded title"),  # finished from the record, no model call
    (2, "Recorded title", 0, "succeeded", "Recorded title"),
    (1, "malformed", 0, "failed", None),  # a recorded failure is finished from the record too, never retried
    (0, None, 1, "succeeded", "A short title"),  # restarted
    (1, None, 1, "succeeded", "A short title"),  # its second and last attempt
    (2, None, 0, "interrupted", None),  # no attempt left
])
async def test_a_background_run_left_running_follows_the_recovery_rules(tmp_path, attempts, recorded, calls, status, title):
    data, keyring = tmp_path / "data", FakeKeyring()
    async with started(data, keyring=keyring) as client:
        conversation_id = await new_conversation(client, title="Named first, so no title run is queued")
        await send(client, conversation_id)
        await asyncio.to_thread(client.state["db"].write, lambda conn: conn.execute(
            "UPDATE conversations SET title = NULL, title_source = NULL"))
        failed = recorded == "malformed"
        run_id = await seed_title_run(client, conversation_id, attempts=attempts, recorded=recorded,
                                      outcome="malformed" if failed else "ok")

    provider = MockProvider()
    async with started(data, provider, keyring=keyring, setup=False) as client:
        await background_idle(client)
        assert len(provider.titles) == calls
        assert await rows(client, "SELECT status, attempts FROM runs WHERE id = ?", run_id) == [
            (status, attempts + calls)]
        assert (await conversation(client, conversation_id))["title"] == title


async def test_a_finished_background_run_never_runs_again(tmp_path):
    data, keyring = tmp_path / "data", FakeKeyring()
    async with started(data, keyring=keyring) as client:
        conversation_id = await new_conversation(client)
        await send(client, conversation_id)
        await background_idle(client)
        run_id = await title_run(client)
    provider = MockProvider()
    async with started(data, provider, keyring=keyring, setup=False) as client:
        await background_idle(client)
        assert provider.chats == []
        assert await rows(client, "SELECT status, attempts FROM runs WHERE id = ?", run_id) == [("succeeded", 1)]


async def test_cancelling_a_title_run_twice_ends_it_once_and_it_never_restarts(tmp_path):
    provider = MockProvider()
    hold_titles(provider)  # never released
    async with started(tmp_path / "data", provider) as client:
        conversation_id = await new_conversation(client)
        await send(client, conversation_id)
        await wait_for(lambda: provider.titles)
        title = await title_run(client)
        first = (await client.post(f"/api/runs/{title}/cancel")).json()
        second = (await client.post(f"/api/runs/{title}/cancel")).json()
        assert first["status"] == second["status"] == "cancelled"
        await background_idle(client)
        await client.state["harness"].kick_background()  # nothing left to start
        await background_idle(client)
        assert len(provider.titles) == 1
        assert await rows(client, "SELECT status, attempts FROM runs WHERE id = ?", title) == [("cancelled", 1)]
        assert (await client.post(f"/api/runs/{title}/cancel")).json()["status"] == "cancelled"


async def test_a_second_message_while_the_title_is_pending_queues_no_second_title(tmp_path):
    provider = MockProvider()
    release = hold_titles(provider, text="The first title")
    async with started(tmp_path / "data", provider) as client:
        conversation_id = await new_conversation(client)
        await send(client, conversation_id, "first")
        await wait_for(lambda: provider.titles)
        await send(client, conversation_id, "second")
        release.set()
        await background_idle(client)
        assert await rows(client, "SELECT count(*) FROM runs WHERE workflow = 'title'") == [(1,)]
        assert len(provider.titles) == 1
        assert (await conversation(client, conversation_id))["title"] == "The first title"


async def test_a_provider_named_with_a_colon_titles_its_conversations(tmp_path):
    provider = MockProvider()
    async with started(tmp_path / "data", provider) as client:
        settings = (await client.get("/api/settings")).json()
        response = await client.put("/api/settings", json={"hash": settings["hash"], "updates": {
            '"providers"."lab:v2".kind': "openai-compatible", '"providers"."lab:v2".base_url': "http://127.0.0.1:9/v1"}})
        assert response.status_code == 200, response.text
        assert (await client.put("/api/keys/lab:v2", json={"key": "k"})).status_code == 200
        conversation_id = await new_conversation(client)
        assert (await send(client, conversation_id, provider="lab:v2", model="m:1"))[-1]["status"] == "succeeded"
        await background_idle(client)
        assert await rows(client, "SELECT status FROM runs WHERE workflow = 'title'") == [("succeeded",)]
        assert provider.titles[0]["model"] == "m:1"


@pytest.mark.parametrize("ended", ["failed", "cancelled", "interrupted"])
async def test_a_title_run_that_ended_without_a_title_is_never_replaced(tmp_path, ended):
    provider = MockProvider()
    provider.title_replies.append((500, {"error": {"message": "boom"}}))
    async with started(tmp_path / "data", provider) as client:
        conversation_id = await new_conversation(client)
        await send(client, conversation_id)
        await background_idle(client)
        await asyncio.to_thread(client.state["db"].write, lambda conn: conn.execute(
            "UPDATE runs SET status = ? WHERE workflow = 'title'", (ended,)))
        await send(client, conversation_id, "another message")
        await background_idle(client)
        assert await rows(client, "SELECT count(*) FROM runs WHERE workflow = 'title'") == [(1,)]
        assert len(provider.titles) == 1


async def test_a_cancel_that_arrives_before_the_title_is_written_wins(tmp_path):
    provider = MockProvider()
    async with started(tmp_path / "data", provider) as client:
        conversation_id = await new_conversation(client)

        def cancel_then_answer(body):  # the Stop arrives while the title is on its way back
            [active] = [a for a in client.state["harness"].registry.runs.values() if a.kind == "background"]
            active.cancel_reason = "researcher"
            active.cancel_requested.set()
            return provider.answer("Too late", cost=0.0003)

        provider.title_replies.append(cancel_then_answer)
        await send(client, conversation_id)
        await background_idle(client)
        assert await rows(client, "SELECT status, cancel_reason FROM runs WHERE workflow = 'title'") == [
            ("cancelled", "researcher")]
        assert (await conversation(client, conversation_id))["title"] is None
        # Its call finished and reported its cost, which is kept, once.
        [(basis,)] = await rows(client, "SELECT basis FROM budget_reservations WHERE paying_conversation_id IS NULL")
        assert basis == "reported"


@pytest.mark.parametrize("status", ["interrupted", "failed", "succeeded"])
async def test_every_finishing_write_of_a_background_run_honours_a_cancel_that_came_first(tmp_path, status):
    from backend.runs import ActiveRun
    async with started(tmp_path / "data") as client:
        conversation_id = await new_conversation(client, title="Named first, so no title run is queued")
        await send(client, conversation_id)
        run_id = await seed_title_run(client, conversation_id, attempts=2)
        active = ActiveRun(run_id, "background", None)
        active.cancel_reason = "researcher"
        active.cancel_requested.set()  # the Stop came while the finishing write was queued
        harness = client.state["harness"]
        await asyncio.to_thread(client.state["db"].write, lambda conn: harness._finish_background(
            conn, active, status, "Too late" if status == "succeeded" else None,
            {"conversation_id": conversation_id, "title_rev": 0}))
        assert await rows(client, "SELECT status, cancel_reason FROM runs WHERE id = ?", run_id) == [
            ("cancelled", "researcher")]
        assert (await conversation(client, conversation_id))["title"] == "Named first, so no title run is queued"


async def test_a_stop_that_comes_while_the_title_commits_is_told_the_title_was_written(tmp_path, monkeypatch):
    import threading
    from backend import runs as runs_module
    provider = MockProvider()
    async with started(tmp_path / "data", provider) as client:
        conversation_id = await new_conversation(client)
        checked, resume = threading.Event(), threading.Event()
        real = runs_module.Harness._finish_background

        def paused_after_the_check(self, conn, active, *args, **kwargs):
            running = active.cancel_requested.is_set()
            real(self, conn, active, *args, **kwargs)  # checks, writes the title, then pauses before COMMIT
            if not running:
                checked.set()
                resume.wait(5)

        monkeypatch.setattr(runs_module.Harness, "_finish_background", paused_after_the_check)
        await send(client, conversation_id)
        await asyncio.to_thread(checked.wait, 5)
        title = await title_run(client)
        cancel = asyncio.create_task(client.post(f"/api/runs/{title}/cancel"))
        await asyncio.sleep(0.1)
        resume.set()
        assert (await cancel).json()["status"] == "succeeded"  # the reply says how it ended
        await background_idle(client)
        assert (await conversation(client, conversation_id))["title"] == "A short title"
        assert await rows(client, "SELECT status FROM runs WHERE id = ?", title) == [("succeeded",)]
