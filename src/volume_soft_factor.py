# coding: utf-8
"""日线量能辅助模块：底部进攻确认 + 顶部退量防守。

只读取决策截面及更早的完整日线。两类职责严格隔离：
1. “两日红K站上五日均量 / 三日温和放量”只在底部或底部启动区加分；
2. 高位持续缩量或突然大幅退量只做风险提示、禁止新开仓和持仓保护监控。

量能永远不能独立生成买卖点；顶部风险也必须等待盘中资金走弱确认后才允许减仓。
"""

from __future__ import annotations

import math
from dataclasses import asdict, dataclass
from typing import Any, Dict

import pandas as pd


@dataclass(frozen=True)
class VolumeSoftFactorConfig:
    ma_window: int = 5
    context_window: int = 60
    mild_daily_ratio_low: float = 1.03
    mild_daily_ratio_high: float = 1.35
    mild_total_ratio_low: float = 1.08
    mild_total_ratio_high: float = 1.80
    bottom_range_position_max: float = 0.48
    bottom_return20_max: float = 0.12
    bottom_ma60_extension_max: float = 1.08
    top_range_position_min: float = 0.78
    top_return20_min: float = 0.12
    top_ma20_extension_min: float = 1.06
    persistent_contraction_days: int = 4
    persistent_latest_to_ma5_max: float = 0.82
    cliff_latest_to_previous_max: float = 0.62
    cliff_latest_to_prior_mean_max: float = 0.65
    bonus_two_red_ma5: int = 5
    bonus_mild_3d: int = 4
    bonus_both: int = 7
    penalty_top_contraction: int = 10
    penalty_top_cliff: int = 8
    penalty_top_both: int = 14


DEFAULT_VOLUME_CONFIG = VolumeSoftFactorConfig()
VALID_EFFECT_MODES = {"SHADOW", "RANKING", "ELIGIBLE_STRENGTH"}


def normalize_effect_mode(value: Any) -> str:
    mode = str(value or "SHADOW").strip().upper()
    return mode if mode in VALID_EFFECT_MODES else "SHADOW"


def _safe_float(value: Any, default: float = 0.0) -> float:
    try:
        number = float(value)
        return number if math.isfinite(number) else default
    except (TypeError, ValueError):
        return default


def _date_key(value: Any) -> str:
    return str(value)[:10] if value is not None else ""


def unavailable_result(reason: str, config: VolumeSoftFactorConfig = DEFAULT_VOLUME_CONFIG) -> Dict[str, Any]:
    return {
        "status": "UNAVAILABLE",
        "active": False,
        "offensive_status": "UNAVAILABLE",
        "offensive_active": False,
        "bottom_context": False,
        "bottom_startup": False,
        "top_context": False,
        "top_persistent_contraction": False,
        "top_volume_cliff": False,
        "risk_level": "UNKNOWN",
        "defense_penalty": 0,
        "blocks_new_entry": False,
        "condition_a": False,
        "condition_b_raw": False,
        "condition_b_quality": False,
        "volume_divergence": False,
        "raw_bonus": 0,
        "raw_offensive_bonus": 0,
        "net_effect": 0,
        "reason": reason,
        "risk_reason": "量能数据不可用",
        "role_scope": "UNAVAILABLE",
        "uses_current_unfinished_bar": False,
        "config": asdict(config),
        "audit": {},
    }


def evaluate_volume_soft_factor(
    frame: pd.DataFrame,
    config: VolumeSoftFactorConfig = DEFAULT_VOLUME_CONFIG,
) -> Dict[str, Any]:
    """评估底部量能进攻条件和顶部退量风险，返回完整审计事实。"""
    required = {"eob", "open", "high", "low", "close", "volume"}
    if frame is None or not required.issubset(frame.columns):
        return unavailable_result("缺少日线OHLCV字段", config)

    data = frame.loc[:, sorted(required)].copy()
    data["eob"] = pd.to_datetime(data["eob"], errors="coerce")
    for column in ("open", "high", "low", "close", "volume"):
        data[column] = pd.to_numeric(data[column], errors="coerce")
    data = (
        data.dropna(subset=list(required))
        .sort_values("eob")
        .drop_duplicates("eob", keep="last")
        .reset_index(drop=True)
    )
    minimum = max(config.context_window, config.ma_window + 1, config.persistent_contraction_days)
    if len(data) < minimum:
        return unavailable_result(f"完整日线不足{minimum}根，无法判断底部/顶部位置", config)
    if (data["volume"].tail(minimum) <= 0).any():
        return unavailable_result("近期成交量存在零值或负值", config)

    data["volume_ma5"] = data["volume"].rolling(config.ma_window, min_periods=config.ma_window).mean()
    data["close_ma20"] = data["close"].rolling(20, min_periods=20).mean()
    data["close_ma60"] = data["close"].rolling(config.context_window, min_periods=config.context_window).mean()
    first, second, third = data.iloc[-3], data.iloc[-2], data.iloc[-1]
    second_ma = _safe_float(second["volume_ma5"], float("nan"))
    third_ma = _safe_float(third["volume_ma5"], float("nan"))
    if not math.isfinite(second_ma) or not math.isfinite(third_ma) or second_ma <= 0 or third_ma <= 0:
        return unavailable_result("五日均量不可用", config)

    second_red = bool(second["close"] > second["open"])
    third_red = bool(third["close"] > third["open"])
    second_vs_ma = _safe_float(second["volume"] / second_ma)
    third_vs_ma = _safe_float(third["volume"] / third_ma)
    condition_a = bool(second_red and third_red and second_vs_ma > 1.0 and third_vs_ma > 1.0)

    ratio_1 = _safe_float(second["volume"] / first["volume"])
    ratio_2 = _safe_float(third["volume"] / second["volume"])
    total_ratio = _safe_float(third["volume"] / first["volume"])
    condition_b_raw = bool(
        config.mild_daily_ratio_low <= ratio_1 <= config.mild_daily_ratio_high
        and config.mild_daily_ratio_low <= ratio_2 <= config.mild_daily_ratio_high
        and config.mild_total_ratio_low <= total_ratio <= config.mild_total_ratio_high
    )
    red_count = int(first["close"] > first["open"]) + int(second_red) + int(third_red)
    three_day_price_change = _safe_float(third["close"] / first["close"] - 1.0)
    condition_b_quality = bool(condition_b_raw and (red_count >= 2 or third["close"] > first["close"]))
    volume_divergence = bool(condition_b_raw and not condition_b_quality)

    recent = data.tail(config.context_window)
    range_low = _safe_float(recent["low"].min())
    range_high = _safe_float(recent["high"].max())
    close = _safe_float(third["close"])
    range_position = (close - range_low) / (range_high - range_low) if range_high > range_low else 0.5
    close_ma20 = _safe_float(third["close_ma20"], close)
    close_ma60 = _safe_float(third["close_ma60"], close)
    return20 = _safe_float(close / data.iloc[-21]["close"] - 1.0) if len(data) >= 21 else 0.0
    drawdown_from_high = _safe_float(close / range_high - 1.0) if range_high > 0 else 0.0
    bottom_context = bool(
        range_position <= config.bottom_range_position_max
        and return20 <= config.bottom_return20_max
        and close <= close_ma60 * config.bottom_ma60_extension_max
    )
    top_context = bool(
        range_position >= config.top_range_position_min
        and (return20 >= config.top_return20_min or close >= close_ma20 * config.top_ma20_extension_min)
    )

    raw_offensive_active = bool(condition_a or condition_b_quality)
    bottom_startup = bool(bottom_context and raw_offensive_active and close >= close_ma20 * 0.96 and third_red)
    if condition_a and condition_b_quality:
        raw_status, raw_offensive_bonus = "BOTH", config.bonus_both
        raw_reason = "连续2日红K站上5日均量，且连续3日温和放量"
    elif condition_a:
        raw_status, raw_offensive_bonus = "TWO_RED_MA5", config.bonus_two_red_ma5
        raw_reason = "连续2日红K且成交量分别站上5日均量"
    elif condition_b_quality:
        raw_status, raw_offensive_bonus = "MILD_3D", config.bonus_mild_3d
        raw_reason = "连续3日温和放量，价格质量合格"
    elif volume_divergence:
        raw_status, raw_offensive_bonus = "VOLUME_DIVERGENCE_ONLY", 0
        raw_reason = "连续3日放量但价格质量不合格，疑似分歧或派发"
    else:
        raw_status, raw_offensive_bonus = "NONE", 0
        raw_reason = "两个底部量能确认条件均未满足"

    contraction = data["volume"].tail(config.persistent_contraction_days).astype(float).tolist()
    descending_steps = sum(curr < prev for prev, curr in zip(contraction, contraction[1:]))
    latest_to_ma5 = _safe_float(third["volume"] / third_ma)
    persistent_contraction = bool(
        top_context
        and descending_steps == max(len(contraction) - 1, 0)
        and latest_to_ma5 <= config.persistent_latest_to_ma5_max
    )
    prior_mean = _safe_float(data["volume"].iloc[-6:-1].mean())
    latest_to_previous = _safe_float(third["volume"] / second["volume"])
    latest_to_prior_mean = _safe_float(third["volume"] / prior_mean) if prior_mean > 0 else 1.0
    volume_cliff = bool(
        top_context
        and (
            latest_to_previous <= config.cliff_latest_to_previous_max
            or latest_to_prior_mean <= config.cliff_latest_to_prior_mean_max
        )
    )

    if persistent_contraction and volume_cliff:
        risk_level, defense_penalty = "EXTREME", config.penalty_top_both
        risk_reason = "高位连续缩量并出现断崖式退量；新仓回避，持仓等待盘中资金走弱确认"
    elif persistent_contraction:
        risk_level, defense_penalty = "EXTREME", config.penalty_top_contraction
        risk_reason = "高位成交量连续收缩；后续延续质量偏弱风险上升，持仓进入保护监控"
    elif volume_cliff:
        risk_level, defense_penalty = "HIGH", config.penalty_top_cliff
        risk_reason = "高位成交量突然大幅退潮；禁止新开仓并观察承接"
    else:
        risk_level, defense_penalty = "NORMAL", 0
        risk_reason = "未发现高位持续缩量或断崖式退量"

    offensive_active = bool(bottom_context and raw_offensive_active and not (persistent_contraction or volume_cliff))
    if offensive_active:
        status = raw_status
        raw_bonus = raw_offensive_bonus
        reason = f"底部/底部启动区确认：{raw_reason}"
        role_scope = "BOTTOM_OFFENSE"
    elif raw_offensive_active and not bottom_context:
        status = "NON_BOTTOM_OFFENSE_DISABLED"
        raw_bonus = 0
        reason = f"{raw_reason}，但当前不在底部区，进攻加分停用"
        role_scope = "TREND_NO_OFFENSE_BONUS"
    else:
        status = raw_status
        raw_bonus = 0
        reason = raw_reason
        role_scope = "BOTTOM_OFFENSE_INACTIVE"

    def upper_shadow_ratio(row: pd.Series) -> float:
        spread = _safe_float(row["high"] - row["low"])
        if spread <= 0:
            return 0.0
        return max(0.0, _safe_float((row["high"] - max(row["open"], row["close"])) / spread))

    return {
        "status": status,
        "active": offensive_active,
        "offensive_status": raw_status,
        "offensive_active": offensive_active,
        "bottom_context": bottom_context,
        "bottom_startup": bottom_startup,
        "top_context": top_context,
        "top_persistent_contraction": persistent_contraction,
        "top_volume_cliff": volume_cliff,
        "risk_level": risk_level,
        "defense_penalty": int(defense_penalty),
        "blocks_new_entry": bool(persistent_contraction or volume_cliff),
        "condition_a": condition_a,
        "condition_b_raw": condition_b_raw,
        "condition_b_quality": condition_b_quality,
        "volume_divergence": volume_divergence,
        "raw_bonus": int(raw_bonus),
        "raw_offensive_bonus": int(raw_offensive_bonus),
        "net_effect": int(raw_bonus - defense_penalty),
        "reason": reason,
        "risk_reason": risk_reason,
        "role_scope": role_scope,
        "uses_current_unfinished_bar": False,
        "config": asdict(config),
        "audit": {
            "asof": _date_key(third["eob"]),
            "dates": [_date_key(first["eob"]), _date_key(second["eob"]), _date_key(third["eob"])],
            "volumes": [round(_safe_float(first["volume"]), 3), round(_safe_float(second["volume"]), 3), round(_safe_float(third["volume"]), 3)],
            "daily_volume_ratios": [round(ratio_1, 4), round(ratio_2, 4)],
            "total_volume_ratio": round(total_ratio, 4),
            "ma5_ratios_last_two": [round(second_vs_ma, 4), round(third_vs_ma, 4)],
            "red_k_last_two": [second_red, third_red],
            "red_count_3d": red_count,
            "price_change_3d": round(three_day_price_change, 6),
            "upper_shadow_ratios_last_two": [round(upper_shadow_ratio(second), 4), round(upper_shadow_ratio(third), 4)],
            "range_position_60d": round(range_position, 4),
            "return_20d": round(return20, 6),
            "drawdown_from_60d_high": round(drawdown_from_high, 6),
            "close_to_ma20": round(close / close_ma20, 4) if close_ma20 > 0 else None,
            "close_to_ma60": round(close / close_ma60, 4) if close_ma60 > 0 else None,
            "contraction_volumes": [round(v, 3) for v in contraction],
            "descending_contraction_steps": descending_steps,
            "latest_to_ma5": round(latest_to_ma5, 4),
            "latest_to_previous": round(latest_to_previous, 4),
            "latest_to_prior5_mean": round(latest_to_prior_mean, 4),
        },
    }


def format_volume_factor_line(candidate_or_factor: Dict[str, Any]) -> str:
    container = candidate_or_factor if isinstance(candidate_or_factor, dict) else {}
    factor = container.get("volume_soft_factor", container)
    status = str(factor.get("status") or "UNAVAILABLE")
    bonus = int(_safe_float(container.get("volume_soft_rank_bonus", factor.get("raw_bonus"))))
    audit = factor.get("audit") or {}
    mode = normalize_effect_mode(container.get("volume_soft_factor_mode"))
    effect = {"SHADOW": "仅记录", "RANKING": "候选排序", "ELIGIBLE_STRENGTH": "合格标的强度"}.get(mode, "候选排序")

    if factor.get("blocks_new_entry"):
        flags = []
        if factor.get("top_persistent_contraction"):
            flags.append("连续缩量")
        if factor.get("top_volume_cliff"):
            flags.append("断崖退量")
        return (
            f"量能防守：{factor.get('risk_level', 'HIGH')}｜高位{'＋'.join(flags) or '退量'}"
            f"｜新仓回避，持仓等盘中走弱确认再减；量能本身不机械卖出"
        )

    labels = {
        "BOTH": "双重确认",
        "TWO_RED_MA5": "连续2日红K站上5日均量",
        "MILD_3D": "连续3日温和放量",
        "VOLUME_DIVERGENCE_ONLY": "放量但价格质量不合格",
        "NON_BOTTOM_OFFENSE_DISABLED": "形态出现但不在底部，进攻加分停用",
        "NONE": "暂未满足",
        "UNAVAILABLE": "数据不可用",
    }
    detail = ""
    ma_ratios = audit.get("ma5_ratios_last_two") or []
    daily_ratios = audit.get("daily_volume_ratios") or []
    if factor.get("offensive_status") in {"BOTH", "TWO_RED_MA5"} and len(ma_ratios) == 2:
        detail = f"｜近2日量/均量 {ma_ratios[0]:.2f}、{ma_ratios[1]:.2f}"
    elif factor.get("offensive_status") in {"MILD_3D", "VOLUME_DIVERGENCE_ONLY"} and len(daily_ratios) == 2:
        detail = f"｜逐日量增 {daily_ratios[0] - 1:+.1%}、{daily_ratios[1] - 1:+.1%}"
    if factor.get("offensive_active"):
        return f"底部量能：+{bonus}（{effect}）｜{labels.get(status, status)}{detail}；只辅助底部启动"
    return f"底部量能：+0｜{labels.get(status, status)}{detail}；趋势途中不加分"
