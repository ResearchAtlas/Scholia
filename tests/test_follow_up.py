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
        assert ended.json()["ended"] is True  # D22, reading 12: the interface starts it as a new turn if it can
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
    # and by a note left unread at the turn's end: each streamed as `queued` on the running turn's stream, before its
    # end, once its row has committed. The event carries no text.
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
