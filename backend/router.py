"""The router: what a turn pre-fetches before its answer call (slice-1 spec section 7.3; ticket 17).

The route is a pre-fetch, not a restriction. Rules run first, on English and Chinese patterns, in the
order of section 7.3's table; the first that fires decides. When none fires, a small model on the
project's allowed routes decides within `[retrieval] router_ms` (backend/runs.py makes that call,
through the dispatch path), but only when `[models] router` is set: unset, the router uses its rules
only and makes no call (ticket 71). With no small model, or when it fails, times out or refuses, the
fallback is literature when the project has materials and no retrieval otherwise, and the fallback is
recorded.

Routes: `literature` (library evidence; with a selection, restricted to the selected papers),
`none` (no retrieval), `memory` (memory and project state) and `find_papers` (project state and
memory; outside results come only from find_papers). Memory, project state and find_papers arrive with
S1-21 and S1-27, so those routes pre-load nothing yet, and say so (scope notes, reading 4).
"""

import re
import time

ROUTES = ("literature", "none", "memory", "find_papers", "memory_literature")

# Section 7.3's rules, in its table's order: (rule, route, pattern). Word boundaries for English;
# Chinese has none, so its words match anywhere.
RULES = (
    ("rewrite", "none", re.compile(
        r"\b(rewrite|rephrase|tighten|polish|proofread|translate|explain this|explain the following)\b"
        r"|改写|润色|翻译|解释(?:一下)?(?:这|下面|以下)", re.IGNORECASE)),
    ("earlier_decision", "memory", re.compile(
        r"\b(last time|we decided|why did i|earlier we|previously we)\b|之前|上次|我们决定", re.IGNORECASE)),
    ("find_papers", "find_papers", re.compile(
        r"\b(find|search for|look for|recommend) (?:me )?(?:new |recent |more )?(papers|literature|studies|articles)\b"
        r"|\blatest research\b|找文献|最新研究|找论文", re.IGNORECASE)),
    ("comparison", "memory_literature", re.compile(
        r"\b(compare|comparison|versus|vs\.?|differ(?:ence|ences|s)? between)\b|比较|区别|对比", re.IGNORECASE)),
)
LABELS = {"literature", "none", "memory"}  # what the small model may answer


def rules(message, *, selected=False):
    """(route, the rule that fired) from the rules alone, or (None, None) when none fires. A selection
    (papers selected in the Library or @-mentioned) is the table's first row: literature, restricted to
    it, whatever else the message says."""
    if selected:
        return "literature", "selection"
    for rule, route, pattern in RULES:
        if pattern.search(message):
            return route, rule
    return None, None


def fallback(has_materials):
    """Section 7.3's fallback: literature when the project has materials, no retrieval otherwise."""
    return "literature" if has_materials else "none"


def decide(message, *, selected=False, has_materials=False, small_model=False, deadline_ms=100):
    """The rules' decision for a turn: {"route", "rule", "decided_by", "rules_ms"}. decided_by is "rules",
    "router" (a small model is set and must decide; the caller makes that call) or "fallback"."""
    started = time.monotonic()
    route, rule = rules(message, selected=selected)
    spent = round((time.monotonic() - started) * 1000, 3)
    if route is not None and spent <= deadline_ms:
        return {"route": route, "rule": rule, "decided_by": "rules", "rules_ms": spent}
    if small_model:
        return {"route": None, "rule": None, "decided_by": "router", "rules_ms": spent}
    return {"route": fallback(has_materials), "rule": None, "decided_by": "fallback", "rules_ms": spent}


ROUTER_RULES = (
    "You route a researcher's message for a research assistant. Reply with exactly one word: "
    "literature (the answer needs passages from the researcher's papers), none (it needs no retrieval: "
    "a rewrite, a translation, a greeting, general knowledge) or memory (it refers to an earlier "
    "decision or conversation). Reply with the word only."
)


def parse(text):
    """The small model's route from its reply, or None when it is not one of LABELS."""
    word = (text or "").strip().strip(".").lower()
    return word if word in LABELS else None
