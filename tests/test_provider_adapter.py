"""The provider adapter: bounded attempts and time, error kinds, usage per attempt.

A model call makes at most two HTTP attempts: the second only when an
OpenAI-compatible endpoint rejects the optional reasoning field, retried without
it and remembered for that route. No other failure is retried. Requests go
through a mock transport, never the network.
"""

import asyncio

import httpx
import pytest

from backend import openrouter
from backend.outbound_gate import OutboundDenied
from backend.providers import Provider, Route

pytestmark = pytest.mark.asyncio
OPENROUTER = Route(Provider("openrouter", "openrouter", "https://openrouter.ai/api/v1"), "test/model")
LOCAL = Route(Provider("local", "openai-compatible", "http://127.0.0.1:11434/v1"), "llama")
MESSAGES = [{"role": "user", "content": "hello"}]


def ok(text="Hi.", **usage):
    return httpx.Response(200, json={"choices": [{"message": {"content": text}}], "usage": usage})


def client_for(*responses):
    """A client whose transport answers in turn with responses (or raises them)."""
    sent = []

    async def handle(request):
        sent.append(request)
        item = responses[len(sent) - 1] if len(sent) <= len(responses) else ok()
        if isinstance(item, BaseException):
            raise item
        if callable(item):
            return await item(request)
        return item

    return httpx.AsyncClient(transport=httpx.MockTransport(handle)), sent


@pytest.fixture(autouse=True)
def fresh_cache():
    openrouter.clear_negotiation_cache()
    yield
    openrouter.clear_negotiation_cache()


@pytest.fixture
def takes_reasoning(monkeypatch):
    """Stand in for a model whose capability record says it takes a reasoning object."""
    monkeypatch.setattr(openrouter, "resolve_model_reasoning",
                        lambda route, effort, **kw: ({"effort": effort}, None) if effort else (None, None))


async def test_a_successful_call_reports_its_attempt_and_cost():
    client, sent = client_for(ok("Hello.", prompt_tokens=3, completion_tokens=2, total_tokens=5, cost=0.0012))
    result = await openrouter.query_model(client, OPENROUTER, "key", MESSAGES)
    assert (result.ok, result.content, result.reported_cost, result.dispatched) == (True, "Hello.", 0.0012, True)
    [attempt] = result.attempts
    assert attempt.record() == {"outcome": "ok", "http_status": 200, "elapsed_ms": attempt.elapsed_ms,
                                "dispatched": True, "prompt_tokens": 3, "completion_tokens": 2, "total_tokens": 5,
                                "charge": "reported", "cost_usd": 0.0012}
    body = sent[0].read()
    assert b'"usage": {"include": true}' in body or b'"usage":{"include":true}' in body  # OpenRouter reports cost
    assert sent[0].headers["authorization"] == "Bearer key"


async def test_an_openai_compatible_provider_gets_no_openrouter_fields():
    client, sent = client_for(ok())
    await openrouter.query_model(client, LOCAL, "key", MESSAGES, effort="high")
    import json
    body = json.loads(sent[0].read())
    assert body == {"model": "llama", "messages": MESSAGES}  # no usage, provider or reasoning object


async def test_an_unknown_model_gets_no_reasoning_object():
    client, sent = client_for(ok())
    await openrouter.query_model(client, OPENROUTER, "key", MESSAGES, effort="high")
    assert b"reasoning" not in sent[0].read()


async def test_zero_retention_off_openrouter_is_refused_before_any_request():
    client, sent = client_for(ok())
    with pytest.raises(ValueError, match="zero-retention"):
        await openrouter.query_model(client, LOCAL, "key", MESSAGES, zdr_enabled=True)
    assert sent == []


async def test_zero_retention_on_openrouter_carries_the_flag():
    client, sent = client_for(ok())
    await openrouter.query_model(client, OPENROUTER, "key", MESSAGES, zdr_enabled=True)
    import json
    assert json.loads(sent[0].read())["provider"] == {"zdr": True}


@pytest.mark.parametrize("status, kind", [
    (400, "request"), (401, "auth"), (402, "quota"), (403, "request"), (404, "request"), (408, "timeout"),
    (429, "rate_limit"), (500, "other"), (502, "other"), (503, "other"),
])
async def test_an_http_error_is_classified_and_never_retried(status, kind):
    client, sent = client_for(httpx.Response(status, json={"error": {"message": "secret body text"}}))
    result = await openrouter.query_model(client, OPENROUTER, "key", MESSAGES, effort="high")
    assert (result.ok, result.error_kind, len(sent)) == (False, kind, 1)
    assert result.attempts[0].http_status == status


@pytest.mark.parametrize("error, kind, dispatched", [
    (httpx.ConnectError("no route"), "network", False),
    (httpx.ConnectTimeout("slow connect"), "network", False),
    (OutboundDenied("not_allowed_at_level", None), "refused", False),
    (httpx.ReadTimeout("slow read"), "timeout", True),
    (httpx.ReadError("reset"), "network", True),
    (httpx.RemoteProtocolError("bad"), "other", True),
])
async def test_a_transport_error_is_classified_and_says_whether_the_request_left(error, kind, dispatched):
    client, sent = client_for(error)
    result = await openrouter.query_model(client, OPENROUTER, "key", MESSAGES)
    assert (result.error_kind, result.dispatched, len(sent)) == (kind, dispatched, 1)
    assert result.reported_cost is None


@pytest.mark.parametrize("body", [
    {"choices": [{"message": {"content": "   "}}], "usage": {"cost": 0.003}},
    {"choices": [{"message": {"content": None}}], "usage": {"cost": 0.003}},
    {"choices": [], "usage": {"cost": 0.003}},
    {"usage": {"cost": 0.003}},
])
async def test_usage_survives_a_malformed_answer(body):
    client, _ = client_for(httpx.Response(200, json=body))
    result = await openrouter.query_model(client, OPENROUTER, "key", MESSAGES)
    assert (result.error_kind, result.reported_cost) == ("malformed", 0.003)


async def test_an_ok_attempt_without_usage_has_no_reported_cost():
    client, _ = client_for(ok())
    result = await openrouter.query_model(client, OPENROUTER, "key", MESSAGES)
    assert result.ok and result.reported_cost is None
    assert result.attempts[0].record()["charge"] == "unknown"


async def test_a_rejected_reasoning_field_is_retried_once_without_it_and_remembered(takes_reasoning):
    import json
    client, sent = client_for(httpx.Response(400, json={"error": "unknown field reasoning"}), ok(cost=0.001))
    result = await openrouter.query_model(client, LOCAL, "key", MESSAGES, effort="high")
    assert result.ok and len(sent) == 2 and len(result.attempts) == 2
    assert "reasoning" in json.loads(sent[0].read()) and "reasoning" not in json.loads(sent[1].read())
    assert result.reported_cost == 0.001  # the rejected attempt reported nothing and was not billed

    client, sent = client_for(ok())  # remembered: the field is no longer sent for this route and key
    await openrouter.query_model(client, LOCAL, "key", MESSAGES, effort="high")
    assert "reasoning" not in json.loads(sent[0].read()) and len(sent) == 1
    client, sent = client_for(ok())  # another key is another negotiation
    await openrouter.query_model(client, LOCAL, "other-key", MESSAGES, effort="high")
    assert "reasoning" in json.loads(sent[0].read())


@pytest.mark.parametrize("second", [
    httpx.Response(400, json={"error": "still bad"}),
    httpx.Response(500, json={"error": "down"}),
    httpx.ReadTimeout("slow"),
])
async def test_a_failed_negotiation_stops_at_two_attempts_and_is_not_remembered(takes_reasoning, second):
    import json
    client, sent = client_for(httpx.Response(400, json={}), second)
    result = await openrouter.query_model(client, LOCAL, "key", MESSAGES, effort="high")
    assert not result.ok and len(sent) == 2
    client, sent = client_for(ok())
    await openrouter.query_model(client, LOCAL, "key", MESSAGES, effort="high")
    assert "reasoning" in json.loads(sent[0].read())


async def test_a_500_then_nothing_more_even_with_reasoning(takes_reasoning):
    client, sent = client_for(httpx.Response(500, json={}), ok())
    result = await openrouter.query_model(client, LOCAL, "key", MESSAGES, effort="high")
    assert result.error_kind == "other" and len(sent) == 1


async def test_openrouter_never_drops_the_reasoning_field_on_a_400(takes_reasoning):
    client, sent = client_for(httpx.Response(400, json={}), ok())
    result = await openrouter.query_model(client, OPENROUTER, "key", MESSAGES, effort="high")
    assert result.error_kind == "request" and len(sent) == 1


async def test_the_negotiation_memory_expires_and_is_bounded(takes_reasoning, monkeypatch):
    import json
    client, _ = client_for(httpx.Response(400, json={}), ok())
    await openrouter.query_model(client, LOCAL, "key", MESSAGES, effort="high")
    monkeypatch.setattr(openrouter, "NEGOTIATION_TTL_SECONDS", -1)
    openrouter._negotiated.clear()
    client, _ = client_for(httpx.Response(400, json={}), ok())
    await openrouter.query_model(client, LOCAL, "key", MESSAGES, effort="high")  # remembered, already expired
    client, sent = client_for(ok())
    await openrouter.query_model(client, LOCAL, "key", MESSAGES, effort="high")
    assert "reasoning" in json.loads(sent[0].read())
    monkeypatch.setattr(openrouter, "NEGOTIATION_TTL_SECONDS", 3600)
    for n in range(openrouter.NEGOTIATION_MAX + 10):
        openrouter._remember_no_reasoning(Route(LOCAL.provider, f"model-{n}"), "key")
    assert len(openrouter._negotiated) == openrouter.NEGOTIATION_MAX


async def test_both_attempts_share_one_total_time_bound(takes_reasoning):
    async def slow_rejection(request):
        await asyncio.sleep(0.3)
        return httpx.Response(400, json={})

    async def slow_answer(request):
        await asyncio.sleep(5)
        return ok()

    client, sent = client_for(slow_rejection, slow_answer)
    loop = asyncio.get_running_loop()
    started = loop.time()
    result = await openrouter.query_model(client, LOCAL, "key", MESSAGES, effort="high", timeout=0.6)
    assert result.error_kind == "timeout" and len(sent) == 2
    assert loop.time() - started < 1.5


async def test_cancellation_propagates_to_the_caller():
    async def never(request):
        await asyncio.sleep(60)

    client, _ = client_for(never)
    task = asyncio.create_task(openrouter.query_model(client, OPENROUTER, "key", MESSAGES))
    await asyncio.sleep(0.05)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task


async def test_reasoning_text_comes_only_from_the_messages_own_fields():
    assert openrouter.extract_reasoning({"content": "<think>hidden</think>answer"}) is None
    assert openrouter.extract_reasoning({"reasoning": "step one"}) == "step one"
    assert openrouter.extract_reasoning({"reasoning_details": [{"text": "a"}, {"summary": "b"}, "x"]}) == "a\n\nb"
    long = openrouter.extract_reasoning({"reasoning": "x" * 3000})
    assert len(long) < 2100 and long.endswith("(reasoning truncated)")
    assert openrouter.reasoning_tokens_from_usage({"completion_tokens_details": {"reasoning_tokens": 0}}) is None
    assert openrouter.reasoning_tokens_from_usage({"completion_tokens_details": {"reasoning_tokens": 7}}) == 7
