#!/usr/bin/env python3
"""Publish an already reviewed command; drafts are rejected."""

from __future__ import annotations

import argparse
import json
import sys
import datetime as dt
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from premarket_command.publisher import publish_contract  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--contract", type=Path, required=True)
    parser.add_argument("--publish-root", type=Path, default=ROOT / "data" / "premarket_command" / "published")
    parser.add_argument("--expected-execution-date", default=dt.datetime.now().astimezone().strftime("%Y%m%d"))
    args = parser.parse_args()
    contract = json.loads(args.contract.read_text(encoding="utf-8"))
    if str(contract.get("execution_trade_date") or "").replace("-", "") != args.expected_execution_date.replace("-", ""):
        raise SystemExit("execution_trade_date does not match --expected-execution-date")
    archive, latest = publish_contract(contract, args.publish_root)
    print(json.dumps({"archive": str(archive), "latest": str(latest)}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
