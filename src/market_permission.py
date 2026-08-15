# coding: utf-8
"""A股动态交易许可与持仓风险动作层。"""

from __future__ import annotations

import math
from typing import Any, Dict, Mapping, Optional


MARKET_PERMISSION_VERSION = "market_permission_v1_domestic_first"


def _safe_float(value: Any, default: float = 0.0) -> float:
    try:
        result = float(value)
        return result if math.isfinite(result) else default
    except (TypeError, ValueError):
        return default


class MarketPermissionEngine:
    """国内实时证据拥有决策权；外围市场最多下调一级，且不能单独触发卖出。"""

    def __init__(self):
        self.latest: Dict[str, Any] = self._unknown("尚无国内市场快照")

    @staticmethod
    def _unknown(reason: str) -> Dict[str, Any]:
        return {
            "version": MARKET_PERMISSION_VERSION,
            "state": "WARMING_UP",
            "state_cn": "市场许可预热",
            "new_entry_permission": "SELECTIVE",
            "route_permission": ["PULLBACK_RECLAIM", "REPAIR_REVERSAL"],
            "position_bias": "HOLD_AND_OBSERVE",
            "position_action_cn": "不集体砍仓；等待A股实时广度、板块和个股证据",
            "domestic_score": 50,
            "global_adjustment": 0,
            "reason": reason,
            "global_can_trigger_exit": False,
        }

    def evaluate(
        self,
        market_snapshot: Optional[Mapping[str, Any]],
        global_snapshot: Optional[Mapping[str, Any]] = None,
        *,
        premarket_daily: Optional[Mapping[str, Any]] = None,
    ) -> Dict[str, Any]:
        market = dict(market_snapshot or {})
        global_market = dict(global_snapshot or {})
        ready = str(market.get("status") or "") == "GREEN" and int(_safe_float(market.get("eligible_row_count"))) >= 50
        breadth = _safe_float(market.get("market_positive_breadth"), float("nan"))
        median_return = _safe_float(market.get("market_median_pct"), float("nan"))
        overlap = _safe_float(market.get("top_decile_overlap"), float("nan"))
        limit_meta = market.get("limit_up_meta") or {}
        limit_up_count = int(_safe_float(limit_meta.get("row_count")))
        # 正式内存快照使用rows；落盘审计快照使用all_market_compact/top_boards。
        rows = list(market.get("rows") or market.get("all_market_compact") or market.get("top_boards") or [])
        healthy_count = sum(
            bool(row.get("entry_support"))
            and _safe_float(row.get("health_percentile")) >= 0.75
            and _safe_float(row.get("top_quartile_persistence")) >= 0.50
            and str(row.get("rotation_state") or "") in {"SUSTAINED_LEADER", "HEALTHY_RISING", "ROTATION_IN"}
            for row in rows
        )
        healthy_ratio = healthy_count / len(rows) if rows else 0.0

        if ready and all(math.isfinite(value) for value in (breadth, median_return)):
            score = 50.0
            score += max(-22.0, min(22.0, (breadth - 0.50) * 80.0))
            score += max(-18.0, min(18.0, median_return * 700.0))
            if math.isfinite(overlap):
                score += max(-7.0, min(7.0, (overlap - 0.45) * 20.0))
            # 使用全市场健康板块占比，不按榜单前N名计数，避免“先排序再证明市场强”。
            score += max(-4.0, min(8.0, (healthy_ratio - 0.08) * 55.0))
            score += min(6.0, limit_up_count * 0.12)
            domestic_source = "FULL_MARKET_INTRADAY"
        else:
            daily = dict(premarket_daily or {})
            pool_breadth = _safe_float(daily.get("pool_breadth"), 0.50)
            pool_median = _safe_float(daily.get("pool_median_return_5d"), 0.0)
            score = 50.0 + max(-10.0, min(10.0, (pool_breadth - 0.50) * 35.0))
            score += max(-8.0, min(8.0, pool_median * 120.0))
            domestic_source = "PREMARKET_D1_POOL_PROXY_LOW_CONFIDENCE"

        global_adjustment = int(_safe_float(global_market.get("score_adjustment"), 0))
        # 外围最多改变5分，且只有国内已偏弱时才能把许可继续下调。
        score_with_global = score + global_adjustment
        domestic_score = int(round(max(0.0, min(100.0, score))))
        combined_score = int(round(max(0.0, min(100.0, score_with_global))))

        if ready and breadth <= 0.24 and median_return <= -0.018:
            state = "PANIC_CLEARING"
        elif ready and (combined_score < 34 or (breadth <= 0.34 and median_return < -0.009)):
            state = "RISK_OFF"
        elif ready and combined_score >= 68 and breadth >= 0.57 and healthy_ratio >= 0.08:
            state = "ATTACK_TREND"
        elif ready and (str(market.get("market_regime")) == "FAST_ROTATION" or (breadth >= 0.56 and overlap < 0.34)):
            state = "OVERHEATED_FAST_ROTATION"
        elif ready and combined_score >= 48:
            state = "SELECTIVE_ROTATION"
        elif ready:
            state = "REPAIR_OR_CAUTION"
        else:
            state = "PREMARKET_PLAN"

        definitions = {
            "ATTACK_TREND": ("进攻趋势", "OPEN", ["BREAKOUT", "PULLBACK_RECLAIM", "MA_REACCELERATION"], "HOLD_WINNERS", "强势核心优先持有；个股实时转弱才减仓"),
            "SELECTIVE_ROTATION": ("选择性轮动", "SELECTIVE", ["PULLBACK_RECLAIM", "NON_EXTENDED_BREAKOUT"], "HOLD_STRONG_REDUCE_LAGGARDS", "保留相对强势；板块掉队且资金流出的弱仓才减仓"),
            "OVERHEATED_FAST_ROTATION": ("过热快轮动", "SELECTIVE", ["CONTROLLED_PULLBACK_ONLY"], "HOLD_WINNERS_PROTECT_PROFIT", "不追普通突破；持有赢家并加强利润保护"),
            "REPAIR_OR_CAUTION": ("修复/谨慎", "SELECTIVE", ["REPAIR_REVERSAL", "PULLBACK_RECLAIM"], "REDUCE_CONFIRMED_WEAK_ONLY", "不硬扛确认走弱仓，也不因大盘一般误杀独立强势仓"),
            "RISK_OFF": ("风险关闭", "CLOSED", ["HOLD_ONLY"], "REDUCE_WEAK_HOLD_RELATIVE_STRENGTH", "关闭新仓；优先减板块退潮、资金流出且跌破结构的可卖仓"),
            "PANIC_CLEARING": ("恐慌出清", "CLOSED", ["NO_NEW_ENTRY"], "EXIT_CONFIRMED_WEAK_PROTECT_LOCKED", "停止新仓；可卖弱仓按实时卖点退出，T+1锁定仓只报警并停止加仓"),
            "PREMARKET_PLAN": ("盘前计划", "SELECTIVE", ["WAIT_INTRADAY_CONFIRMATION"], "HOLD_AND_OBSERVE", "盘前不集体砍仓；开盘后由A股实时状态接管"),
        }
        state_cn, entry, routes, position_bias, position_action = definitions[state]
        reason = (
            f"国内分{domestic_score}/100（{domestic_source}），外围修正{global_adjustment:+d}，"
            f"合成{combined_score}/100；外围不能单独触发卖出"
        )
        self.latest = {
            "version": MARKET_PERMISSION_VERSION,
            "state": state,
            "state_cn": state_cn,
            "new_entry_permission": entry,
            "route_permission": routes,
            "position_bias": position_bias,
            "position_action_cn": position_action,
            "domestic_score": domestic_score,
            "combined_score": combined_score,
            "global_state": global_market.get("state", "UNKNOWN"),
            "global_adjustment": global_adjustment,
            "market_positive_breadth": breadth if math.isfinite(breadth) else None,
            "market_median_pct": median_return if math.isfinite(median_return) else None,
            "healthy_board_count": healthy_count,
            "healthy_board_ratio": round(healthy_ratio, 6),
            "limit_up_count": limit_up_count,
            "domestic_source": domestic_source,
            "reason": reason,
            "global_can_trigger_exit": False,
            "no_forced_collective_exit": True,
        }
        return self.latest
