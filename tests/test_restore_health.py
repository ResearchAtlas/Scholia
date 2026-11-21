"""Design review AC (PR 63, batch G): what the engine says, and serves, in each state a restore passes through. Each
step waits for the event it names (a held worker thread, a lock taken); nothing sleeps for a length of time."""

import asyncio

import pytest

import backend.backups as backups_module
from scholia_app import started
from test_backups import hold

pytestmark = pytest.mark.asyncio


async def names(client):
    answer = await client.get("/api/projects")
    return answer.status_code, {p["name"] for p in answer.json().get("projects", [])}


async def a_backup_then_a_project(client):
    backup = (await client.post("/api/backups")).json()["id"]
    await client.post("/api/projects", json={"name": "Made after the backup"})
    return backup


# r6-1's finding 2, its mechanism: while the backup is staged every read is answered, by the database from before.
# Guard for every option that keeps "checked and copied while the app runs on" (backups.py:719): green at the head.
async def test_while_a_restore_stages_its_backup_the_app_serves_the_database_from_before(tmp_path, monkeypatch):
    async with started(tmp_path / "data") as client:
        backup = await a_backup_then_a_project(client)
        staging, release = hold(monkeypatch, backups_module, "_stage")
        restore = asyncio.create_task(client.post("/api/backups/restore", json={"generation": backup}))
        await asyncio.to_thread(staging.wait, 10)
        assert (await client.get("/api/health")).status_code == 200
        assert (await client.get("/api/setup")).status_code == 200
        assert (await client.get("/api/settings")).status_code == 200
        assert await names(client) == (200, {"General", "Made after the backup"})
        release.set()
        assert (await restore).status_code == 200
        assert await names(client) == (200, {"General"})


# Rule E: health says a restore is under way while its backup is staged, and no longer once it has ended.
async def test_health_says_a_restore_is_under_way_while_its_backup_is_staged(tmp_path, monkeypatch):
    async with started(tmp_path / "data") as client:
        backup = await a_backup_then_a_project(client)
        assert "restoring" not in (await client.get("/api/health")).json()
        staging, release = hold(monkeypatch, backups_module, "_stage")
        restore = asyncio.create_task(client.post("/api/backups/restore", json={"generation": backup}))
        await asyncio.to_thread(staging.wait, 10)
        assert (await client.get("/api/health")).json().get("restoring") is True
        release.set()
        assert (await restore).status_code == 200
        assert "restoring" not in (await client.get("/api/health")).json()


# Before it takes its lock: the journal check runs in a worker thread first (backups.py:701).
async def test_health_says_a_restore_is_under_way_before_it_takes_its_lock(tmp_path, monkeypatch):
    async with started(tmp_path / "data") as client:
        backup = await a_backup_then_a_project(client)
        checking, release = hold(monkeypatch, backups_module, "_replay_pending")
        restore = asyncio.create_task(client.post("/api/backups/restore", json={"generation": backup}))
        await asyncio.to_thread(checking.wait, 10)
        assert not client.state["restore_lock"].locked()
        assert (await client.get("/api/health")).json().get("restoring") is True
        assert await names(client) == (200, {"General", "Made after the backup"})
        release.set()
        assert (await restore).status_code == 200
        assert "restoring" not in (await client.get("/api/health")).json()


# While it waits for a backup or an export that is being written (backups_lock): as long as that takes.
async def test_health_says_a_restore_is_under_way_while_it_waits_for_a_backup_being_written(tmp_path):
    async with started(tmp_path / "data") as client:
        backup = await a_backup_then_a_project(client)
        async with client.state["backups_lock"]:  # a full backup or an export holds it while it writes (backups.py:570)
            restore = asyncio.create_task(client.post("/api/backups/restore", json={"generation": backup}))
            while not client.state["restore_lock"].locked():
                await asyncio.sleep(0)
            assert not client.state.get("restoring")  # the gate refuses nothing yet
            assert await names(client) == (200, {"General", "Made after the backup"})
            assert (await client.get("/api/health")).json().get("restoring") is True
        assert (await restore).status_code == 200
        assert "restoring" not in (await client.get("/api/health")).json()


# During the swap, as before it: and a second restore asked meanwhile, refused, does not end what health says.
async def test_health_says_a_restore_is_under_way_during_the_swap_whatever_a_second_request_is_told(tmp_path, monkeypatch):
    async with started(tmp_path / "data") as client:
        backup = await a_backup_then_a_project(client)
        swapping, release = hold(monkeypatch, backups_module, "_put_in_place")
        restore = asyncio.create_task(client.post("/api/backups/restore", json={"generation": backup}))
        await asyncio.to_thread(swapping.wait, 10)
        second = await client.post("/api/backups/restore", json={"generation": backup})
        assert (second.status_code, second.json()["code"]) == (409, "restoring")
        assert (await client.get("/api/health")).json().get("restoring") is True
        assert (await names(client))[0] == 503
        release.set()
        assert (await restore).status_code == 200
        assert "restoring" not in (await client.get("/api/health")).json()


# A restore the engine refused has ended too.
async def test_health_no_longer_says_so_after_a_refused_restore(tmp_path):
    async with started(tmp_path / "data") as client:
        refused = await client.post("/api/backups/restore", json={"generation": "daily/20200101T000000000000Z"})
        assert refused.status_code == 404
        assert "restoring" not in (await client.get("/api/health")).json()
        assert (await names(client))[0] == 200
