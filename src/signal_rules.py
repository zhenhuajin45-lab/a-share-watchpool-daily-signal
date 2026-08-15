# coding: utf-8
"""A股轮动的唯一日/月线信号规则源。

回测、盘前候选和实时服务必须调用本模块，避免三套指标/阈值逐渐漂移。
所有判断只使用传入截面及更早的数据；背离检测显式区分最后一根是否已闭合，
日线盘中默认排除未完成K线，分钟聚合器则纳入已闭合的最后一根K线。
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any, Dict, Optional, Tuple

import numpy as np
import pandas as pd


@dataclass(frozen=True)
class SignalRuleConfig:
    fast_kdj: Tuple[int, int, int] = (8, 2, 2)
    slow_kdj: Tuple[int, int, int] = (9, 20, 2)
    macd: Tuple[int, int, int] = (5, 10, 5)
    slow_j_buy_low: float = 30.0
    slow_j_buy_high: float = 40.0
    slow_j_sell_low: float = 60.0
    max_return_5d: float = 0.12
    extreme_return_5d: float = 0.30
    max_extension_20d: float = 0.03
    divergence_lookback: int = 90
    divergence_pivot_radius: int = 2
    divergence_min_pivot_gap: int = 4
    minimum_daily_bars: int = 120
    minimum_monthly_bars: int = 24
    medium_confidence_monthly_bars: int = 36
    high_confidence_monthly_bars: int = 60


DEFAULT_CONFIG = SignalRuleConfig()


def _safe_float(value: Any, default: float = 0.0) -> float:
    try:
        result = float(value)
        return result if math.isfinite(result) else default
    except (TypeError, ValueError):
        return default


def _date_key(value: Any) -> Optional[str]:
    if value is None:
        return None
    return str(value)[:10]


def _ema(series: pd.Series, span: int) -> pd.Series:
    return series.ewm(span=span, adjust=False, min_periods=1).mean()


def compute_kdj(frame: pd.DataFrame, params: Tuple[int, int, int], prefix: str) -> pd.DataFrame:
    """中国行情软件常见的 RSV + SMA(X,N,1) 递推口径。

    pandas ewm(alpha=1/N, adjust=False) 与 SMA(X,N,1) 的递推形式一致。
    指标起始值按 50 初始化；在使用超过35根日线后，初值影响已显著衰减。
    """
    n, m1, m2 = params
    high_n = frame["high"].rolling(n, min_periods=n).max()
    low_n = frame["low"].rolling(n, min_periods=n).min()
    denominator = (high_n - low_n).replace(0, np.nan)
    rsv = ((frame["close"] - low_n) * 100.0 / denominator).fillna(50.0)
    k = rsv.ewm(alpha=1.0 / m1, adjust=False, min_periods=1).mean()
    d = k.ewm(alpha=1.0 / m2, adjust=False, min_periods=1).mean()
    j = 3.0 * k - 2.0 * d
    return pd.DataFrame({f"{prefix}_k": k, f"{prefix}_d": d, f"{prefix}_j": j}, index=frame.index)


def compute_features(
    frame: pd.DataFrame,
    minimum: Optional[int] = None,
    config: SignalRuleConfig = DEFAULT_CONFIG,
) -> Optional[pd.DataFrame]:
    if frame is None or len(frame) == 0:
        return None
    minimum = config.minimum_daily_bars if minimum is None else int(minimum)
    result = frame.copy()
    if "eob" not in result.columns:
        if "trade_date" in result.columns:
            result["eob"] = pd.to_datetime(result["trade_date"])
        else:
            return None
    result["eob"] = pd.to_datetime(result["eob"], errors="coerce")
    for column in ("open", "high", "low", "close", "volume", "amount", "pre_close"):
        if column in result.columns:
            result[column] = pd.to_numeric(result[column], errors="coerce")
    result = result.dropna(subset=["eob", "high", "low", "close"]).sort_values("eob").reset_index(drop=True)
    if len(result) < minimum:
        return None

    fast = compute_kdj(result, config.fast_kdj, "fast")
    slow = compute_kdj(result, config.slow_kdj, "slow")
    macd_fast, macd_slow, macd_signal = config.macd
    diff = _ema(result["close"], macd_fast) - _ema(result["close"], macd_slow)
    dea = _ema(diff, macd_signal)
    result = pd.concat([result, fast, slow], axis=1)
    result["macd_diff"] = diff
    result["macd_dea"] = dea
    result["macd_hist"] = 2.0 * (diff - dea)
    result["return_5d"] = result["close"].pct_change(5)
    result["high_20d"] = result["high"].rolling(20, min_periods=5).max()
    result["extension_20d"] = result["close"] / result["high_20d"] - 1.0
    previous_close = result.get("pre_close", result["close"].shift(1)).where(lambda x: x > 0, result["close"].shift(1))
    true_range = pd.concat(
        [result["high"] - result["low"], (result["high"] - previous_close).abs(), (result["low"] - previous_close).abs()],
        axis=1,
    ).max(axis=1)
    result["atr14_pct"] = true_range.rolling(14, min_periods=5).mean() / result["close"].replace(0, np.nan)
    return result.dropna(subset=["fast_k", "fast_d", "slow_k", "slow_d", "slow_j", "macd_hist"]).reset_index(drop=True)


def resample_monthly(daily: pd.DataFrame) -> pd.DataFrame:
    if daily is None or len(daily) == 0:
        return pd.DataFrame()
    frame = daily.copy()
    frame["eob"] = pd.to_datetime(frame["eob"], errors="coerce")
    frame = frame.dropna(subset=["eob"]).sort_values("eob")
    if frame.empty:
        return frame
    eob_for_period = frame["eob"]
    if getattr(eob_for_period.dt, "tz", None) is not None:
        eob_for_period = eob_for_period.dt.tz_localize(None)
    frame["month"] = eob_for_period.dt.to_period("M")
    aggregations = {
        "eob": ("eob", "last"),
        "open": ("open", "first"),
        "high": ("high", "max"),
        "low": ("low", "min"),
        "close": ("close", "last"),
    }
    if "volume" in frame.columns:
        aggregations["volume"] = ("volume", "sum")
    if "amount" in frame.columns:
        aggregations["amount"] = ("amount", "sum")
    return frame.groupby("month", sort=True).agg(**aggregations).reset_index(drop=True)


def detect_macd_divergence(
    frame: pd.DataFrame,
    config: SignalRuleConfig = DEFAULT_CONFIG,
    *,
    last_bar_complete: bool = False,
) -> Dict[str, Any]:
    """比较两个已确认价格拐点及其对应的归一化 DIF。

    拐点必须同时拥有左右各 ``pivot_radius`` 根K线，因此不会把尚未确认的
    右端极值当成正式背离。日线研究默认把最后一根视为尚未完成并排除；分钟
    聚合器传入的全部是闭合K线，必须显式设置 ``last_bar_complete=True``，避免
    再多丢一根完整K线。价格极值与DIF使用同一拐点附近的数据，避免固定分段法
    把两个不同日期的极值错误拼成背离。
    """
    radius = config.divergence_pivot_radius
    minimum = 2 * radius + config.divergence_min_pivot_gap + 3
    if frame is None or len(frame) < minimum or "macd_diff" not in frame.columns:
        return {"state": "NONE", "detail": "样本不足", "uses_current_bar": False}
    source = frame if last_bar_complete else frame.iloc[:-1]
    completed = source.tail(config.divergence_lookback).copy().reset_index(drop=True)
    if len(completed) < minimum:
        return {"state": "NONE", "detail": "样本不足", "uses_current_bar": False}
    completed["osc_norm"] = completed["macd_diff"] / completed["close"].replace(0, np.nan)
    valid_osc = completed["osc_norm"].dropna()
    if valid_osc.empty:
        return {"state": "NONE", "detail": "MACD数据不足", "uses_current_bar": False}
    scale = _safe_float(valid_osc.std())
    threshold = max(scale * 0.25, 0.00005)

    pivot_lows, pivot_highs = [], []
    for index in range(radius, len(completed) - radius):
        row = completed.iloc[index]
        left = completed.iloc[index - radius:index]
        right = completed.iloc[index + 1:index + radius + 1]
        if row["low"] < left["low"].min() and row["low"] < right["low"].min():
            pivot_lows.append(index)
        if row["high"] > left["high"].max() and row["high"] > right["high"].max():
            pivot_highs.append(index)

    def candidate_pairs(indices):
        for recent_pos in range(len(indices) - 1, 0, -1):
            recent_index = indices[recent_pos]
            for prior_pos in range(recent_pos - 1, -1, -1):
                prior_index = indices[prior_pos]
                if recent_index - prior_index >= config.divergence_min_pivot_gap:
                    yield prior_index, recent_index

    def last_divergent_pair(indices, kind: str):
        for prior_index, recent_index in candidate_pairs(indices):
            prior, recent = completed.iloc[prior_index], completed.iloc[recent_index]
            if kind == "LOW":
                prior_osc = completed.iloc[max(0, prior_index - radius):prior_index + radius + 1]["osc_norm"].min()
                recent_osc = completed.iloc[max(0, recent_index - radius):recent_index + radius + 1]["osc_norm"].min()
                matched = recent["low"] <= prior["low"] * 0.995 and recent_osc >= prior_osc + threshold
            else:
                prior_osc = completed.iloc[max(0, prior_index - radius):prior_index + radius + 1]["osc_norm"].max()
                recent_osc = completed.iloc[max(0, recent_index - radius):recent_index + radius + 1]["osc_norm"].max()
                matched = recent["high"] >= prior["high"] * 1.005 and recent_osc <= prior_osc - threshold
            if matched:
                return prior_index, recent_index
        return None

    # 选最近一组真正成立的背离，而不是简单选最近两个拐点。这样后续出现新的
    # 同向拐点时，仍能判断旧背离是“继续有效”还是“被价格+振荡器同步创新失效”。
    low_pair = last_divergent_pair(pivot_lows, "LOW")
    high_pair = last_divergent_pair(pivot_highs, "HIGH")
    facts: Dict[str, Any] = {
        "pivot_method": f"confirmed_radius_{radius}",
        "threshold": threshold,
        "low_pair_found": low_pair is not None,
        "high_pair_found": high_pair is not None,
        "last_bar_complete": bool(last_bar_complete),
        "source_last_bar_included": bool(last_bar_complete),
    }
    bullish = bool(low_pair)
    if low_pair:
        prior_index, recent_index = low_pair
        prior, recent = completed.iloc[prior_index], completed.iloc[recent_index]
        prior_osc = completed.iloc[max(0, prior_index - radius):prior_index + radius + 1]["osc_norm"].min()
        recent_osc = completed.iloc[max(0, recent_index - radius):recent_index + radius + 1]["osc_norm"].min()
        facts.update({
            "prior_low_date": _date_key(prior["eob"]),
            "recent_low_date": _date_key(recent["eob"]),
            "prior_low_price": _safe_float(prior["low"]),
            "recent_low_price": _safe_float(recent["low"]),
            "prior_osc_low": _safe_float(prior_osc),
            "recent_osc_low": _safe_float(recent_osc),
        })

    bearish = bool(high_pair)
    if high_pair:
        prior_index, recent_index = high_pair
        prior, recent = completed.iloc[prior_index], completed.iloc[recent_index]
        prior_osc = completed.iloc[max(0, prior_index - radius):prior_index + radius + 1]["osc_norm"].max()
        recent_osc = completed.iloc[max(0, recent_index - radius):recent_index + radius + 1]["osc_norm"].max()
        facts.update({
            "prior_high_date": _date_key(prior["eob"]),
            "recent_high_date": _date_key(recent["eob"]),
            "prior_high_price": _safe_float(prior["high"]),
            "recent_high_price": _safe_float(recent["high"]),
            "prior_osc_high": _safe_float(prior_osc),
            "recent_osc_high": _safe_float(recent_osc),
        })

    state = "CONFLICT" if bullish and bearish else ("BULLISH" if bullish else ("BEARISH" if bearish else "NONE"))

    # 背离是一段结构，不是永远有效的静态标签。若最近已确认背离之后价格继续
    # 创出同向极值、同时振荡器也同步创新，则原背离已经失效。这里仅改变背离
    # 生命周期，不把一次失效直接翻译成反向买卖信号。
    lifecycle = "NONE"
    invalidated_by = None
    if state in {"BEARISH", "CONFLICT"} and high_pair:
        _, recent_index = high_pair
        later = completed.iloc[recent_index + 1:]
        if len(later):
            later_high = _safe_float(later["high"].max(), float("nan"))
            later_osc = _safe_float(later["osc_norm"].max(), float("nan"))
            if (
                math.isfinite(later_high) and math.isfinite(later_osc)
                and later_high >= facts["recent_high_price"] * 1.005
                and later_osc >= facts["recent_osc_high"] + threshold
            ):
                if state == "BEARISH":
                    state = "NONE"
                lifecycle = "INVALIDATED"
                invalidated_by = "HIGHER_HIGH_WITH_OSCILLATOR_CONFIRMATION"
    if state in {"BULLISH", "CONFLICT"} and low_pair:
        _, recent_index = low_pair
        later = completed.iloc[recent_index + 1:]
        if len(later):
            later_low = _safe_float(later["low"].min(), float("nan"))
            later_osc = _safe_float(later["osc_norm"].min(), float("nan"))
            if (
                math.isfinite(later_low) and math.isfinite(later_osc)
                and later_low <= facts["recent_low_price"] * 0.995
                and later_osc <= facts["recent_osc_low"] - threshold
            ):
                if state == "BULLISH":
                    state = "NONE"
                lifecycle = "INVALIDATED"
                invalidated_by = "LOWER_LOW_WITH_OSCILLATOR_CONFIRMATION"

    candidate_state = "NONE"
    if lifecycle != "INVALIDATED" and state == "NONE" and len(completed) >= radius + 2:
        recent_window = completed.iloc[-(radius + 1):]
        candidate_high_index = int(recent_window["high"].idxmax())
        candidate_low_index = int(recent_window["low"].idxmin())
        if pivot_highs:
            prior_index = pivot_highs[-1]
            if candidate_high_index > prior_index:
                prior = completed.iloc[prior_index]
                candidate = completed.iloc[candidate_high_index]
                prior_osc = completed.iloc[max(0, prior_index - radius):prior_index + radius + 1]["osc_norm"].max()
                candidate_osc = completed.iloc[max(0, candidate_high_index - radius):candidate_high_index + 1]["osc_norm"].max()
                if candidate["high"] >= prior["high"] * 1.005 and candidate_osc <= prior_osc - threshold:
                    candidate_state = "CANDIDATE_BEARISH"
        if pivot_lows:
            prior_index = pivot_lows[-1]
            if candidate_low_index > prior_index:
                prior = completed.iloc[prior_index]
                candidate = completed.iloc[candidate_low_index]
                prior_osc = completed.iloc[max(0, prior_index - radius):prior_index + radius + 1]["osc_norm"].min()
                candidate_osc = completed.iloc[max(0, candidate_low_index - radius):candidate_low_index + 1]["osc_norm"].min()
                if candidate["low"] <= prior["low"] * 0.995 and candidate_osc >= prior_osc + threshold:
                    candidate_state = (
                        "CANDIDATE_CONFLICT" if candidate_state == "CANDIDATE_BEARISH"
                        else "CANDIDATE_BULLISH"
                    )
    if lifecycle == "NONE" and state != "NONE":
        lifecycle = "CONFIRMED_ACTIVE"
    elif lifecycle == "NONE" and candidate_state != "NONE":
        lifecycle = "CANDIDATE_UNCONFIRMED"
    active_recent_index = None
    if state in {"BEARISH", "CONFLICT"} and high_pair:
        active_recent_index = high_pair[1]
    elif state in {"BULLISH", "CONFLICT"} and low_pair:
        active_recent_index = low_pair[1]
    confirmed_index = (
        active_recent_index + radius
        if active_recent_index is not None and active_recent_index + radius < len(completed)
        else None
    )
    confirmed_at = _date_key(completed.iloc[confirmed_index]["eob"]) if confirmed_index is not None else None
    signal_available_at = confirmed_at
    dif_corroborated = False
    hist_corroborated = False
    price_move_atr = None
    zero_axis_context = "UNKNOWN"
    if state in {"BEARISH", "CONFLICT"} and high_pair:
        prior_index, recent_index = high_pair
        dif_corroborated = facts.get("recent_osc_high", 0) < facts.get("prior_osc_high", 0)
        if "macd_hist" in completed.columns:
            prior_hist = _safe_float(completed.iloc[max(0, prior_index - radius):prior_index + radius + 1]["macd_hist"].max())
            recent_hist = _safe_float(completed.iloc[max(0, recent_index - radius):recent_index + radius + 1]["macd_hist"].max())
            hist_corroborated = recent_hist < prior_hist
            facts.update({"prior_hist_peak": prior_hist, "recent_hist_peak": recent_hist})
        price_move_atr = (facts["recent_high_price"] / facts["prior_high_price"] - 1.0)
        zero_axis_context = "ABOVE_ZERO" if facts.get("recent_osc_high", 0) > 0 else "BELOW_ZERO"
    elif state in {"BULLISH", "CONFLICT"} and low_pair:
        prior_index, recent_index = low_pair
        dif_corroborated = facts.get("recent_osc_low", 0) > facts.get("prior_osc_low", 0)
        if "macd_hist" in completed.columns:
            prior_hist = _safe_float(completed.iloc[max(0, prior_index - radius):prior_index + radius + 1]["macd_hist"].min())
            recent_hist = _safe_float(completed.iloc[max(0, recent_index - radius):recent_index + radius + 1]["macd_hist"].min())
            hist_corroborated = recent_hist > prior_hist
            facts.update({"prior_hist_trough": prior_hist, "recent_hist_trough": recent_hist})
        price_move_atr = (facts["prior_low_price"] / facts["recent_low_price"] - 1.0)
        zero_axis_context = "BELOW_ZERO" if facts.get("recent_osc_low", 0) < 0 else "ABOVE_ZERO"
    pivot_gap = None
    active_pair = high_pair if state in {"BEARISH", "CONFLICT"} else low_pair
    if active_pair:
        pivot_gap = active_pair[1] - active_pair[0]
    quality_score = 0
    if state != "NONE":
        quality_score = 45
        quality_score += 20 if hist_corroborated else 0
        quality_score += 15 if pivot_gap is not None and pivot_gap >= config.divergence_min_pivot_gap * 2 else 5
        quality_score += 15 if price_move_atr is not None and price_move_atr >= 0.01 else 5
        quality_score = min(100, quality_score)
    return {
        "state": state,
        "detail": f"已确认价格拐点(radius={radius})与对应DIF/收盘价；阈值={threshold:.6f}",
        "uses_current_bar": bool(last_bar_complete),
        "lifecycle": lifecycle,
        "candidate_state": candidate_state,
        "invalidated_by": invalidated_by,
        "divergence_type": (
            "REGULAR_BEARISH" if state == "BEARISH"
            else ("REGULAR_BULLISH" if state == "BULLISH" else state)
        ),
        "dif_confirmed": bool(dif_corroborated),
        "hist_corroborated": bool(hist_corroborated),
        "zero_axis_context": zero_axis_context,
        "confirmed_at": confirmed_at,
        "signal_available_at": signal_available_at,
        "pivot_gap_bars": pivot_gap,
        "quality_score": quality_score,
        "quality_role": "STRUCTURAL_EVIDENCE_NOT_STANDALONE_ACTION",
        **facts,
    }


def classify_monthly_context(
    monthly: Optional[pd.DataFrame],
    config: SignalRuleConfig = DEFAULT_CONFIG,
) -> Dict[str, Any]:
    if monthly is None or len(monthly) < config.minimum_monthly_bars:
        return {"state": "MONTHLY_UNKNOWN", "support": False, "blocks_buy": False, "risk_weight": -3, "reason": f"月线样本不足（至少{config.minimum_monthly_bars}个月），只降置信度，不作硬否决"}
    features = compute_features(monthly, minimum=config.minimum_monthly_bars, config=config)
    if features is None or len(features) < 2:
        return {"state": "MONTHLY_UNKNOWN", "support": False, "blocks_buy": False, "risk_weight": -3, "reason": "月线指标不可用，只降置信度，不作硬否决"}
    previous, current = features.iloc[-2], features.iloc[-1]
    sample_count = len(monthly)
    confidence = (
        "HIGH" if sample_count >= config.high_confidence_monthly_bars
        else ("MEDIUM" if sample_count >= config.medium_confidence_monthly_bars else "LOW")
    )
    j = _safe_float(current["slow_j"])
    slow_rising = current["slow_j"] > previous["slow_j"] and current["slow_k"] >= previous["slow_k"]
    slow_bull = current["slow_k"] > current["slow_d"]
    fast_cross = previous["fast_k"] <= previous["fast_d"] and current["fast_k"] > current["fast_d"]
    fast_bull = current["fast_k"] > current["fast_d"]
    fast_rising = current["fast_j"] > previous["fast_j"] and current["fast_k"] >= previous["fast_k"]
    fast_down = previous["fast_k"] >= previous["fast_d"] and current["fast_k"] < current["fast_d"]

    # 月线的两个参数体系职责不同：9,20,2 定义中期结构，8,2,2 只判断
    # 当前月相位是在修复、共振还是降温。快速月线不能推翻慢结构，只能增减权重。
    if slow_bull and fast_bull:
        dual_alignment = "DUAL_BULLISH"
    elif (slow_bull or slow_rising) and (fast_down or not fast_bull):
        dual_alignment = "SLOW_UP_FAST_COOLING"
    elif not slow_bull and (fast_cross or fast_bull or fast_rising):
        dual_alignment = "FAST_REPAIRING_SLOW_WEAK"
    elif not slow_bull and not fast_bull:
        dual_alignment = "DUAL_WEAK"
    else:
        dual_alignment = "MIXED"

    if j >= config.slow_j_sell_low and fast_down:
        state, support, blocks, risk_weight, reason = (
            "MONTHLY_HIGH_COOLING", False, False, -6,
            f"月线9,20,2慢J={j:.1f}处于60+且8,2,2转弱，进入保护观察",
        )
    elif j >= config.slow_j_sell_low and (fast_bull or fast_rising):
        state, support, blocks, risk_weight, reason = (
            "MONTHLY_HIGH_TREND", True, False, -1,
            f"月线9,20,2慢J={j:.1f}偏高，但8,2,2仍向上；不机械卖出，提高盘中承接要求",
        )
    elif not slow_bull and not slow_rising and (fast_cross or fast_rising):
        state, support, blocks, risk_weight, reason = (
            "MONTHLY_FAST_REPAIR", True, False, 2,
            "月线9,20,2结构仍弱，但8,2,2先行修复，只提供早期支持、不替代日线确认",
        )
    elif not slow_bull and not slow_rising:
        state, support, blocks, risk_weight, reason = (
            "MONTHLY_DUAL_WEAK", False, False, -8,
            "月线9,20,2与8,2,2均弱，显著降低优先级但不作单项硬否决",
        )
    elif config.slow_j_buy_low <= j <= config.slow_j_buy_high and slow_rising and fast_bull:
        state, support, blocks, risk_weight, reason = (
            "MONTHLY_DUAL_RECOVERING", True, False, 8,
            f"月线9,20,2慢J={j:.1f}处于30-40并修复，8,2,2同步向上",
        )
    elif (slow_bull or slow_rising) and fast_bull:
        state, support, blocks, risk_weight, reason = (
            "MONTHLY_DUAL_SUPPORT", True, False, 8,
            "月线9,20,2结构与8,2,2相位共振向上",
        )
    elif slow_bull or slow_rising:
        state, support, blocks, risk_weight, reason = (
            "MONTHLY_SLOW_SUPPORT_FAST_COOLING", True, False, 2,
            "月线9,20,2结构仍有支持，但8,2,2正在降温",
        )
    else:
        state, support, blocks, risk_weight, reason = "MONTHLY_NEUTRAL", False, False, 0, "月线双体系中性，不作硬否决"
    return {
        "state": state,
        "support": support,
        "blocks_buy": blocks,
        "risk_weight": risk_weight,
        "reason": reason,
        "confidence": confidence,
        "sample_count": sample_count,
        "slow_k": round(_safe_float(current["slow_k"]), 3),
        "slow_d": round(_safe_float(current["slow_d"]), 3),
        "slow_j": round(j, 3),
        "fast_k": round(_safe_float(current["fast_k"]), 3),
        "fast_d": round(_safe_float(current["fast_d"]), 3),
        "fast_j": round(_safe_float(current["fast_j"]), 3),
        "fast_cross": bool(fast_cross),
        "fast_bullish": bool(fast_bull),
        "fast_rising": bool(fast_rising),
        "fast_down": bool(fast_down),
        "dual_alignment": dual_alignment,
        "parameter_roles": "MONTHLY_9_20_2_STRUCTURE_PLUS_8_2_2_PHASE",
    }


def classify_daily_signal(
    frame: Optional[pd.DataFrame],
    monthly: Optional[pd.DataFrame],
    sector_state: int = 0,
    sector_confidence: str = "LOW",
    config: SignalRuleConfig = DEFAULT_CONFIG,
) -> Optional[Dict[str, Any]]:
    if frame is None or len(frame) < 2:
        return None
    previous, current = frame.iloc[-2], frame.iloc[-1]
    required = (
        "fast_k", "fast_d", "fast_j", "slow_k", "slow_d", "slow_j",
        "macd_diff", "macd_hist", "return_5d", "extension_20d",
    )
    if any(column not in frame.columns for column in required):
        return None
    numeric = [current[column] for column in required] + [
        previous["fast_k"], previous["fast_d"], previous["fast_j"], previous["slow_k"],
        previous["slow_d"], previous["slow_j"], previous["macd_diff"], previous["macd_hist"],
    ]
    if not all(math.isfinite(_safe_float(value, float("nan"))) for value in numeric):
        return None

    # 日线只让9,20,2拥有决策权。日线8,2,2保留在输出中用于审计和研究，
    # 但不再参与买入、卖出、评分或缺失条件，避免跨周期职责混用。
    fast_cross = previous["fast_k"] <= previous["fast_d"] and current["fast_k"] > current["fast_d"]
    fast_bullish = current["fast_k"] > current["fast_d"]
    fast_recovery = fast_bullish and current["fast_j"] > previous["fast_j"] and current["fast_k"] <= 80
    fast_trigger = fast_cross or fast_recovery
    fast_down = previous["fast_k"] >= previous["fast_d"] and current["fast_k"] < current["fast_d"]

    slow_cross = previous["slow_k"] <= previous["slow_d"] and current["slow_k"] > current["slow_d"]
    slow_bullish = current["slow_k"] > current["slow_d"]
    slow_recovery = (
        slow_bullish
        and current["slow_j"] > previous["slow_j"]
        and current["slow_k"] >= previous["slow_k"]
    )
    slow_confirmed = slow_cross or slow_recovery
    slow_down = previous["slow_k"] >= previous["slow_d"] and current["slow_k"] < current["slow_d"]
    slow_j_buy_zone = config.slow_j_buy_low <= current["slow_j"] <= config.slow_j_buy_high
    slow_j_sell_zone = current["slow_j"] >= config.slow_j_sell_low

    divergence = detect_macd_divergence(frame, config=config)
    macd_improving = current["macd_hist"] > previous["macd_hist"] and current["macd_diff"] >= previous["macd_diff"]
    macd_deteriorating = current["macd_hist"] < previous["macd_hist"] and current["macd_diff"] <= previous["macd_diff"]
    macd_buy_evidence = macd_improving or divergence["state"] == "BULLISH"
    macd_bearish_risk = divergence["state"] == "BEARISH"
    price_extended = current["return_5d"] > config.max_return_5d
    price_extreme = current["return_5d"] > config.extreme_return_5d
    not_chasing = not price_extreme
    sector_blocks = sector_state < 0 and sector_confidence in {"MEDIUM", "HIGH"}
    month = classify_monthly_context(monthly, config=config)

    reason_codes = []
    if slow_j_buy_zone:
        reason_codes.append("SLOW_J_IN_30_40")
    if slow_confirmed:
        reason_codes.append("SLOW_CONFIRMED")
    if macd_improving:
        reason_codes.append("MACD_IMPROVING")
    if divergence["state"] == "BULLISH":
        reason_codes.append("MACD_BULLISH_DIVERGENCE")
    if sector_blocks:
        reason_codes.append("SECTOR_WEAK_RISK")
    if month["state"] in {"MONTHLY_HIGH_RISK", "MONTHLY_WEAK", "MONTHLY_UNKNOWN"}:
        reason_codes.append("MONTHLY_RISK")
    if price_extended:
        reason_codes.append("PRICE_EXTENDED_RISK")
    if price_extreme:
        reason_codes.append("PRICE_EXTREME_RISK")
    if macd_bearish_risk:
        reason_codes.append("MACD_BEARISH_DIVERGENCE")

    recent_high_zone = _safe_float(frame["slow_j"].iloc[-4:-1].max(), float("-inf")) >= config.slow_j_sell_low
    high_zone_rollover = recent_high_zone and current["slow_j"] < previous["slow_j"] and (slow_down or macd_deteriorating)
    bearish_divergence_exit = macd_bearish_risk and current["slow_j"] >= 50 and (slow_down or macd_deteriorating)
    confirmed_exit = bool(
        (high_zone_rollover and slow_down and macd_deteriorating)
        or bearish_divergence_exit
    )

    if confirmed_exit:
        daily_route = "RISK_EXIT"
    elif slow_bullish and macd_buy_evidence and not macd_bearish_risk and current["slow_j"] >= 40:
        daily_route = "TREND_CONTINUATION"
    elif slow_j_buy_zone and slow_confirmed and macd_buy_evidence and not macd_bearish_risk:
        daily_route = "TREND_PULLBACK"
    elif current["slow_j"] < config.slow_j_buy_low and slow_confirmed and macd_buy_evidence and not macd_bearish_risk:
        daily_route = "REVERSAL_REPAIR"
    elif slow_j_sell_zone:
        daily_route = "HOLD_PROTECT"
    else:
        daily_route = "NO_SETUP"

    signal_strength = 20
    signal_strength += 20 if slow_confirmed else 0
    signal_strength += 12 if slow_bullish else 0
    signal_strength += 18 if macd_buy_evidence else 0
    signal_strength += 10 if sector_state > 0 else (-15 if sector_blocks else 0)
    signal_strength += int(month.get("risk_weight", 0))
    signal_strength += {"TREND_CONTINUATION": 8, "TREND_PULLBACK": 6, "REVERSAL_REPAIR": 3}.get(daily_route, 0)
    signal_strength -= 15 if macd_bearish_risk else 0
    signal_strength -= 4 if price_extended else 0
    signal_strength -= 12 if price_extreme else 0
    signal_strength = max(0, min(100, int(signal_strength)))

    intraday_eligible = bool(
        daily_route in {"TREND_CONTINUATION", "TREND_PULLBACK"}
        and signal_strength >= 55
        and not confirmed_exit
        and not macd_bearish_risk
        and not price_extreme
        and not sector_blocks
    )
    buy_ready = intraday_eligible and signal_strength >= 70

    if confirmed_exit:
        action, lane, status = "EXIT", "CONFIRMED_DAILY_REVERSAL", "EXIT_CANDIDATE"
        reason = (
            f"退出观察：慢J={current['slow_j']:.1f}本身不构成卖点；本次由高位回落、"
            f"日线9,20,2结构转弱、MACD恶化/顶背离形成组合确认"
        )
    elif buy_ready:
        action = "BUY"
        lane = daily_route
        status = "A_TREND_PRIORITY" if daily_route == "TREND_CONTINUATION" else "A_PRIORITY"
        reason = (
            f"{daily_route}强度{signal_strength}/100：慢线{'向上' if slow_confirmed else '未确认'}，"
            f"MACD{'改善' if macd_buy_evidence else '未改善'}；高位只提高承接要求，不作机械否决"
        )
    elif daily_route == "REVERSAL_REPAIR":
        action, lane, status = "WATCH", daily_route, "D_REVERSAL_SHADOW"
        reason = f"反转修复强度{signal_strength}/100：历史探索尚无正期望证据，只记录影子事件，不生成正式盘中买点"
    elif intraday_eligible:
        action, lane, status = "WATCH", daily_route, "B_INTRADAY_CONFIRM"
        reason = f"{daily_route}强度{signal_strength}/100：允许盘中用板块扩散、VWAP和盘口事件继续确认"
    elif slow_j_sell_zone:
        action, lane, status = "WATCH", daily_route, "H_PROTECT"
        reason = (
            f"慢J={current['slow_j']:.1f}进入60+保护区，但尚无组合卖点；"
            "继续持有观察，只有结构、动量和板块转弱共同出现才升级退出"
        )
    elif slow_j_buy_zone:
        action, lane, status = "WATCH", "WAIT_CONFIRMATION", "C_WAIT_CONFIRM"
        missing = []
        if not slow_confirmed:
            missing.append("慢线确认")
        if not macd_buy_evidence:
            missing.append("MACD改善/底背离")
        if sector_blocks:
            missing.append("板块止弱")
        reason = f"慢J={current['slow_j']:.1f}处于性价比区，仍缺少：{'、'.join(missing) or '盘中承接'}"
    else:
        action, lane, status = "WAIT", daily_route, "NO_DAILY_SETUP"
        reason = f"当前路线={daily_route}，强度{signal_strength}/100，尚未形成值得盘中触发的日线先验"

    return {
        "action": action,
        "lane": lane,
        "status": status,
        "reason": reason,
        "reason_codes": reason_codes,
        "daily_route": daily_route,
        "signal_strength": signal_strength,
        "intraday_eligible": intraday_eligible,
        "protection_level": "HIGH" if slow_j_sell_zone else ("MEDIUM" if current["slow_j"] >= 50 else "NORMAL"),
        "signal_date": _date_key(current["eob"]),
        "close": round(_safe_float(current["close"]), 6),
        "pre_close": round(_safe_float(current.get("pre_close"), _safe_float(previous["close"])), 6),
        "fast_k": round(_safe_float(current["fast_k"]), 3),
        "fast_d": round(_safe_float(current["fast_d"]), 3),
        "fast_j": round(_safe_float(current["fast_j"]), 3),
        "fast_trigger": bool(fast_trigger),
        "fast_down": bool(fast_down),
        "daily_fast_822_role": "DIAGNOSTIC_ONLY_NOT_DECISION",
        "daily_primary_kdj": "9,20,2",
        "slow_k": round(_safe_float(current["slow_k"]), 3),
        "slow_d": round(_safe_float(current["slow_d"]), 3),
        "slow_j": round(_safe_float(current["slow_j"]), 3),
        "slow_j_zone": "VALUE_30_40" if slow_j_buy_zone else ("PROTECT_60_PLUS" if slow_j_sell_zone else "NEUTRAL"),
        "slow_confirmed": bool(slow_confirmed),
        "slow_down": bool(slow_down),
        "macd_diff": round(_safe_float(current["macd_diff"]), 6),
        "macd_dea": round(_safe_float(current.get("macd_dea")), 6),
        "macd_hist": round(_safe_float(current["macd_hist"]), 6),
        "macd_improving": bool(macd_improving),
        "macd_deteriorating": bool(macd_deteriorating),
        "macd_divergence": divergence["state"],
        "macd_divergence_detail": divergence["detail"],
        "return_5d": round(_safe_float(current["return_5d"]), 6),
        "extension_20d": round(_safe_float(current["extension_20d"]), 6),
        "not_chasing": bool(not_chasing),
        "price_extended": bool(price_extended),
        "price_extreme": bool(price_extreme),
        "atr14_pct": round(_safe_float(current.get("atr14_pct")), 6),
        "sector_state": int(sector_state),
        "sector_confidence": sector_confidence,
        "sector_blocks_buy": bool(sector_blocks),
        "monthly_state": month["state"],
        "monthly_support": bool(month["support"]),
        "monthly_blocks_buy": bool(month["blocks_buy"]),
        "monthly_reason": month["reason"],
        "monthly_confidence": month.get("confidence", "UNKNOWN"),
        "monthly_sample_count": month.get("sample_count", 0),
        "monthly_slow_j": month.get("slow_j"),
        "monthly_fast_k": month.get("fast_k"),
        "monthly_fast_d": month.get("fast_d"),
        "monthly_fast_j": month.get("fast_j"),
        "monthly_fast_trigger": bool(month.get("fast_cross") or month.get("fast_rising")),
        "monthly_dual_alignment": month.get("dual_alignment", "UNKNOWN"),
        "monthly_parameter_roles": month.get("parameter_roles", "MONTHLY_UNKNOWN"),
        "slow_j_sell_zone": bool(slow_j_sell_zone),
        "high_zone_rollover": bool(high_zone_rollover),
        "bearish_divergence_exit": bool(bearish_divergence_exit),
        "confirmed_exit": confirmed_exit,
        "score": signal_strength,
        "signal_score": signal_strength,
        "rules_version": "daily_signal_rules_v6_layered_cycles",
        "feature_no_lookahead": True,
        "no_lookahead_scope": "PRICE_AND_INDICATOR_FEATURES_AS_OF_SIGNAL_DATE_ONLY",
    }
