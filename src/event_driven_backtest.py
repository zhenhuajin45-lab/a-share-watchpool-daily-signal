# coding: utf-8
"""A股轮动事件驱动研究原型。

把 R0 的日线 BUY 候选当作观察名单，使用 GoldMiner 1 分钟数据逐根回放：
- 日线信号在收盘后才可见；
- 日内只有出现结构事件才入场，不使用固定时钟；
- 信号在当前分钟收盘确认，下一分钟开盘成交，避免同分钟偷看；
- 卖出由失效/动态止盈事件触发，并严格执行 T+1；
- 不做仓位管理，结果以候选级别的毛收益为主。
"""

from __future__ import annotations

import json
import math
import os
import re
from datetime import datetime
from pathlib import Path

import numpy as np
import pandas as pd

from gm.api import history, set_token


ROOT = Path(r"D:\codex\a_share_rotation")
SIGNALS_FILE = ROOT / "outputs" / "gm_backtest" / "20260809_130910" / "signals.jsonl"
DATA_ROOT = ROOT / "data" / "goldminer" / "1m_20260511_20260807"
OUTPUT_ROOT = ROOT / "outputs" / "event_driven" / "20260809_r1"
REPORT_FILE = ROOT / "reports" / "event_driven_backtest_r1_20260809.md"

DATA_START = "2026-01-01 00:00:00"
INTRADAY_START = "2026-05-11 00:00:00"
INTRADAY_END = "2026-08-08 16:00:00"


def _json_default(value):
    if isinstance(value, (np.integer, np.floating)):
        return value.item()
    if isinstance(value, (np.bool_,)):
        return bool(value)
    if isinstance(value, datetime):
        return value.isoformat()
    return str(value)


def _safe_name(symbol: str) -> str:
    return re.sub(r"[^A-Za-z0-9_.-]", "_", symbol)


def _read_jsonl(path: Path):
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def _cached_history(symbol: str, frequency: str, start: str, end: str) -> pd.DataFrame:
    DATA_ROOT.mkdir(parents=True, exist_ok=True)
    path = DATA_ROOT / f"{_safe_name(symbol)}_{frequency}.pkl"
    if path.exists():
        return pd.read_pickle(path)
    fields = "symbol,eob,open,high,low,close,volume,amount,pre_close"
    frame = history(symbol, frequency, start, end, fields=fields, adjust=1, df=True)
    if frame is None:
        frame = pd.DataFrame()
    frame.to_pickle(path)
    return frame


def _normalise(frame: pd.DataFrame) -> pd.DataFrame:
    if frame is None or frame.empty:
        return pd.DataFrame()
    frame = frame.copy()
    frame["eob"] = pd.to_datetime(frame["eob"])
    for col in ("open", "high", "low", "close", "volume", "amount", "pre_close"):
        if col in frame:
            frame[col] = pd.to_numeric(frame[col], errors="coerce")
    frame = frame.dropna(subset=["eob", "open", "high", "low", "close"])
    frame = frame.sort_values("eob").reset_index(drop=True)
    frame["trade_date"] = frame["eob"].dt.strftime("%Y-%m-%d")
    return frame


def _prepare_daily(frame: pd.DataFrame) -> pd.DataFrame:
    frame = _normalise(frame)
    if frame.empty:
        return frame
    previous = frame["pre_close"].where(frame["pre_close"] > 0, frame["close"].shift(1))
    true_range = pd.concat(
        [frame["high"] - frame["low"], (frame["high"] - previous).abs(), (frame["low"] - previous).abs()],
        axis=1,
    ).max(axis=1)
    frame["atr_pct"] = (true_range.rolling(14, min_periods=5).mean() / frame["close"]).clip(0.015, 0.15)
    frame["date"] = frame["trade_date"]
    return frame


def _prepare_minute(frame: pd.DataFrame) -> pd.DataFrame:
    frame = _normalise(frame)
    if frame.empty:
        return frame
    amount = frame["amount"].fillna(0.0)
    volume = frame["volume"].fillna(0.0)
    typical = (frame["high"] + frame["low"] + frame["close"]) / 3.0
    frame["vwap_value"] = amount.where(amount > 0, typical * volume)
    groups = frame.groupby("trade_date", sort=False)
    frame["session_amount"] = groups["vwap_value"].cumsum()
    frame["session_volume"] = groups["volume"].cumsum()
    frame["session_vwap"] = frame["session_amount"] / frame["session_volume"].replace(0, np.nan)
    frame["session_vwap"] = frame["session_vwap"].fillna(frame["close"])
    frame["ema5"] = frame["close"].ewm(span=5, adjust=False, min_periods=1).mean()
    frame["ema10"] = frame["close"].ewm(span=10, adjust=False, min_periods=1).mean()
    frame["macd_hist"] = 2.0 * (frame["ema5"] - frame["ema10"])
    frame["clock"] = frame["eob"].dt.strftime("%H:%M")
    frame["prior_volume_median"] = frame["volume"].shift(1).rolling(5, min_periods=1).median()
    frame["rvol_local"] = frame["volume"] / frame["prior_volume_median"].replace(0, np.nan)
    frame["rvol_local"] = frame["rvol_local"].replace([np.inf, -np.inf], np.nan).fillna(1.0)
    return frame


def _next_session(minute: pd.DataFrame, signal_date: str):
    dates = sorted(set(minute["trade_date"]))
    for value in dates:
        if value > signal_date:
            return value
    return None


def _adaptive_entry(day: pd.DataFrame, previous_close: float, atr_pct: float):
    """返回 (entry_bar_index, pattern, diagnostic)，索引指向成交分钟。"""
    if day.empty or not math.isfinite(previous_close) or previous_close <= 0:
        return None
    atr = float(atr_pct) if math.isfinite(float(atr_pct)) else 0.05
    atr = float(np.clip(atr, 0.015, 0.15))
    state = "WAIT"
    impulse_high = None
    pullback_low = None
    pullback_start = None

    for i in range(1, len(day) - 1):
        row = day.iloc[i]
        previous = day.iloc[i - 1]
        close = float(row["close"])
        vwap = float(row["session_vwap"])
        rvol = float(row["rvol_local"])

        # 动态整理后的突破：按结构窗口观察，不按钟点下单。
        if i >= 5:
            lookback = day.iloc[max(0, i - 5):i]
            box_range = float((lookback["high"].max() - lookback["low"].min()) / previous_close)
            prior_high = float(lookback["high"].max())
            breakout = close > prior_high * (1.0 + max(0.02 * atr, 0.0005))
            if box_range <= 0.75 * atr and breakout and close > vwap and rvol >= 0.95:
                return i + 1, "DYNAMIC_BREAKOUT", {"bar_index": i, "rvol": rvol, "box_range_pct": box_range}

        if state == "WAIT":
            if close >= previous_close * (1.0 + 0.10 * atr) and close >= vwap and rvol >= 0.80:
                state = "IMPULSE"
                impulse_high = float(row["high"])
            continue

        if state == "IMPULSE":
            impulse_high = max(float(impulse_high), float(row["high"]))
            retrace = (float(impulse_high) - close) / previous_close
            if max(0.06 * atr, 0.002) <= retrace <= 0.75 * atr and close >= vwap * (1.0 - 0.20 * atr) and rvol <= 1.35:
                state = "PULLBACK"
                pullback_low = float(row["low"])
                pullback_start = i
            continue

        if state == "PULLBACK":
            pullback_low = min(float(pullback_low), float(row["low"]))
            impulse_range = max(float(impulse_high) - float(pullback_low), previous_close * 0.001)
            recovery_level = float(pullback_low) + 0.42 * impulse_range
            confirmed = (
                close >= recovery_level
                and close > vwap
                and close > float(previous["close"])
                and rvol >= 0.85
            )
            if confirmed:
                return i + 1, "PULLBACK_RECLAIM", {
                    "bar_index": i,
                    "rvol": rvol,
                    "pullback_start_index": pullback_start,
                    "recovery_level": recovery_level,
                }
    return None


def _first_open(day: pd.DataFrame):
    if day.empty:
        return None
    return 0, "NEXT_SESSION_OPEN", {}


def _simulate_exit(minute: pd.DataFrame, entry_global_index: int, entry_date: str, entry_price: float, atr_pct: float):
    atr = float(atr_pct) if math.isfinite(float(atr_pct)) else 0.05
    atr = float(np.clip(atr, 0.015, 0.15))
    hard_stop_pct = max(0.80 * atr, 0.025)
    trail_pct = max(0.60 * atr, 0.020)
    peak = float(entry_price)
    below_vwap_count = 0
    weak_count = 0

    for j in range(entry_global_index + 1, len(minute) - 1):
        row = minute.iloc[j]
        if row["trade_date"] <= entry_date:
            continue
        previous = minute.iloc[j - 1]
        close = float(row["close"])
        peak = max(peak, float(row["high"]))
        below_vwap = close < float(row["session_vwap"]) and close < float(row["ema5"])
        weak_momentum = float(row["macd_hist"]) < float(previous["macd_hist"]) and close < float(row["ema5"])
        below_vwap_count = below_vwap_count + 1 if below_vwap else 0
        weak_count = weak_count + 1 if weak_momentum else 0

        hard_stop = close <= entry_price * (1.0 - hard_stop_pct)
        protected_profit = peak >= entry_price * (1.0 + 0.50 * atr)
        trailing = protected_profit and close <= peak * (1.0 - trail_pct)
        failed_reclaim = below_vwap_count >= 2 and weak_count >= 2
        if not (hard_stop or trailing or failed_reclaim):
            continue

        next_bar = minute.iloc[j + 1]
        reason = "HARD_STOP" if hard_stop else "TRAILING_EXIT" if trailing else "FAILED_RECLAIM"
        return {
            "exit_signal_time": row["eob"].isoformat(),
            "exit_time": next_bar["eob"].isoformat(),
            "exit_date": next_bar["trade_date"],
            "exit_price": float(next_bar["open"]),
            "exit_reason": reason,
            "hold_minutes_after_entry": int(j - entry_global_index),
        }

    last = minute.iloc[-1]
    return {
        "exit_signal_time": None,
        "exit_time": None,
        "exit_date": last["trade_date"],
        "exit_price": float(last["close"]),
        "exit_reason": "OPEN_AT_END",
        "hold_minutes_after_entry": int(len(minute) - 1 - entry_global_index),
    }


def _run_mode(mode: str, signals_by_symbol, daily_by_symbol, minute_by_symbol):
    trades = []
    skipped = []
    for symbol, signals in signals_by_symbol.items():
        daily = daily_by_symbol.get(symbol, pd.DataFrame())
        minute = minute_by_symbol.get(symbol, pd.DataFrame())
        if daily.empty or minute.empty:
            for signal in signals:
                skipped.append({"symbol": symbol, "signal_date": signal["signal_date"], "reason": "NO_DATA"})
            continue
        active_until = None
        for signal in sorted(signals, key=lambda item: item["signal_date"]):
            signal_date = signal["signal_date"]
            if active_until and signal_date <= active_until:
                skipped.append({"symbol": symbol, "signal_date": signal_date, "reason": "OVERLAP_WITH_OPEN_TRADE"})
                continue
            daily_rows = daily[daily["date"] == signal_date]
            if daily_rows.empty:
                skipped.append({"symbol": symbol, "signal_date": signal_date, "reason": "NO_DAILY_BAR"})
                continue
            daily_row = daily_rows.iloc[-1]
            entry_session = _next_session(minute, signal_date)
            if not entry_session:
                skipped.append({"symbol": symbol, "signal_date": signal_date, "reason": "NO_NEXT_SESSION"})
                continue
            day = minute[minute["trade_date"] == entry_session].reset_index()
            if day.empty or len(day) < 2:
                skipped.append({"symbol": symbol, "signal_date": signal_date, "reason": "NO_INTRADAY_BARS"})
                continue
            decision = (
                _adaptive_entry(day, float(daily_row["close"]), float(daily_row["atr_pct"]))
                if mode == "ADAPTIVE"
                else _first_open(day)
            )
            if decision is None:
                skipped.append({"symbol": symbol, "signal_date": signal_date, "reason": "NO_EVENT_TRIGGER"})
                continue
            local_index, pattern, diagnostic = decision
            entry_bar = day.iloc[local_index]
            entry_time = entry_bar["eob"]
            entry_date = entry_bar["trade_date"]
            global_index = int(entry_bar["index"])
            entry_price = float(entry_bar["open"])
            exit_info = _simulate_exit(minute, global_index, entry_date, entry_price, float(daily_row["atr_pct"]))
            gross_return = exit_info["exit_price"] / entry_price - 1.0
            trade = {
                "symbol": symbol,
                "signal_date": signal_date,
                "entry_session": entry_session,
                "entry_signal_time": (day.iloc[max(0, local_index - 1)]["eob"].isoformat() if local_index > 0 else None),
                "entry_time": entry_time.isoformat(),
                "entry_date": entry_date,
                "entry_price": entry_price,
                "entry_pattern": pattern,
                "entry_diagnostic": json.dumps(diagnostic, ensure_ascii=False),
                "daily_lane": signal.get("lane"),
                "daily_signal_score": signal.get("signal_score"),
                "atr_pct_at_signal": float(daily_row["atr_pct"]),
                "mode": mode,
                **exit_info,
                "gross_return": gross_return,
                "completed": exit_info["exit_reason"] != "OPEN_AT_END",
            }
            trades.append(trade)
            active_until = exit_info["exit_date"]
    return trades, skipped


def _summary(trades, skipped, mode):
    frame = pd.DataFrame(trades)
    completed = frame[frame["completed"]].copy() if not frame.empty else frame
    result = {
        "mode": mode,
        "candidates": len(trades) + len(skipped),
        "entries": len(trades),
        "completed": len(completed),
        "open_at_end": int(len(frame) - len(completed)),
        "skipped": len(skipped),
        "entry_rate": len(trades) / max(1, len(trades) + len(skipped)),
    }
    if completed.empty:
        return result
    returns = completed["gross_return"].astype(float)
    winners = returns[returns > 0]
    losers = returns[returns <= 0]
    result.update(
        {
            "win_rate": float((returns > 0).mean()),
            "mean_gross_return": float(returns.mean()),
            "median_gross_return": float(returns.median()),
            "best_trade": float(returns.max()),
            "worst_trade": float(returns.min()),
            "profit_factor": float(winners.sum() / abs(losers.sum())) if not losers.empty and losers.sum() != 0 else None,
            "compound_equal_weight": float((1.0 + returns).prod() - 1.0),
            "exit_reasons": completed["exit_reason"].value_counts().to_dict(),
            "entry_patterns": frame["entry_pattern"].value_counts().to_dict(),
        }
    )
    return result


def _fmt_pct(value):
    if value is None:
        return "n/a"
    try:
        if not math.isfinite(float(value)):
            return "n/a"
    except (TypeError, ValueError):
        return "n/a"
    return f"{float(value) * 100:.2f}%"


def main():
    token = os.getenv("GOLDMINER_TOKEN", "").strip()
    if token:
        set_token(token)
    raw_signals = _read_jsonl(SIGNALS_FILE)
    buys = [item for item in raw_signals if item.get("action") == "BUY" and item.get("signal_date")]
    signals_by_symbol = {}
    for item in buys:
        signals_by_symbol.setdefault(item["symbol"], []).append(item)
    symbols = sorted(signals_by_symbol)

    daily_by_symbol = {}
    minute_by_symbol = {}
    for index, symbol in enumerate(symbols, start=1):
        daily_by_symbol[symbol] = _prepare_daily(_cached_history(symbol, "1d", DATA_START, INTRADAY_END))
        minute_by_symbol[symbol] = _prepare_minute(_cached_history(symbol, "1m", INTRADAY_START, INTRADAY_END))
        print(f"loaded {index}/{len(symbols)} {symbol}: daily={len(daily_by_symbol[symbol])}, minute={len(minute_by_symbol[symbol])}")

    adaptive_trades, adaptive_skipped = _run_mode("ADAPTIVE", signals_by_symbol, daily_by_symbol, minute_by_symbol)
    baseline_trades, baseline_skipped = _run_mode("NEXT_OPEN", signals_by_symbol, daily_by_symbol, minute_by_symbol)
    summaries = [
        _summary(adaptive_trades, adaptive_skipped, "ADAPTIVE"),
        _summary(baseline_trades, baseline_skipped, "NEXT_OPEN"),
    ]

    OUTPUT_ROOT.mkdir(parents=True, exist_ok=True)
    pd.DataFrame(adaptive_trades + baseline_trades).to_csv(OUTPUT_ROOT / "trades.csv", index=False, encoding="utf-8-sig")
    pd.DataFrame(adaptive_skipped + baseline_skipped).to_csv(OUTPUT_ROOT / "skipped.csv", index=False, encoding="utf-8-sig")
    (OUTPUT_ROOT / "summary.json").write_text(json.dumps(summaries, ensure_ascii=False, indent=2, default=_json_default), encoding="utf-8")

    july3 = [item for item in adaptive_trades if item["signal_date"] == "2026-07-03"]
    july3_frame = pd.DataFrame(july3)
    lines = [
        "# 事件驱动日内回测 R1",
        "",
        "## 研究口径",
        "",
        "- 日线 BUY 候选来自 R0，信号在日线收盘后才可见。",
        "- 日内数据为 GoldMiner 1 分钟历史数据；当前不是逐笔 Tick。",
        "- 事件在分钟收盘确认，下一分钟开盘成交；没有使用未来分钟数据。",
        "- 自适应模式只在动态突破或回踩重夺结构出现时买入。",
        "- 卖出使用动态止损、盈利回撤和失败重夺事件，并执行 T+1。",
        "- 不计手续费、印花税和滑点；不模拟仓位管理。",
        "",
        "## 总体结果",
        "",
        "| 模式 | 候选数 | 入场数 | 入场率 | 完成交易 | 胜率 | 平均毛收益 | 中位数 | 等权复合 |",
        "|---|---:|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for item in summaries:
        lines.append(
            f"| {item['mode']} | {item['candidates']} | {item['entries']} | {_fmt_pct(item['entry_rate'])} | "
            f"{item.get('completed', 0)} | {_fmt_pct(item.get('win_rate'))} | {_fmt_pct(item.get('mean_gross_return'))} | "
            f"{_fmt_pct(item.get('median_gross_return'))} | {_fmt_pct(item.get('compound_equal_weight'))} |"
        )
    lines += [
        "",
        "## 自适应模式退出原因",
        "",
        json.dumps(summaries[0].get("exit_reasons", {}), ensure_ascii=False),
        "",
        "## 2026-07-03切片",
        "",
    ]
    if july3_frame.empty:
        lines.append("该日没有完成自适应入场，说明5个候选均未形成可接受的日内结构。")
    else:
        lines.append(july3_frame[["symbol", "entry_time", "entry_pattern", "entry_price", "exit_time", "exit_reason", "gross_return"]].to_markdown(index=False))
    lines += [
        "",
        "## 解释边界",
        "",
        "本轮是事件驱动原型，不是最终参数。若自适应模式入场率很低，不能直接视为策略失败；需要同时观察被过滤候选后续走势，判断过滤是有效规避还是过度保守。下一轮应把入场事件、未入场候选的后续收益、交易触发原因和T+1可卖状态逐笔展开。",
        "",
        f"数据缓存：{DATA_ROOT}",
        f"交易明细：{OUTPUT_ROOT / 'trades.csv'}",
    ]
    REPORT_FILE.parent.mkdir(parents=True, exist_ok=True)
    REPORT_FILE.write_text("\n".join(lines), encoding="utf-8")
    print(json.dumps(summaries, ensure_ascii=False, indent=2, default=_json_default))
    print(f"report={REPORT_FILE}")


if __name__ == "__main__":
    main()
