# Carried over from AI Advisory Board, backend/budget_router.py at commit
# b5d687820e88c10de25a9a2343d3cc478e497524, adapted for Scholia. It brings the helpers it
# needs from the same commit: the task signal and its keywords (rag_utils.py and
# config.py) and the model tiers (execution_modes.py). Left out: retrieval budgets,
# Council, web search and the steward, which Scholia does not have yet, and the
# advisory budget brackets, which budgets per conversation and project replace. Prices
# come from the route's provider catalog (openrouter_client.py), not a curated registry.
"""The run plan for a message and the pre-send cost estimate (D3).

The plan records the task signal, the model tier and the model a step will use,
and a deliberately conservative estimate of what the step will cost. The estimate
is what the harness reserves against the budgets before the call; the settled
cost comes from the provider's reported usage. Estimates are approximate and
never authoritative.
"""

import re
from dataclasses import asdict, dataclass

from backend.openrouter_client import get_model_metadata

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

# Rough tokens per mode for one answer call. Deliberately conservative and clearly
# approximate: heuristics, never measured.
_MODE_TOKENS = {
    "quick": {"input": 2000, "output": 500},
    "standard": {"input": 4000, "output": 1000},
    "research": {"input": 8000, "output": 2000},
}
_TITLE_TOKENS = {"input": 600, "output": 40}
# Rough reasoning tokens (billed as output) a generation call adds at each effort.
_REASONING_OUTPUT_TOKENS = {"minimal": 0, "low": 800, "medium": 2000, "high": 5000, "xhigh": 9000}
EFFORT_LEVELS = tuple(_REASONING_OUTPUT_TOKENS)  # the levels a turn may ask for, lowest first
# Per million tokens, when the provider publishes no price.
_UNKNOWN_PRICE = {"input": 1.0, "output": 5.0}


@dataclass
class RunPlan:
    """The observable routing decision for one message."""
    mode: str  # "quick", "standard", "research"
    model_tier: str  # "budget", "mid", "premium"
    model: str | None  # None when Auto cannot choose for this provider
    predicted_cost: float  # estimated USD
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


def create_run_plan(query: str, model: str | None, route_for, *, effort: str | None = None,
                    is_openrouter: bool = True, has_files: bool = False, offered=lambda m: True,
                    picked=()) -> RunPlan:
    """The plan for a message. model is a model id or Auto; route_for(model) gives
    the route whose catalog prices it. Auto takes the first model the provider offers
    (offered(model)) among the tiers' preferred models on OpenRouter, the balanced tier
    first, then among the models picked for it; with none, the plan's model is None."""
    signal = detect_task_signal(query, has_files)
    if model and model != AUTO:
        chosen, reason = model, "chosen_model"
    else:
        preferred = [m for tier in ("mid", "budget", "premium") for m in MODEL_TIERS[tier]] if is_openrouter else []
        chosen = next((m for m in [*preferred, *picked] if offered(m)), None)
        reason = ("auto_mid_tier" if chosen == MODEL_TIERS["mid"][0] else "auto_offered") if chosen else "model_needed"
    predicted = estimate_message_cost(signal, route_for(chosen), effort) if chosen else 0.0
    tier = next((name for name, models in MODEL_TIERS.items() if chosen in models), "mid")
    return RunPlan(mode=signal, model_tier=tier, model=chosen, predicted_cost=predicted,
                   policy_reason=reason, task_signal=signal)


def _model_call_cost(route, input_tokens: int, output_tokens: int) -> float:
    """The provider's published price for the route's model, or the conservative
    unknown-price heuristic ($1 and $5 per million tokens)."""
    pricing = (get_model_metadata(route) or {}).get("pricing") or {} if route else {}
    input_price = pricing.get("input")
    output_price = pricing.get("output")
    if input_price is None:
        input_price = _UNKNOWN_PRICE["input"]
    if output_price is None:
        output_price = _UNKNOWN_PRICE["output"]
    return (input_tokens / 1_000_000) * input_price + (output_tokens / 1_000_000) * output_price


def estimate_message_cost(mode: str, route, effort: str | None = None) -> float:
    """A rough, conservative estimate of one answer call in this mode. Reasoning
    tokens at the chosen effort are added, at medium when none is chosen, since a
    model may reason by default."""
    tokens = _MODE_TOKENS.get(mode, _MODE_TOKENS["standard"])
    reasoning = _REASONING_OUTPUT_TOKENS.get(effort or "medium", _REASONING_OUTPUT_TOKENS["medium"])
    return round(_model_call_cost(route, tokens["input"], tokens["output"] + reasoning), 6)


def estimate_title_cost(route) -> float:
    """A rough estimate of one title call: a short prompt and a few words back."""
    return round(_model_call_cost(route, _TITLE_TOKENS["input"], _TITLE_TOKENS["output"]), 6)


def cost_from_usage(route, usage: dict) -> float | None:
    """An estimate from reported token counts, when the provider reported tokens but no
    cost; None when it reported neither."""
    prompt, completion = usage.get("prompt_tokens"), usage.get("completion_tokens")
    if not all(isinstance(n, int) and not isinstance(n, bool) and n >= 0 for n in (prompt, completion)):
        return None
    return round(_model_call_cost(route, prompt, completion), 9)
