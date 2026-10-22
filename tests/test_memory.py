"""Memory records, their checks and sources, the memory API and the per-project switch (S1-21a; slice-1 spec F10,
sections 4.1, 5 and 8; checklist M1 to M4, V2, V3, P2, G2, G3). Every model call goes to the test-owned provider."""

import asyncio
import json
import sqlite3

import pytest

from backend import memory
from scholia_app import MockProvider, background_idle, send, started
from test_materials import added, project_of, settled

pytestmark = pytest.mark.asyncio

MARKDOWN = ("notes.md", b"# Notes\n\nMinimum wages raise the earnings of low-paid workers.\n")


async def rows(client, sql, *args):
    return await asyncio.to_thread(client.state["db"].read, lambda conn: conn.execute(sql, args).fetchall())


async def write(client, fn):
    return await asyncio.to_thread(client.state["db"].write, fn)


async def conversation_in(client, project, title="t"):
    return (await client.post("/api/conversations", json={"project_id": project, "title": title})).json()["id"]


async def set_project(client, project, updates):
    settings = (await client.get(f"/api/settings?project_id={project}")).json()
    saved = await client.put("/api/settings", json={"project_id": project, "hash": settings["hash"], "updates": updates})
    assert saved.status_code == 200, saved.text


async def memory_off(client, project):
    await set_project(client, project, {"memory.enabled": False})


def extracted(provider, *records, cost=0.001):
    """The extractor's recorded reply: these candidates."""
    provider.memory_replies.append(provider.answer(json.dumps({"records": list(records)}), cost=cost))


def candidate(content, status="confirmed", kind="decision", span="", replaces=""):
    return {"type": kind, "content": content, "status": status, "span": span, "replaces": replaces}


async def records(client, project=None):
    found = (await client.get("/api/memory", params={"project_id": project} if project else {})).json()
    return found["personal"] + found["project"]


# M1, M2: the record's shape, checked by the database


async def test_a_record_of_an_unknown_type_status_or_source_kind_is_refused_by_the_database(tmp_path):
    async with started(tmp_path / "data") as client:
        project = await project_of(client)

        def insert(kind, status, source="turn", scope="project"):
            def fn(conn):
                record = memory.new_id()
                conn.execute("INSERT INTO memory_records (id, scope, project_id, type, content, status)"
                             " VALUES (?, ?, ?, ?, 'x', ?)", (record, scope, project, kind, status))
                conn.execute("INSERT INTO memory_sources (memory_id, source_kind, source_id) VALUES (?, ?, 'x')",
                             (record, source))
            return write(client, fn)
        for kind, status in (("fact", "confirmed"), ("decision", "deleted"), ("decision", "active")):
            with pytest.raises(sqlite3.IntegrityError):
                await insert(kind, status)
        with pytest.raises(sqlite3.IntegrityError):
            await insert("decision", "confirmed", source="web")
        with pytest.raises(sqlite3.IntegrityError):  # never confirmed: the 0001 check (M2)
            await insert("progress", "confirmed")
        with pytest.raises(sqlite3.IntegrityError):
            await insert("agent_note", "confirmed")
        await insert("decision", "inferred")  # a valid one is kept
        assert await rows(client, "SELECT type, status FROM memory_records") == [("decision", "inferred")]


async def test_the_api_never_creates_or_confirms_progress_or_an_agent_note(tmp_path):
    async with started(tmp_path / "data") as client:
        project = await project_of(client)
        for kind in ("progress", "agent_note"):
            response = await client.post("/api/memory", json={"project_id": project, "type": kind, "content": "x"})
            assert response.status_code == 400 and response.json()["code"] == "invalid_request"
        await added(client, project, MARKDOWN)  # what's done: a progress record, written by code
        [progress] = [r for r in await records(client, project) if r["type"] == "progress"]
        assert progress["status"] == "inferred"
        refused = await client.patch(f"/api/memory/{progress['id']}", json={"version": 1, "status": "confirmed"})
        assert refused.status_code == 400 and refused.json()["code"] == "not_confirmable"


# V2: the API; M3: every record has a source; M4: versions; G2: blank text


async def test_the_views_create_edit_supersede_and_forget_records_at_the_version_seen(tmp_path):
    async with started(tmp_path / "data") as client:
        project = await project_of(client)
        created = await client.post("/api/memory", json={"project_id": project, "type": "decision",
                                                          "content": "We use semi-structured interviews."})
        assert created.status_code == 201, created.text
        first = created.json()
        assert (first["status"], first["version"], first["scope"]) == ("confirmed", 1, "project")
        assert first["sources"] == [{"kind": "researcher", "id": "views"}]  # M3
        personal = (await client.post("/api/memory", json={"project_id": project, "type": "preference",
                                                           "scope": "personal", "content": "Short answers."})).json()
        assert personal["scope"] == "personal"
        bad = await client.post("/api/memory", json={"project_id": project, "type": "decision", "scope": "personal",
                                                      "content": "x"})
        assert bad.json()["code"] == "invalid_scope"
        for blank in ("", "   ", "​​"):  # G2: nothing visible
            response = await client.post("/api/memory", json={"project_id": project, "type": "decision", "content": blank})
            assert response.status_code == 400 and response.json()["code"] == "empty"

        edited = await client.patch(f"/api/memory/{first['id']}", json={"version": 1, "content": "We use surveys."})
        assert edited.status_code == 200, edited.text
        second = edited.json()
        assert (second["version"], second["supersedes_id"], second["status"]) == (2, first["id"], "confirmed")
        assert await rows(client, "SELECT status FROM memory_records WHERE id = ?", first["id"]) == [("superseded",)]
        stale = await client.patch(f"/api/memory/{first['id']}", json={"version": 1, "content": "Again"})
        assert stale.status_code == 409 and stale.json()["code"] == "version_changed"
        stale = await client.patch(f"/api/memory/{second['id']}", json={"version": 1, "content": "Again"})
        assert stale.status_code == 409
        shown = await records(client, project)
        assert {r["id"] for r in shown} == {second["id"], personal["id"]}  # a superseded record is not listed

        assert (await client.delete(f"/api/memory/{second['id']}")).json() == {"ok": True}
        # Forget deletes the content, with the version it replaced; a tombstone and a content-free audit record stay
        assert await rows(client, "SELECT count(*) FROM memory_records WHERE id IN (?, ?)", first["id"], second["id"]) == [(0,)]
        assert await rows(client, "SELECT count(*) FROM memory_sources WHERE memory_id IN (?, ?)",
                          first["id"], second["id"]) == [(0,)]
        assert await rows(client, "SELECT kind, title FROM tombstones WHERE object_id = ?", second["id"]) == [("memory", None)]
        [(data,)] = await rows(client, "SELECT data FROM audit_log WHERE event = 'deletion' ORDER BY seq DESC LIMIT 1")
        assert "surveys" not in data and json.loads(data)["kind"] == "memory"
        assert (await client.delete(f"/api/memory/{second['id']}")).status_code == 404


async def test_a_record_written_by_any_path_has_a_source(tmp_path):
    provider = MockProvider()
    async with started(tmp_path / "data", provider) as client:
        project = await project_of(client)
        conversation = await conversation_in(client, project)
        extracted(provider, candidate("We study wages.", "proposed", "project_state"))
        await send(client, conversation, "We study wages")
        await background_idle(client)
        await send(client, conversation, "Remember: my method is semi-structured interviews")
        await added(client, project, MARKDOWN)
        await client.post("/api/memory", json={"project_id": project, "type": "event", "content": "Met my advisor."})
        assert await rows(client, "SELECT count(*) FROM memory_records m WHERE NOT EXISTS"
                                  " (SELECT 1 FROM memory_sources s WHERE s.memory_id = m.id)") == [(0,)]
        assert (await rows(client, "SELECT count(*) FROM memory_records"))[0][0] >= 4


# V3, G3: the per-project switch


async def test_memory_off_stops_extraction_recall_and_things_to_confirm_and_a_command_says_so(tmp_path):
    provider = MockProvider()
    async with started(tmp_path / "data", provider) as client:
        project = await project_of(client)
        conversation = await conversation_in(client, project)
        await client.post("/api/memory", json={"project_id": project, "type": "decision", "content": "Kept while off."})
        extracted(provider, candidate("A proposal", "proposed"))
        await send(client, conversation, "I am considering a survey")
        await background_idle(client)
        assert len(provider.extractions) == 1
        await memory_off(client, project)

        stream = await send(client, conversation, "Remember: my deadline is in May")
        assert {"type": "memory", "action": "memory_off", "count": 0} in stream
        await send(client, conversation, "What did we decide?")
        await background_idle(client)
        assert len(provider.extractions) == 1  # no extraction while it is off (G3)
        assert await rows(client, "SELECT count(*) FROM runs WHERE workflow = 'memory'") == [(1,)]
        assert "Kept while off." not in json.dumps(provider.answers[-1])  # no recall
        off_note = provider.answers[-2]["messages"][0]["content"]
        assert "Memory is off in this project" in off_note
        review = (await client.get(f"/api/memory/review?project_id={project}")).json()
        assert review == {"proposals": [], "notice": False, "memory_enabled": False}
        listed = (await client.get(f"/api/memory?project_id={project}")).json()
        assert listed["memory_enabled"] is False and {r["content"] for r in listed["project"]} >= {"Kept while off."}
        turns = (await client.get(f"/api/conversations/{conversation}")).json()["turns"]
        assert [t["memory_status"] for t in turns] == ["indexed", "skipped", "skipped"]

        await set_project(client, project, {"memory.enabled": True})  # on again: the next turn extracts
        await send(client, conversation, "Thanks")
        await background_idle(client)
        assert len(provider.extractions) == 2


# P2: what's done, from AAB's own records


async def test_papers_added_and_removed_are_progress_records_without_titles(tmp_path):
    async with started(tmp_path / "data") as client:
        project = await project_of(client)
        listing = await added(client, project, MARKDOWN, ("other.md", b"# Other\n\nText.\n"))
        await settled(client, project)
        [done] = [r for r in await records(client, project) if r["type"] == "progress"]
        assert done["structured"]["event"] == "materials_added" and done["structured"]["count"] == 2
        assert {s["kind"] for s in done["sources"]} == {"paper"}
        first = listing["materials"][0]["id"]
        assert (await client.delete(f"/api/materials/{first}")).status_code == 200
        progress = {r["structured"]["event"]: r for r in await records(client, project) if r["type"] == "progress"}
        assert progress["materials_removed"]["structured"]["count"] == 1
        assert "notes" not in progress["materials_removed"]["content"]  # no title is kept
        assert progress["materials_added"]["sources"] == [{"kind": "paper", "id": listing["materials"][1]["id"]}]
        await client.delete(f"/api/materials/{listing['materials'][1]['id']}")
        progress = {r["structured"]["event"] for r in await records(client, project) if r["type"] == "progress"}
        assert progress == {"materials_removed"}  # the count of added papers went with the last of them


async def test_commands_recognize_english_and_chinese_and_never_a_question():
    cases = {
        "Remember: my method is semi-structured interviews": ("save", "my method is semi-structured interviews"),
        "remember that I prefer short answers": ("save", "I prefer short answers"),
        "Please save: the deadline is 30 November": ("save", "the deadline is 30 November"),
        "save this": ("save", None),
        "Remember this.": ("save", None),
        "Correct: the sample is 40 interviews, not 30": ("correct", "the sample is 40 interviews, not 30"),
        "update: we now use a survey": ("update", "we now use a survey"),
        "update this": ("update", None),
        "Forget: the survey idea": ("forget", "the survey idea"),
        "forget about the pilot": ("forget", "the pilot"),
        "forget this": ("forget", None),
        "记住：我的方法是半结构化访谈": ("save", "我的方法是半结构化访谈"),
        "请记住我偏好简短的回答": ("save", "我偏好简短的回答"),
        "保存这个": ("save", None),
        "更正：样本是40人": ("correct", "样本是40人"),
        "更新：我们改用问卷": ("update", "我们改用问卷"),
        "忘记：问卷的想法": ("forget", "问卷的想法"),
        "忘记这个": ("forget", None),
    }
    for message, (action, text) in cases.items():
        assert memory.command(message) == {"action": action, "text": text}, message
    for message in ("Remember when we decided on interviews?", "Save time by batching?", "What do you remember?",
                    "Saving money matters", "I forgot the file", "忘记了文件", "记住了吗？", "Update me on the papers",
                    "Note how the wages changed", "x" * 10):
        assert memory.command(message) is None, message
    assert memory.command("Remember: " + "x" * 1001) is None  # longer than a record holds: not a command
