# coding: utf-8
"""5/15/30/60/120分钟多周期指标引擎。

周期职责固定为：

* 日线/月线结构由 ``signal_rules`` 处理；
* 5/15/30/60/120分钟只使用 KDJ(8,2,2) 与 MACD(5,10,5)；
* 指标只在对应分钟K线完成后更新，当前未完成K线只记录价格，不参与信号，
  从而避免分钟级重绘和事后偷看。
* 60分钟当前只承担结构和30/60分钟背离风险解释；120分钟只做长结构预热，
  MA225不足时明确报告而不硬阻断；二者都不改变既有5/15/30分钟
  正式触发条件；待历史前向验证通过后再决定是否提升权重。

历史一分钟数据只用于指标预热；当天由顺序到达的Tick合成一分钟K线，再聚合为
5/15/30/60/120分钟K线。该模块只输出事实与确认状态，不发送订单。
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import datetime, time as dt_time, timedelta
from pathlib import Path
from typing import Any, Dict, Iterable, Mapping, Optional

import numpy as np
import pandas as pd

from signal_rules import DEFAULT_CONFIG, compute_kdj, detect_macd_divergence


def _safe_float(value: Any, default: float = 0.0) -> float:
    try:
        result = float(value)
        return result if math.isfinite(result) else default
    except (TypeError, ValueError):
        return default


def _as_local_naive(value: Any) -> Optional[datetime]:
    if isinstance(value, pd.Timestamp):
        value = value.to_pydatetime()
    if not isinstance(value, datetime):
        try:
            value = datetime.fromisoformat(str(value or "").replace("Z", "+00:00"))
        except (TypeError, ValueError):
            return None
    if value.tzinfo is not None:
        try:
            value = value.astimezone().replace(tzinfo=None)
        except Exception:
            value = value.replace(tzinfo=None)
    return value


def _continuous_session(ts: datetime) -> bool:
    now = ts.time()
    return dt_time(9, 30) <= now <= dt_time(11, 30) or dt_time(13, 0) <= now <= dt_time(15, 0)


def _minute_eob(ts: datetime) -> datetime:
    """把Tick映射到与GoldMiner一分钟历史一致的K线结束标签。"""
    floor = ts.replace(second=0, microsecond=0)
    if ts.time() == dt_time(11, 30):
        return floor
    if ts.time() == dt_time(15, 0):
        return floor
    return floor + timedelta(minutes=1)


def _session_start(eob: datetime) -> Optional[datetime]:
    day = eob.replace(hour=0, minute=0, second=0, microsecond=0)
    if dt_time(9, 30) < eob.time() <= dt_time(11, 30):
        return day.replace(hour=9, minute=30)
    if dt_time(13, 0) < eob.time() <= dt_time(15, 0):
        return day.replace(hour=13, minute=0)
    return None


def _period_eob(eob: datetime, minutes: int) -> Optional[datetime]:
    start = _session_start(eob)
    if start is None:
        return None
    elapsed = int((eob - start).total_seconds() // 60)
    if elapsed <= 0:
        return None
    group = (elapsed - 1) // minutes + 1
    return start + timedelta(minutes=group * minutes)


def _normalize_minute_frame(frame: Optional[pd.DataFrame]) -> pd.DataFrame:
    columns = ["symbol", "eob", "open", "high", "low", "close", "volume", "amount"]
    if frame is None or len(frame) == 0:
        return pd.DataFrame(columns=columns)
    result = frame.copy()
    if "eob" not in result.columns:
        return pd.DataFrame(columns=columns)
    result["eob"] = pd.to_datetime(result["eob"], errors="coerce")
    if getattr(result["eob"].dt, "tz", None) is not None:
        result["eob"] = result["eob"].dt.tz_convert("Asia/Shanghai").dt.tz_localize(None)
    for column in ("open", "high", "low", "close", "volume", "amount"):
        if column not in result.columns:
            result[column] = 0.0
        result[column] = pd.to_numeric(result[column], errors="coerce")
    if "symbol" not in result.columns:
        result["symbol"] = ""
    result = result.dropna(subset=["eob", "open", "high", "low", "close"])
    result = result[result["close"] > 0].sort_values("eob").drop_duplicates("eob", keep="last")
    return result[columns].reset_index(drop=True)


def aggregate_completed_minutes(frame: pd.DataFrame, minutes: int) -> pd.DataFrame:
    """按A股上午/下午会话分别聚合，绝不跨午休拼K线。"""
    normalized = _normalize_minute_frame(frame)
    if normalized.empty:
        return normalized
    # 向量化会话分桶。历史预热约45万根一分钟K线，逐行Python map会显著拖慢启动。
    minute_of_day = normalized["eob"].dt.hour * 60 + normalized["eob"].dt.minute
    morning = (minute_of_day > 9 * 60 + 30) & (minute_of_day <= 11 * 60 + 30)
    afternoon = (minute_of_day > 13 * 60) & (minute_of_day <= 15 * 60)
    session_start = pd.Series(np.nan, index=normalized.index)
    session_start.loc[morning] = 9 * 60 + 30
    session_start.loc[afternoon] = 13 * 60
    elapsed = minute_of_day - session_start
    group_number = np.ceil(elapsed / float(minutes))
    period_minute_of_day = session_start + group_number * minutes
    normalized["period_eob"] = (
        normalized["eob"].dt.normalize()
        + pd.to_timedelta(period_minute_of_day, unit="m")
    )
    normalized = normalized.dropna(subset=["period_eob"])
    if normalized.empty:
        return normalized
    result = normalized.groupby("period_eob", sort=True).agg(
        symbol=("symbol", "last"),
        open=("open", "first"),
        high=("high", "max"),
        low=("low", "min"),
        close=("close", "last"),
        volume=("volume", "sum"),
        amount=("amount", "sum"),
        source_minute_count=("eob", "count"),
        latest_source_eob=("eob", "max"),
    ).reset_index().rename(columns={"period_eob": "eob"})
    # 只纳入已经走到周期结束点的K线。历史中无成交分钟可能缺失，因此不强制
    # source_minute_count==minutes，但最新源分钟必须达到理论周期结束时刻。
    result = result[result["latest_source_eob"] >= result["eob"]]
    return result.reset_index(drop=True)


def _indicator_frame(frame: pd.DataFrame) -> Optional[pd.DataFrame]:
    if frame is None or len(frame) < 20:
        return None
    result = frame.copy().sort_values("eob").reset_index(drop=True)
    kdj = compute_kdj(result, DEFAULT_CONFIG.fast_kdj, "kdj822")
    fast, slow, signal = DEFAULT_CONFIG.macd
    diff = result["close"].ewm(span=fast, adjust=False, min_periods=1).mean() - result["close"].ewm(
        span=slow, adjust=False, min_periods=1
    ).mean()
    dea = diff.ewm(span=signal, adjust=False, min_periods=1).mean()
    result = pd.concat([result, kdj], axis=1)
    result["macd_diff"] = diff
    result["macd_dea"] = dea
    result["macd_hist"] = 2.0 * (diff - dea)
    previous_close = result["close"].shift(1)
    true_range = pd.concat(
        [
            result["high"] - result["low"],
            (result["high"] - previous_close).abs(),
            (result["low"] - previous_close).abs(),
        ],
        axis=1,
    ).max(axis=1)
    result["atr14"] = true_range.rolling(14, min_periods=5).mean()
    for window in (5, 20, 50, 99, 128, 225):
        result[f"ma{window}"] = result["close"].rolling(window, min_periods=window).mean()
    return result


def classify_period(frame: pd.DataFrame, minutes: int) -> Dict[str, Any]:
    features = _indicator_frame(frame)
    if features is None or len(features) < 2:
        return {
            "minutes": minutes,
            "state": "UNAVAILABLE",
            "supportive": False,
            "bearish": False,
            "score": 0,
            "reason": "完成K线不足20根",
            "parameter_roles": "KDJ_8_2_2_PLUS_MACD_5_10_5",
        }
    previous, current = features.iloc[-2], features.iloc[-1]
    kdj_cross = previous["kdj822_k"] <= previous["kdj822_d"] and current["kdj822_k"] > current["kdj822_d"]
    kdj_down = previous["kdj822_k"] >= previous["kdj822_d"] and current["kdj822_k"] < current["kdj822_d"]
    kdj_bullish = current["kdj822_k"] > current["kdj822_d"]
    kdj_rising = current["kdj822_j"] > previous["kdj822_j"] and current["kdj822_k"] >= previous["kdj822_k"]
    macd_cross = previous["macd_diff"] <= previous["macd_dea"] and current["macd_diff"] > current["macd_dea"]
    macd_bullish = current["macd_diff"] > current["macd_dea"]
    macd_improving = current["macd_hist"] > previous["macd_hist"] and current["macd_diff"] >= previous["macd_diff"]
    # features中的最后一根已经由会话边界确认闭合，不能像日线盘中快照那样再丢一根。
    divergence = detect_macd_divergence(
        features,
        config=DEFAULT_CONFIG,
        last_bar_complete=True,
    )

    atr14 = _safe_float(current.get("atr14"))
    ma20 = _safe_float(current.get("ma20"), float("nan"))
    prior_ma20 = _safe_float(features.iloc[-6].get("ma20"), float("nan")) if len(features) >= 6 else float("nan")
    ma20_slope_atr_5 = (
        (ma20 - prior_ma20) / atr14
        if atr14 > 0 and math.isfinite(ma20) and math.isfinite(prior_ma20)
        else None
    )
    extension_ma20_atr = (
        (_safe_float(current["close"]) - ma20) / atr14
        if atr14 > 0 and math.isfinite(ma20)
        else None
    )
    prior = features.iloc[:-1]
    prior_high_20 = _safe_float(prior["high"].tail(20).max(), float("nan")) if len(prior) else float("nan")
    prior_low_20 = _safe_float(prior["low"].tail(20).min(), float("nan")) if len(prior) else float("nan")

    score = 45
    score += 15 if kdj_bullish else -10
    score += 12 if kdj_rising else 0
    score += 8 if kdj_cross else 0
    score += 13 if macd_improving else -4
    score += 7 if macd_bullish else 0
    score += 15 if divergence["state"] == "BULLISH" else 0
    score -= 25 if divergence["state"] == "BEARISH" else 0
    score -= 8 if kdj_down and not macd_improving else 0
    score = max(0, min(100, int(round(score))))
    supportive = bool(
        score >= 58
        and (kdj_bullish or kdj_cross)
        and (macd_improving or macd_bullish or divergence["state"] == "BULLISH")
        and divergence["state"] != "BEARISH"
    )
    # 单周期顶背离是风险提示，不再单独把整个周期判死刑。只有背离同时伴随
    # KDJ转弱、MACD未改善且综合分很低，才升级为BEARISH；否则保持MIXED，
    # 让另外两个周期和实时承接决定是否降级为B档。
    structural_bearish = bool(score <= 38 and (kdj_down or not kdj_bullish) and not macd_improving)
    divergence_confirmed_bearish = bool(
        divergence["state"] == "BEARISH"
        and score <= 45
        and (kdj_down or not kdj_bullish)
        and not macd_improving
    )
    bearish = structural_bearish or divergence_confirmed_bearish
    if supportive and score >= 75:
        state = "BULLISH"
    elif supportive:
        state = "RECOVERING"
    elif bearish:
        state = "BEARISH"
    else:
        state = "MIXED"
    return {
        "minutes": minutes,
        "state": state,
        "supportive": supportive,
        "bearish": bearish,
        "score": score,
        "asof": pd.Timestamp(current["eob"]).isoformat(),
        "completed_bar_count": len(features),
        "k": round(_safe_float(current["kdj822_k"]), 3),
        "d": round(_safe_float(current["kdj822_d"]), 3),
        "j": round(_safe_float(current["kdj822_j"]), 3),
        "kdj_cross": bool(kdj_cross),
        "kdj_down": bool(kdj_down),
        "kdj_bullish": bool(kdj_bullish),
        "kdj_rising": bool(kdj_rising),
        "macd_diff": round(_safe_float(current["macd_diff"]), 6),
        "macd_dea": round(_safe_float(current["macd_dea"]), 6),
        "macd_hist": round(_safe_float(current["macd_hist"]), 6),
        "macd_cross": bool(macd_cross),
        "macd_bullish": bool(macd_bullish),
        "macd_improving": bool(macd_improving),
        "macd_divergence": divergence["state"],
        "divergence_lifecycle": divergence.get("lifecycle", "NONE"),
        "divergence_candidate": divergence.get("candidate_state", "NONE"),
        "divergence_invalidated_by": divergence.get("invalidated_by"),
        "divergence_type": divergence.get("divergence_type", "NONE"),
        "divergence_confirmed_at": divergence.get("confirmed_at"),
        "divergence_signal_available_at": divergence.get("signal_available_at"),
        "divergence_hist_corroborated": divergence.get("hist_corroborated", False),
        "divergence_zero_axis_context": divergence.get("zero_axis_context", "UNKNOWN"),
        "divergence_quality": divergence.get("quality_score", 0),
        "divergence_warning": divergence["state"] == "BEARISH",
        "close": round(_safe_float(current["close"]), 4),
        "high": round(_safe_float(current["high"]), 4),
        "low": round(_safe_float(current["low"]), 4),
        "atr14": round(atr14, 6) if atr14 > 0 else None,
        "ma5": round(_safe_float(current.get("ma5"), float("nan")), 6) if math.isfinite(_safe_float(current.get("ma5"), float("nan"))) else None,
        "ma20": round(ma20, 6) if math.isfinite(ma20) else None,
        "ma50": round(_safe_float(current.get("ma50"), float("nan")), 6) if math.isfinite(_safe_float(current.get("ma50"), float("nan"))) else None,
        "ma99": round(_safe_float(current.get("ma99"), float("nan")), 6) if math.isfinite(_safe_float(current.get("ma99"), float("nan"))) else None,
        "ma128": round(_safe_float(current.get("ma128"), float("nan")), 6) if math.isfinite(_safe_float(current.get("ma128"), float("nan"))) else None,
        "ma225": round(_safe_float(current.get("ma225"), float("nan")), 6) if math.isfinite(_safe_float(current.get("ma225"), float("nan"))) else None,
        "ma20_slope_atr_5bars": round(ma20_slope_atr_5, 6) if ma20_slope_atr_5 is not None else None,
        "extension_ma20_atr": round(extension_ma20_atr, 6) if extension_ma20_atr is not None else None,
        "prior_high_20": round(prior_high_20, 4) if math.isfinite(prior_high_20) else None,
        "prior_low_20": round(prior_low_20, 4) if math.isfinite(prior_low_20) else None,
        "reason": (
            f"KDJ(8,2,2){'多头' if kdj_bullish else '空头'}、"
            f"MACD(5,10,5){'改善' if macd_improving else '未改善'}、背离{divergence['state']}"
        ),
        "parameter_roles": "KDJ_8_2_2_PLUS_MACD_5_10_5",
        "uses_completed_bars_only": True,
    }


@dataclass
class _SymbolState:
    seed_1m: pd.DataFrame
    seed_periods: Dict[int, pd.DataFrame]
    today_completed: pd.DataFrame
    current_bar: Optional[Dict[str, Any]] = None
    trade_date: Optional[str] = None
    last_context: Optional[Dict[str, Any]] = None
    last_five_trigger_at: Optional[datetime] = None
    period_contexts: Dict[int, Dict[str, Any]] = field(default_factory=dict)


class MultiTimeframeIndicatorEngine:
    PERIODS = (5, 15, 30, 60, 120)
    # 保留现有正式决策三周期，60分钟先作为影子结构层，避免未经验证就改变线上行为。
    DECISION_PERIODS = (5, 15, 30)

    def __init__(self):
        self.states: Dict[str, _SymbolState] = {}

    def seed(self, symbol: str, minute_frame: Optional[pd.DataFrame]) -> Dict[str, Any]:
        normalized = _normalize_minute_frame(minute_frame)
        if not normalized.empty:
            normalized = normalized[normalized["symbol"].astype(str).isin({"", symbol})].copy()
            normalized["symbol"] = symbol
        # 400根已远大于最长指标与背离窗口；保留更多只会拖慢逐日重放。
        periods = {minutes: aggregate_completed_minutes(normalized, minutes).tail(400).reset_index(drop=True) for minutes in self.PERIODS}
        self.states[symbol] = _SymbolState(
            seed_1m=normalized.tail(20000).reset_index(drop=True),
            seed_periods=periods,
            today_completed=pd.DataFrame(columns=normalized.columns),
            period_contexts={minutes: classify_period(periods[minutes], minutes) for minutes in self.PERIODS},
        )
        return {
            "symbol": symbol,
            "seed_1m_count": len(normalized),
            "seed_period_counts": {str(key): len(value) for key, value in periods.items()},
            "seed_last_eob": pd.Timestamp(normalized.iloc[-1]["eob"]).isoformat() if len(normalized) else None,
        }

    def seed_from_directory(self, root: Path, symbols: Iterable[str]) -> Dict[str, Any]:
        root = Path(root)
        rows = []
        for symbol in symbols:
            path = root / f"{symbol}_1m.pkl"
            try:
                frame = pd.read_pickle(path) if path.exists() else None
                row = self.seed(symbol, frame)
                row["source"] = str(path) if path.exists() else "MISSING"
            except Exception as exc:
                row = self.seed(symbol, None)
                row.update({"source": str(path), "error": f"{type(exc).__name__}: {str(exc)[:160]}"})
            rows.append(row)
        return {
            "root": str(root),
            "symbol_count": len(rows),
            "ready_count": sum(row["seed_1m_count"] > 0 for row in rows),
            "rows": rows,
        }

    @staticmethod
    def _new_minute_bar(symbol: str, eob: datetime, observation: Mapping[str, Any]) -> Dict[str, Any]:
        price = _safe_float(observation.get("price"))
        return {
            "symbol": symbol,
            "eob": eob,
            "open": price,
            "high": price,
            "low": price,
            "close": price,
            "volume": 0.0,
            "amount": 0.0,
            "first_cum_volume": _safe_float(observation.get("cum_volume")),
            "last_cum_volume": _safe_float(observation.get("cum_volume")),
            "first_cum_amount": _safe_float(observation.get("cum_amount")),
            "last_cum_amount": _safe_float(observation.get("cum_amount")),
        }

    @staticmethod
    def _update_minute_bar(bar: Dict[str, Any], observation: Mapping[str, Any]) -> None:
        price = _safe_float(observation.get("price"))
        bar["high"] = max(_safe_float(bar.get("high")), price)
        bar["low"] = min(_safe_float(bar.get("low"), price), price)
        bar["close"] = price
        bar["last_cum_volume"] = _safe_float(observation.get("cum_volume"), _safe_float(bar.get("last_cum_volume")))
        bar["last_cum_amount"] = _safe_float(observation.get("cum_amount"), _safe_float(bar.get("last_cum_amount")))

    @staticmethod
    def _finalize_minute_bar(bar: Dict[str, Any]) -> Dict[str, Any]:
        result = dict(bar)
        volume_delta = _safe_float(bar.get("last_cum_volume")) - _safe_float(bar.get("first_cum_volume"))
        amount_delta = _safe_float(bar.get("last_cum_amount")) - _safe_float(bar.get("first_cum_amount"))
        result["volume"] = max(0.0, volume_delta)
        result["amount"] = max(0.0, amount_delta)
        for key in ("first_cum_volume", "last_cum_volume", "first_cum_amount", "last_cum_amount"):
            result.pop(key, None)
        return result

    def _build_context(self, symbol: str, state: _SymbolState, event_ts: datetime) -> Dict[str, Any]:
        period_context: Dict[str, Dict[str, Any]] = {}
        latest_minute_eob = None
        if len(state.today_completed):
            latest_value = state.today_completed.iloc[-1]["eob"]
            latest_minute_eob = latest_value.to_pydatetime() if isinstance(latest_value, pd.Timestamp) else latest_value
        # 分别聚合，不能把5分钟K线再次拼成15/30分钟，否则停牌/缺分钟时边界易漂移。
        for minutes in self.PERIODS:
            period_boundary = bool(
                latest_minute_eob and _period_eob(latest_minute_eob, minutes) == latest_minute_eob
            )
            if period_boundary or minutes not in state.period_contexts:
                today_period = aggregate_completed_minutes(state.today_completed, minutes) if len(state.today_completed) else pd.DataFrame()
                if today_period.empty:
                    combined = state.seed_periods[minutes].copy()
                elif state.seed_periods[minutes].empty:
                    combined = today_period.copy()
                else:
                    combined = pd.concat([state.seed_periods[minutes], today_period], ignore_index=True)
                if len(combined):
                    combined = combined.sort_values("eob").drop_duplicates("eob", keep="last").reset_index(drop=True)
                state.period_contexts[minutes] = classify_period(combined, minutes)
            period_context[str(minutes)] = state.period_contexts[minutes]

        decision_rows = [period_context[str(minutes)] for minutes in self.DECISION_PERIODS]
        available = [row for row in decision_rows if row.get("state") != "UNAVAILABLE"]
        supportive = [row for row in available if row.get("supportive")]
        bearish = [row for row in available if row.get("bearish")]
        five = period_context["5"]
        higher = [period_context["15"], period_context["30"]]
        higher_support = sum(bool(row.get("supportive")) for row in higher)
        higher_bearish = sum(bool(row.get("bearish")) for row in higher)
        raw_five_trigger = bool(
            five.get("supportive")
            and (five.get("kdj_cross") or five.get("kdj_rising"))
            and (five.get("macd_improving") or five.get("macd_cross") or five.get("macd_divergence") == "BULLISH")
        )
        if raw_five_trigger:
            state.last_five_trigger_at = event_ts
        recent_five_trigger = bool(
            state.last_five_trigger_at
            and 0 <= (event_ts - state.last_five_trigger_at).total_seconds() <= 15 * 60
            and five.get("supportive")
        )
        trigger_confirmed = bool(recent_five_trigger and higher_support >= 1 and higher_bearish < 2)
        full_bullish = bool(len(supportive) == 3)
        if full_bullish:
            alignment = "FULL_BULLISH"
        elif len(supportive) >= 2 and len(bearish) == 0:
            alignment = "BULLISH_2_OF_3"
        elif len(bearish) >= 2:
            alignment = "BEARISH_2_OF_3"
        else:
            alignment = "MIXED"
        score = int(round(np.mean([row.get("score", 0) for row in available]))) if available else 0
        thirty = period_context["30"]
        sixty = period_context["60"]
        one_twenty = period_context["120"]
        structural_bearish_divergences = [
            str(minutes)
            for minutes, row in ((30, thirty), (60, sixty))
            if row.get("macd_divergence") == "BEARISH"
            and row.get("divergence_lifecycle") == "CONFIRMED_ACTIVE"
        ]
        structural_bullish_divergences = [
            str(minutes)
            for minutes, row in ((30, thirty), (60, sixty))
            if row.get("macd_divergence") == "BULLISH"
            and row.get("divergence_lifecycle") == "CONFIRMED_ACTIVE"
        ]
        if len(structural_bearish_divergences) == 2:
            divergence_30_60 = "BOTH_BEARISH"
        elif structural_bearish_divergences:
            divergence_30_60 = f"BEARISH_{structural_bearish_divergences[0]}M"
        elif len(structural_bullish_divergences) == 2:
            divergence_30_60 = "BOTH_BULLISH"
        elif structural_bullish_divergences:
            divergence_30_60 = f"BULLISH_{structural_bullish_divergences[0]}M"
        else:
            divergence_30_60 = "NONE"
        return {
            "symbol": symbol,
            "event_ts": event_ts.isoformat(),
            "alignment": alignment,
            "score": score,
            "available_count": len(available),
            "supportive_count": len(supportive),
            "bearish_count": len(bearish),
            "five_minute_trigger": raw_five_trigger,
            "recent_five_minute_trigger": recent_five_trigger,
            "last_five_minute_trigger_at": state.last_five_trigger_at.isoformat() if state.last_five_trigger_at else None,
            "trigger_confirmed": trigger_confirmed,
            "sudden_trend_confirmed": bool(trigger_confirmed and score >= 68 and five.get("score", 0) >= 72),
            "divergence_30_60": divergence_30_60,
            "structural_bearish_divergence_periods": structural_bearish_divergences,
            "structural_bullish_divergence_periods": structural_bullish_divergences,
            "higher_timeframe_risk_shadow": bool(structural_bearish_divergences),
            "higher_timeframe_support_shadow": bool(structural_bullish_divergences),
            "sixty_minute_role": "STRUCTURE_AND_DIVERGENCE_SHADOW_ZERO_WEIGHT",
            "one_twenty_minute_structure_shadow": {
                "status": "READY" if one_twenty.get("state") != "UNAVAILABLE" else "UNAVAILABLE",
                "state": one_twenty.get("state"),
                "score": one_twenty.get("score"),
                "asof": one_twenty.get("asof"),
                "ma20": one_twenty.get("ma20"),
                "ma50": one_twenty.get("ma50"),
                "ma225": one_twenty.get("ma225"),
                "ma225_ready": one_twenty.get("ma225") is not None,
                "warmup_bar_count": one_twenty.get("completed_bar_count", 0),
                "role": "LONG_STRUCTURE_SHADOW_NO_MA225_HARD_BLOCK",
            },
            "periods": period_context,
            "uses_completed_bars_only": True,
            "current_partial_minute_excluded": True,
            "no_lookahead_scope": "ONLY_PERIOD_BARS_COMPLETED_BEFORE_CURRENT_TICK",
        }

    def update(self, observation: Mapping[str, Any]) -> Dict[str, Any]:
        symbol = str(observation.get("symbol") or "")
        event_ts = _as_local_naive(observation.get("event_ts") or observation.get("created_at"))
        price = _safe_float(observation.get("price"))
        if not symbol or event_ts is None or price <= 0 or not _continuous_session(event_ts):
            return {"symbol": symbol, "state": "IGNORED", "reason": "非连续竞价时段或数据无效"}
        if symbol not in self.states:
            self.seed(symbol, None)
        state = self.states[symbol]
        trade_date = event_ts.strftime("%Y-%m-%d")
        if state.trade_date != trade_date:
            state.trade_date = trade_date
            state.today_completed = pd.DataFrame(columns=state.seed_1m.columns)
            state.current_bar = None
            state.last_context = None
            state.last_five_trigger_at = None

        eob = _minute_eob(event_ts)
        finalized = False
        if state.current_bar is None:
            state.current_bar = self._new_minute_bar(symbol, eob, observation)
        elif eob == state.current_bar["eob"]:
            self._update_minute_bar(state.current_bar, observation)
        elif eob > state.current_bar["eob"]:
            completed = self._finalize_minute_bar(state.current_bar)
            completed_frame = pd.DataFrame([completed])
            state.today_completed = completed_frame if state.today_completed.empty else pd.concat(
                [state.today_completed, completed_frame], ignore_index=True
            )
            state.current_bar = self._new_minute_bar(symbol, eob, observation)
            finalized = True
        else:
            return state.last_context or {"symbol": symbol, "state": "OUT_OF_ORDER_IGNORED"}

        # 1分钟K线每分钟落地，但多周期指标只需要在完整5分钟边界刷新。
        completed_eob = completed.get("eob") if finalized else None
        five_boundary = bool(
            finalized and completed_eob and _period_eob(completed_eob, 5) == completed_eob
        )
        if five_boundary:
            context = self._build_context(symbol, state, event_ts)
            context["minute_bar_finalized"] = True
            context["latest_price"] = price
            state.last_context = context
        elif state.last_context is None:
            state.last_context = {
                "symbol": symbol,
                "event_ts": event_ts.isoformat(),
                "alignment": "WARMING_UP_TODAY",
                "score": 0,
                "available_count": 0,
                "supportive_count": 0,
                "bearish_count": 0,
                "five_minute_trigger": False,
                "trigger_confirmed": False,
                "sudden_trend_confirmed": False,
                "periods": {},
                "uses_completed_bars_only": True,
                "current_partial_minute_excluded": True,
                "no_lookahead_scope": "ONLY_PERIOD_BARS_COMPLETED_BEFORE_CURRENT_TICK",
            }
        result = dict(state.last_context)
        result["latest_price"] = price
        result["current_partial_minute_eob"] = eob.isoformat()
        return result

    def context_for(self, symbol: str) -> Dict[str, Any]:
        state = self.states.get(symbol)
        return dict(state.last_context) if state and state.last_context else {
            "symbol": symbol,
            "alignment": "NO_TICK_CONTEXT",
            "trigger_confirmed": False,
            "sudden_trend_confirmed": False,
            "periods": {},
        }

    def snapshot(self) -> Dict[str, Any]:
        rows = [self.context_for(symbol) for symbol in sorted(self.states)]
        return {"rows": rows, "by_symbol": {row["symbol"]: row for row in rows}}
