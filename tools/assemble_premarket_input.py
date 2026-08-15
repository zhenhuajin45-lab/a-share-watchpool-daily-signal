#!/usr/bin/env python3
"""Merge normalized evidence into the strategy-neutral input contract."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any


def load(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"expected object: {path}")
    return value


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--source-trade-date", required=True)
    parser.add_argument("--execution-trade-date", required=True)
    parser.add_argument("--market-sentiment", type=Path, required=True)
    parser.add_argument("--gm", type=Path, required=True)
    parser.add_argument("--author-ledger", type=Path, required=True)
    parser.add_argument("--external", type=Path, required=True)
    parser.add_argument("--sector-cycle", type=Path, required=True)
    parser.add_argument("--topic-context", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    ledger = load(args.author_ledger)
    payload = {
        "source_trade_date": args.source_trade_date.replace("-", ""),
        "execution_trade_date": args.execution_trade_date.replace("-", ""),
        "market_sentiment": load(args.market_sentiment),
        "major_indices": load(args.gm).get("major_indices") or [],
        "author_ratio": {
            "metric_name": "作者多空比",
            "calibration_only": True,
            "thresholds": {"bottom_watch": 0.3, "negative_effect": 0.6, "balance": 1.0, "stage_top_watch_1": 1.5, "stage_top_watch_2": 2.0},
            "observations": ledger.get("observations") or [],
            "ledger_path": str(args.author_ledger),
        },
        "external_market": load(args.external),
        "sector_cycle": load(args.sector_cycle),
        "topic_context": load(args.topic_context),
        "evidence_paths": {
            "market_sentiment": str(args.market_sentiment),
            "gm": str(args.gm),
            "author_ledger": str(args.author_ledger),
            "external": str(args.external),
            "sector_cycle": str(args.sector_cycle),
            "topic_context": str(args.topic_context),
        },
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    temporary = args.output.with_suffix(args.output.suffix + ".tmp")
    temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temporary.replace(args.output)
    print(json.dumps({"output": str(args.output), "source_trade_date": payload["source_trade_date"], "evidence_paths": len(payload["evidence_paths"])}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
