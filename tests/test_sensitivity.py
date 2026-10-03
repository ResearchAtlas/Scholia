"""Sensitivity levels and revocation at dispatch (slice-1 spec sections 5, 10 and F11; ticket 18).

Tightening a project applies at once and revokes its running work in the same transaction;
loosening needs the researcher's confirmation; both are audited. Before every dispatch,
retries included, the run must still be running and not revoked, with its owners, under the
project's current level and route. A revoked run ends cancelled, reason revoked; Continue
starts a new turn under the current policy, and a revoked run in a deleted conversation
offers none. Revocation cases for materials, memory, child runs, Council and indexing come
with the PRs that add them; an indexing-style run's dispatch check is covered here.
"""

import asyncio
import json

import pytest

from backend import governance, openrouter, openrouter_client
from backend.db import new_id
from backend.runs import may_dispatch
from scholia_app import FakeKeyring, MockProvider, background_idle, events, send, started

pytestmark = pytest.mark.asyncio

ZDR_MODEL = "example/zdr-model"
LOCAL = "http://127.0.0.1:11434/v1"


@pytest.fixture(autouse=True)
def fresh_catalog():
    openrouter_client.clear_cache()
    yield
    openrouter_client.clear_cache()


async def rows(client, sql, *args):
    return await asyncio.to_thread(client.state["db"].read, lambda conn: conn.execute(sql, args).fetchall())


async def wait_for(predicate, timeout=5.0):
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while not predicate():
        if loop.time() > deadline:
            raise AssertionError("condition not reached")
        await asyncio.sleep(0.01)


async def new_project(client, level="normal"):
    return (await client.post("/api/projects", json={"name": "Study", "sensitivity": level})).json()["id"]


async def new_conversation(client, project):
    return (await client.post("/api/conversations", json={"project_id": project})).json()["id"]


async def set_level(client, project, level, token=None):
    return await client.post(f"/api/projects/{project}/sensitivity",
                             json={"level": level, **({"token": token} if token else {})})


def held(provider, text="A held answer."):
    release = asyncio.Event()

    async def reply(body):
        await release.wait()
        return provider.answer(text)

    provider.replies.insert(0, reply)
    return release


async def test_tightening_applies_at_once_revokes_running_work_and_is_audited(tmp_path):
    provider = MockProvider()
    release = held(provider)
    async with started(tmp_path / "data", provider) as client:
        project = await new_project(client)
        conversation = await new_conversation(client, project)
        stream = asyncio.create_task(client.post(f"/api/conversations/{conversation}/message/stream",
                                                 json={"content": "SECRET-QUESTION"}))
        await wait_for(lambda: provider.answers)
        response = await set_level(client, project, "private")
        assert response.status_code == 200 and response.json()["sensitivity"] == "private"
        finished = events(await stream)
        release.set()
        assert finished[-1]["status"] == "cancelled" and finished[-1]["cancel_reason"] == "revoked"
        [turn] = (await client.get(f"/api/conversations/{conversation}")).json()["turns"]
        assert (turn["status"], turn["cancel_reason"], turn["answer"]) == ("cancelled", "revoked", None)
        [(data,)] = await rows(client, "SELECT data FROM audit_log WHERE event = 'sensitivity_changed'")
        assert json.loads(data) == {"from": "normal", "to": "private", "revoked_runs": 1}
        assert "SECRET" not in json.dumps(await rows(client, "SELECT * FROM audit_log"))
        await background_idle(client)


async def test_a_tightening_marks_the_running_runs_revoked_in_its_own_transaction(tmp_path):
    async with started(tmp_path / "data") as client:
        project = await new_project(client)
        conversation = await new_conversation(client, project)
        run_id, other = new_id(), new_id()

        def seed(conn):  # running work this process does not hold, as another task's would be
            conn.execute("INSERT INTO runs (id, project_id, conversation_id, kind) VALUES (?, ?, ?, 'turn')",
                         (run_id, project, conversation))
            conn.execute("INSERT INTO runs (id, project_id, kind, status) VALUES (?, ?, 'background', 'succeeded')",
                         (other, project))
        await asyncio.to_thread(client.state["db"].write, seed)
        assert (await set_level(client, project, "local_only")).status_code == 200
        assert await rows(client, "SELECT id, status, cancel_reason FROM runs ORDER BY id = ?", run_id) == [
            (other, "succeeded", None), (run_id, "running", "revoked")]


async def test_loosening_needs_a_confirmation_for_exactly_that_change(tmp_path):
    async with started(tmp_path / "data") as client:
        project = await new_project(client, "local_only")
        first = await set_level(client, project, "normal")
        assert (first.status_code, first.json()["code"]) == (409, "confirmation_required")
        token = first.json()["token"]
        other = await set_level(client, project, "private", token)  # a token confirms one change only
        assert other.json()["code"] == "confirmation_required"
        assert (await client.get(f"/api/projects/{project}")).json()["sensitivity"] == "local_only"
        token = (await set_level(client, project, "private")).json()["token"]
        assert (await set_level(client, project, "private", token)).json()["sensitivity"] == "private"
        assert (await set_level(client, project, "normal", token)).json()["code"] == "confirmation_required"  # once
        audit = await rows(client, "SELECT data FROM audit_log WHERE event = 'sensitivity_changed'")
        assert [json.loads(d) for (d,) in audit] == [{"from": "local_only", "to": "private", "revoked_runs": 0}]


async def test_the_general_project_stays_normal_and_the_same_level_changes_nothing(tmp_path):
    async with started(tmp_path / "data") as client:
        [general] = [p for p in (await client.get("/api/projects")).json()["projects"] if p["kind"] == "general"]
        response = await set_level(client, general["id"], "private")
        assert (response.status_code, response.json()["code"]) == (400, "general_project")
        project = await new_project(client, "private")
        assert (await set_level(client, project, "private")).status_code == 200
        assert await rows(client, "SELECT count(*) FROM audit_log WHERE event = 'sensitivity_changed'") == [(0,)]
        assert (await set_level(client, new_id(), "private")).status_code == 404


async def test_a_new_project_takes_its_level_from_the_question_and_records_it(tmp_path):
    async with started(tmp_path / "data") as client:
        for level in ("normal", "private", "local_only"):
            created = (await client.post("/api/projects", json={"name": "SECRET-NAME", "sensitivity": level})).json()
            assert (created["sensitivity"], created["review_lock"]) == (level, False)
        response = await client.post("/api/projects", json={"name": "P", "sensitivity": "secret"})
        assert response.status_code == 400
        audit = await rows(client, "SELECT data FROM audit_log WHERE event = 'project_created' ORDER BY seq")
        assert [json.loads(d) for (d,) in audit] == [
            {"sensitivity": level, "review_lock": False} for level in ("normal", "private", "local_only")]


@pytest.mark.parametrize("how, reason", [("tighten", "not_declared"), ("revoke", "revoked")])
async def test_a_project_changed_between_a_step_and_its_retry_sends_nothing_more(tmp_path, monkeypatch, how, reason):
    # The adapter retries once when an OpenAI-compatible endpoint rejects the reasoning field.
    # Here the project changes in the record between the attempts, before this process hears of
    # it: the gate's dispatch check, in its decision transaction, refuses the retry.
    monkeypatch.setattr(openrouter, "resolve_model_reasoning", lambda *a, **k: ({"effort": "high"}, None))
    provider = MockProvider()
    async with started(tmp_path / "data", provider) as client:
        current = (await client.get("/api/settings")).json()
        await client.put("/api/settings", json={"hash": current["hash"], "updates": {
            "providers.local.kind": "openai-compatible", "providers.local.base_url": LOCAL, "providers.local.models": "all"}})
        await client.put("/api/keys/local", json={"key": "local"})
        project = await new_project(client)
        conversation = await new_conversation(client, project)
        db = client.state["db"]

        def change(conn):
            if how == "tighten":
                conn.execute("UPDATE projects SET sensitivity = 'local_only' WHERE id = ?", (project,))
            governance.revoke_running(conn, project)

        async def rejected(body):
            await asyncio.to_thread(db.write, change)
            return 400, {"error": {"message": "unknown field: reasoning"}}

        provider.replies.append(rejected)
        stream = await send(client, conversation, model="llama", provider="local", effort="high")
        assert len(provider.answers) == 1  # the retry was never sent
        assert (stream[-1]["status"], stream[-1]["cancel_reason"]) == ("cancelled", "revoked")
        denied = await rows(client, "SELECT data ->> 'reason' FROM audit_log WHERE event = 'outbound'"
                                    " AND data ->> 'decision' = 'deny'")
        assert denied == [(reason,)]
        [(status, settled)] = await rows(client, "SELECT status, basis FROM budget_reservations"
                                                 " WHERE run_id = ?", stream[0]["run_id"])
        assert status == "settled"  # the first attempt went out: its cost is kept


async def test_a_title_run_while_its_project_is_tightened_stops_and_writes_no_title(tmp_path):
    provider = MockProvider()
    release = asyncio.Event()

    async def title(body):
        await release.wait()
        return provider.answer("A title")

    provider.title_replies.append(title)
    async with started(tmp_path / "data", provider) as client:
        project = await new_project(client)
        conversation = await new_conversation(client, project)
        await send(client, conversation)
        await wait_for(lambda: provider.titles)
        assert (await set_level(client, project, "private")).status_code == 200
        release.set()
        await background_idle(client)
        assert len(provider.titles) == 1  # nothing more
        assert await rows(client, "SELECT status, cancel_reason FROM runs WHERE workflow = 'title'") == [
            ("cancelled", "revoked")]
        assert (await client.get(f"/api/conversations/{conversation}")).json()["title"] is None


async def test_a_title_run_revoked_while_it_waited_makes_no_call_even_after_a_restart(tmp_path, monkeypatch):
    from backend.runs import Harness
    data, keyring = tmp_path / "data", FakeKeyring()
    real = Harness.kick_background

    async def not_now(self):  # the title run is queued, but does not start before the app closes
        return None

    monkeypatch.setattr(Harness, "kick_background", not_now)
    async with started(data, keyring=keyring) as client:
        project = await new_project(client)
        conversation = await new_conversation(client, project)
        await send(client, conversation)
        assert (await set_level(client, project, "local_only")).status_code == 200
        assert await rows(client, "SELECT status, cancel_reason FROM runs WHERE workflow = 'title'") == [
            ("running", "revoked")]

    monkeypatch.setattr(Harness, "kick_background", real)
    provider = MockProvider()
    async with started(data, provider, keyring=keyring, setup=False) as client:
        await background_idle(client)
        assert provider.chats == []
        assert await rows(client, "SELECT status, cancel_reason, attempts FROM runs WHERE workflow = 'title'") == [
            ("cancelled", "revoked", 0)]


async def test_continue_after_a_tightening_builds_its_turn_under_the_current_policy(tmp_path):
    provider = MockProvider(catalog=[ZDR_MODEL], zero_retention=[ZDR_MODEL])
    release = held(provider)
    async with started(tmp_path / "data", provider) as client:
        current = (await client.get("/api/settings")).json()
        await client.put("/api/settings", json={"hash": current["hash"], "updates": {"providers.openrouter.models": "all"}})
        project = await new_project(client)
        conversation = await new_conversation(client, project)
        stream = asyncio.create_task(client.post(f"/api/conversations/{conversation}/message/stream",
                                                 json={"content": "Where were we?", "model": ZDR_MODEL}))
        await wait_for(lambda: provider.answers)
        assert "provider" not in provider.answers[0]  # sent under Normal
        await set_level(client, project, "private")
        revoked = events(await stream)[0]["run_id"]
        release.set()

        refused = await client.post(f"/api/runs/{revoked}/continue", json={"model": ZDR_MODEL})
        assert (refused.status_code, refused.json()["code"]) == (403, "key_not_confirmed")  # Private's rule now
        await client.post("/api/key-attestations", json={"provider": "openrouter", "statement": governance.KEY_STATEMENT})
        continued = events(await client.post(f"/api/runs/{revoked}/continue", json={"model": ZDR_MODEL}))
        assert continued[-1]["status"] == "succeeded"
        assert provider.answers[-1]["provider"] == {"zdr": True}
        assert [m["content"] for m in provider.answers[-1]["messages"][1:]] == ["Where were we?"]  # no recorded step reused
        turns = (await client.get(f"/api/conversations/{conversation}")).json()["turns"]
        assert [(t["status"], t["cancel_reason"], t["continues"]) for t in turns] == [
            ("cancelled", "revoked", None), ("succeeded", None, revoked)]
        await background_idle(client)


async def test_a_revoked_run_in_a_deleted_conversation_offers_no_continue(tmp_path):
    provider = MockProvider()
    release = held(provider)
    async with started(tmp_path / "data", provider) as client:
        project = await new_project(client)
        conversation = await new_conversation(client, project)
        stream = asyncio.create_task(client.post(f"/api/conversations/{conversation}/message/stream",
                                                 json={"content": "hi"}))
        await wait_for(lambda: provider.answers)
        await set_level(client, project, "private")
        revoked = events(await stream)[0]["run_id"]
        release.set()
        assert (await client.delete(f"/api/conversations/{conversation}")).status_code == 200
        response = await client.post(f"/api/runs/{revoked}/continue")
        assert response.status_code == 404
        assert len(provider.answers) == 1


async def test_a_crash_leaves_a_revoked_turn_revoked(tmp_path):
    data, keyring = tmp_path / "data", FakeKeyring()
    async with started(data, keyring=keyring) as client:
        project = await new_project(client)
        conversation = await new_conversation(client, project)
        run_id = new_id()

        def seed(conn):  # a turn revoked in the record, as a crash right after the tightening leaves it
            conn.execute("INSERT INTO runs (id, project_id, conversation_id, kind, cancel_reason)"
                         " VALUES (?, ?, ?, 'turn', 'revoked')", (run_id, project, conversation))
            conn.execute("INSERT INTO turns (run_id, conversation_id, seq, author, user_message)"
                         " VALUES (?, ?, 0, 'researcher', '{\"text\": \"hi\"}')", (run_id, conversation))
        await asyncio.to_thread(client.state["db"].write, seed)
    async with started(data, keyring=keyring, setup=False) as client:
        [turn] = (await client.get(f"/api/conversations/{conversation}")).json()["turns"]
        assert (turn["status"], turn["cancel_reason"]) == ("cancelled", "revoked")
        assert (await client.post(f"/api/runs/{run_id}/continue")).status_code == 200  # its conversation remains
        await background_idle(client)


async def test_an_indexing_style_run_with_no_conversation_dispatches_and_its_projects_deletion_revokes_it(tmp_path):
    async with started(tmp_path / "data") as client:
        project = await new_project(client)
        db, gate = client.state["db"], client.state["gate"]
        run_id = new_id()
        await asyncio.to_thread(db.write, lambda conn: conn.execute(
            "INSERT INTO runs (id, project_id, kind, workflow) VALUES (?, ?, 'background', 'index')", (run_id, project)))
        check = lambda conn: may_dispatch(conn, run_id)  # noqa: E731
        assert await asyncio.to_thread(db.read, check) is True  # no conversation, no source turn: no deletion
        async with gate.async_client(project, admit=check) as http:
            assert (await http.get("https://openrouter.ai/api/v1/models")).status_code == 200
        assert (await client.delete(f"/api/projects/{project}")).status_code == 200
        assert await asyncio.to_thread(db.read, check) is False


async def test_the_dispatch_check_follows_each_kind_of_runs_owners(tmp_path):
    async with started(tmp_path / "data") as client:
        project = await new_project(client)
        conversation = await new_conversation(client, project)
        await send(client, conversation)
        await background_idle(client)
        db = client.state["db"]
        [(turn,)] = await rows(client, "SELECT id FROM runs WHERE kind = 'turn'")
        ids = {name: new_id() for name in ("turn", "detached", "orphan_turn", "missing")}

        def seed(conn):
            conn.execute("INSERT INTO runs (id, project_id, conversation_id, kind) VALUES (?, ?, ?, 'turn')",
                         (ids["turn"], project, conversation))
            conn.execute("INSERT INTO runs (id, project_id, kind, workflow, source_turn_id)"
                         " VALUES (?, ?, 'background', 'title', ?)", (ids["detached"], project, turn))
            conn.execute("INSERT INTO runs (id, project_id, kind) VALUES (?, ?, 'turn')", (ids["orphan_turn"], project))
        await asyncio.to_thread(db.write, seed)
        checks = lambda conn: {name: may_dispatch(conn, run) for name, run in ids.items()}  # noqa: E731
        assert await asyncio.to_thread(db.read, checks) == {
            "turn": True, "detached": True, "orphan_turn": False, "missing": False}  # a turn needs its conversation
        await asyncio.to_thread(db.write, lambda conn: conn.execute(
            "UPDATE projects SET review_lock = 1, sensitivity = 'local_only' WHERE id = ?", (project,)))
        assert set((await asyncio.to_thread(db.read, checks)).values()) == {False}  # nothing in a locked project


async def test_the_listing_marks_what_local_only_and_locked_projects_allow(tmp_path):
    provider = MockProvider(catalog=["example/cloud-model"])
    async with started(tmp_path / "data", provider) as client:
        local_only = await new_project(client, "local_only")
        locked = (await client.post("/api/projects", json={"name": "R", "sensitivity": "local_only",
                                                           "review_lock": True})).json()["id"]
        for project, refusal in ((local_only, "route_not_allowed"), (locked, "review_locked")):
            listing = (await client.get("/api/providers/openrouter/models", params={"project_id": project})).json()
            assert [(m["allowed"], m["refusal"]) for m in listing["models"]] == [(False, refusal)]
        assert (await client.get("/api/providers/openrouter/models", params={"project_id": new_id()})).status_code == 404


async def test_a_level_changed_while_a_turn_is_admitted_refuses_it_before_anything_is_written(tmp_path, monkeypatch):
    from backend import runs
    provider = MockProvider()
    async with started(tmp_path / "data", provider) as client:
        project = await new_project(client)
        conversation = await new_conversation(client, project)
        db = client.state["db"]
        real = runs.load_instructions

        def tightened_meanwhile(*args):  # after admission read the project's policy, before it writes
            db.write(lambda conn: conn.execute("UPDATE projects SET sensitivity = 'private' WHERE id = ?", (project,)))
            return real(*args)

        monkeypatch.setattr(runs, "load_instructions", tightened_meanwhile)
        refused = await client.post(f"/api/conversations/{conversation}/message/stream", json={"content": "hi"})
        assert (refused.status_code, refused.json()["code"]) == (409, "project_changed")
        assert provider.chats == [] and await rows(client, "SELECT count(*) FROM runs") == [(0,)]
