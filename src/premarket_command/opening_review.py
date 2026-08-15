"""09:20 delta review that can only preserve or tighten a published command."""

from __future__ import annotations

import copy
import datetime as dt
from typing import Any

from .publisher import publication_gate_complete


def _cap(command: dict[str, Any], key: str) -> int:
    position = command.get("position_command") if isinstance(command.get("position_command"), dict) else {}
    try:
        return max(0, int(float(position.get(key) or 0)))
    except (TypeError, ValueError):
        return 0


def apply_opening_tighten_only(
    published_command: dict[str, Any],
    opening_command: dict[str, Any],
) -> dict[str, Any]:
    """Return a 09:20 revision without ever widening the published permissions."""
    if not publication_gate_complete(published_command):
        raise ValueError("09:20 review requires a PUBLISHED baseline")

    result = copy.deepcopy(published_command)
    baseline_cap = _cap(published_command, "base_cap_pct")
    opening_cap = _cap(opening_command, "base_cap_pct")
    final_cap = min(baseline_cap, opening_cap)

    baseline_rotation = published_command.get("sector_rotation") if isinstance(published_command.get("sector_rotation"), dict) else {}
    opening_rotation = opening_command.get("sector_rotation") if isinstance(opening_command.get("sector_rotation"), dict) else {}
    opening_names = {
        str(item.get("sector_name"))
        for item in opening_rotation.get("primary_attack_sectors") or []
        if isinstance(item, dict) and item.get("sector_name")
    }
    final_sectors = [
        copy.deepcopy(item)
        for item in baseline_rotation.get("primary_attack_sectors") or []
        if isinstance(item, dict) and str(item.get("sector_name")) in opening_names
    ]

    source_health = opening_command.get("source_health") if isinstance(opening_command.get("source_health"), dict) else {}
    opening_healthy = source_health.get("publishable") is True
    baseline_expansion = _cap(published_command, "conditional_expansion_cap_pct")
    opening_expansion = _cap(opening_command, "conditional_expansion_cap_pct")
    expansion_enabled = bool(
        opening_healthy
        and (published_command.get("position_command") or {}).get("conditional_expansion_enabled") is True
        and (opening_command.get("position_command") or {}).get("conditional_expansion_enabled") is True
    )
    final_expansion = min(baseline_expansion, opening_expansion) if expansion_enabled else final_cap
    final_expansion = max(final_cap, final_expansion)

    position = result.get("position_command") if isinstance(result.get("position_command"), dict) else {}
    result["position_command"] = {
        **position,
        "base_cap_pct": final_cap,
        "conditional_expansion_cap_pct": final_expansion,
        "conditional_expansion_enabled": expansion_enabled and final_expansion > final_cap,
    }
    result["sector_rotation"] = {**baseline_rotation, "primary_attack_sectors": final_sectors}
    result["schema_version"] = "a_share_premarket_command_opening_reviewed_v1"
    result["revision_type"] = "OPENING_0920_TIGHTEN_ONLY"
    result["revised_at"] = dt.datetime.now().astimezone().replace(microsecond=0).isoformat()
    result["opening_delta_audit"] = {
        "baseline_cap_pct": baseline_cap,
        "opening_deterministic_cap_pct": opening_cap,
        "final_cap_pct": final_cap,
        "baseline_primary_sectors": [str(item.get("sector_name")) for item in baseline_rotation.get("primary_attack_sectors") or [] if isinstance(item, dict)],
        "opening_primary_sectors": sorted(opening_names),
        "final_primary_sectors": [str(item.get("sector_name")) for item in final_sectors],
        "opening_source_publishable": opening_healthy,
        "opening_source_blockers": list(source_health.get("blockers") or []),
        "permission_change": "TIGHTENED" if final_cap < baseline_cap or len(final_sectors) < len(baseline_rotation.get("primary_attack_sectors") or []) else "UNCHANGED",
    }
    return result
