"""What a model request can reach (the M2 charter's model-route rules 1, 3 and 6).

A model's listing describes its best endpoint, not every one. A request on OpenRouter can reach
each of the model's endpoints its routing allows: a Private request only the zero-retention
endpoints it is limited to (provider.only); a request pinned to an endpoint only that one; a
request that needs a parameter only those that list it (provider.require_parameters). So the
window is the smallest among them, a parameter is supported only when each lists it, and an
estimate takes the highest rate among them, from OpenRouter's per-endpoint listing
(openrouter_client.endpoints, priced by endpoint_pricing). A provider without such a listing (an
OpenAI-compatible server) is its one endpoint: its model row says what it has, and it publishes
no price in a known unit, so its estimate takes the unknown-price rates. On OpenRouter an
endpoint that publishes no prompt or completion rate is not estimated: the route is refused.
"""

import math
from dataclasses import dataclass

from backend import endpoint_pricing, openrouter
from backend.budget_router import UNKNOWN_RATES
from backend.openrouter_client import COMPRESSION_WINDOW


@dataclass(frozen=True)
class Reach:
    tags: tuple  # the endpoints a request can reach; () for a provider without an endpoint listing
    window: int | None  # the smallest window any of them reports; None when none does
    rates: dict  # USD per token, the highest among them ("input", "output", "request")
    problem: str | None = None  # why nothing can be reached: route_unverified, route_unpriced, route_unsupported
    output: int | None = None  # the smallest completion limit any of them lists, if one does
    unreported: bool = False  # one of them reports no window: a setting must give it (Harness._route_facts)
    tools: bool = False  # every one of them takes tool calls (S1-19: a route without them runs as chat with the library)


def _related(tag, pin):
    """Whether routing to pin can reach tag: the tag itself, or a variant of a base slug ("deepinfra"
    reaches "deepinfra/turbo")."""
    return tag == pin or tag.startswith(pin + "/")


def reachable(listing, provider, needs=()):
    """The endpoints of listing an OpenRouter request carrying this `provider` routing (openrouter.routing)
    can reach, as OpenRouter routes it: a tag in provider.only reaches itself and, as a base slug, its
    variants ("example" reaches "example/turbo"); provider.ignore excludes the same way; a pin with no
    fallbacks reaches only what the pin names; with provider.require_parameters, only endpoints whose
    listing takes every parameter in needs. Every bound reach computes comes from this one set."""
    found = list(listing)
    if provider.get("zdr"):  # limited to its zero-retention endpoints, or none
        only = provider.get("only") or ()
        found = [e for e in found if any(_related(e["tag"], tag) for tag in only)]
    found = [e for e in found if not any(_related(e["tag"], tag) for tag in provider.get("ignore") or ())]
    if provider.get("order") and provider.get("allow_fallbacks") is False:
        found = [e for e in found if _related(e["tag"], provider["order"][0])]
    if provider.get("require_parameters") and needs:
        found = [e for e in found if set(needs) <= set(e["supported_parameters"] or ())]
    return found


def reach(provider, row, listing, *, zero_retention=False, pin=None, needs=()) -> Reach:
    """What a request on provider for the model whose catalog row is row can reach, given the model's
    endpoint listing (None when it could not be read): the endpoints the routing it sends lets it reach
    (reachable), narrowed to those that take every parameter it needs (provider.require_parameters).
    pin is the one endpoint tag the request is pinned to, which must then be the only endpoint it can
    reach (rule 1). The window, the completion limit, the rates and Private's 8,192-token rule are all
    read from that same set, variants of a base slug included."""
    if not provider.is_openrouter:
        return Reach((), (row or {}).get("context_length"), UNKNOWN_RATES, tools=(row or {}).get("supports_tools") is True)
    if listing is None:
        return Reach((), None, UNKNOWN_RATES, "route_unverified")
    routing = openrouter.routing(row, zdr_enabled=zero_retention, pin=pin, require=bool(needs))
    routed = reachable(listing, routing)  # before parameters narrow it
    if zero_retention and not {tag for tag in routing.get("only") or ()} <= {e["tag"] for e in routed}:
        return Reach((), None, UNKNOWN_RATES, "route_unverified")  # an endpoint it may reach is not described
    if pin is not None and [e["tag"] for e in routed] != [pin]:  # a base slug names several endpoints, or none
        return Reach((), None, UNKNOWN_RATES, "route_unsupported")
    # Section 6.4: a Private request reaches no endpoint whose window is 8,192 tokens or less, where
    # OpenRouter compresses by default, by the endpoint listing's own report too. Checked before the
    # parameters narrow the set: a listing that does not say it takes them is no proof it is skipped.
    if zero_retention and any(not e["context_length"] or e["context_length"] <= COMPRESSION_WINDOW for e in routed):
        return Reach((), None, UNKNOWN_RATES, "route_unsupported")
    found = reachable(listing, routing, needs)
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
        try:
            price = endpoint_pricing.price_endpoint(None, endpoint)
        except endpoint_pricing.EndpointPricingError:  # no published prompt or completion rate: no guess
            return Reach((), None, UNKNOWN_RATES, "route_unpriced")  # (Harold, 2026-10-20)
        except (TypeError, ValueError):  # an unreadable rate is no evidence that it is low
            return Reach((), None, UNKNOWN_RATES, "route_unpriced")
        if price["unaccounted_nonzero_price_keys"]:
            return Reach((), None, UNKNOWN_RATES, "route_unpriced")  # a charge nothing here can bound
        rates = {"input": max(rates["input"], price["max_input_per_token"]),
                 "output": max(rates["output"], price["max_output_per_token"]),
                 "request": max(rates["request"], price["per_request_usd"])}
    return Reach(tuple(e["tag"] for e in found), min(windows) if windows else None, rates,
                 output=min(caps) if caps else None, unreported=len(windows) < len(found),
                 tools=all("tools" in (e["supported_parameters"] or ()) for e in found))


def _prices(value):
    """Whether every rate published in a pricing table, overrides included, is a finite amount of at
    least zero (one that cannot be read as a number is left to endpoint_pricing, which refuses it)."""
    if isinstance(value, dict):
        return all(_prices(v) for v in value.values())
    if isinstance(value, list):
        return all(_prices(v) for v in value)
    if isinstance(value, bool):  # a flag where a rate should be: no price
        return False
    if value in (None, ""):
        return True
    try:
        number = float(value)
    except (TypeError, ValueError):
        return True
    return math.isfinite(number) and number >= 0
