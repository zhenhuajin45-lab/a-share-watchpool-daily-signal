# coding: utf-8
"""全市场二阶段动态观察名单。

第一阶段由全市场板块/个股横截面产生发现候选；本模块只负责稳定名单、
数据就绪状态与订阅生命周期，不直接产生买卖信号。正式信号仍由统一的
日线、分钟、Tick、资金行为、板块与 T+1 状态机共同决定。
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Set


DYNAMIC_UNIVERSE_VERSION = "dynamic_universe_v1_two_stage_formal_tick"


def _safe_float(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


class DynamicUniverseManager:
    """给动态订阅增加容量、驻留、防抖和持仓保护边界。"""

    def __init__(
        self,
        *,
        max_active: int = 12,
        max_per_board: int = 2,
        minimum_score: int = 68,
        minimum_residence_seconds: int = 8 * 60,
        misses_to_retire: int = 2,
    ) -> None:
        self.max_active = max(1, int(max_active))
        self.max_per_board = max(1, int(max_per_board))
        self.minimum_score = max(0, int(minimum_score))
        self.minimum_residence_seconds = max(0, int(minimum_residence_seconds))
        self.misses_to_retire = max(1, int(misses_to_retire))
        self.active: Dict[str, Dict[str, Any]] = {}
        self.prepare_failures: Dict[str, Dict[str, Any]] = {}
        self.last_snapshot_id: Optional[str] = None
        self.generation = 0

    @staticmethod
    def _board_name(row: Mapping[str, Any]) -> str:
        return str((row.get("matched_board") or {}).get("board_name") or "未映射板块")

    def eligible_discoveries(
        self,
        rows: Sequence[Mapping[str, Any]],
        *,
        base_symbols: Iterable[str] = (),
    ) -> List[Dict[str, Any]]:
        """只接收已通过一级入场逻辑的 A/B 前排，不拿 C 级凑数量。"""

        base = set(base_symbols)
        ranked: List[Dict[str, Any]] = []
        for source in rows:
            row = dict(source)
            symbol = str(row.get("symbol") or "")
            grade = str(row.get("strategy_match_grade") or "")
            score = int(round(_safe_float(row.get("discovery_score"))))
            if (
                not symbol
                or symbol in base
                or not bool(row.get("entry_logic_match"))
                or grade not in {"A_EARLY_LEADER", "B_STRONG_FRONT"}
                or score < self.minimum_score
            ):
                continue
            ranked.append(row)
        ranked.sort(key=lambda row: (
            -int(str(row.get("strategy_match_grade")) == "A_EARLY_LEADER"),
            -int(round(_safe_float(row.get("discovery_score")))),
            -_safe_float(row.get("amount")),
            str(row.get("symbol") or ""),
        ))
        selected: List[Dict[str, Any]] = []
        board_counts: Dict[str, int] = {}
        for row in ranked:
            board = self._board_name(row)
            if board_counts.get(board, 0) >= self.max_per_board:
                continue
            selected.append(row)
            board_counts[board] = board_counts.get(board, 0) + 1
        return selected

    def plan(
        self,
        rows: Sequence[Mapping[str, Any]],
        *,
        snapshot_id: str,
        now: datetime,
        base_symbols: Iterable[str] = (),
        protected_symbols: Iterable[str] = (),
    ) -> Dict[str, Any]:
        """根据一次新的市场快照返回准备、保留和退订计划。

        同一个 snapshot_id 重复调用是幂等的，不会累计 miss。
        """

        if snapshot_id and snapshot_id == self.last_snapshot_id:
            return {
                "status": "UNCHANGED_SNAPSHOT",
                "snapshot_id": snapshot_id,
                "to_prepare": [],
                "to_retire": [],
                "active_symbols": list(self.active),
                "version": DYNAMIC_UNIVERSE_VERSION,
            }
        self.last_snapshot_id = snapshot_id or now.isoformat()
        self.generation += 1
        protected: Set[str] = set(protected_symbols)
        discoveries = self.eligible_discoveries(rows, base_symbols=base_symbols)
        discovery_map = {str(row.get("symbol")): row for row in discoveries}

        to_retire: List[str] = []
        for symbol, record in list(self.active.items()):
            if symbol in discovery_map:
                record["misses"] = 0
                record["last_seen_at"] = now.isoformat()
                record["discovery"] = discovery_map[symbol]
                continue
            record["misses"] = int(record.get("misses", 0)) + 1
            admitted_at = record.get("admitted_at")
            residence = (now - admitted_at).total_seconds() if isinstance(admitted_at, datetime) else 10**9
            if (
                symbol not in protected
                and record["misses"] >= self.misses_to_retire
                and residence >= self.minimum_residence_seconds
            ):
                to_retire.append(symbol)

        retained_count = len(self.active) - len(to_retire)
        capacity = max(0, self.max_active - retained_count)
        to_prepare = [
            row for row in discoveries
            if str(row.get("symbol")) not in self.active
        ][:capacity]
        return {
            "status": "PLAN_READY",
            "snapshot_id": self.last_snapshot_id,
            "generation": self.generation,
            "discovery_count": len(discoveries),
            "to_prepare": to_prepare,
            "to_retire": to_retire,
            "retained": [symbol for symbol in self.active if symbol not in to_retire],
            "protected": sorted(protected),
            "capacity_after_retire": capacity,
            "version": DYNAMIC_UNIVERSE_VERSION,
        }

    def activate(
        self,
        symbol: str,
        *,
        candidate: Mapping[str, Any],
        discovery: Mapping[str, Any],
        data_quality: Mapping[str, Any],
        now: datetime,
    ) -> None:
        self.active[symbol] = {
            "symbol": symbol,
            "candidate": dict(candidate),
            "discovery": dict(discovery),
            "data_quality": dict(data_quality),
            "admitted_at": now,
            "last_seen_at": now.isoformat(),
            "misses": 0,
            "subscribed": True,
            "generation": self.generation,
        }
        self.prepare_failures.pop(symbol, None)

    def mark_prepare_failure(self, symbol: str, reason: str, *, now: datetime) -> None:
        prior = self.prepare_failures.get(symbol) or {}
        self.prepare_failures[symbol] = {
            "symbol": symbol,
            "reason": str(reason)[:300],
            "failed_at": now.isoformat(),
            "count": int(prior.get("count", 0)) + 1,
            "generation": self.generation,
        }

    def retire(self, symbols: Iterable[str]) -> None:
        for symbol in symbols:
            self.active.pop(str(symbol), None)

    def candidate_map(self) -> Dict[str, Dict[str, Any]]:
        return {
            symbol: dict(record.get("candidate") or {})
            for symbol, record in self.active.items()
            if record.get("subscribed") and (record.get("data_quality") or {}).get("formal_ready")
        }

    def snapshot(self) -> Dict[str, Any]:
        rows = []
        for symbol, record in self.active.items():
            discovery = record.get("discovery") or {}
            quality = record.get("data_quality") or {}
            rows.append({
                "symbol": symbol,
                "name": (record.get("candidate") or {}).get("name") or discovery.get("name"),
                "board_name": self._board_name(discovery),
                "discovery_score": discovery.get("discovery_score"),
                "strategy_match_grade": discovery.get("strategy_match_grade"),
                "admitted_at": (
                    record.get("admitted_at").isoformat()
                    if isinstance(record.get("admitted_at"), datetime)
                    else record.get("admitted_at")
                ),
                "last_seen_at": record.get("last_seen_at"),
                "misses": record.get("misses", 0),
                "formal_ready": bool(quality.get("formal_ready")),
                "data_quality": quality,
            })
        rows.sort(key=lambda row: (-int(row.get("formal_ready", False)), -int(_safe_float(row.get("discovery_score"))), row["symbol"]))
        return {
            "status": "READY",
            "version": DYNAMIC_UNIVERSE_VERSION,
            "generation": self.generation,
            "last_snapshot_id": self.last_snapshot_id,
            "max_active": self.max_active,
            "active_count": len(rows),
            "formal_ready_count": sum(bool(row.get("formal_ready")) for row in rows),
            "rows": rows,
            "prepare_failures": list(self.prepare_failures.values()),
        }
