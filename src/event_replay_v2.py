# coding: utf-8
"""用1分钟数据近似回放当前Tick事件状态机。

限制：分钟收盘价作为事件观察值，累计成交额/量用于VWAP；没有历史五档和竞价路径。
事件在当前分钟结束后确认，成交代理使用下一分钟开盘，避免同分钟偷看。
"""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

import pandas as pd

from intraday_engine import IntradayEventEngine
from live_signal_service import DailyCandidateBuilder, load_pool, load_taxonomy
from signal_rules import classify_daily_signal, compute_features, resample_monthly


ROOT = Path(r"D:\codex\a_share_rotation")
DAILY_CACHE = ROOT / "data" / "goldminer" / "daily_adjust_prev_20210101_20260807"
MINUTE_CACHE = ROOT / "data" / "goldminer" / "1m_20260511_20260807"
SIGNAL_AUDIT = ROOT / "reports" / "signal_history_invariant_audit_202606_202607.json"
OUTPUT_JSON = ROOT / "reports" / "event_replay_v2_202606_202607.json"
OUTPUT_MD = ROOT / "reports" / "event_replay_v2_202606_202607.md"


def daily_candidate_for_asof(symbol, asof, frames, builder):
    result = builder.build(frames, asof=asof)
    return next((row for row in result["candidates"] if row["symbol"] == symbol), None)


def held_candidate_for_trade_date(symbol, trade_date, daily_raw, entry):
    prior = daily_raw[pd.to_datetime(daily_raw["eob"]).dt.strftime("%Y-%m-%d") < trade_date]
    features = compute_features(prior)
    signal = classify_daily_signal(features, resample_monthly(features), sector_state=0, sector_confidence="LOW") if features is not None else None
    candidate = dict(signal or {})
    candidate.update({
        "symbol": symbol,
        "monitor_sell": True,
        "position_entry_date": entry["execution_date"],
        "position_entry_price": entry["execution_price"],
        "position_source": "MINUTE_REPLAY_VIRTUAL_POSITION",
    })
    if candidate.get("action") != "EXIT":
        candidate["action"] = "MONITOR_EXIT"
    return candidate


def pseudo_tick(row, cum_volume, cum_amount):
    return {
        "symbol": row["symbol"],
        "created_at": row["eob"],
        "price": float(row["close"]),
        "cum_volume": float(cum_volume),
        "cum_amount": float(cum_amount),
        "quotes": [],
    }


def prepare_minute(symbol):
    frame = pd.read_pickle(MINUTE_CACHE / f"{symbol}_1m.pkl").copy()
    frame["eob"] = pd.to_datetime(frame["eob"])
    frame["trade_date"] = frame["eob"].dt.strftime("%Y-%m-%d")
    return frame.sort_values("eob").reset_index(drop=True)


def replay_one(signal, daily_frames, builder):
    symbol = signal["symbol"]
    minute_path = MINUTE_CACHE / f"{symbol}_1m.pkl"
    if not minute_path.exists():
        return {**signal, "status": "NO_MINUTE_DATA"}
    candidate = daily_candidate_for_asof(symbol, signal["signal_date"], daily_frames, builder)
    if not candidate or candidate.get("action") != "BUY":
        return {**signal, "status": "SIGNAL_NOT_REPRODUCED"}

    minute = prepare_minute(symbol)
    trade_dates = sorted(date for date in minute["trade_date"].unique() if date > signal["signal_date"])
    if not trade_dates:
        return {**signal, "status": "NO_NEXT_TRADING_DAY"}
    entry_day = trade_dates[0]
    day = minute[minute["trade_date"] == entry_day].reset_index(drop=True)
    engine = IntradayEventEngine()
    cum_volume = cum_amount = 0.0
    buy_event = entry = None
    for index, row in day.iterrows():
        cum_volume += float(row.get("volume", 0) or 0)
        cum_amount += float(row.get("amount", 0) or 0)
        event = engine.on_tick(pseudo_tick(row, cum_volume, cum_amount), candidate, {"gate": "NEUTRAL", "label": "NO_HISTORICAL_AUCTION"})
        if event and index + 1 < len(day):
            buy_event = event
            execution = day.iloc[index + 1]
            entry = {
                "event_ts": event["event_ts"],
                "event_price": event["price"],
                "execution_date": entry_day,
                "execution_ts": execution["eob"].isoformat(),
                "execution_price": float(execution["open"]),
            }
            break
    if not entry:
        return {**signal, "status": "NO_INTRADAY_BUY_EVENT", "entry_day": entry_day}

    daily_raw = daily_frames[symbol]
    sell_event = exit_fill = None
    for trade_date in trade_dates[1:]:
        day = minute[minute["trade_date"] == trade_date].reset_index(drop=True)
        held_candidate = held_candidate_for_trade_date(symbol, trade_date, daily_raw, entry)
        cum_volume = cum_amount = 0.0
        for index, row in day.iterrows():
            cum_volume += float(row.get("volume", 0) or 0)
            cum_amount += float(row.get("amount", 0) or 0)
            event = engine.on_tick(pseudo_tick(row, cum_volume, cum_amount), held_candidate)
            if event and index + 1 < len(day):
                sell_event = event
                execution = day.iloc[index + 1]
                exit_fill = {
                    "event_ts": event["event_ts"],
                    "event_price": event["price"],
                    "pattern": event["pattern"],
                    "execution_date": trade_date,
                    "execution_ts": execution["eob"].isoformat(),
                    "execution_price": float(execution["open"]),
                }
                break
        if exit_fill:
            break

    result = {
        **signal,
        "status": "CLOSED" if exit_fill else "OPEN_AT_REPLAY_END",
        "buy_event": buy_event,
        "entry": entry,
        "sell_event": sell_event,
        "exit": exit_fill,
    }
    if exit_fill:
        result["gross_return"] = round(exit_fill["execution_price"] / entry["execution_price"] - 1.0, 6)
    else:
        final_close = float(minute.iloc[-1]["close"])
        result["mark_date"] = minute.iloc[-1]["trade_date"]
        result["mark_price"] = final_close
        result["gross_return_to_mark"] = round(final_close / entry["execution_price"] - 1.0, 6)
    return result


def main():
    audit = json.loads(SIGNAL_AUDIT.read_text(encoding="utf-8"))
    pool = load_pool()
    daily_frames = {symbol: pd.read_pickle(DAILY_CACHE / f"{symbol}_1d.pkl") for symbol in pool}
    builder = DailyCandidateBuilder(pool, load_taxonomy())
    results = [replay_one(signal, daily_frames, builder) for signal in audit.get("buy_signals", [])]
    payload = {
        "generated_at": datetime.now().isoformat(),
        "rules_version": audit.get("rules_version"),
        "scope": audit.get("scope"),
        "data_quality": {
            "bar_frequency": "1m",
            "event_observation": "minute_close",
            "fill_proxy": "next_minute_open",
            "vwap": "cumulative_minute_amount/cumulative_minute_volume",
            "order_book": "UNAVAILABLE",
            "auction": "UNAVAILABLE_NEUTRAL",
            "is_tick_grade": False,
        },
        "time_integrity": audit.get("time_integrity"),
        "results": results,
    }
    OUTPUT_JSON.write_text(json.dumps(payload, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    lines = [
        "# 事件驱动分钟近似回放 V2",
        "",
        "当前规则的日线BUY只在下一交易日逐分钟寻找“冲高—受控回撤—再收复”事件；事件分钟结束后确认，下一分钟开盘作成交代理。",
        "历史五档与竞价路径不可用，因此这不是Tick级结论。精选池也是事后池，不能证明收益普适性。",
        "",
    ]
    for row in results:
        entry = row.get("entry") or {}
        exit_fill = row.get("exit") or {}
        lines.append(
            f"- {row['signal_date']} {row['symbol']} {row.get('name','')}：{row['status']}；"
            f"入场={entry.get('execution_ts')}@{entry.get('execution_price')}；"
            f"退出={exit_fill.get('execution_ts')}@{exit_fill.get('execution_price')}({exit_fill.get('pattern')})；"
            f"毛收益={row.get('gross_return', row.get('gross_return_to_mark'))}"
        )
    OUTPUT_MD.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(json.dumps({"json": str(OUTPUT_JSON), "markdown": str(OUTPUT_MD), "statuses": [row["status"] for row in results]}, ensure_ascii=False))


if __name__ == "__main__":
    main()
