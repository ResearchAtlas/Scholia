"""A test app for cited answers: the model provider is a test-owned MockProvider, and the local helper
S1-16's stand-in (test_search.Remote), so a project's papers are read, indexed and embedded as the app
does it and a turn's evidence comes from its own pipeline. Nothing leaves the process."""

import asyncio
import contextlib
import json

from backend import router
from backend.citations import JUDGE_RULES
from backend.local_helper import EMBEDDING, Config
from network_guard import allow_subprocess
from scholia_app import MockProvider, started
from test_local_helper import PIN, WEIGHTS, Fake
from test_materials import added, project_of
from test_search import Remote, idle

ANSWER = "Minimum wages raise the earnings of low-paid workers [S1.1]."


def is_judge(body):
    return (body or {}).get("messages", [{}])[0].get("content", "").startswith(JUDGE_RULES)


def is_router(body):
    return (body or {}).get("messages", [{}])[0].get("content", "").startswith(router.ROUTER_RULES)


class Provider(MockProvider):
    """A MockProvider whose answers and judge replies are separate: `answer_text` (or a callable of the
    request) answers turns; `verdicts` (a callable of the judge's request, or a fixed reply) answers the
    support judge; `judged` keeps the judge's requests."""

    def __init__(self, *, answer_text=ANSWER, verdicts=None, **options):
        super().__init__(**options)
        self.answer_text, self.verdicts, self.judged = answer_text, verdicts, []
        self.replies.append(self._reply)

    def _reply(self, body):
        self.replies.append(self._reply)  # every chat call comes here
        if is_judge(body):
            self.judged.append(body)
            reply = self.verdicts(body) if callable(self.verdicts) else self.verdicts
            if isinstance(reply, tuple) or asyncio.iscoroutine(reply):
                return reply  # a (status, body) pair, or a coroutine giving one
            count = body["messages"][-1]["content"].count("Pair ")
            return self.answer(reply if reply is not None else "\n".join(f"{i}: supported" for i in range(1, count + 1)))
        text = self.answer_text(body) if callable(self.answer_text) else self.answer_text
        return text if isinstance(text, tuple) or asyncio.iscoroutine(text) else self.answer(text)

    @property
    def answers(self):
        return [b for b in self.chats if not is_judge(b) and not is_router(b) and not b["messages"][0]["content"]
                .startswith("Write a title")]


@contextlib.asynccontextmanager
async def cited(tmp_path, provider=None, *, install=True):
    """(client, project) with the stand-in helper; the client's provider answers chat calls."""
    provider = provider or Provider()
    data = tmp_path / "data"
    path = data / "models" / EMBEDDING / PIN["file"]
    if install and not path.exists():
        path.parent.mkdir(parents=True)
        path.write_bytes(WEIGHTS)
    fake = Fake(tmp_path / "bundle")
    remote = Remote(fake)

    async def transport(request):
        return await (remote(request) if request.url.host == "127.0.0.1" else provider(request))

    with allow_subprocess(str(fake.binary)):
        async with started(data, transport, helper=Config(binary=fake.binary, models={EMBEDDING: PIN})) as client:
            client.remote, client.provider = remote, provider
            yield client


async def with_papers(client, *papers):
    """A project holding these papers, read, indexed and embedded: (project id, material ids)."""
    project = await project_of(client)
    materials = []
    for item in papers:
        materials += [m["id"] for m in (await added(client, project, item))["materials"]]
    await idle(client, project)
    return project, materials


async def conversation_in(client, project):
    return (await client.post("/api/conversations", json={"project_id": project, "title": "t"})).json()["id"]


async def events_of(client, run_id):
    rows = await asyncio.to_thread(client.state["db"].read, lambda conn: conn.execute(
        "SELECT type, data, json_extract(data, '$.body') FROM run_events WHERE run_id = ? ORDER BY seq",
        (run_id,)).fetchall())
    return [(kind, json.loads(data), ref) for kind, data, ref in rows]


async def rows(client, sql, *args):
    return await asyncio.to_thread(client.state["db"].read, lambda conn: conn.execute(sql, args).fetchall())
