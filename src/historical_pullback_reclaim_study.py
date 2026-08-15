# coding: utf-8
"""V7入场位置规则的事前滚动研究。

目标不是回测完整实盘服务，而是隔离验证本轮最关键改动：高位趋势先进入ARMED，
只有在受控回踩、重新收复、分钟多周期仍支持后，才形成技术入场候选。

严格边界：
- D日候选只使用D-1及以前的日线；
- 日内信号只使用当前及以前的完整1分钟K线；
- 信号在分钟收盘确认，下一分钟开盘作为成交代理；
- 不使用历史五档、集合竞价和全市场板块快照，因此不伪装成完整策略收益；
- 当前29只池在研究区间后才记录，结果存在事后选池偏差，只用于验证入场位置因子。
"""

from __future__ import annotations

import json
import math
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from statistics import mean, median
from typing import Any, Dict, Iterable, List, Optional, Tuple

import numpy as np
import pandas as pd

from live_signal_service import DailyCandidateBuilder, load_pool, load_taxonomy
from multitimeframe_engine import aggregate_completed_minutes, classify_period


ROOT = Path(r"D:\codex\a_share_rotation")
DAILY_ROOT = ROOT / "data" / "goldminer" / "daily_adjust_prev_20210101_20260807"
MINUTE_ROOT = ROOT / "data" / "goldminer" / "1m_20260511_20260807"
REPORT_JSON = ROOT / "reports" / "historical_pullback_reclaim_study_20260511_20260807.json"
REPORT_MD = ROOT / "reports" / "historical_pullback_reclaim_study_20260511_20260807.md"
START_DATE = "2026-05-11"
END_DATE = "2026-08-07"


def _safe_float(value: Any, default: float = 0.0) -> float:
    try:
        result = float(value)
        return result if math.isfinite(result) else default
    except (TypeError, ValueError):
        return default


def _load_frame(path: Path) -> pd.DataFrame:
    if not path.exists():
        return pd.DataFrame()
    frame = pd.read_pickle(path).copy()
    if "eob" in frame:
        frame["eob"] = pd.to_datetime(frame["eob"], errors="coerce")
        if getattr(frame["eob"].dt, "tz", None) is not None:
            frame["eob"] = frame["eob"].dt.tz_localize(None)
    for column in ("open", "high", "low", "close", "volume", "amount", "pre_close"):
        if column in frame:
            frame[column] = pd.to_numeric(frame[column], errors="coerce")
    return frame.dropna(subset=["eob"]).sort_values("eob").reset_index(drop=True)


def _prepare_minute(frame: pd.DataFrame) -> pd.DataFrame:
    if frame.empty:
        return frame
    result = frame.dropna(subset=["open", "high", "low", "close"]).copy()
    result["trade_date"] = result["eob"].dt.strftime("%Y-%m-%d")
    result = result[(result["trade_date"] >= START_DATE) & (result["trade_date"] <= END_DATE)]
    result["volume"] = result.get("volume", 0).fillna(0.0)
    result["amount"] = result.get("amount", 0).fillna(0.0)
    groups = result.groupby("trade_date", sort=False)
    result["cum_volume"] = groups["volume"].cumsum()
    result["cum_amount"] = groups["amount"].cumsum()
    result["vwap"] = result["cum_amount"] / result["cum_volume"].replace(0, np.nan)
    result["vwap"] = result["vwap"].where(
        (result["vwap"] >= result["close"] * 0.5) & (result["vwap"] <= result["close"] * 1.5),
        result["vwap"] / 100.0,
    ).fillna(result["close"])
    result["minute_return_3"] = result["close"].pct_change(3)
    return result.reset_index(drop=True)


def _profit_factor(values: Iterable[float]) -> Optional[float]:
    values = [float(value) for value in values if value is not None and math.isfinite(float(value))]
    gains = sum(value for value in values if value > 0)
    losses = -sum(value for value in values if value < 0)
    return gains / losses if losses > 0 else (None if gains == 0 else float("inf"))


def _summary(rows: List[Dict[str, Any]], prefix: str) -> Dict[str, Any]:
    result: Dict[str, Any] = {"count": len(rows)}
    for horizon in ("d0", "d1", "d3", "d5"):
        values = [row.get(f"{prefix}_{horizon}") for row in rows]
        values = [float(value) for value in values if value is not None and math.isfinite(float(value))]
        excess = [row.get(f"{prefix}_{horizon}_excess") for row in rows]
        excess = [float(value) for value in excess if value is not None and math.isfinite(float(value))]
        result[horizon] = {
            "n": len(values),
            "mean": mean(values) if values else None,
            "median": median(values) if values else None,
            "win_rate": sum(value > 0 for value in values) / len(values) if values else None,
            "profit_factor": _profit_factor(values),
            "mean_excess_vs_csi300_open_proxy": mean(excess) if excess else None,
            "excess_win_rate": sum(value > 0 for value in excess) / len(excess) if excess else None,
        }
    mfe = [row.get(f"{prefix}_mfe_1d") for row in rows if row.get(f"{prefix}_mfe_1d") is not None]
    mae = [row.get(f"{prefix}_mae_1d") for row in rows if row.get(f"{prefix}_mae_1d") is not None]
    result["path_1d"] = {
        "mean_mfe": mean(mfe) if mfe else None,
        "mean_mae": mean(mae) if mae else None,
    }
    return result


def _period_contexts(periods: Dict[int, pd.DataFrame], signal_ts: pd.Timestamp) -> Dict[str, Any]:
    contexts: Dict[str, Dict[str, Any]] = {}
    for minutes, frame in periods.items():
        completed = frame[frame["eob"] <= signal_ts]
        contexts[str(minutes)] = classify_period(completed.tail(400), minutes)
    available = [row for row in contexts.values() if row.get("state") != "UNAVAILABLE"]
    supportive = [row for row in available if row.get("supportive")]
    bearish = [row for row in available if row.get("bearish")]
    five = contexts.get("5") or {}
    higher = [contexts.get("15") or {}, contexts.get("30") or {}]
    raw_five_trigger = bool(
        five.get("supportive")
        and (five.get("kdj_cross") or five.get("kdj_rising"))
        and (five.get("macd_improving") or five.get("macd_cross") or five.get("macd_divergence") == "BULLISH")
    )
    score = int(round(np.mean([row.get("score", 0) for row in available]))) if available else 0
    return {
        "periods": contexts,
        "score": score,
        "supportive_count": len(supportive),
        "bearish_count": len(bearish),
        "trigger_confirmed": bool(
            raw_five_trigger
            and sum(bool(row.get("supportive")) for row in higher) >= 1
            and sum(bool(row.get("bearish")) for row in higher) < 2
        ),
    }


def _next_minute_open(day: pd.DataFrame, signal_index: int) -> Optional[Tuple[int, float]]:
    next_index = signal_index + 1
    if next_index >= len(day):
        return None
    signal_ts = pd.Timestamp(day.iloc[signal_index]["eob"])
    next_ts = pd.Timestamp(day.iloc[next_index]["eob"])
    if signal_ts.hour == 11 and next_ts.hour == 13:
        return None
    value = _safe_float(day.iloc[next_index]["open"])
    return (next_index, value) if value > 0 else None


def _path_metrics(
    symbol: str,
    signal_date: str,
    entry_index: int,
    entry_price: float,
    minute_by_date: Dict[str, pd.DataFrame],
    daily: pd.DataFrame,
    benchmark: pd.DataFrame,
    prefix: str,
) -> Dict[str, Any]:
    result: Dict[str, Any] = {}
    stock_dates = daily["eob"].dt.strftime("%Y-%m-%d").tolist()
    if signal_date not in stock_dates or entry_price <= 0:
        return result
    day_position = stock_dates.index(signal_date)
    day = minute_by_date[signal_date]
    remaining = day.iloc[entry_index:]
    result[f"{prefix}_mfe_1d"] = _safe_float(remaining["high"].max()) / entry_price - 1.0 if len(remaining) else None
    result[f"{prefix}_mae_1d"] = _safe_float(remaining["low"].min()) / entry_price - 1.0 if len(remaining) else None

    benchmark_dates = benchmark["eob"].dt.strftime("%Y-%m-%d").tolist()
    benchmark_position = benchmark_dates.index(signal_date) if signal_date in benchmark_dates else None
    benchmark_start = (
        _safe_float(benchmark.iloc[benchmark_position].get("open"))
        if benchmark_position is not None else 0.0
    )
    horizon_offsets = {"d0": 0, "d1": 1, "d3": 3, "d5": 5}
    for label, offset in horizon_offsets.items():
        stock_target = day_position + offset
        if stock_target >= len(daily):
            result[f"{prefix}_{label}"] = None
            result[f"{prefix}_{label}_excess"] = None
            continue
        stock_return = _safe_float(daily.iloc[stock_target]["close"]) / entry_price - 1.0
        result[f"{prefix}_{label}"] = stock_return
        benchmark_target = benchmark_position + offset if benchmark_position is not None else None
        if benchmark_target is not None and benchmark_target < len(benchmark) and benchmark_start > 0:
            benchmark_return = _safe_float(benchmark.iloc[benchmark_target]["close"]) / benchmark_start - 1.0
            result[f"{prefix}_{label}_benchmark"] = benchmark_return
            result[f"{prefix}_{label}_excess"] = stock_return - benchmark_return
        else:
            result[f"{prefix}_{label}_excess"] = None
    return result


def _study_symbol_day(
    symbol: str,
    name: str,
    candidate: Dict[str, Any],
    day: pd.DataFrame,
    periods: Dict[int, pd.DataFrame],
    minute_by_date: Dict[str, pd.DataFrame],
    daily: pd.DataFrame,
    benchmark: pd.DataFrame,
) -> Optional[Dict[str, Any]]:
    if candidate.get("daily_route") != "TREND_CONTINUATION" or _safe_float(candidate.get("signal_strength")) < 70:
        return None
    if len(day) < 20:
        return None
    reference_close = _safe_float(candidate.get("close"), _safe_float(candidate.get("pre_close")))
    if reference_close <= 0:
        return None
    atr = min(max(_safe_float(candidate.get("atr14_pct"), 0.05), 0.015), 0.15)
    pullback_min = min(max(0.08 * atr, 0.003), 0.008)
    pullback_max = min(max(0.35 * atr, 0.012), 0.030)
    gap_cap = min(max(0.16 * atr, 0.006), 0.012)

    armed_index: Optional[int] = None
    for index, row in day.iterrows():
        clock = row["eob"].strftime("%H:%M")
        if clock < "09:35" or clock > "14:40":
            continue
        intraday_return = _safe_float(row["close"]) / reference_close - 1.0
        vwap_gap = _safe_float(row["close"]) / _safe_float(row["vwap"]) - 1.0
        if (
            0.015 <= intraday_return <= 0.07
            and 0.008 <= vwap_gap <= 0.035
            and _safe_float(row.get("minute_return_3")) > 0
        ):
            armed_index = int(index)
            break
    if armed_index is None:
        return None
    baseline_execution = _next_minute_open(day, armed_index)
    if baseline_execution is None:
        return None
    baseline_index, baseline_price = baseline_execution

    peak = _safe_float(day.iloc[armed_index]["high"])
    pullback_low: Optional[float] = None
    pullback_index: Optional[int] = None
    reclaim_signal_index: Optional[int] = None
    reclaim_mtf: Dict[str, Any] = {}
    for index in range(armed_index + 1, len(day) - 1):
        row = day.iloc[index]
        if row["eob"].strftime("%H:%M") > "14:50":
            break
        peak = max(peak, _safe_float(row["high"]))
        close = _safe_float(row["close"])
        retrace = (peak - close) / peak if peak > 0 else 0.0
        if pullback_min <= retrace <= pullback_max and close >= _safe_float(row["vwap"]) * 0.995:
            if pullback_index is None:
                pullback_index = index
                pullback_low = close
            else:
                pullback_low = min(_safe_float(pullback_low, close), close)
        if pullback_index is None or pullback_low is None or index - pullback_index < 1:
            continue
        if retrace > pullback_max or close < _safe_float(row["vwap"]) * 0.992:
            pullback_index = None
            pullback_low = None
            continue
        reclaim_level = pullback_low + 0.40 * (peak - pullback_low)
        vwap_gap = close / _safe_float(row["vwap"]) - 1.0
        momentum_3 = close / _safe_float(day.iloc[max(0, index - 3)]["close"]) - 1.0
        if close >= reclaim_level and close >= _safe_float(row["vwap"]) * 0.999 and momentum_3 > 0 and vwap_gap <= gap_cap:
            mtf = _period_contexts(periods, pd.Timestamp(row["eob"]))
            if mtf["trigger_confirmed"] and mtf["score"] >= 70 and mtf["bearish_count"] <= 1:
                reclaim_signal_index = index
                reclaim_mtf = mtf
                break
    trade_date = str(day.iloc[0]["trade_date"])
    event = {
        "symbol": symbol,
        "name": name,
        "trade_date": trade_date,
        "daily_asof": candidate.get("signal_date"),
        "daily_strength": candidate.get("signal_strength"),
        "capital_structure": candidate.get("capital_structure") or {},
        "capital_rank_adjustment": candidate.get("capital_rank_adjustment"),
        "atr14_pct": atr,
        "armed_signal_ts": pd.Timestamp(day.iloc[armed_index]["eob"]).isoformat(),
        "armed_signal_price": _safe_float(day.iloc[armed_index]["close"]),
        "armed_vwap_gap": _safe_float(day.iloc[armed_index]["close"]) / _safe_float(day.iloc[armed_index]["vwap"]) - 1.0,
        "baseline_entry_ts": pd.Timestamp(day.iloc[baseline_index]["eob"]).isoformat(),
        "baseline_entry_price": baseline_price,
        "reclaim_found": reclaim_signal_index is not None,
        "pullback_min": pullback_min,
        "pullback_max": pullback_max,
        "entry_vwap_gap_cap": gap_cap,
    }
    event.update(_path_metrics(
        symbol, trade_date, baseline_index, baseline_price, minute_by_date, daily, benchmark, "baseline",
    ))
    if reclaim_signal_index is not None:
        reclaim_execution = _next_minute_open(day, reclaim_signal_index)
        if reclaim_execution is not None:
            reclaim_index, reclaim_price = reclaim_execution
            event.update({
                "reclaim_signal_ts": pd.Timestamp(day.iloc[reclaim_signal_index]["eob"]).isoformat(),
                "reclaim_signal_price": _safe_float(day.iloc[reclaim_signal_index]["close"]),
                "reclaim_entry_ts": pd.Timestamp(day.iloc[reclaim_index]["eob"]).isoformat(),
                "reclaim_entry_price": reclaim_price,
                "reclaim_vwap_gap": _safe_float(day.iloc[reclaim_signal_index]["close"]) / _safe_float(day.iloc[reclaim_signal_index]["vwap"]) - 1.0,
                "reclaim_mtf_score": reclaim_mtf.get("score"),
                "reclaim_mtf_supportive_count": reclaim_mtf.get("supportive_count"),
                "reclaim_mtf_bearish_count": reclaim_mtf.get("bearish_count"),
            })
            event.update(_path_metrics(
                symbol, trade_date, reclaim_index, reclaim_price, minute_by_date, daily, benchmark, "reclaim",
            ))
        else:
            event["reclaim_found"] = False
    return event


def build_study() -> Dict[str, Any]:
    pool = load_pool()
    taxonomy = load_taxonomy()
    daily_frames = {symbol: _load_frame(DAILY_ROOT / f"{symbol}_1d.pkl") for symbol in pool}
    minute_frames = {symbol: _prepare_minute(_load_frame(MINUTE_ROOT / f"{symbol}_1m.pkl")) for symbol in pool}
    benchmark = _load_frame(DAILY_ROOT / "SHSE.000300_1d.pkl")
    benchmark = benchmark[(benchmark["eob"].dt.strftime("%Y-%m-%d") >= START_DATE)]

    minute_by_symbol_date: Dict[str, Dict[str, pd.DataFrame]] = {}
    period_by_symbol: Dict[str, Dict[int, pd.DataFrame]] = {}
    all_dates = set()
    for symbol, frame in minute_frames.items():
        if frame.empty or "trade_date" not in frame.columns:
            minute_by_symbol_date[symbol] = {}
            period_by_symbol[symbol] = {minutes: pd.DataFrame() for minutes in (5, 15, 30)}
            continue
        minute_by_symbol_date[symbol] = {
            key: group.reset_index(drop=True)
            for key, group in frame.groupby("trade_date", sort=True)
        }
        all_dates.update(minute_by_symbol_date[symbol])
        period_by_symbol[symbol] = {
            minutes: aggregate_completed_minutes(frame, minutes)
            for minutes in (5, 15, 30)
        }
    trade_dates = sorted(date for date in all_dates if START_DATE <= date <= END_DATE)
    daily_calendar = sorted(set(
        date for frame in daily_frames.values()
        for date in frame["eob"].dt.strftime("%Y-%m-%d").tolist()
        if date <= END_DATE
    ))
    builder = DailyCandidateBuilder(pool, taxonomy)
    events: List[Dict[str, Any]] = []
    candidate_day_count = 0
    for trade_date in trade_dates:
        prior_dates = [date for date in daily_calendar if date < trade_date]
        if not prior_dates:
            continue
        asof = prior_dates[-1]
        payload = builder.build(daily_frames, asof=asof)
        for candidate in payload.get("candidates", []):
            if candidate.get("daily_route") != "TREND_CONTINUATION" or _safe_float(candidate.get("signal_strength")) < 70:
                continue
            symbol = str(candidate.get("symbol"))
            day = minute_by_symbol_date.get(symbol, {}).get(trade_date)
            if day is None or day.empty:
                continue
            candidate_day_count += 1
            event = _study_symbol_day(
                symbol,
                str(candidate.get("name") or taxonomy.get("symbols", {}).get(symbol, {}).get("name") or ""),
                candidate,
                day,
                period_by_symbol[symbol],
                minute_by_symbol_date[symbol],
                daily_frames[symbol],
                benchmark,
            )
            if event:
                events.append(event)

    reclaim_events = [row for row in events if row.get("reclaim_found") and row.get("reclaim_entry_price")]
    result = {
        "generated_at": datetime.now().isoformat(),
        "period": {"start": START_DATE, "end": END_DATE},
        "pool_size": len(pool),
        "pool_recorded_after_study_period": True,
        "candidate_stock_days": candidate_day_count,
        "armed_event_count": len(events),
        "reclaim_event_count": len(reclaim_events),
        "reclaim_conversion_rate": len(reclaim_events) / len(events) if events else None,
        "baseline_summary": _summary(events, "baseline"),
        "reclaim_summary": _summary(reclaim_events, "reclaim"),
        "events": events,
        "no_lookahead": {
            "daily": "D日只用D-1及以前日线",
            "intraday": "当前完整分钟确认，下一分钟开盘成交代理",
            "benchmark": "沪深300使用当日开盘作为分钟入场的近似基准起点",
        },
        "limitations": [
            "29只精选池在研究区间结束后记录，存在事后选池/幸存者偏差，不能证明普适盈利。",
            "历史五档、集合竞价路径和全市场板块快照不可用，本研究只验证入场位置与分钟技术确认。",
            "1分钟OHLC无法复原Tick内先后顺序；信号分钟收盘确认并用下一分钟开盘代理成交。",
            "不计手续费、滑点、涨跌停排队和实际仓位管理。",
        ],
    }
    return result


def _pct(value: Any) -> str:
    return "NA" if value is None else f"{float(value):+.2%}"


def _summary_table(summary: Dict[str, Any]) -> List[str]:
    lines = [
        "| 期限 | 样本 | 平均 | 中位 | 胜率 | PF | 平均沪深300超额* |",
        "|---|---:|---:|---:|---:|---:|---:|",
    ]
    for horizon in ("d0", "d1", "d3", "d5"):
        row = summary.get(horizon) or {}
        pf = row.get("profit_factor")
        lines.append(
            f"| {horizon.upper()} | {row.get('n', 0)} | {_pct(row.get('mean'))} | {_pct(row.get('median'))} | "
            f"{_pct(row.get('win_rate'))} | {'NA' if pf is None else f'{pf:.2f}'} | {_pct(row.get('mean_excess_vs_csi300_open_proxy'))} |"
        )
    return lines


def render_markdown(result: Dict[str, Any]) -> str:
    baseline = result["baseline_summary"]
    reclaim = result["reclaim_summary"]
    lines = [
        "# V7高位武装—回踩收复历史研究",
        "",
        f"区间：{START_DATE} 至 {END_DATE}；精选池：{result['pool_size']}只；日线趋势候选股票日：{result['candidate_stock_days']}。",
        "",
        f"高位ARMED事件：{result['armed_event_count']}；完成回踩收复且分钟多周期确认：{result['reclaim_event_count']}；转换率：{_pct(result['reclaim_conversion_rate'])}。",
        "",
        "## A. 高位出现即按下一分钟开盘买入（位置基线）",
        "",
        *_summary_table(baseline),
        "",
        f"1日路径：平均MFE {_pct(baseline['path_1d']['mean_mfe'])}；平均MAE {_pct(baseline['path_1d']['mean_mae'])}。",
        "",
        "## B. 受控回踩—收复—多周期确认后，下一分钟开盘买入",
        "",
        *_summary_table(reclaim),
        "",
        f"1日路径：平均MFE {_pct(reclaim['path_1d']['mean_mfe'])}；平均MAE {_pct(reclaim['path_1d']['mean_mae'])}。",
        "",
        "*沪深300只有日线，本表用信号当日指数开盘作为分钟入场的近似起点；只作方向参考。*",
        "",
        "## 事前边界与限制",
        "",
        *[f"- {item}" for item in result["limitations"]],
        "",
    ]
    return "\n".join(lines)


def main() -> None:
    result = build_study()
    REPORT_JSON.write_text(json.dumps(result, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    REPORT_MD.write_text(render_markdown(result), encoding="utf-8")
    print(json.dumps({
        "json": str(REPORT_JSON),
        "markdown": str(REPORT_MD),
        "candidate_stock_days": result["candidate_stock_days"],
        "armed_event_count": result["armed_event_count"],
        "reclaim_event_count": result["reclaim_event_count"],
        "baseline_summary": result["baseline_summary"],
        "reclaim_summary": result["reclaim_summary"],
    }, ensure_ascii=False, indent=2, default=str))


if __name__ == "__main__":
    main()
