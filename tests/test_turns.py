"""Turns: admission, the commit boundaries, cancel, failures, interruption and recovery.

The app runs in process; model calls go to a test-owned mock behind the outbound
gate's transport. Titles and other detached work are in test_detached_tail.py.
"""

import asyncio
import json

import pytest

from backend import runs as runs_module
from backend.db import new_id
from scholia_app import FakeKeyring, MockProvider, background_idle, events, send, started

pytestmark = pytest.mark.asyncio


async def new_conversation(client, **body):
    response = await client.post("/api/conversations", json=body)
    assert response.status_code == 201, response.text
    return response.json()["id"]


async def rows(client, sql, *args):
    return await asyncio.to_thread(client.state["db"].read, lambda conn: conn.execute(sql, args).fetchall())


async def counts(client):
    return {table: (await rows(client, f"SELECT count(*) FROM {table}"))[0][0]
            for table in ("runs", "turns", "run_events", "budget_reservations")}


def held(provider):
    """Make the provider's next answer wait until the returned event is set."""
    release = asyncio.Event()

    async def reply(body):
        await release.wait()
        return provider.answer("A held answer.")

    provider.replies.insert(0, reply)
    return release


async def wait_for(predicate, timeout=5.0):
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while not predicate():
        if loop.time() > deadline:
            raise AssertionError("condition not reached")
        await asyncio.sleep(0.01)


def active_turn(client, conversation_id):
    return client.state["harness"].registry.turns[conversation_id]


# Admission and the commit boundaries


async def test_a_message_runs_one_turn_through_its_commit_boundaries(tmp_path):
    async with started(tmp_path / "data") as client:
        conversation = await new_conversation(client)
        stream = await send(client, conversation)

        assert [e["type"] for e in stream] == ["run_started", "step", "chat_response", "run_finished"]
        run_id = stream[0]["run_id"]
        assert stream[2]["content"] == "An answer." and stream[2]["result_saved"] is True
        assert stream[3] == {"type": "run_finished", "run_id": run_id, "status": "succeeded", "cost_usd": 0.002}
        [turn] = (await client.get(f"/api/conversations/{conversation}")).json()["turns"]
        assert (turn["status"], turn["answer"], turn["result_saved"], turn["cost_usd"]) == (
            "succeeded", {"text": "An answer."}, True, 0.002)
        assert await rows(client, "SELECT status, settled_usd, basis FROM budget_reservations WHERE run_id = ?",
                          run_id) == [("settled", 0.002, "reported")]
        types = [t for (t,) in await rows(client, "SELECT type FROM run_events WHERE run_id = ? ORDER BY seq", run_id)]
        assert types == ["route", "step_started", "model_attempt", "step_finished", "run_finished"]
        [(attempt,)] = await rows(client, "SELECT data FROM run_events WHERE run_id = ? AND type = 'model_attempt'",
                                  run_id)
        attempt = json.loads(attempt)
        assert attempt["outcome"] == "ok" and attempt["charge"] == "reported" and attempt["cost_usd"] == 0.002
        assert "What is a cohort study?" not in json.dumps(attempt)  # an attempt holds no content
        await background_idle(client)


@pytest.mark.parametrize("body, status, code", [
    ({"content": ""}, 400, "empty_message"),
    ({"content": "   "}, 400, "empty_message"),
    ({"content": "x" * 100_001}, 400, "message_too_long"),
    ({"content": "hi", "effort": "extreme"}, 400, "invalid_effort"),
    ({"content": "hi", "provider": "elsewhere"}, 400, "unknown_provider"),
    ({"content": 5}, 400, "invalid_request"),
])
async def test_a_request_that_fails_validation_writes_nothing(tmp_path, body, status, code):
    async with started(tmp_path / "data") as client:
        conversation = await new_conversation(client)
        before = await counts(client)
        response = await client.post(f"/api/conversations/{conversation}/message/stream", json=body)
        assert (response.status_code, response.json()["code"]) == (status, code)
        assert await counts(client) == before
        assert client.provider.chats == []
        assert client.state["harness"].registry.turns == {}  # the claim was released


async def test_no_provider_or_no_key_refuses_before_writing(tmp_path):
    async with started(tmp_path / "data", setup=False) as client:
        conversation = await new_conversation(client)
        response = await client.post(f"/api/conversations/{conversation}/message/stream", json={"content": "hi"})
        assert response.json()["code"] == "no_provider"
        await client.put("/api/settings", json={"hash": None, "updates": {
            "providers.openrouter.kind": "openrouter", "providers.openrouter.base_url": "https://openrouter.ai/api/v1"}})
        response = await client.post(f"/api/conversations/{conversation}/message/stream", json={"content": "hi"})
        assert response.json()["code"] == "provider_key_missing"
        assert (await counts(client))["turns"] == 0


async def test_an_unknown_conversation_is_404(tmp_path):
    async with started(tmp_path / "data") as client:
        response = await client.post(f"/api/conversations/{new_id()}/message/stream", json={"content": "hi"})
        assert (response.status_code, response.json()["code"]) == (404, "not_found")
        assert (await counts(client))["runs"] == 0


async def test_a_second_send_while_a_turn_runs_is_409_and_changes_nothing(tmp_path):
    provider = MockProvider()
    release = held(provider)
    async with started(tmp_path / "data", provider) as client:
        conversation, other = await new_conversation(client), await new_conversation(client)
        first = asyncio.create_task(send(client, conversation))
        await wait_for(lambda: provider.answers)
        before = await counts(client)

        response = await client.post(f"/api/conversations/{conversation}/message/stream", json={"content": "again"})
        assert (response.status_code, response.json()["code"]) == (409, "active_run")
        assert await counts(client) == before
        # Another conversation is not blocked.
        assert (await send(client, other))[-1]["status"] == "succeeded"

        release.set()
        assert (await first)[-1]["status"] == "succeeded"
        assert (await send(client, conversation, "again"))[-1]["status"] == "succeeded"
        await background_idle(client)


async def test_a_dropped_response_releases_its_claim_after_the_grace_period(tmp_path, monkeypatch):
    async with started(tmp_path / "data") as client:
        conversation = await new_conversation(client)
        harness = client.state["harness"]
        dropped = await harness.admit_turn(conversation, "never streamed")  # admitted, response never started
        with pytest.raises(runs_module.AdmissionError, match="already running"):
            await harness.admit_turn(conversation, "too soon")
        monkeypatch.setattr(runs_module, "STALE_CLAIM_SECONDS", 0)

        assert (await send(client, conversation, "later"))[-1]["status"] == "succeeded"
        assert await rows(client, "SELECT status, cancel_reason FROM runs WHERE id = ?", dropped.run_id) == [
            ("interrupted", None)]
        assert client.provider.answers and all(b["messages"][-1]["content"] == "later" for b in client.provider.answers)
        await background_idle(client)


async def test_instructions_and_earlier_answers_go_into_the_request(tmp_path):
    async with started(tmp_path / "data") as client:
        await client.put("/api/instructions", json={"text": "Always use British spelling."})
        conversation = await new_conversation(client)
        await send(client, conversation, "first question")
        client.provider.replies.append((500, {"error": {"message": "upstream"}}))
        await send(client, conversation, "second question")  # fails: its question stays, no answer
        await send(client, conversation, "third question")
        messages = client.provider.answers[-1]["messages"]
        assert messages[0]["role"] == "system" and "Always use British spelling." in messages[0]["content"]
        assert [(m["role"], m["content"]) for m in messages[1:]] == [
            ("user", "first question"), ("assistant", "An answer."), ("user", "third question")]
        await background_idle(client)


# Cancel


async def test_cancel_during_the_model_call_ends_the_turn_and_settles_its_estimate_once(tmp_path):
    provider = MockProvider()
    held(provider)  # never released: the call is in flight when Stop comes
    async with started(tmp_path / "data", provider) as client:
        conversation = await new_conversation(client)
        stream = asyncio.create_task(client.post(f"/api/conversations/{conversation}/message/stream",
                                                 json={"content": "hi"}))
        await wait_for(lambda: provider.answers)
        run_id = active_turn(client, conversation).run_id

        assert (await client.post(f"/api/runs/{run_id}/cancel")).json() == {"run_id": run_id, "status": "cancelling"}
        final = events(await stream)[-1]
        assert (final["type"], final["status"]) == ("run_finished", "cancelled")
        assert await rows(client, "SELECT status, cancel_reason FROM runs WHERE id = ?", run_id) == [
            ("cancelled", "researcher")]
        assert await rows(client, "SELECT answer, result_saved, reason_code, memory_status FROM turns WHERE run_id = ?",
                          run_id) == [(None, 0, "cancelled", "skipped")]
        [(estimate, settled, basis)] = await rows(
            client, "SELECT estimate_usd, settled_usd, basis FROM budget_reservations WHERE run_id = ?", run_id)
        assert (settled, basis) == (estimate, "estimated") and estimate > 0  # never shown as $0
        # Repeating is safe and reports the terminal status; an unknown run is 404.
        assert (await client.post(f"/api/runs/{run_id}/cancel")).json() == {"run_id": run_id, "status": "cancelled"}
        assert (await client.post(f"/api/runs/{new_id()}/cancel")).status_code == 404
        # No answer was committed, so no title run was written.
        assert await rows(client, "SELECT count(*) FROM runs WHERE kind = 'background'") == [(0,)]


async def test_a_stop_seen_before_the_primary_commit_publishes_no_answer(tmp_path):
    provider = MockProvider()
    async with started(tmp_path / "data", provider) as client:
        conversation = await new_conversation(client)

        def stop_then_answer(body):  # the Stop arrives while the answer is on its way back
            active_turn(client, conversation).cancel_requested.set()
            return provider.answer("Too late.")

        provider.replies.append(stop_then_answer)
        stream = await send(client, conversation)
        assert "chat_response" not in [e["type"] for e in stream]
        assert stream[-1]["status"] == "cancelled"
        [turn] = (await client.get(f"/api/conversations/{conversation}")).json()["turns"]
        assert (turn["answer"], turn["status"]) == (None, "cancelled")
        # The call itself finished and reported its cost, which is kept, once.
        assert await rows(client, "SELECT settled_usd, basis FROM budget_reservations") == [(0.002, "reported")]


async def test_a_cancel_after_the_primary_commit_keeps_the_answer(tmp_path):
    async with started(tmp_path / "data") as client:
        conversation = await new_conversation(client)
        run_id = (await send(client, conversation))[0]["run_id"]
        assert (await client.post(f"/api/runs/{run_id}/cancel")).json() == {"run_id": run_id, "status": "succeeded"}
        [turn] = (await client.get(f"/api/conversations/{conversation}")).json()["turns"]
        assert turn["answer"] == {"text": "An answer."}
        await background_idle(client)


# Failures


@pytest.mark.parametrize("status, body, reason", [
    (500, {"error": {"message": "provider exploded with secret details"}}, "other"),
    (401, {"error": {"message": "bad key"}}, "auth"),
    (402, {"error": {"message": "no credit"}}, "quota"),
    (403, {"error": {"message": "moderation"}}, "request"),
    (429, {"error": {"message": "slow down"}}, "rate_limit"),
    (200, {"choices": [{"message": {"content": "  "}}], "usage": {"cost": 0.001}}, "malformed"),
    (200, {"choices": []}, "malformed"),
])
async def test_a_provider_failure_fails_the_turn_with_its_kind_and_settles_once(tmp_path, status, body, reason):
    provider = MockProvider((status, body))
    async with started(tmp_path / "data", provider) as client:
        conversation = await new_conversation(client)
        stream = await send(client, conversation)
        assert [e["type"] for e in stream][-2:] == ["error", "run_finished"]
        assert stream[-2]["code"] == reason and stream[-1]["status"] == "failed"
        [turn] = (await client.get(f"/api/conversations/{conversation}")).json()["turns"]
        assert (turn["status"], turn["reason_code"], turn["answer"]) == ("failed", reason, None)
        [(estimate, settled, basis)] = await rows(client, "SELECT estimate_usd, settled_usd, basis FROM budget_reservations")
        if "usage" in body:  # usage on a malformed answer still counts, as reported
            assert (settled, basis) == (0.001, "reported")
        else:  # the request went out and nothing says what it cost
            assert (settled, basis) == (estimate, "estimated")
        assert len(provider.answers) == 1  # no retry


async def test_a_request_the_gate_refuses_never_leaves_and_its_reservation_is_released(tmp_path):
    provider = MockProvider()
    async with started(tmp_path / "data", provider) as client:
        project = (await client.post("/api/projects", json={"name": "Interviews"})).json()["id"]
        await asyncio.to_thread(client.state["db"].write, lambda conn: conn.execute(
            "UPDATE projects SET sensitivity = 'private' WHERE id = ?", (project,)))
        conversation = await new_conversation(client, project_id=project)
        stream = await send(client, conversation)
        assert (stream[-2]["code"], stream[-1]["status"]) == ("refused", "failed")
        assert provider.chats == []
        assert await rows(client, "SELECT status, settled_usd FROM budget_reservations") == [("released", None)]
        assert await rows(client, "SELECT json_extract(data, '$.decision'), json_extract(data, '$.reason')"
                                  " FROM audit_log WHERE event = 'outbound'") == [("deny", "private_inputs_missing")]


async def test_a_step_that_does_not_fit_the_budget_is_refused_before_any_call(tmp_path):
    provider = MockProvider()
    async with started(tmp_path / "data", provider) as client:
        project = (await client.post("/api/projects", json={"name": "Thesis"})).json()["id"]
        settings = (await client.get("/api/settings", params={"project_id": project})).json()
        response = await client.put("/api/settings", json={"project_id": project, "hash": settings["hash"],
                                                           "updates": {"project.budget_usd": 0.0001}})
        assert response.status_code == 200, response.text
        conversation = await new_conversation(client, project_id=project)
        stream = await send(client, conversation)
        assert [e["type"] for e in stream] == ["run_started", "limit_reached", "run_finished"]
        assert stream[1]["budget"] == "project" and stream[-1]["status"] == "failed"
        assert provider.chats == []
        assert (await counts(client))["budget_reservations"] == 0
        [turn] = (await client.get(f"/api/conversations/{conversation}")).json()["turns"]
        assert turn["reason_code"] == "budget"


# Interruption and recovery


async def test_a_running_turn_this_process_does_not_hold_reads_as_interrupted(tmp_path):
    async with started(tmp_path / "data") as client:
        conversation = await new_conversation(client)
        run_id = new_id()

        def orphan(conn):
            project = conn.execute("SELECT project_id FROM conversations WHERE id = ?", (conversation,)).fetchone()[0]
            conn.execute("INSERT INTO runs (id, project_id, conversation_id, kind) VALUES (?, ?, ?, 'turn')",
                         (run_id, project, conversation))
            conn.execute("INSERT INTO turns (run_id, conversation_id, seq, author, user_message)"
                         " VALUES (?, ?, 0, 'researcher', '{\"text\": \"hi\"}')", (run_id, conversation))
        await asyncio.to_thread(client.state["db"].write, orphan)
        [turn] = (await client.get(f"/api/conversations/{conversation}")).json()["turns"]
        assert turn["status"] == "interrupted"


async def test_startup_records_a_crashed_turn_as_interrupted_and_settles_its_reservation(tmp_path):
    data, keyring = tmp_path / "data", FakeKeyring()
    async with started(data, keyring=keyring) as client:
        conversation = await new_conversation(client)
        run_id = new_id()

        def crashed(conn):
            project = conn.execute("SELECT project_id FROM conversations WHERE id = ?", (conversation,)).fetchone()[0]
            conn.execute("INSERT INTO runs (id, project_id, conversation_id, kind) VALUES (?, ?, ?, 'turn')",
                         (run_id, project, conversation))
            conn.execute("INSERT INTO turns (run_id, conversation_id, seq, author, user_message)"
                         " VALUES (?, ?, 0, 'researcher', '{\"text\": \"hi\"}')", (run_id, conversation))
            conn.execute("INSERT INTO budget_reservations (id, run_id, step_seq, paying_conversation_id, project_id,"
                         " estimate_usd) VALUES (?, ?, 0, ?, ?, 0.03)", (new_id(), run_id, conversation, project))
        await asyncio.to_thread(client.state["db"].write, crashed)

    async with started(data, keyring=keyring, setup=False) as client:
        assert await rows(client, "SELECT status, settled_cost_usd FROM runs WHERE id = ?", run_id) == [
            ("interrupted", 0.03)]
        assert await rows(client, "SELECT status, settled_usd, basis FROM budget_reservations") == [
            ("settled", 0.03, "estimated")]
        [turn] = (await client.get(f"/api/conversations/{conversation}")).json()["turns"]
        assert (turn["status"], turn["reason_code"]) == ("interrupted", "interrupted")
        # The conversation accepts a new message.
        assert (await send(client, conversation, "again"))[-1]["status"] == "succeeded"
        await background_idle(client)


async def test_shutdown_interrupts_a_running_turn_and_settles_its_call(tmp_path):
    provider = MockProvider()
    held(provider)
    async with started(tmp_path / "data", provider) as client:
        conversation = await new_conversation(client)
        stream = asyncio.create_task(client.post(f"/api/conversations/{conversation}/message/stream",
                                                 json={"content": "hi"}))
        await wait_for(lambda: provider.answers)
        run_id = active_turn(client, conversation).run_id
        await client.state["harness"].shutdown(timeout=5)
        assert events(await stream)[-1]["status"] == "interrupted"
        assert await rows(client, "SELECT status, cancel_reason FROM runs WHERE id = ?", run_id) == [("interrupted", None)]
        assert (await rows(client, "SELECT basis FROM budget_reservations"))[0][0] == "estimated"
        response = await client.post(f"/api/conversations/{conversation}/message/stream", json={"content": "x"})
        assert (response.status_code, response.json()["code"]) == (503, "shutting_down")
