"""Continue: a new turn from an interrupted one, from the conversation as it is now."""

import asyncio
import json

import pytest

from backend.db import new_id
from scholia_app import FakeKeyring, MockProvider, background_idle, events, started

pytestmark = pytest.mark.asyncio


async def rows(client, sql, *args):
    return await asyncio.to_thread(client.state["db"].read, lambda conn: conn.execute(sql, args).fetchall())


async def seed_turn(client, conversation_id, *, status="running", cancel_reason=None, text="Where were we?"):
    run_id = new_id()

    def write(conn):
        project = conn.execute("SELECT project_id FROM conversations WHERE id = ?", (conversation_id,)).fetchone()[0]
        (seq,) = conn.execute("SELECT coalesce(max(seq) + 1, 0) FROM turns WHERE conversation_id = ?",
                              (conversation_id,)).fetchone()
        conn.execute("INSERT INTO runs (id, project_id, conversation_id, kind, status, cancel_reason)"
                     " VALUES (?, ?, ?, 'turn', ?, ?)", (run_id, project, conversation_id, status, cancel_reason))
        conn.execute("INSERT INTO turns (run_id, conversation_id, seq, author, user_message) VALUES (?, ?, ?, 'researcher', ?)",
                     (run_id, conversation_id, seq, json.dumps({"text": text})))
    await asyncio.to_thread(client.state["db"].write, write)
    return run_id


async def test_continue_after_a_crash_sends_the_message_again_as_a_new_turn(tmp_path):
    data, keyring = tmp_path / "data", FakeKeyring()
    async with started(data, keyring=keyring) as client:
        conversation = (await client.post("/api/conversations", json={"title": "Kept"})).json()["id"]
        interrupted = await seed_turn(client, conversation)  # as a crash leaves it

    provider = MockProvider()
    async with started(data, provider, keyring=keyring, setup=False) as client:
        response = await client.post(f"/api/runs/{interrupted}/continue")
        stream = events(response)
        assert stream[-1]["status"] == "succeeded"
        turns = (await client.get(f"/api/conversations/{conversation}")).json()["turns"]
        assert [(t["status"], t["continues"]) for t in turns] == [("interrupted", None), ("succeeded", interrupted)]
        assert turns[1]["message"] == {"text": "Where were we?"}
        # The interrupted turn's own steps are not reused: one fresh call with the message again.
        assert [m["content"] for m in provider.answers[0]["messages"][1:]] == ["Where were we?"]
        assert (await client.post(f"/api/runs/{interrupted}/continue")).json()["code"] == "not_continuable"
        await background_idle(client)


@pytest.mark.parametrize("status, cancel_reason, ok", [
    ("cancelled", "revoked", True),
    ("cancelled", "limit", True),
    ("cancelled", "researcher", False),  # a Stop is final; the researcher can send again
    ("failed", None, False),
    ("succeeded", None, False),
])
async def test_which_turns_offer_continue(tmp_path, status, cancel_reason, ok):
    async with started(tmp_path / "data") as client:
        conversation = (await client.post("/api/conversations", json={"title": "Kept"})).json()["id"]
        run_id = await seed_turn(client, conversation, status=status, cancel_reason=cancel_reason)
        response = await client.post(f"/api/runs/{run_id}/continue")
        if ok:
            assert events(response)[-1]["status"] == "succeeded"
        else:
            assert (response.status_code, response.json()["code"]) == (409, "not_continuable")
        await background_idle(client)


async def test_only_the_latest_turn_can_be_continued_and_an_unknown_one_is_404(tmp_path):
    async with started(tmp_path / "data") as client:
        conversation = (await client.post("/api/conversations", json={"title": "Kept"})).json()["id"]
        older = await seed_turn(client, conversation)
        await seed_turn(client, conversation, status="succeeded")
        assert (await client.post(f"/api/runs/{older}/continue")).json()["code"] == "not_continuable"
        assert (await client.post(f"/api/runs/{new_id()}/continue")).status_code == 404
        assert await rows(client, "SELECT count(*) FROM turns") == [(2,)]
