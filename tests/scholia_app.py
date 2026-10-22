"""Shared helpers for tests that drive the backend app in process.

`started(data_dir, provider)` runs the app's startup and shutdown around a block
and yields an httpx client bound to it. Model calls go to `MockProvider`, a
test-owned stand-in for OpenRouter behind the outbound gate's transport, so no
request ever leaves the process. Keys live in `FakeKeyring`, never the Keychain.
"""

import asyncio
import contextlib
import json
import pathlib

import httpx

from backend import budget_router
from backend.app import create_app
from backend.db import new_id as _new_id

ORIGIN = "http://127.0.0.1:8765"
PARAMETERS = ["max_tokens", "temperature", "reasoning", "include_reasoning", "tools", "tool_choice"]
PRICING = {"prompt": "0.000001", "completion": "0.000005"}  # USD per token, as OpenRouter publishes them
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
    Its model listing holds the ids in `catalog` (each with a 128K window; by default the
    models OpenRouter's Recommended offers), and its zero-retention endpoints those in
    `zero_retention`: an id (one endpoint tagged "example" with a 128K window) or a listing
    row of its own; none by default. Any other provider's listing holds the ids in `local`. A
    model's endpoint listing (/models/{id}/endpoints) is
    `endpoints[id]` when set, else its zero-retention rows, else one endpoint tagged "example"
    with a 128K window; a row that names no pricing or parameters publishes PRICING and takes PARAMETERS."""

    def __init__(self, *replies, cost=0.002, catalog=None, zero_retention=(), scholarly=None, endpoints=None, zotero=None):
        self.scholarly = scholarly if scholarly is not None else MockScholarly()  # OpenAlex, Crossref and arXiv
        self.zotero = zotero if zotero is not None else MockZotero(state="not_running")  # S1-15
        self.catalog = sorted(budget_router.RECOMMENDED) if catalog is None else list(catalog)
        self.zero_retention = list(zero_retention)
        self.endpoints = dict(endpoints or {})
        self.parameters = PARAMETERS  # what an endpoint listing row takes by default (without "tools": no agent loop)
        self.local = ["llama", "m", "m:1"]  # what another provider's model listing holds, each with a 128K window
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
        if (request.url.host, request.url.port) == MockZotero.ORIGIN:  # S1-15
            return streamed(await self.zotero(request))
        body = json.loads(request.content) if request.content else None
        self.requests.append((request.method, request.url.path, body))
        self.headers.append(request.headers)
        self.started.set()
        if self.hold is not None:
            await self.hold.wait()
        if request.url.path.endswith("/models"):
            models = self.catalog if request.url.host == "openrouter.ai" else self.local
            return httpx.Response(200, json={"data": [{"id": m, "context_length": 128000, "pricing": PRICING}
                                                      for m in models]})
        if request.url.path.endswith("/endpoints") and "/models/" in request.url.path:
            model = request.url.path.split("/models/", 1)[1].removesuffix("/endpoints")
            return httpx.Response(200, json={"data": {"id": model, "endpoints": self.listing(model)}})
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

    def listing(self, model):
        rows = self.endpoints.get(model) or [
            row for row in self.zero_retention if isinstance(row, dict) and row.get("model_id") == model]
        rows = rows or [{"tag": "example", "context_length": 128000, "supported_parameters": self.parameters}]
        # A row that names its pricing or parameters keeps them.
        return [{"pricing": PRICING, "supported_parameters": self.parameters, **row} for row in rows]

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


def streaming(handler):
    """A transport handler whose answers arrive streamed (see streamed), as the catalog's reader
    needs them: it decodes and bounds a body itself (backend/openrouter_client.py, _bounded)."""
    async def answer(request):
        response = handler(request)
        if asyncio.iscoroutine(response):
            response = await response
        return streamed(response)
    return answer


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


class MockZotero:
    """A test-owned stand-in for Zotero's local API (S1-15), behind the gate's mock transport, holding
    made-up items only, never a real library. state: ready, api_off (403), old (404: Zotero before 7),
    not_running (no connection). Each request is kept in `requests` as (method, raw path and query,
    headers). `answers` holds answers given first, before any route: a status, a callable of the
    request (async or not) giving a response, or None (that request answered as usual). `hold`, when
    set to an asyncio.Event, keeps requests waiting until it is set, after `started` is set. Files are named by `files` (the attachment's file
    URL text, or a status), as Zotero's file/view/url answers."""

    ORIGIN = ("127.0.0.1", 23119)

    def __init__(self, state="ready", server_id=None, groups=()):
        self.state, self.server_id, self.groups = state, server_id, list(groups)
        self.collections, self.items, self.files = {}, {}, {}  # by library; by (library, key); by (library, key)
        self.requests, self.answers, self.hold, self.started, self.held = [], [], None, asyncio.Event(), None

    def item(self, key, title, *, library="users/0", doi=None, collections=(), pdf=None, linked=False,
             item_type="journalArticle", year=2024, extra=None):
        """Add an item; pdf, a path, becomes its PDF attachment (key: the item's with its last letter Z)."""
        csl = {"id": f"http://zotero.org/users/local/test/items/{key}", "type": "article-journal", "title": title,
               "author": [{"family": "Example", "given": "Ana"}], "issued": {"date-parts": [[year]]},
               **({"DOI": doi} if doi is not None else {}), **(extra or {})}
        children = []
        if pdf is not None:
            attachment = key[:7] + "Z"
            children.append({"key": attachment, "data": {"key": attachment, "itemType": "attachment",
                                                         "contentType": "application/pdf", "parentItem": key,
                                                         "linkMode": "linked_file" if linked else "imported_file",
                                                         "dateAdded": "2024-01-01T00:00:00Z"}})
            self.files[library, attachment] = pdf if isinstance(pdf, (int, str)) else pathlib.Path(pdf).as_uri()
        self.items[library, key] = {"key": key, "data": {"key": key, "itemType": item_type, "title": title,
                                                         "collections": list(collections)},
                                    "meta": {"creatorSummary": "Example", "parsedDate": f"{year}-01-01"},
                                    "csljson": csl, "children": children}
        return self.items[library, key]

    def _answer(self, status, body=None, total=None):
        headers = {"Zotero-API-Version": "3", **({"Zotero-Server-ID": self.server_id} if self.server_id else {}),
                   **({"Total-Results": str(total)} if total is not None else {})}
        if isinstance(body, str):
            return httpx.Response(status, text=body, headers=headers)
        return httpx.Response(status, json=body, headers=headers) if body is not None else \
            httpx.Response(status, text="Not found" if status == 404 else "", headers=headers)

    async def __call__(self, request):
        self.requests.append((request.method, request.url.raw_path.decode(), dict(request.headers)))
        if self.hold is not None and (self.held is None or self.held in request.url.raw_path.decode()):
            self.started.set()
            await self.hold.wait()
        if self.state == "not_running":
            raise httpx.ConnectError("connection refused", request=request)
        answer = self.answers.pop(0) if self.answers else None  # None: answered as Zotero would
        if isinstance(answer, int):
            return self._answer(answer)
        if answer is not None:
            answer = answer(request)
            return await answer if asyncio.iscoroutine(answer) else answer
        if self.state == "api_off":
            return httpx.Response(403, text="Local API is not enabled")
        if self.state == "old":
            return httpx.Response(404, text="No endpoint found")
        sent = request.headers.get("zotero-server-id")
        if sent is not None and sent != self.server_id:
            return self._answer(412, "Zotero-Server-ID does not match this server")
        path, params = request.url.path, request.url.params
        if path == "/api/users/0/groups":
            return self._answer(200, [{"id": number, "data": {"id": number, "name": name}} for number, name in self.groups])
        parts = path.removeprefix("/api/").split("/")
        library, rest = "/".join(parts[:2]), parts[2:]
        start, limit = int(params.get("start", 0)), int(params.get("limit", 1000))
        if rest == ["collections"]:
            found = self.collections.get(library, [])
            return self._answer(200, [{"key": c["key"], "data": {"key": c["key"], "name": c["name"],
                                                                 "parentCollection": c.get("parent") or False}}
                                      for c in found[start:start + limit]], len(found))
        if rest[-2:] == ["items", "top"]:
            collection = rest[1] if rest[0] == "collections" else None
            found = [i for (lib, _), i in self.items.items() if lib == library
                     and (collection is None or collection in i["data"]["collections"])]
            return self._answer(200, [{k: i[k] for k in ("key", "data", "meta")} for i in found[start:start + limit]],
                                len(found))
        if len(rest) >= 2 and rest[0] == "items":
            item = self.items.get((library, rest[1]))
            if rest[2:] == ["file", "view", "url"]:
                named = self.files.get((library, rest[1]))
                return self._answer(named) if isinstance(named, int) else \
                    self._answer(200, named) if named is not None else self._answer(404)
            if item is None:
                return self._answer(404)
            if rest[2:] == ["children"]:
                return self._answer(200, item["children"])
            if rest[2:] == []:
                return self._answer(200, {"key": item["key"], "data": item["data"], "csljson": item["csljson"]})
        return self._answer(404)

    @property
    def paths(self):
        return [path for _, path, _ in self.requests]


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
                      transport=httpx.MockTransport(streaming(provider)), **options)


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
        busy = [a for a in harness.registry.runs.values() if a.kind == "background"]
        if busy:
            try:  # a test may have made the database refuse reads: then none is taken for waiting
                asking = await asyncio.to_thread(harness.db.read, lambda conn: {r for (r,) in conn.execute(
                    "SELECT id FROM runs WHERE status = 'running' AND waiting = 'ask'")})
            except Exception:
                asking = set()
            busy = [a for a in busy if a.run_id not in asking]
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


def read_in_process(monkeypatch):
    """Read materials in this process, as the app did before readings moved to a child process
    (backend/reading.py): for tests that replace the parser, its version or its OCR engine here to
    see what the app does with a reading, which a child would not see. The child's own handling is
    tested in tests/test_reading.py, with the real child and a test-owned one."""
    import hashlib
    from pathlib import Path

    from backend import extraction, reading

    def stored(path, sha256):
        data = Path(path).read_bytes()
        if hashlib.sha256(data).hexdigest() != sha256:
            raise FileNotFoundError("the stored file changed")
        return data

    monkeypatch.setattr(reading, "read", lambda path, sha256, kind, stop, progress, **options: extraction.extract(
        stored(path, sha256), kind, stop, progress))
    monkeypatch.setattr(reading, "render", lambda path, sha256, number, scale, stop, **options: extraction.render_page(
        stored(path, sha256), number, scale))
