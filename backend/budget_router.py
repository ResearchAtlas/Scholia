# Carried over from AI Advisory Board, backend/budget_router.py at commit
# b5d687820e88c10de25a9a2343d3cc478e497524, adapted for Scholia. It brings the helpers it
# needs from the same commit: the task signal and its keywords (rag_utils.py and
# config.py) and the model tiers (execution_modes.py). Left out: retrieval budgets,
# Council, web search and the steward, which Scholia does not have yet, and the
# advisory budget brackets, which budgets per conversation and project replace. Prices
# come from the endpoints a request can reach (reach.py), not a curated registry, and an
# estimate counts the step's own request, not a fixed amount per mode (Scholia, S1-19).
"""The run plan for a message and the pre-send cost estimate (D3).

The plan records the task signal, the model tier and the model a step will use. A step's
estimate is a deliberately conservative bound on what it will cost: its counted request and
its whole output limit at the highest rates it can reach. The estimate is what the harness
reserves against the budgets before the call; the settled cost comes from the provider's
reported usage. Estimates are approximate and never authoritative.
"""

import re
from dataclasses import asdict, dataclass

AUTO = "auto"

TASK_SIGNALS = {
    "research_keywords": [
        "cite", "cites", "cited", "citing", "paper", "papers", "compare", "compares", "compared",
        "comparing", "analyze", "analyzes", "analyzed", "analyzing", "research", "researches",
        "researched", "researching", "study", "studies", "studied", "studying", "investigate",
        "investigates", "investigated", "investigating", "sources", "evidence", "literature",
    ],
    "quick_keywords": ["quick", "quickly", "briefly", "short", "summary", "summarize", "recap", "tldr"],
    "research_query_length": 200,  # chars
}

# Preferred models per tier on OpenRouter. Auto uses the balanced tier's first model
# until the router chooses among the routes a project allows.
MODEL_TIERS = {
    "budget": ["google/gemini-2.5-flash-lite", "deepseek/deepseek-v4-flash", "qwen/qwen3.5-9b"],
    "mid": ["google/gemini-3.1-pro-preview", "openai/gpt-5.4-mini", "deepseek/deepseek-v4-pro"],
    "premium": ["anthropic/claude-opus-4.8", "openai/gpt-5.5", "google/gemini-3.1-pro-preview"],
}
# The models offered as Recommended on OpenRouter: the tiers' preferred models.
RECOMMENDED = frozenset(model for tier in MODEL_TIERS.values() for model in tier)

# Effort levels a turn may ask for, lowest first, with the reasoning tokens (billed as output) each
# roughly adds; a step's output limit bounds them (backend/context.py).
_REASONING_OUTPUT_TOKENS = {"minimal": 0, "low": 800, "medium": 2000, "high": 5000, "xhigh": 9000}
EFFORT_LEVELS = tuple(_REASONING_OUTPUT_TOKENS)  # the levels a turn may ask for, lowest first
# USD per token, when the provider publishes no price: $1 per million input and $5 output.
UNKNOWN_RATES = {"input": 1.0 / 1_000_000, "output": 5.0 / 1_000_000, "request": 0.0}


@dataclass
class RunPlan:
    """The observable routing decision for one message."""
    mode: str  # "quick", "standard", "research"
    model_tier: str  # "budget", "mid", "premium"
    model: str | None  # None when Auto cannot choose for this provider
    predicted_cost: float | None  # estimated USD: the answer step's, set once its request is counted
    policy_reason: str  # why this model
    task_signal: str

    def to_dict(self):
        return asdict(self)


def _contains_keyword(query_lower: str, keyword: str) -> bool:
    """Match a keyword or phrase without substring false positives."""
    normalized = keyword.lower().strip()
    if not normalized:
        return False
    return re.search(rf"(?<!\w){re.escape(normalized)}(?!\w)", query_lower) is not None


def detect_task_signal(query: str, has_files: bool = False) -> str:
    """"quick", "standard" or "research", from heuristics on the request."""
    if has_files or len(query) > TASK_SIGNALS["research_query_length"]:
        return "research"
    query_lower = query.lower()
    if any(_contains_keyword(query_lower, k) for k in TASK_SIGNALS["research_keywords"]):
        return "research"
    if any(_contains_keyword(query_lower, k) for k in TASK_SIGNALS["quick_keywords"]):
        return "quick"
    return "standard"


def create_run_plan(query: str, model: str | None, *, is_openrouter: bool = True, has_files: bool = False,
                    offered=lambda m: True, picked=()) -> RunPlan:
    """The plan for a message. model is a model id or Auto. Auto takes the first model the provider
    offers (offered(model)) among the tiers' preferred models on OpenRouter, the balanced tier first,
    then among the models picked for it; with none, the plan's model is None. Its cost is estimated
    once the answer step's request is built (request_cost)."""
    signal = detect_task_signal(query, has_files)
    if model and model != AUTO:
        chosen, reason = model, "chosen_model"
    else:
        preferred = [m for tier in ("mid", "budget", "premium") for m in MODEL_TIERS[tier]] if is_openrouter else []
        chosen = next((m for m in [*preferred, *picked] if offered(m)), None)
        reason = ("auto_mid_tier" if chosen == MODEL_TIERS["mid"][0] else "auto_offered") if chosen else "model_needed"
    tier = next((name for name, models in MODEL_TIERS.items() if chosen in models), "mid")
    return RunPlan(mode=signal, model_tier=tier, model=chosen, predicted_cost=None,
                   policy_reason=reason, task_signal=signal)


def request_cost(rates, input_tokens: int, output_tokens: int) -> float:
    """A step's estimate (D3): its counted request and its whole output limit at the highest rates
    among the endpoints it can reach (backend/reach.py), plus any charge per request. The settled
    cost comes from the provider's reported usage."""
    return round(input_tokens * rates["input"] + output_tokens * rates["output"] + rates["request"], 9)


def cost_from_usage(rates, usage: dict) -> float | None:
    """An estimate from reported token counts, at the step's rates, when the provider reported tokens
    but no cost; None when it reported neither."""
    prompt, completion = usage.get("prompt_tokens"), usage.get("completion_tokens")
    if not all(isinstance(n, int) and not isinstance(n, bool) and n >= 0 for n in (prompt, completion)):
        return None
    return request_cost(rates, prompt, completion)
