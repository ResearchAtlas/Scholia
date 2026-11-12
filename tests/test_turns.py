"""Turns: admission, the commit boundaries, cancel, failures, interruption and recovery.

The app runs in process; model calls go to a test-owned mock behind the outbound
gate's transport. Titles and other detached work are in test_detached_tail.py.
"""

import asyncio
import threading
import json

import pytest

from backend import runs as runs_module
from backend.db import new_id
from scholia_app import PARAMETERS, FakeKeyring, MockProvider, background_idle, events, send, started

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

        assert [e["type"] for e in stream] == ["run_started", "route", "step", "chat_response", "run_finished"]
        run_id = stream[0]["run_id"]
        assert stream[3]["content"] == "An answer." and stream[3]["result_saved"] is True
        assert stream[4] == {"type": "run_finished", "run_id": run_id, "status": "succeeded", "cost_usd": 0.002,
                             "accounting": {"reported_usd": 0.002, "estimated_usd": 0, "unknown_attempts": 0,
                                            "complete": True}}
        [turn] = (await client.get(f"/api/conversations/{conversation}")).json()["turns"]
        assert (turn["status"], turn["answer"], turn["result_saved"], turn["cost_usd"]) == (
            "succeeded", {"text": "An answer."}, True, 0.002)
        assert await rows(client, "SELECT status, settled_usd, basis FROM budget_reservations WHERE run_id = ?",
                          run_id) == [("settled", 0.002, "reported")]
        types = [t for (t,) in await rows(client, "SELECT type FROM run_events WHERE run_id = ? ORDER BY seq", run_id)]
        assert types == ["route", "step_started", "dispatch", "model_attempt", "step_finished", "citation_checks",
                         "run_finished"]
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
    # Failed answers stay visible but never enter later prompts as the assistant's words (section 1.5,
    # S1-19): a failed turn and a refused one leave nothing in the history.
    async with started(tmp_path / "data") as client:
        await client.put("/api/instructions", json={"text": "Always use British spelling."})
        conversation = await new_conversation(client)
        await send(client, conversation, "first question")
        client.provider.replies.append((500, {"error": {"message": "upstream"}}))
        await send(client, conversation, "second question")  # fails: its question stays, no answer
        client.provider.replies.append((200, {"choices": [{"message": {"content": "Words the model wrote",
                                                                       "refusal": "Declined."}}]}))
        await send(client, conversation, "refused question")
        await send(client, conversation, "third question")
        messages = client.provider.answers[-1]["messages"]
        assert messages[0]["role"] == "system"
        assert "## AGENTS.md\nAlways use British spelling." in messages[0]["content"]  # labeled (section 8)
        assert [(m["role"], m["content"]) for m in messages[1:]] == [
            ("user", "first question"), ("assistant", "An answer."), ("user", "third question")]
        assert "Words the model wrote" not in json.dumps(client.provider.answers[-1])
        await background_idle(client)


async def test_a_turn_keeps_the_route_effort_and_request_admission_chose(tmp_path):
    # Model-route rule 5: Auto picks the route and effort once per researcher request, and every step
    # records them; a catalog read between admission and the call changes nothing for the turn.
    from backend import openrouter_client
    provider = MockProvider()
    async with started(tmp_path / "data", provider) as client:
        conversation = await new_conversation(client)
        harness = client.state["harness"]
        claim = await harness.admit_turn(conversation, "admitted at 128K", effort="low")
        model = claim.context["route"].model
        provider.endpoints[model] = [{"tag": "example", "context_length": 8192}]  # what a later read would say
        openrouter_client.clear_cache()
        [event async for event in harness.events(claim)]
        [body] = provider.answers
        assert body["max_tokens"] == 8000  # sized for the window admission read
        data = [json.loads(d) for (t, d) in await rows(
            client, "SELECT type, data FROM run_events WHERE run_id = ? ORDER BY seq", claim.run_id)
            if t in ("route", "step_started", "model_attempt")]
        assert {(d["route"], d.get("effort", "low")) for d in data} == {(f"openrouter:{model}", "low")}
        assert data[0]["effort"] == data[1]["effort"] == "low" and data[1]["window"] == 128000
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

        assert (await client.post(f"/api/runs/{run_id}/cancel")).json() == {"run_id": run_id, "status": "cancelled"}
        final = events(await stream)[-1]
        assert (final["type"], final["status"]) == ("run_finished", "cancelled")
        assert await rows(client, "SELECT status, cancel_reason FROM runs WHERE id = ?", run_id) == [
            ("cancelled", "researcher")]
        assert await rows(client, "SELECT answer, result_saved, reason_code, memory_status FROM turns WHERE run_id = ?",
                          run_id) == [(None, 0, "cancelled", "skipped")]
        [(estimate, settled, basis)] = await rows(
            client, "SELECT estimate_usd, settled_usd, basis FROM budget_reservations WHERE run_id = ?", run_id)
        assert (settled, basis) == (estimate, "estimated") and estimate > 0  # never shown as $0
        [(step,)] = await rows(client, "SELECT data FROM run_events WHERE run_id = ? AND type = 'step_started'", run_id)
        assert json.loads(step)["estimate_usd"] == estimate  # the request-sized estimate, settled once (S1-19)
        # The attempt cut off in flight is recorded, content-free, with an unknown charge; it names the exact
        # text it sent (S1-19b: stored before it was sent, in the content store, never in the event). S1-33 (PR 52
        # cycle 3): recorded from the adapter's own attempt, so with the time it had run.
        [(attempt,)] = await rows(client, "SELECT data FROM run_events WHERE run_id = ? AND type = 'model_attempt'",
                                  run_id)
        assert json.loads(attempt) == {"step": 0, "attempt": 0, "route": json.loads(attempt)["route"], "outcome": "cancelled",
                                       "http_status": None, "elapsed_ms": json.loads(attempt)["elapsed_ms"],
                                       "dispatched": True, "charge": "unknown",
                                       "retention": {"level": "normal"},  # the terms it was sent under
                                       "body": json.loads(attempt)["body"]}
        assert len(json.loads(attempt)["body"]) == 64
        [turn] = (await client.get(f"/api/conversations/{conversation}")).json()["turns"]
        assert turn["accounting"] == {"reported_usd": 0, "estimated_usd": estimate, "unknown_attempts": 1,
                                      "complete": True}
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
    (403, {"error": {"message": "moderation"}}, "policy_block"),  # OpenRouter's moderation (model-route rule 4)
    (429, {"error": {"message": "slow down"}}, "rate_limit"),
    (200, {"choices": [{"message": {"content": "  "}}], "usage": {"cost": 0.001}}, "malformed"),
    (200, {"choices": []}, "malformed"),
    # A reply of tool calls alone, to a request that offered no tools, is no answer (G6).
    (200, {"choices": [{"message": {"content": None, "tool_calls": [{"id": "c", "type": "function"}]}}],
           "usage": {"cost": 0.001}}, "malformed"),
    (200, {"choices": [{"message": {"content": None, "refusal": "I can't help with that."}}],
           "usage": {"cost": 0.001}}, "model_refusal"),
    (200, {"choices": [{"message": {"content": "Partial"}, "finish_reason": "content_filter"}],
           "usage": {"cost": 0.001}}, "policy_block"),
])
async def test_a_provider_failure_fails_the_turn_with_its_kind_and_settles_once(tmp_path, status, body, reason):
    provider = MockProvider((status, body))
    provider.parameters = [p for p in PARAMETERS if p != "tools"]  # a route without tool calls: none are offered
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


async def test_a_request_the_gate_refuses_never_leaves_and_its_reservation_is_released(tmp_path, monkeypatch):
    from backend import governance
    provider = MockProvider(catalog=["example/model"])  # listed, with no zero-retention endpoint
    async with started(tmp_path / "data", provider) as client:
        project = (await client.post("/api/projects", json={"name": "Interviews", "sensitivity": "private"})).json()["id"]
        # Admission is told the route is allowed and the key confirmed; the gate, which reads the
        # allowlist and OpenRouter's catalog itself, refuses a model with no zero-retention endpoint.
        monkeypatch.setattr(governance.Policy, "problem", lambda self, provider, model, key=None: None)
        monkeypatch.setattr(governance, "key_attested", lambda *args: True)
        real_reach = runs_module.reach.reach  # and what the request can reach is read as for Normal
        monkeypatch.setattr(runs_module.reach, "reach", lambda provider, row, listing, **kw: real_reach(provider, row, listing))
        conversation = await new_conversation(client, project_id=project)
        await client.put("/api/settings", json={"hash": (await client.get("/api/settings")).json()["hash"],
                                                "updates": {"providers.openrouter.models": "all"}})
        stream = await send(client, conversation, model="example/model")
        assert (stream[-2]["code"], stream[-1]["status"]) == ("refused", "failed")
        assert provider.chats == []
        assert await rows(client, "SELECT status, settled_usd FROM budget_reservations") == [("released", None)]
        assert await rows(client, "SELECT json_extract(data, '$.decision'), json_extract(data, '$.reason')"
                                  " FROM audit_log WHERE event = 'outbound' AND json_extract(data, '$.kind') IS NOT NULL"
                                  " AND json_extract(data, '$.decision') = 'deny'") == [("deny", "route_not_allowed")]


async def test_a_step_that_does_not_fit_the_budget_is_refused_before_any_call(tmp_path):
    provider = MockProvider()
    async with started(tmp_path / "data", provider) as client:
        project = (await client.post("/api/projects", json={"name": "Thesis"})).json()["id"]
        settings = (await client.get("/api/settings", params={"project_id": project})).json()
        response = await client.put("/api/settings", json={"project_id": project, "hash": settings["hash"],
                                                           "updates": {"project.budget_usd": 0.0001}})
        assert response.status_code == 200, response.text
        conversation = await new_conversation(client, project_id=project)
        sending = asyncio.create_task(send(client, conversation))
        from test_agent_loop import asked
        ask = await asked(client, conversation)  # S1-33: it asks before the step; Not now ends it at the limit
        await client.post(f"/api/runs/{ask['run_id']}/asks/{ask['ask_id']}", json={"option": "not_now"})
        stream = await sending
        assert [e["type"] for e in stream] == ["run_started", "route", "ask", "limit_reached", "run_finished"]
        assert stream[3]["budget"] == "project" and stream[-1]["status"] == "cancelled"
        assert provider.chats == []
        assert (await counts(client))["budget_reservations"] == 0
        [turn] = (await client.get(f"/api/conversations/{conversation}")).json()["turns"]
        assert (turn["reason_code"], turn["cancel_reason"]) == ("budget", "limit")  # stopped at a limit
        # Once the budget is raised, Continue sends the message again.
        settings = (await client.get("/api/settings", params={"project_id": project})).json()
        await client.put("/api/settings", json={"project_id": project, "hash": settings["hash"],
                                                "updates": {"project.budget_usd": 50}})
        continued = events(await client.post(f"/api/runs/{turn['run_id']}/continue"))
        assert continued[-1]["status"] == "succeeded" and len(provider.answers) == 1


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
        # A call was in flight at the crash: its cost counts at the estimate and the record says it is incomplete.
        assert turn["accounting"] == {"reported_usd": 0, "estimated_usd": 0.03, "unknown_attempts": 0,
                                      "complete": False}
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


async def test_a_stop_before_the_stream_starts_prevents_any_dispatch(tmp_path):
    async with started(tmp_path / "data") as client:
        conversation = await new_conversation(client)
        harness = client.state["harness"]
        claim = await harness.admit_turn(conversation, "hi")  # admitted; its response has not started
        assert (await client.post(f"/api/runs/{claim.run_id}/cancel")).json()["status"] == "cancelled"
        assert [event async for event in harness.events(claim)] == []  # the late response streams nothing
        assert client.provider.chats == []
        assert await rows(client, "SELECT status, cancel_reason FROM runs WHERE id = ?", claim.run_id) == [
            ("cancelled", "researcher")]
        assert (await counts(client))["budget_reservations"] == 0
        assert (await send(client, conversation, "again"))[-1]["status"] == "succeeded"
        await background_idle(client)


# Cases from review


async def test_a_reply_recorded_after_its_conversation_was_deleted_still_settles_as_reported(tmp_path):
    from backend.db import delete
    provider = MockProvider()
    arrived = asyncio.Event()
    release = asyncio.Event()

    async def reply(body):
        arrived.set()
        await release.wait()
        return provider.answer("late", cost=0.0015)

    provider.replies.append(reply)
    async with started(tmp_path / "data", provider) as client:
        conversation = await new_conversation(client, title="t")
        stream = asyncio.create_task(send(client, conversation))
        await arrived.wait()
        # The deletion commits between the reply and its record (no revocation reaches the turn here).
        await asyncio.to_thread(delete, client.state["db"], client.state["content"], "conversation", conversation)
        release.set()
        stream = await stream
        assert "chat_response" not in [e["type"] for e in stream]  # nothing is published for a deleted turn
        assert stream[-1]["status"] == "deleted"
        assert await rows(client, "SELECT run_id, paying_conversation_id, status, settled_usd, basis"
                                  " FROM budget_reservations") == [(None, None, "settled", 0.0015, "reported")]
        assert await rows(client, "SELECT count(*) FROM runs") == [(0,)]


async def test_a_closed_response_cancels_its_turn_even_while_send_is_blocked(tmp_path):
    provider = MockProvider()
    held(provider)
    async with started(tmp_path / "data", provider) as client:
        conversation = await new_conversation(client, title="t")
        disconnect, blocked = asyncio.Event(), asyncio.Event()
        body = json.dumps({"content": "hi"}).encode()
        messages = [{"type": "http.request", "body": body, "more_body": False}]

        async def receive():
            if messages:
                return messages.pop(0)
            await disconnect.wait()
            return {"type": "http.disconnect"}

        async def send_blocked(message):
            if message["type"] == "http.response.body" and message.get("body"):
                blocked.set()
                await asyncio.Event().wait()  # the client stopped reading

        scope = {"type": "http", "asgi": {"version": "3.0", "spec_version": "2.4"}, "http_version": "1.1",
                 "method": "POST", "path": f"/api/conversations/{conversation}/message/stream",
                 "raw_path": b"", "query_string": b"", "scheme": "http", "client": ("127.0.0.1", 1),
                 "server": ("127.0.0.1", 8765),
                 "headers": [(b"host", b"127.0.0.1:8765"), (b"x-scholia-client", b"local"),
                             (b"content-type", b"application/json"), (b"content-length", str(len(body)).encode())]}
        response = asyncio.create_task(client.app(scope, receive, send_blocked))
        await asyncio.wait_for(blocked.wait(), 5)
        run_id = active_turn(client, conversation).run_id
        disconnect.set()
        await asyncio.wait_for(response, 5)
        await wait_for(lambda: not client.state["harness"].registry.is_active(run_id))
        assert await rows(client, "SELECT status, cancel_reason FROM runs WHERE id = ?", run_id) == [
            ("cancelled", "researcher")]


async def test_a_response_that_fails_before_it_starts_leaves_no_claim(tmp_path):
    async with started(tmp_path / "data") as client:
        conversation = await new_conversation(client, title="t")
        body = json.dumps({"content": "hi"}).encode()
        messages = [{"type": "http.request", "body": body, "more_body": False}]

        async def receive():
            return messages.pop(0) if messages else {"type": "http.disconnect"}

        async def send_fails(message):
            raise OSError("the client went away")

        scope = {"type": "http", "asgi": {"version": "3.0"}, "http_version": "1.1", "method": "POST",
                 "path": f"/api/conversations/{conversation}/message/stream", "raw_path": b"", "query_string": b"",
                 "scheme": "http", "client": ("127.0.0.1", 1), "server": ("127.0.0.1", 8765),
                 "headers": [(b"host", b"127.0.0.1:8765"), (b"x-scholia-client", b"local"),
                             (b"content-type", b"application/json"), (b"content-length", str(len(body)).encode())]}
        with pytest.raises(Exception):
            await client.app(scope, receive, send_fails)
        await wait_for(lambda: not client.state["harness"].registry.turns)
        assert (await send(client, conversation, "again"))[-1]["status"] == "succeeded"  # no 409 left behind
        assert client.provider.answers and all(b["messages"][-1]["content"] == "again" for b in client.provider.answers)


async def test_a_call_cancelled_during_the_gate_check_is_released_not_charged(tmp_path, monkeypatch):
    import time
    from backend import providers as providers_module
    real = providers_module.gate_inputs
    checking = asyncio.Event()
    loop = asyncio.get_running_loop()

    def slow_inputs(data_root):
        turn = client.state["harness"].registry.turns.get(conversation)
        if turn is not None and turn.call is not None:  # the turn's own call, reserved, is at the gate
            loop.call_soon_threadsafe(checking.set)
        time.sleep(0.3)  # the gate is still checking when Stop arrives
        return real(data_root)

    async with started(tmp_path / "data") as client:
        await send(client, await new_conversation(client, title="t"))  # the catalog and endpoints, read once
        # PR 52 (CI on 82e95ab): its memory extraction is done before the patch, so only the turn's own call is checked
        # after it (a late extraction's check had set the signal before the turn reserved: no reservation to release)
        await background_idle(client)
        conversation = await new_conversation(client, title="t")
        monkeypatch.setattr(providers_module, "gate_inputs", slow_inputs)
        stream = asyncio.create_task(send(client, conversation))
        await asyncio.wait_for(checking.wait(), 5)
        run_id = active_turn(client, conversation).run_id
        await client.post(f"/api/runs/{run_id}/cancel")
        assert (await stream)[-1]["status"] == "cancelled"
        assert len(client.provider.chats) == 2  # the first conversation's turn and its extraction only
        assert await rows(client, "SELECT status, settled_usd FROM budget_reservations WHERE run_id = ?",
                          run_id) == [("released", None)]


async def test_a_receipt_is_recorded_even_if_stop_comes_while_the_client_closes(tmp_path, monkeypatch):
    import httpx
    real_exit = httpx.AsyncClient.__aexit__
    closing = asyncio.Event()

    async def slow_exit(self, *exc):
        closing.set()
        await asyncio.sleep(0.3)
        await real_exit(self, *exc)

    async with started(tmp_path / "data") as client:
        await send(client, await new_conversation(client, title="t"))  # the catalog and endpoints, read once
        await background_idle(client)  # S1-21a: its extraction's client closes before this test's does
        conversation = await new_conversation(client, title="t")
        monkeypatch.setattr(httpx.AsyncClient, "__aexit__", slow_exit)
        stream = asyncio.create_task(send(client, conversation))
        await asyncio.wait_for(closing.wait(), 5)
        run_id = active_turn(client, conversation).run_id
        await client.post(f"/api/runs/{run_id}/cancel")
        assert (await stream)[-1]["status"] == "cancelled"
        assert await rows(client, "SELECT status, settled_usd, basis FROM budget_reservations WHERE run_id = ?",
                          run_id) == [("settled", 0.002, "reported")]
        assert await rows(client, "SELECT count(*) FROM run_events WHERE type = 'model_attempt' AND run_id = ?",
                          run_id) == [(1,)]


async def test_an_answer_that_cannot_be_saved_is_still_shown_marked_unsaved(tmp_path, monkeypatch):
    async with started(tmp_path / "data") as client:
        conversation = await new_conversation(client, title="t")

        def disk_full(self, conn, claim, answer, ctx):
            raise OSError("disk full")

        monkeypatch.setattr(runs_module.Harness, "_commit_answer", disk_full)
        stream = await send(client, conversation)
        response = next(e for e in stream if e["type"] == "chat_response")
        assert (response["content"], response["result_saved"], response["error"]) == ("An answer.", False, "save_failed")
        assert stream[-1]["status"] == "failed"
        [turn] = (await client.get(f"/api/conversations/{conversation}")).json()["turns"]
        assert (turn["status"], turn["reason_code"], turn["answer"]) == ("failed", "save_failed", None)
        assert turn["accounting"] == {"reported_usd": 0.002, "estimated_usd": 0, "unknown_attempts": 0,
                                      "complete": True}


@pytest.mark.parametrize("cost", [float("inf"), float("nan"), -0.5, "0.1", True])
async def test_an_invalid_reported_cost_neither_breaks_the_turn_nor_counts(tmp_path, cost):
    provider = MockProvider()
    import json as json_module
    body = json_module.dumps({"choices": [{"message": {"content": "Fine."}}],
                              "usage": {"prompt_tokens": 1000, "completion_tokens": 1000, "cost": cost}})
    provider.replies.append((200, body.encode()))  # Infinity and NaN as some providers write them
    async with started(tmp_path / "data", provider) as client:
        conversation = await new_conversation(client, title="t")
        stream = await send(client, conversation)
        assert stream[-1]["status"] == "succeeded"
        # Settled from the reported tokens at the price for the model, marked estimated.
        [(settled, basis)] = await rows(client, "SELECT settled_usd, basis FROM budget_reservations")
        assert basis == "estimated" and settled == pytest.approx(0.006)
        [turn] = (await client.get(f"/api/conversations/{conversation}")).json()["turns"]
        # Its tokens priced it, so it is estimated, not unknown.
        assert turn["accounting"]["unknown_attempts"] == 0 and turn["accounting"]["reported_usd"] == 0


async def test_a_providers_settings_and_key_do_not_change_while_a_run_uses_it(tmp_path):
    provider = MockProvider()
    release = held(provider)
    data = tmp_path / "data"
    async with started(data, provider) as client:
        conversation = await new_conversation(client, title="t")
        stream = asyncio.create_task(send(client, conversation))
        await wait_for(lambda: provider.answers)
        before = (data / "config.toml").read_text()
        settings = (await client.get("/api/settings")).json()
        for response in (
            await client.put("/api/keys/openrouter", json={"key": "new"}),
            await client.post("/api/setup", json={"openrouter_key": "new"}),
            await client.put("/api/settings", json={"hash": settings["hash"], "updates": {
                "ui.language": "en", "providers.openrouter.base_url": "https://openrouter.ai/api/v2"}}),
            await client.put("/api/settings", json={"hash": settings["hash"], "updates": {
                "ui.language": "en", '"providers".openrouter.base_url': "https://openrouter.ai/api/v2"}}),
            await client.put("/api/settings", json={"hash": settings["hash"], "updates": {
                "ui.language": "en", "providers": None}}),
            await client.put("/api/settings", json={"hash": settings["hash"], "updates": {
                "providers": {"other": {"kind": "openrouter", "base_url": "https://example.org/v1"}}}}),
        ):
            assert (response.status_code, response.json()["code"]) == (409, "active_run")
        assert (data / "config.toml").read_text() == before  # a mixed update changed no field
        assert client.keyring.keys[(runs_module.credentials.SERVICE, "openrouter")] != "new"
        unrelated = await client.put("/api/settings", json={"hash": settings["hash"], "updates": {"ui.language": "en"}})
        assert unrelated.status_code == 200
        release.set()
        await stream
        await background_idle(client)
        assert (await client.put("/api/keys/openrouter", json={"key": "new"})).status_code == 200


async def test_the_turn_waits_for_its_reader_before_each_step(tmp_path):
    """Pull: a reader that has the first event and has not asked for the next holds the turn
    before its reservation and its model call."""
    async with started(tmp_path / "data") as client:
        conversation = await new_conversation(client, title="t")
        harness = client.state["harness"]
        claim = await harness.admit_turn(conversation, "hi")
        events_ = harness.events(claim)
        first = await anext(events_)
        assert first["type"] == "run_started"
        await asyncio.sleep(0.2)  # the reader is busy with the first event
        assert (await counts(client))["budget_reservations"] == 0 and client.provider.chats == []
        assert (await anext(events_))["type"] == "route"  # the route is decided before any reservation
        assert (await counts(client))["budget_reservations"] == 0 and client.provider.chats == []
        second = await anext(events_)
        assert second["type"] == "step"
        await asyncio.sleep(0.2)
        assert (await counts(client))["budget_reservations"] == 1 and client.provider.chats == []
        rest = [event async for event in events_]
        assert [e["type"] for e in rest] == ["chat_response", "run_finished"]
        assert len(client.provider.chats) == 1
        await background_idle(client)


async def test_shutdown_waits_for_detached_work_before_the_database_closes(tmp_path):
    """Detached work (starting background runs) uses the database: it is finished, not
    left running, when the app stops and the database closes."""
    async with started(tmp_path / "data") as client:
        conversation = await new_conversation(client)
        await send(client, conversation)  # its end detaches the start of its title run
        harness = client.state["harness"]
        detached = set(harness._tasks)
    assert detached and all(task.done() for task in detached)
    assert all(task.done() for task in harness._tasks)
    assert not harness.registry.runs


async def test_a_forced_shutdown_never_closes_the_database_under_running_work(tmp_path):
    """Work that outlasts shutdown's bounded wait keeps its connection until it finishes;
    the database closes after it, and refuses anything later."""
    import threading
    from backend.db import DatabaseClosedError
    async with started(tmp_path / "data") as client:
        harness, db = client.state["harness"], client.state["db"]
        loop = asyncio.get_running_loop()
        reading, release = asyncio.Event(), threading.Event()

        def slow(conn):
            loop.call_soon_threadsafe(reading.set)
            release.wait(5)
            return conn.execute("SELECT count(*) FROM projects").fetchone()

        held = harness._detach(asyncio.to_thread(db.read, slow))
        await reading.wait()
        assert await harness.shutdown(timeout=0.1) == 1  # the bound passes with the read still running
        closing = asyncio.ensure_future(asyncio.to_thread(db.close))
        await asyncio.sleep(0.2)
        assert not closing.done()  # waiting for the read, not closing its connection under it
        release.set()
        assert await held == (1,)
        await closing
        with pytest.raises(DatabaseClosedError):
            await asyncio.to_thread(db.read, lambda conn: None)


@pytest.mark.parametrize("first, second, settled", [
    # Both attempts report their cost: the call settles at their sum, reported.
    ((400, {"error": {}, "usage": {"prompt_tokens": 10, "completion_tokens": 0, "cost": 0.001}}),
     {"prompt_tokens": 10, "completion_tokens": 10, "cost": 0.002}, (0.003, "reported")),
    # Token counts and no cost: each is priced from its tokens (unknown price: $1 in, $5 out per
    # million), marked estimated.
    ((400, {"error": {}, "usage": {"prompt_tokens": 1000, "completion_tokens": 0}}),
     {"prompt_tokens": 1000, "completion_tokens": 1000}, (0.007, "estimated")),
    # A rejection that went out and reported nothing keeps the estimate (F11).
    ((400, {"error": {}}), {"prompt_tokens": 10, "completion_tokens": 10, "cost": 0.002}, ("estimate", "estimated")),
])
async def test_a_call_settles_from_every_attempt_it_made(tmp_path, monkeypatch, first, second, settled):
    from backend import openrouter
    monkeypatch.setattr(openrouter, "resolve_model_reasoning",
                        lambda route, effort, **kw: ({"effort": "high"}, None))
    provider = MockProvider(first, (200, {"choices": [{"message": {"content": "ok"}}], "usage": second}))
    async with started(tmp_path / "data", provider) as client:
        settings = (await client.get("/api/settings")).json()
        await client.put("/api/settings", json={"hash": settings["hash"], "updates": {
            "providers.lab.kind": "openai-compatible", "providers.lab.base_url": "http://127.0.0.1:9/v1"}})
        await client.put("/api/keys/lab", json={"key": "k"})
        conversation = await new_conversation(client, title="t")
        assert (await send(client, conversation, provider="lab", model="m"))[-1]["status"] == "succeeded"
        assert len(provider.answers) == 2  # the field was rejected once, then sent without it
        [(estimate, amount, basis)] = await rows(
            client, "SELECT estimate_usd, settled_usd, basis FROM budget_reservations")
        assert estimate > 0.002
        assert (round(amount, 9), basis) == ((estimate if settled[0] == "estimate" else settled[0]), settled[1])
        [turn] = (await client.get(f"/api/conversations/{conversation}")).json()["turns"]
        # Only an attempt that went out and reported nothing usable counts as unknown.
        assert turn["accounting"]["unknown_attempts"] == (1 if settled[0] == "estimate" else 0)


async def test_a_claim_still_being_admitted_is_never_released_as_stale(tmp_path, monkeypatch):
    async with started(tmp_path / "data") as client:
        conversation = await new_conversation(client)
        harness = client.state["harness"]
        loading = threading.Event()
        release = threading.Event()
        real = runs_module.load_instructions

        def slow_instructions(*args):  # the first admission stalls before its run is written
            loading.set()
            release.wait(5)
            return real(*args)

        monkeypatch.setattr(runs_module, "load_instructions", slow_instructions)
        monkeypatch.setattr(runs_module, "STALE_CLAIM_SECONDS", 0)
        first = asyncio.create_task(harness.admit_turn(conversation, "slow"))
        await asyncio.to_thread(loading.wait, 5)
        with pytest.raises(runs_module.AdmissionError, match="already running"):
            await harness.admit_turn(conversation, "second")
        release.set()
        claim = await first
        stream = [event async for event in harness.events(claim)]
        assert stream[-1]["status"] == "succeeded"
        await background_idle(client)


async def test_a_stop_before_the_turns_task_first_runs_ends_it_cancelled(tmp_path):
    async with started(tmp_path / "data") as client:
        conversation = await new_conversation(client)
        harness = client.state["harness"]
        claim = await harness.admit_turn(conversation, "stopped at once")
        claim.task = asyncio.create_task(harness._turn(claim))
        harness._request_cancel(claim, "researcher")  # before the task's first step
        await claim.task
        assert await rows(client, "SELECT status, cancel_reason FROM runs WHERE id = ?", claim.run_id) == [
            ("cancelled", "researcher")]
        assert not harness.registry.is_active(claim.run_id)
        assert (await send(client, conversation, "next"))[-1]["status"] == "succeeded"  # the claim is free
        await background_idle(client)


@pytest.mark.parametrize("handler", ["cancelled", "budget"])
async def test_a_turn_whose_terminal_write_fails_reads_interrupted_once_released(tmp_path, monkeypatch, handler):
    async with started(tmp_path / "data") as client:
        project = (await client.post("/api/projects", json={"name": "Thesis"})).json()["id"]
        if handler == "budget":  # its first step does not fit: the budget handler writes the terminal record
            settings = (await client.get("/api/settings", params={"project_id": project})).json()
            await client.put("/api/settings", json={"project_id": project, "hash": settings["hash"],
                                                    "updates": {"project.budget_usd": 0.0001}})
        conversation = await new_conversation(client, project_id=project)
        harness = client.state["harness"]
        claim = await harness.admit_turn(conversation, "a question")

        def failing(*args, **kwargs):  # the terminal write raises, so nothing of it commits
            raise RuntimeError("the terminal write failed")

        monkeypatch.setattr(runs_module.Harness, "_finish_turn", failing)
        if handler == "cancelled":
            claim.task = asyncio.create_task(harness._turn(claim))
            harness._request_cancel(claim, "researcher")  # its cancellation handler writes the terminal record
        else:  # a reader pulls it to its first step, which does not fit: it asks (S1-33), and Not now ends it at the limit
            stream = harness.events(claim)
            assert [(await anext(stream))["type"] for _ in range(3)] == ["run_started", "route", "ask"]
            [ask] = (await client.get("/api/asks", params={"conversation_id": conversation})).json()["asks"]
            await client.post(f"/api/runs/{claim.run_id}/asks/{ask['ask_id']}", json={"option": "not_now"})
            assert [event["type"] async for event in stream] == []
        with pytest.raises(RuntimeError):
            await claim.task
        assert not harness.registry.is_active(claim.run_id) and claim.run_id not in harness.registry.finished
        [turn] = (await client.get(f"/api/conversations/{conversation}")).json()["turns"]
        assert turn["status"] == "interrupted"  # its record says running and nothing here holds it
        await background_idle(client)


@pytest.mark.parametrize("ending", ["cancelled", "succeeded"])
async def test_a_read_taken_before_a_turns_terminal_record_commits_never_reads_it_interrupted(tmp_path, monkeypatch,
                                                                                            ending):
    """Main CI on 7cff1e7 (the loopback test in test_local_guard.py): a poll's read, taken while a turn's terminal record
    was still to commit, whose status was worked out once the turn had been released, read it interrupted with no
    cancel reason. A turn whose terminal record has committed is known finished before it is released, as a background
    run is: that read says running, and the next one reads how it ended."""
    from backend.runs import Registry, derived_status
    real_release, seen = Registry.release, []

    def release(self, active):  # the moment after the turn's hold is released, as a read taken before its commit reads it
        real_release(self, active)
        if active.kind == "turn":
            seen.append(derived_status("running", active.run_id, self))

    monkeypatch.setattr(Registry, "release", release)
    provider = MockProvider()
    if ending == "cancelled":
        held(provider)  # never released: the call is in flight when its stream closes
    async with started(tmp_path / "data", provider) as client:
        conversation = await new_conversation(client)
        stream = asyncio.create_task(client.post(f"/api/conversations/{conversation}/message/stream",
                                                 json={"content": "hi"}))
        if ending == "cancelled":
            await wait_for(lambda: provider.answers)
            client.state["harness"].stream_closed(active_turn(client, conversation))  # as a closed connection does
        assert events(await stream)[-1]["status"] == ending
        assert seen == ["running"]
        [turn] = (await client.get(f"/api/conversations/{conversation}")).json()["turns"]
        assert (turn["status"], turn["cancel_reason"]) == {"cancelled": ("cancelled", "researcher"),
                                                           "succeeded": ("succeeded", None)}[ending]
        await background_idle(client)


@pytest.mark.parametrize("by", ["stop", "disconnect"])
async def test_a_turn_stopped_before_its_response_starts_never_reads_interrupted(tmp_path, monkeypatch, by):
    """PR 46 review: a claim whose response never started was released before its terminal write, so a read meanwhile,
    or one taken before that write committed, read it interrupted. Its conversation is free at once, as before; the run
    stays held until its terminal record commits, and is known finished from then on."""
    from backend.runs import Registry, derived_status
    real_release, real_finish, seen, gate = Registry.release, runs_module.Harness._finish_turn, [], threading.Event()

    def release(self, active):  # the moment after its hold is released, as a read taken before its commit reads it
        real_release(self, active)
        if active.kind == "turn":
            seen.append((active.run_id, derived_status("running", active.run_id, self)))

    def finish(self, conn, *args, **kwargs):  # its terminal write waits until the reads below are done
        gate.wait(10)
        return real_finish(self, conn, *args, **kwargs)

    monkeypatch.setattr(Registry, "release", release)
    monkeypatch.setattr(runs_module.Harness, "_finish_turn", finish)
    async with started(tmp_path / "data") as client:
        conversation = await new_conversation(client)
        harness = client.state["harness"]
        claim = await harness.admit_turn(conversation, "never started")
        stopped = asyncio.create_task(harness.cancel(claim.run_id)) if by == "stop" else None
        if stopped is None:
            harness.stream_closed(claim)  # its response ended before it started
        await wait_for(lambda: claim.closing is not None)
        reading = (await client.get(f"/api/conversations/{conversation}")).json()
        assert reading["running"] is False and reading["turns"][0]["status"] == "running"  # never interrupted
        newer = harness.registry.claim_turn(conversation)  # a new message may be sent at once
        gate.set()
        ended = await (stopped if stopped is not None else claim.closing)
        assert (ended["status"], ended["cancel_reason"]) == ("cancelled", "researcher")
        assert harness.registry.turns[conversation] is newer  # the old run's release leaves the newer claim held
        harness.registry.release(newer)
        assert [status for run, status in seen if run == claim.run_id] == ["running"]
        assert not harness.registry.is_active(claim.run_id)
        [turn] = (await client.get(f"/api/conversations/{conversation}")).json()["turns"]
        assert (turn["status"], turn["cancel_reason"]) == ("cancelled", "researcher")
        await background_idle(client)


@pytest.mark.parametrize("at", ["before_its_write_starts", "during_its_write"])
async def test_a_stop_request_cancelled_while_it_waits_never_cancels_the_terminal_write_of_a_turn_never_started(
        tmp_path, monkeypatch, at):
    """PR 46 recheck: a Stop request cancelled while it waited for the terminal write of a turn whose response never
    started cancelled that write's task, so the run was released before its record committed and never known finished
    (it read interrupted), or, cancelled before the task first ran, held for good. The write runs to its end whatever
    becomes of its waiters."""
    real_ended, real_finish = runs_module.Harness._ended, runs_module.Harness._finish_turn
    entered, writing, gate = [], threading.Event(), threading.Event()

    async def ended(self, active, fn):
        entered.append(active.run_id)
        return await real_ended(self, active, fn)

    def finish(self, conn, *args, **kwargs):  # its terminal write waits until the request is cancelled
        writing.set()
        gate.wait(10)
        return real_finish(self, conn, *args, **kwargs)

    monkeypatch.setattr(runs_module.Harness, "_ended", ended)
    monkeypatch.setattr(runs_module.Harness, "_finish_turn", finish)
    async with started(tmp_path / "data") as client:
        conversation = await new_conversation(client)
        harness = client.state["harness"]
        claim = await harness.admit_turn(conversation, "never started")
        stop = asyncio.create_task(harness.cancel(claim.run_id))
        if at == "before_its_write_starts":
            await asyncio.sleep(0)  # the request has asked and waits; the write's task has not run yet
            assert claim.closing is not None and entered == []
            gate.set()
        else:
            await asyncio.to_thread(writing.wait, 10)
        stop.cancel()
        with pytest.raises(asyncio.CancelledError):
            await stop
        if at == "during_its_write":
            assert harness.registry.is_active(claim.run_id)  # still held while its write runs
            [turn] = (await client.get(f"/api/conversations/{conversation}")).json()["turns"]
            assert turn["status"] == "running"
            gate.set()
        await asyncio.wait({claim.closing}, timeout=10)
        assert claim.closing.done() and not claim.closing.cancelled()
        assert not harness.registry.is_active(claim.run_id) and claim.run_id in harness.registry.finished
        assert claim.provider is not None and not harness.provider_busy(claim.provider)  # nothing held for good
        [turn] = (await client.get(f"/api/conversations/{conversation}")).json()["turns"]
        assert (turn["status"], turn["cancel_reason"]) == ("cancelled", "researcher")
        await background_idle(client)


@pytest.mark.parametrize("write", ["answer", "provider_failure"])
async def test_a_terminal_write_that_commits_while_its_turn_is_stopped_marks_it_finished_however_its_cleanup_ends(
        tmp_path, monkeypatch, write):
    """PR 46 recheck 3: a Stop during a turn's terminal write (its answer, or a provider failure) cancelled the await
    while the writer went on and committed; when the Stop's own write then failed, the turn was released never known
    finished, and a read taken before that commit read it interrupted. The writer marks it finished as it commits.
    (PR 52 cycle 4: a Stop that lands once the write began now waits for it, so the turn ends as that write recorded
    it and the Stop has no write of its own; the stub that fails it stays to show none is made.)"""
    from backend.runs import Registry, derived_status
    real_release, real_commit, real_finish = Registry.release, runs_module.Harness._commit_answer, \
        runs_module.Harness._finish_turn
    seen, writing, gate, calls = [], threading.Event(), threading.Event(), []

    def release(self, active):  # the moment after the turn's hold is released, as a read taken before its commit reads it
        real_release(self, active)
        if active.kind == "turn":
            seen.append(derived_status("running", active.run_id, self))

    def held(real):  # the terminal write, its transaction done and still to commit until the Stop has come
        def run(self, conn, *args, **kwargs):
            result = real(self, conn, *args, **kwargs)
            writing.set()
            gate.wait(10)
            return result
        return run

    def finish(self, conn, *args, **kwargs):  # the provider failure's write is held; the Stop's own write fails
        calls.append(args)
        if write == "provider_failure" and len(calls) == 1:
            return held(real_finish)(self, conn, *args, **kwargs)
        raise RuntimeError("the Stop's write failed")

    monkeypatch.setattr(Registry, "release", release)
    monkeypatch.setattr(runs_module.Harness, "_finish_turn", finish)
    if write == "answer":
        monkeypatch.setattr(runs_module.Harness, "_commit_answer", held(real_commit))
    provider = MockProvider(*([(401, {"error": {"message": "bad key"}})] if write == "provider_failure" else []))
    async with started(tmp_path / "data", provider) as client:
        conversation = await new_conversation(client)
        harness = client.state["harness"]
        claim = await harness.admit_turn(conversation, "a question")

        async def read():  # a reader pulls the turn to its terminal write
            return [event async for event in harness.events(claim)]
        reader = asyncio.create_task(read())
        await asyncio.to_thread(writing.wait, 10)
        harness._request_cancel(claim, "researcher")  # the Stop, while the terminal write is still to commit
        await asyncio.sleep(0.05)  # the turn's await is cancelled and the Stop's own write is queued
        gate.set()
        await claim.task
        assert len(calls) == (0 if write == "answer" else 1)  # only the turn's own terminal write
        await reader
        assert seen == ["running"] and claim.run_id in harness.registry.finished
        [turn] = (await client.get(f"/api/conversations/{conversation}")).json()["turns"]
        assert turn["status"] == ("succeeded" if write == "answer" else "failed")
        await background_idle(client)


async def test_a_turn_stopped_before_its_response_starts_whose_terminal_write_fails_reads_interrupted(tmp_path,
                                                                                                    monkeypatch):
    async with started(tmp_path / "data") as client:
        conversation = await new_conversation(client)
        harness = client.state["harness"]
        claim = await harness.admit_turn(conversation, "never started")

        def failing(*args, **kwargs):  # the terminal write raises, so nothing of it commits
            raise RuntimeError("the terminal write failed")

        monkeypatch.setattr(runs_module.Harness, "_finish_turn", failing)
        harness.stream_closed(claim)
        with pytest.raises(RuntimeError):
            await claim.closing
        assert not harness.registry.is_active(claim.run_id) and claim.run_id not in harness.registry.finished
        [turn] = (await client.get(f"/api/conversations/{conversation}")).json()["turns"]
        assert turn["status"] == "interrupted"  # its record says running and nothing here holds it
        await background_idle(client)


async def test_an_admission_stopped_by_shutdown_writes_no_run(tmp_path, monkeypatch):
    async with started(tmp_path / "data") as client:
        conversation = await new_conversation(client)
        harness = client.state["harness"]
        loading, release = threading.Event(), threading.Event()
        real = runs_module.load_instructions

        def slow_instructions(*args):
            loading.set()
            release.wait(5)
            return real(*args)

        monkeypatch.setattr(runs_module, "load_instructions", slow_instructions)
        admitting = asyncio.create_task(harness.admit_turn(conversation, "slow"))
        await asyncio.to_thread(loading.wait, 5)
        claim = harness.registry.turns[conversation]
        harness._request_cancel(claim, "shutdown")  # as shutdown does, before the run is written
        await claim.closing
        release.set()
        with pytest.raises(runs_module.AdmissionError, match="closing"):
            await admitting
        assert await rows(client, "SELECT count(*) FROM runs WHERE kind = 'turn'") == [(0,)]
        assert conversation not in harness.registry.turns


async def test_an_admission_whose_request_goes_away_while_its_run_is_written_ends_cancelled(tmp_path, monkeypatch):
    async with started(tmp_path / "data") as client:
        conversation = await new_conversation(client)
        harness = client.state["harness"]
        writing, release = threading.Event(), threading.Event()
        real = runs_module._event

        def slow_event(conn, run_id, event_type, data):  # the admission transaction is under way
            if event_type == "route":
                writing.set()
                release.wait(5)
            return real(conn, run_id, event_type, data)

        monkeypatch.setattr(runs_module, "_event", slow_event)
        admitting = asyncio.create_task(harness.admit_turn(conversation, "gone"))
        await asyncio.to_thread(writing.wait, 5)
        admitting.cancel()  # the request goes away
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await admitting
        assert await rows(client, "SELECT status, cancel_reason FROM runs WHERE kind = 'turn'") == [
            ("cancelled", "researcher")]
        assert conversation not in harness.registry.turns
        monkeypatch.setattr(runs_module, "_event", real)
        assert (await send(client, conversation, "next"))[-1]["status"] == "succeeded"
        await background_idle(client)


async def test_a_cancelled_conversation_deletion_still_stops_its_running_turn(tmp_path, monkeypatch):
    from backend import app as app_module
    provider = MockProvider()
    held(provider)  # the turn's call stays in flight
    async with started(tmp_path / "data", provider) as client:
        conversation = await new_conversation(client)
        stream = asyncio.create_task(client.post(f"/api/conversations/{conversation}/message/stream",
                                                 json={"content": "hi"}))
        await wait_for(lambda: provider.answers)
        run_id = active_turn(client, conversation).run_id
        entered, release = threading.Event(), threading.Event()
        real = app_module.delete

        def slow_delete(*args, **kwargs):
            entered.set()
            release.wait(5)
            return real(*args, **kwargs)

        monkeypatch.setattr(app_module, "delete", slow_delete)
        deletion = asyncio.create_task(client.delete(f"/api/conversations/{conversation}"))
        await asyncio.to_thread(entered.wait, 5)
        deletion.cancel()  # the request goes away while the deletion commits
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await deletion
        await wait_for(lambda: not client.state["harness"].registry.is_active(run_id))  # revoked all the same
        await stream


async def test_a_provider_change_waits_for_an_admission_snapshot_and_is_then_refused_as_busy(tmp_path, monkeypatch):
    async with started(tmp_path / "data") as client:
        conversation = await new_conversation(client)
        harness = client.state["harness"]
        loading, release = threading.Event(), threading.Event()
        real = runs_module.load_settings

        def slow_settings(*args):
            if not loading.is_set():
                loading.set()
                release.wait(5)
            return real(*args)

        monkeypatch.setattr(runs_module, "load_settings", slow_settings)
        admitting = asyncio.create_task(harness.admit_turn(conversation, "hi"))
        await asyncio.to_thread(loading.wait, 5)
        settings = (await client.get("/api/settings")).json()
        change = asyncio.create_task(client.put("/api/settings", json={"hash": settings["hash"], "updates": {
            "providers.openrouter.kind": "openai-compatible"}}))
        await asyncio.sleep(0.2)
        assert not change.done()  # it waits for the snapshot
        release.set()
        claim = await admitting
        assert (await change).json()["code"] == "active_run"  # then the turn holds the provider
        assert [e async for e in harness.events(claim)][-1]["status"] == "succeeded"
        await background_idle(client)


async def test_a_conversation_deleted_between_reservation_and_dispatch_sends_nothing(tmp_path, monkeypatch):
    from backend.db import ContentStore
    async with started(tmp_path / "data") as client:
        conversation = await new_conversation(client)
        harness = client.state["harness"]
        claim = await harness.admit_turn(conversation, "never sent")
        stream = harness.events(claim)
        assert (await anext(stream))["type"] == "run_started"
        assert (await anext(stream))["type"] == "route"
        assert (await anext(stream))["type"] == "step"  # reserved; it dispatches on the next pull
        cleaning, release = threading.Event(), threading.Event()
        real = ContentStore.collect_garbage

        def slow_cleanup(self, *args):  # the deletion has committed; its cleanup is slow
            cleaning.set()
            release.wait(5)
            return real(self, *args)

        monkeypatch.setattr(ContentStore, "collect_garbage", slow_cleanup)
        deletion = asyncio.create_task(client.delete(f"/api/conversations/{conversation}"))
        await asyncio.to_thread(cleaning.wait, 5)
        rest = [event async for event in stream]  # revoked on commit, before the cleanup ends
        release.set()
        assert (await deletion).json() == {"ok": True}
        assert client.provider.answers == []
        assert rest[-1]["type"] == "run_finished"


@pytest.mark.parametrize("reason, outcome", [("researcher", ("cancelled", "researcher")),
                                             ("shutdown", ("interrupted", None))])
@pytest.mark.parametrize("ending", ["provider_error", "limit"])
async def test_a_stop_that_comes_before_a_failed_or_limited_turn_commits_wins(tmp_path, monkeypatch, ending,
                                                                               reason, outcome):
    provider = MockProvider()
    async with started(tmp_path / "data", provider) as client:
        conversation = await new_conversation(client, title="t")
        harness = client.state["harness"]
        claim = await harness.admit_turn(conversation, "stopped")

        def stop():  # the Stop (or shutdown) comes as the turn is about to end otherwise: only the flag
            claim.cancel_reason = reason
            claim.cancel_requested.set()

        if ending == "provider_error":
            def stop_then_fail(body):
                stop()
                return (500, {"error": {"message": "upstream"}})
            provider.replies.append(stop_then_fail)
        else:
            real = runs_module.spending.reserve

            def stop_then_refuse(conn, **kwargs):
                stop()
                return real(conn, **{**kwargs, "conversation_budget_usd": 0.0000001})
            monkeypatch.setattr(runs_module.spending, "reserve", stop_then_refuse)
        stream = [event async for event in harness.events(claim)]
        assert [e["type"] for e in stream if e["type"] in ("error", "limit_reached")] == []
        assert (stream[-1]["status"], stream[-1].get("cancel_reason")) == outcome
        assert await rows(client, "SELECT status, cancel_reason FROM runs WHERE id = ?", claim.run_id) == [outcome]
        if reason == "shutdown":  # interrupted by closing the app: Continue is offered
            if ending == "limit":  # the continued turn's step fits again (S1-33: one that does not would ask)
                monkeypatch.setattr(runs_module.spending, "reserve", real)
            assert (await client.post(f"/api/runs/{claim.run_id}/continue")).status_code == 200


@pytest.mark.parametrize("content", ["​", " ️ "])
async def test_a_message_with_nothing_visible_is_refused_and_sends_nothing(tmp_path, content):
    async with started(tmp_path / "data") as client:
        conversation = await new_conversation(client, title="t")
        response = await client.post(f"/api/conversations/{conversation}/message/stream", json={"content": content})
        assert (response.status_code, response.json()["code"]) == (400, "empty_message")
        assert client.provider.answers == [] and await rows(client, "SELECT count(*) FROM runs") == [(0,)]


async def test_an_answer_with_nothing_visible_fails_the_turn_as_malformed(tmp_path):
    provider = MockProvider((200, {"choices": [{"message": {"content": "​‍"}}], "usage": {"cost": 0.001}}))
    async with started(tmp_path / "data", provider) as client:
        conversation = await new_conversation(client, title="t")
        stream = await send(client, conversation)
        assert (stream[-2]["code"], stream[-1]["status"]) == ("malformed", "failed")


async def test_a_blank_model_is_refused_in_a_request_and_passed_over_in_settings(tmp_path):
    async with started(tmp_path / "data") as client:
        conversation = await new_conversation(client, title="t")
        response = await client.post(f"/api/conversations/{conversation}/message/stream",
                                     json={"content": "hi", "model": "   "})
        assert (response.status_code, response.json()["code"]) == (400, "model_needed")
        assert client.provider.answers == []
        project = (await client.get(f"/api/conversations/{conversation}")).json()["project_id"]
        settings = (await client.get("/api/settings", params={"project_id": project})).json()
        await client.put("/api/settings", json={"project_id": project, "hash": settings["hash"],
                                                "updates": {"models.default": " ​ "}})
        assert (await send(client, conversation, "hello"))[-1]["status"] == "succeeded"
        assert client.provider.answers[-1]["model"].strip() == client.provider.answers[-1]["model"] != ""
        await background_idle(client)


async def test_an_answer_cut_at_its_output_limit_is_saved_and_marked_cut_in_its_trace(tmp_path):
    # Harold, 2026-10-20: saved as succeeded, marked cut in the answer and in the step's trace.
    provider = MockProvider((200, {"choices": [{"message": {"content": "Half an ans"}, "finish_reason": "length"}],
                                   "usage": {"cost": 0.001}}))
    async with started(tmp_path / "data", provider) as client:
        conversation = await new_conversation(client, title="t")
        stream = await send(client, conversation)
        assert stream[-1]["status"] == "succeeded"
        [turn] = (await client.get(f"/api/conversations/{conversation}")).json()["turns"]
        assert (turn["status"], turn["answer"]) == ("succeeded", {"text": "Half an ans", "cut": True})
        [(finished,)] = await rows(client, "SELECT data FROM run_events WHERE run_id = ? AND type = 'step_finished'",
                                   turn["run_id"])
        assert json.loads(finished)["cut"] == "output_limit"


@pytest.mark.parametrize("model", ["x/unpriced", None])  # a chosen model, and Auto
async def test_an_openrouter_model_is_refused_as_unpriced_only_when_its_endpoints_publish_no_price(tmp_path, model):
    # Harold, 2026-10-20, and the connector's finding on PR 37: the catalog's aggregate price may be
    # missing while every endpoint publishes one; the endpoints decide. An endpoint without a price
    # refuses a chosen model; Auto passes over it to the next model it would take.
    import httpx
    from backend import budget_router, openrouter_client

    class Unpriced(MockProvider):  # its catalog lists models with no aggregate price
        async def __call__(self, request):
            if request.url.path.endswith("/models") and request.url.host == "openrouter.ai":
                return httpx.Response(200, json={"data": [{"id": m, "context_length": 128000} for m in self.catalog]})
            return await super().__call__(request)

    first = model or budget_router.MODEL_TIERS["mid"][0]
    provider = Unpriced(catalog=["x/unpriced", *budget_router.RECOMMENDED])
    async with started(tmp_path / "data", provider) as client:
        current = (await client.get("/api/settings")).json()
        await client.put("/api/settings", json={"hash": current["hash"], "updates": {"providers.openrouter.models": "all"}})
        conversation = await new_conversation(client, title="t")
        options = {"model": model} if model else {}
        assert (await send(client, conversation, **options))[-1]["status"] == "succeeded"
        assert provider.answers[-1]["model"] == first  # priced by its endpoints, used
        provider.endpoints[first] = [{"tag": "example", "context_length": 128000, "pricing": {}}]
        openrouter_client.clear_cache()
        if model:
            before = await counts(client)
            refused = await client.post(f"/api/conversations/{conversation}/message/stream",
                                        json={"content": "hi", "model": model})
            assert (refused.status_code, refused.json()["code"]) == (400, "route_unpriced")
            assert len(provider.answers) == 1 and await counts(client) == before  # nothing written or sent
        else:
            assert (await send(client, conversation))[-1]["status"] == "succeeded"
            assert provider.answers[-1]["model"] == budget_router.MODEL_TIERS["mid"][1]  # Auto passed over it



async def test_an_endpoint_that_does_not_take_the_output_limit_is_not_used(tmp_path):
    # External review a2 and charter section 10 rule 2: the output limit is a parameter every request
    # relies on, so it is required of the endpoint (provider.require_parameters) and a model whose
    # endpoints do not list it is refused before anything is written.
    from backend import budget_router
    model = budget_router.MODEL_TIERS["mid"][0]
    provider = MockProvider(endpoints={model: [{"tag": "a", "context_length": 128000,
                                                "supported_parameters": ["temperature"]}]})
    async with started(tmp_path / "data", provider) as client:
        conversation = await new_conversation(client, title="t")
        refused = await client.post(f"/api/conversations/{conversation}/message/stream",
                                    json={"content": "hi", "model": model})  # chosen: Auto would pass over it
        assert (refused.status_code, refused.json()["code"]) == (400, "route_unsupported")
        assert provider.chats == [] and (await counts(client))["runs"] == 0
        provider.endpoints[model][0]["supported_parameters"] = ["max_tokens", "temperature"]
        from backend import openrouter_client
        openrouter_client.clear_cache()
        assert (await send(client, conversation))[-1]["status"] == "succeeded"
        [body] = provider.answers
        assert body["max_tokens"] == 8000 and body["provider"] == {"require_parameters": True}



async def test_a_model_whose_reasoning_record_is_dropped_still_needs_the_output_limit(tmp_path, monkeypatch):
    # Review a4 (P3-2): when reasoning cannot be honored and is dropped, the endpoints must still take
    # the output limit, or the model is refused before anything is written.
    from backend import budget_router, openrouter
    monkeypatch.setattr(openrouter, "resolve_model_reasoning",
                        lambda route, effort, **kw: ({"effort": effort}, "elsewhere") if effort else (None, None))
    model = budget_router.MODEL_TIERS["mid"][0]
    provider = MockProvider(endpoints={model: [{"tag": "a", "context_length": 128000,
                                                "supported_parameters": ["reasoning"]}]})
    async with started(tmp_path / "data", provider) as client:
        conversation = await new_conversation(client, title="t")
        refused = await client.post(f"/api/conversations/{conversation}/message/stream",
                                    json={"content": "hi", "effort": "high", "model": model})
        assert (refused.status_code, refused.json()["code"]) == (400, "route_unsupported")
        assert provider.chats == []


async def test_an_answer_cut_at_its_output_limit_that_cannot_be_saved_keeps_its_cut_mark(tmp_path, monkeypatch):
    # The connector's finding on PR 37: the save-failure fallback carries the cut mark, as a saved answer does.
    provider = MockProvider((200, {"choices": [{"message": {"content": "Half an ans"}, "finish_reason": "length"}],
                                   "usage": {"cost": 0.001}}), (200, {"choices": [{"message": {"content": "Whole."}}],
                                                                      "usage": {"cost": 0.001}}))
    async with started(tmp_path / "data", provider) as client:
        conversation = await new_conversation(client, title="t")

        def disk_full(self, conn, claim, answer, ctx):
            raise OSError("disk full")

        monkeypatch.setattr(runs_module.Harness, "_commit_answer", disk_full)
        response = next(e for e in await send(client, conversation) if e["type"] == "chat_response")
        assert (response["content"], response["result_saved"], response["cut"]) == ("Half an ans", False, True)
        response = next(e for e in await send(client, conversation) if e["type"] == "chat_response")
        assert response["result_saved"] is False and "cut" not in response  # a whole answer carries no mark


# S1-19b: cited turns

async def test_stop_during_the_judge_call_settles_each_step_once_and_publishes_nothing(tmp_path):
    # L1: the answer's attempt is settled as reported, the judge's in flight at its estimate; no answer.
    from cited import Provider, cited, conversation_in, rows, with_papers
    from test_search import WAGES
    release, judging = asyncio.Event(), asyncio.Event()

    async def held(body):
        judging.set()
        await release.wait()
        return Provider().answer("1: supported")
    provider = Provider(verdicts=held)
    async with cited(tmp_path, provider) as client:
        project, _ = await with_papers(client, WAGES)
        conversation = await conversation_in(client, project)
        stream = asyncio.create_task(client.post(f"/api/conversations/{conversation}/message/stream",
                                                 json={"content": "What do minimum wages do to earnings?"}))
        await asyncio.wait_for(judging.wait(), 10)
        [(run_id,)] = await rows(client, "SELECT id FROM runs WHERE kind = 'turn'")
        assert (await client.post(f"/api/runs/{run_id}/cancel")).json()["status"] == "cancelled"
        finished = events(await stream)
        release.set()
        assert (finished[-1]["status"], finished[-1]["cancel_reason"]) == ("cancelled", "researcher")
        assert await rows(client, "SELECT answer FROM turns WHERE run_id = ?", run_id) == [(None,)]
        assert await rows(client, "SELECT count(*) FROM citations") == [(0,)]
        spending = sorted(await rows(client, "SELECT status, basis, settled_usd > 0 FROM budget_reservations"
                                             " WHERE run_id = ? AND status != 'released'", run_id))  # the judge's share
        assert spending == [("settled", "estimated", 1), ("settled", "reported", 1)]
        again = await send(client, conversation, "What do minimum wages do to earnings?")  # a new turn
        assert again[-1]["status"] == "succeeded"


async def test_two_conversations_of_one_project_asking_at_once_keep_their_own_citations_and_reservations(tmp_path):
    # G8: each turn's citations and reservations stay its own.
    from cited import Provider, cited, conversation_in, rows, with_papers
    from test_search import WAGES, paper
    both = asyncio.Event()
    count = 0

    async def answer(body):
        nonlocal count
        count += 1
        if count == 2:
            both.set()
        await asyncio.wait_for(both.wait(), 10)  # both answers are in flight together
        question = body["messages"][-1]["content"]
        return Provider().answer("Rents rose [S1.1]." if "rents" in question else "Earnings rise [S1.1].")
    provider = Provider(answer_text=answer)
    async with cited(tmp_path, provider) as client:
        project, [wages, rents] = await with_papers(client, WAGES, paper("Rents", "Housing rents rose in the city."))
        first, second = await conversation_in(client, project), await conversation_in(client, project)
        streams = await asyncio.gather(
            client.post(f"/api/conversations/{first}/message/stream",
                        json={"content": "earnings?", "material_ids": [wages]}),
            client.post(f"/api/conversations/{second}/message/stream",
                        json={"content": "rents?", "material_ids": [rents]}))
        assert [events(s)[-1]["status"] for s in streams] == ["succeeded", "succeeded"]
        for conversation, material in ((first, wages), (second, rents)):
            [turn] = (await client.get(f"/api/conversations/{conversation}")).json()["turns"]
            assert [c["material_id"] for c in turn["citations"]] == [material]
            assert await rows(client, "SELECT count(*) FROM budget_reservations WHERE run_id = ? AND status != 'released'",
                              turn["run_id"]) == [(2,)]  # the answer's and the judge's; the judge's share released


# S1-19c: the agent loop's turn facts (I3, G8)


@pytest.mark.asyncio
async def test_a_second_message_is_refused_while_the_loop_waits_and_stop_between_steps_ends_it(tmp_path):
    from agentic import Scripted, call
    from cited import cited, conversation_in, with_papers
    from test_agent_loop import asked
    from test_search import WAGES
    provider = Scripted([call("ask", question="Go on?", options=["Yes"])], "Done.")
    async with cited(tmp_path, provider) as client:
        project, _ = await with_papers(client, WAGES)
        conversation = await conversation_in(client, project)
        sending = asyncio.create_task(send(client, conversation, "Which papers do I have?"))
        ask = await asked(client, conversation)
        second = await client.post(f"/api/conversations/{conversation}/message/stream", json={"content": "And?"})
        assert (second.status_code, second.json()["code"]) == (409, "active_run")  # one active turn
        assert (await client.post(f"/api/runs/{ask['run_id']}/cancel")).json()["status"] == "cancelled"
        stream = await sending
        assert stream[-1]["status"] == "cancelled" and len(provider.agent_steps) == 1


@pytest.mark.asyncio
async def test_two_conversations_of_one_project_keep_their_own_citations_and_reservations(tmp_path):
    from agentic import Scripted, call
    from cited import cited, conversation_in, rows, with_papers
    from test_search import WAGES
    provider = Scripted([call("search_passages", query="wages")], [call("search_passages", call_id="c2", query="wages")],
                        "One [S1.1].", "Two [S1.1].")
    async with cited(tmp_path, provider) as client:
        project, _ = await with_papers(client, WAGES)
        first, second = await conversation_in(client, project), await conversation_in(client, project)
        streams = await asyncio.gather(send(client, first, "Which papers?"), send(client, second, "Which papers?"))
        assert [s[-1]["status"] for s in streams] == ["succeeded", "succeeded"]
        for stream, conversation in zip(streams, (first, second)):
            run_id = stream[0]["run_id"]
            owners = await rows(client, "SELECT DISTINCT owner_id FROM citations WHERE owner_id = ?", run_id)
            paying = await rows(client, "SELECT DISTINCT paying_conversation_id FROM budget_reservations WHERE run_id = ?",
                                run_id)
            assert owners == [(run_id,)] and paying == [(conversation,)]


# S1-33 fix batch 3 (PR 52 cycle 3, design review A): what each attempt did, from the adapter's own list, settles a
# call and writes its record, even when the call never comes back to its receipt (a Stop, a failed write). Lifecycle
# first (charter section 10): the turn's cleanup closes such a call as its attempts say, once.

REJECTED = (400, {"error": {}, "usage": {"prompt_tokens": 10, "completion_tokens": 0, "cost": 0.2}})  # went out: $0.20


async def lab(client, monkeypatch):
    """A conversation on an OpenAI-compatible provider sent the reasoning field, which a first attempt that answers
    REJECTED makes the adapter retry without (openrouter.query_model)."""
    from backend import openrouter
    monkeypatch.setattr(openrouter, "resolve_model_reasoning", lambda route, effort, **kw: ({"effort": "high"}, None))
    settings = (await client.get("/api/settings")).json()
    await client.put("/api/settings", json={"hash": settings["hash"], "updates": {
        "providers.lab.kind": "openai-compatible", "providers.lab.base_url": "http://127.0.0.1:9/v1"}})
    await client.put("/api/keys/lab", json={"key": "k"})
    return await new_conversation(client, title="t")


def fails_once(monkeypatch, name):
    """runs.<name> raises the first time it is called (the receipt's write); the calls made."""
    real, calls = getattr(runs_module, name), []

    def failing(*args, **kwargs):
        calls.append(True)
        if len(calls) == 1:
            raise RuntimeError("the receipt's write failed")
        return real(*args, **kwargs)
    monkeypatch.setattr(runs_module, name, failing)
    return calls


async def attempts_of(client, run_id):
    return [(d["attempt"], d["outcome"], d["dispatched"], d["charge"], d.get("cost_usd"), "body" in d)
            for d in (json.loads(data) for (data,) in await rows(
                client, "SELECT data FROM run_events WHERE run_id = ? AND type = 'model_attempt' ORDER BY seq", run_id))]


async def reservation(client, run_id):
    [found] = await rows(client, "SELECT estimate_usd, status, settled_usd, basis FROM budget_reservations"
                                 " WHERE run_id = ?", run_id)
    return found


async def test_a_reported_request_whose_receipt_write_fails_keeps_its_reported_charge(tmp_path, monkeypatch):
    # E1 case 3 (SPEC:154, 425): the one request reported $0.20, then the write recording it failed. The turn's cleanup
    # closes the call as its attempt says: settled at $0.20 reported, never at its estimate with an unknown attempt.
    async with started(tmp_path / "data", MockProvider(cost=0.2)) as client:
        conversation = await new_conversation(client, title="t")
        failed = fails_once(monkeypatch, "_settle_attempts")
        stream = await send(client, conversation)
        assert failed and stream[-1]["status"] == "failed"
        run_id = stream[0]["run_id"]
        assert (await reservation(client, run_id))[1:] == ("settled", 0.2, "reported")
        assert await attempts_of(client, run_id) == [(0, "ok", True, "reported", 0.2, True)]
        [turn] = (await client.get(f"/api/conversations/{conversation}")).json()["turns"]
        assert turn["accounting"]["unknown_attempts"] == 0


async def test_a_reported_attempt_keeps_its_charge_beside_a_retry_that_never_connected_when_the_receipt_fails(
        tmp_path, monkeypatch):
    # E1 case 1: the first attempt went out and reported $0.20; its retry never connected; the receipt's write failed.
    # Both attempts are recorded as they were, and the call is charged the first one's $0.20, reported.
    import httpx

    def unreachable(body):
        raise httpx.ConnectError("connection refused")
    provider = MockProvider(REJECTED, unreachable)
    async with started(tmp_path / "data", provider) as client:
        conversation = await lab(client, monkeypatch)
        fails_once(monkeypatch, "_settle_attempts")
        stream = await send(client, conversation, provider="lab", model="m")
        assert stream[-1]["status"] == "failed" and len(provider.answers) == 2
        run_id = stream[0]["run_id"]
        assert (await reservation(client, run_id))[1:] == ("settled", 0.2, "reported")
        assert await attempts_of(client, run_id) == [(0, "request", True, "reported", 0.2, True),
                                                     (1, "network", False, "unknown", None, False)]


async def test_a_stop_while_the_gate_decides_on_a_retry_records_it_unsent_and_keeps_the_first_attempts_charge(
        tmp_path, monkeypatch):
    # E1 case 2: the retry's body was kept, and Stop came while the gate was still deciding on it, so it never left.
    # The record says so (no body kept for it, not dispatched), and the call is charged what its first attempt reported.
    import time
    from backend import outbound_gate
    provider = MockProvider(REJECTED)
    async with started(tmp_path / "data", provider) as client:
        conversation = await lab(client, monkeypatch)
        harness, loop, real, chats = client.state["harness"], asyncio.get_running_loop(), outbound_gate.OutboundGate._check, []

        def checking(self, request, scope, hop=None):
            if request.url.path.endswith("/chat/completions"):
                chats.append(request)
                if len(chats) == 2:  # the retry: Stop arrives while the gate decides
                    loop.call_soon_threadsafe(lambda: harness._request_cancel(active_turn(client, conversation),
                                                                              "researcher"))
                    time.sleep(0.3)
            return real(self, request, scope, hop)
        monkeypatch.setattr(outbound_gate.OutboundGate, "_check", checking)
        stream = await send(client, conversation, provider="lab", model="m")
        assert stream[-1]["status"] == "cancelled" and len(chats) == 2 and len(provider.answers) == 1
        run_id = stream[0]["run_id"]
        assert (await reservation(client, run_id))[1:] == ("settled", 0.2, "reported")
        assert await attempts_of(client, run_id) == [(0, "request", True, "reported", 0.2, True),
                                                     (1, "cancelled", False, "unknown", None, False)]
        assert await rows(client, "SELECT attempt FROM run_bodies WHERE run_id = ?", run_id) == [(0,)]


async def test_a_retried_call_whose_two_reported_costs_sum_past_what_a_float_holds_keeps_them_as_reported(
        tmp_path, monkeypatch):
    # PR 52 cycle 5 A-F2 (SPEC:425): the rejected attempt and its retry each reported 1.7e308, a valid amount; their sum
    # overflowed to infinity, which settlement took for no amount, so the call settled at its estimate, marked
    # estimated. The total is held at the most a float holds (spending.combined), settled as reported, and the turn's
    # record and stream carry it.
    import sys
    big = 1.7e308
    rejected = (400, {"error": {}, "usage": {"prompt_tokens": 10, "completion_tokens": 0, "cost": big}})
    answered = (200, {"choices": [{"message": {"role": "assistant", "content": "Hi."}}],
                      "usage": {"prompt_tokens": 12, "completion_tokens": 5, "cost": big}})
    async with started(tmp_path / "data", MockProvider(rejected, answered)) as client:
        conversation = await lab(client, monkeypatch)
        stream = await send(client, conversation, provider="lab", model="m")
        assert stream[-1]["status"] == "succeeded"
        run_id = stream[0]["run_id"]
        assert (await reservation(client, run_id))[1:] == ("settled", sys.float_info.max, "reported")
        assert await attempts_of(client, run_id) == [(0, "request", True, "reported", big, True),
                                                     (1, "ok", True, "reported", big, True)]
        assert (stream[-1]["cost_usd"], stream[-1]["accounting"]["reported_usd"]) == (sys.float_info.max,) * 2
        [turn] = (await client.get(f"/api/conversations/{conversation}")).json()["turns"]
        assert turn["cost_usd"] == sys.float_info.max


async def test_a_stop_while_a_retry_is_in_flight_keeps_both_attempts_and_charges_at_least_the_reported_one(
        tmp_path, monkeypatch):
    # N1: the retry had left when Stop came, with no usage: the call keeps its first attempt's $0.20 and its estimate
    # for the one cut off, settled at the larger, marked estimated (_settle_attempts); neither attempt is lost.
    reached = asyncio.Event()

    async def in_flight(body):
        reached.set()
        await asyncio.sleep(30)
    provider = MockProvider(REJECTED, in_flight)
    async with started(tmp_path / "data", provider) as client:
        conversation = await lab(client, monkeypatch)
        sending = asyncio.create_task(send(client, conversation, provider="lab", model="m"))
        await asyncio.wait_for(reached.wait(), 5)
        run_id = active_turn(client, conversation).run_id
        await client.post(f"/api/runs/{run_id}/cancel")
        assert (await sending)[-1]["status"] == "cancelled"
        estimate, status, settled, basis = await reservation(client, run_id)
        assert (status, settled, basis) == ("settled", max(0.2, estimate), "estimated")
        assert await attempts_of(client, run_id) == [(0, "request", True, "reported", 0.2, True),
                                                     (1, "cancelled", True, "unknown", None, True)]
        [turn] = (await client.get(f"/api/conversations/{conversation}")).json()["turns"]
        assert turn["accounting"]["unknown_attempts"] == 1


async def test_a_retry_whose_body_could_not_be_kept_never_left_and_the_first_attempt_keeps_its_charge(
        tmp_path, monkeypatch):
    # N2: the write keeping the retry's body failed before it was sent, so the call never came back to its receipt. Its
    # first attempt, which went out and reported $0.20, is recorded and charged; nothing says the retry was sent.
    provider = MockProvider(REJECTED)
    real = runs_module._keep_body

    def keeping(conn, call, number, ref):
        if number == 1:
            raise RuntimeError("the body's write failed")
        return real(conn, call, number, ref)
    monkeypatch.setattr(runs_module, "_keep_body", keeping)
    async with started(tmp_path / "data", provider) as client:
        conversation = await lab(client, monkeypatch)
        stream = await send(client, conversation, provider="lab", model="m")
        assert stream[-1]["status"] == "failed" and len(provider.answers) == 1
        run_id = stream[0]["run_id"]
        assert (await reservation(client, run_id))[1:] == ("settled", 0.2, "reported")
        assert await attempts_of(client, run_id) == [(0, "request", True, "reported", 0.2, True)]


async def test_a_call_whose_outcome_could_not_be_written_before_the_app_stopped_settles_at_its_estimate_by_the_crash_rule(
        tmp_path, monkeypatch):
    # The residual Harold accepted (2026-11-11, item 2; N3): the request never connected, and neither the receipt nor the
    # turn's cleanup could write that before the app stopped. Section 6.1's crash rule then settles the reservation at
    # its estimate, marked estimated, and the turn reads interrupted with Continue: the app could not know better.
    import httpx
    from backend import spending
    failing, releasing = [True], spending.release

    def release(conn, reservation_id):
        if failing[0]:
            raise RuntimeError("the write failed")
        return releasing(conn, reservation_id)

    def unreachable(body):
        raise httpx.ConnectError("connection refused")
    monkeypatch.setattr(spending, "release", release)
    async with started(tmp_path / "data", MockProvider(unreachable)) as client:
        conversation = await new_conversation(client, title="t")
        stream = await send(client, conversation)
        assert not [e for e in stream if e["type"] == "run_finished"]  # its end could not be written either
        run_id = stream[0]["run_id"]
        failing[0] = False
        await client.state["harness"].recover(kick=False)  # as at the next start
        estimate, *settled = await reservation(client, run_id)
        assert settled == ["settled", estimate, "estimated"]
        assert await rows(client, "SELECT status FROM runs WHERE id = ?", run_id) == [("interrupted",)]


# PR 52 cycle 4 (design review A2): a reply the adapter can read nothing from, or whose numbers no float can hold, is a
# reply like any other: its call is recorded as the attempt went, and charged at an estimate when nothing says its cost.


async def test_a_reply_too_deeply_nested_to_decode_is_charged_at_its_estimate_as_a_request_that_went_out(tmp_path):
    # G1 (cycle 4 A's F1): a 200 reply of 10,000 nested arrays. The decoder's RecursionError escaped the adapter before
    # the attempt joined the call's list, so the request that went out was released at $0 and its kept body deleted. It
    # is now the malformed reply it is: the turn fails malformed, its one attempt went out, its body is kept, and the
    # call is charged its estimate, marked estimated, the charge unknown (SPEC:425).
    async with started(tmp_path / "data", MockProvider((200, b"[" * 10000 + b"]" * 10000))) as client:
        conversation = await new_conversation(client, title="t")
        stream = await send(client, conversation)
        assert stream[-1]["status"] == "failed" and {"type": "error", "code": "malformed"} in stream
        run_id = stream[0]["run_id"]
        estimate, *settled = await reservation(client, run_id)
        assert settled == ["settled", estimate, "estimated"]
        assert await attempts_of(client, run_id) == [(0, "malformed", True, "unknown", None, True)]
        assert stream[-1]["accounting"]["unknown_attempts"] == 1


@pytest.mark.parametrize("usage", [{"prompt_tokens": 12, "completion_tokens": 5, "cost": 10 ** 400},
                                   {"prompt_tokens": 10 ** 400, "completion_tokens": 5}])
async def test_a_reply_whose_cost_or_token_count_no_float_holds_is_delivered_and_charged_at_an_estimate(tmp_path, usage):
    # Design review A2 (G1, new): a reported cost or token count of 401 digits raised OverflowError in the receipt and
    # again in the turn's cleanup, leaving the turn with no end and its reservation open until the next start. Neither
    # is a number the record can use: the answer is delivered, and the call is charged from its readable token counts
    # at the step's rates, or else at its estimate (its attempt's charge then unknown), settled as estimated either way.
    reply = (200, {"choices": [{"message": {"role": "assistant", "content": "Hi."}}], "usage": usage})
    async with started(tmp_path / "data", MockProvider(reply)) as client:
        conversation = await new_conversation(client, title="t")
        stream = await send(client, conversation)
        assert stream[-1]["status"] == "succeeded"
        assert [e["content"] for e in stream if e["type"] == "chat_response"] == ["Hi."]
        estimate, status, settled, basis = await reservation(client, stream[0]["run_id"])
        assert (status, basis) == ("settled", "estimated") and 0 < settled <= estimate
        [attempt] = await attempts_of(client, stream[0]["run_id"])
        if "cost" not in usage:  # no count the step's rates can price: its estimate, its charge unknown (review F5)
            assert settled == estimate and attempt[3] == "unknown" and stream[-1]["accounting"]["unknown_attempts"] == 1
        else:
            assert attempt[3] == "estimated" and stream[-1]["accounting"]["unknown_attempts"] == 0


async def test_a_turn_whose_reply_a_defect_left_unread_settles_at_its_estimate_with_its_attempt_sent(tmp_path, monkeypatch):
    # Design review A2's Option 2 (its test 7): the decoder made to raise, a failure between the reply and the attempt's
    # ending. The gate's notice had already put the attempt in the call's list as sent, so the turn's cleanup settles
    # the call at its estimate with that attempt sent, its body kept and its charge unknown, never released at $0.
    from backend import openrouter

    def broken(response):
        raise RuntimeError("the decoder failed")
    monkeypatch.setattr(openrouter, "_json", broken)
    async with started(tmp_path / "data", MockProvider()) as client:
        conversation = await new_conversation(client, title="t")
        stream = await send(client, conversation)
        assert stream[-1]["status"] == "failed"
        run_id = stream[0]["run_id"]
        estimate, *settled = await reservation(client, run_id)
        assert settled == ["settled", estimate, "estimated"]
        assert await attempts_of(client, run_id) == [(0, "other", True, "unknown", None, True)]


async def test_while_a_turns_request_is_at_the_provider_its_call_already_says_it_went_out(tmp_path):
    # Design review A2 (its test 7): in flight, the call's own list holds the attempt as sent (no stand-in says "never
    # left" while the provider has the request).
    arrived, release = asyncio.Event(), asyncio.Event()

    async def held(body):
        arrived.set()
        await release.wait()
        return provider.answer("An answer.")
    provider = MockProvider(held)
    async with started(tmp_path / "data", provider) as client:
        conversation = await new_conversation(client, title="t")
        sending = asyncio.create_task(send(client, conversation))
        await asyncio.wait_for(arrived.wait(), 10)
        call = active_turn(client, conversation).call
        assert call.dispatched and [(a.outcome, a.dispatched) for a in call.attempts] == [("other", True)]
        release.set()
        assert (await sending)[-1]["status"] == "succeeded"


@pytest.mark.parametrize("ending", ["answer", "failed"])
async def test_a_stop_landing_while_a_turns_end_is_written_still_gets_that_end_on_the_stream(tmp_path, monkeypatch, ending):
    # PR 52 cycle 4 (G5's class, batch 4 part 2 item 3): Stop lands while the turn's terminal write runs, once it began.
    # The answer (or the failure) commits, and the stream says so as the record does, before it ends: chat_response,
    # saved (or the error), then run_finished with the recorded status. Before, the answer was saved but never sent.
    import time
    method = "_commit_answer" if ending == "answer" else "_finish_turn"
    real = getattr(runs_module.Harness, method)
    provider = MockProvider() if ending == "answer" else MockProvider((200, {"choices": [{"message": {"content": " "}}]}))
    async with started(tmp_path / "data", provider) as client:
        harness, loop = client.state["harness"], asyncio.get_running_loop()

        def writing(self, conn, *args, **kwargs):
            done = real(self, conn, *args, **kwargs)
            if not getattr(writing, "stopped", False):
                writing.stopped = True
                claim = harness.registry.turns[conversation]
                loop.call_soon_threadsafe(lambda: harness._request_cancel(claim, "researcher"))
                time.sleep(0.3)  # the Stop lands while this write is still to commit
            return done
        monkeypatch.setattr(runs_module.Harness, method, writing)
        conversation = await new_conversation(client, title="t")
        stream = await send(client, conversation)
        [turn] = (await client.get(f"/api/conversations/{conversation}")).json()["turns"]
        if ending == "answer":
            assert turn["status"] == "succeeded" and turn["answer"] == {"text": "An answer."}
            assert [e["type"] for e in stream][-2:] == ["chat_response", "run_finished"]
            assert (stream[-2]["content"], stream[-2]["result_saved"], stream[-1]["status"]) == ("An answer.", True, "succeeded")
        else:
            assert turn["status"] == "failed"
            assert stream[-2:] == [{"type": "error", "code": "malformed"}, {**stream[-1], "status": "failed"}]
