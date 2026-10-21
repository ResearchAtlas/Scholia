"""The routing set's rules-only run (S1-19; slice-1 spec sections 7.3 and 14; ticket 70; ticket 22).

    uv run --no-sync python -I evals/routing.py [evals/routing_set.jsonl]

`routing_set.jsonl` is Claude's draft of `evals/routing_set`: 200 requests, 100 English and 100 Chinese,
each labeled with the section 7.3 table row it belongs to (`row`), the route that row expects (`route`,
in backend/router.py's ROUTES) and whether it needs the library's evidence (`needs_retrieval`). It is
synthetic and provisional until Harold reviews the labels with the gold sets.

This run asks the router's rules alone (no small model, as with `[models] router` unset; a project with
papers, so the fallback is literature) and reports, per language and overall, how often the route
matches the label, missed retrieval (needed, not routed to the library) and needless retrieval (routed
to the library, not needed). The rates are reported, never gated: the thresholds (missed at most 5%,
needless at most 20%) apply at the preview, with the small model, on real models.
"""

import json
import sys
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from backend import router  # noqa: E402

SET = Path(__file__).with_name("routing_set.jsonl")
ROWS = {"selection": "literature", "rewrite": "none", "earlier_decision": "memory", "find_papers": "find_papers",
        "comparison": "memory_literature", "interest": "literature", "manuscript": "literature",
        "critique": "literature", "library_question": "literature", "no_retrieval": "none"}
LIBRARY = {"literature", "memory_literature"}  # the routes that pre-load the library's evidence


def load(path=SET):
    with open(path, encoding="utf-8") as f:
        return [json.loads(line) for line in f if line.strip()]


def run(items):
    """{language or "all": {"n", "matched", "missed", "needless", "by_row": {row: matched of n}}} from the rules."""
    out = {}
    for item in items:
        decision = router.decide(item["text"], selected=item["selected"], has_materials=True, small_model=False)
        routed = decision["route"] in LIBRARY
        for key in (item["language"], "all"):
            entry = out.setdefault(key, {"n": 0, "matched": 0, "by_rule": 0, "missed": 0, "needless": 0,
                                         "by_row": Counter()})
            entry["n"] += 1
            entry["by_rule"] += decision["decided_by"] == "rules"
            entry["matched"] += decision["route"] == item["route"]
            entry["missed"] += item["needs_retrieval"] and not routed
            entry["needless"] += routed and not item["needs_retrieval"]
            entry["by_row"][item["row"]] += decision["route"] == item["route"]
    return out


def report(out):
    lines = []
    for key, entry in sorted(out.items(), key=lambda item: item[0] == "all"):
        n = entry["n"]
        lines.append(f"{key}: {n} requests; decided by a rule {entry['by_rule'] / n:.1%} (the rest by the fallback, "
                     f"which the rows without a rule are labeled with); route as labeled {entry['matched'] / n:.1%}; "
                     f"missed retrieval {entry['missed'] / n:.1%}; needless retrieval {entry['needless'] / n:.1%}")
    return lines


if __name__ == "__main__":
    print("\n".join(report(run(load(Path(sys.argv[1]) if len(sys.argv) > 1 else SET)))))
