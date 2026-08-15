# coding: utf-8
"""精选池 A 股轮动实时信号服务（信号版，不下单）。

设计边界：
1. 盘前只使用上一交易日及更早的日线数据，并加入未完成的当前月月线状态；
2. 集合竞价使用第三方行情轮询，GoldMiner 只作为历史数据/交易事实源；
3. 盘中使用 GoldMiner Tick 订阅，只对精选池和盘前候选做事件识别；
4. 买入事件是 WATCH/CONFIRMED_SIGNAL，不调用任何订单 API；
5. 所有原始快照、规则判断、飞书 outbox 都写入 D 盘，避免 C 盘膨胀。

本文件不把“昨日涨停、连板、趋势股、融资融券”等动态/风格标签当作行业。
行业分组只读取 universe/sector_taxonomy.json 中的稳定分类。
"""

from __future__ import annotations

import hashlib
import copy
import json
import math
import os
import random
import re
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import defaultdict, deque
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple

import numpy as np
import pandas as pd

from action_layer import candidate_action_line, decide_event_actions, format_action_card
from auction_path import AuctionPathAnalyzer
from capital_behavior_engine import (
    CAPITAL_BEHAVIOR_VERSION,
    CapitalBehaviorEngine,
    analyze_structural_capital,
)
from continuation_engine import TrendContinuationAnalyzer
from dynamic_universe import DYNAMIC_UNIVERSE_VERSION, DynamicUniverseManager
from feishu_cards import (
    CARD_UI_VERSION,
    build_report_card,
    build_signal_card,
    build_text_summary_card,
    signal_template,
    validate_card,
)
from intraday_engine import IntradayEventEngine, is_continuous_session, normalize_tick
from global_market_context import GlobalMarketMonitor
from limit_behavior import LimitBehaviorEngine
from market_permission import MarketPermissionEngine
from market_sector_feed import FullMarketSectorRadar
from multitimeframe_engine import MultiTimeframeIndicatorEngine
from numeric_pattern_plugin import (
    NumericPatternTagPlugin,
    candidate_tag_line,
    inline_tag_text,
    intraday_inline_tag_text,
    summary_low_support_tag_line,
    summary_tag_line,
)
from sector_health import LiveSectorHealthEngine
from signal_rules import (
    DEFAULT_CONFIG,
    classify_daily_signal,
    compute_features,
    resample_monthly,
)
from structured_timing import (
    STRUCTURED_TIMING_VERSION,
    StructuredTimingEngine,
    build_daily_timing_context,
    format_structured_timing_line,
)
from sector_context import build_group_returns, select_sector_context
from premarket_plan import (
    build_price_battle_plan,
    evaluate_moving_average_prior,
    format_price_battle_plan,
)
from volume_soft_factor import (
    evaluate_volume_soft_factor,
    format_volume_factor_line,
    normalize_effect_mode,
)


ROOT = Path(r"D:\codex\a_share_rotation")
ORIGINAL_POOL_FILE = ROOT / "universe" / "selected_pool_20260809.txt"
RESEARCH_POOL_FILE = ROOT / "universe" / "research_pool_20260811.txt"
POOL_FILES = (ORIGINAL_POOL_FILE, RESEARCH_POOL_FILE)
# 向后兼容只引用旧常量的研究脚本；实时服务默认使用 POOL_FILES 合并池。
POOL_FILE = ORIGINAL_POOL_FILE
TAXONOMY_FILE = ROOT / "universe" / "sector_taxonomy.json"
LIVE_ROOT = ROOT / "data" / "live_signal"
MINUTE_HISTORY_ROOT = ROOT / "data" / "goldminer" / "1m_20260511_20260807"
MINUTE_LIVE_CACHE_ROOT = ROOT / "data" / "goldminer" / "live_1m_seed"
DYNAMIC_SEED_ROOT = ROOT / "data" / "goldminer" / "dynamic_live_seed"
DYNAMIC_DAILY_CACHE_ROOT = DYNAMIC_SEED_ROOT / "daily"
DYNAMIC_MINUTE_CACHE_ROOT = DYNAMIC_SEED_ROOT / "1m"
VIRTUAL_LEDGER_FILE = LIVE_ROOT / "virtual_signal_positions.json"
DEFAULT_DEEPSEEK_KEY_FILE = Path(
    r"C:\Users\yushe\.goldminer3\projects\f402b79e-75de-11f1-bb12-00919e4351bc\DEEPSEEK_API_LOCAL.txt"
)

FAST_KDJ = DEFAULT_CONFIG.fast_kdj
SLOW_KDJ = DEFAULT_CONFIG.slow_kdj
MACD = DEFAULT_CONFIG.macd
MIN_DAILY_BARS = DEFAULT_CONFIG.minimum_daily_bars
POOL_RECORDED_ON = "2026-08-09"

# 9,20,2 的 J 值不是普通 9,3,3 的超买超卖阈值。
# 这里把 30-40 设为核心买入区，60+ 设为高位风险区；边界可在回测中调参，不能直接放宽成“快线一金叉就买”。
SLOW_J_BUY_LOW = DEFAULT_CONFIG.slow_j_buy_low
SLOW_J_BUY_HIGH = DEFAULT_CONFIG.slow_j_buy_high
SLOW_J_SELL_LOW = DEFAULT_CONFIG.slow_j_sell_low

FIXED_FEISHU_SLOTS = (
    ("08:45:00", "盘前总结"),
    ("09:26:30", "集合竞价总结"),
    ("10:00:00", "盘中总结"),
    ("11:45:00", "午间总结"),
    ("14:00:00", "午后总结"),
    ("14:55:00", "收盘前总结"),
    ("15:30:00", "盘后总结"),
)

_JSONL_WRITE_LOCK = threading.RLock()


def _json_default(value: Any):
    if isinstance(value, (datetime, date, pd.Timestamp)):
        return value.isoformat()
    if isinstance(value, (np.integer, np.floating)):
        return value.item()
    if isinstance(value, (np.bool_,)):
        return bool(value)
    return str(value)


def _safe_float(value: Any, default: float = 0.0) -> float:
    try:
        result = float(value)
        return result if math.isfinite(result) else default
    except (TypeError, ValueError):
        return default


def _date_key(value: Any) -> Optional[str]:
    if value is None:
        return None
    if isinstance(value, pd.Timestamp):
        return value.strftime("%Y-%m-%d")
    if isinstance(value, datetime):
        return value.strftime("%Y-%m-%d")
    if isinstance(value, date):
        return value.isoformat()
    return str(value)[:10]


def _now() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _safe_name(symbol: str) -> str:
    return re.sub(r"[^A-Za-z0-9_.-]", "_", symbol)


def append_jsonl(path: Path, payload: Dict[str, Any]) -> None:
    with _JSONL_WRITE_LOCK:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(payload, ensure_ascii=False, default=_json_default) + "\n")


def load_pool_entries(paths: Optional[Sequence[Path]] = None) -> Dict[str, Dict[str, Any]]:
    """读取原始池和自研池，并保留逐标的生效日期与池标签。"""
    pattern = re.compile(r"\|\s*((?:SHSE|SZSE)\.\d{6})\s*\|")
    name_pattern = re.compile(r"\|\s*(?:SHSE|SZSE)\.\d{6}\s*\|\s*([^|]+?)\s*(?:\||$)")
    entries: Dict[str, Dict[str, Any]] = {}
    for path in tuple(paths or POOL_FILES):
        lines = path.read_text(encoding="utf-8").splitlines()
        metadata: Dict[str, str] = {}
        for line in lines:
            if "=" in line and not line.lstrip().startswith(("-", "#")):
                key, value = line.split("=", 1)
                metadata[key.strip()] = value.strip()
        default_research = "research_pool" in path.name.lower()
        group = metadata.get("pool_group") or ("RESEARCH_POOL" if default_research else "ORIGINAL_POOL")
        group_cn = metadata.get("pool_group_cn") or ("自研池" if group == "RESEARCH_POOL" else "原始精选池")
        recorded_on = metadata.get("recorded_on") or POOL_RECORDED_ON
        for line in lines:
            match = pattern.search(line)
            if not match:
                continue
            symbol = match.group(1)
            name_match = name_pattern.search(line)
            name = name_match.group(1).strip() if name_match else ""
            if symbol in entries:
                continue
            entries[symbol] = {
                "symbol": symbol,
                "name": name,
                "pool_group": group,
                "pool_group_cn": group_cn,
                "pool_tags": [group_cn] + (["新增观察"] if group == "RESEARCH_POOL" else []),
                "is_research_pool": group == "RESEARCH_POOL",
                "pool_recorded_on": recorded_on,
                "pool_source_file": str(path),
            }
    if not entries:
        raise RuntimeError(f"合并股票池为空: {', '.join(str(path) for path in (paths or POOL_FILES))}")
    return entries


def load_pool(path: Optional[Path] = None) -> List[str]:
    paths = (path,) if path is not None else POOL_FILES
    return list(load_pool_entries(paths))


def load_taxonomy(path: Path = TAXONOMY_FILE) -> Dict[str, Any]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    symbols = payload.get("symbols", {})
    if not symbols:
        raise RuntimeError(f"严格行业分类为空: {path}")
    return payload


def _features(frame: pd.DataFrame, minimum: int = MIN_DAILY_BARS) -> Optional[pd.DataFrame]:
    return compute_features(frame, minimum=minimum, config=DEFAULT_CONFIG)


def _monthly_bars(daily: pd.DataFrame) -> pd.DataFrame:
    return resample_monthly(daily)


def _signal_from_frame(
    frame: Optional[pd.DataFrame],
    monthly: Optional[pd.DataFrame],
    sector_state: int,
    sector_confidence: str = "LOW",
) -> Optional[Dict[str, Any]]:
    return classify_daily_signal(
        frame,
        monthly,
        sector_state=sector_state,
        sector_confidence=sector_confidence,
        config=DEFAULT_CONFIG,
    )


class DailyCandidateBuilder:
    """从上一交易日数据构建候选；不接受未来日期过滤后的数据以外的信息。"""

    def __init__(
        self,
        pool: Sequence[str],
        taxonomy: Dict[str, Any],
        pool_recorded_on: str = POOL_RECORDED_ON,
        pool_membership: Optional[Mapping[str, Mapping[str, Any]]] = None,
        volume_effect_mode: Optional[str] = None,
    ):
        self.pool = list(pool)
        self.taxonomy = taxonomy
        self.pool_recorded_on = pool_recorded_on
        self.pool_membership = {
            symbol: {
                "symbol": symbol,
                "pool_group": "ORIGINAL_POOL",
                "pool_group_cn": "原始精选池",
                "pool_tags": ["原始精选池"],
                "is_research_pool": False,
                "pool_recorded_on": pool_recorded_on,
                **dict((pool_membership or {}).get(symbol, {})),
            }
            for symbol in self.pool
        }
        self.volume_effect_mode = normalize_effect_mode(
            volume_effect_mode or os.getenv("A_SHARE_ROTATION_VOLUME_FACTOR_MODE", "SHADOW")
        )

    def build(self, daily_frames: Dict[str, pd.DataFrame], asof: Optional[str] = None) -> Dict[str, Any]:
        if not asof:
            all_dates = []
            for raw in daily_frames.values():
                if raw is not None and len(raw) and "eob" in raw.columns:
                    parsed = pd.to_datetime(raw["eob"], errors="coerce").dropna()
                    all_dates.extend(parsed.dt.strftime("%Y-%m-%d").tolist())
            asof = max(all_dates) if all_dates else None
        if not asof:
            raise RuntimeError("无法确定日线决策截面，拒绝在无asof条件下生成候选")

        frames: Dict[str, pd.DataFrame] = {}
        stale_symbols: List[Dict[str, str]] = []
        for symbol in self.pool:
            raw = daily_frames.get(symbol)
            if raw is None or len(raw) == 0:
                continue
            normalized = raw.copy()
            normalized["eob"] = pd.to_datetime(normalized["eob"], errors="coerce")
            normalized = normalized[normalized["eob"].dt.strftime("%Y-%m-%d") <= asof]
            feature = _features(normalized)
            if feature is not None and not feature.empty:
                last_date = _date_key(feature.iloc[-1]["eob"])
                if last_date == asof:
                    frames[symbol] = feature
                else:
                    stale_symbols.append({"symbol": symbol, "last_date": last_date or "NONE", "required_date": asof})

        returns: Dict[str, float] = {}
        for symbol, frame in frames.items():
            if len(frame) >= 6:
                returns[symbol] = _safe_float(frame.iloc[-1]["close"] / frame.iloc[-6]["close"] - 1.0)
        breadth_values = list(returns.values())
        breadth = float(np.mean(np.asarray(breadth_values) > 0)) if breadth_values else 0.0
        median_return = float(np.median(breadth_values)) if breadth_values else 0.0
        pool_state = 1 if breadth >= 0.55 and median_return > 0 else (-1 if breadth <= 0.40 and median_return < 0 else 0)

        groups = build_group_returns(returns, self.taxonomy)

        candidates: Dict[str, Dict[str, Any]] = {}
        for symbol, frame in frames.items():
            sector = select_sector_context(symbol, groups, self.taxonomy)
            monthly = _monthly_bars(frame)
            signal = _signal_from_frame(frame, monthly, sector["state"], sector_confidence=sector["confidence"])
            if signal is None:
                continue
            item = dict(self.taxonomy.get("symbols", {}).get(symbol, {}))
            membership = dict(self.pool_membership.get(symbol, {}))
            item.update({
                "symbol": symbol,
                "pool_group": membership.get("pool_group", "ORIGINAL_POOL"),
                "pool_group_cn": membership.get("pool_group_cn", "原始精选池"),
                "pool_tags": membership.get("pool_tags") or ["原始精选池"],
                "is_research_pool": bool(membership.get("is_research_pool")),
                "pool_recorded_on": membership.get("pool_recorded_on", self.pool_recorded_on),
                "group_level": sector["level"],
                "group_key": sector["key"],
                "group_source": sector["source"],
                "group_member_count": sector["member_count"],
                "group_breadth": sector["breadth"],
                "group_median_return_5d": sector["median_return_5d"],
            })
            item.update(signal)
            capital_structure = analyze_structural_capital(frame)
            moving_average_prior = evaluate_moving_average_prior(frame)
            volume_factor = evaluate_volume_soft_factor(frame)
            raw_volume_bonus = int(volume_factor.get("raw_bonus", 0))
            volume_defense_penalty = int(volume_factor.get("defense_penalty", 0))
            rank_bonus = (
                raw_volume_bonus
                if self.volume_effect_mode in {"RANKING", "ELIGIBLE_STRENGTH"} and item.get("action") != "EXIT"
                else 0
            )
            item["base_signal_strength"] = int(item.get("signal_strength", 0))
            item["volume_soft_factor"] = volume_factor
            item["volume_soft_factor_mode"] = self.volume_effect_mode
            item["volume_soft_raw_bonus"] = raw_volume_bonus
            item["volume_soft_rank_bonus"] = rank_bonus
            item["volume_defense_penalty"] = volume_defense_penalty
            item["volume_entry_blocked"] = bool(volume_factor.get("blocks_new_entry"))
            if item["volume_entry_blocked"]:
                item["protection_level"] = "HIGH"
            # 历史分层未证明结构分本身具有新开仓正收益，禁止把它机械叠加到候选排序。
            # 保留research_adjustment便于审计，但实盘生效值固定为0；资金结构只定义场景，
            # 真正买点必须由盘中资金、板块、多周期与位置共同确认。
            capital_research_adjustment = max(
                -7, min(7, int(round((_safe_float(capital_structure.get("score"), 50.0) - 50.0) * 0.14)))
            )
            capital_rank_adjustment = 0
            item["capital_structure"] = capital_structure
            item["capital_research_adjustment"] = capital_research_adjustment
            item["capital_rank_adjustment"] = capital_rank_adjustment
            # 均线只负责盘前准备路线，排序影响严格限制在[-4,+4]；盘中实时
            # 板块、资金、分钟周期与成交性仍拥有正式买点决策权。
            ma_rank_adjustment = int(max(
                -4, min(4, _safe_float(moving_average_prior.get("rank_adjustment"), 0))
            ))
            item["moving_average_prior"] = moving_average_prior
            item["timing_static_context"] = build_daily_timing_context(frame)
            item["price_battle_plan"] = build_price_battle_plan(
                frame, item["timing_static_context"], symbol=symbol,
                signal_strength=int(_safe_float(item.get("signal_strength"))),
                daily_route=str(item.get("daily_route") or item.get("lane") or ""),
                protection_level=str(item.get("protection_level") or ""),
            )
            item["ma_prior_rank_adjustment"] = ma_rank_adjustment
            item["candidate_rank_score"] = max(
                0, min(
                    100,
                    item["base_signal_strength"] + rank_bonus - volume_defense_penalty
                    + capital_rank_adjustment + ma_rank_adjustment,
                )
            )
            item["universe_point_in_time"] = bool(asof >= str(item["pool_recorded_on"]))
            item["overall_no_lookahead"] = bool(item.get("feature_no_lookahead") and item["universe_point_in_time"])
            candidates[symbol] = item

        action_rank = {"BUY": 0, "WATCH": 1, "EXIT": 2, "WAIT": 3}
        ordered = sorted(
            candidates.values(),
            key=lambda row: (
                action_rank.get(row.get("action"), 9),
                -int(row.get("candidate_rank_score", row.get("score", 0))),
                row["symbol"],
            ),
        )
        volume_status_counts: Dict[str, int] = defaultdict(int)
        for row in ordered:
            volume_status_counts[str((row.get("volume_soft_factor") or {}).get("status") or "UNAVAILABLE")] += 1
        return {
            "asof": asof,
            "pool_size": len(self.pool),
            "available_size": len(frames),
            "stale_symbols": stale_symbols,
            "pool_breadth": breadth,
            "pool_median_return_5d": median_return,
            "pool_state": pool_state,
            "pool_state_role": "MARKET_CONTEXT_ONLY_NOT_SECTOR_FALLBACK",
            "candidates": ordered,
            "strict_taxonomy": True,
            "feature_no_lookahead": True,
            "pool_recorded_on": self.pool_recorded_on,
            "pool_membership": self.pool_membership,
            "pool_group_counts": dict(
                (group, sum(row.get("pool_group") == group for row in self.pool_membership.values()))
                for group in {row.get("pool_group") for row in self.pool_membership.values()}
            ),
            "universe_point_in_time": all(row.get("universe_point_in_time") for row in ordered),
            "overall_no_lookahead": all(row.get("overall_no_lookahead") for row in ordered),
            "volume_soft_factor_mode": self.volume_effect_mode,
            "volume_soft_factor_status_counts": dict(volume_status_counts),
            "volume_soft_factor_no_lookahead": True,
            "capital_behavior_version": CAPITAL_BEHAVIOR_VERSION,
            "capital_behavior_no_lookahead": True,
            "premarket_ma_plan_version": "premarket_ma_prior_v1_low_weight",
            "premarket_ma_prior_max_abs_weight": 4,
            "structured_timing_version": STRUCTURED_TIMING_VERSION,
            "structured_timing_effect": "NONE_SHADOW_ZERO_WEIGHT",
            "rules_version": "daily_signal_rules_v13_structured_timing_shadow",
        }


class FeishuNotifier:
    """飞书自定义机器人可靠投递器。

    信号生成与飞书网络 I/O 完全解耦：调用方先把完整消息持久化到 D 盘，
    后台单线程按优先级、官方频控和退避规则投递。只有飞书业务码为 0 才算
    DELIVERED；HTTP 200 但业务码非 0 绝不能进入成功去重集合。
    """

    RETRYABLE_CODES = {11232, 11233, 19006, 230020, 99991400}
    BACKOFF_SECONDS = (1.0, 3.0, 7.0, 15.0, 30.0, 60.0, 120.0)

    def __init__(
        self,
        webhook_url: Optional[str] = None,
        outbox: Path = LIVE_ROOT / "feishu_outbox.jsonl",
        timeout: float = 10.0,
        dry_run: bool = False,
        *,
        queue_path: Optional[Path] = None,
        max_payload_bytes: int = 18_000,
        min_interval_seconds: float = 0.27,
        minute_limit: int = 90,
    ):
        self.webhook_url = webhook_url or os.getenv("A_SHARE_ROTATION_FEISHU_WEBHOOK_URL", "").strip()
        self.outbox = Path(outbox)
        self.queue_path = Path(queue_path or self.outbox.with_name("feishu_delivery_queue.jsonl"))
        self.timeout = timeout
        self.dry_run = dry_run or os.getenv("A_SHARE_ROTATION_FEISHU_DRY_RUN", "0") == "1"
        # 官方硬上限为 20KB；保留少量边界余量，按实际发送字节计算。
        self.max_payload_bytes = min(max(4_096, int(max_payload_bytes)), 19_500)
        # 官方 5次/秒、100次/分钟；本地保守控制在约4次/秒、90次/分钟。
        self.min_interval_seconds = max(0.25, float(min_interval_seconds))
        self.minute_limit = min(max(1, int(minute_limit)), 90)
        self._condition = threading.Condition(threading.RLock())
        self._rate_lock = threading.RLock()
        self._pending: Dict[str, Dict[str, Any]] = {}
        self._terminal: Dict[str, Dict[str, Any]] = {}
        self._inflight: set[str] = set()
        self._sequence = 0
        self._last_send_monotonic = 0.0
        self._minute_send_times: deque[float] = deque()
        self._stopping = False
        self._workers: List[threading.Thread] = []
        self._revalidator: Optional[Callable[[Dict[str, Any]], Dict[str, Any]]] = None
        self._load_queue_state()
        if not self.dry_run and self.webhook_url:
            # 两条通道共享总限速。交易事件不会被一个正在超时/重试的长总结阻塞。
            self._workers = [
                threading.Thread(
                    target=self._worker_loop,
                    kwargs={"realtime_lane": True},
                    name="feishu-realtime-delivery",
                    daemon=True,
                ),
                threading.Thread(
                    target=self._worker_loop,
                    kwargs={"realtime_lane": False},
                    name="feishu-report-delivery",
                    daemon=True,
                ),
            ]
            for worker in self._workers:
                worker.start()

    @staticmethod
    def encode_payload(body: Dict[str, Any]) -> bytes:
        return json.dumps(body, ensure_ascii=True).encode("ascii")

    def set_revalidator(self, callback: Optional[Callable[[Dict[str, Any]], Dict[str, Any]]]) -> None:
        """注册延迟事件复核器；只添加提示，不删除或篡改原始信号。"""

        self._revalidator = callback

    def close(self, timeout: float = 2.0) -> None:
        """停止后台发送线程；正式服务正常退出时可调用，未送达项仍留在D盘。"""

        with self._condition:
            self._stopping = True
            self._condition.notify_all()
        deadline = time.monotonic() + max(0.0, timeout)
        for worker in self._workers:
            worker.join(timeout=max(0.0, deadline - time.monotonic()))

    def delivery_snapshot(self) -> Dict[str, Any]:
        with self._condition:
            pending = list(self._pending.values())
            return {
                "worker_alive": bool(self.dry_run or (len(self._workers) == 2 and all(worker.is_alive() for worker in self._workers))),
                "worker_count": 0 if self.dry_run else len(self._workers),
                "pending_count": len(pending),
                "pending_high_priority": sum(int(row.get("priority", 3)) <= 1 for row in pending),
                "oldest_pending_seconds": max(
                    [max(0.0, time.time() - float(row.get("enqueued_epoch") or time.time())) for row in pending]
                    or [0.0]
                ),
                "max_payload_bytes": self.max_payload_bytes,
                "minute_limit": self.minute_limit,
                "min_interval_seconds": self.min_interval_seconds,
                "queue_path": str(self.queue_path),
            }

    @staticmethod
    def _response_code(response_body: Mapping[str, Any]) -> Any:
        if "code" in response_body:
            return response_body.get("code")
        return response_body.get("StatusCode")

    @classmethod
    def _response_ok(cls, response_body: Mapping[str, Any], http_status: int = 200) -> bool:
        code = cls._response_code(response_body)
        return 200 <= int(http_status) < 300 and code in (0, "0")

    @staticmethod
    def _event_priority(event_id: str) -> int:
        upper = str(event_id).upper()
        if ":SELL:" in upper or ":BUY:" in upper or "HARD_STOP" in upper:
            return 0
        if any(token in upper for token in (":RISK:", ":OPPORTUNITY_", ":DISCOVERY_")):
            return 1
        if upper.startswith("AUCTION_SUMMARY:"):
            return 4
        if upper.startswith(("PERIODIC:", "POST_CLOSE:", "PREMARKET:")):
            return 6
        return 3

    @staticmethod
    def _event_time(event_id: str, delivery_context: Optional[Mapping[str, Any]]) -> str:
        context = delivery_context or {}
        return str(context.get("event_ts") or context.get("created_at") or _now())

    def _append_queue_record(self, record: Dict[str, Any]) -> None:
        append_jsonl(self.queue_path, record)

    def _load_queue_state(self) -> None:
        """从追加日志恢复尚未成功的消息，确保程序重启后继续投递。"""

        if not self.queue_path.exists():
            return
        latest: Dict[str, Dict[str, Any]] = {}
        try:
            for raw in self.queue_path.read_text(encoding="utf-8").splitlines():
                if not raw.strip():
                    continue
                row = json.loads(raw)
                delivery_id = str(row.get("delivery_id") or "")
                if delivery_id:
                    latest[delivery_id] = row
        except Exception:
            # 队列日志损坏不能拖垮行情主进程；新消息仍可继续写入。
            return
        for delivery_id, row in latest.items():
            state = str(row.get("queue_state") or "")
            if state in {"DELIVERED", "FAILED_PERMANENT", "DRY_RUN"}:
                self._terminal[delivery_id] = row
                continue
            item = row.get("item")
            if isinstance(item, dict) and item.get("body"):
                self._pending[delivery_id] = item
                self._sequence = max(self._sequence, int(item.get("sequence") or 0))

    def _delivered_in_legacy_outbox(self, event_id: str) -> bool:
        if not self.outbox.exists():
            return False
        legacy_success = False
        multipart_success: Dict[int, set[int]] = defaultdict(set)
        try:
            for line in self.outbox.read_text(encoding="utf-8").splitlines()[-1000:]:
                if not line.strip():
                    continue
                old = json.loads(line)
                if old.get("event_id") != event_id:
                    continue
                status = old.get("status")
                response_code = old.get("response_code")
                if status == "DRY_RUN":
                    return True
                if status in {"SENT", "DELIVERED"} and response_code in (0, "0"):
                    part_count = int(old.get("part_count") or 1)
                    part_index = int(old.get("part_index") or 1)
                    if part_count <= 1:
                        legacy_success = True
                    else:
                        multipart_success[part_count].add(part_index)
        except Exception:
            return False
        return legacy_success or any(len(parts) >= total for total, parts in multipart_success.items())

    def _already_sent(self, event_id: str) -> bool:
        with self._condition:
            related = [
                row for row in self._terminal.values()
                if str((row.get("item") or {}).get("logical_event_id") or row.get("logical_event_id") or "") == event_id
            ]
            if related and all(str(row.get("queue_state")) in {"DELIVERED", "DRY_RUN"} for row in related):
                return True
        return self._delivered_in_legacy_outbox(event_id)

    def _already_accepted(self, event_id: str) -> bool:
        with self._condition:
            return any(
                str(item.get("logical_event_id") or "") == event_id
                for item in self._pending.values()
            ) or self._already_sent(event_id)

    def _text_bodies(self, text: str) -> List[Dict[str, Any]]:
        initial = {"msg_type": "text", "content": {"text": text}}
        if len(self.encode_payload(initial)) <= self.max_payload_bytes:
            return [initial]
        # 保留所有字符并优先在换行处拆分；编号前缀也计入20KB边界。
        lines = str(text).splitlines(keepends=True) or [str(text)]
        chunks: List[str] = []
        current = ""
        target = max(1_000, self.max_payload_bytes - 1_000)
        for line in lines:
            candidate = current + line
            probe = {"msg_type": "text", "content": {"text": candidate}}
            if current and len(self.encode_payload(probe)) > target:
                chunks.append(current)
                current = ""
            while line and len(self.encode_payload({"msg_type": "text", "content": {"text": line}})) > target:
                low, high = 1, len(line)
                while low < high:
                    middle = (low + high + 1) // 2
                    size = len(self.encode_payload({"msg_type": "text", "content": {"text": line[:middle]}}))
                    if size <= target:
                        low = middle
                    else:
                        high = middle - 1
                chunks.append(line[:low])
                line = line[low:]
            current += line
        if current:
            chunks.append(current)
        total = len(chunks)
        return [
            {"msg_type": "text", "content": {"text": f"【续页 {index}/{total}】\n{chunk}"}}
            for index, chunk in enumerate(chunks, 1)
        ]

    def _card_bodies(self, card: Dict[str, Any], fallback_text: str) -> List[Dict[str, Any]]:
        initial = {"msg_type": "interactive", "card": card}
        if len(self.encode_payload(initial)) <= self.max_payload_bytes:
            return [initial]
        elements = list((card or {}).get("elements") or [])
        if not elements:
            return self._text_bodies(fallback_text or "飞书卡片超过20KB且没有可拆分元素。")
        base_card = {key: copy.deepcopy(value) for key, value in card.items() if key != "elements"}
        chunks: List[List[Dict[str, Any]]] = []
        current: List[Dict[str, Any]] = []
        target = max(4_096, self.max_payload_bytes - 700)
        for element in elements:
            trial = current + [element]
            probe = {"msg_type": "interactive", "card": {**copy.deepcopy(base_card), "elements": trial}}
            if current and len(self.encode_payload(probe)) > target:
                chunks.append(current)
                current = []
            current.append(element)
            single = {"msg_type": "interactive", "card": {**copy.deepcopy(base_card), "elements": current}}
            if len(self.encode_payload(single)) > target:
                # 极端单元素超限时，改用完整审计文本拆页，绝不截断原始内容。
                return self._text_bodies(fallback_text or json.dumps(card, ensure_ascii=False))
        if current:
            chunks.append(current)
        total = len(chunks)
        bodies: List[Dict[str, Any]] = []
        for index, chunk in enumerate(chunks, 1):
            part = copy.deepcopy(base_card)
            title = (((part.get("header") or {}).get("title") or {}).get("content") or "A股轮动")
            part.setdefault("header", {}).setdefault("title", {})["content"] = f"{title}（{index}/{total}）"
            part["elements"] = chunk
            body = {"msg_type": "interactive", "card": part}
            if len(self.encode_payload(body)) > self.max_payload_bytes:
                return self._text_bodies(fallback_text or json.dumps(card, ensure_ascii=False))
            bodies.append(body)
        return bodies

    def _peak_not_before(self, priority: int) -> float:
        now = datetime.now()
        if priority < 4 or now.minute not in {0, 30} or now.second > 15:
            return time.time()
        # 固定总结按原时点生成，投递错开官方明确提示的整点拥堵窗口。
        target_second = 8 + random.randint(0, 4)
        return time.time() + max(0.0, float(target_second - now.second))

    def _enqueue_bodies(
        self,
        bodies: Sequence[Dict[str, Any]],
        event_id: str,
        *,
        message_type: str,
        audit_text: str,
        fallback_text: str = "",
        priority: Optional[int] = None,
        delivery_context: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        if self._already_accepted(event_id):
            return {
                "ok": True, "accepted": True, "deduplicated": True,
                "event_id": event_id, "message_type": message_type,
            }
        if self.dry_run:
            delivery_ids = []
            total = len(bodies)
            for index, body in enumerate(bodies, 1):
                delivery_id = event_id if total == 1 else f"{event_id}#part-{index}-of-{total}"
                delivery_ids.append(delivery_id)
                append_jsonl(self.outbox, {
                    "ts": _now(), "event_id": event_id, "delivery_id": delivery_id,
                    "part_index": index, "part_count": total,
                    "digest": hashlib.sha256(event_id.encode("utf-8")).hexdigest()[:16],
                    "message_type": str(body.get("msg_type") or message_type),
                    "text": audit_text, "dry_run": True, "status": "DRY_RUN",
                })
            return {
                "ok": True, "accepted": True, "dry_run": True, "delivered": True,
                "delivery_state": "DRY_RUN", "event_id": event_id,
                "delivery_ids": delivery_ids, "part_count": total, "message_type": message_type,
            }
        if not self.webhook_url:
            append_jsonl(self.outbox, {
                "ts": _now(), "event_id": event_id, "message_type": message_type,
                "text": audit_text, "dry_run": False, "status": "NO_WEBHOOK_CONFIGURED",
            })
            return {"ok": False, "accepted": False, "error": "NO_WEBHOOK_CONFIGURED", "event_id": event_id}
        priority_value = self._event_priority(event_id) if priority is None else int(priority)
        delivery_ids: List[str] = []
        event_not_before = self._peak_not_before(priority_value)
        with self._condition:
            total = len(bodies)
            for index, body in enumerate(bodies, 1):
                self._sequence += 1
                delivery_id = event_id if total == 1 else f"{event_id}#part-{index}-of-{total}"
                item = {
                    "delivery_id": delivery_id,
                    "logical_event_id": event_id,
                    "part_index": index,
                    "part_count": total,
                    "message_type": str(body.get("msg_type") or message_type),
                    "body": body,
                    "audit_text": audit_text,
                    "fallback_text": fallback_text,
                    "priority": priority_value,
                    "sequence": self._sequence,
                    "attempt": 0,
                    "created_at": self._event_time(event_id, delivery_context),
                    "enqueued_at": _now(),
                    "enqueued_epoch": time.time(),
                    "not_before_epoch": event_not_before,
                    "delivery_context": delivery_context or {},
                }
                self._pending[delivery_id] = item
                delivery_ids.append(delivery_id)
                self._append_queue_record({
                    "ts": _now(), "queue_state": "ENQUEUED",
                    "delivery_id": delivery_id, "logical_event_id": event_id, "item": item,
                })
            self._condition.notify_all()
        return {
            "ok": True, "accepted": True, "queued": True, "delivered": False,
            "delivery_state": "QUEUED", "event_id": event_id,
            "delivery_ids": delivery_ids, "part_count": len(delivery_ids),
            "message_type": message_type,
        }

    def _wait_for_logical_event(self, event_id: str, timeout: float) -> Dict[str, Any]:
        deadline = time.monotonic() + max(0.0, timeout)
        with self._condition:
            while True:
                pending = [item for item in self._pending.values() if item.get("logical_event_id") == event_id]
                terminal = [
                    row for row in self._terminal.values()
                    if str((row.get("item") or {}).get("logical_event_id") or row.get("logical_event_id") or "") == event_id
                ]
                if not pending and terminal:
                    ok = all(row.get("queue_state") in {"DELIVERED", "DRY_RUN"} for row in terminal)
                    results = [row.get("result") or {} for row in terminal]
                    last_result = results[-1] if results else {}
                    return {
                        "ok": ok, "accepted": True, "delivered": ok,
                        "delivery_state": "DELIVERED" if ok else "FAILED_PERMANENT",
                        "event_id": event_id, "part_count": len(terminal),
                        "response_code": last_result.get("response_code"),
                        "response_msg": last_result.get("response_msg"),
                        "response": last_result.get("response") or {},
                    }
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return {
                        "ok": False, "accepted": True, "queued": True, "delivered": False,
                        "delivery_state": "QUEUED_TIMEOUT", "event_id": event_id,
                    }
                self._condition.wait(timeout=min(remaining, 0.5))

    def _rate_limit_wait(self) -> None:
        while True:
            with self._rate_lock:
                now = time.monotonic()
                while self._minute_send_times and now - self._minute_send_times[0] >= 60.0:
                    self._minute_send_times.popleft()
                waits = [self.min_interval_seconds - (now - self._last_send_monotonic)]
                if len(self._minute_send_times) >= self.minute_limit:
                    waits.append(60.0 - (now - self._minute_send_times[0]))
                delay = max(waits)
                if delay <= 0:
                    self._last_send_monotonic = now
                    self._minute_send_times.append(now)
                    return
            time.sleep(min(delay, 1.0))

    @staticmethod
    def _header_seconds(headers: Mapping[str, Any]) -> Optional[float]:
        for key in ("x-ogw-ratelimit-reset", "retry-after"):
            value = headers.get(key) or headers.get(key.title())
            try:
                if value is not None:
                    return max(0.0, float(value))
            except (TypeError, ValueError):
                pass
        return None

    def _delivery_banner(self, item: Dict[str, Any]) -> str:
        age = max(0.0, time.time() - float(item.get("enqueued_epoch") or time.time()))
        if age <= 20 or not item.get("delivery_context"):
            return ""
        review: Dict[str, Any] = {}
        if self._revalidator is not None:
            try:
                review = self._revalidator(item) or {}
            except Exception as exc:
                review = {"status": "REVALIDATION_ERROR", "message": f"复核异常：{type(exc).__name__}"}
        message = str(review.get("message") or "盘面已经变化，收到后须结合最新行情和后续事件复核。")
        return f"⚠️ 延迟投递复核｜原事件 {item.get('created_at')}｜延迟约{age:.0f}秒｜{message}"

    @staticmethod
    def _decorate_body(body: Dict[str, Any], banner: str) -> Dict[str, Any]:
        if not banner:
            return body
        decorated = copy.deepcopy(body)
        if decorated.get("msg_type") == "text":
            content = decorated.setdefault("content", {})
            content["text"] = f"{banner}\n\n{content.get('text', '')}"
            return decorated
        card = decorated.get("card") or {}
        card.setdefault("elements", []).insert(0, {
            "tag": "div", "text": {"tag": "lark_md", "content": f"**{banner}**"},
        })
        return decorated

    def _attempt_once(self, item: Dict[str, Any]) -> Dict[str, Any]:
        self._rate_limit_wait()
        body = self._decorate_body(copy.deepcopy(item["body"]), self._delivery_banner(item))
        payload = self.encode_payload(body)
        if len(payload) > 20_000:
            return {"success": False, "retryable": False, "response_code": "LOCAL_PAYLOAD_TOO_LARGE", "response_msg": f"{len(payload)} bytes"}
        request = urllib.request.Request(
            self.webhook_url,
            data=payload,
            headers={"Content-Type": "application/json; charset=utf-8"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                raw = response.read().decode("utf-8", errors="ignore") or "{}"
                response_body = json.loads(raw)
                http_status = int(getattr(response, "status", 200) or 200)
                headers = dict(response.headers.items())
            code = self._response_code(response_body)
            success = self._response_ok(response_body, http_status)
            return {
                "success": success,
                "retryable": (not success and code in self.RETRYABLE_CODES) or http_status >= 500,
                "http_status": http_status,
                "response_code": code,
                "response_msg": response_body.get("msg") or response_body.get("StatusMessage"),
                "response": response_body,
                "headers": headers,
            }
        except urllib.error.HTTPError as exc:
            try:
                raw = exc.read().decode("utf-8", errors="ignore") or "{}"
                response_body = json.loads(raw)
            except Exception:
                response_body = {}
            headers = dict(exc.headers.items()) if exc.headers else {}
            code = self._response_code(response_body)
            return {
                "success": False,
                "retryable": int(exc.code) == 429 or int(exc.code) >= 500 or code in self.RETRYABLE_CODES,
                "http_status": int(exc.code),
                "response_code": code,
                "response_msg": response_body.get("msg") or str(exc),
                "response": response_body,
                "headers": headers,
            }
        except Exception as exc:
            return {
                "success": False, "retryable": True,
                "error_type": type(exc).__name__, "response_msg": str(exc)[:240], "headers": {},
            }

    def _retry_delay(self, attempt: int, result: Mapping[str, Any]) -> float:
        server_wait = self._header_seconds(result.get("headers") or {})
        if server_wait is not None:
            return server_wait + random.uniform(0.15, 0.75)
        base = self.BACKOFF_SECONDS[min(max(0, attempt - 1), len(self.BACKOFF_SECONDS) - 1)]
        return base + random.uniform(0.15, min(1.5, base * 0.2 + 0.15))

    def _outbox_delivery_record(self, item: Dict[str, Any], status: str, result: Mapping[str, Any]) -> Dict[str, Any]:
        record = {
            "ts": _now(), "event_id": item.get("logical_event_id"),
            "delivery_id": item.get("delivery_id"),
            "part_index": item.get("part_index"), "part_count": item.get("part_count"),
            "digest": hashlib.sha256(str(item.get("logical_event_id")).encode("utf-8")).hexdigest()[:16],
            "message_type": item.get("message_type"), "text": item.get("audit_text"),
            "dry_run": False, "status": status, "attempt": item.get("attempt"),
            "response_code": result.get("response_code"), "response_msg": result.get("response_msg"),
            "http_status": result.get("http_status"), "error_type": result.get("error_type"),
        }
        return record

    def _enqueue_fallback_after_permanent_failure(self, item: Dict[str, Any]) -> None:
        fallback = str(item.get("fallback_text") or "").strip()
        if item.get("message_type") != "interactive" or not fallback:
            return
        fallback_id = f"{item.get('logical_event_id')}:text_fallback"
        self._enqueue_bodies(
            self._text_bodies(fallback), fallback_id,
            message_type="text", audit_text=fallback, priority=int(item.get("priority", 3)),
            delivery_context=item.get("delivery_context") or {},
        )

    def _prior_parts_delivered(self, item: Mapping[str, Any]) -> bool:
        part_index = int(item.get("part_index") or 1)
        if part_index <= 1:
            return True
        logical_event_id = str(item.get("logical_event_id") or "")
        for prior_index in range(1, part_index):
            prior_id = f"{logical_event_id}#part-{prior_index}-of-{item.get('part_count')}"
            terminal = self._terminal.get(prior_id) or {}
            if terminal.get("queue_state") not in {"DELIVERED", "DRY_RUN"}:
                return False
        return True

    def _cancel_remaining_parts(self, failed_item: Mapping[str, Any]) -> None:
        logical_event_id = str(failed_item.get("logical_event_id") or "")
        failed_index = int(failed_item.get("part_index") or 1)
        for delivery_id, item in list(self._pending.items()):
            if str(item.get("logical_event_id") or "") != logical_event_id:
                continue
            if int(item.get("part_index") or 1) <= failed_index:
                continue
            terminal = {
                "ts": _now(), "queue_state": "FAILED_PERMANENT",
                "failure_reason": "PRIOR_MULTIPART_DELIVERY_FAILED",
                "delivery_id": delivery_id, "logical_event_id": logical_event_id, "item": item,
            }
            self._append_queue_record(terminal)
            self._terminal[delivery_id] = terminal
            self._pending.pop(delivery_id, None)

    def _worker_loop(self, *, realtime_lane: bool) -> None:
        while True:
            with self._condition:
                if self._stopping:
                    return
                now = time.time()
                ready = [
                    item for delivery_id, item in self._pending.items()
                    if delivery_id not in self._inflight
                    and float(item.get("not_before_epoch") or 0) <= now
                    and self._prior_parts_delivered(item)
                    and ((int(item.get("priority", 3)) <= 1) if realtime_lane else (int(item.get("priority", 3)) >= 2))
                ]
                if not ready:
                    future = [
                        float(item.get("not_before_epoch") or now + 1)
                        for delivery_id, item in self._pending.items()
                        if delivery_id not in self._inflight
                        and ((int(item.get("priority", 3)) <= 1) if realtime_lane else (int(item.get("priority", 3)) >= 2))
                    ]
                    wait_for = max(0.05, min(1.0, min(future) - now)) if future else 1.0
                    self._condition.wait(timeout=wait_for)
                    continue
                item = min(ready, key=lambda row: (int(row.get("priority", 3)), int(row.get("sequence") or 0)))
                self._inflight.add(str(item.get("delivery_id")))
            result = self._attempt_once(item)
            fallback_item: Optional[Dict[str, Any]] = None
            with self._condition:
                delivery_id = str(item.get("delivery_id"))
                self._inflight.discard(delivery_id)
                current = self._pending.get(delivery_id)
                if current is None:
                    continue
                current["attempt"] = int(current.get("attempt") or 0) + 1
                if result.get("success"):
                    record = self._outbox_delivery_record(current, "DELIVERED", result)
                    append_jsonl(self.outbox, record)
                    terminal = {
                        "ts": _now(), "queue_state": "DELIVERED", "delivery_id": delivery_id,
                        "logical_event_id": current.get("logical_event_id"), "item": current,
                        "result": dict(result),
                    }
                    self._append_queue_record(terminal)
                    self._terminal[delivery_id] = terminal
                    self._pending.pop(delivery_id, None)
                elif result.get("retryable"):
                    delay = self._retry_delay(int(current["attempt"]), result)
                    current["not_before_epoch"] = time.time() + delay
                    current["last_result"] = dict(result)
                    append_jsonl(self.outbox, self._outbox_delivery_record(current, "RETRY_SCHEDULED", result))
                    self._append_queue_record({
                        "ts": _now(), "queue_state": "RETRY_SCHEDULED", "delivery_id": delivery_id,
                        "logical_event_id": current.get("logical_event_id"), "retry_after_seconds": delay,
                        "item": current, "result": dict(result),
                    })
                else:
                    append_jsonl(self.outbox, self._outbox_delivery_record(current, "FAILED_PERMANENT", result))
                    terminal = {
                        "ts": _now(), "queue_state": "FAILED_PERMANENT", "delivery_id": delivery_id,
                        "logical_event_id": current.get("logical_event_id"), "item": current,
                        "result": dict(result),
                    }
                    self._append_queue_record(terminal)
                    self._terminal[delivery_id] = terminal
                    self._pending.pop(delivery_id, None)
                    self._cancel_remaining_parts(current)
                    fallback_item = copy.deepcopy(current)
                self._condition.notify_all()
            if fallback_item is not None:
                self._enqueue_fallback_after_permanent_failure(fallback_item)

    def _send_body(
        self,
        body: Dict[str, Any],
        event_id: str,
        *,
        message_type: str,
        audit_text: str,
        fallback_text: str = "",
        priority: Optional[int] = None,
        delivery_context: Optional[Dict[str, Any]] = None,
        wait_for_delivery: bool = False,
    ) -> Dict[str, Any]:
        digest = hashlib.sha256(event_id.encode("utf-8")).hexdigest()[:16]
        record: Dict[str, Any] = {
            "ts": _now(), "event_id": event_id, "digest": digest,
            "message_type": message_type, "text": audit_text, "dry_run": self.dry_run,
        }
        if message_type == "interactive":
            record["card_ui_version"] = CARD_UI_VERSION
            record["card_header"] = (((body.get("card") or {}).get("header") or {}).get("title") or {}).get("content")
        if self._already_sent(event_id):
            return {"ok": True, "deduplicated": True, "event_id": event_id, "message_type": message_type}
        if self.dry_run:
            record["status"] = "DRY_RUN"
            append_jsonl(self.outbox, record)
            return {"ok": True, "dry_run": True, "event_id": event_id, "message_type": message_type}
        if not self.webhook_url:
            record["status"] = "NO_WEBHOOK_CONFIGURED"
            append_jsonl(self.outbox, record)
            return {"ok": False, "error": record["status"], "event_id": event_id}
        bodies = self._text_bodies(str((body.get("content") or {}).get("text") or "")) if message_type == "text" else [body]
        queued = self._enqueue_bodies(
            bodies, event_id, message_type=message_type, audit_text=audit_text,
            fallback_text=fallback_text, priority=priority, delivery_context=delivery_context,
        )
        if wait_for_delivery and queued.get("accepted"):
            return self._wait_for_logical_event(event_id, timeout=max(3.0, self.timeout + 5.0))
        return queued

    def send_text(
        self,
        text: str,
        event_id: str,
        *,
        priority: Optional[int] = None,
        delivery_context: Optional[Dict[str, Any]] = None,
        wait_for_delivery: bool = False,
    ) -> Dict[str, Any]:
        body = {"msg_type": "text", "content": {"text": text}}
        return self._send_body(
            body, event_id, message_type="text", audit_text=text,
            priority=priority, delivery_context=delivery_context, wait_for_delivery=wait_for_delivery,
        )

    def send_card(
        self,
        card: Dict[str, Any],
        event_id: str,
        *,
        fallback_text: str = "",
        priority: Optional[int] = None,
        delivery_context: Optional[Dict[str, Any]] = None,
        wait_for_delivery: bool = False,
    ) -> Dict[str, Any]:
        """发送飞书交互卡片；结构或投递失败时自动补发简短纯文本。"""

        errors = validate_card(card)
        if errors:
            fallback = fallback_text or f"飞书卡片结构异常：{'、'.join(errors)}"
            result = self.send_text(
                fallback, f"{event_id}:text_fallback", priority=priority,
                delivery_context=delivery_context, wait_for_delivery=wait_for_delivery,
            )
            result.update({"card_ok": False, "card_errors": errors, "fallback_used": True})
            return result
        bodies = self._card_bodies(card, fallback_text)
        if any(body.get("msg_type") != "interactive" for body in bodies):
            return self._enqueue_bodies(
                bodies, event_id, message_type="text", audit_text=fallback_text,
                priority=priority, delivery_context=delivery_context,
            )
        body = bodies[0] if len(bodies) == 1 else {"msg_type": "interactive", "card": card}
        if len(bodies) > 1:
            result = self._enqueue_bodies(
                bodies, event_id, message_type="interactive", audit_text=fallback_text or "[interactive card]",
                fallback_text=fallback_text, priority=priority, delivery_context=delivery_context,
            )
            if wait_for_delivery and result.get("accepted"):
                result = self._wait_for_logical_event(event_id, timeout=max(3.0, self.timeout + 5.0))
            result["card_ok"] = bool(result.get("ok"))
            return result
        result = self._send_body(
            body, event_id, message_type="interactive", audit_text=fallback_text or "[interactive card]",
            fallback_text=fallback_text, priority=priority, delivery_context=delivery_context,
            wait_for_delivery=wait_for_delivery,
        )
        if result.get("ok") or result.get("accepted") or self.dry_run:
            result["card_ok"] = True
            return result
        result["card_ok"] = False
        return result


class DeepSeekAdvisor:
    """辅助摘要器；失败时不影响机械规则，不参与逐 Tick 硬门槛。"""

    def __init__(self, key_file: Path = DEFAULT_DEEPSEEK_KEY_FILE):
        self.key_file = Path(os.getenv("A_SHARE_ROTATION_DEEPSEEK_KEY_FILE", str(key_file)))
        self.url = os.getenv("A_SHARE_ROTATION_DEEPSEEK_URL", "https://api.deepseek.com/chat/completions")
        self.model = os.getenv("A_SHARE_ROTATION_DEEPSEEK_MODEL", "deepseek-v4-pro")

    def _read_key(self) -> str:
        if not self.key_file.exists():
            return ""
        for line in self.key_file.read_text(encoding="utf-8", errors="ignore").splitlines():
            if line.strip().startswith("DEEPSEEK_API_KEY="):
                return line.split("=", 1)[1].strip().strip('"').strip("'")
        return ""

    def summarize(self, facts: Dict[str, Any]) -> Dict[str, Any]:
        key = self._read_key()
        if not key:
            return {"ok": False, "status": "NO_KEY"}
        prompt = (
            "你是A股精选池轮动策略的辅助复核员。只根据下面的规则引擎事实包，输出简短中文摘要："
            "1) 市场/细分赛道状态；2) 候选和风险；3) 需要等待什么确认。不得臆造新闻、不得修改规则、不得给出下单指令。\n"
            + json.dumps(facts, ensure_ascii=False, default=_json_default)
        )
        payload = {
            "model": self.model,
            "messages": [{"role": "system", "content": "只做辅助判断，不具有订单权限。"}, {"role": "user", "content": prompt}],
            "temperature": 0.1,
            "stream": False,
        }
        request = urllib.request.Request(
            self.url,
            data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            headers={"Content-Type": "application/json", "Authorization": f"Bearer {key}"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=180) as response:
                result = json.loads(response.read().decode("utf-8", errors="ignore") or "{}")
            content = (((result.get("choices") or [{}])[0].get("message") or {}).get("content") or "").strip()
            return {"ok": bool(content), "status": "OK" if content else "EMPTY", "content": content[:1800]}
        except Exception as exc:
            return {"ok": False, "status": "ERROR", "error_type": type(exc).__name__, "error": str(exc)[:240]}


class AuctionProvider:
    """第三方集合竞价/盘口轮询：腾讯+东方财富，09:25 后加新浪交叉校验。"""

    def __init__(self, symbols: Sequence[str], timeout: float = 5.0):
        self.symbols = list(symbols)
        self.timeout = timeout

    @staticmethod
    def _tencent_code(symbol: str) -> str:
        market, code = symbol.split(".", 1)
        return ("sh" if market == "SHSE" else "sz") + code

    @staticmethod
    def _eastmoney_secid(symbol: str) -> str:
        market, code = symbol.split(".", 1)
        return ("1." if market == "SHSE" else "0.") + code

    @staticmethod
    def _sina_code(symbol: str) -> str:
        return AuctionProvider._tencent_code(symbol)

    def _get(self, url: str, encoding: str = "utf-8") -> str:
        request = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0", "Referer": "https://finance.qq.com"})
        with urllib.request.urlopen(request, timeout=self.timeout) as response:
            return response.read().decode(encoding, errors="ignore")

    @staticmethod
    def _freshness(source_timestamp: Optional[str], expected_trade_date: Optional[str]) -> str:
        if not source_timestamp or not expected_trade_date:
            return "UNKNOWN"
        return "FRESH" if str(source_timestamp)[:10] == expected_trade_date else "STALE"

    def _tencent(self) -> Dict[str, Dict[str, Any]]:
        url = "https://qt.gtimg.cn/q=" + ",".join(self._tencent_code(s) for s in self.symbols)
        result: Dict[str, Dict[str, Any]] = {}
        text = self._get(url, "gbk")
        for part in text.split(";"):
            if '="' not in part or "v_" not in part:
                continue
            left, right = part.split('="', 1)
            code = left.split("v_")[-1].strip()
            symbol = next((s for s in self.symbols if self._tencent_code(s) == code), None)
            values = right.rstrip('"').split("~")
            if not symbol or len(values) < 6:
                continue
            try:
                price, prev_close = _safe_float(values[3]), _safe_float(values[4])
                if price <= 0:
                    continue
                top5 = []
                for i in range(5):
                    top5.append({"level": i + 1, "bid_p": _safe_float(values[9 + i * 2] if len(values) > 9 + i * 2 else 0), "bid_v": _safe_float(values[10 + i * 2] if len(values) > 10 + i * 2 else 0) * 100, "ask_p": _safe_float(values[19 + i * 2] if len(values) > 19 + i * 2 else 0), "ask_v": _safe_float(values[20 + i * 2] if len(values) > 20 + i * 2 else 0) * 100})
                bid, ask = top5[0], top5[0]
                source_timestamp = None
                if len(values) > 30 and len(values[30]) >= 14:
                    source_timestamp = datetime.strptime(values[30][:14], "%Y%m%d%H%M%S").isoformat()
                result[symbol] = {"symbol": symbol, "name": values[1], "provider": "tencent", "price": price, "prev_close": prev_close, "pct_chg": price / prev_close - 1 if prev_close else 0, "bid1_price": bid["bid_p"], "bid1_volume": bid["bid_v"], "ask1_price": ask["ask_p"], "ask1_volume": ask["ask_v"], "top5": top5, "received_at": _now(), "source_timestamp": source_timestamp, "timestamp_quality": "provider_timestamp" if source_timestamp else "local_receive_time"}
            except Exception:
                continue
        return result

    def _sina(self) -> Dict[str, Dict[str, Any]]:
        url = "https://hq.sinajs.cn/list=" + ",".join(self._sina_code(s) for s in self.symbols)
        result: Dict[str, Dict[str, Any]] = {}
        text = self._get(url, "gbk")
        for part in text.split(";"):
            if '="' not in part or "hq_str_" not in part:
                continue
            left, right = part.split('="', 1)
            code = left.split("hq_str_")[-1].strip()
            symbol = next((s for s in self.symbols if self._sina_code(s) == code), None)
            values = right.rstrip('"').split(",")
            if not symbol or len(values) < 32:
                continue
            price, prev_close = _safe_float(values[3]), _safe_float(values[2])
            if price <= 0:
                continue
            top5 = [{"level": i + 1, "bid_p": _safe_float(values[11 + i * 2]), "bid_v": _safe_float(values[10 + i * 2]), "ask_p": _safe_float(values[21 + i * 2]), "ask_v": _safe_float(values[20 + i * 2])} for i in range(5)]
            source_timestamp = None
            if len(values) > 31 and values[30] and values[31]:
                source_timestamp = f"{values[30]}T{values[31]}"
            result[symbol] = {"symbol": symbol, "name": values[0], "provider": "sina", "price": price, "prev_close": prev_close, "pct_chg": price / prev_close - 1 if prev_close else 0, "bid1_price": top5[0]["bid_p"], "bid1_volume": top5[0]["bid_v"], "ask1_price": top5[0]["ask_p"], "ask1_volume": top5[0]["ask_v"], "top5": top5, "received_at": _now(), "source_timestamp": source_timestamp, "timestamp_quality": "provider_timestamp" if source_timestamp else "local_receive_time"}
        return result

    def _eastmoney(self) -> Dict[str, Dict[str, Any]]:
        fields = "f12,f13,f14,f2,f3,f18,f31,f32,f124"
        query = urllib.parse.urlencode({"fltt": "2", "fields": fields, "secids": ",".join(self._eastmoney_secid(s) for s in self.symbols)})
        text = self._get("https://push2delay.eastmoney.com/api/qt/ulist.np/get?" + query)
        data = json.loads(text or "{}")
        result: Dict[str, Dict[str, Any]] = {}
        for item in ((data.get("data") or {}).get("diff") or []):
            code, market = str(item.get("f12", "")), str(item.get("f13", ""))
            symbol = ("SHSE." if market == "1" else "SZSE.") + code
            if symbol not in self.symbols:
                continue
            price, prev_close = _safe_float(item.get("f2")), _safe_float(item.get("f18"))
            if price <= 0:
                continue
            source_timestamp = None
            source_epoch = int(_safe_float(item.get("f124"), 0))
            if source_epoch > 0:
                source_timestamp = datetime.fromtimestamp(source_epoch).isoformat()
            result[symbol] = {"symbol": symbol, "name": item.get("f14", ""), "provider": "eastmoney_delay", "price": price, "prev_close": prev_close, "pct_chg": _safe_float(item.get("f3")) / 100, "bid1_price": _safe_float(item.get("f31")), "ask1_price": _safe_float(item.get("f32")), "top5": [], "received_at": _now(), "source_timestamp": source_timestamp, "timestamp_quality": "provider_timestamp" if source_timestamp else "local_receive_time"}
        return result

    def snapshot(self, include_sina: bool = False, expected_trade_date: Optional[str] = None) -> Dict[str, Any]:
        providers: Dict[str, Dict[str, Dict[str, Any]]] = {}
        errors: Dict[str, str] = {}
        for name, function in (("tencent", self._tencent), ("eastmoney_delay", self._eastmoney)):
            try:
                providers[name] = function()
            except Exception as exc:
                errors[name] = f"{type(exc).__name__}: {str(exc)[:160]}"
        if include_sina:
            try:
                providers["sina"] = self._sina()
            except Exception as exc:
                errors["sina"] = f"{type(exc).__name__}: {str(exc)[:160]}"

        rows: List[Dict[str, Any]] = []
        for symbol in self.symbols:
            candidates = [provider[symbol] for provider in providers.values() if symbol in provider]
            if not candidates:
                continue
            for candidate in candidates:
                candidate["source_freshness"] = self._freshness(candidate.get("source_timestamp"), expected_trade_date)
            usable = [row for row in candidates if row.get("source_freshness") != "STALE"]
            selected_pool = usable or candidates
            selected = max(selected_pool, key=lambda row: (len(row.get("top5") or []), 1 if row.get("provider") == "tencent" else 0))
            prices = [_safe_float(row.get("price")) for row in usable if _safe_float(row.get("price")) > 0]
            row = dict(selected)
            row["provider_count"] = len(usable)
            row["provider_total_count"] = len(candidates)
            row["cross_source_spread"] = max(prices) - min(prices) if len(prices) >= 2 else None
            bid_volume = _safe_float(row.get("bid1_volume"))
            ask_volume = _safe_float(row.get("ask1_volume"))
            row["bid_ask_imbalance"] = (bid_volume - ask_volume) / (bid_volume + ask_volume) if bid_volume + ask_volume > 0 else None
            row["auction_path"] = self._path(row)
            rows.append(row)
        return {"snapshot_at": _now(), "expected_trade_date": expected_trade_date, "include_sina": include_sina, "rows": rows, "provider_errors": errors, "pool_size": len(self.symbols)}

    @staticmethod
    def _path(row: Dict[str, Any]) -> str:
        spread = _safe_float(row.get("cross_source_spread"), float("nan"))
        imbalance = _safe_float(row.get("bid_ask_imbalance"), 0.0)
        if math.isfinite(spread) and spread > max(_safe_float(row.get("prev_close")) * 0.003, 0.02):
            return "CROSS_SOURCE_CONFLICT"
        if imbalance < -0.35:
            return "SELL_PRESSURE"
        if imbalance > 0.35:
            return "BUY_PRESSURE"
        if not row.get("top5"):
            return "NO_DEPTH_PRICE_ANCHOR"
        return "BALANCED"


class LiveSignalService:
    def __init__(self, notifier: Optional[FeishuNotifier] = None, advisor: Optional[DeepSeekAdvisor] = None):
        self.pool_membership = load_pool_entries()
        self.pool = list(self.pool_membership)
        self.taxonomy = load_taxonomy()
        self.builder = DailyCandidateBuilder(self.pool, self.taxonomy, pool_membership=self.pool_membership)
        self.notifier = notifier or FeishuNotifier()
        self.advisor = advisor or DeepSeekAdvisor()
        self.auction = AuctionProvider(self.pool)
        self.auction_analyzer = AuctionPathAnalyzer()
        self.latest_daily: Dict[str, Any] = {}
        self.latest_auction: Dict[str, Any] = {}
        self.latest_auction_analysis: Dict[str, Any] = {}
        self.auction_trade_date: Optional[str] = None
        self.intraday = IntradayEventEngine()
        self.limit_behavior = LimitBehaviorEngine()
        self.continuation = TrendContinuationAnalyzer()
        self.capital_behavior = CapitalBehaviorEngine()
        self.multitimeframe = MultiTimeframeIndicatorEngine()
        self.structured_timing = StructuredTimingEngine()
        self.numeric_tags = NumericPatternTagPlugin()
        self.multitimeframe_seed_status: Dict[str, Any] = {}
        self.structured_timing_seed_status: Dict[str, Any] = {}
        self.sector_health = LiveSectorHealthEngine(self.taxonomy)
        self.market_sector_radar = FullMarketSectorRadar()
        self.global_market = GlobalMarketMonitor()
        self.market_permission = MarketPermissionEngine()
        self.dynamic_universe = DynamicUniverseManager(
            max_active=int(os.getenv("A_SHARE_ROTATION_DYNAMIC_MAX_ACTIVE", "12")),
            max_per_board=2,
            minimum_score=int(os.getenv("A_SHARE_ROTATION_DYNAMIC_MIN_SCORE", "68")),
        )
        self.dynamic_candidates: Dict[str, Dict[str, Any]] = {}
        self.dynamic_subscribe_callback: Optional[Callable[[Sequence[str]], None]] = None
        self.dynamic_unsubscribe_callback: Optional[Callable[[Sequence[str]], None]] = None
        self.dynamic_reconcile_lock = threading.RLock()
        self.dynamic_last_check_monotonic = 0.0
        self.market_sector_refresh_lock = threading.RLock()
        self.market_sector_refresh_thread: Optional[threading.Thread] = None
        self.market_sector_refresh_started_at: Optional[datetime] = None
        self.last_tick_log_second: Dict[str, str] = {}
        self.last_summary_group_states: Dict[str, str] = {}
        self.last_summary_strength_buckets: Dict[str, str] = {}
        self.last_dynamic_core_symbols: List[str] = []
        self.context = None
        self.last_daily_history_source = "UNINITIALIZED"
        self.virtual_positions = self._load_virtual_positions()
        self.intraday_numeric_restore_status: Dict[str, Any] = {"status": "NOT_ATTEMPTED"}
        self.notifier.set_revalidator(self._revalidate_feishu_delivery)

    @staticmethod
    def _load_virtual_positions() -> Dict[str, Dict[str, Any]]:
        if not VIRTUAL_LEDGER_FILE.exists():
            return {}
        try:
            payload = json.loads(VIRTUAL_LEDGER_FILE.read_text(encoding="utf-8"))
            return payload.get("positions", {}) if isinstance(payload, dict) else {}
        except Exception:
            return {}

    def _save_virtual_positions(self) -> None:
        VIRTUAL_LEDGER_FILE.parent.mkdir(parents=True, exist_ok=True)
        payload = {"updated_at": _now(), "kind": "SIGNAL_LEDGER_NOT_BROKER_POSITIONS", "positions": self.virtual_positions}
        temporary = VIRTUAL_LEDGER_FILE.with_suffix(".tmp")
        temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2, default=_json_default), encoding="utf-8")
        temporary.replace(VIRTUAL_LEDGER_FILE)

    def initial_subscription_symbols(self) -> List[str]:
        """固定池永远订阅；未来的全市场动态持仓重启后也不能丢失卖点。"""

        return list(dict.fromkeys([*self.pool, *self.virtual_positions.keys()]))

    def configure_dynamic_subscription(
        self,
        subscribe_callback: Callable[[Sequence[str]], None],
        unsubscribe_callback: Callable[[Sequence[str]], None],
    ) -> None:
        self.dynamic_subscribe_callback = subscribe_callback
        self.dynamic_unsubscribe_callback = unsubscribe_callback

    def _log(self, kind: str, payload: Dict[str, Any]) -> None:
        day = datetime.now().strftime("%Y-%m-%d")
        append_jsonl(LIVE_ROOT / day / f"{kind}.jsonl", {"logged_at": _now(), **payload})

    def _revalidate_feishu_delivery(self, item: Dict[str, Any]) -> Dict[str, Any]:
        """为延迟超过20秒的交易事件补充当前状态，不删除原始事件。"""

        context = item.get("delivery_context") or {}
        if context.get("kind") != "TRADE_EVENT":
            return {"status": "NOT_APPLICABLE", "message": "这是延迟送达的状态消息，请以标题中的数据截止时间为准。"}
        symbol = str(context.get("symbol") or "")
        original_event = str(context.get("event") or "")
        original_event_id = str(item.get("logical_event_id") or "")
        current = (self.intraday.snapshot().get("by_symbol") or {}).get(symbol) or {}
        if not current:
            return {
                "status": "NO_CURRENT_STATE",
                "message": "暂时无法取得当前状态；保留原事件供复盘，禁止把延迟消息直接当作当前成交指令。",
            }
        last_events = current.get("last_event") or {}
        phase = str(current.get("phase") or "UNKNOWN")
        current_price = _safe_float(current.get("price"))
        vwap = _safe_float(current.get("vwap"))
        kind = "BUY" if original_event == "BUY_EVENT_WATCH" else (
            "SELL" if original_event == "SELL_EVENT_WATCH" else (
                "RISK" if original_event == "RISK_EVENT_WATCH" else "OPPORTUNITY"
            )
        )
        same_latest = str(last_events.get(kind) or "") == original_event_id
        price_line = f"当前价{current_price:.3f}" + (f"、VWAP {vwap:.3f}" if vwap > 0 else "")
        if same_latest:
            return {
                "status": "STILL_LATEST_FOR_KIND",
                "message": f"该事件仍是本类别最新事件；当前阶段{phase}，{price_line}，执行前仍需查看是否已有更新的反向事件。",
            }
        return {
            "status": "SUPERSEDED_OR_STATE_CHANGED",
            "message": f"事件后状态已经变化；当前阶段{phase}，{price_line}。保留原事件供审计，但不得直接执行旧动作，以最新实时事件为准。",
        }

    def refresh_market_sectors_async(self, force: bool = False) -> Dict[str, Any]:
        """后台刷新全市场板块排名，避免网络请求阻塞Tick回调。"""
        now = datetime.now()
        with self.market_sector_refresh_lock:
            if self.market_sector_refresh_thread is not None and self.market_sector_refresh_thread.is_alive():
                return {"status": "ALREADY_RUNNING"}
            latest_asof = self.market_sector_radar.latest.get("asof")
            if isinstance(latest_asof, datetime) and not force and (now - latest_asof).total_seconds() < 240:
                return {"status": "FRESH_CACHE", "asof": latest_asof.isoformat()}

            def worker() -> None:
                result = self.market_sector_radar.refresh()
                full_market_stock_candidates = self.market_sector_radar.full_market_core_candidates(limit=6)
                permission = self.market_permission.evaluate(
                    result,
                    self.global_market.latest,
                    premarket_daily=self.latest_daily,
                )
                self._log("market_sector_snapshots", {
                    "status": result.get("status"),
                    "asof": result.get("asof"),
                    "elapsed_ms": result.get("elapsed_ms"),
                    "row_count": result.get("row_count"),
                    "raw_row_count": result.get("raw_row_count"),
                    "eligible_row_count": result.get("eligible_row_count"),
                    "excluded_row_count": result.get("excluded_row_count"),
                    "market_regime": result.get("market_regime"),
                    "market_median_pct": result.get("market_median_pct"),
                    "market_positive_breadth": result.get("market_positive_breadth"),
                    "top_decile_overlap": result.get("top_decile_overlap"),
                    "technical_ready_count": result.get("technical_ready_count"),
                    "stock_meta": result.get("stock_meta"),
                    "limit_up_meta": result.get("limit_up_meta"),
                    "source": result.get("source"),
                    "last_error": result.get("last_error"),
                    "top_boards": result.get("rows", [])[:20],
                    "all_market_compact": [
                        {key: row.get(key) for key in (
                            "board_code", "board_name", "board_type", "board_pct", "board_amount",
                            "turnover_rate", "volume_ratio", "main_net_inflow", "up_count", "down_count",
                            "breadth", "leading_stock", "leading_stock_pct", "limit_up_count",
                            "first_board_count", "multi_board_count", "max_board_streak", "ladder_levels",
                            "technical_status", "technical_score", "health_score_raw", "health_percentile",
                            "percentile_delta", "top_quartile_persistence", "rotation_state",
                            "entry_support", "rotation_caution", "market_regime",
                        )}
                        for row in result.get("rows", [])
                    ],
                    "full_market_stock_candidates": [
                        {key: row.get(key) for key in (
                            "symbol", "code", "name", "price", "pct", "amount", "volume_ratio",
                            "discovery_score", "entry_logic_match", "strategy_match_grade",
                            "relative_excess_vs_board", "early_location_cap", "next_validation",
                            "cancel_when", "reason",
                        )} | {
                            "matched_board": {
                                key: (row.get("matched_board") or {}).get(key) for key in (
                                    "board_code", "board_name", "rotation_state", "health_percentile",
                                    "entry_support", "rotation_caution",
                                )
                            },
                        }
                        for row in full_market_stock_candidates
                    ],
                })
                self._log("market_permission_snapshots", {"stage": "market_sector_refresh", **permission})

            self.market_sector_refresh_started_at = now
            self.market_sector_refresh_thread = threading.Thread(
                target=worker,
                name="AshareRotationMarketSectorRadar",
                daemon=True,
            )
            self.market_sector_refresh_thread.start()
            return {"status": "STARTED", "started_at": now.isoformat()}

    def _frames_from_context(self, context: Any) -> Dict[str, pd.DataFrame]:
        """优先批量读取明确前复权的历史日线；上下文缓存只作为降级路径。"""
        frames: Dict[str, pd.DataFrame] = {}
        try:
            from gm.api import ADJUST_PREV, history

            decision_time = self._context_now(context)
            end_time = decision_time.strftime("%Y-%m-%d %H:%M:%S")
            start_time = (decision_time - timedelta(days=2200)).strftime("%Y-%m-%d")
            batch = history(
                ",".join(self.pool),
                "1d",
                start_time,
                end_time,
                fields="symbol,eob,open,high,low,close,volume,amount,pre_close",
                skip_suspended=True,
                adjust=ADJUST_PREV,
                df=True,
            )
            if batch is not None and len(batch) and "symbol" in batch.columns:
                for symbol, part in batch.groupby("symbol", sort=False):
                    frames[str(symbol)] = part.copy().reset_index(drop=True)
                self.last_daily_history_source = "GOLDMINER_HISTORY_RANGE_BATCH_ADJUST_PREV"
                return frames
        except Exception as exc:
            self._log("errors", {"kind": "daily_history_batch", "error_type": type(exc).__name__, "error": str(exc)[:240]})

        for symbol in self.pool:
            try:
                frame = context.data(symbol, frequency="1d", count=1600, fields="symbol,eob,open,high,low,close,volume,amount,pre_close")
                if frame is not None and len(frame):
                    frames[symbol] = frame.copy()
            except Exception as exc:
                self._log("errors", {"kind": "daily_history", "symbol": symbol, "error_type": type(exc).__name__, "error": str(exc)[:180]})
        self.last_daily_history_source = "CONTEXT_DAILY_CACHE_FALLBACK_UNVERIFIED_ADJUSTMENT"
        return frames

    def seed_intraday_history(self, context: Any = None) -> Dict[str, Any]:
        """从D盘预热分钟指标，并用GoldMiner把缓存补到D-1。

        固定研究缓存截止于2026-08-07，实盘不能永远沿用这个截面；因此后续交易日
        只增量拉取缺口并写入D盘live_1m_seed。若增量失败，自检会明确降级/失败。
        """
        if (
            self.multitimeframe_seed_status.get("ready_count") == len(self.pool)
            and self.structured_timing_seed_status.get("ready_count") == len(self.pool)
        ):
            return self.multitimeframe_seed_status
        MINUTE_LIVE_CACHE_ROOT.mkdir(parents=True, exist_ok=True)
        frames: Dict[str, pd.DataFrame] = {}
        source_paths: Dict[str, Path] = {}
        for symbol in self.pool:
            live_path = MINUTE_LIVE_CACHE_ROOT / f"{symbol}_1m.pkl"
            archive_path = MINUTE_HISTORY_ROOT / f"{symbol}_1m.pkl"
            path = live_path if live_path.exists() else archive_path
            source_paths[symbol] = path
            try:
                frames[symbol] = pd.read_pickle(path) if path.exists() else pd.DataFrame()
            except Exception:
                frames[symbol] = pd.DataFrame()

        expected_asof = str(self.latest_daily.get("asof") or "")[:10]
        stale = []
        for symbol, frame in frames.items():
            last_date = ""
            if len(frame) and "eob" in frame.columns:
                parsed = pd.to_datetime(frame["eob"], errors="coerce").dropna()
                last_date = parsed.max().strftime("%Y-%m-%d") if len(parsed) else ""
            if not last_date or (expected_asof and last_date < expected_asof):
                stale.append(symbol)

        fetch_error = None
        if stale and context is not None and expected_asof:
            try:
                from gm.api import ADJUST_PREV, history

                starts = []
                for symbol in stale:
                    frame = frames[symbol]
                    if len(frame) and "eob" in frame.columns:
                        parsed = pd.to_datetime(frame["eob"], errors="coerce").dropna()
                        if len(parsed):
                            starts.append((parsed.max() - timedelta(days=2)).strftime("%Y-%m-%d"))
                start_time = min(starts) if starts else (pd.Timestamp(expected_asof) - pd.Timedelta(days=120)).strftime("%Y-%m-%d")
                fetched = history(
                    ",".join(stale), "1m", start_time, f"{expected_asof} 15:00:00",
                    fields="symbol,eob,open,high,low,close,volume,amount,pre_close",
                    skip_suspended=True, adjust=ADJUST_PREV, df=True,
                )
                if fetched is not None and len(fetched) and "symbol" in fetched.columns:
                    for symbol, part in fetched.groupby("symbol", sort=False):
                        symbol = str(symbol)
                        old = frames.get(symbol, pd.DataFrame())
                        merged = pd.concat([old, part.copy()], ignore_index=True) if len(old) else part.copy()
                        merged["eob"] = pd.to_datetime(merged["eob"], errors="coerce")
                        merged = merged.dropna(subset=["eob"]).sort_values("eob").drop_duplicates("eob", keep="last")
                        frames[symbol] = merged.reset_index(drop=True)
                        frames[symbol].to_pickle(MINUTE_LIVE_CACHE_ROOT / f"{symbol}_1m.pkl")
            except Exception as exc:
                fetch_error = f"{type(exc).__name__}: {str(exc)[:220]}"
                self._log("errors", {"kind": "minute_history_increment", "symbols": stale, "error": fetch_error})

        rows = []
        timing_rows = []
        for symbol in self.pool:
            row = self.multitimeframe.seed(symbol, frames.get(symbol))
            row["source"] = str(source_paths.get(symbol, "MISSING"))
            rows.append(row)
            timing_row = self.structured_timing.seed(symbol, frames.get(symbol))
            timing_row["source"] = str(source_paths.get(symbol, "MISSING"))
            timing_rows.append(timing_row)
        self.multitimeframe_seed_status = {
            "root": str(MINUTE_LIVE_CACHE_ROOT),
            "archive_root": str(MINUTE_HISTORY_ROOT),
            "expected_asof": expected_asof or None,
            "symbol_count": len(rows),
            "ready_count": sum(row["seed_1m_count"] > 0 for row in rows),
            "stale_before_increment": stale,
            "fetch_error": fetch_error,
            "rows": rows,
        }
        self.structured_timing_seed_status = {
            "root": str(MINUTE_LIVE_CACHE_ROOT),
            "expected_asof": expected_asof or None,
            "symbol_count": len(timing_rows),
            "ready_count": sum(row.get("status") == "READY" for row in timing_rows),
            "rows": timing_rows,
            "rules_version": STRUCTURED_TIMING_VERSION,
            "strategy_effect": "NONE_SHADOW_ZERO_WEIGHT",
        }
        self._log("multitimeframe_seed", self.multitimeframe_seed_status)
        self._log("structured_timing_seed", self.structured_timing_seed_status)
        return self.multitimeframe_seed_status

    @staticmethod
    def _group_history_frame(frame: Optional[pd.DataFrame]) -> Dict[str, pd.DataFrame]:
        if frame is None or not len(frame) or "symbol" not in frame.columns:
            return {}
        return {
            str(symbol): part.copy().reset_index(drop=True)
            for symbol, part in frame.groupby("symbol", sort=False)
        }

    @staticmethod
    def _dynamic_taxonomy_for(discoveries: Sequence[Mapping[str, Any]]) -> Dict[str, Any]:
        symbols: Dict[str, Dict[str, Any]] = {}
        for row in discoveries:
            symbol = str(row.get("symbol") or "")
            if not symbol:
                continue
            board = dict(row.get("matched_board") or {})
            board_name = str(board.get("board_name") or row.get("industry") or "全市场动态方向")
            stable_themes = [board_name]
            stable_themes.extend(str(value) for value in row.get("concepts") or [] if value)
            stable_themes = list(dict.fromkeys(value for value in stable_themes if value))[:8]
            symbols[symbol] = {
                "name": str(row.get("name") or symbol.split(".")[-1]),
                "primary_industry": str(row.get("industry") or board_name),
                "subindustry": board_name,
                "niche": board_name,
                "stable_themes": stable_themes,
                "classification_status": "INTRADAY_POINT_IN_TIME_DISCOVERY",
                "source": "eastmoney_full_market_stock_and_board_snapshot",
            }
        return {
            "taxonomy_version": "dynamic_point_in_time_taxonomy_v1",
            "scope": "FULL_MARKET_DYNAMIC_DEEP_VALIDATION",
            "symbols": symbols,
        }

    def _merge_dynamic_numeric_tags(
        self,
        frames: Mapping[str, pd.DataFrame],
        *,
        asof: str,
        names: Mapping[str, str],
    ) -> None:
        """动态票复用同一TAG规则，但仍保持零权重旁路。"""

        if not frames:
            return
        temporary = NumericPatternTagPlugin()
        result = temporary.scan_recent_daily_extremes(
            dict(frames),
            asof=asof,
            names=dict(names),
            source="GOLDMINER_DYNAMIC_HISTORY_ADJUST_PREV",
        )
        destination = self.numeric_tags.latest_scan
        if destination.get("status") != "READY":
            return
        destination.setdefault("by_symbol", {}).update(result.get("by_symbol") or {})
        existing_high_ids = {
            (row.get("symbol"), row.get("trade_date"), row.get("price_text"))
            for row in destination.get("hits") or []
        }
        for row in result.get("hits") or []:
            key = (row.get("symbol"), row.get("trade_date"), row.get("price_text"))
            if key not in existing_high_ids:
                destination.setdefault("hits", []).append(row)
        existing_low_ids = {
            (row.get("symbol"), row.get("anchor_trade_date"), row.get("price_text"))
            for row in destination.get("low_support_hits") or []
        }
        for row in result.get("low_support_hits") or []:
            key = (row.get("symbol"), row.get("anchor_trade_date"), row.get("price_text"))
            if key not in existing_low_ids:
                destination.setdefault("low_support_hits", []).append(row)

    def _prepare_dynamic_discoveries(
        self,
        context: Any,
        discoveries: Sequence[Mapping[str, Any]],
        *,
        now: datetime,
    ) -> Dict[str, Any]:
        """在订阅前一次性补齐 D-1 日线和截至当前的已完成分钟K。"""

        symbols = [str(row.get("symbol") or "") for row in discoveries if row.get("symbol")]
        if not symbols:
            return {"ready": {}, "failures": {}, "data_quality": {}}
        asof = str(self.latest_daily.get("asof") or "")[:10]
        if not asof:
            return {
                "ready": {},
                "failures": {symbol: "固定池D-1截面尚未就绪" for symbol in symbols},
                "data_quality": {},
            }
        try:
            from gm.api import ADJUST_PREV, history

            daily = history(
                ",".join(symbols), "1d",
                (now - timedelta(days=2200)).strftime("%Y-%m-%d"),
                f"{asof} 23:59:59",
                fields="symbol,eob,open,high,low,close,volume,amount,pre_close",
                skip_suspended=True, adjust=ADJUST_PREV, df=True,
            )
            minute_end = (pd.Timestamp(now).floor("min") - pd.Timedelta(seconds=1)).strftime("%Y-%m-%d %H:%M:%S")
            minute = history(
                ",".join(symbols), "1m",
                (now - timedelta(days=75)).strftime("%Y-%m-%d"), minute_end,
                fields="symbol,eob,open,high,low,close,volume,amount,pre_close",
                skip_suspended=True, adjust=ADJUST_PREV, df=True,
            )
        except Exception as exc:
            reason = f"动态历史拉取失败：{type(exc).__name__}: {str(exc)[:180]}"
            return {"ready": {}, "failures": {symbol: reason for symbol in symbols}, "data_quality": {}}

        daily_frames = self._group_history_frame(daily)
        minute_frames = self._group_history_frame(minute)
        taxonomy = self._dynamic_taxonomy_for(discoveries)
        membership = {
            symbol: {
                "symbol": symbol,
                "pool_group": "FULL_MARKET_DYNAMIC",
                "pool_group_cn": "全市场动态池",
                "pool_tags": ["全市场动态池", "板块动量深检"],
                "is_research_pool": False,
                # 这是实时发现名单；生效时刻另行记录，不能倒填为盘前已知。
                "pool_recorded_on": asof,
            }
            for symbol in symbols
        }
        builder = DailyCandidateBuilder(
            symbols, taxonomy, pool_recorded_on=asof, pool_membership=membership,
        )
        try:
            built = builder.build(daily_frames, asof=asof)
        except Exception as exc:
            reason = f"动态日线特征构建失败：{type(exc).__name__}: {str(exc)[:180]}"
            return {"ready": {}, "failures": {symbol: reason for symbol in symbols}, "data_quality": {}}
        candidate_map = {
            str(row.get("symbol")): dict(row)
            for row in built.get("candidates") or [] if row.get("symbol")
        }
        discovery_map = {str(row.get("symbol")): dict(row) for row in discoveries}
        ready: Dict[str, Dict[str, Any]] = {}
        quality_by_symbol: Dict[str, Dict[str, Any]] = {}
        failures: Dict[str, str] = {}
        DYNAMIC_DAILY_CACHE_ROOT.mkdir(parents=True, exist_ok=True)
        DYNAMIC_MINUTE_CACHE_ROOT.mkdir(parents=True, exist_ok=True)
        for symbol in symbols:
            daily_frame = daily_frames.get(symbol, pd.DataFrame()).copy()
            minute_frame = minute_frames.get(symbol, pd.DataFrame()).copy()
            daily_dates = pd.to_datetime(daily_frame.get("eob"), errors="coerce").dropna() if len(daily_frame) else pd.Series(dtype="datetime64[ns]")
            minute_dates = pd.to_datetime(minute_frame.get("eob"), errors="coerce").dropna() if len(minute_frame) else pd.Series(dtype="datetime64[ns]")
            daily_last = daily_dates.max() if len(daily_dates) else pd.NaT
            minute_last = minute_dates.max() if len(minute_dates) else pd.NaT
            current_day_minute_required = now.strftime("%H:%M:%S") >= "09:36:00"
            minute_intraday_fresh = bool(
                not pd.isna(minute_last)
                and (
                    str(minute_last)[:10] == now.strftime("%Y-%m-%d")
                    if current_day_minute_required else str(minute_last)[:10] >= asof
                )
            )
            quality = {
                "daily_count": int(len(daily_frame)),
                "daily_last_eob": None if pd.isna(daily_last) else pd.Timestamp(daily_last).isoformat(),
                "daily_asof_required": asof,
                "daily_front_adjusted": True,
                "minute_count": int(len(minute_frame)),
                "minute_last_eob": None if pd.isna(minute_last) else pd.Timestamp(minute_last).isoformat(),
                "minute_current_day_required": current_day_minute_required,
                "minute_intraday_fresh": minute_intraday_fresh,
                "candidate_built": symbol in candidate_map,
                "point_in_time_discovery_at": now.isoformat(),
                "formal_ready": bool(
                    len(daily_frame) >= MIN_DAILY_BARS
                    and not pd.isna(daily_last) and str(daily_last)[:10] == asof
                    and len(minute_frame) >= 800 and minute_intraday_fresh
                    and symbol in candidate_map
                ),
                "no_lookahead": True,
                "version": DYNAMIC_UNIVERSE_VERSION,
            }
            quality_by_symbol[symbol] = quality
            if not quality["formal_ready"]:
                failures[symbol] = (
                    f"动态深检未就绪：日线{quality['daily_count']}根/截止{quality['daily_last_eob']}，"
                    f"分钟{quality['minute_count']}根/截止{quality['minute_last_eob']}"
                )
                continue
            daily_frame.to_pickle(DYNAMIC_DAILY_CACHE_ROOT / f"{_safe_name(symbol)}_1d.pkl")
            minute_frame.to_pickle(DYNAMIC_MINUTE_CACHE_ROOT / f"{_safe_name(symbol)}_1m.pkl")
            candidate = candidate_map[symbol]
            discovery = discovery_map[symbol]
            candidate.update({
                "pool_group": "FULL_MARKET_DYNAMIC",
                "pool_group_cn": "全市场动态池",
                "pool_tags": ["全市场动态池", "板块动量深检"],
                "dynamic_discovery": discovery,
                "dynamic_admitted_at": now.isoformat(),
                "dynamic_formal_data_ready": True,
                "dynamic_universe_version": DYNAMIC_UNIVERSE_VERSION,
                "market_sector": dict(discovery.get("matched_board") or {}),
                "overall_no_lookahead": True,
            })
            self.multitimeframe.seed(symbol, minute_frame)
            self.structured_timing.seed(symbol, minute_frame)
            ready[symbol] = candidate
        self._merge_dynamic_numeric_tags(
            {symbol: daily_frames[symbol] for symbol in ready if symbol in daily_frames},
            asof=asof,
            names={symbol: str(ready[symbol].get("name") or "") for symbol in ready},
        )
        return {"ready": ready, "failures": failures, "data_quality": quality_by_symbol}

    def reconcile_dynamic_universe(self, context: Any = None, *, force: bool = False) -> Dict[str, Any]:
        """在 GoldMiner 回调主线程中完成动态深检和订阅变更。

        外部网络板块刷新仍在后台线程；这里仅消费一份已经完成的新快照。
        """

        now = self._context_now(context)
        if not force and not is_continuous_session(now):
            return {"status": "OUTSIDE_CONTINUOUS_SESSION"}
        with self.dynamic_reconcile_lock:
            monotonic_now = time.monotonic()
            if not force and monotonic_now - self.dynamic_last_check_monotonic < 20.0:
                return {"status": "THROTTLED"}
            self.dynamic_last_check_monotonic = monotonic_now
            latest = self.market_sector_radar.latest
            radar_asof = latest.get("asof")
            if latest.get("status") not in {"GREEN", "YELLOW"} or not radar_asof:
                return {"status": "RADAR_NOT_READY", "radar_status": latest.get("status")}
            if isinstance(radar_asof, datetime) and abs((now - radar_asof).total_seconds()) > 600:
                return {"status": "STALE_RADAR_SNAPSHOT", "radar_asof": radar_asof.isoformat()}
            snapshot_id = radar_asof.isoformat() if isinstance(radar_asof, datetime) else str(radar_asof)
            discoveries = self.market_sector_radar.full_market_core_candidates(limit=36)
            protected = set(self.virtual_positions) - set(self.pool)
            plan = self.dynamic_universe.plan(
                discoveries,
                snapshot_id=snapshot_id,
                now=now,
                base_symbols=self.pool,
                protected_symbols=protected,
            )
            if plan.get("status") == "UNCHANGED_SNAPSHOT":
                return plan

            prepared = self._prepare_dynamic_discoveries(context, plan.get("to_prepare") or [], now=now)
            ready_map = prepared.get("ready") or {}
            failures = prepared.get("failures") or {}
            subscribed: List[str] = []
            subscription_error = None
            if ready_map:
                if self.dynamic_subscribe_callback is None:
                    subscription_error = "动态订阅回调未配置"
                else:
                    try:
                        self.dynamic_subscribe_callback(list(ready_map))
                        subscribed = list(ready_map)
                    except Exception as exc:
                        subscription_error = f"{type(exc).__name__}: {str(exc)[:220]}"
            if subscription_error:
                for symbol in ready_map:
                    failures[symbol] = f"Tick订阅失败：{subscription_error}"
            for symbol in subscribed:
                discovery = next(
                    (dict(row) for row in plan.get("to_prepare") or [] if str(row.get("symbol")) == symbol),
                    {},
                )
                candidate = ready_map[symbol]
                quality = (prepared.get("data_quality") or {}).get(symbol) or {}
                self.dynamic_candidates[symbol] = candidate
                self.dynamic_universe.activate(
                    symbol, candidate=candidate, discovery=discovery,
                    data_quality=quality, now=now,
                )
            for symbol, reason in failures.items():
                self.dynamic_universe.mark_prepare_failure(symbol, reason, now=now)

            retired: List[str] = []
            retire_errors: Dict[str, str] = {}
            for symbol in plan.get("to_retire") or []:
                if symbol in protected:
                    continue
                try:
                    if self.dynamic_unsubscribe_callback is None:
                        raise RuntimeError("动态退订回调未配置")
                    self.dynamic_unsubscribe_callback([symbol])
                    retired.append(symbol)
                except Exception as exc:
                    retire_errors[symbol] = f"{type(exc).__name__}: {str(exc)[:220]}"
            self.dynamic_universe.retire(retired)
            for symbol in retired:
                self.dynamic_candidates.pop(symbol, None)

            result = {
                "status": "RECONCILED" if not subscription_error and not retire_errors else "DEGRADED",
                "asof": now.isoformat(),
                "radar_snapshot_id": snapshot_id,
                "plan": plan,
                "subscribed": subscribed,
                "retired": retired,
                "prepare_failures": failures,
                "subscription_error": subscription_error,
                "retire_errors": retire_errors,
                "snapshot": self.dynamic_universe.snapshot(),
                "version": DYNAMIC_UNIVERSE_VERSION,
            }
            self._log("dynamic_universe_reconciliation", result)
            return result

    @staticmethod
    def _context_now(context: Any) -> datetime:
        value = getattr(context, "now", None) if context is not None else None
        if callable(value):
            try:
                value = value()
            except Exception:
                value = None
        if isinstance(value, pd.Timestamp):
            return value.to_pydatetime()
        return value if isinstance(value, datetime) else datetime.now()

    def _derive_asof(self, frames: Dict[str, pd.DataFrame], purpose: str, now: datetime) -> Optional[str]:
        all_dates: List[str] = []
        for frame in frames.values():
            if "eob" not in frame.columns or not len(frame):
                continue
            parsed = pd.to_datetime(frame["eob"], errors="coerce").dropna()
            all_dates.extend(parsed.dt.strftime("%Y-%m-%d").tolist())
        if not all_dates:
            return None
        today = now.strftime("%Y-%m-%d")
        post_close = purpose.startswith("POST_CLOSE") and now.strftime("%H:%M:%S") >= "15:00:00"
        eligible = [value for value in all_dates if value <= today] if post_close else [value for value in all_dates if value < today]
        return max(eligible) if eligible else None

    def refresh_numeric_daily_high_tags(self, frames: Dict[str, pd.DataFrame], asof: str) -> Dict[str, Any]:
        """旁路复用前复权日线，扫描最近10日高点TAG与低点支撑观察TAG。

        不额外拉取数据，也不回写frames。任何失败只标记插件UNAVAILABLE，不会阻断主策略。
        """
        cached = self.numeric_tags.snapshot()
        if cached.get("status") == "READY" and cached.get("asof") == str(asof)[:10]:
            return cached
        try:
            end_date = str(asof)[:10]
            if "ADJUST_PREV" not in self.last_daily_history_source:
                raise RuntimeError(f"日线复权口径未验证: {self.last_daily_history_source}")
            names = {
                symbol: str((self.taxonomy.get("symbols", {}).get(symbol) or {}).get("name") or "")
                for symbol in self.pool
            }
            result = self.numeric_tags.scan_recent_daily_extremes(
                frames,
                asof=end_date,
                names=names,
                source=self.last_daily_history_source,
            )
            if result.get("ready_symbol_count") != len(self.pool):
                result["coverage_warning"] = f"{result.get('ready_symbol_count', 0)}/{len(self.pool)}"
            self._log("numeric_daily_high_tags", result)
            return result
        except Exception as exc:
            result = self.numeric_tags.set_unavailable(str(asof), f"{type(exc).__name__}: {str(exc)[:220]}")
            self._log("errors", {"kind": "numeric_daily_high_tags", "error": result.get("error")})
            return result

    def refresh_candidates(
        self,
        context: Any,
        purpose: str,
        asof: Optional[str] = None,
        expected_asof: Optional[str] = None,
    ) -> Dict[str, Any]:
        frames = self._frames_from_context(context)
        decision_time = self._context_now(context)
        asof = asof or self._derive_asof(frames, purpose, decision_time)
        if not asof:
            raise RuntimeError(f"{purpose}: 无法取得严格早于决策时刻的完整日线截面")
        if expected_asof and str(asof)[:10] != str(expected_asof)[:10]:
            raise RuntimeError(
                f"{purpose}: 完整日线尚未就绪，要求{str(expected_asof)[:10]}，实际仅到{str(asof)[:10]}"
            )
        self.latest_daily = self.builder.build(frames, asof=asof)
        self.latest_daily["decision_timestamp"] = decision_time.isoformat()
        self.latest_daily["purpose"] = purpose
        self.latest_daily["daily_history_source"] = self.last_daily_history_source
        self.latest_daily["price_adjustment"] = "ADJUST_PREV/front-adjusted" if "ADJUST_PREV" in self.last_daily_history_source else "UNVERIFIED_FALLBACK"
        self.latest_daily["current_day_excluded"] = not purpose.startswith("POST_CLOSE")
        universe_known = bool(self.latest_daily.get("universe_point_in_time"))
        self.latest_daily["universe_point_in_time"] = universe_known
        self.latest_daily["overall_no_lookahead"] = bool(self.latest_daily.get("feature_no_lookahead") and universe_known)
        for row in self.latest_daily.get("candidates", []):
            row["overall_no_lookahead"] = bool(row.get("feature_no_lookahead") and row.get("universe_point_in_time"))
            row["timing_static_context"] = row.get("timing_static_context") or {"status": "UNAVAILABLE"}
        self.sector_health.set_candidates(self.latest_daily.get("candidates", []))
        numeric_scan = self.refresh_numeric_daily_high_tags(frames, asof)
        self.latest_daily["numeric_tag_plugin"] = {
            "status": numeric_scan.get("status"),
            "asof": numeric_scan.get("asof"),
            "lookback_trading_days": numeric_scan.get("lookback_trading_days"),
            "ready_symbol_count": numeric_scan.get("ready_symbol_count", 0),
            "tagged_symbol_count": numeric_scan.get("tagged_symbol_count", 0),
            "hit_count": numeric_scan.get("hit_count", 0),
            "low_support_ready_symbol_count": numeric_scan.get("daily_low_support_ready_symbol_count", 0),
            "low_support_tagged_symbol_count": numeric_scan.get("low_support_tagged_symbol_count", 0),
            "low_support_hit_count": numeric_scan.get("low_support_hit_count", 0),
            "low_support_confirm_days": numeric_scan.get("low_support_confirm_days", 3),
            "low_support_test_tolerance_pct": numeric_scan.get("low_support_test_tolerance_pct", 0.01),
            "strategy_effect": "NONE_TAG_ONLY",
        }
        self._log("daily_candidates", {"purpose": purpose, "payload": self.latest_daily})
        return self.latest_daily

    def _dynamic_core_snapshot(
        self,
        opportunity_rows: Sequence[Dict[str, Any]],
        limit: int = 6,
    ) -> Dict[str, Any]:
        """按当前时点重排合并池核心3+补充3，并记录相对上次的变化。"""

        ranked = []
        for source in opportunity_rows:
            row = dict(source)
            candidate = row.get("candidate") or {}
            continuation = row.get("continuation") or {}
            capital = row.get("capital_behavior") or {}
            mtf = row.get("multitimeframe") or {}
            market = candidate.get("market_sector") or {}
            live_score = _safe_float(row.get("strength"), _safe_float(candidate.get("candidate_rank_score")))
            # 动态核心不是买点排名。位置透支、板块轮出或资金流出会降低盯盘优先级，
            # 但不会擅自生成买卖事件。
            score = live_score
            score += 5 if continuation.get("confirmed") else 0
            score += 4 if capital.get("flow_persistence_confirmed") else 0
            score += 3 if mtf.get("trigger_confirmed") else 0
            score += 4 if market.get("entry_support") else 0
            score -= 8 if market.get("rotation_caution") else 0
            score -= 10 if capital.get("phase") == "CONFIRMED_OUTFLOW" else 0
            score -= 5 if _safe_float(row.get("current_return")) > 0.10 else 0
            row["dynamic_core_score"] = max(0, min(100, int(round(score))))
            ranked.append(row)
        ranked.sort(key=lambda row: (-row["dynamic_core_score"], -_safe_float(row.get("strength")), row.get("symbol", "")))
        selected = ranked[: max(0, int(limit))]
        current = [str(row.get("symbol")) for row in selected]
        previous = list(self.last_dynamic_core_symbols)
        changes = {
            "new": [symbol for symbol in current if symbol not in previous],
            "continued": [symbol for symbol in current if symbol in previous],
            "exited": [symbol for symbol in previous if symbol not in current],
        }
        self.last_dynamic_core_symbols = current
        return {"core": selected[:3], "supplement": selected[3:6], "changes": changes, "all": selected}

    def bootstrap(self, context: Any) -> Dict[str, Any]:
        """进程错过08:45调度时静默补齐候选，不发送重复飞书。"""
        now = self._context_now(context)
        purpose = "POST_CLOSE_BOOTSTRAP" if now.strftime("%H:%M:%S") >= "15:00:00" else "PREMARKET_BOOTSTRAP"
        try:
            result = self.refresh_candidates(context, purpose)
            self.seed_intraday_history(context)
            self.restore_intraday_numeric_highs(now=now)
            global_snapshot = self.global_market.refresh(now=now)
            self.market_permission.evaluate(
                self.market_sector_radar.latest,
                global_snapshot,
                premarket_daily=result,
            )
            self._log("global_market_snapshots", global_snapshot)
            self.refresh_market_sectors_async(force=True)
            return result
        except Exception as exc:
            self._log("errors", {"kind": "bootstrap", "error_type": type(exc).__name__, "error": str(exc)[:240]})
            return {}

    def restore_intraday_numeric_highs(
        self,
        *,
        now: Optional[datetime] = None,
        day_root: Optional[Path] = None,
    ) -> Dict[str, Any]:
        """中途重启时从当日5秒证据日志恢复session high与已命中TAG。"""

        now = now or datetime.now()
        trade_date = now.strftime("%Y-%m-%d")
        if now.strftime("%H:%M:%S") < "09:30:00":
            self.intraday_numeric_restore_status = {
                "status": "NOT_REQUIRED_PREOPEN", "trade_date": trade_date,
                "coverage": "FULL_SESSION_WILL_START_WITH_PROCESS",
            }
            return self.intraday_numeric_restore_status
        root = Path(day_root) if day_root is not None else LIVE_ROOT / trade_date
        path = root / "tick_samples.jsonl"
        if not path.exists():
            self.intraday_numeric_restore_status = {
                "status": "PARTIAL_SINCE_RESTART", "trade_date": trade_date,
                "coverage": "INTRADAY_HIGH_PARTIAL_SINCE_RESTART", "source": str(path),
                "restored_observation_count": 0,
            }
            return self.intraday_numeric_restore_status
        names = {
            symbol: str((self.taxonomy.get("symbols", {}).get(symbol) or {}).get("name") or "")
            for symbol in self.pool
        }
        observation_count = 0
        parse_errors = 0
        with path.open("r", encoding="utf-8") as handle:
            for line in handle:
                if not line.strip():
                    continue
                try:
                    record = json.loads(line)
                    observation = record.get("observation") or {}
                    symbol = str(observation.get("symbol") or record.get("symbol") or "")
                    price = _safe_float(observation.get("price"))
                    event_ts = str(observation.get("event_ts") or record.get("logged_at") or "")
                    if symbol not in names or price <= 0 or not event_ts.startswith(trade_date):
                        continue
                    self.numeric_tags.update_intraday_high(
                        symbol, names.get(symbol, ""), trade_date, event_ts, price,
                        source="RECOVERED_FROM_TICK_SAMPLES_JSONL",
                    )
                    observation_count += 1
                except Exception:
                    parse_errors += 1
        self.numeric_tags.intraday_scan["coverage"] = "FULL_FROM_FIRST_LOGGED_TICK"
        self.numeric_tags.intraday_scan["recovered_from"] = str(path)
        self.intraday_numeric_restore_status = {
            "status": "RESTORED", "trade_date": trade_date,
            "coverage": "FULL_FROM_FIRST_LOGGED_TICK", "source": str(path),
            "restored_observation_count": observation_count, "parse_errors": parse_errors,
            "restored_symbol_count": len(self.numeric_tags.intraday_scan.get("by_symbol") or {}),
            "restored_hit_count": len(self.numeric_tags.intraday_scan.get("hits") or []),
        }
        return self.intraday_numeric_restore_status

    def _candidate_map(self) -> Dict[str, Dict[str, Any]]:
        candidates = {
            row["symbol"]: row
            for row in self.latest_daily.get("candidates", []) if row.get("symbol")
        }
        candidates.update(self.dynamic_universe.candidate_map())
        # 动态池持仓即使重启时已不在热点名单，也必须保留退出分析所需的最后候选快照。
        for symbol, position in self.virtual_positions.items():
            if symbol not in candidates and isinstance(position.get("candidate_snapshot"), dict):
                candidates[symbol] = dict(position["candidate_snapshot"])
        return candidates

    @staticmethod
    def _short_code(symbol: str) -> str:
        return str(symbol or "").split(".")[-1]

    @staticmethod
    def _format_price(symbol: str, value: Any) -> str:
        number = _safe_float(value, float("nan"))
        if not math.isfinite(number):
            return "不可用"
        return f"{number:.3f}" if str(symbol).endswith("517400") else f"{number:.2f}"

    @staticmethod
    def _strength_bucket(value: Any) -> str:
        score = int(_safe_float(value))
        if score >= 80:
            return "强"
        if score >= 65:
            return "较强"
        if score >= 50:
            return "中性观察"
        return "弱"

    @staticmethod
    def _strength_bar(value: Any) -> str:
        score = max(0, min(100, int(_safe_float(value))))
        filled = min(10, score // 10)
        return "█" * filled + "░" * (10 - filled)

    @staticmethod
    def _route_cn(value: str) -> str:
        return {
            "TREND_CONTINUATION": "趋势延续",
            "TREND_PULLBACK": "趋势回踩",
            "REVERSAL_REPAIR": "低位修复（影子观察）",
            "HOLD_PROTECT": "持有保护",
            "RISK_EXIT": "组合转弱退出",
            "NO_SETUP": "尚无日线机会",
            "WAIT_CONFIRMATION": "等待日线确认",
            "CONFIRMED_DAILY_REVERSAL": "日线组合转弱",
        }.get(str(value), str(value or "尚未分类"))

    @staticmethod
    def _pattern_cn(value: str) -> str:
        return {
            "TREND_EXPANSION": "板块扩散下的趋势承接",
            "TREND_ACCEPTANCE": "强趋势开盘承接确认",
            "TREND_REACCELERATION": "强趋势承接后再加速",
            "PULLBACK_RECLAIM": "受控回撤后重新转强",
            "VIRTUAL_STOP_LOSS": "虚拟台账止损确认",
            "VIRTUAL_LOSS_PROTECTION": "持仓亏损达到保护线，先减仓",
            "T1_FAILED_CONTINUATION_PROTECTION": "次日延续失败，先降低可卖仓风险",
            "T1_FAILED_CONTINUATION_EXIT": "减仓后延续仍失败，退出剩余可卖仓",
            "STRUCTURE_FAILURE_EXIT": "盘前结构支撑失守并确认",
            "DUAL_30_60_DIVERGENCE_EXIT": "30/60分钟双重顶背离退出",
            "CONFIRMED_TREND_EXIT": "日线退出与60分钟破位共振",
            "PANIC_WEAK_POSITION_EXIT": "市场恐慌与个股流出共振退出",
            "PROFIT_PROTECTION_REVERSAL": "盈利保护转弱",
            "SECTOR_RELATIVE_WEAKNESS": "板块内相对掉队",
            "DAILY_EXIT_FAILED_HIGH": "日线高位组合转弱",
            "SAME_DAY_CONTINUATION_INVALIDATION": "当日趋势延续失效",
            "SUDDEN_TREND_BREAKOUT": "突发趋势多周期确认",
            "SUDDEN_TREND_DISCOVERY": "突发趋势发现",
            "SUDDEN_TREND_ARMED": "突发趋势已武装，等待回踩",
            "PRELIMINARY_TREND_WATCH": "预观察，等待完整5分钟确认",
            "ARMED_WAIT_PULLBACK": "趋势已武装，等待回踩",
            "PULLBACK_IN_PROGRESS": "受控回踩进行中",
            "TREND_PULLBACK_RECLAIM": "近期回踩后重新收复",
            "CAPITAL_FLOW_CONTINUATION": "持续资金流确认推进",
            "SECTOR_LEADER_ACCEPTANCE": "强板块前排承接确认",
            "CAPITAL_LED_EARLY_REVERSAL": "资金领先的大周期早期转折",
            "POSITION_RECOVERY_AFTER_REDUCE": "减仓后资金与结构重新修复",
            "PLATFORM_REACCELERATION_SHADOW": "平台蓄势后再加速（影子观察）",
            "SIGNALLED": "已有正式买点，持续跟踪",
            "SUDDEN_TREND_LIMIT_LOCKED": "突发趋势已发现但涨停封死",
            "TREND_LIMIT_LOCKED": "趋势已确认但涨停封死",
            "MULTITIMEFRAME_TREND_REVERSAL": "5/15/30分钟趋势共振转弱",
            "MACD_30_60_DIVERGENCE_RISK": "30/60分钟顶背离与资金/板块共同转弱",
            "MARKET_RISK_WEAK_POSITION": "市场风险与个股弱化共同确认",
            "CAPITAL_OUTFLOW_CONFIRMED": "持续资金流出确认",
            "TOP_VOLUME_CONTRACTION_DEFENSE": "高位退量后盘中走弱确认",
        }.get(str(value), str(value or "结构待确认"))

    @staticmethod
    def _market_rotation_cn(value: str) -> str:
        return {
            "SUSTAINED_LEADER": "持续主线",
            "ROTATION_IN": "轮入增强",
            "HEALTHY_RISING": "健康走强",
            "FLASH_HEAT": "瞬时冲高/持续性待证",
            "ROTATION_OUT": "轮出退潮",
            "WEAK": "全市场弱势",
            "NEUTRAL": "中性",
            "UNAVAILABLE": "数据不足",
        }.get(str(value), str(value or "数据不足"))

    @staticmethod
    def _entry_state_cn(value: str) -> str:
        return {
            "ENTRY_READY_CAPITAL_LED_EARLY": "早期资金转折与T+1生存门已确认",
            "ENTRY_READY_FLOW_CONTINUATION": "持续资金流入场已确认",
            "ENTRY_READY_SECTOR_LEADER_ACCEPTANCE": "强板块前排承接入场已确认",
            "ENTRY_READY_AFTER_RECLAIM": "回踩收复入场已确认",
            "ARMED_WAIT_PULLBACK": "趋势已武装，等待更好位置",
            "WAIT_RECLAIM_CONFIRMATION": "回踩中，等待承接收复",
            "WAIT_COMPLETED_5M_TRIGGER": "等待完整5分钟触发",
            "UNEXECUTABLE": "当前不可成交",
            "PLATFORM_REACCELERATION_SHADOW": "平台再加速已捕捉，尚未成为正式新仓",
        }.get(str(value), str(value or "未分级"))

    @staticmethod
    def _multitimeframe_line(context: Dict[str, Any]) -> str:
        periods = context.get("periods") or {}
        labels = {"BULLISH": "多", "RECOVERING": "修复", "MIXED": "混合", "BEARISH": "弱", "UNAVAILABLE": "不足"}
        parts = []
        for key in ("5", "15", "30", "60", "120"):
            row = periods.get(key) or {}
            if not row:
                parts.append(f"{key}分钟:预热")
                continue
            parts.append(
                f"{key}分钟:{labels.get(str(row.get('state')), row.get('state'))}"
                f"/KDJ-J{_safe_float(row.get('j')):.1f}/MACD{'↑' if row.get('macd_improving') else '→'}"
            )
        alignment = {
            "FULL_BULLISH": "三周期共振",
            "BULLISH_2_OF_3": "两周期支持",
            "BEARISH_2_OF_3": "两周期转弱",
            "MIXED": "周期分歧",
            "WARMING_UP_TODAY": "等待首根完整5分钟线",
            "NO_TICK_CONTEXT": "等待实时行情",
        }.get(str(context.get("alignment")), str(context.get("alignment") or "等待实时行情"))
        divergence = str(context.get("divergence_30_60") or "NONE")
        return (
            "｜".join(parts)
            + f"｜{alignment} {int(_safe_float(context.get('score')))}/100"
            + f"｜30/60背离:{divergence}（60/120m暂零权重）"
        )

    @staticmethod
    def _capital_behavior_line(context: Dict[str, Any]) -> str:
        if not context or context.get("status") not in {"READY", "WARMING_UP"}:
            return "日内资金窗口尚未形成；大周期资金结构只作先验，不单独产生动作"
        structure = context.get("structure") or {}
        phase = context.get("phase_cn") or context.get("phase") or "待判断"
        regime = context.get("regime_cn") or context.get("regime") or "证据尚不稳定"
        structure_phase = structure.get("phase_cn") or structure.get("phase") or "未知"
        confidence = {"HIGH": "高", "MEDIUM": "中", "LOW": "低"}.get(
            str(context.get("confidence")), str(context.get("confidence") or "低")
        )
        return (
            f"日内{regime}（当前{phase}）{int(_safe_float(context.get('score')))}分（置信{confidence}）｜"
            f"大周期{structure_phase} {int(_safe_float(structure.get('score'), 50))}分｜"
            f"解释：{context.get('intent_hypothesis', '证据暂不一致')}"
        )

    @staticmethod
    def _limit_behavior_line(context: Dict[str, Any]) -> str:
        if not context or context.get("status") != "READY" or context.get("classification") == "NOT_NEAR_LIMIT":
            return "未进入涨停行为观察区"
        label = {
            "APPROACHING_LIMIT": "接近涨停",
            "FIRST_TOUCH_LIMIT": "首次触板附近",
            "THIN_SEAL": "薄封观察",
            "SEALED": "封板",
            "OPEN_BOARD": "开板",
            "SELLING_ABSORBED": "开板抛压暂被吸收",
            "RESEAL_STRENGTHENING": "回封增强",
            "REPEATED_WEAK_RESEAL": "反复弱回封",
            "DISTRIBUTION_WARNING": "疑似派发预警",
        }.get(str(context.get("classification")), str(context.get("classification")))
        return (
            f"{label}｜开板{int(_safe_float(context.get('open_count')))}次/回封"
            f"{int(_safe_float(context.get('reseal_count')))}次｜旁路权重0；无完整逐笔委托时不确认派发"
        )

    @staticmethod
    def _continuation_status_cn(value: str) -> str:
        return {
            "OBSERVING": "观察开盘承接",
            "TESTING_ACCEPTANCE": "承接条件尚未齐备",
            "ACCEPTED": "承接确认",
            "REACCELERATING": "承接后再加速",
            "FAILED_ACCEPTANCE": "承接失败",
            "DEGRADED": "延续性降级",
            "LIMIT_LOCKED": "接近涨停，成交性待确认",
            "DATA_CONFLICT": "竞价数据冲突",
            "NOT_APPLICABLE": "非趋势延续路线",
            "NO_LIVE_CONTEXT": "等待盘中行情",
        }.get(str(value), str(value or "等待判断"))

    @staticmethod
    def _divergence_cn(value: str) -> str:
        return {
            "BULLISH": "底背离",
            "BEARISH": "顶背离",
            "NONE": "未确认背离",
            "UNAVAILABLE": "数据不足",
        }.get(str(value), str(value or "未确认背离"))

    @staticmethod
    def _auction_label_cn(value: str) -> str:
        return {
            "GROUP_CONFIRMED_HIGH_GAP": "同板块共振高开",
            "HIGH_GAP_NEEDS_CONFIRMATION": "高开，等待承接",
            "WEAK_GAP_NEEDS_REPAIR": "低开，等待修复",
            "FAKE_STRENGTH": "竞价冲高回落，谨慎确认",
            "SELL_PRESSURE": "竞价卖压偏强，谨慎确认",
            "STABLE_SUPPORT": "价格与盘口稳定",
            "DATA_CONFLICT": "多源数据冲突",
            "NEUTRAL": "竞价方向中性",
            "NO_AUCTION_EVIDENCE": "竞价证据不足",
        }.get(str(value), str(value or "竞价证据不足"))

    @staticmethod
    def _sector_state_cn(value: str) -> str:
        return {
            "IGNITION": "点火",
            "EXPANSION": "扩散强势",
            "HEALTHY_TREND": "健康趋势",
            "CONCENTRATED": "集中/脆弱",
            "DIVERGING": "分歧",
            "DECAY": "退潮",
            "NEUTRAL": "中性",
            "UNAVAILABLE": "样本不足",
        }.get(str(value), str(value or "未知"))

    @staticmethod
    def _role_cn(value: str) -> str:
        return {
            "LEADER": "前排龙头",
            "FRONT": "前排",
            "CORE": "中军",
            "FOLLOWER": "跟随",
            "LAGGARD": "掉队",
            "UNCLASSIFIED": "待分类",
        }.get(str(value), str(value or "待分类"))

    @staticmethod
    def _phase_cn(value: str) -> str:
        return {
            "WAIT_IMPULSE": "等待有效转强",
            "IMPULSE": "已出现转强，等待承接",
            "PULLBACK": "回踩中，等待恢复",
            "SIGNALLED": "事件已触发",
        }.get(str(value), str(value or "等待行情"))

    @staticmethod
    def _live_strength(candidate: Dict[str, Any], sector: Dict[str, Any], auction: Dict[str, Any]) -> int:
        score = int(_safe_float(candidate.get("signal_strength")))
        # 量能永远不能创建盘中资格；仅当日线已经给出资格，且显式切换到
        # ELIGIBLE_STRENGTH 模式时，才进入实时强度。默认 RANKING 不进入。
        if (
            candidate.get("volume_soft_factor_mode") == "ELIGIBLE_STRENGTH"
            and bool(candidate.get("intraday_eligible"))
            and candidate.get("action") != "EXIT"
        ):
            score += int(_safe_float(candidate.get("volume_soft_raw_bonus")))
        score -= int(_safe_float(candidate.get("volume_defense_penalty")))
        # 合并池内部梯队只作旁证，不能在全市场垃圾板块里“矮子拔高个”。
        score += {
            "EXPANSION": 4,
            "IGNITION": 3,
            "HEALTHY_TREND": 3,
            "DIVERGING": 0,
            "CONCENTRATED": -3,
            "DECAY": -7,
        }.get(str(sector.get("state")), 0)
        score += {"LEADER": 2, "CORE": 2, "FRONT": 1, "LAGGARD": -4}.get(str(sector.get("role")), 0)
        score += {"SUPPORT": 5, "CAUTION": -3, "HARD_VETO": -30}.get(str(auction.get("gate")), 0)
        market_sector = candidate.get("market_sector") or {}
        percentile = _safe_float(market_sector.get("health_percentile"), float("nan"))
        rotation_state = str(market_sector.get("rotation_state") or "UNAVAILABLE")
        if math.isfinite(percentile):
            score += 12 if percentile >= 0.90 else (7 if percentile >= 0.75 else (-8 if percentile <= 0.25 else 0))
            score += {
                "SUSTAINED_LEADER": 6, "ROTATION_IN": 3, "HEALTHY_RISING": 3,
                "FLASH_HEAT": -5, "ROTATION_OUT": -10, "WEAK": -8,
            }.get(rotation_state, 0)
            if _safe_float(market_sector.get("breadth"), 0.5) < 0.40:
                score -= 4
            if _safe_float(market_sector.get("inflow_ratio")) > 0:
                score += 2
        else:
            score -= 3
        return max(0, min(100, score))

    @staticmethod
    def _price_limit_ratio(symbol: str) -> float:
        code = str(symbol).split(".")[-1]
        return 0.20 if code.startswith(("300", "301", "688")) else 0.10

    @classmethod
    def _sudden_trend_context(
        cls,
        candidate: Dict[str, Any],
        observation: Dict[str, Any],
        multitimeframe: Dict[str, Any],
        sector: Dict[str, Any],
        market_sector: Dict[str, Any],
        auction: Dict[str, Any],
    ) -> Dict[str, Any]:
        """识别D-1未入选但盘中突然形成的趋势；发现与可成交严格分级。"""
        symbol = str(observation.get("symbol") or candidate.get("symbol") or "")
        reference_close = _safe_float(candidate.get("close"), _safe_float(candidate.get("pre_close")))
        price = _safe_float(observation.get("price"))
        intraday_return = price / reference_close - 1.0 if reference_close > 0 and price > 0 else 0.0
        vwap = _safe_float(observation.get("vwap"))
        vwap_gap = price / vwap - 1.0 if vwap > 0 else None
        atr = max(_safe_float(candidate.get("atr14_pct"), 0.04), 0.015)
        breakout_threshold = min(max(0.75 * atr, 0.04), 0.07)
        # 全精选池持续扫描的早期升级阈值。它只创建“已发现/已武装”状态，
        # 正式新仓仍必须完成后续回踩—收复，因此降低发现阈值不会直接放大追高交易。
        early_threshold = min(max(0.30 * atr, 0.015), 0.030)
        limit_ratio = cls._price_limit_ratio(symbol)
        near_limit = intraday_return >= limit_ratio - 0.006
        ask1 = _safe_float(observation.get("ask1_price"))
        bid1 = _safe_float(observation.get("bid1_price"))
        limit_locked = bool(near_limit and ask1 <= 0 and bid1 >= price * 0.999)
        quotes_available = bool(observation.get("quotes"))
        executable = bool(not limit_locked and (ask1 > 0 or not quotes_available))
        imbalance = observation.get("amount_imbalance")
        order_support = imbalance is None or _safe_float(imbalance) >= -0.05
        above_vwap = vwap <= 0 or price >= vwap * 0.998
        sector_support = str(sector.get("state")) in {"IGNITION", "EXPANSION", "HEALTHY_TREND"}
        market_board_support = bool(market_sector.get("entry_support"))
        market_rotation_caution = bool(market_sector.get("rotation_caution"))
        volume_entry_blocked = bool((candidate.get("volume_soft_factor") or {}).get("blocks_new_entry"))
        needs_upgrade = not bool(candidate.get("intraday_eligible")) and candidate.get("action") not in {"BUY", "EXIT"}
        discovered = bool(
            needs_upgrade
            and intraday_return >= early_threshold
            and multitimeframe.get("sudden_trend_confirmed")
            and above_vwap
            and order_support
            and not auction.get("hard_veto")
        )
        mtf_score = int(_safe_float(multitimeframe.get("score")))
        score = int(round(
            0.55 * mtf_score
            + min(20.0, max(0.0, intraday_return / max(breakout_threshold, 0.001) * 12.0))
            + (13 if sector_support else (7 if market_board_support else 0))
            + (10 if imbalance is not None and _safe_float(imbalance) >= 0.15 else 5 if order_support else 0)
            + (5 if above_vwap else 0)
        ))
        score = max(0, min(100, score))
        not_far_from_vwap = vwap <= 0 or price <= vwap * (1.0 + min(max(0.45 * atr, 0.018), 0.04))
        formal_t1 = bool(
            discovered and intraday_return >= breakout_threshold and executable and not_far_from_vwap
            and (sector_support or market_board_support)
            and not market_rotation_caution and not volume_entry_blocked
            and multitimeframe.get("alignment") in {"FULL_BULLISH", "BULLISH_2_OF_3"}
            and score >= 82
        )
        reasons = []
        if intraday_return >= breakout_threshold:
            reasons.append(f"涨幅{intraday_return:+.2%}达到突发趋势阈值{breakout_threshold:.2%}")
        elif intraday_return >= early_threshold:
            reasons.append(f"涨幅{intraday_return:+.2%}达到早期升级阈值{early_threshold:.2%}，先武装等待回踩")
        if multitimeframe.get("sudden_trend_confirmed"):
            reasons.append("5分钟触发且15/30分钟至少一档支持")
        if sector_support or market_board_support:
            reasons.append("板块/全市场方向支持")
        else:
            reasons.append("暂缺板块共振，仅保留个股发现层")
        if limit_locked:
            reasons.append("卖一为空且封在涨停附近，发现不等于可成交")
        return {
            "discovered": discovered,
            "formal_t1_entry": formal_t1,
            "executable": executable,
            "limit_locked": limit_locked,
            "near_limit": near_limit,
            "score": score,
            "entry_quality": score if executable else 0,
            "reference_close": reference_close or None,
            "intraday_return": intraday_return,
            "move_threshold": breakout_threshold,
            "early_discovery_threshold": early_threshold,
            "vwap_gap": vwap_gap,
            "sector_support": sector_support,
            "market_board_support": market_board_support,
            "market_rotation_caution": market_rotation_caution,
            "volume_entry_blocked": volume_entry_blocked,
            "needs_daily_upgrade": needs_upgrade,
            "reasons": reasons,
            "no_lookahead": True,
        }

    def _apply_auction_group_context(self, analysis: Dict[str, Any]) -> Dict[str, Any]:
        """用同一稳定主题的竞价截面区分板块共振高开与孤立高开。"""
        rows = analysis.get("rows", [])
        by_symbol = {str(row.get("symbol")): row for row in rows}
        theme_rows: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
        taxonomy_symbols = self.taxonomy.get("symbols", {})
        for symbol, row in by_symbol.items():
            if row.get("source_freshness") == "STALE" or row.get("final_gap") is None:
                continue
            for theme in taxonomy_symbols.get(symbol, {}).get("stable_themes") or []:
                theme_rows[str(theme)].append(row)
        group_facts = {}
        for theme, members in theme_rows.items():
            if len(members) < 2:
                continue
            gaps = [_safe_float(row.get("final_gap")) for row in members]
            positive = sum(value > 0 for value in gaps)
            high = sum(value > 0.03 for value in gaps)
            median_gap = float(np.median(gaps))
            confirmed = bool(
                (len(members) >= 3 and positive / len(members) >= 0.67 and median_gap >= 0.01)
                or (high >= 2 and median_gap > 0)
            )
            group_facts[theme] = {
                "theme": theme,
                "member_count": len(members),
                "positive_count": positive,
                "high_gap_count": high,
                "median_gap": median_gap,
                "group_confirmed": confirmed,
                "source": "SELECTED_POOL_AUCTION_CROSS_SECTION",
            }
        for symbol, row in by_symbol.items():
            themes = taxonomy_symbols.get(symbol, {}).get("stable_themes") or []
            contexts = [group_facts[str(theme)] for theme in themes if str(theme) in group_facts]
            if not contexts:
                continue
            contexts.sort(key=lambda item: (not item["group_confirmed"], -item["member_count"], -item["median_gap"]))
            group = contexts[0]
            row["group_context"] = group
            if row.get("label") == "HIGH_GAP_NEEDS_CONFIRMATION" and group["group_confirmed"]:
                row["gate"] = "SUPPORT"
                row["label"] = "GROUP_CONFIRMED_HIGH_GAP"
                row["hard_veto"] = False
                row["allows_intraday_validation"] = True
                row.setdefault("reasons", []).append(
                    f"{group['theme']}同组{group['positive_count']}/{group['member_count']}上涨，中位高开{group['median_gap']:.2%}"
                )
        analysis["auction_group_facts"] = group_facts
        analysis["by_symbol"] = by_symbol
        self.auction_analyzer.latest_analysis = analysis
        return analysis

    @staticmethod
    def next_fixed_summary(now: datetime) -> Dict[str, str]:
        current_time = now.strftime("%H:%M:%S")
        for slot, label in FIXED_FEISHU_SLOTS:
            if slot > current_time:
                return {"when": f"今天 {slot}", "label": label, "slot": slot}
        return {"when": "下一交易日 08:45:00", "label": "盘前总结", "slot": "08:45:00"}

    def startup_self_check(
        self,
        context: Any,
        tick_subscription_ok: bool,
        registered_schedule_count: int,
    ) -> Dict[str, Any]:
        """启动后执行机械自检，并在正式模式立即发送飞书启动回执。"""
        now = self._context_now(context)
        candidates = self.latest_daily.get("candidates", [])
        action_counts = {
            action: sum(row.get("action") == action for row in candidates)
            for action in ("BUY", "WATCH", "EXIT", "WAIT")
        }
        taxonomy_symbols = self.taxonomy.get("symbols", {})
        minute_rows = self.multitimeframe_seed_status.get("rows") or []
        minute_expected = str(self.latest_daily.get("asof") or "")[:10]
        minute_fresh = bool(
            len(minute_rows) == len(self.pool)
            and minute_expected
            and all(str(row.get("seed_last_eob") or "")[:10] >= minute_expected for row in minute_rows)
        )
        numeric_snapshot = self.numeric_tags.snapshot()
        delivery_health = self.notifier.delivery_snapshot()
        checks = {
            "tick_subscription_submitted": bool(tick_subscription_ok),
            "schedules_registered": registered_schedule_count == 11,
            "pool_loaded": bool(self.pool) and len(self.pool) == len(self.pool_membership),
            "pool_membership_complete": all(symbol in self.pool_membership for symbol in self.pool),
            "pool_group_counts_valid": bool(
                sum(not row.get("is_research_pool") for row in self.pool_membership.values()) == 29
                and sum(bool(row.get("is_research_pool")) for row in self.pool_membership.values()) == 4
            ),
            "taxonomy_complete": all(symbol in taxonomy_symbols for symbol in self.pool),
            "daily_snapshot_ready": bool(self.latest_daily.get("asof")),
            "daily_coverage_complete": self.latest_daily.get("available_size") == len(self.pool),
            "daily_adjustment_verified": self.latest_daily.get("price_adjustment") == "ADJUST_PREV/front-adjusted",
            "feature_time_boundary_ok": bool(self.latest_daily.get("feature_no_lookahead")),
            "minute_history_seed_ready": self.multitimeframe_seed_status.get("ready_count") == len(self.pool),
            "minute_history_fresh_to_daily_asof": minute_fresh,
            "numeric_tag_plugin_loaded": isinstance(self.numeric_tags, NumericPatternTagPlugin),
            "numeric_front_adjusted_daily_high_ready": bool(
                numeric_snapshot.get("status") == "READY"
                and numeric_snapshot.get("price_adjustment") == "ADJUST_PREV/front-adjusted"
            ),
            "numeric_front_adjusted_daily_low_support_ready": bool(
                numeric_snapshot.get("status") == "READY"
                and numeric_snapshot.get("price_adjustment") == "ADJUST_PREV/front-adjusted"
                and numeric_snapshot.get("daily_low_support_ready_symbol_count") == len(self.pool)
            ),
            "numeric_intraday_high_recovery_ready": bool(
                now.strftime("%H:%M:%S") < "09:30:00"
                or self.intraday_numeric_restore_status.get("status") == "RESTORED"
            ),
            "volume_soft_factor_ready": bool(candidates) and all(
                isinstance(row.get("volume_soft_factor"), dict)
                and (row.get("volume_soft_factor") or {}).get("status") != "UNAVAILABLE"
                for row in candidates
            ),
            "capital_structure_ready": bool(candidates) and all(
                isinstance(row.get("capital_structure"), dict)
                and (row.get("capital_structure") or {}).get("status") == "READY"
                for row in candidates
            ),
            "capital_intraday_engine_loaded": isinstance(self.capital_behavior, CapitalBehaviorEngine),
            "limit_behavior_shadow_loaded": isinstance(self.limit_behavior, LimitBehaviorEngine),
            "structured_timing_engine_loaded": isinstance(self.structured_timing, StructuredTimingEngine),
            "structured_timing_daily_context_ready": bool(candidates) and all(
                (row.get("timing_static_context") or {}).get("status") == "READY"
                and (row.get("timing_static_context") or {}).get("no_lookahead")
                for row in candidates
            ),
            "structured_timing_volume_profile_ready": (
                self.structured_timing_seed_status.get("ready_count") == len(self.pool)
            ),
            "full_market_sector_refresh_submitted": bool(
                self.market_sector_refresh_thread is not None
                or self.market_sector_radar.latest.get("status") != "UNINITIALIZED"
            ),
            "dynamic_universe_manager_loaded": isinstance(self.dynamic_universe, DynamicUniverseManager),
            "dynamic_subscription_callbacks_ready": bool(
                self.dynamic_subscribe_callback is not None
                and self.dynamic_unsubscribe_callback is not None
            ),
            "data_root_writable": bool(LIVE_ROOT.parent.exists() and os.access(LIVE_ROOT.parent, os.W_OK)),
            "feishu_ready": bool(self.notifier.dry_run or self.notifier.webhook_url),
            "feishu_delivery_worker_ready": bool(delivery_health.get("worker_alive")),
            "deepseek_key_available": bool(self.advisor._read_key()),
        }
        critical_names = (
            "tick_subscription_submitted", "schedules_registered", "pool_loaded", "pool_membership_complete",
            "pool_group_counts_valid", "taxonomy_complete", "full_market_sector_refresh_submitted",
            "dynamic_universe_manager_loaded", "dynamic_subscription_callbacks_ready",
            "daily_snapshot_ready", "daily_coverage_complete", "daily_adjustment_verified",
            "feature_time_boundary_ok", "minute_history_seed_ready", "minute_history_fresh_to_daily_asof",
            "volume_soft_factor_ready", "data_root_writable", "feishu_ready", "feishu_delivery_worker_ready",
        )
        critical_ok = all(checks[name] for name in critical_names)
        warnings = [name for name, passed in checks.items() if not passed and name not in critical_names]
        failures = [name for name in critical_names if not checks[name]]
        status = "SUCCESS" if critical_ok and not warnings else ("DEGRADED" if critical_ok else "FAILED")
        next_summary = self.next_fixed_summary(now)
        headline = "启动自检成功" if status == "SUCCESS" else ("启动完成但有告警" if status == "DEGRADED" else "启动自检失败")
        top_candidates = [row for row in candidates if row.get("action") != "WAIT"]
        top_candidates.sort(key=lambda row: -int(_safe_float(row.get("candidate_rank_score", row.get("signal_strength")))))
        top_lines = [
            f"- {row.get('name') or self._short_code(row.get('symbol'))}（{self._short_code(row.get('symbol'))}）"
            f"｜{row.get('pool_group_cn', '原始精选池')}"
            f"｜{self._route_cn(row.get('daily_route'))} {int(_safe_float(row.get('candidate_rank_score', row.get('signal_strength'))))}/100"
            f"｜{candidate_action_line(row, row.get('symbol') in self.virtual_positions)}"
            for row in top_candidates[:6]
        ]
        failed_or_warned = failures + warnings
        text = "\n".join([
            f"【A股轮动｜{headline}】",
            f"启动时间：{now.strftime('%Y-%m-%d %H:%M:%S')}；模式：{'测试，不外发' if self.notifier.dry_run else '正式飞书'}",
            f"日线截止：{self.latest_daily.get('asof')}；覆盖：{self.latest_daily.get('available_size', 0)}/{len(self.pool)}；来源：{self.latest_daily.get('daily_history_source')}",
            f"信号概况：BUY {action_counts['BUY']}｜WATCH {action_counts['WATCH']}｜EXIT {action_counts['EXIT']}｜WAIT {action_counts['WAIT']}",
            f"自检：{sum(checks.values())}/{len(checks)}通过"
            + (f"｜需注意：{'、'.join(failed_or_warned)}" if failed_or_warned else "｜全部正常"),
            f"监控池：原始精选池{sum(not row.get('is_research_pool') for row in self.pool_membership.values())}只"
            f"｜自研池{sum(bool(row.get('is_research_pool')) for row in self.pool_membership.values())}只｜合计{len(self.pool)}只。",
            f"实时边界：固定池{len(self.pool)}只全订阅；全市场动态深检容量{self.dynamic_universe.max_active}只，"
            "只在D-1日线/分钟种子自检通过后动态订阅；信号事件驱动，不按固定时刻买卖。",
            f"V16结构择时：{STRUCTURED_TIMING_VERSION}｜默认零权重影子验证；60分钟只作结构/背离旁证，不擅自改变正式动作。",
            "当前优先观察（最多6只；前3核心、后3补充，均不是立即买入）：",
            *(top_lines or ["- 当前没有非WAIT候选；全池仍持续扫描盘中升级。"]),
            f"下一条固定飞书：{next_summary['when']}｜{next_summary['label']}",
            "运行边界：只发观察信号，不下单；实际买入后严格遵守A股T+1。",
        ])
        event_id = f"startup:{now.strftime('%Y-%m-%dT%H:%M:%S')}"
        card = build_report_card(
            f"🚦 A股轮动｜{headline}",
            template="green" if status == "SUCCESS" else ("orange" if status == "DEGRADED" else "red"),
            fields=[
                ("启动时间", now.strftime("%m-%d %H:%M:%S")),
                ("运行模式", "正式飞书" if not self.notifier.dry_run else "测试不外发"),
                ("数据覆盖", f"{self.latest_daily.get('available_size', 0)}/{len(self.pool)}"),
                ("自检结果", f"{sum(checks.values())}/{len(checks)}通过"),
                ("盘前BUY/WATCH", f"{action_counts['BUY']} / {action_counts['WATCH']}"),
                ("下一固定消息", f"{next_summary['when']} {next_summary['label']}"),
            ],
            sections=[
                ("✅ 自检结论", (
                    f"{sum(checks.values())}/{len(checks)}项通过。"
                    + (f"需注意：{'、'.join(failed_or_warned)}。" if failed_or_warned else "关键链路均正常。")
                    + f" 固定池{len(self.pool)}只已提交实时订阅；全市场动态深检容量{self.dynamic_universe.max_active}只。"
                )),
                ("🎯 核心观察（最多3只）", "\n".join(top_lines[:3]) if top_lines else "- 当前没有非WAIT候选，不凑数。"),
                ("👀 补充观察（最多3只）", "\n".join(top_lines[3:6]) if len(top_lines) > 3 else "- 暂无补充标的。"),
                ("⏰ 下一条固定消息", f"{next_summary['when']}｜{next_summary['label']}；期间买点、卖点和失效事件仍实时推送。"),
                ("🧱 运行边界", "候选不等于买点；系统只发信号、不下单；实际买入后严格遵守A股T+1。"),
                ("🧭 V16影子择时", "动态Path、Location、Room、15分钟Setup、逐笔Execution和30/60分钟背离已加载；当前零权重，只记录与解释，待历史/前向验收。"),
            ],
            footer="本卡抵达即表示飞书投递正常｜全池继续事件驱动监控｜不下单｜A股T+1",
        )
        feishu = self.notifier.send_card(
            card, event_id, fallback_text=text, priority=2, wait_for_delivery=True,
        )
        delivery_ok = bool(feishu.get("delivered") or (self.notifier.dry_run and feishu.get("ok")))
        checks["feishu_delivery_ok"] = delivery_ok
        if not delivery_ok:
            failures.append("feishu_delivery_ok")
            status = "FAILED"
        result = {
            "status": status,
            "checks": checks,
            "failures": failures,
            "warnings": warnings,
            "next_summary": next_summary,
            "action_counts": action_counts,
            "feishu_delivery": delivery_health,
            "feishu": feishu,
        }
        self._log("startup_health", result)
        return result

    def _compact_daily_facts(self) -> Dict[str, Any]:
        rows = []
        for row in self.latest_daily.get("candidates", []):
            if row.get("action") == "WAIT":
                continue
            rows.append({key: row.get(key) for key in (
                "symbol", "name", "action", "status", "lane", "reason", "slow_j", "slow_j_zone",
                "slow_confirmed", "fast_trigger", "macd_divergence", "monthly_state", "group_level",
                "group_key", "group_source", "sector_confidence", "daily_route", "signal_strength",
                "intraday_eligible", "protection_level",
                "daily_primary_kdj", "monthly_slow_j", "monthly_fast_j", "monthly_dual_alignment",
                "candidate_rank_score", "volume_soft_factor_mode", "volume_soft_raw_bonus",
                "volume_soft_rank_bonus", "volume_defense_penalty", "volume_entry_blocked",
                "volume_soft_factor", "capital_structure", "pool_group", "pool_group_cn", "pool_tags",
                "capital_rank_adjustment",
            )})
        return {
            "asof": self.latest_daily.get("asof"),
            "decision_timestamp": self.latest_daily.get("decision_timestamp"),
            "pool_breadth": self.latest_daily.get("pool_breadth"),
            "pool_median_return_5d": self.latest_daily.get("pool_median_return_5d"),
            "rules_version": self.latest_daily.get("rules_version"),
            "signals": rows,
        }

    def _candidate_lines(self, limit: int = 10) -> List[str]:
        rows = [row for row in self.latest_daily.get("candidates", []) if row.get("action") != "WAIT"]
        rows.sort(key=lambda row: (-int(_safe_float(row.get("candidate_rank_score", row.get("signal_strength")))), row.get("name", "")))
        lines = []
        for row in rows[:limit]:
            lines.append(
                f"- {row.get('name','未知')}（{self._short_code(row['symbol'])}）｜{row.get('pool_group_cn', '原始精选池')}｜"
                f"{self._strength_bar(row.get('signal_strength'))} {int(_safe_float(row.get('signal_strength')))}/100"
                f"（{self._strength_bucket(row.get('signal_strength'))}）｜{row.get('group_key') or row.get('niche','未分类')}\n"
                f"  路线：{self._route_cn(row.get('daily_route') or row.get('lane'))}｜"
                f"日线9,20,2 J={_safe_float(row.get('slow_j')):.1f}｜"
                f"月线9,20,2 J={_safe_float(row.get('monthly_slow_j')):.1f} / 8,2,2 J={_safe_float(row.get('monthly_fast_j')):.1f}｜{row.get('reason')}\n"
                f"  大周期资金：{(row.get('capital_structure') or {}).get('phase_cn','未知')} "
                f"{int(_safe_float((row.get('capital_structure') or {}).get('score'), 50))}/100｜"
                f"{(row.get('capital_structure') or {}).get('intent_hypothesis','等待证据')}\n"
                f"  盘前均线计划：{(row.get('moving_average_prior') or {}).get('route_cn','均线计划不可用')}｜"
                f"低权重{int(_safe_float(row.get('ma_prior_rank_adjustment'))):+d}｜"
                f"{(row.get('moving_average_prior') or {}).get('reason','仅等待盘中实时确认')}\n"
                f"  {format_price_battle_plan(row.get('price_battle_plan') or {})}\n"
                f"  {candidate_action_line(row, row.get('symbol') in self.virtual_positions)}\n"
                f"  {format_volume_factor_line(row)}\n"
                f"  {candidate_tag_line(self.numeric_tags.context_for(row.get('symbol')))}"
            )
        if len(rows) > limit:
            lines.append(f"- 另有{len(rows) - limit}只低优先级观察标的，详见D盘证据日志。")
        return lines or ["当前没有达到盘中观察强度的候选。"]

    def premarket_summary(self, context: Any) -> Dict[str, Any]:
        daily = self.refresh_candidates(context, "PREMARKET_D_MINUS_1")
        now = self._context_now(context)
        global_snapshot = self.global_market.refresh(now=now)
        permission = self.market_permission.evaluate(
            self.market_sector_radar.latest,
            global_snapshot,
            premarket_daily=daily,
        )
        self._log("global_market_snapshots", global_snapshot)
        self._log("market_permission_snapshots", {"stage": "premarket", **permission})
        facts = {
            "stage": "premarket",
            "daily": self._compact_daily_facts(),
            "global_market": global_snapshot,
            "market_permission": permission,
            "rule": "D-1 only; MA is low-weight prior; intraday facts can override",
        }
        advice = self.advisor.summarize(facts)
        text = "\n".join([
            "【A股轮动｜盘前行动计划】",
            f"数据截止：{daily.get('asof')}；精选池：{len(self.pool)}只；可用日线：{daily.get('available_size')}",
            f"池宽度：{daily.get('pool_breadth', 0):.1%}；5日中位数：{daily.get('pool_median_return_5d', 0):.2%}",
            f"市场许可：{permission.get('state_cn')}｜新仓{permission.get('new_entry_permission')}｜"
            f"持仓：{permission.get('position_action_cn')}",
            f"外围参考：{self.global_market.compact_line(global_snapshot)}",
            "盘前价格作战图（D-1前复权；价位到达后仍须实时确认）：",
            *self._candidate_lines(),
            "使用：A=回踩收复优先，B=突破接受次之，C=观察不追；阻力位是减速/保护点，不是无条件卖点。均线只提供[-4,+4]排序修正，开盘后由市场许可、板块、资金行为、30/60分钟结构和成交性接管。",
            "持仓原则：市场差先关闭新仓；只减板块退潮、资金流出且个股结构破坏的可卖弱仓，不对独立强势仓机械集体砍仓。",
            "T+1边界：今日新买部分即使盘中转弱也不能卖，只能停止加仓并列入次日优先处理。",
            f"DeepSeek辅助：{advice.get('content') if advice.get('ok') else '不可用，保留机械规则结果。'}",
        ])
        event_id = f"premarket:{daily.get('asof')}"
        card = build_text_summary_card(
            text,
            template="blue",
            fields=[
                ("数据截止", daily.get("asof")),
                ("精选池", f"{len(self.pool)}只"),
                ("日线覆盖", f"{daily.get('available_size', 0)}/{len(self.pool)}"),
                ("池宽度", f"{daily.get('pool_breadth', 0):.1%}"),
                ("市场许可", f"{permission.get('state_cn')} / {permission.get('new_entry_permission')}"),
                ("外围温度", global_snapshot.get("state_cn", "未知")),
            ],
            footer="均线是低权重盘前先验｜盘中实时证据拥有主决策权｜外围不能单独触发买卖｜系统不下单",
        )
        result = self.notifier.send_card(card, event_id, fallback_text=text)
        self._log("summaries", {"stage": "premarket", "event_id": event_id, "advice": advice, "feishu": result})
        return result

    def auction_snapshot(self, context: Any = None, include_sina: bool = False) -> Dict[str, Any]:
        trade_date = self._context_now(context).strftime("%Y-%m-%d")
        if self.auction_trade_date != trade_date:
            self.auction_analyzer.reset()
            self.auction_trade_date = trade_date
        raw = self.auction.snapshot(include_sina=include_sina, expected_trade_date=trade_date)
        self.latest_auction = self.auction_analyzer.add_snapshot(raw)
        self.latest_auction_analysis = self._apply_auction_group_context(self.auction_analyzer.latest_analysis)
        self._log("auction_snapshots", {"snapshot": self.latest_auction, "analysis": self.latest_auction_analysis})
        return self.latest_auction

    def auction_summary(self, context: Any = None) -> Dict[str, Any]:
        self.refresh_market_sectors_async()
        if not self.latest_daily:
            self.refresh_candidates(context, "AUCTION_FALLBACK")
        if not self.latest_auction:
            self.auction_snapshot(context, include_sina=True)
        analysis = self.latest_auction_analysis or self._apply_auction_group_context(self.auction_analyzer.analyze())
        rows = analysis.get("rows", [])
        lines = [
            "【A股轮动｜集合竞价解读】",
            f"快照数：{analysis.get('snapshot_count', 0)}；新鲜报价：{analysis.get('fresh_symbol_count', 0)}/{len(self.pool)}；陈旧报价：{analysis.get('stale_symbol_count', 0)}；数据口径：竞价报价代理",
        ]
        daily_candidate_map = self._candidate_map()
        candidate_symbols = {row.get("symbol") for row in self.latest_daily.get("candidates", []) if row.get("action") in {"BUY", "WATCH", "EXIT"}}
        for row in rows:
            if row.get("symbol") not in candidate_symbols:
                continue
            gate_cn = {"SUPPORT": "支持", "CAUTION": "谨慎确认", "HARD_VETO": "数据硬否决", "NEUTRAL": "中性"}.get(row.get("gate"), row.get("gate"))
            label_cn = {
                "GROUP_CONFIRMED_HIGH_GAP": "同板块共振高开",
                "HIGH_GAP_NEEDS_CONFIRMATION": "高开，等待承接",
                "WEAK_GAP_NEEDS_REPAIR": "低开，等待修复",
                "FAKE_STRENGTH": "竞价冲高回落",
                "SELL_PRESSURE": "竞价卖压偏强",
                "STABLE_SUPPORT": "价格与盘口稳定",
                "DATA_CONFLICT": "多源数据冲突",
                "NEUTRAL": "未形成明确方向",
            }.get(row.get("label"), row.get("label"))
            group = row.get("group_context") or {}
            lines.append(
                f"- {row.get('name','未知')}（{self._short_code(row.get('symbol'))}）｜竞价 {_safe_float(row.get('final_gap')):+.2%}｜{gate_cn}：{label_cn}"
                + (f"｜{group.get('theme')} {group.get('positive_count')}/{group.get('member_count')}同步上涨" if group else "")
                + f"\n  {candidate_action_line(daily_candidate_map.get(row.get('symbol'), {}), row.get('symbol') in self.virtual_positions)}"
                + f"\n  {format_volume_factor_line(daily_candidate_map.get(row.get('symbol'), {}))}"
                + f"\n  {candidate_tag_line(self.numeric_tags.context_for(row.get('symbol')))}"
            )
        lines.append("解释口径：高开不再机械否决；同板块共振可升级为趋势观察，但仍需开盘后的VWAP、盘口和动量确认。只有多源数据冲突属于硬否决。")
        advice = self.advisor.summarize({"stage": "auction", "daily": self._compact_daily_facts(), "auction": analysis})
        lines.append(f"DeepSeek辅助：{advice.get('content') if advice.get('ok') else '不可用，保留机械规则结果。'}")
        event_id = f"auction_summary:{datetime.now().strftime('%Y-%m-%d')}"
        text = "\n".join(lines)
        card = build_text_summary_card(
            text,
            template="orange",
            fields=[
                ("竞价快照", f"{analysis.get('snapshot_count', 0)}次"),
                ("新鲜报价", f"{analysis.get('fresh_symbol_count', 0)}/{len(self.pool)}"),
                ("陈旧报价", f"{analysis.get('stale_symbol_count', 0)}只"),
                ("定位", "竞价只做盘中确认条件"),
            ],
            footer="高开不机械否决｜只有数据冲突是硬否决｜仍需VWAP、盘口和多周期确认",
        )
        result = self.notifier.send_card(card, event_id, fallback_text=text)
        self._log("summaries", {"stage": "auction", "event_id": event_id, "advice": advice, "feishu": result})
        return result

    @staticmethod
    def _tick_value(tick: Any, name: str, default: Any = None) -> Any:
        if isinstance(tick, dict):
            return tick.get(name, default)
        try:
            return getattr(tick, name)
        except AttributeError:
            try:
                return tick[name]
            except Exception:
                return default

    def on_tick(self, tick: Any) -> Optional[Dict[str, Any]]:
        symbol = str(self._tick_value(tick, "symbol", ""))
        allowed_symbols = set(self.pool) | set(self.dynamic_universe.active) | set(self.virtual_positions)
        if symbol not in allowed_symbols:
            return None
        received_at = datetime.now()
        normalized = normalize_tick(tick, received_at=received_at)
        if normalized is None:
            return None
        # 统一会话边界必须位于所有有状态引擎之前。集合竞价由AuctionProvider处理；
        # 午休和15:00后的伪Tick不能污染VWAP、累计量、分钟K、资金行为与板块状态。
        if not is_continuous_session(normalized["event_ts"]):
            return None
        # 正式运行时拒绝跨交易日迟到/缓存Tick。否则前一交易日残留消息可能在今日
        # 开盘时推进状态机；dry-run保留历史时间，便于严格回放和单元测试。
        if not self.notifier.dry_run and normalized["event_ts"].date() != received_at.date():
            self._log("rejected_tick", {
                "symbol": symbol,
                "reason": "CROSS_TRADE_DATE_TICK",
                "event_ts": normalized["event_ts"].isoformat(),
                "received_at": received_at.isoformat(),
            })
            return None
        if not self.notifier.dry_run:
            self.refresh_market_sectors_async()
        candidate = self._candidate_map().get(symbol)
        effective_candidate = dict(candidate or {})
        virtual_position = self.virtual_positions.get(symbol)
        if virtual_position:
            effective_candidate.update({
                "monitor_sell": True,
                "position_entry_date": virtual_position.get("entry_date"),
                "position_entry_price": virtual_position.get("entry_price"),
                "position_entry_pattern": virtual_position.get("entry_pattern"),
                "position_entry_route": virtual_position.get("entry_route"),
                "position_source": "SIGNAL_LEDGER_NOT_BROKER_POSITION",
            })
            if effective_candidate.get("action") != "EXIT":
                effective_candidate["action"] = "MONITOR_EXIT"
        auction_gate = self.auction_analyzer.gate_for(symbol)
        intraday_numeric_hit = self.numeric_tags.update_intraday_high(
            symbol,
            str(effective_candidate.get("name") or ""),
            normalized["event_ts"].strftime("%Y-%m-%d"),
            normalized["event_ts"].isoformat(),
            normalized["price"],
        )
        if intraday_numeric_hit:
            self._log("numeric_intraday_high_tags", intraday_numeric_hit)
        limit_behavior = self.limit_behavior.update(normalized, effective_candidate)
        effective_candidate["limit_behavior"] = limit_behavior
        if limit_behavior.get("state_changed") and limit_behavior.get("classification") != "NOT_NEAR_LIMIT":
            self._log("limit_behavior_tags", limit_behavior)
        # 多周期引擎只使用当前Tick以前已经完成的分钟K线；当前分钟不会参与判断。
        if symbol not in self.multitimeframe.states:
            path = (
                DYNAMIC_MINUTE_CACHE_ROOT / f"{_safe_name(symbol)}_1m.pkl"
                if symbol not in self.pool
                else MINUTE_LIVE_CACHE_ROOT / f"{symbol}_1m.pkl"
            )
            if not path.exists() and symbol in self.pool:
                path = MINUTE_HISTORY_ROOT / f"{symbol}_1m.pkl"
            try:
                seed_frame = pd.read_pickle(path) if path.exists() else None
                self.multitimeframe.seed(symbol, seed_frame)
                self.structured_timing.seed(symbol, seed_frame)
            except Exception as exc:
                self.multitimeframe.seed(symbol, None)
                self.structured_timing.seed(symbol, None)
                self._log("errors", {"kind": "minute_seed_on_demand", "symbol": symbol, "error": str(exc)[:180]})
        multitimeframe = self.multitimeframe.update(normalized)
        live_sector = self.sector_health.update(normalized)
        market_sector = self.market_sector_radar.context_for_candidate(effective_candidate)
        effective_candidate["market_sector"] = market_sector
        market_permission = self.market_permission.evaluate(
            self.market_sector_radar.latest,
            self.global_market.latest,
            premarket_daily=self.latest_daily,
        )
        effective_candidate["market_permission"] = market_permission
        effective_candidate["market_new_entry_allowed"] = market_permission.get("new_entry_permission") != "CLOSED"
        continuation_sector = dict(live_sector)
        if market_sector.get("board_code"):
            continuation_sector.update({
                "market_board_pct": market_sector.get("board_pct"),
                "market_board_rank": market_sector.get("board_rank"),
                "market_board_percentile": market_sector.get("health_percentile"),
                "market_board_health_score": market_sector.get("health_score_raw"),
                "market_board_state": market_sector.get("rotation_state"),
                "market_board_entry_support": market_sector.get("entry_support"),
                "market_board_rotation_caution": market_sector.get("rotation_caution"),
                "market_board_breadth": market_sector.get("breadth"),
                "market_board_persistence": market_sector.get("top_quartile_persistence"),
                "market_universe_count": market_sector.get("market_universe_count"),
                "market_median_pct": market_sector.get("market_median_pct"),
            })
        effective_candidate["continuation_sector"] = continuation_sector
        base_live_strength = self._live_strength(effective_candidate, live_sector, auction_gate)
        continuation_context = self.continuation.update(
            normalized, effective_candidate, auction=auction_gate, sector=continuation_sector,
        )
        live_strength = (
            int(_safe_float(continuation_context.get("score"), base_live_strength))
            if effective_candidate.get("daily_route") == "TREND_CONTINUATION"
            else base_live_strength
        )
        if multitimeframe.get("periods"):
            mtf_adjustment = int(round((_safe_float(multitimeframe.get("score")) - 50.0) * 0.20))
            live_strength = max(0, min(100, live_strength + mtf_adjustment))
        capital_context = self.capital_behavior.update(
            normalized,
            effective_candidate,
            sector=continuation_sector,
            multitimeframe=multitimeframe,
        )
        if capital_context.get("status") == "READY":
            capital_adjustment = max(
                -6,
                min(6, int(round((_safe_float(capital_context.get("score"), 50.0) - 50.0) * 0.12))),
            )
            live_strength = max(0, min(100, live_strength + capital_adjustment))
        effective_candidate["live_sector"] = live_sector
        effective_candidate["live_signal_strength"] = live_strength
        effective_candidate["continuation_context"] = continuation_context
        effective_candidate["multitimeframe"] = multitimeframe
        effective_candidate["capital_behavior"] = capital_context
        structured_timing = self.structured_timing.update(
            normalized,
            effective_candidate,
            multitimeframe,
            sector=continuation_sector,
            capital=capital_context,
            continuation=continuation_context,
            market_permission=market_permission,
        )
        effective_candidate["structured_timing"] = structured_timing
        if structured_timing.get("material_state_transition"):
            self._log("structured_timing_transitions", {
                "symbol": symbol,
                "name": effective_candidate.get("name"),
                "transition": structured_timing.get("material_state_transition"),
                "raw_path_transition": structured_timing.get("path_transition"),
                "context": structured_timing,
            })
        sudden_trend = self._sudden_trend_context(
            effective_candidate, normalized, multitimeframe, live_sector, market_sector, auction_gate,
        )
        effective_candidate["sudden_trend_context"] = sudden_trend
        if effective_candidate.get("daily_route") == "TREND_CONTINUATION":
            effective_candidate["intraday_eligible"] = bool(continuation_context.get("confirmed"))
        elif (
            effective_candidate.get("daily_route") == "TREND_PULLBACK"
            and live_strength >= 60
            and live_sector.get("state") in {"IGNITION", "EXPANSION", "HEALTHY_TREND"}
            and (not market_sector.get("board_code") or market_sector.get("entry_support"))
            and not market_sector.get("rotation_caution")
            and not (effective_candidate.get("volume_soft_factor") or {}).get("blocks_new_entry")
            and not auction_gate.get("hard_veto")
        ):
            effective_candidate["intraday_eligible"] = True
        if candidate:
            log_bucket = normalized["event_ts"].strftime("%Y-%m-%d %H:%M:%S")
            if int(normalized["event_ts"].strftime("%S")) % 5 == 0 and self.last_tick_log_second.get(symbol) != log_bucket:
                self.last_tick_log_second[symbol] = log_bucket
                self._log("tick_samples", {
                    "symbol": symbol,
                    "candidate_action": candidate.get("action"),
                    "candidate_status": candidate.get("status"),
                    "auction_gate": auction_gate.get("gate"),
                    "live_signal_strength": live_strength,
                    "live_sector": live_sector,
                    "continuation": continuation_context,
                    "multitimeframe": multitimeframe,
                    "capital_behavior": capital_context,
                    "structured_timing": structured_timing,
                    "sudden_trend": sudden_trend,
                    "market_sector": market_sector,
                    "observation": normalized,
                })
        event = self.intraday.on_tick(tick, effective_candidate or None, auction_gate=auction_gate, received_at=received_at)
        if event is None:
            return None
        event["name"] = effective_candidate.get("name", "")
        event["candidate"] = effective_candidate
        event["tick_received_at"] = received_at.isoformat()
        event["execution_rule"] = "信号服务不下单；如当日实际买入，A股T+1要求当天不得卖出"
        event["capital_behavior"] = capital_context
        event["limit_behavior"] = limit_behavior
        event["market_permission"] = market_permission
        event["structured_timing"] = structured_timing
        action_decision = decide_event_actions(event, effective_candidate)
        event["action_decision"] = action_decision
        numeric_tag_context = self.numeric_tags.context_for(symbol)
        intraday_numeric_context = self.numeric_tags.intraday_context_for(symbol)
        event["numeric_auxiliary_tag"] = numeric_tag_context
        event["numeric_intraday_high_tag"] = intraday_numeric_context
        action_card = format_action_card(action_decision)
        numeric_text = inline_tag_text(numeric_tag_context)
        intraday_numeric_text = intraday_inline_tag_text(intraday_numeric_context)
        numeric_tag_lines = [row for row in (numeric_text, intraday_numeric_text) if row]
        volume_factor_lines = [format_volume_factor_line(effective_candidate)] if candidate else []
        pool_badge = f"｜{effective_candidate.get('pool_group_cn', '原始精选池')}"
        market_sector_text = (
            f"｜全市场{market_sector.get('board_name')}·{self._market_rotation_cn(market_sector.get('rotation_state'))} "
            f"健康分位{_safe_float(market_sector.get('health_percentile')):.0%}｜广度{_safe_float(market_sector.get('breadth')):.0%}"
            f"｜涨停{int(_safe_float(market_sector.get('limit_up_count')))}只/最高{int(_safe_float(market_sector.get('max_board_streak')))}板"
            if market_sector.get("board_code") else ""
        )
        market_permission_text = (
            f"市场许可：{market_permission.get('state_cn','未知')}｜"
            f"新仓{market_permission.get('new_entry_permission','SELECTIVE')}｜"
            f"持仓原则：{market_permission.get('position_action_cn','等待实时证据')}"
        )
        if event["event"] == "DISCOVERY_EVENT_WATCH":
            sudden = event.get("sudden_trend") or {}
            text = "\n".join([
                "【A股轮动｜突发趋势已发现｜当前不可成交】",
                f"{event.get('name','')}（{self._short_code(symbol)}）{pool_badge}｜发现强度 {event.get('composite_signal_strength',0)}/100（不是胜率）",
                action_card,
                f"现价 {self._format_price(symbol, event.get('price'))}｜相对昨收 {_safe_float(event.get('intraday_return')):+.2%}｜"
                f"VWAP {self._format_price(symbol, event.get('vwap'))}",
                f"板块：{live_sector.get('theme','未形成同主题样本')}·{self._sector_state_cn(live_sector.get('state'))}｜"
                f"角色：{self._role_cn(live_sector.get('role'))}{market_sector_text}",
                market_permission_text,
                f"多周期：{self._multitimeframe_line(event.get('multitimeframe') or {})}",
                format_structured_timing_line(structured_timing),
                f"资金行为：{self._capital_behavior_line(event.get('capital_behavior') or capital_context)}",
                f"涨停路径：{self._limit_behavior_line(limit_behavior)}",
                f"识别依据：{'；'.join(sudden.get('reasons') or ['突发趋势条件成立'])}",
                "成交性：卖一为空/价格封在涨停附近，系统只记录“捕捉到”，绝不伪装成可以买到的买点。",
                *volume_factor_lines,
                *numeric_tag_lines,
                "后续：继续实时监控；恢复成交并完成回踩—收复后才可能升级。本消息不写入虚拟持仓、不发送订单。",
            ])
        elif event["event"] in {"BUY_EVENT_WATCH", "OPPORTUNITY_EVENT_WATCH"}:
            formal_t1_entry = event["event"] == "BUY_EVENT_WATCH"
            if formal_t1_entry:
                if symbol not in self.virtual_positions:
                    self.virtual_positions[symbol] = {
                        "entry_date": str(event.get("event_ts") or "")[:10],
                        "entry_ts": event.get("event_ts"),
                        "entry_price": event.get("price"),
                        "source_event_id": event.get("event_id"),
                        "entry_pattern": event.get("pattern"),
                        "entry_route": effective_candidate.get("daily_route"),
                        "entry_signal_strength": event.get("composite_signal_strength", live_strength),
                        "pool_group": effective_candidate.get("pool_group", "ORIGINAL_POOL"),
                        "pool_group_cn": effective_candidate.get("pool_group_cn", "原始精选池"),
                        "candidate_snapshot": {
                            key: copy.deepcopy(effective_candidate.get(key))
                            for key in (
                                "symbol", "name", "pool_group", "pool_group_cn", "pool_tags",
                                "primary_industry", "subindustry", "niche", "stable_themes",
                                "close", "pre_close", "atr14_pct", "slow_j", "fast_j",
                                "daily_route", "action", "status", "signal_strength",
                                "candidate_rank_score", "protection_level", "volume_soft_factor",
                                "price_battle_plan", "moving_average_prior", "timing_static_context",
                                "dynamic_discovery", "dynamic_formal_data_ready",
                            )
                            if effective_candidate.get(key) is not None
                        },
                        "kind": "SIGNAL_LEDGER_NOT_BROKER_POSITION",
                    }
                    event["position_ledger_operation"] = "OPENED_NEW_SIGNAL_POSITION"
                else:
                    # 已有底仓的正式买点是加仓/做T买入腿参考，不能覆盖原入场日，
                    # 否则会错误重置T+1与后续持有期统计。
                    event["position_ledger_operation"] = "KEPT_EXISTING_POSITION_T_REFERENCE"
                self._save_virtual_positions()
            continuation = event.get("continuation") or {}
            if event.get("sudden_trend"):
                continuation_line = "突发升级：D-1未入选；由当日涨幅、多周期、板块/盘口及成交性共同升级。"
            else:
                continuation_line = (
                f"延续确认：日线{_safe_float((continuation.get('daily') or {}).get('score')):.0f}｜"
                f"竞价{_safe_float((continuation.get('auction') or {}).get('score')):.0f}｜"
                f"板块{_safe_float((continuation.get('sector') or {}).get('score')):.0f}｜"
                f"承接{_safe_float(continuation.get('acceptance_score')):.0f}"
                if continuation.get("daily") else "延续确认：当前为趋势回踩路线，按板块、VWAP与回踩收复评价。"
                )
            displayed_gap_cap = (
                event.get("flow_entry_vwap_gap_cap")
                if event.get("pattern") in {"CAPITAL_FLOW_CONTINUATION", "CAPITAL_LED_EARLY_REVERSAL", "SECTOR_LEADER_ACCEPTANCE"}
                else event.get("entry_vwap_gap_cap")
            )
            t1_context = event.get("t1_survivability") or {}
            t1_line = (
                f"T+1生存性：{t1_context.get('grade','—')}级 {t1_context.get('score','—')}/100｜"
                f"阻断项：{'、'.join(t1_context.get('blockers_cn') or t1_context.get('blockers') or ['无'])}｜"
                f"证据年龄 {_safe_float((t1_context.get('features') or {}).get('signal_age_minutes')):.0f}分钟"
                if t1_context else "T+1生存性：旧路径未提供独立评估"
            )
            text = "\n".join([
                (
                    "【A股轮动｜持仓做T/加仓买点】" if formal_t1_entry and virtual_position
                    else (
                        "【A股轮动｜减仓后风险缓解｜不是回补信号】"
                        if event.get("pattern") == "POSITION_RECOVERY_AFTER_REDUCE"
                        else (
                        "【A股轮动｜T+1早期资金转折信号】"
                        if formal_t1_entry and event.get("pattern") == "CAPITAL_LED_EARLY_REVERSAL"
                        else ("【A股轮动｜T+1新开仓信号】" if formal_t1_entry else "【A股轮动｜趋势机会/持仓做T参考】")
                        )
                    )
                ),
                f"{event.get('name','')}（{self._short_code(symbol)}）{pool_badge}｜综合强度 "
                f"{event.get('composite_signal_strength', live_strength)}/100｜位置质量 {event.get('entry_quality','不可用')}/100",
                action_card,
                f"位置：现价 {self._format_price(symbol, event['price'])}｜VWAP {self._format_price(symbol, event.get('vwap'))}｜"
                f"偏离 {_safe_float(event.get('vwap_gap')):.2%}｜本路径允许上限 {_safe_float(displayed_gap_cap):.2%}",
                f"触发：{self._pattern_cn(event.get('pattern'))}｜状态 {self._entry_state_cn(event.get('entry_state'))}｜"
                f"盘口 {_safe_float(event.get('amount_imbalance')):.2f}",
                t1_line,
                continuation_line,
                f"板块：{live_sector.get('theme','未形成同主题样本')}·{self._sector_state_cn(live_sector.get('state'))}｜角色：{self._role_cn(live_sector.get('role'))}{market_sector_text}",
                market_permission_text,
                f"多周期：{self._multitimeframe_line(event.get('multitimeframe') or multitimeframe)}",
                format_structured_timing_line(structured_timing),
                f"资金行为：{self._capital_behavior_line(event.get('capital_behavior') or capital_context)}",
                f"涨停路径：{self._limit_behavior_line(limit_behavior)}",
                *volume_factor_lines,
                *numeric_tag_lines,
                (
                    "分级：已有信号底仓，本次保留原入场日，作为做T/加仓买入腿参考。"
                    if formal_t1_entry and virtual_position
                    else ("分级：达到T+1新开仓门槛，已进入虚拟台账。" if formal_t1_entry else "分级：只确认短线趋势机会，未达到T+1新开仓门槛；如已有底仓，可作为做T参考。")
                ),
                (
                    "边界：不下单；原持仓台账不被覆盖；做T卖出腿只处理昨日前可卖底仓。"
                    if formal_t1_entry and virtual_position
                    else ("边界：不下单；正式新仓写入虚拟台账并持续跟踪卖点；A股当天新买入不得当天卖出。" if formal_t1_entry else "边界：继续实时监控；本事件不写入虚拟新仓，也不是立即买入指令。")
                ),
            ])
        elif event["event"] == "RISK_EVENT_WATCH":
            continuation = event.get("continuation") or {}
            text = "\n".join([
                "【A股轮动｜当日信号失效预警｜T+1不可卖】",
                f"{event.get('name','')}（{self._short_code(symbol)}）{pool_badge}｜{self._pattern_cn(event.get('pattern'))}｜"
                f"延续强度 {continuation.get('score',0)}/100",
                action_card,
                *volume_factor_lines,
                *numeric_tag_lines,
                f"现价 {self._format_price(symbol, event.get('price'))}｜VWAP {self._format_price(symbol, event.get('vwap'))}｜"
                f"相对VWAP {_safe_float(event.get('vwap_gap')):.2%}｜60秒动量 {_safe_float(event.get('momentum_60s')):.2%}",
                f"板块：{live_sector.get('theme','未形成同主题样本')}·{self._sector_state_cn(live_sector.get('state'))}｜角色：{self._role_cn(live_sector.get('role'))}",
                market_permission_text,
                f"多周期：{self._multitimeframe_line(event.get('multitimeframe') or multitimeframe)}",
                format_structured_timing_line(structured_timing),
                f"资金行为：{self._capital_behavior_line(event.get('capital_behavior') or capital_context)}",
                f"失效原因：{'、'.join((continuation.get('missing') or ['延续性组合条件转弱'])[:3])}",
                "处理边界：这是风险预警，不是卖出指令；若该信号对应当天实际买入，A股T+1使其当天不可卖。",
                "虚拟信号台账继续保留，下一交易日按卖点规则持续跟踪。",
            ])
        else:
            held_action_code = str(((action_decision.get("existing_position") or {}).get("code") or ""))
            if symbol in self.virtual_positions and held_action_code == "EXIT":
                event["closed_virtual_position"] = self.virtual_positions.pop(symbol)
                event["position_ledger_operation"] = "CLOSED_ON_FULL_EXIT_SIGNAL"
                self._save_virtual_positions()
            elif symbol in self.virtual_positions:
                # 程序不知道实际减仓数量；减仓提示不能把整笔虚拟持仓从台账删除，
                # 否则后续趋势修复、保护和再入场链条会被遗忘。
                event["position_ledger_operation"] = "KEPT_AFTER_REDUCE_ALERT"
            text = "\n".join([
                "【A股轮动｜盘中减仓/卖出事件】",
                f"{event.get('name','')}（{self._short_code(symbol)}）{pool_badge}｜现价 {self._format_price(symbol, event['price'])}",
                action_card,
                *volume_factor_lines,
                *numeric_tag_lines,
                f"板块：{live_sector.get('theme','未形成同主题样本')}·{self._sector_state_cn(live_sector.get('state'))}｜角色：{self._role_cn(live_sector.get('role'))}{market_sector_text}",
                market_permission_text,
                f"结构：{self._pattern_cn(event.get('pattern'))}；高位回撤 {_safe_float(event.get('drawdown')):.2%}｜60秒动量 {_safe_float(event.get('momentum_60s')):.2%}｜VWAP {self._format_price(symbol, event.get('vwap'))}",
                f"退出分层：{event.get('exit_tier','REDUCE')}｜入场路线{event.get('exit_route_profile','未记录')}｜"
                f"确认持续{_safe_float(event.get('confirmation_persistence_seconds')):.0f}秒｜"
                f"{'结构失守位 ' + self._format_price(symbol, event.get('structure_failure_below')) if event.get('structure_failure_below') else '未触发盘前结构硬失守'}",
                f"多周期：{self._multitimeframe_line(event.get('multitimeframe') or multitimeframe)}",
                format_structured_timing_line(structured_timing),
                f"资金行为：{self._capital_behavior_line(event.get('capital_behavior') or capital_context)}",
                f"慢J {_safe_float(effective_candidate.get('slow_j')):.1f}只表示保护级别；本次卖点由价格、动量和盘口组合确认。",
                "行动层已经给出减仓或卖出分级；系统不自动下单。若该标的是当天买入，必须遵守T+1，不得当天卖出。",
            ])
        card_template = signal_template(str(event.get("event")), str(event.get("pattern")))
        card = build_signal_card(
            text,
            event=event,
            short_code=self._short_code(symbol),
            action_decision=action_decision,
            template=card_template,
        )
        result = self.notifier.send_card(
            card,
            event["event_id"],
            fallback_text=text,
            priority=0 if event.get("event") in {"BUY_EVENT_WATCH", "SELL_EVENT_WATCH"} else 1,
            delivery_context={
                "kind": "TRADE_EVENT",
                "symbol": symbol,
                "event": event.get("event"),
                "pattern": event.get("pattern"),
                "event_ts": event.get("event_ts"),
                "price": event.get("price"),
            },
        )
        event["feishu"] = result
        self._log("tick_events", event)
        return event

    def periodic_summary(self, slot: str) -> Dict[str, Any]:
        self.refresh_market_sectors_async()
        intraday = self.intraday.snapshot()
        intraday_map = intraday.get("by_symbol", {})
        candidate_map = self._candidate_map()
        auction_map = (self.latest_auction_analysis or {}).get("by_symbol", {})
        sector_snapshot = self.sector_health.snapshot()
        continuation_snapshot = self.continuation.snapshot()
        continuation_map = continuation_snapshot.get("by_symbol", {})
        capital_snapshot = self.capital_behavior.snapshot()
        capital_map = capital_snapshot.get("by_symbol", {})
        multitimeframe_snapshot = self.multitimeframe.snapshot()
        multitimeframe_map = multitimeframe_snapshot.get("by_symbol", {})
        structured_timing_snapshot = self.structured_timing.snapshot()
        structured_timing_map = structured_timing_snapshot.get("by_symbol", {})
        limit_behavior_snapshot = self.limit_behavior.snapshot()
        numeric_snapshot = self.numeric_tags.snapshot()
        numeric_rows = self.numeric_tags.recent_hits(limit=2)
        numeric_lines = [summary_tag_line(row, self._short_code(row.get("symbol"))) for row in numeric_rows]
        low_support_rows = self.numeric_tags.recent_low_support_hits(limit=2)
        low_support_lines = [
            summary_low_support_tag_line(row, self._short_code(row.get("symbol")))
            for row in low_support_rows
        ]
        intraday_numeric_lines = [
            f"- {row.get('name') or '未知'}（{self._short_code(row.get('symbol'))}）｜"
            f"今日运行最高{row.get('price_text')}｜{row.get('primary_pattern_cn')}｜只做旁路提醒"
            for row in self.numeric_tags.recent_intraday_hits(limit=2)
        ]
        sector_groups = [
            row for row in sector_snapshot.get("groups", [])
            if row.get("observed_count", 0) >= 2
        ]
        market_snapshot = self.market_sector_radar.latest or {}
        market_permission = self.market_permission.evaluate(
            market_snapshot,
            self.global_market.latest,
            premarket_daily=self.latest_daily,
        )
        market_rows = list(market_snapshot.get("rows") or [])
        market_rows.sort(key=lambda row: (-_safe_float(row.get("health_score_raw")), row.get("board_name", "")))
        market_lines = []
        for row in market_rows[:5]:
            netflow = _safe_float(row.get("main_net_inflow")) / 100000000.0
            technical = "技术预热" if row.get("technical_status") != "READY" else f"技术{int(_safe_float(row.get('technical_score')))}/100"
            market_lines.append(
                f"- {row.get('board_name')}｜{self._market_rotation_cn(row.get('rotation_state'))}｜"
                f"健康{int(_safe_float(row.get('health_score_raw')))}/100（全市场{_safe_float(row.get('health_percentile')):.0%}分位）｜"
                f"涨幅{_safe_float(row.get('board_pct')):+.2%}｜广度{_safe_float(row.get('breadth')):.0%}｜量比{_safe_float(row.get('volume_ratio')):.2f}｜"
                f"主力净流入{netflow:+.2f}亿｜梯队{int(_safe_float(row.get('limit_up_count')))}只/最高{int(_safe_float(row.get('max_board_streak')))}板｜{technical}"
            )

        group_lines = []
        for group in sector_groups[:3]:
            market_group = self.market_sector_radar.context_for_theme(str(group.get("theme") or ""))
            market_text = (
                f"｜全市场{market_group.get('board_name')}·{self._market_rotation_cn(market_group.get('rotation_state'))} "
                f"健康分位{_safe_float(market_group.get('health_percentile')):.0%}｜广度{_safe_float(market_group.get('breadth')):.0%}"
                f"｜梯队{int(_safe_float(market_group.get('limit_up_count')))}只/最高{int(_safe_float(market_group.get('max_board_streak')))}板"
                if market_group.get("board_code") else "｜全市场板块暂无可靠匹配"
            )
            members = group.get("members", [])
            leaders = [row.get("name") for row in members if row.get("role") in {"LEADER", "FRONT"}][:3]
            cores = [row.get("name") for row in members if row.get("role") == "CORE"][:2]
            followers = [row.get("name") for row in members if row.get("role") == "FOLLOWER"][:3]
            ladder = []
            if leaders:
                ladder.append("前排 " + "、".join(leaders))
            if cores:
                ladder.append("中军 " + "、".join(cores))
            if followers:
                ladder.append("跟随 " + "、".join(followers))
            group_lines.append(
                f"- {group.get('theme')}｜{self._sector_state_cn(group.get('state'))}｜{self._strength_bar(group.get('score'))} {group.get('score')}/100｜"
                f"上涨{group.get('up_count')}/{group.get('observed_count')}｜站上VWAP {group.get('above_vwap_count')}/{group.get('observed_count')}｜"
                f"中位涨幅{_safe_float(group.get('median_return')):+.2%}{market_text}\n"
                f"  梯队：{'；'.join(ladder) or '尚未形成清晰梯队'}"
            )

        opportunity_rows = []
        risk_rows = []
        for symbol, base_candidate in candidate_map.items():
            tick_row = intraday_map.get(symbol)
            if not tick_row:
                continue
            candidate = dict(base_candidate)
            candidate["market_sector"] = self.market_sector_radar.context_for_candidate(candidate)
            sector = self.sector_health.context_for(symbol)
            auction = auction_map.get(symbol, {})
            continuation = continuation_map.get(symbol, {})
            multitimeframe = multitimeframe_map.get(symbol, {})
            capital_behavior = capital_map.get(symbol, {})
            structured_timing = structured_timing_map.get(symbol, {})
            base_strength = self._live_strength(candidate, sector, auction)
            strength = (
                int(_safe_float(continuation.get("score"), base_strength))
                if candidate.get("daily_route") == "TREND_CONTINUATION"
                else base_strength
            )
            if multitimeframe.get("periods"):
                strength = max(0, min(100, strength + int(round((_safe_float(multitimeframe.get("score")) - 50) * 0.20))))
            reference_close = _safe_float(candidate.get("close"))
            price = _safe_float(tick_row.get("price"))
            current_return = price / reference_close - 1.0 if price > 0 and reference_close > 0 else None
            row = {
                "symbol": symbol,
                "candidate": candidate,
                "tick": tick_row,
                "sector": sector,
                "auction": auction,
                "continuation": continuation,
                "multitimeframe": multitimeframe,
                "capital_behavior": capital_behavior,
                "structured_timing": structured_timing,
                "strength": strength,
                "current_return": current_return,
            }
            if candidate.get("action") == "EXIT" or candidate.get("protection_level") == "HIGH":
                risk_rows.append(row)
            if (
                candidate.get("intraday_eligible") or candidate.get("action") in {"BUY", "WATCH"}
                or (multitimeframe.get("trigger_confirmed") and _safe_float(current_return) >= 0.04)
            ):
                opportunity_rows.append(row)
        opportunity_rows.sort(key=lambda row: (-row["strength"], -_safe_float(row.get("current_return")), row["symbol"]))
        risk_rows.sort(key=lambda row: (-int(row["candidate"].get("action") == "EXIT"), -row["strength"], row["symbol"]))

        dynamic_core = self._dynamic_core_snapshot(opportunity_rows, limit=6)
        dynamic_scores = {row["symbol"]: row["dynamic_core_score"] for row in dynamic_core["all"]}
        for row in opportunity_rows:
            row["dynamic_core_score"] = dynamic_scores.get(row["symbol"], row["strength"])
        core_symbols = {row["symbol"] for row in dynamic_core["core"]}
        supplement_symbols = {row["symbol"] for row in dynamic_core["supplement"]}
        opportunity_rows.sort(key=lambda row: (
            0 if row["symbol"] in core_symbols else (1 if row["symbol"] in supplement_symbols else 2),
            -int(row.get("dynamic_core_score", row["strength"])),
            -row["strength"],
            row["symbol"],
        ))

        opportunity_lines = []
        for row in opportunity_rows[:6]:
            candidate, tick_row, sector = row["candidate"], row["tick"], row["sector"]
            continuation = row.get("continuation") or {}
            multitimeframe = row.get("multitimeframe") or {}
            capital_behavior = row.get("capital_behavior") or {}
            structured_timing = row.get("structured_timing") or {}
            symbol = row["symbol"]
            if candidate.get("daily_route") == "TREND_CONTINUATION":
                live_state = self._continuation_status_cn(continuation.get("status"))
                missing = continuation.get("missing") or []
                lifecycle = str(tick_row.get("entry_state") or "")
                lifecycle_text = (
                    self._pattern_cn(lifecycle)
                    if lifecycle in {
                        "PRELIMINARY_TREND_WATCH", "ARMED_WAIT_PULLBACK", "PULLBACK_IN_PROGRESS",
                        "TREND_PULLBACK_RECLAIM", "SUDDEN_TREND_ARMED", "SIGNALLED",
                    }
                    else self._phase_cn(lifecycle)
                ) if lifecycle else "尚未进入盘中生命周期"
                condition_text = (
                    f"生命周期：{lifecycle_text}｜延续性：{live_state}"
                    + (f"；还缺{'、'.join(missing[:2])}" if missing else "；组合证据齐备")
                )
            else:
                condition_text = self._phase_cn(tick_row.get("phase"))
            opportunity_lines.append(
                f"- {'核心' if symbol in core_symbols else '补充'}｜{candidate.get('name','未知')}（{self._short_code(symbol)}）｜"
                f"动态{int(row.get('dynamic_core_score', row['strength']))}/100｜强度{row['strength']}/100｜"
                f"涨幅{_safe_float(row.get('current_return')):+.2%}\n"
                f"  {candidate_action_line(candidate, symbol in self.virtual_positions)}\n"
                f"  {sector.get('theme','未形成同主题样本')}·{self._role_cn(sector.get('role'))}｜"
                f"现价{self._format_price(symbol, tick_row.get('price'))}｜VWAP {self._format_price(symbol, tick_row.get('vwap'))}｜"
                f"{condition_text}｜多周期：{self._multitimeframe_line(multitimeframe)}\n"
                f"  资金行为：{self._capital_behavior_line(capital_behavior)}"
                f"\n  {format_structured_timing_line(structured_timing)}"
            )
        if len(opportunity_rows) > 6:
            opportunity_lines.append(f"- 另有{len(opportunity_rows) - 6}只低优先级观察标的，已写入D盘日志。")

        structured_timing_lines = []
        timing_ready_rows = [
            row for row in structured_timing_snapshot.get("rows", [])
            if row.get("status") == "READY"
        ]
        timing_ready_rows.sort(key=lambda row: (-int(_safe_float(row.get("shadow_score"))), str(row.get("symbol"))))
        for row in timing_ready_rows[:6]:
            symbol = str(row.get("symbol") or "")
            candidate = candidate_map.get(symbol) or {}
            structured_timing_lines.append(
                f"- {candidate.get('name') or self._short_code(symbol)}（{self._short_code(symbol)}）｜"
                f"{format_structured_timing_line(row)}"
            )

        risk_lines = []
        for row in risk_rows[:3]:
            candidate, sector = row["candidate"], row["sector"]
            label = "退出候选，继续持有等实时确认" if candidate.get("action") == "EXIT" else "高位保护，尚非卖点"
            risk_lines.append(
                f"- {candidate.get('name','未知')}（{self._short_code(row['symbol'])}）｜{label}｜"
                f"慢J {_safe_float(candidate.get('slow_j')):.1f}｜{sector.get('theme','未形成同主题样本')}·{self._sector_state_cn(sector.get('state'))}\n"
                f"  {candidate_action_line(candidate, row['symbol'] in self.virtual_positions)}"
            )

        limit_behavior_lines = []
        for row in sorted(
            limit_behavior_snapshot.get("rows", []),
            key=lambda item: str(item.get("asof") or ""), reverse=True,
        )[:4]:
            symbol = str(row.get("symbol") or "")
            candidate = candidate_map.get(symbol) or {}
            limit_behavior_lines.append(
                f"- {candidate.get('name') or self._short_code(symbol)}（{self._short_code(symbol)}）｜"
                f"{self._limit_behavior_line(row)}"
            )

        changes = []
        current_group_states = {row.get("theme"): row.get("state") for row in sector_groups}
        for theme, state in current_group_states.items():
            old = self.last_summary_group_states.get(theme)
            if old and old != state:
                changes.append(f"- {theme}：{self._sector_state_cn(old)} → {self._sector_state_cn(state)}")
        current_strength_buckets = {
            row["symbol"]: self._strength_bucket(row["strength"])
            for row in opportunity_rows
        }
        for symbol, bucket in current_strength_buckets.items():
            old = self.last_summary_strength_buckets.get(symbol)
            if old and old != bucket:
                candidate = candidate_map.get(symbol, {})
                changes.append(f"- {candidate.get('name','未知')}（{self._short_code(symbol)}）：{old} → {bucket}")
        if not self.last_summary_group_states:
            changes.append("- 本次为V4结构化总结的首个比较基准。")
        name_by_symbol = {
            symbol: str((candidate_map.get(symbol) or {}).get("name") or self._short_code(symbol))
            for symbol in set(
                dynamic_core["changes"]["new"]
                + dynamic_core["changes"]["continued"]
                + dynamic_core["changes"]["exited"]
            )
        }
        if dynamic_core["changes"]["new"]:
            changes.append("- 新晋动态核心/补充：" + "、".join(name_by_symbol[symbol] for symbol in dynamic_core["changes"]["new"]))
        if dynamic_core["changes"]["exited"]:
            changes.append("- 退出本时点核心6：" + "、".join(name_by_symbol[symbol] for symbol in dynamic_core["changes"]["exited"]))
        self.last_summary_group_states = current_group_states
        self.last_summary_strength_buckets = current_strength_buckets

        top_group = sector_groups[0] if sector_groups else None
        top_market = market_rows[0] if market_rows else None
        continuation_confirmed = [
            row for row in continuation_snapshot.get("rows", []) if row.get("confirmed")
        ]
        if market_snapshot.get("market_regime") == "FAST_ROTATION":
            headline = "全市场板块轮动较快，瞬时榜首不直接转成买点；只接受有广度、梯队、量能和连续快照支持的方向。"
        elif top_market:
            headline = (
                f"全市场当前健康度领先方向为{top_market.get('board_name')}（{self._market_rotation_cn(top_market.get('rotation_state'))}）；"
                "精选/自研合并池只在这些全市场强方向中寻找个股资金确认。"
            )
        elif top_group and top_group.get("state") in {"IGNITION", "EXPANSION", "HEALTHY_TREND"}:
            headline = (
                f"当前资金在精选池内最集中于{top_group.get('theme')}，状态为{self._sector_state_cn(top_group.get('state'))}；"
                "优先寻找板块前排/中军的VWAP承接，不因高开幅度机械放弃。"
            )
        elif top_group:
            headline = f"当前最强方向为{top_group.get('theme')}，但状态仍是{self._sector_state_cn(top_group.get('state'))}，暂不把孤立上涨当成健康主线。"
        else:
            headline = "当前精选池尚未形成可确认的板块梯队，继续等待实时行情覆盖和资金扩散。"
        next_summary = self.next_fixed_summary(datetime.now())
        full_market_core = self.market_sector_radar.full_market_core_candidates(limit=6)
        pool_short_codes = {self._short_code(symbol) for symbol in self.pool}
        dynamic_snapshot = self.dynamic_universe.snapshot()
        dynamic_by_symbol = {row.get("symbol"): row for row in dynamic_snapshot.get("rows") or []}
        dynamic_failures = {
            row.get("symbol"): row for row in dynamic_snapshot.get("prepare_failures") or []
        }
        full_market_core_lines = [
            f"- {row.get('name')}（{row.get('code')}）｜发现{row.get('discovery_score')}/100｜"
            f"{(row.get('matched_board') or {}).get('board_name','未匹配')}·"
            f"{self._market_rotation_cn((row.get('matched_board') or {}).get('rotation_state'))}｜"
            f"涨幅{_safe_float(row.get('pct')):+.2%} / 超板块{_safe_float(row.get('relative_excess_vs_board')):+.2%}｜"
            f"{'与人工池重合·双重入选' if str(row.get('code')) in pool_short_codes else '池外发现'}｜"
            f"{'✅动态深检通过·Tick实时监控' if str(row.get('symbol')) in dynamic_by_symbol and (dynamic_by_symbol[str(row.get('symbol'))].get('formal_ready')) else (f'⚠️深检待重试·{str((dynamic_failures.get(str(row.get("symbol"))) or {}).get("reason") or "数据未齐")[:35]}' if str(row.get('symbol')) in dynamic_failures else ('🟠一级匹配·等待动态深检名额' if row.get('entry_logic_match') else '🟡板块观察，个股条件未齐'))}｜"
            f"下一关：{row.get('next_validation')}"
            for row in full_market_core
        ]
        monitored_total = len(set(self.pool) | set(self.dynamic_universe.active) | set(self.virtual_positions))
        latest_tick_ts = max((str(row.get("event_ts") or "") for row in intraday.get("rows", [])), default="")
        text = "\n".join([
            f"【A股轮动｜盘中总结 {slot}】",
            f"一句话：{headline}",
            f"实时心跳：已覆盖{len(intraday_map)}/{monitored_total}只｜最后行情{latest_tick_ts[11:19] if len(latest_tick_ts) >= 19 else '暂无'}｜固定池与动态深检池继续事件驱动监控",
            f"动态深检：正式Tick监控{dynamic_snapshot.get('formal_ready_count', 0)}/{dynamic_snapshot.get('active_count', 0)}只｜容量{dynamic_snapshot.get('max_active', self.dynamic_universe.max_active)}只｜退出名单不影响已有持仓卖点监控",
            f"全市场板块：{market_snapshot.get('eligible_row_count', 0)}/{market_snapshot.get('raw_row_count', 0)}个有效｜"
            f"环境{market_snapshot.get('market_regime', 'UNAVAILABLE')}｜上涨板块占比{_safe_float(market_snapshot.get('market_positive_breadth')):.0%}",
            f"交易许可：{market_permission.get('state_cn')}｜新仓{market_permission.get('new_entry_permission')}｜"
            f"国内{market_permission.get('domestic_score', 50)}/100｜外围修正{int(_safe_float(market_permission.get('global_adjustment'))):+d}",
            f"持仓应对：{market_permission.get('position_action_cn')}｜外围市场不能单独触发减仓/卖出。",
            f"虚拟信号台账：{len(self.virtual_positions)}只（不是券商真实持仓）",
            f"趋势延续确认：{len(continuation_confirmed)}只；确认必须同时通过日线、竞价、板块和盘中承接。",
            f"分钟共振：{sum(bool(row.get('trigger_confirmed')) for row in multitimeframe_snapshot.get('rows', []))}只；"
            "5/15/30/60/120分钟统一使用KDJ(8,2,2)+MACD(5,10,5)，只读完整K线；60/120分钟暂为结构旁证。",
            f"V16结构择时影子：条件齐备 {sum(bool(row.get('shadow_entry_ready')) for row in timing_ready_rows)}只；"
            "不改变正式信号数量，只用于验证Path/Room/Location/15分钟Setup与执行证据。",
            f"资金行为：主动进攻/受控推进 {sum(row.get('phase') in {'AGGRESSIVE_INFLOW','CONTROLLED_ADVANCE'} for row in capital_snapshot.get('rows', []))}只；"
            f"流出确认 {sum(row.get('phase') == 'CONFIRMED_OUTFLOW' for row in capital_snapshot.get('rows', []))}只。",
            "\n一、全市场板块健康度（主证据，不按涨幅单排）",
            *(market_lines or ["- 全市场板块源暂不可用；本时点不把池内相对强弱当作市场主线。"]),
            "\n二、合并池内部梯队（仅作个股角色旁证）",
            *(group_lines or ["- 暂无至少2只成员具备实时行情的稳定主题。"]),
            "\n三、当前最值得盯的机会（先看动作，再看条件）",
            *(opportunity_lines or ["- 暂无达到中性观察以上强度的标的。"]),
            "\n四、全市场强板块→强个股（最多6只；显示一级发现与二级深检状态）",
            *(full_market_core_lines or ["- 当前没有通过板块持续性、成交容量与位置过滤的池外粗筛标的；不凑数。"]),
            "\n五、V16结构择时影子榜（零权重，不是买入榜）",
            *(structured_timing_lines or ["- 尚未形成足够的顺序Tick结构上下文。"]),
            "\n六、风险与卖点",
            *(risk_lines or ["- 暂无确认卖点；高J本身不再等于卖出。"]),
            "\n七、涨停路径行为（旁路观察）",
            *(limit_behavior_lines or ["- 当前没有标的进入涨停行为观察区。"]),
            "\n八、特殊数字TAG（旁路观察，不改变主策略）",
            *(intraday_numeric_lines or ["- 今日运行最高价暂未命中特殊数字。"]),
            *(numeric_lines or ["- 近10个交易日日线最高价暂无可展示TAG。"]),
            *(low_support_lines or ["- 近10个交易日暂无特殊数字低点形成连续3日回踩不破。"]),
            "\n九、相比上次总结",
            *(changes[:8] or ["- 板块和信号强度级别没有发生明显变化。"]),
            f"\n下一条固定飞书：{next_summary['when']}｜{next_summary['label']}；期间出现买点、卖点或失效事件会实时推送。",
            "使用边界：事件驱动，不固定几点买卖；“新开仓/继续持有/做T/减仓/卖出”是条件式行为信号，系统不发送订单，也不知道券商真实持仓。",
        ])
        event_id = f"periodic:{datetime.now().strftime('%Y-%m-%d')}:{slot}"
        card = build_text_summary_card(
            text,
            template="turquoise",
            fields=[
                ("总结时点", slot),
                ("实时覆盖", f"{len(intraday_map)}/{monitored_total}"),
                ("动态深检", f"{dynamic_snapshot.get('formal_ready_count', 0)}/{dynamic_snapshot.get('max_active', self.dynamic_universe.max_active)}"),
                ("机会观察", f"{len(opportunity_rows)}只"),
                ("风险观察", f"{len(risk_rows)}只"),
                ("板块组", f"{len(sector_groups)}组"),
                ("交易许可", f"{market_permission.get('state_cn')} / {market_permission.get('new_entry_permission')}"),
                ("下一固定消息", f"{next_summary['when']} {next_summary['label']}"),
            ],
            footer="固定总结是状态快照，不是买卖触发｜真正动作只看实时事件卡片｜系统不下单",
        )
        result = self.notifier.send_card(card, event_id, fallback_text=text)
        self._log("summaries", {
            "stage": "periodic_v4", "slot": slot, "event_id": event_id,
            "headline": headline, "sector_snapshot": sector_snapshot,
            "continuation_snapshot": continuation_snapshot,
            "capital_behavior_snapshot": capital_snapshot,
            "limit_behavior_snapshot": limit_behavior_snapshot,
            "multitimeframe_snapshot": multitimeframe_snapshot,
            "structured_timing_snapshot": structured_timing_snapshot,
            "numeric_tag_snapshot": numeric_snapshot,
            "market_permission": market_permission,
            "dynamic_universe_snapshot": dynamic_snapshot,
            "opportunity_count": len(opportunity_rows), "risk_count": len(risk_rows),
            "text": text, "feishu": result,
        })
        return result

    def post_close_summary(self, context: Any) -> Dict[str, Any]:
        trade_date = self._context_now(context).strftime("%Y-%m-%d")
        prior_daily = self.latest_daily
        daily_fresh = True
        try:
            daily = self.refresh_candidates(
                context,
                "POST_CLOSE_FOR_NEXT_DAY",
                expected_asof=trade_date,
            )
        except RuntimeError as exc:
            # 日线供应商盘后落库可能延迟。盘中事实照常总结，但拒绝用D-1截面
            # 冒充今日收盘后候选，避免把过期信息交给AI或发成明日建议。
            daily_fresh = False
            daily = prior_daily or {}
            self._log("daily_freshness_gate", {
                "stage": "post_close", "trade_date": trade_date,
                "status": "STALE_BLOCKED", "message": str(exc),
                "retained_asof": daily.get("asof"),
            })
        numeric_rows = self.numeric_tags.recent_hits(limit=4)
        numeric_lines = [summary_tag_line(row, self._short_code(row.get("symbol"))) for row in numeric_rows]
        low_support_rows = self.numeric_tags.recent_low_support_hits(limit=4)
        low_support_lines = [
            summary_low_support_tag_line(row, self._short_code(row.get("symbol")))
            for row in low_support_rows
        ]
        grouped: Dict[str, int] = defaultdict(int)
        if daily_fresh:
            for row in daily.get("candidates", []):
                if row.get("action") in {"BUY", "WATCH"}:
                    grouped[row.get("niche", "未分类")] += 1
        sector_lines = [f"{key}: {value}只候选" for key, value in sorted(grouped.items(), key=lambda item: (-item[1], item[0]))[:8]]
        market_rows = list((self.market_sector_radar.latest or {}).get("rows") or [])
        healthy_market_rows = [
            row for row in market_rows
            if row.get("rotation_state") in {"SUSTAINED_LEADER", "HEALTHY_RISING", "ROTATION_IN"}
            and _safe_float(row.get("health_percentile")) >= 0.80
            and _safe_float(row.get("top_quartile_persistence")) >= 0.50
            and not row.get("rotation_caution")
        ]
        healthy_market_rows.sort(key=lambda row: (
            -_safe_float(row.get("health_percentile")),
            -_safe_float(row.get("top_quartile_persistence")),
            -_safe_float(row.get("health_score_raw")),
        ))
        healthy_market_lines = [
            f"- **{row.get('board_name')}**｜{self._market_rotation_cn(row.get('rotation_state'))}｜"
            f"健康{int(_safe_float(row.get('health_score_raw')))}/100｜全市场{_safe_float(row.get('health_percentile')):.0%}分位｜"
            f"广度{_safe_float(row.get('breadth')):.0%}｜持续{_safe_float(row.get('top_quartile_persistence')):.0%}｜"
            f"梯队{int(_safe_float(row.get('limit_up_count')))}只/最高{int(_safe_float(row.get('max_board_streak')))}板"
            for row in healthy_market_rows[:5]
        ]
        advice = self.advisor.summarize({
            "stage": "post_close",
            "daily": self._compact_daily_facts() if daily_fresh else {
                "status": "CURRENT_DAILY_NOT_READY",
                "retained_asof": daily.get("asof"),
                "instruction": "不得生成明日个股建议，只总结今日盘中已发生事实",
            },
            "strict_sector_counts": grouped if daily_fresh else {},
        })
        candidates = (
            [row for row in daily.get("candidates", []) if row.get("action") != "WAIT"]
            if daily_fresh else []
        )
        candidates.sort(key=lambda row: -int(_safe_float(row.get("candidate_rank_score", row.get("signal_strength")))))
        candidate_lines = [
            f"- **{row.get('name','未知')}（{self._short_code(row.get('symbol'))}）**｜"
            f"{self._route_cn(row.get('daily_route') or row.get('lane'))}｜"
            f"{int(_safe_float(row.get('candidate_rank_score', row.get('signal_strength'))))}/100｜"
            f"{row.get('group_key') or row.get('niche','未分类')}\n"
            f"  {candidate_action_line(row, row.get('symbol') in self.virtual_positions)}"
            for row in candidates[:6]
        ]
        sections = [
            ("📌 收盘结论", (
                (
                    f"明日共有 **{len(candidates)}只** 非WAIT观察候选。盘后评分只负责缩小范围，"
                    "不把高分直接当买点；真正的新开仓必须等盘中回踩—收复和板块/多周期共振。"
                    if daily_fresh else
                    "**本时点不发布明日个股候选。** 今日完整日线尚未落库，系统已阻止D-1截面冒充盘后结果；"
                    "下一交易日盘前会重新计算。"
                )
            )),
            ("👀 明日优先观察", "\n".join(candidate_lines) if candidate_lines else "- 当前没有非WAIT候选，全池仍会扫描盘中突发趋势。"),
            ("🧭 细分方向", "｜".join(sector_lines) if sector_lines else "暂无候选细分赛道。"),
            ("🌐 全市场健康趋势方向", "\n".join(healthy_market_lines) if healthy_market_lines else "- 暂无同时满足健康分位、持续性和非尾声条件的方向；不在弱市场里矮子拔高。"),
            ("🔢 特殊数字TAG", "\n".join(
                (numeric_lines or ["- 近10日日线最高价暂无可展示TAG。"])
                + (low_support_lines or ["- 近10日暂无特殊数字低点形成连续3日回踩不破。"])
            ) + "\n- 全部TAG观察权重均为0，不改变评分和动作。"),
            ("✅ 明日执行口径", (
                "- **空仓**：只响应实时“新开仓”卡片；武装/回踩中均不追价。\n"
                "- **已有仓位**：按继续持有、做T、减仓、卖出的分级动作处理；做T卖出腿只针对昨日前可卖底仓。\n"
                "- **共同边界**：信号失效以后等待下一事件；系统不自动下单，A股新买部分严格T+1。"
            )),
            ("🤖 辅助复核", advice.get("content") if advice.get("ok") else "DeepSeek本次不可用，保留机械规则结果。"),
        ]
        text = "\n".join([
            f"【A股轮动｜盘后总结 {trade_date}】",
            *(f"{title}\n{body}" for title, body in sections),
        ])
        event_id = f"post_close:{trade_date}"
        card = build_report_card(
            f"📊 A股轮动｜盘后复盘 {trade_date}",
            template="green",
            fields=[
                ("交易日", trade_date),
                ("监控池", f"{daily.get('available_size', 0)}/{len(self.pool)}"),
                ("日线新鲜度", "今日已就绪" if daily_fresh else "未就绪/已阻断"),
                ("明日观察", f"{len(candidates)}只" if daily_fresh else "待盘前刷新"),
                ("虚拟信号持仓", f"{len(self.virtual_positions)}只"),
                ("候选数据截止", daily.get("asof")),
                ("规则版本", str(daily.get("rules_version") or "V7")),
            ],
            sections=sections,
            footer=f"卡片UI {CARD_UI_VERSION}｜所有详细证据保存在D盘｜信号≠收益承诺｜系统不下单",
        )
        result = self.notifier.send_card(card, event_id, fallback_text=text)
        self._log("summaries", {
            "stage": "post_close", "event_id": event_id, "advice": advice,
            "card_ui_version": CARD_UI_VERSION, "candidate_count": len(candidates),
            "daily_fresh": daily_fresh, "feishu": result,
        })
        return result
