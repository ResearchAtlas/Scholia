"""evals/memory_scenarios (S1-21a; slice-1 spec section 14, "Memory behaves as specified", 100%; Harold's spec 26.3;
checklist E1): the draft's shape (about 40 scenarios, half English and half Chinese, every 26.3 category and section
14's four orderings, each with an exact oracle) and every scenario replayed offline through the app's own memory code
with recorded extractor replies. Live accuracy on real extractors is [PAID]: never run in M2."""

import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "evals"))

import memory_scenarios  # noqa: E402


def test_the_draft_covers_every_category_ordering_and_language_with_an_oracle_each():
    scenarios = memory_scenarios.load()
    assert 36 <= len(scenarios) <= 44
    languages = Counter(s["language"] for s in scenarios)
    assert languages["en"] == languages["zh"] == len(scenarios) // 2
    for language in ("en", "zh"):
        mine = [s for s in scenarios if s["language"] == language]
        assert {s["category"] for s in mine} == set(memory_scenarios.CATEGORIES)
        assert {s["ordering"] for s in mine} - {None} == set(memory_scenarios.ORDERINGS)
    assert len({s["id"] for s in scenarios}) == len(scenarios)
    assert all(s["oracle"] and s["steps"] and s["title"] for s in scenarios)


def test_every_scenario_holds_offline():
    failed = {s["id"]: problem for s in memory_scenarios.load() if (problem := memory_scenarios.run(s)) is not None}
    assert failed == {}
