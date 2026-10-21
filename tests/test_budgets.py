"""Budget reservations and settlement (backend/spending.py) with the deletion service.

The budget bar, notices and the asks at the cap come with the budgets PR; these
tests cover admission, settling once, and spending that outlives deletion.
"""

import asyncio
import sqlite3
import threading

import pytest

from backend import spending
from backend.db import ContentStore, Database, delete, new_id


@pytest.fixture
def db(tmp_path):
    with Database(tmp_path / "data") as database:
        yield database


def add_project(db, name="Thesis"):
    project_id = new_id()
    db.write(lambda conn: conn.execute(
        "INSERT INTO projects (id, name, kind) VALUES (?, ?, 'research')", (project_id, name)))
    return project_id


def add_turn(db, project_id, conversation_id=None):
    """A conversation (new unless given) with one running turn. Returns (conversation, run)."""
    conversation_id = conversation_id or new_id()
    run_id = new_id()

    def write(conn):
        conn.execute("INSERT OR IGNORE INTO conversations (id, project_id) VALUES (?, ?)", (conversation_id, project_id))
        (seq,) = conn.execute("SELECT count(*) FROM turns WHERE conversation_id = ?", (conversation_id,)).fetchone()
        conn.execute("INSERT INTO runs (id, project_id, conversation_id, kind) VALUES (?, ?, ?, 'turn')",
                     (run_id, project_id, conversation_id))
        conn.execute("INSERT INTO turns (run_id, conversation_id, seq, author, user_message) VALUES (?, ?, ?, 'researcher', '{}')",
                     (run_id, conversation_id, seq))

    db.write(write)
    return conversation_id, run_id


def reserve(db, project_id, conversation_id, run_id, estimate, *, project_budget=50.0, conversation_budget=10.0, step=0):
    return db.write(lambda conn: spending.reserve(
        conn, run_id=run_id, step_seq=step, project_id=project_id, paying_conversation_id=conversation_id,
        estimate_usd=estimate, project_budget_usd=project_budget, conversation_budget_usd=conversation_budget))


def row(db, reservation_id):
    return db.read(lambda conn: conn.execute(
        "SELECT run_id, paying_conversation_id, status, settled_usd, basis FROM budget_reservations WHERE id = ?",
        (reservation_id,)).fetchone())


def project_spent(db, project_id):
    return db.read(lambda conn: spending.spent(conn, project_id=project_id))


# Admission


def test_a_reservation_that_fits_both_budgets_is_admitted_and_counts_at_once(db):
    project = add_project(db)
    conversation, run = add_turn(db, project)
    reservation = reserve(db, project, conversation, run, 0.25)
    assert row(db, reservation) == (run, conversation, "open", None, None)
    assert project_spent(db, project) == 0.25
    assert db.read(lambda conn: spending.spent(conn, conversation_id=conversation)) == 0.25


@pytest.mark.parametrize("budgets, refused_by", [
    ({"project_budget": 1.0, "conversation_budget": 10.0}, "project"),
    ({"project_budget": 50.0, "conversation_budget": 1.0}, "conversation"),
])
def test_a_reservation_that_does_not_fit_is_refused_and_writes_nothing(db, budgets, refused_by):
    project = add_project(db)
    conversation, run = add_turn(db, project)
    reserve(db, project, conversation, run, 0.6, **budgets)
    with pytest.raises(spending.BudgetExceeded) as refused:
        reserve(db, project, conversation, run, 0.6, step=1, **budgets)
    assert refused.value.budget == refused_by
    assert db.read(lambda conn: conn.execute("SELECT count(*) FROM budget_reservations").fetchone()) == (1,)


def test_two_conversations_admitting_075_against_1_left_admit_exactly_one(db):
    """Two steps race for the last dollar of the project's budget: the single writer
    admits one and refuses the other, whatever the order."""
    project = add_project(db)
    first, run_a = add_turn(db, project)
    second, run_b = add_turn(db, project)
    earlier, earlier_run = add_turn(db, project)
    reserve(db, project, earlier, earlier_run, 49.0, conversation_budget=None)  # $1 left of the project's $50
    results, barrier = [], threading.Barrier(2)

    def admit(conversation, run):
        barrier.wait()
        try:
            reserve(db, project, conversation, run, 0.75, step=1)
            results.append("admitted")
        except spending.BudgetExceeded:
            results.append("refused")

    threads = [threading.Thread(target=admit, args=args) for args in ((first, run_a), (second, run_b))]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert sorted(results) == ["admitted", "refused"]
    assert project_spent(db, project) == 49.75


def test_spend_counts_settled_and_open_but_not_released(db):
    project = add_project(db)
    conversation, run = add_turn(db, project)
    settled, opened, released = (reserve(db, project, conversation, run, 1.0, step=n) for n in range(3))
    db.write(lambda conn: spending.settle(conn, settled, 0.4))
    db.write(lambda conn: spending.release(conn, released))
    assert project_spent(db, project) == 1.4  # 0.4 settled + 1.0 still open
    assert db.read(lambda conn: spending.run_cost(conn, run)) == 1.4


@pytest.mark.parametrize("estimate", [-0.01, float("nan"), float("inf"), None, "0.1", True])
def test_an_invalid_estimate_is_refused(db, estimate):
    project = add_project(db)
    conversation, run = add_turn(db, project)
    with pytest.raises(ValueError):
        reserve(db, project, conversation, run, estimate)


# Settling once


def test_a_reported_cost_settles_once_and_a_late_or_duplicate_report_changes_nothing(db):
    project = add_project(db)
    conversation, run = add_turn(db, project)
    reservation = reserve(db, project, conversation, run, 0.5)
    assert db.write(lambda conn: spending.settle(conn, reservation, 0.123)) is True
    assert db.write(lambda conn: spending.settle(conn, reservation, 0.123)) is False  # the same report again
    assert db.write(lambda conn: spending.settle(conn, reservation, 9.0)) is False  # a later, different report
    assert db.write(lambda conn: spending.release(conn, reservation)) is False
    assert row(db, reservation)[2:] == ("settled", 0.123, "reported")


@pytest.mark.parametrize("report", [None, -1, float("nan"), float("inf"), "0.2", False])
def test_a_call_without_valid_usage_settles_at_its_estimate_never_zero(db, report):
    project = add_project(db)
    conversation, run = add_turn(db, project)
    reservation = reserve(db, project, conversation, run, 0.5)
    db.write(lambda conn: spending.settle(conn, reservation, report))
    assert row(db, reservation)[2:] == ("settled", 0.5, "estimated")


def test_reported_tokens_without_a_cost_settle_at_their_estimate(db):
    project = add_project(db)
    conversation, run = add_turn(db, project)
    reservation = reserve(db, project, conversation, run, 0.5)
    db.write(lambda conn: spending.settle(conn, reservation, None, estimated_usd=0.0042))
    assert row(db, reservation)[2:] == ("settled", 0.0042, "estimated")


def test_a_reported_zero_is_a_valid_cost(db):
    project = add_project(db)
    conversation, run = add_turn(db, project)
    reservation = reserve(db, project, conversation, run, 0.5)
    db.write(lambda conn: spending.settle(conn, reservation, 0))
    assert row(db, reservation)[2:] == ("settled", 0.0, "reported")


def test_an_undispatched_reservation_is_released_and_cannot_settle_later(db):
    project = add_project(db)
    conversation, run = add_turn(db, project)
    reservation = reserve(db, project, conversation, run, 0.5)
    assert db.write(lambda conn: spending.release(conn, reservation)) is True
    assert db.write(lambda conn: spending.settle(conn, reservation, 0.2)) is False
    assert row(db, reservation)[2:] == ("released", None, None)
    assert project_spent(db, project) == 0


@pytest.mark.parametrize("change", [
    "status = 'open', settled_at = NULL, settled_usd = NULL, basis = NULL",
    "settled_usd = 0.01",
    "basis = 'estimated'",
    "estimate_usd = 0",
    "project_id = (SELECT id FROM projects WHERE kind = 'general')",
])
def test_the_database_refuses_any_change_to_a_settled_reservation(db, change):
    project = add_project(db)
    conversation, run = add_turn(db, project)
    reservation = reserve(db, project, conversation, run, 0.5)
    db.write(lambda conn: spending.settle(conn, reservation, 0.3))
    with pytest.raises(sqlite3.IntegrityError, match="never changes"):
        db.write(lambda conn: conn.execute(f"UPDATE budget_reservations SET {change} WHERE id = ?", (reservation,)))
    assert row(db, reservation)[2:] == ("settled", 0.3, "reported")


def test_a_settled_row_must_carry_its_amount_and_basis(db):
    project = add_project(db)
    conversation, run = add_turn(db, project)
    reservation = reserve(db, project, conversation, run, 0.5)
    with pytest.raises(sqlite3.IntegrityError, match="CHECK"):
        db.write(lambda conn: conn.execute(
            "UPDATE budget_reservations SET status = 'settled', settled_at = '2026-10-02T00:00:00.000Z' WHERE id = ?",
            (reservation,)))


def test_reservations_left_open_by_a_crash_settle_once_at_their_estimate(db):
    project = add_project(db)
    conversation, run = add_turn(db, project)
    left_open = reserve(db, project, conversation, run, 0.5)
    done = reserve(db, project, conversation, run, 0.4, step=1)
    db.write(lambda conn: spending.settle(conn, done, 0.1))
    assert db.write(spending.settle_left_open) == 1
    assert db.write(spending.settle_left_open) == 0
    assert row(db, left_open)[2:] == ("settled", 0.5, "estimated")
    assert row(db, done)[2:] == ("settled", 0.1, "reported")


# Spending through deletion


def test_deleting_a_conversation_keeps_its_spending_in_the_project(db):
    store = ContentStore(db)
    project = add_project(db)
    conversation, run = add_turn(db, project)
    other, other_run = add_turn(db, project)
    settled = reserve(db, project, conversation, run, 0.5)
    db.write(lambda conn: spending.settle(conn, settled, 0.3))
    in_flight = reserve(db, project, conversation, run, 0.2, step=1)
    reserve(db, project, other, other_run, 0.1)

    delete(db, store, "conversation", conversation)

    # Content-free rows stay: amount, basis, times, project and id; no run or conversation.
    assert row(db, settled) == (None, None, "settled", 0.3, "reported")
    assert row(db, in_flight) == (None, None, "open", None, None)
    assert project_spent(db, project) == 0.6  # 0.3 settled + 0.2 in flight + 0.1 elsewhere
    # The call in flight at the deletion settles once, by the reservation's id.
    assert db.write(lambda conn: spending.settle(conn, in_flight, 0.15)) is True
    assert db.write(lambda conn: spending.settle(conn, in_flight, 0.15)) is False
    assert project_spent(db, project) == 0.55
    # The project's budget still counts what the deleted conversation spent.
    with pytest.raises(spending.BudgetExceeded):
        reserve(db, project, other, other_run, 0.5, project_budget=1.0, step=1)


def test_usage_after_the_project_is_deleted_is_dropped_and_recreates_nothing(db):
    store = ContentStore(db)
    project = add_project(db)
    conversation, run = add_turn(db, project)
    in_flight = reserve(db, project, conversation, run, 0.2)
    undispatched = reserve(db, project, conversation, run, 0.2, step=1)

    delete(db, store, "project", project)

    assert db.write(lambda conn: spending.settle(conn, in_flight, 0.1)) is False
    assert db.write(lambda conn: spending.release(conn, undispatched)) is False
    assert db.read(lambda conn: conn.execute("SELECT count(*) FROM budget_reservations").fetchone()) == (0,)
    assert project_spent(db, project) == 0


@pytest.mark.asyncio
async def test_a_budget_lowered_while_a_turn_waits_binds_its_next_call(tmp_path):
    from scholia_app import started
    async with started(tmp_path / "data") as client:
        conversation = (await client.post("/api/conversations", json={})).json()["id"]
        project = (await client.get(f"/api/conversations/{conversation}")).json()["project_id"]
        harness = client.state["harness"]
        claim = await harness.admit_turn(conversation, "admitted under the old budget")
        stream = harness.events(claim)
        assert (await anext(stream))["type"] == "run_started"  # its call is reserved on the next pull
        settings = (await client.get("/api/settings", params={"project_id": project})).json()
        response = await client.put("/api/settings", json={"project_id": project, "hash": settings["hash"],
                                                           "updates": {"project.budget_usd": 0.000001}})
        assert response.status_code == 200, response.text
        rest = [event async for event in stream]
        assert [e for e in rest if e["type"] == "limit_reached"] == [{"type": "limit_reached", "budget": "project"}]
        assert client.provider.answers == []  # nothing was sent


@pytest.mark.asyncio
async def test_a_turn_reserves_from_its_counted_request_and_output_limit_at_the_highest_reachable_rate(tmp_path):
    # status.md's S1-19 follow-up and model-route rule 6: no fixed bucket per mode. A long message
    # reserves more than a short one, and the reservation is the request's counted tokens and its
    # whole output limit at the highest rates among the model's endpoints.
    import json
    from backend import budget_router, context
    from scholia_app import MockProvider, send, started
    model = budget_router.MODEL_TIERS["mid"][0]  # what Auto picks first
    provider = MockProvider(endpoints={model: [
        {"tag": "a", "context_length": 131072, "pricing": {"prompt": "0.000002", "completion": "0.00001",
                                                           "input_cache_write": "0.0000025"}},
        {"tag": "b", "context_length": 131072, "pricing": {"prompt": "0.000001", "completion": "0.000012",
                                                           "overrides": [{"prompt": "0.000004"}]}}]})
    async with started(tmp_path / "data", provider) as client:
        conversation = (await client.post("/api/conversations", json={})).json()["id"]
        await send(client, conversation, "Short?")
        long = "A long question about cohort studies and their follow-up. " * 1500  # about 90,000 characters
        await send(client, conversation, long)
        read = client.state["db"].read
        steps = [json.loads(d) for (d,) in await asyncio.to_thread(read, lambda conn: conn.execute(
            "SELECT data FROM run_events r JOIN runs u ON u.id = r.run_id WHERE r.type = 'step_started'"
            " AND u.kind = 'turn' ORDER BY u.started_at, r.seq").fetchall())]
        reserved = await asyncio.to_thread(read, lambda conn: conn.execute(
            "SELECT b.estimate_usd FROM budget_reservations b JOIN runs u ON u.id = b.run_id WHERE u.kind = 'turn'"
            " ORDER BY u.started_at").fetchall())
        assert [e for (e,) in reserved] == [s["estimate_usd"] for s in steps]
        assert steps[1]["estimate_usd"] > steps[0]["estimate_usd"] and steps[1]["input_tokens"] > 20000
        for step, body in zip(steps, provider.answers):
            # 4e-6 a prompt token (b's long-context override), 1.2e-5 an output one (b's completion).
            assert step["estimate_usd"] == pytest.approx(step["input_tokens"] * 4e-6 + step["max_tokens"] * 1.2e-5)
            # counted with the tool definitions it carries (S1-19c: the agent loop's)
            assert step["input_tokens"] == context.serialized_tokens(body["messages"], body.get("tools", ())) \
                and body["max_tokens"] == 8000


@pytest.mark.asyncio
async def test_a_request_too_costly_for_the_budget_stops_at_the_limit_before_any_call(tmp_path):
    # Lifecycle (L9): the request-sized reservation does not fit, so no step starts and nothing is sent.
    from scholia_app import MockProvider, send, started
    provider = MockProvider()
    async with started(tmp_path / "data", provider) as client:
        conversation = (await client.post("/api/conversations", json={})).json()["id"]
        project = (await client.get(f"/api/conversations/{conversation}")).json()["project_id"]
        settings = (await client.get("/api/settings", params={"project_id": project})).json()
        # 8,000 output tokens at $5 per million alone is $0.04: more than this budget holds.
        await client.put("/api/settings", json={"project_id": project, "hash": settings["hash"],
                                                "updates": {"project.budget_usd": 0.03}})
        stream = await send(client, conversation, "Short?")
        assert [e["type"] for e in stream] == ["run_started", "route", "limit_reached", "run_finished"]
        assert provider.chats == []
        assert await asyncio.to_thread(client.state["db"].read, lambda conn: conn.execute(
            "SELECT count(*) FROM budget_reservations").fetchone()) == (0,)


# S1-19c: the agent loop's steps and tools (lifecycle L1, L9)


@pytest.mark.asyncio
async def test_a_step_the_budget_cannot_hold_stops_the_loop_at_its_limit_and_starts_nothing_more(tmp_path):
    from agentic import Scripted, call, steps_reply
    from cited import cited, conversation_in, rows, with_papers
    from scholia_app import send
    from test_search import WAGES
    provider = Scripted(lambda body: steps_reply(provider, [call("list_materials")], cost=0.03), "Never.")
    async with cited(tmp_path, provider) as client:
        project, _ = await with_papers(client, WAGES)
        conversation = await conversation_in(client, project)
        await asyncio.to_thread(client.state["db"].write, lambda conn: conn.execute(
            "UPDATE conversations SET budget_usd = 0.08 WHERE id = ?", (conversation,)))
        stream = await send(client, conversation, "Which papers do I have?")
        assert {"type": "limit_reached", "budget": "conversation"} in stream
        assert stream[-1]["cancel_reason"] == "limit" and len(provider.agent_steps) == 1  # the admitted step finished
        reservations = await rows(client, "SELECT status, settled_usd FROM budget_reservations WHERE run_id = ?",
                                  stream[0]["run_id"])
        assert ("settled", 0.03) in reservations and not [r for r in reservations if r[0] == "open"]


@pytest.mark.asyncio
async def test_a_stop_during_a_tool_call_settles_its_model_call_once_and_publishes_nothing(tmp_path):
    from agentic import Scripted, call
    from cited import cited, conversation_in, rows, with_papers
    from scholia_app import send
    from test_search import WAGES
    release = asyncio.Event()
    started = asyncio.Event()

    async def held(body):
        started.set()
        await release.wait()
        return "Never delivered [S1.1]."
    provider = Scripted([call("answer_from_library", question="What do minimum wages do?")], "Never.", inner=[held])
    async with cited(tmp_path, provider) as client:
        project, _ = await with_papers(client, WAGES)
        conversation = await conversation_in(client, project)
        sending = asyncio.create_task(send(client, conversation, "Which papers do I have?"))
        await started.wait()  # the tool's own answer step is in flight
        run_id = (await rows(client, "SELECT id FROM runs WHERE conversation_id = ?", conversation))[0][0]
        assert (await client.post(f"/api/runs/{run_id}/cancel")).json()["status"] == "cancelled"
        release.set()
        stream = await sending
        assert stream[-1]["status"] == "cancelled" and not [e for e in stream if e["type"] == "chat_response"]
        reservations = await rows(client, "SELECT status, basis FROM budget_reservations WHERE run_id = ?", run_id)
        assert not [r for r in reservations if r[0] == "open"]
        attempts = await rows(client, "SELECT json_extract(data, '$.outcome') FROM run_events WHERE run_id = ?"
                                      " AND type = 'model_attempt'", run_id)
        assert attempts.count(("cancelled",)) == 1  # the call in flight, settled once at its estimate
        assert await rows(client, "SELECT answer FROM turns WHERE run_id = ?", run_id) == [(None,)]
