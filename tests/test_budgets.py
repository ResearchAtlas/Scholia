"""Budget reservations and settlement (backend/spending.py) with the deletion service; S1-33's budget asks,
notices, spend and the evaluation project's conservative admission (backend/budgets.py)."""

import asyncio
import sqlite3
import threading
import tomllib

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


@pytest.mark.parametrize("estimate", [-0.01, float("nan"), float("inf"), None, "0.1", True,
                                      pytest.param(10 ** 400, id="1e400")])
def test_an_invalid_estimate_is_refused(db, estimate):
    # 10 ** 400: an amount no float can hold is no amount (spending.amount; PR 52 cycle 4, design review A2)
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
        assert (await anext(stream))["type"] == "route"
        ask = await anext(stream)  # S1-33: the lowered budget does not hold the step, so it asks first
        assert ask["type"] == "ask"
        [listed] = (await client.get("/api/asks", params={"conversation_id": conversation})).json()["asks"]
        assert (listed["params"]["budget"], listed["params"]["limit_usd"]) == ("project", 0.000001)
        await client.post(f"/api/runs/{claim.run_id}/asks/{listed['ask_id']}", json={"option": "not_now"})
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
        sending = asyncio.create_task(send(client, conversation, "Short?"))
        from test_agent_loop import asked
        ask = await asked(client, conversation)  # S1-33: it asks before the step; Not now ends it at the limit
        await client.post(f"/api/runs/{ask['run_id']}/asks/{ask['ask_id']}", json={"option": "not_now"})
        stream = await sending
        assert [e["type"] for e in stream] == ["run_started", "route", "ask", "limit_reached", "run_finished"]
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
        sending = asyncio.create_task(send(client, conversation, "Which papers do I have?"))
        from test_agent_loop import asked
        ask = await asked(client, conversation)  # S1-33: the second step asks first; Stop ends it at the limit
        assert ask["kind"] == "budget" and len(provider.agent_steps) == 1
        await client.post(f"/api/runs/{ask['run_id']}/asks/{ask['ask_id']}", json={"option": ask["options"][-1]})
        stream = await sending
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


# S1-33: the budget asks (slice-1 spec F11, At the cap and Before a step; section 6.2, Effects), the bar's
# notices and spend. A turn's model calls ask before a reservation that does not fit; background runs never ask.


async def _app(tmp_path, provider=None):
    from scholia_app import started
    return started(tmp_path / "data", provider)


async def _conversation(client, budget=None, title="Named"):
    """A titled conversation (no title run beside its turns) in the General project: (conversation, project)."""
    conversation = (await client.post("/api/conversations", json={"title": title})).json()["id"]
    project = (await client.get(f"/api/conversations/{conversation}")).json()["project_id"]
    if budget is not None:
        await asyncio.to_thread(client.state["db"].write, lambda conn: conn.execute(
            "UPDATE conversations SET budget_usd = ? WHERE id = ?", (budget, conversation)))
    return conversation, project


async def _spent(client, project, usd, conversation=None):
    """Spending of an earlier, finished run, settled: counted toward the project (and the conversation, if given)."""
    from backend.db import utc_now
    await asyncio.to_thread(client.state["db"].write, lambda conn: conn.execute(
        "INSERT INTO budget_reservations (id, step_seq, paying_conversation_id, project_id, estimate_usd, status,"
        " settled_usd, basis, settled_at) VALUES (?, 0, ?, ?, ?, 'settled', ?, 'reported', ?)",
        (new_id(), conversation, project, usd, usd, utc_now())))


async def _project_budget(client, project, usd=None):
    """Set the project's budget in its config.toml (usd given), or read it."""
    settings = (await client.get("/api/settings", params={"project_id": project})).json()
    if usd is None:
        return settings["values"]["project"]["budget_usd"]
    response = await client.put("/api/settings", json={"project_id": project, "hash": settings["hash"],
                                                       "updates": {"project.budget_usd": usd}})
    assert response.status_code == 200, response.text


async def _rows(client, sql, *args):
    return await asyncio.to_thread(client.state["db"].read, lambda conn: conn.execute(sql, args).fetchall())


async def _asking(client, conversation, text="Short?"):
    """A turn sent and waiting on its first ask: (the send's task, the ask as the conversation lists it)."""
    from scholia_app import send
    from test_agent_loop import asked
    sending = asyncio.create_task(send(client, conversation, text))
    return sending, await asked(client, conversation)


async def _answer(client, ask, **body):
    return await client.post(f"/api/runs/{ask['run_id']}/asks/{ask['ask_id']}", json=body)


async def _decisions(client, run_id):
    import json
    return [json.loads(d) for (d,) in await _rows(
        client, "SELECT data FROM run_events WHERE run_id = ? AND type = 'budget_decision' ORDER BY seq", run_id)]


async def _own_budget(client, conversation):
    return (await _rows(client, "SELECT budget_usd FROM conversations WHERE id = ?", conversation))[0][0]


@pytest.mark.asyncio
async def test_a_step_alone_over_the_budget_left_asks_first_and_go_ahead_raises_the_limit_just_enough(tmp_path):
    # A2, A6, B5: money is left, but this one step's estimate is more: asked before it is reserved or sent.
    async with await _app(tmp_path) as client:
        conversation, project = await _conversation(client, budget=0.03)
        sending, ask = await _asking(client, conversation)
        assert (ask["kind"], ask["options"], ask["text_box"]) == ("budget", ["go_ahead", "not_now"], True)
        params = ask["params"]
        assert (params["form"], params["budget"], params["limit_usd"], params["spent_usd"], params["phase"]) == (
            "step", "conversation", 0.03, 0, "answer")
        assert client.provider.answers == []  # nothing sent, nothing reserved while it asks
        assert await _rows(client, "SELECT count(*) FROM budget_reservations") == [(0,)]
        answered = await _answer(client, ask, option="go_ahead")
        assert answered.status_code == 200, answered.text
        assert answered.json()["budget"] == {"applied": True, "budget": "conversation", "limit_usd": params["estimate_usd"]}
        stream = await sending
        assert stream[-1]["status"] == "succeeded"
        assert await _own_budget(client, conversation) == params["estimate_usd"]  # exactly what the step needed
        [step] = await _rows(client, "SELECT json_extract(data, '$.estimate_usd') FROM run_events WHERE run_id = ?"
                                     " AND type = 'step_started'", ask["run_id"])
        assert step[0] == params["estimate_usd"]  # the ask's estimate is the reservation's own amount (B5)
        [decision] = await _decisions(client, ask["run_id"])
        assert {k: decision[k] for k in ("ask_id", "budget", "decision", "from_usd", "to_usd", "applied")} == {
            "ask_id": ask["ask_id"], "budget": "conversation", "decision": "go_ahead", "from_usd": 0.03,
            "to_usd": params["estimate_usd"], "applied": True}
        audited = await _rows(client, "SELECT data FROM audit_log WHERE event = 'budget_decision'")
        assert len(audited) == 1 and "Short?" not in audited[0][0]


@pytest.mark.asyncio
async def test_at_the_cap_continue_adds_the_budgets_amount_again_never_compounding(tmp_path):
    # A1, A5, G1: settled spend has reached the conversation's limit: Continue, Set a new limit, or Stop.
    async with await _app(tmp_path) as client:
        conversation, project = await _conversation(client, budget=0.5)
        await _spent(client, project, 0.5, conversation)
        sending, ask = await _asking(client, conversation)
        assert (ask["options"], ask["params"]["form"]) == (["continue", "stop"], "cap")
        assert (await _answer(client, ask, option="continue")).json()["budget"]["limit_usd"] == 1.0
        assert (await sending)[-1]["status"] == "succeeded"
        assert await _own_budget(client, conversation) == 1.0
        await _spent(client, project, 0.5, conversation)  # the cap again
        sending, ask = await _asking(client, conversation, "Again?")
        assert ask["params"]["form"] == "cap"
        assert (await _answer(client, ask, option="continue")).json()["budget"]["limit_usd"] == 1.5  # $0.50 again
        assert (await sending)[-1]["status"] == "succeeded"
        decisions = await _decisions(client, ask["run_id"])
        assert [(d["decision"], d["from_usd"], d["to_usd"], d["amount_usd"]) for d in decisions] == [
            ("continue", 1.0, 1.5, 0.5)]


@pytest.mark.asyncio
@pytest.mark.parametrize("form, option", [("cap", "stop"), ("step", "not_now")])
async def test_stop_and_not_now_end_the_turn_at_the_limit_with_continue_offered(tmp_path, form, option):
    # A8: the run ends cancelled at the limit, keeping its settled costs, and Continue builds a new turn.
    async with await _app(tmp_path) as client:
        conversation, project = await _conversation(client, budget=0.03)
        if form == "cap":
            await _spent(client, project, 0.03, conversation)
        sending, ask = await _asking(client, conversation)
        assert ask["params"]["form"] == form
        assert (await _answer(client, ask, option=option)).json()["budget"]["applied"] is False
        stream = await sending
        assert {"type": "limit_reached", "budget": "conversation"} in stream
        assert (stream[-1]["status"], stream[-1]["cancel_reason"]) == ("cancelled", "limit")
        assert await _own_budget(client, conversation) == 0.03 and client.provider.answers == []
        [decision] = await _decisions(client, ask["run_id"])
        assert (decision["decision"], decision["applied"]) == (option, False)
        turn = (await client.get(f"/api/conversations/{conversation}")).json()["turns"][-1]
        assert (turn["status"], turn["cancel_reason"]) == ("cancelled", "limit")
        await asyncio.to_thread(client.state["db"].write, lambda conn: conn.execute(
            "UPDATE conversations SET budget_usd = 5 WHERE id = ?", (conversation,)))
        again = await client.post(f"/api/runs/{ask['run_id']}/continue", json={})
        assert again.status_code == 200 and '"succeeded"' in again.text


@pytest.mark.asyncio
async def test_set_a_new_limit_takes_a_typed_amount_that_fits_the_step(tmp_path):
    # A7, G4: a typed limit, checked through visible; one that is not a positive amount, or still does not fit
    # the step, is refused, nothing changing and the ask staying open.
    async with await _app(tmp_path) as client:
        conversation, project = await _conversation(client, budget=0.03)
        sending, ask = await _asking(client, conversation)
        for text, code in (("lots", "invalid_limit"), ("0", "invalid_limit"), ("-5", "invalid_limit"),
                           ("inf", "invalid_limit"), ("nan", "invalid_limit"), ("​", "invalid_answer"),
                           ("0.031", "limit_too_low")):
            refused = await _answer(client, ask, text=text)
            assert (refused.status_code, refused.json()["code"]) == (400, code), text
        assert await _own_budget(client, conversation) == 0.03
        assert await _decisions(client, ask["run_id"]) == []
        assert (await _answer(client, ask, text=" $2.5 ")).json()["budget"] == {
            "applied": True, "budget": "conversation", "limit_usd": 2.5}
        assert (await sending)[-1]["status"] == "succeeded"
        assert await _own_budget(client, conversation) == 2.5
        [decision] = await _decisions(client, ask["run_id"])
        assert (decision["decision"], decision["to_usd"]) == ("set_limit", 2.5)


@pytest.mark.parametrize("text, usd", [
    ("$1,234.05", 1234.05), ("US$1,234.05", 1234.05), ("1,234", 1234.0), ("$999,999.99", 999999.99),
    ("US$1,000,000.00", 1000000.0), ("＄１，２３４．０５", 1234.05), (" 25 ", 25.0), ("$2.50", 2.5)])
def test_a_typed_limit_is_taken_as_the_needed_text_shows_amounts_grouped_in_thousands(text, usd):
    # Design review V's adjacent finding: the needed text says amounts from $1,000 with grouping ("$1,234.05", in
    # zh-CN "US$1,234.05"), and typing one exactly as shown was refused invalid_limit
    from backend import budgets
    assert budgets._typed(text) == usd


@pytest.mark.parametrize("text", ["1,23", "12,3456", ",123", "1,,234", "1,234,5", "1.234,05", "1 234", "1,234.", "$,5",
                                  "0,123", "00,123", "0.0000000001"])  # the internal review: never shown so, or $0
def test_a_typed_limit_grouped_any_other_way_is_still_refused(text):
    from backend import budgets
    with pytest.raises(budgets.Refused) as refused:
        budgets._typed(text)
    assert refused.value.code == "invalid_limit"


@pytest.mark.asyncio
async def test_the_least_limit_a_step_needs_typed_back_as_the_interface_says_it_applies(tmp_path):
    # The needed text's amount (frontend/src/action.js: rounded up to the cent, grouped in thousands, "$" in English and
    # "US$" in Chinese), typed back exactly, applies; a cent less is refused with the same needed amount
    import math
    async with await _app(tmp_path) as client:
        conversation, project = await _conversation(client, budget=1000.0)
        await _project_budget(client, project, 5000.0)
        await _spent(client, project, 1234.0, conversation)
        sending, ask = await _asking(client, conversation)
        refused = await _answer(client, ask, text="1000.5")
        assert (refused.status_code, refused.json()["code"]) == (400, "limit_too_low")
        cents = math.ceil(refused.json()["needed_usd"] * 100 - 1e-9) / 100
        less = await _answer(client, ask, text=f"${cents - 0.01:,.2f}")
        assert (less.json()["code"], less.json()["needed_usd"]) == ("limit_too_low", refused.json()["needed_usd"])
        answered = await _answer(client, ask, text=f"US${cents:,.2f}")
        assert answered.json()["budget"] == {"applied": True, "budget": "conversation", "limit_usd": cents}
        assert (await sending)[-1]["status"] == "succeeded"


@pytest.mark.asyncio
async def test_two_conversations_admitting_075_against_1_left_one_is_admitted_and_one_asks(tmp_path):
    # SPEC:1197 end to end: two turns, each a $0.75 step, against $1 left of the project's budget.
    from scholia_app import MockProvider
    model = budget_router_model()
    release = asyncio.Event()

    async def held(body):
        await release.wait()
        return provider.answer("First.")
    provider = MockProvider(held, endpoints={model: [{"tag": "a", "context_length": 128000,
                                                      "pricing": {"prompt": "0", "completion": "0.00009375"}}]})
    async with await _app(tmp_path, provider) as client:
        first, project = await _conversation(client)
        second, _ = await _conversation(client, title="Other")
        await _spent(client, project, 49.0)
        from scholia_app import send
        from test_agent_loop import asked
        sending = asyncio.create_task(send(client, first, "Short?"))
        while not provider.answers:  # the first is admitted, its call in flight
            await asyncio.sleep(0.01)
        other, ask = await _asking(client, second)
        assert (ask["params"]["form"], ask["params"]["budget"], ask["params"]["estimate_usd"]) == ("step", "project", 0.75)
        release.set()
        assert (await sending)[-1]["status"] == "succeeded"
        await _answer(client, ask, option="not_now")
        assert (await other)[-1]["cancel_reason"] == "limit"
        assert len(provider.answers) == 1


def budget_router_model():
    from backend import budget_router
    return budget_router.MODEL_TIERS["mid"][0]  # what Auto picks first


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["another_continue", "settings_edit"])
async def test_a_stale_answer_applies_nothing_and_the_step_is_checked_again(tmp_path, change):
    # L6: the limit an ask was raised against changed before its answer (another conversation's Continue, or a
    # Settings edit): the answer applies nothing and its step is checked again, so the project's limit is never
    # raised twice for one cap.
    async with await _app(tmp_path) as client:
        first, project = await _conversation(client)
        second, _ = await _conversation(client, title="Other")
        await _project_budget(client, project, 1.0)
        await _spent(client, project, 1.0)
        sending_a, ask_a = await _asking(client, first)
        if change == "another_continue":
            sending_b, ask_b = await _asking(client, second)
            assert (await _answer(client, ask_a, option="continue")).json()["budget"]["limit_usd"] == 2.0
            await sending_a
            stale, sending = ask_b, sending_b
        else:
            await _project_budget(client, project, 3.0)
            stale, sending = ask_a, sending_a
        answered = (await _answer(client, stale, option="continue")).json()
        assert answered["budget"]["applied"] is False
        assert (await sending)[-1]["status"] == "succeeded"  # checked again: it fits the limit as it is now
        assert await _project_budget(client, project) == (2.0 if change == "another_continue" else 3.0)
        [decision] = await _decisions(client, stale["run_id"])
        assert (decision["applied"], decision["stale"]) == (False, True)


@pytest.mark.asyncio
async def test_a_project_budget_answer_whose_settings_write_fails_raises_nothing_and_asks_again(tmp_path, monkeypatch):
    # L5 (b), reading 4: the answer and its decision commit first; the settings file is then written; when that
    # fails nothing is raised, the reply says so, and the step asks again.
    from backend import settings
    async with await _app(tmp_path) as client:
        conversation, project = await _conversation(client)
        await _project_budget(client, project, 1.0)
        await _spent(client, project, 1.0)
        sending, ask = await _asking(client, conversation)
        saving = settings.Settings.save

        def failing(self, updates, answer=False):
            if "project.budget_usd" in updates:
                raise OSError("disk full")
            return saving(self, updates, answer)
        monkeypatch.setattr(settings.Settings, "save", failing)
        answered = (await _answer(client, ask, option="continue")).json()
        assert answered["budget"] == {"applied": False, "budget": "project", "limit_usd": 1.0, "error": "not_saved"}
        from test_agent_loop import asked
        deadline = asyncio.get_running_loop().time() + 10
        while True:  # asked again, by a new ask
            again = await asked(client, conversation)
            if again["ask_id"] != ask["ask_id"]:
                break
            assert asyncio.get_running_loop().time() < deadline
            await asyncio.sleep(0.05)
        assert await _project_budget(client, project) == 1.0
        # recorded decided, then not saved, and audited as not applied: no record says the budget was raised (review 1)
        decided = await _decisions(client, ask["run_id"])
        assert [(d["decision"], d["applied"], d["to_usd"], d.get("saved")) for d in decided] == [
            ("continue", False, 1.0, None), ("continue", False, 1.0, False)]
        import json
        audited = [json.loads(d) for (d,) in await _rows(client, "SELECT data FROM audit_log WHERE event = 'budget_decision'")]
        assert [(a["decision"], a["to_usd"], a["applied"]) for a in audited] == [("continue", 1.0, False)]
        monkeypatch.setattr(settings.Settings, "save", saving)
        await _answer(client, again, option="continue")  # saved now: decided, then applied, and audited once each
        assert (await sending)[-1]["status"] == "succeeded" and await _project_budget(client, project) == 2.0
        decided = await _decisions(client, ask["run_id"])
        assert [(d["applied"], d["to_usd"]) for d in decided[2:]] == [(False, 1.0), (True, 2.0)]
        audited = await _rows(client, "SELECT json_extract(data, '$.to_usd'), json_extract(data, '$.applied')"
                                      " FROM audit_log WHERE event = 'budget_decision'")
        assert audited == [(1.0, 0), (2.0, 1)]


@pytest.mark.asyncio
async def test_a_budget_answer_and_its_effect_commit_together(tmp_path, monkeypatch):
    # A4: a failure after the change, inside the answer's transaction, leaves neither the answer nor the change.
    from backend import governance
    async with await _app(tmp_path) as client:
        conversation, _ = await _conversation(client, budget=0.03)
        sending, ask = await _asking(client, conversation)
        recording = governance.record

        def failing(conn, event, *args, **kwargs):
            if event == "budget_decision":
                raise RuntimeError("audit failed")
            return recording(conn, event, *args, **kwargs)
        monkeypatch.setattr(governance, "record", failing)
        with pytest.raises(RuntimeError):
            await _answer(client, ask, option="go_ahead")
        monkeypatch.setattr(governance, "record", recording)
        assert await _own_budget(client, conversation) == 0.03
        assert await _rows(client, "SELECT count(*) FROM run_events WHERE type IN ('ask_answered', 'budget_decision')"
                                   " AND run_id = ?", ask["run_id"]) == [(0,)]
        assert (await _answer(client, ask, option="not_now")).status_code == 200  # still open
        await sending


@pytest.mark.asyncio
async def test_a_stop_while_waiting_on_a_budget_ask_settles_nothing_twice_and_a_late_answer_changes_nothing(tmp_path):
    # L1, G9: the refused step was never reserved; the ask closes with its run; a late answer is refused.
    async with await _app(tmp_path) as client:
        conversation, _ = await _conversation(client, budget=0.03)
        sending, ask = await _asking(client, conversation)
        assert (await client.post(f"/api/runs/{ask['run_id']}/cancel")).json()["status"] == "cancelled"
        stream = await sending
        assert (stream[-1]["status"], stream[-1].get("cancel_reason")) == ("cancelled", "researcher")
        late = await _answer(client, ask, option="go_ahead")
        assert (late.status_code, late.json()["code"]) == (409, "ask_closed")
        assert await _own_budget(client, conversation) == 0.03
        assert await _rows(client, "SELECT count(*) FROM budget_reservations") == [(0,)]
        assert await _rows(client, "SELECT count(*) FROM run_events WHERE type = 'budget_decision'") == [(0,)]


@pytest.mark.asyncio
async def test_a_stop_racing_a_continue_answer_takes_exactly_one_of_them(tmp_path):
    # L1: either the answer is refused (the Stop came first), or it applies and the Stop ends the resumed run.
    async with await _app(tmp_path) as client:
        conversation, project = await _conversation(client, budget=0.5)
        await _spent(client, project, 0.5, conversation)
        for _ in range(3):
            sending, ask = await _asking(client, conversation)
            stopped, answered = await asyncio.gather(client.post(f"/api/runs/{ask['run_id']}/cancel"),
                                                     _answer(client, ask, option="continue"))
            stream = await sending
            assert stream[-1]["status"] in ("cancelled", "succeeded")
            decisions = await _decisions(client, ask["run_id"])
            answers = await _rows(client, "SELECT count(*) FROM run_events WHERE run_id = ? AND type = 'ask_answered'"
                                          " AND json_extract(data, '$.by') = 'researcher'", ask["run_id"])
            assert answers == [(len(decisions),)] and len(decisions) == (answered.status_code == 200)
            assert await _rows(client, "SELECT count(*) FROM budget_reservations WHERE status = 'open'") == [(0,)]
            await asyncio.to_thread(client.state["db"].write, lambda conn: conn.execute(
                "UPDATE conversations SET budget_usd = 0.5 WHERE id = ?", (conversation,)))


@pytest.mark.asyncio
async def test_deleting_the_conversation_while_its_turn_waits_on_a_budget_ask_keeps_its_spending(tmp_path):
    # L3 (a): the ask goes with its run; the conversation's spending stays in the project's total.
    async with await _app(tmp_path) as client:
        conversation, project = await _conversation(client, budget=0.5)
        await _spent(client, project, 0.5, conversation)
        sending, ask = await _asking(client, conversation)
        assert (await client.delete(f"/api/conversations/{conversation}")).status_code == 200
        await sending
        assert (await _answer(client, ask, option="continue")).status_code in (404, 409)
        assert (await client.get("/api/asks")).json()["asks"] == []
        spend = (await client.get(f"/api/projects/{project}/spend")).json()
        assert spend["project"]["settled_usd"] == 0.5


@pytest.mark.asyncio
async def test_tightening_the_project_while_a_budget_ask_waits_refuses_its_answer_and_revokes_the_run(tmp_path):
    # L4: a policy change makes the ask invalid; the run is revoked by the change and sends nothing.
    async with await _app(tmp_path) as client:
        project = (await client.post("/api/projects", json={"name": "Thesis"})).json()["id"]
        conversation = (await client.post("/api/conversations", json={"project_id": project, "title": "t"})).json()["id"]
        await asyncio.to_thread(client.state["db"].write, lambda conn: conn.execute(
            "UPDATE conversations SET budget_usd = 0.03 WHERE id = ?", (conversation,)))
        sending, ask = await _asking(client, conversation)
        assert (await client.post(f"/api/projects/{project}/sensitivity", json={"level": "local_only"})).status_code == 200
        refused = await _answer(client, ask, option="go_ahead")
        assert refused.status_code == 409 and refused.json()["code"] in ("ask_invalid", "ask_closed")
        stream = await sending
        assert (stream[-1]["status"], stream[-1].get("cancel_reason")) == ("cancelled", "revoked")
        assert client.provider.answers == [] and await _own_budget(client, conversation) == 0.03


@pytest.mark.asyncio
async def test_notices_at_50_85_and_90_percent_are_recorded_once_per_budget_and_limit(tmp_path):
    # N2, G3: one settlement from 40% to 95% records each threshold it crossed once, streamed as `budget`; a
    # raised limit rearms them.
    from scholia_app import MockProvider, send
    async with await _app(tmp_path, MockProvider(cost=0.055)) as client:
        conversation, project = await _conversation(client, budget=0.1)
        await _spent(client, project, 0.04, conversation)
        stream = await send(client, conversation, "Short?")
        shown = [e for e in stream if e["type"] == "budget"]
        assert [(e["budget"], e["percent"]) for e in shown] == [("conversation", 50), ("conversation", 85),
                                                                ("conversation", 90)]
        assert shown[-1]["settled_usd"] == pytest.approx(0.095) and shown[-1]["limit_usd"] == 0.1
        noticed = await _rows(client, "SELECT json_extract(data, '$.budget'), json_extract(data, '$.percent'),"
                                      " json_extract(data, '$.limit_usd') FROM run_events WHERE type = 'budget_notice'")
        assert sorted(noticed) == [("conversation", 50, 0.1), ("conversation", 85, 0.1), ("conversation", 90, 0.1)]
        await asyncio.to_thread(client.state["db"].write, lambda conn: conn.execute(
            "UPDATE conversations SET budget_usd = 0.2 WHERE id = ?", (conversation,)))
        stream = await send(client, conversation, "Again?")  # 0.15 of 0.2: 75%, a new limit's 50% only
        assert [(e["budget"], e["percent"], e["limit_usd"]) for e in stream if e["type"] == "budget"] == [
            ("conversation", 50, 0.2)]


@pytest.mark.asyncio
async def test_spend_reports_settled_and_open_spend_against_both_budgets_without_content(tmp_path):
    # N3: GET /api/projects/{id}/spend, a deleted conversation's spending staying in the project's figure.
    async with await _app(tmp_path) as client:
        conversation, project = await _conversation(client, budget=2.0)
        await _project_budget(client, project, 20.0)
        await _spent(client, project, 0.5, conversation)
        other, _ = await _conversation(client, title="Other")
        await _spent(client, project, 0.25, other)
        spend = (await client.get(f"/api/projects/{project}/spend", params={"conversation_id": conversation})).json()
        assert spend == {
            "project": {"limit_usd": 20.0, "settled_usd": 0.75, "open_usd": 0, "reported_usd": 0.75, "estimated_usd": 0},
            "conversation": {"limit_usd": 2.0, "settled_usd": 0.5, "open_usd": 0, "own": True, "reported_usd": 0.5,
                             "estimated_usd": 0},
            "notice_percents": [50, 85, 90]}
        default = (await client.get(f"/api/projects/{project}/spend", params={"conversation_id": other})).json()
        assert default["conversation"] == {"limit_usd": 10, "settled_usd": 0.25, "open_usd": 0, "own": False,
                                           "reported_usd": 0.25, "estimated_usd": 0}
        await client.delete(f"/api/conversations/{conversation}")
        assert (await client.get(f"/api/projects/{project}/spend")).json()["project"]["settled_usd"] == 0.75
        assert (await client.get(f"/api/projects/{project}/spend", params={"conversation_id": conversation})).status_code == 404
        assert (await client.get(f"/api/projects/{new_id()}/spend")).status_code == 404


# S1-33: conservative admission for the preview's evaluation project (SPEC:1223; T70:52-55; reading 15)


@pytest.mark.asyncio
async def test_an_evaluation_project_reserves_the_conservative_bound_and_never_asks(tmp_path):
    # P1, P2: each request reserves its estimate times the attempts one call may make; at its budget the run stops
    # at the limit with no ask; a normal project's reservation is unchanged.
    from backend import openrouter
    from scholia_app import send
    async with await _app(tmp_path) as client:
        made = await client.post("/api/projects", json={"name": "Evaluation", "evaluation": True})
        assert made.status_code == 201 and made.json()["evaluation"] is True
        project = made.json()["id"]
        conversation = (await client.post("/api/conversations", json={"project_id": project, "title": "t"})).json()["id"]
        stream = await send(client, conversation, "Short?")
        assert stream[-1]["status"] == "succeeded"
        [(estimate, reserved)] = await _rows(
            client, "SELECT json_extract(e.data, '$.request_usd'), b.estimate_usd FROM run_events e"
                    " JOIN budget_reservations b ON b.id = json_extract(e.data, '$.reservation_id')"
                    " WHERE e.type = 'step_started' AND e.run_id = ?", stream[0]["run_id"])
        assert reserved == pytest.approx(estimate * openrouter.MAX_ATTEMPTS)
        await _project_budget(client, project, 0.05)
        stream = await send(client, conversation, "Again?")
        assert {"type": "limit_reached", "budget": "project"} in stream and stream[-1]["cancel_reason"] == "limit"
        assert not [e for e in stream if e["type"] == "ask"]
        assert await _rows(client, "SELECT count(*) FROM run_events WHERE type = 'ask'") == [(0,)]


@pytest.mark.asyncio
async def test_the_evaluation_mark_is_set_at_creation_only(tmp_path):
    # P3: it lives in the database; no settings file or API change clears or sets it later.
    import sqlite3
    async with await _app(tmp_path) as client:
        project = (await client.post("/api/projects", json={"name": "Evaluation", "evaluation": True})).json()["id"]
        normal = (await client.post("/api/projects", json={"name": "Thesis"})).json()
        assert normal["evaluation"] is False
        for value in (False, True):  # refused, never ignored (D18; checklist P3), for the evaluation project or not
            for target in (project, normal["id"]):
                changed = await client.patch(f"/api/projects/{target}", json={"evaluation": value})
                assert (changed.status_code, changed.json()["code"]) == (400, "evaluation_at_creation")
        assert (await client.get(f"/api/projects/{normal['id']}")).json()["evaluation"] is False
        assert (await client.get(f"/api/projects/{project}")).json()["evaluation"] is True
        with pytest.raises(sqlite3.IntegrityError, match="evaluation"):
            await asyncio.to_thread(client.state["db"].write, lambda conn: conn.execute(
                "UPDATE projects SET evaluation = 0 WHERE id = ?", (project,)))


def test_a_child_runs_reservation_pays_its_parents_conversation_and_a_background_runs_pays_none(db):
    # B2, reading 16: the paying conversation is fixed at admission; S1-27 and S1-34 add the flows.
    project = add_project(db)
    conversation, parent = add_turn(db, project)
    child, background = new_id(), new_id()
    db.write(lambda conn: conn.execute(
        "INSERT INTO runs (id, project_id, conversation_id, parent_run_id, kind) VALUES (?, ?, ?, ?, 'child'),"
        " (?, ?, NULL, NULL, 'background')", (child, project, conversation, parent, background, project)))
    reserve(db, project, conversation, child, 0.25)
    reserve(db, project, None, background, 0.5)
    assert db.read(lambda conn: spending.spent(conn, conversation_id=conversation)) == 0.25
    assert project_spent(db, project) == 0.75
    with pytest.raises(spending.BudgetExceeded) as refused:  # the child's next step meets its parent's budget
        reserve(db, project, conversation, child, 0.5, conversation_budget=0.5, step=1)
    assert refused.value.budget == "conversation"


@pytest.mark.asyncio
async def test_a_budget_of_zero_asks_at_the_cap_and_the_route_holds_through_the_ask(tmp_path):
    # G2, I6: a zero budget is used up already; after Continue the step names the turn's own route and effort.
    import json
    async with await _app(tmp_path) as client:
        conversation, _ = await _conversation(client, budget=0)
        sending, ask = await _asking(client, conversation)
        assert (ask["params"]["form"], ask["params"]["limit_usd"]) == ("cap", 0)
        # Continue on a zero budget adds the default's amount, never nothing (review 1)
        assert (await _answer(client, ask, option="continue")).json()["budget"]["limit_usd"] == 10
        assert (await sending)[-1]["status"] == "succeeded"
        events = {kind: json.loads(data) for kind, data in await _rows(
            client, "SELECT type, data FROM run_events WHERE run_id = ? AND type IN ('route', 'step_started')"
                    " ORDER BY seq", ask["run_id"])}
        assert (events["step_started"]["route"], events["step_started"]["effort"]) == (
            events["route"]["route"], events["route"]["effort"])


@pytest.mark.asyncio
async def test_a_stop_landing_during_the_refused_reservation_ends_the_turn_and_raises_no_ask(tmp_path, monkeypatch):
    # Review 1, finding 1 (L1): the write that refuses the step runs to its end through a cancellation, which must
    # not be lost: the turn ends as stopped, with no ask nobody could answer.
    from scholia_app import send
    async with await _app(tmp_path) as client:
        conversation, _ = await _conversation(client, budget=0.03)
        harness, loop, real, fired = client.state["harness"], asyncio.get_running_loop(), spending.reserve, []

        def reserve(conn, **kwargs):  # a Stop arrives while the refusing write is in flight
            if not fired:
                fired.append(1)
                loop.call_soon_threadsafe(harness._request_cancel, harness.registry.runs[kwargs["run_id"]], "researcher")
            return real(conn, **kwargs)
        monkeypatch.setattr(spending, "reserve", reserve)
        stream = await asyncio.wait_for(send(client, conversation, "Short?"), 10)
        assert (stream[-1]["status"], stream[-1].get("cancel_reason")) == ("cancelled", "researcher")
        assert await _rows(client, "SELECT count(*) FROM run_events WHERE type = 'ask'") == [(0,)]


@pytest.mark.asyncio
async def test_continue_on_a_zero_budget_adds_the_configured_default(tmp_path):
    # Recheck 1: the personal default as the researcher set it, not the shipped one.
    async with await _app(tmp_path) as client:
        read = (await client.get("/api/settings")).json()
        await client.put("/api/settings", json={"hash": read["hash"], "updates": {"budget.conversation_usd": 25}})
        conversation, _ = await _conversation(client, budget=0)
        sending, ask = await _asking(client, conversation)
        assert (await _answer(client, ask, option="continue")).json()["budget"]["limit_usd"] == 25
        assert (await sending)[-1]["status"] == "succeeded"


# S1-33 fix batch 1 (PR 52 review 1)


@pytest.mark.asyncio
async def test_the_projects_continue_amount_survives_deleting_the_conversation_that_set_it(tmp_path):
    # Finding 1 (A5, reading 3): $1 becomes $2, the conversation that asked is deleted, and the next Continue still
    # adds $1: never compounding, whatever history is deleted.
    async with await _app(tmp_path) as client:
        first, project = await _conversation(client)
        await _project_budget(client, project, 1.0)
        await _spent(client, project, 1.0)
        sending, ask = await _asking(client, first)
        assert (await _answer(client, ask, option="continue")).json()["budget"]["limit_usd"] == 2.0
        await sending
        assert (await client.delete(f"/api/conversations/{first}")).status_code == 200
        second, _ = await _conversation(client, title="Other")
        [(spent,)] = await _rows(client, "SELECT sum(settled_usd) FROM budget_reservations WHERE project_id = ?", project)
        await _spent(client, project, round(2.0 - spent, 9))  # the cap again
        sending, ask = await _asking(client, second)
        assert ask["params"]["form"] == "cap"
        assert (await _answer(client, ask, option="continue")).json()["budget"]["limit_usd"] == 3.0
        await sending


@pytest.mark.asyncio
async def test_a_settings_write_that_fails_after_replacing_the_file_is_judged_by_what_the_file_says(tmp_path, monkeypatch):
    # Finding 3 (L5 b, U2): the file was replaced and then its folder's sync failed: the budget did change, so the
    # reply, the record and the audit say it was applied, and the step goes on with the new limit.
    import json
    from backend import settings
    async with await _app(tmp_path) as client:
        conversation, project = await _conversation(client)
        await _project_budget(client, project, 1.0)
        await _spent(client, project, 1.0)
        sending, ask = await _asking(client, conversation)
        real = settings.write_private

        def replaced_then_failed(path, data):
            real(path, data)
            raise OSError("fsync of the folder failed")
        monkeypatch.setattr(settings, "write_private", replaced_then_failed)
        answered = (await _answer(client, ask, option="continue")).json()["budget"]
        monkeypatch.setattr(settings, "write_private", real)
        assert answered == {"applied": True, "budget": "project", "limit_usd": 2.0}
        assert await _project_budget(client, project) == 2.0
        assert (await sending)[-1]["status"] == "succeeded"
        assert [(d["applied"], d["to_usd"]) for d in await _decisions(client, ask["run_id"])] == [(False, 1.0), (True, 2.0)]
        audited = [json.loads(d) for (d,) in await _rows(client, "SELECT data FROM audit_log WHERE event = 'budget_decision'")]
        assert [(a["to_usd"], a["applied"]) for a in audited] == [(2.0, True)]


# What a failed write may leave that the loader reads as its defaults: not TOML (fix batch 1 review), and valid TOML whose
# [project] is not a table, or whose budget is one, with a problem keyed by that table or below the budget (PR 52 cycle
# 4, G2: the readers took these as the $50 default)
UNREAD = [b"[project\nbudget_usd = ", b'project = "broken"\n', b"[[project]]\nbudget_usd = 2\n",
          b"[project.budget_usd]\ntoken = 1\n"]


@pytest.mark.asyncio
@pytest.mark.parametrize("left", UNREAD)
async def test_a_failed_write_that_leaves_an_unreadable_file_is_never_taken_as_saved(tmp_path, monkeypatch, left):
    # Fix batch 1 review (P3): an unreadable file reads as defaults, which say nothing of what it holds.
    from backend import settings
    async with await _app(tmp_path) as client:
        conversation, project = await _conversation(client)
        await _project_budget(client, project, 25.0)
        await _spent(client, project, 25.0)
        sending, ask = await _asking(client, conversation)
        real = settings.write_private

        def broken(path, data):
            real(path, left)
            raise OSError("fsync of the folder failed")
        monkeypatch.setattr(settings, "write_private", broken)
        answered = (await _answer(client, ask, text="50")).json()["budget"]  # 50 is also the shipped default
        monkeypatch.setattr(settings, "write_private", real)
        assert (answered["applied"], answered.get("error")) == (False, "not_saved")
        sending.cancel()
        with pytest.raises((asyncio.CancelledError, Exception)):
            await sending


@pytest.mark.asyncio
@pytest.mark.parametrize("left", UNREAD)
async def test_after_a_failed_save_leaves_the_file_unreadable_the_waiting_step_asks_again_never_at_the_default(
        tmp_path, monkeypatch, left):
    # S1-33 fix batch 3 (E4; Harold, 2026-10-21: if the write fails nothing is raised and the next step asks again): the
    # answer's write left the project's config.toml unreadable, whose loader then gives the shipped $50 default. That is
    # never the limit: the waiting step does not go out, it asks again, saying the budget could not be read (never that
    # it is $0); Continue has no amount to add to and applies nothing; the bar shows no project limit; and every
    # admission (a turn's, a title's, an extraction's, a judge's unspent held again) reads no room (runs._budgets_now).
    from backend import settings
    from test_agent_loop import asked
    async with await _app(tmp_path) as client:
        conversation, project = await _conversation(client)
        await _project_budget(client, project, 1.0)
        await _spent(client, project, 1.0)
        sending, ask = await _asking(client, conversation)
        real = settings.write_private

        def broken(path, data):
            real(path, left)
            raise OSError("fsync of the folder failed")
        monkeypatch.setattr(settings, "write_private", broken)
        answered = (await _answer(client, ask, text="2")).json()["budget"]
        monkeypatch.setattr(settings, "write_private", real)
        assert (answered["applied"], answered.get("error"), answered["limit_usd"]) == (False, "not_saved", 1.0)
        again = await asked(client, conversation)
        while again["ask_id"] == ask["ask_id"]:
            assert not sending.done(), (await sending)[-1]
            await asyncio.sleep(0.02)
            again = await asked(client, conversation)
        assert (again["params"]["budget"], again["params"].get("unread")) == ("project", True)
        assert client.provider.answers == []  # the step never went out
        spend = (await client.get(f"/api/projects/{project}/spend", params={"conversation_id": conversation})).json()
        assert spend["project"]["limit_usd"] is None
        assert (await _answer(client, again, option="continue")).json()["budget"]["applied"] is False
        decided = (await _decisions(client, again["run_id"]))[-1]
        assert (decided["from_usd"], decided["to_usd"], decided["applied"]) == (None, None, False)  # unknown, never $0
        third = await asked(client, conversation)
        while third["ask_id"] == again["ask_id"]:
            await asyncio.sleep(0.02)
            third = await asked(client, conversation)
        assert third["params"].get("unread") is True and client.provider.answers == []
        await _answer(client, third, option="stop")
        stream = await sending
        assert (stream[-1]["status"], stream[-1]["cancel_reason"]) == ("cancelled", "limit")
        assert client.provider.answers == []
        from backend import runs
        assert (await asyncio.to_thread(runs._budgets_now, tmp_path / "data", project))[0] == 0.0


@pytest.mark.asyncio
async def test_a_limit_typed_while_the_project_budget_cannot_be_read_is_saved_and_recorded_from_an_unknown_limit(tmp_path):
    # Fix batch 3 internal review (P3-1): the file is valid TOML but its budget is not a number, so the loader gives the
    # default; the step asks, saying the budget could not be read; a typed limit is saved over it, and the records say
    # it came from an unknown limit, never from $0.
    import json
    from scholia_app import send
    from test_agent_loop import asked
    async with await _app(tmp_path) as client:
        conversation, project = await _conversation(client)
        config = tmp_path / "data" / "projects" / project / "config.toml"
        config.parent.mkdir(parents=True, exist_ok=True)
        config.write_text('[project]\nbudget_usd = "abc"\n')
        sending = asyncio.create_task(send(client, conversation, "Short?"))
        ask = await asked(client, conversation)
        assert (ask["params"]["budget"], ask["params"].get("unread")) == ("project", True)
        assert (await _answer(client, ask, text="3")).json()["budget"] == {"applied": True, "budget": "project",
                                                                             "limit_usd": 3.0}
        assert (await sending)[-1]["status"] == "succeeded"
        assert await _project_budget(client, project) == 3.0
        assert [(d["from_usd"], d["to_usd"], d["applied"]) for d in await _decisions(client, ask["run_id"])] == [
            (None, None, False), (None, 3.0, True)]  # decided, then saved
        audited = [json.loads(d) for (d,) in await _rows(client, "SELECT data FROM audit_log WHERE event = 'budget_decision'")]
        assert [(a["from_usd"], a["to_usd"], a["applied"]) for a in audited] == [(None, 3.0, True)]


@pytest.mark.asyncio
@pytest.mark.parametrize("left", UNREAD[1:])
async def test_a_project_budget_its_valid_toml_does_not_give_is_no_room_for_any_reader(tmp_path, left):
    # PR 52 cycle 4 (G2, E4's class, cycle 4 A's probes): [project] not a table, or the budget a table, gives the loader's
    # $50 default. Every reader takes it as a budget the file does not give: no room for any admission (_budgets_now)
    # nor for a judge's unspent held again; no project notice against $50; a Continue decided at $50 applies nothing;
    # /spend shows no project limit.
    from types import SimpleNamespace
    from backend import budgets, runs
    from backend.db import utc_now
    from scholia_app import background_idle, send
    data = tmp_path / "data"
    async with await _app(tmp_path) as client:
        conversation, project = await _conversation(client)
        run_id = (await send(client, conversation, "Short?"))[0]["run_id"]
        await background_idle(client)
        await _spent(client, project, 30.0)  # 60% of the $50 default
        config = data / "projects" / project / "config.toml"
        config.parent.mkdir(parents=True, exist_ok=True)
        config.write_bytes(left)
        assert (await asyncio.to_thread(runs._budgets_now, data, project))[0] == 0.0
        answered = new_id()  # an answer's $1 reservation, $0.10 of it spent: $0.90 to hold again for its judge
        await asyncio.to_thread(client.state["db"].write, lambda conn: conn.execute(
            "INSERT INTO budget_reservations (id, run_id, step_seq, paying_conversation_id, project_id, estimate_usd,"
            " status, settled_usd, basis, settled_at) VALUES (?, ?, 9, ?, ?, 1.0, 'settled', 0.1, 'reported', ?)",
            (answered, run_id, conversation, project, utc_now())))
        write = client.state["db"].write
        assert await asyncio.to_thread(write, lambda conn: runs._hold_unspent(
            conn, SimpleNamespace(reservation_id=answered), {}, data)) is None
        assert await asyncio.to_thread(write, lambda conn: budgets.notices(conn, run_id, data)) == []
        ask = {"ask_id": new_id(), "project_id": project,
               "params": {"form": "cap", "budget": "project", "limit_usd": 50.0, "spent_usd": 50.0, "estimate_usd": 0.01}}
        assert await asyncio.to_thread(write, lambda conn: budgets.decide(conn, run_id, ask, "continue", None, data)) == (
            {"applied": False, "budget": "project", "limit_usd": None}, None)
        spend = (await client.get(f"/api/projects/{project}/spend", params={"conversation_id": conversation})).json()
        assert spend["project"]["limit_usd"] is None


@pytest.mark.asyncio
async def test_an_evaluation_project_whose_budget_its_file_does_not_give_stops_at_the_limit_and_sends_nothing(tmp_path):
    # PR 52 cycle 4 (G2, P2): its mark makes no budget ask, and the loader's $50 default for a [project] that is not a
    # table is no room: the turn stops at the limit, and nothing is sent.
    from scholia_app import send
    async with await _app(tmp_path) as client:
        project = (await client.post("/api/projects", json={"name": "Evaluation", "evaluation": True})).json()["id"]
        conversation = (await client.post("/api/conversations", json={"project_id": project, "title": "t"})).json()["id"]
        (tmp_path / "data" / "projects" / project / "config.toml").write_text('project = "broken"\n')
        stream = await send(client, conversation, "Short?")
        assert {"type": "limit_reached", "budget": "project"} in stream and stream[-1]["cancel_reason"] == "limit"
        assert not [e for e in stream if e["type"] == "ask"] and client.provider.answers == []


# S1-33 fix batch 2 (PR 52 cycle 2 on 4769f6e): lifecycle first (charter section 10)


async def _project_conversation(client, budget=None):
    """A research project (its budget set, if given) and a titled conversation in it: (conversation, project)."""
    project = (await client.post("/api/projects", json={"name": "Thesis"})).json()["id"]
    conversation = (await client.post("/api/conversations", json={"project_id": project, "title": "t"})).json()["id"]
    if budget is not None:
        await _project_budget(client, project, budget)
    return conversation, project


@pytest.mark.asyncio
@pytest.mark.parametrize("meanwhile", ["settings_edit", "project_deleted"])
async def test_a_cancelled_budget_answer_keeps_its_locks_until_its_settings_write_ends(tmp_path, monkeypatch, meanwhile):
    # D1 (A4, L3, L5): the request answering a project budget ask is cancelled while its config.toml write runs in its
    # worker. The project-files and settings locks stay held until that write and its record end, so a Settings edit
    # made meanwhile is never overwritten by it (it is refused as changed since read), and a project deleted
    # meanwhile is deleted after it, its folder never recreated.
    import contextlib
    import threading
    from backend import asks
    async with await _app(tmp_path) as client:
        conversation, project = await _project_conversation(client, budget=1.0)
        await _spent(client, project, 1.0)
        read = (await client.get("/api/settings", params={"project_id": project})).json()
        sending, ask = await _asking(client, conversation)
        entered, release, saving = threading.Event(), threading.Event(), asks._save_project_budget

        def held(data_dir, decided):
            entered.set()
            release.wait(10)
            return saving(data_dir, decided)
        monkeypatch.setattr(asks, "_save_project_budget", held)
        answering = asyncio.create_task(_answer(client, ask, option="continue"))
        try:
            assert await asyncio.to_thread(entered.wait, 10)
            answering.cancel()
            if meanwhile == "settings_edit":
                other = asyncio.create_task(client.put("/api/settings", json={
                    "project_id": project, "hash": read["hash"], "updates": {"project.budget_usd": 5}}))
            else:
                other = asyncio.create_task(client.delete(f"/api/projects/{project}", params={"purge_backups": True}))
            await asyncio.sleep(0.3)
            waited = not other.done()  # it waits for the answer's write, which still holds the locks
        finally:
            release.set()
        with contextlib.suppress(asyncio.CancelledError, Exception):
            await answering
        done = await other
        if not waited:
            sending.cancel()
        assert waited
        if meanwhile == "settings_edit":
            assert (done.status_code, done.json()["code"]) == (409, "settings_changed")  # never overwritten unseen
            assert await _project_budget(client, project) == 2.0
            assert (await sending)[-1]["status"] == "succeeded"  # the answer woke its run
        else:
            assert done.status_code == 200, done.text
            assert not (tmp_path / "data" / "projects" / project).exists()
            with contextlib.suppress(Exception):
                await sending


@pytest.mark.asyncio
@pytest.mark.parametrize("ending", ["stop", "recovery"])
async def test_a_settlement_on_stop_or_recovery_records_the_notices_it_reaches(tmp_path, ending):
    # D5 (N2, reading 8): a call stopped in flight settles at its estimate, and an open reservation a crash left settles
    # at startup: each settlement records the budget notices it reaches, as a settlement with usage does; the Stop's
    # are streamed as `budget` too.
    from scholia_app import MockProvider, send
    release = asyncio.Event()

    async def held(body):
        await release.wait()
        return provider.answer("Late.")
    provider = MockProvider(held) if ending == "stop" else MockProvider()
    async with await _app(tmp_path, provider) as client:
        conversation, project = await _conversation(client, budget=1.0)
        await _spent(client, project, 0.49, conversation)
        if ending == "stop":
            sending = asyncio.create_task(send(client, conversation, "Short?"))
            while not provider.answers:  # its call is in flight
                await asyncio.sleep(0.01)
            run_id = (await _rows(client, "SELECT id FROM runs WHERE conversation_id = ? AND status = 'running'",
                                  conversation))[0][0]
            assert (await client.post(f"/api/runs/{run_id}/cancel")).status_code == 200
            release.set()
            stream = await sending
            assert ("conversation", 50) in [(e["budget"], e["percent"]) for e in stream if e["type"] == "budget"]
        else:
            run_id = (await send(client, conversation, "Short?"))[0]["run_id"]

            def as_crashed(conn):  # running, as a crash left it, with a call in flight: $0.05 reserved, no usage
                conn.execute("UPDATE runs SET status = 'running', finished_at = NULL WHERE id = ?", (run_id,))
                conn.execute("INSERT INTO budget_reservations (id, run_id, step_seq, paying_conversation_id, project_id,"
                             " estimate_usd) VALUES (?, ?, 9, ?, ?, 0.05)", (new_id(), run_id, conversation, project))
            harness = client.state["harness"]
            harness.registry.finished.discard(run_id)
            await asyncio.to_thread(client.state["db"].write, as_crashed)
            await harness.recover(kick=False)
        noticed = await _rows(client, "SELECT json_extract(data, '$.budget'), json_extract(data, '$.percent')"
                                      " FROM run_events WHERE run_id = ? AND type = 'budget_notice'", run_id)
        assert ("conversation", 50) in noticed


@pytest.mark.asyncio
async def test_the_notices_a_recorded_settlement_reached_are_streamed_when_stop_lands_as_its_client_closes(
        tmp_path, monkeypatch):
    # PR 52 cycle 4 (G5, N2; cycle 4 A's probe): both budgets at $0.49 of $1, and a $0.02 reply, whose receipt records
    # both 50% notices; Stop lands while the call's client closes, once that receipt committed. Both are streamed as
    # `budget` all the same (the turn's cleanup finds the call recorded, with nothing more to notice).
    from backend import outbound_gate
    from scholia_app import MockProvider, send
    answered = asyncio.Event()

    def reply(body):
        answered.set()
        return provider.answer("Hi.", cost=0.02)
    provider = MockProvider(reply)
    real = outbound_gate._AsyncTransport.aclose

    async def closing(self):
        await real(self)
        harness = client.state["harness"]
        if answered.is_set() and (active := harness.registry.turns.get(conversation)) is not None:
            harness._request_cancel(active, "researcher")
            await asyncio.sleep(0)  # the Stop lands here, the call's receipt committed
    monkeypatch.setattr(outbound_gate._AsyncTransport, "aclose", closing)
    async with await _app(tmp_path, provider) as client:
        conversation, project = await _conversation(client, budget=1.0)
        await _project_budget(client, project, 1.0)
        await _spent(client, project, 0.49, conversation)
        stream = await send(client, conversation, "Short?")
        assert stream[-1]["status"] == "cancelled"
        noticed = await _rows(client, "SELECT json_extract(data, '$.budget'), json_extract(data, '$.percent')"
                                      " FROM run_events WHERE type = 'budget_notice'")
        assert sorted(noticed) == [("conversation", 50), ("project", 50)]
        assert sorted((e["budget"], e["percent"]) for e in stream if e["type"] == "budget") == sorted(noticed)


# S1-33 fix batch 2: Continue's amount, the notices' record and the spend's bases


@pytest.mark.asyncio
@pytest.mark.parametrize("edits, after", [((5.0, 2.0), 4.0), ((2.0,), 3.0)])
async def test_a_settings_edit_makes_the_project_limit_continues_amount_even_back_at_the_last_decided_value(
        tmp_path, edits, after):
    # D3 (A5, reading 3): $1, Continue to $2, then $5 and $2 set in Settings: $2 is now the researcher's own setting, so
    # the next Continue adds $2 (to $4), not the $1 the old decision added. Saving the $2 unchanged changes nothing, so
    # the decision's $1 stays (to $3): Harold, 2026-11-12, design review C3's Option 2, which decides from what the save
    # changes (batches 5 and 6 took it out on any save of the budget, to $4).
    async with await _app(tmp_path) as client:
        conversation, project = await _conversation(client)
        await _project_budget(client, project, 1.0)
        await _spent(client, project, 1.0)
        sending, ask = await _asking(client, conversation)
        assert (await _answer(client, ask, option="continue")).json()["budget"]["limit_usd"] == 2.0
        await sending
        for usd in edits:
            await _project_budget(client, project, usd)
        [(spent,)] = await _rows(client, "SELECT sum(settled_usd) FROM budget_reservations WHERE project_id = ?", project)
        await _spent(client, project, round(2.0 - spent, 9))  # the cap again
        sending, ask = await _asking(client, conversation, "Again?")
        assert ask["params"]["form"] == "cap"
        assert (await _answer(client, ask, option="continue")).json()["budget"]["limit_usd"] == after
        await sending


@pytest.mark.asyncio
async def test_a_settings_edit_cancelled_once_its_file_is_written_still_makes_the_limit_continues_amount(
        tmp_path, monkeypatch):
    # S1-33 fix batch 3 (E3, reading 3): as D3's test, but each Settings save is cancelled once its config.toml write
    # has completed. What the old decision left for Continue goes in that same write (design review C's Option 2), so
    # it is never skipped: the next Continue adds $2 (to $4), not the $1 the old decision added.
    import contextlib
    import threading
    from backend import settings as settings_module
    real_save, written, release = settings_module.Settings.save, threading.Event(), threading.Event()

    def saving(self, updates, answer=False):
        result = real_save(self, updates, answer)
        if "project.budget_usd" in updates and armed:
            written.set()
            release.wait(10)  # the request is cancelled here, its file already written
        return result
    armed = False
    monkeypatch.setattr(settings_module.Settings, "save", saving)
    async with await _app(tmp_path) as client:
        conversation, project = await _conversation(client)
        await _project_budget(client, project, 1.0)
        await _spent(client, project, 1.0)
        sending, ask = await _asking(client, conversation)
        assert (await _answer(client, ask, option="continue")).json()["budget"]["limit_usd"] == 2.0
        await sending
        armed = True
        for usd in (5.0, 2.0):
            read = (await client.get("/api/settings", params={"project_id": project})).json()
            written.clear()
            release.clear()
            editing = asyncio.create_task(client.put("/api/settings", json={
                "project_id": project, "hash": read["hash"], "updates": {"project.budget_usd": usd}}))
            assert await asyncio.to_thread(written.wait, 10)
            editing.cancel()
            release.set()
            with contextlib.suppress(asyncio.CancelledError):
                await editing
            assert await _project_budget(client, project) == usd  # its file was written
        armed = False
        [(spent,)] = await _rows(client, "SELECT sum(settled_usd) FROM budget_reservations WHERE project_id = ?", project)
        await _spent(client, project, round(2.0 - spent, 9))  # the cap again
        sending, ask = await _asking(client, conversation, "Again?")
        assert ask["params"]["form"] == "cap"
        assert (await _answer(client, ask, option="continue")).json()["budget"]["limit_usd"] == 4.0
        await sending


@pytest.mark.asyncio
async def test_a_settings_save_over_a_budget_its_file_did_not_give_makes_the_limit_continues_amount(tmp_path):
    # PR 52 cycle 4 (G3, reading 3; cycle 4 A's probe): $25, Continue to $50 (adding $25), the budget then made invalid
    # by hand, and set to $50 in Settings. The save compares with what the file held, which gave no budget, never with
    # the loader's $50 default: $50 is a limit set there, so the next Continue adds $50 (to $100), not the old $25.
    import re
    async with await _app(tmp_path) as client:
        conversation, project = await _conversation(client)
        await _project_budget(client, project, 25.0)
        await _spent(client, project, 25.0)
        sending, ask = await _asking(client, conversation)
        assert (await _answer(client, ask, option="continue")).json()["budget"]["limit_usd"] == 50.0
        await sending
        config = tmp_path / "data" / "projects" / project / "config.toml"
        config.write_text(re.sub(r"budget_usd = .*", 'budget_usd = "abc"', config.read_text()))
        await _project_budget(client, project, 50.0)
        [(spent,)] = await _rows(client, "SELECT sum(settled_usd) FROM budget_reservations WHERE project_id = ?", project)
        await _spent(client, project, round(50.0 - spent, 9))  # the cap again
        sending, ask = await _asking(client, conversation, "Again?")
        assert ask["params"]["form"] == "cap"
        assert (await _answer(client, ask, option="continue")).json()["budget"]["limit_usd"] == 100.0
        await sending


# PR 52 cycle 5 (A-F1; Harold, 2026-11-12, design review C's Option 2): what Continue adds to the project budget is in its
# config.toml beside the limit (budget_continue_usd, written for the limit budget_continue_at_usd), in the same file
# replace as the limit, and a Settings save that changes the limit takes it out (batch 7). Each case starts at $25,
# Continued to $50 (adding $25).


def _config(tmp_path, project):
    return tmp_path / "data" / "projects" / project / "config.toml"


def _hand_set(tmp_path, project, text):
    """A hand edit of the project budget's line in its config.toml."""
    import re
    config = _config(tmp_path, project)
    config.write_text(re.sub(r"(?m)^budget_usd = .*$", f"budget_usd = {text}", config.read_text()))


async def _continue_at_cap(client, conversation, project, limit, text="Again?"):
    """Spend up to the project's limit, answer its cap ask Continue, and return the limit it gives."""
    [(spent,)] = await _rows(client, "SELECT coalesce(sum(settled_usd), 0) FROM budget_reservations WHERE project_id = ?",
                             project)
    await _spent(client, project, round(limit - spent, 9))
    sending, ask = await _asking(client, conversation, text)
    assert ask["params"]["form"] == "cap"
    answered = (await _answer(client, ask, option="continue")).json()["budget"]
    await sending
    return answered["limit_usd"]


async def _at_fifty(client):
    """A conversation in the General project, whose budget $25 was Continued to $50: (conversation, project)."""
    conversation, project = await _conversation(client)
    await _project_budget(client, project, 25.0)
    assert await _continue_at_cap(client, conversation, project, 25.0, "Short?") == 50.0
    return conversation, project


@pytest.mark.asyncio
@pytest.mark.parametrize("by_hand", ["30", '"abc"'])
async def test_a_settings_save_that_fails_after_replacing_the_file_and_is_saved_again_makes_the_limit_continues_amount(
        tmp_path, monkeypatch, by_hand):
    # A-F1, both forms: the budget set by hand to $30 (or made invalid), then $50 saved in Settings, whose folder's sync
    # fails once the file was replaced, and saved again: $50 is the researcher's setting, so Continue adds $50.
    from backend import settings
    async with await _app(tmp_path) as client:
        conversation, project = await _at_fifty(client)
        _hand_set(tmp_path, project, by_hand)
        real = settings.write_private

        def replaced_then_failed(path, data):
            real(path, data)
            raise OSError("fsync of the folder failed")
        monkeypatch.setattr(settings, "write_private", replaced_then_failed)
        read = (await client.get("/api/settings", params={"project_id": project})).json()
        with pytest.raises(OSError):  # the save fails (the in-process client raises what the app did not answer)
            await client.put("/api/settings", json={"project_id": project, "hash": read["hash"],
                                                    "updates": {"project.budget_usd": 50.0}})
        monkeypatch.setattr(settings, "write_private", real)
        assert await _project_budget(client, project) == 50.0  # its file was replaced
        await _project_budget(client, project, 50.0)  # saved again
        assert await _continue_at_cap(client, conversation, project, 50.0) == 100.0


@pytest.mark.asyncio
async def test_a_budget_answer_whose_record_fails_after_its_file_write_still_never_compounds(tmp_path, monkeypatch):
    # Design review C (the ask's side): the answer's file write succeeds and its record after it fails; the amount was
    # written with the limit, so the next Continue adds $25 (to $75), never the $50 limit (to $100).
    from backend import asks
    async with await _app(tmp_path) as client:
        conversation, project = await _conversation(client)
        await _project_budget(client, project, 25.0)

        def failing(conn, run_id, decided, saved):
            raise RuntimeError("the record could not be written")
        monkeypatch.setattr(asks, "_budget_saved", failing)
        assert await _continue_at_cap(client, conversation, project, 25.0, "Short?") == 50.0
        monkeypatch.undo()
        assert await _continue_at_cap(client, conversation, project, 50.0) == 75.0


@pytest.mark.asyncio
@pytest.mark.parametrize("edits, after", [(("80",), 160.0), (("80", "50"), 75.0)])
async def test_a_hand_edit_of_the_limit_makes_it_the_amount_but_one_back_to_the_same_value_brings_the_old_amount_back(
        tmp_path, edits, after):
    # Design review C: a hand edit to $80 is the researcher's own limit, so Continue adds $80 (to $160). One away and back
    # to $50 (read in between) brings the $25 back (to $75): the accepted residual (Harold, 2026-11-12), since no design
    # keeping the limit in a hand-editable file can tell it from no edit at all.
    async with await _app(tmp_path) as client:
        conversation, project = await _at_fifty(client)
        for usd in edits:
            _hand_set(tmp_path, project, usd)
            assert (await client.get(f"/api/projects/{project}/spend")).json()["project"]["limit_usd"] == float(usd)
        assert await _continue_at_cap(client, conversation, project, float(edits[-1])) == after


@pytest.mark.asyncio
async def test_a_deleted_project_config_takes_what_continue_adds_with_it(tmp_path):
    # Design review C: config.toml deleted, the limit is the shipped $50 and nothing beside it: Continue adds $50.
    async with await _app(tmp_path) as client:
        conversation, project = await _at_fifty(client)
        _config(tmp_path, project).unlink()
        assert await _project_budget(client, project) == 50
        assert await _continue_at_cap(client, conversation, project, 50.0) == 100.0


def test_a_backup_keeps_what_continue_adds_beside_the_limit():
    from backend.settings import known_only
    import tomllib
    kept = known_only(b"[project]\nbudget_usd = 50.0\nbudget_continue_usd = 25.0\nbudget_continue_at_usd = 50.0\n")
    assert tomllib.loads(kept.decode())["project"] == {"budget_usd": 50.0, "budget_continue_usd": 25.0,
                                                       "budget_continue_at_usd": 50.0}


async def _saved(client, project, updates):
    """A Settings save of the project's config.toml as just read: the response."""
    read = (await client.get("/api/settings", params={"project_id": project})).json()
    return await client.put("/api/settings", json={"project_id": project, "hash": read["hash"], "updates": updates})


# PR 52 batch 7 (rule 1 recheck B7-1; Harold, 2026-11-12, design review C3's Option 2): Settings.save decides from the
# file before and the bytes it is about to write, read back, never from how a save names its keys. A changed limit
# takes what Continue adds out in the same replace; what Continue adds changed any other way is refused, nothing
# written; anything else is written.


@pytest.mark.asyncio
@pytest.mark.parametrize("updates", [
    # The pair alone, without the budget (design review C2: to $1,050, $550, $57).
    {"project.budget_continue_usd": 1000.0, "project.budget_continue_at_usd": 50.0},
    {"project": {"budget_continue_usd": 1000.0, "budget_continue_at_usd": 50.0}},
    {"project.budget_continue_usd": 500.0},
    {'project."budget_continue_usd"': 7.0},
    {'project."budget\\u005Fcontinue_usd"': 7.0},
    # Each key removed alone (to $100).
    {"project.budget_continue_usd": None},
    {"project.budget_continue_at_usd": None},
])
async def test_a_settings_save_changing_what_continue_adds_alone_is_refused_and_writes_nothing(tmp_path, updates):
    # A project Settings save that changes budget_continue_usd or budget_continue_at_usd and not the limit is refused
    # before anything is written, so the next Continue still adds the $25 (to $75). As in batch 6.
    async with await _app(tmp_path) as client:
        conversation, project = await _at_fifty(client)
        before = _config(tmp_path, project).read_bytes()
        response = await _saved(client, project, updates)
        assert (response.status_code, response.json()["code"]) == (400, "invalid_setting")
        assert _config(tmp_path, project).read_bytes() == before
        assert await _continue_at_cap(client, conversation, project, 50.0) == 75.0


@pytest.mark.asyncio
@pytest.mark.parametrize("updates", [
    # The rule 1 recheck's B7-1: a child of the budget that does not exist, removed, in each of its spellings.
    {"project.budget_usd.unused": None},
    {'"project" . "budget_usd" . "unused"': None},
    {'project."budget\\u005Fusd".unused': None},
    {"project.budget_usd.unused.more": None},
    {"project.budget_continue_usd.unused": None},  # a child of what Continue adds (batch 6: 400)
    # The budget saved unchanged, as Enter on the Settings page does, in each spelling (batch 6: to $100).
    *({key: 50.0} for key in ("project.budget_usd", '"project".budget_usd', "project . budget_usd",
                              'project."budget\\u005Fusd"')),
    {"project": {"budget_usd": 50.0}},
    {"project.template": "x"},  # another key
    # Cycle 6's A-F1 probe with the same values: the pair taken out, then put back by a later table, or by quoted dotted
    # aliases (batch 6: 400).
    {"project.budget_continue_usd": None, "project.budget_continue_at_usd": None,
     "project": {"budget_usd": 50.0, "budget_continue_usd": 25.0, "budget_continue_at_usd": 50.0}},
    {"project.budget_continue_usd": None, "project.budget_continue_at_usd": None, "project.budget_usd": 50.0,
     '"project".budget_continue_usd': 25.0, '"project".budget_continue_at_usd': 50.0},
    # The budget with an aliased pair, all as they were (batch 6: 400).
    {"project.budget_usd": 50.0, '"project".budget_continue_usd': 25.0, '"project".budget_continue_at_usd': 50.0},
    None,  # the whole of values.project, as read after the Continue, saved back (batch 6: 400)
])
async def test_a_settings_save_leaving_the_limit_and_what_continue_adds_as_they_were_keeps_what_continue_adds(
        tmp_path, updates):
    # A save that changes neither the limit nor what Continue adds, however it names them, is written, and the next
    # Continue still adds the $25 (to $75).
    async with await _app(tmp_path) as client:
        conversation, project = await _at_fifty(client)
        if updates is None:
            updates = {"project": (await client.get("/api/settings", params={"project_id": project})).json()["values"]
                       ["project"]}
        assert (response := await _saved(client, project, updates)).status_code == 200, response.text
        assert await _continue_at_cap(client, conversation, project, 50.0) == 75.0


@pytest.mark.asyncio
async def test_a_settings_save_of_what_continue_adds_is_refused_in_a_new_project(tmp_path):
    # Design review C2: a new project at $25, never Continued, given $0.01 for that limit would add $0.01 (to $25.01).
    async with await _app(tmp_path) as client:
        project = (await client.post("/api/projects", json={"name": "P"})).json()["id"]
        conversation = (await client.post("/api/conversations", json={"title": "Named", "project_id": project})).json()["id"]
        await _project_budget(client, project, 25.0)
        response = await _saved(client, project, {"project.budget_continue_usd": 0.01,
                                                  "project.budget_continue_at_usd": 25.0})
        assert (response.status_code, response.json()["code"]) == (400, "invalid_setting")
        assert await _continue_at_cap(client, conversation, project, 25.0, "Short?") == 50.0


@pytest.mark.asyncio
@pytest.mark.parametrize("updates, limit", [
    *(({key: 60.0}, 60.0) for key in ("project.budget_usd", '"project".budget_usd', "project . budget_usd",
                                       'project."budget\\u005Fusd"')),
    ({"project": {"budget_usd": 60.0}}, 60.0),
    # The budget changed with an aliased pair: the pair goes with the limit, never adding $1,000 (batch 6: 400).
    ({"project.budget_usd": 60.0, '"project".budget_continue_usd': 1000.0, '"project".budget_continue_at_usd': 60.0},
     60.0),
    ({"project.budget_usd": None}, 50.0),  # reset to the shipped $50
    ({"project": None}, 50.0),  # the whole table removed, what Continue adds with it
])
async def test_a_settings_save_changing_the_limit_takes_what_continue_adds_out_with_it(tmp_path, updates, limit):
    # A5, reading 3: a limit changed in Settings, in any spelling, or reset, is the researcher's own, so the next
    # Continue adds that limit (to twice it), not the $25.
    async with await _app(tmp_path) as client:
        conversation, project = await _at_fifty(client)
        assert (response := await _saved(client, project, updates)).status_code == 200, response.text
        assert await _continue_at_cap(client, conversation, project, limit) == 2 * limit


@pytest.mark.asyncio
async def test_a_settings_save_changing_nothing_while_the_cap_ask_waits_keeps_what_its_continue_adds(tmp_path):
    # B7-1 during an open ask, with a note and a queued message waiting: the save removing a child of the budget that
    # does not exist is written and leaves the ask open, and its Continue adds the $25 (to $75; batch 6: to $100).
    from scholia_app import MockProvider
    from test_agent_loop import asked
    async with await _app(tmp_path, MockProvider(cost=0)) as client:
        conversation, project = await _at_fifty(client)
        [(spent,)] = await _rows(client, "SELECT sum(settled_usd) FROM budget_reservations WHERE project_id = ?", project)
        await _spent(client, project, round(50.0 - spent, 9))
        sending, ask = await _asking(client, conversation, "Again?")
        assert ask["params"]["form"] == "cap"
        assert (await client.post(f"/api/runs/{ask['run_id']}/steering",
                                  json={"content": "Only recent papers"})).status_code == 200
        queued = (await client.post(f"/api/conversations/{conversation}/queue", json={"content": "Next question"})).json()
        assert (response := await _saved(client, project, {"project.budget_usd.unused": None})).status_code == 200, \
            response.text
        assert (await asked(client, conversation))["ask_id"] == ask["ask_id"]
        assert (await _answer(client, ask, option="continue")).json()["budget"]["limit_usd"] == 75.0
        assert (await asyncio.wait_for(sending, 10))[-1]["status"] == "succeeded"
        read = (await client.get(f"/api/conversations/{conversation}")).json()
        assert read["queued"] == [queued] and read["turns"][-1]["steering"][0]["state"] == "read"


@pytest.mark.asyncio
async def test_a_settings_save_that_would_move_what_continue_adds_in_a_hand_written_layout_is_refused(tmp_path):
    # Design review C3: the project's keys written by hand as dotted keys at the root, $25 Continued to $50. A save adding
    # a table there makes tomlkit write its header among those keys, so the keys after it would move under it, what
    # Continue adds among them: 400, nothing written, and Continue still adds $25 (to $75; batch 6: 200, to $100).
    async with await _app(tmp_path) as client:
        conversation, project = await _conversation(client)
        await _project_budget(client, project, 25.0)
        _config(tmp_path, project).write_text('project.budget_usd = 25.0\nproject.template = "imrad"\n')
        assert await _continue_at_cap(client, conversation, project, 25.0, "Short?") == 50.0
        before = _config(tmp_path, project).read_bytes()
        response = await _saved(client, project, {"project.unused": {}})
        assert (response.status_code, response.json().get("code")) == (400, "invalid_setting")
        assert _config(tmp_path, project).read_bytes() == before
        assert await _continue_at_cap(client, conversation, project, 50.0) == 75.0


_PAIRED = b"[project]\nbudget_usd = 50.0\nbudget_continue_usd = 25.0\nbudget_continue_at_usd = 50.0\n"


def _project_file(tmp_path, monkeypatch, raw):
    """A project's config.toml holding raw, and the list each later write_private call appends its bytes to."""
    from backend import settings
    real, written = settings.write_private, []
    real(tmp_path / "projects" / "p" / "config.toml", raw)
    monkeypatch.setattr(settings, "write_private", lambda path, data: (written.append(data), real(path, data)))
    return written


@pytest.mark.parametrize("raw, updates, kept", [
    (_PAIRED, {"project.template": "x"}, {"budget_usd": 50.0, "budget_continue_usd": 25.0,
                                          "budget_continue_at_usd": 50.0, "template": "x"}),
    (_PAIRED, {"project.budget_usd": 50}, {"budget_usd": 50, "budget_continue_usd": 25.0,
                                           "budget_continue_at_usd": 50.0}),  # 50 is the same limit as 50.0
    (_PAIRED, {"project.budget_usd.unused": None}, tomllib.loads(_PAIRED.decode())["project"]),
    (_PAIRED, {"project.budget_usd": 60.0}, {"budget_usd": 60.0}),
    (_PAIRED, {"project.budget_usd": 60.0, "project.budget_continue_usd": 1000.0}, {"budget_usd": 60.0}),
    (_PAIRED, {"project.budget_usd": None}, {}),
    # The same keys written by hand as dotted keys at the root: each is taken out of the table as it is then.
    (b'project.budget_usd = 50.0\nproject.budget_continue_usd = 25.0\nproject.budget_continue_at_usd = 50.0\n'
     b'project.template = "imrad"\n', {"project.budget_usd": 60.0}, {"budget_usd": 60.0, "template": "imrad"}),
    # A limit made invalid by hand, then reset: a change, as setting it is (G3).
    (b'[project]\nbudget_usd = "abc"\nbudget_continue_usd = 25.0\nbudget_continue_at_usd = 50.0\n',
     {"project.budget_usd": None}, {}),
    # A limit of true, given by hand, is no budget: a limit of 1 is a change, never the same as true.
    (b"[project]\nbudget_usd = true\nbudget_continue_usd = 0.5\nbudget_continue_at_usd = 1\n",
     {"project.budget_usd": 1}, {"budget_usd": 1}),
])
def test_a_project_files_save_is_written_with_what_continue_adds_kept_unless_the_limit_changed(
        tmp_path, monkeypatch, raw, updates, kept):
    from backend.settings import load_settings
    written = _project_file(tmp_path, monkeypatch, raw)
    load_settings(tmp_path, "p").save(updates)
    assert len(written) == 1 and tomllib.loads(written[0].decode()).get("project", {}) == kept


@pytest.mark.parametrize("raw, updates", [
    (_PAIRED, {"project.budget_continue_usd": 1000.0}),
    (_PAIRED, {"project.budget_continue_at_usd": None}),
    (_PAIRED, {"project": {"budget_usd": 50.0, "budget_continue_usd": 25.5}}),
    # What Continue adds given as true by hand is none: 1 is a change, never the same as true.
    (b"[project]\nbudget_usd = 1.0\nbudget_continue_usd = true\nbudget_continue_at_usd = 1.0\n",
     {"project.budget_continue_usd": 1}),
])
def test_a_project_files_save_changing_what_continue_adds_alone_raises_and_writes_nothing(
        tmp_path, monkeypatch, raw, updates):
    from backend.settings import load_settings
    written = _project_file(tmp_path, monkeypatch, raw)
    with pytest.raises(ValueError):
        load_settings(tmp_path, "p").save(updates)
    assert written == [] and (tmp_path / "projects" / "p" / "config.toml").read_bytes() == raw


def test_a_budget_answers_save_writes_the_limit_and_what_continue_adds_in_one_file_write(tmp_path, monkeypatch):
    from backend.settings import load_settings
    written = _project_file(tmp_path, monkeypatch, _PAIRED)
    load_settings(tmp_path, "p").save({"project.budget_usd": 75.0, "project.budget_continue_usd": 25.0,
                                       "project.budget_continue_at_usd": 75.0}, answer=True)
    assert len(written) == 1 and tomllib.loads(written[0].decode())["project"] == {
        "budget_usd": 75.0, "budget_continue_usd": 25.0, "budget_continue_at_usd": 75.0}


@pytest.mark.asyncio
async def test_a_budget_answer_whose_file_write_fails_before_the_replace_keeps_the_old_amount(tmp_path, monkeypatch):
    # Design review C: at the $50 cap, Continue's write fails before the file is replaced: nothing is raised, and the
    # file still holds $50 with its $25, so the next Continue adds $25 (to $75).
    import contextlib
    from backend import settings
    async with await _app(tmp_path) as client:
        conversation, project = await _at_fifty(client)
        before = _config(tmp_path, project).read_bytes()

        def failed(path, data):
            raise OSError("disk full")
        monkeypatch.setattr(settings, "write_private", failed)
        [(spent,)] = await _rows(client, "SELECT sum(settled_usd) FROM budget_reservations WHERE project_id = ?", project)
        await _spent(client, project, round(50.0 - spent, 9))
        sending, ask = await _asking(client, conversation, "Again?")
        assert (await _answer(client, ask, option="continue")).json()["budget"]["applied"] is False
        monkeypatch.undo()
        assert _config(tmp_path, project).read_bytes() == before
        sending.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await sending
        assert await _continue_at_cap(client, conversation, project, 50.0, "Once more?") == 75.0


@pytest.mark.asyncio
async def test_a_budget_answer_writes_the_limit_and_what_continue_adds_in_one_file_write(tmp_path, monkeypatch):
    import tomllib
    from backend import settings
    async with await _app(tmp_path) as client:
        conversation, project = await _conversation(client)
        await _project_budget(client, project, 25.0)
        real, written = settings.write_private, []

        def recording(path, data):
            if path == _config(tmp_path, project):
                written.append(data)
            real(path, data)
        monkeypatch.setattr(settings, "write_private", recording)
        assert await _continue_at_cap(client, conversation, project, 25.0, "Short?") == 50.0
        assert len(written) == 1
        table = tomllib.loads(written[0].decode())["project"]
        assert (table["budget_usd"], table["budget_continue_usd"], table["budget_continue_at_usd"]) == (50, 25, 50)


@pytest.mark.asyncio
async def test_a_pair_made_invalid_by_hand_adds_the_limit_and_leaves_the_budget_read(tmp_path):
    # Design review C: what Continue adds made invalid by hand is none, never an unread budget: the bar shows the $50
    # limit, the ask is an ordinary cap ask, and Continue adds the limit (to $100).
    import re
    async with await _app(tmp_path) as client:
        conversation, project = await _at_fifty(client)
        config = _config(tmp_path, project)
        config.write_text(re.sub(r"(?m)^budget_continue_usd = .*$", 'budget_continue_usd = "x"', config.read_text()))
        assert (await client.get(f"/api/projects/{project}/spend")).json()["project"]["limit_usd"] == 50.0
        [(spent,)] = await _rows(client, "SELECT sum(settled_usd) FROM budget_reservations WHERE project_id = ?", project)
        await _spent(client, project, round(50.0 - spent, 9))
        sending, ask = await _asking(client, conversation, "Again?")
        assert not ask["params"].get("unread")
        assert (await _answer(client, ask, option="continue")).json()["budget"]["limit_usd"] == 100.0
        await sending


@pytest.mark.asyncio
async def test_a_stale_typed_answer_that_applied_nothing_never_sets_the_next_continues_amount(tmp_path):
    # D7 (A5, L6): an ask raised at the $1 default, the default then set to $2, and the stale ask answered "100":
    # nothing applied, so at the $2 cap Continue adds $2 (to $4), never the $100 that was not applied.
    async with await _app(tmp_path) as client:
        read = (await client.get("/api/settings")).json()
        await client.put("/api/settings", json={"hash": read["hash"], "updates": {"budget.conversation_usd": 1}})
        conversation, project = await _conversation(client)
        await _spent(client, project, 1.0, conversation)
        sending, ask = await _asking(client, conversation)
        read = (await client.get("/api/settings")).json()
        await client.put("/api/settings", json={"hash": read["hash"], "updates": {"budget.conversation_usd": 2}})
        stale = (await _answer(client, ask, text="100")).json()["budget"]
        assert stale["applied"] is False
        assert (await sending)[-1]["status"] == "succeeded"
        [(spent,)] = await _rows(client, "SELECT sum(settled_usd) FROM budget_reservations WHERE paying_conversation_id = ?",
                                 conversation)
        await _spent(client, project, round(2.0 - spent, 9), conversation)
        sending, ask = await _asking(client, conversation, "Again?")
        assert ask["params"]["form"] == "cap"
        assert (await _answer(client, ask, option="continue")).json()["budget"]["limit_usd"] == 4.0
        await sending


@pytest.mark.asyncio
async def test_a_project_notice_is_recorded_once_whatever_conversation_is_deleted_or_moved(tmp_path):
    # D4 (N2, reading 8): the project's notices are the project's own record, not its runs' events, which go with a
    # conversation deleted or moved: deleting one never rearms a notice, and a moved one never carries a notice into
    # another project (whose spend it does not describe), nor takes one away from its first.
    from scholia_app import MockProvider, send
    async with await _app(tmp_path, MockProvider(cost=0.01)) as client:
        first, project = await _project_conversation(client, budget=1.0)
        other = (await client.post("/api/projects", json={"name": "Other"})).json()["id"]
        await _project_budget(client, other, 1.0)

        async def noticed(where):
            return await _rows(client, "SELECT count(*) FROM run_events e JOIN runs r ON r.id = e.run_id"
                                       " WHERE e.type = 'budget_notice' AND json_extract(e.data, '$.budget') = 'project'"
                                       " AND json_extract(e.data, '$.percent') = 50 AND json_extract(e.data, '$.project_id') = ?", where)
        await _spent(client, project, 0.5)
        stream = await send(client, first, "Short?")
        assert ("project", 50) in [(e["budget"], e["percent"]) for e in stream if e["type"] == "budget"]
        from scholia_app import background_idle
        await background_idle(client)
        moved = await client.post(f"/api/conversations/{first}/move", json={"project_id": other})
        assert moved.status_code == 200, moved.text
        second = (await client.post("/api/conversations", json={"project_id": project, "title": "t"})).json()["id"]
        stream = await send(client, second, "Again?")  # the first project at 52%: its 50% was recorded already
        assert not [e for e in stream if e["type"] == "budget" and e["budget"] == "project"]
        await _spent(client, other, 0.5)
        third = (await client.post("/api/conversations", json={"project_id": other, "title": "t"})).json()["id"]
        stream = await send(client, third, "There?")  # the other project reaches its own 50% now
        assert ("project", 50) in [(e["budget"], e["percent"]) for e in stream if e["type"] == "budget"]
        assert (await client.delete(f"/api/conversations/{third}")).status_code == 200
        await background_idle(client)
        fourth = (await client.post("/api/conversations", json={"project_id": other, "title": "t"})).json()["id"]
        stream = await send(client, fourth, "Still?")  # deleting the conversation that recorded it rearms nothing
        assert not [e for e in stream if e["type"] == "budget" and e["budget"] == "project"]
        assert await noticed(project) == [(1,)]  # the moved conversation's run still says whose notice it recorded
        import json
        kept = dict(await _rows(client, "SELECT id, budget_noticed FROM projects WHERE id IN (?, ?)", project, other))
        assert json.loads(kept[project]) == [[50, 1.0]] and json.loads(kept[other]) == [[50, 1.0]]


@pytest.mark.asyncio
async def test_spend_gives_each_budgets_settled_spend_by_basis(tmp_path):
    # D6 (N3): settled spend split into reported and estimated, for the project and the conversation, beside the totals.
    from backend.db import utc_now
    async with await _app(tmp_path) as client:
        conversation, project = await _conversation(client, budget=2.0)
        await _spent(client, project, 0.1, conversation)
        await asyncio.to_thread(client.state["db"].write, lambda conn: conn.execute(
            "INSERT INTO budget_reservations (id, step_seq, paying_conversation_id, project_id, estimate_usd, status,"
            " settled_usd, basis, settled_at) VALUES (?, 0, ?, ?, 0.045494, 'settled', 0.045494, 'estimated', ?)",
            (new_id(), conversation, project, utc_now())))
        await _spent(client, project, 0.25)  # another conversation's, or a background run's
        spend = (await client.get(f"/api/projects/{project}/spend", params={"conversation_id": conversation})).json()
        assert {k: spend["project"][k] for k in ("settled_usd", "reported_usd", "estimated_usd")} == {
            "settled_usd": 0.395494, "reported_usd": 0.35, "estimated_usd": 0.045494}
        assert {k: spend["conversation"][k] for k in ("settled_usd", "reported_usd", "estimated_usd")} == {
            "settled_usd": 0.145494, "reported_usd": 0.1, "estimated_usd": 0.045494}


# S1-33 fix batch 2 (D20): the checklist's named tests not yet written (B1, P1, L3 d, L4)


@pytest.mark.asyncio
async def test_the_routers_and_compactions_calls_ask_at_a_budget_as_any_step_does(tmp_path):
    # B1: every turn call goes through _reserve: the router's small model (phase route) and compaction (phase compact)
    # ask at the cap like an agent step, and once answered the call is made and the turn goes on.
    from backend import budget_router
    from scholia_app import MockProvider
    from test_compaction import Long, put_settings, small_history
    provider = MockProvider()
    async with await _app(tmp_path, provider) as client:
        await put_settings(client, {"models.router": sorted(budget_router.RECOMMENDED)[0]})
        provider.replies.append(lambda body: provider.answer("none"))
        conversation, _ = await _conversation(client, budget=0)
        sending, ask = await _asking(client, conversation)
        assert (ask["params"]["phase"], ask["params"]["form"]) == ("route", "cap")
        await _answer(client, ask, option="continue")
        assert (await sending)[-1]["status"] == "succeeded"
        [route] = await _rows(client, "SELECT count(*) FROM run_events WHERE run_id = ? AND type = 'step_started'"
                                      " AND json_extract(data, '$.phase') = 'route'", ask["run_id"])
        assert route == (1,)
    provider = Long()
    (tmp_path / "second").mkdir()
    async with await _app(tmp_path / "second", provider) as client:
        from scholia_app import send
        await small_history(client)
        conversation, _ = await _conversation(client)
        for i in range(3):
            assert (await send(client, conversation, f"QUESTION-{i} about cohorts"))[-1]["status"] == "succeeded"
        [(spent,)] = await _rows(client, "SELECT sum(settled_usd) FROM budget_reservations WHERE paying_conversation_id = ?",
                                 conversation)
        await asyncio.to_thread(client.state["db"].write, lambda conn: conn.execute(
            "UPDATE conversations SET budget_usd = ? WHERE id = ?", (spent, conversation)))  # at its cap
        sending, ask = await _asking(client, conversation, "QUESTION-3 about cohorts")
        assert (ask["params"]["phase"], ask["params"]["form"]) == ("compact", "cap")
        await _answer(client, ask, text="5")  # a limit the rest of the turn fits in, so nothing asks again
        stream = await sending
        assert stream[-1]["status"] == "succeeded" and len(provider.summaries) == 1


@pytest.mark.asyncio
async def test_the_evaluation_bound_holds_for_titles_extractions_citation_checks_and_the_judges_share(tmp_path):
    # P1 (SPEC:1223): in the evaluation project every reservation is the request's bound times the attempts one call
    # may make: a title, a memory extraction, a citation check, and the support judge's share held with its answer.
    from backend import openrouter
    from cited import Provider, cited, events_of
    from scholia_app import background_idle, run_finished, send
    from test_materials import added
    from test_search import WAGES, idle
    async with cited(tmp_path, Provider()) as client:
        project = (await client.post("/api/projects", json={"name": "Evaluation", "evaluation": True})).json()["id"]
        await added(client, project, WAGES)
        await idle(client, project)
        conversation = (await client.post("/api/conversations", json={"project_id": project})).json()["id"]  # untitled
        run_id = (await send(client, conversation, "What do minimum wages do to earnings?"))[0]["run_id"]
        await background_idle(client)
        [check] = (await client.post("/api/citations/check", json={"run_id": run_id})).json()["run_ids"]
        assert (await run_finished(client, check))["status"] == "succeeded"
        steps = [(d["phase"], d) for (r,) in await _rows(client, "SELECT id FROM runs WHERE project_id = ?", project)
                 for kind, d, _ in await events_of(client, r) if kind == "step_started"]
        phases = {phase for phase, _ in steps}
        assert {"answer", "judge", "title", "extract_memory", "citation_check"} <= phases, phases
        for phase, step in steps:
            if phase in ("title", "extract_memory", "citation_check", "answer"):
                assert step["estimate_usd"] == pytest.approx(step["request_usd"] * openrouter.MAX_ATTEMPTS), phase
            if phase == "answer":  # the judge's share, held with it, is bounded the same way
                assert step["allowance_usd"] == pytest.approx(step["judge_estimate_usd"] * openrouter.MAX_ATTEMPTS)


async def _paper_turn_at_its_cap(client):
    """A project with one paper read, its budget at the cap, and a turn about it waiting on the cap ask: (project,
    material, the send's task, the ask)."""
    from test_materials import added
    from test_search import WAGES, idle
    conversation, project = await _project_conversation(client)
    [material] = [m["id"] for m in (await added(client, project, WAGES))["materials"]]
    await idle(client, project)
    await _project_budget(client, project, 1.0)
    await _spent(client, project, 1.0)
    sending, ask = await _asking(client, conversation, "What do minimum wages do to earnings?")
    assert ask["params"]["form"] == "cap"
    return project, material, sending, ask


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["paper_deleted", "tightened"])
async def test_a_change_between_a_continue_answer_and_the_resumed_step_sends_nothing(tmp_path, monkeypatch, change):
    # L3 (d), L4: the Continue answer commits; then, before the resumed step is reserved and dispatched, a paper its
    # request carries is deleted, or the project is tightened: the step is never sent and the run ends revoked.
    from backend import runs
    from cited import Provider, cited
    asking, where = runs.Harness._budget_ask, {}

    async def then_change(self, active, *args):
        reply = await asking(self, active, *args)  # the answer has committed: the step is about to be reserved again
        client = where["client"]  # the change runs beside the turn (it waits for the runs it stops to end)
        where["changing"] = asyncio.create_task(
            client.delete(f"/api/materials/{where['material']}") if where["change"] == "paper_deleted"
            else client.post(f"/api/projects/{where['project']}/sensitivity", json={"level": "local_only"}))
        deadline = asyncio.get_running_loop().time() + 10
        while not active.cancel_requested.is_set():  # the change has revoked the run
            assert asyncio.get_running_loop().time() < deadline
            await asyncio.sleep(0.01)
        return reply
    monkeypatch.setattr(runs.Harness, "_budget_ask", then_change)
    async with cited(tmp_path, Provider()) as client:
        where.update(change=change, client=client)
        project, material, sending, ask = await _paper_turn_at_its_cap(client)
        where.update(project=project, material=material)
        before = len(client.provider.answers)
        assert (await _answer(client, ask, option="continue")).status_code == 200
        stream = await sending
        assert (await where["changing"]).status_code == 200
        assert (stream[-1]["status"], stream[-1].get("cancel_reason")) == ("cancelled", "revoked")
        assert len(client.provider.answers) == before  # nothing sent after the change
        assert await _rows(client, "SELECT count(*) FROM budget_reservations WHERE status = 'open'") == [(0,)]


@pytest.mark.asyncio
async def test_a_queued_message_admitted_after_a_tightening_is_built_under_the_new_policy(tmp_path):
    # L4: a message queued under Normal, then the project made Local only, then sent: its turn is admitted under Local
    # only, so nothing goes to a cloud provider.
    from scholia_app import send
    async with await _app(tmp_path) as client:
        conversation, project = await _project_conversation(client)
        await send(client, conversation, "First?")
        queued = (await client.post(f"/api/conversations/{conversation}/queue", json={"content": "Next?"})).json()
        assert (await client.post(f"/api/projects/{project}/sensitivity", json={"level": "local_only"})).status_code == 200
        before = len(client.provider.answers)
        sent = await client.post(f"/api/conversations/{conversation}/queue/{queued['id']}/send", json={})
        assert len(client.provider.answers) == before  # no cloud call under Local only
        if sent.status_code == 200:  # admitted, and refused a route under the new policy
            from scholia_app import events
            assert events(sent)[-1]["status"] != "succeeded"


# PR 52's follow-up, design review V's Option 1 (rule 2, very large values: the first review's A-F1, C-F2, C-F3 and the
# cycles before): an amount is $0 to spending.MAX_USD, $1,000,000. A value from outside above it is not an amount, so a
# single reported cost above it is an invalid report, counted at the estimate and marked estimated, and a limit above it
# is refused where it enters (the settings files, a typed limit, an answer that would raise one past it). One computed
# record (a step's estimate, a call's attempts) is held at the most; every sum of amounts stays far below what a float
# holds, so its cents are exact.

MAX = 1_000_000.0
BIG = 1.7e308  # a float, and no amount


def test_no_value_past_the_most_an_amount_can_be_is_one_and_one_computed_record_is_held_there(db):
    # A-F1's values cannot enter: a spend at what a float holds, a limit just below it, an estimate of 1.7e308
    import math
    import sys
    from backend import budget_router
    assert spending.MAX_USD == MAX and spending.amount(MAX) == MAX
    for value in (MAX + 0.01, 2e6, BIG, sys.float_info.max, math.nextafter(sys.float_info.max, 0)):
        assert spending.amount(value) is None, value
    project = add_project(db)
    conversation, run = add_turn(db, project)
    with pytest.raises(ValueError):
        reserve(db, project, conversation, run, BIG, project_budget=None, conversation_budget=None)
    assert spending.combined(BIG + BIG) == MAX and spending.combined(0.1 + 0.2) == 0.3
    assert budget_router.request_cost({"input": BIG, "output": BIG, "request": 0.0}, 10, 10) == MAX
    assert budget_router.cost_from_usage({"input": 1.0, "output": 0.0, "request": 0.0},
                                         {"prompt_tokens": 10 ** 300, "completion_tokens": 0}) is None
    assert budget_router.cost_from_usage({"input": 0.5, "output": 0.0, "request": 0.0},
                                         {"prompt_tokens": 2_000_000, "completion_tokens": 0}) == MAX


def test_sums_of_amounts_at_the_most_are_exact_and_a_spend_past_a_limit_fits_no_step(db):
    from backend import budgets
    from backend.runs import _accounting
    project = add_project(db)
    conversation, run = add_turn(db, project)
    for step in (0, 1, 2):
        reservation = reserve(db, project, conversation, run, 0.01, step=step, project_budget=None, conversation_budget=None)
        db.write(lambda conn: spending.settle(conn, reservation, MAX))
    reserve(db, project, None, add_turn(db, project)[1], MAX, project_budget=None)  # open, at its estimate
    assert project_spent(db, project) == 4 * MAX
    assert db.read(lambda conn: spending.spent(conn, conversation_id=conversation)) == 3 * MAX
    assert db.read(lambda conn: spending.run_cost(conn, run)) == 3 * MAX
    assert db.read(lambda conn: _accounting(conn, run))["reported_usd"] == 3 * MAX
    assert db.read(lambda conn: budgets.totals(conn, project_id=project)) == (3 * MAX, MAX)
    assert db.read(lambda conn: budgets.bases(conn, project_id=project))["reported_usd"] == 3 * MAX
    with pytest.raises(spending.BudgetExceeded):
        reserve(db, project, conversation, run, 0.01, step=3, project_budget=None, conversation_budget=MAX)


@pytest.mark.parametrize("already, estimate", [(999999.5, 0.4), (123456.789123456, 0.75), (500000.0, 500000.0),
                                               (999999.999999999, 0.000000001), (0.0, MAX)])
def test_the_limit_go_ahead_sets_admits_its_step_at_any_spend_within_the_most(db, already, estimate):
    # A6 (the internal review's F1): Go ahead sets the limit to what the step needs, summed as budgets.decide sums it;
    # admission sums spend and estimate the same way, so that limit admits the step, never asks again
    project = add_project(db)
    conversation, run = add_turn(db, project)
    if already:
        reserve(db, project, conversation, run, already, project_budget=None, conversation_budget=None)
    spent = db.read(lambda conn: spending.spent(conn, conversation_id=conversation))
    needed = round(spent + spending.amount(estimate), 9)
    assert needed <= MAX
    assert reserve(db, project, conversation, run, estimate, step=1, project_budget=None, conversation_budget=needed)


def test_the_limit_go_ahead_sets_admits_a_step_and_its_judges_share_reserved_together_within_the_most(db):
    # The recheck's F1-residual (pre-existing at 87371d7): an answer step reserves its request and its judge's share in
    # one transaction; Go ahead's limit is spent + (request + share), the rows sum as (spent + request) + share. Within
    # the most a limit can be, the step is always admitted.
    import random
    rng = random.Random(7)
    cases = [((333619.774470715,), 4.363519406, 7.995511916)] + [
        (tuple(round(rng.uniform(0, 333333) / 10 ** rng.choice([0, 0, 1, 3]), 9) for _ in range(3)),
         round(rng.uniform(0.000000001, 9), 9), round(rng.uniform(0.000000001, 9), 9)) for _ in range(500)]
    for rows, request, share in cases:
        project = add_project(db)
        conversation, run = add_turn(db, project)
        for step, usd in enumerate(rows):
            reserve(db, project, conversation, run, usd, project_budget=None, conversation_budget=None, step=step)
        spent = db.read(lambda conn: spending.spent(conn, conversation_id=conversation))
        needed = round(spent + round(request + share, 9), 9)  # as budgets.decide sets it

        def both(conn):
            for usd in (request, share):
                spending.reserve(conn, run_id=run, step_seq=9, project_id=project, paying_conversation_id=conversation,
                                 estimate_usd=usd, project_budget_usd=None, conversation_budget_usd=needed)
        db.write(both)  # never BudgetExceeded


def test_a_calls_attempts_settle_as_reported_up_to_the_most_and_an_attempt_reporting_more_counts_its_estimate(db):
    # SPEC:425: two attempts of $1,000,000 settle as reported at the most (one call's record); two that each reported
    # 1.7e308 reported no amount, so the call settles at its estimate, marked estimated, as for any invalid report
    from backend.openrouter import Attempt, ModelResult
    from backend.runs import _settle_attempts
    project = add_project(db)
    conversation, run = add_turn(db, project)
    free = {"input": 0, "output": 0, "request": 0}
    reservation = reserve(db, project, conversation, run, 0.04, project_budget=None, conversation_budget=None)
    attempts = [Attempt("request", 400, 5, {"cost": MAX}), Attempt("ok", 200, 5, {"cost": MAX})]
    db.write(lambda conn: _settle_attempts(conn, reservation, free, attempts))
    assert row(db, reservation)[2:] == ("settled", MAX, "reported")
    assert ModelResult("Hi.", None, None, attempts).reported_cost == MAX
    over = reserve(db, project, conversation, run, 0.04, step=1, project_budget=None, conversation_budget=None)
    attempts = [Attempt("request", 400, 5, {"cost": BIG}), Attempt("ok", 200, 5, {"cost": BIG})]
    db.write(lambda conn: _settle_attempts(conn, over, free, attempts))
    assert row(db, over)[2:] == ("settled", 0.04, "estimated")
    assert ModelResult("Hi.", None, None, attempts).reported_cost is None
    counted = [Attempt("ok", 200, 5, {"prompt_tokens": 10 ** 300, "completion_tokens": 0})]  # tokens priced past it
    other = reserve(db, project, conversation, run, 0.04, step=2, project_budget=None, conversation_budget=None)
    db.write(lambda conn: _settle_attempts(conn, other, {"input": 1e10, "output": 0, "request": 0}, counted))
    assert row(db, other)[2:] == ("settled", 0.04, "estimated")


@pytest.mark.asyncio
async def test_spend_answers_with_exact_totals_when_its_settled_calls_sum_past_the_most_one_can_be(tmp_path):
    # N3 (two settled calls of 1.7e308 made GET /spend answer 500): three of $1,000,000 give $3,000,000, and the limits
    # it gives are those an amount can be
    async with await _app(tmp_path) as client:
        conversation, project = await _conversation(client, budget=10.0)
        await _project_budget(client, project, 5.0)
        for _ in range(3):
            await _spent(client, project, MAX, conversation)
        response = await client.get(f"/api/projects/{project}/spend", params={"conversation_id": conversation})
        assert response.status_code == 200, response.text
        for budget, limit in (("project", 5.0), ("conversation", 10.0)):
            assert {k: response.json()[budget][k] for k in ("limit_usd", "settled_usd", "reported_usd")} == {
                "limit_usd": limit, "settled_usd": 3 * MAX, "reported_usd": 3 * MAX}


@pytest.mark.parametrize("schema, key", [("project", "project.budget_usd"), ("personal", "budget.conversation_usd")])
def test_a_settings_budget_outside_a_billionth_to_the_most_is_refused_and_one_written_by_hand_is_unread(
        tmp_path, schema, key):
    # C-F3's limits cannot enter from a settings file: the Settings page's "A setting is not valid." (400)
    from backend.runs import _budget_unread
    from backend.settings import load_settings
    project_id = "p" if schema == "project" else None
    table, name = key.split(".")
    for bad in (MAX + 0.01, 1e9, BIG, 1e-10):
        with pytest.raises(ValueError):
            load_settings(tmp_path, project_id).save({key: bad})
    for good in (MAX, 1e-9, 25):
        load_settings(tmp_path, project_id).save({key: good})
    path = tmp_path / ("projects/p/config.toml" if project_id else "config.toml")
    path.write_text(f"[{table}]\n{name} = 2000000.0\n")
    assert _budget_unread(load_settings(tmp_path, project_id), table, name)  # E4's: no room, the ask says so


@pytest.mark.asyncio
async def test_notices_follow_the_share_of_a_limit_at_the_most(tmp_path):
    # N2, SPEC:421: 59,000 of $1,000,000 is 5.9%: no notice; 940,000 is 94%: each budget's three, once
    from scholia_app import MockProvider, background_idle, send
    async with await _app(tmp_path, MockProvider(cost=0.002)) as client:
        conversation, project = await _conversation(client, budget=MAX)
        await _project_budget(client, project, MAX)
        await _spent(client, project, 59000.0, conversation)
        stream = await send(client, conversation, "Short?")
        assert stream[-1]["status"] == "succeeded"
        await background_idle(client)  # its memory extraction settles against the project too
        assert [e for e in stream if e["type"] == "budget"] == []
        assert await _rows(client, "SELECT count(*) FROM run_events WHERE type = 'budget_notice'") == [(0,)]
        await _spent(client, project, 881000.0, conversation)
        stream = await send(client, conversation, "Again?")
        assert sorted((e["budget"], e["percent"]) for e in stream if e["type"] == "budget") == [
            (budget, percent) for budget in ("conversation", "project") for percent in (50, 85, 90)]


@pytest.mark.asyncio
async def test_at_the_most_a_limit_can_be_only_stop_ends_the_cap_ask_and_nothing_raises_a_limit_past_it(tmp_path):
    # A5, A7 at the bound: the project at $1,000,000 past its cap. Continue would add $1,000,000 again, and any limit
    # that covers the step is past the most: each is refused with the new text and no needed amount, nothing changes,
    # and Stop ends the turn at its limit
    async with await _app(tmp_path) as client:
        conversation, project = await _conversation(client)
        await _project_budget(client, project, MAX)
        await _spent(client, project, MAX, conversation)
        sending, ask = await _asking(client, conversation)
        assert (ask["params"]["form"], ask["params"]["budget"]) == ("cap", "project")
        for body in ({"option": "continue"}, {"text": "1000000.01"}, {"text": "2000000"}, {"text": "1000000"},
                     {"text": "US$1,000,000.00"}, {"text": "1" * 400}):  # the internal review: digits past a float too
            refused = await _answer(client, ask, **body)
            assert (refused.status_code, refused.json()["code"]) == (400, "limit_too_high"), body
            assert "needed_usd" not in refused.json()
        assert await _project_budget(client, project) == MAX and await _decisions(client, ask["run_id"]) == []
        assert (await _answer(client, ask, option="stop")).status_code == 200
        assert [(e["type"], e.get("status")) for e in await sending][-1] == ("run_finished", "cancelled")
        assert await _project_budget(client, project) == MAX


@pytest.mark.asyncio
async def test_a_step_needing_more_than_the_most_says_no_needed_amount_and_go_ahead_is_refused(tmp_path):
    # C-F2: a step needing 46847608461976.055 had its needed text say a cent too little, so the amount typed as said was
    # refused again. Such a spend is stored only from before the bound (stored values are not checked again on read):
    # no limit can cover the step, so a typed limit is refused with no needed amount. And a step ask whose Go ahead
    # would set a limit past the most is refused too; Not now ends it.
    async with await _app(tmp_path) as client:
        conversation, project = await _conversation(client)
        await _project_budget(client, project, MAX)
        await _spent(client, project, 46847608461976.05, conversation)
        sending, ask = await _asking(client, conversation)
        for text in ("46847608461976.06", "999999"):
            refused = await _answer(client, ask, text=text)
            assert (refused.status_code, refused.json()["code"], "needed_usd" in refused.json()) == (
                400, "limit_too_high", False), text
        assert (await _answer(client, ask, option="stop")).status_code == 200
        await sending
    (tmp_path / "step").mkdir()
    async with await _app(tmp_path / "step") as client:
        conversation, project = await _conversation(client)
        await _project_budget(client, project, MAX)
        await _spent(client, project, MAX - 0.01, conversation)
        sending, ask = await _asking(client, conversation)
        assert (ask["params"]["form"], ask["params"]["budget"]) == ("step", "project")
        for body in ({"option": "go_ahead"}, {"text": "999999.99"}):
            refused = await _answer(client, ask, **body)
            assert (refused.status_code, refused.json()["code"], "needed_usd" in refused.json()) == (
                400, "limit_too_high", False), body
        assert (await _answer(client, ask, option="not_now")).status_code == 200
        assert [(e["type"], e.get("status")) for e in await sending][-1] == ("run_finished", "cancelled")


@pytest.mark.asyncio
async def test_the_least_limit_a_step_needs_just_under_the_most_typed_back_as_said_applies_and_a_cent_less_does_not(
        tmp_path):
    # C-F2 in range: the needed amount, as the interface says it in English and in Chinese (rounded up to the cent and
    # grouped), typed back applies; a cent less is refused with the same needed amount
    import math
    async with await _app(tmp_path) as client:
        for shown in ("${:,.2f}", "US${:,.2f}"):
            conversation, project = await _conversation(client, budget=999990.0)
            await _project_budget(client, project, MAX)
            await _spent(client, project, 999998.5, conversation)
            sending, ask = await _asking(client, conversation)
            refused = await _answer(client, ask, text="999990.5")
            needed = refused.json()["needed_usd"]
            assert refused.json()["code"] == "limit_too_low" and needed <= MAX
            cents = math.ceil(needed * 100 - 1e-9) / 100
            less = await _answer(client, ask, text=shown.format(cents - 0.01))
            assert (less.json()["code"], less.json()["needed_usd"]) == ("limit_too_low", needed)
            answered = await _answer(client, ask, text=shown.format(cents))
            assert answered.json()["budget"]["limit_usd"] == cents, answered.text
            assert (await sending)[-1]["status"] == "succeeded"
            await _project_budget(client, project, 50.0)  # back to the General project's own room for the next pass
            await asyncio.to_thread(client.state["db"].write, lambda conn: conn.execute(
                "DELETE FROM budget_reservations WHERE project_id = ?", (project,)))


# PR 52's follow-up (cycle 5 B2; SPEC:672, SPEC:732, A9, G9): a run whose terminal write failed is no longer held by this
# process, though its row still says running and waiting on its ask: it reads interrupted, and so does its ask, which no
# answer can apply. Continue applied ($0 to $10 for a conversation, $1 to $2 for a project).


@pytest.mark.asyncio
@pytest.mark.parametrize("ending, budget", [("stop", "conversation"), ("stop", "project"), ("closed", "conversation")])
async def test_a_budget_ask_whose_run_ended_without_its_terminal_write_is_closed_and_changes_no_limit(
        tmp_path, monkeypatch, ending, budget):
    import contextlib
    from backend import runs
    from test_agent_loop import asked
    async with await _app(tmp_path) as client:
        harness = client.state["harness"]
        conversation, project = await _conversation(client, budget=0 if budget == "conversation" else None)
        if budget == "project":
            await _project_budget(client, project, 1.0)
            await _spent(client, project, 1.0)
        claim = await harness.admit_turn(conversation, "Short?")

        async def read():
            return [event async for event in harness.events(claim)]
        reader = asyncio.create_task(read())
        ask = await asked(client, conversation)
        assert (ask["params"]["form"], ask["params"]["budget"]) == ("cap", budget)
        real = runs._close_asks

        def failing(conn, run_id):
            raise RuntimeError("the terminal write failed")
        monkeypatch.setattr(runs, "_close_asks", failing)
        if ending == "stop":
            await client.post(f"/api/runs/{claim.run_id}/cancel")
        else:
            reader.cancel()  # its window went: the closed stream stops the turn
        await asyncio.wait({claim.task, reader})
        with contextlib.suppress(BaseException):
            claim.task.result()
        monkeypatch.setattr(runs, "_close_asks", real)  # restored once its run is let go
        assert claim.run_id not in harness.registry.runs
        assert await _rows(client, "SELECT status, waiting FROM runs WHERE id = ?", claim.run_id) == [("running", "ask")]
        turn = (await client.get(f"/api/conversations/{conversation}")).json()["turns"][-1]
        assert turn["status"] == "interrupted"
        answered = await _answer(client, ask, option="continue")
        assert await _own_budget(client, conversation) == (0 if budget == "conversation" else None)
        assert await _project_budget(client, project) == (1.0 if budget == "project" else 50)
        assert (answered.status_code, answered.json()["code"]) == (409, "ask_closed")
        assert await _decisions(client, claim.run_id) == []
        assert (await client.get("/api/asks", params={"conversation_id": conversation})).json()["asks"] == []


# Fix batch 1, A-F3 (Harold, 2026-10-21: nothing is admitted against a budget its file does not give, so each step asks;
# 2026-11-12 (3) for the personal default): such a budget is read as $0, and a step whose route publishes zero prices
# fitted it, so it went out with no ask. A budget of $0 (one used up already, G2, or one not read) admits nothing.

_FREE = {"tag": "free", "context_length": 128000, "pricing": {"prompt": "0", "completion": "0"}}


@pytest.mark.asyncio
@pytest.mark.parametrize("budget", ["conversation", "project", "own zero"])
async def test_a_step_that_costs_nothing_asks_while_its_budget_cannot_be_read_or_is_zero(tmp_path, budget):
    from scholia_app import MockProvider
    async with await _app(tmp_path, MockProvider(endpoints={budget_router_model(): [_FREE]})) as client:
        conversation, project = await _conversation(client, budget=0 if budget == "own zero" else None)
        if budget == "conversation":
            personal = tmp_path / "data" / "config.toml"
            personal.write_bytes(b'[budget]\nconversation_usd = "broken"\n' + personal.read_bytes())
        elif budget == "project":
            await _project_budget(client, project, 50.0)
            _hand_set(tmp_path, project, '"broken"')
        sending, ask = await _asking(client, conversation)
        params = ask["params"]
        assert (params["budget"], params["form"], params["estimate_usd"], params.get("unread")) == (
            "project" if budget == "project" else "conversation", "cap", 0, None if budget == "own zero" else True)
        assert client.provider.answers == []  # it never went out
        assert (await _answer(client, ask, option="stop")).status_code == 200
        assert [(e["type"], e.get("cancel_reason")) for e in await sending][-1] == ("run_finished", "limit")
        assert client.provider.answers == []


def test_a_background_call_that_costs_nothing_is_not_admitted_against_a_budget_of_zero(db):
    # A10: a background run never asks; under a project budget its file does not give (read as $0) its call is refused
    # as any other is, so a title ends failed and an extraction is skipped, whatever its route costs
    project = add_project(db)
    with pytest.raises(spending.BudgetExceeded):
        reserve(db, project, None, add_turn(db, project)[1], 0.0, project_budget=0.0, conversation_budget=None)
    assert reserve(db, project, None, add_turn(db, project)[1], 0.0, project_budget=1e-9, conversation_budget=None)


# PR 52's follow-up (Harold, 2026-11-12, the batch 3 readings' (3)): E4's rule for the personal config.toml. While it gives
# no valid conversation budget, its loader's shipped $10 says nothing of what it holds: a conversation without a limit of
# its own has no room, so each step asks and the ask says the budget could not be read (never that it is $0); Continue
# and Go ahead apply nothing; a typed limit applies, the conversation's own from then on. One with its own limit is read
# from the record, as before.

PERSONAL_UNREAD = [b'budget = "broken"\n', b"[[budget]]\nconversation_usd = 2\n", b"[budget.conversation_usd]\ntoken = 1\n",
                   b'[budget]\nconversation_usd = "ten"\n']  # a file not valid TOML gives no provider either: no turn runs


@pytest.mark.asyncio
@pytest.mark.parametrize("left", PERSONAL_UNREAD)
async def test_while_the_personal_file_gives_no_conversation_budget_each_step_asks_and_only_a_typed_limit_applies(
        tmp_path, left):
    from scholia_app import send
    from test_agent_loop import asked
    async with await _app(tmp_path) as client:
        conversation, project = await _conversation(client)
        own, _ = await _conversation(client, budget=2.0, title="Own")
        personal = tmp_path / "data" / "config.toml"
        personal.write_bytes(left + personal.read_bytes())  # before its providers table, which stays as it was
        sending, ask = await _asking(client, conversation)
        params = ask["params"]
        assert (params["budget"], params["form"], params["limit_usd"], params.get("unread")) == (
            "conversation", "cap", 0, True)
        assert client.provider.answers == []  # the step never went out at the shipped default
        spend = (await client.get(f"/api/projects/{project}/spend", params={"conversation_id": conversation})).json()
        assert (spend["conversation"]["limit_usd"], spend["conversation"]["own"]) == (None, False)
        assert (await _answer(client, ask, option="continue")).json()["budget"]["applied"] is False
        [decided] = await _decisions(client, ask["run_id"])
        assert (decided["from_usd"], decided["to_usd"], decided["amount_usd"], decided["applied"]) == (
            None, None, None, False)  # unknown, never $0, and nothing to add to
        again = await asked(client, conversation)
        while again["ask_id"] == ask["ask_id"]:
            assert not sending.done(), (await sending)[-1]
            await asyncio.sleep(0.02)
            again = await asked(client, conversation)
        assert again["params"].get("unread") is True and client.provider.answers == []
        answered = await _answer(client, again, text="3")
        assert answered.json()["budget"] == {"applied": True, "budget": "conversation", "limit_usd": 3.0}
        assert (await sending)[-1]["status"] == "succeeded"
        assert await _own_budget(client, conversation) == 3.0
        decided = (await _decisions(client, again["run_id"]))[-1]
        assert (decided["decision"], decided["from_usd"], decided["to_usd"], decided["amount_usd"]) == (
            "set_limit", None, 3.0, 3.0)
        assert (await send(client, own, "Own?"))[-1]["status"] == "succeeded"  # its own limit, from the record
        from backend import runs
        assert (await asyncio.to_thread(runs._budgets_now, tmp_path / "data", project))[1] == 0.0


@pytest.mark.asyncio
async def test_a_personal_file_not_valid_toml_gives_no_room_and_one_giving_its_budget_or_none_is_read_as_before(
        tmp_path):
    # The rule's edges: a file not valid TOML gives no room; one without the key, or with a problem elsewhere, gives the
    # default as it always did
    from backend import runs
    async with await _app(tmp_path) as client:
        _, project = await _conversation(client)
        for raw, default in ((b"[budget\nconversation_usd = ", 0.0), (b"", 10), (b'[budget]\nnotice_percents = "x"\n', 10),
                             (b"[budget]\nconversation_usd = 4\n", 4), (b'[ui]\nfollow_up = 7\n', 10)):
            (tmp_path / "data" / "config.toml").write_bytes(raw)
            assert (await asyncio.to_thread(runs._budgets_now, tmp_path / "data", project))[1] == default, raw


# PR 52's follow-up (batch 7's residual; design review C3's "Outside this class"): in a project file whose keys are root
# dotted keys, a save adding a table under `project` makes tomlkit write its header after the first of them, so every key
# after it moves under the new table. With the limit not first, the limit moved too and read as reset to the shipped $50
# (200). A project file's save writes nothing unless every key it does not name reads back as it was (Settings.save).


@pytest.mark.asyncio
async def test_a_settings_save_that_would_move_the_limit_in_a_hand_written_layout_is_refused_and_writes_nothing(tmp_path):
    async with await _app(tmp_path) as client:
        conversation, project = await _conversation(client)
        await _project_budget(client, project, 25.0)
        _config(tmp_path, project).write_text('project.template = "imrad"\nproject.budget_usd = 25.0\n')
        assert await _continue_at_cap(client, conversation, project, 25.0, "Short?") == 50.0
        before = _config(tmp_path, project).read_bytes()
        response = await _saved(client, project, {"project.unused": {}})
        assert (response.status_code, response.json().get("code")) == (400, "invalid_setting")
        assert _config(tmp_path, project).read_bytes() == before
        assert await _project_budget(client, project) == 50.0
        assert await _continue_at_cap(client, conversation, project, 50.0) == 75.0


@pytest.mark.asyncio
async def test_a_save_removing_a_child_of_the_limit_beside_a_table_that_would_move_it_is_refused(tmp_path):
    # Fix batch 1, A-F2: the first review's probe. Removing a child of a number (`project.budget_usd.x`) changes nothing,
    # but it exempted the number from the check, so the table moved it with the pair: 200, and Continue then added
    # $50 (to $100), not the $25 it was given (to $75).
    async with await _app(tmp_path) as client:
        conversation, project = await _conversation(client)
        await _project_budget(client, project, 25.0)
        assert await _continue_at_cap(client, conversation, project, 25.0, "Short?") == 50.0
        _config(tmp_path, project).write_text(
            'project.template = "imrad"\nproject.budget_continue_usd = 25.0\nproject.budget_continue_at_usd = 50.0\n'
            'project.budget_usd = 50.0\n')
        before = _config(tmp_path, project).read_bytes()
        response = await _saved(client, project, {"project.unused": {}, "project.budget_usd.x": None})
        assert (response.status_code, response.json().get("code")) == (400, "invalid_setting")
        assert _config(tmp_path, project).read_bytes() == before
        assert await _continue_at_cap(client, conversation, project, 50.0) == 75.0


_MOVING = b'project.template = "imrad"\nproject.budget_usd = 25.0\n'


@pytest.mark.parametrize("raw, updates", [
    (b'project.template = "imrad"\nproject.budget_usd = 80.0\n', {"project.unused": {}}),  # the limit would move
    (b'project.budget_usd = 80.0\nproject.template = "imrad"\n', {"project.unused": {}}),  # another key would
    (b'project.citation_style = "apa"\nproject.template = "imrad"\n', {"project.extra": {"x": 1}}),
    # the internal review's F3: an empty table of its own names none of the keys under it
    (_MOVING, {"project.unused": {}, "project": {}}),
    (_MOVING, {"project": {"unused": {}}}),
    # Fix batch 1, A-F2: a save naming a child of a value it would move (a removal that changes nothing), for each kind
    # of key the first review moved, and a save naming a value it would move rather than set
    (_MOVING, {"project.unused": {}, "project.budget_usd.x": None}),
    (b'project.citation_style = "apa"\nproject.template = "imrad"\n', {"project.unused": {}, "project.template.x": None}),
    (b'project.template = "imrad"\nmemory.enabled = false\n', {"project.unused": {}, "memory.enabled.x": None}),
    (b'project.template = "imrad"\ntools.deny = ["write"]\n', {"project.unused": {}, "tools.deny.x": None}),
    (b'project.template = "imrad"\nlimits.turn_minutes = 5\n', {"project.unused": {}, "limits.turn_minutes.x": None}),
    (b'project.template = "imrad"\nmodels.default = "m"\n', {"project.unused": {}, "models.default.x": None}),
    (b'project.template = "imrad"\nui.panel = "library"\n', {"project.unused": {}, "ui.panel.x": None}),
    (b'project.template = "imrad"\nmcp.servers = [{ name = "a" }]\n', {"project.unused": {}, "mcp.servers.x": None}),
    (_MOVING, {"project.unused": {}, "project.budget_usd": 30.0}),  # the limit it sets would read as the shipped $50
    (_MOVING, {"project.budget_usd": 30.0, "project.unused": {}}),
    (b'project.template = "imrad"\nmemory.enabled = false\n', {"project.unused": {}, "memory.enabled": False}),
])
def test_a_project_files_save_moving_a_key_it_does_not_name_raises_and_writes_nothing(tmp_path, monkeypatch, raw, updates):
    from backend.settings import load_settings
    written = _project_file(tmp_path, monkeypatch, raw)
    with pytest.raises(ValueError):
        load_settings(tmp_path, "p").save(updates)
    assert written == [] and (tmp_path / "projects" / "p" / "config.toml").read_bytes() == raw


_LAYOUTS = [b'project.template = "imrad"\nproject.budget_usd = 80.0\nmemory.enabled = true\n',
            b'memory.enabled = true\nproject.budget_usd = 80.0\nproject.citation_style = "apa"\n',
            b'project.budget_usd = 80.0\nproject.budget_continue_usd = 25.0\nproject.budget_continue_at_usd = 80.0\n'
            b'limits.turn_minutes = 12\n',
            b'[project]\ntemplate = "imrad"\nbudget_usd = 80.0\n[memory]\nenabled = true\n',
            b"[project]\n", b"[project]\n[memory]\n",  # empty tables, as a reset leaves them, which a save writes into
            b"project.budget_usd = 80.0\n"]  # a reset of its only key leaves no table


@pytest.mark.parametrize("raw", _LAYOUTS)
@pytest.mark.parametrize("updates, answer", [
    ({"project.citation_style": "vancouver"}, False), ({"memory.enabled": False}, False),
    ({"project.budget_usd": 60}, False), ({"project.budget_usd": None}, False), ({"limits.turn_minutes": 9}, False),
    ({"project.budget_usd": 90.0, "project.budget_continue_usd": 10.0, "project.budget_continue_at_usd": 90.0}, True)])
def test_the_apps_own_project_saves_move_no_key_in_any_layout_and_are_written(tmp_path, monkeypatch, raw, updates, answer):
    # The rule refuses only a save that would move what it does not name: the Settings page's saves and a budget
    # answer's, on hand-written layouts too, are written as before (a probe over 207 layouts found none refused)
    from backend.settings import load_settings
    written = _project_file(tmp_path, monkeypatch, raw)
    load_settings(tmp_path, "p").save(updates, answer=answer)
    assert len(written) == 1
