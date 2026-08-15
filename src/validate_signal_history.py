# coding: utf-8
"""对精选池执行逐日、事前截面的规则不变量审计。"""

from __future__ import annotations

import json
from collections import Counter
from datetime import datetime
from pathlib import Path

import pandas as pd

from live_signal_service import DailyCandidateBuilder, load_pool, load_taxonomy


ROOT = Path(r"D:\codex\a_share_rotation")
CACHE = ROOT / "data" / "goldminer" / "daily_adjust_prev_20210101_20260807"
REPORTS = ROOT / "reports"
START = "2026-06-01"
END = "2026-07-31"


def load_frames(pool):
    return {symbol: pd.read_pickle(CACHE / f"{symbol}_1d.pkl") for symbol in pool}


def trading_dates(frames):
    values = set()
    for frame in frames.values():
        values.update(str(value)[:10] for value in pd.to_datetime(frame["eob"]) if START <= str(value)[:10] <= END)
    return sorted(values)


def forward_outcome(signal, frame, actions_by_date):
    raw = frame.copy()
    raw["date"] = pd.to_datetime(raw["eob"]).dt.strftime("%Y-%m-%d")
    raw = raw.sort_values("eob").reset_index(drop=True)
    matches = raw.index[raw["date"] == signal["signal_date"]].tolist()
    if not matches or matches[0] + 1 >= len(raw):
        return {"entry_available": False}
    entry_index = matches[0] + 1
    entry_price = float(raw.iloc[entry_index]["open"])
    outcome = {
        "entry_available": True,
        "entry_rule": "next_trading_day_open_proxy_not_intraday_event_fill",
        "entry_date": raw.iloc[entry_index]["date"],
        "entry_price": round(entry_price, 4),
    }
    for offset in (0, 1, 3, 5):
        index = entry_index + offset
        outcome[f"return_to_close_t_plus_{offset}"] = (
            round(float(raw.iloc[index]["close"]) / entry_price - 1.0, 6) if index < len(raw) else None
        )
    later_exits = sorted(
        date for date, action in actions_by_date.items()
        if date > signal["signal_date"] and action == "EXIT"
    )
    if later_exits:
        exit_signal_date = later_exits[0]
        exit_matches = raw.index[raw["date"] == exit_signal_date].tolist()
        if exit_matches and exit_matches[0] + 1 < len(raw):
            execution_index = exit_matches[0] + 1
            exit_price = float(raw.iloc[execution_index]["open"])
            outcome.update({
                "first_exit_signal_date": exit_signal_date,
                "exit_execution_date": raw.iloc[execution_index]["date"],
                "exit_execution_price": round(exit_price, 4),
                "return_to_first_exit_next_open": round(exit_price / entry_price - 1.0, 6),
            })
    return outcome


def main():
    pool = load_pool()
    taxonomy = load_taxonomy()
    frames = load_frames(pool)
    builder = DailyCandidateBuilder(pool, taxonomy)
    dates = trading_dates(frames)
    totals = Counter()
    violations = []
    results_by_date = {}
    stale_by_date = {}

    for asof in dates:
        result = builder.build(frames, asof=asof)
        results_by_date[asof] = {row["symbol"]: row for row in result["candidates"]}
        stale_by_date[asof] = result.get("stale_symbols", [])
        for row in result["candidates"]:
            totals[row["action"]] += 1
            if row["signal_date"] != asof:
                violations.append({"date": asof, "symbol": row["symbol"], "rule": "CROSS_SECTION_DATE_MISMATCH", "actual": row["signal_date"]})
            if not row.get("feature_no_lookahead"):
                violations.append({"date": asof, "symbol": row["symbol"], "rule": "FEATURE_NO_LOOKAHEAD_FLAG_FALSE"})
            if row["action"] == "BUY":
                checks = {
                    "SLOW_J_ZONE": 30 <= row["slow_j"] <= 40,
                    "SLOW_CONFIRMED": row["slow_confirmed"],
                    "FAST_TRIGGER": row["fast_trigger"],
                    "MACD_EVIDENCE": row["macd_improving"] or row["macd_divergence"] == "BULLISH",
                    "NO_MACD_BEARISH_VETO": row["macd_divergence"] != "BEARISH",
                    "NOT_CHASING": row["not_chasing"],
                    "NO_SECTOR_VETO": not row["sector_blocks_buy"],
                    "NO_MONTHLY_VETO": not row["monthly_blocks_buy"],
                }
                for rule, passed in checks.items():
                    if not passed:
                        violations.append({"date": asof, "symbol": row["symbol"], "rule": f"BUY_{rule}"})
            if row["action"] == "EXIT" and not (
                row["slow_j_sell_zone"] or row["high_zone_rollover"] or row["bearish_divergence_exit"]
            ):
                violations.append({"date": asof, "symbol": row["symbol"], "rule": "EXIT_WITHOUT_EXIT_CONDITION"})

    buys = []
    for asof in dates:
        for symbol, row in results_by_date[asof].items():
            if row["action"] != "BUY":
                continue
            actions = {date: rows[symbol]["action"] for date, rows in results_by_date.items() if symbol in rows}
            buys.append({
                "signal_date": asof,
                "symbol": symbol,
                "name": row.get("name"),
                "slow_j": row["slow_j"],
                "macd_divergence": row["macd_divergence"],
                "monthly_state": row["monthly_state"],
                "monthly_confidence": row["monthly_confidence"],
                "sector_source": row["group_source"],
                **forward_outcome(row, frames[symbol], actions),
            })

    snapshots = {}
    for symbol in ("SHSE.600301", "SZSE.000831", "SHSE.603259", "SHSE.688222"):
        row = results_by_date.get("2026-07-03", {}).get(symbol)
        if row:
            snapshots[symbol] = {key: row.get(key) for key in (
                "name", "action", "status", "slow_k", "slow_d", "slow_j", "slow_confirmed",
                "fast_trigger", "macd_diff", "macd_hist", "macd_divergence",
                "monthly_state", "monthly_slow_j", "sector_state", "group_source", "reason",
            )}

    payload = {
        "generated_at": datetime.now().isoformat(),
        "scope": {"pool_size": len(pool), "start": START, "end": END, "trading_dates": len(dates)},
        "data": {
            "source": "GoldMiner history",
            "adjustment": "ADJUST_PREV/front-adjusted",
            "cache": str(CACHE),
            "point_in_time_note": "每个asof重新截断原始数据；策略分类只见asof及更早K线",
        },
        "time_integrity": {
            "feature_no_lookahead": True,
            "pool_recorded_on": "2026-08-09",
            "universe_point_in_time_for_test_window": False,
            "overall_no_lookahead": False,
            "admissible_use": "信号规则/买点位置审计",
            "inadmissible_use": "策略收益或普适价值证明",
        },
        "rules_version": "daily_signal_rules_v3",
        "action_totals": dict(totals),
        "buy_signals": buys,
        "stale_by_date": {date: rows for date, rows in stale_by_date.items() if rows},
        "invariant_violation_count": len(violations),
        "invariant_violations": violations,
        "july_03_snapshots": snapshots,
        "limitations": [
            "下一交易日开盘仅是日线可复核代理，不等于Tick事件引擎真实成交价",
            "本报告验证规则和时间边界，不构成收益能力结论",
            "Tick状态机当前通过合成路径测试，仍需真实逐笔回放和仿真盘观测",
            "板块为精选池内稳定分类代理，尚不是官方行业指数",
            "精选池在2026-08-09记录，反放到6—7月属于事后条件回放，不能作为无前视收益证据",
        ],
    }
    REPORTS.mkdir(parents=True, exist_ok=True)
    json_path = REPORTS / "signal_history_invariant_audit_202606_202607.json"
    md_path = REPORTS / "signal_history_invariant_audit_202606_202607.md"
    json_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")

    lines = [
        "# A股轮动信号历史不变量审计（2026-06至2026-07）",
        "",
        f"- 精选池：{len(pool)}个标的；交易日：{len(dates)}",
        f"- 动作总数：{dict(totals)}",
        f"- BUY：{len(buys)}次；价格特征规则/时间边界违规：{len(violations)}次",
        "- 数据：GoldMiner前复权日线；每个交易日重新按asof截断",
        "- 重要：票池于2026-08-09记录，6—7月回放是事后池条件研究，不能证明策略收益或普适性",
        "",
        "## BUY事件（下一交易日开盘仅作日线代理）",
        "",
    ]
    if buys:
        for row in buys:
            lines.append(
                f"- {row['signal_date']} {row['symbol']} {row['name']}：慢J={row['slow_j']}，"
                f"月线={row['monthly_state']}({row['monthly_confidence']})，"
                f"次日开盘={row.get('entry_price')}，T+3收盘收益={row.get('return_to_close_t_plus_3')}，"
                f"首个退出信号次日开盘收益={row.get('return_to_first_exit_next_open')}"
            )
    else:
        lines.append("- 无")
    lines.extend(["", "## 2026-07-03重点截面", ""])
    for symbol, row in snapshots.items():
        lines.append(
            f"- {symbol} {row['name']}：{row['action']}；慢K/D/J={row['slow_k']}/{row['slow_d']}/{row['slow_j']}；"
            f"慢线确认={row['slow_confirmed']}；月线={row['monthly_state']}；原因={row['reason']}"
        )
    lines.extend(["", "## 边界", "", *[f"- {item}" for item in payload["limitations"]]])
    md_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(json.dumps({"json": str(json_path), "markdown": str(md_path), "action_totals": dict(totals), "buys": len(buys), "violations": len(violations)}, ensure_ascii=False))


if __name__ == "__main__":
    main()
