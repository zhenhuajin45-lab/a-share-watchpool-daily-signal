# coding: utf-8
"""强趋势延续性分析器。

目标不是把“涨得高”直接翻译成买入，而是依次验证：

1. D-1趋势本身是否健康，而非单纯短期透支；
2. 集合竞价价格路径和同板块共振是否支持资金续接；
3. 开盘后是否真实站稳VWAP、维持板块梯队并出现承接/再加速；
4. 当前价格是否仍处于可执行位置，而不是已经远离当日成本中枢。

所有状态只使用当前Tick及更早数据。集合竞价数据是公开报价代理，不能独立确认买点；
除多源价格冲突外，竞价弱势可以被盘中真实承接修复。
"""

from __future__ import annotations

import math
from collections import defaultdict, deque
from dataclasses import dataclass
from datetime import datetime, time as dt_time
from typing import Any, Deque, Dict, List, Mapping, Optional


def _safe_float(value: Any, default: float = 0.0) -> float:
    try:
        result = float(value)
        return result if math.isfinite(result) else default
    except (TypeError, ValueError):
        return default


def _as_datetime(value: Any) -> datetime:
    if isinstance(value, datetime):
        return value.replace(tzinfo=None) if value.tzinfo else value
    try:
        result = datetime.fromisoformat(str(value or "").replace("Z", "+00:00"))
        return result.replace(tzinfo=None) if result.tzinfo else result
    except (TypeError, ValueError):
        return datetime.now()


def _continuous_session(ts: datetime) -> bool:
    now = ts.time()
    return dt_time(9, 30) <= now <= dt_time(11, 30) or dt_time(13, 0) <= now <= dt_time(15, 0)


@dataclass(frozen=True)
class ContinuationConfig:
    min_observation_seconds: int = 120
    short_window_seconds: int = 60
    acceptance_window_seconds: int = 180
    min_above_vwap_ratio: float = 0.65
    min_confirm_score: int = 72
    min_confirmation_persistence_seconds: int = 45
    min_daily_quality: int = 55
    min_auction_quality: int = 30
    max_limit_gap: float = 0.095


DEFAULT_CONFIG = ContinuationConfig()


class TrendContinuationAnalyzer:
    """逐Tick生成趋势延续阶段、证据、缺失条件和综合分。"""

    def __init__(self, config: ContinuationConfig = DEFAULT_CONFIG):
        self.config = config
        self.states: Dict[str, Dict[str, Any]] = defaultdict(self._new_state)

    @staticmethod
    def _new_state() -> Dict[str, Any]:
        return {
            "trade_date": None,
            "samples": deque(maxlen=1800),
            "open_price": 0.0,
            "session_high": 0.0,
            "session_low": float("inf"),
            "support_tested": False,
            "support_reclaimed": False,
            "support_tested_at": None,
            "support_reclaimed_at": None,
            "confirmation_candidate_since": None,
            "accepted_once": False,
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
    def _momentum(rows: List[Dict[str, Any]]) -> float:
        if len(rows) < 2 or _safe_float(rows[0].get("price")) <= 0:
            return 0.0
        return _safe_float(rows[-1].get("price")) / _safe_float(rows[0].get("price")) - 1.0

    @staticmethod
    def _daily_quality(candidate: Mapping[str, Any]) -> Dict[str, Any]:
        score = _safe_float(candidate.get("signal_strength"), 40.0)
        factors = []
        atr = max(_safe_float(candidate.get("atr14_pct"), 0.05), 0.015)
        return_5d = _safe_float(candidate.get("return_5d"))
        stretch = return_5d / atr
        if candidate.get("daily_route") == "TREND_CONTINUATION":
            factors.append("D-1属于趋势延续路线")
        else:
            score -= 20
            factors.append("不是趋势延续路线")
        if candidate.get("slow_confirmed"):
            score += 5
            factors.append("慢线方向仍向上")
        if candidate.get("macd_improving"):
            score += 5
            factors.append("MACD动能改善")
        divergence = str(candidate.get("macd_divergence") or "NONE")
        if divergence == "BEARISH":
            score -= 28
            factors.append("存在已确认MACD顶背离")
        if stretch > 5.0:
            score -= 22
            factors.append("5日涨幅相对ATR严重透支")
        elif stretch > 3.0:
            score -= 10
            factors.append("5日涨幅相对ATR偏高")
        elif stretch > 0:
            factors.append("5日涨幅仍处于可解释波动范围")
        slow_j = _safe_float(candidate.get("slow_j"))
        if slow_j >= 85:
            score -= 8
            factors.append("慢J极高，承接要求提高")
        elif slow_j >= 60:
            score -= 2
            factors.append("慢J处于保护区但不机械否决")
        if candidate.get("price_extreme"):
            score -= 18
            factors.append("D-1价格极端延伸")
        return {
            "score": max(0, min(100, int(round(score)))),
            "stretch_atr": stretch,
            "factors": factors,
        }

    @staticmethod
    def _auction_quality(auction: Mapping[str, Any]) -> Dict[str, Any]:
        if auction.get("hard_veto") or auction.get("gate") == "HARD_VETO":
            return {"score": 0, "state": "DATA_CONFLICT", "hard_veto": True, "factors": ["竞价多源价格冲突"]}
        score = 45.0
        factors = []
        if auction.get("label") == "DATA_CONFLICT_LIVE_REPAIR":
            score -= 15
            factors.append("竞价代理多源不同步，只允许盘中真实承接修复")
        gap = _safe_float(auction.get("final_gap"), float("nan"))
        retreat = _safe_float(auction.get("price_retreat"))
        bid_drop = _safe_float(auction.get("bid_drop_ratio"), float("nan"))
        locked_gap_change = _safe_float(auction.get("locked_gap_change"), float("nan"))
        imbalance = _safe_float(auction.get("final_amount_imbalance"), float("nan"))
        group = auction.get("group_context") or {}
        group_confirmed = bool(group.get("group_confirmed"))

        if math.isfinite(gap):
            if -0.015 <= gap <= 0.03:
                score += 12
                factors.append("竞价涨跌幅处于可承接区")
            elif 0.03 < gap < 0.095:
                score += 7 if group_confirmed else -3
                factors.append("高开得到板块共振" if group_confirmed else "高开仍需开盘承接")
            elif gap >= 0.095:
                score -= 5
                factors.append("接近涨停，需先确认真实可成交性")
            elif gap < -0.035:
                score -= 15
                factors.append("竞价大幅低开，需要强修复")
            else:
                score -= 4
                factors.append("竞价偏弱")
        else:
            score -= 8
            factors.append("竞价涨跌幅不可用")

        if retreat <= 0.005:
            score += 8
            factors.append("竞价价格保持稳定")
        elif retreat <= 0.012:
            score += 3
        elif retreat >= 0.025:
            score -= 12
            factors.append("竞价虚拟价格显著回落")
        else:
            score -= 5
            factors.append("竞价价格有所回落")

        if math.isfinite(bid_drop):
            if bid_drop <= 0.35:
                score += 5
            elif bid_drop >= 0.90:
                score -= 7
                factors.append("竞价买盘金额保持度偏弱")
        if math.isfinite(locked_gap_change):
            if locked_gap_change >= 0.003:
                score += 6
                factors.append("9:20后虚拟开盘价继续增强")
            elif locked_gap_change <= -0.01:
                score -= 9
                factors.append("9:20后虚拟开盘价明显回落")
        if math.isfinite(imbalance):
            if imbalance >= 0.15:
                score += 8
                factors.append("竞价盘口金额偏多")
            elif imbalance <= -0.35:
                score -= 8
                factors.append("竞价盘口金额偏空")
        if group_confirmed:
            score += 12
            factors.append(
                f"{group.get('theme','同主题')}竞价{group.get('positive_count',0)}/{group.get('member_count',0)}同步上涨"
            )
        if str(auction.get("confidence")) == "LOW":
            score -= 5
            factors.append("竞价代理置信度偏低")

        score = max(0, min(100, int(round(score))))
        state = "STRONG" if score >= 70 else ("SUPPORTIVE" if score >= 55 else ("CAUTION" if score >= 35 else "FRAGILE"))
        return {"score": score, "state": state, "hard_veto": False, "factors": factors}

    @staticmethod
    def _sector_quality(sector: Mapping[str, Any]) -> Dict[str, Any]:
        state = str(sector.get("state") or "UNAVAILABLE")
        raw = _safe_float(sector.get("score"), 0.0)
        # 合并池内部状态只占小权重；全市场动态健康度才是板块确认主证据。
        local_score = 45.0 if state == "UNAVAILABLE" else 50.0 + (raw - 50.0) * 0.25
        role = str(sector.get("role") or "UNCLASSIFIED")
        local_score += {"LEADER": 3, "FRONT": 2, "CORE": 2, "FOLLOWER": 0, "LAGGARD": -8}.get(role, -1)
        excess = _safe_float(sector.get("symbol_excess_vs_group"))
        local_score += 2 if excess >= 0.01 else (-4 if excess <= -0.015 else 0)
        market_board_pct = _safe_float(sector.get("market_board_pct"), float("nan"))
        market_board_rank = int(_safe_float(sector.get("market_board_rank"), 9999))
        market_percentile = _safe_float(sector.get("market_board_percentile"), float("nan"))
        market_health = _safe_float(sector.get("market_board_health_score"), 50.0)
        market_state = str(sector.get("market_board_state") or "UNAVAILABLE")
        market_entry_support = bool(sector.get("market_board_entry_support"))
        market_rotation_caution = bool(sector.get("market_board_rotation_caution"))
        market_breadth = _safe_float(sector.get("market_board_breadth"), 0.5)
        market_persistence = _safe_float(sector.get("market_board_persistence"), 0.0)
        if math.isfinite(market_percentile):
            market_score = 0.65 * market_health + 35.0 * market_percentile
            market_score += {
                "SUSTAINED_LEADER": 7, "ROTATION_IN": 3, "HEALTHY_RISING": 3,
                "FLASH_HEAT": -8, "ROTATION_OUT": -15, "WEAK": -10,
            }.get(market_state, 0)
            score = 0.72 * market_score + 0.28 * local_score
        else:
            score = min(local_score, 55.0)
        if state == "DIVERGING":
            score = min(score, 62)
        elif state == "CONCENTRATED":
            score = min(score, 55)
        elif state == "DECAY":
            score = min(score, 30)
        local_not_broken = state != "DECAY" and role != "LAGGARD"
        emerging_leader_supported = bool(
            market_state == "ROTATION_IN"
            and math.isfinite(market_percentile)
            and market_percentile >= 0.85
            and market_breadth >= 0.55
            and market_persistence >= 0.30
        )
        full_market_supported = bool(
            math.isfinite(market_percentile)
            and market_percentile >= 0.68
            and ((market_entry_support and not market_rotation_caution) or emerging_leader_supported)
        )
        return {
            "score": max(0, min(100, int(round(score)))),
            "state": state,
            "role": role,
            "healthy": bool(full_market_supported and local_not_broken),
            "local_corroboration": state in {"IGNITION", "EXPANSION", "HEALTHY_TREND"} and role != "LAGGARD",
            "full_market_supported": full_market_supported,
            "symbol_excess_vs_group": excess,
            "market_board_pct": market_board_pct if math.isfinite(market_board_pct) else None,
            "market_board_rank": market_board_rank if market_board_rank < 9999 else None,
            "market_board_percentile": market_percentile if math.isfinite(market_percentile) else None,
            "market_board_state": market_state,
            "market_rotation_caution": market_rotation_caution,
            "emerging_leader_supported": emerging_leader_supported,
        }

    def _append(self, state: Dict[str, Any], observation: Mapping[str, Any]) -> Optional[Dict[str, Any]]:
        ts = _as_datetime(observation.get("event_ts"))
        if not _continuous_session(ts):
            return None
        trade_date = ts.strftime("%Y-%m-%d")
        if state.get("trade_date") != trade_date:
            state.clear()
            state.update(self._new_state())
            state["trade_date"] = trade_date
        row = dict(observation)
        row["event_ts"] = ts
        samples: Deque[Dict[str, Any]] = state["samples"]
        if samples and ts < samples[-1]["event_ts"]:
            return None
        if samples and ts.replace(microsecond=0) == samples[-1]["event_ts"].replace(microsecond=0):
            samples[-1] = row
        else:
            samples.append(row)
        price = _safe_float(row.get("price"))
        if state["open_price"] <= 0:
            state["open_price"] = price
        state["session_high"] = max(_safe_float(state.get("session_high")), price)
        state["session_low"] = min(_safe_float(state.get("session_low"), price), price)
        return row

    def update(
        self,
        observation: Mapping[str, Any],
        candidate: Mapping[str, Any],
        auction: Optional[Mapping[str, Any]] = None,
        sector: Optional[Mapping[str, Any]] = None,
    ) -> Dict[str, Any]:
        symbol = str(observation.get("symbol") or "")
        if not symbol:
            return {"status": "NO_SYMBOL", "confirmed": False, "score": 0}
        daily = self._daily_quality(candidate)
        auction_result = self._auction_quality(auction or {})
        sector_result = self._sector_quality(sector or {})
        if candidate.get("daily_route") != "TREND_CONTINUATION":
            context = {
                "symbol": symbol, "status": "NOT_APPLICABLE", "confirmed": False,
                "score": 0, "daily": daily, "auction": auction_result, "sector": sector_result,
                "missing": ["当前不是趋势延续路线"],
            }
            self.states[symbol]["last_context"] = context
            return context
        state = self.states[symbol]
        current = self._append(state, observation)
        if current is None:
            return state.get("last_context") or {
                "symbol": symbol, "status": "WAITING_SESSION", "confirmed": False,
                "score": 0, "daily": daily, "auction": auction_result, "sector": sector_result,
                "missing": ["等待连续竞价行情"],
            }
        samples: Deque[Dict[str, Any]] = state["samples"]
        now = current["event_ts"]
        window_short = self._window(samples, now, self.config.short_window_seconds)
        window_acceptance = self._window(samples, now, self.config.acceptance_window_seconds)
        span = (
            (window_acceptance[-1]["event_ts"] - window_acceptance[0]["event_ts"]).total_seconds()
            if len(window_acceptance) >= 2 else 0.0
        )
        vwap_rows = [row for row in window_acceptance if _safe_float(row.get("vwap")) > 0]
        above_vwap_ratio = (
            sum(_safe_float(row.get("price")) >= _safe_float(row.get("vwap")) for row in vwap_rows) / len(vwap_rows)
            if vwap_rows else 0.0
        )
        price = _safe_float(current.get("price"))
        vwap = _safe_float(current.get("vwap"))
        vwap_gap = price / vwap - 1.0 if vwap > 0 else None
        reference_close = _safe_float(candidate.get("close"), _safe_float(candidate.get("pre_close")))
        current_return = price / reference_close - 1.0 if reference_close > 0 else None
        open_hold = price / state["open_price"] - 1.0 if state["open_price"] > 0 else None
        drawdown = (state["session_high"] - price) / state["session_high"] if state["session_high"] > 0 else 0.0
        recovery = price / state["session_low"] - 1.0 if state["session_low"] > 0 else 0.0
        momentum_60 = self._momentum(window_short)
        momentum_180 = self._momentum(window_acceptance)
        imbalance = current.get("amount_imbalance")
        atr = min(max(_safe_float(candidate.get("atr14_pct"), 0.05), 0.015), 0.15)
        max_vwap_extension = min(max(0.35 * atr, 0.012), 0.035)
        max_fade = min(max(0.22 * atr, 0.008), 0.025)
        final_gap = _safe_float((auction or {}).get("final_gap"), float("nan"))
        gap_retained = True
        if math.isfinite(final_gap) and final_gap > 0.01 and current_return is not None:
            gap_retained = current_return >= final_gap - max_fade
        limit_locked_proxy = bool(
            math.isfinite(final_gap) and final_gap >= self.config.max_limit_gap
            and current_return is not None and current_return >= self.config.max_limit_gap
            and abs(_safe_float(current.get("bid1_price")) - _safe_float(current.get("ask1_price"))) < 1e-9
        )
        requires_support_test = bool(
            (math.isfinite(final_gap) and final_gap > 0.03)
            or auction_result["score"] < 55
            or _safe_float((auction or {}).get("price_retreat")) >= 0.012
        )
        session_span = (
            (samples[-1]["event_ts"] - samples[0]["event_ts"]).total_seconds()
            if len(samples) >= 2 else 0.0
        )
        support_drawdown = max(0.003, 0.08 * atr)
        if (
            session_span >= 60
            and (drawdown >= support_drawdown or (vwap_gap is not None and vwap_gap <= -0.001))
        ):
            if not state.get("support_tested"):
                state["support_tested_at"] = now
            state["support_tested"] = True
        if (
            state.get("support_tested")
            and vwap_gap is not None and vwap_gap >= 0
            and momentum_60 > 0
            and recovery >= 0.003
        ):
            state["support_reclaimed"] = True
            state["support_reclaimed_at"] = now
        support_reclaim_age = (
            (now - state["support_reclaimed_at"]).total_seconds()
            if state.get("support_reclaimed_at") is not None else None
        )
        support_reclaim_fresh = bool(
            state.get("support_reclaimed")
            and support_reclaim_age is not None
            and 0 <= support_reclaim_age <= 900
        )
        if state.get("support_reclaimed") and not support_reclaim_fresh:
            state["support_reclaimed"] = False
            state["support_reclaimed_at"] = None

        acceptance_score = 15.0
        acceptance_score += 30.0 * above_vwap_ratio
        if vwap_gap is not None:
            if -0.001 <= vwap_gap <= 0.012:
                acceptance_score += 20
            elif 0.012 < vwap_gap <= max_vwap_extension:
                acceptance_score += 10
            elif vwap_gap < -0.006:
                acceptance_score -= 15
            elif vwap_gap > max_vwap_extension:
                acceptance_score -= 12
        if momentum_60 > 0:
            acceptance_score += 8
        if momentum_180 > 0:
            acceptance_score += 8
        if gap_retained:
            acceptance_score += 8
        else:
            acceptance_score -= 10
        if drawdown > max_fade:
            acceptance_score -= 12
        if recovery >= 0.004:
            acceptance_score += 5
        acceptance_score = max(0, min(100, int(round(acceptance_score))))

        micro_score = 50.0
        if imbalance is not None:
            micro_score += 25 if _safe_float(imbalance) >= 0.15 else (-30 if _safe_float(imbalance) <= -0.35 else 0)
        if momentum_60 > 0.0015:
            micro_score += 15
        elif momentum_60 < -0.0015:
            micro_score -= 15
        micro_score = max(0, min(100, int(round(micro_score))))

        score = int(round(
            0.25 * daily["score"]
            + 0.18 * auction_result["score"]
            + 0.22 * sector_result["score"]
            + 0.27 * acceptance_score
            + 0.08 * micro_score
        ))
        missing = []
        if span < self.config.min_observation_seconds:
            missing.append("开盘承接观察不足120秒")
        if daily["score"] < self.config.min_daily_quality:
            missing.append("D-1趋势质量不足")
        auction_repaired = bool(
            auction_result["score"] < self.config.min_auction_quality
            and acceptance_score >= 85
            and sector_result["healthy"]
            and span >= self.config.min_observation_seconds
        )
        if auction_result["score"] < self.config.min_auction_quality and not auction_repaired:
            missing.append("竞价续接质量偏弱，需更强盘中修复")
        market_supported = bool(sector_result.get("full_market_supported"))
        sector_confirmed = bool(market_supported and sector_result["state"] != "DECAY" and sector_result["role"] != "LAGGARD")
        if not sector_confirmed:
            missing.append("全市场板块健康/持续性尚未确认，或个股在合并池内已掉队")
        if above_vwap_ratio < self.config.min_above_vwap_ratio:
            missing.append("近180秒站上VWAP比例不足65%")
        if vwap_gap is None:
            missing.append("VWAP证据不可用")
        elif vwap_gap < -0.001:
            missing.append("现价尚未站稳VWAP")
        elif vwap_gap > max_vwap_extension:
            missing.append("现价远离VWAP，位置已透支")
        if momentum_180 < 0:
            missing.append("180秒动量尚未转正")
        if not gap_retained:
            missing.append("竞价强度开盘后未能保持")
        if requires_support_test and not support_reclaim_fresh:
            missing.append("高开/脆弱竞价尚未完成首次压力测试与收复")
        if imbalance is not None and _safe_float(imbalance) <= -0.35 and momentum_60 <= 0.002:
            missing.append("盘口卖压尚未被价格动量消化")
        if limit_locked_proxy:
            missing.append("接近涨停且盘口显示难以成交")
        if auction_result["hard_veto"]:
            missing.append("竞价多源数据冲突")

        raw_confirmed = bool(
            not missing and score >= self.config.min_confirm_score and not auction_result["hard_veto"]
        )
        hard_confirmation_reset = bool(
            auction_result["hard_veto"]
            or limit_locked_proxy
            or (vwap_gap is not None and vwap_gap < -0.006 and above_vwap_ratio < 0.35 and not sector_result["healthy"])
            or (not gap_retained and momentum_180 < -0.003)
        )
        if raw_confirmed:
            if state.get("confirmation_candidate_since") is None:
                state["confirmation_candidate_since"] = now
            confirmation_persistence = (now - state["confirmation_candidate_since"]).total_seconds()
        else:
            # 确认持续时间必须连续。旧版仅在“硬失效”时清零，导致几十分钟前的
            # 承接证据可以给当前冲高背书；任何一轮组合证据不完整都重新计时。
            state["confirmation_candidate_since"] = None
            confirmation_persistence = 0.0
        confirmed = bool(
            raw_confirmed
            and confirmation_persistence >= self.config.min_confirmation_persistence_seconds
        )
        if raw_confirmed and not confirmed:
            missing.append(
                f"等待至少{self.config.min_confirmation_persistence_seconds}秒后的第二次四段确认"
            )
        if auction_result["hard_veto"]:
            status = "DATA_CONFLICT"
        elif limit_locked_proxy:
            status = "LIMIT_LOCKED"
        elif span < self.config.min_observation_seconds:
            status = "OBSERVING"
        elif confirmed and momentum_60 > 0.0015:
            status = "REACCELERATING"
        elif confirmed:
            status = "ACCEPTED"
        elif vwap_gap is not None and vwap_gap < -0.006 and above_vwap_ratio < 0.35:
            status = "FAILED_ACCEPTANCE"
        elif state.get("accepted_once"):
            status = "DEGRADED"
        else:
            status = "TESTING_ACCEPTANCE"
        if confirmed:
            state["accepted_once"] = True

        context = {
            "symbol": symbol,
            "asof": now.isoformat(),
            "status": status,
            "confirmed": confirmed,
            "score": max(0, min(100, score)),
            "daily": daily,
            "auction": auction_result,
            "sector": sector_result,
            "acceptance_score": acceptance_score,
            "micro_score": micro_score,
            "observation_seconds": span,
            "confirmation_persistence_seconds": confirmation_persistence,
            "above_vwap_ratio_180s": above_vwap_ratio,
            "vwap": vwap or None,
            "vwap_gap": vwap_gap,
            "current_return": current_return,
            "open_hold": open_hold,
            "session_drawdown": drawdown,
            "session_recovery": recovery,
            "momentum_60s": momentum_60,
            "momentum_180s": momentum_180,
            "gap_retained": gap_retained,
            "requires_support_test": requires_support_test,
            "support_tested": bool(state.get("support_tested")),
            "support_reclaimed": support_reclaim_fresh,
            "support_tested_at": state.get("support_tested_at").isoformat() if state.get("support_tested_at") else None,
            "support_reclaimed_at": state.get("support_reclaimed_at").isoformat() if state.get("support_reclaimed_at") else None,
            "support_reclaim_age_seconds": support_reclaim_age,
            "auction_repaired_by_live_acceptance": auction_repaired,
            "max_vwap_extension": max_vwap_extension,
            "amount_imbalance": imbalance,
            "missing": missing,
            "evidence": [
                f"日线质量{daily['score']}",
                f"竞价续接{auction_result['score']}",
                f"板块健康{sector_result['score']}",
                f"盘中承接{acceptance_score}",
                f"近180秒站上VWAP {above_vwap_ratio:.0%}",
            ],
            "rules_version": "trend_continuation_v1",
            "no_lookahead": True,
        }
        state["last_context"] = context
        return context

    def context_for(self, symbol: str) -> Dict[str, Any]:
        return dict(self.states.get(symbol, {}).get("last_context") or {
            "symbol": symbol,
            "status": "NO_LIVE_CONTEXT",
            "confirmed": False,
            "score": 0,
            "missing": ["等待盘中行情"],
        })

    def snapshot(self) -> Dict[str, Any]:
        rows = [dict(state.get("last_context") or {}) for state in self.states.values() if state.get("last_context")]
        rows.sort(key=lambda row: (-int(row.get("score", 0)), str(row.get("symbol", ""))))
        return {
            "rows": rows,
            "by_symbol": {row["symbol"]: row for row in rows if row.get("symbol")},
            "rules_version": "trend_continuation_v1",
        }
