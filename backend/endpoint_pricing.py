# Carried over from AI Advisory Board, backend/endpoint_pricing.py at commit
# b5d687820e88c10de25a9a2343d3cc478e497524, adapted for Scholia: the endpoints URL is
# built from the OpenRouter provider passed in, not the legacy configuration; requests
# go through the HTTP client passed in (the outbound gate's); and the served-tag
# resolver, used only by the current app's maintainer probes, is left out.
"""Per-ENDPOINT price resolution from OpenRouter's public `/endpoints` API.

Billing is per ENDPOINT -- not per model, and not per provider name. On
openai/gpt-4o-mini the tags `azure` and `azure/swedencentral` are both served by
provider "Azure" yet price completion at 0.0000006 vs 0.00000066. So a pin must
name an exact tag, and an absent or ambiguous match fails rather than guessing a
rate.

`GET /api/v1/models/{author}/{slug}/endpoints` is PUBLIC (no key) and free -- it
is metadata, not inference, so resolving prices here costs nothing and is safe to
run before any paid call.

In Scholia it prices every endpoint a request can reach, so an estimate takes the
highest rate among them (backend/reach.py; the M2 charter's model-route rule 6),
including the cache-write and long-context rates (`pricing.overrides`). The current
app's maintainer probes, which first used it to bound their spend, are not carried over.

Rates are USD PER TOKEN (strings upstream); the catalog's are per MILLION.
A pinned endpoint's price can exceed the model-wide value, so an estimate from
the model-wide price could under-count.
"""
import re
from datetime import datetime, timezone

# What a TEXT-ONLY, plugin-free probe call can be billed for, and how a bound must
# account for each. Anything published non-zero and NOT listed here is unaccounted:
# the caller refuses rather than silently leaving it out of a spend ceiling.
_TOKEN_PRICE_KEYS = frozenset({"prompt", "completion", "internal_reasoning"})
# Providers publish a FAMILY of prompt-side cache rates, not one key: Anthropic adds
# `input_cache_write_1h` alongside `input_cache_write` for its 1-hour tier. Match the
# family by prefix so a new TTL variant is accounted for automatically instead of
# tripping the fail-closed guard (or, worse, being dropped from a ceiling).
_CACHE_KEY_PREFIXES = ("input_cache_write", "input_cache_read")
_PER_REQUEST_PRICE_KEYS = frozenset({"request", "web_search"})
# Cannot apply: the probe sends one short text message and attaches no plugin, so no
# image/audio/video units are ever billed (see reasoning_probe._post).
_INAPPLICABLE_PRICE_KEYS = frozenset({
    "image", "image_output", "audio", "input_audio_cache", "video",
})
# Not a charge: `discount` reduces cost. `overrides` holds other rates, such as long-context ones
# (Scholia; the M2 charter's model-route rule 6): see _override_rates.
_NON_PRICE_KEYS = frozenset({"discount"})
_ACCOUNTED_PRICE_KEYS = (
    _TOKEN_PRICE_KEYS | _PER_REQUEST_PRICE_KEYS | _INAPPLICABLE_PRICE_KEYS | _NON_PRICE_KEYS | {"overrides"}
)


def _is_accounted_price_key(key):
    return key in _ACCOUNTED_PRICE_KEYS or key.startswith(_CACHE_KEY_PREFIXES)


def _is_nonzero_rate(value):
    """True when a published price could actually bill. Unparseable values count as
    non-zero: an unreadable rate is not evidence that it is free."""
    if value in (None, ""):
        return False
    try:
        return float(value) != 0.0
    except (TypeError, ValueError):
        return True


# The conditions an override may name: token counts past which its rates apply. Its shape is not
# documented, so only these are read as conditions (Scholia; Harold, 2026-10-20); any other key, or a
# value of another shape, makes the endpoint's pricing unaccounted, and no estimate is made from it.
_CONDITION_KEYS = frozenset({"min_prompt_tokens", "max_prompt_tokens", "min_context_tokens", "max_context_tokens"})


def _override_rates(value, path="overrides"):
    """({price key: [rates]}, [unaccounted paths]) from `pricing.overrides`, whose rates apply under
    conditions, such as prompts past a length. Read conservatively (Scholia): every price key found at
    any depth counts as a rate that may apply; a key in _CONDITION_KEYS is a condition when its value is
    a whole number; anything else that is not zero or empty (another key, a bare value, a list of
    values) is unaccounted."""
    if not isinstance(value, (dict, list)):
        return {}, [path] if _is_nonzero_rate(value) or isinstance(value, bool) else []
    rates, unaccounted = {}, []
    for key, item in (value.items() if isinstance(value, dict) else enumerate(value)):
        name = key if isinstance(key, str) else None
        if isinstance(item, (dict, list)):
            found, missing = _override_rates(item, f"{path}.{key}")
            for k, v in found.items():
                rates.setdefault(k, []).extend(v)
            unaccounted += missing
        elif name in _CONDITION_KEYS:
            if type(item) is not int or item < 0:
                unaccounted.append(f"{path}.{key}")
        elif name is not None and _is_accounted_price_key(name) and name not in _NON_PRICE_KEYS | {"overrides"}:
            if _is_nonzero_rate(item):
                try:
                    rates.setdefault(name, []).append(float(item))
                except (TypeError, ValueError):
                    unaccounted.append(f"{path}.{key}")
        elif isinstance(item, bool) or _is_nonzero_rate(item):
            unaccounted.append(f"{path}.{key}")
    return rates, unaccounted


class EndpointPricingError(RuntimeError):
    """A pinned endpoint's rate could not be resolved with certainty. Callers must
    treat this as fatal for the route in question: guessing a rate would silently
    invalidate whatever guarantee (billing evidence, spend ceiling) depends on it."""


def normalize_provider_name(name):
    """Provider identifiers arrive in two spellings: endpoint tags are slugs
    ('google-vertex'), while router metadata reports display names ('Google Vertex').
    Compare on a case/punctuation-insensitive basis."""
    return re.sub(r"[^a-z0-9]", "", (name or "").lower())



async def fetch_endpoints(client, provider, model):
    """Every published endpoint for a model on an OpenRouter provider, with the URL they
    came from. Public and keyless: metadata, not inference, so this cannot spend."""
    url = f"{provider.base_url.rstrip('/')}/models/{model}/endpoints"
    resp = await client.get(url, timeout=30.0)
    resp.raise_for_status()
    body = resp.json()
    data = body.get("data") if isinstance(body.get("data"), dict) else body
    return url, ((data or {}).get("endpoints") or [])


def price_endpoint(url, endpoint):
    """Pricing + provenance for ONE endpoint object. Rates are USD PER TOKEN."""
    pricing = endpoint.get("pricing") or {}

    def rate(key):
        value = pricing.get(key)
        return float(value) if value not in (None, "") else None

    prompt_rate = rate("prompt")
    completion_rate = rate("completion")
    if prompt_rate is None or completion_rate is None:
        raise EndpointPricingError(
            f"endpoint {endpoint.get('tag')!r} publishes no prompt/completion pricing; "
            "cannot price this route"
        )

    # Endpoints bill more than the two token rates. Surface every component a caller
    # bounding spend must account for; omitting one silently under-counts a ceiling.
    # `web_search` is real and INTRINSIC on search-native models (perplexity/sonar
    # publishes 0.005/search and searches on every completion -- backend/web_search.py
    # documents them as having native search built in), and providers that auto-cache
    # can bill the prompt at the cache-WRITE rate instead of the prompt rate.
    def rate0(key):
        return rate(key) or 0.0

    # FAIL CLOSED on anything we do not know how to charge for. A silently ignored
    # non-zero rate is an under-counted ceiling; the caller must refuse instead.
    unaccounted = sorted(
        key for key, value in pricing.items()
        if not _is_accounted_price_key(key) and _is_nonzero_rate(value)
    )
    overrides, missing = _override_rates(pricing.get("overrides") or {})
    unaccounted = sorted(unaccounted + missing)

    def highest(keys):  # the base rate or any override's, whichever is highest (Scholia)
        found = [rate0(key) for key in pricing if key in keys]
        found += [rate for key, values in overrides.items() if key in keys for rate in values]
        return max(found or [0.0])

    cache_writes = {key for key in [*pricing, *overrides] if key.startswith("input_cache_write")}

    return {
        "source": url,
        "fetched_at": datetime.now(timezone.utc).isoformat(),
        "units": "USD per token",
        # Endpoint identity -- the reading is valid for THIS route only.
        "tag": endpoint.get("tag"),
        "provider_name": endpoint.get("provider_name"),
        "endpoint_name": endpoint.get("name"),
        "endpoint_model_id": endpoint.get("model_id"),
        "endpoint_model_name": endpoint.get("model_name"),
        "prompt_per_token": prompt_rate,
        "completion_per_token": completion_rate,
        # A non-zero internal_reasoning price on THIS endpoint is an explicit,
        # named separate charge for reasoning tokens.
        "internal_reasoning_per_token": rate("internal_reasoning"),
        # A provider that auto-caches bills the prompt at the cache-WRITE rate. Take
        # the WORST across the whole cache-write family (plain + TTL variants such as
        # Anthropic's input_cache_write_1h), since any of them could apply.
        "input_cache_write_per_token": max(
            [rate0(key) for key in pricing if key.startswith("input_cache_write")] or [0.0]
        ),
        # Flat charges that land once per call regardless of token counts.
        "per_request_usd": highest({"request"}) + highest({"web_search"}),
        # The most a prompt token or an output token can cost here (Scholia): the prompt at its own
        # rate, a cache write's or a long-context override's; output at the completion or reasoning rate.
        "max_input_per_token": highest({"prompt"} | cache_writes),
        "max_output_per_token": highest({"completion", "internal_reasoning"}),
        # Non-zero published rates this module cannot map to a probe call. Non-empty
        # means NO sound bound can be computed -- callers must refuse, not guess.
        "unaccounted_nonzero_price_keys": unaccounted,
        "pricing_raw": pricing,
    }



async def fetch_endpoint_pricing(client, provider, model, provider_tag):
    """Resolve the EXACT pinned endpoint and return its own pricing + provenance.

    The tag must match exactly -- no prefix or provider-name fallback -- and an
    absent or ambiguous match raises rather than guessing a rate.
    """
    url, endpoints = await fetch_endpoints(client, provider, model)
    matches = [e for e in endpoints if e.get("tag") == provider_tag]
    if not matches:
        raise EndpointPricingError(
            f"no endpoint on {model} has tag == {provider_tag!r}. Pricing is per endpoint, so the "
            "pin must name an exact tag -- variant tags (e.g. 'azure/swedencentral') price "
            "differently from their base tag."
        )
    if len(matches) > 1:
        raise EndpointPricingError(
            f"tag {provider_tag!r} matched {len(matches)} endpoints on {model} -- ambiguous, "
            "so no single rate can be attributed to this route"
        )
    return price_endpoint(url, matches[0])
