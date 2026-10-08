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
import time

import pytest

from backend import governance, openrouter, openrouter_client
from backend.db import new_id
from backend.runs import may_dispatch
from scholia_app import FakeKeyring, MockProvider, background_idle, confirm_key, declare, events, send, started

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
        assert (await confirm_key(client)).status_code == 200
        continued = events(await client.post(f"/api/runs/{revoked}/continue", json={"model": ZDR_MODEL}))
        assert continued[-1]["status"] == "succeeded"
        assert provider.answers[-1]["provider"] == {"zdr": True, "only": ["example"]}
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


# A revocation between the gate's decision and the request's entry into the transport


def revocation_requests(client, project, conversation):
    return {
        "tighten": lambda: set_level(client, project, "private"),
        "lock": lambda: client.post(f"/api/projects/{project}/review-lock", json={"locked": True}),
        "delete": lambda: client.delete(f"/api/conversations/{conversation}"),
    }


def recording(monkeypatch, provider, order):
    """Record when a revocation writes (in its transaction, just before it commits) and when a
    chat request enters the transport (the provider's handler, called as the transport is entered)."""
    from backend.db import deletion
    real_revoke_running, real_revoke = governance.revoke_running, deletion._revoke
    monkeypatch.setattr(governance, "revoke_running",
                        lambda *args: order.append("revoked") or real_revoke_running(*args))
    monkeypatch.setattr(deletion, "_revoke", lambda conn: order.append("revoked") or real_revoke(conn))

    async def entered(request):  # the transport's handler, in front of the provider
        if request.url.path.endswith("/chat/completions"):
            order.append("entered")
        return await provider(request)

    return entered


@pytest.mark.parametrize("change", ["tighten", "lock", "delete"])
async def test_a_revocation_committed_after_a_decision_means_the_request_is_decided_again(tmp_path, monkeypatch,
                                                                                         change):
    # The exact interleaving: the gate has decided to allow the request; before the request enters
    # the transport, a tightening, a lock or a deletion revokes the run and commits, this process
    # hears of it only later, and the loop is held up just before the request would enter. The
    # request never enters the transport after the revocation commits: it is decided again, and
    # refused.
    from backend import outbound_gate
    from backend.runs import Harness
    order = []
    provider = MockProvider()
    async with started(tmp_path / "data", recording(monkeypatch, provider, order)) as client:
        project = await new_project(client)
        conversation = await new_conversation(client, project)
        loop, db = asyncio.get_running_loop(), client.state["db"]
        real_check, real_dispatched = outbound_gate.OutboundGate._check, outbound_gate._dispatched
        real_harness_revoke = Harness.revoke
        request = revocation_requests(client, project, conversation)[change]

        def committed():
            row = db.read(lambda conn: conn.execute("SELECT cancel_reason FROM runs WHERE kind = 'turn'").fetchone())
            return row is None or row[0] == "revoked"

        def check_then_revoke(self, request_, scope, *args):
            decided = real_check(self, request_, scope, *args)
            if scope.project_id == project and "revoked" not in order:  # the turn's call, decided and allowed
                asyncio.run_coroutine_threadsafe(request(), loop)
                deadline = time.monotonic() + 1
                while not committed() and time.monotonic() < deadline:
                    time.sleep(0.01)
            return decided

        def dispatched(*args):  # the last moment before the request enters: the loop is held up
            loop.call_soon(time.sleep, 0.3)
            real_dispatched(*args)

        monkeypatch.setattr(outbound_gate.OutboundGate, "_check", check_then_revoke)
        monkeypatch.setattr(outbound_gate, "_dispatched", dispatched)
        monkeypatch.setattr(Harness, "revoke", lambda self, ids: loop.call_later(0.3, real_harness_revoke, self, ids))
        response = await client.post(f"/api/conversations/{conversation}/message/stream", json={"content": "hi"})
        await wait_for(lambda: "revoked" in order)
        await asyncio.sleep(0.4)
        await background_idle(client)
        assert "entered" not in order[order.index("revoked"):]  # never entered after the revocation
        assert "entered" not in order and provider.chats == []
        decisions = await rows(client, "SELECT data ->> 'decision' FROM audit_log WHERE event = 'outbound'"
                                       " AND project_id = ? ORDER BY seq", project)
        assert decisions == [("allow",), ("deny",)]  # decided again after the revocation, and refused
        if change != "delete":
            assert events(response)[-1]["cancel_reason"] == "revoked"


@pytest.mark.parametrize("change", ["tighten", "lock", "delete"])
async def test_a_revocation_that_begins_as_a_request_enters_the_transport_finds_it_entered(tmp_path, monkeypatch,
                                                                                          change):
    # The other side of the boundary: the revocation is asked for at the last moment before the
    # request enters the transport, and the loop is then held up while it commits. The request
    # entered in the same step as its last check, so it is the in-flight case Stop handles; it
    # never enters after the revocation commits.
    from backend import outbound_gate
    from backend.runs import Harness
    order = []
    provider = MockProvider()
    async with started(tmp_path / "data", recording(monkeypatch, provider, order)) as client:
        project = await new_project(client)
        conversation = await new_conversation(client, project)
        loop = asyncio.get_running_loop()
        real_dispatched, real_harness_revoke = outbound_gate._dispatched, Harness.revoke
        request = revocation_requests(client, project, conversation)[change]

        def dispatched(*args):
            if not order:
                loop.create_task(request())
                loop.call_soon(time.sleep, 0.3)  # the loop is held up while the revocation commits
            real_dispatched(*args)

        monkeypatch.setattr(outbound_gate, "_dispatched", dispatched)
        monkeypatch.setattr(Harness, "revoke", lambda self, ids: loop.call_later(0.3, real_harness_revoke, self, ids))
        await client.post(f"/api/conversations/{conversation}/message/stream", json={"content": "hi"})
        await wait_for(lambda: "revoked" in order)
        await asyncio.sleep(0.4)
        await background_idle(client)
        assert order[:2] == ["entered", "revoked"]


async def test_a_revocation_elsewhere_in_the_project_has_the_request_decided_again_and_sent(tmp_path, monkeypatch):
    from backend import outbound_gate
    order = []
    provider = MockProvider()
    async with started(tmp_path / "data", recording(monkeypatch, provider, order)) as client:
        project = await new_project(client)
        conversation = await new_conversation(client, project)
        other = await new_conversation(client, project)
        loop = asyncio.get_running_loop()
        real_check = outbound_gate.OutboundGate._check

        def check_then_delete_other(self, request_, scope, *args):
            decided = real_check(self, request_, scope, *args)
            if scope.project_id == project and "revoked" not in order:
                asyncio.run_coroutine_threadsafe(client.delete(f"/api/conversations/{other}"), loop).result(timeout=2)
            return decided

        monkeypatch.setattr(outbound_gate.OutboundGate, "_check", check_then_delete_other)
        stream = await send(client, conversation)
        assert stream[-1]["status"] == "succeeded" and len(provider.answers) == 1
        await background_idle(client)
        decisions = await rows(client, "SELECT data ->> 'decision' FROM audit_log WHERE event = 'outbound'"
                                       " AND project_id = ? ORDER BY seq LIMIT 2", project)
        assert decisions == [("allow",), ("allow",)]  # decided again once the deletion settled


async def test_revocations_and_dispatches_never_wait_for_each_others_workers(tmp_path):
    # With one worker for the loop's threads, a turn's request, a tightening of another project
    # and deletions all finish: no revocation holds a worker while waiting for a dispatch that
    # needs one, nor the other way round.
    from concurrent.futures import ThreadPoolExecutor
    provider = MockProvider()
    async with started(tmp_path / "data", provider) as client:
        loop = asyncio.get_running_loop()
        loop.set_default_executor(ThreadPoolExecutor(1))
        project, elsewhere = await new_project(client), await new_project(client)
        conversation = await new_conversation(client, project)
        others = [await new_conversation(client, project) for _ in range(3)]
        work = [send(client, conversation), set_level(client, elsewhere, "private"),
                *(client.delete(f"/api/conversations/{other}") for other in others)]
        done = await asyncio.wait_for(asyncio.gather(*work), timeout=10)
        assert done[0][-1]["status"] == "succeeded"
        await background_idle(client)


async def test_a_deletion_orders_the_requests_of_whatever_project_its_records_belong_to(tmp_path, monkeypatch):
    # The conversation being deleted moves from project A to project B after the deletion began,
    # a turn in B is decided before the deletion commits, and this process hears of the deletion
    # only later: the request never enters the transport after the deletion.
    import contextlib
    import threading
    from backend import outbound_gate
    from backend.db import deletion
    from backend.runs import Harness
    order = []
    provider = MockProvider()
    async with started(tmp_path / "data", recording(monkeypatch, provider, order)) as client:
        a, b = await new_project(client), await new_project(client)
        conversation = await new_conversation(client, a)
        loop, db, gate = asyncio.get_running_loop(), client.state["db"], client.state["gate"]
        real_check, decided, sent = outbound_gate.OutboundGate._check, threading.Event(), {}

        def gone():
            return db.read(lambda conn: conn.execute("SELECT count(*) FROM conversations WHERE id = ?",
                                                     (conversation,)).fetchone()) == (0,)

        @contextlib.contextmanager
        def barrier_then_move(*args):
            with gate.revoking_from_thread(*args):
                async def move_and_send():
                    await client.post(f"/api/conversations/{conversation}/move", json={"project_id": b})
                    sent["stream"] = asyncio.ensure_future(client.post(
                        f"/api/conversations/{conversation}/message/stream", json={"content": "hi"}))
                asyncio.run_coroutine_threadsafe(move_and_send(), loop).result(timeout=2)
                decided.wait(1)  # B's request decided before the deletion writes, when it may be
                yield

        def check_then_wait(self, request_, scope, *args):
            found = real_check(self, request_, scope, *args)
            if scope.project_id == b:
                decided.set()
                deadline = time.monotonic() + 1
                while not gone() and time.monotonic() < deadline:  # the deletion commits meanwhile
                    time.sleep(0.01)
            return found

        real_harness_revoke = Harness.revoke
        monkeypatch.setitem(deletion.REVOKING, db, barrier_then_move)
        monkeypatch.setattr(outbound_gate.OutboundGate, "_check", check_then_wait)
        monkeypatch.setattr(Harness, "revoke", lambda self, ids: loop.call_later(0.3, real_harness_revoke, self, ids))
        assert (await client.delete(f"/api/conversations/{conversation}")).status_code == 200
        await sent["stream"]
        await asyncio.sleep(0.4)
        await background_idle(client)
        assert "revoked" in order and "entered" not in order[order.index("revoked"):]
        assert provider.chats == []


@pytest.mark.parametrize("change", ["withdraw", "disable"])
async def test_a_route_taken_away_between_a_decision_and_entry_means_the_request_is_decided_again(
        tmp_path, monkeypatch, change):
    # A declaration withdrawn, or the allowlist entry turned off, after a Private project's request
    # was decided and before it entered the transport: it is decided again, and refused.
    from backend import outbound_gate, openrouter_client
    openrouter_client.clear_cache()
    order = []
    provider = MockProvider(catalog=["example/zdr-model"], zero_retention=["example/zdr-model"])
    async with started(tmp_path / "data", recording(monkeypatch, provider, order)) as client:
        current = (await client.get("/api/settings")).json()
        await client.put("/api/settings", json={"hash": current["hash"], "updates": {
            "providers.local.kind": "openai-compatible", "providers.local.base_url": LOCAL,
            "providers.local.models": "all", "providers.openrouter.models": "all"}})
        await client.put("/api/keys/local", json={"key": "local"})
        assert (await declare(client, "local")).status_code == 200
        assert (await confirm_key(client)).status_code == 200
        project = await new_project(client, "private")
        conversation = await new_conversation(client, project)
        loop, db = asyncio.get_running_loop(), client.state["db"]
        real_check = outbound_gate.OutboundGate._check
        take_away = {
            "withdraw": (lambda: client.delete("/api/local-declarations/local"),
                         lambda conn: conn.execute("SELECT count(*) FROM local_declarations").fetchone() == (0,)),
            "disable": (lambda: client.put("/api/private-routes/openrouter:*", json={"enabled": False}),
                        lambda conn: conn.execute("SELECT count(*) FROM private_routes").fetchone() == (1,)),
        }[change]

        def check_then_take_away(self, request_, scope, *args):
            found = real_check(self, request_, scope, *args)
            if scope.project_id == project and "taken away" not in order:
                asyncio.run_coroutine_threadsafe(take_away[0](), loop)
                deadline = time.monotonic() + 1
                while not db.read(take_away[1]) and time.monotonic() < deadline:
                    time.sleep(0.01)
                order.append("taken away")
            return found

        monkeypatch.setattr(outbound_gate.OutboundGate, "_check", check_then_take_away)
        route = {"model": "llama", "provider": "local"} if change == "withdraw" else {"model": "example/zdr-model"}
        stream = await send(client, conversation, **route)
        await background_idle(client)
        assert order[0] == "taken away" and "entered" not in order and provider.chats == []
        reasons = await rows(client, "SELECT data ->> 'decision', data ->> 'reason' FROM audit_log"
                                     " WHERE event = 'outbound' AND project_id = ? ORDER BY seq", project)
        assert reasons == [("allow", None), ("deny", "not_declared" if change == "withdraw" else "route_not_allowed")]
        assert stream[-1]["status"] == "failed"


# Every write that can take a route away is ordered with dispatch, whatever happens to its request


async def private_setup(client, *, key_owner="openrouter"):
    """A Private project and conversation that may use example/zdr-model on OpenRouter, with the
    key confirmed through key_owner's card."""
    current = (await client.get("/api/settings")).json()
    await client.put("/api/settings", json={"hash": current["hash"], "updates": {"providers.openrouter.models": "all"}})
    assert (await confirm_key(client, key_owner)).status_code == 200
    project = await new_project(client, "private")
    return project, await new_conversation(client, project)


def zdr_provider():
    return MockProvider(catalog=["example/zdr-model"], zero_retention=["example/zdr-model"])


async def test_a_cancelled_allowlist_change_keeps_its_mark_until_its_write_has_finished(tmp_path, monkeypatch):
    # The request turning the allowlist entry off is cancelled while its write has not reached
    # the database. Its mark stays until the write has finished, so a Private request decided
    # meanwhile is decided again after it, and refused; it never enters the transport after.
    import threading
    from backend import openrouter_client, outbound_gate
    openrouter_client.clear_cache()
    order, provider = [], zdr_provider()
    async with started(tmp_path / "data", recording(monkeypatch, provider, order)) as client:
        project, conversation = await private_setup(client)
        db, loop = client.state["db"], asyncio.get_running_loop()
        real_write, real_check = db.write, outbound_gate.OutboundGate._check
        started_, go = threading.Event(), threading.Event()

        def slow_write(fn):  # the allowlist write waits before it reaches the database
            if fn.__qualname__.endswith("change_private_route.<locals>.change"):
                started_.set()
                go.wait(3)
            return real_write(fn)

        def disabled(conn):
            return conn.execute("SELECT count(*) FROM private_routes WHERE enabled = 0").fetchone() == (1,)

        def check_then_let_it_go(self, request_, scope, *args):
            found = real_check(self, request_, scope, *args)
            if scope.project_id == project:
                go.set()
                deadline = time.monotonic() + 1
                while not real_write(disabled) and time.monotonic() < deadline:  # it commits meanwhile
                    time.sleep(0.01)
            return found

        monkeypatch.setattr(db, "write", slow_write)
        monkeypatch.setattr(outbound_gate.OutboundGate, "_check", check_then_let_it_go)
        change = asyncio.create_task(client.put("/api/private-routes/openrouter:*", json={"enabled": False}))
        await asyncio.to_thread(started_.wait, 3)
        change.cancel()
        await asyncio.sleep(0.05)  # the cancellation has reached the request
        threading.Timer(0.5, go.set).start()  # the write goes on after a while in any case
        stream = await send(client, conversation, model="example/zdr-model")
        with pytest.raises(asyncio.CancelledError):
            await change
        await background_idle(client)
        assert "entered" not in order and provider.chats == []
        assert stream[-1]["status"] == "failed"
        assert await asyncio.to_thread(real_write, disabled)


async def test_a_key_changed_through_another_name_is_ordered_with_dispatch_and_ends_only_its_confirmation(
        tmp_path, monkeypatch):
    # Two names for the same OpenRouter key, each with its own confirmation. Changing the other
    # name's key between the first name's decision and its entry into the transport is ordered
    # with it: the request is decided again. Its own confirmation stands; the change forgot every
    # catalog, so it is refused for its route until the catalog is read again, not for its key.
    from backend import openrouter_client, outbound_gate
    from scholia_app import KEY
    openrouter_client.clear_cache()
    order, provider = [], zdr_provider()
    async with started(tmp_path / "data", recording(monkeypatch, provider, order)) as client:
        current = (await client.get("/api/settings")).json()
        await client.put("/api/settings", json={"hash": current["hash"], "updates": {
            "providers.work.kind": "openrouter", "providers.work.base_url": "https://openrouter.ai/api/v1",
            "providers.work.models": "all"}})
        assert (await client.put("/api/keys/work", json={"key": KEY})).status_code == 200  # the same key
        assert (await confirm_key(client, "work")).status_code == 200
        project, conversation = await private_setup(client)
        loop, db = asyncio.get_running_loop(), client.state["db"]
        real_check = outbound_gate.OutboundGate._check

        def check_then_change_the_other_key(self, request_, scope, *args):
            found = real_check(self, request_, scope, *args)
            if scope.project_id == project and "changed" not in order:
                order.append("changed")
                asyncio.run_coroutine_threadsafe(client.put("/api/keys/work", json={"key": "sk-or-another"}), loop)
                deadline = time.monotonic() + 1
                while db.read(lambda conn: conn.execute("SELECT count(*) FROM key_attestations").fetchone()) != (1,) \
                        and time.monotonic() < deadline:
                    time.sleep(0.01)
            return found

        monkeypatch.setattr(outbound_gate.OutboundGate, "_check", check_then_change_the_other_key)
        stream = await send(client, conversation, model="example/zdr-model", provider="openrouter")
        await background_idle(client)
        assert order == ["changed"] and provider.chats == [] and stream[-1]["status"] == "failed"
        reasons = await rows(client, "SELECT data ->> 'reason' FROM audit_log WHERE event = 'outbound'"
                                     " AND project_id = ? ORDER BY seq", project)
        assert reasons == [(None,), ("route_not_allowed",)]
        assert await rows(client, "SELECT provider FROM key_attestations") == [("openrouter",)]


async def test_a_confirmation_that_lapses_between_decision_and_entry_is_honoured(tmp_path, monkeypatch):
    from backend import openrouter_client, outbound_gate
    from datetime import UTC, datetime, timedelta
    from backend.db import utc_now
    openrouter_client.clear_cache()
    order, provider = [], zdr_provider()
    async with started(tmp_path / "data", recording(monkeypatch, provider, order)) as client:
        project, conversation = await private_setup(client)
        db = client.state["db"]
        lapses = (datetime.now(UTC) + timedelta(seconds=1.5)).isoformat(timespec="milliseconds").replace("+00:00", "Z")
        await asyncio.to_thread(db.write, lambda conn: conn.execute("UPDATE key_attestations SET expires_at = ?",
                                                                    (lapses,)))
        real_check = outbound_gate.OutboundGate._check

        def check_then_wait_past_it(self, request_, scope, *args):
            found = real_check(self, request_, scope, *args)
            if scope.project_id == project and "decided" not in order:
                order.append("decided")
                while utc_now() <= lapses:  # the confirmation lapses before the request would enter
                    time.sleep(0.02)
            return found

        monkeypatch.setattr(outbound_gate.OutboundGate, "_check", check_then_wait_past_it)
        stream = await send(client, conversation, model="example/zdr-model")
        await background_idle(client)
        assert order == ["decided"] and provider.chats == []
        reasons = await rows(client, "SELECT data ->> 'reason' FROM audit_log WHERE event = 'outbound'"
                                     " AND project_id = ? ORDER BY seq", project)
        assert reasons == [(None,), ("key_not_confirmed",)] and stream[-1]["status"] == "failed"


@pytest.mark.parametrize("change", [{"providers.openrouter.enabled": False},
                                    {"providers.openrouter.base_url": "https://api.moved.example/v1"}])
async def test_a_provider_turned_off_or_moved_between_a_catalog_decision_and_entry_is_honoured(
        tmp_path, monkeypatch, change):
    from backend import openrouter_client, outbound_gate
    openrouter_client.clear_cache()
    order, provider = [], zdr_provider()

    async def handler(request):  # in front of the provider: when a catalog page enters the transport
        if request.url.path.endswith("/models"):
            order.append("entered")
        return await provider(request)

    async with started(tmp_path / "data", handler) as client:
        openrouter_client.clear_cache()
        loop = asyncio.get_running_loop()
        real_check = outbound_gate.OutboundGate._check

        async def turn_off():
            current = (await client.get("/api/settings")).json()
            return await client.put("/api/settings", json={"hash": current["hash"], "updates": change})

        def check_then_turn_off(self, request_, scope, *args):
            found = real_check(self, request_, scope, *args)
            if request_.url.path.endswith("/models") and "changed" not in order:
                order.append("changed")
                assert asyncio.run_coroutine_threadsafe(turn_off(), loop).result(timeout=2).status_code == 200
            return found

        monkeypatch.setattr(outbound_gate.OutboundGate, "_check", check_then_turn_off)
        listing = await client.get("/api/providers/openrouter/models")
        assert order == ["changed"]  # the catalog page never entered the transport after the change
        assert listing.status_code in (404, 409) or listing.json()["status"]["error"] == "refresh_failed"


async def test_a_catalog_read_ends_at_its_deadline_under_a_revocation_that_does_not_end(tmp_path, monkeypatch):
    from backend import openrouter_client
    openrouter_client.clear_cache()
    monkeypatch.setattr(openrouter_client, "CATALOG_SECONDS", 0.5)
    async with started(tmp_path / "data") as client:
        openrouter_client.clear_cache()
        gate = client.state["gate"]
        gate._begin(None)  # a revocation of every project under way, for longer than the deadline
        try:
            listing = await asyncio.wait_for(client.get("/api/providers/openrouter/models"), timeout=5)
        finally:
            gate._end(None)
        assert listing.json()["status"]["error"] == "refresh_failed" and listing.json()["models"] == []


# Across a restore: what a request began on the app a restore replaced ends there


async def test_a_cancelled_provider_save_finishes_before_a_restore_puts_its_settings_in_place(tmp_path, monkeypatch):
    # A save turning a provider off is paused after its digest check, before the file is replaced,
    # and its request cancelled. The restore that follows waits for it: the restored config.toml
    # is the backup's, never overwritten by the save after it.
    import threading
    from backend import settings as settings_module
    async with started(tmp_path / "data") as client:
        backup = (await client.post("/api/backups")).json()["id"]  # OpenRouter on
        current = (await client.get("/api/settings")).json()
        real, paused, go, written = settings_module.write_private, *(threading.Event() for _ in range(3))

        def write_after_a_pause(path, data):
            if path.name == "config.toml" and not go.is_set():
                paused.set()
                go.wait(10)
                real(path, data)
                written.set()
                return None
            return real(path, data)

        monkeypatch.setattr(settings_module, "write_private", write_after_a_pause)
        save = asyncio.create_task(client.put("/api/settings", json={
            "hash": current["hash"], "updates": {"providers.openrouter.enabled": False}}))
        assert await asyncio.to_thread(paused.wait, 5)
        save.cancel()
        await asyncio.sleep(0.05)  # the cancellation has reached the request
        restore = asyncio.create_task(client.post("/api/backups/restore", json={"generation": backup}))
        await wait_for(lambda: restore.done() or client.state["writers"]._waiting)  # done, or waiting for the save
        go.set()
        assert (await restore).status_code == 200
        with pytest.raises(asyncio.CancelledError):
            await save
        assert await asyncio.to_thread(written.wait, 5)  # the save's write is over, whenever it ran
        values = (await client.get("/api/settings")).json()["values"]
        assert values["providers"]["openrouter"].get("enabled") is not False  # the backup's settings stand


async def test_a_catalog_request_decided_before_a_restore_never_enters_the_transport_after_it(tmp_path, monkeypatch):
    # A catalog refresh is decided; its caller is cancelled, which leaves the refresh running and
    # lets a restore through. The restore stops the app the decision was made on: the decision is
    # never acted on afterwards, so the request never enters the transport after the swap.
    import threading
    from backend import outbound_gate
    order, provider = [], MockProvider(catalog=["example/model"])

    async def handler(request):  # in front of the provider: when a catalog page enters the transport
        if request.url.path.endswith("/models"):
            order.append("entered")
        return await provider(request)

    async with started(tmp_path / "data", handler) as client:
        backup = (await client.post("/api/backups")).json()["id"]
        openrouter_client.clear_cache()
        real_check, decided, go = outbound_gate.OutboundGate._check, threading.Event(), threading.Event()

        def check_then_wait(self, request_, scope, *args):
            found = real_check(self, request_, scope, *args)
            if request_.url.path.endswith("/models") and not go.is_set():
                decided.set()
                go.wait(10)  # its continuation on the loop comes after the restore
            return found

        monkeypatch.setattr(outbound_gate.OutboundGate, "_check", check_then_wait)
        listing = asyncio.create_task(client.get("/api/providers/openrouter/models"))
        assert await asyncio.to_thread(decided.wait, 5)
        [refresh] = [state["task"] for state in openrouter_client._caches.values() if state["task"] is not None]
        listing.cancel()  # the refresh goes on, shielded
        with pytest.raises(asyncio.CancelledError):
            await listing
        assert (await client.post("/api/backups/restore", json={"generation": backup})).status_code == 200
        go.set()
        await asyncio.wait({refresh}, timeout=5)
        assert refresh.done() and order == []
