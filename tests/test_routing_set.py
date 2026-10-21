"""The routing set's draft (S1-19; slice-1 spec section 14's evals/routing_set row; ticket 70; ticket 22):
its shape and labels, and the router's rules over it, which are deterministic and replay the same. The
rules-only rates are reported by evals/routing.py, not gated (the thresholds apply at the preview)."""

import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "evals"))

import routing  # noqa: E402
from backend import router  # noqa: E402


def test_the_set_holds_200_labeled_requests_100_in_each_language():
    items = routing.load()
    assert len(items) == 200 and Counter(i["language"] for i in items) == {"en": 100, "zh": 100}
    assert len({i["id"] for i in items}) == 200 and len({i["text"] for i in items}) == 200
    for item in items:
        assert set(item) == {"id", "language", "text", "selected", "row", "route", "needs_retrieval"}, item["id"]
        assert item["route"] == routing.ROWS[item["row"]] and item["route"] in router.ROUTES, item["id"]
        assert item["needs_retrieval"] == (item["route"] in routing.LIBRARY) and item["selected"] == (item["row"] == "selection")
        han = any("一" <= ch <= "鿿" for ch in item["text"])
        assert han if item["language"] == "zh" else item["row"] == "rewrite" or not han, item["id"]
    for language in ("en", "zh"):  # every row of section 7.3's table, in both languages
        assert {i["row"] for i in items if i["language"] == language} == set(routing.ROWS)


def test_a_rule_that_fires_routes_as_labeled_and_a_run_replays_the_same():
    items = routing.load()
    for item in items:
        decision = router.decide(item["text"], selected=item["selected"], has_materials=True)
        if decision["decided_by"] == "rules":
            assert decision["route"] == item["route"], (item["id"], decision)
    first, second = routing.run(items), routing.run(items)
    assert first == second and first["all"]["n"] == 200
    assert all(entry["missed"] + entry["needless"] <= entry["n"] for entry in first.values())
    assert len(routing.report(first)) == 3
