# coding: utf-8
"""飞书交互卡片 UI 组件。

目标不是把日志原样搬进群，而是让使用者按“结论 -> 动作 -> 依据 -> 边界”阅读。
本模块只负责展示，不参与信号判断，也不改变任何交易动作。
"""

from __future__ import annotations

import re
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple


CARD_UI_VERSION = "feishu_card_ui_v1"


def signal_template(event_type: str, pattern: str = "") -> str:
    """按事件风险/生命周期分配稳定颜色，颜色只表达动作层级。"""

    if event_type == "BUY_EVENT_WATCH":
        return "green"
    if event_type == "SELL_EVENT_WATCH":
        return "red"
    if event_type in {"RISK_EVENT_WATCH", "DISCOVERY_EVENT_WATCH"}:
        return "orange"
    if pattern == "PULLBACK_IN_PROGRESS":
        return "turquoise"
    if pattern in {"SUDDEN_TREND_ARMED", "ARMED_WAIT_PULLBACK"}:
        return "blue"
    if pattern == "PLATFORM_REACCELERATION_SHADOW":
        return "purple"
    if pattern == "PRELIMINARY_TREND_WATCH":
        return "grey"
    return "blue"


def _clean(value: Any, limit: Optional[int] = None) -> str:
    text = str(value or "").replace("\r\n", "\n").replace("\r", "\n").strip()
    text = re.sub(r"\n{3,}", "\n\n", text)
    if limit is not None and len(text) > limit:
        return text[: max(0, limit - 12)].rstrip() + "\n…（已精简）"
    return text


def _field(label: str, value: Any) -> Dict[str, Any]:
    return {
        "is_short": True,
        "text": {
            "tag": "lark_md",
            "content": f"**{_clean(label, 40)}**\n{_clean(value, 180)}",
        },
    }


def _markdown(content: str) -> Dict[str, Any]:
    return {"tag": "div", "text": {"tag": "lark_md", "content": _clean(content)}}


def build_report_card(
    title: str,
    *,
    template: str = "blue",
    fields: Optional[Sequence[Tuple[str, Any]]] = None,
    sections: Optional[Sequence[Tuple[str, str]]] = None,
    footer: str = "",
) -> Dict[str, Any]:
    """生成兼容自定义机器人 Webhook 的经典交互卡片 JSON。"""

    elements: List[Dict[str, Any]] = []
    visible_fields = [(label, value) for label, value in (fields or []) if str(value or "").strip()]
    if visible_fields:
        elements.append({"tag": "div", "fields": [_field(label, value) for label, value in visible_fields]})

    visible_sections = [(heading, _clean(body)) for heading, body in (sections or []) if _clean(body)]
    for index, (heading, body) in enumerate(visible_sections):
        if elements or index:
            elements.append({"tag": "hr"})
        elements.append(_markdown(f"**{_clean(heading, 80)}**\n{body}"))

    if footer:
        if elements:
            elements.append({"tag": "hr"})
        elements.append({
            "tag": "note",
            "elements": [{"tag": "plain_text", "content": _clean(footer, 500)}],
        })

    return {
        "config": {"wide_screen_mode": True, "enable_forward": True},
        "header": {
            "template": template,
            "title": {"tag": "plain_text", "content": _clean(title, 120)},
        },
        "elements": elements,
    }


def extract_title(text: str, default: str = "A股轮动信号") -> Tuple[str, List[str]]:
    lines = [line.rstrip() for line in str(text or "").splitlines()]
    while lines and not lines[0].strip():
        lines.pop(0)
    if not lines:
        return default, []
    first = lines.pop(0).strip()
    match = re.match(r"^[【\[](.+?)[】\]]$", first)
    return (match.group(1) if match else first or default), lines


def build_signal_card(
    text: str,
    *,
    event: Dict[str, Any],
    short_code: str,
    action_decision: Dict[str, Any],
    template: str,
    footer: str = "",
) -> Dict[str, Any]:
    """把实时事件压缩成动作优先的卡片，原文本仍作为审计/回退保留。"""

    title, lines = extract_title(text)
    name = str(event.get("name") or "未知标的")
    event_ts = str(event.get("event_ts") or "")
    event_time = event_ts[11:19] if len(event_ts) >= 19 else event_ts or "实时"
    price = event.get("price")
    price_text = f"{float(price):.3f}".rstrip("0").rstrip(".") if price not in (None, "") else "—"
    strength = event.get("composite_signal_strength")
    capital = event.get("capital_behavior") or {}
    timing = event.get("structured_timing") or {}
    structure = capital.get("structure") or (event.get("candidate") or {}).get("capital_structure") or {}
    empty = action_decision.get("empty_position") or {}
    held = action_decision.get("existing_position") or {}

    fields: List[Tuple[str, Any]] = [
        ("标的", f"{name}（{short_code}）"),
        ("事件时间", event_time),
        ("现价", price_text),
        ("综合强度", f"{strength}/100（非胜率）" if strength is not None else "—"),
    ]
    if capital:
        fields.append((
            "日内资金行为",
            f"{capital.get('regime_cn', capital.get('phase_cn', '样本积累中'))}｜{capital.get('score', 50)}/100｜{capital.get('confidence', 'LOW')}",
        ))
    if structure:
        fields.append((
            "大周期资金阶段",
            f"{structure.get('phase_cn', '未判断')}｜{structure.get('score', 50)}/100｜{structure.get('entry_prior', 'NEUTRAL')}",
        ))
    if timing.get("status") == "READY":
        location = timing.get("location") or {}
        fields.append((
            "结构择时（零权重）",
            f"{timing.get('path','—')}｜{location.get('state','—')}｜Room {float(location.get('room_atr') or 0):.2f}ATR｜{timing.get('shadow_score',0)}分",
        ))

    action_body = "\n".join([
        f"🟢 **空仓：{empty.get('label', '等待')}**｜{empty.get('instruction', '')}",
        f"🔵 **已有仓位：{held.get('label', '观察')}**｜{held.get('instruction', '')}",
        f"⏱️ **切换条件**｜{action_decision.get('urgency', '等待下一事件')}；{action_decision.get('invalidation', '')}",
        f"🧭 **结构旁证（零权重）**｜{action_decision.get('structured_timing_advisory', '尚无结构择时上下文')}",
    ])

    # 旧文本中的行动指令在卡片顶部已经单独展示，避免重复造成“一大坨”。
    details: List[str] = []
    skipping_action = False
    for raw in lines:
        line = raw.strip()
        if not line:
            continue
        if line == "【行动指令｜先看这里】":
            skipping_action = True
            continue
        if skipping_action and (
            line.startswith("空仓：") or line.startswith("已有仓位：")
            or line.startswith("切换：") or line.startswith("结构旁证：")
        ):
            continue
        skipping_action = False
        # 第一行的名称/强度已进入顶部字段，不重复展示。
        if name in line and ("强度" in line or short_code in line):
            continue
        details.append(f"- {line}" if not line.startswith(("- ", "• ")) else line)

    return build_report_card(
        title,
        template=template,
        fields=fields,
        sections=[
            ("🎯 现在怎么做", action_body),
            ("🔎 触发依据", "\n".join(details) or "等待下一条完整事件。"),
        ],
        footer=footer or f"UI {CARD_UI_VERSION}｜仅发信号、不下单｜A股新买入部分严格遵守T+1",
    )


def build_text_summary_card(
    text: str,
    *,
    template: str = "blue",
    fields: Optional[Sequence[Tuple[str, Any]]] = None,
    footer: str = "只发观察信号，不自动下单；所有事实同步保留在D盘审计日志。",
) -> Dict[str, Any]:
    """给盘前、竞价、固定总结提供可读性回退；按中文章节自动分区。"""

    title, lines = extract_title(text)
    sections: List[Tuple[str, str]] = []
    heading = "📌 核心结论"
    bucket: List[str] = []
    section_pattern = re.compile(r"^(?:[一二三四五六七八九十]+、|#+\s*)")
    for raw in lines:
        line = raw.strip()
        if not line:
            continue
        if section_pattern.match(line):
            if bucket:
                sections.append((heading, "\n".join(bucket)))
            heading = line.lstrip("# ")
            bucket = []
        else:
            bucket.append(line)
    if bucket:
        sections.append((heading, "\n".join(bucket)))
    if not sections:
        sections = [(heading, "暂无可展示内容。")]
    # 不静默截断章节；超过飞书20KB时由可靠投递层按完整元素自动拆页。
    return build_report_card(title, template=template, fields=fields, sections=sections, footer=footer)


def validate_card(card: Dict[str, Any]) -> List[str]:
    """发送前的轻量结构自检；返回错误列表。"""

    errors: List[str] = []
    if not isinstance(card, dict):
        return ["card_not_dict"]
    if not ((card.get("header") or {}).get("title") or {}).get("content"):
        errors.append("missing_header_title")
    if not isinstance(card.get("elements"), list) or not card.get("elements"):
        errors.append("missing_elements")
    if len(str(card)) > 28000:
        errors.append("card_too_large")
    return errors
