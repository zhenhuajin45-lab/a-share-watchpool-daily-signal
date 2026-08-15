"""Restrictive DeepSeek review contract for the premarket command."""

from __future__ import annotations

import json
from typing import Any


def build_deepseek_prompt(command: dict[str, Any]) -> str:
    return """你是A股多策略盘前指挥台的反方质检员。请逐项核对输入合同中的大盘情绪、主要指数、多空周期、外围共振、仓位上限、板块轮动、主攻板块和盘前纪律。

硬性纪律：
1. 只能维持、降低仓位或将状态改为观察；禁止提高仓位。
2. 禁止新增输入中不存在的主攻板块，禁止输出股票或股票池。
3. 作者多空比与内部SWR是两个独立指标，禁止比较绝对值或求平均。作者指标看0.3/0.6/1/1.5/2周期位置，SWR验证广度、量能、赚钱效应、指数和主线。
4. 高于2不等于继续看多，必须区分趋势突破和高潮拥挤；作者复刻模型未PROMOTED前不得参与门控。
5. 开盘啦、公众号和新闻标题只做交叉验证。缺失不是空头证据，标题不是事实结论。
6. 每个异议必须引用具体字段和数值。若证据不足，标记REVIEW_PENDING，不得自行补造。
7. 输出严格JSON，不要Markdown。

输出结构：
{"schema_version":"premarket_deepseek_review_v1","available":true,"verdict":"CONFIRM|CONFIRM_WITH_RESTRICTIONS|OBSERVE_ONLY|REVIEW_PENDING","conclusion":"","emotion_audit":[],"index_audit":[],"author_ratio_audit":[],"swr_audit":[],"external_market_audit":[],"sector_rotation_audit":[],"discipline_audit":[],"disagreements":[],"sector_downgrades":[],"recommended_position_cap_pct":0,"opening_change_triggers":[]}

输入合同：
""" + json.dumps(command, ensure_ascii=False, indent=2)


def apply_restrictive_review(command: dict[str, Any], review: dict[str, Any]) -> dict[str, Any]:
    position = command.get("position_command") if isinstance(command.get("position_command"), dict) else {}
    base_cap = int(float(position.get("base_cap_pct") or 0))
    try:
        reviewed_cap = int(float(review.get("recommended_position_cap_pct")))
    except (TypeError, ValueError):
        reviewed_cap = base_cap
    final_cap = min(base_cap, max(reviewed_cap, 0))
    deterministic_expansion = int(float(position.get("conditional_expansion_cap_pct") or base_cap))
    # Expansion is a separate permission and must be explicitly preserved by the
    # reviewer.  A lower reviewed base cap must never be bypassed by an inherited
    # deterministic expansion ceiling.
    try:
        reviewed_expansion = int(float(review.get("recommended_expansion_cap_pct")))
    except (TypeError, ValueError):
        reviewed_expansion = final_cap
    final_expansion = min(deterministic_expansion, final_cap, max(reviewed_expansion, 0))
    verdict = str(review.get("verdict") or "REVIEW_PENDING").upper()
    available = review.get("available") is True
    if verdict in {"OBSERVE_ONLY", "REVIEW_PENDING"}:
        final_expansion = final_cap

    downgraded = {str(value) for value in review.get("sector_downgrades") or []}
    rotation = command.get("sector_rotation") if isinstance(command.get("sector_rotation"), dict) else {}
    primary: list[dict[str, Any]] = []
    for item in rotation.get("primary_attack_sectors") or []:
        row = dict(item)
        if str(row.get("sector_name")) in downgraded:
            row["permission"] = "RECONFIRM_ONLY"
            row["deepseek_downgraded"] = True
        primary.append(row)

    source_health = command.get("source_health") if isinstance(command.get("source_health"), dict) else {}
    source_publishable = source_health.get("publishable") is True
    deterministic_ready = command.get("status") == "READY_FOR_DEEPSEEK_REVIEW"
    publishable = (
        available
        and verdict in {"CONFIRM", "CONFIRM_WITH_RESTRICTIONS"}
        and source_publishable
        and deterministic_ready
    )
    publication_blockers = list(source_health.get("blockers") or [])
    if not available:
        publication_blockers.append("deepseek_unavailable")
    if verdict not in {"CONFIRM", "CONFIRM_WITH_RESTRICTIONS"}:
        publication_blockers.append(f"deepseek_verdict:{verdict}")
    return {
        **command,
        "schema_version": "a_share_premarket_command_reviewed_v1",
        "release_status": "PUBLISHED" if publishable else "REVIEW_PENDING",
        "position_command": {
            **position,
            "deterministic_base_cap_pct": base_cap,
            "deterministic_expansion_cap_pct": deterministic_expansion,
            "base_cap_pct": final_cap,
            "conditional_expansion_cap_pct": final_expansion,
            "conditional_expansion_enabled": final_expansion > final_cap,
        },
        "sector_rotation": {**rotation, "primary_attack_sectors": primary},
        "deepseek_review": review,
        "publication_gate": {
            "deterministic_ready": deterministic_ready,
            "source_publishable": source_publishable,
            "deepseek_available": available,
            "deepseek_verdict": verdict,
            "blockers": list(dict.fromkeys(publication_blockers)),
        },
        "policy": {**(command.get("policy") or {}), "deepseek_adjustment_direction": "tighten_only"},
    }
