"""Atomic publication for reviewed premarket command contracts."""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

from .acceptance import operational_release_gate_met


def publication_gate_complete(contract: dict[str, Any]) -> bool:
    gate = contract.get("publication_gate") if isinstance(contract.get("publication_gate"), dict) else {}
    return bool(
        contract.get("release_status") == "PUBLISHED"
        and gate.get("deterministic_ready") is True
        and gate.get("source_publishable") is True
        and gate.get("deepseek_available") is True
        and str(gate.get("deepseek_verdict") or "").upper() in {"CONFIRM", "CONFIRM_WITH_RESTRICTIONS"}
        and gate.get("operational_release_gate_met") is True
        and operational_release_gate_met(contract.get("operational_acceptance"))
        and not gate.get("blockers")
    )


def publish_contract(contract: dict[str, Any], publish_root: Path) -> tuple[Path, Path]:
    if contract.get("release_status") != "PUBLISHED":
        raise ValueError("only PUBLISHED contracts may enter the published directory")
    if not publication_gate_complete(contract):
        raise ValueError("publication_gate is incomplete or blocked")
    if (contract.get("policy") or {}).get("contains_stock_pool") is not False:
        raise ValueError("published command must remain stock-pool neutral")
    execution_date = str(contract.get("execution_trade_date") or "")
    if len(execution_date) != 8 or not execution_date.isdigit():
        raise ValueError("execution_trade_date must be YYYYMMDD")
    publish_root.mkdir(parents=True, exist_ok=True)
    archive = publish_root / f"premarket_command_{execution_date}.json"
    latest = publish_root / "latest.json"
    body = json.dumps(contract, ensure_ascii=False, indent=2) + "\n"
    for target in (archive, latest):
        temporary = target.with_suffix(target.suffix + ".tmp")
        temporary.write_text(body, encoding="utf-8")
        os.replace(temporary, target)
    return archive, latest
