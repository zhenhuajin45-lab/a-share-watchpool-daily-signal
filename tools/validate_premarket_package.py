#!/usr/bin/env python3
"""Offline package validation and regression checks."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
REQUIRED = [
    "README.md",
    "src/premarket_command/engine.py",
    "src/premarket_command/review.py",
    "adapters/gm_market_data_adapter.py",
    "adapters/kaipanla_windows_uia_capture.py",
    "adapters/author_ratio_ledger.py",
    "docs/premarket_command/01_LOGIC_SPEC.md",
    "docs/premarket_command/02_DATA_CONTRACTS.md",
    "docs/premarket_command/03_WINDOWS_DEPLOYMENT.md",
    "docs/premarket_command/04_DEEPSEEK_DISCIPLINE.md",
    "docs/premarket_command/05_RUNBOOK_AND_ACCEPTANCE.md",
    "docs/premarket_command/06_SOURCE_MAP.md",
    "docs/premarket_command/CODEX_HANDOFF_PROMPT.md",
    "examples/premarket_input.sample.json",
]


def main() -> int:
    missing = [name for name in REQUIRED if not (ROOT / name).exists()]
    if missing:
        print(json.dumps({"ok": False, "missing": missing}, ensure_ascii=False, indent=2))
        return 1
    sys.path.insert(0, str(ROOT / "src"))
    from premarket_command.engine import build_premarket_command
    from premarket_command.review import apply_restrictive_review

    sample = json.loads((ROOT / "examples" / "premarket_input.sample.json").read_text(encoding="utf-8"))
    command = build_premarket_command(sample)
    base_cap = command["position_command"]["base_cap_pct"]
    sectors = [item["sector_name"] for item in command["sector_rotation"]["primary_attack_sectors"]]
    tightened = apply_restrictive_review(command, {
        "available": True,
        "verdict": "CONFIRM_WITH_RESTRICTIONS",
        "recommended_position_cap_pct": max(base_cap - 10, 0),
        "sector_downgrades": sectors[:1],
    })
    assertions = {
        "no_stock_pool": command["policy"]["contains_stock_pool"] is False and "stock_plan" not in command,
        "deepseek_tighten_only": tightened["position_command"]["base_cap_pct"] <= base_cap,
        "deepseek_cannot_add_sector": len(tightened["sector_rotation"]["primary_attack_sectors"]) == len(sectors),
        "source_health_present": bool(command.get("source_health")),
        "position_cap_bounded": 0 <= base_cap <= 100,
        "partial_evidence_cannot_publish": tightened.get("release_status") == "REVIEW_PENDING",
        "review_cannot_bypass_cap_with_expansion": tightened["position_command"]["conditional_expansion_cap_pct"] <= tightened["position_command"]["base_cap_pct"],
        "no_order_api_in_command_layer": not any(
            token in path.read_text(encoding="utf-8")
            for path in (ROOT / "src" / "premarket_command").glob("*.py")
            for token in ("order_volume(", "order_target_volume(", "order_percent(", "order_target_percent(")
        ),
    }
    tests = subprocess.run([sys.executable, "-m", "pytest", "-q", str(ROOT / "tests")], cwd=ROOT, text=True, capture_output=True)
    ok = all(assertions.values()) and tests.returncode == 0
    print(json.dumps({"ok": ok, "assertions": assertions, "pytest_returncode": tests.returncode, "pytest_output": (tests.stdout + tests.stderr)[-2000:]}, ensure_ascii=False, indent=2))
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
