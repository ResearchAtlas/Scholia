"""What a model request can reach (the M2 charter's model-route rules 1, 3 and 6).

A model's listing describes its best endpoint, not every one. A request on OpenRouter can reach
each of the model's endpoints its routing allows: a Private request only the zero-retention
endpoints it is limited to (provider.only); a request pinned to an endpoint only that one; a
request that needs a parameter only those that list it (provider.require_parameters). So the
window is the smallest among them, a parameter is supported only when each lists it, and an
estimate takes the highest rate among them, from OpenRouter's per-endpoint listing
(openrouter_client.endpoints, priced by endpoint_pricing). A provider without such a listing (an
OpenAI-compatible server) is its one endpoint: its model row says what it has.
"""

import math
from dataclasses import dataclass

from backend import endpoint_pricing
from backend.budget_router import UNKNOWN_RATES


@dataclass(frozen=True)
class Reach:
    tags: tuple  # the endpoints a request can reach; () for a provider without an endpoint listing
    window: int | None  # the smallest window any of them reports; None when none does
    rates: dict  # USD per token, the highest among them ("input", "output", "request")
    problem: str | None = None  # why nothing can be reached: route_unverified, route_unpriced, route_unsupported
    output: int | None = None  # the smallest completion limit any of them lists, if one does
    unreported: bool = False  # one of them reports no window: a setting must give it (Harness._route_facts)


def _related(tag, pin):
    """Whether routing to pin can reach tag: the tag itself, or a variant of a base slug ("deepinfra"
    reaches "deepinfra/turbo")."""
    return tag == pin or tag.startswith(pin + "/")


def reach(provider, row, listing, *, zero_retention=False, pin=None, needs=()) -> Reach:
    """What a request on provider for the model whose catalog row is row can reach, given the model's
    endpoint listing (None when it could not be read). pin is the one endpoint tag the request is
    pinned to, which must then be the only endpoint it can reach (rule 1); needs the parameters the
    request relies on."""
    if not provider.is_openrouter:
        return Reach((), (row or {}).get("context_length"), UNKNOWN_RATES)
    if listing is None:
        return Reach((), None, UNKNOWN_RATES, "route_unverified")
    found = list(listing)
    if zero_retention:
        usable = set(((row or {}).get("zdr_endpoints") or {}).get("usable") or ())
        found = [e for e in found if e["tag"] in usable]
        if {e["tag"] for e in found} != usable:  # an endpoint it may reach is not described
            return Reach((), None, UNKNOWN_RATES, "route_unverified")
    if pin is not None:
        found = [e for e in found if _related(e["tag"], pin)]
        if [e["tag"] for e in found] != [pin]:  # a base slug names several endpoints, or none
            return Reach((), None, UNKNOWN_RATES, "route_unsupported")
    if needs:
        found = [e for e in found if set(needs) <= set(e["supported_parameters"] or ())]
    if not found:
        return Reach((), None, UNKNOWN_RATES, "route_unsupported")
    # The smallest window any of them reports; one that reports none leaves it to a setting, which
    # can lower this but never raise it (providers.window).
    windows = [e["context_length"] for e in found if e["context_length"] is not None]
    caps = [e["max_completion_tokens"] for e in found if e.get("max_completion_tokens") is not None]
    rates = {"input": 0.0, "output": 0.0, "request": 0.0}
    for endpoint in found:
        if not _prices(endpoint["pricing"]):  # a rate that is no price (OpenRouter's -1 for a variable-priced
            return Reach((), None, UNKNOWN_RATES, "route_unpriced")  # router, a non-finite value): never $0
        # A prompt or completion rate it does not publish is taken at the unknown-price rate; every rate it
        # does publish, and the refusal of a charge nothing here can bound, still apply.
        pricing = dict(endpoint["pricing"])
        for key, fallback in (("prompt", UNKNOWN_RATES["input"]), ("completion", UNKNOWN_RATES["output"])):
            if pricing.get(key) in (None, ""):
                pricing[key] = repr(fallback)
        try:
            price = endpoint_pricing.price_endpoint(None, {**endpoint, "pricing": pricing})
        except (TypeError, ValueError):  # an unreadable rate is no evidence that it is low
            return Reach((), None, UNKNOWN_RATES, "route_unpriced")
        if price["unaccounted_nonzero_price_keys"]:
            return Reach((), None, UNKNOWN_RATES, "route_unpriced")  # a charge nothing here can bound
        rates = {"input": max(rates["input"], price["max_input_per_token"]),
                 "output": max(rates["output"], price["max_output_per_token"]),
                 "request": max(rates["request"], price["per_request_usd"])}
    return Reach(tuple(e["tag"] for e in found), min(windows) if windows else None, rates,
                 output=min(caps) if caps else None, unreported=len(windows) < len(found))


def _prices(value):
    """Whether every rate published in a pricing table, overrides included, is a finite amount of at
    least zero (one that cannot be read as a number is left to endpoint_pricing, which refuses it)."""
    if isinstance(value, dict):
        return all(_prices(v) for v in value.values())
    if isinstance(value, list):
        return all(_prices(v) for v in value)
    if isinstance(value, bool) or value in (None, ""):
        return True
    try:
        number = float(value)
    except (TypeError, ValueError):
        return True
    return math.isfinite(number) and number >= 0
