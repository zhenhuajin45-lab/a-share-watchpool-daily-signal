# coding: utf-8
"""V4日线候选的探索性事件研究。

股票池在2026-08-09才记录，因此本报告只回答“规则放宽后信号位置如何”，不能
证明普适收益。D日收盘生成候选，D+1开盘进入；普通A股最早在再下一交易日退出。
"""

from __future__ import annotations

import argparse
import json
import math
from collections import Counter, defaultdict
from pathlib import Path
from statistics import mean, median
from typing import Any, Dict, Iterable, List

import numpy as np
import pandas as pd

from live_signal_service import load_pool, load_taxonomy
from sector_context import build_group_returns, select_sector_context
from signal_rules import classify_daily_signal, compute_features, resample_monthly


ROOT = Path(r"D:\codex\a_share_rotation")
CACHE = ROOT / "data" / "goldminer" / "daily_adjust_prev_20210101_20260807"


def _safe_float(value: Any, default: float = 0.0) -> float:
    try:
        result = float(value)
        return result if math.isfinite(result) else default
    except (TypeError, ValueError):
        return default


def _dates(frame: pd.DataFrame) -> List[str]:
    return pd.to_datetime(frame["eob"], errors="coerce").dt.strftime("%Y-%m-%d").tolist()


def _profit_factor(values: Iterable[float]) -> float | None:
    values = list(values)
    gains = sum(value for value in values if value > 0)
    losses = -sum(value for value in values if value < 0)
    return gains / losses if losses > 0 else (float("inf") if gains > 0 else None)


def _summary(rows: List[Dict[str, Any]], key: str) -> Dict[str, Any]:
    values = [row[key] for row in rows if row.get(key) is not None]
    excess = [row[f"excess_{key}"] for row in rows if row.get(f"excess_{key}") is not None]
    net = [value - 0.002 for value in values]
    return {
        "events": len(values),
        "win_rate": float(np.mean(np.asarray(values) > 0)) if values else None,
        "mean_gross": mean(values) if values else None,
        "median_gross": median(values) if values else None,
        "profit_factor_gross": _profit_factor(values),
        "mean_net_after_assumed_20bp": mean(net) if net else None,
        "median_net_after_assumed_20bp": median(net) if net else None,
        "mean_excess_vs_hs300": mean(excess) if excess else None,
        "excess_win_rate": float(np.mean(np.asarray(excess) > 0)) if excess else None,
    }


def _build_snapshot(feature_frames: Dict[str, pd.DataFrame], taxonomy: Dict[str, Any], asof: str) -> List[Dict[str, Any]]:
    frames = {}
    returns = {}
    for symbol, full in feature_frames.items():
        frame = full[full["date"] <= asof]
        if frame.empty or frame.iloc[-1]["date"] != asof:
            continue
        frames[symbol] = frame
        if len(frame) >= 6:
            returns[symbol] = _safe_float(frame.iloc[-1]["close"] / frame.iloc[-6]["close"] - 1.0)
    groups = build_group_returns(returns, taxonomy)
    candidates = []
    for symbol, frame in frames.items():
        sector = select_sector_context(symbol, groups, taxonomy)
        signal = classify_daily_signal(
            frame,
            resample_monthly(frame),
            sector_state=sector["state"],
            sector_confidence=sector["confidence"],
        )
        if not signal:
            continue
        row = dict(taxonomy.get("symbols", {}).get(symbol, {}))
        row.update({
            "symbol": symbol,
            "group_key": sector["key"],
            "group_source": sector["source"],
        })
        row.update(signal)
        candidates.append(row)
    return candidates


def run(start: str, end: str) -> Dict[str, Any]:
    pool = load_pool()
    taxonomy = load_taxonomy()
    frames = {symbol: pd.read_pickle(CACHE / f"{symbol}_1d.pkl") for symbol in pool}
    benchmark = pd.read_pickle(CACHE / "SHSE.000300_1d.pkl").copy()
    for frame in [*frames.values(), benchmark]:
        frame["date"] = pd.to_datetime(frame["eob"], errors="coerce").dt.strftime("%Y-%m-%d")
        frame.sort_values("date", inplace=True)
        frame.reset_index(drop=True, inplace=True)
    benchmark_index = {row["date"]: index for index, row in benchmark.iterrows()}
    feature_frames = {}
    for symbol, raw in frames.items():
        feature = compute_features(raw)
        if feature is not None:
            feature["date"] = pd.to_datetime(feature["eob"], errors="coerce").dt.strftime("%Y-%m-%d")
            feature_frames[symbol] = feature
    trade_dates = [value for value in benchmark["date"].tolist() if start <= value <= end]
    events = []
    action_counts = Counter()
    route_counts = Counter()
    for asof in trade_dates:
        candidates = _build_snapshot(feature_frames, taxonomy, asof)
        action_counts.update(row.get("action") for row in candidates)
        route_counts.update(row.get("daily_route") for row in candidates)
        for candidate in candidates:
            if candidate.get("action") != "BUY" or not candidate.get("intraday_eligible"):
                continue
            symbol = candidate["symbol"]
            frame = frames[symbol]
            indices = frame.index[frame["date"] == asof].tolist()
            if not indices:
                continue
            signal_index = indices[-1]
            entry_index = signal_index + 1
            if entry_index >= len(frame):
                continue
            entry_date = frame.iloc[entry_index]["date"]
            benchmark_entry_index = benchmark_index.get(entry_date)
            if benchmark_entry_index is None:
                continue
            entry_price = _safe_float(frame.iloc[entry_index]["open"])
            benchmark_entry = _safe_float(benchmark.iloc[benchmark_entry_index]["open"])
            if entry_price <= 0 or benchmark_entry <= 0:
                continue
            event = {
                "signal_date": asof,
                "entry_date": entry_date,
                "symbol": symbol,
                "name": candidate.get("name", ""),
                "route": candidate.get("daily_route"),
                "signal_strength": candidate.get("signal_strength"),
                "slow_j": candidate.get("slow_j"),
                "sector_state": candidate.get("sector_state"),
                "sector_confidence": candidate.get("sector_confidence"),
                "entry_price": entry_price,
            }
            for offset, label in ((1, "t1_sellable"), (3, "t3"), (5, "t5")):
                stock_exit_index = entry_index + offset
                benchmark_exit_index = benchmark_entry_index + offset
                if stock_exit_index < len(frame) and benchmark_exit_index < len(benchmark):
                    stock_return = _safe_float(frame.iloc[stock_exit_index]["close"]) / entry_price - 1.0
                    benchmark_return = _safe_float(benchmark.iloc[benchmark_exit_index]["close"]) / benchmark_entry - 1.0
                    event[label] = stock_return
                    event[f"excess_{label}"] = stock_return - benchmark_return
                else:
                    event[label] = None
                    event[f"excess_{label}"] = None
            end_index = min(entry_index + 5, len(frame) - 1)
            window = frame.iloc[entry_index:end_index + 1]
            event["mfe_5d"] = _safe_float(window["high"].max()) / entry_price - 1.0
            event["mae_5d"] = _safe_float(window["low"].min()) / entry_price - 1.0
            events.append(event)

    horizons = {key: _summary(events, key) for key in ("t1_sellable", "t3", "t5")}
    by_route = {}
    for route in sorted({row["route"] for row in events}):
        subset = [row for row in events if row["route"] == route]
        by_route[route] = {key: _summary(subset, key) for key in ("t1_sellable", "t3", "t5")}
    by_strength = {}
    for label, low, high in (("80_100", 80, 101), ("70_79", 70, 80), ("0_69", 0, 70)):
        subset = [row for row in events if low <= int(_safe_float(row["signal_strength"])) < high]
        by_strength[label] = {key: _summary(subset, key) for key in ("t1_sellable", "t3", "t5")}
    return {
        "scope": {
            "start": start,
            "end": end,
            "pool": "selected_pool_recorded_2026-08-09_post_selected",
            "entry": "signal_D_close_then_D_plus_1_open",
            "t1_sellable": "entry_day_plus_1_close; respects newly_bought_A_share_not_sellable_on_entry_day",
            "cost_assumption": "gross plus shadow net subtracting assumed 20bp round trip; not broker fee truth",
        },
        "action_counts": dict(action_counts),
        "route_counts": dict(route_counts),
        "event_count": len(events),
        "horizons": horizons,
        "by_route": by_route,
        "by_strength": by_strength,
        "events": events,
    }


def _pct(value: Any) -> str:
    return "NA" if value is None else f"{value:.2%}"


def render(result: Dict[str, Any]) -> str:
    lines = [
        "# A股轮动V4日线候选探索性事件研究",
        "",
        f"区间：{result['scope']['start']} 至 {result['scope']['end']}；事件数：{result['event_count']}",
        "",
        "> 重要限制：29只股票池在2026-08-09才记录，以下历史结果存在事后精选/幸存者偏差，只用于诊断V4是否把信号放得过宽，不能作为实盘盈利证明。",
        "",
        "## 总体",
        "",
        "| 最早卖出/观察期限 | 样本 | 胜率 | 平均毛收益 | 中位毛收益 | 毛PF | 假设20bp后平均 | 平均沪深300超额 |",
        "|---|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for key, label in (("t1_sellable", "买入次日收盘（最早可卖）"), ("t3", "第3日收盘"), ("t5", "第5日收盘")):
        row = result["horizons"][key]
        lines.append(
            f"| {label} | {row['events']} | {_pct(row['win_rate'])} | {_pct(row['mean_gross'])} | {_pct(row['median_gross'])} | "
            f"{row['profit_factor_gross'] if row['profit_factor_gross'] is not None else 'NA'} | {_pct(row['mean_net_after_assumed_20bp'])} | {_pct(row['mean_excess_vs_hs300'])} |"
        )
    lines.extend(["", "## 按路线（T+1最早可卖）", "", "| 路线 | 样本 | 胜率 | 平均毛收益 | 中位数 | PF | 假设20bp后平均 | 沪深300超额 |", "|---|---:|---:|---:|---:|---:|---:|---:|"])
    for route, values in result["by_route"].items():
        row = values["t1_sellable"]
        lines.append(f"| {route} | {row['events']} | {_pct(row['win_rate'])} | {_pct(row['mean_gross'])} | {_pct(row['median_gross'])} | {row['profit_factor_gross']} | {_pct(row['mean_net_after_assumed_20bp'])} | {_pct(row['mean_excess_vs_hs300'])} |")
    lines.extend(["", "## 按日线强度（T+1最早可卖）", "", "| 强度 | 样本 | 胜率 | 平均毛收益 | 中位数 | PF | 假设20bp后平均 |", "|---|---:|---:|---:|---:|---:|---:|"])
    for label, values in result["by_strength"].items():
        row = values["t1_sellable"]
        lines.append(f"| {label} | {row['events']} | {_pct(row['win_rate'])} | {_pct(row['mean_gross'])} | {_pct(row['median_gross'])} | {row['profit_factor_gross']} | {_pct(row['mean_net_after_assumed_20bp'])} |")
    lines.extend([
        "",
        "## 解释",
        "",
        "- BUY表示允许进入盘中事件确认，不代表次日开盘应直接成交；NEXT_OPEN仅用于衡量日线候选先验。",
        "- 若日线候选整体为正、盘中事件反而显著变差，问题主要在日内买点；若日线候选本身为负，则必须继续收缩日线路线。",
        "- 本区间已参与规则发现，不能继续充当独立样本外验证集。",
    ])
    return "\n".join(lines) + "\n"


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--start", default="2026-05-11")
    parser.add_argument("--end", default="2026-08-07")
    args = parser.parse_args()
    result = run(args.start, args.end)
    stamp = f"{args.start.replace('-', '')}_{args.end.replace('-', '')}"
    json_path = ROOT / "reports" / f"v4_daily_event_study_{stamp}.json"
    md_path = ROOT / "reports" / f"v4_daily_event_study_{stamp}.md"
    json_path.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    md_path.write_text(render(result), encoding="utf-8")
    print(json.dumps({"json": str(json_path), "markdown": str(md_path), "event_count": result["event_count"], "horizons": result["horizons"]}, ensure_ascii=False))


if __name__ == "__main__":
    main()
