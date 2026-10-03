"""The Private allowlist and what Private projects may send (slice-1 spec sections 6.4, 10 and 14).

Every Private request to OpenRouter carries provider.zdr = true and no plugins or server
tools; a model is offered only when OpenRouter lists a zero-retention endpoint for it and an
allowlist entry covers it; a non-OpenRouter cloud route is refused; a declared local server
is used without OpenRouter's flags or key confirmation. A step that cannot carry the
controls is refused before any request is sent, and before anything is written.
"""

import asyncio
import json

import pytest

from backend import governance, openrouter_client
from scholia_app import KEY, MockProvider, background_idle, confirm_key, declare, send, started

pytestmark = pytest.mark.asyncio

ZDR_MODEL = "example/zdr-model"
PLAIN_MODEL = "example/kept-model"  # OpenRouter lists it, with no zero-retention endpoint
LOCAL = "http://127.0.0.1:11434/v1"


@pytest.fixture(autouse=True)
def fresh_catalog():
    openrouter_client.clear_cache()
    yield
    openrouter_client.clear_cache()


def provider_for_private():
    return MockProvider(catalog=[ZDR_MODEL, PLAIN_MODEL], zero_retention=[ZDR_MODEL])


async def rows(client, sql, *args):
    return await asyncio.to_thread(client.state["db"].read, lambda conn: conn.execute(sql, args).fetchall())


async def save(client, updates):
    current = (await client.get("/api/settings")).json()
    response = await client.put("/api/settings", json={"hash": current["hash"], "updates": updates})
    assert response.status_code == 200, response.text


async def private_conversation(client, *, confirm=True, offer="all"):
    await save(client, {"providers.openrouter.models": offer})
    project = (await client.post("/api/projects", json={"name": "Interviews", "sensitivity": "private"})).json()["id"]
    if confirm:
        response = await confirm_key(client)
        assert response.status_code == 200, response.text
    conversation = (await client.post("/api/conversations", json={"project_id": project})).json()["id"]
    return project, conversation


async def test_every_private_request_to_openrouter_carries_zdr_and_no_plugins_or_server_tools(tmp_path):
    provider = provider_for_private()
    async with started(tmp_path / "data", provider) as client:
        _, conversation = await private_conversation(client)
        stream = await send(client, conversation, model=ZDR_MODEL, effort="high")
        assert stream[-1]["status"] == "succeeded"
        await background_idle(client)  # the title run goes the same way
        assert len(provider.chats) == 2
        for body in provider.chats:
            assert body["provider"]["zdr"] is True
            assert not {"plugins", "tools", "reasoning", "web_search_options"} & body.keys()
        decisions = await rows(client, "SELECT data ->> 'decision', data ->> 'sensitivity' FROM audit_log"
                                       " WHERE event = 'outbound' AND data ->> 'kind' = 'model_provider'"
                                       " AND project_id IS NOT (SELECT id FROM projects WHERE kind = 'general')")
        assert decisions == [("allow", "private")] * 2


async def test_a_model_without_a_zero_retention_endpoint_is_neither_offered_nor_sent(tmp_path):
    provider = provider_for_private()
    async with started(tmp_path / "data", provider) as client:
        project, conversation = await private_conversation(client)
        listing = (await client.get("/api/providers/openrouter/models", params={"project_id": project})).json()
        assert {m["id"]: (m["allowed"], m["refusal"]) for m in listing["models"]} == {
            ZDR_MODEL: (True, None), PLAIN_MODEL: (False, "private_route_not_allowed")}
        assert "allowed" not in (await client.get("/api/providers/openrouter/models")).json()["models"][0]
        before = await rows(client, "SELECT count(*) FROM runs")
        refused = await client.post(f"/api/conversations/{conversation}/message/stream",
                                    json={"content": "SECRET-INTERVIEW", "model": PLAIN_MODEL})
        assert (refused.status_code, refused.json()["code"]) == (403, "private_route_not_allowed")
        assert provider.chats == [] and await rows(client, "SELECT count(*) FROM runs") == before  # nothing written
        assert await rows(client, "SELECT count(*) FROM turns") == [(0,)]


async def test_auto_in_a_private_project_picks_only_a_model_it_allows(tmp_path):
    provider = provider_for_private()
    async with started(tmp_path / "data", provider) as client:
        _, conversation = await private_conversation(client, offer=[PLAIN_MODEL, ZDR_MODEL])
        assert (await send(client, conversation, model="auto"))[-1]["status"] == "succeeded"
        assert provider.answers[-1]["model"] == ZDR_MODEL
        await save(client, {"providers.openrouter.models": [PLAIN_MODEL]})
        refused = await client.post(f"/api/conversations/{conversation}/message/stream",
                                    json={"content": "hi", "model": "auto"})
        assert (refused.status_code, refused.json()["code"]) == (403, "private_route_not_allowed")
        await background_idle(client)


async def test_a_private_step_reads_the_catalog_it_needs_before_it_is_admitted(tmp_path):
    provider = provider_for_private()
    async with started(tmp_path / "data", provider) as client:
        _, conversation = await private_conversation(client)
        openrouter_client.clear_cache()  # as at a launch, before the picker read anything
        assert (await send(client, conversation, model=ZDR_MODEL))[-1]["status"] == "succeeded"
        await background_idle(client)


async def test_a_non_openrouter_cloud_route_is_refused_for_private(tmp_path):
    provider = provider_for_private()
    async with started(tmp_path / "data", provider) as client:
        await save(client, {"providers.cloud.kind": "openai-compatible",
                            "providers.cloud.base_url": "https://api.other-provider.example/v1",
                            "providers.cloud.models": "all"})
        assert (await client.put("/api/keys/cloud", json={"key": "sk-cloud"})).status_code == 200
        _, conversation = await private_conversation(client)
        refused = await client.post(f"/api/conversations/{conversation}/message/stream",
                                    json={"content": "hi", "model": "some-model", "provider": "cloud"})
        assert (refused.status_code, refused.json()["code"]) == (403, "route_not_allowed")
        assert provider.chats == []


async def test_a_declared_local_server_takes_private_requests_without_openrouters_flags_or_key(tmp_path):
    provider = provider_for_private()
    async with started(tmp_path / "data", provider) as client:
        await save(client, {"providers.local.kind": "openai-compatible", "providers.local.base_url": LOCAL,
                            "providers.local.models": "all"})
        assert (await client.put("/api/keys/local", json={"key": "local"})).status_code == 200
        _, conversation = await private_conversation(client, confirm=False)  # no OpenRouter confirmation
        refused = await client.post(f"/api/conversations/{conversation}/message/stream",
                                    json={"content": "hi", "model": "llama", "provider": "local"})
        assert (refused.status_code, refused.json()["code"]) == (403, "not_declared")  # loopback is only transport
        assert (await declare(client, "local")).status_code == 200
        stream = await send(client, conversation, model="llama", provider="local")
        assert stream[-1]["status"] == "succeeded"
        assert "provider" not in provider.answers[-1]
        await background_idle(client)


async def test_a_local_only_project_with_a_declared_server_uses_it_by_default(tmp_path):
    provider = provider_for_private()
    async with started(tmp_path / "data", provider) as client:
        await save(client, {"providers.local.kind": "openai-compatible", "providers.local.base_url": LOCAL,
                            "providers.local.models": ["llama"]})
        assert (await client.put("/api/keys/local", json={"key": "local"})).status_code == 200
        await declare(client, "local")
        project = (await client.post("/api/projects", json={"name": "Offline", "sensitivity": "local_only"})).json()["id"]
        conversation = (await client.post("/api/conversations", json={"project_id": project})).json()["id"]
        refused = await client.post(f"/api/conversations/{conversation}/message/stream",
                                    json={"content": "hi", "model": "x", "provider": "openrouter"})
        assert (refused.status_code, refused.json()["code"]) == (403, "route_not_allowed")
        assert (await send(client, conversation, model="auto"))[-1]["status"] == "succeeded"  # OpenRouter is passed over
        assert provider.answers[-1]["model"] == "llama"
        await background_idle(client)


async def test_the_shipped_list_is_dated_and_openrouter_only():
    shipped = governance.shipped()
    assert shipped["checked_on"] and shipped["entries"]
    for entry in shipped["entries"]:
        assert entry["route_key"].startswith("openrouter:")
        assert entry["required_flags"]["provider"]["zdr"] is True
        assert entry["allowed_features"] == [] and entry["terms_url"].startswith("https://openrouter.ai/")
        assert entry["checked_on"].startswith(shipped["checked_on"])


async def test_allowlist_edits_take_effect_at_once_and_are_audited(tmp_path):
    provider = MockProvider(catalog=[ZDR_MODEL, "example/other"], zero_retention=[ZDR_MODEL, "example/other"])
    async with started(tmp_path / "data", provider) as client:
        project, conversation = await private_conversation(client)
        listed = (await client.get("/api/private-routes")).json()
        assert [(r["route_key"], r["source"], r["enabled"], r["stale"]) for r in listed["routes"]] == [
            ("openrouter:*", "shipped", True, False)]

        # A researcher's exact entry, turned off, excludes its model; the rest stay allowed.
        changed = await client.put(f"/api/private-routes/openrouter:{ZDR_MODEL}", json={"enabled": False})
        assert [(r["route_key"], r["source"], r["enabled"]) for r in changed.json()["routes"]] == [
            ("openrouter:*", "shipped", True), (f"openrouter:{ZDR_MODEL}", "researcher", False)]
        refused = await client.post(f"/api/conversations/{conversation}/message/stream",
                                    json={"content": "hi", "model": ZDR_MODEL})
        assert refused.json()["code"] == "private_route_not_allowed"
        assert (await send(client, conversation, model="example/other"))[-1]["status"] == "succeeded"

        # The shipped entry turned off: nothing is allowed but an enabled exact entry.
        await client.put("/api/private-routes/openrouter:*", json={"enabled": False})
        await client.put(f"/api/private-routes/openrouter:{ZDR_MODEL}", json={"enabled": True})
        refused = await client.post(f"/api/conversations/{conversation}/message/stream",
                                    json={"content": "hi", "model": "example/other"})
        assert refused.json()["code"] == "private_route_not_allowed"
        assert (await send(client, conversation, model=ZDR_MODEL))[-1]["status"] == "succeeded"
        # A row for a shipped entry keeps the file's flags, whatever it holds.
        await asyncio.to_thread(client.state["db"].write, lambda conn: conn.execute(
            "UPDATE private_routes SET required_flags = '{}' WHERE route_key = 'openrouter:*'"))
        entry = next(r for r in (await client.get("/api/private-routes")).json()["routes"] if r["route_key"] == "openrouter:*")
        assert entry["required_flags"] == {"provider": {"zdr": True}}

        audit = await rows(client, "SELECT data FROM audit_log WHERE event = 'private_route_changed' ORDER BY seq")
        assert [json.loads(d)["route"] for (d,) in audit] == [
            f"openrouter:{ZDR_MODEL}", "openrouter:*", f"openrouter:{ZDR_MODEL}"]
        await background_idle(client)


async def test_only_openrouter_routes_can_be_added_and_an_unknown_one_is_not_rechecked(tmp_path):
    async with started(tmp_path / "data") as client:
        for key in ("cloud:model", "openrouter:", "openrouter: padded", "model", "openrouter:SECRET note",
                    "openrouter:模型", "openrouter:" + "x" * 201):
            response = await client.put(f"/api/private-routes/{key}", json={"enabled": True})
            assert (response.status_code, response.json()["code"]) == (400, "not_openrouter_route"), key
        response = await client.put("/api/private-routes/openrouter:x/unknown", json={"rechecked": True})
        assert response.status_code == 404
        assert await rows(client, "SELECT count(*) FROM private_routes") == [(0,)]


async def test_an_entry_checked_over_six_months_ago_is_flagged_until_it_is_checked_again(tmp_path):
    async with started(tmp_path / "data") as client:
        db = client.state["db"]
        shipped_on = governance.shipped()["entries"][0]["checked_on"]
        later = governance.months_later(shipped_on, 7)

        def entry(conn):
            [found] = governance.allowlist(conn, now=later)
            return found["stale"], found["checked_on"]

        assert await asyncio.to_thread(db.read, entry) == (True, shipped_on)
        response = await client.put("/api/private-routes/openrouter:*", json={"rechecked": True})
        assert response.status_code == 200
        [(checked,)] = await rows(client, "SELECT checked_on FROM list_checks WHERE list = 'private_routes'"
                                          " AND entry_id = 'openrouter:*'")
        assert checked >= shipped_on and response.json()["routes"][0]["checked_on"] == checked
        rechecked = governance.months_later(shipped_on, 3)  # as if the re-check had been then
        await asyncio.to_thread(db.write, lambda conn: conn.execute("UPDATE list_checks SET checked_on = ?", (rechecked,)))
        assert await asyncio.to_thread(db.read, entry) == (False, rechecked)


async def test_the_gate_reads_the_allowlist_and_the_catalog_itself(tmp_path):
    # The same rules hold where every request passes: a route the selector would not show is
    # refused at the gate, before it is sent, even if admission were wrong.
    provider = provider_for_private()
    async with started(tmp_path / "data", provider) as client:
        project, _ = await private_conversation(client)
        await client.get("/api/providers/openrouter/models")  # the catalog, as read
        inputs = client.state["gate"]._inputs()
        db = client.state["db"]
        found = await asyncio.to_thread(db.read, lambda conn: [inputs.private_route(conn, "openrouter", KEY, model)
                                                                for model in (ZDR_MODEL, PLAIN_MODEL)])
        assert (found[0]["route_key"], found[0]["required_flags"], found[1]) == (
            "openrouter:*", {"provider": {"zdr": True}}, None)
        assert project


async def attempt_terms(client):
    rows_ = await rows(client, "SELECT r.workflow, e.data FROM run_events e JOIN runs r ON r.id = e.run_id"
                               " WHERE e.type = 'model_attempt' ORDER BY r.started_at, e.seq")
    return [(workflow, json.loads(data).get("retention")) for workflow, data in rows_]


def applied(entry):
    return {"route_key": entry["route_key"], "terms_url": entry["terms_url"], "checked_on": entry["checked_on"]}


async def test_each_attempt_records_the_retention_terms_the_gate_applied(tmp_path):
    # Ticket 18's provenance: each call's route and its retention terms, as the gate decided them.
    provider = provider_for_private()
    async with started(tmp_path / "data", provider) as client:
        await save(client, {"providers.local.kind": "openai-compatible", "providers.local.base_url": LOCAL,
                            "providers.local.models": "all"})
        await client.put("/api/keys/local", json={"key": "local"})
        await declare(client, "local")
        _, conversation = await private_conversation(client)
        await send(client, conversation, model=ZDR_MODEL)
        await send(client, conversation, model="llama", provider="local")
        normal = (await client.post("/api/conversations", json={"title": "Plain"})).json()["id"]
        await send(client, normal, model=ZDR_MODEL)
        await background_idle(client)
        shipped = applied(governance.shipped()["entries"][0])
        [(declared_at,)] = await rows(client, "SELECT declared_at FROM local_declarations")
        [(until,)] = await rows(client, "SELECT expires_at FROM key_attestations")
        private = {"level": "private", "zero_retention": True, "allowlist": [shipped], "key_confirmed_until": until}
        assert await attempt_terms(client) == [
            ("agent", private),
            ("title", private),
            ("agent", {"level": "private", "declared_origin": "http://127.0.0.1:11434", "declared_at": declared_at}),
            ("agent", {"level": "normal"}),
        ]


async def test_the_terms_recorded_are_those_applied_at_dispatch_not_at_admission(tmp_path, monkeypatch):
    # Admitted under the shipped entry; an entry for the model itself, and a new declaration of the
    # local server, land before the dispatch: each attempt records what was applied then.
    from backend import runs
    provider = provider_for_private()
    async with started(tmp_path / "data", provider) as client:
        await save(client, {"providers.local.kind": "openai-compatible", "providers.local.base_url": LOCAL,
                            "providers.local.models": "all"})
        await client.put("/api/keys/local", json={"key": "local"})
        await declare(client, "local")
        _, conversation = await private_conversation(client)
        real = runs.Harness._reserve
        changes = [lambda: client.put(f"/api/private-routes/openrouter:{ZDR_MODEL}", json={"enabled": True}),
                   lambda: declare(client, "local")]

        async def changed_then_reserve(self, *args, **kwargs):
            if changes:
                await changes.pop(0)()
            return await real(self, *args, **kwargs)

        monkeypatch.setattr(runs.Harness, "_reserve", changed_then_reserve)
        await send(client, conversation, model=ZDR_MODEL, content="no title for this one")
        await send(client, conversation, model="llama", provider="local")
        await background_idle(client)
        exact = next(e for e in (await client.get("/api/private-routes")).json()["routes"]
                     if e["route_key"] == f"openrouter:{ZDR_MODEL}")
        [(declared_at,)] = await rows(client, "SELECT declared_at FROM local_declarations")
        [(until,)] = await rows(client, "SELECT expires_at FROM key_attestations")
        terms = [t for workflow, t in await attempt_terms(client) if workflow == "agent"]
        assert terms == [
            {"level": "private", "zero_retention": True, "allowlist": [applied(exact)], "key_confirmed_until": until},
            {"level": "private", "declared_origin": "http://127.0.0.1:11434", "declared_at": declared_at},
        ]


async def test_the_gate_reads_the_catalog_of_the_provider_and_key_a_request_goes_through(tmp_path, monkeypatch):
    # Two OpenRouter entries with their own keys. Between admission and dispatch, the second one's
    # catalog, read again, no longer lists a zero-retention endpoint for the model: its request is
    # refused at the gate, whatever the first one's catalog says.
    from backend import runs
    provider = provider_for_private()
    async with started(tmp_path / "data", provider) as client:
        await save(client, {"providers.work.kind": "openrouter", "providers.work.base_url": "https://openrouter.ai/api/v1",
                            "providers.work.models": "all"})
        assert (await client.put("/api/keys/work", json={"key": "sk-or-work-test-key"})).status_code == 200
        _, conversation = await private_conversation(client)
        assert (await confirm_key(client, "work")).status_code == 200
        for name in ("openrouter", "work"):
            assert ZDR_MODEL in {m["id"] for m in (await client.get(f"/api/providers/{name}/models")).json()["models"]}
        real = runs.Harness._reserve

        async def refreshed_then_reserve(self, *args, **kwargs):
            [work] = [state for (name, *_), state in openrouter_client._caches.items() if name == "work"]
            work["models"] = {**work["models"], ZDR_MODEL: {**work["models"][ZDR_MODEL], "supports_zdr": False}}
            return await real(self, *args, **kwargs)

        monkeypatch.setattr(runs.Harness, "_reserve", refreshed_then_reserve)
        stream = await send(client, conversation, model=ZDR_MODEL, provider="work")
        await background_idle(client)
        assert provider.chats == [] and stream[-1]["status"] == "failed"
        assert {reason for (reason,) in await rows(
            client, "SELECT data ->> 'reason' FROM audit_log WHERE event = 'outbound'"
                    " AND data ->> 'decision' = 'deny'")} == {"route_not_allowed"}
