# coding: utf-8
"""A股新开仓的 T+1 生存能力评估。

该模块不预测明日涨跌，也不把单一阈值包装成胜率。它只回答一个更窄的问题：
当日买入后无法卖出的前提下，当前趋势质量、位置、证据新鲜度和资金承接，是否足以
支持把观察事件升级为正式新仓信号。

所有输入均来自当前时点或更早；评分只用于信号分级，系统仍不发送订单。
"""

from __future__ import annotations

import math
from typing import Any, Dict, Mapping, Optional


T1_SURVIVABILITY_VERSION = "t1_survivability_v1_ex_ante"


def _safe_float(value: Any, default: float = 0.0) -> float:
    try:
        result = float(value)
        return result if math.isfinite(result) else default
    except (TypeError, ValueError):
        return default


def _clip(value: float, low: float, high: float) -> float:
    return max(low, min(high, value))


def evaluate_t1_survivability(
    *,
    price: float,
    session_open: float,
    session_high: float,
    session_low: float,
    vwap: Optional[float],
    reference_close: Optional[float],
    atr14_pct: float,
    momentum_60s: float,
    momentum_180s: float,
    sector: Optional[Mapping[str, Any]] = None,
    continuation: Optional[Mapping[str, Any]] = None,
    capital_behavior: Optional[Mapping[str, Any]] = None,
    multitimeframe: Optional[Mapping[str, Any]] = None,
    route: str = "TREND_CONTINUATION",
    signal_age_minutes: Optional[float] = None,
    discovery_price: Optional[float] = None,
) -> Dict[str, Any]:
    """返回事前 T+1 分级、硬阻断项和可解释特征。

    A 级可以进入正式新仓门；B 级只表示结构接近，不直接建仓；C 级继续观察或等待
    重新筑基。硬阻断项不是永久否决，后续价格修复、板块修复或新一轮事件可以重算。
    """

    price = _safe_float(price)
    session_open = _safe_float(session_open, price)
    session_high = max(_safe_float(session_high, price), price)
    session_low = min(_safe_float(session_low, price), price)
    vwap_value = _safe_float(vwap)
    reference = _safe_float(reference_close)
    atr = _clip(_safe_float(atr14_pct, 0.05), 0.015, 0.15)
    sector = dict(sector or {})
    continuation = dict(continuation or {})
    capital = dict(capital_behavior or {})
    mtf = dict(multitimeframe or {})

    open_hold = price / session_open - 1.0 if session_open > 0 else None
    session_drawdown = (session_high - price) / session_high if session_high > 0 else 0.0
    session_recovery = (
        (price - session_low) / (session_high - session_low)
        if session_high > session_low else 1.0
    )
    vwap_gap = price / vwap_value - 1.0 if vwap_value > 0 else None
    intraday_return = price / reference - 1.0 if reference > 0 else None
    discovery_extension = (
        price / _safe_float(discovery_price) - 1.0
        if _safe_float(discovery_price) > 0 else None
    )
    age = None if signal_age_minutes is None else max(0.0, _safe_float(signal_age_minutes))

    blockers = []
    cautions = []
    evidence = []

    if price <= 0 or session_open <= 0 or session_high <= 0 or session_low <= 0:
        blockers.append("SESSION_PATH_UNAVAILABLE")
    if open_hold is not None and open_hold < -0.015 and session_recovery < 0.80:
        blockers.append("OPENING_WEAKNESS_NOT_REPAIRED")
    if session_drawdown > max(0.025, 0.45 * atr) and session_recovery < 0.65:
        blockers.append("DEEP_SESSION_DRAWDOWN_NOT_REPAIRED")
    if vwap_gap is not None and vwap_gap > 0.022:
        blockers.append("TOO_FAR_ABOVE_SESSION_COST")
    market_state = str(
        sector.get("market_board_state") or sector.get("rotation_state") or "UNAVAILABLE"
    )
    if market_state in {"ROTATION_OUT", "WEAK"}:
        blockers.append("MARKET_SECTOR_ROTATING_OUT")
    if str(capital.get("phase") or "") == "CONFIRMED_OUTFLOW":
        blockers.append("CAPITAL_OUTFLOW_CONFIRMED")
    if str(mtf.get("alignment") or "") == "BEARISH_2_OF_3" or int(
        _safe_float(mtf.get("bearish_count"))
    ) >= 2:
        blockers.append("MULTITIMEFRAME_BEARISH_CONFLICT")

    sudden_route = str(route).upper().startswith("SUDDEN")
    if sudden_route and age is not None and age > 60 and _safe_float(discovery_extension) > 0.015:
        blockers.append("DISCOVERY_STALE_AND_ALREADY_EXTENDED")
    elif age is not None and age > 150:
        blockers.append("SIGNAL_EVIDENCE_TOO_OLD")
    elif age is not None and age > 60:
        cautions.append("SIGNAL_FRESHNESS_DECAY")

    score = 50.0
    if open_hold is None:
        cautions.append("OPEN_PATH_UNAVAILABLE")
    elif open_hold >= 0:
        score += 8
        evidence.append("现价不弱于开盘价")
    elif open_hold >= -0.01:
        score += 3
    else:
        score -= 10
        cautions.append("BELOW_OPEN")

    if session_recovery >= 0.80:
        score += 12
        evidence.append("位于当日高低区间上部")
    elif session_recovery >= 0.65:
        score += 6
    else:
        score -= 10
        cautions.append("SESSION_RECOVERY_WEAK")

    if session_drawdown <= 0.01:
        score += 10
        evidence.append("距离当日高点仍近")
    elif session_drawdown <= 0.02:
        score += 4
    else:
        score -= 8
        cautions.append("SESSION_DRAWDOWN_LARGE")

    if vwap_gap is None:
        score -= 3
        cautions.append("VWAP_UNAVAILABLE")
    elif -0.003 <= vwap_gap <= 0.02:
        score += 10
        evidence.append("价格仍在可解释的VWAP承接区")
    elif vwap_gap < -0.003:
        score -= 8
        cautions.append("BELOW_VWAP")
    else:
        score -= 10

    if momentum_180s > 0:
        score += 6
    else:
        score -= 6
        cautions.append("MEDIUM_MOMENTUM_NOT_POSITIVE")
    score += 3 if momentum_60s > 0 else -3

    market_entry_support = bool(
        sector.get("market_board_entry_support") or sector.get("entry_support")
    )
    local_state = str(sector.get("state") or "UNAVAILABLE")
    if market_entry_support or market_state in {"SUSTAINED_LEADER", "HEALTHY_RISING"}:
        score += 8
        evidence.append("全市场板块仍提供承接")
    elif local_state in {"IGNITION", "EXPANSION", "HEALTHY_TREND"}:
        score += 4
    else:
        score -= 5
        cautions.append("SECTOR_SUPPORT_INSUFFICIENT")
    if sector.get("market_board_rotation_caution") or sector.get("rotation_caution"):
        score -= 4
        cautions.append("FAST_ROTATION_CAUTION")

    acceptance = _safe_float(continuation.get("acceptance_score"))
    if acceptance >= 78:
        score += 8
        evidence.append("盘中承接评分达到正式区")
    elif continuation and acceptance < 65:
        score -= 7
        cautions.append("ACCEPTANCE_SCORE_LOW")

    if capital.get("flow_persistence_confirmed"):
        score += 8
        evidence.append("资金流证据持续而非单个快照")
    elif capital.get("entry_support"):
        score += 4
    elif capital.get("status") == "READY":
        score -= 3
        cautions.append("CAPITAL_PERSISTENCE_NOT_CONFIRMED")

    alignment = str(mtf.get("alignment") or "")
    if alignment == "FULL_BULLISH":
        score += 6
    elif alignment == "BULLISH_2_OF_3":
        score += 4
    elif alignment == "MIXED":
        score += 1

    if intraday_return is not None:
        if intraday_return <= 0.03:
            score += 4
        elif intraday_return > max(0.08, 1.25 * atr):
            score -= 8
            cautions.append("DAILY_EXTENSION_REQUIRES_REBASE")

    if age is not None:
        if age <= 30:
            score += 4
        elif age > 60:
            score -= min(15.0, 5.0 + (age - 60.0) / 18.0)

    score = int(round(_clip(score, 0.0, 100.0)))
    blockers = list(dict.fromkeys(blockers))
    cautions = list(dict.fromkeys(cautions))
    if blockers:
        grade = "C"
    elif score >= 78:
        grade = "A"
    elif score >= 68:
        grade = "B"
    else:
        grade = "C"
    reason_cn = {
        "SESSION_PATH_UNAVAILABLE": "日内路径数据不可用",
        "OPENING_WEAKNESS_NOT_REPAIRED": "相对开盘弱势尚未完成修复",
        "DEEP_SESSION_DRAWDOWN_NOT_REPAIRED": "当日深回撤尚未收回",
        "TOO_FAR_ABOVE_SESSION_COST": "价格距离当日成本中枢过远",
        "MARKET_SECTOR_ROTATING_OUT": "全市场板块正在轮出/走弱",
        "CAPITAL_OUTFLOW_CONFIRMED": "持续资金流出已经确认",
        "MULTITIMEFRAME_BEARISH_CONFLICT": "至少两个分钟周期转弱",
        "DISCOVERY_STALE_AND_ALREADY_EXTENDED": "首次发现已过时且价格继续扩张",
        "SIGNAL_EVIDENCE_TOO_OLD": "本轮入场证据已经过时",
    }

    return {
        "score": score,
        "grade": grade,
        "formal_entry_allowed": bool(grade == "A" and not blockers),
        "blockers": blockers,
        "blockers_cn": [reason_cn.get(value, value) for value in blockers],
        "cautions": cautions,
        "evidence": evidence,
        "features": {
            "open_hold": open_hold,
            "session_drawdown": session_drawdown,
            "session_recovery_ratio": session_recovery,
            "vwap_gap": vwap_gap,
            "intraday_return": intraday_return,
            "signal_age_minutes": age,
            "discovery_extension": discovery_extension,
            "atr14_pct": atr,
        },
        "meaning": "A=可进入正式新仓门；B=条件接近但只观察；C=等待修复或重新筑基",
        "no_lookahead": True,
        "rules_version": T1_SURVIVABILITY_VERSION,
    }
