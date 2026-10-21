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
        assert step["judge_estimate_usd"] > 0 and step["allowance_usd"] == step["judge_estimate_usd"]
        tight, _ = await with_papers(client, WAGES)
        current = (await client.get("/api/settings", params={"project_id": tight})).json()
        await client.put("/api/settings", json={"project_id": tight, "hash": current["hash"], "updates": {
            "project.budget_usd": round(step["estimate_usd"] + step["judge_estimate_usd"] / 2, 6)}})  # the answer alone fits
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


# The judge's budget (Harold, decided 2026-10-21, option C): every pair judged, 40 to a call, each sentence as
# delivered; a typical check's share reserved with the answer; more from the answer's unspent reservation, then
# the budget's room; only a call neither covers is left unchecked, for budget, said and counted in Details.

LONG = "and the effect held in every region, every year and every firm size of the synthetic panel " * 4


def long_answer(pairs):
    return " ".join(f"Claim {n} holds {LONG}[S1.1]." for n in range(1, pairs + 1))


async def checked_turn(client, conversation, stream):
    turn = await turn_of(client, conversation)
    steps = [json.loads(d) for (d,) in await rows(client, "SELECT data FROM run_events WHERE run_id = ?"
                                                          " AND type = 'step_started' ORDER BY seq", stream[0]["run_id"])]
    [(checks,)] = await rows(client, "SELECT data FROM run_events WHERE run_id = ? AND type = 'citation_checks'",
                             stream[0]["run_id"])
    return turn, steps, json.loads(checks)["pairs"]


async def probe(client, text):
    """The answer step's estimate and the judge's share for this answer, from a turn in a project of its own."""
    project, _ = await with_papers(client, WAGES)
    conversation = await conversation_in(client, project)
    stream = await send(client, conversation, QUESTION)
    _, steps, _ = await checked_turn(client, conversation, stream)
    return steps[0]["estimate_usd"], steps[0]["allowance_usd"]


async def project_with_budget(client, budget):
    project, _ = await with_papers(client, WAGES)
    current = (await client.get("/api/settings", params={"project_id": project})).json()
    response = await client.put("/api/settings", json={"project_id": project, "hash": current["hash"],
                                                       "updates": {"project.budget_usd": round(budget, 6)}})
    assert response.status_code == 200, response.text
    return project


async def test_eighty_one_pairs_are_all_judged_in_calls_of_forty_and_a_long_sentence_whole(tmp_path):
    provider = Provider(answer_text=long_answer(81))
    async with cited(tmp_path, provider) as client:
        project, _ = await with_papers(client, WAGES)
        conversation = await conversation_in(client, project)
        stream = await send(client, conversation, QUESTION)
        assert [b["messages"][-1]["content"].count("Pair ") for b in provider.judged] == [40, 40, 1]
        assert f"Claim 81 holds {LONG}[S1.1]." in provider.judged[-1]["messages"][-1]["content"]  # whole, as delivered
        turn, _, pairs = await checked_turn(client, conversation, stream)
        assert {c["support"] for c in turn["citations"]} == {"supported"} and "notices" not in turn["answer"]
        assert pairs == {"judged": 81, "failed": 0, "budget": 0}


async def test_a_check_past_its_share_is_funded_from_the_answers_unspent_reservation(tmp_path):
    provider = Provider(answer_text=long_answer(41))
    async with cited(tmp_path, provider) as client:
        answer, share = await probe(client, None)
        tight = await project_with_budget(client, answer + share)  # just the answer and the share
        provider.judged.clear()
        conversation = await conversation_in(client, tight)
        stream = await send(client, conversation, QUESTION)
        turn, steps, pairs = await checked_turn(client, conversation, stream)
        judge_steps = [s for s in steps if s["phase"] == "judge"]
        assert judge_steps[0]["estimate_usd"] > share and judge_steps[0]["funded"] == "answer_unspent"
        assert pairs == {"judged": 41, "failed": 0, "budget": 0} and "notices" not in turn["answer"]


async def test_a_check_past_the_share_and_the_answers_unspent_is_funded_from_the_budgets_room(tmp_path):
    provider = Provider(answer_text=long_answer(41))
    async with cited(tmp_path, provider) as client:
        answer, share = await probe(client, None)
        provider.cost = answer  # the answer settles at its whole estimate: nothing unspent
        roomy = await project_with_budget(client, 40 * answer)
        conversation = await conversation_in(client, roomy)
        stream = await send(client, conversation, QUESTION)
        turn, steps, pairs = await checked_turn(client, conversation, stream)
        assert [s for s in steps if s["phase"] == "judge"][0]["funded"] == "budget"
        assert pairs == {"judged": 41, "failed": 0, "budget": 0}


async def test_a_check_nothing_can_fund_is_left_unchecked_for_budget_and_said_never_past_the_budget(tmp_path):
    provider = Provider(answer_text=long_answer(41))
    async with cited(tmp_path, provider) as client:
        answer, share = await probe(client, None)
        provider.cost = answer  # nothing unspent
        budget = answer + share
        tight = await project_with_budget(client, budget)
        provider.judged.clear()
        conversation = await conversation_in(client, tight)
        stream = await send(client, conversation, QUESTION)
        assert stream[-1]["status"] == "succeeded" and provider.judged == []
        turn, _, pairs = await checked_turn(client, conversation, stream)
        assert pairs == {"judged": 0, "failed": 0, "budget": 41}
        assert "unchecked_budget" in turn["answer"]["notices"] and "unchecked" not in turn["answer"]["notices"]
        assert {c["support"] for c in turn["citations"]} == {"unchecked"}
        [(spent,)] = await rows(client, "SELECT coalesce(sum(CASE status WHEN 'settled' THEN settled_usd WHEN 'open'"
                                        " THEN estimate_usd ELSE 0 END), 0) FROM budget_reservations WHERE project_id = ?",
                                tight)
        assert spent <= budget + 1e-9 and await rows(
            client, "SELECT count(*) FROM budget_reservations WHERE status = 'open'") == [(0,)]
