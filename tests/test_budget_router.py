"""The run plan and the pre-send estimate (budget_router.py), and per-endpoint prices (endpoint_pricing.py)."""

import httpx
import pytest

from backend import budget_router, endpoint_pricing
from backend.providers import Provider, Route

OPENROUTER = Provider("openrouter", "openrouter", "https://openrouter.ai/api/v1")
GENERIC = Provider("lab", "openai-compatible", "http://127.0.0.1:11434/v1")


@pytest.mark.parametrize("query, has_files, signal", [
    ("What does this word mean?", False, "standard"),
    ("What is a cohort study?", False, "research"),
    ("Briefly, what is a p-value?", False, "quick"),
    ("Compare these two papers", False, "research"),
    ("hi", True, "research"),
    ("x" * 201, False, "research"),
    ("I papered the wall", False, "standard"),  # no substring false positive
])
def test_task_signal(query, has_files, signal):
    assert budget_router.detect_task_signal(query, has_files) == signal


def test_auto_takes_the_balanced_tier_on_openrouter_and_needs_a_model_elsewhere():
    plan = budget_router.create_run_plan("hi", "auto", lambda m: Route(OPENROUTER, m))
    assert (plan.model, plan.policy_reason, plan.model_tier) == (budget_router.MODEL_TIERS["mid"][0], "auto_mid_tier", "mid")
    assert plan.predicted_cost > 0
    plan = budget_router.create_run_plan("hi", "auto", lambda m: Route(GENERIC, m), is_openrouter=False)
    assert (plan.model, plan.policy_reason, plan.predicted_cost) == (None, "model_needed", 0.0)
    plan = budget_router.create_run_plan("hi", "llama", lambda m: Route(GENERIC, m), is_openrouter=False)
    assert (plan.model, plan.policy_reason) == ("llama", "chosen_model")


def test_estimates_are_conservative_and_grow_with_effort_and_mode():
    route = Route(OPENROUTER, "unpriced/model")  # unknown price: $1 in, $5 out per million
    quick = budget_router.estimate_message_cost("quick", route, "minimal")
    assert quick == pytest.approx((2000 * 1 + 500 * 5) / 1e6)
    assert budget_router.estimate_message_cost("standard", route) > quick  # no effort counts as medium
    assert budget_router.estimate_message_cost("standard", route, "high") > budget_router.estimate_message_cost(
        "standard", route, "low")
    assert budget_router.estimate_title_cost(route) == pytest.approx((600 + 40 * 5) / 1e6)


@pytest.mark.parametrize("usage, expected", [
    ({"prompt_tokens": 1_000_000, "completion_tokens": 0}, 1.0),
    ({"prompt_tokens": 10, "completion_tokens": "5"}, None),
    ({"prompt_tokens": True, "completion_tokens": 5}, None),
    ({"prompt_tokens": -1, "completion_tokens": 5}, None),
    ({}, None),
])
def test_cost_from_reported_tokens(usage, expected):
    assert budget_router.cost_from_usage(Route(OPENROUTER, "unpriced/model"), usage) == expected


ENDPOINTS = {"data": {"endpoints": [
    {"tag": "azure", "provider_name": "Azure", "pricing": {"prompt": "0.00000015", "completion": "0.0000006"}},
    {"tag": "azure/swedencentral", "provider_name": "Azure",
     "pricing": {"prompt": "0.00000015", "completion": "0.00000066", "input_cache_write_1h": "0.0000003"}},
    {"tag": "odd", "pricing": {"prompt": "0.1", "completion": "0.2", "image": "1", "new_fee": "0.5"}},
]}}


@pytest.mark.asyncio
async def test_a_pinned_endpoint_is_priced_exactly_by_its_tag():
    seen = []

    def serve(request):
        seen.append(str(request.url))
        return httpx.Response(200, json=ENDPOINTS)

    client = httpx.AsyncClient(transport=httpx.MockTransport(serve))
    price = await endpoint_pricing.fetch_endpoint_pricing(client, OPENROUTER, "openai/gpt-4o-mini", "azure/swedencentral")
    assert (price["completion_per_token"], price["input_cache_write_per_token"]) == (0.00000066, 0.0000003)
    assert seen == ["https://openrouter.ai/api/v1/models/openai/gpt-4o-mini/endpoints"]
    with pytest.raises(endpoint_pricing.EndpointPricingError, match="exact tag"):
        await endpoint_pricing.fetch_endpoint_pricing(client, OPENROUTER, "openai/gpt-4o-mini", "azur")
    odd = await endpoint_pricing.fetch_endpoint_pricing(client, OPENROUTER, "m", "odd")
    assert odd["unaccounted_nonzero_price_keys"] == ["new_fee"]  # never silently dropped
    assert endpoint_pricing.normalize_provider_name("Google Vertex") == endpoint_pricing.normalize_provider_name("google-vertex")
