# coding: utf-8
"""A股轮动研究版 GoldMiner 适配器。

研究边界：
- 日线收盘信号，下一交易日的日线撮合事件执行；
- 买入成交日禁止卖出，最早下一交易日才允许卖出；
- 先验证信号链路，不把当前等权执行器视为最终仓位管理；
- 板块只使用严格分类的精选池层级代理；全池宽度仅记录市场上下文，不替代板块。
"""

from __future__ import annotations

import json
import math
import os
import re
from datetime import date, datetime
from pathlib import Path

import numpy as np
import pandas as pd

from signal_rules import DEFAULT_CONFIG, classify_daily_signal, compute_features, resample_monthly
from sector_context import build_group_returns, select_sector_context

from gm.api import (
    OrderSide_Buy,
    OrderSide_Sell,
    OrderStatus_Canceled,
    OrderStatus_DoneForDay,
    OrderStatus_Expired,
    OrderStatus_Filled,
    OrderStatus_Rejected,
    OrderStatus_Stopped,
    OrderType_Market,
    PositionSide_Long,
    order_target_percent,
    subscribe,
)


ROOT = Path(r"D:\codex\a_share_rotation")
POOL_FILE = ROOT / "universe" / "selected_pool_20260809.txt"
TAXONOMY_FILE = ROOT / "universe" / "sector_taxonomy.json"
OUTPUT_ROOT = ROOT / "outputs" / "gm_backtest"

FAST_KDJ = DEFAULT_CONFIG.fast_kdj
SLOW_KDJ = DEFAULT_CONFIG.slow_kdj
MACD = DEFAULT_CONFIG.macd
SUBSCRIBE_COUNT = 1600
MIN_BARS = DEFAULT_CONFIG.minimum_daily_bars
SLOW_J_BUY_LOW = DEFAULT_CONFIG.slow_j_buy_low
SLOW_J_BUY_HIGH = DEFAULT_CONFIG.slow_j_buy_high
SLOW_J_SELL_LOW = DEFAULT_CONFIG.slow_j_sell_low

# 仅为本次组合回测提供可执行成交，不代表最终仓位方案。
TARGET_WEIGHT = 0.10
MAX_HOLDINGS = 10
INCLUDE_ETF = True
UNIVERSE_TIME_INTEGRITY = "EX_POST_SELECTED_POOL_CONDITIONAL_REPLAY_NOT_PERFORMANCE_EVIDENCE"
EXECUTION_ENABLED = os.getenv("A_SHARE_ROTATION_ENABLE_DAILY_BAR_EXECUTION", "0") == "1"


def _json_default(value):
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if isinstance(value, (np.integer, np.floating)):
        return value.item()
    if isinstance(value, (np.bool_,)):
        return bool(value)
    return str(value)


def _write_jsonl(path: Path, payload: dict):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(payload, ensure_ascii=False, default=_json_default) + "\n")


def _date_key(value):
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.date().isoformat()
    if isinstance(value, date):
        return value.isoformat()
    text = str(value)
    return text[:10]


def _load_pool(path: Path):
    symbols = []
    pattern = re.compile(r"\|\s*((?:SHSE|SZSE)\.\d{6})\s*\|")
    for line in path.read_text(encoding="utf-8").splitlines():
        match = pattern.search(line)
        if not match:
            continue
        symbol = match.group(1)
        if not INCLUDE_ETF and symbol == "SHSE.517400":
            continue
        if symbol not in symbols:
            symbols.append(symbol)
    if not symbols:
        raise RuntimeError(f"票池为空: {path}")
    return symbols


def _load_taxonomy(path: Path):
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not payload.get("symbols"):
        raise RuntimeError(f"严格行业分类为空: {path}")
    return payload


def _features(frame: pd.DataFrame):
    return compute_features(frame, minimum=MIN_BARS, config=DEFAULT_CONFIG)


def _frame_from_context(context, symbol):
    data = context.data(
        symbol,
        frequency="1d",
        count=SUBSCRIBE_COUNT,
        fields="symbol,eob,open,high,low,close,volume,amount,pre_close",
    )
    if data is None or len(data) == 0:
        return None
    return _features(data)


def _group_state(context, symbols, frames=None):
    returns = []
    for symbol in symbols:
        frame = frames.get(symbol) if frames is not None else _frame_from_context(context, symbol)
        if frame is None or len(frame) < 6:
            continue
        value = float(frame["close"].iloc[-1] / frame["close"].iloc[-6] - 1.0)
        if math.isfinite(value):
            returns.append(value)
    if not returns:
        return 0, 0.0, 0.0, 0
    breadth = float(np.mean(np.asarray(returns) > 0))
    median_return = float(np.median(returns))
    if breadth >= 0.55 and median_return > 0:
        state = 1
    elif breadth <= 0.40 and median_return < 0:
        state = -1
    else:
        state = 0
    return state, breadth, median_return, len(returns)


def _signal(frame: pd.DataFrame, sector_state: int, sector_confidence: str = "LOW"):
    monthly = resample_monthly(frame)
    return classify_daily_signal(
        frame,
        monthly,
        sector_state=sector_state,
        sector_confidence=sector_confidence,
        config=DEFAULT_CONFIG,
    )


def _held_symbols(context):
    try:
        account = context.account()
        if account is None:
            return set()
        return {
            position.get("symbol")
            for position in account.positions()
            if position.get("symbol") and float(position.get("volume", 0) or 0) > 0
        }
    except Exception:
        return set()


def _can_sell(context, symbol, signal_date):
    buy_date = context.buy_date_by_symbol.get(symbol)
    return bool(buy_date and buy_date < signal_date)


def _submit(context, symbol, action, signal):
    if symbol in context.pending_symbols:
        return
    try:
        if action == "BUY":
            held = _held_symbols(context)
            pending_buys = len([s for s in context.pending_symbols if context.pending_side.get(s) == "BUY"])
            if symbol in held or len(held) + pending_buys >= MAX_HOLDINGS:
                return
            result = order_target_percent(symbol, TARGET_WEIGHT, PositionSide_Long, OrderType_Market)
            context.pending_side[symbol] = "BUY"
        else:
            result = order_target_percent(symbol, 0.0, PositionSide_Long, OrderType_Market)
            context.pending_side[symbol] = "SELL"
        context.pending_symbols.add(symbol)
        _write_jsonl(
            context.orders_path,
            {"event": "order_submit", "symbol": symbol, "action": action, "signal": signal, "result": result},
        )
    except Exception as exc:
        _write_jsonl(
            context.errors_path,
            {"event": "order_submit_error", "symbol": symbol, "action": action, "error": repr(exc), "signal": signal},
        )


def init(context):
    context.pool = _load_pool(POOL_FILE)
    context.taxonomy = _load_taxonomy(TAXONOMY_FILE)
    context.pending_symbols = set()
    context.pending_side = {}
    context.buy_date_by_symbol = {}
    context.last_processed_date = None
    context.signal_counts = {"evaluated": 0, "buy": 0, "exit": 0, "no_signal": 0, "insufficient_data": 0}

    run_id = datetime.now().strftime("%Y%m%d_%H%M%S")
    context.run_dir = OUTPUT_ROOT / run_id
    context.run_dir.mkdir(parents=True, exist_ok=True)
    context.signals_path = context.run_dir / "signals.jsonl"
    context.orders_path = context.run_dir / "orders.jsonl"
    context.executions_path = context.run_dir / "executions.jsonl"
    context.errors_path = context.run_dir / "errors.jsonl"
    context.summary_path = context.run_dir / "summary.json"

    config = {
        "pool_file": str(POOL_FILE),
        "pool_size": len(context.pool),
        "pool_recorded_on": "2026-08-09",
        "universe_time_integrity": UNIVERSE_TIME_INTEGRITY,
        "taxonomy_file": str(TAXONOMY_FILE),
        "sector_mode": "strict_selected_pool_hierarchy_proxy",
        "pool_state_role": "MARKET_CONTEXT_ONLY_NOT_SECTOR_FALLBACK",
        "fast_kdj": FAST_KDJ,
        "slow_kdj": SLOW_KDJ,
        "macd": MACD,
        "subscribe_count": SUBSCRIBE_COUNT,
        "min_bars": MIN_BARS,
        "target_weight_for_execution_only": TARGET_WEIGHT,
        "max_holdings_for_execution_only": MAX_HOLDINGS,
        "execution_enabled": EXECUTION_ENABLED,
        "execution_warning": "默认关闭；GoldMiner日线回调+next-bar撮合会比D信号多延迟一个交易日，不能模拟Tick事件买点",
        "t_plus_one_rule": "signal from D daily bar; order is eligible on next trading day; sell requires buy_date < signal_date; actual execution timestamp must be audited per run",
    }
    _write_jsonl(context.run_dir / "config.jsonl", config)

    subscribe(
        symbols=context.pool,
        frequency="1d",
        count=SUBSCRIBE_COUNT,
        wait_group=True,
        wait_group_timeout="30s",
        fields="symbol,eob,open,high,low,close,volume,amount,pre_close",
    )
    print(f"A股轮动研究版启动: pool={len(context.pool)}, sector_mode={config['sector_mode']}, run_dir={context.run_dir}")


def on_bar(context, bars):
    if not bars:
        return
    signal_date = _date_key(max(bar["eob"] for bar in bars))
    if not signal_date or signal_date == context.last_processed_date:
        return
    context.last_processed_date = signal_date

    # 每个交易日每只股票只计算一次指标，避免板块代理和个股信号重复计算。
    frames = {}
    for symbol in context.pool:
        frame = _frame_from_context(context, symbol)
        if frame is not None and not frame.empty:
            frames[symbol] = frame
        else:
            context.signal_counts["insufficient_data"] += 1
    context.frame_cache = frames

    pool_state, pool_breadth, pool_median_return, pool_count = _group_state(context, context.pool, frames=frames)
    returns = {
        symbol: float(frame["close"].iloc[-1] / frame["close"].iloc[-6] - 1.0)
        for symbol, frame in frames.items()
        if len(frame) >= 6
    }
    sector_groups = build_group_returns(returns, context.taxonomy)
    held = _held_symbols(context)
    daily_records = []

    for symbol, frame in frames.items():
        sector = select_sector_context(symbol, sector_groups, context.taxonomy)
        result = _signal(frame, sector["state"], sector_confidence=sector["confidence"])
        if result is None or result["signal_date"] != signal_date:
            context.signal_counts["insufficient_data"] += 1
            continue

        context.signal_counts["evaluated"] += 1
        if result["action"] == "BUY":
            context.signal_counts["buy"] += 1
        elif result["action"] == "EXIT":
            context.signal_counts["exit"] += 1
        else:
            context.signal_counts["no_signal"] += 1

        record = {
            "event": "signal",
            "symbol": symbol,
            "sector": sector["key"],
            "sector_level": sector["level"],
            "sector_source": sector["source"],
            "sector_state": sector["state"],
            "sector_confidence": sector["confidence"],
            "sector_breadth": sector["breadth"],
            "sector_median_return_5d": sector["median_return_5d"],
            "sector_member_count": sector["member_count"],
            "pool_state": pool_state,
            "pool_breadth": pool_breadth,
            "pool_median_return_5d": pool_median_return,
            "pool_observation_count": pool_count,
            "held_before_signal": symbol in held,
            "universe_point_in_time": False,
            "overall_no_lookahead": False,
            **result,
        }
        # WAIT仅计数；WATCH/BUY/EXIT写入信号账，避免无效I/O。
        if result["action"] != "WAIT":
            _write_jsonl(context.signals_path, record)
            daily_records.append(record)

        if EXECUTION_ENABLED and result["action"] == "BUY":
            _submit(context, symbol, "BUY", record)
        elif EXECUTION_ENABLED and result["action"] == "EXIT" and symbol in held and _can_sell(context, symbol, signal_date):
            _submit(context, symbol, "EXIT", record)

    if daily_records:
        print(f"{signal_date} signals={len(daily_records)} buy={sum(r['action']=='BUY' for r in daily_records)} exit={sum(r['action']=='EXIT' for r in daily_records)}")


def on_execution_report(context, report):
    _write_jsonl(context.executions_path, {"event": "execution_report", **dict(report)})
    symbol = report.get("symbol")
    volume = float(report.get("volume", 0) or 0)
    if not symbol or volume <= 0:
        return
    side = report.get("side")
    execution_date = _date_key(report.get("created_at")) or context.last_processed_date
    context.pending_symbols.discard(symbol)
    context.pending_side.pop(symbol, None)
    if side == OrderSide_Buy:
        context.buy_date_by_symbol[symbol] = execution_date
    elif side == OrderSide_Sell:
        context.buy_date_by_symbol.pop(symbol, None)


def on_order_status(context, order):
    _write_jsonl(context.orders_path, {"event": "order_status", **dict(order)})
    symbol = order.get("symbol")
    if not symbol:
        return
    status = order.get("status")
    if status in {
        OrderStatus_Canceled,
        OrderStatus_DoneForDay,
        OrderStatus_Expired,
        OrderStatus_Rejected,
        OrderStatus_Stopped,
        OrderStatus_Filled,
    }:
        context.pending_symbols.discard(symbol)
        context.pending_side.pop(symbol, None)
    if status == OrderStatus_Filled and float(order.get("filled_volume", 0) or 0) > 0:
        side = order.get("side")
        if side == OrderSide_Buy:
            context.buy_date_by_symbol[symbol] = context.last_processed_date
        elif side == OrderSide_Sell:
            context.buy_date_by_symbol.pop(symbol, None)


def on_backtest_finished(context, indicator):
    context.summary_path.write_text(
        json.dumps(
            {"event": "backtest_finished", "indicator": indicator, "signal_counts": context.signal_counts},
            ensure_ascii=False,
            default=_json_default,
        ),
        encoding="utf-8",
    )
    print(f"A股轮动回测完成，结果目录: {context.run_dir}")
    print(json.dumps(indicator, ensure_ascii=False, default=_json_default))


def on_error(context, code, info):
    _write_jsonl(context.errors_path, {"event": "gm_error", "code": code, "info": info})
    print(f"GoldMiner error: code={code}, info={info}")


def on_shutdown(context):
    _write_jsonl(
        context.run_dir / "lifecycle.jsonl",
        {"event": "shutdown", "last_processed_date": context.last_processed_date},
    )
