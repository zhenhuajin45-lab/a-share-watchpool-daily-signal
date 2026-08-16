"""Operational acceptance gate shared by review and publication."""

from __future__ import annotations

from typing import Any


REQUIRED_CHECKS = (
    "replay_20_days",
    "shadow_5_days",
    "simulation_5_days",
    "no_real_order_contract",
    "all_evidence_valid",
)


def operational_release_gate_met(value: Any) -> bool:
    if (
        not isinstance(value, dict)
        or value.get("schema_version") != "premarket_release_gate_v2"
        or str(value.get("release_gate") or "").upper() != "MET"
    ):
        return False
    checks = value.get("checks") if isinstance(value.get("checks"), dict) else {}
    counts = value.get("counts") if isinstance(value.get("counts"), dict) else {}
    try:
        enough_days = (
            int(counts.get("replay_days", 0)) >= 20
            and int(counts.get("shadow_days", 0)) >= 5
            and int(counts.get("simulation_days", 0)) >= 5
        )
    except (TypeError, ValueError):
        return False
    quality = value.get("evidence_quality") if isinstance(value.get("evidence_quality"), dict) else {}
    try:
        no_invalid_evidence = int(quality.get("invalid_files", -1)) == 0
    except (TypeError, ValueError):
        return False
    return enough_days and no_invalid_evidence and all(checks.get(name) is True for name in REQUIRED_CHECKS)
