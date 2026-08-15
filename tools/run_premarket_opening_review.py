#!/usr/bin/env python3
"""Build and persist the 09:20 tighten-only revision."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from premarket_command.opening_review import apply_opening_tighten_only  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--published", type=Path, required=True)
    parser.add_argument("--opening-command", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    baseline = json.loads(args.published.read_text(encoding="utf-8"))
    opening = json.loads(args.opening_command.read_text(encoding="utf-8"))
    result = apply_opening_tighten_only(baseline, opening)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    temporary = args.output.with_suffix(args.output.suffix + ".tmp")
    temporary.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temporary.replace(args.output)
    print(json.dumps(result["opening_delta_audit"], ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
