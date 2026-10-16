"""The context assembler: the one place a model request is built (slice-1 spec sections 6.6 and 8).

Every model call's request comes from `assemble`, and the harness sends only what it built
(backend/runs.py, Harness._call; tests/test_dispatch_path.py checks it). The request holds
labeled sections in the decided order: the system rules first, then AGENTS.md, the SKILL
CATALOG, ACTIVE SKILLS, PROJECT STATE, USER MEMORY, RESEARCH EVIDENCE, MODEL-GENERATED
INFERENCE and the conflicts list, all in the system message; then the history, oldest first;
then the current request. A section with nothing in it is left out.

The capacity policy (section 8) sizes it for the route's window W: the output limit, then the
framing and the margin, then the protected sections (never cut; over capacity, the step is
refused before any call), then the small sections, evidence and history, each taking whole
records in rank order up to the lesser of its budget and what remains. Every record left out is
recorded in `cuts`, for the step's trace. Tokens are counted with `count`, a conservative
estimate, since AAB has no route's own tokenizer.
"""

import json
import math
from dataclasses import dataclass, field

MIN_OUTPUT = 1024  # the smallest output limit a step has
MESSAGE_FRAMING = 8  # tokens a message's role and template take, counted generously
REPLY_FRAMING = 8  # the template tokens that open the reply
EVIDENCE_FLOOR = 2000  # a pipeline needing evidence refuses under this unless its evidence fits whole (F15)
LETTERS_PER_TOKEN = 3  # ponytail: fixed ratios for runs of ASCII letters; calibrate on recorded counts
WORD_LETTERS = 12  # a run longer than a word counts each letter after these

# The sections after the system rules, in the order the request holds them, with their labels.
LABELS = {
    "agents_md": "AGENTS.md", "skill_catalog": "SKILL CATALOG", "active_skills": "ACTIVE SKILLS",
    "project_state": "PROJECT STATE", "user_memory": "USER MEMORY", "research_evidence": "RESEARCH EVIDENCE",
    "inference": "MODEL-GENERATED INFERENCE", "conflicts": "CONFLICTS AND MISSING INFORMATION",
}
PROTECTED = ("agents_md", "skill_catalog")  # with the system rules and the current request
SMALL = ("project_state", "active_skills", "user_memory", "inference", "conflicts")  # the order they take capacity


class OverWindow(Exception):
    """The request cannot fit the window: code names what is too large, for the interface."""

    def __init__(self, code):
        super().__init__(code)
        self.code = code


@dataclass(frozen=True)
class Request:
    """An assembled request: what the dispatch path sends, and what it was counted as."""
    function: str  # answer, title, route, judge, ... (section 10, Review lock)
    messages: list
    max_tokens: int  # the step's output limit, sent as the request's maximum output
    input_tokens: int  # the serialized request as counted here, framing and tool definitions included
    window: int
    tools: list = field(default_factory=list)  # counted with the request; sent once the agent loop offers tools
    cuts: dict = field(default_factory=dict)  # section -> how many records were left out


def count(text: str) -> int:
    """Tokens in text, never fewer than a provider counts (section 8, Counting). Each non-ASCII
    character counts as its UTF-8 bytes (Han three), the most a byte-level or byte-fallback
    tokenizer gives it (a lone surrogate three); each ASCII digit, punctuation mark or whitespace
    character one, but a single space before a letter, which joins the word after it. A run of ASCII
    letters shaped like a word (lower case, or capitalized) counts one token for every
    LETTERS_PER_TOKEN of its first WORD_LETTERS letters and one for each letter after them; any
    other run (mixed or upper case, as in identifiers, sequences or encoded text) one per letter."""
    tokens = run = 0
    start = 0
    for index, char in enumerate(text):
        if char.isascii() and char.isalpha():
            if not run:
                start = index
            run += 1
            continue
        if run:
            tokens += _letters(text[start:start + run])
            run = 0
        if not char.isascii():
            tokens += len(char.encode("utf-8", "surrogatepass"))
        elif char != " " or text[index - 1:index] == " " or not text[index + 1:index + 2].isalpha():
            tokens += 1
    return tokens + (_letters(text[start:start + run]) if run else 0)


def _letters(run):
    if run.islower() or (run[0].isupper() and run[1:].islower()):
        return -(-min(len(run), WORD_LETTERS) // LETTERS_PER_TOKEN) + max(0, len(run) - WORD_LETTERS)
    return len(run)


def output_limit(window: int, setting: int) -> int:
    """The step's output limit: its output setting, capped at a quarter of the window, and never below
    MIN_OUTPUT."""
    return max(MIN_OUTPUT, min(setting, window // 4))


def _block(label, records):
    return f"## {label}\n" + "\n\n".join(records)


def _take(records, room):
    """The leading records that fit room whole, their tokens, and how many were left out."""
    kept, used = [], 0
    for record in records:
        cost = count(record) + 2  # the blank line between records
        if used + cost > room:
            break
        kept.append(record)
        used += cost
    return kept, used, len(records) - len(kept)


def assemble(function, *, window, output, budgets, system_rules, request, sections=None, history=(),
             tools=(), needs_evidence=False, margin_percent=5) -> Request:
    """Build a request for a route of this window (W) under the capacity policy, or raise OverWindow.

    output is the step's output setting; budgets the [context] settings; sections maps a section
    (LABELS) to its records, best first; history yields earlier turns as (message, answer), newest
    first, read only as far as they fit; tools are function definitions the step offers. With
    needs_evidence (a pipeline that needs evidence), too little room for evidence that does not fit
    whole refuses the step."""
    sections = {name: [r for r in records if r] for name, records in (sections or {}).items()}
    max_tokens = output_limit(window, output)
    framing = 2 * MESSAGE_FRAMING + REPLY_FRAMING + (count(json.dumps(tools, ensure_ascii=False)) if tools else 0)
    room = window - max_tokens - framing - math.ceil(window * margin_percent / 100)
    protected = {"system_rules": count(system_rules), "current_request": count(request)}
    for name in PROTECTED:
        if sections.get(name):
            protected[name] = count(_block(LABELS[name], sections[name])) + 2
    if sum(protected.values()) > room:
        # Named by what is largest: the message, or the instructions around it.
        raise OverWindow("message_over_window" if max(protected, key=protected.get) == "current_request"
                         else "instructions_over_window")
    room -= sum(protected.values())
    kept, cuts = {name: sections[name] for name in PROTECTED if sections.get(name)}, {}

    def fill(name):
        nonlocal room
        records = sections.get(name) or []
        if not records:
            return
        allowed = min(budgets[name], room) - count(f"## {LABELS[name]}\n")
        chosen, used, left = _take(records, allowed)
        if name == "research_evidence" and needs_evidence and left and allowed < EVIDENCE_FLOOR:
            raise OverWindow("evidence_over_window")
        if chosen:
            kept[name] = chosen
            room -= used + count(f"## {LABELS[name]}\n")
        if left:
            cuts[name] = left

    for name in (*SMALL, "research_evidence"):
        fill(name)
    turns, history_room, left = [], min(budgets["history"], room), 0
    for message, answer in history:
        if left:  # older turns are left out whole (compaction comes with S1-21)
            left += 1
            continue
        cost = count(message) + count(answer) + 2 * MESSAGE_FRAMING
        if cost > history_room:
            left = 1
            continue
        turns.append((message, answer))
        history_room -= cost
    if left:
        cuts["history"] = left
    blocks = [_block(LABELS[name], kept[name]) for name in LABELS if name in kept]
    messages = [{"role": "system", "content": "\n\n".join([system_rules, *blocks])}]
    for message, answer in reversed(turns):
        messages += [{"role": "user", "content": message}, {"role": "assistant", "content": answer}]
    messages.append({"role": "user", "content": request})
    tokens = serialized_tokens(messages, tools)
    if tokens + max_tokens > window:  # the policy above makes this impossible; never send one that does not fit
        raise OverWindow("message_over_window")
    return Request(function, messages, max_tokens, tokens, window, list(tools), cuts)


def serialized_tokens(messages, tools=()) -> int:
    """The counted size of a serialized request: each message's content and framing, the reply's
    framing and the tool definitions."""
    return (sum(count(m["content"]) + MESSAGE_FRAMING for m in messages) + REPLY_FRAMING
            + (count(json.dumps(list(tools), ensure_ascii=False)) if tools else 0))
