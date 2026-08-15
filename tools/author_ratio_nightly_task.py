#!/usr/bin/env python3
"""Validate a nightly evidence handoff before appending the author-ratio ledger."""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--trade-date", required=True)
    parser.add_argument("--evidence", type=Path, required=True, help="Browser/OCR evidence JSON; never a guessed ratio")
    parser.add_argument("--ledger", type=Path, required=True)
    args = parser.parse_args()
    evidence = {}
    try:
        evidence = json.loads(args.evidence.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        evidence = {"status": "SOURCE_UNAVAILABLE"}
    status = str(evidence.get("status") or "SOURCE_UNAVAILABLE")
    command = [sys.executable, str(ROOT / "adapters" / "author_ratio_ledger.py"), "--ledger", str(args.ledger), "--trade-date", args.trade_date]
    if status in {"ARTICLE_TEXT_VERIFIED", "ARTICLE_IMAGE_VERIFIED", "CROSS_SOURCE_VERIFIED", "USER_CONFIRMED"} and evidence.get("ratio") is not None:
        command += ["--ratio", str(evidence["ratio"]), "--verification", status, "--source-url", str(evidence.get("source_url") or ""), "--article-title", str(evidence.get("article_title") or ""), "--evidence-path", str(args.evidence), "--evidence-text", json.dumps(evidence, ensure_ascii=False)]
    else:
        attempt = status if status in {"NOT_FOUND", "ARTICLE_FOUND_RATIO_MISSING", "OCR_AMBIGUOUS", "AUTHOR_DID_NOT_PUBLISH", "NON_TRADING_DAY", "SOURCE_UNAVAILABLE"} else "SOURCE_UNAVAILABLE"
        command += ["--attempt-status", attempt, "--source-url", str(evidence.get("source_url") or ""), "--article-title", str(evidence.get("article_title") or ""), "--evidence-path", str(args.evidence), "--evidence-text", json.dumps(evidence, ensure_ascii=False)]
    return subprocess.run(command, check=False).returncode


if __name__ == "__main__":
    raise SystemExit(main())
