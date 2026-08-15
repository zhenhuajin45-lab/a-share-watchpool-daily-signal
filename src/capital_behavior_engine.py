# coding: utf-8
"""多周期资金行为解释引擎。

这里的“资金意图”是由价格、成交、盘口和板块共同支持的可检验假设，绝不把单个
盘口快照或某根K线描述成已经知道某个主力的真实想法。职责分成两层：

1. 日线/周线结构：识别吸收、再积累、趋势推进、派发风险和退潮；
2. Tick层：识别主动流入、受控推进、健康换手、卖压被吸收、混合以及持续流出。

所有计算只使用当前时点及更早数据；本模块不下单。
"""

from __future__ import annotations

import math
from collections import defaultdict, deque
from datetime import datetime
from typing import Any, Deque, Dict, List, Mapping, Optional

import numpy as np
import pandas as pd


CAPITAL_BEHAVIOR_VERSION = "capital_behavior_v2_persistent_multihorizon"


def _safe_float(value: Any, default: float = 0.0) -> float:
    try:
        result = float(value)
        return result if math.isfinite(result) else default
    except (TypeError, ValueError):
        return default


def _clip(value: float, low: float, high: float) -> float:
    return max(low, min(high, value))


def _as_datetime(value: Any) -> datetime:
    if isinstance(value, datetime):
        return value.replace(tzinfo=None) if value.tzinfo else value
    try:
        parsed = datetime.fromisoformat(str(value or "").replace("Z", "+00:00"))
        return parsed.replace(tzinfo=None) if parsed.tzinfo else parsed
    except (TypeError, ValueError):
        return datetime.now()


def _unknown_structure(reason: str) -> Dict[str, Any]:
    return {
        "status": "UNAVAILABLE",
        "phase": "UNKNOWN",
        "phase_cn": "历史数据不足",
        "score": 50,
        "confidence": "LOW",
        "intent_hypothesis": "暂不判断中期资金阶段",
        "evidence": [],
        "risks": [reason],
        "entry_prior": "NEUTRAL",
        "new_entry_role": "LIVE_CONFIRM_REQUIRED",
        "holding_role": "NEUTRAL",
        "no_lookahead": True,
        "rules_version": CAPITAL_BEHAVIOR_VERSION,
    }


def analyze_structural_capital(frame: Optional[pd.DataFrame]) -> Dict[str, Any]:
    """从截至D-1的日线构建20/60日和周线资金阶段先验。"""

    if frame is None or len(frame) < 30:
        return _unknown_structure("至少需要30根完整日线")
    required = {"close", "high", "low", "volume"}
    if not required.issubset(frame.columns):
        return _unknown_structure("缺少价格或成交量字段")

    data = frame.copy()
    if "eob" in data:
        data["eob"] = pd.to_datetime(data["eob"], errors="coerce")
        data = data.dropna(subset=["eob"]).sort_values("eob")
    for column in required | {"open"}:
        if column in data:
            data[column] = pd.to_numeric(data[column], errors="coerce")
    data = data.dropna(subset=list(required)).tail(160).reset_index(drop=True)
    if len(data) < 30:
        return _unknown_structure("清洗后完整日线不足30根")

    close = data["close"].astype(float)
    high = data["high"].astype(float)
    low = data["low"].astype(float)
    volume = data["volume"].astype(float).clip(lower=0)
    returns = close.pct_change().fillna(0.0)
    spread = (high - low).replace(0, np.nan)
    clv = ((2.0 * close - high - low) / spread).replace([np.inf, -np.inf], np.nan).fillna(0.0).clip(-1, 1)
    tail20 = data.index[-20:]
    tail60 = data.index[-60:] if len(data) >= 60 else data.index
    volume20 = volume.loc[tail20]
    cmf20 = float((clv.loc[tail20] * volume20).sum() / max(volume20.sum(), 1.0))
    signed_volume20 = float((np.sign(returns.loc[tail20]) * volume20).sum() / max(volume20.sum(), 1.0))
    up_volume = float(volume20[returns.loc[tail20] > 0].sum())
    down_volume = float(volume20[returns.loc[tail20] < 0].sum())
    up_down_ratio = up_volume / max(down_volume, 1.0)
    ma20 = float(close.tail(20).mean())
    ma60 = float(close.tail(60).mean()) if len(close) >= 60 else float(close.mean())
    return20 = float(close.iloc[-1] / close.iloc[-20] - 1.0) if len(close) >= 20 and close.iloc[-20] > 0 else 0.0
    return60 = float(close.iloc[-1] / close.iloc[-60] - 1.0) if len(close) >= 60 and close.iloc[-60] > 0 else return20
    path20 = float(returns.tail(20).abs().sum())
    trend_efficiency20 = return20 / path20 if path20 > 0 else 0.0
    range_high60 = float(high.loc[tail60].max())
    range_low60 = float(low.loc[tail60].min())
    range_position60 = (
        float((close.iloc[-1] - range_low60) / (range_high60 - range_low60))
        if range_high60 > range_low60 else 0.5
    )
    volume_ma20 = volume.rolling(20, min_periods=5).mean()
    active = volume >= volume_ma20
    accumulation_days = int(((returns > 0) & active).tail(20).sum())
    distribution_days = int(((returns < 0) & active).tail(20).sum())
    volume_5_to_20 = float(volume.tail(5).mean() / max(volume.tail(20).mean(), 1.0))

    weekly_return8 = 0.0
    weekly_flow8 = 0.0
    if "eob" in data and len(data) >= 40:
        weekly_aggregation = {"high": "max", "low": "min", "close": "last", "volume": "sum"}
        if "open" in data.columns:
            weekly_aggregation["open"] = "first"
        weekly = (
            data.set_index("eob")
            .resample("W-FRI")
            .agg(weekly_aggregation)
            .dropna(subset=["close"])
        )
        if len(weekly) >= 8:
            weekly_return8 = float(weekly["close"].iloc[-1] / weekly["close"].iloc[-8] - 1.0)
            weekly_ret = weekly["close"].pct_change().fillna(0.0).tail(8)
            weekly_vol = weekly["volume"].tail(8).clip(lower=0)
            weekly_flow8 = float((np.sign(weekly_ret) * weekly_vol).sum() / max(weekly_vol.sum(), 1.0))

    score = 50.0
    score += _clip(cmf20 * 42.0, -16.0, 16.0)
    score += _clip(signed_volume20 * 34.0, -13.0, 13.0)
    score += _clip(math.log(max(up_down_ratio, 0.05)) * 7.5, -11.0, 11.0)
    score += _clip((accumulation_days - distribution_days) * 2.2, -11.0, 11.0)
    score += 6.0 if close.iloc[-1] > ma20 else -6.0
    score += 5.0 if ma20 > ma60 else -4.0
    score += _clip(weekly_flow8 * 16.0, -7.0, 7.0)
    score = int(round(_clip(score, 0.0, 100.0)))

    trend_up = bool(close.iloc[-1] > ma20 > ma60 and return20 > 0)
    trend_down = bool(close.iloc[-1] < ma20 < ma60 and return20 < 0)
    flow_positive = bool(
        (cmf20 >= 0.05 or signed_volume20 >= 0.10 or up_down_ratio >= 1.25)
        and signed_volume20 >= -0.02
        and accumulation_days >= distribution_days
    )
    flow_negative = bool(
        (cmf20 <= -0.05 or signed_volume20 <= -0.10 or up_down_ratio <= 0.80)
        and signed_volume20 < 0
        and distribution_days > accumulation_days
    )
    high_zone = range_position60 >= 0.75
    if trend_up and flow_positive and score >= 68:
        phase, phase_cn = "MARKUP", "趋势推进"
    elif close.iloc[-1] >= ma20 and ma20 >= ma60 and flow_positive:
        phase, phase_cn = "REACCUMULATION", "趋势中再积累"
    elif not trend_down and flow_positive and range_position60 <= 0.72:
        phase, phase_cn = "ACCUMULATION", "吸收/积累"
    elif high_zone and flow_negative:
        phase, phase_cn = "DISTRIBUTION_RISK", "高位派发风险"
    elif trend_down and flow_negative:
        phase, phase_cn = "MARKDOWN", "退潮下行"
    elif high_zone and distribution_days >= accumulation_days + 2 and cmf20 < 0:
        phase, phase_cn = "DISTRIBUTION_RISK", "高位派发风险"
    else:
        phase, phase_cn = "BALANCED", "多空平衡"

    evidence: List[str] = []
    risks: List[str] = []
    evidence.append(f"20日资金流位置{cmf20:+.2f}，上涨/下跌成交量比{up_down_ratio:.2f}")
    evidence.append(f"20日放量阳/阴日{accumulation_days}/{distribution_days}，量能5/20比{volume_5_to_20:.2f}")
    evidence.append(f"20/60日涨幅{return20:+.1%}/{return60:+.1%}，60日区间位置{range_position60:.0%}")
    if weekly_return8 or weekly_flow8:
        evidence.append(f"8周涨幅{weekly_return8:+.1%}，周线方向成交量{weekly_flow8:+.2f}")
    if phase in {"DISTRIBUTION_RISK", "MARKDOWN"}:
        risks.append("中期资金结构偏弱，日内强势需要更高质量持续流入才能修复")
    if high_zone and cmf20 < 0:
        risks.append("价格处于60日高位但20日资金流为负，存在价量背离")
    if volume_5_to_20 >= 1.8 and return20 < 0:
        risks.append("近期显著放量但价格未同步走强")

    intent = {
        "MARKUP": "中期资金更像持续推进，优先判断日内流入能否延续",
        "REACCUMULATION": "中期趋势仍在，近期更像换手后重新聚集",
        "ACCUMULATION": "资金更像在区间内吸收筹码，等待价格确认",
        "DISTRIBUTION_RISK": "高位成交更像分歧或派发，需要防止把脉冲误判为持续进攻",
        "MARKDOWN": "中期资金偏撤离，单日日内反弹暂按修复看待",
        "BALANCED": "尚未形成稳定的中期资金方向",
    }[phase]
    # 历史分层显示“结构分高”不等于新开仓收益更高：MARKUP尤其容易对应已经伸展的
    # 位置。因此结构只定义场景职责，不再把分数机械解释成买入优势。
    if phase == "MARKUP":
        entry_prior, new_entry_role, holding_role = "CAUTION", "FLOW_AND_LOCATION_CONFIRM", "HOLD_SUPPORT"
    elif phase in {"REACCUMULATION", "ACCUMULATION"}:
        entry_prior, new_entry_role, holding_role = "SETUP", "LIVE_BREAKOUT_CONFIRM", "HOLD_SUPPORT"
    elif phase in {"DISTRIBUTION_RISK", "MARKDOWN"}:
        entry_prior, new_entry_role, holding_role = "CAUTION", "REPAIR_REQUIRED", "PROTECT"
    else:
        entry_prior, new_entry_role, holding_role = "NEUTRAL", "LIVE_CONFIRM_REQUIRED", "NEUTRAL"
    return {
        "status": "READY",
        "phase": phase,
        "phase_cn": phase_cn,
        "score": score,
        "confidence": "HIGH" if len(data) >= 80 else ("MEDIUM" if len(data) >= 50 else "LOW"),
        "intent_hypothesis": intent,
        "entry_prior": entry_prior,
        "new_entry_role": new_entry_role,
        "holding_role": holding_role,
        "historical_entry_edge": "NOT_INFERRED_FROM_STRUCTURE_SCORE",
        "cmf20": cmf20,
        "signed_volume20": signed_volume20,
        "up_down_volume_ratio20": up_down_ratio,
        "accumulation_days20": accumulation_days,
        "distribution_days20": distribution_days,
        "return20": return20,
        "return60": return60,
        "trend_efficiency20": trend_efficiency20,
        "range_position60": range_position60,
        "volume_5_to_20": volume_5_to_20,
        "weekly_return8": weekly_return8,
        "weekly_flow8": weekly_flow8,
        "evidence": evidence,
        "risks": risks,
        "no_lookahead": True,
        "rules_version": CAPITAL_BEHAVIOR_VERSION,
    }


class CapitalBehaviorEngine:
    """从Tick持续更新日内订单流代理，并与D-1结构和板块状态融合。"""

    def __init__(self):
        self.states: Dict[str, Dict[str, Any]] = defaultdict(self._new_state)

    @staticmethod
    def _new_state() -> Dict[str, Any]:
        return {
            "trade_date": None,
            "samples": deque(maxlen=3600),
            "context_history": deque(maxlen=1200),
            "last_context": {},
        }

    @staticmethod
    def _window(samples: Deque[Dict[str, Any]], now: datetime, seconds: int) -> List[Dict[str, Any]]:
        result = []
        for row in reversed(samples):
            elapsed = (now - row["event_ts"]).total_seconds()
            if elapsed < 0:
                continue
            if elapsed > seconds:
                break
            result.append(row)
        result.reverse()
        return result

    @staticmethod
    def _quote_ofi(current: Mapping[str, Any], previous: Optional[Mapping[str, Any]]) -> float:
        if not previous:
            return 0.0
        current_quotes = current.get("quotes") or []
        previous_quotes = previous.get("quotes") or []
        weighted_flow = 0.0
        depth = 0.0
        for index in range(min(5, len(current_quotes), len(previous_quotes))):
            row = current_quotes[index] or {}
            old = previous_quotes[index] or {}
            bid_p, bid_v = _safe_float(row.get("bid_p")), _safe_float(row.get("bid_v"))
            ask_p, ask_v = _safe_float(row.get("ask_p")), _safe_float(row.get("ask_v"))
            old_bid_p, old_bid_v = _safe_float(old.get("bid_p")), _safe_float(old.get("bid_v"))
            old_ask_p, old_ask_v = _safe_float(old.get("ask_p")), _safe_float(old.get("ask_v"))
            if bid_p <= 0 and ask_p <= 0:
                continue
            bid_flow = (bid_v if bid_p >= old_bid_p else 0.0) - (old_bid_v if bid_p <= old_bid_p else 0.0)
            ask_flow = -(ask_v if ask_p <= old_ask_p else 0.0) + (old_ask_v if ask_p >= old_ask_p else 0.0)
            weight = 1.0 / math.sqrt(index + 1.0)
            weighted_flow += weight * (bid_flow + ask_flow)
            depth += weight * max((bid_v + ask_v + old_bid_v + old_ask_v) / 2.0, 0.0)
        return _clip(weighted_flow / depth, -2.0, 2.0) if depth > 0 else 0.0

    @staticmethod
    def _trade_sign(current: Mapping[str, Any], previous: Optional[Mapping[str, Any]]) -> float:
        if not previous:
            return 0.0
        price = _safe_float(current.get("price"))
        bid = _safe_float(current.get("bid1_price"))
        ask = _safe_float(current.get("ask1_price"))
        if bid > 0 and ask > 0:
            mid = (bid + ask) / 2.0
            if price > mid:
                return 1.0
            if price < mid:
                return -1.0
        previous_price = _safe_float(previous.get("price"))
        return 1.0 if price > previous_price else (-1.0 if price < previous_price else 0.0)

    def _append(self, observation: Mapping[str, Any]) -> Optional[Dict[str, Any]]:
        symbol = str(observation.get("symbol") or "")
        if not symbol:
            return None
        ts = _as_datetime(observation.get("event_ts"))
        state = self.states[symbol]
        trade_date = ts.strftime("%Y-%m-%d")
        if state.get("trade_date") != trade_date:
            state.clear()
            state.update(self._new_state())
            state["trade_date"] = trade_date
        samples: Deque[Dict[str, Any]] = state["samples"]
        if samples and ts < samples[-1]["event_ts"]:
            return None
        same_second = bool(samples and ts.replace(microsecond=0) == samples[-1]["event_ts"].replace(microsecond=0))
        previous = samples[-2] if same_second and len(samples) >= 2 else (samples[-1] if samples else None)
        row = dict(observation)
        row["event_ts"] = ts
        current_cum_volume = _safe_float(row.get("cum_volume"))
        previous_cum_volume = _safe_float((previous or {}).get("cum_volume"))
        delta_volume = current_cum_volume - previous_cum_volume if current_cum_volume >= previous_cum_volume else 0.0
        row["delta_volume"] = max(delta_volume, 0.0)
        row["trade_sign"] = self._trade_sign(row, previous)
        row["signed_volume"] = row["trade_sign"] * row["delta_volume"]
        row["quote_ofi"] = self._quote_ofi(row, previous)
        if same_second:
            samples[-1] = row
        else:
            samples.append(row)
        return row

    @staticmethod
    def _momentum(rows: List[Dict[str, Any]]) -> float:
        if len(rows) < 2 or _safe_float(rows[0].get("price")) <= 0:
            return 0.0
        return _safe_float(rows[-1].get("price")) / _safe_float(rows[0].get("price")) - 1.0

    @staticmethod
    def _window_metrics(rows: List[Dict[str, Any]]) -> Dict[str, Any]:
        if not rows:
            return {
                "span_seconds": 0.0, "signed_trade_ratio": 0.0, "quote_ofi": 0.0,
                "mean_depth_imbalance": 0.0, "positive_depth_ratio": 0.0,
                "above_vwap_ratio": 0.0, "momentum": 0.0, "observed_volume": 0.0,
            }
        span = (rows[-1]["event_ts"] - rows[0]["event_ts"]).total_seconds() if len(rows) >= 2 else 0.0
        volume = sum(_safe_float(row.get("delta_volume")) for row in rows)
        signed = sum(_safe_float(row.get("signed_volume")) for row in rows)
        ofi_values = [_safe_float(row.get("quote_ofi")) for row in rows]
        depth_values = [
            _safe_float(row.get("amount_imbalance"))
            for row in rows if row.get("amount_imbalance") is not None
        ]
        vwap_rows = [row for row in rows if _safe_float(row.get("vwap")) > 0]
        return {
            "span_seconds": span,
            "signed_trade_ratio": signed / volume if volume > 0 else 0.0,
            "quote_ofi": float(np.mean(ofi_values)) if ofi_values else 0.0,
            "mean_depth_imbalance": float(np.mean(depth_values)) if depth_values else 0.0,
            "positive_depth_ratio": (
                sum(value >= 0 for value in depth_values) / len(depth_values) if depth_values else 0.5
            ),
            "above_vwap_ratio": (
                sum(_safe_float(row.get("price")) >= _safe_float(row.get("vwap")) for row in vwap_rows) / len(vwap_rows)
                if vwap_rows else 0.5
            ),
            "momentum": CapitalBehaviorEngine._momentum(rows),
            "observed_volume": volume,
        }

    def update(
        self,
        observation: Mapping[str, Any],
        candidate: Mapping[str, Any],
        sector: Optional[Mapping[str, Any]] = None,
        multitimeframe: Optional[Mapping[str, Any]] = None,
    ) -> Dict[str, Any]:
        current = self._append(observation)
        symbol = str(observation.get("symbol") or "")
        if current is None:
            return self.states[symbol].get("last_context") or {"status": "WAITING", "score": 50}
        state = self.states[symbol]
        samples: Deque[Dict[str, Any]] = state["samples"]
        now = current["event_ts"]
        short = self._window_metrics(self._window(samples, now, 30))
        medium = self._window_metrics(self._window(samples, now, 180))
        broad = self._window_metrics(self._window(samples, now, 600))
        structure = candidate.get("capital_structure") or _unknown_structure("候选未携带D-1资金结构")
        sector = sector or {}
        multitimeframe = multitimeframe or {}

        micro = 50.0
        micro += _clip(medium["signed_trade_ratio"] * 24.0, -16.0, 16.0)
        micro += _clip(medium["quote_ofi"] * 22.0, -14.0, 14.0)
        micro += _clip(medium["mean_depth_imbalance"] * 14.0, -9.0, 9.0)
        micro += _clip((medium["above_vwap_ratio"] - 0.5) * 24.0, -12.0, 12.0)
        micro += _clip(medium["momentum"] * 900.0, -12.0, 12.0)
        micro = int(round(_clip(micro, 0.0, 100.0)))

        span = medium["span_seconds"]
        confidence = "HIGH" if span >= 150 and len(samples) >= 25 else ("MEDIUM" if span >= 60 else "LOW")
        price_holding = medium["momentum"] >= -0.0015 and medium["above_vwap_ratio"] >= 0.55
        selling_absorbed = bool(
            medium["signed_trade_ratio"] <= -0.08
            and price_holding
            and medium["quote_ofi"] >= -0.05
        )
        aggressive_inflow = bool(
            medium["signed_trade_ratio"] >= 0.10
            and medium["quote_ofi"] >= 0.04
            and medium["momentum"] >= 0.001
            and medium["above_vwap_ratio"] >= 0.65
        )
        confirmed_outflow = bool(
            medium["signed_trade_ratio"] <= -0.12
            and medium["quote_ofi"] <= -0.06
            and medium["momentum"] <= -0.002
            and medium["above_vwap_ratio"] <= 0.45
        )
        controlled_advance = bool(
            medium["momentum"] > 0
            and medium["above_vwap_ratio"] >= 0.65
            and medium["signed_trade_ratio"] >= -0.05
            and micro >= 62
        )
        if confidence == "LOW":
            phase, phase_cn = "OBSERVING", "样本积累中"
        elif confirmed_outflow:
            phase, phase_cn = "CONFIRMED_OUTFLOW", "持续流出"
        elif aggressive_inflow:
            phase, phase_cn = "AGGRESSIVE_INFLOW", "主动进攻"
        elif selling_absorbed:
            phase, phase_cn = "SELLING_ABSORBED", "卖压被吸收"
        elif controlled_advance:
            phase, phase_cn = "CONTROLLED_ADVANCE", "受控推进"
        elif abs(medium["momentum"]) <= 0.0025 and 0.42 <= medium["above_vwap_ratio"] <= 0.72:
            phase, phase_cn = "HEALTHY_ROTATION", "健康换手/平衡"
        elif micro <= 40:
            phase, phase_cn = "OUTFLOW_WARNING", "流出风险"
        else:
            phase, phase_cn = "MIXED", "资金分歧"

        local_sector_score = _safe_float(sector.get("score"), 50.0 if sector.get("state") == "UNAVAILABLE" else 35.0)
        market_percentile = _safe_float(sector.get("market_board_percentile"), float("nan"))
        market_health_score = _safe_float(sector.get("market_board_health_score"), 50.0)
        market_sector_state = str(sector.get("market_board_state") or "UNAVAILABLE")
        emerging_leader_supported = bool(
            market_sector_state == "ROTATION_IN"
            and math.isfinite(market_percentile)
            and market_percentile >= 0.85
            and _safe_float(sector.get("market_board_breadth"), 0.5) >= 0.55
            and _safe_float(sector.get("market_board_persistence"), 0.0) >= 0.30
        )
        if math.isfinite(market_percentile):
            market_sector_score = 0.65 * market_health_score + 35.0 * market_percentile
            sector_score = 0.75 * market_sector_score + 0.25 * local_sector_score
        else:
            sector_score = min(local_sector_score, 55.0)
        mtf_score = _safe_float(multitimeframe.get("score"), 50.0)
        structure_score = _safe_float(structure.get("score"), 50.0)
        composite = int(round(_clip(0.55 * micro + 0.25 * structure_score + 0.12 * sector_score + 0.08 * mtf_score, 0, 100)))
        structural_risk = str(structure.get("phase")) in {"DISTRIBUTION_RISK", "MARKDOWN"}
        sector_risk = bool(
            str(sector.get("state") or "") in {"DECAY"}
            or str(sector.get("role") or "") == "LAGGARD"
            or market_sector_state in {"ROTATION_OUT", "WEAK"}
            or (bool(sector.get("market_board_rotation_caution")) and not emerging_leader_supported)
        )
        market_sector_support = bool(
            math.isfinite(market_percentile)
            and market_percentile >= 0.68
            and (sector.get("market_board_entry_support") or emerging_leader_supported)
            and market_sector_state not in {"FLASH_HEAT", "ROTATION_OUT", "WEAK"}
        )
        mtf_risk = bool(
            multitimeframe.get("alignment") == "BEARISH_2_OF_3"
            or int(_safe_float(multitimeframe.get("bearish_count"))) >= 2
        )
        context_history: Deque[Dict[str, Any]] = state["context_history"]
        raw_context = {"event_ts": now, "phase": phase, "score": composite}
        if context_history and context_history[-1]["event_ts"] == now:
            context_history[-1] = raw_context
        else:
            context_history.append(raw_context)
        recent_context = [
            row for row in context_history
            if 0 <= (now - row["event_ts"]).total_seconds() <= 60
        ]
        persistence_span = (
            (recent_context[-1]["event_ts"] - recent_context[0]["event_ts"]).total_seconds()
            if len(recent_context) >= 2 else 0.0
        )
        persistence_ready = len(recent_context) >= 4 and persistence_span >= 45
        constructive = [
            row for row in recent_context
            if row["phase"] in {"AGGRESSIVE_INFLOW", "CONTROLLED_ADVANCE", "SELLING_ABSORBED"}
            and row["score"] >= 58
        ]
        active_inflow = [
            row for row in recent_context
            if row["phase"] in {"AGGRESSIVE_INFLOW", "CONTROLLED_ADVANCE"} and row["score"] >= 64
        ]
        outflow = [
            row for row in recent_context
            if row["phase"] in {"CONFIRMED_OUTFLOW", "OUTFLOW_WARNING"} and row["score"] <= 44
        ]
        sample_count = max(len(recent_context), 1)
        constructive_ratio = len(constructive) / sample_count
        active_inflow_ratio = len(active_inflow) / sample_count
        outflow_ratio = len(outflow) / sample_count
        if persistence_ready and phase == "CONFIRMED_OUTFLOW" and outflow_ratio >= 0.50:
            regime, regime_cn = "PERSISTENT_OUTFLOW", "持续撤离确认"
        elif persistence_ready and constructive_ratio >= 0.60 and active_inflow_ratio >= 0.20:
            regime, regime_cn = "INFLOW_ACCEPTANCE", "持续承接/推进"
        elif persistence_ready and constructive_ratio >= 0.60:
            regime, regime_cn = "ABSORPTION", "持续吸收卖压"
        elif phase == "HEALTHY_ROTATION":
            regime, regime_cn = "BALANCED_ROTATION", "平衡换手"
        elif confidence == "LOW":
            regime, regime_cn = "WARMING_UP", "样本积累中"
        else:
            regime, regime_cn = "UNSTABLE", "证据尚不稳定"
        entry_support = bool(
            confidence != "LOW"
            and persistence_ready
            and constructive_ratio >= 0.50
            and phase in {"AGGRESSIVE_INFLOW", "CONTROLLED_ADVANCE", "SELLING_ABSORBED"}
            and composite >= 60
            and not confirmed_outflow
            and not (structural_risk and structure_score < 42)
            and not sector_risk
            and market_sector_support
            and not mtf_risk
        )
        flow_persistence_confirmed = bool(
            confidence == "HIGH"
            and persistence_ready
            and phase in {"AGGRESSIVE_INFLOW", "CONTROLLED_ADVANCE"}
            and composite >= 66
            and constructive_ratio >= 0.60
            and active_inflow_ratio >= 0.20
            and outflow_ratio <= 0.20
            and str(structure.get("phase")) not in {"DISTRIBUTION_RISK", "MARKDOWN"}
            and not sector_risk
            and market_sector_support
            and not mtf_risk
        )
        continuation_acceleration = bool(
            flow_persistence_confirmed
            and market_sector_state in {"SUSTAINED_LEADER", "ROTATION_IN", "HEALTHY_RISING"}
            and _safe_float(multitimeframe.get("score")) >= 72
        )

        intent = {
            "OBSERVING": "数据窗口尚短，只观察不推断持续意图",
            "AGGRESSIVE_INFLOW": "成交方向、盘口补单与价格推进共同指向主动资金进入",
            "CONTROLLED_ADVANCE": "价格在VWAP上方推进且卖压有限，更像有节奏的持续买入",
            "SELLING_ABSORBED": "存在主动卖量但价格未明显下移，更像下方承接在吸收抛压",
            "HEALTHY_ROTATION": "价格与成交暂时平衡，更像换手而非明确进攻或撤离",
            "CONFIRMED_OUTFLOW": "成交方向、盘口和价格共同转弱，更像资金持续撤离",
            "OUTFLOW_WARNING": "多项微观证据偏弱，但尚未形成完整流出确认",
            "MIXED": "成交、盘口与价格方向不一致，暂不强行解释单一意图",
        }[phase]
        evidence = [
            f"180秒主动成交代理{medium['signed_trade_ratio']:+.2f}，多档OFI {medium['quote_ofi']:+.2f}",
            f"180秒站上VWAP {medium['above_vwap_ratio']:.0%}，价格变化{medium['momentum']:+.2%}",
            f"30秒/600秒资金分{int(round(50 + short['signed_trade_ratio'] * 25))}/{int(round(50 + broad['signed_trade_ratio'] * 25))}",
            f"大周期{structure.get('phase_cn', '未知')} {int(structure_score)}/100；全市场板块{market_sector_state} {int(sector_score)}/100",
        ]
        risks: List[str] = []
        if structural_risk:
            risks.append(f"大周期处于{structure.get('phase_cn')}，日内脉冲持续性要求提高")
        if sector_risk:
            risks.append("全市场板块轮出/快速轮动，或个股在合并池旁证中处于后排，资金信号降级")
        if mtf_risk:
            risks.append("至少两个分钟周期转弱，日内资金信号存在趋势冲突")
        if confidence == "LOW":
            risks.append("日内样本窗口不足60秒")
        context = {
            "status": "READY" if confidence != "LOW" else "WARMING_UP",
            "symbol": symbol,
            "asof": now.isoformat(),
            "phase": phase,
            "phase_cn": phase_cn,
            "regime": regime,
            "regime_cn": regime_cn,
            "score": composite,
            "micro_score": micro,
            "confidence": confidence,
            "intent_hypothesis": intent,
            "entry_support": entry_support,
            "flow_persistence_confirmed": flow_persistence_confirmed,
            "continuation_acceleration": continuation_acceleration,
            "hold_support": bool(phase not in {"CONFIRMED_OUTFLOW"} and composite >= 48),
            "risk_level": "HIGH" if confirmed_outflow else ("MEDIUM" if risks else "LOW"),
            "short_30s": short,
            "medium_180s": medium,
            "broad_600s": broad,
            "persistence_60s": {
                "ready": persistence_ready,
                "span_seconds": persistence_span,
                "sample_count": len(recent_context),
                "constructive_ratio": constructive_ratio,
                "active_inflow_ratio": active_inflow_ratio,
                "outflow_ratio": outflow_ratio,
            },
            "structure": structure,
            "evidence": evidence,
            "risks": risks,
            "no_lookahead": True,
            "intent_is_hypothesis_not_observed_fact": True,
            "data_scope": "TICK_PRICE_CUM_VOLUME_TOP5_PROXY_PLUS_D_MINUS_1_STRUCTURE",
            "rules_version": CAPITAL_BEHAVIOR_VERSION,
        }
        state["last_context"] = context
        return context

    def snapshot(self) -> Dict[str, Any]:
        rows = [
            state.get("last_context") for state in self.states.values()
            if state.get("last_context")
        ]
        rows.sort(key=lambda row: (-int(_safe_float(row.get("score"))), str(row.get("symbol"))))
        return {"rows": rows, "by_symbol": {row.get("symbol"): row for row in rows}}
