"""Shared helpers for tests that drive the backend app in process.

`started(data_dir, provider)` runs the app's startup and shutdown around a block
and yields an httpx client bound to it. Model calls go to `MockProvider`, a
test-owned stand-in for OpenRouter behind the outbound gate's transport, so no
request ever leaves the process. Keys live in `FakeKeyring`, never the Keychain.
"""

import asyncio
import contextlib
import json

import httpx

from backend.app import create_app
from backend.db import new_id as _new_id

ORIGIN = "http://127.0.0.1:8765"
HEADERS = {"X-Scholia-Client": "local"}
KEY = "sk-or-test-not-a-real-key"


class FakeKeyring:
    """An in-memory credential store with keyring's backend interface."""

    def __init__(self):
        self.keys = {}

    def get_password(self, service, name):
        return self.keys.get((service, name))

    def set_password(self, service, name, value):
        self.keys[(service, name)] = value

    def delete_password(self, service, name):
        self.keys.pop((service, name), None)


class MockProvider:
    """Answers chat completions like OpenRouter. Each call takes the next reply from
    `replies` (a function of the request body, or a (status, body) pair), or a
    default answer. Set `hold` to an asyncio.Event to keep calls waiting until it is set.
    Its model listing holds the ids in `catalog` (each with a 128K window), and its
    zero-retention endpoints those in `zero_retention`: an id (one endpoint tagged "example"
    with a 128K window) or a listing row of its own; both are empty by default."""

    def __init__(self, *replies, cost=0.002, catalog=(), zero_retention=(), scholarly=None):
        self.scholarly = scholarly if scholarly is not None else MockScholarly()  # OpenAlex, Crossref and arXiv
        self.catalog = list(catalog)
        self.zero_retention = list(zero_retention)
        self.replies = list(replies)
        self.title_replies = []  # replies for title calls, which never take from `replies`
        self.requests = []
        self.headers = []  # each request's headers, in the order of `requests`
        self.cost = cost
        self.hold = None
        self.started = asyncio.Event()

    def answer(self, text, cost=None, **usage):
        return 200, {"choices": [{"message": {"role": "assistant", "content": text}}],
                     "usage": {"prompt_tokens": 12, "completion_tokens": 5, "total_tokens": 17,
                               "cost": self.cost if cost is None else cost, **usage}}

    async def __call__(self, request: httpx.Request) -> httpx.Response:
        if request.url.host in MockScholarly.HOSTS:
            return streamed(await self.scholarly(request))
        body = json.loads(request.content) if request.content else None
        self.requests.append((request.method, request.url.path, body))
        self.headers.append(request.headers)
        self.started.set()
        if self.hold is not None:
            await self.hold.wait()
        if request.url.path.endswith("/models"):
            return httpx.Response(200, json={"data": [{"id": m, "context_length": 128000} for m in self.catalog]})
        if "/endpoints/" in request.url.path:
            return httpx.Response(200, json={"data": [
                m if isinstance(m, dict) else {"model_id": m, "tag": "example", "context_length": 128000}
                for m in self.zero_retention]})
        title = _is_title(body)
        queue = self.title_replies if title else self.replies
        reply = queue.pop(0) if queue else None
        if callable(reply):
            reply = reply(body)
            if asyncio.iscoroutine(reply):
                reply = await reply
        status, payload = reply if reply is not None else self.answer(_default_answer(body))
        if isinstance(payload, bytes):  # a body JSON encoders refuse to write, such as Infinity
            return httpx.Response(status, content=payload, headers={"content-type": "application/json"})
        return httpx.Response(status, json=payload)

    @property
    def chat_headers(self):
        return [headers for (method, path, body), headers in zip(self.requests, self.headers)
                if path.endswith("/chat/completions")]

    @property
    def chats(self):
        return [body for method, path, body in self.requests if path.endswith("/chat/completions")]

    @property
    def answers(self):
        return [body for body in self.chats if not _is_title(body)]

    @property
    def titles(self):
        return [body for body in self.chats if _is_title(body)]


class Chunks(httpx.AsyncByteStream):
    """A body that arrives in these chunks, each only as the client reads it; `read` counts them."""

    def __init__(self, chunks):
        self.chunks, self.read = list(chunks), 0

    async def __aiter__(self):
        for chunk in self.chunks:
            self.read += 1
            yield chunk


def streamed(response):
    """A test's answer as a server's arrives: streamed, not already read (as httpx reads one made from bytes)."""
    if not response.is_stream_consumed:
        return response
    return httpx.Response(response.status_code, headers=response.headers, stream=Chunks([response.content]))


class MockScholarly:
    """Test-owned stand-ins for OpenAlex, Crossref and arXiv's identifier endpoints, behind the gate's
    mock transport: records by DOI or arXiv ID (made-up ones, never real works), each request kept in
    `requests` as (host, path and query, headers). `answers[host]` is a list of statuses (or (status,
    headers) pairs) answered first, before any record; `hold`, when set to an asyncio.Event, keeps
    requests waiting until it is set, after `started` is set: those to the hosts in `held` only,
    when it is set."""

    HOSTS = {"api.openalex.org", "api.crossref.org", "export.arxiv.org"}

    def __init__(self, openalex=None, crossref=None, arxiv=None):
        self.openalex, self.crossref, self.arxiv = dict(openalex or {}), dict(crossref or {}), dict(arxiv or {})
        self.requests, self.answers, self.hold, self.started, self.held = [], {}, None, asyncio.Event(), None

    async def __call__(self, request):
        self.requests.append((request.url.host, request.url.raw_path.decode(), dict(request.headers)))
        self.started.set()
        if self.hold is not None and (self.held is None or request.url.host in self.held):
            await self.hold.wait()
        queued = self.answers.get(request.url.host) or []
        if queued:
            status, headers = (queued.pop(0), {}) if isinstance(queued[0], int) else queued.pop(0)
            return httpx.Response(status, headers=headers, json={})
        path = request.url.path
        if request.url.host == "api.openalex.org" and path.startswith("/works/doi:"):
            record = self.openalex.get(path.removeprefix("/works/doi:"))
            return httpx.Response(200, json=record) if record else httpx.Response(404, json={"error": "Not found"})
        if request.url.host == "api.crossref.org" and path.startswith("/works/"):
            record = self.crossref.get(path.removeprefix("/works/"))
            return httpx.Response(200, json={"status": "ok", "message": record}) if record else \
                httpx.Response(404, text="Resource not found.")
        if request.url.host == "export.arxiv.org" and path == "/api/query":
            identifier = request.url.params.get("id_list")
            return httpx.Response(200, content=arxiv_feed(identifier, self.arxiv.get(identifier)),
                                  headers={"content-type": "application/atom+xml"})
        return httpx.Response(404)

    @property
    def hosts(self):
        return [host for host, _, _ in self.requests]


def openalex_work(doi, title, *, authors=("A. Researcher",), year=2024, venue="Journal of Synthetic Studies",
                  retracted=False):
    """An OpenAlex work record, made up."""
    return {"id": "https://openalex.org/W0000000001", "doi": f"https://doi.org/{doi}", "title": title,
            "display_name": title, "publication_year": year, "type": "article", "is_retracted": retracted,
            "authorships": [{"author": {"display_name": name}} for name in authors],
            "primary_location": {"source": {"display_name": venue}}, "biblio": {"volume": "3", "first_page": "1"}}


def crossref_work(doi, title, *, retracted=False):
    """A Crossref work record (the message), made up."""
    return {"DOI": doi, "title": [title], "type": "journal-article", "author": [{"given": "Ana", "family": "Example"}],
            "issued": {"date-parts": [[2023, 5]]}, "container-title": ["Synthetic Review"],
            **({"updated-by": [{"type": "retraction", "DOI": "10.5555/notice", "label": "Retraction"}]} if retracted else {})}


def arxiv_feed(identifier, title):
    """An arXiv Atom answer: one entry for a known ID, else arXiv's error entry."""
    if title is None:
        entry = "<entry><id>http://arxiv.org/api/errors#incorrect_id_format</id><title>Error</title></entry>"
    else:
        entry = (f"<entry><id>http://arxiv.org/abs/{identifier}v1</id><title>{title}</title>"
                 "<published>2024-01-02T00:00:00Z</published><author><name>Bo Example</name></author></entry>")
    return (f'<?xml version="1.0"?><feed xmlns="http://www.w3.org/2005/Atom"'
            f' xmlns:arxiv="http://arxiv.org/schemas/atom">{entry}</feed>').encode()


def _is_title(body):
    return ((body or {}).get("messages") or [{}])[0].get("content", "").startswith("Write a title")


def _default_answer(body):
    return "A short title" if _is_title(body) else "An answer."


def app_for(data_dir, provider, keyring=None, **options):
    return create_app(data_dir, origin=ORIGIN, keyring_backend=keyring or FakeKeyring(),
                      transport=httpx.MockTransport(provider), **options)


@contextlib.asynccontextmanager
async def started(data_dir, provider=None, *, keyring=None, setup=True, **options):
    """Start the app on data_dir and yield (client, app). setup stores an OpenRouter key."""
    provider = provider or MockProvider()
    keyring = keyring or FakeKeyring()
    app = app_for(data_dir, provider, keyring, **options)
    fastapi_app = app.app
    async with fastapi_app.router.lifespan_context(fastapi_app):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url=ORIGIN, headers=HEADERS,
                                     timeout=30) as client:
            if setup:
                response = await client.post("/api/setup", json={"openrouter_key": KEY})
                assert response.status_code == 200, response.text
            client.app = app
            client.state = fastapi_app.state.scholia
            client.provider = provider
            client.keyring = keyring
            yield client


async def stored_material(client, project, content, media_type):
    """A material of project whose one version is content, stored: its file's SHA-256."""
    sha256 = await asyncio.to_thread(client.state["content"].put, content, media_type)
    material = _new_id()

    def insert(conn):
        conn.execute("INSERT INTO materials (id, project_id, title, source) VALUES (?, ?, 'A paper', 'upload')",
                     (material, project))
        conn.execute("INSERT INTO material_versions (id, material_id, seq, file_sha256, is_current)"
                     " VALUES (?, ?, 0, ?, 1)", (_new_id(), material, sha256))

    await asyncio.to_thread(client.state["db"].write, insert)
    return sha256


def events(response):
    """The server-sent events of a finished stream response, as dicts."""
    return [json.loads(line[len("data: "):]) for line in response.text.splitlines() if line.startswith("data: ")]


async def send(client, conversation_id, content="What is a cohort study?", **options):
    """Send a message and return the stream's events once the turn ends."""
    response = await client.post(f"/api/conversations/{conversation_id}/message/stream",
                                 json={"content": content, **options})
    assert response.status_code == 200, response.text
    return events(response)


async def background_idle(client, timeout=5.0):
    """Wait until no background run is active in the app; one waiting on its question to the
    researcher (the search model's offer, S1-17) is not at work."""
    harness = client.state["harness"]
    deadline = asyncio.get_running_loop().time() + timeout
    while True:
        await asyncio.sleep(0)
        asking = await asyncio.to_thread(harness.db.read, lambda conn: {r for (r,) in conn.execute(
            "SELECT id FROM runs WHERE status = 'running' AND waiting = 'ask'")})
        busy = [a for a in harness.registry.runs.values() if a.kind == "background" and a.run_id not in asking]
        pending = [t for t in harness._tasks if not t.done()]
        if not busy and not pending:
            return
        if asyncio.get_running_loop().time() > deadline:
            raise AssertionError("background work did not finish")
        await asyncio.sleep(0.01)


async def run_finished(client, run_id, timeout=15.0):
    """A background run's row in the background-run list once it has ended."""
    deadline = asyncio.get_running_loop().time() + timeout
    while True:
        rows = (await client.get("/api/activity", params={"run_id": run_id})).json()["runs"]
        if rows and rows[0]["status"] != "running":
            return rows[0]
        if asyncio.get_running_loop().time() > deadline:
            raise AssertionError(f"run {run_id} did not end: {rows}")
        await asyncio.sleep(0.02)


async def confirm_key(client, provider="openrouter"):
    """Confirm a provider's key's data settings as the card does: for the key and statement it showed."""
    [shown] = [p for p in (await client.get("/api/providers")).json()["providers"] if p["name"] == provider]
    confirmation = shown["key_confirmation"]
    return await client.post("/api/key-attestations", json={
        "provider": provider, "statement": confirmation["statement"], "key": confirmation["key"]})


async def declare(client, provider):
    """Declare a provider on this Mac as the card does: for the origin it showed."""
    [shown] = [p for p in (await client.get("/api/providers")).json()["providers"] if p["name"] == provider]
    return await client.post("/api/local-declarations", json={"provider": provider, "origin": shown["origin"]})
