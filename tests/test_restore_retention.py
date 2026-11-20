"""No automatic backup is rotated while a restore is in progress (design review R, Option 1, rider T; PR 63's first
review): a backup a restore needs is where it was, however the restore ends; and a journal a second restore replaces
is settled. Synthetic data; every wait is an event."""

import asyncio
import threading
import zipfile
from datetime import UTC, datetime, timedelta

import pytest

import backend.backups as backups_module
from backend.db import Database
from backend.db.migrations import MIGRATIONS
from backend.runs import Harness
from scholia_app import started
from test_backups import hold

pytestmark = pytest.mark.asyncio


def folder(data, source):
    """Phase A's folder: the backup restored is the one the next backup after the launch's rotates out."""
    now = datetime.now(UTC)
    with Database(data) as db:
        weekly = [db.backup(now=now - timedelta(days=days)) for days in (60, 53, 46, 39)]
        (data / "backups" / "weekly").mkdir()
        for generation in weekly:
            generation.rename(data / "backups" / "weekly" / generation.name)
        daily = {days: db.backup(now=now - timedelta(days=days)).name
                 for days in (30, 22 if source == "weekly" else 26, 14, 13, 12, 11, 10)}
    return f"weekly/{weekly[1].name}" if source == "weekly" else f"daily/{daily[26]}"


async def listed(client):
    return {b["id"]: b for b in (await client.get("/api/backups")).json()["backups"]}


def raising(*args, **kwargs):
    raise OSError("synthetic")


@pytest.mark.parametrize("failure", ["safety copy", "after the swap"])
@pytest.mark.parametrize("source", ["weekly", "daily"])
async def test_a_backup_admitted_while_a_restore_stages_leaves_the_backup_it_restores(tmp_path, monkeypatch, source,
                                                                                      failure):
    # S1: Back up now is admitted while the restore stages (it waits for backups_lock), runs when staging lets the lock
    # go, before the restore has every request out, and its rotation takes the backup being restored; the restore fails
    data = tmp_path / "data"
    restored = await asyncio.to_thread(folder, data, source)
    async with started(data) as client:
        assert restored in await listed(client)
        lock, waiting = client.state["backups_lock"], asyncio.Event()
        acquire = lock.acquire

        async def announced():
            if lock.locked():
                waiting.set()
            return await acquire()
        monkeypatch.setattr(lock, "acquire", announced)
        staging, release = hold(monkeypatch, backups_module, "_stage")
        restore = asyncio.create_task(client.post("/api/backups/restore", json={"generation": restored}))
        assert await asyncio.to_thread(staging.wait, 10)
        backup = asyncio.create_task(client.post("/api/backups"))
        await asyncio.wait_for(waiting.wait(), 10)  # admitted, and waiting for the staging to end
        if failure == "safety copy":
            db, calls = client.state["db"], []
            real = db.backup

            def second_fails(*args, **kwargs):  # Back up now's, then the safety copy
                calls.append(args)
                if len(calls) == 2:
                    raise OSError("synthetic")
                return real(*args, **kwargs)
            monkeypatch.setattr(db, "backup", second_fails)
        else:
            monkeypatch.setattr(backups_module, "_commit_journal", raising)
        release.set()
        assert (await backup).status_code == 200
        response = await restore
        assert (response.status_code, response.json()["code"]) == (
            500, "safety_copy_failed" if failure == "safety copy" else "restore_failed")
        assert restored in await listed(client)  # "nothing was changed"


@pytest.mark.parametrize("source", ["weekly", "daily"])
async def test_an_idle_backup_due_as_a_restore_stops_the_work_leaves_the_backup_it_restores(tmp_path, monkeypatch,
                                                                                           source):
    # S2 (PR 63's first review, pass 2, finding 1): the day's backup comes due at an idle check between the staging and
    # the safety copy, while the restore waits for the work to stop; the safety copy then fails
    data = tmp_path / "data"
    restored = await asyncio.to_thread(folder, data, source)
    monkeypatch.setattr(backups_module, "IDLE_CHECK_SECONDS", 0.01)
    async with started(data) as client:
        db, armed, ran = client.state["db"], threading.Event(), threading.Event()
        due, shutdown = db.backup_if_due, Harness.shutdown

        def the_next_days(now=None):  # nothing until the restore stops the work; then the next day's backup, once
            if not armed.is_set() or ran.is_set():
                return None
            try:
                return due(datetime.now(UTC) + timedelta(days=1, minutes=1))
            finally:
                ran.set()

        async def stopping(self, *args, **kwargs):
            result = await shutdown(self, *args, **kwargs)
            armed.set()
            assert await asyncio.to_thread(ran.wait, 10)  # the idle check took its backup meanwhile
            return result
        monkeypatch.setattr(db, "backup_if_due", the_next_days)
        monkeypatch.setattr(db, "backup", raising)  # the safety copy
        monkeypatch.setattr(Harness, "shutdown", stopping)
        assert restored in await listed(client)
        response = await client.post("/api/backups/restore", json={"generation": restored})
        monkeypatch.setattr(Harness, "shutdown", shutdown)
        assert (response.status_code, response.json()["code"]) == (500, "safety_copy_failed")
        assert ran.is_set() and restored in await listed(client)


def older(data, file):
    """An M1 data folder (schema 3) and its backup as a file."""
    with Database(data, migrations=MIGRATIONS[:3]) as db:
        generation = db.backup()
    with zipfile.ZipFile(file, "w") as archive:
        for path in generation.iterdir():
            archive.write(path, path.name)


@pytest.mark.parametrize("second", ["committed", "put back"])
async def test_a_restore_after_one_that_could_not_be_put_back_leaves_no_backup_the_first_added(tmp_path, monkeypatch,
                                                                                              second):
    # S3 (PR 63's first review, pass 1, finding 8): a restore from an older-schema file fails and cannot be put back;
    # a second restore replaces its journal, and the copy the first one's migrations took (the file's records) stays
    data = tmp_path / "data"
    await asyncio.to_thread(older, data, tmp_path / "m1.zip")
    committing, back, fail, backs = backups_module._commit_journal, backups_module._back_from_journal, [True], []

    def commit(*args):
        if fail[0]:
            raise OSError("the journal cannot be written")
        return committing(*args)

    def back_but_the_first(*args):
        backs.append(args)
        if len(backs) == 1:
            raise OSError("the files cannot be put back now")
        return back(*args)
    monkeypatch.setattr(backups_module, "_commit_journal", commit)
    monkeypatch.setattr(backups_module, "_back_from_journal", back_but_the_first)
    async with started(data) as client:
        before = set(await listed(client))
        response = await client.post("/api/backups/restore", json={"file": str(tmp_path / "m1.zip")})
        assert (response.status_code, response.json()["code"]) == (500, "restore_interrupted")
        first = await listed(client)
        assert sorted(first[i]["schema_version"] for i in set(first) - before) == [3, len(MIGRATIONS)]
        [safety] = [i for i in set(first) - before if first[i]["schema_version"] == len(MIGRATIONS)]
        fail[0] = second != "committed"
        response = await client.post("/api/backups/restore", json={"generation": safety})
        assert response.status_code == (200 if second == "committed" else 500), response.text
        after = await listed(client)
    assert sorted(after[i]["schema_version"] for i in set(after) - before) == [len(MIGRATIONS)]  # its safety copy


@pytest.mark.parametrize("rotation", ["promoted", "deleted"])
async def test_a_restore_put_back_leaves_an_older_schema_backup_next_in_line_to_rotate_where_it_was(
        tmp_path, monkeypatch, rotation):
    # S4: the restored database's own backup before its migrations rotates the backup being restored (to weekly, or
    # away, its copy then standing in for it); the restore is put back
    data = tmp_path / "data"
    now = datetime.now(UTC)

    def week():
        with Database(data, migrations=MIGRATIONS[:3]) as db:
            if rotation == "deleted":
                db.backup(now=now - timedelta(days=10))
            name = db.backup(now=now - timedelta(days=8)).name
            for days in range(5, 0, -1):
                db.backup(now=now - timedelta(days=days))
            return name

    name = await asyncio.to_thread(week)
    async with started(data) as client:
        before = set(await listed(client))
        monkeypatch.setattr(backups_module, "_commit_journal", raising)
        response = await client.post("/api/backups/restore", json={"generation": f"daily/{name}"})
        assert (response.status_code, response.json()["code"]) == (500, "restore_failed")
        after = await listed(client)
    assert before <= set(after)  # every backup where it was
    assert sorted(after[i]["schema_version"] for i in set(after) - before) == [len(MIGRATIONS)]  # and the safety copy



async def test_a_second_restore_put_back_after_one_that_could_not_be_leaves_the_first_to_finish(tmp_path, monkeypatch):
    # Design review R's observation (older than PR 63): the first restore cannot be put back; a second fails after its
    # swap and is put back, and its put back ended the journal, so the next launch came up on the first file's half
    # state with no journal and no restore audit row. The first one's journal is now kept, and the launch finishes it.
    data = tmp_path / "data"
    await asyncio.to_thread(older, data, tmp_path / "m1.zip")
    committing, back, backs = backups_module._commit_journal, backups_module._back_from_journal, []

    def commit(data_dir, journal, audits):  # the restores' commits fail; a launch's put back or replay commits
        if not any(audit.get("interrupted") for audit in audits):
            raise OSError("the journal cannot be written")
        return committing(data_dir, journal, audits)

    def back_but_the_first(*args):
        backs.append(args)
        if len(backs) == 1:
            raise OSError("the files cannot be put back now")
        return back(*args)
    monkeypatch.setattr(backups_module, "_commit_journal", commit)
    monkeypatch.setattr(backups_module, "_back_from_journal", back_but_the_first)
    async with started(data) as client:
        first = await client.post("/api/backups/restore", json={"file": str(tmp_path / "m1.zip")})
        assert (first.status_code, first.json()["code"]) == (500, "restore_interrupted")
        backups = await listed(client)
        [safety] = [i for i in backups if backups[i]["schema_version"] == len(MIGRATIONS)]
        second = await client.post("/api/backups/restore", json={"generation": safety})
        assert (second.status_code, second.json()["code"]) == (500, "restore_failed")
        assert backups_module._read_journal(data) is not None  # the first one's, still to finish
    async with started(data, setup=False) as client:  # the launch finishes the first restore, as its answer said
        assert backups_module._read_journal(data) is None
        rows = await asyncio.to_thread(client.state["db"].read, lambda conn: conn.execute(
            "SELECT json_extract(data, '$.interrupted'), json_extract(data, '$.finished') FROM audit_log"
            " WHERE event = 'restore'").fetchall())
        assert rows == [(1, "forward")]
