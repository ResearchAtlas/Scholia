# Carried over from AI Advisory Board, tests/test_reasoning_capability.py at commit
# b5d687820e88c10de25a9a2343d3cc478e497524, with backend/reasoning_capability.py. The checks of the
# current app's model registry presets are left out with the registry.
import importlib
import json
from datetime import datetime, timedelta, timezone

import pytest


def _cap():
    return importlib.import_module("backend.reasoning_capability")


def _probed_record(cap, model_entry, **overrides):
    rec = cap.unknown_record(model_entry["id"], cap.model_fingerprint(model_entry))
    rec.update(probed=True, probed_at="2026-07-20T00:00:00+00:00",
               supports_reasoning=True, control_surface="levels",
               levels=["low", "high"], provider_pinned="openai")
    rec.update(overrides)
    return rec


def test_missing_sidecar_yields_all_unknown(tmp_path):
    cap = _cap()
    records = cap.load_capabilities(tmp_path / "does-not-exist.json")
    assert records == {}
    rec = cap.get_capability(records, "openai/gpt-4o-mini", {"id": "openai/gpt-4o-mini"})
    assert rec["control_surface"] == "unknown"
    assert rec["supports_reasoning"] is None
    assert rec["probed"] is False


def test_unknown_record_never_reads_as_supported_or_unsupported():
    cap = _cap()
    rec = cap.unknown_record("x/y")
    assert rec["supports_reasoning"] is None  # not False
    assert rec["control_surface"] == "unknown"  # not "none"


def test_probed_record_with_matching_fingerprint_is_returned():
    cap = _cap()
    entry = {"id": "m/1", "supports_reasoning": True, "reasoning_extraction": "field"}
    records = {"m/1": _probed_record(cap, entry)}
    rec = cap.get_capability(records, "m/1", entry)
    assert rec["probed"] is True
    assert rec["control_surface"] == "levels"


def test_stale_fingerprint_is_treated_as_unknown():
    cap = _cap()
    entry = {"id": "m/1", "supports_reasoning": True, "reasoning_extraction": "field"}
    records = {"m/1": _probed_record(cap, entry)}
    # The registry entry changes materially -> fingerprint rotates -> stale.
    changed = {"id": "m/1", "supports_reasoning": True, "reasoning_extraction": "tags"}
    rec = cap.get_capability(records, "m/1", changed)
    assert rec["control_surface"] == "unknown"
    assert rec["probed"] is False


def test_unprobed_record_is_unknown():
    cap = _cap()
    entry = {"id": "m/1"}
    records = {"m/1": cap.unknown_record("m/1", cap.model_fingerprint(entry))}
    assert cap.get_capability(records, "m/1", entry)["control_surface"] == "unknown"


def test_save_load_round_trip(tmp_path):
    cap = _cap()
    entry = {"id": "m/1", "supports_reasoning": True, "reasoning_extraction": "field"}
    path = tmp_path / "sidecar.json"
    cap.save_capabilities([_probed_record(cap, entry)], path)
    records = cap.load_capabilities(path)
    assert records["m/1"]["control_surface"] == "levels"
    # persisted file is data with provenance, not planning prose
    assert "capabilities" in json.loads(path.read_text(encoding="utf-8"))


def test_save_accepts_loader_map_for_a2_round_trip(tmp_path):
    """A2's natural flow: load -> mutate -> save. save_capabilities must accept the
    model_id->record MAP that load_capabilities returns, not only a list."""
    cap = _cap()
    path = tmp_path / "sidecar.json"
    cap.save_capabilities([_probed_record(cap, {"id": "m/1", "supports_reasoning": True, "reasoning_extraction": "field"})], path)
    records = cap.load_capabilities(path)                    # -> a dict map
    records["m/2"] = _probed_record(cap, {"id": "m/2", "supports_reasoning": True, "reasoning_extraction": "field"})
    cap.save_capabilities(records, path)                     # passing the map back must not raise
    reloaded = cap.load_capabilities(path)
    assert set(reloaded) == {"m/1", "m/2"}


def test_age_is_a_warning_not_runtime_invalidation():
    cap = _cap()
    entry = {"id": "m/1", "supports_reasoning": True, "reasoning_extraction": "field"}
    old = _probed_record(cap, entry, probed_at="2026-06-01T00:00:00+00:00")
    records = {"m/1": old}
    now = datetime(2026, 7, 24, tzinfo=timezone.utc)

    # Age flags a maintainer warning...
    assert "m/1" in cap.stale_by_age(records, now=now, max_age_days=30)
    # ...but does NOT invalidate the record at read time (fingerprint still matches).
    assert cap.get_capability(records, "m/1", entry)["probed"] is True

    recent = _probed_record(cap, entry, probed_at=(now - timedelta(days=5)).isoformat())
    assert cap.stale_by_age({"m/1": recent}, now=now, max_age_days=30) == []


def test_probed_record_unverifiable_without_entry_is_unknown():
    # Correction #5: a probed row is authoritative only once validated against the
    # current registry entry. A 2-arg lookup (no model_entry) can't compute the
    # fingerprint, so it must return unknown rather than trust a possibly-stale row.
    cap = _cap()
    entry = {"id": "m/1", "supports_reasoning": True, "reasoning_extraction": "field"}
    records = {"m/1": _probed_record(cap, entry)}
    assert cap.get_capability(records, "m/1")["control_surface"] == "unknown"
    # with the entry it validates and returns the real record
    assert cap.get_capability(records, "m/1", entry)["control_surface"] == "levels"
