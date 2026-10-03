"""Backups, full backups, restore and project export through the API, on synthetic data."""

import asyncio
import json
import os
import sqlite3
import stat
import threading
import zipfile

import pyzipper
import pytest

import backend.backups as backups_module
from backend.db import DB_NAME, Database, new_id
from backend.db.migrations import MIGRATIONS
from scholia_app import background_idle, send, started

pytestmark = pytest.mark.asyncio

PASSPHRASE = "correct horse battery staple"


async def rows(client, sql, *args):
    return await asyncio.to_thread(client.state["db"].read, lambda conn: conn.execute(sql, args).fetchall())


async def new_project(client, name="Thesis", sensitivity="normal"):
    project = (await client.post("/api/projects", json={"name": name})).json()["id"]
    if sensitivity != "normal":  # sensitivity levels come with S1-11; set as its endpoint will
        await asyncio.to_thread(client.state["db"].write, lambda conn: conn.execute(
            "UPDATE projects SET sensitivity = ? WHERE id = ?", (sensitivity, project)))
    return project


async def audit(client, event):
    return [json.loads(data) for (data,) in await rows(client, "SELECT data FROM audit_log WHERE event = ?", event)]


def mode(path):
    return stat.S_IMODE(path.stat().st_mode)


def names(path):
    with pyzipper.AESZipFile(path) as archive:
        return sorted(info.filename for info in archive.infolist())


def read(path, name, passphrase=None):
    with pyzipper.AESZipFile(path) as archive:
        if passphrase:
            archive.setpassword(passphrase.encode())
        return archive.read(name)


# Listing and Back up now


async def test_backups_are_listed_with_kind_time_versions_and_size(tmp_path):
    async with started(tmp_path / "data") as client:
        [launch] = (await client.get("/api/backups")).json()["backups"]  # the day's backup, taken at launch
        response = await client.post("/api/backups")
        assert response.status_code == 200
        listed = (await client.get("/api/backups")).json()["backups"]
    assert [b["id"] for b in listed] == [response.json()["id"], launch["id"]]  # newest first
    assert listed[0]["kind"] == "daily" and listed[0]["schema_version"] == len(MIGRATIONS)
    assert listed[0]["app_version"] and listed[0]["size"] > 0 and listed[0]["time"].endswith("Z")


async def test_a_backup_is_taken_while_idle_and_never_while_a_run_is_active(tmp_path, monkeypatch):
    checks = []
    monkeypatch.setattr(backups_module, "IDLE_CHECK_SECONDS", 0.01)
    monkeypatch.setattr(Database, "backup_if_due", lambda self, now=None: checks.append(now))
    async with started(tmp_path / "data") as client:
        while not checks:  # idle: the check runs
            await asyncio.sleep(0.01)
        client.provider.hold = asyncio.Event()
        conversation = (await client.post("/api/conversations", json={"title": "t"})).json()["id"]
        turn = asyncio.create_task(send(client, conversation))
        await client.provider.started.wait()
        checks.clear()
        await asyncio.sleep(0.1)  # several checks pass while the turn runs
        assert checks == []
        client.provider.hold.set()
        await turn
        await background_idle(client)
        while not checks:
            await asyncio.sleep(0.01)


# Full backups


async def test_a_full_backup_holds_the_database_content_and_settings_and_never_keys_logs_or_the_lock(tmp_path):
    data, destination = tmp_path / "data", tmp_path / "chosen"
    destination.mkdir()
    async with started(data) as client:
        project = await new_project(client)
        await client.put("/api/instructions", json={"project_id": project, "text": "Use APA."})
        sha256 = await asyncio.to_thread(client.state["content"].put, b"%PDF-1.7 a paper", "application/pdf")
        (data / "credentials.json").write_text('{"openrouter": "sk-or-never-copied"}')
        (data / "scholia.lock").write_text("")
        (data / "logs").mkdir(exist_ok=True)
        (data / "logs" / "scholia.log").write_text("a log line")
        response = await client.post("/api/backups/full", json={"destination": str(destination)})
        assert response.status_code == 200, response.text
        result = response.json()
        [audited] = await audit(client, "full_backup")
    path = destination / result["file"].rsplit("/", 1)[1]
    assert result["encrypted"] is False and result["content_files"] == 1 and result["missing_files"] == 0
    assert audited["file"] == str(path) and audited["encrypted"] is False  # its destination, audited
    assert names(path) == sorted([DB_NAME, "backup.json", "config.toml", f"projects/{project}/config.toml",
                                  f"projects/{project}/AGENTS.md", f"content/{sha256[:2]}/{sha256}"])
    assert read(path, f"content/{sha256[:2]}/{sha256}") == b"%PDF-1.7 a paper"
    assert read(path, f"projects/{project}/AGENTS.md") == b"Use APA."
    assert json.loads(read(path, "backup.json"))["schema_version"] == len(MIGRATIONS)
    assert b"sk-or-never-copied" not in path.read_bytes()
    assert mode(path) == 0o600
    assert sorted(p.name for p in destination.iterdir()) == [path.name]  # no temporary file left
    assert not list((data / "backups" / backups_module.STAGING).iterdir())  # nor its copy in the data folder


async def test_a_full_backup_holding_a_private_project_needs_a_passphrase_and_is_encrypted(tmp_path):
    destination = tmp_path / "chosen"
    destination.mkdir()
    async with started(tmp_path / "data") as client:
        await new_project(client, "Interviews", "private")
        refused = await client.post("/api/backups/full", json={"destination": str(destination)})
        assert (refused.status_code, refused.json()["code"]) == (400, "passphrase_required")
        assert list(destination.iterdir()) == []
        response = await client.post("/api/backups/full", json={"destination": str(destination),
                                                                "passphrase": PASSPHRASE})
        assert response.status_code == 200 and response.json()["encrypted"] is True
    [path] = destination.iterdir()
    with pyzipper.AESZipFile(path) as archive:
        assert all(info.flag_bits & 1 for info in archive.infolist())  # every entry is encrypted
    with zipfile.ZipFile(path) as archive, pytest.raises(RuntimeError):
        archive.read(DB_NAME)  # not without the passphrase
    copy = tmp_path / "copy.sqlite3"
    copy.write_bytes(read(path, DB_NAME, PASSPHRASE))
    with sqlite3.connect(copy) as conn:
        assert conn.execute("SELECT name FROM projects WHERE sensitivity = 'private'").fetchall() == [("Interviews",)]


@pytest.mark.parametrize("where, code", [("relative", "invalid_destination"), ("missing", "destination_not_found"),
                                         ("inside", "destination_in_data_folder")])
async def test_a_full_backup_goes_only_to_an_existing_folder_outside_the_data_folder(tmp_path, where, code):
    data = tmp_path / "data"
    destination = {"relative": "backups", "missing": str(tmp_path / "nowhere"),
                   "inside": str(data / "backups")}[where]
    async with started(data) as client:
        response = await client.post("/api/backups/full", json={"destination": destination})
        assert (response.status_code, response.json()["code"]) == (400, code)
        assert await audit(client, "full_backup") == []


async def test_a_folder_that_cannot_be_written_gets_a_code_and_no_path_in_the_log(tmp_path, caplog):
    destination = tmp_path / "read-only"
    destination.mkdir()
    os.chmod(destination, 0o500)
    try:
        async with started(tmp_path / "data") as client:
            project = await new_project(client)
            for path, body in (("/api/backups/full", {}), (f"/api/projects/{project}/export", {})):
                response = await client.post(path, json={"destination": str(destination), **body})
                assert (response.status_code, response.json()["code"]) == (400, "destination_not_writable")
            assert await audit(client, "full_backup") == [] and await audit(client, "project_export") == []
    finally:
        os.chmod(destination, 0o700)
    assert list(destination.iterdir()) == []
    assert str(destination) not in caplog.text and "PermissionError" in caplog.text


async def test_a_full_disk_while_copying_the_database_is_disk_full(tmp_path, monkeypatch, caplog):
    import sqlite3 as sqlite
    destination = tmp_path / "chosen"
    destination.mkdir()

    def full(self, source, target):
        error = sqlite.OperationalError("database or disk is full")
        error.sqlite_errorcode, error.sqlite_errorname = sqlite.SQLITE_FULL, "SQLITE_FULL"
        raise error

    async with started(tmp_path / "data") as client:
        monkeypatch.setattr(Database, "_copy", full)
        for path, body in (("/api/backups", None), ("/api/backups/full", {"destination": str(destination)})):
            response = await client.post(path, json=body)
            assert (response.status_code, response.json()["code"]) == (507, "disk_full"), path
    assert "SQLITE_FULL" in caplog.text and str(tmp_path) not in caplog.text


async def test_a_full_disk_while_reading_a_backup_file_is_not_called_damage(tmp_path, monkeypatch):
    import errno
    destination = tmp_path / "chosen"
    destination.mkdir()
    async with started(tmp_path / "data") as client:
        file = (await client.post("/api/backups/full", json={"destination": str(destination)})).json()["file"]

        def full(staging, relative, source):
            raise OSError(errno.ENOSPC, "No space left on device", str(staging))

        monkeypatch.setattr(backups_module, "_write_staged", full)
        response = await client.post("/api/backups/restore", json={"file": file})
        assert (response.status_code, response.json()["code"]) == (507, "disk_full")
        assert str(tmp_path) not in response.text and not client.state["db"].closed


# Restore


async def test_restoring_an_automatic_backup_brings_back_its_state_and_runs_on(tmp_path):
    data = tmp_path / "data"
    async with started(data) as client:
        kept = await new_project(client, "Kept")
        await client.put("/api/instructions", json={"text": "Before"})
        backup = (await client.post("/api/backups")).json()["id"]
        later = await new_project(client, "Later")
        await client.put("/api/instructions", json={"text": "After"})
        old_db, old_harness = client.state["db"], client.state["harness"]

        response = await client.post("/api/backups/restore", json={"generation": backup})
        assert response.status_code == 200, response.text
        result = response.json()
        assert old_db.closed and old_harness.registry.closed  # the previous ones were stopped
        projects = {p["name"] for p in (await client.get("/api/projects")).json()["projects"]}
        assert projects == {"General", "Kept"}
        assert (await client.get("/api/instructions")).json()["text"] == "Before"
        assert sorted(p.name for p in (data / "projects").iterdir()) == [kept]
        assert not (data / "projects" / later).exists()
        # The safety copy holds the state before the restore, and can itself be restored.
        listed = [b["id"] for b in (await client.get("/api/backups")).json()["backups"]]
        assert result["safety_copy"] in listed and result["damaged_copy"] is None
        conn = sqlite3.connect((data / "backups" / result["safety_copy"] / DB_NAME).as_uri() + "?mode=ro", uri=True)
        assert {name for (name,) in conn.execute("SELECT name FROM projects")} == {"General", "Kept", "Later"}
        conn.close()
        [record] = await audit(client, "restore")
        assert (record["source"], record["backup"], record["safety_copy"]) == ("automatic", backup,
                                                                               result["safety_copy"])
        # The harness runs again in the same process.
        conversation = (await client.post("/api/conversations", json={"project_id": kept})).json()["id"]
        assert (await send(client, conversation))[-1]["status"] == "succeeded"
        restored_db = client.state["db"]
    assert restored_db.closed  # closing the app closed what the restore opened


async def test_a_restore_stops_a_running_turn(tmp_path):
    async with started(tmp_path / "data") as client:
        backup = (await client.post("/api/backups")).json()["id"]
        client.provider.hold = asyncio.Event()
        conversation = (await client.post("/api/conversations", json={"title": "t"})).json()["id"]
        turn = asyncio.create_task(send(client, conversation))
        await client.provider.started.wait()
        response = await client.post("/api/backups/restore", json={"generation": backup})
        assert response.status_code == 200
        assert (await turn)[-1]["status"] == "interrupted"
        client.provider.hold.set()


async def test_a_backup_from_a_newer_version_is_refused_with_its_explanation(tmp_path):
    data = tmp_path / "data"
    async with started(data) as client:
        backup = (await client.post("/api/backups")).json()["id"]
        with sqlite3.connect(data / "backups" / backup / DB_NAME) as conn:
            conn.execute(f"PRAGMA user_version = {len(MIGRATIONS) + 1}")
        project = await new_project(client)
        response = await client.post("/api/backups/restore", json={"generation": backup})
        assert (response.status_code, response.json()["code"]) == (409, "newer_schema")
        assert "newer version" in response.json()["message"]
        assert (await client.get(f"/api/projects/{project}")).status_code == 200  # nothing changed
        assert not client.state["db"].closed


@pytest.mark.parametrize("generation", ["daily/../../x", "monthly/20260101T000000000000Z", "daily/nope",
                                        "daily/../../../elsewhere/20260101T000000000000Z"])
async def test_only_a_listed_backup_can_be_restored(tmp_path, generation):
    async with started(tmp_path / "data") as client:
        response = await client.post("/api/backups/restore", json={"generation": generation})
        assert (response.status_code, response.json()["code"]) == (404, "not_found")
        both = await client.post("/api/backups/restore", json={"generation": "daily/x", "file": "/x.zip"})
        assert (both.status_code, both.json()["code"]) == (400, "invalid_request")
        relative = await client.post("/api/backups/restore", json={"file": "backup.zip"})
        assert (relative.status_code, relative.json()["code"]) == (400, "invalid_request")


async def test_restoring_an_encrypted_full_backup_needs_its_passphrase_and_brings_its_files(tmp_path):
    data, destination = tmp_path / "data", tmp_path / "chosen"
    destination.mkdir()
    async with started(data) as client:
        project = await new_project(client, "Interviews", "private")
        sha256 = await asyncio.to_thread(client.state["content"].put, b"transcript one", "text/plain")
        file = (await client.post("/api/backups/full", json={"destination": str(destination),
                                                             "passphrase": PASSPHRASE})).json()["file"]
        await client.delete(f"/api/projects/{project}")
        (data / "content" / sha256[:2] / sha256).unlink()

        for body, code in (({"file": file}, "passphrase_required"),
                           ({"file": file, "passphrase": "wrong"}, "wrong_passphrase")):
            response = await client.post("/api/backups/restore", json=body)
            assert (response.status_code, response.json()["code"]) == (400, code)
        response = await client.post("/api/backups/restore", json={"file": file, "passphrase": PASSPHRASE})
        assert response.status_code == 200, response.text
        assert response.json()["missing_files"] == [] and response.json()["source"] == "full"
        assert (await client.get(f"/api/projects/{project}")).json()["name"] == "Interviews"
        assert (data / "content" / sha256[:2] / sha256).read_bytes() == b"transcript one"
        assert mode(data / "content" / sha256[:2] / sha256) == 0o600


async def test_a_file_that_is_not_a_backup_is_refused(tmp_path):
    other = tmp_path / "other.zip"
    with zipfile.ZipFile(other, "w") as archive:
        archive.writestr("../escape.txt", "x")
    async with started(tmp_path / "data") as client:
        response = await client.post("/api/backups/restore", json={"file": str(other)})
        assert (response.status_code, response.json()["code"]) == (400, "not_a_backup")
    assert not (tmp_path / "escape.txt").exists()


async def test_a_restore_whose_database_cannot_be_opened_puts_everything_back(tmp_path, monkeypatch):
    data = tmp_path / "data"
    async with started(data) as client:
        backup = (await client.post("/api/backups")).json()["id"]
        project = await new_project(client)
        real = Database.__init__
        opened = []

        def failing_once(self, data_dir, **options):
            if not opened:
                opened.append(True)
                raise RuntimeError("a migration failed")
            real(self, data_dir, **options)

        monkeypatch.setattr(Database, "__init__", failing_once)
        response = await client.post("/api/backups/restore", json={"generation": backup})
        assert (response.status_code, response.json()["code"]) == (500, "restore_failed")
        assert (await client.get(f"/api/projects/{project}")).status_code == 200  # as it was, running again
        assert (data / "projects" / project / "config.toml").is_file()


# Project export


async def test_a_project_exports_as_markdown_json_its_files_and_settings_without_keys(tmp_path):
    data, destination = tmp_path / "data", tmp_path / "chosen"
    destination.mkdir()
    async with started(data) as client:
        project = await new_project(client)
        other = await new_project(client, "Other")
        conversation = (await client.post("/api/conversations", json={"project_id": project,
                                                                      "title": "Sampling"})).json()["id"]
        await send(client, conversation, "How large a sample do I need?")
        await client.post("/api/conversations", json={"project_id": other, "title": "Elsewhere"})
        await client.put("/api/instructions", json={"project_id": project, "text": "Use APA."})
        config = data / "projects" / project / "config.toml"
        config.write_text(config.read_text() + 'api_key = "sk-or-typed-by-hand"\n')  # ignored by the app
        sha256 = await asyncio.to_thread(client.state["content"].put, b"%PDF-1.7 a paper", "application/pdf")
        material, version = new_id(), new_id()

        def add_material(conn):
            conn.execute("INSERT INTO materials (id, project_id, title, source) VALUES (?, ?, 'A paper', 'upload')",
                         (material, project))
            conn.execute("INSERT INTO material_versions (id, material_id, seq, file_sha256, is_current)"
                         " VALUES (?, ?, 0, ?, 1)", (version, material, sha256))
        await asyncio.to_thread(client.state["db"].write, add_material)

        response = await client.post(f"/api/projects/{project}/export", json={"destination": str(destination)})
        assert response.status_code == 200, response.text
        result = response.json()
        [audited] = await audit(client, "project_export")
    path = destination / result["file"].rsplit("/", 1)[1]
    assert audited["file"] == str(path) and (result["conversations"], result["turns"]) == (1, 1)
    assert names(path) == sorted(["project.json", "conversations.json", f"conversations/{conversation}.md",
                                  "materials.json", f"materials/{material}/v0.pdf", "settings/config.toml",
                                  "settings/AGENTS.md"])
    markdown = read(path, f"conversations/{conversation}.md").decode()
    assert "# Sampling" in markdown and "How large a sample do I need?" in markdown and "An answer." in markdown
    [exported] = json.loads(read(path, "conversations.json"))
    [turn] = exported["turns"]
    assert turn["status"] == "succeeded" and turn["accounting"] and turn["cost_usd"] > 0
    assert json.loads(read(path, "project.json"))["project"]["name"] == "Thesis"
    assert json.loads(read(path, "materials.json"))[0]["versions"][0]["file"] == f"materials/{material}/v0.pdf"
    assert read(path, f"materials/{material}/v0.pdf") == b"%PDF-1.7 a paper"
    assert read(path, "settings/AGENTS.md") == b"Use APA."
    assert b"sk-or-typed-by-hand" not in read(path, "settings/config.toml")
    assert b"Elsewhere" not in path.read_bytes() and mode(path) == 0o600


async def test_a_private_project_exports_only_encrypted(tmp_path):
    destination = tmp_path / "chosen"
    destination.mkdir()
    async with started(tmp_path / "data") as client:
        project = await new_project(client, "Interviews", "local_only")
        await client.post("/api/conversations", json={"project_id": project, "title": "Participant 7"})
        refused = await client.post(f"/api/projects/{project}/export", json={"destination": str(destination)})
        assert (refused.status_code, refused.json()["code"]) == (400, "passphrase_required")
        response = await client.post(f"/api/projects/{project}/export",
                                     json={"destination": str(destination), "passphrase": PASSPHRASE})
        assert response.status_code == 200 and response.json()["encrypted"] is True
        missing = await client.post(f"/api/projects/{project[:-1]}0/export", json={"destination": str(destination)})
        assert missing.status_code == 404
    [path] = destination.iterdir()
    assert b"Participant 7" not in path.read_bytes()  # titles are inside, never in the names
    assert b"Participant 7" in read(path, "conversations.json", PASSPHRASE)


async def test_a_restored_app_takes_requests_only_once_its_runs_are_recovered(tmp_path, monkeypatch):
    from backend.runs import Harness
    real, seen = Harness.recover, []

    async def recover(self):
        seen.append(state.get("harness") is self)  # visible to requests while it recovers?
        await real(self)

    async with started(tmp_path / "data") as client:
        state = client.state
        backup = (await client.post("/api/backups")).json()["id"]
        monkeypatch.setattr(Harness, "recover", recover)
        assert (await client.post("/api/backups/restore", json={"generation": backup})).status_code == 200
    assert seen == [False]


# A restore excludes every other change


def hold(monkeypatch, owner, name):
    """Hold owner.name (run in a worker thread) until released: (started, release) events."""
    started_, release = threading.Event(), threading.Event()
    real = getattr(owner, name)

    def held(*args, **kwargs):
        started_.set()
        assert release.wait(10)
        return real(*args, **kwargs)

    monkeypatch.setattr(owner, name, held)
    return started_, release


async def test_a_settings_save_during_a_restore_waits_and_lands_after_it(tmp_path, monkeypatch):
    async with started(tmp_path / "data") as client:
        backup = (await client.post("/api/backups")).json()["id"]
        read = (await client.get("/api/settings")).json()["hash"]
        swapping, release = hold(monkeypatch, backups_module, "_put_in_place")
        restore = asyncio.create_task(client.post("/api/backups/restore", json={"generation": backup}))
        await asyncio.to_thread(swapping.wait, 10)
        save = asyncio.create_task(client.put("/api/settings", json={"hash": read, "updates": {"ui.language": "zh-CN"}}))
        await asyncio.sleep(0.2)
        assert not save.done()  # it waits for the restore, which would otherwise swap it away
        release.set()
        assert (await restore).status_code == 200
        assert (await save).status_code == 200
        assert (await client.get("/api/settings")).json()["values"]["ui"]["language"] == "zh-CN"


async def test_an_instructions_save_and_a_project_creation_during_a_restore_land_after_it(tmp_path, monkeypatch):
    data = tmp_path / "data"
    async with started(data) as client:
        backup = (await client.post("/api/backups")).json()["id"]
        swapping, release = hold(monkeypatch, backups_module, "_put_in_place")
        restore = asyncio.create_task(client.post("/api/backups/restore", json={"generation": backup}))
        await asyncio.to_thread(swapping.wait, 10)
        save = asyncio.create_task(client.put("/api/instructions", json={"text": "Written during the restore"}))
        create = asyncio.create_task(client.post("/api/projects", json={"name": "Created during the restore"}))
        await asyncio.sleep(0.2)
        assert not save.done() and not create.done()
        release.set()
        assert (await restore).status_code == 200
        assert (await save).status_code == 200 and (await create).status_code == 201
        assert (await client.get("/api/instructions")).json()["text"] == "Written during the restore"
        project = (await create).json()["id"]
        assert (await client.get(f"/api/projects/{project}")).status_code == 200  # its record and its folder
        assert (data / "projects" / project / "config.toml").is_file()


async def test_a_restore_waits_for_a_deletion_and_its_purge(tmp_path, monkeypatch):
    import backend.app as app_module
    async with started(tmp_path / "data") as client:
        conversation = (await client.post("/api/conversations", json={"title": "Participant 7"})).json()["id"]
        older = (await client.post("/api/backups")).json()["id"]  # it holds the conversation
        deleted, release = hold(monkeypatch, app_module, "delete")  # committed; the purge has not begun
        deletion = asyncio.create_task(client.delete(f"/api/conversations/{conversation}",
                                                     params={"purge_backups": "true"}))
        await asyncio.to_thread(deleted.wait, 10)
        restore = asyncio.create_task(client.post("/api/backups/restore", json={"generation": older}))
        await asyncio.sleep(0.2)
        assert not restore.done()  # it would bring the conversation back between the deletion and its purge
        release.set()
        assert (await deletion).json()["purged_backups"] >= 1
        assert (await restore).status_code == 404  # the backup that held it was purged first
        assert (await client.get(f"/api/conversations/{conversation}")).status_code == 404


async def test_a_cancelled_deletion_still_purges_the_backups(tmp_path, monkeypatch):
    import backend.app as app_module
    async with started(tmp_path / "data") as client:
        conversation = (await client.post("/api/conversations", json={"title": "Participant 7"})).json()["id"]
        await client.post("/api/backups")
        deleting, release = hold(monkeypatch, app_module, "delete")
        deletion = asyncio.create_task(client.delete(f"/api/conversations/{conversation}",
                                                     params={"purge_backups": "true"}))
        await asyncio.to_thread(deleting.wait, 10)
        deletion.cancel()  # the window closed
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await deletion
        assert len(await audit(client, "backup_purge")) == 1  # its object is gone, so it could not be asked again
        assert len((await client.get("/api/backups")).json()["backups"]) == 1
