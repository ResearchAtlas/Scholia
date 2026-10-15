"""The local model helper and its models (slice 1 sections 7.2, 13 and 15; ticket 19's local model
helper; ticket 71's model downloads): the API under Settings, then Advanced.

The helper is llama.cpp's llama-server, bundled beside the app (tools/build_app.sh), one process per
model. The embedding model's starts on demand, at the first embedding request, never at launch, and
stops after `[helper] idle_stop_minutes` without a request. Before every launch the server and the
libraries it links are checked against the SHA-256 manifest the build wrote, and the model file
against its pin. It listens only on 127.0.0.1, on a port the system picks, which its `listening on`
line names once the model has loaded, and takes a key generated at each launch; only this backend
calls it, through the outbound gate (kind local_helper), which learns the running helpers' URLs
from `urls`. One not ready within `[helper] start_seconds` is stopped. A running helper is checked
every `[helper] health_seconds`. A start that fails, a helper that exits or fails a check, is
started again after each of `[helper] restart_backoff_seconds` in turn; after the last, a notice
says it stopped, and it stays stopped until Start again. A question's embeddings go ahead of
indexing batches, which hold at most one of the server's two slots. When the helper cannot serve,
`embed` raises HelperUnavailable, and the status says search is keyword-only, and why.

Models live in `<data folder>/models/<model>/`, owner-only. A download is an explicit, app-wide
action (ticket 71): one at a time, from Hugging Face or ModelScope as the researcher chose (kept in
`[helper] model_source`), sent through the General project's gated client as a model download,
written to `<file>.part` while it is hashed, and installed only once its size and SHA-256 match the
pin. A `.part` file is never installed: it is removed when its download or import ends, however it
ends, or, when that fails, at the next launch. An import copies a local file the same way. A Local
only project offers no download. Nothing here logs a path, a URL or the helper's own output, which
is read and dropped.
"""

import asyncio
import contextlib
import errno
import hashlib
import json
import logging
import math
import os
import re
import secrets
import shutil
import stat
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

import httpx
from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from fastapi.routing import APIRoute
from pydantic import BaseModel, Field

from backend.db import DatabaseClosedError
from backend.outbound_gate import MODEL_FILE_HOSTS, OutboundDenied
from backend.runs import _through
from backend.settings import SettingsChanged, _make_private_dirs, load_settings, write_private

log = logging.getLogger(__name__)

HF, MODELSCOPE = "https://huggingface.co", "https://modelscope.cn"
EMBEDDING = "qwen3-embedding-0.6b"  # the model's id in the API, and its folder under models/
_EMBEDDING_FILE = "Qwen3-Embedding-0.6B-Q8_0.gguf"
_EMBEDDING_HF = f"{HF}/Qwen/Qwen3-Embedding-0.6B-GGUF/resolve/370f27d7550e0def9b39c1f16d3fbaa13aa67728/{_EMBEDDING_FILE}"
# Qwen's own Q8_0 GGUF of Qwen3-Embedding-0.6B (Apache-2.0), at a pinned revision of the publisher's
# repository on each mirror. Both serve the same file (slice 1 section 15, Mirrors): the same size
# and SHA-256 from each, checked on 2026-10-08.
EMBEDDING_MODEL = {
    "id": EMBEDDING,
    "name": "Qwen3-Embedding-0.6B Q8_0",
    "kind": "embedding",
    "file": _EMBEDDING_FILE,
    "size": 639_150_592,
    "sha256": "06507c7b42688469c4e7298b0a1e16deff06caf291cf0a5b278c308249c3e439",
    "license": "Apache-2.0",
    # Its license files (slice 1 section 18), installed beside it from the copies the app ships
    # (tools/notices, Contents/Resources/licenses): the publisher's repositories ship no license
    # file, only "license: apache-2.0" in the model card, so LICENSE is Apache-2.0's standard text
    # (apache.org's LICENSE-2.0.txt) and SOURCE.txt names the model and where it comes from.
    "notice": {"folder": "Qwen3-Embedding-0.6B", "files": {
        "LICENSE": "cfc7749b96f63bd31c3c42b5c471bf756814053e847c10f3eb003417bc523d30",
        "SOURCE.txt": "6b2b4a08638f6dbee4566d3c984b5c8c0856e1472ffbadb1389910b9d882c524"}},
    "url": _EMBEDDING_HF,  # what tools/fetch.py downloads for CI's self-test
    "sources": {
        "huggingface": {"repository": f"{HF}/Qwen/Qwen3-Embedding-0.6B-GGUF", "url": _EMBEDDING_HF},
        "modelscope": {"repository": f"{MODELSCOPE}/models/Qwen/Qwen3-Embedding-0.6B-GGUF",
                       "url": f"{MODELSCOPE}/models/Qwen/Qwen3-Embedding-0.6B-GGUF/resolve/"
                              f"a6804f0dece24ece939f52ffa3316fd650ca1839/{_EMBEDDING_FILE}"},
    },
}
_RERANKER_HF = (f"{HF}/ggml-org/Qwen3-Reranker-0.6B-Q8_0-GGUF/resolve/a02f48bb4f057028298c21fa033da2b30d7742d5/"
                "qwen3-reranker-0.6b-q8_0.gguf")
# ggml-org's Q8_0 GGUF of Qwen3-Reranker-0.6B (Apache-2.0, as its repository's card states), from its
# Hugging Face repository only: for ticket 70's cancellation test and comparison. It is never
# offered (no reranker can qualify in M2), so it is not in MODELS.
RERANKER_MODEL = {
    "id": "qwen3-reranker-0.6b",
    "name": "Qwen3-Reranker-0.6B Q8_0",
    "kind": "reranker",
    "file": "qwen3-reranker-0.6b-q8_0.gguf",
    "size": 639_153_184,
    "sha256": "22c9979ce4fbcdc5acdc310c6641c32797eff1aa980b8f7a2db8a8ea23429a48",
    "license": "Apache-2.0",
    "url": _RERANKER_HF,
    "sources": {"huggingface": {"repository": f"{HF}/ggml-org/Qwen3-Reranker-0.6B-Q8_0-GGUF", "url": _RERANKER_HF}},
}
MODELS = {EMBEDDING: EMBEDDING_MODEL}  # what Settings offers
SOURCES = ("huggingface", "modelscope")

# Per process: context, physical batch and slots (section 13), about 0.46 GB of cache each; and no
# prompt cache, which llama-server keeps by default (up to 8 GiB of host memory per process) and an
# embedding or reranking server never reuses: measured in S1-16, a helper grew to 12 GB with it.
LIMITS = ["-c", "4096", "-ub", "2048", "-np", "2", "--cache-ram", "0"]
SLOTS = 2
# Texts per indexing request. The server queues each text of a request as a task of its own, so a
# question sent during indexing waits behind the texts already queued. Measured in S1-16 (120 s of
# indexing per size, with a question 0.25 s after each answer): 32 texts per request, question p95
# 4.5 s; 8, 1.2 s; 4, 0.65 s; 1, 0.29 s with none over the 500 ms retrieval deadline, at 8.5
# passages a second against 11.2 with 32.
INDEXING_INPUTS = 1
FLAGS = {"embedding": ["--embedding", "--pooling", "last"], "reranker": ["--reranking"]}
LISTENING = re.compile(rb"listening on http://127\.0\.0\.1:(\d+)")
MANIFEST = "Resources/llama-server.sha256.json"  # under the app's Contents folder; see write_manifest
STOP_SECONDS = 3  # from SIGTERM to SIGKILL
HEALTH_TIMEOUT = httpx.Timeout(5.0)
REQUEST_TIMEOUT = httpx.Timeout(120.0, connect=5.0)
DOWNLOAD_TIMEOUT = httpx.Timeout(60.0, connect=15.0)
MAX_REDIRECTS = 3
PART = ".part"
_FULL = (errno.ENOSPC, errno.EDQUOT)
# Problems a check before a launch finds: no restart helps, so the helper waits for the next request.
_CHECKS = ("model_missing", "model_changed", "binary_missing", "binary_changed")


def _finite(value) -> bool:
    """A real number a float holds: not a boolean, NaN, an infinity or an integer too large."""
    if type(value) not in (int, float):
        return False
    try:
        return math.isfinite(value)
    except OverflowError:
        return False


def _index(row, count) -> int | None:
    """A reply row's index, when it is an integer below count (never a boolean)."""
    index = row.get("index") if isinstance(row, dict) else None
    return index if type(index) is int and 0 <= index < count else None


def _vectors(data, count):
    """The embeddings in a /v1/embeddings reply, in input order, or HelperUnavailable."""
    rows = data.get("data") if isinstance(data, dict) else None
    indexes = [_index(row, count) for row in rows] if isinstance(rows, list) else [None]
    if None in indexes or sorted(indexes) != list(range(count)):  # each text's embedding, once
        raise HelperUnavailable("request_failed")
    vectors = [row.get("embedding") for row in sorted(rows, key=lambda row: row["index"])]
    if not all(isinstance(v, list) and v and all(_finite(x) for x in v) for v in vectors):
        raise HelperUnavailable("request_failed")
    return vectors


class HelperUnavailable(Exception):
    """The helper cannot serve now, so search is keyword-only. reason is a code the interface translates."""

    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


class Refused(Exception):
    """A refused request: status, a stable code the interface translates, and an English message."""

    def __init__(self, status: int, code: str, message: str):
        super().__init__(message)
        self.status, self.code, self.message = status, code, message


class _Exited(Exception):
    """The server exited before it was ready."""


@dataclass(frozen=True)
class Config:
    """create_app's helper option: the server binary (None: this build has none), the models
    offered by id (tests and walkthroughs pass pins of their own) and the license texts the app
    ships, which an installed model's license files are copied from."""
    binary: Path | None = field(default_factory=lambda: bundled_binary())
    models: dict = field(default_factory=lambda: MODELS)
    notices: Path = field(default_factory=lambda: bundled_notices())


def bundled_binary() -> Path | None:
    """llama-server beside the packaged app's executable (Contents/MacOS); none from source."""
    return Path(sys.executable).with_name("llama-server") if getattr(sys, "frozen", False) else None


def bundled_notices() -> Path:
    """The license texts the app ships (tools/license_audit.py): Contents/Resources/licenses in the
    packaged app, tools/notices from source."""
    if getattr(sys, "frozen", False):
        return Path(sys.executable).parents[1] / "Resources" / "licenses"
    return Path(__file__).resolve().parents[1] / "tools" / "notices"


def command(binary, model, kind="embedding") -> list[str]:
    return [str(binary), "-m", str(model), "--offline", "--host", "127.0.0.1", "--port", "0", "--no-webui",
            *FLAGS[kind], *LIMITS]


def _sha256(path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as f:
        while block := f.read(1 << 20):
            digest.update(block)
    return digest.hexdigest()


def mismatch(path: Path, pin: dict) -> str | None:
    """Why the file at `path` differs from `pin` (its size and SHA-256), or None."""
    size = path.stat().st_size
    if size != pin["size"]:
        return f"{path.name}: {size} bytes, expected {pin['size']}"
    digest = _sha256(path)
    if digest != pin["sha256"]:
        return f"{path.name}: SHA-256 {digest}, expected {pin['sha256']}"
    return None


def write_manifest(contents) -> Path:
    """Write the SHA-256 of the helper and every library it links, as signed in the app's Contents
    folder (tools/build_app.sh), where binary_problem reads them before each launch."""
    contents = Path(contents)
    files = ["MacOS/llama-server", *sorted(path.relative_to(contents).as_posix()
                                           for path in (contents / "Frameworks/llama-cpp").glob("*.dylib"))]
    manifest = contents / MANIFEST
    manifest.write_text(json.dumps({name: _sha256(contents / name) for name in files}, indent=2) + "\n")
    return manifest


def binary_problem(binary) -> str | None:
    """Why the helper binary may not be launched, or None: it, and every library the manifest lists
    beside it, has the SHA-256 the build recorded (write_manifest)."""
    if binary is None:
        return "binary_missing"
    contents = Path(binary).parent.parent
    try:
        listed = json.loads((contents / MANIFEST).read_text())
    except (OSError, ValueError):
        return "binary_missing"
    if not isinstance(listed, dict) or listed.get(f"MacOS/{Path(binary).name}") is None:
        return "binary_missing"
    for name, digest in listed.items():
        try:
            if _sha256(contents / name) != digest:
                return "binary_changed"
        except OSError:
            return "binary_missing"
    return None


def model_path(data_dir, pin) -> Path:
    return Path(data_dir) / "models" / pin["id"] / pin["file"]


def installed(path: Path, pin) -> bool:
    """Whether the model's file is in place, by its size; its SHA-256 is checked before each launch."""
    try:
        return path.stat().st_size == pin["size"]
    except OSError:
        return False


async def _launch(binary, model, kind, deadline):
    """Start a server for model and wait, at most deadline seconds, for its `listening on` line.
    Returns (process, port, key). A server that exits, stays silent or is cancelled meanwhile is ended."""
    key = secrets.token_urlsafe(32)
    env = {"LLAMA_API_KEY": key} | {name: os.environ[name] for name in ("HOME", "TMPDIR") if name in os.environ}
    process = await asyncio.create_subprocess_exec(
        *command(binary, model, kind), env=env, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT, limit=1 << 20)
    try:
        port = await asyncio.wait_for(_listening(process.stdout), deadline)
    except BaseException:
        await _end(process, kill=True)
        raise
    return process, port, key


async def _listening(stdout) -> int:
    """The port in the server's `listening on` line, which it writes once the model has loaded."""
    while line := await stdout.readline():
        if match := LISTENING.search(line):
            return int(match.group(1))
    raise _Exited()


async def _drain(stdout):
    """Read the server's output to its end, which is when it exits: unread, the pipe would fill and
    the server would stop. Its lines are dropped (they name files and may echo requests)."""
    while await stdout.read(1 << 16):
        pass


async def _end(process, *, kill=False):
    """Stop a server and reap it: SIGKILL (kill), or SIGTERM and SIGKILL after STOP_SECONDS. A
    cancellation waits for it and is raised after it."""
    async def ending():
        if process.returncode is None:
            with contextlib.suppress(ProcessLookupError):
                process.kill() if kill else process.terminate()
            try:
                await asyncio.wait_for(process.wait(), STOP_SECONDS)
            except TimeoutError:
                with contextlib.suppress(ProcessLookupError):
                    process.kill()
                await process.wait()

    _, cancelled = await _through(ending())
    if cancelled:
        raise asyncio.CancelledError()


class Helper:
    """One model's helper process (see the module's docstring).

    state is "stopped" (problem, if any, is what the last check before a launch found), "starting",
    "running", "restarting" (waiting out a backoff after the failure in problem) or "failed" (the
    notice, after the last backoff)."""

    def __init__(self, local, pin, path, binary):
        self.local, self.pin, self.path, self.binary = local, pin, Path(path), binary
        self.state, self.problem, self.failures, self.ready_seconds = "stopped", None, 0, None
        self._process = self._port = self._key = None
        self._starting = self._watching = self._restarting = None
        self._ending = None  # a stopped process until it is reaped: a start and close() wait for it
        self._slots, self._indexing = asyncio.Semaphore(SLOTS), asyncio.Semaphore(SLOTS - 1)
        self._in_use, self.last_used = 0, time.monotonic()
        self.installs = 0  # model files installed in this process (see Local.verify_installed)

    @property
    def url(self) -> str | None:
        return f"http://127.0.0.1:{self._port}" if self.state == "running" else None

    def public(self) -> dict:
        return {"state": self.state, "problem": self.problem, "failures": self.failures,
                "ready_seconds": self.ready_seconds}

    async def embed(self, texts, *, project_id=None, query=False, admit=None) -> list[list[float]]:
        """One embedding per text, in order. A question's (query) go ahead of indexing batches: a
        batch holds at most one of the server's slots, INDEXING_INPUTS texts at a time, and the
        other stays free for questions. admit(conn), if given, is each request's dispatch check in
        the outbound gate's decision transaction (an index run's, S1-17). Raises HelperUnavailable
        when the helper cannot serve, or the check refuses."""
        texts = list(texts)
        size = len(texts) if query else INDEXING_INPUTS
        vectors = []
        self._in_use += 1
        try:
            async with contextlib.AsyncExitStack() as held:
                if not query:
                    await held.enter_async_context(self._indexing)
                for start in range(0, len(texts), max(size, 1)):
                    async with self._slots:
                        await self._ready()
                        part = texts[start:start + size]
                        vectors += _vectors(await self._post("/v1/embeddings", {"input": part}, project_id, admit),
                                            len(part))
        finally:
            self._in_use -= 1
            self.last_used = time.monotonic()
        return vectors

    async def rerank(self, query, documents, *, deadline, project_id=None) -> list[float] | None:
        """Each document's relevance score, in order, or None when they are not back within deadline
        seconds (ticket 70's cancellation): the reranker's own process is then ended, so none of its
        work goes on, and the next call starts it again. Embedding work runs in another process,
        which this never touches. Raises HelperUnavailable when the helper cannot serve.
        The deadline covers the reranking work only: a stopped reranker is started first (about 0.6 s
        on the reference Mac), which a caller with an end-to-end deadline counts against its own."""
        documents = list(documents)
        await self._ready()
        self._in_use += 1
        try:
            data = await asyncio.wait_for(self._post(
                "/v1/rerank", {"query": query, "documents": documents, "top_n": len(documents)}, project_id), deadline)
        except TimeoutError:
            await self.stop(kill=True)
            return None
        finally:
            self._in_use -= 1
            self.last_used = time.monotonic()
        scores = [None] * len(documents)
        rows = data.get("results") if isinstance(data, dict) else None
        for row in rows if isinstance(rows, list) else []:
            if (index := _index(row, len(documents))) is not None:
                scores[index] = row.get("relevance_score")
        if not all(_finite(score) for score in scores):
            raise HelperUnavailable("request_failed")
        return scores

    async def _post(self, path, body, project_id, admit=None):
        url, key = self.url, self._key
        if url is None:
            raise HelperUnavailable(self.problem or "helper_unavailable")
        try:
            async with await self.local.client(project_id, timeout=REQUEST_TIMEOUT,
                                               **({"admit": admit} if admit is not None else {})) as http:
                response = await http.post(url + path, json=body, headers={"Authorization": f"Bearer {key}"})
            if response.status_code != 200:
                raise HelperUnavailable("request_failed")
            return response.json()
        except (httpx.HTTPError, OutboundDenied, ValueError):
            raise HelperUnavailable("request_failed") from None
        except DatabaseClosedError:  # the gate cannot record a decision: a restore, or the app closing
            raise HelperUnavailable(self.local.unavailable()) from None

    async def _ready(self):
        """Start the helper if it is stopped and wait for it; HelperUnavailable unless it runs."""
        if self.local.closed:
            raise HelperUnavailable("closing")
        if self.state == "stopped" and self._starting is None:
            self._starting = asyncio.create_task(self._start())
        if self._starting is not None:
            try:  # one start for every request; a cancelled request leaves it going
                await asyncio.shield(self._starting)
            except asyncio.CancelledError:
                if asyncio.current_task().cancelling():
                    raise
                # else the start itself was cancelled: the app is closing
        if self.state != "running":
            raise HelperUnavailable("closing" if self.local.closed else self.problem or "helper_unavailable")

    def _check(self) -> str | None:
        """What stops a launch now: the binary or the model file differing from what was pinned."""
        if (problem := binary_problem(self.binary)) is not None:
            return problem
        try:
            return "model_changed" if mismatch(self.path, self.pin) else None
        except OSError:
            return "model_missing"

    async def _start(self):
        times = self.local.timings()
        self.state = "starting"
        try:
            if self._ending is not None:  # the last process is still being ended: one process per model
                await asyncio.wait({self._ending})
            began = time.monotonic()
            if problem := await asyncio.to_thread(self._check):
                self.state, self.problem = "stopped", problem  # checked again at the next request
                return
            self._process, self._port, self._key = await _launch(self.binary, self.path, self.pin["kind"],
                                                                 times["start"])
        except asyncio.CancelledError:
            self.state = "stopped"
            raise
        except TimeoutError:
            self._failed("start_timeout", times)
        except (_Exited, OSError, ValueError):  # ValueError: a log line longer than the reader's limit
            self._failed("start_failed", times)
        else:
            self.state, self.problem = "running", None
            self.ready_seconds = round(time.monotonic() - began, 3)
            self.last_used = time.monotonic()
            self._watching = asyncio.create_task(self._watch(self._process, times))
        finally:
            self._starting = None

    def _failed(self, problem, times):
        """A failed start, an exit or a failed check: start again after the next backoff, or, after
        the last one, stop with the notice."""
        self.failures += 1
        self.problem = problem
        backoff = times["backoff"]
        if self.local.closed:
            self.state = "stopped"
        elif self.failures > len(backoff):
            self.state = "failed"
            log.warning("the local model helper stopped after %d failures (%s)", self.failures, problem)
        else:
            self.state = "restarting"
            self._restarting = asyncio.create_task(self._restart(backoff[self.failures - 1]))

    async def _restart(self, delay):
        await asyncio.sleep(delay)
        self._restarting = None
        if self.state == "restarting" and not self.local.closed:
            self.state = "stopped"
            self._starting = asyncio.create_task(self._start())

    async def _watch(self, process, times):
        """While the server runs: read its output, check its health, stop it once idle, and treat
        an exit or a failed check as a failure."""
        exited = asyncio.ensure_future(_drain(process.stdout))
        checked, problem = time.monotonic(), None
        try:
            while True:
                # while a request is in flight, looked at again after the idle time
                idle_at = (time.monotonic() if self._in_use else self.last_used) + times["idle"]
                wake = min(checked + times["health"], idle_at)
                await asyncio.wait({exited}, timeout=max(0.0, wake - time.monotonic()))
                if exited.done():
                    problem = "crashed"
                    break
                if not self._in_use and time.monotonic() >= self.last_used + times["idle"]:
                    break  # idle: stopped, not a failure
                if time.monotonic() >= checked + times["health"]:
                    healthy = await self._healthy()
                    if healthy is False:
                        problem = "unhealthy"
                        break
                    checked = time.monotonic()
                    if healthy:
                        self.failures = 0
        finally:
            exited.cancel()
        self._watching = None
        if problem:  # marked first: no request starts another process while this one is ended
            self.state, self.problem = "restarting", problem
        try:
            await self.stop()
        finally:
            if problem:  # then the backoff, from when it has ended (or failed to end)
                self._failed(problem, times)

    async def _healthy(self) -> bool | None:
        """Whether the server answers its health check; None when the check could not be made
        because the gate's database is closed (a restore swapping it, or the app closing), which is
        neither a pass nor a failure. Any other refusal fails the check."""
        url, db = self.url, self.local.state.get("db")
        try:
            async with await self.local.client(timeout=HEALTH_TIMEOUT) as http:
                return url is not None and (await http.get(f"{url}/health")).status_code == 200
        except httpx.HTTPError:
            return False
        except OutboundDenied as denied:
            if denied.reason == "revoked" and (db is None or db.closed):  # the database it was decided on
                return None
            log.warning("the local model helper's health check was refused (%s)", denied.reason)
            return False
        except (DatabaseClosedError, HelperUnavailable):  # held writes, or the app closing
            return None
        except Exception as error:  # the gate could not record it (its audit write failed): not checked
            log.warning("the local model helper's health check could not be recorded (%s)", type(error).__name__)
            return None

    async def stop(self, kill=False):
        """End the running server, if any, as a stop rather than a failure (idle, or a cancelled
        rerank with kill): the next request starts it again, once this one is reaped. The stop is
        one operation (_ending), registered before anything is awaited: the watch's cancellation,
        then the process's end. A start and close() wait for it, a cancelled caller leaves it going,
        and a stop while one is under way waits for that one."""
        process, self._process, self._port, self._key = self._process, None, None, None
        watching, self._watching = self._watching, None
        if self.state == "running":
            self.state = "stopped"
        if watching is asyncio.current_task():  # the watch, stopping the process it watched
            watching = None
        if process is not None or watching is not None:
            ending = self._ending = asyncio.ensure_future(_stopped(watching, process, kill))

            def ended(_):
                if self._ending is ending:
                    self._ending = None
            ending.add_done_callback(ended)
        if self._ending is not None:
            await asyncio.shield(self._ending)

    def start_again(self):
        """After the notice (or a stop): start now, with a fresh count of failures."""
        if self.state == "failed":
            self.state, self.problem, self.failures = "stopped", None, 0
        if self.state == "stopped" and self._starting is None and not self.local.closed:
            self._starting = asyncio.create_task(self._start())

    def model_installed(self):
        """The model file was just installed: what the last check found no longer holds. The app's
        `model_installed` callback, if any, hears of it (S1-17 embeds what waited for the model)."""
        self.installs += 1
        if self.state == "stopped" and self.problem in _CHECKS:
            self.problem = None
        if (installed_hook := self.local.state.get("model_installed")) is not None:
            installed_hook()

    async def close(self):
        """Stop everything: a start under way, a pending restart, then the watch and the server, or
        a stop already under way (stop waits for it)."""
        tasks = [task for task in (self._starting, self._restarting) if task is not None]
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.wait(tasks)
        await self.stop()
        self.state = "stopped"


async def _stopped(watching, process, kill):
    """A helper's stop: its watch cancelled, then its process ended and reaped, which happens even
    when the stop is cancelled meanwhile (the app's loop shutting down)."""
    try:
        if watching is not None:
            watching.cancel()
            await asyncio.wait({watching})
    finally:
        if process is not None:
            await _end(process, kill=kill)


@dataclass
class Download:
    model: str
    source: str
    total: int
    received: int = 0
    state: str = "running"  # running, done, failed or cancelled
    problem: str | None = None
    task: asyncio.Task | None = field(default=None, repr=False)

    def public(self) -> dict:
        return {"model": self.model, "source": self.source, "total": self.total, "received": self.received,
                "state": self.state, "problem": self.problem}


class Local:
    """The app's helpers, one per model, and its model downloads and imports."""

    def __init__(self, state, config: Config):
        self.state, self.config, self.data_dir = state, config, Path(state["data_dir"])
        self.closed = False
        self.helpers = {model_id: Helper(self, pin, model_path(self.data_dir, pin), config.binary)
                        for model_id, pin in config.models.items()}
        self.download: Download | None = None
        self.importing = False
        self.unreachable: set[str] = set()  # sources a download could not reach since launch
        self.binary_problem = None  # as checked at launch; every start checks again

    def prepare(self):
        """At launch, in a worker thread: remove what a crash left of a download or an import, and
        check the binary for the status."""
        for part in (self.data_dir / "models").glob(f"*/*{PART}"):
            _remove(part)
        self.binary_problem = binary_problem(self.config.binary)

    def unavailable(self) -> str:
        """Why the gate could not record a request: the app closing, or its database not open now
        (a restore holds its writes, or swapped it)."""
        return "closing" if self.closed else "database_unavailable"

    async def verify_installed(self):
        """Once after launch, in the background: check each model file in place against its pin, so
        the status says it needs replacing when it changed since it was installed, before any launch
        finds it (every launch checks again)."""
        for helper in list(self.helpers.values()):
            installs = helper.installs
            if not installed(helper.path, helper.pin):
                continue
            try:
                changed = await asyncio.to_thread(mismatch, helper.path, helper.pin)
            except OSError:
                continue
            # Only what was checked: not a file installed meanwhile, nor a helper that started since.
            if changed and helper.installs == installs and helper.state == "stopped" and helper.problem is None:
                helper.problem = "model_changed"

    def add(self, pin, path) -> Helper:
        """A helper for a model that is not offered (the reranker's test): its own process."""
        helper = self.helpers[pin["id"]] = Helper(self, pin, path, self.config.binary)
        return helper

    def timings(self) -> dict:
        values = load_settings(self.data_dir).values["helper"]
        return {"idle": values["idle_stop_minutes"] * 60, "start": values["start_seconds"],
                "health": values["health_seconds"], "backoff": list(values["restart_backoff_seconds"])}

    async def client(self, project_id=None, **options):
        """A gated client for project_id's requests, or the General project's, which is always Normal."""
        gate, db = self.state.get("gate"), self.state.get("db")
        if gate is None or db is None or self.closed:
            raise HelperUnavailable("closing")
        if project_id is None:
            project_id = await asyncio.to_thread(db.read, lambda conn: conn.execute(
                "SELECT id FROM projects WHERE kind = 'general'").fetchone()[0])
        return gate.async_client(project_id, **options)

    def status(self) -> dict:
        embedding = self.helpers.get(EMBEDDING)
        model_source = load_settings(self.data_dir).values["helper"].get("model_source")
        return {
            "models": [self._model(helper) for model_id, helper in self.helpers.items() if model_id in self.config.models],
            "download": self.download.public() if self.download else None,
            "helper": embedding.public() if embedding else None,
            "search": self._search(embedding),
            "model_source": model_source,
            # Hugging Face could not be reached and ModelScope could (or was not tried): ModelScope is recommended.
            "recommended_source": "modelscope" if "huggingface" in self.unreachable
            and "modelscope" not in self.unreachable else None,
        }

    def _model(self, helper) -> dict:
        pin = helper.pin
        return {"id": pin["id"], "name": pin["name"], "file": pin["file"], "size": pin["size"],
                "sha256": pin["sha256"], "license": pin["license"], "folder": str(helper.path.parent),
                "sources": {name: source["repository"] for name, source in pin["sources"].items()},
                "installed": installed(helper.path, pin)}

    def _search(self, helper) -> dict:
        """Whether search can use the search model now, or is keyword-only, and why."""
        if helper is None or not installed(helper.path, helper.pin):
            reason = "model_missing"
        elif helper.state in ("restarting", "failed") or helper.problem in _CHECKS:
            reason = "helper_failed" if helper.state == "failed" else helper.problem
        elif self.binary_problem is not None:
            reason = self.binary_problem
        else:
            return {"mode": "hybrid", "reason": None}
        return {"mode": "keyword_only", "reason": reason}

    def _busy(self) -> bool:
        return self.importing or (self.download is not None and self.download.state == "running")

    def _pin(self, model_id):
        pin = self.config.models.get(model_id)
        if pin is None:
            raise Refused(404, "unknown_model", "No such model")
        return pin

    async def start_download(self, model_id, source, project_id=None) -> None:
        pin = self._pin(model_id)
        if source not in pin["sources"]:
            raise Refused(400, "unknown_source", "That source does not serve this model")
        if project_id is not None:  # asked from a project: a Local only one offers no download (ticket 71)
            row = await asyncio.to_thread(self.state["db"].read, lambda conn: conn.execute(
                "SELECT sensitivity FROM projects WHERE id = ?", (project_id,)).fetchone())
            if row is None:
                raise Refused(404, "not_found", "No such project")
            if row[0] == "local_only":
                raise Refused(409, "local_only_no_download", "A Local only project offers no download")
        if self._busy():
            raise Refused(409, "download_running", "A model download or import is under way")
        helper = self.helpers[model_id]
        if installed(helper.path, pin) and helper.problem != "model_changed":
            raise Refused(409, "already_installed", "The model is already installed")
        # Claimed in the same step as the check: no other download or import starts from here on.
        previous, download = self.download, Download(model_id, source, pin["size"])
        self.download = download
        try:
            if await asyncio.to_thread(_free_bytes, helper.path.parent) < pin["size"]:
                raise Refused(507, "disk_full", "The disk is full")
            await asyncio.to_thread(self._remember, source)
            if self.closed:
                raise Refused(503, "closing", "The app is closing")
        except BaseException:
            self.download = previous
            raise
        download.task = asyncio.create_task(self._download(download, pin, helper))

    def _remember(self, source):
        """Keep the mirror chosen in [helper] model_source. A settings file that cannot take it (not
        valid TOML) keeps its own value, and the download goes ahead."""
        for _ in range(2):
            settings = load_settings(self.data_dir)
            if settings.values["helper"].get("model_source") == source:
                return
            try:
                settings.save({"helper.model_source": source})
                return
            except SettingsChanged:
                continue
            except (ValueError, OSError) as error:  # the settings pages show what is wrong with the file
                log.warning("the model source chosen could not be saved (%s)", type(error).__name__)
                return

    async def _download(self, download, pin, helper):
        part = helper.path.with_name(helper.path.name + PART)
        url = pin["sources"][download.source]["url"]
        try:
            await asyncio.to_thread(_make_private_dirs, helper.path.parent)
            digest = hashlib.sha256()
            async with await self.client(timeout=DOWNLOAD_TIMEOUT, follow_redirects=True,
                                         max_redirects=MAX_REDIRECTS) as http:
                async with http.stream("GET", url) as response:
                    if response.status_code != 200:
                        raise Refused(502, "source_refused", "The source did not serve the file")
                    # ponytail: written and hashed on the event loop, a network read at a time; a
                    # worker thread if a slow disk ever holds the loop up.
                    with _private_file(part) as out:
                        async for chunk in response.aiter_bytes():
                            download.received += len(chunk)
                            if download.received > pin["size"]:
                                raise Refused(502, "size_mismatch", "The file is larger than its pin")
                            digest.update(chunk)
                            out.write(chunk)
                        _sync(out)
            if download.received != pin["size"]:
                raise Refused(502, "size_mismatch", "The file's size differs from its pin")
            if digest.hexdigest() != pin["sha256"]:
                raise Refused(502, "hash_mismatch", "The file's SHA-256 differs from its pin")
            # On the loop: no cancellation between the check and the install.
            _write_notices(pin, helper.path.parent, self.config.notices)
            _install(part, helper.path)
        except asyncio.CancelledError:  # Cancel, or the app closing: ended here, so the task itself finishes
            download.state = "cancelled"
        except Refused as refusal:
            download.state, download.problem = "failed", refusal.code
        except OutboundDenied as denied:
            download.state = "failed"
            download.problem = {"cross_origin_redirect": "redirect_refused",
                                # a download goes through the General project, which is never revoked:
                                # the gate's database was swapped (a restore)
                                "revoked": self.unavailable()}.get(denied.reason, "download_refused")
        except (httpx.ConnectError, httpx.ConnectTimeout) as error:
            download.state, download.problem = "failed", "source_unreachable"
            if _host(error) in _served_by(url):  # the source, or the file host it sends its files from
                self.unreachable.add(download.source)
        except httpx.HTTPError:
            download.state, download.problem = "failed", "download_interrupted"
        except (HelperUnavailable, DatabaseClosedError):  # the app closing, or a restore under way
            download.state, download.problem = "failed", self.unavailable()
        except OSError as error:
            download.state, download.problem = "failed", "disk_full" if error.errno in _FULL else "write_failed"
        except Exception as error:  # never left running: a download that failed in any other way says so
            download.state, download.problem = "failed", "download_failed"
            log.error("a model download failed unexpectedly (%s)", type(error).__name__)
        else:
            download.state = "done"
            self.unreachable.discard(download.source)
            helper.model_installed()
        finally:
            _remove(part)
            if download.state == "failed":
                log.warning("a model download failed (%s)", download.problem)

    async def cancel_download(self) -> None:
        download = self.download
        if download is None or download.task is None or download.task.done():
            raise Refused(409, "no_download", "No download is under way")
        await _cancel(download)

    async def import_file(self, model_id, text) -> None:
        pin = self._pin(model_id)
        source = Path(text).expanduser()
        if not source.is_absolute():
            raise Refused(400, "invalid_path", "Name the file by its full path")
        if self._busy():
            raise Refused(409, "download_running", "A model download or import is under way")
        helper = self.helpers[model_id]
        self.importing = True
        try:
            problem, cancelled = await _through(asyncio.to_thread(_import, source, pin, helper.path,
                                                                  self.config.notices))
        finally:
            self.importing = False
        if problem is None:
            helper.model_installed()
        if cancelled:
            raise asyncio.CancelledError()
        if problem is not None:
            log.warning("a model import was refused (%s)", problem)
            raise Refused(507 if problem == "disk_full" else 400, problem, "The file was not imported")

    async def close(self):
        self.closed = True
        if self.download is not None and self.download.task is not None and not self.download.task.done():
            await _cancel(self.download)
        await asyncio.gather(*(helper.close() for helper in self.helpers.values()))


async def _cancel(download):
    """Cancel a download and wait for its end; one cancelled before its first step, which never
    ran its own handling, is marked cancelled here (it wrote nothing)."""
    download.task.cancel()
    await asyncio.wait({download.task})
    if download.state == "running":
        download.state = "cancelled"


def _host(error: httpx.HTTPError) -> str | None:
    """The host of the request an httpx error was raised for, if it names one."""
    try:
        return error.request.url.host
    except RuntimeError:  # no request was attached to it
        return None


def _served_by(url) -> set[str]:
    """The hosts that serve a download from url: its source, and the file hosts that source was
    seen redirecting its files to (outbound_gate.MODEL_FILE_HOSTS)."""
    host = httpx.URL(url).host
    return {host} | {file_host for _, file_host, _ in MODEL_FILE_HOSTS.get(("https", host, 443), ())}


def _free_bytes(folder: Path) -> int:
    while not folder.exists():
        folder = folder.parent
    return shutil.disk_usage(folder).free


def _private_file(path: Path):
    """path opened for writing anew, owner-only (0600)."""
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o600)
    os.fchmod(fd, 0o600)
    return os.fdopen(fd, "wb")


def _sync(out):
    """Write a file's contents through to the disk before it is installed."""
    out.flush()
    os.fsync(out.fileno())


def _code(error: OSError) -> str:
    """An OSError named by its kind, for the log: never its message, which names the file."""
    return errno.errorcode.get(error.errno, type(error).__name__)


def _remove(path: Path):
    """Remove a partial file if it is there. A failure is logged by kind, never raised: the next
    launch removes it (Local.prepare), and nothing ever installs a .part file."""
    try:
        path.unlink(missing_ok=True)
    except OSError as error:
        log.warning("a partial model file could not be removed (%s)", _code(error))


def _install(part: Path, path: Path):
    """Put a verified file, its contents already synced, in place under its own name. The rename
    installs it: a failure to sync the folder afterwards is logged, not reported as a failed install."""
    os.replace(part, path)
    try:
        folder = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(folder)  # makes the rename itself durable
        finally:
            os.close(folder)
    except OSError as error:
        log.warning("the model folder could not be synced after an install (%s)", _code(error))


def _write_notices(pin, folder: Path, notices: Path):
    """Put the model's license files beside it (pin["notice"]), from the copies the app ships, each
    checked against its pin first. Refused (notice_missing) when this copy of Scholia lacks one."""
    notice = pin.get("notice")
    for name, digest in (notice["files"].items() if notice else ()):
        try:
            data = (Path(notices) / notice["folder"] / name).read_bytes()
        except OSError:
            data = None
        if data is None or hashlib.sha256(data).hexdigest() != digest:
            raise Refused(500, "notice_missing", "The model's license file is missing from this copy of Scholia")
        write_private(folder / name, data)


def _import(source: Path, pin, path: Path, notices: Path) -> str | None:
    """Copy source into place if it is the pinned file, hashing what is copied: None, or why not."""
    try:
        fd = os.open(source, os.O_RDONLY | os.O_NONBLOCK)  # a FIFO would otherwise wait for a writer
    except FileNotFoundError:
        return "file_not_found"
    except IsADirectoryError:
        return "not_a_file"
    except PermissionError:
        return "file_unreadable"
    except OSError:
        return "import_failed"
    part = path.with_name(path.name + PART)
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode):
            return "not_a_file"
        if info.st_size != pin["size"]:
            return "size_mismatch"
        _make_private_dirs(path.parent)
        digest = hashlib.sha256()
        with _private_file(part) as out:
            while True:
                try:
                    block = os.read(fd, 1 << 20)
                except OSError:  # the file being read, not the copy
                    return "import_failed"
                if not block:
                    break
                digest.update(block)
                out.write(block)
            _sync(out)
        if digest.hexdigest() != pin["sha256"]:
            return "hash_mismatch"
        _write_notices(pin, path.parent, notices)
        _install(part, path)
        return None
    except Refused as refusal:
        return refusal.code
    except OSError as error:  # writing the copy or its license files
        return "disk_full" if error.errno in _FULL else "write_failed"
    finally:
        os.close(fd)
        _remove(part)


def urls(state) -> tuple[str, ...]:
    """The running helpers' base URLs, which the outbound gate takes as the local helper's."""
    local = state.get("local_helper")
    return tuple(url for helper in local.helpers.values() if (url := helper.url)) if local else ()


async def embed(state, texts, *, project_id=None, query=False, admit=None) -> list[list[float]]:
    """Embeddings from the app's embedding helper (see Helper.embed)."""
    local = state.get("local_helper")
    if local is None or EMBEDDING not in local.helpers:
        raise HelperUnavailable("closing")
    return await local.helpers[EMBEDDING].embed(texts, project_id=project_id, query=query, admit=admit)


# The API


class _Route(APIRoute):
    """Answers Refused as the app answers its own errors: {"code", "message"}."""

    def get_route_handler(self):
        handler = super().get_route_handler()

        async def handle(request):
            try:
                return await handler(request)
            except Refused as refused:
                return JSONResponse({"code": refused.code, "message": refused.message}, status_code=refused.status)
        return handle


@contextlib.asynccontextmanager
async def lifespan(app):
    """Inside the app's own lifespan: its helpers exist while it runs, and stop before it closes."""
    state = app.state.scholia
    local = Local(state, state.get("helper_config") or Config())
    await asyncio.to_thread(local.prepare)
    state["local_helper"] = local
    verifying = asyncio.create_task(local.verify_installed())
    try:
        yield
    finally:
        verifying.cancel()
        await asyncio.wait({verifying})
        await local.close()
        state.pop("local_helper", None)


router = APIRouter(lifespan=lifespan, route_class=_Route)


class DownloadRequest(BaseModel):
    model: str = Field(min_length=1, max_length=100)
    source: Literal[SOURCES]
    project_id: str | None = Field(default=None, max_length=100)  # the project it was asked from, if any


class ImportRequest(BaseModel):
    model: str = Field(min_length=1, max_length=100)
    path: str = Field(min_length=1, max_length=4096)  # the file's full path on this Mac


def _local(request: Request) -> Local:
    local = request.app.state.scholia.get("local_helper")
    if local is None:
        raise Refused(503, "closing", "The app is closing")
    return local


async def _status(local):
    return await asyncio.to_thread(local.status)


@router.get("/api/helper")
async def helper_status(request: Request):
    return await _status(_local(request))


@router.post("/api/helper/models/download", status_code=202)
async def download_model(body: DownloadRequest, request: Request):
    """Download a model: an explicit, app-wide action the researcher consented to (S13)."""
    local = _local(request)
    await local.start_download(body.model, body.source, body.project_id)
    return await _status(local)


@router.delete("/api/helper/models/download")
async def cancel_download(request: Request):
    local = _local(request)
    await local.cancel_download()
    return await _status(local)


@router.post("/api/helper/models/import")
async def import_model(body: ImportRequest, request: Request):
    local = _local(request)
    await local.import_file(body.model, body.path)
    return await _status(local)


@router.post("/api/helper/restart")
async def start_again(request: Request):
    """Start again after the notice."""
    local = _local(request)
    local.helpers[EMBEDDING].start_again()
    return await _status(local)
