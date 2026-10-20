"""The run plan and the pre-send estimate (budget_router.py), per-endpoint prices (endpoint_pricing.py) and what a
request can reach (reach.py)."""

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
    plan = budget_router.create_run_plan("hi", "auto")
    assert (plan.model, plan.policy_reason, plan.model_tier) == (budget_router.MODEL_TIERS["mid"][0], "auto_mid_tier", "mid")
    assert plan.predicted_cost is None  # estimated once the answer step's request is built
    plan = budget_router.create_run_plan("hi", "auto", is_openrouter=False)
    assert (plan.model, plan.policy_reason) == (None, "model_needed")
    plan = budget_router.create_run_plan("hi", "llama", is_openrouter=False)
    assert (plan.model, plan.policy_reason) == ("llama", "chosen_model")


def test_an_estimate_counts_the_request_and_its_whole_output_limit_at_the_given_rates():
    rates = {"input": 2e-6, "output": 1e-5, "request": 0.004}
    assert budget_router.request_cost(rates, 1000, 8000) == pytest.approx(1000 * 2e-6 + 8000 * 1e-5 + 0.004)
    # Unpriced: $1 in and $5 out per million, never $0.
    assert budget_router.request_cost(budget_router.UNKNOWN_RATES, 2000, 1024) == pytest.approx((2000 + 1024 * 5) / 1e6)


@pytest.mark.parametrize("usage, expected", [
    ({"prompt_tokens": 1_000_000, "completion_tokens": 0}, 1.0),
    ({"prompt_tokens": 10, "completion_tokens": "5"}, None),
    ({"prompt_tokens": True, "completion_tokens": 5}, None),
    ({"prompt_tokens": -1, "completion_tokens": 5}, None),
    ({}, None),
])
def test_cost_from_reported_tokens(usage, expected):
    assert budget_router.cost_from_usage(budget_router.UNKNOWN_RATES, usage) == expected


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


def test_overrides_count_as_price_and_an_unaccounted_one_refuses():
    # Model-route rule 6: pricing.overrides holds rates such as long-context ones; the highest applies.
    long_context = {"prompt": "0.00000125", "completion": "0.00001",
                    "overrides": [{"min_prompt_tokens": 272000, "prompt": "0.0000025", "completion": "0.000015"}]}
    price = endpoint_pricing.price_endpoint("u", {"tag": "t", "pricing": long_context})
    assert (price["max_input_per_token"], price["max_output_per_token"]) == (0.0000025, 0.000015)
    assert price["unaccounted_nonzero_price_keys"] == []
    cached = {"prompt": "0.000001", "completion": "0.000002", "input_cache_write": "0.00000125", "request": "0.01",
              "overrides": {"long": {"input_cache_write_1h": "0.000003", "web_search": "0.02"}}}
    price = endpoint_pricing.price_endpoint("u", {"tag": "t", "pricing": cached})
    assert (price["max_input_per_token"], price["per_request_usd"]) == (0.000003, 0.03)
    odd = endpoint_pricing.price_endpoint("u", {"tag": "t", "pricing": {
        "prompt": "0.000001", "completion": "0.000002", "overrides": [{"threshold": 1000, "surcharge": "0.5"}]}})
    assert odd["unaccounted_nonzero_price_keys"] == ["overrides.0.surcharge", "overrides.0.threshold"]


@pytest.mark.parametrize("overrides", [{"surcharge": 1}, "0.5", ["0.5"], {"min_prompt_tokens": "272000"}, {"flag": True}])
def test_an_override_that_is_not_a_known_rate_or_condition_refuses_the_route(overrides):
    # The external review's A3: only named conditions are conditions; with zero base rates, an unknown
    # charge must not leave a $0 estimate.
    from backend import reach
    endpoint = {"tag": "a", "context_length": 8192, "supported_parameters": [],
                "pricing": {"prompt": "0", "completion": "0", "overrides": overrides}}
    assert reach.reach(OPENROUTER, {}, [endpoint]).problem == "route_unpriced"


def test_every_published_rate_counts_and_a_missing_or_unknown_one_refuses():
    # The external review's A2, with Harold's answer (2026-10-20): a missing prompt or completion rate
    # refuses the route; the endpoint's other published charges count, and an unknown one refuses.
    from backend import reach
    endpoint = {"tag": "a", "context_length": 8192, "supported_parameters": [],
                "pricing": {"prompt": "0.000002", "completion": "0.1", "request": "1"}}
    found = reach.reach(OPENROUTER, {}, [endpoint])
    assert (found.problem, found.rates) == (None, {"input": 0.000002, "output": 0.1, "request": 1.0})
    endpoint["pricing"]["new_fee"] = "2"
    assert reach.reach(OPENROUTER, {}, [endpoint]).problem == "route_unpriced"
    del endpoint["pricing"]["prompt"], endpoint["pricing"]["new_fee"]
    assert reach.reach(OPENROUTER, {}, [endpoint]).problem == "route_unpriced"
    # A provider without per-endpoint prices (an OpenAI-compatible server) keeps the unknown-price rates.
    assert reach.reach(GENERIC, {"context_length": 8192}, None).rates == budget_router.UNKNOWN_RATES


def test_what_a_request_can_reach():
    from backend import reach
    listing = [{"tag": "a", "context_length": 131072, "supported_parameters": ["reasoning"],
                "pricing": {"prompt": "0.000001", "completion": "0.000004"}},
               {"tag": "a/turbo", "context_length": 32768, "supported_parameters": [],
                "pricing": {"prompt": "0.000003", "completion": "0.000002"}},
               {"tag": "b", "context_length": 65536, "supported_parameters": ["reasoning"],
                "pricing": {"prompt": "0.000002", "completion": "0.000005"}}]
    found = reach.reach(OPENROUTER, {}, listing)
    # Every endpoint: the smallest window; the highest rates any of them publishes.
    assert (found.tags, found.window, found.problem) == (("a", "a/turbo", "b"), 32768, None)
    assert found.rates == {"input": 0.000003, "output": 0.000005, "request": 0.0}
    # An endpoint that publishes no prompt or completion rate is not estimated (Harold, 2026-10-20).
    for pricing in ({}, {"prompt": "0.000001"}, {"completion": "0.000001", "request": "0.01"}):
        assert reach.reach(OPENROUTER, {}, [*listing, {**listing[2], "tag": "c", "pricing": pricing}]).problem \
            == "route_unpriced", pricing
    # Rule 1: a parameter the request relies on narrows it to the endpoints that list it.
    assert reach.reach(OPENROUTER, {}, listing, needs=("reasoning",)).tags == ("a", "b")
    # A pin must name exactly one endpoint: a base slug that reaches a variant too is refused.
    assert reach.reach(OPENROUTER, {}, listing, pin="b").tags == ("b",)
    assert reach.reach(OPENROUTER, {}, listing, pin="a").problem == "route_unsupported"
    # Private: only the zero-retention endpoints it is limited to, each of which must be described.
    row = {"zdr_endpoints": {"usable": ["a"], "small": []}}
    assert reach.reach(OPENROUTER, row, listing, zero_retention=True).tags == ("a",)
    assert reach.reach(OPENROUTER, {"zdr_endpoints": {"usable": ["c"]}}, listing,
                       zero_retention=True).problem == "route_unverified"
    assert reach.reach(OPENROUTER, {}, None).problem == "route_unverified"  # no listing read
    assert reach.reach(OPENROUTER, {}, [{**listing[0], "pricing": {"prompt": "0", "completion": "0",
                                                                   "new_fee": "1"}}]).problem == "route_unpriced"
    assert reach.reach(GENERIC, {"context_length": 8192}, None) == reach.Reach((), 8192, budget_router.UNKNOWN_RATES)
    # A rate that is no price (-1 for a variable-priced router, or not finite) refuses: never $0.
    for bad in ("-1", "nan", "inf"):
        for pricing in ({"prompt": bad, "completion": "0.000001"},
                        {"prompt": bad, "completion": bad, "request": "0", "input_cache_write": "0"},  # zeros beside it
                        {"prompt": "0.000001", "completion": "0.000002", "overrides": [{"completion": bad}]}):
            assert reach.reach(OPENROUTER, {}, [{**listing[0], "pricing": pricing}]).problem == "route_unpriced", pricing
    # An endpoint that reports no window leaves the smallest reported one as the report, which a
    # setting may lower but never raise; the smallest completion limit listed caps the output.
    unreported = [{**listing[0], "context_length": None}, {**listing[1], "max_completion_tokens": 4096},
                  {**listing[2], "max_completion_tokens": 16384}]
    found = reach.reach(OPENROUTER, {}, unreported)
    assert (found.window, found.output, found.unreported) == (32768, 4096, True)  # a setting must also be made
    from backend import providers
    assert providers.window({"default_window": 131072}, "m", found.window)["in_use"] == 32768


def test_a_flag_where_a_rate_should_be_refuses():
    # Internal review a3 (P3-1): JSON true or false is no price, as the catalog's own reader holds.
    from backend import reach
    for flag in (True, False):
        endpoint = {"tag": "a", "context_length": 8192, "supported_parameters": [],
                    "pricing": {"prompt": flag, "completion": flag}}
        assert reach.reach(OPENROUTER, {}, [endpoint]).problem == "route_unpriced", flag


@pytest.mark.parametrize("pricing", [
    {"prompt": "0", "completion": "0", "input_cache_read": "0.01"},
    {"prompt": "0", "completion": "0", "overrides": [{"min_prompt_tokens": 1000, "input_cache_read": "0.01"}]},
    {"prompt": "0", "completion": "0", "input_cache_read_5m": "0.01"},
])
def test_a_cache_read_rate_counts_toward_the_input_rate(pricing):
    # External review a2: every input-side charge the reader accepts is in the highest input rate,
    # at the base level and inside overrides, so no estimate comes out at $0.
    from backend import reach
    endpoint = {"tag": "a", "context_length": 8192, "supported_parameters": ["max_tokens"], "pricing": pricing}
    assert reach.reach(OPENROUTER, {}, [endpoint]).rates["input"] == 0.01


def test_every_accepted_price_key_is_counted_or_refuses():
    # The sweep: a non-zero rate under each key the reader knows either raises the estimate or refuses.
    from backend import reach
    base = {"prompt": "0", "completion": "0"}
    counted = {"prompt": "input", "input_cache_write": "input", "input_cache_write_1h": "input",
               "input_cache_read": "input", "completion": "output", "internal_reasoning": "output",
               "request": "request", "web_search": "request"}
    for key, side in counted.items():
        endpoint = {"tag": "a", "context_length": 8192, "supported_parameters": [], "pricing": {**base, key: "0.5"}}
        assert reach.reach(OPENROUTER, {}, [endpoint]).rates[side] >= 0.5, key
    # Inapplicable to text-only requests: image, audio and video input. An image output rate refuses.
    for key, problem in (("image", None), ("audio", None), ("input_audio_cache", None), ("video", None),
                         ("image_output", "route_unpriced"), ("something_new", "route_unpriced")):
        endpoint = {"tag": "a", "context_length": 8192, "supported_parameters": [], "pricing": {**base, key: "0.5"}}
        assert reach.reach(OPENROUTER, {}, [endpoint]).problem == problem, key


def test_a_private_request_never_reaches_an_endpoint_the_listing_reports_at_8192_or_less():
    # External review a2: the zero-retention listing said 131,072; the model's endpoint listing says
    # 8,192 (or nothing). Section 6.4 holds by the listing's own report as well.
    from backend import reach
    row = {"zdr_endpoints": {"usable": ["example"], "small": []}}
    for window in (8192, 4096, None):
        listing = [{"tag": "example", "context_length": window, "supported_parameters": ["max_tokens"],
                    "pricing": {"prompt": "0.000001", "completion": "0.000002"}}]
        assert reach.reach(OPENROUTER, row, listing, zero_retention=True).problem == "route_unsupported", window
    listing[0]["context_length"] = 8193
    assert reach.reach(OPENROUTER, row, listing, zero_retention=True).problem is None
