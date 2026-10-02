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
import asyncio, json, os, signal, sys, time
import httpx
from backend import runs
from backend.app import create_app

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


async def answer(request):
    body = json.loads(request.content)
    title = body["messages"][0]["content"].startswith("Write a title")
    if (scenario == "turn_model" and not title) or (scenario in ("title_model", "restart_title_model") and title):
        crash()
    text, cost = ("Crash test title", 0.0003) if title else ("An answer.", 0.002)
    return httpx.Response(200, json={"choices": [{"message": {"content": text}}], "usage": {"cost": cost}})


if scenario == "turn_after_step":  # the step and its cost are recorded; the primary commit never runs
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
            if not scenario.startswith("restart"):
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
        assert attempts == 0  # the attempt in flight left no record: its cost stays unknown
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
        # The first title attempt was in flight: its reservation settled at the estimate, no attempt record.
        assert sorted(basis for _, _, basis in spending) == ["estimated", "reported", "reported"]
        assert attempts == 2  # the answer's and the second title attempt's


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
