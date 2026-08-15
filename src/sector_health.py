# coding: utf-8
"""精选池实时板块健康与梯队引擎。

该模块只使用截至当前 Tick 已经到达的数据。它不会把精选池代理伪装成全市场
板块事实；外部全市场板块适配器接入前，所有输出都明确标记为
``COMBINED_POOL_CORROBORATION_NOT_MARKET_BENCHMARK``。
"""

from __future__ import annotations

import math
from collections import defaultdict, deque
from datetime import datetime, timedelta
from statistics import median
from typing import Any, Deque, Dict, Iterable, List, Mapping, Optional


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


class LiveSectorHealthEngine:
    """用精选池实时行情识别主题宽度、梯队、集中度和状态变化。"""

    def __init__(self, taxonomy: Mapping[str, Any]):
        self.taxonomy = dict(taxonomy)
        self.candidates: Dict[str, Dict[str, Any]] = {}
        self.observations: Dict[str, Dict[str, Any]] = {}
        self.history: Dict[str, Deque[Dict[str, Any]]] = defaultdict(lambda: deque(maxlen=180))
        self._last_history_ts: Dict[str, datetime] = {}
        self._last_recompute_at: Optional[datetime] = None
        self._snapshot: Dict[str, Any] = {"groups": [], "by_group": {}, "by_symbol": {}}

    def set_candidates(self, candidates: Iterable[Mapping[str, Any]]) -> None:
        self.candidates = {
            str(row.get("symbol")): dict(row)
            for row in candidates
            if row.get("symbol")
        }
        self._recompute()

    def update(self, observation: Mapping[str, Any]) -> Dict[str, Any]:
        symbol = str(observation.get("symbol") or "")
        if symbol:
            self.observations[symbol] = dict(observation)
            event_ts = _as_datetime(observation.get("event_ts"))
            if self._last_recompute_at is None or (event_ts - self._last_recompute_at).total_seconds() >= 1:
                self._recompute()
                self._last_recompute_at = event_ts
        return self.context_for(symbol)

    def _theme_members(self) -> Dict[str, List[str]]:
        groups: Dict[str, List[str]] = defaultdict(list)
        symbols = self.taxonomy.get("symbols", {})
        for symbol in self.candidates:
            item = symbols.get(symbol, {})
            for theme in item.get("stable_themes") or []:
                key = str(theme or "").strip()
                if key and symbol not in groups[key]:
                    groups[key].append(symbol)
        return groups

    def _live_row(self, symbol: str) -> Optional[Dict[str, Any]]:
        candidate = self.candidates.get(symbol, {})
        observation = self.observations.get(symbol)
        close = _safe_float(candidate.get("close"))
        if not observation or close <= 0:
            return None
        price = _safe_float(observation.get("price"))
        if price <= 0:
            return None
        vwap = _safe_float(observation.get("vwap"))
        return {
            "symbol": symbol,
            "name": candidate.get("name", ""),
            "price": price,
            "previous_close": close,
            "return": price / close - 1.0,
            "vwap": vwap or None,
            "above_vwap": bool(vwap > 0 and price >= vwap),
            "vwap_gap": price / vwap - 1.0 if vwap > 0 else None,
            "cum_amount": _safe_float(observation.get("cum_amount")),
            "amount_imbalance": observation.get("amount_imbalance"),
            "event_ts": _as_datetime(observation.get("event_ts")),
        }

    @staticmethod
    def _roles(rows: List[Dict[str, Any]]) -> Dict[str, str]:
        ordered = sorted(rows, key=lambda row: (-row["return"], -row["cum_amount"], row["symbol"]))
        if not ordered:
            return {}
        roles = {ordered[0]["symbol"]: "LEADER"}
        core = None
        if len(ordered) >= 3:
            core_candidates = ordered[1:]
            core = max(core_candidates, key=lambda row: row["cum_amount"])
            if core["cum_amount"] > 0:
                roles[core["symbol"]] = "CORE"
        front_limit = max(2, int(math.ceil(len(ordered) * 0.40)))
        for index, row in enumerate(ordered):
            if row["symbol"] in roles:
                continue
            if row["return"] <= 0 and index >= max(1, len(ordered) // 2):
                roles[row["symbol"]] = "LAGGARD"
            elif index < front_limit:
                roles[row["symbol"]] = "FRONT"
            else:
                roles[row["symbol"]] = "FOLLOWER"
        return roles

    def _history_delta(self, theme: str, now: datetime, current: Dict[str, Any]) -> Dict[str, float]:
        history = self.history[theme]
        cutoff = now - timedelta(minutes=5)
        prior = next((row for row in history if row["asof"] >= cutoff), history[0] if history else None)
        delta = {
            "breadth_delta_5m": current["breadth"] - _safe_float(prior.get("breadth")) if prior else 0.0,
            "above_vwap_delta_5m": current["above_vwap_ratio"] - _safe_float(prior.get("above_vwap_ratio")) if prior else 0.0,
            "median_return_delta_5m": current["median_return"] - _safe_float(prior.get("median_return")) if prior else 0.0,
            "amount_delta_5m": current["total_amount"] - _safe_float(prior.get("total_amount")) if prior else 0.0,
        }
        last_ts = self._last_history_ts.get(theme)
        if last_ts is None or (now - last_ts).total_seconds() >= 5:
            history.append({"asof": now, **current})
            self._last_history_ts[theme] = now
        return delta

    @staticmethod
    def _state(group: Dict[str, Any]) -> str:
        n = group["observed_count"]
        breadth = group["breadth"]
        above = group["above_vwap_ratio"]
        med = group["median_return"]
        concentration = group["concentration"]
        delta = group["breadth_delta_5m"] + group["above_vwap_delta_5m"]
        if n < 2:
            return "UNAVAILABLE"
        if concentration >= 0.05 and (n <= 2 or breadth < 0.70):
            return "CONCENTRATED"
        if breadth >= 0.70 and above >= 0.50 and med >= 0.015:
            return "EXPANSION"
        if delta >= 0.25 and med > 0:
            return "IGNITION"
        if breadth >= 0.60 and above >= 0.50 and med > 0:
            return "HEALTHY_TREND"
        if breadth <= 0.40 and above <= 0.40 and med < 0:
            return "DECAY"
        if med > 0 or breadth > 0.50:
            return "DIVERGING"
        return "NEUTRAL"

    @staticmethod
    def _score(group: Dict[str, Any]) -> int:
        median_component = max(-10.0, min(25.0, group["median_return"] / 0.05 * 25.0))
        acceleration = max(-10.0, min(10.0, (group["breadth_delta_5m"] + group["above_vwap_delta_5m"]) * 20.0))
        concentration_penalty = max(0.0, min(15.0, (group["concentration"] - 0.03) * 250.0))
        score = 20.0 + group["breadth"] * 25.0 + group["above_vwap_ratio"] * 20.0
        score += median_component + acceleration - concentration_penalty
        return max(0, min(100, int(round(score))))

    def _recompute(self) -> None:
        groups = []
        by_symbol: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
        for theme, members in self._theme_members().items():
            if len(members) < 2:
                continue
            rows = [row for symbol in members if (row := self._live_row(symbol)) is not None]
            if not rows:
                continue
            now = max(row["event_ts"] for row in rows)
            returns = [row["return"] for row in rows]
            above_count = sum(row["above_vwap"] for row in rows)
            up_count = sum(row["return"] > 0 for row in rows)
            group = {
                "theme": theme,
                "asof": now,
                "member_count": len(members),
                "observed_count": len(rows),
                "up_count": up_count,
                "breadth": up_count / len(rows),
                "above_vwap_count": above_count,
                "above_vwap_ratio": above_count / len(rows),
                "median_return": float(median(returns)),
                "max_return": max(returns),
                "min_return": min(returns),
                "concentration": max(returns) - float(median(returns)),
                "total_amount": sum(row["cum_amount"] for row in rows),
                "source": "COMBINED_POOL_CORROBORATION_NOT_MARKET_BENCHMARK",
                "confidence": "HIGH" if len(rows) >= 5 else ("MEDIUM" if len(rows) >= 3 else "LOW"),
            }
            group.update(self._history_delta(theme, now, group))
            roles = self._roles(rows)
            group["state"] = self._state(group)
            group["score"] = self._score(group)
            group["members"] = [
                {**row, "role": roles.get(row["symbol"], "FOLLOWER")}
                for row in sorted(rows, key=lambda row: (-row["return"], row["symbol"]))
            ]
            groups.append(group)
            for row in group["members"]:
                by_symbol[row["symbol"]].append({
                    "theme": theme,
                    "state": group["state"],
                    "score": group["score"],
                    "confidence": group["confidence"],
                    "role": row["role"],
                    "breadth": group["breadth"],
                    "above_vwap_ratio": group["above_vwap_ratio"],
                    "median_return": group["median_return"],
                    "symbol_return": row["return"],
                    "symbol_excess_vs_group": row["return"] - group["median_return"],
                    "breadth_delta_5m": group["breadth_delta_5m"],
                    "source": group["source"],
                })
        groups.sort(key=lambda row: (-row["score"], -row["median_return"], row["theme"]))
        primary_by_symbol = {}
        for symbol, contexts in by_symbol.items():
            contexts.sort(key=lambda row: (
                {"EXPANSION": 0, "IGNITION": 1, "HEALTHY_TREND": 2, "DIVERGING": 3, "CONCENTRATED": 4, "NEUTRAL": 5, "DECAY": 6}.get(row["state"], 9),
                -row["score"],
                -row["breadth"],
            ))
            primary_by_symbol[symbol] = contexts[0]
        self._snapshot = {
            "asof": max((row["asof"] for row in groups), default=None),
            "groups": groups,
            "by_group": {row["theme"]: row for row in groups},
            "by_symbol": primary_by_symbol,
            "source": "COMBINED_POOL_CORROBORATION_NOT_MARKET_BENCHMARK",
        }

    def context_for(self, symbol: str) -> Dict[str, Any]:
        return dict(self._snapshot.get("by_symbol", {}).get(symbol, {
            "theme": self.candidates.get(symbol, {}).get("group_key", "未形成同主题样本"),
            "state": "UNAVAILABLE",
            "score": 0,
            "confidence": "LOW",
            "role": "UNCLASSIFIED",
            "source": "COMBINED_POOL_CORROBORATION_NOT_MARKET_BENCHMARK",
        }))

    def snapshot(self) -> Dict[str, Any]:
        return self._snapshot
