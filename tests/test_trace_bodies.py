"""The trace's exact request bodies and deletion (S1-19b; slice-1 spec F11 and section 4.2: a deletion
reaches every copy the platform holds). Each model call's body is kept in run_bodies with the papers whose
passages it holds; deleting one of those papers deletes the body in every run that sent it, the content
store collects its file, and the trace says it was removed. Lifecycle cases first. Model calls go to a
test-owned mock and the local helper is S1-16's stand-in; nothing leaves the process."""

import asyncio
import json
import os
import time
from datetime import UTC, datetime, timedelta

import pytest

from backend import app as app_module, backups, runs as runs_module
from backend.db import deletion
from cited import Provider, cited, conversation_in, rows, with_papers
from scholia_app import send, started
from test_search import CHINESE, WAGES

pytestmark = pytest.mark.asyncio
QUESTION = "What do minimum wages do to earnings?"


async def bodies_of(client, run_id):
    """{sha256 named by a model_attempt: (served status, trace's body_removed)} for the run."""
    events = (await client.get(f"/api/runs/{run_id}/trace")).json()["events"]
    out = {}
    for event in events:
        if event["type"] == "model_attempt" and event["data"].get("body"):
            sha = event["data"]["body"]
            status = (await client.get(f"/api/runs/{run_id}/trace/bodies/{sha}")).status_code
            out[sha] = (status, event.get("body_removed", False), event["body_ref"])
    return out


async def collected(client, shas):
    """Whether the content store's collection, an idle period later, removes every one of the bodies' files."""
    content = client.state["content"]
    await asyncio.to_thread(content.collect_garbage, datetime.now(UTC) + timedelta(hours=2))
    return all(not (content.root / sha[:2] / sha).exists() for sha in shas)


# Lifecycle


async def test_a_paper_deleted_while_its_answer_is_being_recorded_keeps_no_body_of_its_text(tmp_path):
    # The body is stored before it is sent; the deletion commits while the call is out, before its record:
    # the body holding the paper's passages is never kept, and the run, revoked by the deletion, publishes nothing.
    from cited import ANSWER
    holder = {}

    async def delete_then_answer():
        client = holder["client"]
        await asyncio.to_thread(deletion.delete, client.state["db"], client.state["content"], "material", holder["material"])
        return provider.answer(ANSWER)
    provider = Provider(answer_text=lambda body: delete_then_answer())
    async with cited(tmp_path, provider) as client:
        project, [material] = await with_papers(client, WAGES)
        holder.update(client=client, material=material)
        conversation = await conversation_in(client, project)
        stream = await send(client, conversation, QUESTION)
        assert stream[-1]["status"] in ("cancelled", "failed")
        run_id = stream[0]["run_id"]
        found = await bodies_of(client, run_id)
        assert found and all(status == 410 and removed and ref is None for status, removed, ref in found.values())
        assert await rows(client, "SELECT count(*) FROM run_bodies") == [(0,)]
        assert await collected(client, found)


async def test_a_body_shared_by_two_runs_goes_with_its_paper_from_both_but_not_with_one_conversation(tmp_path):
    async with cited(tmp_path) as client:
        project, [material] = await with_papers(client, WAGES)
        first, second = await conversation_in(client, project), await conversation_in(client, project)
        one, two = (await send(client, first, QUESTION))[0]["run_id"], (await send(client, second, QUESTION))[0]["run_id"]
        shared = set(await bodies_of(client, one)) & set(await bodies_of(client, two))
        assert shared  # the same request, byte for byte, from two runs
        assert (await client.delete(f"/api/conversations/{first}")).status_code == 200
        assert all(status == 200 for status, _, _ in (await bodies_of(client, two)).values())  # still the other run's
        assert (await client.delete(f"/api/materials/{material}")).status_code == 200
        found = await bodies_of(client, two)
        assert all(found[sha][:2] == (410, True) for sha in shared)
        assert await rows(client, "SELECT count(*) FROM run_bodies") == [(0,)]
        assert await rows(client, "SELECT count(*) FROM run_body_materials") == [(0,)]
        assert await collected(client, shared)


async def test_deleting_one_of_two_papers_in_a_body_removes_the_body(tmp_path):
    provider = Provider(answer_text="Earnings rise [S1.1]. 收入上升 [S2.1].")
    async with cited(tmp_path, provider) as client:
        project, [wages, chinese] = await with_papers(client, WAGES, CHINESE)
        conversation = await conversation_in(client, project)
        run_id = (await send(client, conversation, "最低工资 minimum wages earnings"))[0]["run_id"]
        before = await bodies_of(client, run_id)
        answer_sha = next(iter(before))  # the answer step's, first
        assert sorted(m for (m,) in await rows(client, "SELECT material_id FROM run_body_materials WHERE sha256 = ?",
                                               answer_sha)) == sorted([wages, chinese])
        assert before[answer_sha][0] == 200
        assert (await client.delete(f"/api/materials/{chinese}")).status_code == 200
        after = await bodies_of(client, run_id)
        assert after[answer_sha][:2] == (410, True)  # it held the deleted paper's passages, with the other's
        assert await rows(client, "SELECT count(*) FROM run_body_materials WHERE sha256 = ?", answer_sha) == [(0,)]
        assert await rows(client, "SELECT count(*) FROM materials WHERE id = ?", wages) == [(1,)]
        held = (await client.get(f"/api/runs/{run_id}/trace/bodies/{answer_sha}")).json()
        assert "最低工资提高了" not in json.dumps(held, ensure_ascii=False)


async def test_a_failed_cleanup_after_the_deletion_commits_still_removes_the_body_and_collects_it_later(
        tmp_path, monkeypatch):
    async with cited(tmp_path) as client:
        project, [material] = await with_papers(client, WAGES)
        conversation = await conversation_in(client, project)
        run_id = (await send(client, conversation, QUESTION))[0]["run_id"]
        content = client.state["content"]
        real = content.collect_garbage
        monkeypatch.setattr(content, "collect_garbage", lambda now=None: (_ for _ in ()).throw(OSError("disk")))
        assert (await client.delete(f"/api/materials/{material}")).status_code == 200  # committed; cleanup failed
        found = await bodies_of(client, run_id)
        assert found and all(status == 410 for status, _, _ in found.values())
        monkeypatch.setattr(content, "collect_garbage", real)
        assert await collected(client, found)  # the next collection removes the files


async def aged(client, shas):
    """Make the bodies' rows and files older than the content store's idle window."""
    await asyncio.to_thread(client.state["db"].write, lambda conn: conn.executemany(
        "UPDATE content_files SET created_at = '2020-01-01T00:00:00.000Z' WHERE sha256 = ?", [(s,) for s in shas]))
    old = time.time() - 2 * 3600
    for sha in shas:
        os.utime(client.state["content"].root / sha[:2] / sha, (old, old))


async def until(check, timeout=5.0):
    deadline = asyncio.get_running_loop().time() + timeout
    while not check():
        assert asyncio.get_running_loop().time() < deadline, "timed out"
        await asyncio.sleep(0.02)


@pytest.mark.parametrize("orphan", ["deleted_before_link", "never_sent"])
async def test_a_body_left_unlinked_is_collected_by_the_scheduled_pass_once_idle_without_another_deletion(
        tmp_path, monkeypatch, orphan):
    # T8 (PR 41): a body stored before dispatch and left unlinked (its paper deleted between the store and the
    # link, or the attempt never sent) is collected by the app's own pass once past the idle window, not only
    # by a later deletion; the idle window still protects it before then.
    from backend import openrouter
    monkeypatch.setattr(app_module, "COLLECT_SECONDS", 0.05, raising=False)
    stored, holder, real_stored = [], {}, runs_module._stored

    def storing(content, body):
        ref = real_stored(content, body)
        stored.append(ref)
        if orphan == "deleted_before_link" and len(stored) == 1:
            deletion.delete(holder["client"].state["db"], content, "material", holder["material"])
        return ref
    monkeypatch.setattr(runs_module, "_stored", storing)
    if orphan == "never_sent":
        real = openrouter.query_model

        async def stop_after_keeping(*args, on_body=None, **kwargs):
            async def keep_then_stop(body):
                await on_body(body)
                raise asyncio.CancelledError()
            return await real(*args, on_body=keep_then_stop, **kwargs)
        monkeypatch.setattr(openrouter, "query_model", stop_after_keeping)
    async with cited(tmp_path) as client:
        project, [material] = await with_papers(client, WAGES)
        holder.update(client=client, material=material)
        conversation = await conversation_in(client, project)
        stream = await send(client, conversation, QUESTION)
        assert stream[-1]["status"] in ("cancelled", "failed")
        sha = stored[0]
        content = client.state["content"]
        path = content.root / sha[:2] / sha
        assert await rows(client, "SELECT count(*) FROM run_bodies") == [(0,)] and path.exists()
        passes, real_collect = [], content.collect_garbage
        monkeypatch.setattr(content, "collect_garbage", lambda now=None: passes.append(1) or real_collect(now))
        await until(lambda: len(passes) >= 2)
        assert path.exists()  # not idle yet: kept
        await aged(client, [sha])
        await until(lambda: not path.exists())
        assert await rows(client, "SELECT count(*) FROM content_files WHERE sha256 = ?", sha) == [(0,)]


async def test_unreferenced_content_past_the_idle_window_is_collected_at_launch(tmp_path):
    # T8: the first pass runs as the app opens, before any deletion or timer.
    async with started(tmp_path) as client:
        sha = await asyncio.to_thread(client.state["content"].put, b'{"orphan": true}', "application/json")
        await aged(client, [sha])
        path = client.state["content"].root / sha[:2] / sha
    assert path.exists()
    async with started(tmp_path) as client:
        await until(lambda: not path.exists())
        assert await rows(client, "SELECT count(*) FROM content_files WHERE sha256 = ?", sha) == [(0,)]


# Success paths


async def test_a_kept_body_is_served_and_named_and_a_deleted_papers_body_leaves_backups(tmp_path):
    async with cited(tmp_path) as client:
        project, [material] = await with_papers(client, WAGES)
        conversation = await conversation_in(client, project)
        run_id = (await send(client, conversation, QUESTION))[0]["run_id"]
        found = await bodies_of(client, run_id)
        assert len(found) == 2 and all(status == 200 and ref for status, removed, ref in found.values())  # answer, judge
        linked = await rows(client, "SELECT DISTINCT material_id FROM run_body_materials")
        assert linked == [(material,)]
        db = client.state["db"]
        referenced = {sha for sha, *_ in await asyncio.to_thread(db.read, backups._referenced_content)}
        assert set(found) <= referenced  # a full backup carries them while they are kept
        assert (await client.delete(f"/api/materials/{material}")).status_code == 200
        referenced = {sha for sha, *_ in await asyncio.to_thread(db.read, backups._referenced_content)}
        assert not set(found) & referenced  # and never after the deletion
        assert (await client.get(f"/api/runs/{run_id}/trace/bodies/{'0' * 64}")).status_code == 404


async def test_deleting_the_project_deletes_every_body_and_its_links(tmp_path):
    async with cited(tmp_path) as client:
        project, _ = await with_papers(client, WAGES)
        conversation = await conversation_in(client, project)
        run_id = (await send(client, conversation, QUESTION))[0]["run_id"]
        shas = list(await bodies_of(client, run_id))
        assert (await client.delete(f"/api/projects/{project}")).status_code == 200
        assert await rows(client, "SELECT count(*) FROM run_bodies") == [(0,)]
        assert await rows(client, "SELECT count(*) FROM run_body_materials") == [(0,)]
        assert await collected(client, shas)


async def test_a_body_whose_file_goes_between_its_check_and_its_read_answers_removed_not_500(tmp_path, monkeypatch):
    async with cited(tmp_path) as client:
        project, [material] = await with_papers(client, WAGES)
        conversation = await conversation_in(client, project)
        run_id = (await send(client, conversation, QUESTION))[0]["run_id"]
        sha = next(iter(await bodies_of(client, run_id)))
        content = client.state["content"]
        real = content.read

        def deleted_then_read(name):  # the deletion commits and collection removes the file in between
            deletion.delete(client.state["db"], content, "material", material)
            content.collect_garbage(datetime.now(UTC) + timedelta(hours=2))
            return real(name)
        monkeypatch.setattr(content, "read", deleted_then_read)
        response = await client.get(f"/api/runs/{run_id}/trace/bodies/{sha}")
        assert (response.status_code, response.json()["code"]) == (410, "body_removed")
