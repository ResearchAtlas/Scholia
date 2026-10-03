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
    default answer. Set `hold` to an asyncio.Event to keep calls waiting until it is set."""

    def __init__(self, *replies, cost=0.002):
        self.replies = list(replies)
        self.title_replies = []  # replies for title calls, which never take from `replies`
        self.requests = []
        self.cost = cost
        self.hold = None
        self.started = asyncio.Event()

    def answer(self, text, cost=None, **usage):
        return 200, {"choices": [{"message": {"role": "assistant", "content": text}}],
                     "usage": {"prompt_tokens": 12, "completion_tokens": 5, "total_tokens": 17,
                               "cost": self.cost if cost is None else cost, **usage}}

    async def __call__(self, request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content) if request.content else None
        self.requests.append((request.method, request.url.path, body))
        self.started.set()
        if self.hold is not None:
            await self.hold.wait()
        if request.url.path.endswith("/models") or "/endpoints/" in request.url.path:
            return httpx.Response(200, json={"data": []})
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
    def chats(self):
        return [body for method, path, body in self.requests if path.endswith("/chat/completions")]

    @property
    def answers(self):
        return [body for body in self.chats if not _is_title(body)]

    @property
    def titles(self):
        return [body for body in self.chats if _is_title(body)]


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
    """Wait until no background run is active in the app."""
    harness = client.state["harness"]
    deadline = asyncio.get_running_loop().time() + timeout
    while True:
        await asyncio.sleep(0)
        busy = [a for a in harness.registry.runs.values() if a.kind == "background"]
        pending = [t for t in harness._tasks if not t.done()]
        if not busy and not pending:
            return
        if asyncio.get_running_loop().time() > deadline:
            raise AssertionError("background work did not finish")
        await asyncio.sleep(0.01)
