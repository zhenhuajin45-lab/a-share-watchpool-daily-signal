# coding: utf-8
"""当前精选池信号的简单毛收益检查，不作参数搜索。"""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

import numpy as np
import pandas as pd

from signal_rules import classify_daily_signal, compute_features, resample_monthly


ROOT = Path(r"D:\codex\a_share_rotation")
CACHE = ROOT / "data" / "goldminer" / "daily_adjust_prev_20210101_20260807"
SIGNAL_REPORT = ROOT / "reports" / "signal_history_invariant_audit_202606_202607.json"
OUTPUT_JSON = ROOT / "reports" / "simple_profit_check_20260810.json"
OUTPUT_MD = ROOT / "reports" / "simple_profit_check_20260810.md"
END_DATE = "2026-08-07"


def prepare(frame: pd.DataFrame) -> pd.DataFrame:
    result = frame.copy()
    result["eob"] = pd.to_datetime(result["eob"])
    result["date"] = result["eob"].dt.strftime("%Y-%m-%d")
    return result.sort_values("eob").reset_index(drop=True)


def price_return(frame: pd.DataFrame, entry_index: int, offset: int) -> float | None:
    target = entry_index + offset
    if target >= len(frame):
        return None
    return float(frame.iloc[target]["close"] / frame.iloc[entry_index]["open"] - 1.0)


def first_exit_after(frame: pd.DataFrame, signal_date: str) -> dict | None:
    for asof in frame.loc[frame["date"] > signal_date, "date"]:
        raw = frame[frame["date"] <= asof]
        features = compute_features(raw)
        if features is None:
            continue
        signal = classify_daily_signal(features, resample_monthly(features), sector_state=0, sector_confidence="LOW")
        if signal and signal["action"] == "EXIT":
            index = frame.index[frame["date"] == asof].tolist()[0]
            if index + 1 < len(frame):
                return {
                    "signal_date": asof,
                    "execution_date": frame.iloc[index + 1]["date"],
                    "execution_price": float(frame.iloc[index + 1]["open"]),
                    "reason": signal["reason"],
                }
    return None


def benchmark_return(benchmark: pd.DataFrame, entry_date: str, offset: int) -> float | None:
    matches = benchmark.index[benchmark["date"] == entry_date].tolist()
    if not matches or matches[0] + offset >= len(benchmark):
        return None
    index = matches[0]
    return float(benchmark.iloc[index + offset]["close"] / benchmark.iloc[index]["open"] - 1.0)


def benchmark_between(benchmark: pd.DataFrame, entry_date: str, exit_date: str, exit_at_open: bool) -> float | None:
    entries = benchmark.index[benchmark["date"] == entry_date].tolist()
    exits = benchmark.index[benchmark["date"] == exit_date].tolist()
    if not entries or not exits:
        return None
    entry_price = float(benchmark.iloc[entries[0]]["open"])
    exit_price = float(benchmark.iloc[exits[0]]["open" if exit_at_open else "close"])
    return exit_price / entry_price - 1.0


def main() -> None:
    audit = json.loads(SIGNAL_REPORT.read_text(encoding="utf-8"))
    benchmark = prepare(pd.read_pickle(CACHE / "SHSE.000300_1d.pkl"))
    events = []
    frames = {}

    for raw_signal in audit.get("buy_signals", []):
        symbol = raw_signal["symbol"]
        frame = frames.setdefault(symbol, prepare(pd.read_pickle(CACHE / f"{symbol}_1d.pkl")))
        signal_index = frame.index[frame["date"] == raw_signal["signal_date"]].tolist()[0]
        entry_index = signal_index + 1
        entry_date = frame.iloc[entry_index]["date"]
        entry_price = float(frame.iloc[entry_index]["open"])
        row = {
            "signal_date": raw_signal["signal_date"],
            "symbol": symbol,
            "name": raw_signal.get("name"),
            "entry_date": entry_date,
            "entry_price": entry_price,
        }
        for offset in (0, 1, 3, 5):
            stock_return = price_return(frame, entry_index, offset)
            hs300_return = benchmark_return(benchmark, entry_date, offset)
            row[f"stock_t{offset}"] = stock_return
            row[f"hs300_t{offset}"] = hs300_return
            row[f"excess_t{offset}"] = stock_return - hs300_return if stock_return is not None and hs300_return is not None else None
        end_index = min(entry_index + 5, len(frame) - 1)
        window = frame.iloc[entry_index:end_index + 1]
        row["mfe_5d"] = float(window["high"].max() / entry_price - 1.0)
        row["mae_5d"] = float(window["low"].min() / entry_price - 1.0)
        events.append(row)

    horizon_summary = {}
    for offset in (0, 1, 3, 5):
        returns = [row[f"stock_t{offset}"] for row in events if row[f"stock_t{offset}"] is not None]
        excess = [row[f"excess_t{offset}"] for row in events if row[f"excess_t{offset}"] is not None]
        horizon_summary[f"t{offset}"] = {
            "samples": len(returns),
            "mean_return": float(np.mean(returns)) if returns else None,
            "median_return": float(np.median(returns)) if returns else None,
            "win_rate": float(np.mean(np.asarray(returns) > 0)) if returns else None,
            "mean_excess_vs_hs300": float(np.mean(excess)) if excess else None,
            "excess_win_rate": float(np.mean(np.asarray(excess) > 0)) if excess else None,
        }

    # 简单组合：同一股票持有期间忽略重复BUY；每个新标的固定使用初始资金10%，其余为现金。
    open_symbols = set()
    trades = []
    for event in events:
        if event["symbol"] in open_symbols:
            trades.append({**event, "portfolio_action": "SKIPPED_DUPLICATE_WHILE_HELD"})
            continue
        open_symbols.add(event["symbol"])
        frame = frames[event["symbol"]]
        exit_info = first_exit_after(frame, event["signal_date"])
        if exit_info:
            exit_date = exit_info["execution_date"]
            exit_price = exit_info["execution_price"]
            exit_at_open = True
            status = "EXIT_SIGNAL"
            open_symbols.remove(event["symbol"])
        else:
            marked = frame[frame["date"] <= END_DATE].iloc[-1]
            exit_date = marked["date"]
            exit_price = float(marked["close"])
            exit_at_open = False
            status = "MARK_TO_END_NO_EXIT_SIGNAL"
        gross_return = exit_price / event["entry_price"] - 1.0
        hs300_return = benchmark_between(benchmark, event["entry_date"], exit_date, exit_at_open)
        trades.append({
            **event,
            "portfolio_action": "OPENED",
            "status": status,
            "exit": exit_info,
            "valuation_date": exit_date,
            "valuation_price": exit_price,
            "gross_return": gross_return,
            "hs300_same_period_return": hs300_return,
            "excess_same_period": gross_return - hs300_return if hs300_return is not None else None,
        })

    opened = [row for row in trades if row["portfolio_action"] == "OPENED"]
    fixed_ten_percent_account_return = sum(0.10 * row["gross_return"] for row in opened)
    equal_trade_mean = float(np.mean([row["gross_return"] for row in opened])) if opened else None
    result = {
        "generated_at": datetime.now().isoformat(),
        "rules_version": audit.get("rules_version"),
        "period": {"signal_start": audit["scope"]["start"], "signal_end": audit["scope"]["end"], "valuation_end": END_DATE},
        "assumptions": {
            "fees": 0,
            "slippage": 0,
            "entry": "D日收盘信号，下一交易日开盘",
            "exit": "首个EXIT信号的下一交易日开盘；没有EXIT则按2026-08-07收盘估值",
            "t_plus_one": True,
            "duplicate_buy_while_held": "ignored_in_portfolio; retained_in_signal_event_study",
            "benchmark": "SHSE.000300 沪深300，同日开盘至同期限",
            "intraday_filter": "not applied in daily event study; current minute replay generated zero entries",
        },
        "time_integrity": audit.get("time_integrity"),
        "signal_events": events,
        "horizon_summary": horizon_summary,
        "portfolio_trades": trades,
        "portfolio_summary": {
            "opened_unique_trades": len(opened),
            "skipped_duplicate_signals": sum(row["portfolio_action"] != "OPENED" for row in trades),
            "equal_trade_mean_gross_return": equal_trade_mean,
            "fixed_10pct_per_new_symbol_account_return": fixed_ten_percent_account_return,
            "closed_by_exit_signal": sum(row.get("status") == "EXIT_SIGNAL" for row in opened),
        },
        "verdict": "NOT_PROVEN" if len(events) < 20 else "REQUIRES_REVIEW",
        "verdict_reason": "BUY样本仅3次且股票池为事后精选池；只能观察方向，不能证明赚钱能力。",
    }
    OUTPUT_JSON.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")

    lines = [
        "# A股轮动简单赚钱能力检查",
        "",
        "口径：不计手续费和滑点；D日收盘确认，下一交易日开盘买入；严格T+1；与沪深300同期限比较。",
        "",
        "## 短周期信号结果",
        "",
        "| 期限 | 样本 | 平均收益 | 胜率 | 平均超额 | 超额胜率 |",
        "|---|---:|---:|---:|---:|---:|",
    ]
    for key in ("t0", "t1", "t3", "t5"):
        row = horizon_summary[key]
        lines.append(
            f"| {key}收盘 | {row['samples']} | {row['mean_return']:.2%} | {row['win_rate']:.1%} | "
            f"{row['mean_excess_vs_hs300']:.2%} | {row['excess_win_rate']:.1%} |"
        )
    lines.extend(["", "## 单次信号", ""])
    for row in events:
        lines.append(
            f"- {row['signal_date']} {row['name']}：T+1={row['stock_t1']:.2%}，T+3={row['stock_t3']:.2%}，"
            f"T+5={row['stock_t5']:.2%}；5日MFE={row['mfe_5d']:.2%}，MAE={row['mae_5d']:.2%}。"
        )
    ps = result["portfolio_summary"]
    lines.extend([
        "",
        "## 简单组合",
        "",
        f"- 实际新开标的：{ps['opened_unique_trades']}个；持有期间重复信号忽略：{ps['skipped_duplicate_signals']}次。",
        f"- 两笔独立交易等资金平均毛收益：{ps['equal_trade_mean_gross_return']:.2%}。",
        f"- 每个新标的固定使用初始资金10%、其余现金：账户毛收益{ps['fixed_10pct_per_new_symbol_account_return']:.2%}。",
        f"- 样本结束前由EXIT信号平仓：{ps['closed_by_exit_signal']}笔。",
        "",
        "## 判断",
        "",
        "当前不能证明有稳定赚钱能力。样本只有3个BUY，其中天华新能重复一次；精选池又是事后池。短周期是否存在一点正向迹象要看实际数字，但不能据此调参或下结论。",
        "分钟事件过滤器在这3个候选上全部没有入场，因此其历史收益暂时是0交易、0收益，也没有统计意义。",
    ])
    OUTPUT_MD.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(json.dumps({"json": str(OUTPUT_JSON), "markdown": str(OUTPUT_MD), "horizons": horizon_summary, "portfolio": result["portfolio_summary"]}, ensure_ascii=False))


if __name__ == "__main__":
    main()
