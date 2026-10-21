"""The support judge and the delivered-answer checks (slice-1 spec F4, sections 6.2 and 7.5; final review
F02; ticket 71): pairs per call, the judge reserved with its answer, every failure leaving citations
unchecked and the answer saying so, sentences never removed, and a project tightened before the judge
sending nothing more. Recorded responses only: whether a real judge judges well is [PAID] (tier 3)."""

import asyncio
import json

import pytest

from backend import runs as runs_module
from cited import Provider, cited, conversation_in, events_of, rows, with_papers
from scholia_app import send
from test_search import WAGES, paper, setting

pytestmark = pytest.mark.asyncio
QUESTION = "What do minimum wages do to earnings?"


async def turn_of(client, conversation):
    return (await client.get(f"/api/conversations/{conversation}")).json()["turns"][-1]


async def test_pairs_go_up_to_the_setting_per_call_and_a_larger_batch_is_split(tmp_path):
    text = "Earnings rise [S1.1][S1.2]. Employment barely moves [S1.2]."  # three pairs
    provider = Provider(answer_text=text)
    async with cited(tmp_path, provider) as client:
        project, _ = await with_papers(client, WAGES)
        await setting(client, "retrieval.judge_pairs", 2)
        conversation = await conversation_in(client, project)
        await send(client, conversation, QUESTION)
        assert [b["messages"][-1]["content"].count("Pair ") for b in provider.judged] == [2, 1]
        turn = await turn_of(client, conversation)
        assert {c["support"] for c in turn["citations"]} == {"supported"} and "notices" not in turn["answer"]
        assert all("tool_choice" not in b and "tools" not in b for b in provider.judged)  # a role call forces no tool


async def test_forty_one_pairs_at_the_default_setting_go_in_two_calls(tmp_path):
    text = " ".join(f"Claim {n} holds [S1.1]." for n in range(1, 42))
    provider = Provider(answer_text=text)
    async with cited(tmp_path, provider) as client:
        project, _ = await with_papers(client, WAGES)
        await send(client, await conversation_in(client, project), QUESTION)
        assert [b["messages"][-1]["content"].count("Pair ") for b in provider.judged] == [40, 1]


@pytest.mark.parametrize("failure", ["error", "refusal", "unreadable", "partial", "timeout"])
async def test_a_judge_that_fails_leaves_the_citations_unchecked_and_the_answer_says_so(tmp_path, monkeypatch,
                                                                                        failure):
    async def slow(body):  # past the judge's time bound
        await asyncio.sleep(2)
        return Provider().answer("1: supported\n2: supported")

    if failure == "timeout":
        monkeypatch.setattr(runs_module, "JUDGE_SECONDS", 0.3)
    reply = {"error": (500, {"error": {"message": "down"}}),
             "refusal": (200, {"choices": [{"message": {"content": None, "refusal": "No."}}], "usage": {"cost": 0.001}}),
             "unreadable": "I think it is fine.", "partial": "1: supported", "timeout": slow}[failure]
    text = "Earnings rise [S1.1]. Employment barely moves [S1.2]."
    provider = Provider(answer_text=text, verdicts=reply)
    async with cited(tmp_path, provider) as client:
        project, _ = await with_papers(client, WAGES)
        conversation = await conversation_in(client, project)
        stream = await send(client, conversation, QUESTION)
        assert stream[-1]["status"] == "succeeded"  # the answer is still delivered
        turn = await turn_of(client, conversation)
        assert "unchecked" in turn["answer"]["notices"] and turn["answer"]["text"] == text  # never rewritten
        supports = [c["support"] for c in turn["citations"]]
        assert supports == (["supported", "unchecked"] if failure == "partial" else ["unchecked", "unchecked"])


async def test_the_judge_is_reserved_with_its_answer_and_a_budget_that_cannot_hold_both_refuses_first(tmp_path):
    async with cited(tmp_path) as client:
        project, _ = await with_papers(client, WAGES)
        conversation = await conversation_in(client, project)
        stream = await send(client, conversation, QUESTION)
        [step, *_] = [d for kind, d, _ in await events_of(client, stream[0]["run_id"]) if kind == "step_started"]
        assert step["judge_estimate_usd"] > 0 and step["estimate_usd"] > step["judge_estimate_usd"]
        tight, _ = await with_papers(client, WAGES)
        current = (await client.get("/api/settings", params={"project_id": tight})).json()
        await client.put("/api/settings", json={"project_id": tight, "hash": current["hash"], "updates": {
            "project.budget_usd": round(step["estimate_usd"] - step["judge_estimate_usd"] / 2, 6)}})
        before = len(client.provider.chats)
        refused = await send(client, await conversation_in(client, tight), QUESTION)
        assert [e["type"] for e in refused][-2:] == ["limit_reached", "run_finished"]
        assert len(client.provider.chats) == before  # neither the answer nor the judge was sent


async def test_a_project_tightened_between_the_answer_and_the_judge_sends_nothing_more(tmp_path):
    async with cited(tmp_path) as client:
        project, _ = await with_papers(client, WAGES)
        conversation = await conversation_in(client, project)
        provider = client.provider

        async def answer_then_tighten(body):
            provider.replies.append(provider._reply)
            response = await client.post(f"/api/projects/{project}/sensitivity", json={"level": "local_only"})
            assert response.status_code == 200
            return provider.answer("Earnings rise [S1.1].")
        provider.replies[:] = [answer_then_tighten]
        stream = await send(client, conversation, QUESTION)
        assert (stream[-1]["status"], stream[-1].get("cancel_reason")) == ("cancelled", "revoked")
        assert provider.judged == []  # the judge was never sent
        assert await rows(client, "SELECT count(*) FROM citations") == [(0,)]
        turn = await turn_of(client, conversation)
        assert turn["answer"] is None  # nothing published


async def test_an_answer_with_no_citation_to_a_literature_question_is_not_judged(tmp_path):
    provider = Provider(answer_text="Minimum wages raise earnings.")  # no marker at all
    async with cited(tmp_path, provider) as client:
        project, _ = await with_papers(client, WAGES)
        conversation = await conversation_in(client, project)
        await send(client, conversation, QUESTION)
        turn = await turn_of(client, conversation)
        assert turn["citations"] == [] and provider.judged == [] and "unchecked" not in turn["answer"].get("notices", [])


async def test_a_chinese_question_over_an_english_paper_is_cited_and_judged(tmp_path):
    provider = Provider(answer_text="该论文报告最低工资提高了低收入工人的收入 [S1.1]。")
    async with cited(tmp_path, provider) as client:
        project, _ = await with_papers(client, WAGES)
        conversation = await conversation_in(client, project)
        await send(client, conversation, "最低工资对收入有什么影响？ earnings")
        turn = await turn_of(client, conversation)
        [chip] = turn["citations"]
        assert chip["existence"] == "ok" and chip["sentence"].startswith("该论文报告")
        assert json.loads(json.dumps(provider.judged[0]))["messages"][-1]["content"].count("Pair ") == 1


# The judge's cap and its share (coordinator's reading B, 2026-10-21): at most judge_calls calls of judge_pairs
# pairs; the pairs past that are unchecked, said; the share reserved with the answer bounds every judge call.

@pytest.mark.parametrize("pairs, over", [(80, 0), (81, 1)])
async def test_eighty_pairs_are_judged_in_two_calls_and_the_eighty_first_is_left_unchecked_and_said(tmp_path, pairs, over):
    text = " ".join(f"Claim {n} holds [S1.1]." for n in range(1, pairs + 1))
    provider = Provider(answer_text=text)
    async with cited(tmp_path, provider) as client:
        project, _ = await with_papers(client, WAGES)
        conversation = await conversation_in(client, project)
        stream = await send(client, conversation, QUESTION)
        assert [b["messages"][-1]["content"].count("Pair ") for b in provider.judged] == [40, 40]
        turn = await turn_of(client, conversation)
        supports = [c["support"] for c in turn["citations"]]
        assert supports.count("supported") == 80 and supports.count("unchecked") == over
        notices = turn["answer"].get("notices", [])
        assert ("citations_over_limit" in notices) == bool(over) and ("unchecked" in notices) == bool(over)
        [(checks,)] = await rows(client, "SELECT data FROM run_events WHERE run_id = ? AND type = 'citation_checks'",
                                 stream[0]["run_id"])
        assert json.loads(checks).get("over_limit", 0) == over
        steps = [json.loads(d) for (d,) in await rows(client, "SELECT data FROM run_events WHERE run_id = ?"
                                                              " AND type = 'step_started' ORDER BY seq", stream[0]["run_id"])]
        share = steps[0]["allowance_usd"]
        assert sum(step["estimate_usd"] for step in steps if step["phase"] == "judge") <= share  # within the share


async def test_a_judge_calls_estimate_never_exceeds_its_part_of_the_share_at_the_caps_worst_case():
    # The largest passages the evidence allows, the most pairs the cap allows, and sentences far past the cut
    # in text our counting finds dearest (upper case, digits, punctuation, Han, emoji, space runs).
    from backend import budget_router, context, settings
    from backend.citations import JUDGE_RULES, batches, judge_plan, judge_request
    retrieval = settings._RETRIEVAL
    pairs, calls, units = retrieval["judge_pairs"][0], retrieval["judge_calls"][0], retrieval["judge_sentence_units"][0]
    dear = "ABC 123 !? 工资 😀    " * 200
    passages = {f"p{i}": dear[:2000 - i] for i in range(8)}
    rates = {"input": 3e-6, "output": 15e-6, "request": 0.0}
    for window in (8192, 16384, 32768, 131072):
        size, bound = judge_plan(list(passages.values()), window=window, margin_percent=5, pairs=pairs, units=units)
        assert size >= 1
        share = calls * budget_router.request_cost(rates, bound, context.MIN_OUTPUT)
        wanted = [(dear * 3 + f" {n}", f"p{n % 8}") for n in range(calls * size)]
        spent = 0.0
        for batch in batches(wanted, size):
            request = context.assemble("judge", window=window, output=context.MIN_OUTPUT,
                                       budgets={k: v[0] for k, v in settings._CONTEXT.items()},
                                       margin_percent=5, system_rules=JUDGE_RULES,
                                       request=judge_request(batch, passages, units))
            assert request.input_tokens <= bound and request.max_tokens == context.MIN_OUTPUT
            spent += budget_router.request_cost(rates, request.input_tokens, request.max_tokens)
        assert spent <= share + 1e-12, window
