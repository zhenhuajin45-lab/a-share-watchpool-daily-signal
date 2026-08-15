#!/usr/bin/env python3
"""Build a deterministic premarket command from a normalized input JSON."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


PACKAGE_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PACKAGE_ROOT / "src"))

from premarket_command.engine import build_premarket_command  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--input", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    payload = json.loads(args.input.read_text(encoding="utf-8"))
    command = build_premarket_command(payload)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    temporary = args.output.with_suffix(args.output.suffix + ".tmp")
    temporary.write_text(json.dumps(command, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temporary.replace(args.output)
    print(json.dumps({"status": command["status"], "release_status": command["release_status"], "position_cap": command["position_command"], "output": str(args.output)}, ensure_ascii=False))
    return 0 if command.get("status") == "READY_FOR_DEEPSEEK_REVIEW" else 2


if __name__ == "__main__":
    raise SystemExit(main())
