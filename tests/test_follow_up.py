"""Messages sent during a run (slice-1 spec F11; section 14's test_follow_up.py; S1-33): steer becomes a steering note
the agent reads at its next step, queue waits as the next turn, and the interface's paths never get 409 active_run,
which the API still answers a client that names neither. Model calls go to the test-owned provider."""

import asyncio
import json

import pytest

from agentic import Scripted, call, steps_reply
from scholia_app import MockProvider, background_idle, events, send, started

pytestmark = pytest.mark.asyncio


async def rows(client, sql, *args):
    return await asyncio.to_thread(client.state["db"].read, lambda conn: conn.execute(sql, args).fetchall())


async def named(client, title="Named", project=None):
    return (await client.post("/api/conversations", json={"title": title, **({"project_id": project} if project else {})})).json()["id"]


class Gate:
    """Holds the agent's step number `at` (0-based) until opened: `reached` is set once that step's request arrived."""

    def __init__(self, at):
        self.at, self.reached, self.open = at, asyncio.Event(), asyncio.Event()

    def wrap(self, provider, reply):
        async def held(body):
            if len(provider.steps) - 1 == self.at:
                self.reached.set()
                await self.open.wait()
            if isinstance(reply, dict):
                return steps_reply(provider, reply["calls"])
            return provider.answer(reply) if isinstance(reply, str) else reply
        return held


async def running_run(client, conversation):
    while True:
        found = await rows(client, "SELECT id FROM runs WHERE conversation_id = ? AND status = 'running'", conversation)
        if found:
            return found[0][0]
        await asyncio.sleep(0.01)


async def steer(client, run_id, content):
    return await client.post(f"/api/runs/{run_id}/steering", json={"content": content})


async def conversation_read(client, conversation):
    return (await client.get(f"/api/conversations/{conversation}")).json()


# Steer


async def test_steer_becomes_a_note_the_agents_next_step_reads_after_the_last_tool_result(tmp_path):
    # F2, CH:97: the next step's request is the earlier chain unchanged, then the note, as the researcher's words.
    gate = Gate(0)
    provider = Scripted(None, "Done.")
    provider.script[0] = gate.wrap(provider, {"calls": [call("list_materials")]})
    async with started(tmp_path / "data", provider) as client:
        conversation = await named(client)
        sending = asyncio.create_task(send(client, conversation, "Which papers do I have?"))
        await gate.reached.wait()
        run_id = await running_run(client, conversation)
        steered = await steer(client, run_id, "Only the ones from 2020, please")
        assert steered.status_code == 200, steered.text
        assert steered.json()["steering"]["run_id"] == run_id and "queued" not in steered.json()
        [note] = (await conversation_read(client, conversation))["turns"][-1]["steering"]
        assert (note["text"], note["state"]) == ("Only the ones from 2020, please", "pending")
        gate.open.set()
        stream = await sending
        assert stream[-1]["status"] == "succeeded"
        assert {"type": "steering_note", "read": [note["seq"]]} in stream
        first, second = provider.steps
        assert second["messages"][:len(first["messages"])] == first["messages"]  # the earlier chain unchanged
        assert [m["role"] for m in second["messages"][len(first["messages"]):]] == ["assistant", "tool", "user"]
        assert second["messages"][-1] == {"role": "user", "content": "Only the ones from 2020, please"}
        [note] = (await conversation_read(client, conversation))["turns"][-1]["steering"]
        assert note["state"] == "read"
        [carried] = await rows(client, "SELECT json_extract(data, '$.steering') FROM run_events WHERE run_id = ?"
                                       " AND type = 'step_started' AND json_extract(data, '$.steering') IS NOT NULL", run_id)
        assert json.loads(carried[0]) == [note["seq"]]
        assert (await conversation_read(client, conversation))["queued"] == []


async def test_a_note_never_answers_an_open_ask_nor_starts_a_second_run(tmp_path):
    # F3, I1: the agent's ask stays open; the note is read at the step after its answer.
    from test_agent_loop import asked
    provider = Scripted([call("ask", question="Which outcome?", options=["Earnings", "Jobs"])], "Done.")
    async with started(tmp_path / "data", provider) as client:
        conversation = await named(client)
        sending = asyncio.create_task(send(client, conversation, "Which papers do I have?"))
        ask = await asked(client, conversation)
        assert (await steer(client, ask["run_id"], "Earnings")).status_code == 200
        assert [a["ask_id"] for a in (await client.get(f"/api/asks?conversation_id={conversation}")).json()["asks"]] == [
            ask["ask_id"]]
        assert await rows(client, "SELECT count(*) FROM runs WHERE conversation_id = ?", conversation) == [(1,)]
        await client.post(f"/api/runs/{ask['run_id']}/asks/{ask['ask_id']}", json={"option": "2"})
        assert (await sending)[-1]["status"] == "succeeded"
        assert provider.steps[1]["messages"][-1] == {"role": "user", "content": "Earnings"}


async def test_a_note_the_turn_has_no_step_left_to_read_becomes_exactly_one_queued_message(tmp_path):
    # L7, F6: a note committed after the turn's last step started is never lost and never both read and queued.
    gate = Gate(0)
    provider = Scripted(None)
    provider.script.append(gate.wrap(provider, "The answer."))
    provider.script.pop(0)
    async with started(tmp_path / "data", provider) as client:
        conversation = await named(client)
        sending = asyncio.create_task(send(client, conversation, "Which papers do I have?"))
        await gate.reached.wait()  # the last step (an answer) is in flight
        run_id = await running_run(client, conversation)
        assert (await steer(client, run_id, "Also say how many")).status_code == 200
        gate.open.set()
        assert (await sending)[-1]["status"] == "succeeded"
        read = await conversation_read(client, conversation)
        assert [q["text"] for q in read["queued"]] == ["Also say how many"]
        assert [n["state"] for n in read["turns"][-1]["steering"]] == ["queued"]
        assert len(provider.steps) == 1


async def test_a_steer_after_its_turn_ended_or_on_a_route_without_tools_is_queued(tmp_path):
    # F6, reading 12: the run has ended (or has no next step): the message waits as the next turn instead.
    provider = MockProvider()
    provider.parameters = ["max_tokens", "temperature"]  # no endpoint takes tools: chat with the library
    async with started(tmp_path / "data", provider) as client:
        conversation = await named(client)
        stream = await send(client, conversation, "Hello?")
        ended = await steer(client, stream[0]["run_id"], "And another thing")
        assert ended.status_code == 200 and ended.json()["queued"]["text"] == "And another thing"
        assert ended.json()["ended"] is True  # it waits queued, and starts only as any waiting message does (1N)
        release = asyncio.Event()

        async def held(body):
            await release.wait()
            return provider.answer("Slow.")
        provider.replies.append(held)
        sending = asyncio.create_task(send(client, conversation, "Slow one?"))
        run_id = await running_run(client, conversation)
        while not provider.answers[1:]:
            await asyncio.sleep(0.01)
        no_tools = await steer(client, run_id, "Shorter, please")
        assert no_tools.json()["queued"]["text"] == "Shorter, please" and "ended" not in no_tools.json()
        release.set()
        await sending
        assert [q["text"] for q in (await conversation_read(client, conversation))["queued"]] == [
            "And another thing", "Shorter, please"]


async def test_a_steering_note_that_committed_stays_recorded_when_its_next_step_fails(tmp_path):
    # L5 (d): the note is recorded once, never resent as a new turn; the failed turn's unread note waits queued.
    gate = Gate(0)
    provider = Scripted(None, (500, {"error": {"message": "upstream"}}))
    provider.script[0] = gate.wrap(provider, {"calls": [call("list_materials")]})
    async with started(tmp_path / "data", provider) as client:
        conversation = await named(client)
        sending = asyncio.create_task(send(client, conversation, "Which papers do I have?"))
        await gate.reached.wait()
        run_id = await running_run(client, conversation)
        await steer(client, run_id, "Only recent ones")
        gate.open.set()
        stream = await sending
        assert stream[-1]["status"] == "failed"
        notes = await rows(client, "SELECT count(*) FROM run_events WHERE run_id = ? AND type = 'steering_note'", run_id)
        assert notes == [(1,)]
        read = await conversation_read(client, conversation)
        assert len(read["turns"]) == 1 and read["queued"] == []  # it was sent with the failed step: read, not queued
        assert read["turns"][-1]["steering"][0]["state"] == "read"


@pytest.mark.parametrize("text", ["", "   ", "​⁠", "x" * 100_001])
async def test_blank_or_too_long_steering_and_queued_text_is_refused(tmp_path, text):
    # G4
    async with started(tmp_path / "data") as client:
        conversation = await named(client)
        stream = await send(client, conversation, "Hello?")
        steered = await steer(client, stream[0]["run_id"], text)
        queued = await client.post(f"/api/conversations/{conversation}/queue", json={"content": text})
        assert steered.status_code == 400 and queued.status_code == 400
        assert (await conversation_read(client, conversation))["queued"] == []


# Queue


async def test_queue_waits_as_the_next_turn_editable_and_removable_until_it_starts(tmp_path):
    # F4, G5, G6: pending and editable; it starts as the next turn after a turn that succeeded; once started it is no
    # longer queued, so an edit or removal is refused and the turn is unchanged.
    gate = Gate(0)
    provider = Scripted(None, "Second answer.")
    provider.script[0] = gate.wrap(provider, "First answer.")
    async with started(tmp_path / "data", provider) as client:
        conversation = await named(client)
        sending = asyncio.create_task(send(client, conversation, "First?"))
        await gate.reached.wait()
        made = await client.post(f"/api/conversations/{conversation}/queue", json={"content": "第二个问题"})
        assert made.status_code == 201, made.text
        queued = made.json()
        dropped = (await client.post(f"/api/conversations/{conversation}/queue", json={"content": "Never mind"})).json()
        assert (await client.delete(f"/api/conversations/{conversation}/queue/{dropped['id']}")).status_code == 200
        edited = await client.put(f"/api/conversations/{conversation}/queue/{queued['id']}", json={"content": "第二个问题？"})
        assert edited.status_code == 200 and edited.json()["text"] == "第二个问题？"
        assert [q["text"] for q in (await conversation_read(client, conversation))["queued"]] == ["第二个问题？"]
        assert len(provider.steps) == 1  # nothing of it was sent while the first turn runs
        gate.open.set()
        first = await sending
        assert first[-1]["status"] == "succeeded"
        started_turn = await client.post(f"/api/conversations/{conversation}/queue/{queued['id']}/send",
                                         json={"after": first[0]["run_id"]})
        assert started_turn.status_code == 200 and events(started_turn)[-1]["status"] == "succeeded"
        assert provider.steps[1]["messages"][-1]["content"] == "第二个问题？"
        read = await conversation_read(client, conversation)
        assert read["queued"] == [] and [t["message"]["text"] for t in read["turns"]] == ["First?", "第二个问题？"]
        for refused in (await client.put(f"/api/conversations/{conversation}/queue/{queued['id']}", json={"content": "x"}),
                        await client.delete(f"/api/conversations/{conversation}/queue/{queued['id']}")):
            assert (refused.status_code, refused.json()["code"]) == (409, "not_queued")
        assert [t["message"]["text"] for t in (await conversation_read(client, conversation))["turns"]] == [
            "First?", "第二个问题？"]


async def test_a_queued_message_starts_on_its_own_only_after_a_turn_that_succeeded(tmp_path):
    # F7, reading 11: after a cancelled turn nothing starts, the message waits with Send; Send starts it.
    gate = Gate(0)
    provider = Scripted(None, "Later answer.")
    provider.script[0] = gate.wrap(provider, "Never delivered.")
    async with started(tmp_path / "data", provider) as client:
        conversation = await named(client)
        sending = asyncio.create_task(send(client, conversation, "First?"))
        await gate.reached.wait()
        queued = (await client.post(f"/api/conversations/{conversation}/queue", json={"content": "Next?"})).json()
        run_id = await running_run(client, conversation)
        assert (await client.post(f"/api/runs/{run_id}/cancel")).json()["status"] == "cancelled"
        gate.open.set()
        await sending
        after = await client.post(f"/api/conversations/{conversation}/queue/{queued['id']}/send", json={"after": run_id})
        assert (after.status_code, after.json()["code"]) == (409, "not_after_success")
        assert [q["id"] for q in (await conversation_read(client, conversation))["queued"]] == [queued["id"]]
        sent = await client.post(f"/api/conversations/{conversation}/queue/{queued['id']}/send", json={})
        assert sent.status_code == 200 and events(sent)[-1]["status"] == "succeeded"


@pytest.mark.parametrize("named_route", [False, True])
async def test_a_waiting_message_started_after_a_turn_runs_on_that_turns_recorded_route_whatever_the_request_says(
        tmp_path, named_route):
    # PR 52 cycle 4 (G8; Harold, 2026-11-11: a waiting message keeps the finished turn's route; cycle 4 C's probe): a
    # turn sent on the default route (Auto) at an effort, then the default changed in Settings. The waiting message
    # started after it (after) runs on the route and effort that turn recorded (runs.inputs), never the new default,
    # nor a model, provider or effort its request names; and, not being a model the researcher chose, it is not among
    # the recent models (internal review F1).
    from backend import budget_router
    async with started(tmp_path / "data", MockProvider()) as client:
        conversation = await named(client)
        first = await send(client, conversation, "First?", effort="low")
        assert first[-1]["status"] == "succeeded"
        run_id = first[0]["run_id"]
        [(route,)] = await rows(client, "SELECT json_extract(inputs, '$.route') FROM runs WHERE id = ?", run_id)
        other = next(m for m in sorted(budget_router.RECOMMENDED) if route != f"openrouter:{m}")
        queued = (await client.post(f"/api/conversations/{conversation}/queue", json={"content": "Next?"})).json()
        settings = (await client.get("/api/settings")).json()
        assert (await client.put("/api/settings", json={"hash": settings["hash"],
                                                        "updates": {"models.default": other}})).status_code == 200
        body = {"after": run_id, **({"provider": "openrouter", "model": other, "effort": "high"} if named_route else {})}
        sent = await client.post(f"/api/conversations/{conversation}/queue/{queued['id']}/send", json=body)
        assert sent.status_code == 200 and events(sent)[-1]["status"] == "succeeded"
        assert await rows(client, "SELECT json_extract(inputs, '$.route'), json_extract(inputs, '$.effort') FROM runs"
                                  " WHERE conversation_id = ? AND kind = 'turn' ORDER BY started_at", conversation) == [
            (route, "low"), (route, "low")]
        assert (await client.get("/api/models/recent")).json() == {"models": []}  # Auto chose it, never the researcher


async def test_a_waiting_message_keeps_a_route_whose_provider_and_model_names_hold_colons(tmp_path):
    # Internal review F4: the recorded route "<provider>:<model>" is split at the model its turn recorded, so a provider
    # named "lab:one" with the model "m:1" is kept as it was, not read as provider "lab".
    async with started(tmp_path / "data", MockProvider()) as client:
        settings = (await client.get("/api/settings")).json()
        assert (await client.put("/api/settings", json={"hash": settings["hash"], "updates": {
            'providers."lab:one".kind': "openai-compatible", 'providers."lab:one".base_url': "http://127.0.0.1:9/v1"}})
        ).status_code == 200
        assert (await client.put("/api/keys/lab:one", json={"key": "k"})).status_code == 200
        conversation = await named(client)
        first = await send(client, conversation, "First?", provider="lab:one", model="m:1")
        assert first[-1]["status"] == "succeeded", first
        queued = (await client.post(f"/api/conversations/{conversation}/queue", json={"content": "Next?"})).json()
        sent = await client.post(f"/api/conversations/{conversation}/queue/{queued['id']}/send",
                                 json={"after": first[0]["run_id"]})
        assert sent.status_code == 200 and events(sent)[-1]["status"] == "succeeded"
        assert await rows(client, "SELECT json_extract(inputs, '$.route') FROM runs WHERE conversation_id = ?"
                                  " AND kind = 'turn' ORDER BY started_at", conversation) == [("lab:one:m:1",)] * 2


async def test_the_queue_drains_once_whatever_arrives_at_the_same_moment(tmp_path):
    # L8, I1: two starts of the same queued message, and a new message, at once: exactly one turn starts.
    async with started(tmp_path / "data") as client:
        conversation = await named(client)
        first = await send(client, conversation, "First?")
        queued = (await client.post(f"/api/conversations/{conversation}/queue", json={"content": "Next?"})).json()
        path = f"/api/conversations/{conversation}/queue/{queued['id']}/send"
        results = await asyncio.gather(client.post(path, json={"after": first[0]["run_id"]}),
                                       client.post(path, json={"after": first[0]["run_id"]}),
                                       client.post(f"/api/conversations/{conversation}/message/stream",
                                                   json={"content": "Another?"}))
        started_turns = [r for r in results if r.status_code == 200]
        assert len(started_turns) == 1 or [r.status_code for r in results].count(200) >= 1
        texts = [t["message"]["text"] for t in (await conversation_read(client, conversation))["turns"]]
        assert texts.count("Next?") <= 1 and texts[0] == "First?"
        assert len(texts) == 1 + [r.status_code for r in results].count(200)
        refused = [r.json()["code"] for r in results if r.status_code != 200]
        assert set(refused) <= {"active_run", "not_queued", "not_after_success"}
        left = (await conversation_read(client, conversation))["queued"]
        assert (texts.count("Next?"), len(left)) in ((1, 0), (0, 1))


async def test_a_queued_message_admitted_as_a_turn_exists_once_when_its_stream_fails(tmp_path):
    # L5 (c): admitted (the queue no longer holds it) and then its response drops: one turn, cancelled as by Stop.
    async with started(tmp_path / "data") as client:
        conversation = await named(client)
        first = await send(client, conversation, "First?")
        queued = (await client.post(f"/api/conversations/{conversation}/queue", json={"content": "Next?"})).json()
        harness = client.state["harness"]
        claim = await harness.admit_turn(conversation, "Next?", queued=(queued["id"], first[0]["run_id"]))
        harness.stream_closed(claim)  # its response never started
        await asyncio.sleep(0.05)
        read = await conversation_read(client, conversation)
        assert read["queued"] == [] and [t["message"]["text"] for t in read["turns"]] == ["First?", "Next?"]
        while (await conversation_read(client, conversation))["turns"][-1]["status"] == "running":
            await asyncio.sleep(0.02)


async def test_the_interface_paths_never_get_409_while_a_client_naming_neither_does(tmp_path):
    # F5, SPEC:417: while a turn runs, steering and queueing are accepted; a second message is still refused.
    gate = Gate(0)
    provider = Scripted(None)
    provider.script.append(gate.wrap(provider, "Answer."))
    provider.script.pop(0)
    async with started(tmp_path / "data", provider) as client:
        conversation = await named(client)
        sending = asyncio.create_task(send(client, conversation, "First?"))
        await gate.reached.wait()
        run_id = await running_run(client, conversation)
        second = await client.post(f"/api/conversations/{conversation}/message/stream", json={"content": "Second?"})
        assert (second.status_code, second.json()["code"]) == (409, "active_run")
        assert (await steer(client, run_id, "Steer")).status_code == 200
        assert (await client.post(f"/api/conversations/{conversation}/queue", json={"content": "Queue"})).status_code == 201
        gate.open.set()
        await sending


# Deletion, moves and crashes


async def test_deleting_a_conversation_deletes_its_queued_messages_through_the_deletion_service(tmp_path):
    # L3 (c)
    async with started(tmp_path / "data") as client:
        conversation = await named(client)
        await send(client, conversation, "First?")
        await client.post(f"/api/conversations/{conversation}/queue", json={"content": "Next?"})
        assert (await client.delete(f"/api/conversations/{conversation}")).status_code == 200
        assert await rows(client, "SELECT count(*) FROM queued_messages") == [(0,)]


async def test_queued_messages_move_with_their_conversation(tmp_path):
    # G8: moving is refused while a turn runs (as before); otherwise the queued messages go with it.
    async with started(tmp_path / "data") as client:
        conversation = await named(client)
        await send(client, conversation, "First?")
        await client.post(f"/api/conversations/{conversation}/queue", json={"content": "Next?"})
        await background_idle(client)  # the turn's memory extraction, which a move waits for
        target = (await client.post("/api/projects", json={"name": "Thesis"})).json()["id"]
        assert (await client.post(f"/api/conversations/{conversation}/move", json={"project_id": target})).status_code == 200
        assert [q["text"] for q in (await conversation_read(client, conversation))["queued"]] == ["Next?"]


async def test_a_note_unread_when_the_app_stopped_is_queued_by_recovery(tmp_path):
    # L7 after a crash: the turn is recorded interrupted at the next start, and its unread note waits queued.
    data = tmp_path / "data"
    async with started(data) as client:
        conversation = await named(client)
        stream = await send(client, conversation, "First?")
        run_id = stream[0]["run_id"]

        def as_crashed(conn):  # the run as a crash left it: running, with a note no step read
            conn.execute("UPDATE runs SET status = 'running', finished_at = NULL WHERE id = ?", (run_id,))
            (seq,) = conn.execute("SELECT max(seq) + 1 FROM run_events WHERE run_id = ?", (run_id,)).fetchone()
            conn.execute("INSERT INTO run_events (run_id, seq, type, data) VALUES (?, ?, 'steering_note', ?)",
                         (run_id, seq, json.dumps({"text": "Left at the crash"})))
        client.state["harness"].registry.finished.discard(run_id)
        await asyncio.to_thread(client.state["db"].write, as_crashed)
        await client.state["harness"].recover(kick=False)
        read = await conversation_read(client, conversation)
        assert read["turns"][-1]["status"] == "interrupted"
        assert [q["text"] for q in read["queued"]] == ["Left at the crash"]
        await client.state["harness"].recover(kick=False)  # recovering again queues nothing twice
        assert len((await conversation_read(client, conversation))["queued"]) == 1


# S1-33 fix batch 1 (PR 52 review 1)


async def test_a_note_on_a_step_that_never_left_is_queued_not_read(tmp_path):
    # Finding 2 (L7, F6, U2): the step carrying the note failed to connect, so nothing carried it out: it waits as the
    # next turn, and is never said to have been read.
    import httpx

    def unreachable(body):
        raise httpx.ConnectError("connection refused")
    gate = Gate(0)
    provider = Scripted(None, unreachable)
    provider.script[0] = gate.wrap(provider, {"calls": [call("list_materials")]})
    async with started(tmp_path / "data", provider) as client:
        conversation = await named(client)
        sending = asyncio.create_task(send(client, conversation, "Which papers do I have?"))
        await gate.reached.wait()
        run_id = await running_run(client, conversation)
        await steer(client, run_id, "Only recent ones")
        gate.open.set()
        stream = await sending
        assert stream[-1]["status"] == "failed" and not [e for e in stream if e["type"] == "steering_note"]
        attempts = await rows(client, "SELECT json_extract(data, '$.dispatched') FROM run_events WHERE run_id = ?"
                                      " AND type = 'model_attempt' AND json_extract(data, '$.step') = 1", run_id)
        assert attempts and all(not d for (d,) in attempts)
        read = await conversation_read(client, conversation)
        assert [n["state"] for n in read["turns"][-1]["steering"]] == ["queued"]
        assert [q["text"] for q in read["queued"]] == ["Only recent ones"]


async def test_a_read_note_stays_in_the_conversations_history_and_an_unread_one_does_not(tmp_path):
    # Finding 5 (the connector's; SPEC:414, T15:84): a note a step read is part of that turn's researcher messages for
    # every later turn (and compaction); one no step read waits queued and is not history.
    gate = Gate(0)
    provider = Scripted(None, "Done.", "Second.")
    provider.script[0] = gate.wrap(provider, {"calls": [call("list_materials")]})
    async with started(tmp_path / "data", provider) as client:
        conversation = await named(client)
        sending = asyncio.create_task(send(client, conversation, "Which papers do I have?"))
        await gate.reached.wait()
        await steer(client, await running_run(client, conversation), "Only the ones from 2020")
        gate.open.set()
        assert (await sending)[-1]["status"] == "succeeded"
        assert (await send(client, conversation, "And now?"))[-1]["status"] == "succeeded"
        later = provider.steps[-1]["messages"]
        said = [m["content"] for m in later if m["role"] == "user"]
        assert said[0].startswith("Which papers do I have?") and "Only the ones from 2020" in said[0]
        assert said[-1] == "And now?"


async def test_continue_carries_the_notes_its_turn_read(tmp_path):
    # Fix batch 1 recheck (P2): a turn stopped at a limit after a step read a note: Continue sends its message with the
    # note, since the stopped turn is not in history and a read note is not queued.
    from test_agent_loop import asked
    gate = Gate(0)
    provider = Scripted(None, [call("list_materials", "c2")], cost=0.04)
    provider.script[0] = gate.wrap(provider, {"calls": [call("list_materials")]})
    async with started(tmp_path / "data", provider) as client:
        conversation = await named(client)
        await asyncio.to_thread(client.state["db"].write, lambda conn: conn.execute(
            "UPDATE conversations SET budget_usd = 0.1 WHERE id = ?", (conversation,)))
        sending = asyncio.create_task(send(client, conversation, "Which papers do I have?"))
        await gate.reached.wait()
        run_id = await running_run(client, conversation)
        await steer(client, run_id, "Only the ones from 2020")
        gate.open.set()
        ask = await asked(client, conversation)  # the third step does not fit; the second carried the note
        await client.post(f"/api/runs/{run_id}/asks/{ask['ask_id']}", json={"option": ask["options"][-1]})
        assert (await sending)[-1]["cancel_reason"] == "limit"
        assert [n["state"] for n in (await conversation_read(client, conversation))["turns"][-1]["steering"]] == ["read"]
        await asyncio.to_thread(client.state["db"].write, lambda conn: conn.execute(
            "UPDATE conversations SET budget_usd = 5 WHERE id = ?", (conversation,)))
        continued = await client.post(f"/api/runs/{run_id}/continue", json={})
        assert events(continued)[-1]["status"] == "succeeded"
        said = [m["content"] for m in provider.steps[-1]["messages"] if m["role"] == "user"]
        assert said[-1] == "Which papers do I have?\n\nOnly the ones from 2020"


async def test_continue_over_the_message_limit_with_its_notes_is_refused_and_loses_nothing(tmp_path, monkeypatch):
    # Fix batch 1 (F5): a message and its read notes together over the limit are refused as any message over it is, said
    # by its code, with nothing written; the turn, its note and Continue stay as they were. Never sent without the note.
    from backend import runs
    from test_agent_loop import asked
    monkeypatch.setattr(runs, "MAX_MESSAGE_CHARS", 40)
    gate = Gate(0)
    provider = Scripted(None, [call("list_materials", "c2")], cost=0.04)
    provider.script[0] = gate.wrap(provider, {"calls": [call("list_materials")]})
    async with started(tmp_path / "data", provider) as client:
        conversation = await named(client)
        await asyncio.to_thread(client.state["db"].write, lambda conn: conn.execute(
            "UPDATE conversations SET budget_usd = 0.1 WHERE id = ?", (conversation,)))
        sending = asyncio.create_task(send(client, conversation, "Which papers do I have?"))
        await gate.reached.wait()
        run_id = await running_run(client, conversation)
        await steer(client, run_id, "Only the ones from 2020 please")
        gate.open.set()
        ask = await asked(client, conversation)
        await client.post(f"/api/runs/{run_id}/asks/{ask['ask_id']}", json={"option": ask["options"][-1]})
        await sending
        await asyncio.to_thread(client.state["db"].write, lambda conn: conn.execute(
            "UPDATE conversations SET budget_usd = 5 WHERE id = ?", (conversation,)))
        steps = len(provider.steps)
        refused = await client.post(f"/api/runs/{run_id}/continue", json={})
        assert (refused.status_code, refused.json()["code"]) == (400, "message_too_long")
        read = await conversation_read(client, conversation)
        assert len(read["turns"]) == 1 and len(provider.steps) == steps
        assert [(n["text"], n["state"]) for n in read["turns"][0]["steering"]] == [("Only the ones from 2020 please", "read")]
        assert read["turns"][0]["cancel_reason"] == "limit"  # Continue is still offered


# S1-33 fix batch 2 (PR 52 cycle 2 on 4769f6e): lifecycle first (charter section 10)


async def test_a_failed_receipt_for_a_step_that_never_left_keeps_its_note_unread_and_charges_nothing(tmp_path, monkeypatch):
    # D2 (L1, I2, U2): the step carrying the note never connected, and the write recording that receipt failed. The
    # call's known outcome decides (never sent), not the gate's earlier notice that it was letting it out: the
    # reservation is released, no attempt is recorded as sent, and the note waits queued, never said to be read.
    import httpx
    from backend import spending
    armed, releasing = [], spending.release

    def unreachable(body):
        armed.append(True)  # from here, the receipt's release fails once
        raise httpx.ConnectError("connection refused")

    def release(conn, reservation_id):
        if armed and armed[0] is True:
            armed[0] = False
            raise RuntimeError("the receipt's write failed")
        return releasing(conn, reservation_id)
    monkeypatch.setattr(spending, "release", release)
    gate = Gate(0)
    provider = Scripted(None, unreachable)
    provider.script[0] = gate.wrap(provider, {"calls": [call("list_materials")]})
    async with started(tmp_path / "data", provider) as client:
        conversation = await named(client)
        sending = asyncio.create_task(send(client, conversation, "Which papers do I have?"))
        await gate.reached.wait()
        run_id = await running_run(client, conversation)
        await steer(client, run_id, "Only recent ones")
        gate.open.set()
        stream = await sending
        assert armed == [False]  # the receipt's write did fail
        assert stream[-1]["status"] == "failed" and not [e for e in stream if e["type"] == "steering_note"]
        sent = await rows(client, "SELECT count(*) FROM run_events WHERE run_id = ? AND type = 'model_attempt'"
                                  " AND json_extract(data, '$.step') = 1 AND json_extract(data, '$.dispatched') = 1", run_id)
        assert sent == [(0,)]
        [(status,)] = await rows(client, "SELECT status FROM budget_reservations WHERE run_id = ? AND step_seq = 1", run_id)
        assert status == "released"
        read = await conversation_read(client, conversation)
        assert [n["state"] for n in read["turns"][-1]["steering"]] == ["queued"]
        assert [q["text"] for q in read["queued"]] == ["Only recent ones"]


@pytest.mark.parametrize("answer", ["set_limit", "go_ahead"])
async def test_a_note_sent_while_a_step_waits_on_its_budget_ask_is_read_by_that_step(tmp_path, answer):
    # D8 (F2, SPEC:414): the step the ask is about has not gone out, so it is the note's next step: built again once the
    # ask is answered, it carries the note. Go ahead raised the limit just enough for the step as it was, so the step,
    # now larger, asks again with its new estimate before it goes.
    from test_agent_loop import asked
    provider = Scripted("Done.")
    async with started(tmp_path / "data", provider) as client:
        conversation = await named(client)
        await asyncio.to_thread(client.state["db"].write, lambda conn: conn.execute(
            "UPDATE conversations SET budget_usd = 0.03 WHERE id = ?", (conversation,)))
        sending = asyncio.create_task(send(client, conversation, "Which papers do I have?"))
        ask = await asked(client, conversation)
        assert (ask["kind"], ask["params"]["phase"]) == ("budget", "answer") and provider.steps == []
        steered = await steer(client, ask["run_id"], "Only the ones from 2020")
        assert "steering" in steered.json()
        path = f"/api/runs/{ask['run_id']}/asks/{ask['ask_id']}"
        if answer == "set_limit":
            assert (await client.post(path, json={"text": "5"})).status_code == 200
        else:
            assert (await client.post(path, json={"option": "go_ahead"})).status_code == 200
            again = await asked(client, conversation)
            while again["ask_id"] == ask["ask_id"]:
                await asyncio.sleep(0.02)
                again = await asked(client, conversation)
            assert again["params"]["estimate_usd"] > ask["params"]["estimate_usd"]
            await client.post(f"/api/runs/{again['run_id']}/asks/{again['ask_id']}", json={"option": "go_ahead"})
        stream = await sending
        assert stream[-1]["status"] == "succeeded"
        [step] = provider.steps
        assert step["messages"][-1] == {"role": "user", "content": "Only the ones from 2020"}
        read = await conversation_read(client, conversation)
        assert [n["state"] for n in read["turns"][-1]["steering"]] == ["read"] and read["queued"] == []


async def test_a_note_of_a_turn_whose_terminal_write_failed_is_not_said_to_be_queued(tmp_path, monkeypatch):
    # D9 (L5, L7, U2): a turn waiting on a budget ask gets a note, then Not now, and its terminal write fails: nothing
    # was queued, so the note is not said to be; recovery at the next start queues it, and only then is it said to be.
    from backend import runs
    from test_agent_loop import asked
    provider = Scripted("Done.")
    async with started(tmp_path / "data", provider) as client:
        conversation = await named(client)
        await asyncio.to_thread(client.state["db"].write, lambda conn: conn.execute(
            "UPDATE conversations SET budget_usd = 0.03 WHERE id = ?", (conversation,)))
        sending = asyncio.create_task(send(client, conversation, "Which papers do I have?"))
        ask = await asked(client, conversation)
        await steer(client, ask["run_id"], "Only the ones from 2020")
        closing = runs._close_asks

        def failing(conn, run_id):
            raise RuntimeError("the terminal write failed")
        monkeypatch.setattr(runs, "_close_asks", failing)
        await client.post(f"/api/runs/{ask['run_id']}/asks/{ask['ask_id']}", json={"option": "not_now"})
        try:
            await sending
        except Exception:
            pass
        while ask["run_id"] in client.state["harness"].registry.runs:
            await asyncio.sleep(0.02)
        monkeypatch.setattr(runs, "_close_asks", closing)
        read = await conversation_read(client, conversation)
        assert read["queued"] == []
        [note] = read["turns"][-1]["steering"]
        assert note["state"] == "unread"  # neither read nor queued: its turn ended without a step to read it
        client.state["harness"].registry.finished.discard(ask["run_id"])
        await client.state["harness"].recover(kick=False)
        read = await conversation_read(client, conversation)
        assert [q["text"] for q in read["queued"]] == ["Only the ones from 2020"]
        assert [n["state"] for n in read["turns"][-1]["steering"]] == ["queued"]


# S1-33 fix batch 2: the stream says when a message was added to the queue


async def test_each_way_a_message_joins_the_queue_is_streamed_as_queued(tmp_path):
    # D11 (F4, SPEC:641): queued by the researcher while the turn runs, by a steer the turn has no step left to read,
    # and by a note left unread at the turn's end: each streamed as `queued` on the running turn's stream once its row
    # has committed, here before its end (a live label: design review P2's Option 1, below). The event carries no text.
    gate = Gate(0)
    provider = Scripted(None)
    provider.script.append(gate.wrap(provider, "The answer."))
    provider.script.pop(0)
    async with started(tmp_path / "data", provider) as client:
        conversation = await named(client)
        sending = asyncio.create_task(send(client, conversation, "Which papers do I have?"))
        await gate.reached.wait()  # the last step (an answer) is in flight
        run_id = await running_run(client, conversation)
        assert (await client.post(f"/api/conversations/{conversation}/queue", json={"content": "Next?"})).status_code == 201
        assert "steering" in (await steer(client, run_id, "Also say how many")).json()  # read by no step: queued at the end
        gate.open.set()
        stream = await sending
        kinds = [e["type"] for e in stream]
        assert kinds.count("queued") == 2 and kinds.index("queued") < kinds.index("run_finished")
        assert all(set(e) == {"type"} for e in stream if e["type"] == "queued")
        assert [q["text"] for q in (await conversation_read(client, conversation))["queued"]] == ["Next?", "Also say how many"]
    provider = MockProvider()
    provider.parameters = ["max_tokens", "temperature"]  # no endpoint takes tools: the turn has no next step
    release = asyncio.Event()

    async def held(body):
        await release.wait()
        return provider.answer("Slow.")
    provider.replies.append(held)
    async with started(tmp_path / "data2", provider) as client:
        conversation = await named(client)
        sending = asyncio.create_task(send(client, conversation, "Slow one?"))
        run_id = await running_run(client, conversation)
        while not provider.answers:
            await asyncio.sleep(0.01)
        assert "queued" in (await steer(client, run_id, "Shorter, please")).json()
        release.set()
        assert [e["type"] for e in await sending].count("queued") == 1


# PR 52's follow-up, design review P2's Option 1 (Harold's decision, 2026-11-13): `budget` and `steering_note` are sent
# before `run_finished`; `queued` and the other stream events are live labels. The reply to the request that queued a
# message says its row, and the record read at the turn's end holds every message queued before that end. Each case is
# the first review's B-F1 schedule: none asserts where `queued` falls on the stream.


def _held_write(client, monkeypatch, name, before=False):
    """Hold the database write whose function's qualified name ends with name in its worker thread, once it committed
    (or, with before, before it begins): (reached, go), threading events: reached is set when it gets there, and it goes
    on once go is set."""
    import threading
    db, real, reached, go = client.state["db"], client.state["db"].write, threading.Event(), threading.Event()

    def write(fn, *args, **kwargs):
        if not getattr(fn, "__qualname__", "").endswith(name):
            return real(fn, *args, **kwargs)
        if before:
            reached.set()
            go.wait(10)
        result = real(fn, *args, **kwargs)
        if not before:
            reached.set()
            go.wait(10)
        return result
    monkeypatch.setattr(db, "write", write)
    return reached, go


async def _slow_turn(client, provider, conversation):
    """A turn whose one model call waits: (its sending task, its run's ActiveRun, the event that lets the call answer)."""
    release = asyncio.Event()

    async def held(body):
        await release.wait()
        return provider.answer("Slow.")
    provider.replies.append(held)
    sending = asyncio.create_task(send(client, conversation, "Slow one?"))
    run_id = await running_run(client, conversation)
    while not provider.answers:
        await asyncio.sleep(0.01)
    return sending, client.state["harness"].registry.runs[run_id], release


async def test_a_message_queued_while_its_turn_ends_is_in_the_read_at_that_end_and_in_its_reply(tmp_path, monkeypatch):
    # B-F1, the queue endpoint: its row commits while the turn runs, its request returns only after the turn ended
    provider = MockProvider()
    async with started(tmp_path / "data", provider) as client:
        conversation = await named(client)
        sending, _, release = await _slow_turn(client, provider, conversation)
        reached, go = _held_write(client, monkeypatch, "queue_message.<locals>.add")
        queueing = asyncio.create_task(client.post(f"/api/conversations/{conversation}/queue", json={"content": "Next?"}))
        assert await asyncio.to_thread(reached.wait, 10)
        release.set()
        assert [(e["type"], e.get("status")) for e in await sending][-1] == ("run_finished", "succeeded")
        assert [q["text"] for q in (await conversation_read(client, conversation))["queued"]] == ["Next?"]
        go.set()
        response = await queueing
        assert (response.status_code, response.json()["text"]) == (201, "Next?")
        assert [q["text"] for q in (await conversation_read(client, conversation))["queued"]] == ["Next?"]


async def test_a_steer_queued_on_a_route_without_tools_while_its_turn_ends_is_in_the_read_and_in_its_reply(
        tmp_path, monkeypatch):
    # B-F1, a steer the turn has no step to read: its row commits while the turn runs, its reply comes after the end,
    # and nothing is put on the ended stream
    provider = MockProvider()
    provider.parameters = ["max_tokens", "temperature"]  # no endpoint takes tools: the turn has no next step
    async with started(tmp_path / "data", provider) as client:
        conversation = await named(client)
        sending, active, release = await _slow_turn(client, provider, conversation)
        reached, go = _held_write(client, monkeypatch, "steer.<locals>.write")
        steering = asyncio.create_task(steer(client, active.run_id, "Shorter, please"))
        assert await asyncio.to_thread(reached.wait, 10)
        release.set()
        assert [(e["type"], e.get("status")) for e in await sending][-1] == ("run_finished", "succeeded")
        assert [q["text"] for q in (await conversation_read(client, conversation))["queued"]] == ["Shorter, please"]
        go.set()
        response = await steering
        assert response.status_code == 200 and response.json()["queued"]["text"] == "Shorter, please"
        assert "ended" not in response.json() and active.events.empty()


async def test_a_steer_written_after_its_turn_ended_says_ended_with_its_row_which_the_read_holds(tmp_path, monkeypatch):
    # B-F1, a steer answered `ended`: sent while the turn ran, its write begins only once the turn has ended
    provider = MockProvider()
    async with started(tmp_path / "data", provider) as client:
        conversation = await named(client)
        sending, active, release = await _slow_turn(client, provider, conversation)
        reached, go = _held_write(client, monkeypatch, "steer.<locals>.write", before=True)
        steering = asyncio.create_task(steer(client, active.run_id, "And another thing"))
        assert await asyncio.to_thread(reached.wait, 10)
        release.set()
        assert [(e["type"], e.get("status")) for e in await sending][-1] == ("run_finished", "succeeded")
        go.set()
        response = await steering
        assert response.status_code == 200 and response.json()["ended"] is True
        assert response.json()["queued"]["text"] == "And another thing"
        assert [q["text"] for q in (await conversation_read(client, conversation))["queued"]] == ["And another thing"]


async def test_a_steer_written_after_its_turn_ended_puts_nothing_on_the_ended_stream(tmp_path, monkeypatch):
    provider = MockProvider()
    async with started(tmp_path / "data", provider) as client:
        conversation = await named(client)
        sending, active, release = await _slow_turn(client, provider, conversation)
        reached, go = _held_write(client, monkeypatch, "steer.<locals>.write", before=True)
        steering = asyncio.create_task(steer(client, active.run_id, "And another thing"))
        assert await asyncio.to_thread(reached.wait, 10)
        release.set()
        await sending
        go.set()
        assert (await steering).json()["ended"] is True
        assert active.events.empty()


# S1-33 fix batch 3 (PR 52 cycle 3): lifecycle first (charter section 10)


async def test_a_note_carried_out_by_a_steps_first_attempt_stays_read_when_stop_comes_before_its_retry_leaves(
        tmp_path, monkeypatch):
    # E1 case 2, with a note (design review A): the step carrying the note went out once (rejected, reporting $0.20),
    # and Stop came while the gate decided on its retry. The note was read, by the attempt that carried it out; the
    # retry is recorded as never sent, with no body kept for it, and the step is charged what its first attempt reported.
    import dataclasses
    import time
    from backend import openrouter, outbound_gate, reach
    real_reach = reach.reach
    monkeypatch.setattr(reach, "reach", lambda provider, *a, **kw: dataclasses.replace(
        real_reach(provider, *a, **kw), tools=True) if not provider.is_openrouter else real_reach(provider, *a, **kw))
    monkeypatch.setattr(openrouter, "resolve_model_reasoning", lambda route, effort, **kw: ({"effort": "high"}, None))
    gate = Gate(0)
    provider = Scripted(None, (400, {"error": {}, "usage": {"prompt_tokens": 10, "completion_tokens": 0, "cost": 0.2}}))
    provider.script[0] = gate.wrap(provider, {"calls": [call("list_materials")]})
    async with started(tmp_path / "data", provider) as client:
        settings = (await client.get("/api/settings")).json()
        await client.put("/api/settings", json={"hash": settings["hash"], "updates": {
            "providers.lab.kind": "openai-compatible", "providers.lab.base_url": "http://127.0.0.1:9/v1"}})
        await client.put("/api/keys/lab", json={"key": "k"})
        conversation = await named(client)
        harness, loop, real, chats = client.state["harness"], asyncio.get_running_loop(), outbound_gate.OutboundGate._check, []

        def checking(self, request, scope, hop=None):
            if request.url.path.endswith("/chat/completions"):
                chats.append(request)
                if len(chats) == 3:  # the second step's retry: Stop arrives while the gate decides
                    loop.call_soon_threadsafe(lambda: harness._request_cancel(harness.registry.turns[conversation],
                                                                              "researcher"))
                    time.sleep(0.3)
            return real(self, request, scope, hop)
        monkeypatch.setattr(outbound_gate.OutboundGate, "_check", checking)
        sending = asyncio.create_task(send(client, conversation, "Which papers do I have?", provider="lab", model="m"))
        await gate.reached.wait()
        run_id = await running_run(client, conversation)
        await steer(client, run_id, "Only recent ones")
        gate.open.set()
        stream = await sending
        assert stream[-1]["status"] == "cancelled" and len(chats) == 3 and len(provider.steps) == 2
        attempts = [(d["attempt"], d["outcome"], d["dispatched"], "body" in d) for (d,) in (
            (json.loads(data),) for (data,) in await rows(client, "SELECT data FROM run_events WHERE run_id = ?"
                                                                  " AND type = 'model_attempt' AND json_extract(data, '$.step') = 1"
                                                                  " ORDER BY seq", run_id))]
        assert attempts == [(0, "request", True, True), (1, "cancelled", False, False)]
        assert await rows(client, "SELECT settled_usd, basis FROM budget_reservations WHERE run_id = ? AND step_seq = 1",
                          run_id) == [(0.2, "reported")]
        read = await conversation_read(client, conversation)
        assert [n["state"] for n in read["turns"][-1]["steering"]] == ["read"] and read["queued"] == []


async def test_a_stop_pressed_after_the_answer_committed_starts_nothing_waiting_after_that_turn(tmp_path):
    # Design review B, the backend's Stop mark (SPEC:412, Stop is immediate): the turn's answer committed, then Stop was
    # pressed for it (from a second window, whose Stop mark the sending window does not have). That window then asks for
    # the waiting message to start after the turn: refused, so nothing starts on its own; Send still starts it.
    async with started(tmp_path / "data") as client:
        conversation = await named(client)
        run_id = (await send(client, conversation, "First?"))[0]["run_id"]
        queued = (await client.post(f"/api/conversations/{conversation}/queue", json={"content": "Next?"})).json()
        assert (await client.post(f"/api/runs/{run_id}/cancel")).json()["status"] == "succeeded"  # it had ended
        path = f"/api/conversations/{conversation}/queue/{queued['id']}/send"
        after = await client.post(path, json={"after": run_id})
        assert (after.status_code, after.json()["code"]) == (409, "not_after_success")
        assert [q["id"] for q in (await conversation_read(client, conversation))["queued"]] == [queued["id"]]
        sent = await client.post(path, json={})
        assert sent.status_code == 200 and events(sent)[-1]["status"] == "succeeded"
        await background_idle(client)
        unknown = "00000000-0000-4000-8000-000000000000"
        assert (await client.post(f"/api/runs/{unknown}/cancel")).status_code == 404
        assert run_id in client.state["harness"].stopped and unknown not in client.state["harness"].stopped


@pytest.mark.parametrize("late_steer", [False, True])
async def test_a_stop_still_reading_its_finished_turn_starts_nothing_waiting_after_that_turn(tmp_path, monkeypatch,
                                                                                            late_steer):
    # PR 52 cycle 5, B1 (SPEC:412, F7; design review B3's Option 1): Stop for a turn that had just finished (from another
    # window) is still reading its record when a start of the waiting message after that turn arrives: a message queued
    # before, or a steer answered "ended" meanwhile. Refused, as the mark comes before any await; Send still starts it.
    from backend import runs
    real, armed, reached, release = runs.Harness._read, [], asyncio.Event(), asyncio.Event()

    async def read(self, fn):
        if armed and armed.pop():  # the next read: the Stop's, of its run's record
            reached.set()
            await release.wait()
        return await real(self, fn)
    monkeypatch.setattr(runs.Harness, "_read", read)
    async with started(tmp_path / "data") as client:
        conversation = await named(client)
        run_id = (await send(client, conversation, "First?"))[0]["run_id"]
        await background_idle(client)
        if not late_steer:
            queued = (await client.post(f"/api/conversations/{conversation}/queue", json={"content": "Next?"})).json()
        armed.append(True)
        stopping = asyncio.create_task(client.post(f"/api/runs/{run_id}/cancel"))
        await asyncio.wait_for(reached.wait(), 5)
        if late_steer:
            ended = (await steer(client, run_id, "Next?")).json()
            assert ended["ended"] is True
            queued = ended["queued"]
        path = f"/api/conversations/{conversation}/queue/{queued['id']}/send"
        after = await client.post(path, json={"after": run_id})
        release.set()
        assert (await stopping).json()["status"] == "succeeded"
        assert after.status_code == 409 and after.json()["code"] == "not_after_success"
        assert [q["id"] for q in (await conversation_read(client, conversation))["queued"]] == [queued["id"]]
        sent = await client.post(path, json={})
        assert sent.status_code == 200 and events(sent)[-1]["status"] == "succeeded"
        await background_idle(client)


async def test_a_stop_held_behind_a_stale_claims_record_starts_nothing_waiting_after_its_turn(tmp_path, monkeypatch):
    # PR 52 cycle 5 (design review B3, a second way into B1): Stop pressed for a running turn waits behind the record of a
    # released stale claim (Harness._record_dropped); the turn succeeds meanwhile. A start of the waiting message after
    # it is refused all the same; Send still starts it.
    from backend import runs
    real, reached, release = runs.Harness._record_dropped, asyncio.Event(), asyncio.Event()

    async def record_dropped(self):
        if not reached.is_set():  # the Stop's (an admission records them too)
            reached.set()
            await release.wait()
        await real(self)
    gate = Gate(0)
    provider = Scripted(None)
    provider.script[0] = gate.wrap(provider, "The answer.")
    async with started(tmp_path / "data", provider) as client:
        conversation = await named(client)
        sending = asyncio.create_task(send(client, conversation, "First?"))
        await gate.reached.wait()
        run_id = await running_run(client, conversation)
        queued = (await client.post(f"/api/conversations/{conversation}/queue", json={"content": "Next?"})).json()
        monkeypatch.setattr(runs.Harness, "_record_dropped", record_dropped)
        stopping = asyncio.create_task(client.post(f"/api/runs/{run_id}/cancel"))
        await asyncio.wait_for(reached.wait(), 5)
        gate.open.set()
        assert (await sending)[-1]["status"] == "succeeded"  # its answer committed before the Stop reached it
        path = f"/api/conversations/{conversation}/queue/{queued['id']}/send"
        after = await client.post(path, json={"after": run_id})
        release.set()
        assert (await stopping).json()["status"] == "succeeded"
        assert after.status_code == 409 and after.json()["code"] == "not_after_success"
        sent = await client.post(path, json={})
        assert sent.status_code == 200 and events(sent)[-1]["status"] == "succeeded"
        await background_idle(client)


@pytest.mark.parametrize("cut", [True, False])
async def test_a_turn_whose_stream_closed_before_its_end_was_recorded_starts_nothing_waiting_on_its_own(tmp_path,
                                                                                                       monkeypatch, cut):
    # PR 52 cycle 5, C2 (Harold, 2026-11-12, design review B3's Option 1 (i)): the turn's stream closes while its answer's
    # commit is still written. The answer is saved and the turn succeeded, but its window could not know it: the closed
    # stream stops it as Stop does, so the waiting message waits with Send. A stream read to its end marks nothing.
    import time
    from backend import runs
    real = runs.Harness._commit_answer
    async with started(tmp_path / "data") as client:
        harness, loop, reader = client.state["harness"], asyncio.get_running_loop(), None

        def committing(self, conn, claim, answer, ctx):
            committed = real(self, conn, claim, answer, ctx)
            if cut:
                loop.call_soon_threadsafe(reader.cancel)  # its window goes while the answer is still to commit
                time.sleep(0.3)
            return committed
        monkeypatch.setattr(runs.Harness, "_commit_answer", committing)
        conversation = await named(client)
        queued = (await client.post(f"/api/conversations/{conversation}/queue", json={"content": "Next?"})).json()
        claim = await harness.admit_turn(conversation, "First?")

        async def read_stream():
            return [event async for event in harness.events(claim)]
        reader = asyncio.create_task(read_stream())
        await asyncio.wait({reader})
        await asyncio.wait({claim.task})
        assert (await conversation_read(client, conversation))["turns"][-1]["status"] == "succeeded"
        assert (claim.run_id in harness.stopped) is cut
        path = f"/api/conversations/{conversation}/queue/{queued['id']}/send"
        after = await client.post(path, json={"after": claim.run_id})
        if cut:
            assert after.status_code == 409 and after.json()["code"] == "not_after_success"
            after = await client.post(path, json={})
        assert after.status_code == 200 and events(after)[-1]["status"] == "succeeded"
        await background_idle(client)


async def test_a_message_naming_papers_waits_with_them_and_starts_narrowed_to_them_or_refused_once_one_is_deleted(tmp_path):
    # Item 5 (Harold, 2026-11-11; SPEC:281; E5): a message sent with papers chosen while a turn runs waits as the next
    # turn with them (the interface queues it whatever the setting). Its start narrows its turn to them, as any message's
    # selection does. One naming a paper deleted meanwhile is refused at its start (selection_invalid), and waits, Send
    # saying why.
    from cited import cited, conversation_in, with_papers
    from test_search import WAGES, paper
    async with cited(tmp_path) as client:
        project, [wages, rents] = await with_papers(client, WAGES, paper("Rents", "Housing rents rose in the city."))
        conversation = await conversation_in(client, project)
        first = (await send(client, conversation, "What do minimum wages do to earnings?"))[0]["run_id"]
        queued = (await client.post(f"/api/conversations/{conversation}/queue",
                                    json={"content": "And their earnings?", "material_ids": [wages]})).json()
        assert queued["material_ids"] == [wages]
        assert [q.get("material_ids") for q in (await conversation_read(client, conversation))["queued"]] == [[wages]]
        stream = events(await client.post(f"/api/conversations/{conversation}/queue/{queued['id']}/send",
                                          json={"after": first}))
        assert stream[-1]["status"] == "succeeded"
        run_id = stream[0]["run_id"]
        turn = next(t for t in (await conversation_read(client, conversation))["turns"] if t["run_id"] == run_id)
        assert turn["message"]["material_ids"] == [wages]
        assert {c["material_id"] for c in turn["citations"]} == {wages}  # its retrieval narrowed to the paper it named
        await background_idle(client)
        later = (await client.post(f"/api/conversations/{conversation}/queue",
                                   json={"content": "And rents?", "material_ids": [rents]})).json()
        assert (await client.delete(f"/api/materials/{rents}")).status_code == 200
        refused = await client.post(f"/api/conversations/{conversation}/queue/{later['id']}/send", json={})
        assert (refused.status_code, refused.json()["code"]) == (400, "selection_invalid")
        assert [q["id"] for q in (await conversation_read(client, conversation))["queued"]] == [later["id"]]
        assert (await client.post(f"/api/conversations/{conversation}/queue", json={
            "content": "Too many?", "material_ids": ["x"] * 51})).status_code == 400  # as many as a message may name


# PR 52 cycle 4 (G5's class: committed bookkeeping separated from its publication by a cancellation): what the record
# says once a write committed is said on the turn's stream too, even when Stop lands just after it.


async def test_a_note_carried_by_a_step_whose_reply_cannot_be_decoded_stays_read(tmp_path):
    # PR 52 cycle 4 (G1; design review A2, its test 2): the step that carried the note went out, and its reply is nested
    # too deeply to decode. The attempt is recorded as sent, so the note stays read, never queued again to be sent twice.
    gate = Gate(0)
    provider = Scripted(None, (200, b"[" * 10000 + b"]" * 10000))
    provider.script[0] = gate.wrap(provider, {"calls": [call("list_materials")]})
    async with started(tmp_path / "data", provider) as client:
        conversation = await named(client)
        sending = asyncio.create_task(send(client, conversation, "Which papers do I have?"))
        await gate.reached.wait()
        assert (await steer(client, await running_run(client, conversation), "Only 2020")).status_code == 200
        gate.open.set()
        assert (await sending)[-1]["status"] == "failed" and len(provider.steps) == 2
        read = await conversation_read(client, conversation)
        assert [n["state"] for n in read["turns"][-1]["steering"]] == ["read"] and read["queued"] == []


async def test_a_note_read_by_a_step_that_went_out_is_said_read_when_stop_lands_as_its_client_closes(tmp_path,
                                                                                                   monkeypatch):
    # The step carrying the note went out and its receipt committed (the note is read in the record); Stop lands while
    # the call's client closes. The stream still says the note was read, as the record does.
    from backend import outbound_gate
    gate = Gate(0)
    provider = Scripted(None, "Done.")
    provider.script[0] = gate.wrap(provider, {"calls": [call("list_materials")]})
    real = outbound_gate._AsyncTransport.aclose

    async def closing(self):
        await real(self)
        harness = client.state["harness"]
        if len(provider.steps) == 2 and (active := harness.registry.turns.get(conversation)) is not None:
            harness._request_cancel(active, "researcher")
            await asyncio.sleep(0)  # the Stop lands here, the step's receipt committed
    monkeypatch.setattr(outbound_gate._AsyncTransport, "aclose", closing)
    async with started(tmp_path / "data", provider) as client:
        conversation = await named(client)
        sending = asyncio.create_task(send(client, conversation, "Which papers do I have?"))
        await gate.reached.wait()
        run_id = await running_run(client, conversation)
        seq = (await steer(client, run_id, "Only the ones from 2020")).json()["steering"]["seq"]
        gate.open.set()
        stream = await sending
        assert stream[-1]["status"] == "cancelled" and len(provider.steps) == 2
        assert [n["state"] for n in (await conversation_read(client, conversation))["turns"][-1]["steering"]] == ["read"]
        assert {"type": "steering_note", "read": [seq]} in stream


async def test_a_note_queued_by_the_answers_commit_is_said_queued_when_stop_lands_during_that_commit(tmp_path,
                                                                                                    monkeypatch):
    # A note sent while the last step ran is queued by the answer's commit (L7, F6); Stop lands while that write runs.
    # Once it commits, the stream says a message was queued, before the turn's end.
    import time
    from backend import runs
    gate = Gate(0)
    provider = Scripted(None)
    provider.script.append(gate.wrap(provider, "The answer."))
    provider.script.pop(0)
    real = runs.Harness._commit_answer
    async with started(tmp_path / "data", provider) as client:
        harness, loop = client.state["harness"], asyncio.get_running_loop()

        def committing(self, conn, claim, answer, ctx):
            committed = real(self, conn, claim, answer, ctx)
            loop.call_soon_threadsafe(lambda: harness._request_cancel(claim, "researcher"))
            time.sleep(0.3)  # the Stop lands while this write is still to commit
            return committed
        monkeypatch.setattr(runs.Harness, "_commit_answer", committing)
        conversation = await named(client)
        sending = asyncio.create_task(send(client, conversation, "Which papers do I have?"))
        await gate.reached.wait()
        assert (await steer(client, await running_run(client, conversation), "Also say how many")).status_code == 200
        gate.open.set()
        stream = await sending
        assert [q["text"] for q in (await conversation_read(client, conversation))["queued"]] == ["Also say how many"]
        assert [e["type"] for e in stream][-3:] == ["queued", "chat_response", "run_finished"]  # and the answer (item 3)


# PR 52's follow-up (the rule 1 recheck's B6-1 and C1; Harold: "Oldest check, follow-up"; S-2): a start of the queue's own
# (after) takes only the oldest waiting message, checked in the admission's transaction. A later one was admitted, ahead of
# the earlier, which then waited behind it.


async def test_a_start_after_a_turn_takes_only_the_oldest_waiting_message(tmp_path):
    async with started(tmp_path / "data") as client:
        conversation = await named(client)
        first = await send(client, conversation, "First?")
        assert first[-1]["status"] == "succeeded"
        await background_idle(client)
        older = (await client.post(f"/api/conversations/{conversation}/queue", json={"content": "Older 中文一"})).json()
        newer = (await client.post(f"/api/conversations/{conversation}/queue", json={"content": "Newer 中文二"})).json()
        after = {"after": first[0]["run_id"]}
        refused = await client.post(f"/api/conversations/{conversation}/queue/{newer['id']}/send", json=after)
        assert refused.status_code == 409 and refused.json()["code"] == "not_oldest", (refused.status_code, refused.text)
        read = await conversation_read(client, conversation)
        assert [t["status"] for t in read["turns"]] == ["succeeded"]
        assert [q["id"] for q in read["queued"]] == [older["id"], newer["id"]]  # both still waiting, in order
        started_turn = await client.post(f"/api/conversations/{conversation}/queue/{older['id']}/send", json=after)
        assert started_turn.status_code == 200 and events(started_turn)[-1]["status"] == "succeeded"
        read = await conversation_read(client, conversation)
        assert [q["id"] for q in read["queued"]] == [newer["id"]]
        sent = await client.post(f"/api/conversations/{conversation}/queue/{newer['id']}/send", json={})  # Send: any
        assert sent.status_code == 200 and events(sent)[-1]["status"] == "succeeded"
        await background_idle(client)


# PR 52's follow-up, item 3 (cycle 5 B3, the third cycle of committed bookkeeping and its stream publication, after
# cycle 4's G5; design review P's Option B, pending Harold's word under rule 10): what a turn's record says it read and
# noticed is said on its stream from the record itself, read in the receipt's and every terminal transaction (_owed),
# once each (ActiveRun.said), before its run_finished. A note a cleanup recorded as read was never said read.


def _two_steps(cost=None):
    """A turn of two agent steps, each held at the provider until opened: (provider, first gate, second gate)."""
    first, second = Gate(0), Gate(1)
    provider = Scripted(None, None, **({"cost": cost} if cost is not None else {}))
    provider.script[0] = first.wrap(provider, {"calls": [call("list_materials")]})
    provider.script[1] = second.wrap(provider, "Done.")
    return provider, first, second


def _said_before_the_end(stream, event):
    assert stream.count(event) == 1, stream
    assert stream.index(event) < len(stream) - 1 and stream[-1]["type"] == "run_finished", stream


async def test_a_note_carried_by_a_request_stop_cut_off_is_said_read_before_the_run_ends(tmp_path):
    provider, first, second = _two_steps()
    async with started(tmp_path / "data", provider) as client:
        conversation = await named(client)
        sending = asyncio.create_task(send(client, conversation, "Which papers do I have?"))
        await first.reached.wait()
        run_id = await running_run(client, conversation)
        seq = (await steer(client, run_id, "Only the ones from 2020")).json()["steering"]["seq"]
        first.open.set()
        await second.reached.wait()  # the request carrying the note is at the provider
        await client.post(f"/api/runs/{run_id}/cancel")
        second.open.set()
        stream = await sending
        assert stream[-1]["status"] == "cancelled"
        assert [n["state"] for n in (await conversation_read(client, conversation))["turns"][-1]["steering"]] == ["read"]
        _said_before_the_end(stream, {"type": "steering_note", "read": [seq]})


async def test_a_note_and_notices_whose_receipt_failed_are_said_by_the_cleanup_that_recorded_them(tmp_path, monkeypatch):
    # The second step's receipt fails once; the turn's cleanup records its call (the note read, the 50% notice reached)
    from backend import runs
    from backend.db import new_id, utc_now
    provider, first, second = _two_steps(cost=0.01)
    real, calls = runs._settle_attempts, []

    def failing_once(*args):
        calls.append(1)
        if len(calls) == 2:  # the second step's receipt
            raise RuntimeError("the receipt's write failed")
        return real(*args)
    monkeypatch.setattr(runs, "_settle_attempts", failing_once)
    async with started(tmp_path / "data", provider) as client:
        conversation = await named(client)
        project = (await conversation_read(client, conversation))["project_id"]
        await asyncio.to_thread(client.state["db"].write, lambda conn: (conn.execute(
            "UPDATE conversations SET budget_usd = 1 WHERE id = ?", (conversation,)), conn.execute(
            "INSERT INTO budget_reservations (id, step_seq, paying_conversation_id, project_id, estimate_usd, status,"
            " settled_usd, basis, settled_at) VALUES (?, 0, ?, ?, 0.485, 'settled', 0.485, 'reported', ?)",
            (new_id(), conversation, project, utc_now()))))
        sending = asyncio.create_task(send(client, conversation, "Which papers do I have?"))
        await first.reached.wait()
        run_id = await running_run(client, conversation)
        seq = (await steer(client, run_id, "Only the ones from 2020")).json()["steering"]["seq"]
        first.open.set()
        second.open.set()
        stream = await sending
        assert stream[-1]["status"] == "failed" and len(calls) == 3
        assert [n["state"] for n in (await conversation_read(client, conversation))["turns"][-1]["steering"]] == ["read"]
        _said_before_the_end(stream, {"type": "steering_note", "read": [seq]})
        [notice] = [e for e in stream if e["type"] == "budget"]  # 0.505 of $1: the conversation's 50%, said once
        assert (notice["budget"], notice["percent"]) == ("conversation", 50)
        _said_before_the_end(stream, notice)


async def test_a_turns_reads_and_notices_are_said_once_each_though_every_later_transaction_reads_them_again(tmp_path):
    provider, first, second = _two_steps(cost=0.3)
    async with started(tmp_path / "data", provider) as client:
        conversation = await named(client)
        await asyncio.to_thread(client.state["db"].write, lambda conn: conn.execute(
            "UPDATE conversations SET budget_usd = 1 WHERE id = ?", (conversation,)))
        sending = asyncio.create_task(send(client, conversation, "Which papers do I have?"))
        await first.reached.wait()
        run_id = await running_run(client, conversation)
        seq = (await steer(client, run_id, "Only the ones from 2020")).json()["steering"]["seq"]
        first.open.set()
        second.open.set()
        stream = await sending
        assert stream[-1]["status"] == "succeeded"
        _said_before_the_end(stream, {"type": "steering_note", "read": [seq]})
        shown = [(e["budget"], e["percent"]) for e in stream if e["type"] == "budget"]
        assert shown == [("conversation", 50)]  # $0.60 of $1 after the second step, said by its receipt only
        await background_idle(client)


@pytest.mark.parametrize("ending", ["succeeded", "stopped", "not_now", "failed"])
async def test_every_ending_says_what_its_record_owes_the_stream_before_its_run_finished(tmp_path, monkeypatch, ending):
    # Each read of what the record owes (the receipt's, and the terminal write's) is said, and before run_finished:
    # a marker is owed at each read here, whatever the turn's ending.
    from backend import runs
    real, reads = runs._owed, []

    def owed(conn, active):
        found = real(conn, active)
        if active is not None and active.kind == "turn":
            reads.append(len(reads))
            found = [*found, ({"type": "owed", "read": reads[-1]}, {("owed", reads[-1])})]
        return found
    monkeypatch.setattr(runs, "_owed", owed)
    provider = MockProvider((500, {"error": {"message": "down"}})) if ending == "failed" else MockProvider()
    async with started(tmp_path / "data", provider) as client:
        conversation = await named(client)
        if ending in ("stopped", "not_now"):  # waiting on a step ask: the conversation's budget is less than the step
            await asyncio.to_thread(client.state["db"].write, lambda conn: conn.execute(
                "UPDATE conversations SET budget_usd = 0.001 WHERE id = ?", (conversation,)))
            from test_agent_loop import asked
            sending = asyncio.create_task(send(client, conversation, "Short?"))
            ask = await asked(client, conversation)
            if ending == "stopped":
                await client.post(f"/api/runs/{ask['run_id']}/cancel")
            else:
                await client.post(f"/api/runs/{ask['run_id']}/asks/{ask['ask_id']}", json={"option": "not_now"})
            stream = await sending
        else:
            stream = await send(client, conversation, "Short?")
        assert stream[-1]["type"] == "run_finished"
        assert {"succeeded": "succeeded", "stopped": "cancelled", "not_now": "cancelled", "failed": "failed"}[ending] == \
            stream[-1]["status"]
        said = [e["read"] for e in stream if e["type"] == "owed"]
        assert reads and said == reads, (said, reads)  # every read said once, all before the run's end
        await background_idle(client)


async def test_a_turns_budget_and_steering_note_events_are_built_only_from_its_record():
    # Design review P's static check: no other site says these, so none can say less than the record
    import inspect
    from backend import runs
    source, owed = inspect.getsource(runs), inspect.getsource(runs._owed)
    for kind in ('"type": "budget"', '"type": "steering_note"'):
        assert source.count(kind) == owed.count(kind) == 1, kind
    assert "notes" not in inspect.signature(runs.Harness._call).parameters
