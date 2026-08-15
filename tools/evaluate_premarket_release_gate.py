#!/usr/bin/env python3
"""Evaluate the 20-day replay, 5-day shadow and 5-day simulation gate."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any


def load_jsons(root: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    if not root.exists():
        return rows
    for path in sorted(root.rglob("*.json")):
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if isinstance(value, dict):
            rows.append({**value, "_evidence_path": str(path)})
    return rows


def dates(rows: list[dict[str, Any]]) -> set[str]:
    return {
        str(row.get("execution_trade_date") or row.get("trade_date") or row.get("date"))
        for row in rows
        if row.get("execution_trade_date") or row.get("trade_date") or row.get("date")
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--replay-dir", type=Path, required=True)
    parser.add_argument("--shadow-dir", type=Path, required=True)
    parser.add_argument("--simulation-dir", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    replay, shadow, simulation = (load_jsons(path) for path in (args.replay_dir, args.shadow_dir, args.simulation_dir))
    counts = {"replay_days": len(dates(replay)), "shadow_days": len(dates(shadow)), "simulation_days": len(dates(simulation))}
    checks = {
        "replay_20_days": counts["replay_days"] >= 20,
        "shadow_5_days": counts["shadow_days"] >= 5,
        "simulation_5_days": counts["simulation_days"] >= 5,
        "no_real_order_contract": all(row.get("real_orders_sent") in {None, False, 0} for row in simulation),
    }
    result = {
        "schema_version": "premarket_release_gate_v1",
        "counts": counts,
        "checks": checks,
        "release_gate": "MET" if all(checks.values()) else "NOT_MET",
        "evidence_roots": {"replay": str(args.replay_dir), "shadow": str(args.shadow_dir), "simulation": str(args.simulation_dir)},
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(result, ensure_ascii=False))
    return 0 if result["release_gate"] == "MET" else 2


if __name__ == "__main__":
    raise SystemExit(main())
