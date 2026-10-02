# Carried over from AI Advisory Board, backend/openrouter.py at commit
# b5d687820e88c10de25a9a2343d3cc478e497524, adapted for Scholia: the route, key and
# HTTP client (the outbound gate's, for the request's project) are passed in instead
# of read from the legacy configuration; every HTTP attempt is reported with its usage;
# errors are classified completely and returned, never logged with their bodies; and
# reasoning is sent only for a model whose capability record says how.
"""The provider adapter: one chat completion against a model route.

`query_model` makes at most two HTTP attempts: the second only when an
OpenAI-compatible endpoint rejects the optional `reasoning` field (400), retried
without it. Both share one total time bound. Every attempt is returned with its
outcome, HTTP status, elapsed time and the usage it reported, so the caller can
record and settle what each one cost. Usage is read before the answer is
validated, because a malformed answer can still carry usage. Nothing here logs a
prompt, an answer, a key, a URL or a provider's error body.
"""

import asyncio
import hashlib
import logging
import time
from collections import OrderedDict
from dataclasses import dataclass, field

import httpx

from backend import reasoning_capability
from backend.outbound_gate import DISPATCHED, OutboundDenied
from backend.reasoning_control import resolve_reasoning_payload
from backend.spending import amount

log = logging.getLogger(__name__)

MAX_ATTEMPTS = 2
NEGOTIATION_TTL_SECONDS = 3600  # how long a route is remembered as not taking `reasoning`
NEGOTIATION_MAX = 256
REASONING_SHOWN_CHARS = 2000


def connect_timeout_for(total_timeout: float) -> float:
    """Connect deadline strictly below the wall-clock timeout, so a blocked
    network raises ConnectTimeout (kind=network) before the total bound
    cancels the request (kind=timeout)."""
    return min(10.0, total_timeout / 2)


def classify_error(exc: BaseException) -> str:
    """A coarse, safe failure kind for an exception from a provider call."""
    if isinstance(exc, OutboundDenied):
        return "refused"
    # NetworkError covers Connect/Read/Write/CloseError; ProxyError is a
    # separate TransportError subclass (failed tunnel = network problem too).
    if isinstance(exc, (httpx.NetworkError, httpx.ProxyError, httpx.ConnectTimeout)):
        return "network"
    if isinstance(exc, (httpx.ReadTimeout, httpx.WriteTimeout, httpx.PoolTimeout, asyncio.TimeoutError, TimeoutError)):
        return "timeout"
    if isinstance(exc, httpx.HTTPStatusError):
        return classify_status(exc.response.status_code)
    return "other"


def classify_status(code: int) -> str:
    if code == 401:
        return "auth"
    # 403 is NOT auth: OpenRouter documents it as moderation/guardrail blocks.
    if code == 402:
        return "quota"
    if code == 408:  # OpenRouter documents 408 as request timeout
        return "timeout"
    if code == 429:
        return "rate_limit"
    if 400 <= code < 500:
        return "request"
    return "other"


@dataclass
class Attempt:
    """One HTTP attempt: what it cost and how it ended. No content."""
    outcome: str  # "ok", or the failure kind
    http_status: int | None
    elapsed_ms: int
    usage: dict = field(default_factory=dict)  # tokens and cost as the provider reported them
    dispatched: bool = True  # False when the request certainly never reached the provider

    @property
    def reported_cost(self):
        """The cost the provider reported, if it is a valid amount (finite, not negative)."""
        return amount(self.usage.get("cost"))

    def record(self) -> dict:
        """The run-event form: counts and codes only."""
        tokens = {k: self.usage[k] for k in ("prompt_tokens", "completion_tokens", "total_tokens")
                  if _count(self.usage.get(k))}
        details = self.usage.get("completion_tokens_details") or {}
        reasoning = details.get("reasoning_tokens") if isinstance(details, dict) else None
        if _count(reasoning):
            tokens["reasoning_tokens"] = reasoning
        cost = self.reported_cost
        return {"outcome": self.outcome, "http_status": self.http_status, "elapsed_ms": self.elapsed_ms,
                "dispatched": self.dispatched,
                **tokens, "charge": "reported" if cost is not None else "unknown",
                **({"cost_usd": cost} if cost is not None else {})}


@dataclass
class ModelResult:
    content: str | None
    reasoning: str | None
    error_kind: str | None  # None when the answer is usable
    attempts: list = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return self.error_kind is None

    @property
    def dispatched(self) -> bool:
        """Whether any attempt may have reached the provider (and so may be billed)."""
        return any(attempt.dispatched for attempt in self.attempts)

    @property
    def reported_cost(self):
        """The sum of the attempts' reported costs, or None if any attempt that may
        have been billed reported none (a successful one, or one with usage)."""
        total, known = 0.0, False
        for attempt in self.attempts:
            cost = attempt.reported_cost
            if cost is not None:
                total, known = total + cost, True
            elif attempt.dispatched and (attempt.outcome == "ok" or attempt.http_status is None):
                return None  # it may have been billed, and nothing says what it cost
        return total if known else None


# Routes known to reject the optional reasoning field, keyed by provider origin,
# provider, model and a digest of the key; remembered for an hour after a retry
# without the field succeeded. Cleared when provider settings change.
_negotiated: "OrderedDict[tuple, float]" = OrderedDict()


def clear_negotiation_cache() -> None:
    _negotiated.clear()


def _negotiation_key(route, key):
    return (route.provider.base_url, route.provider.name, route.model, hashlib.sha256(key.encode()).hexdigest())


def _skips_reasoning(route, key) -> bool:
    entry = _negotiation_key(route, key)
    expires = _negotiated.get(entry)
    if expires is None:
        return False
    if expires < time.monotonic():
        del _negotiated[entry]
        return False
    return True


def _remember_no_reasoning(route, key) -> None:
    entry = _negotiation_key(route, key)
    _negotiated[entry] = time.monotonic() + NEGOTIATION_TTL_SECONDS
    _negotiated.move_to_end(entry)
    while len(_negotiated) > NEGOTIATION_MAX:
        _negotiated.popitem(last=False)


def resolve_model_reasoning(route, effort: str | None, *, model_entry=None, capability_records=None,
                            zdr_enabled=False):
    """`(reasoning_object, endpoint_pin)` for this route from the single capability authority.

    A probed model on OpenRouter gets its surface's shape and the endpoint the probe
    used; anything else (an unknown model, another provider, a zero-retention request
    whose endpoint the probe did not see) gets no reasoning object. `model_entry` is
    the catalog's row for the model, which the record's fingerprint is checked against.
    """
    if not effort or not route.provider.is_openrouter or zdr_enabled:
        return None, None
    records = capability_records if capability_records is not None else reasoning_capability.load_capabilities()
    capability = reasoning_capability.get_capability(records, route.model, model_entry)
    if capability.get("control_surface") == "unknown":
        return None, None
    reasoning = resolve_reasoning_payload(capability, effort)
    return reasoning, capability.get("provider_pinned") if reasoning is not None else None


def build_payload(route, messages, *, effort=None, zdr_enabled=False, max_tokens=None, model_entry=None,
                  capability_records=None) -> dict:
    """The request body. Raises ValueError for zero-retention routing off OpenRouter,
    before any request is prepared: the `provider` field means nothing elsewhere, so
    content would go out unprotected while the caller believed it protected."""
    if zdr_enabled and not route.provider.is_openrouter:
        raise ValueError("zero-retention routing requires OpenRouter")
    payload = {"model": route.model, "messages": messages}
    if max_tokens is not None:
        payload["max_tokens"] = max_tokens
    reasoning, pin = resolve_model_reasoning(route, effort, model_entry=model_entry,
                                             capability_records=capability_records, zdr_enabled=zdr_enabled)
    provider = {}
    if zdr_enabled:
        provider["zdr"] = True  # never weakened
    if pin and route.provider.is_openrouter:
        provider["order"] = [pin]
        provider["allow_fallbacks"] = False
    if provider:
        payload["provider"] = provider
    if reasoning:
        payload["reasoning"] = reasoning
    if route.provider.is_openrouter:
        payload["usage"] = {"include": True}  # OpenRouter reports the call's cost
    return payload


async def query_model(client: httpx.AsyncClient, route, key: str, messages, *, timeout: float = 120.0,
                      effort: str | None = None, zdr_enabled: bool = False, max_tokens: int | None = None,
                      model_entry=None, capability_records=None, on_dispatch=None) -> ModelResult:
    """One chat completion. Never raises for a provider failure: the result carries
    the failure kind and every attempt. Cancellation propagates (the caller settles
    what the attempt in flight cost). on_dispatch, if given, is called when the
    outbound gate lets a request go out, so a caller cancelled before that knows
    nothing left.
    """
    extensions = {DISPATCHED: on_dispatch} if on_dispatch is not None else {}
    payload = build_payload(route, messages, effort=effort, zdr_enabled=zdr_enabled, max_tokens=max_tokens,
                            model_entry=model_entry, capability_records=capability_records)
    if "reasoning" in payload and _skips_reasoning(route, key):
        del payload["reasoning"]
    headers = {"Authorization": f"Bearer {key}", "Content-Type": "application/json"}
    deadline = time.monotonic() + timeout
    attempts = []
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return _failed("timeout", attempts, route)
        started = time.monotonic()
        try:
            response = await asyncio.wait_for(
                client.post(route.provider.chat_url, headers=headers, json=payload, extensions=extensions,
                            timeout=httpx.Timeout(remaining, connect=connect_timeout_for(remaining))),
                timeout=remaining,
            )
        except Exception as error:  # cancellation is not an Exception, and propagates
            # Refused by the gate, or no connection made: the request never left.
            sent = not isinstance(error, (OutboundDenied, httpx.ConnectError, httpx.ConnectTimeout))
            attempts.append(Attempt(classify_error(error), None, _ms(started), dispatched=sent))
            return _failed(attempts[-1].outcome, attempts, route)
        body = _json(response)
        usage = body.get("usage") if isinstance(body, dict) and isinstance(body.get("usage"), dict) else {}
        if response.status_code >= 400:
            attempts.append(Attempt(classify_status(response.status_code), response.status_code, _ms(started), usage))
            retry = (response.status_code == 400 and "reasoning" in payload and not route.provider.is_openrouter
                     and len(attempts) < MAX_ATTEMPTS)
            if retry:
                # Some OpenAI-compatible endpoints reject `reasoning` instead of ignoring it.
                log.warning("provider %s rejected the reasoning field; retrying once without it", route.provider.name)
                del payload["reasoning"]
                continue
            return _failed(attempts[-1].outcome, attempts, route)
        content, reasoning = _answer(body)
        if content is None or not content.strip():
            attempts.append(Attempt("malformed", response.status_code, _ms(started), usage))
            return _failed("malformed", attempts, route)
        attempts.append(Attempt("ok", response.status_code, _ms(started), usage))
        if len(attempts) > 1 and "reasoning" not in payload:
            _remember_no_reasoning(route, key)
        return ModelResult(content, reasoning, None, attempts)


def _count(value):
    return isinstance(value, int) and not isinstance(value, bool) and value >= 0


def _failed(kind, attempts, route):
    log.warning("model call failed: provider=%s kind=%s attempts=%d", route.provider.name, kind, len(attempts))
    return ModelResult(None, None, kind, attempts)


def _ms(started):
    return int((time.monotonic() - started) * 1000)


def _json(response):
    try:
        return response.json()
    except ValueError:
        return None


def _answer(body):
    """(content, reasoning text) from a completion body; content None if malformed."""
    try:
        message = body["choices"][0]["message"]
    except (KeyError, IndexError, TypeError):
        return None, None
    if not isinstance(message, dict):
        return None, None
    content = message.get("content")
    return (content if isinstance(content, str) else None), extract_reasoning(message)


def extract_reasoning(message: dict) -> str | None:
    """Reasoning text from the message's own fields, shortened for display. Text
    inside the content is never taken for reasoning: that needs a model whose
    capability record says it marks reasoning with tags."""
    text = message.get("reasoning")
    if not isinstance(text, str) or not text:
        details = message.get("reasoning_details")
        if isinstance(details, str):
            text = details
        else:
            blocks = details if isinstance(details, list) else [details] if isinstance(details, dict) else []
            parts = []
            for block in blocks:
                if isinstance(block, dict):
                    if isinstance(block.get("text"), str):
                        parts.append(block["text"])
                    elif isinstance(block.get("summary"), str):
                        parts.append(block["summary"])
            text = "\n\n".join(parts)
    if not text:
        return None
    return text if len(text) <= REASONING_SHOWN_CHARS else text[:REASONING_SHOWN_CHARS] + "\n...(reasoning truncated)"


def reasoning_tokens_from_usage(usage):
    """The reasoning tokens a call spent, or None when none were reported. The only
    reasoning signal reported after a call: never the presence of reasoning text,
    which can be non-empty while reasoning_tokens is 0."""
    details = (usage or {}).get("completion_tokens_details") or {}
    tokens = details.get("reasoning_tokens") if isinstance(details, dict) else None
    if isinstance(tokens, (int, float)) and not isinstance(tokens, bool) and tokens > 0:
        return int(tokens)
    return None
