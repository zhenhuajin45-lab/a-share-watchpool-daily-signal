#!/usr/bin/env python3
"""Evaluate the 20-day replay, 5-day shadow and 5-day simulation gate."""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
from pathlib import Path
from typing import Any


STAGE_CONTRACTS = {
    "replay": {
        "schema": "premarket_replay_evidence_v1",
        "checks": ("completed", "no_future_data", "source_dates_verified", "deterministic_contract_built"),
        "requires_no_real_orders": False,
    },
    "shadow": {
        "schema": "premarket_shadow_evidence_v1",
        "checks": ("completed", "read_only", "orders_unchanged", "stale_contract_rejected", "date_mismatch_rejected", "review_pending_rejected"),
        "requires_no_real_orders": True,
    },
    "simulation": {
        "schema": "premarket_simulation_evidence_v1",
        "checks": ("completed", "simulated_only", "position_cap_tighten_only", "sector_whitelist_tighten_only", "no_real_order_api"),
        "requires_no_real_orders": True,
    },
}


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
        normalized
        for row in rows
        if (normalized := valid_date(row.get("execution_trade_date"))) is not None
    }


def valid_date(value: Any) -> str | None:
    text = str(value or "").replace("-", "")
    try:
        parsed = dt.datetime.strptime(text, "%Y%m%d").date()
    except ValueError:
        return None
    return parsed.strftime("%Y%m%d") if len(text) == 8 else None


def resolve_evidence_file(record_path: Path, value: Any) -> Path:
    path = Path(str(value or ""))
    if path.is_absolute():
        return path
    workspace_path = Path.cwd() / path
    return workspace_path if workspace_path.is_file() else record_path.parent / path


def validate_record(row: dict[str, Any], stage: str) -> list[str]:
    contract = STAGE_CONTRACTS[stage]
    reasons: list[str] = []
    if row.get("schema_version") != contract["schema"]:
        reasons.append("schema_version")
    if str(row.get("status") or "").upper() != "PASS":
        reasons.append("status")
    if valid_date(row.get("execution_trade_date")) is None:
        reasons.append("execution_trade_date")
    checks = row.get("checks") if isinstance(row.get("checks"), dict) else {}
    missing_checks = [name for name in contract["checks"] if checks.get(name) is not True]
    if missing_checks:
        reasons.append("checks:" + ",".join(missing_checks))
    if contract["requires_no_real_orders"] and row.get("real_orders_sent") is not False:
        reasons.append("real_orders_sent")
    evidence_files = row.get("evidence_files")
    if not isinstance(evidence_files, list) or not evidence_files:
        reasons.append("evidence_files")
        return reasons
    record_path = Path(str(row.get("_evidence_path") or ""))
    for index, evidence in enumerate(evidence_files):
        if not isinstance(evidence, dict):
            reasons.append(f"evidence_files[{index}]")
            continue
        path = resolve_evidence_file(record_path, evidence.get("path"))
        expected = str(evidence.get("sha256") or "").upper()
        if not path.is_file():
            reasons.append(f"evidence_missing[{index}]")
            continue
        actual = hashlib.sha256(path.read_bytes()).hexdigest().upper()
        if len(expected) != 64 or expected != actual:
            reasons.append(f"evidence_hash[{index}]")
    return reasons


def evaluate(replay: list[dict[str, Any]], shadow: list[dict[str, Any]], simulation: list[dict[str, Any]]) -> dict[str, Any]:
    stage_rows = {"replay": replay, "shadow": shadow, "simulation": simulation}
    valid_by_stage: dict[str, list[dict[str, Any]]] = {}
    invalid: list[dict[str, Any]] = []
    for stage, rows in stage_rows.items():
        valid_rows: list[dict[str, Any]] = []
        for row in rows:
            reasons = validate_record(row, stage)
            if reasons:
                invalid.append({"stage": stage, "path": row.get("_evidence_path"), "reasons": reasons})
            else:
                valid_rows.append(row)
        valid_by_stage[stage] = valid_rows
    counts = {
        "replay_days": len(dates(valid_by_stage["replay"])),
        "shadow_days": len(dates(valid_by_stage["shadow"])),
        "simulation_days": len(dates(valid_by_stage["simulation"])),
    }
    no_real_orders = all(
        row.get("real_orders_sent") is False
        for stage in ("shadow", "simulation")
        for row in valid_by_stage[stage]
    )
    checks = {
        "replay_20_days": counts["replay_days"] >= 20,
        "shadow_5_days": counts["shadow_days"] >= 5,
        "simulation_5_days": counts["simulation_days"] >= 5,
        "no_real_order_contract": no_real_orders,
        "all_evidence_valid": not invalid,
    }
    return {
        "schema_version": "premarket_release_gate_v2",
        "counts": counts,
        "checks": checks,
        "evidence_quality": {
            "valid_files": {stage: len(rows) for stage, rows in valid_by_stage.items()},
            "invalid_files": len(invalid),
            "invalid_evidence": invalid,
        },
        "release_gate": "MET" if all(checks.values()) else "NOT_MET",
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--replay-dir", type=Path, required=True)
    parser.add_argument("--shadow-dir", type=Path, required=True)
    parser.add_argument("--simulation-dir", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    replay, shadow, simulation = (load_jsons(path) for path in (args.replay_dir, args.shadow_dir, args.simulation_dir))
    result = evaluate(replay, shadow, simulation)
    result["evidence_roots"] = {"replay": str(args.replay_dir), "shadow": str(args.shadow_dir), "simulation": str(args.simulation_dir)}
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(result, ensure_ascii=False))
    return 0 if result["release_gate"] == "MET" else 2


if __name__ == "__main__":
    raise SystemExit(main())
