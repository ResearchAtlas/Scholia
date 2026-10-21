"""The review lock (slice-1 spec F1, section 10; ticket 18).

The review-lock preset is Local only with the function lock. In M1 the lock fails closed:
every generative function is refused in a locked project, whatever the venue, until the
venue rules (S1-22) relax it. That covers the direct answer and its retries (Continue) and
titles; S1-19b adds the router, the support judge and the citation check, behind the same
dispatch check; memory extraction and subagents come with their PRs. Locking applies at once and revokes running
work; lifting the lock needs confirmation; both are audited, without the venue.
"""

import asyncio
import json

import pytest

from backend.db import new_id
from scholia_app import MockProvider, background_idle, declare, events, send, started

pytestmark = pytest.mark.asyncio

LOCAL = "http://127.0.0.1:11434/v1"


async def rows(client, sql, *args):
    return await asyncio.to_thread(client.state["db"].read, lambda conn: conn.execute(sql, args).fetchall())


async def wait_for(predicate, timeout=5.0):
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while not predicate():
        if loop.time() > deadline:
            raise AssertionError("condition not reached")
        await asyncio.sleep(0.01)


async def locked_project(client, venue="SECRET-JOURNAL"):
    response = await client.post("/api/projects", json={"name": "Review", "sensitivity": "local_only",
                                                        "review_lock": True, "review_venue": venue})
    assert response.status_code == 201, response.text
    return response.json()


async def lock(client, project, locked=True, **body):
    return await client.post(f"/api/projects/{project}/review-lock", json={"locked": locked, **body})


async def with_declared_local_server(client):
    current = (await client.get("/api/settings")).json()
    await client.put("/api/settings", json={"hash": current["hash"], "updates": {
        "providers.local.kind": "openai-compatible", "providers.local.base_url": LOCAL, "providers.local.models": "all"}})
    await client.put("/api/keys/local", json={"key": "local"})
    assert (await declare(client, "local")).status_code == 200


async def test_the_preset_is_local_only_with_the_lock_and_its_venue(tmp_path):
    async with started(tmp_path / "data") as client:
        project = await locked_project(client)
        assert (project["sensitivity"], project["review_lock"], project["review_venue"]) == (
            "local_only", True, "SECRET-JOURNAL")
        for body in ({"sensitivity": "normal", "review_lock": True}, {"sensitivity": "private", "review_lock": True}):
            response = await client.post("/api/projects", json={"name": "P", **body})
            assert response.status_code == 400  # the lock comes with Local only
        plain = (await client.post("/api/projects", json={"name": "P", "review_venue": "ignored"})).json()
        assert plain["review_venue"] is None
        [(data,)] = await rows(client, "SELECT data FROM audit_log WHERE event = 'project_created' AND project_id = ?",
                               project["id"])
        assert json.loads(data) == {"sensitivity": "local_only", "review_lock": True}  # never the venue


@pytest.mark.parametrize("venue", [None, "Sage", "NIH"])
async def test_a_locked_project_refuses_every_generative_function_whatever_the_venue(tmp_path, venue):
    provider = MockProvider()
    async with started(tmp_path / "data", provider) as client:
        await with_declared_local_server(client)  # even a local route the project could otherwise use
        project = (await locked_project(client, venue))["id"]
        conversation = (await client.post("/api/conversations", json={"project_id": project})).json()["id"]
        for body in ({"content": "Summarize the submission"}, {"content": "hi", "model": "llama", "provider": "local"},
                     {"content": "hi", "model": "auto"}):
            response = await client.post(f"/api/conversations/{conversation}/message/stream", json=body)
            assert (response.status_code, response.json()["code"]) == (403, "review_locked")
        assert provider.chats == []
        assert await rows(client, "SELECT count(*) FROM runs") == [(0,)]  # refused before anything is written


async def test_locking_applies_at_once_revokes_running_work_and_its_title(tmp_path):
    provider = MockProvider()
    release = asyncio.Event()

    async def held(body):
        await release.wait()
        return provider.answer("A held answer.")

    provider.replies.append(held)
    async with started(tmp_path / "data", provider) as client:
        await with_declared_local_server(client)
        project = (await client.post("/api/projects", json={"name": "Review"})).json()["id"]
        conversation = (await client.post("/api/conversations", json={"project_id": project})).json()["id"]
        stream = asyncio.create_task(client.post(f"/api/conversations/{conversation}/message/stream",
                                                 json={"content": "hi"}))
        await wait_for(lambda: provider.answers)
        locked = await lock(client, project, venue="Journal")
        assert (locked.json()["sensitivity"], locked.json()["review_lock"]) == ("local_only", True)
        finished = events(await stream)
        release.set()
        assert (finished[-1]["status"], finished[-1]["cancel_reason"]) == ("cancelled", "revoked")
        refused = await client.post(f"/api/runs/{finished[0]['run_id']}/continue",
                                    json={"model": "llama", "provider": "local"})
        assert (refused.status_code, refused.json()["code"]) == (403, "review_locked")  # Continue too
        await background_idle(client)
        assert len(provider.chats) == 1
        [(data,)] = await rows(client, "SELECT data FROM audit_log WHERE event = 'review_lock_changed'")
        assert json.loads(data) == {"locked": True, "from": "normal", "venue_set": True, "revoked_runs": 1}


async def test_lifting_the_lock_needs_confirmation_and_leaves_local_only(tmp_path):
    async with started(tmp_path / "data") as client:
        project = (await locked_project(client))["id"]
        first = await lock(client, project, locked=False)
        assert (first.status_code, first.json()["code"]) == (409, "confirmation_required")
        assert (await client.get(f"/api/projects/{project}")).json()["review_lock"] is True
        lifted = await lock(client, project, locked=False, token=first.json()["token"])
        assert (lifted.json()["review_lock"], lifted.json()["sensitivity"], lifted.json()["review_venue"]) == (
            False, "local_only", None)
        audit = await rows(client, "SELECT data FROM audit_log WHERE event = 'review_lock_changed'")
        assert [json.loads(d) for (d,) in audit] == [
            {"locked": False, "from": "local_only", "venue_set": False, "revoked_runs": 0}]
        assert "SECRET" not in json.dumps(await rows(client, "SELECT * FROM audit_log"))


async def test_a_locked_project_cannot_be_loosened_until_its_lock_is_lifted(tmp_path):
    async with started(tmp_path / "data") as client:
        project = (await locked_project(client))["id"]
        response = await client.post(f"/api/projects/{project}/sensitivity", json={"level": "normal"})
        assert (response.status_code, response.json()["code"]) == (409, "review_locked")
        assert (await lock(client, project, venue="Another")).json()["review_venue"] == "Another"  # venue only


async def test_the_general_project_cannot_be_locked(tmp_path):
    async with started(tmp_path / "data") as client:
        [general] = [p for p in (await client.get("/api/projects")).json()["projects"] if p["kind"] == "general"]
        response = await lock(client, general["id"])
        assert (response.status_code, response.json()["code"]) == (400, "general_project")
        assert (await lock(client, new_id())).status_code == 404


async def test_a_title_run_in_a_project_locked_meanwhile_sends_nothing(tmp_path):
    provider = MockProvider()
    async with started(tmp_path / "data", provider) as client:
        project = (await client.post("/api/projects", json={"name": "Review"})).json()["id"]
        conversation = (await client.post("/api/conversations", json={"project_id": project})).json()["id"]
        db = client.state["db"]
        real = client.state["harness"].kick_background

        async def lock_first():  # the lock lands, in the record, before the title run starts
            await asyncio.to_thread(db.write, lambda conn: conn.execute(
                "UPDATE projects SET review_lock = 1, sensitivity = 'local_only' WHERE id = ?", (project,)))
            await real()

        client.state["harness"].kick_background = lock_first
        await send(client, conversation)
        await background_idle(client)
        assert provider.titles == []
        # Refused at the gate, before it is sent: Local only (the preset) allows no OpenRouter route,
        # and a route it allowed would meet the dispatch check's lock.
        assert await rows(client, "SELECT data ->> 'reason' FROM audit_log WHERE event = 'outbound'"
                                  " AND data ->> 'decision' = 'deny'") == [("not_allowed_at_level",)]
        assert await rows(client, "SELECT status FROM runs WHERE workflow = 'title'") == [("failed",)]


async def test_a_locked_projects_conversation_moves_only_to_another_locked_project(tmp_path):
    async with started(tmp_path / "data") as client:
        source = (await locked_project(client))["id"]
        conversation = (await client.post("/api/conversations", json={"project_id": source})).json()["id"]
        unlocked = (await client.post("/api/projects", json={"name": "L", "sensitivity": "local_only"})).json()["id"]
        response = await client.post(f"/api/conversations/{conversation}/move", json={"project_id": unlocked})
        assert (response.status_code, response.json()["code"]) == (409, "less_strict_project")
        other = (await locked_project(client, None))["id"]
        moved = await client.post(f"/api/conversations/{conversation}/move", json={"project_id": other})
        assert moved.status_code == 200 and moved.json()["project_id"] == other
        back = await client.post(f"/api/conversations/{conversation}/move", json={"project_id": unlocked})
        assert back.json()["code"] == "less_strict_project"


# S1-19b: the router, the support judge and the citation check (I11)

async def router_set(client):
    from backend import budget_router
    current = (await client.get("/api/settings")).json()
    model = sorted(budget_router.RECOMMENDED)[0]
    await client.put("/api/settings", json={"hash": current["hash"], "updates": {"models.router": model}})


async def test_a_locked_project_refuses_the_router_and_the_citation_check_before_anything_is_sent(tmp_path):
    from cited import cited, conversation_in, with_papers
    from test_search import WAGES
    async with cited(tmp_path) as client:
        await router_set(client)
        project, _ = await with_papers(client, WAGES)
        conversation = await conversation_in(client, project)
        answered = await send(client, conversation, "What does the panel show?")  # routed by the small model
        assert answered[-1]["status"] == "succeeded"
        sent = len(client.provider.chats)
        assert (await lock(client, project, venue="J")).status_code == 200
        refused = await client.post(f"/api/conversations/{conversation}/message/stream",
                                    json={"content": "What does the panel show?"})
        assert (refused.status_code, refused.json()["code"]) == (403, "review_locked")
        check = await client.post("/api/citations/check", json={"run_id": answered[0]["run_id"]})
        assert (check.status_code, check.json()["code"]) == (403, "review_locked")
        assert len(client.provider.chats) == sent  # neither a router, an answer nor a judge call


async def test_a_project_locked_while_its_router_call_runs_sends_nothing_more(tmp_path):
    from cited import Provider, cited, conversation_in, is_router, with_papers
    from test_search import WAGES
    release = asyncio.Event()
    provider = Provider()
    async with cited(tmp_path, provider) as client:
        await router_set(client)
        current = (await client.get("/api/settings")).json()
        await client.put("/api/settings", json={"hash": current["hash"], "updates": {"retrieval.router_ms": 5000}})
        project, _ = await with_papers(client, WAGES)
        conversation = await conversation_in(client, project)

        async def held(body):
            await release.wait()
            return provider.answer("literature")
        provider.replies[:] = [held]
        stream = asyncio.create_task(client.post(f"/api/conversations/{conversation}/message/stream",
                                                 json={"content": "What does the panel show?"}))
        await wait_for(lambda: any(is_router(b) for b in provider.chats))
        assert (await lock(client, project, venue="J")).status_code == 200
        release.set()
        finished = events(await stream)
        assert (finished[-1]["status"], finished[-1]["cancel_reason"]) == ("cancelled", "revoked")
        assert provider.answers == [] and provider.judged == []


async def test_a_project_locked_between_the_answer_and_its_judge_sends_no_judge(tmp_path):
    from cited import cited, conversation_in, with_papers
    from test_search import WAGES
    async with cited(tmp_path) as client:
        project, _ = await with_papers(client, WAGES)
        conversation = await conversation_in(client, project)
        provider = client.provider

        async def answer_then_lock(body):
            provider.replies.append(provider._reply)
            assert (await lock(client, project, venue="J")).status_code == 200
            return provider.answer("Earnings rise [S1.1].")
        provider.replies[:] = [answer_then_lock]
        stream = await send(client, conversation, "What do minimum wages do to earnings?")
        assert (stream[-1]["status"], stream[-1].get("cancel_reason")) == ("cancelled", "revoked")
        assert provider.judged == [] and await rows(client, "SELECT count(*) FROM citations") == [(0,)]
