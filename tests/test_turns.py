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
        # text it sent (S1-19b: stored before it was sent, in the content store, never in the event).
        [(attempt,)] = await rows(client, "SELECT data FROM run_events WHERE run_id = ? AND type = 'model_attempt'",
                                  run_id)
        assert json.loads(attempt) == {"step": 0, "attempt": 0, "route": json.loads(attempt)["route"], "outcome": "cancelled",
                                       "http_status": None, "dispatched": True, "charge": "unknown",
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
        stream = await send(client, conversation)
        assert [e["type"] for e in stream] == ["run_started", "route", "limit_reached", "run_finished"]
        assert stream[2]["budget"] == "project" and stream[-1]["status"] == "cancelled"
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
        loop.call_soon_threadsafe(checking.set)
        time.sleep(0.3)  # the gate is still checking when Stop arrives
        return real(data_root)

    async with started(tmp_path / "data") as client:
        await send(client, await new_conversation(client, title="t"))  # the catalog and endpoints, read once
        conversation = await new_conversation(client, title="t")
        monkeypatch.setattr(providers_module, "gate_inputs", slow_inputs)
        stream = asyncio.create_task(send(client, conversation))
        await asyncio.wait_for(checking.wait(), 5)
        run_id = active_turn(client, conversation).run_id
        await client.post(f"/api/runs/{run_id}/cancel")
        assert (await stream)[-1]["status"] == "cancelled"
        assert len(client.provider.chats) == 1  # the first conversation's only
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
        else:  # a reader pulls it to its first step, which does not fit
            assert [event["type"] async for event in harness.events(claim)] == ["run_started", "route"]
        with pytest.raises(RuntimeError):
            await claim.task
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
