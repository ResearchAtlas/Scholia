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
    window: int | None  # the smallest window among them; None when one does not report its window
    rates: dict  # USD per token, the highest among them ("input", "output", "request")
    problem: str | None = None  # why nothing can be reached: route_unverified, route_unpriced, route_unsupported
    output: int | None = None  # the smallest completion limit any of them lists, if one does


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
        try:
            price = endpoint_pricing.price_endpoint(None, endpoint)
        except endpoint_pricing.EndpointPricingError:  # publishes no prompt or completion price
            price = {"unaccounted_nonzero_price_keys": [], "max_input_per_token": UNKNOWN_RATES["input"],
                     "max_output_per_token": UNKNOWN_RATES["output"], "per_request_usd": 0.0}
        except (TypeError, ValueError):  # an unreadable rate is no evidence that it is low
            return Reach((), None, UNKNOWN_RATES, "route_unpriced")
        if price["unaccounted_nonzero_price_keys"] or not all(
                math.isfinite(price[k]) and price[k] >= 0
                for k in ("max_input_per_token", "max_output_per_token", "per_request_usd")):
            # A charge nothing here can bound, or a rate that is no price (OpenRouter's -1 for a
            # variable-priced router, a non-finite value): never an estimate of $0.
            return Reach((), None, UNKNOWN_RATES, "route_unpriced")
        rates = {"input": max(rates["input"], price["max_input_per_token"]),
                 "output": max(rates["output"], price["max_output_per_token"]),
                 "request": max(rates["request"], price["per_request_usd"])}
    return Reach(tuple(e["tag"] for e in found), min(windows) if windows else None, rates,
                 output=min(caps) if caps else None)
