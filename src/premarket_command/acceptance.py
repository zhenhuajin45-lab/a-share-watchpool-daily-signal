"""Operational acceptance gate shared by review and publication."""

from __future__ import annotations

from typing import Any


REQUIRED_CHECKS = (
    "replay_20_days",
    "shadow_5_days",
    "simulation_5_days",
    "no_real_order_contract",
)


def operational_release_gate_met(value: Any) -> bool:
    if not isinstance(value, dict) or str(value.get("release_gate") or "").upper() != "MET":
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
    return enough_days and all(checks.get(name) is True for name in REQUIRED_CHECKS)
