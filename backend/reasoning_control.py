# Carried over from AI Advisory Board, backend/reasoning_control.py at commit
# b5d687820e88c10de25a9a2343d3cc478e497524. Unchanged except that the effort levels it
# imported from thinking_effort.py are defined here.
"""Universal reasoning-effort -> per-surface translation (v1.3.0 B2).

Given a model's capability record (A1 shape) and the universal low/med/high knob
(or Auto), emit a TYPED decision that the wire boundary (B4) renders to the
provider-specific shape. B2 decides WHETHER / HOW MUCH; B4 decides the exact wire
object. Operates on a capability-record dict, so it is table-driven testable with
synthetic records -- no live probe data required.

Rungs (brainstorm sec. 3.6):
  a. none    -> drop (no reasoning control)
  b. onoff   -> any explicit level => on; Auto => the probed native default
  c. budget  -> level -> ratio -> clamp [min, max]
  d. levels  -> snap the universal level to the nearest supported level
  unknown    -> drop, "reasoning not verified" (distinct from a probed `none`)
  Auto (no level) -> omit an explicit effort (correction #7), except onoff which
                     resolves to the native default.
"""
from typing import Any, Dict, Optional

# The universal effort levels, from the current app's thinking_effort.py (same commit).
THINKING_EFFORT_LEVELS = ("minimal", "low", "medium", "high", "xhigh")
THINKING_EFFORT_ORDER = {level: index for index, level in enumerate(THINKING_EFFORT_LEVELS)}

# OUR policy (Decision #3), not provider semantics: fraction of a budget model's
# max tokens for each universal level (brainstorm sec. 3.2 effort_ratio).
_EFFORT_RATIO = {"minimal": 0.1, "low": 0.2, "medium": 0.5, "high": 0.8, "xhigh": 0.95}


def _snap_to_supported(level: str, supported):
    """Nearest supported level to `level` by ordinal distance (rung d). A distance
    tie resolves to the LOWER rung (cheaper / less latency), independent of the
    order the capability lists its supported levels in."""
    if level in supported:
        return level
    target = THINKING_EFFORT_ORDER.get(level, THINKING_EFFORT_ORDER["medium"])

    def _rank(s):
        return THINKING_EFFORT_ORDER.get(s, 99)

    return min(supported, key=lambda s: (abs(_rank(s) - target), _rank(s)))


def _level_to_budget(level: str, budget: Dict[str, Any]) -> Optional[int]:
    """level -> token budget via the documented ratio, clamped to [min, max] (rung c)."""
    hi = budget.get("max")
    if hi is None:
        return None
    lo = budget.get("min", 0)
    return max(lo, min(hi, int(round(_EFFORT_RATIO.get(level, 0.5) * hi))))


def translate_reasoning_control(
    capability: Optional[Dict[str, Any]],
    level: Optional[str],
) -> Dict[str, Any]:
    """Typed translation decision for one member. Kinds:
      {"kind": "auto"}                         -> send no reasoning object
      {"kind": "effort", "effort": <level>}    -> normalized reasoning.effort
      {"kind": "budget", "max_tokens": <int>}  -> token-budget model
      {"kind": "onoff", "on": <bool>}          -> on/off model
      {"kind": "drop", "reason": <str>}        -> greyed/omitted (none/unknown)
    """
    surface = (capability or {}).get("control_surface", "unknown")
    auto = level is None or level == ""

    # onoff resolves for BOTH Auto (native default) and explicit levels (on).
    if surface == "onoff":
        on = bool((capability or {}).get("native_default_on")) if auto else True
        return {"kind": "onoff", "on": on}

    if auto:
        return {"kind": "auto"}  # omit an explicit effort (correction #7)

    if surface == "none":
        return {"kind": "drop", "reason": "no reasoning control"}
    if surface == "budget":
        max_tokens = _level_to_budget(level, (capability or {}).get("budget") or {})
        if max_tokens is None:
            # A budget model with no known max cap can't yield a valid token budget;
            # drop rather than emit an out-of-contract {"max_tokens": None} decision.
            return {"kind": "drop", "reason": "budget model without a known max token cap"}
        return {"kind": "budget", "max_tokens": max_tokens}
    if surface == "levels":
        supported = (capability or {}).get("levels") or list(THINKING_EFFORT_LEVELS)
        return {"kind": "effort", "effort": _snap_to_supported(level, supported)}
    # "unknown" (un-probed) or anything unexpected: drop, but say it's unverified.
    return {"kind": "drop", "reason": "reasoning not verified"}


# --- B4: render a B2 decision to the OpenRouter `reasoning` payload fragment ----
# Verified against the OpenRouter reasoning-tokens docs (2026-07): the normalized
# `reasoning` object supports `effort` (minimal..xhigh/none), `max_tokens`,
# `enabled` (on/off), and `exclude`. Correction #1: prefer this normalized
# interface; provider-native shapes are only for where a reproducible endpoint
# test proves it insufficient (paid -- deferred).

def decision_to_reasoning_payload(decision: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """The value for `payload["reasoning"]`, or None to send no reasoning object.

    auto/drop -> None (omit); effort -> {"effort": level}; budget ->
    {"max_tokens": n}; onoff -> {"enabled": bool}.
    """
    kind = (decision or {}).get("kind")
    if kind == "effort":
        return {"effort": decision["effort"]}
    if kind == "budget":
        max_tokens = decision.get("max_tokens")
        return {"max_tokens": max_tokens} if max_tokens is not None else None
    if kind == "onoff":
        return {"enabled": bool(decision["on"])}
    # "auto" (Auto/native default) and "drop" (none/unknown) -> omit entirely.
    return None


def resolve_reasoning_payload(
    capability: Optional[Dict[str, Any]],
    level: Optional[str],
) -> Optional[Dict[str, Any]]:
    """Full B2->B4 chain: translate the universal level for this model's surface,
    then render the OpenRouter `reasoning` object (or None to omit). The runtime
    wire boundary (openrouter.py) calls this once real capability data exists;
    until then an unknown capability resolves to None (omit) -- Auto-safe."""
    return decision_to_reasoning_payload(translate_reasoning_control(capability, level))
