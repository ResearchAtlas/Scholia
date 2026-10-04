"""Backups, full backups, restore and project export through the API, on synthetic data."""

import asyncio
import contextlib
import json
import os
import sqlite3
import stat
import shutil
import threading
import uuid
import zipfile

import pyzipper
import pytest

import backend.backups as backups_module
from backend.db import DB_NAME, Database, new_id
from backend.db.migrations import MIGRATIONS
from scholia_app import background_idle, send, started, stored_material

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
        sha256 = await stored_material(client, project, b"%PDF-1.7 a paper", "application/pdf")
        await asyncio.to_thread(client.state["content"].put, b"nothing refers to me", "text/plain")
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
    assert not any(b"sk-or-never-copied" in read(path, name) for name in names(path))
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


async def test_a_full_backup_holds_no_file_of_a_project_deleted_moments_before(tmp_path):
    destination = tmp_path / "chosen"
    destination.mkdir()
    async with started(tmp_path / "data") as client:
        private = await new_project(client, "Interviews", "private")
        await stored_material(client, private, b"Participant 7 transcript", "text/plain")
        assert (await client.delete(f"/api/projects/{private}")).status_code == 200  # its file is not collected yet
        response = await client.post("/api/backups/full", json={"destination": str(destination)})
        assert response.status_code == 200 and response.json()["encrypted"] is False  # no Private project is left
        assert response.json()["content_files"] == 0
    [path] = destination.iterdir()
    assert not any(n.startswith("content/") for n in names(path))
    assert not any(b"Participant 7" in read(path, name) for name in names(path))


async def test_a_full_backup_never_holds_a_key_typed_into_a_settings_file(tmp_path):
    data, destination = tmp_path / "data", tmp_path / "chosen"
    destination.mkdir()
    async with started(data) as client:
        project = await new_project(client)
        personal, project_config = data / "config.toml", data / "projects" / project / "config.toml"
        personal.write_text("# my key is sk-or-typed-into-a-comment\n" + personal.read_text() + '\n[ui]\nlanguage = "en"\n'
                            '[misc]\nopenrouter = "sk-or-typed-into-an-ordinary-name"\n'
                            '[providers.extra]\nkind = "openai-compatible"\napi_key = "sk-or-typed-into-personal"\n')
        project_config.write_text(project_config.read_text() + 'token = "sk-or-typed-into-project"\n'
                                  'openrouter = "sk-or-typed-into-a-project-name"  # and sk-or-typed-into-a-note\n')
        (data / "projects" / project / "AGENTS.md").write_text("Use APA.")
        response = await client.post("/api/backups/full", json={"destination": str(destination)})
        assert response.status_code == 200, response.text
        exported = await client.post(f"/api/projects/{project}/export", json={"destination": str(destination)})
        assert exported.status_code == 200, exported.text
    path = destination / response.json()["file"].rsplit("/", 1)[1]
    export = destination / exported.json()["file"].rsplit("/", 1)[1]
    for archive in (path, export):  # read, not as compressed bytes
        assert not any(b"sk-or-typed-into" in read(archive, name) for name in names(archive)), archive.name
    assert b'language = "en"' in read(path, "config.toml") and b"citation_style" in read(
        path, f"projects/{project}/config.toml")  # what the app reads stays
    assert b"citation_style" in read(export, "settings/config.toml") and b"#" not in read(export, "settings/config.toml")


async def test_a_full_backup_is_recorded_before_anything_is_written_and_removed_if_it_fails(tmp_path, monkeypatch):
    import errno
    import backend.db.database as database_module
    destination = tmp_path / "chosen"
    destination.mkdir()
    async with started(tmp_path / "data") as client:
        real = database_module._fsync

        def failing(path):  # the folder cannot be synced once the file is in it
            if path == destination:
                raise OSError(errno.EIO, "I/O error")
            real(path)

        monkeypatch.setattr(backups_module, "_fsync", failing)
        response = await client.post("/api/backups/full", json={"destination": str(destination)})
        assert response.json()["code"] == "write_failed"
        assert list(destination.iterdir()) == []  # not left half published
        [attempt] = await audit(client, "full_backup")
        assert attempt["file"].startswith(str(destination))

        monkeypatch.setattr(backups_module, "_fsync", real)
        db = client.state["db"]

        def unrecordable(fn):
            raise OSError(errno.EIO, "I/O error")

        monkeypatch.setattr(db, "write", unrecordable)
        response = await client.post("/api/backups/full", json={"destination": str(destination)})
        assert response.json()["code"] == "write_failed"
        assert list(destination.iterdir()) == []  # nothing written where it could not be recorded


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
            # Each attempt was recorded before anything was written there, so nothing leaves unrecorded.
            assert len(await audit(client, "full_backup")) == len(await audit(client, "project_export")) == 1
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


async def test_a_restore_works_where_the_file_system_has_no_hard_links(tmp_path, monkeypatch):
    import errno

    def no_links(source, target, **kwargs):  # such as an exFAT drive
        raise OSError(errno.ENOTSUP, "Operation not supported")

    monkeypatch.setattr(backups_module.os, "link", no_links)
    data = tmp_path / "data"
    async with started(data) as client:
        kept = await new_project(client, "Kept")
        backup = (await client.post("/api/backups")).json()["id"]
        await new_project(client, "Later")
        response = await client.post("/api/backups/restore", json={"generation": backup})
        assert response.status_code == 200, response.text
        assert {p["name"] for p in (await client.get("/api/projects")).json()["projects"]} == {"General", "Kept"}
        assert kept in {p["id"] for p in (await client.get("/api/projects")).json()["projects"]}
        # A failed restore puts the database it kept aside (a copy here) back in place.
        later = await new_project(client, "After")
        real = Database.__init__
        opened = []

        def failing_once(self, data_dir, **options):
            if not opened:
                opened.append(True)
                raise RuntimeError("a migration failed")
            real(self, data_dir, **options)

        monkeypatch.setattr(Database, "__init__", failing_once)
        failed = await client.post("/api/backups/restore", json={"generation": backup})
        assert (failed.status_code, failed.json()["code"]) == (500, "restore_failed")
        assert (await client.get(f"/api/projects/{later}")).status_code == 200


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
        sha256 = await stored_material(client, project, b"transcript one", "text/plain")
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


async def test_a_restore_whose_harness_cannot_start_puts_everything_back_and_runs_on(tmp_path, monkeypatch):
    from backend.runs import Harness
    data = tmp_path / "data"
    async with started(data) as client:
        backup = (await client.post("/api/backups")).json()["id"]
        project = await new_project(client)
        real, recovered = Harness.recover, []

        async def failing_once(self, kick=True):
            recovered.append(self)
            if len(recovered) == 1:
                raise RuntimeError("recovery failed")
            await real(self, kick)

        monkeypatch.setattr(Harness, "recover", failing_once)
        response = await client.post("/api/backups/restore", json={"generation": backup})
        assert (response.status_code, response.json()["code"]) == (500, "restore_failed")
        assert (await client.get(f"/api/projects/{project}")).status_code == 200  # the previous state, running
        assert (await client.post("/api/projects", json={"name": "After"})).status_code == 201
        assert (data / "projects" / project / "config.toml").is_file()
        assert not (data / "backups" / backups_module.JOURNAL).exists()
        assert list((data / "backups" / backups_module.STAGING).iterdir()) == []


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
    assert not any(b"Elsewhere" in read(path, name) for name in names(path)) and mode(path) == 0o600


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
        missing = await client.post(f"/api/projects/{new_id()}/export", json={"destination": str(destination)})
        assert missing.status_code == 404
    [path] = destination.iterdir()
    assert b"Participant 7" not in path.read_bytes()  # titles are inside, never in the names
    assert b"Participant 7" in read(path, "conversations.json", PASSPHRASE)


def held_at(monkeypatch, name, after=False):
    """Hold the archive's thread at its first call of backups.<name> (after it returns, when
    after), until the returned Event is set; reached is set once it is held there."""
    reached, go, real = threading.Event(), threading.Event(), getattr(backups_module, name)

    def held(*args):
        first = not reached.is_set()
        result = real(*args) if after else None
        if first:
            reached.set()
            go.wait(10)
        return result if after else real(*args)

    monkeypatch.setattr(backups_module, name, held)
    return reached, go


class Destination:
    """What an archive writes to its destination file, below its guard: each write's size, and a
    hold at the first write of at least hold_at bytes, inside the archive's lock."""

    def __init__(self, monkeypatch, hold_at=None, then=None):
        self.sizes, self.reached, self.go, self.hold_at, self.then = [], threading.Event(), threading.Event(), \
            hold_at, then
        real, outer = backups_module._Guarded, self

        class Recording:
            def __init__(self, raw):
                self.raw = raw

            def write(self, data):
                if outer.hold_at is not None and len(data) >= outer.hold_at and not outer.reached.is_set():
                    outer.reached.set()
                    outer.go.wait(10)
                written = self.raw.write(data)
                outer.sizes.append(len(data))
                if outer.then and len(outer.sizes) == outer.then[0]:
                    outer.then[1]()
                return written

            def __getattr__(self, name):
                return getattr(self.raw, name)

        monkeypatch.setattr(backups_module, "_Guarded", lambda raw, archive: real(Recording(raw), archive))


async def archived_while(client, monkeypatch, url, destination, point, change):
    """Start an archive to destination without a passphrase, hold it at point, make change through
    the API, let it go on, and return its response."""
    reached, go = held_at(monkeypatch, *point)
    task = asyncio.create_task(client.post(url, json={"destination": str(destination)}))
    await asyncio.to_thread(reached.wait, 10)
    changed = await change()
    assert changed.status_code == 200, changed.text
    go.set()
    return await task


POINTS = {"before its first write": ("_free_name",), "with its content written": ("_fsync",)}


@pytest.mark.parametrize("point", POINTS)
@pytest.mark.parametrize("archive", ["export", "full backup"])
async def test_a_project_made_stricter_while_an_archive_is_written_stops_it(tmp_path, monkeypatch, archive, point):
    destination = tmp_path / "chosen"
    destination.mkdir()
    async with started(tmp_path / "data") as client:
        project = await new_project(client, "Interviews")
        await client.post("/api/conversations", json={"project_id": project, "title": "Participant 7"})
        url = f"/api/projects/{project}/export" if archive == "export" else "/api/backups/full"
        refused = await archived_while(client, monkeypatch, url, destination, POINTS[point], lambda: client.post(
            f"/api/projects/{project}/sensitivity", json={"level": "private"}))
        assert (refused.status_code, refused.json()["code"]) == (400, "passphrase_required")
        assert list(destination.iterdir()) == []  # what it wrote is gone
        response = await client.post(url, json={"destination": str(destination), "passphrase": PASSPHRASE})
        assert response.status_code == 200 and response.json()["encrypted"] is True


@pytest.mark.parametrize("held", ["a write of its content", "its naming"])
async def test_a_stricter_level_applies_only_once_the_archive_can_write_no_more(tmp_path, monkeypatch, held):
    destination = tmp_path / "chosen"
    destination.mkdir()
    async with started(tmp_path / "data") as client:
        project = await new_project(client, "Interviews")
        sha256 = await stored_material(client, project, os.urandom(3 << 20), "application/pdf")
        assert sha256
        if held == "its naming":
            reached, go = held_at(monkeypatch, "_publish", after=True)  # inside the archive's hold
        else:
            written = Destination(monkeypatch, hold_at=backups_module._CHUNK)  # a full piece of the file
            reached, go = written.reached, written.go
        archive = asyncio.create_task(client.post(f"/api/projects/{project}/export",
                                                  json={"destination": str(destination)}))
        await asyncio.to_thread(reached.wait, 10)
        change = asyncio.create_task(client.post(f"/api/projects/{project}/sensitivity", json={"level": "private"}))
        await asyncio.sleep(0.3)
        assert not change.done()  # not confirmed while the archive can still write
        assert (await rows(client, "SELECT sensitivity FROM projects WHERE id = ?", project)) == [("normal",)]
        assert project in client.state["gate"]._under_way  # the project's requests are held meanwhile
        go.set()
        assert (await change).status_code == 200
        response = await archive
        assert (await rows(client, "SELECT sensitivity FROM projects WHERE id = ?", project)) == [("private",)]
    if held == "its naming":  # complete before the change took effect
        assert response.status_code == 200 and len(list(destination.iterdir())) == 1
    else:  # its write in progress finished; nothing more was written, and what it wrote is gone
        assert (response.status_code, response.json()["code"]) == (400, "passphrase_required")
        assert list(destination.iterdir()) == []


@pytest.mark.parametrize("change", ["review lock", "move into a Private project", "move into a Normal project"])
async def test_locking_or_moving_a_conversation_out_stops_an_export(tmp_path, monkeypatch, change):
    destination = tmp_path / "chosen"
    destination.mkdir()
    async with started(tmp_path / "data") as client:
        project = await new_project(client, "Interviews")
        other = await new_project(client, "Other", "private" if change == "move into a Private project" else "normal")
        conversation = (await client.post("/api/conversations", json={"project_id": project,
                                                                      "title": "Participant 7"})).json()["id"]
        path, body = ((f"/api/projects/{project}/review-lock", {"locked": True}) if change == "review lock" else
                      (f"/api/conversations/{conversation}/move", {"project_id": other}))
        refused = await archived_while(client, monkeypatch, f"/api/projects/{project}/export", destination,
                                       POINTS["with its content written"], lambda: client.post(path, json=body))
        assert (refused.status_code, refused.json()["code"]) == (400, "passphrase_required")
        assert list(destination.iterdir()) == []


async def test_a_move_whose_request_is_cancelled_still_stops_the_source_exports_until_it_ends(tmp_path, monkeypatch):
    destination = tmp_path / "chosen"
    destination.mkdir()
    async with started(tmp_path / "data") as client:
        source, other = await new_project(client, "Interviews"), await new_project(client, "Other")
        conversation = (await client.post("/api/conversations", json={"project_id": source,
                                                                      "title": "Participant 7"})).json()["id"]
        db, real, reached, go, holding = client.state["db"], client.state["db"].write, threading.Event(), \
            threading.Event(), [True]

        def write(fn):  # the move's write, held before it commits
            if holding and holding.pop():
                reached.set()
                go.wait(10)
            return real(fn)

        monkeypatch.setattr(db, "write", write)
        move = asyncio.create_task(client.post(f"/api/conversations/{conversation}/move", json={"project_id": other}))
        await asyncio.to_thread(reached.wait, 10)
        move.cancel()  # the request goes; the move it began is still under way
        await asyncio.sleep(0.1)
        export = asyncio.create_task(client.post(f"/api/projects/{source}/export",
                                                 json={"destination": str(destination)}))
        await asyncio.sleep(0.1)
        go.set()
        with contextlib.suppress(asyncio.CancelledError):
            await move
        response = await export
        assert (response.status_code, response.json()["code"]) == (400, "passphrase_required")
        assert list(destination.iterdir()) == []
        assert await rows(client, "SELECT project_id FROM conversations WHERE id = ?", conversation) == [(other,)]


async def test_an_archive_writes_at_most_a_chunk_at_a_time(tmp_path, monkeypatch):
    destination, source = tmp_path / "chosen", tmp_path / "big.bin"
    destination.mkdir()
    source.write_bytes(os.urandom(3 * backups_module._CHUNK + 12345))  # incompressible
    text = os.urandom(2 * backups_module._CHUNK + 777)  # a large entry given whole
    written = Destination(monkeypatch)
    path = await asyncio.to_thread(
        backups_module._write_zip, destination, "scholia-project", [("big.bin", source), ("text.md", text)], None,
        stop=lambda: False, audit=lambda path: None, archive=backups_module._Archive(None, stopped=False))
    assert written.sizes and max(written.sizes) <= backups_module._CHUNK
    assert sum(written.sizes) >= path.stat().st_size  # all of it through the guard (headers are rewritten)
    assert read(path, "big.bin") == source.read_bytes() and read(path, "text.md") == text


async def test_a_stopped_archive_writes_nothing_more_not_even_its_closing_records(tmp_path, monkeypatch):
    destination, source = tmp_path / "chosen", tmp_path / "big.bin"
    destination.mkdir()
    source.write_bytes(os.urandom(2 * backups_module._CHUNK))
    archive = backups_module._Archive(None, stopped=False)
    written = Destination(monkeypatch, then=(2, lambda: setattr(archive, "stopped", True)))
    with pytest.raises(backups_module.BackupError) as error:
        await asyncio.to_thread(backups_module._write_zip, destination, "scholia-project", [("big.bin", source)],
                                None, stop=lambda: False, audit=lambda path: None, archive=archive)
    assert (error.value.status, error.value.code) == (400, "passphrase_required")
    assert len(written.sizes) == 2  # no write after the stop: no buffered data, header or central directory
    assert list(destination.iterdir()) == []


async def test_archives_holding_a_project_stop_once_their_write_in_progress_ends(tmp_path):
    archives = backups_module.Archives()
    with archives.watching({"a"}) as of_a, archives.watching({"b"}) as of_b, archives.watching(None) as of_all:
        of_a.lock.acquire()  # a write of it in progress
        entered = asyncio.Event()

        async def change():
            async with archives.stricter("a"):
                entered.set()
                with archives.watching({"a"}) as meanwhile:  # registered while the change is pending
                    assert meanwhile.stopped
                await asyncio.sleep(0)

        task = asyncio.create_task(change())
        await asyncio.sleep(0.2)
        assert not entered.is_set()  # it waits for that write
        of_a.lock.release()
        await task
        assert (of_a.stopped, of_b.stopped, of_all.stopped) == (True, False, True)
        with archives.watching({"a"}) as later:  # after the change: it reads the new level
            assert not later.stopped
    with pytest.raises(RuntimeError), archives.watching({"c"}):
        raise RuntimeError("its archive failed")
    assert not archives._writing  # each one leaves the register, however it ends


async def test_an_archive_of_text_alone_stops_when_the_app_closes_and_leaves_nothing(tmp_path):
    destination = tmp_path / "chosen"
    destination.mkdir()
    closing = iter([False, False])  # the app closes once the first entry is written
    with pytest.raises(backups_module.BackupError) as error:
        await asyncio.to_thread(backups_module._write_zip, destination, "scholia-project",
                                [("a.md", b"text"), ("b.md", b"more")], None, stop=lambda: next(closing, True),
                                audit=lambda path: None)
    assert (error.value.status, error.value.code) == (503, "closing")
    assert list(destination.iterdir()) == []


async def test_an_export_whose_last_check_fails_leaves_no_file(tmp_path, monkeypatch):
    destination = tmp_path / "chosen"
    destination.mkdir()
    async with started(tmp_path / "data") as client:
        project = await new_project(client)
        db, real, written = client.state["db"], backups_module._write_zip, threading.Event()

        def write_zip(*args, **options):
            path = real(*args, **options)
            written.set()
            return path

        def read(fn, real_read=db.read):
            if written.is_set():  # the check that the project still exists, once the file is there
                raise backups_module.DatabaseClosedError("the database is closed")
            return real_read(fn)

        monkeypatch.setattr(backups_module, "_write_zip", write_zip)
        monkeypatch.setattr(db, "read", read)
        response = await client.post(f"/api/projects/{project}/export", json={"destination": str(destination)})
        monkeypatch.undo()
        assert response.status_code == 503
    assert list(destination.iterdir()) == []


@pytest.mark.parametrize("language, lines", [
    ("en", ["# Untitled conversation", "## Researcher (t1)", "## From another conversation (t2)",
            "*No answer: Failed*"]),
    ("zh-CN", ["# 未命名对话", "## 研究者 (t1)", "## 来自另一个对话 (t2)", "*没有回答：失败*"])])
async def test_an_exports_headings_are_in_the_interface_language(language, lines):
    conversation = {"title": None, "turns": [
        {"author": "researcher", "started_at": "t1", "message": {"text": "Q"}, "answer": {"text": "A"},
         "status": "succeeded"},
        {"author": "parent", "started_at": "t2", "message": {"text": "Q2"}, "answer": None, "status": "failed"}]}
    written = backups_module._conversation_markdown(conversation, language).decode().splitlines()
    assert all(line in written for line in lines)


async def test_a_project_export_takes_the_interface_language(tmp_path):
    destination = tmp_path / "chosen"
    destination.mkdir()
    async with started(tmp_path / "data") as client:
        project = await new_project(client)
        conversation = (await client.post("/api/conversations", json={"project_id": project,
                                                                      "title": "Sampling"})).json()["id"]
        await send(client, conversation, "How large a sample do I need?")
        url = f"/api/projects/{project}/export"
        unknown = await client.post(url, json={"destination": str(destination), "language": "fr"})
        assert (unknown.status_code, unknown.json()["code"]) == (400, "invalid_request")  # a supported one only
        response = await client.post(url, json={"destination": str(destination), "language": "zh-CN"})
        assert response.status_code == 200, response.text
    [path] = destination.iterdir()
    assert "## 研究者 (" in read(path, f"conversations/{conversation}.md").decode()


async def test_a_restored_app_takes_requests_only_once_its_runs_are_recovered(tmp_path, monkeypatch):
    from backend.runs import Harness
    real, seen = Harness.recover, []

    async def recover(self, kick=True):
        seen.append(state.get("harness") is self)  # visible to requests while it recovers?
        await real(self, kick)

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


async def test_during_a_restore_only_health_the_backups_and_the_restore_are_served(tmp_path, monkeypatch):
    data = tmp_path / "data"
    async with started(data) as client:
        conversation = (await client.post("/api/conversations", json={"title": "t"})).json()["id"]
        backup = (await client.post("/api/backups")).json()["id"]
        read = (await client.get("/api/settings")).json()["hash"]
        swapping, release = hold(monkeypatch, backups_module, "_put_in_place")
        restore = asyncio.create_task(client.post("/api/backups/restore", json={"generation": backup}))
        await asyncio.to_thread(swapping.wait, 10)
        refused = [
            await client.put("/api/settings", json={"hash": read, "updates": {"ui.language": "zh-CN"}}),
            await client.put("/api/instructions", json={"text": "Written during the restore"}),
            await client.post("/api/projects", json={"name": "Created during the restore"}),
            await client.post(f"/api/conversations/{conversation}/message/stream", json={"content": "hi"}),
            await client.get("/api/projects"),  # reads, and GET routes that write an audit row, too
            await client.get("/api/providers/openrouter/models"),
            await client.post("/api/backups/restore", json={"generation": backup}),  # one restore at a time
        ]
        assert [(r.status_code, r.json()["code"]) for r in refused] == [(503, "restoring")] * 6 + [(409, "restoring")]
        assert (await client.get("/api/health")).status_code == 200
        assert (await client.get("/api/backups")).status_code == 200
        release.set()
        assert (await restore).status_code == 200
        assert (await client.get("/api/instructions")).json()["text"] == ""  # nothing landed in the swap
        assert {p["name"] for p in (await client.get("/api/projects")).json()["projects"]} == {"General"}
        assert (await client.put("/api/instructions", json={"text": "After"})).status_code == 200  # served again


async def test_a_turn_running_when_a_restore_starts_ends_before_the_safety_copy(tmp_path, monkeypatch):
    data = tmp_path / "data"
    async with started(data) as client:
        backup = (await client.post("/api/backups")).json()["id"]
        client.provider.hold = asyncio.Event()
        conversation = (await client.post("/api/conversations", json={"title": "t"})).json()["id"]
        turn = asyncio.create_task(send(client, conversation))
        await client.provider.started.wait()
        response = await client.post("/api/backups/restore", json={"generation": backup})
        assert response.status_code == 200
        assert (await turn)[-1]["status"] == "interrupted"
        client.provider.hold.set()
        safety = data / "backups" / response.json()["safety_copy"] / DB_NAME
    conn = sqlite3.connect(safety.as_uri() + "?mode=ro", uri=True)
    try:  # the safety copy holds the turn as it ended, nothing it could commit after the copy
        assert conn.execute("SELECT status FROM runs WHERE kind = 'turn'").fetchall() == [("interrupted",)]
    finally:
        conn.close()


async def test_a_request_let_in_before_the_app_was_limited_is_refused_once_inside(tmp_path):
    data = tmp_path / "data"
    async with started(data) as client:
        state = client.state
        async with state["writers"].alone():  # as a restore holds it
            save = asyncio.create_task(client.put("/api/instructions", json={"text": "Queued"}))
            await asyncio.sleep(0.1)
            assert not save.done()  # it passed the first check and waits inside the gate
            backups_module._limit(state, "restore_interrupted", "a restore could neither be finished nor undone")
        response = await save
        assert (response.status_code, response.json()["code"]) == (503, "restore_interrupted")
        assert not (data / "AGENTS.md").exists()


async def test_a_cancelled_restore_waiting_its_turn_lets_every_queued_request_in():
    writers = backups_module.Writers()
    release = asyncio.Event()
    entered = []

    async def request(name):  # a request that stays inside, as a slow save does
        async with writers.shared():
            entered.append(name)
            await release.wait()

    async def restoring():
        async with writers.alone():
            pass

    async with writers.shared():
        restore = asyncio.create_task(restoring())  # waits for the request inside
        await asyncio.sleep(0)
        queued = [asyncio.create_task(request(name)) for name in ("first", "second")]  # behind the restore
        await asyncio.sleep(0)
        assert entered == []
        restore.cancel()  # the restore's request went away: both are eligible now
        for _ in range(20):
            await asyncio.sleep(0.01)
        assert sorted(entered) == ["first", "second"]
    release.set()
    await asyncio.gather(*queued)


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


async def test_a_restore_refuses_when_running_work_does_not_stop_and_runs_on(tmp_path, monkeypatch):
    from backend.runs import Harness
    async with started(tmp_path / "data") as client:
        backup = (await client.post("/api/backups")).json()["id"]
        real = Harness.shutdown

        async def stuck(self, timeout=10.0):
            await real(self, timeout)
            return 1  # a task still running after the timeout

        monkeypatch.setattr(Harness, "shutdown", stuck)
        response = await client.post("/api/backups/restore", json={"generation": backup})
        assert (response.status_code, response.json()["code"]) == (409, "work_running")
        monkeypatch.setattr(Harness, "shutdown", real)
        assert (await client.post("/api/projects", json={"name": "After"})).status_code == 201  # running again


async def test_a_refused_restore_of_a_damaged_app_leaves_its_harness_stopped(tmp_path, monkeypatch):
    from backend.runs import Harness
    async with started(tmp_path / "data") as client:
        backup = (await client.post("/api/backups")).json()["id"]
        client.state["db"]._damaged = "integrity_check failed"  # as a backup's full check marks it
        real, resumed = Harness.shutdown, []

        async def stuck(self, timeout=10.0):
            await real(self, timeout)
            return 1  # a task still running after the timeout

        async def resume(self):
            resumed.append(True)

        monkeypatch.setattr(Harness, "shutdown", stuck)
        monkeypatch.setattr(Harness, "resume", resume)
        response = await client.post("/api/backups/restore", json={"generation": backup})
        assert (response.status_code, response.json()["code"]) == (409, "work_running")
        assert resumed == []  # nothing runs in a limited app
        assert (await client.get("/api/health")).json().get("database_damaged")


async def test_damage_found_while_a_restore_waits_for_work_to_stop_leaves_the_harness_stopped(tmp_path, monkeypatch):
    from backend.runs import Harness
    async with started(tmp_path / "data") as client:
        backup = (await client.post("/api/backups")).json()["id"]
        real, resumed, db = Harness.shutdown, [], client.state["db"]

        async def stuck(self, timeout=10.0):
            await real(self, timeout)
            db._damaged = "integrity_check failed"  # a backup let in before found it meanwhile
            return 1  # a task still running after the timeout

        async def resume(self):
            resumed.append(True)

        monkeypatch.setattr(Harness, "shutdown", stuck)
        monkeypatch.setattr(Harness, "resume", resume)
        response = await client.post("/api/backups/restore", json={"generation": backup})
        assert (response.status_code, response.json()["code"]) == (409, "work_running")
        assert resumed == []


async def test_background_runs_start_at_launch_only_once_the_launch_backup_has_checked_the_database(tmp_path,
                                                                                                    monkeypatch):
    from backend.runs import Harness
    kicked = []

    async def kick(self):
        kicked.append(True)

    def finds_damage(self):  # the launch's daily backup: its full check fails
        self._damaged = "integrity_check failed"

    monkeypatch.setattr(Harness, "kick_background", kick)
    monkeypatch.setattr(Database, "backup_if_due", finds_damage)
    async with started(tmp_path / "data", setup=False) as client:  # limited: setup is refused too
        assert kicked == []  # nothing runs on its own on a database found damaged
        assert (await client.get("/api/health")).json().get("database_damaged")
    monkeypatch.undo()
    monkeypatch.setattr(Harness, "kick_background", kick)
    async with started(tmp_path / "other"):
        assert kicked == [True]  # a sound database's runs start after its launch backup


async def test_damage_found_while_writes_are_released_leaves_the_harness_stopped(tmp_path, monkeypatch):
    from backend.runs import Harness
    async with started(tmp_path / "data") as client:
        backup = (await client.post("/api/backups")).json()["id"]
        real, real_release, resumed = Harness.shutdown, Database.release_writes, []

        async def stuck(self, timeout=10.0):
            await real(self, timeout)
            return 1  # a task still running after the timeout

        def release_then_damaged(self):
            real_release(self)
            self._damaged = "integrity_check failed"  # found by a check that ran meanwhile

        async def resume(self):
            resumed.append(True)

        monkeypatch.setattr(Harness, "shutdown", stuck)
        monkeypatch.setattr(Harness, "resume", resume)
        monkeypatch.setattr(Database, "release_writes", release_then_damaged)
        response = await client.post("/api/backups/restore", json={"generation": backup})
        assert (response.status_code, response.json()["code"]) == (409, "work_running")
        assert resumed == []


async def test_a_backup_that_finds_the_database_damaged_stops_the_work_running_on_it(tmp_path, monkeypatch):
    from backend.runs import Harness
    async with started(tmp_path / "data") as client:
        stopped = []
        real = Harness.shutdown

        async def shutdown(self, timeout=10.0):
            stopped.append(True)
            return await real(self, timeout)

        import backend.db.database as database_module

        def failing_check(path, check, **kwargs):
            raise database_module.DatabaseDamagedError(f"{check} failed: synthetic")

        monkeypatch.setattr(Harness, "shutdown", shutdown)
        monkeypatch.setattr(database_module, "_open_checked", failing_check)
        response = await client.post("/api/backups")  # Back up now: its full check fails
        assert (response.status_code, response.json()["code"]) == (409, "database_damaged")
        for _ in range(100):
            if stopped:
                break
            await asyncio.sleep(0.01)
        assert stopped and client.state["harness"].registry.closed  # nothing more is admitted or runs on it


async def test_a_backup_rotated_away_while_staged_is_still_restored(tmp_path, monkeypatch):
    async with started(tmp_path / "data") as client:
        backup = (await client.post("/api/backups")).json()["id"]
        real = backups_module._stage

        def staged_then_rotated(data_dir, body, staging):
            real(data_dir, body, staging)
            shutil.rmtree(data_dir / "backups" / body.generation)  # as retention may, before the swap

        monkeypatch.setattr(backups_module, "_stage", staged_then_rotated)
        assert (await client.post("/api/backups/restore", json={"generation": backup})).status_code == 200


async def test_a_listing_writing_its_audit_row_when_a_restore_starts_is_waited_for(tmp_path):
    data = tmp_path / "data"
    async with started(data) as client:
        backup = (await client.post("/api/backups")).json()["id"]
        client.provider.hold = asyncio.Event()
        listing = asyncio.create_task(client.get("/api/providers/openrouter/models"))
        await client.provider.started.wait()  # its request is out; its audit row is written
        restore = asyncio.create_task(client.post("/api/backups/restore", json={"generation": backup}))
        await asyncio.sleep(0.2)
        assert not restore.done()  # it waits for the listing to end before copying anything
        client.provider.hold.set()
        assert (await listing).status_code == 200
        response = await restore
        assert response.status_code == 200
    conn = sqlite3.connect((data / "backups" / response.json()["safety_copy"] / DB_NAME).as_uri() + "?mode=ro", uri=True)
    try:  # the listing's outbound audit row is in the safety copy, not lost with the old database
        assert conn.execute("SELECT count(*) FROM audit_log WHERE event = 'outbound'").fetchone()[0] >= 1
    finally:
        conn.close()


async def test_a_restore_refused_for_work_that_does_not_stop_leaves_that_work_with_its_harness(tmp_path, monkeypatch):
    from backend.runs import Harness
    async with started(tmp_path / "data") as client:
        backup = (await client.post("/api/backups")).json()["id"]
        harness, release = client.state["harness"], asyncio.Event()
        stuck = harness._detach(release.wait())  # detached work that outlasts the shutdown's wait
        real = Harness.shutdown
        monkeypatch.setattr(Harness, "shutdown", lambda self, timeout=10.0: real(self, 0.05))
        response = await client.post("/api/backups/restore", json={"generation": backup})
        assert (response.status_code, response.json()["code"]) == (409, "work_running")
        assert client.state["harness"] is harness and stuck in harness._tasks  # not replaced: it still owns the work
        assert not harness.registry.closed
        conversation = (await client.post("/api/conversations", json={"title": "t"})).json()["id"]
        assert (await send(client, conversation))[-1]["status"] == "succeeded"  # it admits turns again
        release.set()
        await stuck
        response = await client.post("/api/backups/restore", json={"generation": backup})
        assert response.status_code == 200, response.text  # nothing left running: it goes ahead


async def test_a_write_a_cancelled_task_left_running_never_lands_after_the_safety_copy(tmp_path, monkeypatch):
    import time
    data = tmp_path / "data"
    async with started(data) as client:
        backup = (await client.post("/api/backups")).json()["id"]
        db = client.state["db"]
        running, go, outcome = threading.Event(), threading.Event(), []

        def late_write():  # a worker its cancelled task no longer waits for, paused before its write
            running.set()
            go.wait(5)
            try:
                db.write(lambda conn: conn.execute("INSERT INTO audit_log (event, data) VALUES ('late', '{}')"))
                outcome.append("committed")
            except Exception as error:
                outcome.append(type(error).__name__)

        task = asyncio.create_task(asyncio.to_thread(late_write))
        await asyncio.to_thread(running.wait, 5)
        task.cancel()  # the task ends; its worker thread goes on
        real_backup = Database.backup

        def safety_copy_then_the_late_write(self, *args, **kwargs):
            generation = real_backup(self, *args, **kwargs)
            go.set()
            deadline = time.monotonic() + 5
            while not outcome and time.monotonic() < deadline:
                time.sleep(0.01)
            return generation

        monkeypatch.setattr(Database, "backup", safety_copy_then_the_late_write)
        response = await client.post("/api/backups/restore", json={"generation": backup})
        assert response.status_code == 200, response.text
    conn = sqlite3.connect((data / "backups" / response.json()["safety_copy"] / DB_NAME).as_uri() + "?mode=ro", uri=True)
    try:
        in_copy = conn.execute("SELECT count(*) FROM audit_log WHERE event = 'late'").fetchone()[0]
    finally:
        conn.close()
    assert outcome == ["DatabaseClosedError"] and in_copy == 0  # refused, never committed after the copy


async def test_a_restore_whose_safety_copy_fails_leaves_the_app_writing(tmp_path, monkeypatch):
    async with started(tmp_path / "data") as client:
        backup = (await client.post("/api/backups")).json()["id"]

        refused = []

        def failing(self, *args, **kwargs):
            try:  # writes are held from before the safety copy
                self.write(lambda conn: conn.execute("INSERT INTO audit_log (event, data) VALUES ('late', '{}')"))
            except Exception as error:
                refused.append(type(error).__name__)
            raise OSError("I/O error")

        monkeypatch.setattr(Database, "backup", failing)
        response = await client.post("/api/backups/restore", json={"generation": backup})
        assert (response.status_code, response.json()["code"]) == (500, "safety_copy_failed")
        assert refused == ["DatabaseClosedError"]
        assert (await client.post("/api/projects", json={"name": "After"})).status_code == 201  # admitted again


async def test_a_restore_that_fails_unexpectedly_before_the_swap_leaves_the_app_running(tmp_path, monkeypatch):
    async with started(tmp_path / "data") as client:
        backup = (await client.post("/api/backups")).json()["id"]
        harness, real, calls = client.state["harness"], backups_module._purges, []

        async def failing_the_second_time(state):  # the check made once every request is out
            calls.append(True)
            if len(calls) == 2:
                raise OSError("I/O error")
            return await real(state)

        monkeypatch.setattr(backups_module, "_purges", failing_the_second_time)
        response = await client.post("/api/backups/restore", json={"generation": backup})
        assert (response.status_code, response.json()["code"]) == (500, "restore_failed")
        assert not harness.registry.closed and not client.state["db"]._held
        assert (await client.post("/api/projects", json={"name": "After"})).status_code == 201


async def test_a_write_queued_before_a_restore_is_in_its_safety_copy(tmp_path):
    data = tmp_path / "data"
    async with started(data) as client:
        backup = (await client.post("/api/backups")).json()["id"]
        db = client.state["db"]
        writing, go = threading.Event(), threading.Event()

        def slow(conn):  # queued and running on the writer, its task cancelled
            writing.set()
            go.wait(5)
            conn.execute("INSERT INTO audit_log (event, data) VALUES ('queued', '{}')")

        task = asyncio.create_task(asyncio.to_thread(db.write, slow))
        await asyncio.to_thread(writing.wait, 5)
        task.cancel()
        restore = asyncio.create_task(client.post("/api/backups/restore", json={"generation": backup}))
        await asyncio.sleep(0.3)
        assert not restore.done()  # it waits for the write already queued
        go.set()
        response = await restore
        assert response.status_code == 200, response.text
    conn = sqlite3.connect((data / "backups" / response.json()["safety_copy"] / DB_NAME).as_uri() + "?mode=ro", uri=True)
    try:
        assert conn.execute("SELECT count(*) FROM audit_log WHERE event = 'queued'").fetchone()[0] == 1
    finally:
        conn.close()


async def test_a_title_run_a_refused_restore_stopped_runs_again(tmp_path, monkeypatch):
    from backend.runs import Harness
    async with started(tmp_path / "data") as client:
        backup = (await client.post("/api/backups")).json()["id"]
        harness, calling, calls = client.state["harness"], asyncio.Event(), []

        async def title(body):
            calls.append(body)
            if len(calls) == 1:  # the first title call waits until the restore's shutdown stops it
                calling.set()
                await asyncio.Event().wait()
            return client.provider.answer("Cohort studies")

        client.provider.title_replies = [title, title]
        conversation = (await client.post("/api/conversations", json={})).json()["id"]
        await send(client, conversation)
        await calling.wait()
        release = asyncio.Event()
        stuck = harness._detach(release.wait())  # other work that outlasts the shutdown's wait
        real = Harness.shutdown
        monkeypatch.setattr(Harness, "shutdown", lambda self, timeout=10.0: real(self, 0.2))
        response = await client.post("/api/backups/restore", json={"generation": backup})
        assert (response.status_code, response.json()["code"]) == (409, "work_running")
        release.set()
        await stuck
        await background_idle(client)
        assert len(calls) == 2  # the stopped title run started again on the same harness
        assert (await client.get(f"/api/conversations/{conversation}")).json()["title"] == "Cohort studies"


async def test_a_title_run_still_stopping_when_a_restore_is_refused_runs_again_once_stopped(tmp_path, monkeypatch):
    from backend.runs import Harness
    async with started(tmp_path / "data") as client:
        backup = (await client.post("/api/backups")).json()["id"]
        calling, calls = asyncio.Event(), []

        async def title(body):
            calls.append(body)
            if len(calls) == 1:  # the first title call takes longer to stop than the shutdown waits
                calling.set()
                try:
                    await asyncio.Event().wait()
                except asyncio.CancelledError:
                    await asyncio.sleep(0.5)
                    raise
            return client.provider.answer("Cohort studies")

        client.provider.title_replies = [title, title]
        conversation = (await client.post("/api/conversations", json={})).json()["id"]
        await send(client, conversation)
        await calling.wait()
        real = Harness.shutdown
        monkeypatch.setattr(Harness, "shutdown", lambda self, timeout=10.0: real(self, 0.1))
        response = await client.post("/api/backups/restore", json={"generation": backup})
        assert (response.status_code, response.json()["code"]) == (409, "work_running")
        await background_idle(client)
        assert len(calls) == 2  # started again once it had stopped, by the harness admitting again
        assert (await client.get(f"/api/conversations/{conversation}")).json()["title"] == "Cohort studies"


async def test_a_cancel_while_a_refused_restore_still_stops_a_title_run_ends_it(tmp_path, monkeypatch):
    from backend.runs import Harness
    async with started(tmp_path / "data") as client:
        backup = (await client.post("/api/backups")).json()["id"]
        calling, stopping, calls = asyncio.Event(), asyncio.Event(), []

        async def title(body):
            calls.append(body)
            calling.set()
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:  # its cleanup outlasts the shutdown's wait
                stopping.set()
                await asyncio.sleep(0.5)
                raise

        client.provider.title_replies = [title, title]
        conversation = (await client.post("/api/conversations", json={})).json()["id"]
        await send(client, conversation)
        await calling.wait()
        [run_id] = [run for run, active in client.state["harness"].registry.runs.items() if active.kind == "background"]
        real = Harness.shutdown
        monkeypatch.setattr(Harness, "shutdown", lambda self, timeout=10.0: real(self, 0.1))
        response = await client.post("/api/backups/restore", json={"generation": backup})
        assert (response.status_code, response.json()["code"]) == (409, "work_running")
        assert stopping.is_set() and run_id in client.state["harness"].registry.runs  # still stopping
        cancel = await client.post(f"/api/runs/{run_id}/cancel")  # the researcher's Cancel, admitted again
        assert cancel.json()["status"] == "cancelled"
        await background_idle(client)
        assert len(calls) == 1  # never started again
        [(status, reason)] = await rows(client, "SELECT status, cancel_reason FROM runs WHERE id = ?", run_id)
        assert (status, reason) == ("cancelled", "researcher")


async def test_the_backup_before_a_project_deletion_waits_for_a_restore_staging_its_backup(tmp_path, monkeypatch):
    data = tmp_path / "data"
    async with started(data) as client:
        project = (await client.post("/api/projects", json={"name": "Thesis"})).json()["id"]
        backup = (await client.post("/api/backups")).json()["id"]
        staging, release = hold(monkeypatch, backups_module, "_stage")
        restore = asyncio.create_task(client.post("/api/backups/restore", json={"generation": backup}))
        await asyncio.to_thread(staging.wait, 10)
        backed_up = threading.Event()
        real = backups_module.backup_before_deletion

        def backup_before_deletion(db):
            backed_up.set()
            return real(db)

        monkeypatch.setattr(backups_module, "backup_before_deletion", backup_before_deletion)
        deletion = asyncio.create_task(client.delete(f"/api/projects/{project}"))
        await asyncio.sleep(0.3)
        assert not backed_up.is_set()  # its retention could move the generation being staged
        release.set()
        assert (await restore).status_code == 200
        await deletion
        assert backed_up.is_set()


async def test_a_temporary_name_taken_by_someone_else_is_never_removed(tmp_path, monkeypatch):
    destination, theirs = tmp_path / "Backups", b"another writer's temporary file"
    destination.mkdir()
    monkeypatch.setattr(backups_module.uuid, "uuid4", lambda: uuid.UUID(int=0))  # the same temporary name
    source = tmp_path / "a.txt"
    source.write_bytes(b"x")

    def audit(path):  # just before it creates its temporary file, another writer takes that name
        (destination / f".{path.name}.00000000.tmp").write_bytes(theirs)

    with pytest.raises(FileExistsError):
        await asyncio.to_thread(backups_module._write_zip, destination, "scholia-backup", [("a.txt", source)],
                                None, stop=lambda: False, audit=audit)
    assert [p.read_bytes() for p in destination.iterdir()] == [theirs]  # theirs is left as it was


@pytest.mark.parametrize("hard_links", [True, False])
async def test_a_file_that_appears_at_a_backups_name_meanwhile_is_never_replaced(tmp_path, monkeypatch, hard_links):
    import errno
    destination, audited, theirs = tmp_path / "Backups", [], b"the researcher's own file"
    destination.mkdir()
    if not hard_links:  # such as an exFAT drive
        def no_links(source, target, **kwargs):
            raise OSError(errno.ENOTSUP, "Operation not supported")
        monkeypatch.setattr(backups_module.os, "link", no_links)

    def stop():  # while it is written, a file appears at the name it was to take
        if not (audited[0]).exists():
            audited[0].write_bytes(theirs)
        return False

    source = tmp_path / "a.txt"
    source.write_bytes(b"x")
    path = await asyncio.to_thread(backups_module._write_zip, destination, "scholia-backup", [("a.txt", source)],
                                   None, stop=stop, audit=audited.append)
    assert audited[0].read_bytes() == theirs and path != audited[0]
    assert audited == [audited[0], path]  # the name it took instead is recorded too
    with zipfile.ZipFile(path) as archive:
        assert archive.read("a.txt") == b"x" and mode(path) == 0o600
    assert sorted(p.name for p in destination.iterdir()) == sorted([audited[0].name, path.name])  # no temporary left
