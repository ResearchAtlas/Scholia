"""The context assembler: the one place a model request is built (slice-1 spec sections 6.6 and 8).

Every model call's request comes from `assemble`, and the harness sends only what it built
(backend/runs.py, Harness._call; tests/test_dispatch_path.py checks it). The request holds
labeled sections in the decided order: the system rules first, then AGENTS.md, the SKILL
CATALOG, ACTIVE SKILLS, PROJECT STATE, USER MEMORY, RESEARCH EVIDENCE, MODEL-GENERATED
INFERENCE and the conflicts list, all in the system message; then the history, oldest first;
then the current request; then, in an agent turn, the turn's own chain so far (each assistant step
whole, with its tool calls and reasoning blocks, and each tool result), never cut or rewritten. A
section with nothing in it is left out.

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
# Pairs of lower-case letters that cl100k_base or o200k_base has no token for (read from their
# vocabularies): two such letters can stay apart as one token each, as no other pair can (count).
UNMERGED = frozenset("bq fj fz gj gk gq hj jg jv jw jx jy jz kq kx kz lq mz nq oq qf qg qj qk qo qv qy qz rj tj tq uq"
                     " vq vz wq wv wz xg xh xj xk xq xu xv xw yf yj yq yv zg zj zq zr zv".split())

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
    """Tokens in text, never fewer than a byte-level BPE tokenizer such as cl100k_base or o200k_base
    gives it, however the text is made (section 8, Counting: "must never count fewer").

    Each non-ASCII character counts as its UTF-8 bytes (Han three), the most such a tokenizer gives
    it, a lone surrogate included; each ASCII digit, punctuation mark or whitespace character one, but
    a space just before an ASCII letter, which goes with the letters after it. A run of ASCII letters
    in mixed or upper case counts one per letter. A run in lower case, or capitalized, counts the
    most tokens the tokenizer can leave it in (_word). Each part is counted on its own: the bound for a
    whole run is never above the sum of the bounds for its parts, since a split the rule in _word
    allows for the whole is allowed for each part."""
    tokens = run = 0
    start = 0
    for index, char in enumerate(text):
        if char.isascii() and char.isalpha():
            if not run:
                start = index
            run += 1
            continue
        if run:
            tokens += _letters(text[start:start + run], text[start - 1:start] == " ")
            run = 0
        if not char.isascii():
            tokens += len(char.encode("utf-8", "surrogatepass"))
        elif char != " " or not (text[index + 1:index + 2].isascii() and text[index + 1:index + 2].isalpha()):
            tokens += 1
    return tokens + (_letters(text[start:start + run], text[start - 1:start] == " ") if run else 0)


def _letters(run, spaced):
    if not (run.islower() or (run[0].isupper() and run[1:].islower())):
        return len(run) + spaced
    return _word((" " if spaced else "") + run.lower(), capital=spaced + 1 if run[0].isupper() else None)


def _word(symbols, capital=None):
    """The most tokens BPE can leave symbols in. Merging stops only when no two neighboring tokens
    join into one in the vocabulary, and every pair of a space and a letter, and every lower-case
    pair but UNMERGED, is a token in both vocabularies: so no two one-letter tokens stay side by side
    unless their pair is in UNMERGED (or is a capital and the letter after it, at index capital). The
    largest count under that rule, with every other token at least two symbols long, bounds it."""
    alone, joined = 1, float("-inf")  # the most tokens up to here, ending with a one-symbol or a longer token
    before = (float("-inf"), float("-inf"))  # the same, one symbol back
    for i in range(1, len(symbols)):
        apart = i == capital or (symbols[i - 1] != " " and symbols[i - 1] + symbols[i] in UNMERGED)
        before, (alone, joined) = (alone, joined), (
            1 + max(joined, alone if apart else float("-inf")), 1 + max(*before) if i > 1 else 1)
    return int(max(alone, joined))


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
             tools=(), needs_evidence=False, margin_percent=5, chain=()) -> Request:
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
    if chain:
        protected["chain"] = serialized_tokens(list(chain)) - REPLY_FRAMING
    for name in PROTECTED:
        if sections.get(name):
            protected[name] = count(_block(LABELS[name], sections[name])) + 2
    if sum(protected.values()) > room:
        if tools and sum(protected.values()) - protected.get("skill_catalog", 0) <= \
                room + framing - 2 * MESSAGE_FRAMING - REPLY_FRAMING:
            raise OverWindow("tools_over_window")  # it would fit but for the tool definitions and the skill catalog
        # Named by what is largest: the message, the instructions around it, or the turn's own chain.
        largest = max(protected, key=protected.get)
        raise OverWindow({"current_request": "message_over_window", "chain": "chain_over_window"}.get(
            largest, "instructions_over_window"))
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
    messages += list(chain)
    tokens = serialized_tokens(messages, tools)
    if tokens + max_tokens > window:  # the policy above makes this impossible; never send one that does not fit
        raise OverWindow("message_over_window")
    return Request(function, messages, max_tokens, tokens, window, list(tools), cuts)


def serialized_tokens(messages, tools=()) -> int:
    """The counted size of a serialized request: each message's content and framing (and any other field it
    carries, such as an assistant step's tool calls and reasoning blocks, as serialized), the reply's framing
    and the tool definitions."""
    return (sum(_message_tokens(m) for m in messages) + REPLY_FRAMING
            + (count(json.dumps(list(tools), ensure_ascii=False)) if tools else 0))


def _message_tokens(message):
    extra = {k: v for k, v in message.items() if k not in ("role", "content")}
    return (count(message.get("content") or "") + MESSAGE_FRAMING
            + (count(json.dumps(extra, ensure_ascii=False)) if extra else 0))
