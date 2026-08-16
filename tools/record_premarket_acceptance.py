#!/usr/bin/env python3
"""Create hashed replay, shadow, or simulation acceptance evidence."""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from tools.evaluate_premarket_release_gate import STAGE_CONTRACTS, valid_date, validate_record  # noqa: E402


def build_record(
    stage: str,
    execution_trade_date: str,
    checks: list[str],
    evidence_paths: list[Path],
    output: Path,
    confirm_no_real_orders: bool = False,
    notes: str = "",
) -> dict[str, Any]:
    contract = STAGE_CONTRACTS[stage]
    normalized_date = valid_date(execution_trade_date)
    if normalized_date is None:
        raise ValueError("execution date must be YYYYMMDD")
    required_checks = set(contract["checks"])
    supplied_checks = set(checks)
    missing_checks = sorted(required_checks - supplied_checks)
    unknown_checks = sorted(supplied_checks - required_checks)
    if missing_checks or unknown_checks:
        raise ValueError(f"check set mismatch; missing={missing_checks}, unknown={unknown_checks}")
    if not evidence_paths:
        raise ValueError("at least one --evidence-file is required")
    if contract["requires_no_real_orders"] and not confirm_no_real_orders:
        raise ValueError(f"{stage} requires --confirm-no-real-orders")
    evidence_files: list[dict[str, str]] = []
    for path in evidence_paths:
        if not path.is_file():
            raise ValueError(f"evidence file missing: {path}")
        if path.resolve() == output.resolve():
            raise ValueError("acceptance record cannot hash itself as evidence")
        evidence_files.append({
            "path": str(path),
            "sha256": hashlib.sha256(path.read_bytes()).hexdigest().upper(),
        })
    record: dict[str, Any] = {
        "schema_version": contract["schema"],
        "execution_trade_date": normalized_date,
        "status": "PASS",
        "captured_at": dt.datetime.now().astimezone().replace(microsecond=0).isoformat(),
        "checks": {name: True for name in contract["checks"]},
        "evidence_files": evidence_files,
        "notes": notes or None,
        "_evidence_path": str(output),
    }
    if contract["requires_no_real_orders"]:
        record["real_orders_sent"] = False
    reasons = validate_record(record, stage)
    if reasons:
        raise ValueError("record failed self-validation: " + ",".join(reasons))
    record.pop("_evidence_path", None)
    return record


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--stage", choices=sorted(STAGE_CONTRACTS), required=True)
    parser.add_argument("--execution-date", required=True)
    parser.add_argument("--check", action="append", default=[])
    parser.add_argument("--evidence-file", action="append", type=Path, default=[])
    parser.add_argument("--confirm-no-real-orders", action="store_true")
    parser.add_argument("--notes", default="")
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    try:
        record = build_record(
            args.stage,
            args.execution_date,
            args.check,
            args.evidence_file,
            args.output,
            args.confirm_no_real_orders,
            args.notes,
        )
    except ValueError as exc:
        print(json.dumps({"status": "EVIDENCE_RECORD_REJECTED", "error": str(exc)}, ensure_ascii=False))
        return 2
    args.output.parent.mkdir(parents=True, exist_ok=True)
    temporary = args.output.with_suffix(args.output.suffix + ".tmp")
    temporary.write_text(json.dumps(record, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temporary.replace(args.output)
    print(json.dumps({"status": "PASS", "stage": args.stage, "output": str(args.output)}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
