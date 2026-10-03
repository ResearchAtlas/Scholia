"""What the settings pages and the model picker read and write (slice-1 spec F12, sections
4.4 and 8; the stage walk's Settings): providers turned off, the models a provider offers,
model windows, effort steps, the researcher's recent models, and instruction files that
changed since an editor read them."""

import asyncio

import httpx
import pytest

from backend import budget_router, openrouter_client, providers
from backend.providers import MIN_WINDOW, Provider
from scholia_app import MockProvider, send, started

pytestmark = pytest.mark.asyncio

OPENROUTER = Provider("openrouter", "openrouter", "https://openrouter.ai/api/v1")
LOCAL = Provider("local", "openai-compatible", "http://127.0.0.1:11434/v1")
RECOMMENDED = sorted(budget_router.RECOMMENDED)


class CatalogProvider(MockProvider):
    """A MockProvider whose model listing holds `rows`."""

    def __init__(self, rows):
        super().__init__()
        self.rows = rows

    async def __call__(self, request):
        if request.url.path.endswith("/models"):
            self.requests.append((request.method, request.url.path, None))
            return httpx.Response(200, json={"data": self.rows})
        return await super().__call__(request)


@pytest.fixture(autouse=True)
def fresh_catalog():
    openrouter_client.clear_cache()
    yield
    openrouter_client.clear_cache()


async def save(client, updates, project_id=None):
    current = (await client.get("/api/settings", params={"project_id": project_id} if project_id else {})).json()
    return await client.put("/api/settings", json={"hash": current["hash"], "updates": updates,
                                                   **({"project_id": project_id} if project_id else {})})


@pytest.mark.parametrize("table, model, reported, expected", [
    ({}, "m", 128000, (128000, None, 128000, "ok")),
    ({"default_window": 32768}, "m", 128000, (128000, 32768, 32768, "ok")),  # a setting lowers a report
    ({"windows": {"m": 262144}}, "m", 128000, (128000, 262144, 128000, "ok")),  # never raises it
    ({"default_window": 8192, "windows": {"m": 16384}}, "m", None, (None, 16384, 16384, "ok")),  # the model's wins
    ({"default_window": 8192}, "m", None, (None, 8192, 8192, "ok")),  # fills a window not reported
    ({}, "m", None, (None, None, None, "needed")),
    ({}, "m", 2048, (2048, None, 2048, "too_small")),
    ({"windows": {"m": 8192}}, "m", 2048, (2048, 8192, 2048, "too_small")),
    ({}, "m", "128000", (None, None, None, "needed")),  # a reported value that is not a number
])
async def test_a_models_window_follows_section_8(table, model, reported, expected):
    found = providers.window(table, model, reported)
    assert (found["reported"], found["setting"], found["in_use"], found["status"]) == expected
    assert MIN_WINDOW == 4096


async def test_the_models_a_provider_offers():
    some = RECOMMENDED[0]
    assert providers.offered({}, OPENROUTER, some, budget_router.RECOMMENDED)
    assert not providers.offered({}, OPENROUTER, "x/other", budget_router.RECOMMENDED)  # Recommended by default
    assert providers.offered({"models": "all"}, OPENROUTER, "x/other", budget_router.RECOMMENDED)
    assert providers.offered({}, LOCAL, "llama3", budget_router.RECOMMENDED)  # All by default elsewhere
    assert providers.offered({"models": ["llama3"]}, LOCAL, "llama3", budget_router.RECOMMENDED)
    assert not providers.offered({"models": ["llama3"]}, LOCAL, "qwen", budget_router.RECOMMENDED)


async def test_provider_model_choice_and_off_are_validated(tmp_path):
    async with started(tmp_path / "data") as client:
        for good in ("recommended", "all", ["a/b", "c/d"]):
            assert (await save(client, {"providers.openrouter.models": good})).status_code == 200, good
        for bad in ("some", 3, [""]):
            response = await save(client, {"providers.openrouter.models": bad})
            assert (response.status_code, response.json()["code"]) == (400, "invalid_setting"), bad
        assert (await save(client, {"providers.openrouter.enabled": "no"})).json()["code"] == "invalid_setting"


async def test_a_provider_turned_off_is_listed_but_never_called(tmp_path):
    provider = MockProvider()
    async with started(tmp_path / "data", provider) as client:
        assert (await save(client, {"providers.openrouter.enabled": False})).status_code == 200
        listed = (await client.get("/api/providers")).json()["providers"]
        assert [(p["name"], p["has_key"], p["enabled"]) for p in listed] == [("openrouter", True, False)]
        conversation = (await client.post("/api/conversations", json={})).json()["id"]
        response = await client.post(f"/api/conversations/{conversation}/message/stream", json={"content": "hi"})
        assert (response.status_code, response.json()["code"]) == (400, "no_provider")
        assert provider.chats == []
        assert providers.gate_inputs(tmp_path / "data").provider_urls == ()  # its origin is not allowed
        # Its key can still be replaced, and it comes back on.
        assert (await client.put("/api/keys/openrouter", json={"key": "sk-or-test-other"})).status_code == 200
        assert (await save(client, {"providers.openrouter.enabled": True})).status_code == 200
        assert (await client.get("/api/providers")).json()["providers"][0]["enabled"] is True


async def test_the_model_listing_says_window_offer_and_effort(tmp_path):
    rows = [{"id": RECOMMENDED[0], "context_length": 128000}, {"id": "x/unlisted", "context_length": 2048},
            {"id": "x/no-window"}]
    async with started(tmp_path / "data", CatalogProvider(rows)) as client:
        assert (await save(client, {"providers.openrouter.windows": {"x/no-window": 16384}})).status_code == 200
        models = {m["id"]: m for m in (await client.get("/api/providers/openrouter/models")).json()["models"]}
        first = models[RECOMMENDED[0]]
        assert (first["window"]["in_use"], first["window"]["status"]) == (128000, "ok")
        assert first["offered"] and first["recommended"]
        assert first["effort"] == {"surface": "unknown", "steps": []}  # not yet checked: "Default"
        assert models["x/unlisted"]["window"]["status"] == "too_small"
        assert not models["x/unlisted"]["offered"]  # OpenRouter offers Recommended until told otherwise
        assert models["x/no-window"]["window"] == {"reported": None, "setting": 16384, "in_use": 16384, "status": "ok"}


async def test_recent_models_are_the_last_three_chosen(tmp_path):
    async with started(tmp_path / "data") as client:
        conversation = (await client.post("/api/conversations", json={})).json()["id"]
        for model in ("a/one", "b/two", "a/one", "c/three", None, "d/four"):
            await send(client, conversation, **({"model": model} if model else {}))  # None is Auto
            await asyncio.sleep(0.01)  # distinct start times
        recent = (await client.get("/api/models/recent")).json()["models"]
        assert recent == [{"provider": "openrouter", "model": m} for m in ("d/four", "c/three", "a/one")]


async def test_an_instruction_file_changed_since_it_was_read_is_not_overwritten(tmp_path):
    async with started(tmp_path / "data") as client:
        read = (await client.get("/api/instructions")).json()
        assert (read["text"], read["combined_bytes"]) == ("", 0)
        saved = await client.put("/api/instructions", json={"text": "Cite sources.", "hash": read["hash"]})
        assert saved.status_code == 200
        (tmp_path / "data" / "AGENTS.md").write_text("Edited by hand.")
        stale = await client.put("/api/instructions", json={"text": "Mine.", "hash": saved.json()["hash"]})
        assert (stale.status_code, stale.json()["code"]) == (409, "settings_changed")
        assert (tmp_path / "data" / "AGENTS.md").read_text() == "Edited by hand."
        fresh = (await client.get("/api/instructions")).json()
        assert fresh["text"] == "Edited by hand." and fresh["combined_bytes"] == len("Edited by hand.")
        assert (await client.put("/api/instructions", json={"text": "Mine."})).status_code == 200  # no hash: as before
