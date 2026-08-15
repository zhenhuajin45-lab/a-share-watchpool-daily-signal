# coding: utf-8
"""结构化日内择时影子引擎。

本模块把盘前日线结构和盘中连续状态拆成可审计事实：Session、Path、Location、
Room、15分钟Setup、5分钟/逐笔Execution以及30/60分钟背离风险。V16阶段默认
``SHADOW_ZERO_WEIGHT``：记录、解释、重放，但不直接放行或否决正式买卖信号。

所有日线输入必须截止D-1；分钟指标只读取已经闭合的周期K线；逐笔路径只使用
当前Tick及此前顺序到达的数据，禁止用当日收盘后的高低点反推盘中决策。
"""

from __future__ import annotations

import math
import os
from collections import defaultdict, deque
from dataclasses import dataclass
from datetime import datetime, time as dt_time
from typing import Any, Deque, Dict, Iterable, List, Mapping, Optional, Tuple

import numpy as np
import pandas as pd


STRUCTURED_TIMING_VERSION = "structured_timing_v16_shadow_20260813"


def _safe_float(value: Any, default: float = 0.0) -> float:
    try:
        result = float(value)
        return result if math.isfinite(result) else default
    except (TypeError, ValueError):
        return default


def _clip(value: float, low: float, high: float) -> float:
    return max(low, min(high, value))


def _iso(value: Any) -> Optional[str]:
    try:
        return pd.Timestamp(value).isoformat()
    except Exception:
        return None


def _session_phase(ts: datetime) -> str:
    current = ts.time()
    if dt_time(9, 30) <= current < dt_time(10, 0):
        return "OPENING_DISCOVERY"
    if dt_time(10, 0) <= current <= dt_time(11, 30):
        return "MORNING_CONTINUOUS"
    if dt_time(13, 0) <= current < dt_time(14, 0):
        return "AFTERNOON_RESTART"
    if dt_time(14, 0) <= current < dt_time(14, 45):
        return "AFTERNOON_CONTINUOUS"
    if dt_time(14, 45) <= current <= dt_time(15, 0):
        return "CLOSING_CONFIRMATION"
    return "OUTSIDE_CONTINUOUS_SESSION"


def _confirmed_pivots(frame: pd.DataFrame, column: str, kind: str, radius: int = 2) -> List[Tuple[int, float]]:
    values = pd.to_numeric(frame[column], errors="coerce")
    result: List[Tuple[int, float]] = []
    for index in range(radius, len(frame) - radius):
        value = _safe_float(values.iloc[index], float("nan"))
        if not math.isfinite(value):
            continue
        left = values.iloc[index - radius:index]
        right = values.iloc[index + 1:index + radius + 1]
        if kind == "HIGH" and value > left.max() and value > right.max():
            result.append((index, value))
        elif kind == "LOW" and value < left.min() and value < right.min():
            result.append((index, value))
    return result


def _deduplicate_levels(levels: Iterable[Dict[str, Any]], tolerance: float) -> List[Dict[str, Any]]:
    ordered = sorted(
        (dict(row) for row in levels if _safe_float(row.get("price")) > 0),
        key=lambda row: _safe_float(row.get("price")),
    )
    result: List[Dict[str, Any]] = []
    for row in ordered:
        price = _safe_float(row.get("price"))
        if result and abs(price - _safe_float(result[-1].get("price"))) <= tolerance:
            result[-1].setdefault("sources", [result[-1].get("source")])
            result[-1]["sources"].append(row.get("source"))
            result[-1]["strength"] = max(
                int(result[-1].get("strength", 1)),
                int(row.get("strength", 1)),
            )
        else:
            result.append(row)
    return result


def build_daily_timing_context(frame: Optional[pd.DataFrame]) -> Dict[str, Any]:
    """用D-1及更早的前复权日线构建次日可见的支撑/压力事实。"""
    if frame is None or len(frame) < 20:
        return {
            "status": "UNAVAILABLE",
            "reason": "日线不足20根",
            "rules_version": STRUCTURED_TIMING_VERSION,
            "no_lookahead": True,
        }
    source = frame.copy().sort_values("eob").reset_index(drop=True)
    current = source.iloc[-1]
    close = _safe_float(current.get("close"))
    atr = _safe_float(current.get("atr14_pct")) * close
    if atr <= 0:
        previous_close = pd.to_numeric(source["close"], errors="coerce").shift(1)
        true_range = pd.concat(
            [
                pd.to_numeric(source["high"], errors="coerce") - pd.to_numeric(source["low"], errors="coerce"),
                (pd.to_numeric(source["high"], errors="coerce") - previous_close).abs(),
                (pd.to_numeric(source["low"], errors="coerce") - previous_close).abs(),
            ],
            axis=1,
        ).max(axis=1)
        atr = _safe_float(true_range.tail(14).mean(), close * 0.04)
    atr = max(atr, close * 0.008)

    levels: List[Dict[str, Any]] = []
    for window, strength in ((20, 2), (60, 3), (120, 4)):
        tail = source.tail(min(window, len(source)))
        levels.extend([
            {"price": _safe_float(tail["high"].max()), "source": f"DAILY_HIGH_{window}", "kind": "RESISTANCE", "strength": strength},
            {"price": _safe_float(tail["low"].min()), "source": f"DAILY_LOW_{window}", "kind": "SUPPORT", "strength": strength},
        ])
    for window, strength in ((5, 1), (10, 1), (20, 2), (60, 3)):
        if len(source) >= window:
            levels.append({
                "price": _safe_float(pd.to_numeric(source["close"], errors="coerce").tail(window).mean()),
                "source": f"DAILY_MA_{window}",
                "kind": "DYNAMIC",
                "strength": strength,
            })
    for index, price in _confirmed_pivots(source, "high", "HIGH")[-6:]:
        levels.append({
            "price": price,
            "source": "CONFIRMED_DAILY_PIVOT_HIGH",
            "kind": "RESISTANCE",
            "strength": 3,
            "asof": _iso(source.iloc[index].get("eob")),
        })
    for index, price in _confirmed_pivots(source, "low", "LOW")[-6:]:
        levels.append({
            "price": price,
            "source": "CONFIRMED_DAILY_PIVOT_LOW",
            "kind": "SUPPORT",
            "strength": 3,
            "asof": _iso(source.iloc[index].get("eob")),
        })
    levels = _deduplicate_levels(levels, tolerance=atr * 0.12)
    return {
        "status": "READY",
        "asof": _iso(current.get("eob")),
        "reference_close": close,
        "atr_abs": atr,
        "atr_pct": atr / close if close > 0 else None,
        "levels": levels,
        "price_adjustment": "ADJUST_PREV_FRONT_ADJUSTED",
        "rules_version": STRUCTURED_TIMING_VERSION,
        "no_lookahead": True,
        "scope": "D_MINUS_1_AND_EARLIER_ONLY",
    }


@dataclass(frozen=True)
class TimingThresholds:
    room_good_atr: float = 2.0
    room_tight_atr: float = 1.0
    zone_touch_atr: float = 0.35
    zone_near_atr: float = 0.80
    failure_drawdown_atr: float = 0.90
    path_confirm_observations: int = 3
    path_recovery_observations: int = 4
    failure_cooldown_seconds: int = 300


class StructuredTimingEngine:
    """顺序Tick驱动的结构择时引擎；默认只输出零权重研究标签。"""

    POSITIVE_PATHS = {"GAP_HOLD", "TREND_EXPANSION", "ORDERLY_PULLBACK", "BASE_BUILDING"}
    FAILURE_PATHS = {"GAP_FAILURE", "RALLY_FAILURE", "DISTRIBUTION_SHOCK"}
    POSITIVE_SETUPS = {"BREAKOUT_SETUP", "TREND_PULLBACK_SETUP", "SUPPORT_REVERSAL_SETUP", "RECLAIM_SETUP"}
    POSITIVE_EXECUTIONS = {"VWAP_RECLAIM", "VWAP_HOLD", "HIGHER_LOW", "BREAKOUT_HOLD"}

    def __init__(self, thresholds: TimingThresholds = TimingThresholds(), mode: Optional[str] = None):
        self.thresholds = thresholds
        requested = str(mode or os.getenv("A_SHARE_ROTATION_STRUCTURED_TIMING_MODE", "SHADOW")).upper()
        # V16只允许SHADOW；环境变量写错也不能意外改变正式交易行为。
        self.mode = "SHADOW" if requested != "OFF" else "OFF"
        self.states: Dict[str, Dict[str, Any]] = defaultdict(self._new_state)
        self.volume_profiles: Dict[str, Dict[int, float]] = {}
        self.seed_status: Dict[str, Dict[str, Any]] = {}

    @staticmethod
    def _new_state() -> Dict[str, Any]:
        return {
            "trade_date": None,
            "open": 0.0,
            "high": 0.0,
            "low": float("inf"),
            "samples": deque(maxlen=6000),
            "path": "UNINITIALIZED",
            "pending_path": None,
            "pending_count": 0,
            "last_transition": None,
            "last_failure_at": None,
            "previous_vwap_side": None,
            "last_context": None,
        }

    @staticmethod
    def _minute_slot(ts: datetime) -> int:
        minute = ts.hour * 60 + ts.minute
        if minute <= 11 * 60 + 30:
            elapsed = max(0, minute - (9 * 60 + 30))
        else:
            elapsed = 120 + max(0, minute - 13 * 60)
        return int(math.ceil(max(1, elapsed) / 5.0) * 5)

    def seed(self, symbol: str, minute_frame: Optional[pd.DataFrame]) -> Dict[str, Any]:
        if minute_frame is None or len(minute_frame) == 0 or "eob" not in minute_frame.columns:
            row = {"symbol": symbol, "status": "UNAVAILABLE", "profile_slot_count": 0}
            self.seed_status[symbol] = row
            self.volume_profiles[symbol] = {}
            return row
        frame = minute_frame.copy()
        frame["eob"] = pd.to_datetime(frame["eob"], errors="coerce")
        if getattr(frame["eob"].dt, "tz", None) is not None:
            frame["eob"] = frame["eob"].dt.tz_convert("Asia/Shanghai").dt.tz_localize(None)
        frame["volume"] = pd.to_numeric(frame.get("volume", 0), errors="coerce").fillna(0).clip(lower=0)
        frame = frame.dropna(subset=["eob"]).sort_values("eob")
        frame["trade_date"] = frame["eob"].dt.strftime("%Y-%m-%d")
        minute = frame["eob"].dt.hour * 60 + frame["eob"].dt.minute
        frame = frame[((minute > 9 * 60 + 30) & (minute <= 11 * 60 + 30)) | ((minute > 13 * 60) & (minute <= 15 * 60))]
        if frame.empty:
            return self.seed(symbol, None)
        minute = frame["eob"].dt.hour * 60 + frame["eob"].dt.minute
        morning = minute <= 11 * 60 + 30
        elapsed = np.where(morning, minute - (9 * 60 + 30), 120 + minute - 13 * 60)
        frame["slot"] = (np.ceil(np.maximum(1, elapsed) / 5.0) * 5).astype(int)
        frame["cum_volume"] = frame.groupby("trade_date")["volume"].cumsum()
        daily_last = frame.groupby(["trade_date", "slot"], as_index=False)["cum_volume"].last()
        recent_dates = sorted(daily_last["trade_date"].unique())[-20:]
        daily_last = daily_last[daily_last["trade_date"].isin(recent_dates)]
        profile = daily_last.groupby("slot")["cum_volume"].median().to_dict()
        self.volume_profiles[symbol] = {int(key): _safe_float(value) for key, value in profile.items()}
        row = {
            "symbol": symbol,
            "status": "READY" if profile else "UNAVAILABLE",
            "profile_slot_count": len(profile),
            "history_day_count": len(recent_dates),
            "history_last_date": recent_dates[-1] if recent_dates else None,
        }
        self.seed_status[symbol] = row
        return row

    @staticmethod
    def _window(samples: Deque[Dict[str, Any]], now: datetime, low_seconds: int, high_seconds: int) -> List[Dict[str, Any]]:
        return [
            row for row in samples
            if low_seconds <= (now - row["event_ts"]).total_seconds() <= high_seconds
        ]

    def _classify_path(self, facts: Dict[str, Any], capital: Mapping[str, Any]) -> str:
        gap = facts["gap_atr"]
        drawdown = facts["high_to_now_atr"]
        clv = facts["clv"]
        above_vwap = facts["above_vwap"]
        imbalance = facts["amount_imbalance"]
        open_to_now = facts["open_to_now_atr"]
        rvol = facts.get("rvol")
        capital_score = _safe_float(capital.get("score"), 50.0)
        if drawdown >= 1.15 and clv <= 0.28 and not above_vwap and imbalance <= -0.18:
            return "DISTRIBUTION_SHOCK"
        if gap >= 0.45 and open_to_now <= -0.45 and clv <= 0.42:
            return "GAP_FAILURE"
        if drawdown >= self.thresholds.failure_drawdown_atr and clv <= 0.45 and not above_vwap:
            return "RALLY_FAILURE"
        if clv >= 0.76 and above_vwap and open_to_now >= 0.30 and drawdown <= 0.35 and (
            rvol is None or rvol >= 1.05 or capital_score >= 60
        ):
            return "TREND_EXPANSION"
        if gap >= 0.40 and open_to_now >= -0.25 and clv >= 0.55 and above_vwap:
            return "GAP_HOLD"
        if 0.30 <= drawdown <= 0.90 and clv >= 0.45 and open_to_now >= -0.35 and imbalance >= -0.35:
            return "ORDERLY_PULLBACK"
        if abs(open_to_now) <= 0.35 and clv >= 0.40 and imbalance >= -0.30:
            return "BASE_BUILDING"
        return "MIXED_PATH"

    def _stable_path(self, state: Dict[str, Any], raw_path: str, ts: datetime) -> Tuple[str, Optional[Dict[str, Any]]]:
        current = str(state.get("path") or "UNINITIALIZED")
        immediate = raw_path in self.FAILURE_PATHS and current not in self.FAILURE_PATHS
        if current == "UNINITIALIZED" or immediate:
            confirmed = raw_path
        elif raw_path == current:
            state["pending_path"] = None
            state["pending_count"] = 0
            return current, None
        else:
            if state.get("pending_path") == raw_path:
                state["pending_count"] += 1
            else:
                state["pending_path"] = raw_path
                state["pending_count"] = 1
            required = (
                self.thresholds.path_recovery_observations
                if current in self.FAILURE_PATHS
                else self.thresholds.path_confirm_observations
            )
            if state["pending_count"] < required:
                return current, None
            confirmed = raw_path
        transition = None
        if confirmed != current:
            transition = {
                "from": current,
                "to": confirmed,
                "at": ts.isoformat(),
                "reason": "IMMEDIATE_RISK" if immediate else "HYSTERESIS_CONFIRMED",
            }
            state["path"] = confirmed
            state["pending_path"] = None
            state["pending_count"] = 0
            state["last_transition"] = transition
            if confirmed in self.FAILURE_PATHS:
                state["last_failure_at"] = ts
        return str(state["path"]), transition

    @staticmethod
    def _dynamic_levels(candidate: Mapping[str, Any], multitimeframe: Mapping[str, Any]) -> List[Dict[str, Any]]:
        levels = [dict(row) for row in ((candidate.get("timing_static_context") or {}).get("levels") or [])]
        fifteen = (multitimeframe.get("periods") or {}).get("15") or {}
        for field, kind, source in (
            ("ma20", "DYNAMIC", "M15_MA20"),
            ("prior_high_20", "RESISTANCE", "M15_PRIOR_HIGH20"),
            ("prior_low_20", "SUPPORT", "M15_PRIOR_LOW20"),
        ):
            value = _safe_float(fifteen.get(field))
            if value > 0:
                levels.append({"price": value, "kind": kind, "source": source, "strength": 2})
        return levels

    def _location(self, price: float, atr: float, levels: List[Dict[str, Any]]) -> Dict[str, Any]:
        below = sorted(
            (
                row for row in levels
                if row.get("kind") in {"SUPPORT", "DYNAMIC"}
                and _safe_float(row.get("price")) <= price
            ),
            key=lambda row: _safe_float(row.get("price")), reverse=True,
        )
        above = sorted(
            (
                row for row in levels
                # Room-to-Resistance只读真实结构压力；上方均线可用于趋势状态，
                # 但不能和前高/摆动高点混成同一“可上涨空间”口径。
                if row.get("kind") == "RESISTANCE"
                and _safe_float(row.get("price")) > price
            ),
            key=lambda row: _safe_float(row.get("price")),
        )
        support = below[0] if below else None
        resistance = above[0] if above else None
        support_distance = (price - _safe_float(support.get("price"))) / atr if support and atr > 0 else None
        room = (_safe_float(resistance.get("price")) - price) / atr if resistance and atr > 0 else 9.99
        if resistance is None:
            location = "BREAKOUT_ZONE"
        elif room <= self.thresholds.zone_touch_atr:
            location = "AT_RESISTANCE"
        elif support_distance is not None and support_distance <= self.thresholds.zone_touch_atr:
            location = "AT_SUPPORT"
        elif room <= self.thresholds.room_tight_atr:
            location = "NEAR_RESISTANCE"
        elif support_distance is not None and support_distance <= self.thresholds.zone_near_atr:
            location = "NEAR_SUPPORT"
        else:
            location = "MID_AIR"
        quality = "OPEN" if room >= self.thresholds.room_good_atr else ("TIGHT" if room <= self.thresholds.room_tight_atr else "USABLE")
        return {
            "state": location,
            "room_atr": round(room, 4),
            "room_quality": quality,
            "nearest_support": support,
            "support_distance_atr": round(support_distance, 4) if support_distance is not None else None,
            "nearest_resistance": resistance,
            "atr_abs": round(atr, 6),
            "atr_basis": "M15_COMPLETED_ATR14_OR_DAILY_FALLBACK",
        }

    def _setup_15m(self, multitimeframe: Mapping[str, Any], location: Mapping[str, Any], price: float) -> Dict[str, Any]:
        fifteen = (multitimeframe.get("periods") or {}).get("15") or {}
        if not fifteen or fifteen.get("state") == "UNAVAILABLE":
            return {"state": "UNAVAILABLE", "confirmed_bar_asof": None}
        ma20 = _safe_float(fifteen.get("ma20"))
        prior_high = _safe_float(fifteen.get("prior_high_20"))
        slope = fifteen.get("ma20_slope_atr_5bars")
        bearish_divergence = (
            fifteen.get("macd_divergence") == "BEARISH"
            and fifteen.get("divergence_lifecycle") == "CONFIRMED_ACTIVE"
        )
        if bearish_divergence or fifteen.get("state") == "BEARISH":
            # 历史切片显示该标签不能独立判死刑，改成中性警示名称，避免文字
            # 先入为主；是否构成风险必须再看30/60、位置、Path与资金。
            state = "MOMENTUM_DIVERGENCE_CAUTION"
        elif prior_high > 0 and price >= prior_high * 1.001 and fifteen.get("supportive"):
            state = "BREAKOUT_SETUP"
        elif location.get("state") in {"AT_SUPPORT", "NEAR_SUPPORT"} and (
            fifteen.get("kdj_cross") or fifteen.get("kdj_rising")
        ) and fifteen.get("macd_improving"):
            state = "SUPPORT_REVERSAL_SETUP"
        elif ma20 > 0 and price >= ma20 * 0.995 and slope is not None and _safe_float(slope) >= 0 and (
            fifteen.get("supportive") or fifteen.get("macd_improving")
        ):
            state = "TREND_PULLBACK_SETUP"
        elif fifteen.get("kdj_cross") and fifteen.get("macd_improving"):
            state = "RECLAIM_SETUP"
        else:
            state = "MIXED_SETUP"
        return {
            "state": state,
            "confirmed_bar_asof": fifteen.get("asof"),
            "score": fifteen.get("score"),
            "ma20": fifteen.get("ma20"),
            "ma20_slope_atr_5bars": slope,
            "macd_divergence": fifteen.get("macd_divergence"),
            "divergence_lifecycle": fifteen.get("divergence_lifecycle"),
            "uses_completed_bars_only": True,
        }

    def _execution(self, state: Dict[str, Any], facts: Dict[str, Any], ts: datetime) -> Dict[str, Any]:
        samples = state["samples"]
        price = facts["price"]
        vwap = facts["vwap"]
        side = "ABOVE" if vwap > 0 and price >= vwap else ("BELOW" if vwap > 0 else "UNKNOWN")
        prior_side = state.get("previous_vwap_side")
        recent = self._window(samples, ts, 0, 120)
        earlier = self._window(samples, ts, 120, 360)
        higher_low = False
        if recent and earlier:
            recent_low = min(_safe_float(row.get("price")) for row in recent)
            earlier_low = min(_safe_float(row.get("price")) for row in earlier)
            higher_low = recent_low >= earlier_low + facts["atr"] * 0.08
        if prior_side == "BELOW" and side == "ABOVE":
            execution = "VWAP_RECLAIM"
        elif facts["clv"] >= 0.78 and facts["high_to_now_atr"] <= 0.22 and facts["above_vwap"]:
            execution = "BREAKOUT_HOLD"
        elif higher_low and facts["above_vwap"]:
            execution = "HIGHER_LOW"
        elif facts["above_vwap"] and facts["amount_imbalance"] >= -0.15:
            execution = "VWAP_HOLD"
        elif facts["amount_imbalance"] <= -0.35:
            execution = "SELL_PRESSURE"
        else:
            execution = "WAIT_EXECUTION"
        state["previous_vwap_side"] = side
        return {"state": execution, "higher_low": higher_low, "vwap_side": side, "previous_vwap_side": prior_side}

    @staticmethod
    def _sector_ok(sector: Mapping[str, Any]) -> bool:
        local_ok = bool(
            sector.get("state") in {"IGNITION", "EXPANSION", "HEALTHY_TREND"}
            or sector.get("entry_support")
            or _safe_float(sector.get("health_percentile")) >= 0.60
        )
        market_present = any(
            key in sector for key in (
                "market_board_rank", "market_board_percentile", "market_board_entry_support",
                "market_board_rotation_caution",
            )
        )
        market_ok = bool(
            not market_present
            or (
                sector.get("market_board_entry_support")
                and not sector.get("market_board_rotation_caution")
                and _safe_float(sector.get("market_board_percentile")) >= 0.55
            )
        )
        return bool(local_ok and market_ok)

    def update(
        self,
        observation: Mapping[str, Any],
        candidate: Mapping[str, Any],
        multitimeframe: Mapping[str, Any],
        sector: Optional[Mapping[str, Any]] = None,
        capital: Optional[Mapping[str, Any]] = None,
        continuation: Optional[Mapping[str, Any]] = None,
        market_permission: Optional[Mapping[str, Any]] = None,
    ) -> Dict[str, Any]:
        symbol = str(observation.get("symbol") or candidate.get("symbol") or "")
        ts = observation.get("event_ts")
        if isinstance(ts, pd.Timestamp):
            ts = ts.to_pydatetime()
        if not isinstance(ts, datetime):
            return {"status": "IGNORED", "reason": "缺少事件时间", "symbol": symbol}
        price = _safe_float(observation.get("price"))
        if not symbol or price <= 0:
            return {"status": "IGNORED", "reason": "价格或代码无效", "symbol": symbol}
        state = self.states[symbol]
        trade_date = ts.strftime("%Y-%m-%d")
        if state.get("trade_date") != trade_date:
            state.clear()
            state.update(self._new_state())
            state["trade_date"] = trade_date
        open_hint = _safe_float(observation.get("session_open_hint"), price)
        high_hint = max(price, _safe_float(observation.get("completed_bar_high"), price))
        low_hint = min(price, _safe_float(observation.get("completed_bar_low"), price))
        state["open"] = state["open"] or open_hint
        state["high"] = max(_safe_float(state.get("high")), high_hint)
        state["low"] = min(_safe_float(state.get("low"), price), low_hint)
        state["samples"].append({"event_ts": ts, "price": price})

        static = candidate.get("timing_static_context") or {}
        reference_close = _safe_float(static.get("reference_close"), _safe_float(candidate.get("close"), price))
        daily_atr = _safe_float(static.get("atr_abs"), _safe_float(candidate.get("atr14_pct"), 0.04) * reference_close)
        daily_atr = max(daily_atr, reference_close * 0.008)
        fifteen = (multitimeframe.get("periods") or {}).get("15") or {}
        tactical_atr = _safe_float(fifteen.get("atr14"))
        # 日内Path/Room必须使用决策时点已经闭合的15分钟ATR。只有15分钟尚未
        # 预热或量纲明显异常时才回退D-1日线ATR，并把口径留在日志中。
        if not (reference_close * 0.001 <= tactical_atr <= daily_atr * 1.5):
            tactical_atr = daily_atr
            atr_source = "D_MINUS_1_DAILY_ATR_FALLBACK"
        else:
            atr_source = "COMPLETED_M15_ATR14"
        atr = tactical_atr
        vwap = _safe_float(observation.get("vwap"))
        above_vwap = bool(vwap <= 0 or price >= vwap * 0.997)
        session_range = max(state["high"] - state["low"], atr * 0.05)
        slot = self._minute_slot(ts)
        profile_value = _safe_float(self.volume_profiles.get(symbol, {}).get(slot))
        cum_volume = _safe_float(observation.get("cum_volume"))
        rvol = cum_volume / profile_value if profile_value > 0 and cum_volume > 0 else None
        facts = {
            "price": price,
            "vwap": vwap,
            "atr": atr,
            "gap_atr": (state["open"] - reference_close) / atr,
            "open_to_now_atr": (price - state["open"]) / atr,
            "high_to_now_atr": (state["high"] - price) / atr,
            "clv": _clip((price - state["low"]) / session_range, 0.0, 1.0),
            "above_vwap": above_vwap,
            "amount_imbalance": _safe_float(observation.get("amount_imbalance")),
            "rvol": rvol,
            "atr_source": atr_source,
            "daily_atr_abs": daily_atr,
            "session_open": state["open"],
            "session_high": state["high"],
            "session_low": state["low"],
        }
        capital = capital or {}
        continuation = continuation or {}
        raw_path = self._classify_path(facts, capital)
        path, transition = self._stable_path(state, raw_path, ts)
        levels = self._dynamic_levels(candidate, multitimeframe)
        location = self._location(price, atr, levels)
        setup = self._setup_15m(multitimeframe, location, price)
        execution = self._execution(state, facts, ts)
        sector = sector or {}
        market_permission = market_permission or {}
        sector_ok = self._sector_ok(sector)
        market_open = market_permission.get("new_entry_permission") != "CLOSED"
        route = str(candidate.get("daily_route") or "INTRADAY_DISCOVERY")
        risk_30_60 = str(multitimeframe.get("divergence_30_60") or "NONE")
        bearish_30_60 = risk_30_60.startswith("BEARISH") or risk_30_60 == "BOTH_BEARISH"
        execution_ok = execution.get("state") in self.POSITIVE_EXECUTIONS
        room_open = location.get("room_atr", 0) >= self.thresholds.room_good_atr
        clv_high = facts["clv"] > 0.80
        common_research = bool(path not in self.FAILURE_PATHS and market_open and not bearish_30_60)
        setup_asof = str(setup.get("confirmed_bar_asof") or "")[:10]
        setup_today = bool(setup_asof == trade_date and setup.get("state") in self.POSITIVE_SETUPS)
        capital_ok = bool(
            not capital
            or (
                capital.get("phase") in {"AGGRESSIVE_INFLOW", "CONTROLLED_ADVANCE"}
                and _safe_float(capital.get("score"), 50.0) >= 60
                and str(capital.get("confidence") or "LOW") in {"MEDIUM", "HIGH"}
            )
        )
        continuation_ok = bool(not continuation or continuation.get("confirmed"))
        last_failure_at = state.get("last_failure_at")
        failure_cooldown = bool(
            isinstance(last_failure_at, datetime)
            and 0 <= (ts - last_failure_at).total_seconds() < self.thresholds.failure_cooldown_seconds
        )

        # 严禁再用一条AND链覆盖所有路线。当前历史仅支持把“高开承接+空间+高CLV”
        # 标为快速研究候选；趋势扩张、回踩和反转各自保留唯一缺口，不能写成齐备。
        route_status = "OBSERVE_INCOMPLETE"
        route_missing: List[str] = []
        route_ready = False
        if location.get("state") == "AT_RESISTANCE":
            route_status = "RESISTANCE_RISK_REVIEW"
            route_missing.append("离开压力区或确认有效突破接受")
        elif bearish_30_60:
            route_status = "HIGHER_TIMEFRAME_DIVERGENCE_REVIEW"
            route_missing.append("30/60分钟顶背离失效或被更强结构证据抵消")
        elif path in self.FAILURE_PATHS:
            route_status = "PATH_FAILED_COOLDOWN"
            route_missing.append("失败路径修复并重新形成承接")
        elif route == "TREND_CONTINUATION" and path == "GAP_HOLD":
            route_ready = bool(
                common_research and room_open and clv_high and execution_ok
                and setup_today and capital_ok and continuation_ok
                and (sector_ok if sector else True)
                and not failure_cooldown
            )
            route_status = "GAP_HOLD_FAST_TRACK_SHADOW" if route_ready else "GAP_HOLD_NEEDS_EVIDENCE"
            if not room_open:
                route_missing.append("Room>=2ATR")
            if not clv_high:
                route_missing.append("CLV>0.80")
            if not execution_ok:
                route_missing.append("逐笔/VWAP执行承接")
            if not setup_today:
                route_missing.append("当日已闭合15分钟Setup")
            if sector and not sector_ok:
                route_missing.append("全市场板块健康且非轮出")
            if not capital_ok:
                route_missing.append("资金持续接受")
            if not continuation_ok:
                route_missing.append("延续状态确认")
            if failure_cooldown:
                route_missing.append("失败Path冷却完成")
        elif path == "TREND_EXPANSION":
            route_status = "EXPANSION_NEEDS_PERSISTENCE"
            route_missing.append("扩张保持或首次健康回踩；首次冲高不直接升级")
        elif path == "ORDERLY_PULLBACK" or route in {"TREND_PULLBACK", "BOTTOM_REVERSAL", "REVERSAL"}:
            route_status = "PULLBACK_NEEDS_RECLAIM"
            route_missing.append("关键位/VWAP收复并持续承接")
        elif path == "BASE_BUILDING":
            route_status = "BASE_NEEDS_BREAK_ACCEPTANCE"
            route_missing.append("平台突破后保持或回踩不破")
        else:
            if not room_open:
                route_missing.append("上方可用空间")
            if not execution_ok:
                route_missing.append("逐笔执行证据")
            if sector and not sector_ok:
                route_missing.append("板块健康/持续")

        score_parts = {
            "room": 20 if location.get("room_atr", 0) >= 2 else (8 if location.get("room_atr", 0) >= 1 else -10),
            "path": 25 if path in self.POSITIVE_PATHS else (-25 if path in self.FAILURE_PATHS else 5),
            "setup15": 20 if setup.get("state") in self.POSITIVE_SETUPS else (-8 if setup.get("state") == "MOMENTUM_DIVERGENCE_CAUTION" else 0),
            "execution": 20 if execution.get("state") in self.POSITIVE_EXECUTIONS else (-10 if execution.get("state") == "SELL_PRESSURE" else 0),
            "sector": 10 if sector_ok else (0 if not sector else -5),
            "market": 5 if market_open else -20,
        }
        shadow_score = int(_clip(50 + sum(score_parts.values()) * 0.5, 0, 100))
        path_family = (
            "FAILURE" if path in self.FAILURE_PATHS
            else ("HEALTHY" if path in self.POSITIVE_PATHS else "MIXED")
        )
        previous_context = state.get("last_context") or {}
        material_before = {
            "path_family": previous_context.get("path_family"),
            "route_alignment_status": previous_context.get("route_alignment_status"),
            "shadow_entry_ready": previous_context.get("shadow_entry_ready"),
            "divergence_30_60": previous_context.get("divergence_30_60"),
        }
        material_after = {
            "path_family": path_family,
            "route_alignment_status": route_status,
            "shadow_entry_ready": bool(route_ready),
            "divergence_30_60": risk_30_60,
        }
        material_transition = None
        if material_before != material_after:
            material_transition = {
                "at": ts.isoformat(),
                "from": material_before,
                "to": material_after,
            }
        context = {
            "status": "READY" if self.mode != "OFF" else "OFF",
            "symbol": symbol,
            "event_ts": ts.isoformat(),
            "session": {"phase": _session_phase(ts), "trade_date": trade_date, "role": "CONTEXT_NOT_FIXED_TIME_TRIGGER"},
            "path": path,
            "path_family": path_family,
            "raw_path": raw_path,
            "path_transition": transition,
            "material_state_transition": material_transition,
            "location": location,
            "setup_15m": setup,
            "execution": execution,
            "divergence_30_60": risk_30_60,
            "structure_120m": multitimeframe.get("one_twenty_minute_structure_shadow") or {},
            "higher_timeframe_risk_shadow": bool(multitimeframe.get("higher_timeframe_risk_shadow")),
            "facts": {
                key: (round(value, 6) if isinstance(value, float) else value)
                for key, value in facts.items()
            },
            "route": route,
            "shadow_entry_ready": bool(route_ready),
            "route_alignment_status": route_status,
            "route_missing": route_missing,
            "route_specific": True,
            "failure_cooldown_active": failure_cooldown,
            "shadow_score": shadow_score,
            "score_parts": score_parts,
            "strategy_effect": "NONE_SHADOW_ZERO_WEIGHT",
            "rules_version": STRUCTURED_TIMING_VERSION,
            "no_lookahead": True,
            "decision_boundary": "D_MINUS_1_DAILY_PLUS_COMPLETED_MINUTE_BARS_PLUS_CURRENT_AND_PRIOR_TICKS",
            "rejected_shortcuts": ["NO_MA225_HARD_BLOCK", "NO_FIXED_1445_BAN", "NO_UNIVERSAL_EXTENSION_VETO", "NO_R0_ONE_SIZE_FITS_ALL"],
        }
        state["last_context"] = context
        return context

    def context_for(self, symbol: str) -> Dict[str, Any]:
        state = self.states.get(symbol)
        if state and state.get("last_context"):
            return dict(state["last_context"])
        return {"symbol": symbol, "status": "NO_TICK_CONTEXT", "strategy_effect": "NONE_SHADOW_ZERO_WEIGHT"}

    def snapshot(self) -> Dict[str, Any]:
        rows = [self.context_for(symbol) for symbol in sorted(self.states)]
        return {"rows": rows, "by_symbol": {row.get("symbol"): row for row in rows}}


def format_structured_timing_line(context: Mapping[str, Any]) -> str:
    if not context or context.get("status") not in {"READY", "OFF"}:
        return "结构择时：尚无盘中上下文"
    location = context.get("location") or {}
    setup = context.get("setup_15m") or {}
    execution = context.get("execution") or {}
    ready = (
        "GAP_HOLD快速研究候选"
        if context.get("shadow_entry_ready")
        else str(context.get("route_alignment_status") or "仍待条件补齐")
    )
    return (
        f"结构择时(零权重)：{context.get('path','—')}｜{location.get('state','—')} "
        f"Room { _safe_float(location.get('room_atr')):.2f}ATR｜15m {setup.get('state','—')}｜"
        f"执行 {execution.get('state','—')}｜30/60背离 {context.get('divergence_30_60','NONE')}｜"
        f"{ready} {int(_safe_float(context.get('shadow_score')))}分"
    )
