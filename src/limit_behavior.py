# coding: utf-8
"""涨停附近的资金行为旁路状态机。

只描述盘口事实和风险假设，不产生买卖信号。尤其是“断魂码/特殊数字”永远不会
把状态直接升级为派发；没有完整逐笔委托队列时，最高只能输出 DISTRIBUTION_WARNING。
"""

from __future__ import annotations

import math
from collections import defaultdict
from datetime import datetime
from typing import Any, Dict, Mapping


LIMIT_BEHAVIOR_VERSION = "limit_behavior_shadow_v1"


def _safe_float(value: Any, default: float = 0.0) -> float:
    try:
        number = float(value)
        return number if math.isfinite(number) else default
    except (TypeError, ValueError):
        return default


class LimitBehaviorEngine:
    """跟踪触板、封板、开板和回封路径；所有输出权重均为0。"""

    def __init__(self) -> None:
        self.states: Dict[str, Dict[str, Any]] = defaultdict(dict)

    @staticmethod
    def _limit_ratio(symbol: str) -> float:
        code = str(symbol).split(".")[-1]
        return 0.20 if code.startswith(("300", "301", "688")) else 0.10

    def update(self, observation: Mapping[str, Any], candidate: Mapping[str, Any]) -> Dict[str, Any]:
        symbol = str(observation.get("symbol") or "")
        ts = observation.get("event_ts")
        if not isinstance(ts, datetime):
            try:
                ts = datetime.fromisoformat(str(ts).replace("Z", "+00:00")).replace(tzinfo=None)
            except (TypeError, ValueError):
                ts = None
        if not symbol or not isinstance(ts, datetime):
            return {"status": "UNAVAILABLE", "strategy_effect": "NONE_TAG_ONLY"}
        trade_date = ts.strftime("%Y-%m-%d")
        state = self.states[symbol]
        if state.get("trade_date") != trade_date:
            state.clear()
            state.update({
                "trade_date": trade_date, "last_locked": False, "ever_locked": False,
                "first_touch_at": None, "first_lock_at": None, "last_open_at": None,
                "open_count": 0, "reseal_count": 0, "peak_seal_amount": 0.0,
                "last_seal_amount": 0.0, "classification": "NOT_NEAR_LIMIT",
            })

        price = _safe_float(observation.get("price"))
        reference = _safe_float(candidate.get("close"), _safe_float(candidate.get("pre_close")))
        if price <= 0 or reference <= 0:
            return {"status": "UNAVAILABLE", "strategy_effect": "NONE_TAG_ONLY"}
        ratio = self._limit_ratio(symbol)
        limit_price = reference * (1.0 + ratio)
        intraday_return = price / reference - 1.0
        near_limit = intraday_return >= ratio - 0.006
        approaching = intraday_return >= ratio - 0.025
        ask1 = _safe_float(observation.get("ask1_price"))
        bid1 = _safe_float(observation.get("bid1_price"))
        quotes = observation.get("quotes") or []
        locked = bool(near_limit and quotes and ask1 <= 0 and bid1 >= price * 0.999)
        seal_amount = _safe_float(observation.get("top5_bid_amount")) if locked else 0.0
        cumulative_amount = _safe_float(observation.get("cum_amount"))
        seal_turnover_ratio = seal_amount / cumulative_amount if cumulative_amount > 0 else None
        imbalance = observation.get("amount_imbalance")
        previous_classification = str(state.get("classification") or "NOT_NEAR_LIMIT")

        if approaching and state.get("first_touch_at") is None:
            state["first_touch_at"] = ts
        if locked and not state.get("ever_locked"):
            state["ever_locked"] = True
            state["first_lock_at"] = ts
        if state.get("last_locked") and not locked:
            state["open_count"] += 1
            state["last_open_at"] = ts
        if locked and not state.get("last_locked") and state.get("ever_locked") and state.get("first_lock_at") != ts:
            state["reseal_count"] += 1

        prior_peak = _safe_float(state.get("peak_seal_amount"))
        if locked:
            state["peak_seal_amount"] = max(prior_peak, seal_amount)
        peak_seal = _safe_float(state.get("peak_seal_amount"))
        seal_retention = seal_amount / peak_seal if peak_seal > 0 else None
        distance_to_limit = price / limit_price - 1.0 if limit_price > 0 else None

        distribution_warning = bool(
            state.get("open_count", 0) >= 2
            and not locked
            and distance_to_limit is not None and distance_to_limit <= -0.004
            and imbalance is not None and _safe_float(imbalance) <= -0.15
        )
        weak_reseal = bool(
            locked and state.get("open_count", 0) >= 2
            and seal_retention is not None and seal_retention < 0.60
        )
        absorbed = bool(
            state.get("open_count", 0) >= 1 and not locked and near_limit
            and imbalance is not None and _safe_float(imbalance) >= 0.10
        )
        strengthening_reseal = bool(
            locked and state.get("reseal_count", 0) >= 1
            and seal_retention is not None and seal_retention >= 0.85
        )

        if distribution_warning:
            classification = "DISTRIBUTION_WARNING"
        elif weak_reseal:
            classification = "REPEATED_WEAK_RESEAL"
        elif strengthening_reseal:
            classification = "RESEAL_STRENGTHENING"
        elif absorbed:
            classification = "SELLING_ABSORBED"
        elif state.get("last_locked") and not locked:
            classification = "OPEN_BOARD"
        elif locked:
            classification = "THIN_SEAL" if seal_turnover_ratio is not None and seal_turnover_ratio < 0.03 else "SEALED"
        elif near_limit:
            classification = "FIRST_TOUCH_LIMIT"
        elif approaching:
            classification = "APPROACHING_LIMIT"
        else:
            classification = "NOT_NEAR_LIMIT"

        state["last_locked"] = locked
        state["last_seal_amount"] = seal_amount
        state["classification"] = classification
        state["asof"] = ts.isoformat()
        state["near_limit"] = near_limit
        state["locked"] = locked
        return {
            "status": "READY", "symbol": symbol, "trade_date": trade_date,
            "asof": ts.isoformat(), "price": price, "classification": classification,
            "previous_classification": previous_classification,
            "state_changed": classification != previous_classification,
            "near_limit": near_limit, "locked": locked, "executable": not locked,
            "limit_ratio": ratio, "limit_price_proxy": limit_price,
            "intraday_return": intraday_return, "distance_to_limit": distance_to_limit,
            "open_count": int(state.get("open_count", 0)),
            "reseal_count": int(state.get("reseal_count", 0)),
            "seal_amount": seal_amount, "seal_turnover_ratio": seal_turnover_ratio,
            "seal_retention": seal_retention, "amount_imbalance": imbalance,
            "distribution_confirmed": False,
            "interpretation": "仅为涨停路径旁路观察；无完整逐笔委托队列时不确认派发",
            "strategy_effect": "NONE_TAG_ONLY", "experimental_weight": 0,
            "changes_main_signal": False, "order_submitted": False,
            "rules_version": LIMIT_BEHAVIOR_VERSION, "no_lookahead": True,
        }

    def snapshot(self) -> Dict[str, Any]:
        rows = []
        for symbol, state in self.states.items():
            if state.get("classification") and state.get("classification") != "NOT_NEAR_LIMIT":
                rows.append({"symbol": symbol, **state})
        return {"rows": rows, "by_symbol": {row["symbol"]: row for row in rows}}
