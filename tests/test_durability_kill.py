"""A process killed at each commit boundary of a turn and of its title run.

A child process runs the app on a data folder, with model calls answered by an
in-process mock (it opens no socket), and kills itself with SIGKILL at the point
under test. The test then starts the app on the same folder and checks what
recovery made of it: costs counted once, an attempt in flight left with no
recorded cost and its reservation settled at the estimate, a recorded step
finished from the record with no model call, effects applied once, and a run
with no attempt left marked interrupted.
"""

import asyncio
import json
import signal
import subprocess
import sys
from pathlib import Path

import pytest

from backend.credentials import SERVICE
from network_guard import allow_subprocess
from scholia_app import KEY, FakeKeyring, MockProvider, background_idle, send, started

pytestmark = pytest.mark.asyncio
ROOT = Path(__file__).resolve().parents[1]

CHILD = r"""
import asyncio, base64, json, os, signal, sys, time
import httpx
from backend import runs
from backend.app import create_app
from backend.citations import JUDGE_RULES

data, scenario = sys.argv[1], sys.argv[2]
ORIGIN = "http://127.0.0.1:8765"


def crash(*args, **kwargs):
    os.kill(os.getpid(), signal.SIGKILL)
    while True:  # the signal is delivered asynchronously; never return to the next statement
        time.sleep(1)


class Keyring:
    def get_password(self, service, name):
        return "sk-or-test-not-a-real-key"

    def set_password(self, service, name, value):
        pass

    def delete_password(self, service, name):
        pass


class _Streamed(httpx.AsyncByteStream):  # an answer as a server's arrives, read by the caller
    def __init__(self, data):
        self.data = data

    async def __aiter__(self):
        yield self.data


def listing(request):  # the model catalog and endpoint listing a turn's admission reads; None for a chat
    path = request.url.path
    if path.endswith("/models"):
        body = {"data": [{"id": "google/gemini-3.1-pro-preview", "context_length": 128000,
                          "pricing": {"prompt": "0.000001", "completion": "0.000005"}}]}
    elif path.endswith("/endpoints/zdr"):
        body = {"data": []}
    elif path.endswith("/endpoints"):
        body = {"data": {"endpoints": [{"tag": "example", "context_length": 128000, "supported_parameters": ["max_tokens"],
                                        "pricing": {"prompt": "0.000001", "completion": "0.000005"}}]}}
    else:
        return None
    return httpx.Response(200, stream=_Streamed(json.dumps(body).encode()))


async def answer(request):
    if (found := listing(request)) is not None:
        return found
    body = json.loads(request.content)
    title = body["messages"][0]["content"].startswith("Write a title")
    judge = body["messages"][0]["content"].startswith(JUDGE_RULES)
    if (scenario == "turn_model" and not title) or (scenario in ("title_model", "restart_title_model") and title) \
            or (scenario == "cited_judge" and judge):
        crash()
    text, cost = ("Crash test title", 0.0003) if title else ("1: supported", 0.001) if judge else (
        "Minimum wages raise earnings [S1.1]." if scenario.startswith("cited") else "An answer.", 0.002)
    return httpx.Response(200, json={"choices": [{"message": {"content": text}}], "usage": {"cost": cost}})


if scenario in ("turn_after_step", "cited_after_judge"):  # the steps and costs are recorded; no primary commit
    runs.Harness._commit_answer = crash
elif scenario in ("title_after_step", "restart_finish"):  # inside the finishing transaction, before COMMIT
    runs.Harness._finish_background = crash
elif scenario == "title_after_finish":  # after the finishing transaction committed
    release = runs.Registry.release
    def release_then_crash(self, active):
        release(self, active)
        if active.kind == "background":
            crash()
    runs.Registry.release = release_then_crash
elif scenario == "cited_after_commit":  # after the turn's primary commit, before anything else
    release = runs.Registry.release
    def release_then_crash(self, active):
        release(self, active)
        if active.kind == "turn":
            crash()
    runs.Registry.release = release_then_crash

app = create_app(data, origin=ORIGIN, keyring_backend=Keyring(), transport=httpx.MockTransport(answer))


async def main():
    inner = app.app
    async with inner.router.lifespan_context(inner):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url=ORIGIN,
                                     headers={"X-Scholia-Client": "local"}) as client:
            if scenario == "turn_after_commit":  # the answer and the title run are committed; nothing after
                async def after_commit(self):
                    crash()
                runs.Harness.kick_background = after_commit  # after startup, which also starts background runs
            if scenario.startswith("cited"):  # S1-19b: a turn whose answer cites a paper and is judged
                await client.post("/api/setup", json={"openrouter_key": "sk-or-test-not-a-real-key"})
                project = (await client.post("/api/projects", json={"name": "Thesis"})).json()["id"]
                paper = b"# Wage Floors\n\n## Findings\n\nMinimum wages raise the earnings of low-paid workers.\n"
                await client.post(f"/api/projects/{project}/materials", json={"files": [
                    {"name": "wages.md", "data": base64.b64encode(paper).decode()}]})
                for _ in range(500):  # until it is read and indexed (keyword search: no helper here)
                    runs_now = (await client.get("/api/activity")).json()["runs"]
                    status = (await client.get(f"/api/projects/{project}/index")).json()
                    if status["passages"]["indexed"] and not any(r["status"] == "running" for r in runs_now):
                        break
                    await asyncio.sleep(0.02)
                conversation = (await client.post("/api/conversations", json={"project_id": project})).json()["id"]
                await client.post(f"/api/conversations/{conversation}/message/stream",
                                  json={"content": "What do minimum wages do to earnings?"})
            elif not scenario.startswith("restart"):
                await client.post("/api/setup", json={"openrouter_key": "sk-or-test-not-a-real-key"})
                conversation = (await client.post("/api/conversations", json={})).json()["id"]
                await client.post(f"/api/conversations/{conversation}/message/stream",
                                  json={"content": "What is a cohort study?"})
            await asyncio.sleep(10)  # the background run reaches its kill point meanwhile


asyncio.run(main())
sys.exit(3)  # not killed: the kill point was never reached
"""


def run_child(data, scenario):
    with allow_subprocess(sys.executable):  # the child opens no socket: its model calls stay in process
        child = subprocess.run([sys.executable, "-c", CHILD, str(data), scenario],
                               cwd=ROOT, capture_output=True, timeout=60)
    assert child.returncode == -signal.SIGKILL, child.stderr.decode()[-3000:]


def keyring():
    store = FakeKeyring()
    store.keys[(SERVICE, "openrouter")] = KEY
    return store


async def rows(client, sql, *args):
    return await asyncio.to_thread(client.state["db"].read, lambda conn: conn.execute(sql, args).fetchall())


async def restart(data):
    provider = MockProvider()
    provider.title_replies.append(provider.answer("Recovered title", cost=0.0004))
    return provider, started(data, provider, keyring=keyring(), setup=False)


async def state(client):
    turn = await rows(client, "SELECT r.status, t.answer IS NOT NULL FROM runs r JOIN turns t ON t.run_id = r.id")
    title = await rows(client, "SELECT status, attempts FROM runs WHERE kind = 'background'")
    conversation = await rows(client, "SELECT title, title_rev FROM conversations")
    spending = sorted(await rows(client, "SELECT status, settled_usd, basis FROM budget_reservations"))
    attempts = await rows(client, "SELECT count(*) FROM run_events WHERE type = 'model_attempt'")
    return turn, title, conversation, spending, attempts[0][0]


async def test_killed_during_the_answer_call(tmp_path):
    data = tmp_path / "data"
    run_child(data, "turn_model")
    provider, app = await restart(data)
    async with app as client:
        turn, title, conversation, spending, attempts = await state(client)
        assert turn == [("interrupted", 0)] and title == []
        [(status, settled, basis)] = spending
        assert (status, basis) == ("settled", "estimated") and settled > 0
        # The attempt in flight is recorded at recovery from the body kept before it was sent (S1-19b), with
        # its charge unknown: its cost stays its estimate.
        assert attempts == 1
        [(data,)] = await rows(client, "SELECT data FROM run_events WHERE type = 'model_attempt'")
        attempt = json.loads(data)
        assert (attempt["outcome"], attempt["charge"], len(attempt["body"])) == ("interrupted", "unknown", 64)
        [(kept,)] = await rows(client, "SELECT count(*) FROM run_bodies")
        assert kept == 1  # its exact text, in the trace
        assert provider.chats == []
        [(conversation_id,)] = await rows(client, "SELECT id FROM conversations")
        assert (await send(client, conversation_id, "again"))[-1]["status"] == "succeeded"
        await background_idle(client)


async def test_killed_after_the_step_is_recorded(tmp_path):
    data = tmp_path / "data"
    run_child(data, "turn_after_step")
    provider, app = await restart(data)
    async with app as client:
        turn, title, _, spending, attempts = await state(client)
        assert turn == [("interrupted", 0)] and title == []
        assert spending == [("settled", 0.002, "reported")]  # counted once, as recorded
        assert attempts == 1 and provider.chats == []


async def test_killed_after_the_primary_commit(tmp_path):
    data = tmp_path / "data"
    run_child(data, "turn_after_commit")
    provider, app = await restart(data)
    async with app as client:
        await background_idle(client)
        turn, title, conversation, spending, _ = await state(client)
        assert turn == [("succeeded", 1)]
        assert title == [("succeeded", 1)] and len(provider.titles) == 1 and provider.answers == []
        assert conversation == [("Recovered title", 1)]
        assert spending == [("settled", 0.0004, "reported"), ("settled", 0.002, "reported")]


async def test_killed_during_the_title_call_restarts_it_once(tmp_path):
    data = tmp_path / "data"
    run_child(data, "title_model")
    provider, app = await restart(data)
    async with app as client:
        await background_idle(client)
        turn, title, conversation, spending, attempts = await state(client)
        assert turn == [("succeeded", 1)] and title == [("succeeded", 2)]
        assert len(provider.titles) == 1 and conversation == [("Recovered title", 1)]
        # The first title attempt was in flight: its reservation settled at the estimate, and recovery records
        # it as interrupted from the body kept before it was sent (S1-19b).
        assert sorted(basis for _, _, basis in spending) == ["estimated", "reported", "reported"]
        assert attempts == 3  # the answer's, the interrupted first title attempt's and the second's
        outcomes = sorted(json.loads(d)["outcome"] for (d,) in await rows(
            client, "SELECT data FROM run_events WHERE type = 'model_attempt'"))
        assert outcomes.count("interrupted") == 1


async def test_killed_after_the_title_step_is_recorded_finishes_from_the_record(tmp_path):
    data = tmp_path / "data"
    run_child(data, "title_after_step")
    provider, app = await restart(data)
    async with app as client:
        await background_idle(client)
        _, title, conversation, spending, _ = await state(client)
        assert provider.chats == []  # no model call
        assert title == [("succeeded", 1)] and conversation == [("Crash test title", 1)]
        assert spending == [("settled", 0.0003, "reported"), ("settled", 0.002, "reported")]


async def test_killed_while_finishing_from_the_record_repeats_it_once(tmp_path):
    data = tmp_path / "data"
    run_child(data, "title_after_step")
    run_child(data, "restart_finish")  # killed again inside the finishing transaction
    provider, app = await restart(data)
    async with app as client:
        await background_idle(client)
        _, title, conversation, _, _ = await state(client)
        assert provider.chats == [] and title == [("succeeded", 1)]
        assert conversation == [("Crash test title", 1)]  # applied once


async def test_killed_after_the_terminal_write_never_runs_again(tmp_path):
    data = tmp_path / "data"
    run_child(data, "title_after_finish")
    provider, app = await restart(data)
    async with app as client:
        await background_idle(client)
        _, title, conversation, spending, _ = await state(client)
        assert provider.chats == [] and title == [("succeeded", 1)]
        assert conversation == [("Crash test title", 1)]
        assert spending == [("settled", 0.0003, "reported"), ("settled", 0.002, "reported")]


async def test_killed_during_the_second_title_call_is_interrupted(tmp_path):
    data = tmp_path / "data"
    run_child(data, "title_model")
    run_child(data, "restart_title_model")  # the second and last attempt is killed in flight too
    provider, app = await restart(data)
    async with app as client:
        await background_idle(client)
        _, title, conversation, spending, _ = await state(client)
        assert provider.chats == [] and title == [("interrupted", 2)]
        assert conversation == [(None, 0)]
        assert sorted(basis for _, _, basis in spending) == ["estimated", "estimated", "reported"]


# S1-19b: the support judge runs before the primary commit (L2, L6)

async def cited_state(client):
    turn = await rows(client, "SELECT r.status, t.answer IS NOT NULL FROM runs r JOIN turns t ON t.run_id = r.id")
    citations = await rows(client, "SELECT existence, support FROM citations")
    spending = sorted(await rows(client, "SELECT status, settled_usd, basis FROM budget_reservations"
                                         " WHERE run_id IN (SELECT run_id FROM turns) AND status != 'released'"))
    # The judge's share held with the answer is released, not charged, after a crash (runs._release_allowances).
    assert await rows(client, "SELECT count(*) FROM budget_reservations WHERE status = 'open'") == [(0,)]
    return turn, citations, spending


async def test_killed_during_the_judge_call_leaves_no_answer_and_counts_the_answer_once(tmp_path):
    data = tmp_path / "data"
    run_child(data, "cited_judge")
    provider, app = await restart(data)
    async with app as client:
        turn, citations, spending = await cited_state(client)
        assert turn == [("interrupted", 0)] and citations == []  # nothing marked checked
        [(answer_status, answer_cost, answer_basis), (judge_status, judge_cost, judge_basis)] = sorted(
            spending, key=lambda r: r[2] != "reported")
        assert (answer_status, answer_cost, answer_basis) == ("settled", 0.002, "reported")  # counted once
        assert (judge_status, judge_basis) == ("settled", "estimated") and judge_cost > 0  # in flight: its estimate
        assert provider.answers == [] and provider.chats == []  # recovery sends nothing


async def test_killed_after_the_judge_and_before_the_commit_leaves_no_answer_marked_checked(tmp_path):
    data = tmp_path / "data"
    run_child(data, "cited_after_judge")
    provider, app = await restart(data)
    async with app as client:
        turn, citations, spending = await cited_state(client)
        assert turn == [("interrupted", 0)] and citations == []
        assert sorted(spending) == [("settled", 0.001, "reported"), ("settled", 0.002, "reported")]
        assert provider.chats == []


async def test_killed_after_the_primary_commit_keeps_its_citations_once(tmp_path):
    data = tmp_path / "data"
    run_child(data, "cited_after_commit")
    provider, app = await restart(data)
    async with app as client:
        await background_idle(client)
        turn, citations, _ = await cited_state(client)
        assert turn == [("succeeded", 1)] and citations == [("ok", "supported")]
        assert provider.answers == []
