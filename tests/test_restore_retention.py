"""No automatic backup is rotated while a restore is in progress (design review R, Option 1, rider T; PR 63's first
review): a backup a restore needs is where it was, however the restore ends; and a journal a second restore replaces
is settled. Synthetic data; every wait is an event."""

import asyncio
import errno
import json
import os
import threading
import zipfile
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

import backend.backups as backups_module
import backend.db.database as database_module
from backend.db import DB_NAME, Database
from backend.db.migrations import MIGRATIONS
from backend.runs import Harness
from scholia_app import started
from test_backups import hold
from test_restore_kill import restore_audits

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


@pytest.mark.parametrize("error", ["EACCES", "EIO"])
async def test_a_staging_folder_that_cannot_be_listed_rotates_nothing(tmp_path, monkeypatch, caplog, error):
    # PR 63's recheck r2 (pass 1 F5, pass 2 F1): the guard asked Path.glob, which swallows a listing's error, so an
    # unlistable staging folder read as no restore and retention deleted; design R says nothing is rotated then
    import errno
    import logging
    import os
    import backend.db.database as database_module
    from backend.db.database import RESTORING, STAGING

    def body():
        now = datetime.now(UTC)
        data = tmp_path / "data"
        with Database(data) as db:
            for days in (60, 53, 46, 39, 30):  # one weekly backup more than retention keeps once the next one rotates
                db.backup(now=now - timedelta(days=days))
            backups = data / "backups"
            (backups / "weekly").mkdir(exist_ok=True)
            for generation in sorted((backups / "daily").iterdir()):
                generation.rename(backups / "weekly" / generation.name)
            (backups / STAGING / "a-restore" / RESTORING).mkdir(parents=True)
            before = sorted(p.name for p in (backups / "weekly").iterdir())
            real = os.scandir

            def failing(path="."):
                if not isinstance(path, int) and os.path.basename(os.fspath(path)) in (STAGING, "a-restore"):
                    raise OSError(getattr(errno, error), os.strerror(getattr(errno, error)))
                return real(path)
            monkeypatch.setattr(database_module.os, "scandir", failing)
            import glob  # Path.glob's own scanner, bound at its import, fails the same way
            for owner in [o for o in vars(glob).values() if isinstance(o, type) and "scandir" in vars(o)]:
                monkeypatch.setattr(owner, "scandir", staticmethod(failing))
            with caplog.at_level(logging.WARNING, logger="backend.db.database"):
                db._apply_retention(now=now)
            monkeypatch.undo()
            assert sorted(p.name for p in (backups / "weekly").iterdir()) == before  # nothing rotated
            assert "retention failed" in caplog.text  # and said, as any retention failure is

    await asyncio.to_thread(body)


@pytest.mark.parametrize("launch", ["finishes", "puts back"])
@pytest.mark.parametrize("failure", ["before its journal", "after its swap"])
@pytest.mark.parametrize("second_source", ["migration copy", "safety copy"])
@pytest.mark.parametrize("first_source", ["file", "automatic"])
async def test_a_restore_after_one_that_could_not_be_put_back_keeps_the_backup_it_restores(
        tmp_path, monkeypatch, first_source, second_source, failure, launch):
    # PR 63's recheck r2 (pass 1 F4, pass 2 F2): rider T removed what the unfinished restore added, the backup the second
    # restore restores from included (the first one's backup before its migrations). That one is kept (the
    # coordinator's amendment to T), as an ordinary backup; the safety copy is the control. Design review Y's boundary:
    # a second restore that fails before its journal is written has changed nothing (the two shapes it flips, below)
    data = tmp_path / "data"
    await asyncio.to_thread(older, data, tmp_path / "m1.zip")
    automatic = sorted((data / "backups" / "daily").iterdir())[0].name  # the schema 3 backup the M1 folder took
    committing, back, sync = backups_module._commit_journal, backups_module._back_from_journal, backups_module._sync_tree
    fail, backs, syncs = [True], [], []

    def commit(*args):
        if fail[0]:
            raise OSError("the journal cannot be written")
        return committing(*args)

    def back_but_the_first(*args):
        backs.append(args)
        if len(backs) == 1:
            raise OSError("the files cannot be put back now")
        return back(*args)

    def sync_but_the_second(*args):  # the second restore fails before its journal is written, so before rider T runs
        syncs.append(args)
        if len(syncs) == 2 and failure == "before its journal":
            raise OSError("the staged files cannot be synced")
        return sync(*args)
    monkeypatch.setattr(backups_module, "_commit_journal", commit)
    monkeypatch.setattr(backups_module, "_back_from_journal", back_but_the_first)
    monkeypatch.setattr(backups_module, "_sync_tree", sync_but_the_second)
    async with started(data) as client:
        before = set(await listed(client))
        body = {"file": str(tmp_path / "m1.zip")} if first_source == "file" else {"generation": f"daily/{automatic}"}
        response = await client.post("/api/backups/restore", json=body)
        assert (response.status_code, response.json()["code"]) == (500, "restore_interrupted")
        first = await listed(client)
        added = {first[i]["schema_version"]: i for i in set(first) - before}
        assert sorted(added) == [3, len(MIGRATIONS)]
        source = added[3 if second_source == "migration copy" else len(MIGRATIONS)]
        response = await client.post("/api/backups/restore", json={"generation": source})
        assert response.status_code == 500, response.text
        assert source in await listed(client)  # the backup it restores, where it was
    fail[0] = launch == "puts back"  # the launch's replay forward commits, or fails and puts the first restore back
    async with started(data, setup=False) as client:  # the launch finishes or undoes what is left: it is kept there too,
        # except at design Y's boundary: a restore that fails before its journal is written has changed nothing, so the
        # first restore still owns its backup before its migrations, and its put back removes it with what else it added
        boundary = second_source == "migration copy" and failure == "before its journal" and launch == "puts back"
        assert (source in await listed(client)) is not boundary


async def test_a_staging_entry_that_links_to_a_restoring_folder_is_a_restore(tmp_path):
    # The phase C internal review (F2): the guard read a linked staging entry as no restore, where Path.glob followed it;
    # in the keeping direction, it is one
    from backend.db.database import RESTORING, STAGING, _restoring
    elsewhere = tmp_path / "elsewhere"
    (elsewhere / RESTORING).mkdir(parents=True)
    (tmp_path / "backups" / STAGING).mkdir(parents=True)
    (tmp_path / "backups" / STAGING / "linked").symlink_to(elsewhere, target_is_directory=True)
    assert _restoring(tmp_path / "backups")


@pytest.mark.parametrize("removable", [False, True])
async def test_a_launch_that_cannot_empty_a_restores_staging_says_retention_is_paused(tmp_path, caplog, removable):
    # PR 63's Fable recheck (F2): design R's limit (a staging folder that cannot be removed pauses retention) held with
    # nothing logged; the launch now says so once, naming no path or file. A removable one is the control
    import logging
    from backend.db.database import RESTORING, STAGING
    if not removable and os.geteuid() == 0:
        pytest.skip("a folder's mode keeps nothing from root")
    data = tmp_path / "data"
    await asyncio.to_thread(lambda: Database(data).close())
    left = data / "backups" / STAGING / "a-restore-id"
    (left / RESTORING).mkdir(parents=True)
    (left / RESTORING / "a-staged-file.db").write_bytes(b"x")
    if not removable:
        left.chmod(0o500)  # its restore folder cannot be unlinked from it
    try:
        with caplog.at_level(logging.WARNING, logger="backend.backups"):
            async with started(data, setup=False):
                pass
    finally:
        if left.exists():
            left.chmod(0o700)
    said = [r for r in caplog.records if r.name == "backend.backups" and r.levelno == logging.WARNING]
    if removable:
        assert not said and not (data / "backups" / STAGING).exists()
        return
    assert len(said) == 1 and "retention" in said[0].getMessage() and "paused" in said[0].getMessage()
    text = said[0].getMessage() + repr(said[0].args)
    assert not any(name in text for name in ("a-restore-id", "a-staged-file", STAGING, str(tmp_path)))


# Design review Y (PR 63's recheck r3 finding 2; the Fable recheck's findings 1 and 4): one list of the backups to keep,
# carried by every journal; a restore that replaces an unfinished one changes nothing before its own journal is written.
# Its ten cases (its appendix B), each fault armed by a flag before the request that meets it.
LATEST = len(MIGRATIONS)


class Faults:
    """The faults a scenario arms. commit: the restores' own commits and a launch's forward commit fail, a launch's put
    back commits. back: what the next call of _back_from_journal does ("entry": fails before anything is written;
    "midway": fails at its first move, after direction back is written; None: works)."""

    def __init__(self, monkeypatch):
        self.stop_at_8 = self.sync = self.journal_full = self._moving = False
        self.back = None
        real = {name: getattr(backups_module, name) for name in (
            "_commit_journal", "_back_from_journal", "_sync_tree", "_move", "write_private")}
        usable = database_module._usable_state

        def usable_state(conn, latest):  # the restored database's ninth migration cannot run
            found = usable(conn, latest)
            if self.stop_at_8 and found[0] == 8:
                raise OSError("the ninth migration cannot run")
            return found

        def commit(data_dir, journal, audits):
            if not audits or audits[-1].get("finished") != "back":  # its own row is the last; earlier ones are carried
                raise OSError("the journal cannot be written")
            return real["_commit_journal"](data_dir, journal, audits)

        def back(*args):
            how, self.back = self.back, None
            if how == "entry":
                raise OSError("the files cannot be put back now")
            self._moving = how == "midway"
            try:
                return real["_back_from_journal"](*args)
            finally:
                self._moving = False

        def move(*args):
            if self._moving:
                raise OSError("a file cannot be moved back now")
            return real["_move"](*args)

        def sync(*args):
            if self.sync:
                raise OSError("the staged files cannot be synced")
            return real["_sync_tree"](*args)

        def write(path, data):
            if self.journal_full and Path(path).name == backups_module.JOURNAL:
                raise OSError(errno.ENOSPC, os.strerror(errno.ENOSPC))
            return real["write_private"](path, data)

        monkeypatch.setattr(database_module, "_usable_state", usable_state)
        for name, stand_in in (("_commit_journal", commit), ("_back_from_journal", back), ("_sync_tree", sync),
                               ("_move", move), ("write_private", write)):
            monkeypatch.setattr(backups_module, name, stand_in)


async def restore(client, expected, **body):
    response = await client.post("/api/backups/restore", json=body)
    assert (response.status_code, response.json()["code"]) == expected, response.text


async def added(client, before):
    after = await listed(client)
    return {i: after[i]["schema_version"] for i in set(after) - before}


async def first_restore(client, tmp_path, faults, migrating, back="entry"):
    """A project made, then an older-schema file restored: it fails (while its database migrates, at schema 8, or at
    its commit, migrated) and cannot be put back. Returns (the backups before it, its safety copy, its backup before
    its migrations)."""
    assert (await client.post("/api/projects", json={"name": "Before"})).status_code == 201
    before = set(await listed(client))
    faults.stop_at_8, faults.back = migrating, back
    await restore(client, (500, "restore_interrupted"), file=str(tmp_path / "m1.zip"))
    faults.stop_at_8 = False
    took = await added(client, before)
    assert sorted(took.values()) == [3, LATEST]
    return before, *(next(i for i in took if took[i] == schema) for schema in (LATEST, 3))


@pytest.mark.parametrize("second", ["before its journal", "after its swap"])
async def test_a_restore_put_back_at_a_launch_after_a_second_one_was_tried_leaves_no_backup_it_added(
        tmp_path, monkeypatch, second):
    # Y1 (the Fable recheck's finding 1, its probe T1): the first restore fails while its database migrates and cannot
    # be put back; a second restore fails; the launch replays the first, whose database takes a backup before its
    # remaining migrations, cannot commit it and puts the previous state back
    data = tmp_path / "data"
    await asyncio.to_thread(older, data, tmp_path / "m1.zip")
    faults = Faults(monkeypatch)
    async with started(data) as client:
        before, safety, _ = await first_restore(client, tmp_path, faults, migrating=True)
        faults.sync = second == "before its journal"
        await restore(client, (500, "restore_failed"), generation=safety)
        faults.sync = False
    async with started(data, setup=False) as client:
        assert "Before" in {p["name"] for p in (await client.get("/api/projects")).json()["projects"]}
        assert sorted((await added(client, before)).values()) == [LATEST]  # its safety copy; the head: [8, LATEST]
        assert backups_module._read_journal(data) is None


@pytest.mark.parametrize("source", ["safety copy", "file"])
async def test_a_second_restore_that_cannot_write_its_journal_changes_nothing(tmp_path, monkeypatch, source):
    # Y2 (recheck r3's finding, its write failure): the second restore's first write of the journal fails (ENOSPC). It
    # answers "nothing was changed": the backups listed and the journal are as it found them
    data = tmp_path / "data"
    await asyncio.to_thread(older, data, tmp_path / "m1.zip")
    faults = Faults(monkeypatch)
    async with started(data) as client:
        _, safety, _ = await first_restore(client, tmp_path, faults, migrating=False)
        found = set(await listed(client)), (data / "backups" / backups_module.JOURNAL).read_bytes()
        faults.journal_full = True
        await restore(client, (500, "restore_failed"),
                      **({"generation": safety} if source == "safety copy" else {"file": str(tmp_path / "m1.zip")}))
        faults.journal_full = False
        assert (set(await listed(client)), (data / "backups" / backups_module.JOURNAL).read_bytes()) == found


@pytest.mark.parametrize("launch", ["finishes", "puts back"])
async def test_a_restore_that_fails_before_its_journal_is_written_leaves_its_source_to_the_first(tmp_path, monkeypatch,
                                                                                                  launch):
    # Design Y's boundary, for Harold's word (r3's literal case): the second restore chose the backup the first one's migrations took
    # and failed before any journal of its own was written. The first restore still owns that backup: finished at the
    # launch, it stays (its backup before its migrations); put back, it goes with what else the first one added
    data = tmp_path / "data"
    await asyncio.to_thread(older, data, tmp_path / "m1.zip")
    faults = Faults(monkeypatch)
    async with started(data) as client:
        before, _, copy = await first_restore(client, tmp_path, faults, migrating=False)
        faults.journal_full = True
        await restore(client, (500, "restore_failed"), generation=copy)
        faults.journal_full = False
        assert copy in await listed(client)
        if launch == "finishes":
            monkeypatch.setattr(backups_module, "_commit_journal", lambda data_dir, journal, audits: (
                backups_module.write_private(data_dir / "backups" / backups_module.JOURNAL, json.dumps(
                    {**journal, "direction": "done", "audits": audits}).encode())))
    async with started(data, setup=False) as client:
        assert sorted((await added(client, before)).values()) == ([3, LATEST] if launch == "finishes" else [LATEST])


async def test_three_restores_each_from_the_backup_the_one_before_took_keep_those_backups(tmp_path, monkeypatch):
    # Y3 (the case a per-journal list creates): the second restore restores from the first one's backup before its
    # migrations, the third from the second one's; none can commit; the third is put back at once, the second at the
    # next launch, the first at the one after. The backups the second and the third restored from stay
    data = tmp_path / "data"
    await asyncio.to_thread(older, data, tmp_path / "m1.zip")
    faults = Faults(monkeypatch)
    async with started(data) as client:
        before, safety, first_copy = await first_restore(client, tmp_path, faults, migrating=False)
        faults.back = "entry"
        await restore(client, (500, "restore_interrupted"), generation=first_copy)
        took = await added(client, before)
        [second_copy] = [i for i in took if took[i] == 3 and i != first_copy]
        await restore(client, (500, "restore_failed"), generation=second_copy)
        assert {first_copy, second_copy, safety} == set(await added(client, before))
    async with started(data, setup=False) as client:  # the second is replayed, cannot commit and is put back
        assert (await client.get("/api/projects")).json()["code"] == "restore_interrupted"
        assert {first_copy, second_copy, safety} == set(await added(client, before))
    async with started(data, setup=False) as client:  # the first is replayed, cannot commit and is put back
        assert "Before" in {p["name"] for p in (await client.get("/api/projects")).json()["projects"]}
        assert {first_copy, second_copy, safety} == set(await added(client, before))
        assert backups_module._read_journal(data) is None


async def test_a_second_restore_whose_put_back_a_launch_finishes_has_the_first_one_finished_too(tmp_path, monkeypatch):
    # F4, backups.py:1294-1295 (the Fable recheck's probe T2): both put backs are interrupted after their direction
    # is written. The launch finishes the second one's, which writes the first one's journal back, and then that one
    # too: the previous state, both audit rows, no journal. Without the two lines it runs on the first file's database
    data = tmp_path / "data"
    await asyncio.to_thread(older, data, tmp_path / "m1.zip")
    faults = Faults(monkeypatch)
    async with started(data) as client:
        before, safety, _ = await first_restore(client, tmp_path, faults, migrating=True, back="midway")
        faults.back = "midway"
        await restore(client, (500, "restore_interrupted"), generation=safety)
        assert backups_module._read_journal(data)["direction"] == "back"
    async with started(data, setup=False) as client:
        assert "Before" in {p["name"] for p in (await client.get("/api/projects")).json()["projects"]}
        assert [(a["interrupted"], a["finished"]) for a in await restore_audits(client)] == [(True, "back")] * 2
        assert sorted((await added(client, before)).values()) == [LATEST]
        assert backups_module._read_journal(data) is None


async def test_a_replay_whose_put_back_leaves_an_earlier_restore_keeps_the_app_limited_until_that_one_ends(
        tmp_path, monkeypatch):
    # F4, backups.py:1342-1345 (the Fable recheck's probe T3), and Y1 through the journal written back: the second
    # restore is left to replay; the launch's replay cannot commit and its put back writes the first one's journal
    # back, so the app stays limited; the next launch replays the first (its database takes a backup before its
    # remaining migrations), cannot commit and puts the previous state back, removing what the first one added
    data = tmp_path / "data"
    await asyncio.to_thread(older, data, tmp_path / "m1.zip")
    faults = Faults(monkeypatch)
    async with started(data) as client:
        before, safety, _ = await first_restore(client, tmp_path, faults, migrating=True)
        first = backups_module._read_journal(data)["id"]
        faults.back = "entry"
        await restore(client, (500, "restore_interrupted"), generation=safety)
        assert backups_module._read_journal(data)["replaced"]["id"] == first
    async with started(data, setup=False) as client:
        assert (await client.get("/api/projects")).json()["code"] == "restore_interrupted"  # not the half state
        assert backups_module._read_journal(data)["id"] == first
    async with started(data, setup=False) as client:
        assert "Before" in {p["name"] for p in (await client.get("/api/projects")).json()["projects"]}
        assert [(a["interrupted"], a["finished"]) for a in await restore_audits(client)] == [(True, "back")] * 2
        assert sorted((await added(client, before)).values()) == [LATEST]  # the head: [8, LATEST]
        assert backups_module._read_journal(data) is None


async def test_a_crash_after_the_replacing_journal_is_written_removes_at_the_replay_what_the_replaced_restore_added(
        tmp_path, monkeypatch):
    # Option 1's own step: rider T runs from the written journal, in _forward, before the backup's database is in
    # place; a crash between the write and the removal is finished by the replay, and a later replay removes nothing
    # the replacing restore took itself
    data = tmp_path / "data"
    daily = data / "backups" / "daily"
    kept, first_added, second_added = "20261101T000000000000Z", "20261102T000000000000Z", "20261103T000000000000Z"
    for name in (kept, first_added):
        (daily / name).mkdir(parents=True)
    (data / DB_NAME).write_bytes(b"the previous database")

    def staged(name, content):
        folder = data / "backups" / ".staging" / name / "restore"
        folder.mkdir(parents=True)
        (folder / DB_NAME).write_bytes(content)
        return folder

    backups_module._put_in_place(data, staged("a", b"the first backup"), data / "backups/.staging/a/replaced", "a",
                                 listed={kept})
    forward = backups_module._forward

    def crash(*args):
        raise KeyboardInterrupt
    monkeypatch.setattr(backups_module, "_forward", crash)
    with pytest.raises(KeyboardInterrupt):
        backups_module._put_in_place(data, staged("b", b"the second backup"), data / "backups/damaged/s", "b",
                                     listed={kept, first_added})
    monkeypatch.setattr(backups_module, "_forward", forward)
    journal = backups_module._read_journal(data)
    assert (journal["id"], journal["replaced"]["id"], journal["listed"]) == ("b", "a", [kept])
    assert (daily / first_added).is_dir()  # the crash came before the removal
    backups_module.finish_interrupted_restore(data)
    assert (data / DB_NAME).read_bytes() == b"the second backup" and not (daily / first_added).exists()
    (daily / second_added).mkdir()  # the second one's own backup before its migrations
    backups_module.finish_interrupted_restore(data)  # replayed again: its own backup stays
    assert (daily / second_added).is_dir()
    earlier = backups_module._back(data, backups_module._read_journal(data))  # put back: it goes, the first one's is back
    assert not (daily / second_added).exists() and (daily / kept).is_dir()
    assert (earlier["id"], earlier["listed"], backups_module._read_journal(data)) == ("a", [kept], earlier)
    assert (data / DB_NAME).read_bytes() == b"the first backup"
