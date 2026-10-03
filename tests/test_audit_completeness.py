"""The audit log (ticket 18; slice-1 spec sections 5 and 10): what it records, never content,
the view by project, export, and clearing, which needs confirmation and leaves a record.

The log is append-only in the database: a row never changes, and is deleted only behind a
later record of a clearing.
"""

import asyncio
import json
import sqlite3
import stat

import pytest

from backend import governance
from scholia_app import MockProvider, background_idle, send, started

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
        await client.post("/api/key-attestations", json={"provider": "openrouter", "statement": governance.KEY_STATEMENT})
        await client.post("/api/local-declarations", json={"provider": "local"})
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
        assert first["sent_off_this_mac"] == 2  # the answer and the title, to OpenRouter
        whole = (await client.get("/api/audit")).json()
        assert whole["sent_off_this_mac"] is None and len(whole["entries"]) > len(everything)


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
