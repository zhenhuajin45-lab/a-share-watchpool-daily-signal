# coding: utf-8
"""把研究信号翻译成用户可以直接理解的行为指令。

这一层不产生订单，也不假设已经读取券商真实持仓。每个事件同时给出空仓和已有仓位
两条路径，避免把“机会”“风险”误读成无条件买卖指令。
"""

from __future__ import annotations

from typing import Any, Dict, Optional


ACTION_RULES_VERSION = "behavior_action_layer_v8_explicit_wait_and_leader_acceptance"


def _safe_timing_float(value: Any, default: float = 99.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def build_position_context(
    event_date: str,
    entry_date: str = "",
    broker_position: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """明确区分券商真实数量与信号虚拟台账，绝不伪造可卖数量。"""
    broker_position = broker_position or {}
    broker_known = bool(broker_position)
    total_qty = broker_position.get("total_qty") if broker_known else None
    sellable_qty = broker_position.get("sellable_qty") if broker_known else None
    today_bought_qty = broker_position.get("today_bought_qty") if broker_known else None
    same_day_locked = bool(entry_date and event_date and entry_date[:10] == event_date[:10])
    if broker_known:
        state = "TODAY_LOCKED" if _safe_timing_float(sellable_qty, 0.0) <= 0 and _safe_timing_float(total_qty, 0.0) > 0 else "SELLABLE_AVAILABLE"
    elif same_day_locked:
        state = "SIGNAL_ENTRY_TODAY_BROKER_QTY_UNKNOWN"
    elif entry_date:
        state = "SIGNAL_POSITION_PRIOR_DAY_BROKER_QTY_UNKNOWN"
    else:
        state = "NO_POSITION_FACT"
    return {
        "state": state,
        "entry_date": entry_date[:10] or None,
        "event_date": event_date[:10] or None,
        "same_day_signal_locked": same_day_locked,
        "broker_position_known": broker_known,
        "total_qty": total_qty,
        "sellable_qty": sellable_qty,
        "today_bought_qty": today_bought_qty,
        "quantity_boundary": "BROKER_FACT" if broker_known else "UNKNOWN_NOT_INFERRED_FROM_SIGNAL_LEDGER",
    }


def _leg(code: str, label: str, instruction: str) -> Dict[str, str]:
    return {"code": code, "label": label, "instruction": instruction}


def _price(value: Any) -> str:
    try:
        number = float(value)
        return f"{number:.3f}".rstrip("0").rstrip(".")
    except (TypeError, ValueError):
        return "—"


def build_waiting_conditions(event: Dict[str, Any], candidate: Dict[str, Any]) -> Dict[str, Any]:
    """把抽象的“等待”翻译为可观察的升级、放弃和危险条件。"""
    plan = candidate.get("price_battle_plan") or {}
    tiers = {str(row.get("tier")): row for row in plan.get("entry_tiers") or []}
    tier_a, tier_b = tiers.get("A", {}), tiers.get("B", {})
    risk = plan.get("risk_levels") or {}
    vwap = event.get("vwap")
    reclaim = event.get("trend_reclaim_level") or tier_a.get("trigger_above")
    gate = event.get("gate_evidence") or {}
    missing = []
    labels = {
        "auction_ready": "竞价可验证",
        "sector_ready": "板块仍属前排/健康",
        "multitimeframe_ready": "5分钟触发且15/30至少一档支持",
        "capital_entry_ready": "资金持续承接、未转确认流出",
        "t1_survivability_ready": "T+1隔夜生存性达到正式级",
        "new_entry_extension_ok": "距离涨停/极端伸展仍有安全余量",
    }
    for key, label in labels.items():
        if key in gate and not gate.get(key):
            missing.append(label)
    route_ready = any(bool(gate.get(key)) for key in (
        "recent_pullback_reclaimed", "capital_flow_continuation_ready", "sector_leader_acceptance_ready",
    ))
    if not route_ready:
        missing.append("三条入场路线至少一条：回踩收复 / 持续流入 / 强板块前排承接")
    if not (gate.get("location_ready") or gate.get("flow_location_ready")):
        missing.append("现价回到普通或续强VWAP位置上限内")
    ordinary_gap_cap = _safe_timing_float(event.get("entry_vwap_gap_cap"), 0.0)
    flow_gap_cap = _safe_timing_float(event.get("flow_entry_vwap_gap_cap"), 0.0)
    ordinary_vwap_max = _safe_timing_float(vwap, 0.0) * (1.0 + ordinary_gap_cap) if vwap else 0.0
    flow_vwap_max = _safe_timing_float(vwap, 0.0) * (1.0 + flow_gap_cap) if vwap else 0.0
    leader_max = event.get("sector_leader_acceptance_max_price")
    upgrade = (
        f"路线A：回踩{_price(tier_a.get('zone_low'))}~{_price(tier_a.get('zone_high'))}或VWAP {_price(vwap)}附近后，"
        f"重新收复{_price(reclaim)}；普通路线现价不高于约{_price(ordinary_vwap_max)}，"
        "同时板块前排、5/15分钟与持续资金承接不退化"
    ) if tier_a else (
        f"重新收复{_price(reclaim)}" + (f"/VWAP {_price(vwap)}" if vwap else "")
        + "，且板块、多周期和资金共同恢复"
    )
    breakout = (
        f"路线B：有效突破{_price(tier_b.get('trigger_above'))}后保持一个完整5分钟周期并回踩{_price(tier_b.get('zone_low'))}附近不破；"
        f"若属于强板块前排，也可在150秒承接确认、5/15分钟同向、资金持续流入且现价不高于"
        f"{_price(leader_max or flow_vwap_max)}时升级"
        if tier_b else "另一条路：平台突破后至少一个完整5分钟周期保持接受"
    )
    abandon = (
        f"A回踩路线跌破{_price(risk.get('pullback_failure_below') or risk.get('structure_failure_below'))}，"
        f"或B突破路线跌回{_price(risk.get('breakout_failure_below'))}以下且不能快速收复；"
        "板块轮出/资金确认流出时同样取消本轮"
        if risk else "深破VWAP且板块轮出或资金确认流出：取消本轮"
    )
    danger = (
        f"高于{_price(risk.get('no_chase_above'))}属于追高危险区；"
        f"跌破{_price(risk.get('hard_risk_below'))}属于高风险区"
        if risk else "脉冲拉升后远离VWAP，或30/60分钟顶背离叠加资金流出，属于危险状态"
    )
    return {
        "upgrade": upgrade, "alternate": breakout, "abandon": abandon, "danger": danger,
        "missing": missing, "source": "D_MINUS_1_PRICE_PLAN_PLUS_CURRENT_EVENT_FACTS",
        "no_lookahead": True,
    }


def decide_event_actions(
    event: Dict[str, Any],
    candidate: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """为实时事件生成空仓/持仓两套动作，不使用事件发生后的未来数据。"""

    candidate = candidate or {}
    event_type = str(event.get("event") or "")
    pattern = str(event.get("pattern") or "")
    event_date = str(event.get("event_ts") or "")[:10]
    entry_date = str(
        event.get("position_entry_date")
        or candidate.get("position_entry_date")
        or ""
    )[:10]
    same_day_locked = bool(event.get("t_plus_one_blocked")) or bool(
        entry_date and event_date and entry_date == event_date
    )
    position_context = build_position_context(
        event_date,
        entry_date,
        event.get("broker_position") if isinstance(event.get("broker_position"), dict) else None,
    )
    timing = event.get("structured_timing") or candidate.get("structured_timing") or {}
    waiting = build_waiting_conditions(event, candidate)

    if event_type == "BUY_EVENT_WATCH":
        t1 = event.get("t1_survivability") or {}
        t1_grade = str(t1.get("grade") or event.get("entry_tier") or "")
        if pattern == "CAPITAL_LED_EARLY_REVERSAL":
            empty = _leg(
                "OPEN_NEW_EARLY_FLOW",
                f"新开仓（早期资金转折｜T+1生存{t1_grade or '待核验'}级）",
                "5分钟早期资金转折已同时通过位置、承接和T+1生存门；只在现价仍贴近触发位且资金状态未退化时处理，严禁追涨。",
            )
        elif pattern == "CAPITAL_FLOW_CONTINUATION":
            empty = _leg(
                "OPEN_NEW_FLOW_CONFIRMED",
                "新开仓（资金延续）",
                "大周期、板块和连续资金流已经共振；仅在现价仍贴近触发价、盘口流入未反转时执行，消息到达后若已脉冲拉升则不追。",
            )
        elif pattern == "SECTOR_LEADER_ACCEPTANCE":
            empty = _leg(
                "OPEN_NEW_SECTOR_LEADER",
                f"新开仓（板块前排承接｜T+1生存{t1_grade or '待核验'}级）",
                "强板块前排已完成至少150秒承接，5/15分钟结构与持续流入或卖压吸收得到确认，且30/60分钟无有效顶背离；只在现价仍未越过触发危险区时处理，若消息到达后快速拉离VWAP则放弃追单。",
            )
        else:
            empty = _leg(
                "OPEN_NEW",
                "新开仓",
                "仅在现价仍贴近触发价且结构未失效时执行；消息到达后若已拉升则不追单。",
            )
        held = _leg(
            "HOLD_OR_T_BUY",
            "继续持有/可做T",
            "原仓继续持有；有昨日前可卖底仓时，可把本事件作为做T买入腿。",
        )
        flow_patterns = {"CAPITAL_FLOW_CONTINUATION", "CAPITAL_LED_EARLY_REVERSAL", "SECTOR_LEADER_ACCEPTANCE"}
        urgency = "资金流与当前结构仍有效时处理" if pattern in flow_patterns else "当前结构有效时处理"
        invalidation = "主动成交、盘口补单或VWAP接受度任两项转弱：取消资金买点。" if pattern in flow_patterns else "跌破VWAP或关键回踩位，同时板块转为分歧/退潮：取消新开仓并停止T买。"
    elif event_type == "OPPORTUNITY_EVENT_WATCH":
        if pattern == "POSITION_RECOVERY_AFTER_REDUCE":
            empty = _leg(
                "WAIT_NEW_ENTRY", "空仓继续等待",
                "这是减仓后的状态修复，不等同于新的T+1开仓确认；空仓者等待完整买点。",
            )
            held = _leg(
                "HOLD_RECOVERY_OBSERVE", "剩余仓位继续观察/停止继续减仓",
                "若此前仅减仓且剩余仓位仍在，可停止继续减仓；本事件不支持回补已减部分，只有以后出现新的正式买点才重新增加风险。",
            )
            urgency = "风险缓解，只停止继续减仓，不追价回补"
            invalidation = "再次跌破VWAP且资金/板块重新转弱：恢复事件失效，继续按下一条减仓或卖出事件处理。"
        elif pattern == "PLATFORM_REACCELERATION_SHADOW":
            empty = _leg(
                "WATCH_PLATFORM_SHADOW", "观察/不直接建仓",
                "平台再加速已经被捕捉，但历史分段稳定性尚不足；当前只作影子提示，等待正式回踩收复或后续规则验收。",
            )
            held = _leg(
                "HOLD_PLATFORM_T_REFERENCE", "继续持有/做T参考",
                "原仓继续持有；有昨日前可卖底仓时，可把再加速作为持仓管理参考，不据此新增独立趋势仓。",
            )
            urgency = "平台再加速影子事件，不追价"
            invalidation = "跌回平台或资金持续性消失：影子机会失效；后续正式事件重新判断。"
        elif pattern == "PRELIMINARY_TREND_WATCH":
            empty = _leg("WAIT_COMPLETED_5M", "等待", "先等首个完整5分钟触发，不在开盘噪声中追价。")
            held = _leg("HOLD_OBSERVE", "继续持有观察", "原仓继续持有；分钟周期未确认前不新增做T买入腿。")
            urgency = "预观察，等待完整5分钟K线"
            invalidation = "5分钟未触发或板块转弱：退出本轮观察；触发后升级为已武装。"
        elif pattern == "ARMED_WAIT_PULLBACK" or pattern == "SUDDEN_TREND_ARMED":
            empty = _leg("WAIT_PULLBACK_RECLAIM", "盯回踩/不追", "趋势条件已成立，但位置不适合追；等待近期回踩VWAP/承接区后重新收复。")
            held = _leg("HOLD_WAIT_T_BUY", "继续持有/等T买点", "原仓继续持有；有可卖底仓也只在回踩收复事件后考虑做T买入腿。")
            urgency = "已武装，事件驱动等待回踩"
            invalidation = "回踩后收复且板块、多周期仍健康：升级新开仓；深破VWAP或板块退潮：解除武装。"
        elif pattern == "PULLBACK_IN_PROGRESS":
            empty = _leg("WAIT_RECLAIM", "等待收复", "回踩已出现，但尚未确认重新承接；不要抢跑。")
            held = _leg("HOLD_WATCH_RECLAIM", "继续持有/盯收复", "原仓继续持有；等待价格重新收复承接位后再考虑做T买入腿。")
            urgency = "回踩进行中，等待收复确认"
            invalidation = "收复承接位且条件保持：升级；继续下破或板块转弱：本轮失效。"
        else:
            empty = _leg(
                "WAIT_FORMAL_ENTRY",
                "等待",
                "尚未达到T+1新开仓门槛，不把机会信号当作新开仓指令；等待后续升级。",
            )
            held = _leg(
                "HOLD_T_REFERENCE",
                "继续持有/做T参考",
                "原仓继续持有；有昨日前可卖底仓时，可把本事件作为低吸或做T参考，但不新增独立趋势仓。",
            )
            urgency = "观察级，不追价"
            invalidation = "跌破VWAP/承接位或板块退潮：放弃做T买入；若随后升级正式买点，再重新判断。"
    elif event_type == "DISCOVERY_EVENT_WATCH":
        empty = _leg(
            "DO_NOT_BUY",
            "不买",
            "已经发现趋势但当前不可成交或位置不适合执行，禁止把“捕捉到”误当成买点。",
        )
        held = _leg(
            "HOLD_OBSERVE",
            "继续持有观察",
            "已有仓位继续观察，不在封板或流动性不足的位置追做T；等待恢复可成交后的新事件。",
        )
        urgency = "等待恢复成交"
        invalidation = "开板后承接失败或跌破VWAP：转为风险观察；恢复成交且重新共振才允许升级。"
    elif event_type == "RISK_EVENT_WATCH":
        empty = _leg(
            "AVOID_ENTRY",
            "不买",
            "趋势延续已经失效，空仓者不介入，等待新的完整买点。",
        )
        if same_day_locked:
            held = _leg(
                "T1_LOCKED_STOP_ADDING",
                "T+1锁定/停止加仓",
                "如果是今日新仓，当天不能卖，立即停止加仓并列入下一交易日优先处理；如另有昨日前可卖底仓，可先降低可卖部分风险。",
            )
        else:
            held = _leg(
                "REDUCE",
                "减仓",
                "延续结构已失效，对昨日前可卖仓位降低风险；剩余仓位等待后续卖出或修复事件。",
            )
        urgency = "风险事件，立即停止新增风险"
        invalidation = "重新站稳VWAP并恢复板块及多周期共振：风险缓解，但必须等待新的买点才能重新增加风险。"
    elif event_type == "SELL_EVENT_WATCH":
        # 小周期共振转弱只足以降低风险，不能单独宣判日线主趋势结束；硬止损才直接退出。
        strong_exit_patterns = {
            "VIRTUAL_STOP_LOSS", "STRUCTURE_FAILURE_EXIT", "DUAL_30_60_DIVERGENCE_EXIT",
            "CONFIRMED_TREND_EXIT", "PANIC_WEAK_POSITION_EXIT",
            "T1_FAILED_CONTINUATION_EXIT",
        }
        action_code = "EXIT" if event.get("exit_tier") == "EXIT" or pattern in strong_exit_patterns else "REDUCE"
        action_label = "卖出" if action_code == "EXIT" else "减仓"
        if same_day_locked:
            held = _leg(
                "T1_LOCKED_STOP_ADDING",
                "T+1锁定/停止加仓",
                "今日新买部分依法不能卖；停止加仓。若同时持有昨日前可卖底仓，可按本卖点先处理可卖部分。",
            )
        elif action_code == "EXIT":
            held = _leg(
                "EXIT",
                "卖出",
                "卖点已由价格、动量/盘口及多周期组合确认；对可卖仓位执行退出，不再把它仅当作普通提醒。",
            )
        else:
            held = _leg(
                "REDUCE",
                "减仓",
                "组合转弱已经确认，先降低可卖仓位风险；只有硬止损或更高周期、资金与板块继续共同恶化，才升级为全部卖出。",
            )
        empty = _leg("DO_NOT_BUY", "不买", "当前是减风险事件，空仓者不抄底，等待新的正式买点。")
        urgency = f"{action_label}事件，按可卖数量处理"
        invalidation = "若未执行前快速收复VWAP、重新成为板块前排且多周期修复，可暂缓并等待下一事件；已经成交的不追价买回。"
    else:
        empty = _leg("WAIT", "等待", "当前事件没有形成可执行的新开仓动作。")
        held = _leg("HOLD_OBSERVE", "继续持有观察", "维持原状态，等待明确的买点、减仓或卖出事件。")
        urgency = "等待明确事件"
        invalidation = "以下一条实时事件为准。"

    timing_path = str(timing.get("path") or "")
    timing_location = timing.get("location") or {}
    if timing.get("shadow_entry_ready"):
        timing_advisory = "结构择时影子层命中GAP_HOLD快速研究候选；这是特定路线的零权重旁证，不代表通用条件齐备，也不能替代正式事件。"
    elif timing_path in {"GAP_FAILURE", "RALLY_FAILURE", "DISTRIBUTION_SHOCK"}:
        timing_advisory = f"结构择时影子层识别{timing_path}，新动作先等路径修复；它是风险旁证，不单独触发卖出。"
    elif _safe_timing_float(timing_location.get("room_atr")) < 1.0 and timing_location:
        timing_advisory = "结构择时影子层显示上方空间不足1ATR；注意位置性价比，但不机械否决强趋势延续。"
    else:
        timing_advisory = "结构择时影子层尚未齐备；继续等待动态路径、空间、15分钟Setup与逐笔执行证据。"

    return {
        "rules_version": ACTION_RULES_VERSION,
        "event_type": event_type,
        "pattern": pattern,
        "empty_position": empty,
        "existing_position": held,
        "urgency": urgency,
        "invalidation": invalidation,
        "t_plus_one_locked": same_day_locked,
        "broker_position_known": position_context["broker_position_known"],
        "position_context": position_context,
        "t1_survivability": event.get("t1_survivability") or {},
        "structured_timing_advisory": timing_advisory,
        "structured_timing_effect": "NONE_SHADOW_ZERO_WEIGHT",
        "waiting_conditions": waiting,
    }


def candidate_action_line(candidate: Dict[str, Any], has_signal_position: bool = False) -> str:
    """盘前/定时总结使用；没有实时事件时绝不提前伪造买卖点。"""

    action = str(candidate.get("action") or "WAIT")
    protection = str(candidate.get("protection_level") or "")
    if has_signal_position:
        if action == "EXIT" or protection == "HIGH":
            return "动作：信号台账继续持有但提高保护；收到实时减仓/卖出事件后再处理，不因高J直接卖。"
        return "动作：信号台账继续持有；等待实时减仓/卖出事件，有可卖底仓时才使用做T信号。"
    if action == "EXIT" or protection == "HIGH":
        return "动作：空仓不买；已有仓位继续持有但进入保护监控，等待实时减仓/卖出事件。"
    if action in {"BUY", "WATCH"} or candidate.get("intraday_eligible"):
        plan = candidate.get("price_battle_plan") or {}
        tiers = {str(row.get("tier")): row for row in plan.get("entry_tiers") or []}
        a, b = tiers.get("A", {}), tiers.get("B", {})
        if a or b:
            return (
                f"动作：空仓优先等A档{_price(a.get('zone_low'))}~{_price(a.get('zone_high'))}回踩收复；"
                f"若不回踩，只接受B档突破{_price(b.get('trigger_above'))}后的5分钟确认。"
                "已有仓位继续持有，实时事件可作做T参考。"
            )
        return "动作：空仓等待实时买点，不在盘前/总结时直接买；已有仓位继续持有，实时机会可作做T参考。"
    return "动作：空仓等待；已有仓位维持原状态，当前没有新增操作。"


def format_action_card(decision: Dict[str, Any]) -> str:
    empty = decision.get("empty_position") or {}
    held = decision.get("existing_position") or {}
    waiting = decision.get("waiting_conditions") or {}
    missing = "、".join(waiting.get("missing") or [])
    return "\n".join([
        "【行动指令｜先看这里】",
        f"空仓：[{empty.get('label', '等待')}] {empty.get('instruction', '')}",
        f"已有仓位：[{held.get('label', '继续持有观察')}] {held.get('instruction', '')}",
        f"切换：{decision.get('urgency', '等待明确事件')}；{decision.get('invalidation', '')}",
        f"等什么：{waiting.get('upgrade', '等待价格、板块、资金和多周期共同确认')}。",
        f"备选触发：{waiting.get('alternate', '平台突破接受')}。",
        f"当前还缺：{missing or '没有固定缺口；等下一次完整事件确认'}。",
        f"放弃/危险：{waiting.get('abandon', '结构失效则取消')}；{waiting.get('danger', '远离VWAP不追')}。",
        f"结构旁证：{decision.get('structured_timing_advisory', '尚无结构择时上下文')}",
    ])
