"""evals/skill_choice (S1-33; SPEC:1182, 1195): the set's shape, and the offline replay's scoring through the agent loop
with recorded replies. Nothing is measured of a real model here (the paid evaluation, after M2)."""

import asyncio
import sys
from collections import Counter
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "evals"))

import skill_choice  # noqa: E402
from backend import agent, skills  # noqa: E402


def test_the_set_holds_150_labeled_messages_75_in_each_language():
    rows = skill_choice.load()
    assert len(rows) == 150 and len({r["id"] for r in rows}) == 150
    assert Counter(r["language"] for r in rows) == {"en": 75, "zh-CN": 75}
    tools = {t["function"]["name"] for t in agent.definitions()}  # the registry's tools
    for row in rows:
        assert row["expected_skill"] in (*skill_choice.SKILLS, None), row["id"]
        assert set(row["required_tools"]) <= tools and set(row["forbidden_tools"]) <= tools, row["id"]
        assert not set(row["required_tools"]) & set(row["forbidden_tools"]), row["id"]
        assert row["message"].strip()
    loaded, _ = skills.load()
    assert set(skill_choice.SHIPPED) == set(loaded)  # the preview is scored on exactly the skills it ships
    for language in skill_choice.LANGUAGES:  # every label, and none, in both languages
        assert {r["expected_skill"] for r in rows if r["language"] == language} == {*skill_choice.SKILLS, None}


@pytest.mark.asyncio
async def test_the_replay_scores_skills_and_tools_from_the_runs_record(tmp_path):
    rows = {r["id"]: r for r in skill_choice.load()}
    chosen = [rows["en-001"], rows["en-016"], rows["zh-016"], rows["en-036"]]  # help; literature-review twice; none
    recorded = {
        "en-001": skill_choice.oracle(rows["en-001"]),  # right
        "en-016": [{"calls": [{"name": "use_skill", "arguments": {"name": "literature-review"}}]},
                   {"calls": [{"name": "answer_from_library", "arguments": {"question": "x"}},
                              {"name": "open_place", "arguments": {"place": "library"}}]},  # a forbidden tool
                   {"text": "Done."}],
        "zh-016": [{"calls": [{"name": "use_skill", "arguments": {"name": "help"}}]},  # the wrong skill
                   {"calls": [{"name": "answer_from_library", "arguments": {"question": "x"}}]}, {"text": "Done."}],
        "en-036": [{"text": "A cohort study follows a group over time."}],  # none, and no tool
    }
    results = {r["id"]: r for r in await skill_choice.replay(chosen, recorded)}
    assert {i: (r["skill"], r["tools"]) for i, r in results.items()} == {
        "en-001": (True, True), "en-016": (True, False), "zh-016": (False, True), "en-036": (True, True)}
    assert results["en-016"]["called"] == ["use_skill", "answer_from_library", "open_place"]
    found = skill_choice.report(list(results.values()), "test")
    assert found["preview"]["en"] == {"messages": 3, "skill": 3, "tools": 2, "invalid": 0}
    assert found["preview"]["zh-CN"] == {"messages": 1, "skill": 0, "tools": 1, "invalid": 0}
    assert found["met"] is False  # a forbidden call is never within the 100% the tools row needs
    # the versions replayed: the core prompt's and each shipped skill's (SPEC:761, reading 14)
    assert set(found["versions"]["skills"]) == set(skill_choice.SHIPPED) and len(found["versions"]["core_prompt"]) == 64


@pytest.mark.asyncio
async def test_a_replay_whose_recording_is_missing_runs_out_is_left_over_or_whose_turn_fails_never_passes(tmp_path):
    # D16 (Q2): only a turn that succeeded on its recording, step for step, is evidence: a message with no recording,
    # one whose recording ran out (no reply is invented for it), one with steps the turn never took, and one whose turn
    # did not succeed each fail both rows, said by their problem, and the set is not met.
    rows = {r["id"]: r for r in skill_choice.load()}
    chosen = [rows["en-036"], rows["en-037"], rows["en-038"], rows["zh-036"], rows["en-001"]]
    assert all(r["expected_skill"] is None and not r["required_tools"] for r in chosen[:4])
    recorded = {
        "en-036": [],  # ran out at the first step
        "en-037": [{"text": ""}],  # an empty answer: the turn fails
        "en-038": [{"text": "Done."}, {"calls": [{"name": "open_place", "arguments": {"place": "library"}}]}],
        # zh-036: no recording at all
        "en-001": skill_choice.oracle(rows["en-001"]),
    }
    results = {r["id"]: r for r in await skill_choice.replay(chosen, recorded)}
    assert {i: (r["skill"], r["tools"], r["problem"]) for i, r in results.items()} == {
        "en-036": (False, False, "recording_ran_out"), "en-037": (False, False, "run_failed"),
        "en-038": (False, False, "recording_left_over"), "zh-036": (False, False, "no_recording"),
        "en-001": (True, True, None)}
    found = skill_choice.report(list(results.values()), "test")
    assert found["met"] is False and {f["id"] for f in found["failures"]} == {"en-036", "en-037", "en-038", "zh-036"}
    assert found["preview"]["en"]["invalid"] == 3 and found["preview"]["zh-CN"]["invalid"] == 1


@pytest.mark.asyncio
async def test_a_recording_that_reaches_an_ask_ends_there_not_met_and_the_replay_goes_on(tmp_path):
    # PR 52 cycle 4 (G9, Q2; cycle 4 C's probe): a recording holds no answer for the researcher's side of an ask. The
    # replay stops that turn at the ask, never waits on it, scores it not met (unanswered_ask), and goes on to the next.
    rows = {r["id"]: r for r in skill_choice.load()}
    chosen = [rows["en-001"], rows["en-036"]]
    recorded = {
        "en-001": [{"calls": [{"name": "use_skill", "arguments": {"name": "help"}}]},
                   {"calls": [{"name": "ask", "arguments": {"question": "Which part?", "options": ["Setup", "Budgets"]}}]},
                   {"text": "Done."}],
        "en-036": [{"text": "A cohort study follows a group over time."}],
    }
    results = {r["id"]: r for r in await asyncio.wait_for(skill_choice.replay(chosen, recorded), 30)}
    assert {i: (r["skill"], r["tools"], r["problem"], r["status"]) for i, r in results.items()} == {
        "en-001": (False, False, "unanswered_ask", "cancelled"), "en-036": (True, True, None, "succeeded")}
    assert skill_choice.report(list(results.values()), "test")["preview"]["en"]["invalid"] == 1
