#!/usr/bin/env python3
"""Summarize source evidence health without converting missing data to bearish facts."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any


def load(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return value if isinstance(value, dict) else {}


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--command", type=Path, required=True)
    parser.add_argument("--gm", type=Path)
    parser.add_argument("--gm-market-bundle", type=Path)
    parser.add_argument("--kaipanla", type=Path, action="append", default=[])
    parser.add_argument("--external", type=Path)
    parser.add_argument("--author-ledger", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()

    command = load(args.command)
    sources: dict[str, Any] = {
        "command": {
            "path": str(args.command),
            "status": command.get("status") or "UNAVAILABLE",
            "publishable": (command.get("source_health") or {}).get("publishable") is True,
            "blockers": (command.get("source_health") or {}).get("blockers") or ["command_unavailable"],
        }
    }
    if args.gm:
        value = load(args.gm)
        sources["gm"] = {"path": str(args.gm), "ready": (value.get("sdk_health") or {}).get("ready") is True, "health": value.get("sdk_health") or {}}
    if args.gm_market_bundle:
        value = load(args.gm_market_bundle)
        sources["gm_market_bundle"] = {
            "path": str(args.gm_market_bundle),
            "ready": value.get("status") == "READY" and (value.get("source_health") or {}).get("ready") is True,
            "health": value.get("source_health") or {},
        }
    if args.kaipanla:
        pages = []
        for path in args.kaipanla:
            value = load(path)
            pages.append({"path": str(path), "page_label": value.get("page_label"), "status": value.get("status") or "UNAVAILABLE", "screenshot_path": value.get("screenshot_path")})
        sources["kaipanla"] = {"pages": pages, "ready": len(pages) == 4 and all(row["status"] == "OK" for row in pages), "required": False, "policy": "cross_evidence_only"}
    if args.external:
        value = load(args.external)
        status = str(value.get("status") or "UNAVAILABLE").upper()
        quality = str(value.get("source_quality") or "").lower()
        sources["external"] = {"path": str(args.external), "status": status, "source_quality": quality, "ready": status in {"OK", "READY"} and quality in {"verified_live", "two_source_verified", "cross_checked"}}
    if args.author_ledger:
        value = load(args.author_ledger)
        observations = value.get("observations") if isinstance(value.get("observations"), list) else []
        latest = observations[-1].get("trade_date") if observations else None
        expected = str(command.get("source_trade_date") or "")
        sources["author_ratio"] = {"path": str(args.author_ledger), "latest_trade_date": latest, "verified_observations": len(observations), "ready": bool(expected and latest == expected)}
    required_rows = [row for row in sources.values() if row.get("required", True) is True]
    result = {"schema_version": "premarket_source_health_v1", "sources": sources, "ready": all(row.get("ready", row.get("publishable", False)) is True for row in required_rows)}
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"ready": result["ready"], "output": str(args.output)}, ensure_ascii=False))
    return 0 if result["ready"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
