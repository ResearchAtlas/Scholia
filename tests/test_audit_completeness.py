"""The audit log (ticket 18; slice-1 spec sections 5 and 10): what it records, never content,
the view by project, export, and clearing, which needs confirmation and leaves a record.

The log is append-only in the database: a row never changes, and is deleted only behind a
later record of a clearing.
"""

import asyncio
import json
import re
import sqlite3
import stat
from pathlib import Path

import pytest

from backend import governance
from scholia_app import MockProvider, background_idle, confirm_key, declare, send, started

pytestmark = pytest.mark.asyncio

SECRET = "SECRET-CANARY"
LOCAL = "http://127.0.0.1:11434/v1"
# Every kind of event this milestone records (outbound decisions from S1-04, deletions from S1-02).
EVENTS = {
    "project_created", "sensitivity_changed", "review_lock_changed", "key_changed", "key_attested",
    "local_declared", "local_declaration_withdrawn", "private_route_changed", "conversation_moved",
    "deletion", "outbound", "audit_exported",
}


async def rows(client, sql, *args):
    return await asyncio.to_thread(client.state["db"].read, lambda conn: conn.execute(sql, args).fetchall())


async def confirm_change(client, method, path, body):
    """Send a change that needs confirmation: once for the token, then with it."""
    first = await client.request(method, path, json=body)
    assert (first.status_code, first.json()["code"]) == (409, "confirmation_required")
    return await client.request(method, path, json={**body, "token": first.json()["token"]})


async def test_every_governance_change_is_audited_without_content(tmp_path):
    provider = MockProvider()
    async with started(tmp_path / "data", provider) as client:
        current = (await client.get("/api/settings")).json()
        await client.put("/api/settings", json={"hash": current["hash"], "updates": {
            "providers.local.kind": "openai-compatible", "providers.local.base_url": LOCAL}})
        await client.put("/api/keys/local", json={"key": SECRET})
        project = (await client.post("/api/projects", json={"name": SECRET})).json()["id"]
        other = (await client.post("/api/projects", json={"name": SECRET, "sensitivity": "private"})).json()["id"]
        conversation = (await client.post("/api/conversations", json={"project_id": project, "title": SECRET})).json()["id"]
        await send(client, conversation, content=SECRET)
        await client.post(f"/api/conversations/{conversation}/move", json={"project_id": other})
        await client.post(f"/api/projects/{project}/sensitivity", json={"level": "private"})
        await confirm_change(client, "POST", f"/api/projects/{project}/sensitivity", {"level": "normal"})
        await client.post(f"/api/projects/{project}/review-lock", json={"locked": True, "venue": SECRET})
        await confirm_change(client, "POST", f"/api/projects/{project}/review-lock", {"locked": False})
        assert (await confirm_key(client)).status_code == 200
        assert (await declare(client, "local")).status_code == 200
        await client.delete("/api/local-declarations/local")
        await client.put("/api/private-routes/openrouter:x/model", json={"enabled": True})
        await client.delete(f"/api/conversations/{conversation}")
        await client.post("/api/audit/export", json={"project_id": project})
        await background_idle(client)

        logged = await rows(client, "SELECT event, project_id, data FROM audit_log")
        assert EVENTS <= {event for event, _, _ in logged}
        assert SECRET not in json.dumps(logged)
        for event, project_id, data in logged:
            assert isinstance(json.loads(data), dict)
        by_project = {event for event, project_id, _ in logged if project_id == project}
        assert {"project_created", "sensitivity_changed", "review_lock_changed", "audit_exported"} <= by_project
        assert_named(logged)


async def test_the_view_pages_the_log_by_project_and_counts_what_left_this_mac(tmp_path):
    async with started(tmp_path / "data") as client:
        project = (await client.post("/api/projects", json={"name": "P"})).json()["id"]
        conversation = (await client.post("/api/conversations", json={"project_id": project})).json()["id"]
        await send(client, conversation)
        await background_idle(client)
        for _ in range(3):
            await client.post(f"/api/projects/{project}/sensitivity", json={"level": "local_only"})
            await confirm_change(client, "POST", f"/api/projects/{project}/sensitivity", {"level": "normal"})

        first = (await client.get("/api/audit", params={"project_id": project, "limit": 4})).json()
        assert len(first["entries"]) == 4 and first["next"] == first["entries"][-1]["seq"]
        seqs = [e["seq"] for e in first["entries"]]
        assert seqs == sorted(seqs, reverse=True)  # newest first
        rest = (await client.get("/api/audit", params={"project_id": project, "before": first["next"]})).json()
        assert rest["next"] is None and all(e["seq"] < first["next"] for e in rest["entries"])
        everything = first["entries"] + rest["entries"]
        assert {e["project_id"] for e in everything} == {project}
        assert [e["event"] for e in everything][-1] == "project_created"
        assert first["allowed_off_this_mac"] == 2  # the answer and the title, to OpenRouter
        whole = (await client.get("/api/audit")).json()
        assert whole["allowed_off_this_mac"] is None and len(whole["entries"]) > len(everything)


async def test_an_export_is_an_owner_only_file_in_the_data_folder_and_is_audited(tmp_path):
    async with started(tmp_path / "data") as client:
        project = (await client.post("/api/projects", json={"name": "P"})).json()["id"]
        exported = (await client.post("/api/audit/export", json={"project_id": project})).json()
        path = tmp_path / "data" / "exports" / exported["path"].rsplit("/", 1)[1]
        assert exported["path"] == str(path) and exported["rows"] == 1
        assert stat.S_IMODE(path.stat().st_mode) == 0o600
        assert stat.S_IMODE(path.parent.stat().st_mode) == 0o700
        content = json.loads(path.read_text())
        assert content["format"] == "scholia-audit-log" and [e["event"] for e in content["entries"]] == ["project_created"]
        [(data,)] = await rows(client, "SELECT data FROM audit_log WHERE event = 'audit_exported'")
        assert json.loads(data) == {"destination": f"exports/{path.name}", "rows": 1}
        assert (await client.post("/api/audit/export")).json()["rows"] > 1  # the whole log
        # A project's export holds every clearing of the log, as its view does.
        token = (await client.delete("/api/audit")).json()["token"]
        await client.delete("/api/audit", params={"token": token})
        again = (await client.post("/api/audit/export", json={"project_id": project})).json()
        content = json.loads(Path(again["path"]).read_text())
        assert [e["event"] for e in content["entries"]] == ["audit_cleared"]
        unknown = await client.post("/api/audit/export", json={"project_id": "SECRET not an id"})
        assert unknown.status_code == 404  # only an existing project's id goes in the record
        assert "SECRET" not in json.dumps(await rows(client, "SELECT * FROM audit_log"))


async def test_clearing_needs_confirmation_and_leaves_a_record(tmp_path):
    async with started(tmp_path / "data") as client:
        for name in ("A", "B"):
            await client.post("/api/projects", json={"name": name})
        first = await client.delete("/api/audit")
        assert (first.status_code, first.json()["code"]) == (409, "confirmation_required")
        assert (await client.delete("/api/audit", params={"token": "made-up"})).status_code == 409
        (count,) = (await rows(client, "SELECT count(*) FROM audit_log"))[0]
        cleared = await client.delete("/api/audit", params={"token": first.json()["token"]})
        assert cleared.json() == {"ok": True, "rows": count}
        assert [(e, json.loads(d)) for e, d in await rows(client, "SELECT event, data FROM audit_log")] == [
            ("audit_cleared", {"rows": count})]
        reused = await client.delete("/api/audit", params={"token": first.json()["token"]})
        assert reused.json()["code"] == "confirmation_required"

        # A later clearing keeps the earlier one's record, and a project's view shows both.
        project = (await client.post("/api/projects", json={"name": "C"})).json()["id"]
        again = (await client.delete("/api/audit")).json()["token"]
        await client.delete("/api/audit", params={"token": again})
        assert [e for (e,) in await rows(client, "SELECT event FROM audit_log ORDER BY seq")] == [
            "audit_cleared", "audit_cleared"]
        view = (await client.get("/api/audit", params={"project_id": project})).json()
        assert [e["event"] for e in view["entries"]] == ["audit_cleared", "audit_cleared"]


async def test_the_database_keeps_the_log_append_only(tmp_path):
    async with started(tmp_path / "data") as client:
        await client.post("/api/projects", json={"name": "P"})
        db = client.state["db"]
        before = await rows(client, "SELECT * FROM audit_log")
        for sql in ("UPDATE audit_log SET data = '{}'", "DELETE FROM audit_log"):
            with pytest.raises(sqlite3.IntegrityError):
                await asyncio.to_thread(db.write, lambda conn, sql=sql: conn.execute(sql))
        assert await rows(client, "SELECT * FROM audit_log") == before


async def test_an_export_is_recorded_before_its_file_is_written_and_a_failure_leaves_no_file(tmp_path, monkeypatch):
    from backend import app as app_module
    async with started(tmp_path / "data") as client:
        real = app_module.write_private

        def written_then_failed(path, data):
            real(path, data)
            raise OSError("the folder could not be synced")

        monkeypatch.setattr(app_module, "write_private", written_then_failed)
        response = await client.post("/api/audit/export")
        assert (response.status_code, response.json()["code"]) == (500, "export_failed")
        [(data,)] = await rows(client, "SELECT data FROM audit_log WHERE event = 'audit_exported'")
        destination = tmp_path / "data" / json.loads(data)["destination"]
        assert not destination.exists()  # the record of an attempt stays; no unrecorded file does


async def test_clearing_records_are_never_deleted(tmp_path):
    async with started(tmp_path / "data") as client:
        for _ in range(2):
            token = (await client.delete("/api/audit")).json()["token"]
            await client.delete("/api/audit", params={"token": token})
        db = client.state["db"]
        for which in ("min", "max"):  # the earlier one, older than the latest clearing, as well
            with pytest.raises(sqlite3.IntegrityError):
                await asyncio.to_thread(db.write, lambda conn, which=which: conn.execute(
                    f"DELETE FROM audit_log WHERE seq = (SELECT {which}(seq) FROM audit_log)"))
        assert await rows(client, "SELECT event FROM audit_log") == [("audit_cleared",), ("audit_cleared",)]


CATALOGS = Path(__file__).resolve().parents[1] / "frontend" / "src" / "i18n"


def catalogs():
    return {name: json.loads((CATALOGS / name).read_text(encoding="utf-8")) for name in ("en.json", "zh-CN.json")}


def assert_named(logged):
    """Each event logged, and each field of its details, has its text in every catalog (an
    outbound decision's details are shown by their own texts, checked below)."""
    for name, catalog in catalogs().items():
        assert {e for e, _, _ in logged if f"audit.event.{e}" not in catalog} == set(), name
        assert {key for e, _, data in logged if e != "outbound" for key in json.loads(data)
                if f"audit.field.{key}" not in catalog} == set(), name


def written_events():
    """Every event name the backend writes to the audit log, read from its source."""
    found = set()
    for path in (Path(__file__).resolve().parents[1] / "backend").rglob("*.py"):
        source = path.read_text(encoding="utf-8")
        found |= set(re.findall(r"INSERT INTO audit_log \(event[^)]*\) (?:VALUES \(|SELECT )'(\w+)'", source))
        found |= set(re.findall(r"record\(conn, \"(\w+)\"", source))
    return found


async def test_every_event_the_backend_writes_has_a_heading_in_each_interface_language():
    events = written_events()
    assert EVENTS | {"audit_cleared", "full_backup", "project_export", "restore", "backup_purge"} <= events
    for name, catalog in catalogs().items():
        assert {e for e in events if f"audit.event.{e}" not in catalog} == set(), name


async def test_backups_restores_exports_and_purges_are_logged_with_text_for_each_field(tmp_path):
    destination = tmp_path / "chosen"
    destination.mkdir()
    async with started(tmp_path / "data") as client:
        project = (await client.post("/api/projects", json={"name": "Study"})).json()["id"]
        await client.post("/api/conversations", json={"project_id": project})
        assert (await client.post("/api/backups/full", json={"destination": str(destination)})).status_code == 200
        exported = await client.post(f"/api/projects/{project}/export", json={"destination": str(destination)})
        assert exported.status_code == 200
        backup = (await client.post("/api/backups")).json()["id"]
        assert (await client.post("/api/backups/restore", json={"generation": backup})).status_code == 200
        assert (await client.delete(f"/api/projects/{project}", params={"purge_backups": "true"})).status_code == 200
        logged = await rows(client, "SELECT event, project_id, data FROM audit_log")
        assert {"full_backup", "project_export", "restore", "backup_purge"} <= {e for e, _, _ in logged}
        assert_named(logged)


async def test_every_refusal_reason_and_destination_kind_is_named_in_each_interface_language():
    # The audit view shows the gate's refusals and destination kinds through the catalogs.
    from backend.outbound_gate import Kind
    from test_outbound_gate import REASONS
    root = Path(__file__).resolve().parents[1] / "frontend" / "src" / "i18n"
    for name in ("en.json", "zh-CN.json"):
        catalog = json.loads((root / name).read_text(encoding="utf-8"))
        assert {r for r in REASONS | {"local_server_not_yours"} if f"audit.reason.{r}" not in catalog} == set(), name
        assert {k.value for k in Kind if f"audit.kind.{k.value}" not in catalog} == set(), name


# Clearing keeps the purge records a restore compares


async def clear_log(client):
    token = (await client.delete("/api/audit")).json()["token"]
    return await client.delete("/api/audit", params={"token": token})


def pause_staging(monkeypatch):
    """Hold a restore once its backup is staged, before anything is replaced: (staged, go) events."""
    import threading
    import backend.backups as backups_module
    real, staged, go = backups_module._stage, threading.Event(), threading.Event()

    def stage_then_wait(*args):
        real(*args)
        staged.set()
        assert go.wait(10)

    monkeypatch.setattr(backups_module, "_stage", stage_then_wait)
    return staged, go


async def test_a_log_cleared_while_a_restore_stages_its_backup_keeps_the_restore_valid(tmp_path, monkeypatch):
    # An earlier "Delete everywhere" left one backup, which is restored; the log is cleared between
    # staging and replacement. The purge record stays, so the restore sees no purge since it began.
    async with started(tmp_path / "data") as client:
        gone = (await client.post("/api/projects", json={"name": "Gone"})).json()["id"]
        assert (await client.delete(f"/api/projects/{gone}", params={"purge_backups": "true"})).status_code == 200
        [backup] = [b["id"] for b in (await client.get("/api/backups")).json()["backups"]]
        staged, go = pause_staging(monkeypatch)
        restore = asyncio.create_task(client.post("/api/backups/restore", json={"generation": backup}))
        assert await asyncio.to_thread(staged.wait, 5)
        assert (await clear_log(client)).status_code == 200
        assert await rows(client, "SELECT event FROM audit_log ORDER BY seq") == [("backup_purge",), ("audit_cleared",)]
        with pytest.raises(sqlite3.IntegrityError):  # nor can anything else delete it
            await asyncio.to_thread(client.state["db"].write, lambda conn: conn.execute(
                "DELETE FROM audit_log WHERE event = 'backup_purge'"))
        go.set()
        assert (await restore).status_code == 200


async def test_a_purge_during_a_restore_is_not_hidden_by_clearing_the_log(tmp_path, monkeypatch):
    # A restore stages a backup holding project Kept. Meanwhile Kept is deleted everywhere, and the
    # purge cannot remove the staged copy (staging_left); then the log is cleared. The restore still
    # finds the purge, and refuses: the staged copy of deleted data is never put in place.
    import threading
    import backend.backups as backups_module
    async with started(tmp_path / "data") as client:
        kept = (await client.post("/api/projects", json={"name": "Kept"})).json()["id"]
        backup = (await client.post("/api/backups")).json()["id"]
        staged, go = pause_staging(monkeypatch)
        real_remove = backups_module._remove

        def remove(path):
            if backups_module.STAGING in Path(path).parts:
                raise OSError("I/O error")
            real_remove(path)

        monkeypatch.setattr(backups_module, "_remove", remove)
        db, clearing, cleared = client.state["db"], threading.Event(), threading.Event()
        real_write = db.write

        def write_when_let(fn):  # the clearing is admitted with the deletion, and writes after the purge
            if fn.__qualname__.endswith("clear_audit.<locals>.clear"):
                clearing.set()
                assert cleared.wait(10)
            return real_write(fn)

        monkeypatch.setattr(db, "write", write_when_let)
        restore = asyncio.create_task(client.post("/api/backups/restore", json={"generation": backup}))
        assert await asyncio.to_thread(staged.wait, 5)
        deletion = asyncio.create_task(client.delete(f"/api/projects/{kept}", params={"purge_backups": "true"}))
        token = (await client.delete("/api/audit")).json()["token"]
        clear = asyncio.create_task(client.delete("/api/audit", params={"token": token}))
        assert await asyncio.to_thread(clearing.wait, 5)
        go.set()
        assert (await deletion).json()["staging_left"] is True  # the staged copy is still there
        cleared.set()
        assert (await clear).status_code == 200
        assert (await restore).status_code == 404  # a purge came between: not this backup
        assert kept not in {p["id"] for p in (await client.get("/api/projects")).json()["projects"]}
