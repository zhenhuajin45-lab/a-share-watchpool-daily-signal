# coding: utf-8
"""从V8真实顺序重放事件生成飞书卡片预览；只写D盘，不发送网络请求。"""

from __future__ import annotations

import json
from pathlib import Path

from action_layer import decide_event_actions, format_action_card
from feishu_cards import build_signal_card, validate_card
from live_signal_service import LiveSignalService


ROOT = Path(r"D:\codex\a_share_rotation")
SOURCE = ROOT / "reports" / "today_v8_capital_behavior_replay_20260811.json"
OUTPUT = ROOT / "reports" / "feishu_v8_capital_signal_preview_20260811.json"
TEXT_OUTPUT = ROOT / "reports" / "feishu_v8_capital_signal_preview_20260811.txt"


def main() -> None:
    replay = json.loads(SOURCE.read_text(encoding="utf-8"))
    event = next(row for row in replay["events"] if row.get("event") == "BUY_EVENT_WATCH")
    decision = decide_event_actions(event)
    symbol = str(event.get("symbol"))
    code = symbol.split(".")[-1]
    capital = event.get("capital_behavior") or {}
    sector = event.get("live_sector") or {}
    displayed_cap = event.get("flow_entry_vwap_gap_cap") or event.get("entry_vwap_gap_cap")
    text = "\n".join([
        "【A股轮动｜T+1早期资金转折信号】",
        f"{event.get('name')}（{code}）｜综合强度 {event.get('composite_signal_strength')}/100｜位置质量 {event.get('entry_quality')}/100",
        format_action_card(decision),
        f"位置：现价 {event.get('price'):.2f}｜VWAP {event.get('vwap'):.2f}｜偏离 {event.get('vwap_gap'):.2%}｜本路径允许上限 {displayed_cap:.2%}",
        f"触发：{LiveSignalService._pattern_cn(event.get('pattern'))}｜状态 {LiveSignalService._entry_state_cn(event.get('entry_state'))}",
        f"板块：{sector.get('theme', '未形成主题')}｜{LiveSignalService._sector_state_cn(sector.get('state'))}｜角色 {LiveSignalService._role_cn(sector.get('role'))}",
        f"多周期：{LiveSignalService._multitimeframe_line(event.get('multitimeframe') or {})}",
        f"资金行为：{LiveSignalService._capital_behavior_line(capital)}",
        "分级：5分钟与持续资金流已确认，15/30分钟尚未全部转强；这是B级早期新开仓观察，不是胜率承诺。",
        "边界：系统不下单；信号送达后若已拉离触发位或资金状态退化则不追；实际买入后当天不得卖出。",
    ])
    card = build_signal_card(
        text,
        event=event,
        short_code=code,
        action_decision=decision,
        template="green",
    )
    errors = validate_card(card)
    if errors:
        raise RuntimeError(f"飞书卡片校验失败：{errors}")
    OUTPUT.write_text(json.dumps(card, ensure_ascii=False, indent=2), encoding="utf-8")
    TEXT_OUTPUT.write_text(text + "\n", encoding="utf-8")
    print(json.dumps({"card": str(OUTPUT), "text": str(TEXT_OUTPUT), "validation": "PASS"}, ensure_ascii=False))


if __name__ == "__main__":
    main()
