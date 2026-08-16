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
    parser.add_argument("--gm-market-bundle", type=Path)
    parser.add_argument("--market-sentiment", type=Path)
    parser.add_argument("--gm", type=Path, required=True)
    parser.add_argument("--author-ledger", type=Path, required=True)
    parser.add_argument("--external", type=Path, required=True)
    parser.add_argument("--sector-cycle", type=Path)
    parser.add_argument("--topic-context", type=Path)
    parser.add_argument("--kaipanla-cross-evidence", type=Path)
    parser.add_argument("--release-gate", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    ledger = load(args.author_ledger)
    gm_market_bundle = load(args.gm_market_bundle) if args.gm_market_bundle else None
    if gm_market_bundle:
        if gm_market_bundle.get("status") != "READY" or not (gm_market_bundle.get("source_health") or {}).get("ready"):
            parser.error("--gm-market-bundle must be READY with source_health.ready=true")
        market_sentiment = gm_market_bundle.get("market_sentiment") or {}
        sector_cycle = gm_market_bundle.get("sector_cycle") or {}
    else:
        if not args.market_sentiment or not args.sector_cycle:
            parser.error("provide --gm-market-bundle or both --market-sentiment and --sector-cycle")
        market_sentiment = load(args.market_sentiment)
        sector_cycle = load(args.sector_cycle)
    topic_context = load(args.topic_context) if args.topic_context else {"status": "UNAVAILABLE", "headlines": [], "risk_headlines": []}
    cross_evidence = load(args.kaipanla_cross_evidence) if args.kaipanla_cross_evidence else {"status": "UNAVAILABLE", "policy": "cross_evidence_only"}
    release_gate = load(args.release_gate) if args.release_gate else {"release_gate": "NOT_MET", "reason": "acceptance_evidence_not_supplied"}
    payload = {
        "source_trade_date": args.source_trade_date.replace("-", ""),
        "execution_trade_date": args.execution_trade_date.replace("-", ""),
        "market_sentiment": market_sentiment,
        "major_indices": load(args.gm).get("major_indices") or [],
        "author_ratio": {
            "metric_name": "作者多空比",
            "calibration_only": True,
            "thresholds": {"bottom_watch": 0.3, "negative_effect": 0.6, "balance": 1.0, "stage_top_watch_1": 1.5, "stage_top_watch_2": 2.0},
            "observations": ledger.get("observations") or [],
            "ledger_path": str(args.author_ledger),
        },
        "external_market": load(args.external),
        "sector_cycle": sector_cycle,
        "topic_context": topic_context,
        "cross_evidence": {"kaipanla": cross_evidence},
        "operational_acceptance": release_gate,
        "evidence_paths": {
            "gm_market_bundle": str(args.gm_market_bundle) if args.gm_market_bundle else None,
            "market_sentiment": str(args.market_sentiment) if args.market_sentiment else None,
            "gm": str(args.gm),
            "author_ledger": str(args.author_ledger),
            "external": str(args.external),
            "sector_cycle": str(args.sector_cycle) if args.sector_cycle else None,
            "topic_context": str(args.topic_context) if args.topic_context else None,
            "kaipanla_cross_evidence": str(args.kaipanla_cross_evidence) if args.kaipanla_cross_evidence else None,
            "release_gate": str(args.release_gate) if args.release_gate else None,
        },
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    temporary = args.output.with_suffix(args.output.suffix + ".tmp")
    temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temporary.replace(args.output)
    print(json.dumps({"output": str(args.output), "source_trade_date": payload["source_trade_date"], "gm_market_primary": bool(gm_market_bundle), "release_gate": release_gate.get("release_gate")}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
