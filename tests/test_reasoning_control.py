# Carried over from AI Advisory Board, tests/test_reasoning_control.py at commit
# b5d687820e88c10de25a9a2343d3cc478e497524, with backend/reasoning_control.py. Unchanged.
import importlib

import pytest


def _rc():
    return importlib.import_module("backend.reasoning_control")


def _cap(surface, **kw):
    rec = {"control_surface": surface}
    rec.update(kw)
    return rec


# --- rung d: levels -> snap to nearest supported --------------------------------

@pytest.mark.parametrize("level,expected", [("low", "low"), ("medium", "medium"), ("high", "high")])
def test_levels_full_support_passes_through(level, expected):
    rc = _rc()
    d = rc.translate_reasoning_control(_cap("levels", levels=["low", "medium", "high"]), level)
    assert d == {"kind": "effort", "effort": expected}


def test_levels_snaps_missing_medium_to_lower_rung_on_tie():
    rc = _rc()
    # supported {low, high}; medium is equidistant -> ties resolve to the LOWER rung
    assert rc.translate_reasoning_control(_cap("levels", levels=["low", "high"]), "medium")["effort"] == "low"
    # order-independent: a descending capability list must NOT flip the tie to high
    assert rc.translate_reasoning_control(_cap("levels", levels=["high", "low"]), "medium")["effort"] == "low"
    # an exact-member level passes straight through
    assert rc.translate_reasoning_control(_cap("levels", levels=["low", "high"]), "high")["effort"] == "high"


@pytest.mark.parametrize("level,supported,expected", [
    ("high", ["low", "medium"], "medium"),   # high->medium is nearer than high->low
    ("low", ["medium", "high"], "medium"),    # low->medium is nearer than low->high
    ("minimal", ["high", "xhigh"], "high"),   # far below range -> nearest is the floor
])
def test_levels_snaps_to_the_actually_nearest_when_not_equidistant(level, supported, expected):
    # Pins the nearest-distance branch on NON-equidistant inputs, so a min->max or
    # dropped-abs regression in _snap_to_supported can't ship green (the equidistant
    # case above accepts either side and would miss it).
    rc = _rc()
    assert rc.translate_reasoning_control(_cap("levels", levels=supported), level) == {"kind": "effort", "effort": expected}


# --- rung c: budget -> ratio, clamped ------------------------------------------

def test_budget_maps_level_to_clamped_tokens():
    rc = _rc()
    cap = _cap("budget", budget={"min": 1000, "max": 10000})
    assert rc.translate_reasoning_control(cap, "high")["max_tokens"] == 8000   # 0.8 * 10000
    assert rc.translate_reasoning_control(cap, "low")["max_tokens"] == 2000    # 0.2 * 10000
    # clamp: a tiny max floors at min
    small = _cap("budget", budget={"min": 500, "max": 500})
    assert rc.translate_reasoning_control(small, "low")["max_tokens"] == 500


def test_budget_without_max_drops_instead_of_null_tokens():
    rc = _rc()
    # no budget.max -> no valid token budget -> drop, never {"max_tokens": None}
    d = rc.translate_reasoning_control(_cap("budget", budget={"min": 1000}), "high")
    assert d["kind"] == "drop" and "max token cap" in d["reason"]
    # whole budget block absent -> same drop
    assert rc.translate_reasoning_control(_cap("budget"), "high")["kind"] == "drop"


# --- rung b: onoff -> explicit=on, Auto=native default -------------------------

def test_onoff_any_explicit_level_is_on():
    rc = _rc()
    assert rc.translate_reasoning_control(_cap("onoff", native_default_on=False), "low") == {"kind": "onoff", "on": True}


def test_onoff_auto_uses_native_default():
    rc = _rc()
    assert rc.translate_reasoning_control(_cap("onoff", native_default_on=True), None) == {"kind": "onoff", "on": True}
    assert rc.translate_reasoning_control(_cap("onoff", native_default_on=False), None) == {"kind": "onoff", "on": False}


# --- rung a + unknown: drop with distinct reasons ------------------------------

def test_none_drops_with_reason():
    rc = _rc()
    d = rc.translate_reasoning_control(_cap("none"), "high")
    assert d["kind"] == "drop" and "no reasoning control" in d["reason"]


def test_unknown_drops_as_not_verified():
    rc = _rc()
    d = rc.translate_reasoning_control(_cap("unknown"), "high")
    assert d["kind"] == "drop" and "not verified" in d["reason"]


# --- Auto (no level) omits an explicit effort (correction #7) -------------------

@pytest.mark.parametrize("surface", ["levels", "budget", "none", "unknown"])
def test_auto_omits_effort_for_non_onoff_surfaces(surface):
    rc = _rc()
    assert rc.translate_reasoning_control(_cap(surface), None) == {"kind": "auto"}
    assert rc.translate_reasoning_control(_cap(surface), "") == {"kind": "auto"}


# --- B4: decision -> OpenRouter reasoning payload fragment ----------------------

def test_render_effort_decision():
    rc = _rc()
    assert rc.decision_to_reasoning_payload({"kind": "effort", "effort": "high"}) == {"effort": "high"}


def test_render_budget_decision():
    rc = _rc()
    assert rc.decision_to_reasoning_payload({"kind": "budget", "max_tokens": 8000}) == {"max_tokens": 8000}
    # a budget model with no resolvable max -> omit
    assert rc.decision_to_reasoning_payload({"kind": "budget", "max_tokens": None}) is None


def test_render_onoff_decision_uses_enabled_toggle():
    rc = _rc()
    assert rc.decision_to_reasoning_payload({"kind": "onoff", "on": True}) == {"enabled": True}
    assert rc.decision_to_reasoning_payload({"kind": "onoff", "on": False}) == {"enabled": False}


@pytest.mark.parametrize("decision", [{"kind": "auto"}, {"kind": "drop", "reason": "x"}])
def test_render_auto_and_drop_omit_reasoning(decision):
    rc = _rc()
    assert rc.decision_to_reasoning_payload(decision) is None


def test_resolve_reasoning_payload_end_to_end():
    rc = _rc()
    # levels model at high -> {"effort": "high"}
    assert rc.resolve_reasoning_payload(_cap("levels", levels=["low", "medium", "high"]), "high") == {"effort": "high"}
    # onoff at Auto with native-default off -> {"enabled": False}
    assert rc.resolve_reasoning_payload(_cap("onoff", native_default_on=False), None) == {"enabled": False}
    # unknown capability -> omit (Auto-safe until the probe fills the sidecar)
    assert rc.resolve_reasoning_payload(_cap("unknown"), "high") is None
    # Auto on a levels model -> omit
    assert rc.resolve_reasoning_payload(_cap("levels"), None) is None
