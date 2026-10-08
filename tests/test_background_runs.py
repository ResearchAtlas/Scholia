"""Background runs under Settings, Advanced (slice-1 spec sections 5 and 6.1; the M1 audit's finding 4):
progress and retry in the list, and full backups and project exports as background runs that answer
with their run's id, with Cancel, whose passphrase is held in memory only."""

import asyncio
import threading

import pytest

import backend.backups as backups_module
from scholia_app import background_idle, run_finished, started
from test_materials import PDF, added, hold_extraction, project_of, rows

pytestmark = pytest.mark.asyncio

PASSPHRASE = "a-passphrase-never-stored-anywhere"


def hold_naming(monkeypatch):
    """Hold an archive's thread as it names its file, until go is set."""
    reached, go, real = threading.Event(), threading.Event(), backups_module._free_name

    def held(*args):
        reached.set()
        go.wait(10)
        return real(*args)

    monkeypatch.setattr(backups_module, "_free_name", held)
    return reached, go


async def test_the_list_shows_a_readings_progress_its_papers_and_retry_once_it_failed(tmp_path, monkeypatch):
    reached, go = hold_extraction(monkeypatch)
    async with started(tmp_path / "data") as client:
        project = await project_of(client)
        [paper] = (await added(client, project, PDF))["materials"]
        await asyncio.to_thread(reached.wait, 10)
        [row] = (await client.get("/api/activity", params={"run_id": paper["run_id"]})).json()["runs"]
        assert (row["workflow"], row["status"], row["progress"]) == ("extract", "running", {"done": 0, "total": 1})
        assert row["materials"] == {"titles": ["paper"], "count": 1} and row["retryable"] is False
        listed = (await client.get("/api/activity")).json()["runs"]
        assert {r["workflow"] for r in listed if r["status"] == "running"} >= {"extract", "lookup"}
        await client.post(f"/api/runs/{paper['run_id']}/cancel")
        go.set()
        stopped = await run_finished(client, paper["run_id"])
        assert stopped["retryable"] is True and stopped["progress"] is None


async def test_retry_is_only_for_a_stopped_or_failed_reading_or_lookup(tmp_path):
    async with started(tmp_path / "data") as client:
        project = await project_of(client)
        conversation = (await client.post("/api/conversations", json={"project_id": project})).json()["id"]
        await client.post(f"/api/conversations/{conversation}/message/stream", json={"content": "Hello"})
        await background_idle(client)
        [(title,)] = await rows(client, "SELECT id FROM runs WHERE workflow = 'title'")
        refused = await client.post(f"/api/runs/{title}/retry")
        assert (refused.status_code, refused.json()["code"]) == (409, "not_retryable")
        assert (await client.post("/api/runs/00000000-0000-4000-8000-000000000000/retry")).status_code == 404


async def test_a_full_backup_answers_with_its_run_and_never_stores_its_passphrase(tmp_path):
    data, destination = tmp_path / "data", tmp_path / "chosen"
    destination.mkdir()
    async with started(data) as client:
        await project_of(client, "Interviews", "private")
        response = await client.post("/api/backups/full", json={"destination": str(destination),
                                                                "passphrase": PASSPHRASE})
        assert response.status_code == 202 and set(response.json()) == {"run_id"}
        run = await run_finished(client, response.json()["run_id"])
        assert run["status"] == "succeeded" and run["workflow"] == "full_backup" and run["project_kind"] == "general"
        assert run["result"]["encrypted"] is True and run["result"]["file"].startswith(str(destination))
        await asyncio.to_thread(client.state["db"].checkpoint)
    stored = b"".join(path.read_bytes() for path in data.rglob("*") if path.is_file() and "backups" not in path.parts)
    assert PASSPHRASE.encode() not in stored  # held in memory only: not in the database, its WAL or settings


async def test_cancel_stops_an_export_and_leaves_no_file(tmp_path, monkeypatch):
    destination = tmp_path / "chosen"
    destination.mkdir()
    reached, go = hold_naming(monkeypatch)
    async with started(tmp_path / "data") as client:
        project = await project_of(client)
        run_id = (await client.post(f"/api/projects/{project}/export",
                                    json={"destination": str(destination)})).json()["run_id"]
        await asyncio.to_thread(reached.wait, 10)
        [row] = (await client.get("/api/activity", params={"run_id": run_id})).json()["runs"]
        assert row["status"] == "running" and row["workflow"] == "project_export"
        cancelling = asyncio.create_task(client.post(f"/api/runs/{run_id}/cancel"))
        await asyncio.sleep(0.1)
        go.set()
        assert (await cancelling).json()["status"] == "cancelled"
        run = await run_finished(client, run_id)
        assert (run["status"], run["cancel_reason"]) == ("cancelled", "researcher")
    assert list(destination.iterdir()) == []


async def test_a_cancel_that_comes_once_the_archive_is_published_leaves_it_succeeded(tmp_path, monkeypatch):
    destination = tmp_path / "chosen"
    destination.mkdir()
    published, real_publish = [], backups_module._publish

    def publish(tmp, final):
        real_publish(tmp, final)
        published.append(final)

    monkeypatch.setattr(backups_module, "_publish", publish)
    async with started(tmp_path / "data") as client:
        harness = client.state["harness"]
        real_write, cancelled = harness._write, []

        async def write(fn):  # the run's terminal record, queued once its file is published: a Cancel comes
            if published and not cancelled:
                [active] = [a for a in harness.registry.runs.values() if a.kind == "background"]
                cancelled.append(active.run_id)
                harness._request_cancel(active, "researcher")
                await asyncio.sleep(0.05)  # still queued when the cancellation arrives
            return await real_write(fn)

        monkeypatch.setattr(harness, "_write", write)
        run_id = (await client.post("/api/backups/full", json={"destination": str(destination)})).json()["run_id"]
        run = await run_finished(client, run_id)
        assert cancelled == [run_id]
        assert (run["status"], run["cancel_reason"]) == ("succeeded", None)  # what the folder holds, it says
        assert [path.name for path in destination.iterdir()] == [published[0].name]
        assert run["result"]["file"] == str(published[0])


async def test_a_backup_a_restart_left_unfinished_reads_interrupted_and_is_not_started_again(tmp_path, monkeypatch):
    data, destination = tmp_path / "data", tmp_path / "chosen"
    destination.mkdir()
    reached, go = hold_naming(monkeypatch)
    async with started(data) as client:
        run_id = (await client.post("/api/backups/full", json={"destination": str(destination)})).json()["run_id"]
        await asyncio.to_thread(reached.wait, 10)
        threading.Timer(0.3, go.set).start()  # it goes on while the app closes, which stops it
    monkeypatch.undo()
    async with started(data, setup=False) as client:
        run = await run_finished(client, run_id)
        assert run["status"] == "interrupted"
    assert list(destination.iterdir()) == []


async def test_an_export_of_a_deleted_project_is_refused_before_a_run_starts(tmp_path):
    destination = tmp_path / "chosen"
    destination.mkdir()
    async with started(tmp_path / "data") as client:
        project = await project_of(client)
        await client.delete(f"/api/projects/{project}")
        response = await client.post(f"/api/projects/{project}/export", json={"destination": str(destination)})
        assert response.status_code == 404
        assert await rows(client, "SELECT count(*) FROM runs WHERE workflow = 'project_export'") == [(0,)]
