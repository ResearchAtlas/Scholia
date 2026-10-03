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
        if scenario == "back":  # the restored database cannot be opened, so the previous state goes back
            def failing(*args, **kwargs):
                raise RuntimeError("the restored database cannot be opened")
            backups.Database = failing
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

    def failing_fsync(path):  # the swap fails once it has begun (the journal is written)
        if "/replaced" in str(path) and (data / "backups" / backups_module.JOURNAL).exists():
            raise OSError(errno.EIO, "I/O error")
        real_fsync(path)

    def failing_back(data_dir, journal, audit=False):  # and putting it back fails partway, once
        undone.append(journal["direction"])
        if len(undone) == 1:
            write_private(data_dir / "backups" / backups_module.JOURNAL,
                          json.dumps({**journal, "direction": "back"}).encode())
            raise OSError(errno.EIO, "I/O error")
        return real_back(data_dir, journal, audit)

    async with started(data, setup=False) as client:
        monkeypatch.setattr(backups_module, "_fsync", failing_fsync)
        monkeypatch.setattr(backups_module, "_back", failing_back)
        response = await client.post("/api/backups/restore", json={"generation": backup})
        assert (response.status_code, response.json()["code"]) == (500, "restore_interrupted")
        for method, path, body in (("GET", "/api/projects", None), ("PUT", "/api/instructions", {"text": "x"}),
                                   ("PUT", "/api/settings", {"hash": None, "updates": {"ui.language": "en"}})):
            refused = await client.request(method, path, json=body)  # nothing reads or writes the half-restored folder
            assert refused.json()["code"] == "restore_interrupted", path
        assert "restore" in (await client.get("/api/health")).json()["database_damaged"]
        monkeypatch.setattr(backups_module, "_fsync", real_fsync)

    names, text, folders, projects = await state_after_launch(data)  # the launch finishes putting it back
    assert names == {"General", "Kept", "Later"} and text == "After the backup"
    assert sorted(folders) == sorted([kept, later])
    async with started(data, setup=False) as client:
        rows = await asyncio.to_thread(client.state["db"].read, lambda conn: conn.execute(
            "SELECT data FROM audit_log WHERE event = 'restore'").fetchall())
    [record] = [json.loads(row) for (row,) in rows]
    assert (record["interrupted"], record["finished"]) == (True, "back") and record["id"]


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
        assert (await client.get("/api/projects")).json()["code"] == "restore_interrupted"
        assert (await client.get("/api/backups")).status_code == 200
    assert (data / DB_NAME).read_bytes() == before  # never opened


async def test_a_replayed_restore_whose_harness_cannot_start_is_put_back_from_its_journal(tmp_path, monkeypatch):
    from backend.runs import Harness
    data = tmp_path / "data"
    backup, kept, later = await prepare(data)
    await asyncio.to_thread(run_child, data, "forward", backup)
    real, recovered = Harness.recover, []

    async def failing_once(self):  # the replayed, restored database opens, but its harness cannot recover
        recovered.append(self)
        if len(recovered) == 1:
            raise RuntimeError("recovery failed")
        await real(self)

    monkeypatch.setattr(Harness, "recover", failing_once)
    names, text, folders, projects = await state_after_launch(data)
    assert names == {"General", "Kept", "Later"} and text == "After the backup"  # the previous state, running
    assert sorted(folders) == sorted([kept, later])
    assert not (data / "backups" / backups_module.JOURNAL).exists()


async def test_a_replay_that_can_be_neither_run_nor_undone_keeps_its_journal_and_staging(tmp_path, monkeypatch):
    data = tmp_path / "data"
    backup, kept, later = await prepare(data)
    await asyncio.to_thread(run_child, data, "forward", backup)

    def damaged(*args, **kwargs):
        from backend.db import DatabaseDamagedError
        raise DatabaseDamagedError("quick_check failed")

    def cannot_undo(data_dir, audit=False):
        raise OSError("I/O error")

    monkeypatch.setattr(backups_module, "Database", damaged)
    monkeypatch.setattr(backups_module, "_back_from_journal", cannot_undo)
    async with started(data, setup=False) as client:
        assert (await client.get("/api/projects")).json()["code"] == "restore_interrupted"
    assert (data / "backups" / backups_module.JOURNAL).exists()
    assert any((data / "backups" / backups_module.STAGING).iterdir())  # what the journal points at is kept


async def test_a_restore_syncs_what_its_journal_points_at_and_each_move(tmp_path, monkeypatch):
    import json
    import os as os_module
    data = tmp_path / "data"
    backup, kept, later = await prepare(data)
    events = []
    real_fsync, real_write, real_replace = backups_module._fsync, backups_module.write_private, os_module.replace

    def fsync(path):
        events.append(("fsync", Path(path)))
        real_fsync(path)

    def write(path, payload):
        path = Path(path)
        if path.name == backups_module.JOURNAL and json.loads(payload)["direction"] == "forward":
            staged = data / json.loads(payload)["staged"]
            events.append(("journal", {staged, *staged.rglob("*")}))
        real_write(path, payload)

    def replace(source, target, *args, **kwargs):
        events.append(("replace", Path(target)))
        return real_replace(source, target, *args, **kwargs)

    monkeypatch.setattr(backups_module, "_fsync", fsync)
    monkeypatch.setattr(backups_module, "write_private", write)
    monkeypatch.setattr(os_module, "replace", replace)
    async with started(data, setup=False) as client:
        assert (await client.post("/api/backups/restore", json={"generation": backup})).status_code == 200
    monkeypatch.setattr(os_module, "replace", real_replace)
    [at] = [i for i, (kind, _) in enumerate(events) if kind == "journal"]
    synced = {path for kind, path in events[:at] if kind == "fsync"}
    assert events[at][1] <= synced  # every staged file and folder was on disk before the journal named them
    staged = next(iter(sorted(events[at][1], key=lambda p: len(p.parts))))
    assert {staged.parent, staged.parent.parent, data / "backups", staged.parent / "replaced"} <= synced
    moves = [(i, path) for i, (kind, path) in enumerate(events)
             if kind == "replace" and i > at and path.is_relative_to(data) and path.name != backups_module.JOURNAL
             and not path.name.startswith(".")]
    assert len(moves) >= 4
    for i, target in moves:
        following = next((j for j in range(i + 1, len(events)) if events[j][0] == "replace"), len(events))
        assert ("fsync", target.parent) in events[i + 1:following], target  # synced before the next move


async def test_a_restore_is_the_way_out_of_a_journal_that_cannot_be_read(tmp_path):
    data = tmp_path / "data"
    backup, kept, later = await prepare(data)
    (data / "backups" / backups_module.JOURNAL).write_text("{ not json")
    async with started(data, setup=False) as client:
        assert (await client.get("/api/projects")).json()["code"] == "restore_interrupted"
        response = await client.post("/api/backups/restore", json={"generation": backup})
        assert response.status_code == 200, response.text
        assert response.json()["damaged_copy"]  # the folder it found is kept aside, never deleted
        assert {p["name"] for p in (await client.get("/api/projects")).json()["projects"]} == {"General", "Kept"}
        assert "database_damaged" not in (await client.get("/api/health")).json()
    assert not (data / "backups" / backups_module.JOURNAL).exists()


async def test_a_restore_works_after_a_failed_one_could_not_start_the_previous_database_again(tmp_path, monkeypatch):
    data = tmp_path / "data"
    backup, kept, later = await prepare(data)
    async with started(data, setup=False) as client:
        real, opened = backups_module.Database, []

        def failing_twice(*args, **kwargs):  # the restored database, then the previous one, cannot open
            opened.append(True)
            if len(opened) <= 2:
                raise RuntimeError("cannot open")
            return real(*args, **kwargs)

        monkeypatch.setattr(backups_module, "Database", failing_twice)
        response = await client.post("/api/backups/restore", json={"generation": backup})
        assert response.json()["code"] == "restore_failed"
        assert (await client.get("/api/projects")).json()["code"] == "database_unavailable"  # limited, and says so
        response = await client.post("/api/backups/restore", json={"generation": backup})
        assert response.status_code == 200, response.text  # the folder is treated as damaged: moved aside
        assert {p["name"] for p in (await client.get("/api/projects")).json()["projects"]} == {"General", "Kept"}


async def test_a_restore_done_whose_audit_row_cannot_be_written_is_reported_done_and_says_so(tmp_path, monkeypatch):
    import errno
    from backend.db import Database
    data = tmp_path / "data"
    backup, kept, later = await prepare(data)
    real_missing, real_write, full = backups_module._missing_files, Database.write, []

    def then_the_disk_fills(db):
        result = real_missing(db)
        full.append(True)  # the next write is the restore's audit row
        return result

    def write(self, fn):
        if full:
            full.clear()
            raise OSError(errno.ENOSPC, "No space left on device")
        return real_write(self, fn)

    async with started(data, setup=False) as client:
        monkeypatch.setattr(backups_module, "_missing_files", then_the_disk_fills)
        monkeypatch.setattr(Database, "write", write)
        response = await client.post("/api/backups/restore", json={"generation": backup})
        assert response.status_code == 200, response.text  # committed: never reported as failed
        assert response.json()["not_recorded"] == ["audit"]
        assert {p["name"] for p in (await client.get("/api/projects")).json()["projects"]} == {"General", "Kept"}
        assert (await client.post("/api/projects", json={"name": "After"})).status_code == 201  # it runs


async def test_a_restore_done_whose_missing_files_cannot_be_checked_is_reported_done(tmp_path, monkeypatch):
    data = tmp_path / "data"
    backup, kept, later = await prepare(data)

    def unreadable(db):
        raise OSError("I/O error")

    async with started(data, setup=False) as client:
        monkeypatch.setattr(backups_module, "_missing_files", unreadable)
        response = await client.post("/api/backups/restore", json={"generation": backup})
        assert response.status_code == 200, response.text
        assert (response.json()["not_recorded"], response.json()["missing_files"]) == (["missing_files"], None)
        [(record,)] = await asyncio.to_thread(client.state["db"].read, lambda conn: conn.execute(
            "SELECT data FROM audit_log WHERE event = 'restore'").fetchall())
        assert __import__("json").loads(record)["missing_files"] is None  # audited all the same


async def test_a_restore_done_whose_journal_cannot_be_ended_is_finished_harmlessly_at_launch(tmp_path, monkeypatch):
    data = tmp_path / "data"
    backup, kept, later = await prepare(data)
    real_end, failed = backups_module.end_journal, []

    def failing_once(data_dir):
        if not failed:
            failed.append(True)
            raise OSError("I/O error")
        real_end(data_dir)

    async with started(data, setup=False) as client:
        monkeypatch.setattr(backups_module, "end_journal", failing_once)
        response = await client.post("/api/backups/restore", json={"generation": backup})
        assert response.status_code == 200 and response.json()["not_recorded"] == ["journal"]
        assert (data / "backups" / backups_module.JOURNAL).exists()
        assert (await client.post("/api/projects", json={"name": "After"})).status_code == 201  # it runs
    monkeypatch.setattr(backups_module, "end_journal", real_end)

    names, text, folders, projects = await state_after_launch(data)  # replaying a finished swap changes nothing
    assert names == {"General", "Kept", "After"} and text == "Backed up"
    assert not (data / "backups" / backups_module.JOURNAL).exists()
    assert not (data / "backups" / backups_module.STAGING).exists() or \
        list((data / "backups" / backups_module.STAGING).iterdir()) == []


async def test_a_restore_done_whose_bookkeeping_fails_twice_lists_both_and_logs_no_paths(tmp_path, monkeypatch,
                                                                                      caplog):
    from backend.db import Database
    data = tmp_path / "data"
    backup, kept, later = await prepare(data)
    real_write, real_read, after_audit = Database.write, Database.read, []

    def unreadable(db):
        raise OSError(f"I/O error reading {data}")

    def write(self, fn):
        result = real_write(self, fn)
        if fn.__qualname__.startswith("_replace"):  # the restore's audit row: the schema read comes next
            after_audit.append(True)
        return result

    def read(self, fn):
        if after_audit:
            after_audit.clear()
            raise OSError(f"I/O error reading {data}")
        return real_read(self, fn)

    caplog.set_level("WARNING", logger=backups_module.log.name)
    async with started(data, setup=False) as client:
        monkeypatch.setattr(backups_module, "_missing_files", unreadable)
        monkeypatch.setattr(Database, "write", write)
        monkeypatch.setattr(Database, "read", read)
        response = await client.post("/api/backups/restore", json={"generation": backup})
        assert response.status_code == 200, response.text
        assert response.json()["not_recorded"] == ["missing_files", "schema_version"]
        assert response.json()["schema_version"] is None
    logged = [r.getMessage() for r in caplog.records if r.name == backups_module.log.name]
    assert any("missing_files" in m for m in logged) and any("schema_version" in m for m in logged)
    assert str(tmp_path) not in caplog.text  # what failed is logged, never where


def project_names(data):
    import sqlite3
    from backend.db import DB_NAME
    conn = sqlite3.connect((data / DB_NAME).as_uri() + "?mode=ro", uri=True)
    try:
        return {name for (name,) in conn.execute("SELECT name FROM projects")}
    finally:
        conn.close()


def restore_audits(client):
    import json
    return asyncio.to_thread(client.state["db"].read, lambda conn: [json.loads(data) for (data,) in conn.execute(
        "SELECT data FROM audit_log WHERE event = 'restore'")])


def failing_recovery(monkeypatch, times):
    from backend.runs import Harness
    real, recovered = Harness.recover, []

    async def recover(self):
        recovered.append(self)
        if len(recovered) <= times:
            raise RuntimeError("recovery failed")
        await real(self)

    monkeypatch.setattr(Harness, "recover", recover)
    return lambda: monkeypatch.setattr(Harness, "recover", real)


async def test_a_finished_restore_whose_journal_was_not_ended_is_never_put_back_over_later_work(tmp_path, monkeypatch):
    data = tmp_path / "data"
    backup, kept, later = await prepare(data)
    real_end, failed = backups_module.end_journal, []

    def failing_once(data_dir):
        if not failed:
            failed.append(True)
            raise OSError("I/O error")
        real_end(data_dir)

    async with started(data, setup=False) as client:
        monkeypatch.setattr(backups_module, "end_journal", failing_once)
        response = await client.post("/api/backups/restore", json={"generation": backup})
        assert response.status_code == 200 and response.json()["not_recorded"] == ["journal"]
        assert (await client.post("/api/projects", json={"name": "After"})).status_code == 201  # new work
    restore_recovery = failing_recovery(monkeypatch, 1)  # the next launch cannot recover its harness, once
    async with started(data, setup=False) as client:
        assert (await client.get("/api/projects")).json()["code"] == "database_unavailable"  # limited, offering restore
    assert project_names(data) == {"General", "Kept", "After"}  # never put back: the work since is in place
    restore_recovery()

    names, text, folders, projects = await state_after_launch(data)
    assert names == {"General", "Kept", "After"}
    assert not (data / "backups" / backups_module.JOURNAL).exists()
    async with started(data, setup=False) as client:
        assert len(await restore_audits(client)) == 1  # its audit row once, though the journal outlived it


async def test_a_replay_put_back_whose_previous_database_cannot_run_either_leaves_the_app_limited(tmp_path, monkeypatch):
    data = tmp_path / "data"
    backup, kept, later = await prepare(data)
    await asyncio.to_thread(run_child, data, "forward", backup)
    restore_recovery = failing_recovery(monkeypatch, 2)  # the restored database, then the previous one
    async with started(data, setup=False) as client:
        assert (await client.get("/api/projects")).json()["code"] == "database_unavailable"
        restore_recovery()
        response = await client.post("/api/backups/restore", json={"generation": backup})
        assert response.status_code == 200, response.text  # restore stays the way out
        assert {p["name"] for p in (await client.get("/api/projects")).json()["projects"]} == {"General", "Kept"}


async def test_a_replayed_restore_whose_audit_row_cannot_be_written_keeps_its_journal_to_write_it(tmp_path, monkeypatch):
    from backend.db import Database
    data = tmp_path / "data"
    backup, kept, later = await prepare(data)
    await asyncio.to_thread(run_child, data, "forward", backup)
    real_write, failed = Database.write, []

    def write(self, fn):
        if fn.__module__ == "backend.backups" and not failed:  # the replay's audit row, once
            failed.append(True)
            raise OSError("I/O error")
        return real_write(self, fn)

    monkeypatch.setattr(Database, "write", write)
    async with started(data, setup=False) as client:
        assert {p["name"] for p in (await client.get("/api/projects")).json()["projects"]} == {"General", "Kept"}
        assert await restore_audits(client) == []
    assert failed and (data / "backups" / backups_module.JOURNAL).exists()  # the evidence is kept
    async with started(data, setup=False) as client:
        [record] = await restore_audits(client)
        assert (record["interrupted"], record["finished"]) == (True, "forward")
    assert not (data / "backups" / backups_module.JOURNAL).exists()
