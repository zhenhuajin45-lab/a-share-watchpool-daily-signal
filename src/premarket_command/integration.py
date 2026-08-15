"""Read-only rendering bridge for the existing plan and Feishu summary."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any


def load_published_command(path: Path, expected_execution_date: str | None = None) -> tuple[dict[str, Any] | None, str]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None, "NOT_FOUND"
    except (OSError, json.JSONDecodeError):
        return None, "INVALID"
    if not isinstance(value, dict) or value.get("release_status") != "PUBLISHED":
        return None, "NOT_PUBLISHED"
    if expected_execution_date and str(value.get("execution_trade_date") or "").replace("-", "") != expected_execution_date.replace("-", ""):
        return None, "DATE_MISMATCH"
    return value, "PUBLISHED"


def plan_sector_alignment(command: dict[str, Any], signals: list[dict[str, Any]]) -> dict[str, Any]:
    rotation = command.get("sector_rotation") if isinstance(command.get("sector_rotation"), dict) else {}
    whitelist = {
        str(item.get("sector_name") or "").strip()
        for item in rotation.get("primary_attack_sectors") or []
        if isinstance(item, dict) and item.get("sector_name")
    }
    aligned, observe_only = [], []
    for signal in signals:
        if not isinstance(signal, dict):
            continue
        sector = str(signal.get("group_key") or signal.get("niche") or "").strip()
        if not sector:
            continue
        (aligned if sector in whitelist else observe_only).append(sector)
    return {
        "whitelist": sorted(whitelist),
        "aligned_plan_sectors": sorted(set(aligned)),
        "non_whitelist_plan_sectors": sorted(set(observe_only)),
        "policy": "alignment_is_informational_until_release_gate_met",
    }


def feishu_command_lines(command: dict[str, Any] | None, status: str, signals: list[dict[str, Any]] | None = None) -> list[str]:
    if not command:
        return [f"盘前指挥台：{status}，不改变现有每日计划与策略门控。"]
    emotion = command.get("market_emotion") if isinstance(command.get("market_emotion"), dict) else {}
    position = command.get("position_command") if isinstance(command.get("position_command"), dict) else {}
    rotation = command.get("sector_rotation") if isinstance(command.get("sector_rotation"), dict) else {}
    sectors = [str(item.get("sector_name")) for item in rotation.get("primary_attack_sectors") or [] if isinstance(item, dict) and item.get("sector_name")]
    alignment = plan_sector_alignment(command, signals or [])
    return [
        f"盘前指挥台：{emotion.get('label') or emotion.get('regime') or '未知'}｜仓位上限{position.get('base_cap_pct', 0)}%",
        f"重点方向：{'、'.join(sectors) if sectors else '无主攻白名单，仅观察'}",
        f"计划一致方向：{'、'.join(alignment['aligned_plan_sectors']) if alignment['aligned_plan_sectors'] else '无'}｜非白名单仅观察：{'、'.join(alignment['non_whitelist_plan_sectors']) if alignment['non_whitelist_plan_sectors'] else '无'}",
        "边界：方向白名单只做计划一致性校验，不授予任何个股买入权限。",
    ]
