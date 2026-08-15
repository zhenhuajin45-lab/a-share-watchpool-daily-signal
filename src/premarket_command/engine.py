"""Deterministic, strategy-neutral A-share premarket command engine.

The engine intentionally does not know about stock pools or broker orders. It
combines normalized market evidence into a market regime, position ceiling,
sector-rotation view, and opening disciplines that another strategy may consume.
"""

from __future__ import annotations

import datetime as dt
import math
from typing import Any


COMPONENT_WEIGHTS = {
    "market_breadth": 0.22,
    "limit_structure": 0.14,
    "profit_effect": 0.17,
    "turnover": 0.10,
    "kaipanla_strength": 0.12,
    "index_resonance": 0.13,
    "mainline_cycle": 0.12,
}

STAGE_SCORES = {
    "STARTUP": 0.60,
    "ACCELERATION": 0.85,
    "CLIMAX": 0.25,
    "DIVERGENCE": -0.25,
    "RETREAT": -0.85,
    "FADE": -0.85,
}

INDEX_TREND_SCORES = {
    "BULLISH_ALIGNMENT": 0.75,
    "ABOVE_MA20": 0.40,
    "MIXED": 0.0,
    "BELOW_MA20": -0.75,
}

STAGE_PRIORITY = {
    "ACCELERATION": 0,
    "STARTUP": 1,
    "CLIMAX": 2,
    "DIVERGENCE": 3,
    "RETREAT": 4,
    "FADE": 4,
}

VERIFIED_AUTHOR_STATES = {
    "ARTICLE_TEXT_VERIFIED",
    "ARTICLE_IMAGE_VERIFIED",
    "CROSS_SOURCE_VERIFIED",
    "USER_CONFIRMED",
}


def number(value: Any, default: float | None = None) -> float | None:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return default
    return parsed if math.isfinite(parsed) else default


def clamp(value: float, low: float = 0.0, high: float = 100.0) -> float:
    return max(low, min(value, high))


def _valid_trade_date(value: Any) -> str | None:
    text = str(value or "").replace("-", "")
    if len(text) != 8 or not text.isdigit():
        return None
    try:
        dt.datetime.strptime(text, "%Y%m%d")
    except ValueError:
        return None
    return text


def _emotion_assessment(sentiment: dict[str, Any]) -> dict[str, Any]:
    strength = number(sentiment.get("composite_strength"), 50.0) or 50.0
    breadth = sentiment.get("breadth") if isinstance(sentiment.get("breadth"), dict) else {}
    rise = number(breadth.get("rise_count"), 0.0) or 0.0
    fall = number(breadth.get("fall_count"), 0.0) or 0.0
    breadth_score = 50.0 if rise + fall <= 0 else rise / (rise + fall) * 100.0

    limits = sentiment.get("limit_structure") if isinstance(sentiment.get("limit_structure"), dict) else {}
    limit_up = number(limits.get("limit_up_count"), 0.0) or 0.0
    limit_down = number(limits.get("limit_down_count"), 0.0) or 0.0
    limit_score = (
        50.0
        if limit_up + limit_down <= 0
        else clamp(50.0 + (limit_up - limit_down) / max(limit_up + limit_down, 1.0) * 45.0)
    )
    profit_samples = [
        number(limits.get("yesterday_limit_up_return_pct")),
        number(limits.get("yesterday_chain_return_pct")),
        number(limits.get("yesterday_break_return_pct")),
    ]
    available_profit = [value for value in profit_samples if value is not None]
    average_profit = sum(available_profit) / len(available_profit) if available_profit else 0.0
    profit_score = clamp(50.0 + average_profit * 15.0)

    turnover = sentiment.get("turnover") if isinstance(sentiment.get("turnover"), dict) else {}
    volume_change = number(turnover.get("change_pct"), 0.0) or 0.0
    volume_score = clamp(50.0 + volume_change * 1.5)
    score = round(
        strength * 0.35
        + breadth_score * 0.20
        + limit_score * 0.15
        + profit_score * 0.20
        + volume_score * 0.10,
        1,
    )

    if score < 30:
        state, label, cap = "FREEZE", "冰点/防守", 20
    elif score < 40:
        state, label, cap = "WEAK", "弱势", 35
    elif score < 55:
        state, label, cap = "DIVERGENT_WEAK", "分化偏弱", 50
    elif score < 70:
        state, label, cap = "RECOVERY", "修复/可选强", 65
    elif score < 85:
        state, label, cap = "STRONG", "强势", 80
    else:
        state, label, cap = "OVERHEATED", "亢奋/防追高", 90

    warnings: list[str] = []
    if rise and fall and rise < fall:
        warnings.append("下跌家数多于上涨家数，结构强势不能替代全市场广度。")
    if volume_change <= -12:
        warnings.append("全市场明显缩量，仓位不能仅按涨停数量上调。")
    if available_profit and average_profit < 0:
        warnings.append("昨日强势样本赚钱效应为负，接力环境偏弱。")
    return {
        "state": state,
        "label": label,
        "score": score,
        "base_position_cap_pct": cap,
        "components": {
            "kaipanla_composite_strength": round(strength, 2),
            "breadth_score": round(breadth_score, 2),
            "limit_structure_score": round(limit_score, 2),
            "profit_effect_score": round(profit_score, 2),
            "volume_score": round(volume_score, 2),
        },
        "warnings": warnings,
    }


def index_technical_from_bars(
    name: str,
    symbol: str,
    bars: list[dict[str, Any]],
) -> dict[str, Any]:
    """Calculate reproducible daily index facts from GM-normalized bars."""

    clean: list[dict[str, float | str]] = []
    for row in bars:
        close = number(row.get("close"))
        volume = number(row.get("volume"))
        if close is None or close <= 0 or volume is None or volume < 0:
            continue
        clean.append({"date": str(row.get("date") or row.get("eob") or "")[:10].replace("-", ""), "close": close, "volume": volume})
    if not clean:
        return {"name": name, "symbol": symbol, "status": "UNAVAILABLE"}

    closes = [float(item["close"]) for item in clean]
    volumes = [float(item["volume"]) for item in clean]

    def ma(values: list[float], window: int) -> float | None:
        return sum(values[-window:]) / window if len(values) >= window else None

    close = closes[-1]
    ma5, ma10, ma20 = ma(closes, 5), ma(closes, 10), ma(closes, 20)
    ret5 = (close / closes[-6] - 1.0) * 100.0 if len(closes) >= 6 and closes[-6] else None
    ret20 = (close / closes[-21] - 1.0) * 100.0 if len(closes) >= 21 and closes[-21] else None
    avg_volume5 = sum(volumes[-6:-1]) / 5.0 if len(volumes) >= 6 else 0.0
    volume_ratio = volumes[-1] / avg_volume5 if avg_volume5 > 0 else None
    if ma5 and ma10 and ma20 and close > ma5 > ma10 > ma20:
        trend, label = "BULLISH_ALIGNMENT", "多头排列"
    elif ma20 and ma5 and close >= ma20 and close >= ma5:
        trend, label = "ABOVE_MA20", "趋势偏强"
    elif ma20 and ma5 and close < ma20 and close < ma5:
        trend, label = "BELOW_MA20", "趋势偏弱"
    else:
        trend, label = "MIXED", "震荡分化"
    return {
        "name": name,
        "symbol": symbol,
        "status": "OK",
        "data_date": clean[-1]["date"],
        "close": round(close, 3),
        "ma5": round(ma5, 3) if ma5 else None,
        "ma10": round(ma10, 3) if ma10 else None,
        "ma20": round(ma20, 3) if ma20 else None,
        "return_5d_pct": round(ret5, 3) if ret5 is not None else None,
        "return_20d_pct": round(ret20, 3) if ret20 is not None else None,
        "volume_ratio_5d": round(volume_ratio, 3) if volume_ratio is not None else None,
        "trend": trend,
        "trend_label": label,
    }


def _clip_ratio(value: float) -> float:
    return max(0.25, min(value, 4.0))


def _component(name: str, ratio: float, evidence: dict[str, Any]) -> dict[str, Any]:
    clipped = _clip_ratio(ratio)
    return {
        "name": name,
        "ratio": round(clipped, 4),
        "raw_ratio": round(ratio, 4),
        "weight": COMPONENT_WEIGHTS[name],
        "bias": "LONG" if clipped >= 1.08 else "SHORT" if clipped <= 0.92 else "NEUTRAL",
        "evidence": evidence,
    }


def _swr_components(
    sentiment: dict[str, Any],
    indices: list[dict[str, Any]],
    sector_cycle: dict[str, Any],
) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    breadth = sentiment.get("breadth") if isinstance(sentiment.get("breadth"), dict) else {}
    rise, fall = number(breadth.get("rise_count")), number(breadth.get("fall_count"))
    if rise is not None and fall is not None and rise + fall > 0:
        result.append(_component("market_breadth", (rise + 100.0) / (fall + 100.0), {"rise_count": int(rise), "fall_count": int(fall)}))

    limits = sentiment.get("limit_structure") if isinstance(sentiment.get("limit_structure"), dict) else {}
    limit_up, limit_down = number(limits.get("limit_up_count")), number(limits.get("limit_down_count"))
    if limit_up is not None and limit_down is not None and limit_up + limit_down > 0:
        result.append(_component("limit_structure", math.sqrt((limit_up + 5.0) / (limit_down + 5.0)), {"limit_up_count": int(limit_up), "limit_down_count": int(limit_down)}))

    returns = [
        number(limits.get("yesterday_limit_up_return_pct")),
        number(limits.get("yesterday_chain_return_pct")),
        number(limits.get("yesterday_break_return_pct")),
    ]
    available_returns = [value for value in returns if value is not None]
    if available_returns:
        average = sum(available_returns) / len(available_returns)
        result.append(_component("profit_effect", math.exp(max(-4.0, min(average, 4.0)) / 3.0), {"average_return_pct": round(average, 4), "samples": available_returns}))

    turnover = sentiment.get("turnover") if isinstance(sentiment.get("turnover"), dict) else {}
    turnover_change = number(turnover.get("change_pct"))
    if turnover_change is not None:
        result.append(_component("turnover", math.exp(max(-30.0, min(turnover_change, 30.0)) / 25.0), {"change_pct": round(turnover_change, 4)}))

    strength = number(sentiment.get("composite_strength"))
    if strength is not None:
        result.append(_component("kaipanla_strength", math.exp((clamp(strength) - 50.0) / 18.0), {"composite_strength": round(strength, 2)}))

    index_rows = [item for item in indices if isinstance(item, dict) and item.get("status") == "OK"]
    trend_scores = [INDEX_TREND_SCORES.get(str(item.get("trend") or "")) for item in index_rows]
    available_scores = [value for value in trend_scores if value is not None]
    if available_scores:
        average = sum(available_scores) / len(available_scores)
        result.append(_component("index_resonance", math.exp(average * 0.70), {"average_trend_score": round(average, 4), "trends": [{"name": item.get("name"), "trend": item.get("trend")} for item in index_rows]}))

    cycle_rows = sector_cycle.get("sectors") if isinstance(sector_cycle.get("sectors"), list) else []
    weighted_score = 0.0
    total_weight = 0.0
    evidence: list[dict[str, Any]] = []
    for item in cycle_rows:
        if not isinstance(item, dict):
            continue
        stage = str(item.get("stage") or "").upper()
        stage_score = STAGE_SCORES.get(stage)
        rank = number(item.get("current_rank"))
        if stage_score is None or rank is None or rank <= 0 or rank > 12:
            continue
        rank_weight = 1.0 / math.sqrt(rank)
        weighted_score += stage_score * rank_weight
        total_weight += rank_weight
        evidence.append({"sector_name": item.get("sector_name"), "stage": stage, "rank": int(rank)})
    if total_weight > 0:
        average = weighted_score / total_weight
        result.append(_component("mainline_cycle", math.exp(average * 0.70), {"average_stage_score": round(average, 4), "top_sectors": evidence[:12]}))
    return result


def _swr_state(ratio: float) -> tuple[str, str, list[int]]:
    if ratio < 0.60:
        return "RISK_OFF", "空方占优/防守", [0, 20]
    if ratio < 0.90:
        return "REPAIR", "弱修复/等待确认", [20, 50]
    if ratio < 1.15:
        return "BALANCED", "多空均衡/结构性可做", [35, 60]
    if ratio < 2.00:
        return "HEALTHY_RISK_ON", "多方占优/健康区", [35, 65]
    if ratio < 4.00:
        return "OVERHEAT_OR_BREAKOUT", "高热度/辨别趋势突破", [35, 80]
    return "EXTREME_CROWDING", "极端拥挤/防高潮回撤", [20, 65]


def _build_swr(sentiment: dict[str, Any], indices: list[dict[str, Any]], sector_cycle: dict[str, Any]) -> dict[str, Any]:
    components = _swr_components(sentiment, indices, sector_cycle)
    available_weight = sum(float(item["weight"]) for item in components)
    if available_weight <= 0:
        return {"status": "UNAVAILABLE", "confidence": 0.0, "components": []}
    log_ratio = sum(float(item["weight"]) * math.log(float(item["ratio"])) for item in components)
    ratio = math.exp(log_ratio / available_weight)
    state, label, position_range = _swr_state(ratio)
    warnings: list[str] = []
    by_name = {str(item["name"]): item for item in components}
    if by_name.get("market_breadth", {}).get("bias") == "SHORT":
        warnings.append("全市场广度偏空，结构性主线不能等价为全面风险偏好。")
    if by_name.get("profit_effect", {}).get("bias") == "SHORT":
        warnings.append("强势股隔日反馈偏弱，追涨与接力需要降档。")
    if by_name.get("turnover", {}).get("bias") == "SHORT":
        warnings.append("量能负贡献，扩仓应等待缩量幅度收窄。")
    return {
        "status": "OK" if available_weight >= 0.75 else "PARTIAL",
        "metric_name": "SWR内部市场合力",
        "ratio": round(ratio, 4),
        "market_tolerance_score": round(ratio / (1.0 + ratio) * 100.0, 1),
        "state": state,
        "label": label,
        "position_range_pct": position_range,
        "confidence": round(available_weight, 2),
        "components": components,
        "warnings": warnings,
        "conditional_expansion": {
            "eligible": state in {"BALANCED", "HEALTHY_RISK_ON"} and available_weight >= 0.75,
            "bonus_pct": 10,
            "requirements": {
                "min_full_market_up_ratio": 0.50,
                "min_primary_sector_up_ratio": 0.60,
                "min_turnover_change_pct": -8.0,
                "require_primary_sector_above_vwap": True,
                "max_external_shock_level": "MEDIUM",
            },
        },
    }


def _author_view(reference: dict[str, Any]) -> dict[str, Any]:
    rows = reference.get("observations") if isinstance(reference.get("observations"), list) else []
    by_date: dict[str, dict[str, Any]] = {}
    for item in rows:
        if not isinstance(item, dict):
            continue
        trade_date = str(item.get("trade_date") or "").replace("-", "")
        ratio = number(item.get("ratio"))
        verification = str(item.get("verification") or "").upper()
        if len(trade_date) == 8 and ratio is not None and ratio >= 0 and verification in VERIFIED_AUTHOR_STATES:
            by_date[trade_date] = {"trade_date": trade_date, "ratio": round(ratio, 4), "verification": verification, "source_url": item.get("source_url"), "evidence_path": item.get("evidence_path")}
    observations = [by_date[key] for key in sorted(by_date)]
    current = observations[-1] if observations else {}
    previous = observations[-2] if len(observations) >= 2 else {}
    current_ratio = number(current.get("ratio"))
    previous_ratio = number(previous.get("ratio"))
    change = current_ratio - previous_ratio if current_ratio is not None and previous_ratio is not None else None
    change_pct = change / previous_ratio * 100.0 if change is not None and previous_ratio else None
    thresholds = reference.get("thresholds") if isinstance(reference.get("thresholds"), dict) else {}
    negative_line = number(thresholds.get("negative_effect"), 0.6) or 0.6
    balance_line = number(thresholds.get("balance"), 1.0) or 1.0
    top1 = number(thresholds.get("stage_top_watch_1"), 1.5) or 1.5
    top2 = number(thresholds.get("stage_top_watch_2"), 2.0) or 2.0
    if current_ratio is None:
        state, label, ceiling = "UNAVAILABLE", "作者序列缺失", None
    elif current_ratio < negative_line:
        state, label, ceiling = "BELOW_NEGATIVE_LINE", "跌破0.6/负面效应区", 20
    elif current_ratio < balance_line:
        state, label, ceiling = "REPAIR_BELOW_1", "修复区/尚未回到1", 35
    elif current_ratio < top1:
        state, label, ceiling = "POSITIVE_MIDDLE", "多方中位区", 65
    elif current_ratio <= top2:
        state, label, ceiling = "UPPER_WATCH_ZONE", "1.5-2阶段顶部观察区", 65
    else:
        state, label, ceiling = "ABOVE_2_CONTEXT_REQUIRED", "高于2/辨别趋势或阶段顶", 65
    consecutive_declines = 0
    for left, right in zip(observations, observations[1:]):
        consecutive_declines = consecutive_declines + 1 if float(right["ratio"]) < float(left["ratio"]) else 0
    momentum = (
        "FAST_COOLING"
        if consecutive_declines >= 2 and change_pct is not None and change_pct <= -20
        else "COOLING"
        if change is not None and change < 0
        else "RISING"
        if change is not None and change > 0
        else "FLAT_OR_UNKNOWN"
    )
    return {
        "available": current_ratio is not None,
        "ratio": current_ratio,
        "source_trade_date": current.get("trade_date") or reference.get("source_trade_date"),
        "previous_ratio": previous_ratio,
        "previous_trade_date": previous.get("trade_date") or reference.get("previous_trade_date"),
        "change": round(change, 4) if change is not None else None,
        "change_pct": round(change_pct, 2) if change_pct is not None else None,
        "consecutive_declines": consecutive_declines,
        "momentum": momentum,
        "state": state,
        "label": label,
        "position_ceiling_pct": ceiling,
        "thresholds": {"bottom_watch": 0.3, "negative_effect": negative_line, "balance": balance_line, "stage_top_watch_1": top1, "stage_top_watch_2": top2},
        "observations": observations,
        "calibration_only": bool(reference.get("calibration_only", True)),
        "policy": "作者原值提供周期位置；不得与SWR求平均，复刻值未晋级前不得参与门控。",
    }


def _external_view(external: dict[str, Any]) -> dict[str, Any]:
    gate = external.get("external_tech_shock") if isinstance(external.get("external_tech_shock"), dict) else {}
    return {
        "status": external.get("status") or "UNAVAILABLE",
        "source_quality": external.get("source_quality") or "UNAVAILABLE",
        "level": str(gate.get("level") or external.get("level") or "UNKNOWN").upper(),
        "score": number(gate.get("score"), number(external.get("score"), 0.0)),
        "entry_policy": gate.get("entry_policy") or external.get("entry_policy") or "unknown",
        "position_cap_pct": number(gate.get("position_cap_pct"), number(external.get("position_cap_pct"))),
        "reasons": gate.get("reasons") or external.get("reasons") or [],
        "markets": external.get("markets") or external.get("quotes") or [],
        "fresh_for_execution": external.get("fresh_for_execution") is True,
    }


def _sector_score(item: dict[str, Any]) -> float:
    stage = str(item.get("stage") or "").upper()
    rank = number(item.get("current_rank"), 99.0) or 99.0
    validation = number(item.get("validation_score"), number(item.get("score"), 0.0)) or 0.0
    current_net = number(item.get("current_main_net_yi"), 0.0) or 0.0
    interval_net = number(item.get("interval_net_yi"), 0.0) or 0.0
    inflow_days = number(item.get("net_inflow_days"), 0.0) or 0.0
    stage_bonus = {"ACCELERATION": 24, "STARTUP": 20, "CLIMAX": 4, "DIVERGENCE": -12, "RETREAT": -28, "FADE": -28}.get(stage, -20)
    rank_score = clamp(26.0 - min(rank, 26.0), 0.0, 25.0)
    flow_score = clamp(current_net, -10.0, 10.0) + clamp(interval_net / 3.0, -10.0, 10.0) + min(inflow_days, 5.0) * 2.0
    return round(clamp(validation * 0.35 + rank_score + stage_bonus + flow_score, 0.0, 100.0), 1)


def _sector_rotation(sector_cycle: dict[str, Any], topics: dict[str, Any], limit: int = 8) -> dict[str, Any]:
    rows = sector_cycle.get("sectors") if isinstance(sector_cycle.get("sectors"), list) else []
    headlines = [str(item.get("headline") if isinstance(item, dict) else item) for item in topics.get("headlines", [])]
    risk_headlines = topics.get("risk_headlines") if isinstance(topics.get("risk_headlines"), list) else []
    ranked: list[dict[str, Any]] = []
    for item in rows:
        if not isinstance(item, dict):
            continue
        stage = str(item.get("stage") or "").upper()
        if stage not in STAGE_PRIORITY:
            continue
        name = str(item.get("sector_name") or "")
        confirmations = [headline for headline in headlines if name and name in headline]
        score = _sector_score(item)
        permission = "PRIMARY_ATTACK" if stage in {"STARTUP", "ACCELERATION"} and score >= 60 else "RECONFIRM_ONLY"
        if stage in {"CLIMAX", "DIVERGENCE", "RETREAT", "FADE"}:
            permission = "NO_NEW_ATTACK" if stage != "CLIMAX" else "RECONFIRM_ONLY"
        ranked.append({
            "sector_code": item.get("sector_code"),
            "sector_name": name,
            "stage": stage,
            "cycle_day": item.get("cycle_day"),
            "current_rank": item.get("current_rank"),
            "validation_state": item.get("validation_state"),
            "validation_score": item.get("validation_score"),
            "command_score": score,
            "current_main_net_yi": item.get("current_main_net_yi"),
            "interval_return_pct": item.get("interval_return_pct"),
            "interval_net_yi": item.get("interval_net_yi"),
            "net_inflow_days": item.get("net_inflow_days"),
            "intraday_rhythm": item.get("intraday_rhythm"),
            "topic_confirmations": confirmations,
            "permission": permission,
            "discipline": "只确认方向与生命周期；具体策略仍需自身信号和执行门控。",
        })
    ranked.sort(key=lambda row: (row["permission"] != "PRIMARY_ATTACK", STAGE_PRIORITY.get(str(row["stage"]), 9), -float(row["command_score"]), number(row.get("current_rank"), 99.0) or 99.0))
    primary = [item for item in ranked if item["permission"] == "PRIMARY_ATTACK"][:3]
    return {
        "primary_attack_sectors": primary,
        "rotation_watch": ranked[:limit],
        "risk_headlines": risk_headlines,
        "policy": "板块必须同时看生命周期、排名、强度、资金、区间持续性和题材证据，禁止仅凭涨跌幅定主线。",
    }


def build_premarket_command(payload: dict[str, Any]) -> dict[str, Any]:
    """Build the transferable premarket command contract."""

    sentiment = payload.get("market_sentiment") if isinstance(payload.get("market_sentiment"), dict) else {}
    raw_indices = payload.get("major_indices") if isinstance(payload.get("major_indices"), list) else []
    indices: list[dict[str, Any]] = []
    for item in raw_indices:
        if not isinstance(item, dict):
            continue
        if isinstance(item.get("bars"), list):
            indices.append(index_technical_from_bars(str(item.get("name") or ""), str(item.get("symbol") or ""), item["bars"]))
        else:
            indices.append(dict(item))
    sector_cycle = payload.get("sector_cycle") if isinstance(payload.get("sector_cycle"), dict) else {}
    author = _author_view(payload.get("author_ratio") if isinstance(payload.get("author_ratio"), dict) else {})
    external = _external_view(payload.get("external_market") if isinstance(payload.get("external_market"), dict) else {})
    topics = payload.get("topic_context") if isinstance(payload.get("topic_context"), dict) else {}

    emotion = _emotion_assessment(sentiment)
    swr = _build_swr(sentiment, indices, sector_cycle)
    rotation = _sector_rotation(sector_cycle, topics)
    cap = int(emotion["base_position_cap_pct"])
    weak_indices = [item.get("name") for item in indices if item.get("status") == "OK" and item.get("trend") == "BELOW_MA20"]
    if len(weak_indices) >= 2:
        cap = min(cap, 35)
    external_cap = number(external.get("position_cap_pct"))
    if external_cap is not None:
        cap = min(cap, int(external_cap))
    if external.get("level") == "HIGH":
        cap = min(cap, 35)
    elif external.get("level") == "EXTREME":
        cap = min(cap, 20)
    if swr.get("state") == "RISK_OFF":
        cap = min(cap, 20)
    elif swr.get("state") == "REPAIR":
        cap = min(cap, 50)
    elif swr.get("state") == "EXTREME_CROWDING":
        cap = min(cap, 65)
    author_ceiling = number(author.get("position_ceiling_pct"))
    if author_ceiling is not None:
        cap = min(cap, int(author_ceiling))

    expansion = swr.get("conditional_expansion") if isinstance(swr.get("conditional_expansion"), dict) else {}
    expansion_cap = cap
    if expansion.get("eligible") is True and external.get("level") not in {"HIGH", "EXTREME"} and len(weak_indices) < 2:
        ratio_range = swr.get("position_range_pct") if isinstance(swr.get("position_range_pct"), list) else []
        range_ceiling = int(number(ratio_range[-1], cap) or cap) if ratio_range else cap
        expansion_cap = min(range_ceiling, cap + int(number(expansion.get("bonus_pct"), 0) or 0))
        if author_ceiling is not None:
            expansion_cap = min(expansion_cap, int(author_ceiling))

    source_trade_date = _valid_trade_date(payload.get("source_trade_date"))
    execution_trade_date = _valid_trade_date(payload.get("execution_trade_date"))
    sentiment_date = _valid_trade_date(sentiment.get("trade_date"))
    sector_date = _valid_trade_date(sector_cycle.get("trade_date"))
    external_date = _valid_trade_date((payload.get("external_market") or {}).get("trade_date")) if isinstance(payload.get("external_market"), dict) else None
    topic_date = _valid_trade_date(topics.get("trade_date") or topics.get("source_trade_date"))
    author_observations = payload.get("author_ratio", {}).get("observations", []) if isinstance(payload.get("author_ratio"), dict) else []
    author_dates = [
        _valid_trade_date(item.get("trade_date"))
        for item in author_observations
        if isinstance(item, dict) and str(item.get("verification") or "").upper() in VERIFIED_AUTHOR_STATES
    ]
    latest_author_date = max((value for value in author_dates if value), default=None)
    index_dates = [
        _valid_trade_date(item.get("data_date") or ((item.get("bars") or [{}])[-1].get("date") if isinstance(item.get("bars"), list) and item.get("bars") else None))
        for item in indices
        if isinstance(item, dict) and item.get("status") == "OK"
    ]
    dated_index_count = sum(value == source_trade_date for value in index_dates)
    dates_valid = bool(source_trade_date and execution_trade_date and source_trade_date < execution_trade_date)
    fresh_sources = {
        "market_sentiment_date": sentiment_date == source_trade_date,
        "major_indices_date": dated_index_count >= 3,
        "author_ratio_date": latest_author_date == source_trade_date,
        "sector_cycle_date": sector_date == source_trade_date,
        "external_market_date": external_date == execution_trade_date,
        "topic_context_date": topic_date in {source_trade_date, execution_trade_date},
    }
    required_sources = {
        "market_sentiment": str(sentiment.get("status") or "").upper() in {"OK", "READY"},
        "major_indices": sum(1 for item in indices if item.get("status") == "OK") >= 3,
        "author_ratio": author.get("available") is True,
        "external_market": (
            str(external.get("status") or "").upper() in {"OK", "READY"}
            and str(external.get("source_quality") or "").lower() in {"verified_live", "two_source_verified", "cross_checked"}
            and external.get("level") not in {"UNKNOWN", "UNAVAILABLE", ""}
            and external.get("fresh_for_execution") is True
        ),
        "sector_cycle": str(sector_cycle.get("status") or "").upper() in {"OK", "READY"} and bool(sector_cycle.get("sectors")),
        "topic_context": str(topics.get("status") or "").upper() in {"OK", "READY"},
    }
    missing = [name for name, available in required_sources.items() if not available]
    stale = [name for name, fresh in fresh_sources.items() if not fresh]
    blockers = [*(f"missing:{name}" for name in missing), *(f"stale_or_undated:{name}" for name in stale)]
    if not dates_valid:
        blockers.append("invalid_source_or_execution_trade_date")
    source_publishable = not blockers
    status = "READY_FOR_DEEPSEEK_REVIEW" if source_publishable else "PARTIAL_EVIDENCE_REVIEW_REQUIRED"
    data_quality_cap = 100 if source_publishable else 0
    cap = min(cap, data_quality_cap)
    if not source_publishable:
        expansion_cap = cap
    return {
        "schema_version": "a_share_premarket_command_v1",
        "source_trade_date": str(payload.get("source_trade_date") or ""),
        "execution_trade_date": str(payload.get("execution_trade_date") or ""),
        "generated_at": dt.datetime.now().astimezone().replace(microsecond=0).isoformat(),
        "status": status,
        "release_status": "DRAFT_REVIEW_REQUIRED",
        "market_emotion": emotion,
        "major_indices": indices,
        "index_summary": {"available_count": sum(1 for item in indices if item.get("status") == "OK"), "weak_indices": weak_indices},
        "author_long_short_ratio": author,
        "internal_swr": swr,
        "external_resonance": external,
        "position_command": {
            "base_cap_pct": cap,
            "conditional_expansion_cap_pct": expansion_cap,
            "conditional_expansion_enabled": expansion_cap > cap,
            "expansion_requirements": expansion.get("requirements") or {},
            "decision_rule": "所有独立上限取最小值；条件扩张必须由开盘后确定性证据触发。",
            "data_quality_cap_pct": data_quality_cap,
        },
        "sector_rotation": rotation,
        "opening_change_triggers": [
            "09:20重新抓取外围、指数期货/竞价、开盘啦情绪和板块排名，计算与盘前快照的差量。",
            "任一主要指数由强转弱、外围冲击升至HIGH、主攻板块跌出前12或资金转负时收紧仓位。",
            "任何数据源缺失只降低置信度并报警，不自动视为空头，也不能据此放宽门控。",
        ],
        "premarket_disciplines": [
            "确定性计算先完成，DeepSeek只能复核、否决或收紧，不能提高仓位、补造数据或新增主攻板块。",
            "作者多空比与内部SWR尺度不同，禁止求平均；作者复刻值未晋级前只做误差研究。",
            "开盘啦与公众号是交叉证据，掘金SDK行情是指数和可复算市场事实的主数据源。",
            "主攻板块只授权方向，不向任何具体策略自动授予个股买入权限。",
        ],
        "source_health": {
            "required": required_sources,
            "freshness": fresh_sources,
            "source_trade_date": source_trade_date,
            "execution_trade_date": execution_trade_date,
            "latest_author_date": latest_author_date,
            "dated_index_count": dated_index_count,
            "missing": missing,
            "stale_or_undated": stale,
            "blockers": blockers,
            "publishable": source_publishable,
        },
        "policy": {
            "contains_stock_pool": False,
            "deepseek_required_for_publish": True,
            "deepseek_can_raise_position_cap": False,
            "deepseek_can_add_attack_sector": False,
            "missing_source_is_bearish": False,
            "external_source_can_grant_stock_entry": False,
        },
    }
