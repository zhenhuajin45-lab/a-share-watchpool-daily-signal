# coding: utf-8
"""V16 严格事前、全日时间线仿真。

输入只使用目标时点以前已经落盘的事实：
- 盘前候选：D-1 前复权日线；
- 集合竞价：09:25:10 以前的真实竞价快照；
- 盘中：实时服务约每 5 秒保存的 Tick/五档代理；
- 板块：实时服务当时保存的全市场完整横截面；旧日期没有完整横截面时才降级为匹配板块代理。

2026-08-13 起实时日志已经保存约 968 个有效板块的完整横截面、板块广度和
当时涨停梯队。本脚本严格按 ``logged_at <= event_ts`` 恢复当时快照；绝不使用
盘后当前接口回填上午。V16 结构择时层保持零权重影子口径，不篡改正式信号。
"""

from __future__ import annotations

import argparse
import json
import math
import os
import time
from collections import Counter, defaultdict, deque
from datetime import datetime, timedelta
from pathlib import Path
from statistics import mean
from typing import Any, Deque, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple

import pandas as pd

from action_layer import decide_event_actions
from auction_path import AuctionPathAnalyzer
from capital_behavior_engine import CapitalBehaviorEngine
from continuation_engine import TrendContinuationAnalyzer
from feishu_cards import build_report_card, build_signal_card, signal_template, validate_card
from intraday_engine import IntradayEventEngine
from limit_behavior import LimitBehaviorEngine
from market_permission import MarketPermissionEngine
from market_sector_feed import FullMarketSectorRadar
from live_signal_service import (
    DailyCandidateBuilder,
    FeishuNotifier,
    LiveSignalService,
    load_pool_entries,
    load_taxonomy,
)
from multitimeframe_engine import MultiTimeframeIndicatorEngine
from review_today_v4 import _post_event_stats, _raw_tick
from sector_health import LiveSectorHealthEngine
from signal_rules import compute_features
from structured_timing import (
    StructuredTimingEngine,
    build_daily_timing_context,
    format_structured_timing_line,
)


ROOT = Path(r"D:\codex\a_share_rotation")
ORIGINAL_POOL = ROOT / "universe" / "selected_pool_20260809.txt"
RESEARCH_POOL = ROOT / "universe" / "research_pool_20260811.txt"
DAILY_ROOT = ROOT / "data" / "goldminer" / "daily_adjust_prev_current"
MINUTE_ROOT = ROOT / "data" / "goldminer" / "live_1m_seed"
FIXED_SLOTS = ("10:00:00", "11:45:00", "14:00:00", "14:55:00")
RULES_VERSION = "V17_T1_LEDGER_LINEAGE_FULL_PATH_REPLAY_20260814"
BOARD_PROXY_VERSION = "historical_full_market_968_asof_snapshot_v2"
VIRTUAL_LEDGER_FILE = ROOT / "data" / "live_signal" / "virtual_signal_positions.json"


def _safe_float(value: Any, default: float = 0.0) -> float:
    try:
        result = float(value)
        return result if math.isfinite(result) else default
    except (TypeError, ValueError):
        return default


def _dt(value: Any) -> datetime:
    return pd.Timestamp(value).to_pydatetime().replace(tzinfo=None)


def _clock(value: Any) -> str:
    text = str(value or "")
    return text[11:19] if len(text) >= 19 else text


def _short(symbol: str) -> str:
    return str(symbol or "").split(".")[-1]


def _pct(value: Optional[float]) -> str:
    return "—" if value is None else f"{value:+.2%}"


def _price(value: Any) -> str:
    number = _safe_float(value, float("nan"))
    if not math.isfinite(number):
        return "—"
    return f"{number:.3f}".rstrip("0").rstrip(".")


def _read_jsonl(path: Path) -> Iterable[Dict[str, Any]]:
    if not path.exists():
        return
    with path.open("r", encoding="utf-8-sig", errors="ignore") as handle:
        for line in handle:
            if not line.strip():
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(row, dict):
                yield row


class HistoricalMatchedBoardAdapter:
    """用当时已保存的候选匹配板块排名生成保守动态状态。

    这不是 V9 完整板块健康分。只有同一板块至少出现两个不同刷新快照，且排名、
    涨幅与持续性同时满足时，才给 entry_support；未知时宁可不加分。
    """

    def __init__(self, market_snapshots: Sequence[Tuple[datetime, int]]):
        self.market_snapshots = list(market_snapshots)
        self.history: Dict[str, Deque[Dict[str, Any]]] = defaultdict(lambda: deque(maxlen=8))
        self.latest: Dict[str, Dict[str, Any]] = {}

    def universe_at(self, now: datetime) -> int:
        value = 1000
        for ts, count in self.market_snapshots:
            if ts > now:
                break
            if count > 0:
                value = count
        return max(value, 1)

    def update(self, raw: Mapping[str, Any], now: datetime) -> Dict[str, Any]:
        board_code = str(raw.get("board_code") or "")
        board_name = str(raw.get("board_name") or "")
        if not board_code and not board_name:
            return {
                "status": "NO_HISTORICAL_MATCH",
                "source": BOARD_PROXY_VERSION,
                "entry_support": False,
                "rotation_caution": False,
                "no_lookahead": True,
            }
        key = board_code or board_name
        universe = self.universe_at(now)
        rank = max(1, int(_safe_float(raw.get("board_rank"), universe)))
        percentile = max(0.0, min(1.0, 1.0 - (rank - 0.5) / universe))
        board_pct = _safe_float(raw.get("board_pct"))
        inflow = _safe_float(raw.get("main_net_inflow"))
        signature = (
            round(_safe_float(raw.get("board_price")), 4),
            round(board_pct, 5),
            rank,
            round(inflow, -3),
        )
        history = self.history[key]
        if not history or history[-1]["signature"] != signature:
            history.append({
                "event_ts": now,
                "signature": signature,
                "rank": rank,
                "percentile": percentile,
                "board_pct": board_pct,
                "main_net_inflow": inflow,
            })
        recent = list(history)[-5:]
        persistence = sum(
            row["percentile"] >= 0.75 and row["board_pct"] > 0
            for row in recent
        ) / max(len(recent), 1)
        prior = recent[-2] if len(recent) >= 2 else None
        percentile_delta = percentile - _safe_float((prior or {}).get("percentile"), percentile)
        enough_path = len(recent) >= 2 and (recent[-1]["event_ts"] - recent[0]["event_ts"]).total_seconds() >= 120

        if enough_path and percentile >= 0.90 and persistence >= 0.60 and board_pct > 0:
            rotation_state = "SUSTAINED_LEADER"
        elif enough_path and percentile >= 0.80 and percentile_delta >= 0.08 and board_pct > 0:
            rotation_state = "ROTATION_IN"
        elif enough_path and percentile >= 0.68 and persistence >= 0.60 and board_pct > 0:
            rotation_state = "HEALTHY_RISING"
        elif percentile >= 0.95 and not enough_path:
            rotation_state = "FLASH_HEAT"
        elif enough_path and (percentile_delta <= -0.15 or (board_pct < 0 and inflow < 0)):
            rotation_state = "ROTATION_OUT"
        elif percentile <= 0.25 or (board_pct < 0 and inflow < 0):
            rotation_state = "WEAK"
        else:
            rotation_state = "NEUTRAL"

        entry_support = bool(
            enough_path
            and percentile >= 0.68
            and persistence >= 0.60
            and board_pct > 0
            and rotation_state in {"SUSTAINED_LEADER", "ROTATION_IN", "HEALTHY_RISING"}
        )
        rotation_caution = bool(rotation_state in {"FLASH_HEAT", "ROTATION_OUT", "WEAK"})
        # 没有历史广度时保持中性 0.5，不能伪造板块内部上涨家数。
        context = {
            **dict(raw),
            "status": "HISTORICAL_PROXY",
            "source": BOARD_PROXY_VERSION,
            "market_universe_count": universe,
            "health_percentile": percentile,
            "board_percentile": percentile,
            "health_score_raw": round(100.0 * percentile, 2),
            "breadth": 0.5,
            "breadth_status": "UNAVAILABLE_IN_LEGACY_LOG",
            "inflow_ratio": 1.0 if inflow > 0 else (-1.0 if inflow < 0 else 0.0),
            "rotation_state": rotation_state,
            "entry_support": entry_support,
            "rotation_caution": rotation_caution,
            "snapshot_count": len(history),
            "top_quartile_persistence": persistence,
            "percentile_delta": percentile_delta,
            "proxy_asof": now.isoformat(),
            "no_lookahead": True,
            "historical_limitations": [
                "旧日志仅保留候选匹配板块的实时排名/涨幅/净流入",
                "当时全市场板块广度、量比和涨停梯队未完整落盘",
            ],
        }
        self.latest[key] = context
        return context

    def snapshot(self) -> List[Dict[str, Any]]:
        rows = list(self.latest.values())
        return sorted(rows, key=lambda row: (-_safe_float(row.get("health_percentile")), str(row.get("board_name"))))


class HistoricalFullMarketAdapter:
    """按历史时间推进完整板块横截面，并复用正式候选匹配规则。"""

    def __init__(self, records: Sequence[Mapping[str, Any]]):
        self.records = sorted(
            (( _dt(row.get("logged_at")), dict(row)) for row in records if row.get("logged_at")),
            key=lambda item: item[0],
        )
        self.index = 0
        self.current_ts: Optional[datetime] = None
        self.radar = FullMarketSectorRadar()
        self.latest: Dict[str, Any] = {"status": "UNINITIALIZED", "rows": [], "by_name": {}}
        self.candidate_context_cache: Dict[str, Dict[str, Any]] = {}

    @staticmethod
    def _restore(record: Mapping[str, Any]) -> Dict[str, Any]:
        rows = [dict(row) for row in (record.get("all_market_compact") or record.get("top_boards") or [])]
        rows.sort(key=lambda row: (-_safe_float(row.get("health_score_raw")), str(row.get("board_name") or "")))
        for index, row in enumerate(rows, 1):
            row.setdefault("board_rank", index)
            row.setdefault("market_universe_count", len(rows))
        return {
            key: value for key, value in dict(record).items()
            if key not in {"logged_at", "top_boards", "all_market_compact"}
        } | {
            "asof": record.get("asof") or record.get("logged_at"),
            "rows": rows,
            "by_name": {str(row.get("board_name")): row for row in rows if row.get("board_name")},
            "historical_logged_at": record.get("logged_at"),
            "historical_replay_no_lookahead": True,
        }

    def advance(self, now: datetime) -> Dict[str, Any]:
        while self.index < len(self.records) and self.records[self.index][0] <= now:
            self.current_ts, record = self.records[self.index]
            self.latest = self._restore(record)
            self.radar.latest = self.latest
            self.candidate_context_cache.clear()
            self.index += 1
        return self.latest

    def context_for_candidate(self, candidate: Mapping[str, Any], now: datetime) -> Dict[str, Any]:
        self.advance(now)
        symbol = str(candidate.get("symbol") or "")
        if symbol in self.candidate_context_cache:
            return dict(self.candidate_context_cache[symbol])
        context = {
            **self.radar.context_for_candidate(candidate),
            "source": BOARD_PROXY_VERSION,
            "proxy_asof": self.current_ts.isoformat() if self.current_ts else None,
            "no_lookahead": True,
        }
        self.candidate_context_cache[symbol] = context
        return dict(context)

    def snapshot(self) -> List[Dict[str, Any]]:
        return list(self.latest.get("rows") or [])


def _load_market_timeline(day_root: Path) -> List[Tuple[datetime, int]]:
    rows: List[Tuple[datetime, int]] = []
    last_count = 1000
    for record in _read_jsonl(day_root / "market_sector_snapshots.jsonl"):
        ts = _dt(record.get("logged_at"))
        count = int(_safe_float(record.get("row_count"), last_count))
        if count > 0:
            last_count = count
        rows.append((ts, last_count))
    return sorted(rows)


def _load_market_records(day_root: Path) -> List[Dict[str, Any]]:
    return list(_read_jsonl(day_root / "market_sector_snapshots.jsonl"))


def _load_logged_daily(day_root: Path, trade_date: str) -> Dict[str, Any]:
    """读取当日盘前实际落盘的D-1候选，避免陈旧日线缓存回退一天。"""
    payloads = []
    for record in _read_jsonl(day_root / "daily_candidates.jsonl"):
        payload = record.get("payload") or {}
        if payload.get("candidates") and str(payload.get("asof") or "") < trade_date:
            payloads.append(payload)
    if not payloads:
        raise RuntimeError("缺少当日盘前实际候选截面")
    daily = dict(payloads[-1])
    daily["replay_source"] = str(day_root / "daily_candidates.jsonl")
    daily["replay_source_is_original_premarket_snapshot"] = True
    return daily


def _daily_from_minute(frame: pd.DataFrame, trade_date: str) -> pd.DataFrame:
    source = frame.copy()
    source["eob"] = pd.to_datetime(source["eob"], errors="coerce")
    if getattr(source["eob"].dt, "tz", None) is not None:
        source["eob"] = source["eob"].dt.tz_convert("Asia/Shanghai").dt.tz_localize(None)
    source = source[source["eob"].dt.strftime("%Y-%m-%d") < trade_date].copy()
    source["trade_date"] = source["eob"].dt.strftime("%Y-%m-%d")
    return source.groupby("trade_date", as_index=False).agg(
        symbol=("symbol", "last"), eob=("eob", "last"), open=("open", "first"),
        high=("high", "max"), low=("low", "min"), close=("close", "last"),
        volume=("volume", "sum"), amount=("amount", "sum"),
    )


def _load_replay_positions(trade_date: str) -> Dict[str, Dict[str, Any]]:
    if not VIRTUAL_LEDGER_FILE.exists():
        return {}
    try:
        payload = json.loads(VIRTUAL_LEDGER_FILE.read_text(encoding="utf-8"))
    except Exception:
        return {}
    return {
        symbol: dict(row) for symbol, row in (payload.get("positions") or {}).items()
        if str(row.get("entry_date") or "") < trade_date
    }


def _load_daily(pool: Sequence[str], membership: Mapping[str, Mapping[str, Any]], asof: str) -> Dict[str, Any]:
    frames: Dict[str, pd.DataFrame] = {}
    for symbol in pool:
        path = DAILY_ROOT / f"{symbol}_1d.pkl"
        if path.exists():
            frames[symbol] = pd.read_pickle(path)
    builder = DailyCandidateBuilder(
        pool,
        load_taxonomy(),
        pool_membership=membership,
        volume_effect_mode="SHADOW",
    )
    return builder.build(frames, asof=asof)


def _load_auction(day_root: Path, daily: Dict[str, Any]) -> Dict[str, Any]:
    analyzer = AuctionPathAnalyzer()
    cutoff = _dt(f"{day_root.name} 09:25:30")
    for record in _read_jsonl(day_root / "auction_snapshots.jsonl"):
        if record.get("logged_at") and _dt(record.get("logged_at")) > cutoff:
            continue
        snapshot = record.get("snapshot") or {}
        if snapshot.get("rows"):
            analyzer.add_snapshot(snapshot)
    if not analyzer.snapshots:
        return {"rows": [], "by_symbol": {}, "auction_group_facts": {}}
    helper = LiveSignalService(notifier=FeishuNotifier(dry_run=True))
    return helper._apply_auction_group_context(analyzer.latest_analysis)


def _load_observations(day_root: Path, trade_date: str, pool: Sequence[str]) -> List[Dict[str, Any]]:
    allowed = set(pool)
    start = _dt(f"{trade_date} 09:30:00")
    morning_end = _dt(f"{trade_date} 11:30:00")
    afternoon_start = _dt(f"{trade_date} 13:00:00")
    end = _dt(f"{trade_date} 15:00:00")
    rows: List[Dict[str, Any]] = []
    for record in _read_jsonl(day_root / "tick_samples.jsonl"):
        observation = dict(record.get("observation") or {})
        symbol = str(observation.get("symbol") or "")
        if symbol not in allowed or not observation.get("event_ts"):
            continue
        ts = _dt(observation["event_ts"])
        if not (start <= ts <= morning_end or afternoon_start <= ts <= end):
            continue
        observation["event_ts"] = ts.isoformat()
        observation["_historical_market_sector"] = record.get("market_sector") or {}
        rows.append(observation)
    rows.sort(key=lambda row: (str(row.get("event_ts")), str(row.get("symbol"))))
    return rows


def _continuation_sector(local: Mapping[str, Any], market: Mapping[str, Any]) -> Dict[str, Any]:
    result = dict(local)
    if market.get("board_code") or market.get("board_name"):
        result.update({
            "market_board_pct": market.get("board_pct"),
            "market_board_rank": market.get("board_rank"),
            "market_board_percentile": market.get("health_percentile"),
            "market_board_health_score": market.get("health_score_raw"),
            "market_board_state": market.get("rotation_state"),
            "market_board_entry_support": market.get("entry_support"),
            "market_board_rotation_caution": market.get("rotation_caution"),
            "market_board_breadth": market.get("breadth"),
            "market_board_persistence": market.get("top_quartile_persistence"),
            "market_universe_count": market.get("market_universe_count"),
        })
    return result


def _candidate_action(candidate: Mapping[str, Any], has_position: bool, latest_event: Optional[Mapping[str, Any]]) -> str:
    if latest_event:
        decision = latest_event.get("action_decision") or {}
        leg = (decision.get("existing_position") if has_position else decision.get("empty_position")) or {}
        return f"{leg.get('label', '等待')}：{leg.get('instruction', '')}"
    if has_position:
        return "继续持有观察：尚无新的减仓/卖出事件。"
    if candidate.get("action") in {"BUY", "WATCH"}:
        return "等待实时买点：盘前候选不是立即买入。"
    return "等待：当前未进入新开仓候选。"


def _summary_snapshot(
    slot: str,
    latest_rows: Mapping[str, Mapping[str, Any]],
    candidates: Mapping[str, Mapping[str, Any]],
    events: Sequence[Dict[str, Any]],
    sector: LiveSectorHealthEngine,
    board_adapter: HistoricalMatchedBoardAdapter,
    virtual_positions: Mapping[str, Mapping[str, Any]],
    market_permission: Optional[Mapping[str, Any]] = None,
) -> Dict[str, Any]:
    latest_events: Dict[str, Dict[str, Any]] = {}
    for event in events:
        latest_events[str(event.get("symbol"))] = event
    rows = []
    for symbol, state in latest_rows.items():
        candidate = candidates.get(symbol) or {}
        observation = state.get("observation") or {}
        close = _safe_float(candidate.get("close"))
        price = _safe_float(observation.get("price"))
        rows.append({
            "symbol": symbol,
            "name": candidate.get("name", ""),
            "price": price,
            "return": price / close - 1.0 if close > 0 and price > 0 else None,
            "live_strength": state.get("live_strength"),
            "candidate_action": candidate.get("action"),
            "daily_route": candidate.get("daily_route"),
            "local_sector": state.get("local_sector") or {},
            "market_sector": state.get("market_sector") or {},
            "continuation": state.get("continuation") or {},
            "multitimeframe": state.get("multitimeframe") or {},
            "capital_behavior": state.get("capital_behavior") or {},
            "structured_timing": state.get("structured_timing") or {},
            "market_permission": state.get("market_permission") or market_permission or {},
            "action_text": _candidate_action(candidate, symbol in virtual_positions, latest_events.get(symbol)),
            "has_virtual_position": symbol in virtual_positions,
            "last_event": latest_events.get(symbol),
        })
    rows.sort(key=lambda row: (-int(_safe_float(row.get("live_strength"))), -_safe_float(row.get("return")), row["symbol"]))
    group_rows = [row for row in sector.snapshot().get("groups", []) if int(_safe_float(row.get("observed_count"))) >= 2]
    group_rows.sort(key=lambda row: (-int(_safe_float(row.get("score"))), str(row.get("theme"))))
    return {
        "slot": slot,
        "data_asof": max((str((row.get("observation") or {}).get("event_ts") or "") for row in latest_rows.values()), default=""),
        "coverage": len(latest_rows),
        "pool_size": len(candidates),
        "top_rows": rows[:8],
        "all_rows": rows,
        "sector_groups": group_rows[:6],
        "market_boards": board_adapter.snapshot()[:8],
        "event_count": len(events),
        "event_type_counts": dict(Counter(row.get("event") for row in events)),
        "virtual_position_count": len(virtual_positions),
        "market_permission": dict(market_permission or {}),
    }


def _route_cn(value: str) -> str:
    return {
        "TREND_CONTINUATION": "趋势延续",
        "TREND_PULLBACK": "趋势回踩",
        "HOLD_PROTECT": "持有保护",
        "NO_SETUP": "暂无结构",
    }.get(str(value), str(value or "—"))


def _event_cn(event: Mapping[str, Any]) -> str:
    return {
        "BUY_EVENT_WATCH": "T+1新开仓",
        "OPPORTUNITY_EVENT_WATCH": "趋势机会/等待",
        "DISCOVERY_EVENT_WATCH": "突发趋势发现",
        "RISK_EVENT_WATCH": "当日失效风险",
        "SELL_EVENT_WATCH": "减仓/卖出",
    }.get(str(event.get("event")), str(event.get("event") or "事件"))


def _pattern_cn(value: str) -> str:
    return {
        "PRELIMINARY_TREND_WATCH": "预观察",
        "ARMED_WAIT_PULLBACK": "已武装，等回踩",
        "PULLBACK_IN_PROGRESS": "回踩进行中",
        "TREND_PULLBACK_RECLAIM": "回踩后重新收复",
        "CAPITAL_FLOW_CONTINUATION": "持续资金流确认",
        "CAPITAL_LED_EARLY_REVERSAL": "早期资金转折",
        "PLATFORM_REACCELERATION_SHADOW": "平台再加速（影子）",
        "SUDDEN_TREND_ARMED": "突发趋势已武装",
        "TREND_LIMIT_LOCKED": "趋势已发现但封板不可成交",
        "SAME_DAY_CONTINUATION_INVALIDATION": "当日延续失效",
        "CAPITAL_OUTFLOW_CONFIRMED": "持续流出确认",
        "TOP_VOLUME_CONTRACTION_DEFENSE": "顶部缩量/量退防守",
    }.get(str(value), str(value or "—"))


def _premarket_card(trade_date: str, daily_asof: str, daily: Mapping[str, Any]) -> Tuple[Dict[str, Any], str]:
    candidates = [row for row in daily.get("candidates", []) if row.get("action") in {"BUY", "WATCH"}]
    candidates.sort(key=lambda row: -int(_safe_float(row.get("candidate_rank_score"))))
    lines = []
    for row in candidates[:6]:
        volume = row.get("volume_soft_factor") or {}
        lines.append(
            f"- **{row.get('name')}（{_short(row.get('symbol'))}）**｜{_route_cn(row.get('daily_route'))}｜"
            f"{int(_safe_float(row.get('candidate_rank_score')))}/100｜慢J {_safe_float(row.get('slow_j')):.1f}\n"
            f"  动作：空仓等待盘中正式买点；量能{volume.get('status', 'NONE')}仅作影子记录。"
        )
    text = (
        f"【V16真实顺序回放｜仅模拟｜盘前候选 {trade_date}】\n"
        f"事前日线截止：{daily_asof}；当日已生效研究池覆盖{daily.get('available_size')}/{daily.get('pool_size')}。\n"
        + "\n".join(lines)
    )
    card = build_report_card(
        f"🧪 V16真实顺序回放｜盘前候选 {trade_date}",
        template="blue",
        fields=[
            ("仿真时点", "08:45（盘前）"),
            ("日线截止", daily_asof),
            ("今日事前池", f"已生效合并池 {daily.get('available_size')}/{daily.get('pool_size')}"),
            ("BUY/WATCH", f"{len(candidates)}只"),
        ],
        sections=[
            ("📌 盘前结论", "盘前只缩小范围，不直接买。所有新开仓必须等竞价、板块、多周期和盘中资金行为共同确认。"),
            ("👀 优先候选", "\n".join(lines) or "- 今日无BUY/WATCH候选。"),
            ("🧱 时间边界", f"以上只使用{daily_asof}及以前数据；每只股票按其入池登记日后的下一交易日才参与事前成绩。"),
        ],
        footer="历史仿真｜不下单｜量能正向因子为SHADOW｜A股新买入严格T+1",
    )
    return card, text


def _auction_card(trade_date: str, auction: Mapping[str, Any], candidate_map: Mapping[str, Mapping[str, Any]]) -> Tuple[Dict[str, Any], str]:
    rows = list((auction.get("by_symbol") or {}).values())
    support = [row for row in rows if row.get("gate") == "SUPPORT"]
    caution = [row for row in rows if row.get("gate") == "CAUTION"]
    support.sort(key=lambda row: -_safe_float(row.get("final_gap")))
    caution.sort(key=lambda row: -abs(_safe_float(row.get("final_gap"))))
    support_lines = [
        f"- **{candidate_map.get(str(row.get('symbol')), {}).get('name', row.get('symbol'))}（{_short(row.get('symbol'))}）**｜"
        f"竞价{_pct(row.get('final_gap'))}｜{row.get('label')}"
        for row in support[:6]
    ]
    caution_lines = [
        f"- {candidate_map.get(str(row.get('symbol')), {}).get('name', row.get('symbol'))}（{_short(row.get('symbol'))}）｜"
        f"竞价{_pct(row.get('final_gap'))}｜开盘后等待修复/承接"
        for row in caution[:5]
    ]
    text = (
        f"【V16真实顺序回放｜仅模拟｜集合竞价 {trade_date} 09:26:30】\n"
        f"支持{len(support)}只，谨慎{len(caution)}只。竞价只改变验证难度，不单独触发买入。"
    )
    card = build_report_card(
        f"🧪 V16真实顺序回放｜集合竞价 09:26:30",
        template="turquoise",
        fields=[
            ("真实竞价快照", "截至09:25:10"),
            ("支持", f"{len(support)}只"),
            ("谨慎", f"{len(caution)}只"),
            ("硬否决", f"{sum(bool(row.get('hard_veto')) for row in rows)}只"),
        ],
        sections=[
            ("✅ 竞价支持", "\n".join(support_lines) or "- 暂无竞价直接支持；全部等待开盘验证。"),
            ("⚠️ 需要开盘修复", "\n".join(caution_lines) or "- 暂无重点谨慎项。"),
            ("🎯 执行口径", "竞价高开不等于追涨，低开也不机械否决；开盘后只接受VWAP承接、板块同步和完整分钟周期确认。"),
        ],
        footer="历史仿真｜真实竞价路径｜不下单｜下一固定总结10:00",
    )
    return card, text


def _summary_card(trade_date: str, snapshot: Mapping[str, Any]) -> Tuple[Dict[str, Any], str]:
    slot = str(snapshot.get("slot"))
    permission = snapshot.get("market_permission") or {}
    opportunity_lines = []
    for row in snapshot.get("top_rows", [])[:5]:
        market = row.get("market_sector") or {}
        capital = row.get("capital_behavior") or {}
        mtf = row.get("multitimeframe") or {}
        timing = row.get("structured_timing") or {}
        opportunity_lines.append(
            f"- **{row.get('name')}（{_short(row.get('symbol'))}）**｜{_pct(row.get('return'))}｜强度{int(_safe_float(row.get('live_strength')))}/100\n"
            f"  动作：{row.get('action_text')}\n"
            f"  资金：{capital.get('regime_cn', capital.get('phase_cn', '样本积累'))} {capital.get('score', '—')}｜"
            f"多周期{mtf.get('alignment', '预热')} {mtf.get('score', '—')}｜"
            f"板块{market.get('board_name', '未匹配')}·{market.get('rotation_state', 'UNAVAILABLE')}\n"
            f"  {format_structured_timing_line(timing)}"
        )
    sector_lines = [
        f"- {row.get('theme')}｜{row.get('state')}｜{int(_safe_float(row.get('score')))}/100｜"
        f"上涨{int(_safe_float(row.get('up_count')))}/{int(_safe_float(row.get('observed_count')))}"
        for row in snapshot.get("sector_groups", [])[:4]
    ]
    board_lines = [
        f"- {row.get('board_name')}｜{row.get('rotation_state')}｜涨幅{_pct(row.get('board_pct'))}｜"
        f"当时排名{row.get('board_rank')}/{row.get('market_universe_count')}｜支持={'是' if row.get('entry_support') else '否'}"
        for row in snapshot.get("market_boards", [])[:4]
    ]
    type_counts = snapshot.get("event_type_counts") or {}
    text = (
        f"【V16真实顺序回放｜仅模拟｜盘中总结 {trade_date} {slot[:5]}】\n"
        f"数据截止{_clock(snapshot.get('data_asof'))}，覆盖{snapshot.get('coverage')}/{snapshot.get('pool_size')}，累计事件{snapshot.get('event_count')}。"
    )
    card = build_report_card(
        f"🧪 V16真实顺序回放｜盘中总结 {slot[:5]}",
        template="turquoise",
        fields=[
            ("行情截止", _clock(snapshot.get("data_asof")) or "暂无"),
            ("实时覆盖", f"{snapshot.get('coverage')}/{snapshot.get('pool_size')}"),
            ("累计事件", str(snapshot.get("event_count"))),
            ("正式新仓", str(type_counts.get("BUY_EVENT_WATCH", 0))),
            ("机会观察", str(type_counts.get("OPPORTUNITY_EVENT_WATCH", 0))),
            ("虚拟信号仓", str(snapshot.get("virtual_position_count"))),
            ("市场许可", f"{permission.get('state_cn', '预热')} / {permission.get('new_entry_permission', 'SELECTIVE')}"),
        ],
        sections=[
            ("🎯 当前怎么做", "\n".join(opportunity_lines) or "- 当前没有达到观察门槛的标的。"),
            ("🧭 合并池内部梯队（旁证）", "\n".join(sector_lines) or "- 尚未形成至少2只成员的清晰梯队。"),
            ("🌐 当时全市场匹配板块", "\n".join(board_lines) or "- 当时未匹配到可靠板块；不拿池内相对强弱冒充市场主线。"),
            ("🌡️ 市场交易许可", f"{permission.get('state_cn', '预热')}｜新仓{permission.get('new_entry_permission', 'SELECTIVE')}｜国内{permission.get('domestic_score', '—')}/100｜{permission.get('position_action_cn', '等待实时证据')}"),
            ("🧱 数据边界", "只使用本时点以前的Tick与当时已经落盘的完整板块横截面；V16结构择时为零权重影子层，不会悄悄增删正式事件。"),
        ],
        footer="历史路径仿真｜固定总结是状态快照；真正动作看实时事件卡｜不下单",
    )
    return card, text


def _event_card(event: Dict[str, Any], candidate: Mapping[str, Any]) -> Tuple[Dict[str, Any], str]:
    decision = event.get("action_decision") or decide_event_actions(event, dict(candidate))
    event["action_decision"] = decision
    capital = event.get("capital_behavior") or {}
    mtf = event.get("multitimeframe") or {}
    market = event.get("market_sector") or {}
    permission = event.get("market_permission") or {}
    timing = event.get("structured_timing") or {}
    text = "\n".join([
        f"【V16真实顺序回放｜仅模拟｜{_event_cn(event)}｜{_clock(event.get('event_ts'))}】",
        f"{event.get('name')}（{_short(event.get('symbol'))}）｜现价{_price(event.get('price'))}｜"
        f"VWAP {_price(event.get('vwap'))}｜综合强度{event.get('composite_signal_strength', event.get('live_signal_strength', '—'))}/100",
        f"触发：{_pattern_cn(event.get('pattern'))}｜位置质量{event.get('entry_quality', '—')}/100",
        f"资金：{capital.get('regime_cn', capital.get('phase_cn', '样本积累'))} {capital.get('score', '—')}/100｜"
        f"多周期：{mtf.get('alignment', '预热')} {mtf.get('score', '—')}/100",
        f"板块：{market.get('board_name', '未匹配')}·{market.get('rotation_state', 'UNAVAILABLE')}｜"
        f"当时排名{market.get('board_rank', '—')}/{market.get('market_universe_count', '—')}｜"
        f"板块入场支持{'是' if market.get('entry_support') else '否'}",
        f"市场许可：{permission.get('state_cn', '预热')}｜新仓{permission.get('new_entry_permission', 'SELECTIVE')}｜国内{permission.get('domestic_score', '—')}/100",
        format_structured_timing_line(timing),
    ])
    card = build_signal_card(
        text,
        event=event,
        short_code=_short(event.get("symbol")),
        action_decision=decision,
        template=signal_template(str(event.get("event")), str(event.get("pattern"))),
        footer="V16真实顺序回放｜本卡只含触发时已知事实｜不下单｜新买入严格T+1",
    )
    return card, text


def _postclose_card(
    trade_date: str,
    daily_asof: str,
    daily: Mapping[str, Any],
    next_daily: Mapping[str, Any],
    events: Sequence[Dict[str, Any]],
    observations: Sequence[Dict[str, Any]],
    candidate_map: Mapping[str, Mapping[str, Any]],
    pool_size: int,
) -> Tuple[Dict[str, Any], str, List[Dict[str, Any]]]:
    reviewed = _post_event_stats(list(events), list(observations))
    formal = [row for row in reviewed if row.get("event") == "BUY_EVENT_WATCH"]
    opportunities = [row for row in reviewed if row.get("event") == "OPPORTUNITY_EVENT_WATCH"]
    by_symbol: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
    for observation in observations:
        by_symbol[str(observation.get("symbol"))].append(observation)
    paths = []
    for symbol, rows in by_symbol.items():
        candidate = candidate_map.get(symbol) or {}
        close = _safe_float(candidate.get("close"))
        prices = [_safe_float(row.get("price")) for row in rows if _safe_float(row.get("price")) > 0]
        if close <= 0 or not prices:
            continue
        paths.append({
            "symbol": symbol,
            "name": candidate.get("name", ""),
            "close_return": prices[-1] / close - 1.0,
            "mfe_return": max(prices) / close - 1.0,
            "formal_event": any(row.get("symbol") == symbol and row.get("event") == "BUY_EVENT_WATCH" for row in reviewed),
            "any_event": any(row.get("symbol") == symbol for row in reviewed),
        })
    paths.sort(key=lambda row: -row["close_return"])
    event_lines = []
    for row in reviewed:
        event_lines.append(
            f"- {_clock(row.get('event_ts'))} {row.get('name')}｜{_event_cn(row)}｜{_pattern_cn(row.get('pattern'))}｜"
            f"至收盘{_pct(row.get('to_latest'))}｜后续MFE/MAE {_pct(row.get('post_mfe'))}/{_pct(row.get('post_mae'))}"
        )
    top_path_lines = [
        f"- {row.get('name')}（{_short(row.get('symbol'))}）｜收盘{_pct(row.get('close_return'))}｜"
        f"日内MFE{_pct(row.get('mfe_return'))}｜{'正式新仓' if row.get('formal_event') else ('仅观察事件' if row.get('any_event') else '未触发事件')}"
        for row in paths[:7]
    ]
    next_daily_fresh = str(next_daily.get("asof") or "") == trade_date
    next_rows = (
        [row for row in next_daily.get("candidates", []) if row.get("action") in {"BUY", "WATCH"}]
        if next_daily_fresh else []
    )
    next_rows.sort(key=lambda row: -int(_safe_float(row.get("candidate_rank_score"))))
    next_lines = [
        f"- {row.get('name')}（{_short(row.get('symbol'))}）｜{row.get('pool_group_cn')}｜"
        f"{_route_cn(row.get('daily_route'))}｜{int(_safe_float(row.get('candidate_rank_score')))}/100"
        for row in next_rows[:6]
    ]
    formal_returns = [row.get("to_latest") for row in formal if row.get("to_latest") is not None]
    text = (
        f"【V16真实顺序回放｜仅模拟｜盘后复盘 {trade_date} 15:30】\n"
        f"D-1={daily_asof}；事件{len(reviewed)}个，正式新仓{len(formal)}个，机会观察{len(opportunities)}个。"
    )
    card = build_report_card(
        f"🧪 V16真实顺序回放｜盘后复盘 {trade_date}",
        template="green",
        fields=[
            ("交易日", trade_date),
            ("事前日线", daily_asof),
            ("Tick覆盖", f"{len(by_symbol)}/{pool_size}"),
            ("实时事件", str(len(reviewed))),
            ("正式新仓", str(len(formal))),
            ("机会观察", str(len(opportunities))),
            ("正式信号至收盘", _pct(mean(formal_returns)) if formal_returns else "无正式样本"),
            ("规则", RULES_VERSION),
        ],
        sections=[
            ("📡 今日事件按结果复盘（结果只在盘后展示）", "\n".join(event_lines[:12]) or "- 今日没有实时事件。"),
            ("📈 今日真实路径与漏捕", "\n".join(top_path_lines) or "- 暂无完整路径。"),
            (
                "👀 明日盘前候选预览",
                "\n".join(next_lines) if next_daily_fresh else
                f"- **暂不发布。** 当日日线尚未落库（当前截止{next_daily.get('asof') or '未知'}），禁止用旧截面冒充盘后结果；次日盘前重算。",
            ),
            ("🧩 票池口径", f"当日按入池登记日严格筛选已生效标的，共{pool_size}只；盘后新增标的只从下一交易日进入正式事前流程。"),
            ("🧱 结论边界", "单日仿真用于检查信号位置、覆盖和状态机，不足以证明赚钱能力；今日完整全市场板块横截面严格按当时落盘时点推进。"),
        ],
        footer="历史仿真完成｜所有证据保存在D盘｜不下单｜不构成收益承诺",
    )
    return card, text, reviewed


def build_replay(
    trade_date: str,
    *,
    initial_positions: Optional[Mapping[str, Mapping[str, Any]]] = None,
    ledger_source: str = "EXPLICIT_EMPTY_RESEARCH_START",
) -> Dict[str, Any]:
    day_root = ROOT / "data" / "live_signal" / trade_date
    if not day_root.exists():
        raise RuntimeError(f"缺少当日实时证据目录：{day_root}")
    all_membership = load_pool_entries((ORIGINAL_POOL, RESEARCH_POOL))
    # 盘中只允许使用交易日前已经登记的标的。当天盘后登记的新增池只能进入次日，
    # 既避免回放漏掉已生效自研池，也杜绝把收盘后决定回灌上午。
    active_membership = {
        symbol: meta for symbol, meta in all_membership.items()
        if str(meta.get("pool_recorded_on") or "9999-12-31") < trade_date
    }
    active_pool = list(active_membership)
    daily = _load_logged_daily(day_root, trade_date)
    daily_asof = str(daily.get("asof") or "")
    if not daily_asof or daily_asof >= trade_date:
        raise RuntimeError(f"盘前候选截面不满足D-1边界：{daily_asof}")
    candidate_map = {
        row["symbol"]: dict(row) for row in daily.get("candidates", [])
        if row.get("symbol") in active_membership
    }
    active_pool = list(candidate_map)
    auction = _load_auction(day_root, daily)
    observations = _load_observations(day_root, trade_date, active_pool)
    if not observations:
        raise RuntimeError("没有可重放的真实盘中样本")

    market_records = _load_market_records(day_root)
    full_market_available = bool(
        market_records and len(market_records[0].get("all_market_compact") or []) >= 50
    )
    board_adapter = (
        HistoricalFullMarketAdapter(market_records)
        if full_market_available
        else HistoricalMatchedBoardAdapter(_load_market_timeline(day_root))
    )
    taxonomy = load_taxonomy()
    sector = LiveSectorHealthEngine(taxonomy)
    sector.set_candidates(daily.get("candidates", []))
    continuation = TrendContinuationAnalyzer()
    multitimeframe = MultiTimeframeIndicatorEngine()
    structured_timing = StructuredTimingEngine()
    minute_seed_rows = []
    for symbol, candidate in candidate_map.items():
        path = MINUTE_ROOT / f"{symbol}_1m.pkl"
        if not path.exists():
            minute_seed_rows.append({"symbol": symbol, "status": "UNAVAILABLE"})
            candidate["timing_static_context"] = {"status": "UNAVAILABLE", "no_lookahead": True}
            continue
        frame = pd.read_pickle(path)
        frame["eob"] = pd.to_datetime(frame["eob"], errors="coerce")
        if getattr(frame["eob"].dt, "tz", None) is not None:
            frame["eob"] = frame["eob"].dt.tz_convert("Asia/Shanghai").dt.tz_localize(None)
        seed = frame[frame["eob"].dt.strftime("%Y-%m-%d") < trade_date].copy()
        mtf_row = multitimeframe.seed(symbol, seed)
        timing_row = structured_timing.seed(symbol, seed)
        daily_frame = compute_features(_daily_from_minute(seed, trade_date), minimum=20)
        candidate["timing_static_context"] = build_daily_timing_context(daily_frame)
        minute_seed_rows.append({"symbol": symbol, "multitimeframe": mtf_row, "structured_timing": timing_row})
    minute_seed = {
        "rows": minute_seed_rows,
        "ready_count": sum(
            (row.get("multitimeframe") or {}).get("status") == "READY"
            and (row.get("structured_timing") or {}).get("status") == "READY"
            for row in minute_seed_rows
        ),
        "pool_count": len(candidate_map),
    }
    capital = CapitalBehaviorEngine()
    intraday = IntradayEventEngine()
    limit_behavior = LimitBehaviorEngine()
    market_permission_engine = MarketPermissionEngine()
    auction_map = auction.get("by_symbol") or {}
    # 历史回放严禁读取当前仍在变化的 live virtual_signal_positions.json。
    # 该文件代表“现在还剩什么”，既会漏掉历史上已退出的持仓，也可能把未来状态
    # 倒灌到过去。跨日回放必须由调用方把上一交易日的 ending ledger 显式传进来。
    virtual_positions: Dict[str, Dict[str, Any]] = {
        str(symbol): dict(row) for symbol, row in (initial_positions or {}).items()
        if str(row.get("entry_date") or "") < trade_date
    }
    starting_virtual_positions = {symbol: dict(row) for symbol, row in virtual_positions.items()}
    latest_rows: Dict[str, Dict[str, Any]] = {}
    events: List[Dict[str, Any]] = []
    timeline: List[Dict[str, Any]] = []

    pre_card, pre_text = _premarket_card(trade_date, daily_asof, daily)
    timeline.append({"sort_ts": f"{trade_date}T08:45:00", "kind": "PREMARKET", "card": pre_card, "text": pre_text})
    auction_card, auction_text = _auction_card(trade_date, auction, candidate_map)
    timeline.append({"sort_ts": f"{trade_date}T09:26:30", "kind": "AUCTION", "card": auction_card, "text": auction_text})

    boundaries = deque((_dt(f"{trade_date} {slot}"), slot) for slot in FIXED_SLOTS)
    for source_observation in observations:
        observation = dict(source_observation)
        now = _dt(observation.get("event_ts"))
        observation["event_ts"] = now
        while boundaries and now > boundaries[0][0]:
            boundary, slot = boundaries.popleft()
            if isinstance(board_adapter, HistoricalFullMarketAdapter):
                market_snapshot = board_adapter.advance(boundary)
            else:
                market_snapshot = {}
            permission = market_permission_engine.evaluate(market_snapshot, {}, premarket_daily=daily)
            snap = _summary_snapshot(
                slot, latest_rows, candidate_map, events, sector, board_adapter,
                virtual_positions, market_permission=permission,
            )
            card, text = _summary_card(trade_date, snap)
            timeline.append({"sort_ts": boundary.isoformat(), "kind": "PERIODIC", "slot": slot, "snapshot": snap, "card": card, "text": text})

        symbol = str(observation.get("symbol"))
        base_candidate = candidate_map.get(symbol)
        if not base_candidate:
            continue
        candidate = dict(base_candidate)
        if symbol in virtual_positions:
            candidate.update({
                "action": "MONITOR_EXIT",
                "monitor_sell": True,
                "position_entry_date": virtual_positions[symbol]["entry_date"],
                "position_entry_price": virtual_positions[symbol]["entry_price"],
                "position_entry_pattern": virtual_positions[symbol].get("entry_pattern"),
                "position_entry_route": virtual_positions[symbol].get("entry_route"),
                "position_source": "V16_REPLAY_SIGNAL_LEDGER",
            })
        gate = auction_map.get(symbol, {})
        mtf = multitimeframe.update(observation)
        local_sector = sector.update(observation)
        if isinstance(board_adapter, HistoricalFullMarketAdapter):
            market_snapshot = board_adapter.advance(now)
            market_sector = board_adapter.context_for_candidate(candidate, now)
        else:
            market_snapshot = {}
            market_sector = board_adapter.update(observation.get("_historical_market_sector") or {}, now)
        candidate["market_sector"] = market_sector
        market_permission = market_permission_engine.evaluate(market_snapshot, {}, premarket_daily=daily)
        candidate["market_permission"] = market_permission
        candidate["market_new_entry_allowed"] = market_permission.get("new_entry_permission") != "CLOSED"
        candidate["limit_behavior"] = limit_behavior.update(observation, candidate)
        combined_sector = _continuation_sector(local_sector, market_sector)
        candidate["continuation_sector"] = combined_sector
        continuation_context = continuation.update(observation, candidate, gate, combined_sector)
        base_strength = LiveSignalService._live_strength(candidate, local_sector, gate)
        live_strength = (
            int(_safe_float(continuation_context.get("score"), base_strength))
            if candidate.get("daily_route") == "TREND_CONTINUATION" else base_strength
        )
        if mtf.get("periods"):
            live_strength = max(0, min(100, live_strength + int(round((_safe_float(mtf.get("score")) - 50.0) * 0.20))))
        capital_context = capital.update(observation, candidate, combined_sector, mtf)
        if capital_context.get("status") == "READY":
            live_strength = max(0, min(100, live_strength + max(-6, min(6, int(round((_safe_float(capital_context.get("score"), 50.0) - 50.0) * 0.12))))))
        candidate.update({
            "live_sector": local_sector,
            "market_sector": market_sector,
            "continuation_context": continuation_context,
            "multitimeframe": mtf,
            "capital_behavior": capital_context,
            "limit_behavior": candidate.get("limit_behavior") or {},
            "live_signal_strength": live_strength,
        })
        timing_context = structured_timing.update(
            observation, candidate, mtf,
            sector=combined_sector, capital=capital_context,
            continuation=continuation_context, market_permission=market_permission,
        )
        candidate["structured_timing"] = timing_context
        sudden = LiveSignalService._sudden_trend_context(candidate, observation, mtf, local_sector, market_sector, gate)
        candidate["sudden_trend_context"] = sudden
        if candidate.get("daily_route") == "TREND_CONTINUATION":
            candidate["intraday_eligible"] = bool(continuation_context.get("confirmed"))
        elif (
            candidate.get("daily_route") == "TREND_PULLBACK"
            and live_strength >= 60
            and local_sector.get("state") in {"IGNITION", "EXPANSION", "HEALTHY_TREND"}
            and (not market_sector.get("board_code") or market_sector.get("entry_support"))
            and not market_sector.get("rotation_caution")
            and not (candidate.get("volume_soft_factor") or {}).get("blocks_new_entry")
            and not gate.get("hard_veto")
        ):
            candidate["intraday_eligible"] = True

        latest_rows[symbol] = {
            "observation": dict(observation),
            "live_strength": live_strength,
            "local_sector": local_sector,
            "market_sector": market_sector,
            "continuation": continuation_context,
            "multitimeframe": mtf,
            "capital_behavior": capital_context,
            "structured_timing": timing_context,
            "market_permission": market_permission,
        }
        event = intraday.on_tick(_raw_tick(observation), candidate, auction_gate=gate)
        if not event:
            continue
        event.update({
            "name": candidate.get("name", ""),
            "candidate": candidate,
            "capital_behavior": capital_context,
            "multitimeframe": mtf,
            "market_sector": market_sector,
            "live_sector": local_sector,
            "limit_behavior": candidate.get("limit_behavior") or {},
            "market_permission": market_permission,
            "structured_timing": timing_context,
            "action_decision": decide_event_actions(event, candidate),
            "replay_no_lookahead": True,
            "board_proxy_version": BOARD_PROXY_VERSION,
        })
        if event.get("event") == "BUY_EVENT_WATCH" and symbol not in virtual_positions:
            virtual_positions[symbol] = {
                "entry_date": str(event.get("event_ts"))[:10],
                "entry_price": event.get("price"),
                "entry_ts": event.get("event_ts"),
                "entry_pattern": event.get("pattern"),
                "entry_route": candidate.get("daily_route"),
                "entry_signal_strength": candidate.get("live_signal_strength"),
                "source_event_id": event.get("event_id"),
                "kind": "REPLAY_SIGNAL_LEDGER_NOT_BROKER_POSITION",
            }
        elif event.get("event") == "SELL_EVENT_WATCH" and symbol in virtual_positions:
            existing_action = ((event.get("action_decision") or {}).get("existing_position") or {}).get("code")
            if existing_action == "EXIT":
                virtual_positions.pop(symbol, None)
        events.append(event)
        card, text = _event_card(event, candidate)
        timeline.append({"sort_ts": event.get("event_ts"), "kind": "EVENT", "event": event, "card": card, "text": text})

    while boundaries:
        boundary, slot = boundaries.popleft()
        if isinstance(board_adapter, HistoricalFullMarketAdapter):
            market_snapshot = board_adapter.advance(boundary)
        else:
            market_snapshot = {}
        permission = market_permission_engine.evaluate(market_snapshot, {}, premarket_daily=daily)
        snap = _summary_snapshot(
            slot, latest_rows, candidate_map, events, sector, board_adapter,
            virtual_positions, market_permission=permission,
        )
        card, text = _summary_card(trade_date, snap)
        timeline.append({"sort_ts": boundary.isoformat(), "kind": "PERIODIC", "slot": slot, "snapshot": snap, "card": card, "text": text})

    # 15:30 时当日日线已经完成，可以为下一交易日生成候选；这不回灌今日事件。
    next_membership = {
        symbol: meta for symbol, meta in all_membership.items()
        if str(meta.get("pool_recorded_on") or "9999-12-31") <= trade_date
    }
    next_daily = {"asof": daily_asof, "candidates": [], "stale_blocked": True}
    post_card, post_text, reviewed = _postclose_card(
        trade_date, daily_asof, daily, next_daily, events, observations, candidate_map, len(active_pool),
    )
    timeline.append({"sort_ts": f"{trade_date}T15:30:00", "kind": "POST_CLOSE", "card": post_card, "text": post_text})
    timeline.sort(key=lambda row: (str(row.get("sort_ts")), {"PREMARKET": 0, "AUCTION": 0, "EVENT": 1, "PERIODIC": 2, "POST_CLOSE": 3}.get(str(row.get("kind")), 9)))

    card_errors = [
        {"index": index, "kind": row.get("kind"), "errors": validate_card(row.get("card") or {})}
        for index, row in enumerate(timeline)
        if validate_card(row.get("card") or {})
    ]
    same_day_sell = [
        row for row in events
        if row.get("event") == "SELL_EVENT_WATCH"
        and str(row.get("position_entry_date") or "")[:10] == trade_date
    ]
    audit = {
        "daily_asof_is_d_minus_1": daily_asof < trade_date,
        "active_pool_only_intraday": all(str(row.get("symbol")) in set(active_pool) for row in observations),
        "pool_effective_dates_respected": all(
            str((active_membership.get(str(row.get("symbol"))) or {}).get("pool_recorded_on") or "9999-12-31") < trade_date
            for row in observations
        ),
        "tick_time_boundary_ok": all(
            ("09:30:00" <= _clock(row.get("event_ts")) <= "11:30:00")
            or ("13:00:00" <= _clock(row.get("event_ts")) <= "15:00:00")
            for row in observations
        ),
        "event_time_boundary_ok": all(_clock(row.get("event_ts")) <= "15:00:00" for row in events),
        "board_proxy_no_lookahead": all(bool((row.get("market_sector") or {}).get("no_lookahead")) for row in events),
        "same_day_formal_sell_count": len(same_day_sell),
        "card_errors": card_errors,
        "timeline_sorted": all(str(timeline[index]["sort_ts"]) <= str(timeline[index + 1]["sort_ts"]) for index in range(len(timeline) - 1)),
        "fixed_slots_present": all(any(row.get("slot") == slot for row in timeline) for slot in FIXED_SLOTS),
        "postclose_stale_not_misrepresented": bool(
            str(next_daily.get("asof") or "") == trade_date or next_daily.get("stale_blocked")
        ),
        "structured_timing_shadow_zero_weight": all(
            (row.get("structured_timing") or {}).get("strategy_effect") == "NONE_SHADOW_ZERO_WEIGHT"
            for row in events
        ),
        "full_market_snapshot_asof_replay": bool(full_market_available),
        "historical_ledger_does_not_read_current_live_file": True,
        "position_ledger_entries_predate_session": all(
            str(row.get("entry_date") or "") < trade_date for row in starting_virtual_positions.values()
        ),
    }
    return {
        "generated_at": datetime.now().isoformat(),
        "trade_date": trade_date,
        "daily_asof": daily_asof,
        "rules_version": RULES_VERSION,
        "board_proxy_version": BOARD_PROXY_VERSION,
        "data_quality": {
            "tick_source": str(day_root / "tick_samples.jsonl"),
            "tick_evidence_count": len(observations),
            "tick_sampling": "约5秒落盘，非交易所完整逐笔",
            "auction_source": str(day_root / "auction_snapshots.jsonl"),
            "market_sector_source": "当时已经落盘的完整全市场行业/稳定概念健康横截面",
            "market_sector_full_snapshot": bool(full_market_available),
            "market_sector_snapshot_count": len(market_records),
            "global_market_historical_snapshot": False,
            "daily_adjustment": "ADJUST_PREV/front-adjusted",
            "minute_seed": minute_seed,
        },
        "pool": {
            "today_effective_count": len(active_pool),
            "today_original_count": sum(not meta.get("is_research_pool") for meta in active_membership.values()),
            "today_research_count": sum(bool(meta.get("is_research_pool")) for meta in active_membership.values()),
            "next_day_merged_count": len(next_membership),
        },
        "position_ledger": {
            "source": ledger_source,
            "start": starting_virtual_positions,
            "end": {symbol: dict(row) for symbol, row in virtual_positions.items()},
            "current_live_ledger_used": False,
        },
        "daily": daily,
        "auction": auction,
        "events": reviewed,
        "event_summary": {
            "count": len(reviewed),
            "type_counts": dict(Counter(row.get("event") for row in reviewed)),
            "pattern_counts": dict(Counter(row.get("pattern") for row in reviewed)),
            "formal_entries": sum(row.get("event") == "BUY_EVENT_WATCH" for row in reviewed),
            "opportunities": sum(row.get("event") == "OPPORTUNITY_EVENT_WATCH" for row in reviewed),
        },
        "timeline": timeline,
        "audit": audit,
        "limitations": [
            "本次规则由8月11日问题推动形成，是发现性当日重放，不是独立样本外验证",
            "Tick为约5秒证据样本，主动成交方向和五档OFI是代理，不是完整逐笔成交队列",
            "今天已使用当时落盘的完整全市场板块横截面；外围市场历史快照未落盘，未用收盘后数据补造",
            "未计手续费、滑点和消息到达后的真实成交延迟",
        ],
    }


def _write_artifacts(result: Dict[str, Any]) -> Tuple[Path, Path]:
    suffix = result["trade_date"].replace("-", "")
    out_root = ROOT / "reports" / f"v17_full_path_replay_{suffix}"
    out_root.mkdir(parents=True, exist_ok=True)
    json_path = out_root / "replay.json"
    md_path = out_root / "timeline.md"
    json_path.write_text(json.dumps(result, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    lines = [
        f"# V16全日真实路径仿真 {result['trade_date']}",
        "",
        f"事前日线：{result['daily_asof']}；Tick：{result['data_quality']['tick_evidence_count']}条；事件：{result['event_summary']['count']}个。",
        "",
        "| 顺序 | 仿真时点 | 类型 | 标题 |",
        "|---:|---|---|---|",
    ]
    for index, row in enumerate(result["timeline"], 1):
        title = ((((row.get("card") or {}).get("header") or {}).get("title") or {}).get("content") or "")
        lines.append(f"| {index} | {_clock(row.get('sort_ts'))} | {row.get('kind')} | {title} |")
    lines.extend(["", "## 审计", "", "```json", json.dumps(result["audit"], ensure_ascii=False, indent=2), "```", ""])
    md_path.write_text("\n".join(lines), encoding="utf-8")
    return json_path, md_path


def _send_timeline(result: Dict[str, Any], pause_seconds: float = 1.0) -> List[Dict[str, Any]]:
    outbox = ROOT / "data" / "replay" / result["trade_date"] / "v16_full_path" / "feishu_outbox.jsonl"
    notifier = FeishuNotifier(outbox=outbox, dry_run=False)
    delivery = []
    run_id = datetime.now().strftime("%Y%m%d_%H%M%S")
    for index, row in enumerate(result["timeline"], 1):
        event_id = f"v16_history_replay:{result['trade_date']}:{run_id}:{index:02d}:{row.get('kind')}"
        response = notifier.send_card(
            row["card"], event_id, fallback_text=row.get("text", ""),
            wait_for_delivery=True,
            delivery_context={
                "kind": "V16_HISTORICAL_FULL_PATH_REPLAY",
                "event_ts": row.get("sort_ts"),
                "timeline_kind": row.get("kind"),
            },
        )
        delivery.append({
            "index": index,
            "sort_ts": row.get("sort_ts"),
            "kind": row.get("kind"),
            "event_id": event_id,
            "ok": bool(response.get("ok")),
            "response": response,
        })
        if index < len(result["timeline"]):
            time.sleep(max(0.2, pause_seconds))
    notifier.close(timeout=2.0)
    return delivery


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--trade-date", default="2026-08-11")
    parser.add_argument(
        "--chain-from",
        help="从该交易日起按D盘现有交易日顺序连续回放到 --trade-date；持仓只沿用前一日回放结果",
    )
    parser.add_argument("--from-json", type=Path, help="发送已经通过干跑审计的回放文件，避免重复计算")
    parser.add_argument("--send-feishu", action="store_true")
    parser.add_argument("--pause-seconds", type=float, default=1.0)
    args = parser.parse_args()
    if args.from_json:
        result = json.loads(args.from_json.read_text(encoding="utf-8"))
        json_path = args.from_json
        md_path = args.from_json.parent / "timeline.md"
    else:
        if args.chain_from:
            if args.send_feishu:
                raise RuntimeError("跨日链式研究回放默认禁止直接发送飞书；请先审计单日JSON后再显式发送")
            available_dates = sorted(
                path.name for path in (ROOT / "data" / "live_signal").iterdir()
                if path.is_dir() and args.chain_from <= path.name <= args.trade_date
                and (path / "tick_samples.jsonl").exists()
            )
            if not available_dates or available_dates[0] != args.chain_from or available_dates[-1] != args.trade_date:
                raise RuntimeError(f"链式回放日期不完整：{available_dates}")
            ledger: Dict[str, Dict[str, Any]] = {}
            chain_outputs = []
            result = {}
            json_path = Path()
            md_path = Path()
            for index, replay_date in enumerate(available_dates):
                source = (
                    f"CHAIN_START_EMPTY:{args.chain_from}"
                    if index == 0 else f"PRIOR_REPLAY_END:{available_dates[index - 1]}"
                )
                result = build_replay(replay_date, initial_positions=ledger, ledger_source=source)
                json_path, md_path = _write_artifacts(result)
                ledger = {
                    symbol: dict(row)
                    for symbol, row in (result.get("position_ledger") or {}).get("end", {}).items()
                }
                chain_outputs.append({
                    "trade_date": replay_date,
                    "json": str(json_path),
                    "events": result.get("event_summary"),
                    "ledger_end_count": len(ledger),
                })
            print(json.dumps({
                "chain_from": args.chain_from,
                "chain_to": args.trade_date,
                "days": chain_outputs,
                "final_ledger": ledger,
            }, ensure_ascii=False, indent=2))
            return
        result = build_replay(args.trade_date)
        json_path, md_path = _write_artifacts(result)
    delivery = []
    if args.send_feishu:
        if not os.getenv("A_SHARE_ROTATION_FEISHU_WEBHOOK_URL", "").strip():
            raise RuntimeError("--send-feishu 需要环境变量 A_SHARE_ROTATION_FEISHU_WEBHOOK_URL")
        if not all(
            value is True or key in {"same_day_formal_sell_count", "card_errors"}
            for key, value in result["audit"].items()
        ) or result["audit"]["same_day_formal_sell_count"] != 0 or result["audit"]["card_errors"]:
            raise RuntimeError(f"审计未通过，拒绝发送飞书：{result['audit']}")
        delivery = _send_timeline(result, pause_seconds=args.pause_seconds)
        delivery_path = json_path.parent / "feishu_delivery.json"
        delivery_path.write_text(json.dumps(delivery, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    print(json.dumps({
        "json": str(json_path),
        "markdown": str(md_path),
        "timeline_count": len(result["timeline"]),
        "event_summary": result["event_summary"],
        "audit": result["audit"],
        "delivery": {
            "attempted": len(delivery),
            "ok": sum(bool(row.get("ok")) for row in delivery),
        },
    }, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
