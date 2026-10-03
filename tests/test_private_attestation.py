"""The OpenRouter key confirmation for Private projects (slice-1 spec section 6.4).

A Private project refuses an OpenRouter key without a current confirmation of its data
settings: one that is missing, expired, of an earlier statement, or of another key. The
confirmation is recorded in the audit log, with a salted fingerprint of the key, never the
key, and lasts six months.
"""

import asyncio
import hashlib
import json
import stat
from pathlib import Path

import pytest

from backend import governance, openrouter_client
from scholia_app import KEY, MockProvider, background_idle, send, started

pytestmark = pytest.mark.asyncio

MODEL = "example/zdr-model"
CATALOGS = Path(__file__).resolve().parents[1] / "frontend" / "src" / "i18n"
# Each statement version's digest over its texts in every catalog. A changed text needs a new
# version in backend/governance.py (which asks again) and its digest here.
STATEMENT_DIGESTS = {
    ("privacy.key.", governance.KEY_STATEMENT): "620e5ca07776fb1592c0c2430cc10cceafb933dbc7fcafc7ac52f2ae9d0b8b38",
    ("privacy.local.", governance.LOCAL_STATEMENT): "c83761d9b76d1cb6894fa6eca407a55d6cd54216de6d105bf7ce1e76a12076b5",
}


@pytest.fixture(autouse=True)
def fresh_catalog():
    openrouter_client.clear_cache()
    yield
    openrouter_client.clear_cache()


async def rows(client, sql, *args):
    return await asyncio.to_thread(client.state["db"].read, lambda conn: conn.execute(sql, args).fetchall())


async def write(client, sql, *args):
    await asyncio.to_thread(client.state["db"].write, lambda conn: conn.execute(sql, args))


async def setup_private(client):
    current = (await client.get("/api/settings")).json()
    await client.put("/api/settings", json={"hash": current["hash"], "updates": {"providers.openrouter.models": "all"}})
    project = (await client.post("/api/projects", json={"name": "Interviews", "sensitivity": "private"})).json()["id"]
    return (await client.post("/api/conversations", json={"project_id": project})).json()["id"]


async def confirm(client, statement=governance.KEY_STATEMENT, provider="openrouter"):
    return await client.post("/api/key-attestations", json={"provider": provider, "statement": statement})


async def refused_code(client, conversation):
    response = await client.post(f"/api/conversations/{conversation}/message/stream",
                                 json={"content": "hi", "model": MODEL})
    return response.status_code, response.json().get("code")


async def confirmation_status(client):
    [openrouter] = [p for p in (await client.get("/api/providers")).json()["providers"] if p["name"] == "openrouter"]
    return openrouter["key_confirmation"]["status"]


async def test_without_a_confirmation_a_private_project_refuses_the_key_before_anything_is_sent(tmp_path):
    provider = MockProvider(catalog=[MODEL], zero_retention=[MODEL])
    async with started(tmp_path / "data", provider) as client:
        conversation = await setup_private(client)
        assert await confirmation_status(client) == "missing"
        assert await refused_code(client, conversation) == (403, "key_not_confirmed")
        assert provider.chats == [] and await rows(client, "SELECT count(*) FROM turns") == [(0,)]
        assert (await confirm(client)).status_code == 200
        assert await confirmation_status(client) == "current"
        assert (await send(client, conversation, model=MODEL))[-1]["status"] == "succeeded"
        await background_idle(client)


async def test_a_confirmation_is_recorded_with_a_fingerprint_never_the_key(tmp_path):
    async with started(tmp_path / "data") as client:
        assert (await confirm(client)).status_code == 200
        [(fingerprint, statement, confirmed, expires)] = await rows(
            client, "SELECT key_fingerprint, statement, confirmed_at, expires_at FROM key_attestations")
        assert statement == governance.KEY_STATEMENT
        assert expires == governance.months_later(confirmed, 6)
        assert KEY not in fingerprint and fingerprint != hashlib.sha256(KEY.encode()).hexdigest()  # salted
        salt = tmp_path / "data" / governance.SALT_FILE
        assert stat.S_IMODE(salt.stat().st_mode) == 0o600
        [(data,)] = await rows(client, "SELECT data FROM audit_log WHERE event = 'key_attested'")
        assert json.loads(data) == {"provider": "openrouter", "statement": governance.KEY_STATEMENT, "expires_at": expires}
        dumped = json.dumps(await rows(client, "SELECT * FROM audit_log")) + json.dumps(
            await rows(client, "SELECT * FROM key_attestations"))
        assert KEY not in dumped


@pytest.mark.parametrize("change, status", [
    ("UPDATE key_attestations SET expires_at = '2020-01-01T00:00:00.000Z'", "expired"),
    ("UPDATE key_attestations SET statement = '2020-01-01'", "outdated"),  # an earlier statement
])
async def test_an_expired_confirmation_or_one_of_an_earlier_statement_refuses_the_key(tmp_path, change, status):
    provider = MockProvider(catalog=[MODEL], zero_retention=[MODEL])
    async with started(tmp_path / "data", provider) as client:
        conversation = await setup_private(client)
        await confirm(client)
        await write(client, change)
        assert await confirmation_status(client) == status
        assert await refused_code(client, conversation) == (403, "key_not_confirmed")
        assert provider.chats == []
        await confirm(client)  # asked again
        assert (await send(client, conversation, model=MODEL))[-1]["status"] == "succeeded"
        await background_idle(client)


async def test_a_new_key_needs_its_own_confirmation(tmp_path):
    provider = MockProvider(catalog=[MODEL], zero_retention=[MODEL])
    async with started(tmp_path / "data", provider) as client:
        conversation = await setup_private(client)
        await confirm(client)
        assert (await client.put("/api/keys/openrouter", json={"key": "sk-or-another-test-key"})).status_code == 200
        assert await confirmation_status(client) == "other_key"
        assert await refused_code(client, conversation) == (403, "key_not_confirmed")
        assert provider.chats == []


async def test_the_gate_refuses_a_key_whose_confirmation_lapsed_after_admission(tmp_path, monkeypatch):
    from backend import runs
    provider = MockProvider(catalog=[MODEL], zero_retention=[MODEL])
    async with started(tmp_path / "data", provider) as client:
        conversation = await setup_private(client)
        await confirm(client)
        real = runs.Harness._reserve

        async def lapse_then_reserve(self, *args, **kwargs):  # the confirmation expires before the dispatch
            await write(client, "UPDATE key_attestations SET expires_at = '2020-01-01T00:00:00.000Z'")
            return await real(self, *args, **kwargs)

        monkeypatch.setattr(runs.Harness, "_reserve", lapse_then_reserve)
        stream = await send(client, conversation, model=MODEL)
        assert (stream[-2]["code"], stream[-1]["status"]) == ("refused", "failed")
        assert provider.chats == []
        assert await rows(client, "SELECT data ->> 'reason' FROM audit_log WHERE event = 'outbound'"
                                  " AND data ->> 'decision' = 'deny'") == [("key_not_confirmed",)]


@pytest.mark.parametrize("body, code", [
    ({"provider": "openrouter", "statement": "2020-01-01"}, (409, "statement_changed")),
    ({"provider": "nobody", "statement": governance.KEY_STATEMENT}, (404, "unknown_provider")),
    ({"provider": "local", "statement": governance.KEY_STATEMENT}, (400, "not_openrouter")),
])
async def test_a_confirmation_needs_the_current_statement_and_an_openrouter_key(tmp_path, body, code):
    async with started(tmp_path / "data") as client:
        current = (await client.get("/api/settings")).json()
        await client.put("/api/settings", json={"hash": current["hash"], "updates": {
            "providers.local.kind": "openai-compatible", "providers.local.base_url": "http://127.0.0.1:11434/v1"}})
        response = await client.post("/api/key-attestations", json=body)
        assert (response.status_code, response.json()["code"]) == code
        assert await rows(client, "SELECT count(*) FROM key_attestations") == [(0,)]


async def test_six_months_is_counted_in_calendar_months():
    assert governance.months_later("2026-08-31T10:00:00.000Z", 6) == "2027-02-28T10:00:00.000Z"
    assert governance.months_later("2026-10-03T00:00:00.000Z", -6) == "2026-04-03T00:00:00.000Z"
    assert governance.months_later("2027-08-31T00:00:00.000Z", 6) == "2028-02-29T00:00:00.000Z"


@pytest.mark.parametrize("prefix, version", list(STATEMENT_DIGESTS))
async def test_a_statement_changes_only_with_its_version(prefix, version):
    texts = []
    for name in ("en.json", "zh-CN.json"):
        catalog = json.loads((CATALOGS / name).read_text(encoding="utf-8"))
        texts += [f"{key}={catalog[key]}" for key in sorted(catalog) if key.startswith(prefix)]
    digest = hashlib.sha256("\n".join(texts).encode()).hexdigest()
    assert texts and digest == STATEMENT_DIGESTS[prefix, version], (
        f"the {prefix} statement changed: give it a new version in backend/governance.py and record {digest}")
