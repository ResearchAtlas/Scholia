# Carried over from AI Advisory Board, backend/reasoning_capability.py at commit
# b5d687820e88c10de25a9a2343d3cc478e497524. Unchanged. Its sidecar, reasoning_capabilities.json,
# is carried with it, empty as it was: no model has been probed.
"""Per-model reasoning-capability sidecar — the single capability authority (v1.3.0 A1).

The reasoning-control surface of a model (does it reason? via levels / a token
budget / on-off? which levels? on-by-default?) is EMPIRICAL and cannot be trusted
from `model_registry.json` metadata (see the v1.3.0 brainstorm). Phase A2 probes
each model and writes one record per model here; runtime consumers (Phase B) read
from this sidecar rather than from `model_registry.json.supports_reasoning`.

This module is the schema + loader only. It ships with an empty/absent sidecar
until A2 runs, and every lookup then returns an explicit "unknown" record — never
a fabricated "supported"/"unsupported".

Design rules (execution corrections #2, #5, #6, #7):
  * The sidecar is checked-in DATA (a JSON file), not planning documentation.
  * A record whose stored model fingerprint no longer matches the registry entry
    is STALE and treated as unknown at read time (runtime invalidation).
  * Age is a MAINTAINER/release warning at 30 days -- NOT runtime invalidation.
    `stale_by_age()` reports old rows; `get_capability()` ignores age.
  * `control_surface` is one of levels/budget/onoff/none/unknown; "unknown" is the
    un-probed value, distinct from a probed "none".
  * `native_default_on` (onoff models) is what the Auto state reads.
  * `provider_pinned` records the exact endpoint tag the probe used (correction #6).
"""
from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional

SIDECAR_PATH = Path(__file__).resolve().parent / "reasoning_capabilities.json"

CONTROL_SURFACES = {"levels", "budget", "onoff", "none", "unknown"}
AGE_WARNING_DAYS = 30


def model_fingerprint(model_entry: Dict[str, Any]) -> str:
    """Stable fingerprint of a registry model's reasoning-relevant identity.

    The registry has no explicit version, so we hash the id plus the declared
    fields that would change what a probe should find. A material change to the
    registry entry rotates the fingerprint, invalidating a stale probe row.
    """
    material = {
        "id": model_entry.get("id"),
        "supports_reasoning": model_entry.get("supports_reasoning"),
        "reasoning_extraction": model_entry.get("reasoning_extraction"),
    }
    blob = json.dumps(material, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()[:16]


def unknown_record(model_id: str, fingerprint: Optional[str] = None) -> Dict[str, Any]:
    """An explicit un-probed record. Absence != negative: nothing here reads as
    "supported" or as a definitive "unsupported"."""
    return {
        "model_id": model_id,
        "probed": False,
        "probed_at": None,
        "fingerprint": fingerprint,
        "provider_pinned": None,
        "supports_reasoning": None,
        "control_surface": "unknown",
        "levels": None,
        "budget": None,
        "varies_effort": None,
        "adaptive": None,
        "native_default_on": None,
        "plain": None,  # per-request-shape result for the plain shape (A2)
    }


def load_capabilities(path: Path = SIDECAR_PATH) -> Dict[str, Dict[str, Any]]:
    """Load the sidecar into {model_id: record}. A missing sidecar (fresh checkout,
    never probed) is not an error -- it yields an empty map so every lookup is
    "unknown"."""
    if not Path(path).exists():
        return {}
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    records = data.get("capabilities", data) if isinstance(data, dict) else {}
    return {rec["model_id"]: rec for rec in records.values() if rec.get("model_id")} \
        if isinstance(records, dict) else {r["model_id"]: r for r in records if r.get("model_id")}


def get_capability(
    records: Dict[str, Dict[str, Any]],
    model_id: str,
    model_entry: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """The capability record for a model, or an "unknown" record when it is absent,
    un-probed, or STALE (its stored fingerprint no longer matches the current
    registry entry). Age is intentionally NOT considered here (correction #5)."""
    current_fp = model_fingerprint(model_entry) if model_entry else None
    rec = records.get(model_id)
    if rec is None or not rec.get("probed"):
        return unknown_record(model_id, current_fp)
    if current_fp is None or rec.get("fingerprint") != current_fp:
        # Registry entry changed since the probe -> stale; OR no model_entry was
        # passed so the fingerprint can't be computed -> unverifiable. Either way,
        # a probed row is only authoritative once validated against the current
        # registry entry, so return unknown until re-probed (correction #5).
        return unknown_record(model_id, current_fp)
    return rec


def stale_by_age(
    records: Dict[str, Dict[str, Any]],
    *,
    now: datetime,
    max_age_days: int = AGE_WARNING_DAYS,
) -> List[str]:
    """Model ids whose probe is older than `max_age_days`. A MAINTAINER/release
    WARNING (correction #5) -- callers surface it, it does NOT invalidate rows at
    runtime. `now` is passed in for testability."""
    stale: List[str] = []
    cutoff = now.timestamp() - max_age_days * 86400
    for model_id, rec in records.items():
        probed_at = rec.get("probed_at")
        if not probed_at:
            continue
        try:
            ts = datetime.fromisoformat(probed_at.replace("Z", "+00:00")).timestamp()
        except (ValueError, AttributeError):
            continue
        if ts < cutoff:
            stale.append(model_id)
    return stale


def save_capabilities(records: Iterable[Dict[str, Any]], path: Path = SIDECAR_PATH) -> None:
    """Write records to the checked-in sidecar. A2 owns writing real values; this
    is the persistence seam (do not hand-edit capability values). Accepts either a
    list of records OR the model_id->record map that load_capabilities() returns,
    so A2's load -> mutate -> save round-trips without a shape mismatch."""
    values = records.values() if isinstance(records, dict) else records
    payload = {
        "_doc": "Probe-written reasoning-capability sidecar (v1.3.0 A1/A2). Do not "
                "hand-edit values; A2's active-probe writes them.",
        "written_at": datetime.now(timezone.utc).isoformat(),
        "capabilities": {rec["model_id"]: rec for rec in values},
    }
    Path(path).write_text(json.dumps(payload, indent=2), encoding="utf-8")
