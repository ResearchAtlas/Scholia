"""The local model helper's lifecycle, and model downloads and imports (S1-16).

The helper is a test-owned stand-in for llama-server: a small Python program, allowed by the
network block, that records how it was started and behaves as the test says (ready, silent, or
exiting), and opens no socket. Its HTTP side, and the model download sources, are answered in
process through the outbound gate's mock transport. Models are synthetic bytes with their own pins.
"""

import asyncio
import contextlib
import errno
import fcntl
import hashlib
import json
import logging
import os
import stat
import sys
import threading
import time
from pathlib import Path

import httpx
import pytest

from backend import local_helper, logs
from backend.db import new_id
from backend.outbound_gate import OutboundDenied
from backend.local_helper import EMBEDDING, EMBEDDING_MODEL, RERANKER_MODEL, Config, HelperUnavailable
from network_guard import allow_subprocess
from scholia_app import started

WEIGHTS = b"synthetic GGUF weights, not a model. " * 64
RERANKER_WEIGHTS = b"synthetic reranker weights. " * 64
HF_CDN = "https://us.aws.cdn.hf.co/xet-bridge-us/0123/abcd?X-Amz-Signature=signed"
MS_CDN = "https://cdn-lfs-cn-1.modelscope.cn/prod/lfs-objects/01/23/abcd?auth_key=signed"

FAKE_SERVER = r'''#!{python}
# A stand-in for llama-server: it records how it was started, then behaves as the control file says.
# With a stop_delay file, it takes that many seconds to end after SIGTERM, as a busy server may.
import json, os, signal, sys, time
control = {control!r}
try:
    with open(os.path.join(control, "stop_delay")) as f:
        delay = float(f.read())
    def ending(signum, frame):
        time.sleep(delay)
        sys.exit(0)
    signal.signal(signal.SIGTERM, ending)
except FileNotFoundError:
    pass
with open(os.path.join(control, "launches.jsonl"), "a") as f:
    f.write(json.dumps({{"argv": sys.argv[1:], "env": dict(os.environ), "pid": os.getpid()}}) + "\n")
try:
    with open(os.path.join(control, "behavior")) as f:
        behavior = f.read().split()
except FileNotFoundError:
    behavior = ["ready"]
print("srv  load_model: loading model", flush=True)
if behavior[0] == "exit":
    print("error: could not load the model", flush=True)
    sys.exit(1)
if behavior[0] == "ready":
    with open(os.path.join(control, "launches.jsonl")) as f:
        launches = sum(1 for _ in f)
    print(f"srv  llama_server: listening on http://127.0.0.1:{{50000 + launches}}", flush=True)
    if len(behavior) > 1:  # "ready N": exits N seconds after it was ready
        time.sleep(float(behavior[1]))
        sys.exit(1)
while True:
    time.sleep(1)
'''


def pin_for(base, weights):
    return {**base, "size": len(weights), "sha256": hashlib.sha256(weights).hexdigest()}


PIN = pin_for(EMBEDDING_MODEL, WEIGHTS)
RERANKER_PIN = pin_for(RERANKER_MODEL, RERANKER_WEIGHTS)


class Fake:
    """The stand-in server's files: the bundle (binary, a library and the build's manifest) and the
    control folder the binary reports to."""

    def __init__(self, root: Path):
        self.contents = root / "Scholia.app" / "Contents"
        self.binary = self.contents / "MacOS" / "llama-server"
        self.library = self.contents / "Frameworks" / "llama-cpp" / "libggml.0.dylib"
        self.control = root / "control"
        for folder in (self.binary.parent, self.library.parent, self.contents / "Resources", self.control):
            folder.mkdir(parents=True)
        self.binary.write_text(FAKE_SERVER.format(python=sys.executable, control=str(self.control)))
        self.binary.chmod(0o755)
        self.library.write_bytes(b"a stand-in library")
        local_helper.write_manifest(self.contents)

    def behave(self, behavior):
        (self.control / "behavior").write_text(behavior)

    def stop_slowly(self, seconds):
        """Servers launched from now on take this long to end after SIGTERM."""
        (self.control / "stop_delay").write_text(str(seconds))

    @property
    def launches(self):
        path = self.control / "launches.jsonl"
        return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []


class Remote:
    """Answers everything the gate lets out: the helper's HTTP side on 127.0.0.1 (health, embeddings
    and reranking, with the key of the server launched last) and the model download sources."""

    def __init__(self, fake):
        self.fake = fake
        self.helper_requests = []  # (port, path, body)
        self.source_requests = []  # URLs
        self.healthy = True
        self.hold = {}  # path -> asyncio.Event that requests wait for
        self.files = {}  # URL -> (status, headers, body or async iterator)
        self.scores = None  # reranking scores to answer with, instead of 1 / (rank + 1)
        self.rows = None  # embedding reply rows to answer with, instead of one per input

    def key(self, port):
        """The key of the server that announced port (50000 + its launch's number)."""
        launches = self.fake.launches
        return launches[port - 50001]["env"]["LLAMA_API_KEY"] if 0 < port - 50000 <= len(launches) else None

    async def __call__(self, request):
        if request.url.host == "127.0.0.1":
            return await self.helper(request)
        self.source_requests.append(str(request.url))
        status, headers, body = self.files.get(str(request.url), (404, {}, b""))
        if status == "unreachable":
            raise httpx.ConnectError("no route to the host", request=request)
        if isinstance(body, bytes):
            return httpx.Response(status, headers=headers, content=body)
        return httpx.Response(status, headers=headers, stream=body)

    async def helper(self, request):
        body = json.loads(request.content) if request.content else None
        self.helper_requests.append((request.url.port, request.url.path, body))
        if request.url.path == "/health":
            return httpx.Response(200 if self.healthy else 503, json={"status": "ok"})
        if request.headers.get("authorization") != f"Bearer {self.key(request.url.port)}":
            return httpx.Response(401, json={"error": "invalid api key"})
        if request.url.path in self.hold:
            await self.hold[request.url.path].wait()
        if request.url.path == "/v1/embeddings":
            return httpx.Response(200, json={"data": self.rows or [{"index": i, "embedding": [0.5, 0.5, 0.5, 0.5]}
                                                                   for i, _ in reversed(list(enumerate(body["input"])))]})
        if request.url.path == "/v1/rerank":
            scores = self.scores or [1.0 / (i + 1) for i, _ in enumerate(body["documents"])]
            return httpx.Response(200, json={"results": [{"index": i, "relevance_score": score}
                                                         for i, score in enumerate(scores)]})
        return httpx.Response(404)


class Streamed(httpx.AsyncByteStream):
    """A download body sent in pieces, which can wait between pieces."""

    def __init__(self, data, piece=64, gate=None, fail_after=None):
        self.data, self.piece, self.gate, self.fail_after = data, piece, gate, fail_after
        self.sent = 0

    async def __aiter__(self):
        for start in range(0, len(self.data), self.piece):
            if self.gate is not None and start:
                await self.gate.wait()
            if self.sent == self.fail_after:
                raise httpx.ReadError("the connection was reset")
            self.sent += 1
            yield self.data[start:start + self.piece]


TIMINGS = {"idle": 600, "start": 5, "health": 30, "backoff": [0.05, 0.05, 0.05]}


@pytest.fixture
def timings(monkeypatch):
    values = dict(TIMINGS)
    monkeypatch.setattr(local_helper.Local, "timings", lambda self: dict(values))
    return values


@pytest.fixture
def fake(tmp_path):
    return Fake(tmp_path / "bundle")


@contextlib.asynccontextmanager
async def app(tmp_path, fake, *, install=True, models=None, binary="fake", notices=None):
    """The app with the stand-in helper (allowed to start) and, with install, the model in place."""
    data = tmp_path / "data"
    models = models or {EMBEDDING: PIN}
    if install:
        path = data / "models" / EMBEDDING / PIN["file"]
        path.parent.mkdir(parents=True)
        path.write_bytes(WEIGHTS)
    remote = Remote(fake)
    config = Config(binary=fake.binary if binary == "fake" else binary, models=models,
                    **({"notices": notices} if notices is not None else {}))
    with allow_subprocess(str(fake.binary)):
        async with started(data, remote, setup=False, helper=config) as client:
            client.remote = remote
            client.local = client.state["local_helper"]
            yield client


async def until(predicate, timeout=5.0):
    deadline = time.monotonic() + timeout
    while not predicate():
        if time.monotonic() > deadline:
            raise AssertionError("timed out")
        await asyncio.sleep(0.01)


@contextlib.contextmanager
def app_log(tmp_path):
    """The app's default log, configured as the desktop entry configures it (every logger, formatted,
    tracebacks included): yields a function returning its text."""
    root = tmp_path / "log-root"
    root.mkdir()
    handler = logs.configure(root)
    path = root / "logs" / "scholia.log"
    try:
        yield lambda: path.read_text() if path.exists() else ""
    finally:
        logging.getLogger().removeHandler(handler)
        handler.close()


def names_nothing(text, tmp_path):
    """No path, file name or download address in a log."""
    return not any(secret in text for secret in (str(tmp_path), tmp_path.name, PIN["file"], "huggingface.co", "hf.co"))


def path_of(fd) -> str:
    return fcntl.fcntl(fd, fcntl.F_GETPATH, bytes(1024)).rstrip(b"\0").decode()


def alive(pid):
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    return True


async def outbound(client):
    return await asyncio.to_thread(client.state["db"].read, lambda conn: [
        (project_id, json.loads(data)) for project_id, data in conn.execute(
            "SELECT project_id, data FROM audit_log WHERE event = 'outbound' ORDER BY seq")])


async def general(client):
    return await asyncio.to_thread(client.state["db"].read, lambda conn: conn.execute("SELECT id FROM projects WHERE kind = 'general'").fetchone()[0])


async def add_project(client, sensitivity):
    project_id = new_id()
    await asyncio.to_thread(client.state["db"].write, lambda conn: conn.execute(
        "INSERT INTO projects (id, name, kind, sensitivity) VALUES (?, 'P', 'research', ?)", (project_id, sensitivity)))
    return project_id


def helper(client):
    return client.local.helpers[EMBEDDING]


# The helper's lifecycle, lifecycle boundaries first


@pytest.mark.asyncio
async def test_a_helper_that_never_becomes_ready_is_stopped_at_the_start_deadline(tmp_path, fake, timings):
    fake.behave("silent")
    timings.update(start=0.5, backoff=[30])
    async with app(tmp_path, fake) as client:
        began = time.monotonic()
        with pytest.raises(HelperUnavailable) as caught:
            await local_helper.embed(client.state, ["a question"], query=True)
        assert caught.value.reason == "start_timeout"
        assert 0.5 <= time.monotonic() - began < 3
        [launch] = fake.launches
        assert not alive(launch["pid"])  # stopped and reaped
        assert helper(client).state == "restarting"
        status = (await client.get("/api/helper")).json()
        assert status["search"] == {"mode": "keyword_only", "reason": "start_timeout"}


@pytest.mark.asyncio
async def test_a_crashing_helper_is_started_again_after_each_backoff_then_the_notice(tmp_path, fake, timings):
    fake.behave("exit")
    async with app(tmp_path, fake) as client:
        with pytest.raises(HelperUnavailable, match="start_failed"):
            await local_helper.embed(client.state, ["text"])
        await until(lambda: helper(client).state == "failed")
        assert len(fake.launches) == 4  # the first start, then one after each of the three backoffs
        await asyncio.sleep(0.3)
        assert len(fake.launches) == 4  # no automatic start after the notice
        status = (await client.get("/api/helper")).json()
        assert status["helper"]["state"] == "failed" and status["helper"]["problem"] == "start_failed"
        assert status["search"] == {"mode": "keyword_only", "reason": "helper_failed"}
        with pytest.raises(HelperUnavailable, match="start_failed"):  # keyword-only meanwhile, no start
            await local_helper.embed(client.state, ["text"], query=True)
        assert len(fake.launches) == 4
        fake.behave("ready")
        response = await client.post("/api/helper/restart")  # Start again
        assert response.status_code == 200
        await until(lambda: helper(client).state == "running")
        assert helper(client).failures == 0
        assert await local_helper.embed(client.state, ["text"]) == [[0.5] * 4]


@pytest.mark.asyncio
async def test_a_running_helper_that_exits_is_restarted(tmp_path, fake, timings):
    timings.update(backoff=[0.5, 0.5, 0.5])
    async with app(tmp_path, fake) as client:
        await local_helper.embed(client.state, ["text"])
        first = fake.launches[0]["pid"]
        os.kill(first, 9)  # a crash
        await until(lambda: helper(client).state == "restarting")
        # Meanwhile search is keyword-only and says why, and a request starts nothing before the backoff.
        status = (await client.get("/api/helper")).json()
        assert status["search"] == {"mode": "keyword_only", "reason": "crashed"}
        with pytest.raises(HelperUnavailable, match="crashed"):
            await local_helper.embed(client.state, ["text"], query=True)
        assert len(fake.launches) == 1
        await until(lambda: len(fake.launches) == 2 and helper(client).state == "running")
        assert helper(client).failures == 1
        assert await local_helper.embed(client.state, ["text"]) == [[0.5] * 4]
        ports = [port for port, path, _ in client.remote.helper_requests if path == "/v1/embeddings"]
        assert ports == [50001, 50002]  # each launch's own port, from its listening line


@pytest.mark.asyncio
async def test_a_helper_that_fails_its_health_check_is_restarted(tmp_path, fake, timings):
    timings.update(health=0.1, backoff=[0.5, 0.5, 0.5])
    async with app(tmp_path, fake) as client:
        await local_helper.embed(client.state, ["text"])
        await until(lambda: any(path == "/health" for _, path, _ in client.remote.helper_requests))
        assert len(fake.launches) == 1 and helper(client).state == "running"
        client.remote.healthy = False
        await until(lambda: not alive(fake.launches[0]["pid"]))
        assert helper(client).state == "restarting" and len(fake.launches) == 1  # ended, then the backoff
        assert (await client.get("/api/helper")).json()["search"] == {"mode": "keyword_only", "reason": "unhealthy"}
        await until(lambda: len(fake.launches) == 2)
        client.remote.healthy = True
        await until(lambda: helper(client).state == "running" and helper(client).failures == 0)


@pytest.mark.asyncio
async def test_closing_the_app_while_the_helper_starts_leaves_no_process(tmp_path, fake, timings):
    fake.behave("silent")
    timings.update(start=30)
    async with app(tmp_path, fake) as client:
        waiting = asyncio.create_task(local_helper.embed(client.state, ["text"], query=True))
        await until(lambda: fake.launches)
        pid = fake.launches[0]["pid"]
        assert alive(pid) and helper(client).state == "starting"
    with pytest.raises(HelperUnavailable):
        await waiting
    assert not alive(pid)


@pytest.mark.asyncio
async def test_closing_the_app_stops_a_running_helper(tmp_path, fake, timings):
    async with app(tmp_path, fake) as client:
        await local_helper.embed(client.state, ["text"])
        pid = fake.launches[0]["pid"]
    assert not alive(pid)


@pytest.mark.asyncio
async def test_closing_the_app_during_a_restart_backoff_starts_nothing(tmp_path, fake, timings):
    fake.behave("exit")
    timings.update(backoff=[0.3])
    async with app(tmp_path, fake) as client:
        with pytest.raises(HelperUnavailable, match="start_failed"):
            await local_helper.embed(client.state, ["text"])
        assert helper(client).state == "restarting"
    await asyncio.sleep(0.5)
    assert len(fake.launches) == 1 and helper(client).state == "stopped"


@pytest.mark.asyncio
async def test_a_health_check_the_gate_cannot_record_is_neither_a_pass_nor_a_failure(tmp_path, fake, timings):
    """A restore holds the database's writes, so the gate cannot record a decision: the helper keeps
    running and its checks resume once writes are admitted again; requests meanwhile cannot be sent."""
    timings.update(health=0.05)
    async with app(tmp_path, fake) as client:
        await local_helper.embed(client.state, ["text"])
        db = client.state["db"]
        await asyncio.to_thread(db.hold_writes)
        try:
            client.remote.helper_requests.clear()
            await asyncio.sleep(0.3)  # several checks' worth
            assert client.remote.helper_requests == []  # none went out unrecorded
            assert helper(client).state == "running" and helper(client).failures == 0
            assert helper(client)._watching is not None and not helper(client)._watching.done()
            with pytest.raises(HelperUnavailable, match="database_unavailable"):  # not "closing": it is not
                await local_helper.embed(client.state, ["text"], query=True)
        finally:
            await asyncio.to_thread(db.release_writes)
        await until(lambda: any(path == "/health" for _, path, _ in client.remote.helper_requests))
        assert len(fake.launches) == 1 and helper(client).state == "running"


@pytest.mark.asyncio
async def test_a_model_file_that_changed_since_it_was_installed_is_found_at_launch(tmp_path, fake, timings):
    path = model_file(tmp_path)
    path.parent.mkdir(parents=True)
    path.write_bytes(WEIGHTS.replace(b"synthetic", b"Synthetic"))  # the pinned size, other bytes
    async with app(tmp_path, fake, install=False) as client:
        await until(lambda: helper(client).problem == "model_changed")
        status = (await client.get("/api/helper")).json()
        assert status["models"][0]["installed"] is True  # in place, by its size
        assert status["search"] == {"mode": "keyword_only", "reason": "model_changed"}
        assert fake.launches == [] and (await outbound(client)) == []
        serve(client, HF_URL, WEIGHTS, via=HF_CDN)
        status = await downloaded(client, source="huggingface")  # replaced, as a download
        assert status["search"]["mode"] == "hybrid" and path.read_bytes() == WEIGHTS


@pytest.mark.asyncio
async def test_a_request_while_a_failed_helper_is_ended_is_refused_and_starts_nothing(tmp_path, fake, timings):
    """A helper that fails its check is marked restarting before its process is ended: a question
    meanwhile is refused with the reason, and the next process starts only once this one is gone."""
    timings.update(health=0.1, backoff=[0.2, 0.2, 0.2])
    fake.stop_slowly(1.0)
    async with app(tmp_path, fake) as client:
        await local_helper.embed(client.state, ["text"])
        pid = fake.launches[0]["pid"]
        client.remote.healthy = False
        await until(lambda: helper(client).state == "restarting")
        assert alive(pid)  # still being ended
        with pytest.raises(HelperUnavailable, match="unhealthy"):
            await local_helper.embed(client.state, ["a question"], query=True)
        assert len(fake.launches) == 1 and helper(client).state == "restarting" and alive(pid)
        client.remote.healthy = True
        await until(lambda: len(fake.launches) == 2, timeout=10)
        assert not alive(pid)  # one process per model
        await until(lambda: helper(client).state == "running")


@pytest.mark.asyncio
async def test_a_request_while_an_idle_helper_is_ended_waits_for_its_end(tmp_path, fake, timings):
    timings.update(idle=0.2)
    fake.stop_slowly(0.8)
    async with app(tmp_path, fake) as client:
        await local_helper.embed(client.state, ["text"])
        pid = fake.launches[0]["pid"]
        await until(lambda: helper(client).state == "stopped")  # idle: being ended
        assert alive(pid)
        question = asyncio.create_task(local_helper.embed(client.state, ["a question"], query=True))
        while alive(pid):
            assert len(fake.launches) == 1  # no second process while the first is being ended
            await asyncio.sleep(0.01)
        assert await question == [[0.5] * 4] and len(fake.launches) == 2


@pytest.mark.asyncio
@pytest.mark.parametrize("why", ["idle", "unhealthy"])
async def test_closing_the_app_while_a_helper_is_ended_waits_for_it(tmp_path, fake, timings, why):
    timings.update(idle=0.2 if why == "idle" else 600, health=0.1, backoff=[30])
    fake.stop_slowly(1.0)
    async with app(tmp_path, fake) as client:
        await local_helper.embed(client.state, ["text"])
        pid = fake.launches[0]["pid"]
        client.remote.healthy = why != "unhealthy"
        await until(lambda: helper(client).state == ("stopped" if why == "idle" else "restarting"))
        assert alive(pid)
    assert not alive(pid) and len(fake.launches) == 1  # closing waited for it: nothing is left running


@pytest.mark.asyncio
async def test_a_failure_is_counted_even_when_ending_its_process_fails(tmp_path, fake, timings, monkeypatch):
    real_end, failed = local_helper._end, []

    async def failing_end(process, *, kill=False):
        await real_end(process, kill=kill)
        if not failed:
            failed.append(process.pid)
            raise RuntimeError("the end could not be confirmed")

    monkeypatch.setattr(local_helper, "_end", failing_end)
    async with app(tmp_path, fake) as client:
        await local_helper.embed(client.state, ["text"])
        os.kill(fake.launches[0]["pid"], 9)
        await until(lambda: len(fake.launches) == 2 and helper(client).state == "running")  # restarted all the same
        assert failed and helper(client).failures == 1


@pytest.mark.asyncio
async def test_a_health_check_the_gate_refuses_for_another_reason_fails(tmp_path, fake, timings, monkeypatch):
    timings.update(health=0.05, backoff=[30])
    async with app(tmp_path, fake) as client:
        await local_helper.embed(client.state, ["text"])
        monkeypatch.setattr(local_helper, "urls", lambda state: ())  # the gate no longer knows its address
        await until(lambda: helper(client).state == "restarting" and not alive(fake.launches[0]["pid"]))
        assert helper(client).problem == "unhealthy" and len(fake.launches) == 1


class _HeldCheck:
    """The launch-time check of an installed model, held once it has hashed the file."""

    def __init__(self, monkeypatch):
        self.real, self.calls, self.returned = local_helper.mismatch, 0, False
        self.hashed, self.release = threading.Event(), threading.Event()
        monkeypatch.setattr(local_helper, "mismatch", self)

    def __call__(self, path, pin):
        self.calls += 1
        result = self.real(path, pin)
        if self.calls == 1:  # the launch's; any check before a start goes straight through
            self.hashed.set()
            self.release.wait(5)
            self.returned = True
        return result

    async def finish(self):
        self.release.set()
        await until(lambda: self.returned)
        await asyncio.sleep(0.05)  # the check's own next step, on the loop


@pytest.mark.asyncio
@pytest.mark.parametrize("meanwhile", ["import", "start"])
async def test_the_launch_check_leaves_alone_what_changed_while_it_hashed(tmp_path, fake, timings, monkeypatch,
                                                                          meanwhile):
    check = _HeldCheck(monkeypatch)
    path = model_file(tmp_path)
    path.parent.mkdir(parents=True)
    path.write_bytes(WEIGHTS.replace(b"synthetic", b"Synthetic"))  # the pinned size, other bytes
    async with app(tmp_path, fake, install=False) as client:
        await asyncio.to_thread(check.hashed.wait, 5)  # it has found the file changed, and is held
        if meanwhile == "import":  # an install: the check's finding no longer holds
            good = tmp_path / "good.gguf"
            good.write_bytes(WEIGHTS)
            response = await client.post("/api/helper/models/import", json={"model": EMBEDDING, "path": str(good)})
            assert response.status_code == 200
        else:  # put right by hand, and started: its own check before the launch passed
            path.write_bytes(WEIGHTS)
            await local_helper.embed(client.state, ["text"])
        await check.finish()
        assert helper(client).problem is None
        assert (await client.get("/api/helper")).json()["search"] == {"mode": "hybrid", "reason": None}
        assert helper(client).state == ("running" if meanwhile == "start" else "stopped")


@pytest.mark.asyncio
async def test_the_helper_starts_on_demand_with_its_flags_and_a_new_key_each_launch(tmp_path, fake, timings):
    async with app(tmp_path, fake) as client:
        await asyncio.sleep(0.1)
        assert fake.launches == []  # never at launch
        assert (await client.get("/api/helper")).json()["helper"]["state"] == "stopped"
        assert await local_helper.embed(client.state, ["first", "second"]) == [[0.5] * 4, [0.5] * 4]
        [launch] = fake.launches
        model = tmp_path / "data" / "models" / EMBEDDING / PIN["file"]
        assert launch["argv"] == ["-m", str(model), "--offline", "--host", "127.0.0.1", "--port", "0", "--no-webui",
                                  "--embedding", "--pooling", "last", "-c", "4096", "-ub", "2048", "-np", "2",
                                  "--cache-ram", "0"]
        # Only the key and the folders it may need; no proxy, no other key.
        # (the stand-in's own runtime may add its locale and text encoding)
        assert set(launch["env"]) - {"__CF_USER_TEXT_ENCODING", "LC_CTYPE"} <= {"LLAMA_API_KEY", "HOME", "TMPDIR"}
        assert len(launch["env"]["LLAMA_API_KEY"]) >= 40
        await helper(client).stop()
        assert not alive(launch["pid"])
        await local_helper.embed(client.state, ["third"])
        keys = [launch["env"]["LLAMA_API_KEY"] for launch in fake.launches]
        assert len(keys) == 2 and keys[0] != keys[1]


@pytest.mark.asyncio
async def test_requests_reach_the_helper_only_through_the_gate_while_it_runs(tmp_path, fake, timings):
    async with app(tmp_path, fake) as client:
        project_id = (await add_project(client, "local_only"))
        await local_helper.embed(client.state, ["a passage"], project_id=project_id)
        url = helper(client).url
        assert url == "http://127.0.0.1:50001" and local_helper.urls(client.state) == (url,)
        rows = (await outbound(client))
        assert [(p, row["kind"], row["decision"], row["destination"]) for p, row in rows] == [
            (project_id, "local_helper", "allow", "http://127.0.0.1:50001")]
        await helper(client).stop()
        assert local_helper.urls(client.state) == ()
        gate = client.state["gate"]
        async with gate.async_client((await general(client))) as http:  # its old address is no destination now
            with pytest.raises(local_helper.OutboundDenied, match="unknown_destination"):
                await http.get(f"{url}/health")


@pytest.mark.asyncio
async def test_a_helper_idle_for_the_set_time_is_stopped(tmp_path, fake, timings):
    timings.update(idle=0.2)
    async with app(tmp_path, fake) as client:
        await local_helper.embed(client.state, ["text"])
        pid = fake.launches[0]["pid"]
        await until(lambda: helper(client).state == "stopped" and not alive(pid))
        assert helper(client).failures == 0 and helper(client).problem is None
        await local_helper.embed(client.state, ["text"])  # started again on demand
        assert len(fake.launches) == 2
        await until(lambda: helper(client).state == "stopped")
        assert (await client.post("/api/helper/restart")).status_code == 200  # Start again, from stopped
        await until(lambda: len(fake.launches) == 3 and helper(client).state == "running")


@pytest.mark.asyncio
async def test_a_question_goes_ahead_of_indexing_batches(tmp_path, fake, timings):
    async with app(tmp_path, fake) as client:
        await local_helper.embed(client.state, ["warm"])
        client.remote.helper_requests.clear()
        held = client.remote.hold["/v1/embeddings"] = asyncio.Event()
        first = asyncio.create_task(local_helper.embed(client.state, ["1a", "1b", "1c"]))
        second = asyncio.create_task(local_helper.embed(client.state, ["2a", "2b"]))
        await until(lambda: len(client.remote.helper_requests) == 1)
        question = asyncio.create_task(local_helper.embed(client.state, ["q1", "q2"], query=True))
        await until(lambda: len(client.remote.helper_requests) == 2)
        await asyncio.sleep(0.05)
        # The batch holds one slot, one text at a time; the question takes the other, all its texts at once.
        assert [body["input"] for _, _, body in client.remote.helper_requests] == [["1a"], ["q1", "q2"]]
        held.set()
        assert await first == [[0.5] * 4] * 3 and await second == [[0.5] * 4] * 2 and len(await question) == 2
        assert [body["input"] for _, _, body in client.remote.helper_requests][2:] == [["1b"], ["1c"], ["2a"], ["2b"]]


@pytest.mark.asyncio
async def test_the_binary_and_its_libraries_are_checked_before_every_launch(tmp_path, fake, timings):
    async with app(tmp_path, fake) as client:
        fake.library.write_bytes(b"a changed library")
        with pytest.raises(HelperUnavailable, match="binary_changed"):
            await local_helper.embed(client.state, ["text"])
        assert fake.launches == [] and helper(client).state == "stopped"
        (fake.contents / local_helper.MANIFEST).unlink()
        with pytest.raises(HelperUnavailable, match="binary_missing"):
            await local_helper.embed(client.state, ["text"])
        local_helper.write_manifest(fake.contents)  # as a new build would record it
        await local_helper.embed(client.state, ["text"])
        await helper(client).stop()
        fake.binary.write_text(fake.binary.read_text() + "\n# changed\n")
        with pytest.raises(HelperUnavailable, match="binary_changed"):
            await local_helper.embed(client.state, ["text"])
        assert len(fake.launches) == 1


@pytest.mark.asyncio
async def test_without_a_bundled_helper_search_is_keyword_only(tmp_path, fake, timings):
    async with app(tmp_path, fake, binary=None) as client:
        with pytest.raises(HelperUnavailable, match="binary_missing"):
            await local_helper.embed(client.state, ["text"])
        status = (await client.get("/api/helper")).json()
        assert status["search"] == {"mode": "keyword_only", "reason": "binary_missing"}


@pytest.mark.asyncio
async def test_the_model_is_checked_before_launch(tmp_path, fake, timings):
    async with app(tmp_path, fake) as client:
        path = tmp_path / "data" / "models" / EMBEDDING / PIN["file"]
        path.write_bytes(WEIGHTS.replace(b"synthetic", b"Synthetic"))  # same size, other bytes
        with pytest.raises(HelperUnavailable, match="model_changed"):
            await local_helper.embed(client.state, ["text"])
        assert (await client.get("/api/helper")).json()["search"] == {"mode": "keyword_only", "reason": "model_changed"}
        path.unlink()
        with pytest.raises(HelperUnavailable, match="model_missing"):
            await local_helper.embed(client.state, ["text"])
        assert fake.launches == []
        status = (await client.get("/api/helper")).json()
        assert status["search"] == {"mode": "keyword_only", "reason": "model_missing"}
        assert status["models"][0]["installed"] is False


@pytest.mark.asyncio
async def test_the_reranker_is_cancelled_by_ending_its_own_process(tmp_path, fake, timings):
    """Ticket 70's cancellation: past the deadline the reranker's own process is ended within a bounded
    grace period, and embedding work, in its own process, goes on."""
    async with app(tmp_path, fake) as client:
        path = tmp_path / "reranker.gguf"
        path.write_bytes(RERANKER_WEIGHTS)
        reranker = client.local.add(RERANKER_PIN, path)
        documents = [f"passage {i}" for i in range(24)]
        scores = await reranker.rerank("a question", documents, deadline=2)
        assert scores == [1.0 / (i + 1) for i in range(24)]
        assert "--reranking" in fake.launches[0]["argv"] and "--embedding" not in fake.launches[0]["argv"]
        await local_helper.embed(client.state, ["text"])
        reranker_pid, embedding_pid = fake.launches[0]["pid"], fake.launches[1]["pid"]
        assert len(local_helper.urls(client.state)) == 2  # one process, and one address, per model
        client.remote.hold["/v1/rerank"] = asyncio.Event()  # work that would run past the deadline
        began = time.monotonic()
        embedding = asyncio.create_task(local_helper.embed(client.state, ["meanwhile"], query=True))
        assert await reranker.rerank("a question", documents, deadline=0.2) is None
        grace = time.monotonic() - began - 0.2
        assert grace < 1.0  # ended and reaped within the grace period
        assert not alive(reranker_pid) and reranker.state == "stopped" and reranker.failures == 0
        assert alive(embedding_pid) and await embedding == [[0.5] * 4]
        assert helper(client).state == "running"
        del client.remote.hold["/v1/rerank"]
        assert await reranker.rerank("a question", documents, deadline=2) is not None  # started again
        assert len(fake.launches) == 3


@pytest.mark.asyncio
@pytest.mark.parametrize("score", [float("nan"), float("inf"), "0.9", None, True, 10 ** 400])
async def test_a_reranking_score_that_is_not_a_finite_number_is_refused(tmp_path, fake, timings, score):
    async with app(tmp_path, fake) as client:
        path = tmp_path / "reranker.gguf"
        path.write_bytes(RERANKER_WEIGHTS)
        reranker = client.local.add(RERANKER_PIN, path)
        client.remote.scores = [0.5, score]
        with pytest.raises(HelperUnavailable, match="request_failed"):
            await reranker.rerank("a question", ["one", "two"], deadline=2)


@pytest.mark.asyncio
@pytest.mark.parametrize("rows", [
    [{"index": 0, "embedding": [0.5, True]}],
    [{"index": 0, "embedding": [0.5, 10 ** 400]}],  # an integer no float holds
    [{"index": 0, "embedding": [0.5, float("nan")]}],
    [{"index": True, "embedding": [0.5, 0.5]}],
    [{"index": "0", "embedding": [0.5, 0.5]}],
    [{"embedding": [0.5, 0.5]}],
    [{"index": 0, "embedding": [0.5]}, {"index": 0, "embedding": [0.5]}],  # the same text twice, the other none
], ids=["boolean", "huge integer", "nan", "boolean index", "text index", "no index", "repeated index"])
async def test_an_embedding_reply_that_is_not_one_vector_of_real_numbers_per_text_is_refused(tmp_path, fake, timings,
                                                                                         rows):
    async with app(tmp_path, fake) as client:
        client.remote.rows = rows
        with pytest.raises(HelperUnavailable, match="request_failed"):
            await local_helper.embed(client.state, ["text"] * len(rows), query=True)


def test_lifecycle_values_come_from_the_helper_settings(tmp_path):
    (tmp_path / "config.toml").write_text(
        "[helper]\nidle_stop_minutes = 2\nstart_seconds = 12.5\nhealth_seconds = 20\nrestart_backoff_seconds = [2, 4]\n")
    local = local_helper.Local({"data_dir": tmp_path}, Config(binary=None))
    assert local.timings() == {"idle": 120, "start": 12.5, "health": 20, "backoff": [2, 4]}
    (tmp_path / "config.toml").write_text('[helper]\nstart_seconds = 0\nrestart_backoff_seconds = []\nmodel_source = "x"\n')
    local = local_helper.Local({"data_dir": tmp_path}, Config(binary=None))
    assert local.timings() == {"idle": 600, "start": 30, "health": 30, "backoff": [1, 5, 30]}  # defaults


# Downloads and imports


HF_URL = PIN["sources"]["huggingface"]["url"]
MS_URL = PIN["sources"]["modelscope"]["url"]


def serve(client, url, body, via=None):
    """The source answers url, through a redirect to the file host via when given."""
    if via is not None:
        client.remote.files[url] = (302, {"Location": via}, b"")
        url = via
    client.remote.files[url] = (200, {}, body)


async def downloaded(client, **body):
    response = await client.post("/api/helper/models/download", json={"model": EMBEDDING, **body})
    assert response.status_code == 202, response.text
    await until(lambda: client.local.download.state != "running")
    return (await client.get("/api/helper")).json()


def model_file(tmp_path):
    return tmp_path / "data" / "models" / EMBEDDING / PIN["file"]


# An installed model's folder: the model and its license files (slice 1 section 18).
INSTALLED = sorted([PIN["file"], "LICENSE", "SOURCE.txt"])
NOTICES = Path(__file__).resolve().parents[1] / "tools" / "notices" / "Qwen3-Embedding-0.6B"


def leftovers(tmp_path):
    folder = tmp_path / "data" / "models" / EMBEDDING
    return sorted(path.name for path in folder.iterdir()) if folder.exists() else []


@pytest.mark.asyncio
@pytest.mark.parametrize("source, url, cdn", [("huggingface", HF_URL, HF_CDN), ("modelscope", MS_URL, MS_CDN)])
async def test_a_download_is_verified_installed_and_sent_through_general(tmp_path, fake, timings, source, url, cdn):
    async with app(tmp_path, fake, install=False) as client:
        project_id = (await add_project(client, "private"))
        serve(client, url, WEIGHTS, via=cdn)
        status = await downloaded(client, source=source, project_id=project_id)
        assert status["download"] == {"model": EMBEDDING, "source": source, "total": len(WEIGHTS),
                                      "received": len(WEIGHTS), "state": "done", "problem": None}
        assert status["models"][0]["installed"] is True and status["search"]["mode"] == "hybrid"
        assert status["model_source"] == source  # the mirror chosen is remembered
        path = model_file(tmp_path)
        assert path.read_bytes() == WEIGHTS and leftovers(tmp_path) == INSTALLED
        for name in ("LICENSE", "SOURCE.txt"):  # its license files, as the app ships them, owner-only
            assert (path.parent / name).read_bytes() == (NOTICES / name).read_bytes()
            assert stat.S_IMODE((path.parent / name).stat().st_mode) == 0o600
        assert stat.S_IMODE(path.stat().st_mode) == 0o600
        assert stat.S_IMODE(path.parent.stat().st_mode) == 0o700
        assert stat.S_IMODE(path.parent.parent.stat().st_mode) == 0o700
        assert client.remote.source_requests == [url, cdn]
        rows = (await outbound(client))  # app-wide: the General project's client, whichever project asked
        assert [(p, row["kind"], row["decision"], row["method"]) for p, row in rows] == [
            ((await general(client)), "model_download", "allow", "GET")] * 2
        assert await local_helper.embed(client.state, ["text"]) == [[0.5] * 4]


@pytest.mark.asyncio
async def test_a_download_cancelled_midway_leaves_nothing_installed(tmp_path, fake, timings):
    async with app(tmp_path, fake, install=False) as client:
        gate = asyncio.Event()
        body = Streamed(WEIGHTS, piece=256, gate=gate)
        serve(client, HF_URL, body, via=HF_CDN)
        response = await client.post("/api/helper/models/download", json={"model": EMBEDDING, "source": "huggingface"})
        assert response.status_code == 202
        await until(lambda: body.sent == 1 and client.local.download.received == 256)
        assert leftovers(tmp_path) == [PIN["file"] + ".part"]
        assert stat.S_IMODE((model_file(tmp_path).parent / (PIN["file"] + ".part")).stat().st_mode) == 0o600
        status = (await client.delete("/api/helper/models/download")).json()
        assert status["download"]["state"] == "cancelled" and status["models"][0]["installed"] is False
        assert leftovers(tmp_path) == []
        assert status["search"] == {"mode": "keyword_only", "reason": "model_missing"}
        gate.set()
        assert (await client.delete("/api/helper/models/download")).json()["code"] == "no_download"


@pytest.mark.asyncio
@pytest.mark.parametrize("body, problem", [
    (WEIGHTS.replace(b"synthetic", b"Synthetic"), "hash_mismatch"),  # same size, other bytes
    (WEIGHTS + b"!", "size_mismatch"),
    (WEIGHTS[:-1], "size_mismatch"),
])
async def test_a_download_that_differs_from_its_pin_installs_nothing(tmp_path, fake, timings, body, problem):
    async with app(tmp_path, fake, install=False) as client:
        serve(client, HF_URL, body, via=HF_CDN)
        status = await downloaded(client, source="huggingface")
        assert status["download"]["state"] == "failed" and status["download"]["problem"] == problem
        assert status["models"][0]["installed"] is False and leftovers(tmp_path) == []


@pytest.mark.asyncio
@pytest.mark.parametrize("location", ["https://evil.example/model.gguf", MS_CDN, "https://cdn-lfs.hf.co/model.gguf"])
async def test_a_redirect_to_a_host_not_allowed_is_refused(tmp_path, fake, timings, location):
    async with app(tmp_path, fake, install=False) as client:
        serve(client, HF_URL, WEIGHTS, via=location)
        status = await downloaded(client, source="huggingface")
        assert status["download"]["problem"] == "redirect_refused"
        assert client.remote.source_requests == [HF_URL]  # the other host got nothing
        assert [(row["decision"], row["reason"]) for _, row in (await outbound(client))] == [
            ("allow", None), ("deny", "cross_origin_redirect")]
        assert leftovers(tmp_path) == [] and status["models"][0]["installed"] is False


@pytest.mark.asyncio
@pytest.mark.parametrize("where", ["source", "file host"])
async def test_a_source_that_cannot_be_reached_makes_modelscope_the_recommendation(tmp_path, fake, timings, where):
    """Spec 1265: ModelScope is recommended when Hugging Face cannot be reached, whether its own host
    or the file host it sends its files from does not answer (the coordinator's reading); a refusal is
    an answer, so it recommends nothing."""
    async with app(tmp_path, fake, install=False) as client:
        status = (await client.get("/api/helper")).json()
        assert status["recommended_source"] is None and status["model_source"] is None

        client.remote.files[HF_URL] = (302, {"Location": HF_CDN}, b"")
        client.remote.files[HF_CDN] = (403, {}, b"denied")  # answered: refused, not unreachable
        status = await downloaded(client, source="huggingface")
        assert status["download"]["problem"] == "source_refused" and status["recommended_source"] is None

        client.remote.files[HF_URL if where == "source" else HF_CDN] = ("unreachable", {}, b"")
        status = await downloaded(client, source="huggingface")
        assert status["download"]["problem"] == "source_unreachable"
        assert status["recommended_source"] == "modelscope" and status["model_source"] == "huggingface"

        client.remote.files[MS_URL] = (302, {"Location": MS_CDN}, b"")  # and the reverse: ModelScope's file host
        client.remote.files[MS_CDN] = ("unreachable", {}, b"")
        status = await downloaded(client, source="modelscope")
        assert status["download"]["problem"] == "source_unreachable" and status["recommended_source"] is None

        serve(client, MS_URL, WEIGHTS, via=MS_CDN)
        status = await downloaded(client, source="modelscope")
        assert status["download"]["state"] == "done" and status["model_source"] == "modelscope"


@pytest.mark.asyncio
async def test_a_local_only_project_offers_no_download(tmp_path, fake, timings):
    async with app(tmp_path, fake, install=False) as client:
        project_id = (await add_project(client, "local_only"))
        serve(client, HF_URL, WEIGHTS, via=HF_CDN)
        response = await client.post("/api/helper/models/download",
                                     json={"model": EMBEDDING, "source": "huggingface", "project_id": project_id})
        assert response.status_code == 409 and response.json()["code"] == "local_only_no_download"
        assert client.local.download is None and client.remote.source_requests == [] and (await outbound(client)) == []
        status = (await client.get("/api/helper")).json()
        assert status["search"] == {"mode": "keyword_only", "reason": "model_missing"}
        assert status["model_source"] is None  # nothing remembered either
        response = await client.post("/api/helper/models/download",
                                     json={"model": EMBEDDING, "source": "huggingface", "project_id": new_id()})
        assert response.status_code == 404


@pytest.mark.asyncio
async def test_reading_the_status_sends_and_writes_nothing(tmp_path, fake, timings):
    """Consent declined: the screen read the status and the researcher chose Cancel."""
    async with app(tmp_path, fake, install=False) as client:
        for _ in range(3):
            status = (await client.get("/api/helper")).json()
        assert status["models"] == [{
            "id": EMBEDDING, "name": PIN["name"], "file": PIN["file"], "size": PIN["size"], "sha256": PIN["sha256"],
            "license": "Apache-2.0", "folder": str(tmp_path / "data" / "models" / EMBEDDING),
            "sources": {"huggingface": "https://huggingface.co/Qwen/Qwen3-Embedding-0.6B-GGUF",
                        "modelscope": "https://modelscope.cn/models/Qwen/Qwen3-Embedding-0.6B-GGUF"},
            "installed": False}]
        assert status["download"] is None and status["helper"]["state"] == "stopped"
        assert (await outbound(client)) == [] and client.remote.source_requests == [] and fake.launches == []
        assert not (tmp_path / "data" / "models").exists()
        config = tmp_path / "data" / "config.toml"
        assert not config.exists() or "model_source" not in config.read_text()


@pytest.mark.asyncio
async def test_downloads_are_refused_when_one_runs_when_installed_or_without_disk_space(tmp_path, fake, timings,
                                                                                        monkeypatch):
    async with app(tmp_path, fake, install=False) as client:
        gate = asyncio.Event()
        serve(client, HF_URL, Streamed(WEIGHTS, piece=256, gate=gate), via=HF_CDN)
        body = {"model": EMBEDDING, "source": "huggingface"}
        assert (await client.post("/api/helper/models/download", json=body)).status_code == 202
        again = await client.post("/api/helper/models/download", json=body)
        assert again.status_code == 409 and again.json()["code"] == "download_running"
        refused = await client.post("/api/helper/models/import", json={"model": EMBEDDING, "path": str(tmp_path / "x")})
        assert refused.json()["code"] == "download_running"
        gate.set()
        await until(lambda: client.local.download.state == "done")
        again = await client.post("/api/helper/models/download", json=body)
        assert again.status_code == 409 and again.json()["code"] == "already_installed"
        model_file(tmp_path).unlink()
        monkeypatch.setattr(local_helper, "_free_bytes", lambda folder: len(WEIGHTS) - 1)
        full = await client.post("/api/helper/models/download", json=body)
        assert full.status_code == 507 and full.json()["code"] == "disk_full"
        for wrong, code in (({"model": "other", "source": "huggingface"}, "unknown_model"),
                            ({"model": EMBEDDING, "source": "elsewhere"}, "invalid_request")):
            assert (await client.post("/api/helper/models/download", json=wrong)).json()["code"] == code


@pytest.mark.asyncio
async def test_an_import_is_verified_like_a_download(tmp_path, fake, timings):
    async with app(tmp_path, fake, install=False) as client:
        offline = tmp_path / "offline"
        offline.mkdir()
        cases = [
            (offline / "other.gguf", WEIGHTS.replace(b"synthetic", b"Synthetic"), "hash_mismatch"),
            (offline / "short.gguf", WEIGHTS[:-1], "size_mismatch"),
            (offline / "missing.gguf", None, "file_not_found"),
            (offline, None, "not_a_file"),
            (offline / "unreadable.gguf", WEIGHTS, "file_unreadable"),
        ]
        for path, content, code in cases:
            if content is not None:
                path.write_bytes(content)
            if code == "file_unreadable":
                path.chmod(0)
            response = await client.post("/api/helper/models/import", json={"model": EMBEDDING, "path": str(path)})
            assert response.status_code == 400 and response.json()["code"] == code, path
            assert leftovers(tmp_path) == []
        relative = await client.post("/api/helper/models/import", json={"model": EMBEDDING, "path": "model.gguf"})
        assert relative.json()["code"] == "invalid_path"
        good = offline / "Qwen3-Embedding-0.6B-Q8_0.gguf"
        good.write_bytes(WEIGHTS)
        response = await client.post("/api/helper/models/import", json={"model": EMBEDDING, "path": str(good)})
        assert response.status_code == 200 and response.json()["models"][0]["installed"] is True
        assert model_file(tmp_path).read_bytes() == WEIGHTS and leftovers(tmp_path) == INSTALLED
        assert (model_file(tmp_path).parent / "LICENSE").read_bytes() == (NOTICES / "LICENSE").read_bytes()
        assert stat.S_IMODE(model_file(tmp_path).stat().st_mode) == 0o600
        assert (await outbound(client)) == []  # an import sends nothing
        assert await local_helper.embed(client.state, ["text"]) == [[0.5] * 4]


@pytest.mark.asyncio
async def test_what_a_crash_left_of_a_download_is_removed_at_launch(tmp_path, fake, timings):
    folder = tmp_path / "data" / "models" / EMBEDDING
    folder.mkdir(parents=True)
    (folder / (PIN["file"] + ".part")).write_bytes(WEIGHTS[:100])
    async with app(tmp_path, fake, install=False) as client:
        assert leftovers(tmp_path) == []
        assert (await client.get("/api/helper")).json()["models"][0]["installed"] is False


@pytest.mark.asyncio
async def test_closing_the_app_during_a_download_leaves_nothing(tmp_path, fake, timings):
    async with app(tmp_path, fake, install=False) as client:
        serve(client, HF_URL, Streamed(WEIGHTS, piece=256, gate=asyncio.Event()), via=HF_CDN)
        assert (await client.post("/api/helper/models/download",
                                  json={"model": EMBEDDING, "source": "huggingface"})).status_code == 202
        await until(lambda: client.local.download.received > 0)
        download = client.local.download
    assert download.state == "cancelled" and leftovers(tmp_path) == []


@pytest.mark.asyncio
async def test_downloads_or_imports_asked_for_together_start_one(tmp_path, fake, timings):
    async with app(tmp_path, fake, install=False) as client:
        gate = asyncio.Event()
        serve(client, HF_URL, Streamed(WEIGHTS, piece=256, gate=gate), via=HF_CDN)
        offline = tmp_path / "Qwen3-Embedding-0.6B-Q8_0.gguf"
        offline.write_bytes(WEIGHTS)
        body = {"model": EMBEDDING, "source": "huggingface"}
        answers = await asyncio.gather(*(client.post("/api/helper/models/download", json=body) for _ in range(2)))
        assert sorted(response.status_code for response in answers) == [202, 409]
        assert [r.json()["code"] for r in answers if r.status_code == 409] == ["download_running"]
        refused = await client.post("/api/helper/models/import", json={"model": EMBEDDING, "path": str(offline)})
        assert refused.json()["code"] == "download_running"
        gate.set()
        await until(lambda: client.local.download.state == "done")
        assert client.remote.source_requests == [HF_URL, HF_CDN]  # one download, from one request

        model_file(tmp_path).unlink()
        answers = await asyncio.gather(
            client.post("/api/helper/models/import", json={"model": EMBEDDING, "path": str(offline)}),
            client.post("/api/helper/models/download", json=body))
        assert sorted(response.status_code for response in answers) in ([200, 409], [202, 409])  # either one
        assert [r.json()["code"] for r in answers if r.status_code == 409] == ["download_running"]
        await until(lambda: not client.local._busy())
        assert model_file(tmp_path).read_bytes() == WEIGHTS and leftovers(tmp_path) == INSTALLED


_private_file = local_helper._private_file


class _Failing:
    """The download's .part file, which fails at its first write."""

    def __init__(self, path, error):
        self.out, self.error = _private_file(path), error

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.out.close()

    def write(self, data):
        self.out.write(data)
        raise self.error


@pytest.mark.asyncio
@pytest.mark.parametrize("failure, problem", [
    ("not_served", "source_refused"),
    ("reset", "download_interrupted"),
    (OSError(errno.ENOSPC, "No space left on device"), "disk_full"),
    (OSError(errno.EIO, "Input/output error"), "write_failed"),
    (RuntimeError("unexpected"), "download_failed"),
    ("held", "database_unavailable"),  # a restore holds the database's writes: the gate cannot record it
])
async def test_a_download_that_fails_on_the_way_installs_nothing_and_says_why(tmp_path, fake, timings, monkeypatch,
                                                                              failure, problem):
    with app_log(tmp_path) as log_text:
        await _fails_on_the_way(tmp_path, fake, monkeypatch, failure, problem)
    # The app's default log says what failed, never where: no path, no file name, no URL.
    assert f"a model download failed ({problem})" in log_text() and names_nothing(log_text(), tmp_path)


async def _fails_on_the_way(tmp_path, fake, monkeypatch, failure, problem):
    async with app(tmp_path, fake, install=False) as client:
        if failure == "not_served":
            client.remote.files[HF_URL] = (302, {"Location": HF_CDN}, b"")
            client.remote.files[HF_CDN] = (403, {}, b"denied")
        else:
            serve(client, HF_URL, Streamed(WEIGHTS, piece=256, fail_after=2 if failure == "reset" else None), via=HF_CDN)
        if isinstance(failure, Exception):
            monkeypatch.setattr(local_helper, "_private_file", lambda path: _Failing(path, failure))
        db = client.state["db"]
        if failure == "held":
            await asyncio.to_thread(db.hold_writes)
        try:
            status = await downloaded(client, source="huggingface")
        finally:
            await asyncio.to_thread(db.release_writes)
        assert status["download"]["state"] == "failed" and status["download"]["problem"] == problem
        assert status["models"][0]["installed"] is False and leftovers(tmp_path) == []
        assert client.local.download.task.done()  # never left running: the next download may start
        monkeypatch.setattr(local_helper, "_private_file", _private_file)
        serve(client, HF_URL, WEIGHTS, via=HF_CDN)
        assert (await downloaded(client, source="huggingface"))["download"]["state"] == "done"


@pytest.mark.asyncio
async def test_a_partial_file_that_cannot_be_removed_is_logged_by_kind_and_answered_with_a_code(tmp_path, fake,
                                                                                               timings, monkeypatch):
    real_unlink = Path.unlink

    def unlink(self, missing_ok=False):  # as when the folder lost its write permission
        if self.name.endswith(".part") and self.exists():
            raise PermissionError(errno.EACCES, "Permission denied", str(self))
        return real_unlink(self, missing_ok=missing_ok)

    changed = WEIGHTS.replace(b"synthetic", b"Synthetic")
    with app_log(tmp_path) as log_text:
        async with app(tmp_path, fake, install=False) as client:
            monkeypatch.setattr(Path, "unlink", unlink)
            serve(client, HF_URL, changed, via=HF_CDN)
            status = await downloaded(client, source="huggingface")
            assert status["download"]["problem"] == "hash_mismatch" and status["models"][0]["installed"] is False
            other = tmp_path / "other.gguf"
            other.write_bytes(changed)
            response = await client.post("/api/helper/models/import", json={"model": EMBEDDING, "path": str(other)})
            assert response.status_code == 400 and response.json()["code"] == "hash_mismatch"
            assert leftovers(tmp_path) == [PIN["file"] + ".part"]  # never installed; left for the next launch
        async with app(tmp_path, fake, install=False) as client:  # which cannot remove it either, and starts
            assert leftovers(tmp_path) == [PIN["file"] + ".part"] and client.local.download is None
            monkeypatch.setattr(Path, "unlink", real_unlink)
        async with app(tmp_path, fake, install=False) as client:
            assert leftovers(tmp_path) == []
    text = log_text()
    assert text.count("a partial model file could not be removed (EACCES)") == 3 and names_nothing(text, tmp_path)


def _fsyncs(monkeypatch, fail=lambda fd, path, calls: False):
    """os.fsync and os.replace, recorded as (call, file name), with fsync failing (EIO) where fail says,
    given the calls so far."""
    calls, real_fsync, real_replace = [], os.fsync, os.replace

    def fsync(fd):
        path = path_of(fd)
        calls.append(("fsync", Path(path).name))
        if fail(fd, path, calls):
            raise OSError(errno.EIO, "Input/output error")
        return real_fsync(fd)

    def replace(source, target):
        calls.append(("replace", Path(source).name, Path(target).name))
        return real_replace(source, target)

    monkeypatch.setattr(os, "fsync", fsync)
    monkeypatch.setattr(os, "replace", replace)
    return calls


async def _install(client, tmp_path, how, body=WEIGHTS):
    """Download or import the model: (whether it went through, the problem's code)."""
    if how == "download":
        serve(client, HF_URL, body, via=HF_CDN)
        download = (await downloaded(client, source="huggingface"))["download"]
        return download["state"] == "done", download["problem"]
    offline = tmp_path / f"offline-{time.monotonic_ns()}.gguf"
    offline.write_bytes(body)
    response = await client.post("/api/helper/models/import", json={"model": EMBEDDING, "path": str(offline)})
    return response.status_code == 200, response.json().get("code")


@pytest.mark.asyncio
@pytest.mark.parametrize("how", ["download", "import"])
async def test_a_model_is_synced_before_it_is_renamed_into_place_and_its_folder_after(tmp_path, fake, timings,
                                                                                      monkeypatch, how):
    async with app(tmp_path, fake, install=False) as client:
        calls = _fsyncs(monkeypatch)
        assert await _install(client, tmp_path, how) == (True, None)
        part, file, folder = PIN["file"] + ".part", PIN["file"], EMBEDDING
        synced, renamed = calls.index(("fsync", part)), calls.index(("replace", part, file))
        assert synced < renamed and ("fsync", folder) in calls[renamed:]


@pytest.mark.asyncio
@pytest.mark.parametrize("how", ["download", "import"])
async def test_a_model_whose_contents_cannot_be_synced_is_not_installed(tmp_path, fake, timings, monkeypatch, how):
    async with app(tmp_path, fake, install=False) as client:
        _fsyncs(monkeypatch, fail=lambda fd, path, calls: path.endswith(".part"))
        assert await _install(client, tmp_path, how) == (False, "write_failed")
        assert leftovers(tmp_path) == [] and (await client.get("/api/helper")).json()["models"][0]["installed"] is False


@pytest.mark.asyncio
@pytest.mark.parametrize("how", ["download", "import"])
async def test_a_failure_after_the_model_is_renamed_into_place_still_counts_it_installed(tmp_path, fake, timings,
                                                                                         monkeypatch, how):
    path = model_file(tmp_path)
    path.parent.mkdir(parents=True)
    path.write_bytes(WEIGHTS.replace(b"synthetic", b"Synthetic"))
    with app_log(tmp_path) as log_text:
        async with app(tmp_path, fake, install=False) as client:
            await until(lambda: helper(client).problem == "model_changed")
            # The folder's sync, once the model is renamed into place, fails.
            _fsyncs(monkeypatch, fail=lambda fd, synced, calls: stat.S_ISDIR(os.fstat(fd).st_mode)
                    and ("replace", path.name + ".part", path.name) in calls)
            assert await _install(client, tmp_path, how) == (True, None)  # installed, and said so
            assert helper(client).problem is None and path.read_bytes() == WEIGHTS
            assert (await client.get("/api/helper")).json()["search"] == {"mode": "hybrid", "reason": None}
    text = log_text()
    assert "the model folder could not be synced after an install (EIO)" in text and names_nothing(text, tmp_path)


@pytest.mark.asyncio
@pytest.mark.parametrize("how", ["download", "import"])
async def test_without_the_models_license_file_nothing_is_installed(tmp_path, fake, timings, how):
    notices = tmp_path / "notices" / "Qwen3-Embedding-0.6B"
    notices.mkdir(parents=True)
    (notices / "SOURCE.txt").write_bytes((NOTICES / "SOURCE.txt").read_bytes())  # LICENSE is missing
    async with app(tmp_path, fake, install=False, notices=notices.parent) as client:
        assert await _install(client, tmp_path, how) == (False, "notice_missing")
        assert leftovers(tmp_path) == []
        (notices / "LICENSE").write_text("not the license")  # nor is another text taken for it
        assert await _install(client, tmp_path, how) == (False, "notice_missing")
        (notices / "LICENSE").write_bytes((NOTICES / "LICENSE").read_bytes())
        assert await _install(client, tmp_path, how) == (True, None) and leftovers(tmp_path) == INSTALLED


def test_the_models_license_files_are_pinned_and_ship_with_the_app():
    from tools import license_audit
    notice = EMBEDDING_MODEL["notice"]
    assert {name: hashlib.sha256((NOTICES / name).read_bytes()).hexdigest() for name in notice["files"]} \
        == notice["files"]
    # LICENSE is Apache-2.0's standard text, as apache.org publishes it (LICENSE-2.0.txt).
    assert notice["files"]["LICENSE"] == "cfc7749b96f63bd31c3c42b5c471bf756814053e847c10f3eb003417bc523d30"
    license, files = license_audit.component(notice["folder"])
    assert license == "Apache-2.0" and sorted(dest for _, dest in files) == sorted(notice["files"])
    assert notice["folder"] in license_audit._shipped_by_default()  # in every bundle's licenses folder


@pytest.mark.asyncio
@pytest.mark.parametrize("reason, problem", [("revoked", "database_unavailable"),
                                             ("gate_inputs_unavailable", "download_refused")])
async def test_a_download_the_gate_refuses_says_why_in_the_researchers_terms(tmp_path, fake, timings, monkeypatch,
                                                                             reason, problem):
    def refuse(request):
        raise OutboundDenied(reason, None)

    async def refusing(project_id=None, **options):
        return httpx.AsyncClient(transport=httpx.MockTransport(refuse))

    async with app(tmp_path, fake, install=False) as client:
        monkeypatch.setattr(client.local, "client", refusing)
        status = await downloaded(client, source="huggingface")
        assert status["download"]["problem"] == problem and leftovers(tmp_path) == []


@pytest.mark.asyncio
async def test_a_download_cancelled_before_it_began_is_cancelled_not_running(tmp_path, fake, timings):
    async with app(tmp_path, fake, install=False) as client:
        serve(client, HF_URL, WEIGHTS, via=HF_CDN)
        await client.local.start_download(EMBEDDING, "huggingface")
        await client.local.cancel_download()  # before its task took a step
        assert client.local.download.state == "cancelled" and client.remote.source_requests == []
        assert (await downloaded(client, source="huggingface"))["download"]["state"] == "done"


def test_the_model_source_setting_takes_only_the_two_mirrors(tmp_path):
    from backend.settings import load_settings
    settings = load_settings(tmp_path)
    with pytest.raises(ValueError):
        settings.save({"helper.model_source": "elsewhere"})
    settings.save({"helper.model_source": "modelscope"})
    assert load_settings(tmp_path).values["helper"]["model_source"] == "modelscope"


def test_the_offered_models_and_the_reranker_pins():
    assert set(local_helper.MODELS) == {EMBEDDING}  # no reranker is offered in M2 (ticket 72)
    assert EMBEDDING_MODEL["size"] == 639_150_592 and RERANKER_MODEL["size"] == 639_153_184
    assert set(EMBEDDING_MODEL["sources"]) == set(local_helper.SOURCES)
    assert local_helper.command("/b", "/m", "reranker")[-9:] == [
        "--reranking", "-c", "4096", "-ub", "2048", "-np", "2", "--cache-ram", "0"]
