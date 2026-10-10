"""Background runs under Settings, Advanced (slice-1 spec sections 5 and 6.1; the M1 audit's finding 4):
progress and retry in the list, and full backups and project exports as background runs that answer
with their run's id, with Cancel, whose passphrase is held in memory only."""

import asyncio
import threading

import pytest

import backend.backups as backups_module
from scholia_app import background_idle, run_finished, started
from test_materials import PDF, added, hold_extraction, project_of, rows, settled

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


async def test_a_run_that_ends_while_the_list_is_read_never_reads_interrupted(tmp_path, monkeypatch):
    reached, go = hold_extraction(monkeypatch)
    async with started(tmp_path / "data") as client:
        project = (await client.post("/api/projects", json={"name": "Review", "sensitivity": "local_only",
                                                           "review_lock": True})).json()["id"]  # no lookup: one run
        [paper] = (await added(client, project, PDF))["materials"]
        await asyncio.to_thread(reached.wait, 10)
        harness, db = client.state["harness"], client.state["db"]
        seen, resume, real_read = threading.Event(), threading.Event(), db.read

        def read(fn):  # the list's read returns only once the run it saw running has ended and been released
            result = real_read(fn)
            if fn.__name__ == "listing" and not seen.is_set():
                seen.set()
                resume.wait(10)
            return result

        monkeypatch.setattr(db, "read", read)
        listed = asyncio.create_task(client.get("/api/activity", params={"run_id": paper["run_id"]}))
        await asyncio.to_thread(seen.wait, 10)  # the read saw the reading running
        go.set()
        while harness.registry.is_active(paper["run_id"]):  # its terminal record committed, then released
            await asyncio.sleep(0.01)
        resume.set()
        [row] = (await listed).json()["runs"]
        assert row["status"] == "running"  # what the read saw, never interrupted
        assert (await run_finished(client, paper["run_id"]))["status"] == "succeeded"


async def test_a_reading_whose_terminal_write_fails_reads_interrupted_needs_attention_and_is_tried_again(
        tmp_path, monkeypatch):
    from backend import runs as runs_module
    real, failed = runs_module.Harness._finish_local, []

    def failing_once(self, conn, active, status, cancel_reason, summary, effect=None):
        if effect is not None and not failed:  # the reading's terminal write, the first time: nothing of it commits
            failed.append(active.run_id)
            raise RuntimeError("the terminal write failed")
        return real(self, conn, active, status, cancel_reason, summary, effect)

    monkeypatch.setattr(runs_module.Harness, "_finish_local", failing_once)
    async with started(tmp_path / "data") as client:
        project = await project_of(client)
        result = await added(client, project, PDF)
        [paper] = result["materials"]
        run = await run_finished(client, paper["run_id"])
        assert failed == [paper["run_id"]]
        assert await rows(client, "SELECT status FROM runs WHERE id = ?", paper["run_id"]) == [("running",)]
        assert (run["status"], run["retryable"]) == ("interrupted", True)  # as the list reads it, Retry offered
        [stopped] = await settled(client, project)  # its lookup did not wait for the abandoned reading
        assert (stopped["state"], stopped["reason"]) == ("needs_attention", "interrupted")
        assert stopped["reading"]["status"] == "interrupted" and stopped["lookup"]["outcome"] == "not_read"
        again = await client.post(f"/api/runs/{paper['run_id']}/retry")
        assert again.status_code == 201, again.text
        assert (await run_finished(client, again.json()["run_id"]))["status"] == "succeeded"
        [ready] = await settled(client, project)
        assert ready["state"] == "ready" and ready["extraction"]["passages"] > 0


async def test_the_list_reads_a_runs_status_and_its_retry_at_one_moment(tmp_path, monkeypatch):
    """A reading whose terminal write fails is released just after the list's read has looked at it (held,
    so not to be tried again) and before the list answers: the row says running without Retry, as its read
    found it, never interrupted without Retry (which a later read would correct)."""
    from backend import materials
    from backend import runs as runs_module
    real_finish, real_details = runs_module.Harness._finish_local, materials.run_details
    writing, looked, target = threading.Event(), threading.Event(), []

    def failing_once(self, conn, active, status, cancel_reason, summary, effect=None):
        if effect is not None and not target:  # the reading's terminal write: fails once the list has looked
            target.append(active.run_id)
            writing.set()
            looked.wait(10)
            raise RuntimeError("the terminal write failed")
        return real_finish(self, conn, active, status, cancel_reason, summary, effect)

    async with started(tmp_path / "data") as client:
        registry = client.state["harness"].registry

        def looking(conn, run_id, *args):  # the list's read, the run still held; then held up until it is released
            found = real_details(conn, run_id, *args)
            if target and run_id == target[0] and not looked.is_set():
                looked.set()
                for _ in range(500):
                    if not registry.is_active(run_id):
                        break
                    threading.Event().wait(0.01)
            return found
        monkeypatch.setattr(runs_module.Harness, "_finish_local", failing_once)
        monkeypatch.setattr(materials, "run_details", looking)
        project = await project_of(client)
        [paper] = (await added(client, project, PDF))["materials"]
        assert await asyncio.to_thread(writing.wait, 10)
        [row] = (await client.get("/api/activity", params={"run_id": paper["run_id"]})).json()["runs"]
        assert looked.is_set() and not registry.is_active(paper["run_id"])  # released before the list answered
        assert (row["status"], row["retryable"]) == ("running", False)  # as its read found it
        later = await run_finished(client, paper["run_id"])
        assert (later["status"], later["retryable"]) == ("interrupted", True)


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


async def test_the_list_offers_retry_exactly_where_the_endpoint_takes_it(tmp_path):
    async with started(tmp_path / "data") as client:
        project = await project_of(client)
        broken = await added(client, project, ("broken.pdf", b"%PDF-1.7\n" + b"\x00garbage" * 50))
        [paper] = broken["materials"]
        failed = await run_finished(client, paper["run_id"])
        assert (failed["status"], failed["retryable"]) == ("failed", True)
        # Its file replaced since: the failed reading is of a version no longer current.
        await added(client, project, ("fixed.md", b"# Fixed\n\nA readable file this time.\n"), material_id=paper["id"])
        [row] = (await client.get("/api/activity", params={"run_id": paper["run_id"]})).json()["runs"]
        refused = await client.post(f"/api/runs/{paper['run_id']}/retry")
        assert row["retryable"] is False and (refused.status_code, refused.json()["code"]) == (409, "not_retryable")
        # An interrupted lookup is offered Retry, and the endpoint takes it.
        lookup_run = broken["lookup_run_id"]
        await run_finished(client, lookup_run)
        await asyncio.to_thread(client.state["db"].write, lambda conn: conn.execute(
            "UPDATE runs SET status = 'interrupted' WHERE id = ?", (lookup_run,)))
        [row] = (await client.get("/api/activity", params={"run_id": lookup_run})).json()["runs"]
        assert row["status"] == "interrupted" and row["retryable"] is True
        assert (await client.post(f"/api/runs/{lookup_run}/retry")).status_code == 201


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
