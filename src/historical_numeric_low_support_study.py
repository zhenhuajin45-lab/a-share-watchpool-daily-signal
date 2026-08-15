# coding: utf-8
"""特殊数字低点支撑A/B/C影子规则的严格事前历史对照。

确认日收盘后信号才可见，下一交易日开盘作为入场代理。特殊数字事件与完全相同的
普通局部低点支撑事件对照，用于判断“特殊数字”本身是否有增量价值。
"""

from __future__ import annotations

import json
import math
from collections import defaultdict
from datetime import datetime
from pathlib import Path
from statistics import mean, median
from typing import Any, Dict, List, Optional, Sequence, Tuple

import pandas as pd

from numeric_pattern_plugin import classify_numeric_patterns, infer_price_precision


ROOT = Path(r"D:\codex\a_share_rotation")
DAILY_ROOT = ROOT / "data" / "goldminer" / "daily_adjust_prev_current"
BENCHMARK_PATH = ROOT / "data" / "goldminer" / "daily_adjust_prev_20210101_20260807" / "SHSE.000300_1d.pkl"
START = "2026-05-11"
END = "2026-08-07"


def _safe(value: Any, default: float = 0.0) -> float:
    try:
        number = float(value)
        return number if math.isfinite(number) else default
    except (TypeError, ValueError):
        return default


def _prepare(path: Path) -> pd.DataFrame:
    frame = pd.read_pickle(path).copy()
    frame["eob"] = pd.to_datetime(frame["eob"], errors="coerce")
    frame = frame.dropna(subset=["eob"]).sort_values("eob").drop_duplicates("eob", keep="last")
    for column in ("open", "high", "low", "close"):
        frame[column] = pd.to_numeric(frame[column], errors="coerce")
    previous = frame["close"].shift(1)
    tr = pd.concat([
        frame["high"] - frame["low"],
        (frame["high"] - previous).abs(),
        (frame["low"] - previous).abs(),
    ], axis=1).max(axis=1)
    frame["atr14"] = tr.rolling(14, min_periods=5).mean()
    frame["date"] = frame["eob"].dt.strftime("%Y-%m-%d")
    return frame.reset_index(drop=True)


def _variant(first_three_held: Sequence[bool], retests: Sequence[bool]) -> List[Tuple[str, int]]:
    result: List[Tuple[str, int]] = []
    if len(first_three_held) >= 3 and all(first_three_held[:3]) and all(retests[:3]):
        result.append(("A_STRICT_3_CONSECUTIVE_RETESTS", 2))
    if len(first_three_held) >= 3 and all(first_three_held[:3]) and sum(retests[:3]) >= 2:
        result.append(("B_3DAY_OBSERVE_2_VALID_RETESTS", 2))
    count = 0
    for offset, (held, retested) in enumerate(zip(first_three_held[:10], retests[:10])):
        if not held:
            break
        count += int(retested)
        if count >= 3:
            result.append(("C_10DAY_3_VALID_RETESTS", offset))
            break
    return result


def _detect(frame: pd.DataFrame, symbol: str) -> List[Dict[str, Any]]:
    precision = infer_price_precision(symbol)
    tick_size = 10 ** (-precision)
    events: Dict[Tuple[int, str], Dict[str, Any]] = {}
    for anchor in range(5, len(frame) - 3):
        anchor_date = str(frame.iloc[anchor]["date"])
        if anchor_date > END:
            break
        anchor_low = round(_safe(frame.iloc[anchor]["low"]), precision)
        if anchor_low <= 0:
            continue
        prior5 = frame.iloc[max(0, anchor - 4):anchor + 1]
        if anchor_low > round(_safe(prior5["low"].min()), precision):
            continue
        atr = _safe(frame.iloc[anchor]["atr14"])
        if atr <= 0:
            continue
        band_pct = min(0.012, max(2.0 * tick_size / anchor_low, 0.25 * atr / anchor_low))
        upper = anchor_low * (1.0 + band_pct)
        later = frame.iloc[anchor + 1:min(len(frame), anchor + 11)]
        held, retests, accepted = [], [], []
        for offset, (_, row) in enumerate(later.iterrows()):
            low = round(_safe(row["low"]), precision)
            is_held = low >= anchor_low
            held.append(is_held)
            previous_close = _safe(frame.iloc[anchor + offset]["close"])
            approached = previous_close >= anchor_low * (1.0 + 0.25 * band_pct)
            row_open, row_high, row_close = map(_safe, (row["open"], row["high"], row["close"]))
            lower_wick = max(0.0, min(row_open, row_close) - low)
            day_range = max(tick_size, row_high - low)
            acceptance = bool(
                row_close >= anchor_low * (1.0 + 0.25 * band_pct)
                and (row_close >= row_open or lower_wick / day_range >= 0.30)
            )
            accepted.append(acceptance)
            retests.append(bool(is_held and low <= upper and approached and acceptance))
        classification = classify_numeric_patterns(anchor_low, precision)
        for variant, confirmation_offset in _variant(held, retests):
            confirmation_index = anchor + 1 + confirmation_offset
            if confirmation_index >= len(frame):
                continue
            confirmation_date = str(frame.iloc[confirmation_index]["date"])
            if not (START <= confirmation_date <= END):
                continue
            events[(anchor, variant)] = {
                "symbol": symbol, "anchor_index": anchor, "anchor_date": anchor_date,
                "anchor_low": anchor_low, "confirmation_index": confirmation_index,
                "confirmation_date": confirmation_date, "variant": variant,
                "special_numeric": bool(classification["has_tag"]),
                "primary_pattern": classification.get("primary_pattern"),
                "band_pct": band_pct, "atr14": atr,
                "valid_retest_count": sum(retests[:confirmation_offset + 1]),
                "no_lookahead": True,
            }
    return list(events.values())


def _attach_outcomes(event: Dict[str, Any], frame: pd.DataFrame, benchmark: pd.DataFrame) -> Optional[Dict[str, Any]]:
    entry_index = int(event["confirmation_index"]) + 1
    if entry_index >= len(frame):
        return None
    entry_price = _safe(frame.iloc[entry_index]["open"])
    entry_date = str(frame.iloc[entry_index]["date"])
    if entry_price <= 0:
        return None
    benchmark_map = {str(row["date"]): index for index, row in benchmark.iterrows()}
    benchmark_index = benchmark_map.get(entry_date)
    benchmark_open = _safe(benchmark.iloc[benchmark_index]["open"]) if benchmark_index is not None else 0.0
    result = {**event, "entry_date": entry_date, "entry_price": entry_price}
    for days, offset in ((1, 0), (3, 2), (5, 4), (10, 9)):
        target = entry_index + offset
        if target >= len(frame):
            result[f"return_{days}d"] = None
            result[f"excess_{days}d"] = None
            continue
        stock_return = _safe(frame.iloc[target]["close"]) / entry_price - 1.0
        result[f"return_{days}d"] = stock_return
        benchmark_target = benchmark_index + offset if benchmark_index is not None else None
        if benchmark_target is not None and benchmark_target < len(benchmark) and benchmark_open > 0:
            benchmark_return = _safe(benchmark.iloc[benchmark_target]["close"]) / benchmark_open - 1.0
            result[f"excess_{days}d"] = stock_return - benchmark_return
        else:
            result[f"excess_{days}d"] = None
    end5 = min(len(frame), entry_index + 5)
    path = frame.iloc[entry_index:end5]
    result["mae_5d"] = _safe(path["low"].min()) / entry_price - 1.0 if len(path) else None
    result["mfe_5d"] = _safe(path["high"].max()) / entry_price - 1.0 if len(path) else None
    return result


def _summary(rows: Sequence[Dict[str, Any]], key: str = "return_5d") -> Dict[str, Any]:
    values = [_safe(row[key]) for row in rows if row.get(key) is not None]
    gains = sum(value for value in values if value > 0)
    losses = -sum(value for value in values if value < 0)
    excess = [_safe(row["excess_5d"]) for row in rows if row.get("excess_5d") is not None]
    maes = [_safe(row["mae_5d"]) for row in rows if row.get("mae_5d") is not None]
    return {
        "samples": len(values), "mean": mean(values) if values else None,
        "median": median(values) if values else None,
        "win_rate": sum(value > 0 for value in values) / len(values) if values else None,
        "profit_factor": gains / losses if losses > 0 else None,
        "mean_excess": mean(excess) if excess else None,
        "mean_mae_5d": mean(maes) if maes else None,
    }


def main() -> None:
    benchmark = _prepare(BENCHMARK_PATH)
    all_events: List[Dict[str, Any]] = []
    for path in sorted(DAILY_ROOT.glob("*_1d.pkl")):
        symbol = path.stem[:-3]
        frame = _prepare(path)
        for event in _detect(frame, symbol):
            outcome = _attach_outcomes(event, frame, benchmark)
            if outcome:
                all_events.append(outcome)
    groups = {
        "special_all": [row for row in all_events if row["special_numeric"]],
        "ordinary_all": [row for row in all_events if not row["special_numeric"]],
    }
    for variant in ("A_STRICT_3_CONSECUTIVE_RETESTS", "B_3DAY_OBSERVE_2_VALID_RETESTS", "C_10DAY_3_VALID_RETESTS"):
        groups[f"special_{variant}"] = [row for row in groups["special_all"] if row["variant"] == variant]
        groups[f"ordinary_{variant}"] = [row for row in groups["ordinary_all"] if row["variant"] == variant]
    summaries = {name: _summary(rows) for name, rows in groups.items()}
    payload = {
        "generated_at": datetime.now().isoformat(), "period": [START, END],
        "pool_size": len(list(DAILY_ROOT.glob("*_1d.pkl"))),
        "event_count": len(all_events), "summaries": summaries, "events": all_events,
        "no_lookahead": True,
        "limitations": [
            "当前33只池在研究期结束后才形成，存在事后选池/幸存者偏差",
            "特殊数字规则是玄学旁路假设，本研究只检验是否有统计增量，不赋交易权重",
            "确认日收盘后才可见，下一交易日开盘为成交代理；不计手续费与滑点",
            "同一锚点可同时命中A/B/C，三版本比较不应相加为独立样本",
        ],
    }
    out_root = ROOT / "reports" / "numeric_low_support_study_20260812"
    out_root.mkdir(parents=True, exist_ok=True)
    (out_root / "result.json").write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    lines = [
        "# 特殊数字低点支撑A/B/C历史对照", "",
        f"区间：{START}至{END}；池：当前33只（有事后选池偏差）；确认日收盘后可见，次日开盘代理进入。", "",
        "| 分组 | 样本 | 5日平均 | 中位 | 胜率 | PF | 沪深300超额 | 5日MAE |", "|---|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for name, summary in summaries.items():
        def pct(value: Any) -> str:
            return "NA" if value is None else f"{value:+.2%}"
        pf = "NA" if summary["profit_factor"] is None else f"{summary['profit_factor']:.2f}"
        lines.append(
            f"| {name} | {summary['samples']} | {pct(summary['mean'])} | {pct(summary['median'])} | "
            f"{pct(summary['win_rate'])} | {pf} | {pct(summary['mean_excess'])} | {pct(summary['mean_mae_5d'])} |"
        )
    lines.extend(["", "## 边界", ""] + [f"- {value}" for value in payload["limitations"]])
    (out_root / "report.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(json.dumps({"output": str(out_root), "events": len(all_events), "summaries": summaries}, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
