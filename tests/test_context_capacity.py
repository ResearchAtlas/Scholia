"""The context assembler's capacity policy and the window W (slice-1 spec section 8; section 14's
test_context_capacity.py row).

The final serialized requests, with tool definitions and Chinese text, at 8K, 16K, 32K and 128K
windows and for the worked example; counting; the windows a route may use, including one whose
provider's catalog has not been read and one whose endpoints accept different windows (the M2
charter's model-route rule 3). Model calls go to a test-owned mock.
"""

import asyncio
import json
import math
import random
import string
from pathlib import Path

import httpx
import pytest

from backend import context, openrouter_client, providers
from backend.settings import _CONTEXT
from scholia_app import MockProvider, send, started

BUDGETS = {name: default for name, (default, _) in _CONTEXT.items()}
RULES = "You are Scholia, a research assistant. Answer in the language of the researcher's message."
TOOLS = [{"type": "function", "function": {"name": "search_passages", "description": "Search the library's passages",
                                           "parameters": {"type": "object", "properties": {"query": {"type": "string"}}}}}]
ENGLISH = "Cohort studies follow a group of people over time to see who develops an outcome. "
CHINESE = "队列研究在一段时间内跟踪一组人，观察谁出现了某种结果。"


async def rows(client, sql, *args):
    return await asyncio.to_thread(client.state["db"].read, lambda conn: conn.execute(sql, args).fetchall())


@pytest.fixture(autouse=True)
def fresh_catalog():
    openrouter_client.clear_cache()
    yield
    openrouter_client.clear_cache()


def history(turns):
    return [(f"question {i} {CHINESE}", f"answer {i} {ENGLISH * 20}") for i in range(turns)][::-1]  # newest first


def build(window, **options):
    options = {"budgets": BUDGETS, "system_rules": RULES, "request": "Compare these two cohorts. 比较这两个队列。",
               "output": BUDGETS["output_limit"], **options}
    return context.assemble("answer", window=window, **options)


@pytest.mark.parametrize("window", [8192, 16384, 32768, 131072])
def test_the_serialized_request_fits_its_window_with_tools_and_chinese_text(window):
    request = build(window, tools=TOOLS, history=history(200), sections={
        "agents_md": ["Use British spelling. 使用英式拼写。"],
        "research_evidence": [f"[S{i}.1] {ENGLISH * 6} {CHINESE * 3}" for i in range(1, 30)],
        "conflicts": ["No passage reports the follow-up length. 没有段落报告随访时长。"]})
    assert request.max_tokens == max(1024, min(8000, window // 4))  # the output limit, sent as the maximum
    margin = math.ceil(window * 5 / 100)
    # Counted on the serialized request, tool definitions included: it and its output limit fit W.
    assert request.input_tokens == context.serialized_tokens(request.messages, request.tools)
    assert request.input_tokens + request.max_tokens + margin <= window
    assert request.tools == TOOLS
    # Protected sections are whole; evidence and history are cut only in whole records, and traced.
    system = request.messages[0]["content"]
    assert system.startswith(RULES) and "## AGENTS.md\nUse British spelling. 使用英式拼写。" in system
    assert request.messages[-1] == {"role": "user", "content": "Compare these two cohorts. 比较这两个队列。"}
    kept = [record.removeprefix("## RESEARCH EVIDENCE\n") for record in system.split("\n\n")
            if record.startswith(("[S", "## RESEARCH EVIDENCE"))]
    assert kept == [f"[S{i}.1] {ENGLISH * 6} {CHINESE * 3}" for i in range(1, len(kept) + 1)]  # by rank, whole
    turns = request.messages[1:-1]
    assert len(turns) % 2 == 0 and (turns or window < 32768)  # evidence comes first; small windows keep no turn
    assert not turns or turns[-2]["content"] == history(200)[0][0]  # the newest turn kept, last
    assert request.cuts["history"] == 200 - len(turns) // 2 and request.cuts["research_evidence"] == 29 - len(kept)


def test_the_worked_example():
    # A 16,384-token window with no local tokenizer: an output limit of 4,096, a margin of 820, and
    # 11,468 tokens for framing, the protected and small sections, evidence and history.
    request = build(16384, request="hi")
    assert request.max_tokens == 4096 and math.ceil(16384 * 5 / 100) == 820
    assert 16384 - 4096 - 820 == 11468
    room = 11468 - (2 * context.MESSAGE_FRAMING + context.REPLY_FRAMING)
    fill = " ".join(["x"] * (room - context.count(RULES)))  # the protected sections fill it exactly
    filler = build(16384, request=fill)
    assert filler.input_tokens + filler.max_tokens + 820 <= 16384
    with pytest.raises(context.OverWindow) as over:
        build(16384, request=fill + " x")
    assert over.value.code == "message_over_window"


def test_sections_come_labeled_in_the_decided_order_and_empty_ones_are_left_out():
    sections = {name: [f"{name} record"] for name in context.LABELS}
    system = build(131072, sections=sections).messages[0]["content"]
    positions = [system.index(f"## {label}\n") for label in context.LABELS.values()]
    assert positions == sorted(positions) and system.index(RULES) == 0
    assert list(context.LABELS.values()) == ["AGENTS.md", "SKILL CATALOG", "ACTIVE SKILLS", "PROJECT STATE",
                                             "USER MEMORY", "RESEARCH EVIDENCE", "MODEL-GENERATED INFERENCE",
                                             "CONFLICTS AND MISSING INFORMATION"]
    bare = build(131072, sections={"user_memory": [], "research_evidence": [""]}).messages[0]["content"]
    assert bare == RULES  # nothing to say: no label


def test_protected_sections_are_never_cut_and_over_capacity_the_step_is_refused_naming_what():
    with pytest.raises(context.OverWindow) as over:
        build(8192, request=ENGLISH * 400)
    assert over.value.code == "message_over_window"
    with pytest.raises(context.OverWindow) as over:
        build(8192, sections={"agents_md": [ENGLISH * 400]})
    assert over.value.code == "instructions_over_window"


def test_small_sections_drop_their_lowest_ranked_records_whole():
    records = [f"memory {i}: {ENGLISH}" for i in range(100)]  # far over USER MEMORY's 1,000 tokens
    request = build(131072, sections={"user_memory": records})
    kept = [r for r in request.messages[0]["content"].split("\n\n") if r.startswith(("memory", "## USER MEMORY"))]
    kept[0] = kept[0].removeprefix("## USER MEMORY\n")
    assert kept == records[:len(kept)] and 0 < len(kept) < 100 and request.cuts == {"user_memory": 100 - len(kept)}


def test_a_pipeline_needing_evidence_refuses_when_too_little_room_is_left_unless_it_fits_whole():
    # Final review F15: under 2,000 tokens left for evidence, evidence that does not fit whole refuses;
    # a short abstract that fits is used as it is.
    crowded = {"agents_md": [ENGLISH * 15]}
    with pytest.raises(context.OverWindow) as over:
        build(4096, sections={**crowded, "research_evidence": [ENGLISH * 30] * 4}, needs_evidence=True)
    assert over.value.code == "evidence_over_window"
    abstract = "Abstract. " + ENGLISH * 3  # under 200 tokens as counted
    request = build(4096, sections={**crowded, "research_evidence": [abstract]}, needs_evidence=True)
    assert abstract in request.messages[0]["content"]
    assert build(4096, sections={**crowded, "research_evidence": [ENGLISH * 30] * 4}).cuts  # no pipeline: cut


@pytest.mark.parametrize("text, at_least", [
    ("hello world", 2), ("1234567890", 10), ("。，！", 9), ("队列研究", 12), ("naïve café", 4), ("a  b", 3),
])
def test_counting_is_conservative_for_latin_and_han_text(text, at_least):
    assert context.count(text) >= at_least


# Adversarial texts, each from its own seeded generator, and the most tokens cl100k_base or o200k_base
# gives each: measured with their vocabularies and pre-tokenizers on this Mac (the vocabularies VS Code
# ships; nothing downloaded), as the external review asked (S1-19 part A, A1).
_UNMERGED = sorted(context.UNMERGED)
_L = string.ascii_lowercase


def _words(rng, k, lo, hi, sep=" ", cap=False):
    out = ("".join(rng.choice(_L) for _ in range(rng.randint(lo, hi))) for _ in range(k))
    return sep.join(w.capitalize() if cap else w for w in out)


def _pick(seed, choices, n, sep=""):
    rng = random.Random(seed)
    return sep.join(rng.choice(choices) for _ in range(n))


def _adversarial():
    def r(seed):
        return random.Random(seed)
    return {
        "qzx x2000": " ".join(["qzx"] * 2000),
        "short random lowercase": _words(r(1), 3000, 1, 6),
        "short random lowercase by line": _words(r(2), 3000, 1, 4, "\n"),
        "capitalized random": _words(r(3), 2000, 2, 8, cap=True),
        "unmerged pairs chained": _pick(4, _UNMERGED, 2000, ""),
        "unmerged pairs spaced": _pick(5, _UNMERGED, 2000, " "),
        "long random lowercase": _pick(6, _L, 6000, ""),
        "random mixed case": _pick(7, string.ascii_letters, 6000, ""),
        "random upper case": _pick(8, string.ascii_uppercase, 6000, ""),
        "digits spaced": _pick(9, "0123456789", 3000, " "),
        "punctuation runs": _pick(10, string.punctuation, 6000, ""),
        "punctuation and letters": _pick(11, string.punctuation + _L, 6000, ""),
        "mixed scripts": _pick(12, ["q", "z", "é", "ж", "队", "😀", " ", "ß", "\u0301", "ا", "1", "."], 6000),
        "printable ASCII": _pick(13, string.printable, 6000, ""),
        "base64-like": _pick(14, string.ascii_letters + string.digits + "+/=", 6000, ""),
    }


MEASURED = {
    "qzx x2000": 4000, "short random lowercase": 6389, "short random lowercase by line": 7656,
    "capitalized random": 6073, "unmerged pairs chained": 2710, "unmerged pairs spaced": 3460,
    "long random lowercase": 3224, "random mixed case": 3979, "random upper case": 3573, "digits spaced": 5999,
    "punctuation runs": 3969, "punctuation and letters": 4172, "mixed scripts": 5992, "printable ASCII": 4625,
    "base64-like": 4311,
}


@pytest.mark.parametrize("name", list(MEASURED))
def test_counting_is_never_below_cl100k_or_o200k_on_adversarial_text(name):
    # Short random lower-case runs (2,000 "qzx" are 4,000 tokens), letter pairs neither vocabulary
    # joins, digits, punctuation runs, mixed scripts and encoded text.
    assert context.count(_adversarial()[name]) >= MEASURED[name]


def test_the_letter_bound_follows_what_bpe_can_leave():
    # No two one-letter tokens stay side by side unless their pair is never joined: "qz" is not a
    # token, so " qzx" can be three; "the" can be two at most; a mixed-case run counts each letter.
    assert (context.count(" qzx"), context.count("the"), context.count("aBcD")) == (3, 2, 4)
    assert context.count("bq" * 10) == 15  # "b" and "q" may stay apart; "q" and "b" join
    assert context.count("half \ud800 pair") >= 7  # a lone surrogate counts, rather than failing


RECORDED = Path(__file__).parent / "fixtures" / "recorded_usage.json"


def test_counting_is_never_below_what_providers_reported_for_recorded_requests():
    # Section 8, Counting: against the input tokens providers reported for recorded requests, in
    # English and Chinese. Recording them takes paid calls, which M2 never makes: they come from
    # Harold's live check after S1-19 (the M2 charter, section 9). Until then nothing is recorded.
    recorded = json.loads(RECORDED.read_text()) if RECORDED.exists() else []
    if not recorded:
        pytest.skip("no provider-reported counts recorded yet (they need a paid call; Harold's live check)")
    for entry in recorded:
        assert context.serialized_tokens(entry["request"]["messages"], entry["request"].get("tools", ())) \
            >= entry["usage"]["prompt_tokens"], entry["route"]


@pytest.mark.parametrize("table, reported, status, in_use", [
    ({}, 131072, "ok", 131072),  # the provider's report
    ({"windows": {"m": 32768}}, 131072, "ok", 32768),  # a setting lowers it
    ({"windows": {"m": 262144}}, 131072, "ok", 131072),  # and never raises it
    ({"default_window": 65536}, None, "ok", 65536),  # or fills one not reported
    ({"default_window": 65536, "windows": {"m": 16384}}, None, "ok", 16384),  # the model's own setting wins
    ({}, 4095, "too_small", 4095), ({}, 4096, "ok", 4096),
    ({}, None, "needed", None),  # neither: unusable until one is set
])
def test_the_window_in_use(table, reported, status, in_use):
    window = providers.window(table, "m", reported)
    assert (window["status"], window["in_use"]) == (status, in_use)


@pytest.mark.asyncio
async def test_a_route_whose_catalog_cannot_be_read_is_refused_before_any_call(tmp_path):
    # status.md's S1-19 follow-up: the window of a route whose provider's catalog has not been read is
    # unknown, so the route is refused, with nothing written, reserved or sent.
    class Down(MockProvider):
        async def __call__(self, request):
            if request.url.path.endswith(("/models", "/zdr")):
                self.requests.append((request.method, request.url.path, None))
                return httpx.Response(503, json={"error": {"message": "down"}})
            return await super().__call__(request)

    async with started(tmp_path / "data", Down()) as client:
        conversation = (await client.post("/api/conversations", json={})).json()["id"]
        response = await client.post(f"/api/conversations/{conversation}/message/stream", json={"content": "hi"})
        assert (response.status_code, response.json()["code"]) == (400, "catalog_unread")
        assert client.provider.chats == []
        assert await rows(client, "SELECT count(*) FROM runs WHERE kind = 'turn'") == [(0,)]
        assert await rows(client, "SELECT count(*) FROM budget_reservations") == [(0,)]


@pytest.mark.asyncio
async def test_the_window_is_the_smallest_any_reachable_endpoint_accepts(tmp_path):
    # Model-route rule 3: one endpoint takes 128K, another 16K, so the request is sized for 16K and no
    # provider-side compression cuts it; the step records the window and the endpoints.
    model = "google/gemini-3.1-pro-preview"  # what Auto picks first
    provider = MockProvider(endpoints={model: [
        {"tag": "big", "context_length": 131072, "pricing": {"prompt": "0.000002", "completion": "0.00001"}},
        {"tag": "small", "context_length": 16384, "pricing": {"prompt": "0.000001", "completion": "0.000012"}}]})
    async with started(tmp_path / "data", provider) as client:
        conversation = (await client.post("/api/conversations", json={})).json()["id"]
        response = await client.post(f"/api/conversations/{conversation}/message/stream",
                                     json={"content": "x " * 12000})
        assert (response.status_code, response.json()["code"]) == (400, "message_over_window")
        assert provider.chats == []
        await send(client, conversation, "A short question.")
        [body] = provider.answers
        assert body["max_tokens"] == 4096  # a quarter of 16,384
        [(step,)] = await rows(client, "SELECT r.data FROM run_events r JOIN runs u ON u.id = r.run_id"
                                       " WHERE r.type = 'step_started' AND u.kind = 'turn'")
        step = json.loads(step)
        assert (step["window"], step["endpoints"], step["max_tokens"]) == (16384, ["big", "small"], 4096)
        # Priced at the highest rate either endpoint publishes: 2e-6 a prompt token, 1.2e-5 an output one.
        assert step["estimate_usd"] == pytest.approx(step["input_tokens"] * 2e-6 + 4096 * 1.2e-5)


@pytest.mark.asyncio
async def test_an_endpoint_that_reports_no_window_needs_a_setting_which_never_raises_a_reported_one(tmp_path):
    # Section 8 and model-route rule 3: with one endpoint at 8,192 and one that reports nothing, the
    # route is unusable until a window is set, and a larger setting still leaves W at 8,192.
    model = "google/gemini-3.1-pro-preview"
    provider = MockProvider(endpoints={model: [{"tag": "a", "context_length": 8192}, {"tag": "b"}]})
    async with started(tmp_path / "data", provider) as client:
        conversation = (await client.post("/api/conversations", json={})).json()["id"]
        refused = await client.post(f"/api/conversations/{conversation}/message/stream",
                                    json={"content": "hi", "model": model})  # chosen: Auto would pass over it
        assert (refused.status_code, refused.json()["code"]) == (400, "model_window")
        current = (await client.get("/api/settings")).json()
        await client.put("/api/settings", json={"hash": current["hash"],
                                                "updates": {"providers.openrouter.default_window": 131072}})
        await send(client, conversation, "hi", model=model)
        [body] = provider.answers
        assert body["max_tokens"] == 2048  # a quarter of 8,192


@pytest.mark.asyncio
async def test_tool_definitions_and_the_skill_catalog_are_counted_and_a_window_too_small_for_them_has_no_tools(tmp_path):
    # S1-19c (K1, section 8): an agent step's request counts its tool definitions and holds the SKILL CATALOG; at
    # 4,096 the definitions leave no room, so the turn runs as chat with the library and says skills are unavailable.
    model = "google/gemini-3.1-pro-preview"
    provider = MockProvider(endpoints={model: [{"tag": "a", "context_length": 4096}]})
    async with started(tmp_path / "data", provider) as client:
        conversation = (await client.post("/api/conversations", json={})).json()["id"]
        stream = await send(client, conversation, "hi", model=model)
        assert stream[-1]["status"] == "succeeded"
        [body] = provider.answers
        assert "tools" not in body and "SKILL CATALOG" not in body["messages"][0]["content"]
        assert [e for e in stream if e["type"] == "chat_response"][0]["notices"] == ["skills_unavailable"]
        provider.endpoints[model] = [{"tag": "a", "context_length": 131072}]
        from backend import openrouter_client
        openrouter_client.forget_endpoints(provider_route(client), model)
        await send(client, conversation, "hi again", model=model)
        body = provider.answers[-1]
        assert body["tools"] and "## SKILL CATALOG\n- help: " in body["messages"][0]["content"]
        [step] = [json.loads(d) for (d,) in await asyncio.to_thread(client.state["db"].read, lambda conn: conn.execute(
            "SELECT data FROM run_events WHERE type = 'step_started' ORDER BY rowid DESC LIMIT 1").fetchall())]
        assert step["input_tokens"] == context.serialized_tokens(body["messages"], body["tools"])


def provider_route(client):
    from backend import providers
    from backend.settings import load_settings
    return providers.configured(client.state["data_dir"], load_settings(client.state["data_dir"]))["openrouter"]
