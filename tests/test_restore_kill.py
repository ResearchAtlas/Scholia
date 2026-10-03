"""A process killed while a restore puts a backup in place, or puts the previous state back.

A child process runs the app on a data folder and restores an automatic backup, killing itself
with SIGKILL at the step under test. The test then starts the app on the same folder: the launch
finishes what the journal says, forward or back, so the database and the settings files are
one state again (all the backup's, or all as before), and nothing is left in staging.
"""

import asyncio
import subprocess
import sys
from pathlib import Path

import pytest

import backend.backups as backups_module
from network_guard import allow_subprocess
from scholia_app import started

pytestmark = pytest.mark.asyncio
ROOT = Path(__file__).resolve().parents[1]

CHILD = r"""
import asyncio, os, signal, sys, time
import httpx
import backend.backups as backups
from backend.app import create_app

data, scenario, generation = sys.argv[1], sys.argv[2], sys.argv[3]
replace = os.replace


def crash():
    os.kill(os.getpid(), signal.SIGKILL)
    while True:  # the signal is delivered asynchronously; never return to the next statement
        time.sleep(1)


def replacing(source, target, *args, **kwargs):
    source, target = str(source), str(target)
    if scenario == "forward" and target.endswith("/projects") and "/.staging/" in target:
        crash()  # the backup's database is in place; the live projects folder was about to go aside
    if scenario == "back" and "/replaced/" in source and source.endswith("/config.toml"):
        crash()  # putting the previous state back: the projects folder is back, config.toml not yet
    return replace(source, target, *args, **kwargs)


backups.os.replace = replacing
if scenario == "back":  # the restored database cannot be opened, so the previous state goes back
    def failing(*args, **kwargs):
        raise RuntimeError("the restored database cannot be opened")
    backups.Database = failing


class Keyring:
    def get_password(self, service, name):
        return None

    def set_password(self, service, name, value):
        pass

    def delete_password(self, service, name):
        pass


app = create_app(data, origin="http://127.0.0.1:8765", keyring_backend=Keyring())


async def main():
    inner = app.app
    async with inner.router.lifespan_context(inner):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://127.0.0.1:8765",
                                     headers={"X-Scholia-Client": "local"}) as client:
            await client.post("/api/backups/restore", json={"generation": generation})
    print("not killed", flush=True)


asyncio.run(main())
"""


async def prepare(data):
    """A backup holding project Kept and the instructions "Backed up", then project Later and new
    instructions after it. Returns the backup's id and both projects' ids."""
    async with started(data) as client:
        kept = (await client.post("/api/projects", json={"name": "Kept"})).json()["id"]
        await client.put("/api/instructions", json={"text": "Backed up"})
        backup = (await client.post("/api/backups")).json()["id"]
        later = (await client.post("/api/projects", json={"name": "Later"})).json()["id"]
        await client.put("/api/instructions", json={"text": "After the backup"})
    return backup, kept, later


def run_child(data, scenario, generation):
    with allow_subprocess(sys.executable):
        result = subprocess.run([sys.executable, "-c", CHILD, str(data), scenario, generation], cwd=ROOT,
                                capture_output=True, timeout=120)
    assert result.returncode == -9, result.stderr.decode()[-2000:]  # killed at the step under test


async def state_after_launch(data):
    async with started(data, setup=False) as client:
        names = {p["name"] for p in (await client.get("/api/projects")).json()["projects"]}
        text = (await client.get("/api/instructions")).json()["text"]
        folders = sorted(p.name for p in (data / "projects").iterdir())
        projects = {p["name"]: p["id"] for p in (await client.get("/api/projects")).json()["projects"]}
        assert (await client.post("/api/projects", json={"name": "Afterwards"})).status_code == 201  # it runs
    return names, text, folders, projects


async def test_a_crash_after_the_database_was_swapped_is_finished_forward_at_launch(tmp_path):
    data = tmp_path / "data"
    backup, kept, later = await prepare(data)
    await asyncio.to_thread(run_child, data, "forward", backup)
    assert (data / "backups" / backups_module.JOURNAL).exists()  # the crash left the swap half done

    names, text, folders, projects = await state_after_launch(data)
    assert names == {"General", "Kept"} and text == "Backed up"  # all the backup's: database and settings
    assert folders == [kept]
    assert not (data / "backups" / backups_module.JOURNAL).exists()
    assert not (data / "backups" / backups_module.STAGING).exists() or \
        list((data / "backups" / backups_module.STAGING).iterdir()) == []


async def test_a_crash_while_putting_the_previous_state_back_is_finished_back_at_launch(tmp_path):
    data = tmp_path / "data"
    backup, kept, later = await prepare(data)
    await asyncio.to_thread(run_child, data, "back", backup)
    assert (data / "backups" / backups_module.JOURNAL).exists()

    names, text, folders, projects = await state_after_launch(data)
    assert names == {"General", "Kept", "Later"} and text == "After the backup"  # all as before the restore
    assert sorted(folders) == sorted([kept, later])
    assert not (data / "backups" / backups_module.JOURNAL).exists()


async def test_a_restore_whose_undo_fails_starts_nothing_and_the_launch_puts_it_back(tmp_path, monkeypatch):
    import errno
    import json
    from backend.settings import write_private
    data = tmp_path / "data"
    backup, kept, later = await prepare(data)
    real_fsync, real_back, undone = backups_module._fsync, backups_module._back, []

    def failing_fsync(path):  # the swap fails once everything is in place
        if "/replaced" in str(path):
            raise OSError(errno.EIO, "I/O error")
        real_fsync(path)

    def failing_back(data_dir, journal):  # and putting it back fails partway, once
        undone.append(journal["direction"])
        if len(undone) == 1:
            write_private(data_dir / "backups" / backups_module.JOURNAL,
                          json.dumps({**journal, "direction": "back"}).encode())
            raise OSError(errno.EIO, "I/O error")
        real_back(data_dir, journal)

    async with started(data, setup=False) as client:
        monkeypatch.setattr(backups_module, "_fsync", failing_fsync)
        monkeypatch.setattr(backups_module, "_back", failing_back)
        response = await client.post("/api/backups/restore", json={"generation": backup})
        assert (response.status_code, response.json()["code"]) == (500, "restore_interrupted")
        projects = await client.get("/api/projects")  # nothing runs on the half-restored folder
        assert (projects.status_code, projects.json()["code"]) == (503, "database_unavailable")
        monkeypatch.setattr(backups_module, "_fsync", real_fsync)

    names, text, folders, projects = await state_after_launch(data)  # the launch finishes putting it back
    assert names == {"General", "Kept", "Later"} and text == "After the backup"
    assert sorted(folders) == sorted([kept, later])
    async with started(data, setup=False) as client:
        rows = await asyncio.to_thread(client.state["db"].read, lambda conn: conn.execute(
            "SELECT data FROM audit_log WHERE event = 'restore'").fetchall())
    assert [json.loads(row) for (row,) in rows] == [{"interrupted": True, "finished": "back"}]


async def test_putting_the_previous_state_back_twice_keeps_its_wal(tmp_path):
    from backend.db import DB_NAME
    data, staged, aside = tmp_path / "data", tmp_path / "data/backups/.staging/x/restore", \
        tmp_path / "data/backups/.staging/x/replaced"
    staged.mkdir(parents=True)
    (data / DB_NAME).write_bytes(b"the damaged database")
    (data / f"{DB_NAME}-wal").write_bytes(b"its committed work")
    (data / "config.toml").write_text("previous")
    (staged / DB_NAME).write_bytes(b"the backup's database")
    (staged / "config.toml").write_text("from the backup")
    backups_module._put_in_place(data, staged, aside)
    journal = {"direction": "back", "staged": "backups/.staging/x/restore", "aside": "backups/.staging/x/replaced",
               "live_database": True, "live": ["config.toml"], "backup": ["config.toml"]}
    backups_module._back(data, journal)
    backups_module._back(data, journal)  # a crash before the journal ended: the launch runs it again
    assert (data / DB_NAME).read_bytes() == b"the damaged database"
    assert (data / f"{DB_NAME}-wal").read_bytes() == b"its committed work"
    assert (data / "config.toml").read_text() == "previous"


async def test_a_journal_that_cannot_be_read_leaves_the_folder_unopened_and_offers_only_a_restore(tmp_path):
    from backend.db import DB_NAME
    data = tmp_path / "data"
    await prepare(data)
    (data / "backups" / backups_module.JOURNAL).write_text("{ not json")
    before = (data / DB_NAME).read_bytes()
    async with started(data, setup=False) as client:
        assert "restore a backup" in (await client.get("/api/health")).json()["database_damaged"]
        assert (await client.get("/api/projects")).json()["code"] == "database_damaged"
        assert (await client.get("/api/backups")).status_code == 200
    assert (data / DB_NAME).read_bytes() == before  # never opened
