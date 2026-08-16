#!/usr/bin/env python3
"""Convert a GM auction snapshot into a strictly restrictive opening candidate."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any


def canonical(value: Any) -> str:
    text = str(value or "").strip().replace(" ", "").upper()
    for suffix in ("概念", "行业", "板块"):
        if text.endswith(suffix.upper()) and len(text) > len(suffix):
            text = text[: -len(suffix)]
    return text


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--published", type=Path, required=True)
    parser.add_argument("--gm-opening", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    baseline = json.loads(args.published.read_text(encoding="utf-8"))
    opening = json.loads(args.gm_opening.read_text(encoding="utf-8"))
    base_position = baseline.get("position_command") or {}
    cap = int(float(base_position.get("base_cap_pct") or 0))
    snapshot = opening.get("market_snapshot") if isinstance(opening.get("market_snapshot"), dict) else {}
    ready = opening.get("status") == "READY" and (opening.get("source_health") or {}).get("ready") is True
    breadth = snapshot.get("breadth") if isinstance(snapshot.get("breadth"), dict) else {}
    rise, fall = int(breadth.get("rise_count") or 0), int(breadth.get("fall_count") or 0)
    up_ratio = rise / (rise + fall) if rise + fall else None
    reasons: list[str] = []
    if ready and up_ratio is not None:
        if up_ratio < 0.35:
            cap = min(cap, 20)
            reasons.append("auction_breadth_below_0.35")
        elif up_ratio < 0.45:
            cap = min(cap, 35)
            reasons.append("auction_breadth_below_0.45")
    sector_by_key = {canonical(row.get("sector_name")): row for row in snapshot.get("sectors") or [] if isinstance(row, dict)}
    baseline_rotation = baseline.get("sector_rotation") or {}
    kept = []
    for sector in baseline_rotation.get("primary_attack_sectors") or []:
        if not isinstance(sector, dict):
            continue
        observed = sector_by_key.get(canonical(sector.get("sector_name")))
        if not ready or observed is None:
            kept.append(dict(sector))
            continue
        if float(observed.get("auction_return_pct") or 0) >= 0 and float(observed.get("auction_up_ratio") or 0) >= 0.45:
            kept.append({**sector, "opening_gm_confirmation": observed})
        else:
            reasons.append(f"primary_sector_weakened:{sector.get('sector_name')}")
    output = {
        "schema_version": "premarket_opening_candidate_v1",
        "execution_trade_date": baseline.get("execution_trade_date"),
        "position_command": {**base_position, "base_cap_pct": cap, "conditional_expansion_cap_pct": cap, "conditional_expansion_enabled": False},
        "sector_rotation": {**baseline_rotation, "primary_attack_sectors": kept},
        "source_health": {
            "publishable": ready,
            "blockers": [] if ready else ["gm_opening_auction_unavailable"],
            "source": "GM_CURRENT_INCLUDE_CALL_AUCTION",
        },
        "opening_candidate_audit": {"market_up_ratio": round(up_ratio, 4) if up_ratio is not None else None, "reasons": reasons, "gm_opening_path": str(args.gm_opening)},
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    temporary = args.output.with_suffix(args.output.suffix + ".tmp")
    temporary.write_text(json.dumps(output, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temporary.replace(args.output)
    print(json.dumps({"ready": ready, "cap": cap, "kept_sectors": len(kept), "output": str(args.output)}, ensure_ascii=False))
    return 0 if ready else 2


if __name__ == "__main__":
    raise SystemExit(main())
