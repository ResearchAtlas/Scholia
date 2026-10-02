# Carried over from AI Advisory Board, tests/test_model_catalog.py at commit
# b5d687820e88c10de25a9a2343d3cc478e497524, with backend/openrouter_client.py, adapted
# to its per-provider catalog: the provider, key and client are passed in, and the
# current app's curated registry and model-selection routes are left out.
"""Offline catalog checks: discovery, refresh, isolation, and money/selection boundaries."""

import asyncio

import httpx
import pytest

from backend import budget_router
from backend import openrouter_client as catalog
from backend.providers import Provider, Route

OPENROUTER = Provider("openrouter", "openrouter", "https://openrouter.ai/api/v1")
GENERIC = Provider("lab", "openai-compatible", "https://llm.example.org/v1")
KEY = "catalog-test-key"


@pytest.fixture(autouse=True)
def isolated_catalog():
    catalog.clear_cache()
    yield
    catalog.clear_cache()


def client_for(handler):
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


def state(provider=OPENROUTER, key=KEY):
    return catalog._cache_state(provider, key)


@pytest.mark.asyncio
async def test_discovery_updates_prices_and_feeds_the_estimate():
    rows = [{"id": "new/reasoner", "supported_parameters": ["reasoning", "tools"],
             "pricing": {"prompt": "0", "completion": "0"}, "context_length": 32000}]

    def serve(request):
        assert request.headers["authorization"] == f"Bearer {KEY}"
        return httpx.Response(200, json={"data": [] if request.url.path.endswith("/zdr") else rows})

    client = client_for(serve)
    models = await catalog.models(client, OPENROUTER, KEY)
    model = models["new/reasoner"]
    assert (model["supports_reasoning"], model["reasoning_extraction"], model["supports_tools"]) == (True, "field", True)
    assert (model["context_length"], model["supports_zdr"]) == (32000, False)
    route = Route(OPENROUTER, "new/reasoner")
    assert budget_router._model_call_cost(route, 1_000_000, 1_000_000) == 0
    rows[0]["pricing"] = {"prompt": "0.000003", "completion": "0.000008"}
    state()["last_attempt"] = 0
    await catalog.models(client, OPENROUTER, KEY, force=True)
    assert budget_router._model_call_cost(route, 1_000_000, 1_000_000) == pytest.approx(11)
    assert budget_router.cost_from_usage(route, {"prompt_tokens": 1_000_000, "completion_tokens": 1_000_000}) == 11
    # A model the catalog does not list is priced with the conservative unknown-price heuristic.
    assert budget_router._model_call_cost(Route(OPENROUTER, "unlisted"), 1_000_000, 1_000_000) == 6


@pytest.mark.asyncio
async def test_failure_preserves_catalog_and_empty_success_is_authoritative():
    response = {"data": [{"id": "a"}]}

    def serve(request):
        return httpx.Response(200, json={"data": []} if request.url.path.endswith("/zdr") else response)

    client = client_for(serve)
    assert "a" in await catalog.models(client, OPENROUTER, KEY)
    response = {"error": "not a models catalog"}
    state()["last_attempt"] = 0
    assert "a" in await catalog.models(client, OPENROUTER, KEY, force=True)
    status = catalog.catalog_status(OPENROUTER, KEY)
    assert status["stale"] and status["error"] == "refresh_failed"
    response = {"data": []}
    state()["last_attempt"] = 0
    assert await catalog.models(client, OPENROUTER, KEY, force=True) == {}
    assert not catalog.catalog_status(OPENROUTER, KEY)["stale"]


@pytest.mark.asyncio
async def test_zdr_failure_is_visible_and_retries_after_the_cooldown(monkeypatch):
    now = [1000.0]
    monkeypatch.setattr(catalog.time, "time", lambda: now[0])
    zdr_ok = True
    requests = []

    def serve(request):
        requests.append(request.url.path)
        if request.url.path.endswith("/zdr"):
            return httpx.Response(200, json={"data": [{"model_id": "model/a"}]}) if zdr_ok else httpx.Response(503)
        return httpx.Response(200, json={"data": [{"id": "model/a"}]})

    client = client_for(serve)
    assert (await catalog.models(client, OPENROUTER, KEY))["model/a"]["supports_zdr"] is True
    zdr_ok = False
    now[0] += 30
    rows = await catalog.models(client, OPENROUTER, KEY, force=True)
    assert rows["model/a"]["supports_zdr"] is None  # unverified, never assumed
    assert catalog.catalog_status(OPENROUTER, KEY)["error"] == "zdr_unverified"
    zdr_ok = True
    now[0] += 29
    await catalog.models(client, OPENROUTER, KEY, force=True)
    assert len(requests) == 4  # even a forced refresh waits for the cooldown
    now[0] += 1
    await catalog.models(client, OPENROUTER, KEY)  # recovers before the hour is up
    assert len(requests) == 6 and catalog.catalog_status(OPENROUTER, KEY)["error"] is None


@pytest.mark.asyncio
async def test_single_flight_and_a_key_change_invalidates_the_generation(monkeypatch):
    calls = 0
    entered, release = asyncio.Event(), asyncio.Event()

    async def fetch(client, url, id_field, key):
        nonlocal calls
        calls += 1
        if id_field == "id":
            entered.set()
            await release.wait()
            return [{"id": "old-account"}]
        return []

    monkeypatch.setattr(catalog, "_fetch_catalog_rows", fetch)
    client = client_for(lambda request: httpx.Response(500))
    a = asyncio.create_task(catalog.models(client, OPENROUTER, KEY))
    await entered.wait()
    b = asyncio.create_task(catalog.models(client, OPENROUTER, KEY, force=True))
    await asyncio.sleep(0)
    release.set()
    assert await a == await b
    await catalog.models(client, OPENROUTER, KEY, force=True)
    assert calls == 2  # one /models and one zero-retention list, once
    assert catalog.get_model_metadata(Route(OPENROUTER, "old-account")) is not None
    # Another key is another account's catalog: the old one is dropped.
    entered.clear()
    release.clear()
    a = asyncio.create_task(catalog.models(client, OPENROUTER, "second-key"))
    await entered.wait()
    assert catalog.get_model_metadata(Route(OPENROUTER, "old-account")) is None
    catalog.clear_cache()  # a change during the fetch discards its result
    release.set()
    assert await a is None


@pytest.mark.asyncio
async def test_pagination_completeness_and_no_redirect_credentials():
    requests = []

    def serve(request):
        requests.append(request)
        if request.url.params.get("after") == "a":
            return httpx.Response(200, json={"data": [{"id": "b"}], "has_more": False, "total": 2})
        return httpx.Response(200, json={"data": [{"id": "a"}], "has_more": True, "last_id": "a"})

    url = f"{OPENROUTER.base_url}/models"
    assert [row["id"] for row in await catalog._fetch_catalog_rows(client_for(serve), url, "id", KEY)] == ["a", "b"]
    assert len(requests) == 2
    requests.clear()

    def redirect(request):
        requests.append(request)
        return httpx.Response(302, headers={"Location": "https://untrusted.example/models"})

    assert await catalog._fetch_catalog_rows(client_for(redirect), url, "id", KEY) is None
    assert len(requests) == 1
    for body in ({"data": [], "total": 1}, {"data": [{"id": "a"}], "next_cursor": "x"},
                 {"data": [{"id": "a"}, {"id": "a"}]}, {"data": [None]}, {"data": [{"id": " a"}]},
                 {"data": [{"id": "a\n"}]}):
        assert await catalog._fetch_catalog_rows(client_for(lambda r, b=body: httpx.Response(200, json=b)),
                                                 url, "id", KEY) is None


@pytest.mark.asyncio
async def test_provider_count_and_links_cannot_publish_an_incomplete_catalog():
    response = {"data": [{"id": "kept"}], "total_count": 1, "links": {"next": None}}
    requests = []

    def serve(request):
        requests.append(request)
        return httpx.Response(200, json={"data": []} if request.url.path.endswith("/zdr") else response)

    client = client_for(serve)
    assert set(await catalog.models(client, OPENROUTER, KEY)) == {"kept"}
    for fields in ({"total_count": 1}, {"total_count": True}, {"total_count": "0"}, {"total_count": None},
                   {"total_count": -1}, {"total_count": 0, "total": 1},
                   {"links": {"next": "https://untrusted.example/models"}}, {"links": {"next": ""}},
                   {"links": []}, {"links": None}):
        response = {"data": [], **fields}
        requests.clear()
        state()["last_attempt"] = 0
        assert set(await catalog.models(client, OPENROUTER, KEY, force=True)) == {"kept"}
        assert catalog.catalog_status(OPENROUTER, KEY)["stale"]
        assert len(requests) == 1  # never follow a provider link or publish partial rows


def test_price_unknown_is_distinct_from_zero_and_does_not_cross_providers():
    for invalid in (None, "", " ", True, -1, "NaN", "Infinity", "1e308"):
        parsed = catalog.parse_model({"id": "m", "pricing": {"prompt": invalid, "completion": "0"}}, OPENROUTER)
        assert parsed["pricing_source"] == "unknown" and parsed["pricing"]["input"] is None
    parsed = catalog.parse_model({"id": "free", "pricing": {"prompt": "0", "completion": "0"}}, OPENROUTER)
    assert parsed["pricing_source"] == "provider" and parsed["pricing"] == {"input": 0, "output": 0}
    parsed = catalog.parse_model({"id": "generic", "pricing": {"prompt": "1", "completion": "2"}}, GENERIC)
    assert parsed["pricing_source"] == "unknown" and parsed["supports_zdr"] is False


@pytest.mark.asyncio
async def test_generic_discovery_skips_zero_retention_and_keeps_providers_apart():
    def serve(request):
        assert not request.url.path.endswith("/zdr")
        return httpx.Response(200, json={"data": [{"id": "local/new"},
                                                  {"id": "image/only", "architecture": {"output_modalities": ["image"]}}]})

    models = await catalog.models(client_for(serve), GENERIC, KEY)
    assert models["local/new"]["supports_zdr"] is False and models["image/only"]["text_output"] is False
    assert catalog.get_model_metadata(Route(OPENROUTER, "local/new")) is None  # another provider's catalog


@pytest.mark.asyncio
async def test_automatic_ttl_refresh_and_size_limit(monkeypatch):
    count = 0

    def serve(request):
        nonlocal count
        count += 1
        return httpx.Response(200, json={"data": []})

    client = client_for(serve)
    await catalog.models(client, OPENROUTER, KEY)
    assert count == 2
    state().update(last_fetched=0, last_attempt=0)
    await catalog.models(client, OPENROUTER, KEY)
    assert count == 4
    monkeypatch.setattr(catalog, "MAX_CATALOG_BYTES", 2)
    assert await catalog._fetch_catalog_rows(client, f"{OPENROUTER.base_url}/models", "id", KEY) is None


@pytest.mark.asyncio
@pytest.mark.parametrize("responses, expected", [
    ((200, 200), {"reachable": True, "key_valid": True, "error_kind": None}),
    ((200, 401), {"reachable": True, "key_valid": False, "error_kind": "auth"}),
    ((200, 500), {"reachable": True, "key_valid": None, "error_kind": "other"}),
])
async def test_connectivity_check_never_makes_a_paid_call(responses, expected):
    seen = []

    def serve(request):
        seen.append(request.url.path)
        return httpx.Response(responses[len(seen) - 1], json={})

    assert await catalog.check_connectivity(client_for(serve), OPENROUTER, KEY) == expected
    assert seen == ["/api/v1/models", "/api/v1/key"]


@pytest.mark.asyncio
async def test_connectivity_reports_an_unreachable_provider():
    def serve(request):
        raise httpx.ConnectError("no route")

    assert await catalog.check_connectivity(client_for(serve), OPENROUTER, KEY) == {
        "reachable": False, "key_valid": None, "error_kind": "network"}


def test_a_snapshot_older_than_a_cache_clear_lists_and_caches_nothing():
    import asyncio

    from backend import openrouter_client
    from backend.providers import Provider
    provider = Provider("openrouter", "openrouter", "https://openrouter.ai/api/v1")
    calls = []

    async def answer(request):
        calls.append(request.url.path)
        return httpx.Response(200, json={"data": [{"id": "a/b"}]})

    async def go():
        snapshot = openrouter_client.generation()  # read with the provider and its old key
        openrouter_client.clear_cache()  # a key change lands meanwhile
        async with httpx.AsyncClient(transport=httpx.MockTransport(answer)) as client:
            return await openrouter_client.models(client, provider, "old", generation=snapshot)

    assert asyncio.run(go()) is None
    assert calls == [] and not openrouter_client._caches
