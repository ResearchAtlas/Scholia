"""The trace's exact request bodies and deletion (S1-19b; slice-1 spec F11 and section 4.2: a deletion
reaches every copy the platform holds). Each model call's body is kept in run_bodies with the papers whose
passages it holds; deleting one of those papers deletes the body in every run that sent it, the content
store collects its file, and the trace says it was removed. Lifecycle cases first. Model calls go to a
test-owned mock and the local helper is S1-16's stand-in; nothing leaves the process."""

import asyncio
import json
from datetime import UTC, datetime, timedelta

import pytest

from backend import backups, runs as runs_module
from backend.db import deletion
from cited import Provider, cited, conversation_in, rows, with_papers
from scholia_app import send
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


async def test_a_paper_deleted_while_its_answer_is_being_recorded_keeps_no_body_of_its_text(tmp_path, monkeypatch):
    # The deletion commits after the answer came back and before its record: the body holding the paper's
    # passages is never kept, and the run, revoked by the deletion, publishes nothing.
    async with cited(tmp_path) as client:
        project, [material] = await with_papers(client, WAGES)
        conversation = await conversation_in(client, project)
        real, done = runs_module._stored, []

        def delete_then_store(content, body):
            if not done:
                done.append(deletion.delete(client.state["db"], content, "material", material))
            return real(content, body)
        monkeypatch.setattr(runs_module, "_stored", delete_then_store)
        stream = await send(client, conversation, QUESTION)
        assert stream[-1]["status"] in ("cancelled", "failed") and done
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
