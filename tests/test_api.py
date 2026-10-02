"""Projects, conversations, setup, providers, keys, settings and instructions through the API."""

import asyncio
import json
import stat

import pytest
import tomlkit

from backend.credentials import FALLBACK_FILE, SERVICE
from backend.db import new_id
from scholia_app import KEY, FakeKeyring, background_idle, send, started

pytestmark = pytest.mark.asyncio


async def rows(client, sql, *args):
    return await asyncio.to_thread(client.state["db"].read, lambda conn: conn.execute(sql, args).fetchall())


# Projects


async def test_a_new_project_gets_its_folder_defaults_and_an_audit_record(tmp_path):
    data = tmp_path / "data"
    async with started(data) as client:
        response = await client.post("/api/projects", json={"name": "Dissertation"})
        assert response.status_code == 201
        project = response.json()
        assert (project["name"], project["kind"], project["sensitivity"], project["review_lock"]) == (
            "Dissertation", "research", "normal", False)
        folder = data / "projects" / project["id"]
        config = tomlkit.parse((folder / "config.toml").read_text())
        assert config["project"] == {"citation_style": "apa7", "citation_style_zh": "gbt7714-2025",
                                     "template": "imrad", "budget_usd": 50}
        assert (folder / "AGENTS.md").read_bytes() == b""
        for path in (folder, folder / "config.toml", folder / "AGENTS.md"):
            assert stat.S_IMODE(path.stat().st_mode) == (0o700 if path.is_dir() else 0o600)
        assert await rows(client, "SELECT event, project_id FROM audit_log WHERE event = 'project_created'") == [
            ("project_created", project["id"])]
        listed = (await client.get("/api/projects")).json()["projects"]
        assert [p["kind"] for p in listed] == ["general", "research"]  # General first


@pytest.mark.parametrize("body", [{}, {"name": ""}, {"name": "x" * 201}, {"name": 3}])
async def test_a_project_needs_a_name(tmp_path, body):
    async with started(tmp_path / "data") as client:
        response = await client.post("/api/projects", json=body)
        assert (response.status_code, response.json()["code"]) == (400, "invalid_request")
        assert len((await client.get("/api/projects")).json()["projects"]) == 1


async def test_a_project_can_be_renamed_and_given_a_venue(tmp_path):
    async with started(tmp_path / "data") as client:
        project = (await client.post("/api/projects", json={"name": "Draft"})).json()["id"]
        changed = (await client.patch(f"/api/projects/{project}", json={"name": "Thesis", "target_venue": "APA"})).json()
        assert (changed["name"], changed["target_venue"]) == ("Thesis", "APA")
        assert (await client.patch(f"/api/projects/{new_id()}", json={"name": "x"})).status_code == 404
        assert (await client.patch(f"/api/projects/{project}", json={"name": None})).status_code == 400


async def test_deleting_a_project_removes_its_folder_and_records_but_never_general(tmp_path):
    data = tmp_path / "data"
    async with started(data) as client:
        project = (await client.post("/api/projects", json={"name": "Old"})).json()["id"]
        conversation = (await client.post("/api/conversations", json={"project_id": project})).json()["id"]
        await send(client, conversation)
        await background_idle(client)
        assert (await client.delete(f"/api/projects/{project}")).json() == {"ok": True}
        assert not (data / "projects" / project).exists()
        assert (await client.get(f"/api/projects/{project}")).status_code == 404
        assert await rows(client, "SELECT count(*) FROM budget_reservations") == [(0,)]  # no spending total outlives it
        assert await rows(client, "SELECT kind, title FROM tombstones WHERE object_id = ?", project) == [
            ("project", "Old")]
        [general] = (await client.get("/api/projects")).json()["projects"]
        response = await client.delete(f"/api/projects/{general['id']}")
        assert (response.status_code, response.json()["code"]) == (400, "general_project")
        assert (await client.delete(f"/api/projects/{new_id()}")).status_code == 404


# Conversations


async def test_conversations_live_in_a_project_general_by_default(tmp_path):
    async with started(tmp_path / "data") as client:
        [general] = (await client.get("/api/projects")).json()["projects"]
        project = (await client.post("/api/projects", json={"name": "Thesis"})).json()["id"]
        first = (await client.post("/api/conversations", json={})).json()
        second = (await client.post("/api/conversations", json={"project_id": project, "title": "Methods"})).json()
        assert (first["project_id"], first["title"], first["title_source"]) == (general["id"], None, None)
        assert (second["project_id"], second["title"], second["title_source"]) == (project, "Methods", "researcher")
        listed = (await client.get("/api/conversations", params={"project_id": project})).json()["conversations"]
        assert [c["id"] for c in listed] == [second["id"]]
        assert len((await client.get("/api/conversations")).json()["conversations"]) == 2
        assert (await client.post("/api/conversations", json={"project_id": new_id()})).status_code == 404
        assert (await client.get(f"/api/conversations/{new_id()}")).status_code == 404


async def test_a_rename_always_moves_title_rev_even_to_the_same_text(tmp_path):
    async with started(tmp_path / "data") as client:
        conversation = (await client.post("/api/conversations", json={"title": "Same"})).json()["id"]
        once = (await client.put(f"/api/conversations/{conversation}", json={"title": "Same"})).json()
        twice = (await client.put(f"/api/conversations/{conversation}", json={"title": "Same"})).json()
        assert (once["title_rev"], twice["title_rev"], twice["title_source"]) == (1, 2, "researcher")
        assert (await client.put(f"/api/conversations/{new_id()}", json={"title": "x"})).status_code == 404
        assert (await client.put(f"/api/conversations/{conversation}", json={"title": ""})).status_code == 400


async def test_deleting_a_conversation_keeps_its_spending_in_the_project(tmp_path):
    async with started(tmp_path / "data") as client:
        conversation = (await client.post("/api/conversations", json={})).json()["id"]
        await send(client, conversation)
        await background_idle(client)
        before = await rows(client, "SELECT sum(settled_usd) FROM budget_reservations")
        assert (await client.delete(f"/api/conversations/{conversation}")).json() == {"ok": True}
        assert await rows(client, "SELECT sum(settled_usd) FROM budget_reservations") == before
        assert await rows(client, "SELECT count(*) FROM budget_reservations WHERE run_id IS NOT NULL"
                                  " OR paying_conversation_id IS NOT NULL") == [(0,)]
        assert (await client.delete(f"/api/conversations/{conversation}")).status_code == 404


# Setup, providers and keys


async def test_first_run_setup_stores_the_key_in_the_credential_store_only(tmp_path):
    data, keyring = tmp_path / "data", FakeKeyring()
    async with started(data, keyring=keyring, setup=False) as client:
        assert (await client.get("/api/setup")).json() == {"needed": True}
        response = await client.post("/api/setup", json={"openrouter_key": KEY})
        assert response.json() == {"ok": True, "warning": None}
        assert (await client.get("/api/setup")).json() == {"needed": False}
        assert keyring.keys[(SERVICE, "openrouter")] == KEY
        assert KEY not in (data / "config.toml").read_text()
        assert not (data / FALLBACK_FILE).exists()
        assert (await client.get("/api/providers")).json() == {"providers": [
            {"name": "openrouter", "kind": "openrouter", "base_url": "https://openrouter.ai/api/v1", "has_key": True}]}


async def test_without_a_credential_store_the_key_goes_to_an_owner_only_file_with_a_warning(tmp_path):
    class Broken(FakeKeyring):
        def set_password(self, service, name, value):
            raise RuntimeError("no keychain")

    data = tmp_path / "data"
    async with started(data, keyring=Broken(), setup=False) as client:
        response = await client.post("/api/setup", json={"openrouter_key": KEY})
        assert response.json() == {"ok": True, "warning": "credential_store_unavailable"}
        assert json.loads((data / FALLBACK_FILE).read_text()) == {"openrouter": KEY}
        assert stat.S_IMODE((data / FALLBACK_FILE).stat().st_mode) == 0o600


async def test_several_providers_each_with_their_own_key(tmp_path):
    async with started(tmp_path / "data") as client:
        settings = (await client.get("/api/settings")).json()
        response = await client.put("/api/settings", json={"hash": settings["hash"], "updates": {
            "providers.lab.kind": "openai-compatible", "providers.lab.base_url": "http://127.0.0.1:11434/v1"}})
        assert response.status_code == 200, response.text
        assert (await client.put("/api/keys/lab", json={"key": "lab-key"})).json() == {"ok": True, "warning": None}
        assert (await client.put("/api/keys/nowhere", json={"key": "k"})).status_code == 404
        listed = {p["name"]: p["has_key"] for p in (await client.get("/api/providers")).json()["providers"]}
        assert listed == {"openrouter": True, "lab": True}
        # A turn can use the other provider by name, with a model chosen for it.
        stream = await send(client, (await client.post("/api/conversations", json={"title": "x"})).json()["id"],
                            provider="lab", model="llama")
        assert stream[-1]["status"] == "succeeded"
        assert client.provider.answers[-1]["model"] == "llama"
        # Auto cannot choose for a provider without a catalog of tiers.
        response = await client.post(f"/api/conversations/{(await client.post('/api/conversations', json={})).json()['id']}"
                                     "/message/stream", json={"content": "hi", "provider": "lab"})
        assert response.json()["code"] == "model_needed"


async def test_a_providers_models_come_from_its_catalog_through_the_gate(tmp_path):
    async with started(tmp_path / "data") as client:
        response = await client.get("/api/providers/openrouter/models")
        assert response.status_code == 200 and response.json()["models"] == []
        paths = [path for method, path, body in client.provider.requests]
        assert paths == ["/api/v1/models", "/api/v1/endpoints/zdr"]
        assert (await client.get("/api/providers/nowhere/models")).status_code == 404
        assert await rows(client, "SELECT count(*) FROM audit_log WHERE event = 'outbound'") == [(2,)]


# Settings and instructions


async def test_settings_are_saved_only_against_the_file_the_client_read(tmp_path):
    data = tmp_path / "data"
    async with started(data) as client:
        read = (await client.get("/api/settings")).json()
        assert read["values"]["ui"]["language"] == "system"
        saved = await client.put("/api/settings", json={"hash": read["hash"], "updates": {"ui.language": "zh-CN"}})
        assert saved.json()["values"]["ui"]["language"] == "zh-CN"
        stale = await client.put("/api/settings", json={"hash": read["hash"], "updates": {"ui.language": "en"}})
        assert (stale.status_code, stale.json()["code"]) == (409, "settings_changed")
        bad = await client.put("/api/settings", json={"hash": saved.json()["hash"], "updates": {"ui.language": "fr"}})
        assert (bad.status_code, bad.json()["code"]) == (400, "invalid_setting")
        secret = await client.put("/api/settings", json={"hash": saved.json()["hash"],
                                                          "updates": {"providers.x.api_key": "sk-123"}})
        assert secret.status_code == 400 and "sk-123" not in (data / "config.toml").read_text()


async def test_a_project_file_cannot_hold_providers_or_keys(tmp_path):
    async with started(tmp_path / "data") as client:
        project = (await client.post("/api/projects", json={"name": "P"})).json()["id"]
        read = (await client.get("/api/settings", params={"project_id": project})).json()
        assert read["values"]["project"]["budget_usd"] == 50
        response = await client.put("/api/settings", json={"project_id": project, "hash": read["hash"], "updates": {
            "providers.openrouter.base_url": "https://evil.example/v1"}})
        assert response.status_code == 400
        assert (await client.get("/api/settings", params={"project_id": new_id()})).status_code == 404


async def test_instructions_are_saved_owner_only_and_warned_at_the_cap(tmp_path):
    data = tmp_path / "data"
    async with started(data) as client:
        project = (await client.post("/api/projects", json={"name": "P"})).json()["id"]
        assert (await client.put("/api/instructions", json={"text": "Be brief."})).json() == {"ok": True, "warnings": []}
        assert (await client.get("/api/instructions")).json()["text"] == "Be brief."
        assert stat.S_IMODE((data / "AGENTS.md").stat().st_mode) == 0o600
        big = await client.put("/api/instructions", json={"project_id": project, "text": "x" * 40_000})
        assert any("32 KiB" in warning for warning in big.json()["warnings"])
        got = (await client.get("/api/instructions", params={"project_id": project})).json()
        assert got["text"] == "x" * 40_000 and got["cap_bytes"] == 32 * 1024


async def test_key_changes_are_audited_without_the_key(tmp_path):
    async with started(tmp_path / "data") as client:
        await client.put("/api/keys/openrouter", json={"key": "sk-or-second-key"})
        audited = await rows(client, "SELECT event, data FROM audit_log WHERE event = 'key_changed' ORDER BY seq")
        assert [json.loads(data) for _, data in audited] == [
            {"provider": "openrouter", "stored_in": "credential_store"}] * 2
        everything = json.dumps(await rows(client, "SELECT * FROM audit_log"))
        assert KEY not in everything and "sk-or-second-key" not in everything


async def test_a_cancelled_project_creation_keeps_the_folder_of_a_project_that_was_written(tmp_path, monkeypatch):
    import time
    from backend.db import Database
    data = tmp_path / "data"
    async with started(data) as client:
        real_write = Database.write
        writing = asyncio.Event()
        loop = asyncio.get_running_loop()

        def slow_write(self, fn):
            loop.call_soon_threadsafe(writing.set)
            time.sleep(0.3)
            return real_write(self, fn)

        monkeypatch.setattr(Database, "write", slow_write)
        request = asyncio.create_task(client.post("/api/projects", json={"name": "Raced"}))
        await asyncio.wait_for(writing.wait(), 5)
        request.cancel()
        with pytest.raises(asyncio.CancelledError):
            await request
        monkeypatch.undo()
        await asyncio.sleep(0.5)
        [(project,)] = await rows(client, "SELECT id FROM projects WHERE name = 'Raced'")
        assert (data / "projects" / project / "config.toml").is_file()
        assert (data / "projects" / project / "AGENTS.md").is_file()


async def test_a_provider_named_with_a_slash_can_be_given_a_key_and_listed(tmp_path):
    async with started(tmp_path / "data") as client:
        settings = (await client.get("/api/settings")).json()
        await client.put("/api/settings", json={"hash": settings["hash"], "updates": {
            '"providers"."lab/internal".kind': "openai-compatible",
            '"providers"."lab/internal".base_url': "http://127.0.0.1:9/v1"}})
        assert (await client.put("/api/keys/lab/internal", json={"key": "k"})).json() == {"ok": True, "warning": None}
        assert (await client.get("/api/providers/lab/internal/models")).status_code == 200
        listed = {p["name"]: p["has_key"] for p in (await client.get("/api/providers")).json()["providers"]}
        assert listed["lab/internal"] is True


@pytest.mark.parametrize("title", ["", "   "])
async def test_an_empty_title_is_no_title_so_one_is_generated(tmp_path, title):
    async with started(tmp_path / "data") as client:
        conversation = (await client.post("/api/conversations", json={"title": title})).json()
        assert (conversation["title"], conversation["title_source"]) == (None, None)
        await send(client, conversation["id"])
        await background_idle(client)
        assert (await client.get(f"/api/conversations/{conversation['id']}")).json()["title"] == "A short title"


async def test_instructions_cannot_recreate_the_folder_of_a_project_being_deleted(tmp_path, monkeypatch):
    import time
    from backend import app as app_module
    data = tmp_path / "data"
    async with started(data) as client:
        project = (await client.post("/api/projects", json={"name": "Going"})).json()["id"]
        real_delete = app_module.delete
        deleting = asyncio.Event()
        loop = asyncio.get_running_loop()

        def slow_delete(*args, **kwargs):
            loop.call_soon_threadsafe(deleting.set)
            time.sleep(0.3)
            return real_delete(*args, **kwargs)

        monkeypatch.setattr(app_module, "delete", slow_delete)
        deletion = asyncio.create_task(client.delete(f"/api/projects/{project}"))
        await deleting.wait()
        written = await client.put("/api/instructions", json={"project_id": project, "text": "secret notes"})
        assert (await deletion).json() == {"ok": True}
        assert written.status_code == 404
        assert not (data / "projects" / project).exists()


async def test_two_deletions_of_one_project_end_in_ok_and_not_found(tmp_path):
    async with started(tmp_path / "data") as client:
        project = (await client.post("/api/projects", json={"name": "Twice"})).json()["id"]
        first, second = await asyncio.gather(client.delete(f"/api/projects/{project}"),
                                             client.delete(f"/api/projects/{project}"))
        assert sorted([first.status_code, second.status_code]) == [200, 404]


async def test_a_project_folder_left_by_a_failed_removal_is_reported_and_removed_at_the_next_launch(tmp_path):
    import os
    data, keyring = tmp_path / "data", FakeKeyring()
    async with started(data, keyring=keyring) as client:
        project = (await client.post("/api/projects", json={"name": "Stuck"})).json()["id"]
        folder = data / "projects" / project
        os.chmod(folder, 0o500)  # its files cannot be removed now
        try:
            assert (await client.delete(f"/api/projects/{project}")).json() == {"ok": True, "files_left": True}
            assert (folder / "AGENTS.md").exists()
        finally:
            os.chmod(folder, 0o700)
    async with started(data, keyring=keyring, setup=False):
        assert not folder.exists()  # the tombstone named it; the next launch removed it
