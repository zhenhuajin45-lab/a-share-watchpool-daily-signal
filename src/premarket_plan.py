# coding: utf-8
"""盘前低权重均线计划。

本模块只使用D-1及以前的完整日线，回答“次日重点等待哪类结构”。它不能生成
盘前买点，也不能覆盖盘中的板块、资金行为、分钟周期和成交性判断。
"""

from __future__ import annotations

import math
from typing import Any, Dict, Mapping, Optional

import numpy as np
import pandas as pd


PREMARKET_MA_PLAN_VERSION = "premarket_ma_prior_v1_low_weight"
PRICE_BATTLE_PLAN_VERSION = "premarket_price_battle_plan_v2_route_specific_d_minus_1"


def _safe_float(value: Any, default: float = 0.0) -> float:
    try:
        result = float(value)
        return result if math.isfinite(result) else default
    except (TypeError, ValueError):
        return default


def _round_price(symbol: str, value: float) -> float:
    return round(value, 3 if str(symbol).endswith("517400") else 2)


def build_price_battle_plan(
    frame: pd.DataFrame,
    timing_context: Optional[Mapping[str, Any]] = None,
    *,
    symbol: str = "",
    signal_strength: int = 0,
    daily_route: str = "",
    protection_level: str = "",
) -> Dict[str, Any]:
    """把D-1支撑/压力转成次日条件式作战图，价位不是无条件挂单。"""
    if frame is None or len(frame) < 20:
        return {"status": "UNAVAILABLE", "version": PRICE_BATTLE_PLAN_VERSION, "no_lookahead": True}
    data = frame.copy().sort_values("eob") if "eob" in frame.columns else frame.copy()
    close = _safe_float(data.iloc[-1].get("close"))
    if close <= 0:
        return {"status": "UNAVAILABLE", "version": PRICE_BATTLE_PLAN_VERSION, "no_lookahead": True}
    context = dict(timing_context or {})
    atr = _safe_float(context.get("atr_abs"))
    if atr <= 0:
        high = pd.to_numeric(data.get("high"), errors="coerce")
        low = pd.to_numeric(data.get("low"), errors="coerce")
        prior = pd.to_numeric(data.get("close"), errors="coerce").shift(1)
        atr = _safe_float(pd.concat([high - low, (high - prior).abs(), (low - prior).abs()], axis=1).max(axis=1).tail(14).mean(), close * 0.04)
    atr = max(atr, close * 0.008)
    levels = [dict(row) for row in context.get("levels") or [] if _safe_float(row.get("price")) > 0]
    supports = sorted({_safe_float(row.get("price")) for row in levels if row.get("kind") in {"SUPPORT", "DYNAMIC"} and _safe_float(row.get("price")) <= close}, reverse=True)
    resistances = sorted({_safe_float(row.get("price")) for row in levels if row.get("kind") == "RESISTANCE" and _safe_float(row.get("price")) > close})
    ma20 = _safe_float(pd.to_numeric(data["close"], errors="coerce").tail(20).mean())
    support = supports[0] if supports else ma20
    resistance = resistances[0] if resistances else close + 1.5 * atr
    next_resistance = resistances[1] if len(resistances) >= 2 else resistance + 1.2 * atr
    pullback_low = max(support - 0.10 * atr, 0.01)
    pullback_high = support + 0.35 * atr
    reclaim_trigger = pullback_high
    breakout_trigger = resistance + 0.05 * atr
    breakout_accept_low = resistance - 0.12 * atr
    chase_danger = resistance + 0.60 * atr
    structural_fail = max(support - 0.45 * atr, 0.01)
    hard_risk = max(support - 0.85 * atr, 0.01)
    grade = (
        "A" if int(signal_strength) >= 88 and daily_route in {"TREND_CONTINUATION", "TREND_PULLBACK"}
        else "B" if int(signal_strength) >= 78
        else "C"
    )
    grade_note = {
        "A": "盘前重点候选；A回踩与B突破两条实时路线都可验证，仍不能盘前直接下单",
        "B": "次级候选；优先等A回踩，B突破需更强板块与资金证据",
        "C": "观察候选；除非盘中升级为全市场前排并完成承接，否则不开新仓",
    }[grade]
    if protection_level == "HIGH":
        grade_note += "；高位保护开启，允许强势延续但禁止远离VWAP追价"
    return {
        "status": "READY", "version": PRICE_BATTLE_PLAN_VERSION, "symbol": symbol,
        "asof": str(data.iloc[-1].get("eob") or "")[:19], "reference_close": _round_price(symbol, close),
        "atr_abs": _round_price(symbol, atr),
        "plan_grade": grade, "plan_grade_note": grade_note,
        "support": _round_price(symbol, support), "resistance": _round_price(symbol, resistance),
        "next_resistance": _round_price(symbol, next_resistance),
        "entry_tiers": [
            {
                "tier": "A", "name": "回踩收复", "zone_low": _round_price(symbol, pullback_low),
                "zone_high": _round_price(symbol, pullback_high), "trigger_above": _round_price(symbol, reclaim_trigger),
                "condition": "进入支撑区后不破，重新收复触发价/VWAP，且板块仍在前排、5/15分钟转强、资金停止流出",
            },
            {
                "tier": "B", "name": "突破接受", "zone_low": _round_price(symbol, breakout_accept_low),
                "zone_high": _round_price(symbol, breakout_trigger + 0.20 * atr), "trigger_above": _round_price(symbol, breakout_trigger),
                "condition": "突破压力后保持至少一个完整5分钟周期，回踩压力位不破；板块与资金同步扩张",
            },
            {
                "tier": "C", "name": "观察不追", "zone_low": _round_price(symbol, breakout_trigger + 0.20 * atr),
                "zone_high": _round_price(symbol, chase_danger), "trigger_above": None,
                "condition": "只观察强度；除非形成新的平台或回踩收复，否则不作为T+1新仓",
            },
        ],
        "risk_levels": {
            "no_chase_above": _round_price(symbol, chase_danger),
            "structure_failure_below": _round_price(symbol, structural_fail),
            "pullback_failure_below": _round_price(symbol, structural_fail),
            "breakout_failure_below": _round_price(symbol, breakout_accept_low),
            "hard_risk_below": _round_price(symbol, hard_risk),
            "first_take_profit_or_reduce": _round_price(symbol, resistance),
            "second_take_profit_or_protect": _round_price(symbol, next_resistance),
            "pullback_first_target": _round_price(symbol, resistance),
            "pullback_second_target": _round_price(symbol, next_resistance),
            "breakout_first_target": _round_price(symbol, next_resistance),
        },
        "execution_boundary": "价格到位仍不是买卖指令；必须由实时板块、VWAP、资金、多周期和成交性共同确认",
        "price_adjustment": "ADJUST_PREV_FRONT_ADJUSTED", "no_lookahead": True,
    }


def format_price_battle_plan(plan: Mapping[str, Any]) -> str:
    if not plan or plan.get("status") != "READY":
        return "价格作战图：不可用，等待盘中实时结构"
    tiers = {str(row.get("tier")): row for row in plan.get("entry_tiers") or []}
    a, b = tiers.get("A", {}), tiers.get("B", {})
    risk = plan.get("risk_levels") or {}
    return (
        f"盘前分级{plan.get('plan_grade','C')}：{plan.get('plan_grade_note','条件式观察')}\n"
        f"  参考{plan.get('reference_close')}｜支撑{plan.get('support')}｜压力{plan.get('resistance')} / 次压{plan.get('next_resistance')}\n"
        f"  进场A：回踩{a.get('zone_low')}~{a.get('zone_high')}后收复{a.get('trigger_above')}；"
        f"进场B：突破{b.get('trigger_above')}并保持完整5分钟接受\n"
        f"  A路径兑现：{risk.get('pullback_first_target')}首次减速复核，{risk.get('pullback_second_target')}提高保护；"
        f"B路径：{risk.get('breakout_first_target')}先复核，跌回{risk.get('breakout_failure_below')}以下视为突破失败；"
        f"高于{risk.get('no_chase_above')}不追\n"
        f"  危险：A路径跌破{risk.get('pullback_failure_below')}结构失效；跌破{risk.get('hard_risk_below')}进入日线高风险。"
    )


def evaluate_moving_average_prior(frame: pd.DataFrame) -> Dict[str, Any]:
    """生成低权重均线先验；排序修正严格限制在[-4,+4]。"""

    if frame is None or len(frame) < 65:
        return {
            "version": PREMARKET_MA_PLAN_VERSION,
            "status": "UNAVAILABLE",
            "route": "INSUFFICIENT_DATA",
            "route_cn": "均线数据不足",
            "rank_adjustment": 0,
            "supports_watch": False,
            "blocks_entry": False,
            "reason": "至少需要65根D-1及以前完整日线",
            "decision_role": "LOW_WEIGHT_PRIOR_NOT_ENTRY_TRIGGER",
            "no_lookahead": True,
        }

    data = frame.copy()
    if "eob" in data.columns:
        data["eob"] = pd.to_datetime(data["eob"], errors="coerce")
        data = data.dropna(subset=["eob"]).sort_values("eob")
    for column in ("close", "high", "low"):
        if column not in data.columns:
            return {
                "version": PREMARKET_MA_PLAN_VERSION,
                "status": "UNAVAILABLE",
                "route": "MISSING_PRICE_FIELDS",
                "route_cn": "均线字段不足",
                "rank_adjustment": 0,
                "supports_watch": False,
                "blocks_entry": False,
                "reason": "缺少close/high/low字段",
                "decision_role": "LOW_WEIGHT_PRIOR_NOT_ENTRY_TRIGGER",
                "no_lookahead": True,
            }
        data[column] = pd.to_numeric(data[column], errors="coerce")
    data = data.dropna(subset=["close", "high", "low"]).tail(180).reset_index(drop=True)
    if len(data) < 65:
        return {
            "version": PREMARKET_MA_PLAN_VERSION,
            "status": "UNAVAILABLE",
            "route": "INSUFFICIENT_CLEAN_DATA",
            "route_cn": "均线数据不足",
            "rank_adjustment": 0,
            "supports_watch": False,
            "blocks_entry": False,
            "reason": "清洗后完整日线不足65根",
            "decision_role": "LOW_WEIGHT_PRIOR_NOT_ENTRY_TRIGGER",
            "no_lookahead": True,
        }

    close = data["close"].astype(float)
    high = data["high"].astype(float)
    low = data["low"].astype(float)
    ma5 = close.rolling(5).mean()
    ma10 = close.rolling(10).mean()
    ma20 = close.rolling(20).mean()
    ma60 = close.rolling(60).mean()
    current = _safe_float(close.iloc[-1])
    previous = _safe_float(close.iloc[-2])
    current_ma5, current_ma10 = _safe_float(ma5.iloc[-1]), _safe_float(ma10.iloc[-1])
    current_ma20, current_ma60 = _safe_float(ma20.iloc[-1]), _safe_float(ma60.iloc[-1])
    previous_ma20 = _safe_float(ma20.iloc[-2])
    ma20_five_days_ago = _safe_float(ma20.iloc[-6])
    high20_before_today = _safe_float(high.iloc[-21:-1].max())

    previous_close = close.shift(1)
    true_range = pd.concat(
        [high - low, (high - previous_close).abs(), (low - previous_close).abs()], axis=1,
    ).max(axis=1)
    atr14 = _safe_float(true_range.rolling(14).mean().iloc[-1])
    atr_pct = atr14 / current if current > 0 else 0.0
    extension_ma20 = current / current_ma20 - 1.0 if current_ma20 > 0 else 0.0
    breakout_distance = current / high20_before_today - 1.0 if high20_before_today > 0 else 0.0
    ma20_slope_5d = current_ma20 / ma20_five_days_ago - 1.0 if ma20_five_days_ago > 0 else 0.0

    bull_stack = current > current_ma5 > current_ma10 > current_ma20 > current_ma60
    ma20_rising = current_ma20 > previous_ma20 and ma20_slope_5d > 0
    first_reclaim = previous <= _safe_float(ma20.iloc[-2]) and current > current_ma20 and ma20_rising
    near_breakout = current >= high20_before_today * 0.985
    breakout = current >= high20_before_today * 1.001
    # “非伸展”随波动自适应：最多容许约2.2倍ATR，但上限8%、下限3%。
    extension_cap = min(0.08, max(0.03, 2.2 * atr_pct))
    non_extended = extension_ma20 <= extension_cap
    compressed = (
        (high.tail(8).max() - low.tail(8).min()) / max(_safe_float(close.tail(8).median()), 1e-9)
        <= min(0.12, max(0.035, 3.2 * atr_pct))
    )

    if bull_stack and breakout and non_extended:
        route, route_cn, adjustment = "NON_EXTENDED_BREAKOUT", "非伸展20日突破预备", 4
        reason = "均线多头、MA20上行并突破20日平台；只列入盘中突破接受/回踩确认计划"
    elif bull_stack and compressed and near_breakout and non_extended:
        route, route_cn, adjustment = "BULL_STACK_REACCELERATION", "多头排列平台再加速预备", 3
        reason = "均线多头且近8日波动收敛，接近平台；等待盘中放量接受而不是盘前追价"
    elif first_reclaim and extension_ma20 <= max(0.04, 1.5 * atr_pct):
        route, route_cn, adjustment = "MA20_RECLAIM", "MA20首次收复预备", 3
        reason = "D-1收复上行MA20；等待板块修复、VWAP承接及30分钟动能确认"
    elif bull_stack and ma20_rising and non_extended:
        route, route_cn, adjustment = "BULL_TREND_PULLBACK", "多头趋势回踩预备", 2
        reason = "均线多头且尚未明显伸展；盘中只等待回踩承接或再积累完成"
    elif extension_ma20 > extension_cap or breakout_distance > max(0.03, atr_pct):
        route, route_cn, adjustment = "OVEREXTENDED", "趋势伸展保护", -3
        reason = "价格相对MA20/平台已经伸展；空仓不按均线追，已有仓等待实时资金和卖点"
    elif current < current_ma20 < current_ma60 and ma20_slope_5d < 0:
        route, route_cn, adjustment = "WEAK_MA_STRUCTURE", "均线弱结构", -4
        reason = "价格位于下行MA20/MA60下方；只有底背离与资金修复可重新升级"
    else:
        route, route_cn, adjustment = "NEUTRAL", "均线中性等待", 0
        reason = "均线结构没有形成独立计划；以盘中实时板块和资金行为为主"

    return {
        "version": PREMARKET_MA_PLAN_VERSION,
        "status": "READY",
        "asof": str(data.iloc[-1].get("eob") or "")[:19],
        "route": route,
        "route_cn": route_cn,
        "rank_adjustment": int(max(-4, min(4, adjustment))),
        "supports_watch": adjustment > 0,
        # 盘前均线永不直接禁止盘中事件，负分只改变准备优先级。
        "blocks_entry": False,
        "reason": reason,
        "ma5": round(current_ma5, 4),
        "ma10": round(current_ma10, 4),
        "ma20": round(current_ma20, 4),
        "ma60": round(current_ma60, 4),
        "ma20_slope_5d": round(ma20_slope_5d, 6),
        "extension_ma20": round(extension_ma20, 6),
        "extension_cap": round(extension_cap, 6),
        "distance_to_prior_20d_high": round(breakout_distance, 6),
        "atr14_pct": round(atr_pct, 6),
        "bull_stack": bool(bull_stack),
        "compressed_8d": bool(compressed),
        "decision_role": "LOW_WEIGHT_PRIOR_NOT_ENTRY_TRIGGER",
        "live_override_allowed": True,
        "no_lookahead": True,
    }
