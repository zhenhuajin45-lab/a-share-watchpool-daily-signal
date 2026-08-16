#!/usr/bin/env python3
"""Build GM-sourced full-market sentiment and sector-cycle evidence.

This adapter is read-only.  GM supplies the A-share universe, daily bars,
historical price limits, SW2021 industries, concept memberships and money flow.
Kaipanla is deliberately not required for the deterministic output.
"""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import math
import os
import re
import sys
from pathlib import Path
from typing import Any, Iterable

import numpy as np
import pandas as pd


A_SHARE_MARKET_SECTOR = "001004"
CONCEPT_SECTOR_TYPE = "1003"
NON_ATTACK_CONCEPT = re.compile(
    r"(?:AB股|AH股|B股|基金|债券|转债|沪股通|深股通|融资融券|指数|成份|上证|深证|沪深|标普|MSCI|富时|同股同权|注册制|送转|预盈预增|预亏预减|破净|高股息|低价股|高价股|ST股|昨日|近期新高|百日新高|历史新高|强势股|连板|涨停|跌停|龙虎榜|大宗交易|增持|减持|解禁|回购|股权转让|举牌|次新股|热股|热门股|人气股|高振幅|高换手|竞价|首板|反包|炸板)",
    re.I,
)


def chunks(values: list[str], size: int) -> Iterable[list[str]]:
    for index in range(0, len(values), size):
        yield values[index : index + size]


def clamp(value: float, low: float = 0.0, high: float = 100.0) -> float:
    return max(low, min(value, high))


def finite(value: Any, default: float = 0.0) -> float:
    try:
        result = float(value)
    except (TypeError, ValueError):
        return default
    return result if math.isfinite(result) else default


def compact_date(value: Any) -> str:
    text = "".join(character for character in str(value or "") if character.isdigit())
    return text[:8]


def iso_date(value: str) -> str:
    return f"{value[:4]}-{value[4:6]}-{value[6:8]}"


def frame_or_empty(value: Any) -> pd.DataFrame:
    if isinstance(value, pd.DataFrame):
        return value.copy()
    if value is None:
        return pd.DataFrame()
    return pd.DataFrame(value)


def safe_concat(frames: list[pd.DataFrame]) -> pd.DataFrame:
    valid = [frame for frame in frames if isinstance(frame, pd.DataFrame) and not frame.empty]
    return pd.concat(valid, ignore_index=True) if valid else pd.DataFrame()


def last_return_samples(
    symbols: set[str],
    latest: pd.DataFrame,
) -> float | None:
    if not symbols or latest.empty:
        return None
    rows = latest[latest["symbol"].isin(symbols)]
    if rows.empty:
        return None
    values = pd.to_numeric(rows["return_pct"], errors="coerce").dropna()
    return round(float(values.mean()), 4) if not values.empty else None


def at_limit(price: Any, limit_price: Any, tick: Any = 0.01) -> bool:
    price_value, limit_value = finite(price, -1), finite(limit_price, -1)
    tolerance = max(finite(tick, 0.01) / 2.0, 0.0001)
    return price_value > 0 and limit_value > 0 and price_value >= limit_value - tolerance


def at_lower_limit(price: Any, limit_price: Any, tick: Any = 0.01) -> bool:
    price_value, limit_value = finite(price, -1), finite(limit_price, -1)
    tolerance = max(finite(tick, 0.01) / 2.0, 0.0001)
    return price_value > 0 and limit_value > 0 and price_value <= limit_value + tolerance


def build_market_sentiment(
    bars: pd.DataFrame,
    instruments: pd.DataFrame,
    trade_dates: list[str],
    universe_count: int,
) -> dict[str, Any]:
    if bars.empty or instruments.empty or len(trade_dates) < 3:
        return {"status": "UNAVAILABLE", "trade_date": trade_dates[-1] if trade_dates else None}
    frame = bars.copy()
    frame["trade_date"] = frame["eob"].map(compact_date)
    frame["close"] = pd.to_numeric(frame["close"], errors="coerce")
    frame["pre_close"] = pd.to_numeric(frame["pre_close"], errors="coerce")
    frame["return_pct"] = (frame["close"] / frame["pre_close"] - 1.0) * 100.0
    inst = instruments.copy()
    inst["trade_date"] = inst["trade_date"].map(compact_date)
    selected_dates = trade_dates[-3:]
    daily: dict[str, pd.DataFrame] = {}
    for date in selected_dates:
        day_bars = frame[frame["trade_date"] == date].copy()
        day_inst = inst[inst["trade_date"] == date][["symbol", "upper_limit", "lower_limit", "price_tick", "is_suspended"]].drop_duplicates("symbol")
        daily[date] = day_bars.merge(day_inst, on="symbol", how="left")
    current, previous, previous2 = daily[selected_dates[-1]], daily[selected_dates[-2]], daily[selected_dates[-3]]
    active = current[current["is_suspended"].fillna(0).astype(float) == 0].copy()
    rise_count = int((active["return_pct"] > 0.0001).sum())
    fall_count = int((active["return_pct"] < -0.0001).sum())
    flat_count = int(len(active) - rise_count - fall_count)
    active["is_limit_up"] = active.apply(lambda row: at_limit(row.get("close"), row.get("upper_limit"), row.get("price_tick")), axis=1)
    active["is_limit_down"] = active.apply(lambda row: at_lower_limit(row.get("close"), row.get("lower_limit"), row.get("price_tick")), axis=1)
    active["is_break_board"] = active.apply(
        lambda row: at_limit(row.get("high"), row.get("upper_limit"), row.get("price_tick")) and not at_limit(row.get("close"), row.get("upper_limit"), row.get("price_tick")),
        axis=1,
    )
    for value in (previous, previous2):
        value["is_limit_up"] = value.apply(lambda row: at_limit(row.get("close"), row.get("upper_limit"), row.get("price_tick")), axis=1)
        value["is_break_board"] = value.apply(
            lambda row: at_limit(row.get("high"), row.get("upper_limit"), row.get("price_tick")) and not at_limit(row.get("close"), row.get("upper_limit"), row.get("price_tick")),
            axis=1,
        )
    previous_limit_symbols = set(previous.loc[previous["is_limit_up"], "symbol"].astype(str))
    previous2_limit_symbols = set(previous2.loc[previous2["is_limit_up"], "symbol"].astype(str))
    chain_symbols = previous_limit_symbols & previous2_limit_symbols
    previous_break_symbols = set(previous.loc[previous["is_break_board"], "symbol"].astype(str))
    yesterday_limit_return = last_return_samples(previous_limit_symbols, active)
    yesterday_chain_return = last_return_samples(chain_symbols, active)
    yesterday_break_return = last_return_samples(previous_break_symbols, active)
    amount_current = float(pd.to_numeric(active.get("amount"), errors="coerce").fillna(0).sum())
    amount_previous = float(pd.to_numeric(previous.get("amount"), errors="coerce").fillna(0).sum())
    turnover_change = ((amount_current / amount_previous) - 1.0) * 100.0 if amount_previous > 0 else None
    limit_up_count = int(active["is_limit_up"].sum())
    limit_down_count = int(active["is_limit_down"].sum())
    break_count = int(active["is_break_board"].sum())
    touched_limit_up = limit_up_count + break_count
    break_rate = break_count / touched_limit_up * 100.0 if touched_limit_up else 0.0
    breadth_score = 50.0 if rise_count + fall_count == 0 else rise_count / (rise_count + fall_count) * 100.0
    limit_score = 50.0 if limit_up_count + limit_down_count == 0 else clamp(50 + (limit_up_count - limit_down_count) / (limit_up_count + limit_down_count) * 45)
    profit_values = [value for value in (yesterday_limit_return, yesterday_chain_return, yesterday_break_return) if value is not None]
    profit_score = clamp(50.0 + (sum(profit_values) / len(profit_values) if profit_values else 0.0) * 7.0)
    turnover_score = clamp(50.0 + finite(turnover_change) * 1.5)
    break_score = clamp(100.0 - break_rate * 2.0)
    composite = round(0.35 * breadth_score + 0.25 * limit_score + 0.20 * profit_score + 0.10 * turnover_score + 0.10 * break_score, 2)
    coverage = len(active) / universe_count if universe_count else 0.0
    return {
        "schema_version": "gm_market_sentiment_v1",
        "status": "OK" if coverage >= 0.90 else "PARTIAL",
        "source": "GM_FULL_A_SHARE_DAILY_RECOMPUTED",
        "trade_date": selected_dates[-1],
        "composite_strength": composite,
        "breadth": {"rise_count": rise_count, "fall_count": fall_count, "flat_count": flat_count, "active_count": len(active), "universe_count": universe_count, "coverage": round(coverage, 4)},
        "turnover": {"amount_yi": round(amount_current / 100_000_000.0, 2), "previous_amount_yi": round(amount_previous / 100_000_000.0, 2), "change_pct": round(turnover_change, 2) if turnover_change is not None else None},
        "limit_structure": {
            "limit_up_count": limit_up_count,
            "limit_down_count": limit_down_count,
            "break_board_count": break_count,
            "break_board_rate_pct": round(break_rate, 2),
            "yesterday_limit_up_return_pct": yesterday_limit_return,
            "yesterday_chain_return_pct": yesterday_chain_return,
            "yesterday_break_return_pct": yesterday_break_return,
            "previous_limit_up_sample_size": len(previous_limit_symbols),
            "previous_chain_sample_size": len(chain_symbols),
            "previous_break_sample_size": len(previous_break_symbols),
        },
        "calculation": {"price_adjustment": "ADJUST_NONE", "limit_prices": "GM_get_history_instruments", "missing_is_bearish": False},
    }


def build_symbol_features(bars: pd.DataFrame, money_flow: pd.DataFrame, trade_dates: list[str]) -> pd.DataFrame:
    frame = bars.copy()
    frame["trade_date"] = frame["eob"].map(compact_date)
    for column in ("close", "pre_close", "amount"):
        frame[column] = pd.to_numeric(frame[column], errors="coerce")
    frame["return_pct"] = (frame["close"] / frame["pre_close"] - 1.0) * 100.0
    frame = frame.sort_values(["symbol", "trade_date"])
    latest_date = trade_dates[-1]
    rows: list[dict[str, Any]] = []
    flow = money_flow.copy()
    if not flow.empty:
        flow["trade_date"] = flow["trade_date"].map(compact_date)
        flow["main_net_in"] = pd.to_numeric(flow["main_net_in"], errors="coerce").fillna(0.0)
    for symbol, part in frame.groupby("symbol", sort=False):
        part = part.dropna(subset=["close"]).sort_values("trade_date")
        if part.empty or str(part.iloc[-1]["trade_date"]) != latest_date:
            continue
        latest = part.iloc[-1]
        base = part.iloc[-6]["close"] if len(part) >= 6 else np.nan
        prior_amount = part.iloc[-6:-1]["amount"].dropna() if len(part) >= 6 else pd.Series(dtype=float)
        amount_ratio = float(latest["amount"] / prior_amount.mean()) if not prior_amount.empty and prior_amount.mean() > 0 else 1.0
        symbol_flow = flow[flow["symbol"] == symbol] if not flow.empty else pd.DataFrame()
        current_flow = float(symbol_flow.loc[symbol_flow["trade_date"] == latest_date, "main_net_in"].sum()) if not symbol_flow.empty else 0.0
        rows.append({
            "symbol": symbol,
            "current_return_pct": finite(latest["return_pct"]),
            "interval_return_pct": ((float(latest["close"]) / float(base)) - 1.0) * 100.0 if pd.notna(base) and base > 0 else 0.0,
            "amount": finite(latest["amount"]),
            "amount_ratio_5d": amount_ratio,
            "current_main_net": current_flow,
            "interval_main_net": float(symbol_flow["main_net_in"].sum()) if not symbol_flow.empty else 0.0,
            "net_inflow_days": int((symbol_flow["main_net_in"] > 0).sum()) if not symbol_flow.empty else 0,
        })
    return pd.DataFrame(rows)


def sector_rows(
    features: pd.DataFrame,
    memberships: pd.DataFrame,
    group_type: str,
    minimum_members: int,
    maximum_members: int,
) -> list[dict[str, Any]]:
    if features.empty or memberships.empty:
        return []
    merged = memberships[["symbol", "sector_code", "sector_name"]].drop_duplicates().merge(features, on="symbol", how="inner")
    output: list[dict[str, Any]] = []
    for (code, name), part in merged.groupby(["sector_code", "sector_name"], sort=False):
        name_text = str(name)
        member_count = int(part["symbol"].nunique())
        if member_count < minimum_members or member_count > maximum_members:
            continue
        if group_type == "CONCEPT" and NON_ATTACK_CONCEPT.search(name_text):
            continue
        current_return = float(part["current_return_pct"].median())
        interval_return = float(part["interval_return_pct"].median())
        breadth = float((part["current_return_pct"] > 0).mean())
        amount_ratio = float(part["amount_ratio_5d"].replace([np.inf, -np.inf], np.nan).dropna().median())
        amount_yi = float(part["amount"].sum() / 100_000_000.0)
        current_net_yi = float(part["current_main_net"].sum() / 100_000_000.0)
        interval_net_yi = float(part["interval_main_net"].sum() / 100_000_000.0)
        inflow_days = int(round(float(part["net_inflow_days"].median())))
        flow_rate = current_net_yi / amount_yi * 100.0 if amount_yi > 0 else 0.0
        score = (
            0.30 * clamp(50 + current_return * 10)
            + 0.20 * clamp(50 + interval_return * 5)
            + 0.20 * breadth * 100
            + 0.15 * clamp(50 + (amount_ratio - 1.0) * 50)
            + 0.15 * clamp(50 + flow_rate * 5)
        )
        output.append({
            "sector_code": str(code),
            "sector_name": name_text,
            "group_type": group_type,
            "member_count": member_count,
            "observed_count": len(part),
            "current_return_pct": round(current_return, 4),
            "interval_return_pct": round(interval_return, 4),
            "up_ratio": round(breadth, 4),
            "amount_yi": round(amount_yi, 2),
            "amount_ratio_5d": round(amount_ratio, 4),
            "current_main_net_yi": round(current_net_yi, 2),
            "interval_net_yi": round(interval_net_yi, 2),
            "net_inflow_days": inflow_days,
            "main_net_rate_pct": round(flow_rate, 4),
            "validation_score": int(round(clamp(score))),
        })
    return output


def finish_sector_cycle(rows: list[dict[str, Any]], trade_date: str, maximum_output: int) -> dict[str, Any]:
    rows.sort(key=lambda row: (-int(row["validation_score"]), -finite(row["current_return_pct"]), str(row["sector_name"])))
    for rank, row in enumerate(rows, 1):
        row["current_rank"] = rank
        current, interval = finite(row["current_return_pct"]), finite(row["interval_return_pct"])
        flow, breadth, amount_ratio = finite(row["current_main_net_yi"]), finite(row["up_ratio"]), finite(row["amount_ratio_5d"], 1.0)
        if interval >= 8.0 and current >= 1.0 and amount_ratio >= 1.4:
            stage = "CLIMAX"
        elif rank <= 12 and current >= 0.5 and interval >= 1.5 and flow > 0 and breadth >= 0.55:
            stage = "ACCELERATION"
        elif rank <= 20 and current > 0 and flow >= 0 and breadth >= 0.50:
            stage = "STARTUP"
        elif interval > 0 and (current < 0 or flow < 0):
            stage = "DIVERGENCE"
        elif current < 0 and interval < 0:
            stage = "RETREAT"
        else:
            stage = "FADE"
        row["stage"] = stage
        row["cycle_day"] = max(1, min(5, int(row.get("net_inflow_days") or 0)))
        row["validation_state"] = "CONFIRMED" if int(row["validation_score"]) >= 60 else "WATCH"
        row["intraday_rhythm"] = "PERSISTENT" if current > 0 and flow > 0 else ("DISTRIBUTING" if current > 0 and flow < 0 else ("FADING" if current < 0 else "MIXED"))
        row["source"] = "GM_RECOMPUTED"
    selected = rows[:maximum_output]
    return {
        "schema_version": "gm_sector_cycle_v1",
        "status": "READY" if selected else "UNAVAILABLE",
        "source": "GM_SW2021_L1_AND_CONCEPT_RECOMPUTED",
        "trade_date": trade_date,
        "sectors": selected,
        "policy": {
            "industry_membership_point_in_time": True,
            "concept_membership_point_in_time": False,
            "concepts_not_for_historical_replay_without_snapshot": True,
            "kaipanla_required": False,
            "non_thematic_concepts_filtered": True,
        },
    }


def write_json_atomic(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2, default=str) + "\n", encoding="utf-8")
    temporary.replace(path)


def evidence_file(frame: pd.DataFrame, path: Path) -> dict[str, Any]:
    path.parent.mkdir(parents=True, exist_ok=True)
    frame.to_csv(path, index=False, encoding="utf-8-sig", compression="gzip")
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    return {"path": str(path), "rows": len(frame), "sha256": digest}


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--trade-date", required=True, help="Closed A-share trade date, YYYYMMDD")
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--evidence-dir", type=Path, required=True)
    parser.add_argument("--lookback", type=int, default=10)
    parser.add_argument("--batch-size", type=int, default=250)
    parser.add_argument("--max-sectors", type=int, default=120)
    parser.add_argument("--include-concepts", action="store_true")
    args = parser.parse_args()
    trade_date = compact_date(args.trade_date)
    payload: dict[str, Any] = {
        "schema_version": "gm_market_breadth_sector_bundle_v1",
        "trade_date": trade_date,
        "captured_at": dt.datetime.now().astimezone().isoformat(),
        "status": "UNAVAILABLE",
        "market_sentiment": {},
        "sector_cycle": {},
        "source_health": {"token_present": bool(os.environ.get("GM_TOKEN")), "errors": []},
        "evidence": {},
    }
    try:
        from gm.api import (  # type: ignore
            ADJUST_NONE,
            get_history_instruments,
            get_trading_dates,
            history,
            set_token,
            stk_get_money_flow,
            stk_get_sector_constituents,
            stk_get_symbol_industry,
            stk_get_symbol_sector,
        )

        token = os.environ.get("GM_TOKEN", "").strip()
        if not token:
            raise RuntimeError("GM_TOKEN missing")
        set_token(token)
        end_date = dt.datetime.strptime(trade_date, "%Y%m%d").date()
        calendar = get_trading_dates(exchange="SHSE", start_date=end_date - dt.timedelta(days=45), end_date=end_date)
        dates = sorted({compact_date(value) for value in calendar if compact_date(value)})
        if not dates or dates[-1] != trade_date:
            raise RuntimeError("trade_date is not the latest closed SHSE trading date in requested range")
        dates = dates[-max(args.lookback, 8) :]
        universe = frame_or_empty(stk_get_sector_constituents(A_SHARE_MARKET_SECTOR))
        symbols = sorted(set(universe.get("symbol", pd.Series(dtype=str)).astype(str)))
        if len(symbols) < 4000:
            raise RuntimeError(f"A-share universe unexpectedly small: {len(symbols)}")

        bars_frames: list[pd.DataFrame] = []
        instrument_frames: list[pd.DataFrame] = []
        industry_frames: list[pd.DataFrame] = []
        concept_frames: list[pd.DataFrame] = []
        money_frames: list[pd.DataFrame] = []
        start_time = f"{iso_date(dates[0])} 00:00:00"
        end_time = f"{iso_date(trade_date)} 15:30:00"
        for batch_index, batch in enumerate(chunks(symbols, args.batch_size)):
            try:
                bars_frames.append(frame_or_empty(history(symbol=batch, frequency="1d", start_time=start_time, end_time=end_time, fields="symbol,eob,open,high,low,close,pre_close,amount,volume", skip_suspended=True, adjust=ADJUST_NONE, df=True)))
                instrument_frames.append(frame_or_empty(get_history_instruments(batch, start_date=iso_date(dates[-3]), end_date=iso_date(trade_date), df=True)))
                industry = frame_or_empty(stk_get_symbol_industry(batch, source="sw2021", level=1, date=iso_date(trade_date)))
                if not industry.empty:
                    industry_frames.append(industry.rename(columns={"industry_code": "sector_code", "industry_name": "sector_name"})[["symbol", "sector_code", "sector_name"]])
                if args.include_concepts:
                    concept_frames.append(frame_or_empty(stk_get_symbol_sector(batch, sector_type=CONCEPT_SECTOR_TYPE))[["symbol", "sector_code", "sector_name"]])
            except Exception as exc:
                payload["source_health"]["errors"].append(f"batch:{batch_index}:{type(exc).__name__}:{str(exc)[:160]}")
        for trade_day in dates[-5:]:
            for batch_index, batch in enumerate(chunks(symbols, args.batch_size)):
                try:
                    money_frames.append(frame_or_empty(stk_get_money_flow(batch, trade_date=iso_date(trade_day))))
                except Exception as exc:
                    payload["source_health"]["errors"].append(f"money:{trade_day}:{batch_index}:{type(exc).__name__}:{str(exc)[:120]}")

        bars = safe_concat(bars_frames)
        instruments = safe_concat(instrument_frames)
        industries = safe_concat(industry_frames)
        concepts = safe_concat(concept_frames)
        money_flow = safe_concat(money_frames)
        sentiment = build_market_sentiment(bars, instruments, dates, len(symbols))
        features = build_symbol_features(bars, money_flow, dates)
        sectors = sector_rows(features, industries, "SW2021_L1", 20, 1500)
        if args.include_concepts:
            sectors.extend(sector_rows(features, concepts, "CONCEPT", 8, 1500))
        sector_cycle = finish_sector_cycle(sectors, trade_date, args.max_sectors)
        prefix = args.evidence_dir / trade_date
        payload["evidence"] = {
            "universe": evidence_file(universe, prefix / "universe.csv.gz"),
            "bars": evidence_file(bars, prefix / "bars.csv.gz"),
            "instruments": evidence_file(instruments, prefix / "instruments.csv.gz"),
            "industries": evidence_file(industries, prefix / "industries.csv.gz"),
            "concepts": evidence_file(concepts, prefix / "concepts.csv.gz") if args.include_concepts else None,
            "money_flow": evidence_file(money_flow, prefix / "money_flow.csv.gz"),
        }
        bar_coverage = int(features["symbol"].nunique()) / len(symbols) if len(symbols) else 0.0
        industry_coverage = int(industries["symbol"].nunique()) / len(symbols) if not industries.empty else 0.0
        money_coverage = int(money_flow.loc[money_flow["trade_date"].map(compact_date) == trade_date, "symbol"].nunique()) / len(symbols) if not money_flow.empty else 0.0
        checks = {
            "calendar_trade_date_exact": dates[-1] == trade_date,
            "universe_complete": len(symbols) >= 4000,
            "bar_coverage": bar_coverage >= 0.90,
            "industry_coverage": industry_coverage >= 0.90,
            "money_flow_coverage": money_coverage >= 0.85,
            "market_sentiment_ready": sentiment.get("status") == "OK",
            "sector_cycle_ready": sector_cycle.get("status") == "READY",
        }
        payload.update({"market_sentiment": sentiment, "sector_cycle": sector_cycle})
        payload["source_health"].update({
            "checks": checks,
            "ready": all(checks.values()) and not payload["source_health"]["errors"],
            "universe_count": len(symbols),
            "bar_symbol_count": int(features["symbol"].nunique()) if not features.empty else 0,
            "bar_coverage": round(bar_coverage, 4),
            "industry_coverage": round(industry_coverage, 4),
            "money_flow_coverage": round(money_coverage, 4),
            "trading_dates": dates,
            "query_contract": {"adjust": "ADJUST_NONE", "industry": "sw2021_level1", "concept_sector_type": CONCEPT_SECTOR_TYPE if args.include_concepts else None},
        })
        payload["status"] = "READY" if payload["source_health"]["ready"] else "PARTIAL"
    except Exception as exc:
        payload["source_health"]["errors"].append(f"fatal:{type(exc).__name__}:{str(exc)[:400]}")
    write_json_atomic(args.output, payload)
    print(json.dumps({"status": payload["status"], "ready": payload["source_health"].get("ready", False), "trade_date": trade_date, "universe_count": payload["source_health"].get("universe_count"), "bar_coverage": payload["source_health"].get("bar_coverage"), "industry_coverage": payload["source_health"].get("industry_coverage"), "money_flow_coverage": payload["source_health"].get("money_flow_coverage"), "errors": payload["source_health"].get("errors", [])[:3], "output": str(args.output)}, ensure_ascii=False))
    exit_code = 0 if payload["status"] == "READY" else 2
    sys.stdout.flush()
    sys.stderr.flush()
    os._exit(exit_code)


if __name__ == "__main__":
    main()
