# Carried over from AI Advisory Board, backend/openrouter_client.py at commit
# b5d687820e88c10de25a9a2343d3cc478e497524, adapted for Scholia: one catalog per
# configured provider (several can be configured at once) instead of the legacy
# configuration's single provider; the HTTP client, the outbound gate's, is passed in;
# no curated registry, so a model's facts come from its provider's listing alone; and
# failures are logged without their URL, body or exception text.
"""Discover a provider's models: `/models`, and OpenRouter's zero-retention endpoints.

Catalogs are cached per provider, base URL and key for an hour, with a 30-second
cooldown between refreshes. `get_model_metadata` reads the cache only, so a
generation path never waits on the network for it. Credentials go only to the
configured base URL, and provider-supplied links are never followed.
"""

import asyncio
import hashlib
import json
import logging
import math
import time

import httpx

from backend.openrouter import classify_error

log = logging.getLogger(__name__)

CACHE_TTL_SECONDS = 3600
REFRESH_COOLDOWN_SECONDS = 30
MAX_CATALOG_BYTES = 8 * 1024 * 1024
MAX_CATALOG_MODELS = 10000
CATALOG_SECONDS = 20

_caches: dict[tuple, dict] = {}
_generation = 0  # bumped by clear_cache(); a snapshot taken before it lists nothing (Scholia)


def generation():
    """The cache's generation, to read with a provider and key snapshot (see models)."""
    return _generation


def _scope(provider, key):
    # Credentials affect account-scoped catalogs. Never expose this digest.
    return (provider.name, provider.base_url, hashlib.sha256((key or "").encode()).digest())


def _cache_state(provider, key):
    scope = _scope(provider, key)
    state = _caches.get(scope)
    if state is None:
        # A provider's earlier scope (another key or URL) is dropped.
        for old in [s for s in _caches if s[0] == provider.name]:
            del _caches[old]
        state = _caches[scope] = {"models": None, "last_fetched": 0, "last_attempt": 0, "task": None, "error": None}
    return state


async def _fetch_catalog_rows(client, url, id_field, key):
    """Only send credentials to the configured URL. Never follow provider links."""
    headers = {"Authorization": f"Bearer {key}"} if key else {}
    rows, seen, cursor = [], set(), None
    expected_total = None
    total_bytes = 0
    deadline = time.monotonic() + CATALOG_SECONDS
    try:
        while True:
            if time.monotonic() > deadline:
                raise ValueError("catalog deadline")
            async with client.stream("GET", url, headers=headers, params={"after": cursor} if cursor else None,
                                     timeout=10.0) as response:
                response.raise_for_status()
                body = bytearray()
                async for chunk in response.aiter_bytes():
                    body.extend(chunk)
                    total_bytes += len(chunk)
                    if total_bytes > MAX_CATALOG_BYTES or time.monotonic() > deadline:
                        raise ValueError("oversized catalog")
                payload = json.loads(body)
            if not isinstance(payload, dict) or not isinstance(payload.get("data"), list):
                raise ValueError("invalid catalog shape")
            page = payload["data"]
            for row in page:
                if not isinstance(row, dict) or not isinstance(row.get(id_field), str) or not row[id_field].strip():
                    raise ValueError("invalid catalog row")
                identity = row[id_field]
                if identity != identity.strip() or len(identity) > 512 or any(ord(c) < 32 for c in identity):
                    raise ValueError("invalid catalog identity")
                if id_field == "id" and identity in seen:
                    raise ValueError("duplicate model id")
                seen.add(identity)
                rows.append(row)
                if len(rows) > MAX_CATALOG_MODELS:
                    raise ValueError("oversized catalog")
            # Honor same-endpoint cursors; never follow provider-supplied URLs.
            links = payload.get("links", {})
            if not isinstance(links, dict) or links.get("next") is not None:
                raise ValueError("unsupported pagination links")
            if any(payload.get(k) for k in ("next", "next_page", "next_cursor")):
                raise ValueError("unsupported pagination")
            for field in ("total", "total_count"):
                if field in payload:
                    total = payload[field]
                    if type(total) is not int or total < 0 or (expected_total is not None and total != expected_total):
                        raise ValueError("invalid catalog total")
                    expected_total = total
            if expected_total is not None and len(rows) > expected_total:
                raise ValueError("incomplete catalog")
            if payload.get("has_more", False) is False:
                if expected_total is not None and expected_total != len(rows):
                    raise ValueError("incomplete catalog")
                return rows
            if payload.get("has_more") is not True or not page:
                raise ValueError("invalid pagination")
            next_cursor = payload.get("last_id")
            if next_cursor != page[-1][id_field] or next_cursor == cursor:
                raise ValueError("invalid pagination cursor")
            cursor = next_cursor
    except Exception as error:  # cancellation propagates
        # No upstream response body, headers, URL query or credential in logs or the UI.
        log.warning("model catalog fetch failed (%s); keeping the previous catalog", classify_error(error))
        return None


def _rate(value):
    if isinstance(value, bool) or not isinstance(value, (str, int, float)):
        return None
    try:
        number = float(value)
        return number if math.isfinite(number) and number >= 0 else None
    except (ValueError, OverflowError):
        return None


def parse_model(raw, provider, supports_zdr=None):
    """A catalog row in Scholia's shape. Prices are USD per million tokens, known only
    for OpenRouter: an OpenAI-compatible /models has no standard currency or unit."""
    raw_pricing = raw.get("pricing") if isinstance(raw.get("pricing"), dict) else {}
    rates = [_rate(raw_pricing.get(k)) if provider.is_openrouter else None for k in ("prompt", "completion")]
    pricing = {k: rate * 1_000_000 if rate is not None and math.isfinite(rate * 1_000_000) else None
               for k, rate in zip(("input", "output"), rates)}
    architecture = raw.get("architecture") if isinstance(raw.get("architecture"), dict) else {}
    parameters = raw.get("supported_parameters")
    parameters = parameters if isinstance(parameters, list) and all(isinstance(p, str) for p in parameters) else None
    modalities = architecture.get("output_modalities")
    text_output = "text" in modalities if isinstance(modalities, list) and modalities else None
    context = raw.get("context_length")
    context = context if type(context) is int and context > 0 else None
    model = {
        "id": raw["id"], "name": raw.get("name") if isinstance(raw.get("name"), str) else raw["id"],
        "context_length": context, "pricing": pricing,
        "pricing_source": "provider" if all(r is not None for r in pricing.values()) else "unknown",
        "supports_zdr": supports_zdr if provider.is_openrouter else False,
        "supported_parameters": parameters, "text_output": text_output,
        "supports_tools": None if parameters is None else "tools" in parameters,
    }
    if provider.is_openrouter and parameters is not None:
        model["supports_reasoning"] = "reasoning" in parameters or "include_reasoning" in parameters
        if model["supports_reasoning"]:
            model["reasoning_extraction"] = "field"
    return model


async def _refresh(client, provider, key, state):
    state["last_attempt"] = time.time()
    base = provider.base_url.rstrip("/")
    raw = await _fetch_catalog_rows(client, f"{base}/models", "id", key)
    if raw is None:
        state["error"] = "refresh_failed"
        return
    zdr = None
    if provider.is_openrouter:
        rows = await _fetch_catalog_rows(client, f"{base}/endpoints/zdr", "model_id", key)
        zdr = {row["model_id"] for row in rows} if rows is not None else None
    parsed = {row["id"]: parse_model(row, provider, None if zdr is None else row["id"] in zdr) for row in raw}
    if _caches.get(_scope(provider, key)) is not state:
        return  # a key or URL change during the await invalidates this generation
    # Zero-retention availability that could not be verified leaves those models unavailable to Private.
    state.update(models=parsed, last_fetched=time.time(),
                 error="zdr_unverified" if provider.is_openrouter and zdr is None else None)


async def models(client, provider, key, *, force=False, generation=None):
    """The provider's models by id, refreshed when stale, or None if never fetched.
    generation, if given, is generation() as read with provider and key: if the cache was
    cleared since (their settings changed), the snapshot is stale and nothing is listed or
    cached from it."""
    if generation is not None and generation != _generation:
        return None
    state = _cache_state(provider, key)
    age = time.time() - state["last_fetched"]
    if state["task"] is not None and not state["task"].done():
        await asyncio.shield(state["task"])
    elif ((force or state["error"] or state["models"] is None or age >= CACHE_TTL_SECONDS)
          and time.time() - state["last_attempt"] >= REFRESH_COOLDOWN_SECONDS):
        state["task"] = asyncio.create_task(_refresh(client, provider, key, state))
        await asyncio.shield(state["task"])
    return state["models"] if _caches.get(_scope(provider, key)) is state else None


def catalog_status(provider, key):
    """The catalog's freshness for this provider and key. Read only: it never makes or
    drops a cache entry (Scholia)."""
    state = _caches.get(_scope(provider, key)) or {"last_fetched": 0, "error": None}
    return {"last_fetched": state["last_fetched"] or None,
            "stale": bool(state["error"]) or time.time() - state["last_fetched"] >= CACHE_TTL_SECONDS,
            "error": state["error"]}


def catalog_read(route) -> bool:
    """Whether the route's provider has a catalog read and cached. Never performs network I/O."""
    return any(name == route.provider.name and base_url == route.provider.base_url and state["models"]
               for (name, base_url, _), state in _caches.items())


def get_model_metadata(route):
    """The cached catalog row for a route's model, or None. Never performs network I/O."""
    for (name, base_url, _), state in _caches.items():
        if name == route.provider.name and base_url == route.provider.base_url and state["models"]:
            return state["models"].get(route.model)
    return None


def clear_cache():
    global _generation
    _generation += 1
    _caches.clear()


async def check_connectivity(client, provider, key):
    """Two-stage probe: an unauthenticated GET /models proves reachability; with a
    key, OpenRouter's GET /key checks it (401 or 403 means a bad key). Credit
    exhaustion is not detectable here; it surfaces at chat time. Never a paid call.
    """
    result = {"reachable": False, "key_valid": None, "error_kind": None}
    base = provider.base_url.rstrip("/")
    try:
        await client.get(f"{base}/models", timeout=httpx.Timeout(10.0, connect=8.0))
        result["reachable"] = True  # any HTTP response proves the path works
    except Exception as error:  # cancellation propagates
        result["error_kind"] = classify_error(error)
        log.warning("provider %s is not reachable (%s)", provider.name, result["error_kind"])
        return result
    if not key or not provider.is_openrouter:
        return result  # an OpenAI-compatible endpoint has no standard key check
    try:
        response = await client.get(f"{base}/key", headers={"Authorization": f"Bearer {key}"},
                                    timeout=httpx.Timeout(10.0, connect=8.0))
        if response.status_code in (401, 403):
            result.update(key_valid=False, error_kind="auth")
        elif response.is_success:
            result["key_valid"] = True
        else:
            result["error_kind"] = "other"
    except Exception as error:  # cancellation propagates
        result["error_kind"] = classify_error(error)
    return result
