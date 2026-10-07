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
from scholia_app import KEY, FakeKeyring, MockProvider, background_idle, confirm_key, send, started

pytestmark = pytest.mark.asyncio

MODEL = "example/zdr-model"
CATALOGS = Path(__file__).resolve().parents[1] / "frontend" / "src" / "i18n"
# Each statement version's digest over its texts in every catalog. A changed text needs a new
# version in backend/governance.py (which asks again) and its digest here.
STATEMENT_DIGESTS = {
    ("privacy.key.", governance.KEY_STATEMENT): "e8ee79b5ebe8740a679f6f22357ed6768a1235a45b1130d3414e0a090afcaba5",
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


async def confirm(client):
    return await confirm_key(client)


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
        assert await confirmation_status(client) == "missing"  # the change ended the provider's confirmations
        assert await refused_code(client, conversation) == (403, "key_not_confirmed")
        assert provider.chats == []
        assert (await client.put("/api/keys/openrouter", json={"key": KEY})).status_code == 200  # the first key again
        assert await confirmation_status(client) == "missing"
        assert await refused_code(client, conversation) == (403, "key_not_confirmed")


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
    ({"provider": "openrouter", "key": "0" * 16}, (409, "key_changed")),  # not the key the card showed
    ({"provider": "nobody"}, (404, "unknown_provider")),
    ({"provider": "local"}, (400, "not_openrouter")),
])
async def test_a_confirmation_needs_the_current_statement_and_the_openrouter_key_it_was_shown(tmp_path, body, code):
    async with started(tmp_path / "data") as client:
        current = (await client.get("/api/settings")).json()
        await client.put("/api/settings", json={"hash": current["hash"], "updates": {
            "providers.local.kind": "openai-compatible", "providers.local.base_url": "http://127.0.0.1:11434/v1"}})
        [shown] = [p for p in (await client.get("/api/providers")).json()["providers"] if p["name"] == "openrouter"]
        body = {"statement": governance.KEY_STATEMENT, "key": shown["key_confirmation"]["key"], **body}
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


async def test_a_confirmation_is_for_the_key_the_card_showed(tmp_path):
    async with started(tmp_path / "data") as client:
        [shown] = [p for p in (await client.get("/api/providers")).json()["providers"] if p["name"] == "openrouter"]
        assert (await client.put("/api/keys/openrouter", json={"key": "sk-or-replaced-meanwhile"})).status_code == 200
        response = await client.post("/api/key-attestations", json={
            "provider": "openrouter", "statement": shown["key_confirmation"]["statement"],
            "key": shown["key_confirmation"]["key"]})
        assert (response.status_code, response.json()["code"]) == (409, "key_changed")
        assert await rows(client, "SELECT count(*) FROM key_attestations") == [(0,)]
        assert KEY not in json.dumps(shown)  # the card's reference is not the key


async def test_a_private_title_run_resumed_after_a_restart_reads_the_catalog_first(tmp_path):
    from backend.runs import Harness
    data, keyring = tmp_path / "data", FakeKeyring()
    real = Harness.kick_background

    async def not_now(self):  # the title run is queued, then the app closes before it starts
        return None

    Harness.kick_background = not_now
    try:
        async with started(data, MockProvider(catalog=[MODEL], zero_retention=[MODEL]), keyring=keyring) as client:
            conversation = await setup_private(client)
            await confirm(client)
            assert (await send(client, conversation, model=MODEL))[-1]["status"] == "succeeded"
    finally:
        Harness.kick_background = real
    openrouter_client.clear_cache()  # a new launch has read no catalog
    provider = MockProvider(catalog=[MODEL], zero_retention=[MODEL])
    async with started(data, provider, keyring=keyring, setup=False) as client:
        await background_idle(client)
        assert [body["provider"] for body in provider.titles] == [{"zdr": True, "only": ["example"]}]
        assert await rows(client, "SELECT status FROM runs WHERE workflow = 'title'") == [("succeeded",)]


async def add_work(client, key=KEY):
    """A second OpenRouter entry, work, with key."""
    current = (await client.get("/api/settings")).json()
    await client.put("/api/settings", json={"hash": current["hash"], "updates": {
        "providers.work.kind": "openrouter", "providers.work.base_url": "https://openrouter.ai/api/v1",
        "providers.work.models": "all"}})
    assert (await client.put("/api/keys/work", json={"key": key})).status_code == 200


async def status_of(client, name):
    [shown] = [p for p in (await client.get("/api/providers")).json()["providers"] if p["name"] == name]
    return shown["key_confirmation"]["status"]


async def test_a_confirmation_is_for_one_provider_even_when_another_holds_the_same_key(tmp_path):
    # Two OpenRouter entries with the same key: a confirmation made through one is not the other's,
    # and a key changed away and back is asked about again while the other's confirmation stands.
    provider = MockProvider(catalog=[MODEL], zero_retention=[MODEL])
    async with started(tmp_path / "data", provider) as client:
        conversation = await setup_private(client)
        await add_work(client)
        assert (await confirm_key(client, "work")).status_code == 200
        assert await status_of(client, "openrouter") == "missing"
        assert await refused_code(client, conversation) == (403, "key_not_confirmed")
        assert (await confirm_key(client, "openrouter")).status_code == 200
        assert (await client.put("/api/keys/openrouter", json={"key": "sk-or-another-test-key"})).status_code == 200
        assert (await client.put("/api/keys/openrouter", json={"key": KEY})).status_code == 200  # the first key again
        assert (await status_of(client, "openrouter"), await status_of(client, "work")) == ("missing", "current")
        assert await refused_code(client, conversation) == (403, "key_not_confirmed")
        assert provider.chats == []


async def test_the_gate_checks_the_confirmation_of_the_provider_a_request_goes_through(tmp_path, monkeypatch):
    # Both entries confirmed the same key; the first one's confirmation ends after admission. Its
    # request is refused at the gate, though the other entry's confirmation of that key stands.
    from backend import runs
    provider = MockProvider(catalog=[MODEL], zero_retention=[MODEL])
    async with started(tmp_path / "data", provider) as client:
        conversation = await setup_private(client)
        await add_work(client)
        for name in ("openrouter", "work"):
            assert (await confirm_key(client, name)).status_code == 200
        real = runs.Harness._reserve

        async def unconfirmed_then_reserve(self, *args, **kwargs):
            await write(client, "DELETE FROM key_attestations WHERE provider = 'openrouter'")
            return await real(self, *args, **kwargs)

        monkeypatch.setattr(runs.Harness, "_reserve", unconfirmed_then_reserve)
        stream = await send(client, conversation, model=MODEL, provider="openrouter")
        assert (stream[-2]["code"], stream[-1]["status"]) == ("refused", "failed")
        assert provider.chats == []
        assert await rows(client, "SELECT data ->> 'reason' FROM audit_log WHERE event = 'outbound'"
                                  " AND data ->> 'decision' = 'deny'") == [("key_not_confirmed",)]
