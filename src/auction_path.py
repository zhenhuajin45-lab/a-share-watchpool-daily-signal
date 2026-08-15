# coding: utf-8
"""集合竞价第三方报价路径分析。

腾讯/新浪/东方财富公开报价并非交易所逐笔委托事实，因此这里只称
CALL_AUCTION_QUOTE_PROXY。除数据冲突外，价格和盘口只提供支持/谨慎证据，
不能用单个高开阈值永久否决盘中的真实承接事件。
"""

from __future__ import annotations

import math
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Dict, List, Optional


def _safe_float(value: Any, default: float = 0.0) -> float:
    try:
        result = float(value)
        return result if math.isfinite(result) else default
    except (TypeError, ValueError):
        return default


@dataclass(frozen=True)
class AuctionPathConfig:
    max_buy_gap: float = 0.03
    min_buy_gap: float = -0.035
    fake_strength_retreat: float = 0.012
    fake_strength_bid_drop: float = 0.50
    conflict_ratio: float = 0.003
    severe_conflict_ratio: float = 0.030
    support_imbalance: float = 0.15
    veto_imbalance: float = -0.35


DEFAULT_AUCTION_CONFIG = AuctionPathConfig()


def _top5_amounts(row: Dict[str, Any]) -> Dict[str, float]:
    bid_amount = 0.0
    ask_amount = 0.0
    for level in row.get("top5") or []:
        bid_amount += _safe_float(level.get("bid_p")) * _safe_float(level.get("bid_v"))
        ask_amount += _safe_float(level.get("ask_p")) * _safe_float(level.get("ask_v"))
    if bid_amount <= 0:
        bid_amount = _safe_float(row.get("bid1_price")) * _safe_float(row.get("bid1_volume"))
    if ask_amount <= 0:
        ask_amount = _safe_float(row.get("ask1_price")) * _safe_float(row.get("ask1_volume"))
    imbalance = (bid_amount - ask_amount) / (bid_amount + ask_amount) if bid_amount + ask_amount > 0 else None
    return {"top5_bid_amount": bid_amount, "top5_ask_amount": ask_amount, "amount_imbalance": imbalance}


class AuctionPathAnalyzer:
    def __init__(self, config: AuctionPathConfig = DEFAULT_AUCTION_CONFIG):
        self.config = config
        self.snapshots: List[Dict[str, Any]] = []
        self.latest_analysis: Dict[str, Any] = {"rows": [], "by_symbol": {}}

    def reset(self) -> None:
        self.snapshots.clear()
        self.latest_analysis = {"rows": [], "by_symbol": {}}

    def add_snapshot(self, snapshot: Dict[str, Any]) -> Dict[str, Any]:
        enriched = dict(snapshot)
        enriched_rows = []
        for raw in snapshot.get("rows", []):
            row = dict(raw)
            row.update(_top5_amounts(row))
            row["data_kind"] = "CALL_AUCTION_QUOTE_PROXY"
            enriched_rows.append(row)
        enriched["rows"] = enriched_rows
        self.snapshots.append(enriched)
        self.latest_analysis = self.analyze()
        return enriched

    def analyze(self) -> Dict[str, Any]:
        histories: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
        for snapshot in self.snapshots:
            snapshot_at = str(snapshot.get("snapshot_at") or "")
            for raw in snapshot.get("rows", []):
                row = dict(raw)
                row["snapshot_at"] = snapshot_at
                histories[str(row.get("symbol"))].append(row)

        rows = []
        for symbol, history in histories.items():
            history.sort(key=lambda row: row.get("snapshot_at") or "")
            usable_history = [row for row in history if row.get("source_freshness") != "STALE"]
            if not usable_history:
                final = history[-1]
                rows.append({
                    "symbol": symbol,
                    "name": final.get("name", ""),
                    "snapshot_count": len(history),
                    "fresh_snapshot_count": 0,
                    "final_gap": None,
                    "gate": "NEUTRAL",
                    "label": "STALE_SOURCE_DATA",
                    "hard_veto": False,
                    "allows_intraday_validation": True,
                    "reasons": [f"源行情日期{str(final.get('source_timestamp') or 'UNKNOWN')[:10]}与目标交易日不一致"],
                    "confidence": "LOW",
                    "data_kind": "CALL_AUCTION_QUOTE_PROXY",
                    "timestamp_quality": final.get("timestamp_quality", "UNKNOWN"),
                    "source_freshness": "STALE",
                })
                continue
            history = usable_history
            final = history[-1]
            locked_history = [
                row for row in history
                if str(row.get("snapshot_at") or "").replace("T", " ")[-8:] >= "09:20:00"
            ] or history[-1:]
            prev_close = _safe_float(final.get("prev_close"))
            final_gap = _safe_float(final.get("pct_chg"))
            peak_gap = max((_safe_float(row.get("pct_chg")) for row in history), default=final_gap)
            price_retreat = max(peak_gap - final_gap, 0.0)
            bid_amounts = [_safe_float(row.get("top5_bid_amount")) for row in history]
            max_bid_amount = max(bid_amounts, default=0.0)
            final_bid_amount = bid_amounts[-1] if bid_amounts else 0.0
            bid_drop_ratio = (max_bid_amount - final_bid_amount) / max_bid_amount if max_bid_amount > 0 else None
            locked_start_gap = _safe_float(locked_history[0].get("pct_chg"), final_gap)
            locked_gap_change = final_gap - locked_start_gap
            locked_start_bid_amount = _safe_float(locked_history[0].get("top5_bid_amount"))
            locked_bid_retention = (
                final_bid_amount / locked_start_bid_amount
                if locked_start_bid_amount > 0 else None
            )
            cross_spread = _safe_float(final.get("cross_source_spread"), float("nan"))
            conflict_ratio = cross_spread / prev_close if math.isfinite(cross_spread) and prev_close > 0 else None
            conflict = bool(
                final.get("auction_path") == "CROSS_SOURCE_CONFLICT"
                or (conflict_ratio is not None and conflict_ratio > self.config.conflict_ratio)
            )
            severe_conflict = bool(
                conflict_ratio is not None
                and conflict_ratio > self.config.severe_conflict_ratio
                and int(_safe_float(final.get("provider_count"), 0)) >= 2
            )
            final_imbalance = final.get("amount_imbalance")
            no_depth = not bool(final.get("top5")) or final_imbalance is None
            gate, label, reasons = "NEUTRAL", "NEUTRAL", []
            hard_veto = False
            if severe_conflict:
                gate, label, hard_veto = "HARD_VETO", "SEVERE_DATA_CONFLICT", True
                reasons.append(f"多源价格冲突达到{conflict_ratio:.2%}，超过严重冲突阈值")
            elif conflict:
                # 公开行情代理在集合竞价阶段可能存在源间刷新不同步。普通冲突不能
                # 永久剥夺盘中真实Tick修复资格，否则会系统性漏掉健康趋势。
                gate, label = "CAUTION", "DATA_CONFLICT_LIVE_REPAIR"
                reasons.append(
                    f"多源价格存在{conflict_ratio:.2%}差异，降级为谨慎并等待开盘真实承接修复"
                    if conflict_ratio is not None else "多源价格存在差异，等待开盘真实承接修复"
                )
            elif final_gap > self.config.max_buy_gap:
                gate, label = "CAUTION", "HIGH_GAP_NEEDS_CONFIRMATION"
                reasons.append(f"最终竞价代理涨幅{final_gap:.2%}，需要板块共振和开盘承接确认")
            elif final_gap < self.config.min_buy_gap:
                gate, label = "CAUTION", "WEAK_GAP_NEEDS_REPAIR"
                reasons.append(f"最终竞价代理跌幅{final_gap:.2%}，需要开盘修复确认")
            elif price_retreat >= self.config.fake_strength_retreat and bid_drop_ratio is not None and bid_drop_ratio >= self.config.fake_strength_bid_drop:
                gate, label = "CAUTION", "FAKE_STRENGTH"
                reasons.append(f"价格回落{price_retreat:.2%}且买盘金额回撤{bid_drop_ratio:.1%}")
            elif final_imbalance is not None and final_imbalance <= self.config.veto_imbalance:
                gate, label = "CAUTION", "SELL_PRESSURE"
                reasons.append(f"最终盘口金额不平衡{final_imbalance:.2f}")
            elif (
                not no_depth
                and -0.015 <= final_gap <= self.config.max_buy_gap
                and final_imbalance is not None
                and final_imbalance >= self.config.support_imbalance
                and price_retreat < 0.008
            ):
                gate, label = "SUPPORT", "STABLE_SUPPORT"
                reasons.append("价格路径稳定且盘口金额偏多")
            else:
                reasons.append("竞价代理未形成明确支持或否决")

            provider_count = int(_safe_float(final.get("provider_count"), 0))
            confidence = "MEDIUM" if len(history) >= 3 and provider_count >= 2 and not no_depth else "LOW"
            row = {
                "symbol": symbol,
                "name": final.get("name", ""),
                "snapshot_count": len(history),
                "fresh_snapshot_count": len(history),
                "final_gap": final_gap,
                "peak_gap": peak_gap,
                "price_retreat": price_retreat,
                "max_bid_amount": max_bid_amount,
                "final_bid_amount": final_bid_amount,
                "bid_drop_ratio": bid_drop_ratio,
                "locked_snapshot_count": len(locked_history),
                "locked_start_gap": locked_start_gap,
                "locked_gap_change": locked_gap_change,
                "locked_bid_retention": locked_bid_retention,
                "final_amount_imbalance": final_imbalance,
                "provider_count": provider_count,
                "cross_source_spread": final.get("cross_source_spread"),
                "cross_source_spread_ratio": conflict_ratio,
                "no_depth": no_depth,
                "gate": gate,
                "label": label,
                "hard_veto": hard_veto,
                "allows_intraday_validation": not hard_veto,
                "reasons": reasons,
                "confidence": confidence,
                "data_kind": "CALL_AUCTION_QUOTE_PROXY",
                "timestamp_quality": final.get("timestamp_quality", "UNKNOWN"),
                "source_freshness": final.get("source_freshness", "UNKNOWN"),
            }
            rows.append(row)
        rows.sort(key=lambda row: ({"HARD_VETO": 0, "CAUTION": 1, "SUPPORT": 2, "NEUTRAL": 3}.get(row["gate"], 9), -_safe_float(row.get("final_gap")), row["symbol"]))
        return {
            "analyzed_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "snapshot_count": len(self.snapshots),
            "rows": rows,
            "by_symbol": {row["symbol"]: row for row in rows},
            "fresh_symbol_count": sum(row.get("source_freshness") != "STALE" for row in rows),
            "stale_symbol_count": sum(row.get("source_freshness") == "STALE" for row in rows),
            "data_kind": "CALL_AUCTION_QUOTE_PROXY",
            "can_confirm_buy_alone": False,
        }

    def gate_for(self, symbol: str) -> Dict[str, Any]:
        return self.latest_analysis.get("by_symbol", {}).get(symbol, {
            "symbol": symbol,
            "gate": "NEUTRAL",
            "label": "NO_AUCTION_EVIDENCE",
            "hard_veto": False,
            "allows_intraday_validation": True,
            "confidence": "LOW",
            "data_kind": "CALL_AUCTION_QUOTE_PROXY",
        })
