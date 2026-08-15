# coding: utf-8
"""V8多周期资金行为引擎：按时间顺序重放单个交易日。

候选只使用D-1以前的前复权日线；分钟指标只读取已完成K线；日内资金状态只使用
当前Tick及之前的累计成交和五档盘口代理。事件发生后的路径统计仅用于复盘，绝不回灌信号。
"""

from __future__ import annotations

import argparse
import json
from collections import Counter
from datetime import datetime
from pathlib import Path
from statistics import mean, median
from typing import Any, Dict, List, Optional

import pandas as pd

from auction_path import AuctionPathAnalyzer
from capital_behavior_engine import CapitalBehaviorEngine
from continuation_engine import TrendContinuationAnalyzer
from intraday_engine import IntradayEventEngine
from live_signal_service import (
    DailyCandidateBuilder,
    DeepSeekAdvisor,
    FeishuNotifier,
    LiveSignalService,
    load_pool,
    load_taxonomy,
)
from multitimeframe_engine import MultiTimeframeIndicatorEngine
from review_today_v4 import ROOT, _path_stats, _post_event_stats, _raw_tick, _read_jsonl, _safe_float
from sector_health import LiveSectorHealthEngine


DAILY_ROOT = ROOT / "data" / "goldminer" / "daily_adjust_prev_current"
MINUTE_ROOT = ROOT / "data" / "goldminer" / "live_1m_seed"


def _clock(value: Any) -> str:
    return str(value or "")[11:19]


def _pct(value: Optional[float]) -> str:
    return "—" if value is None else f"{value:+.2%}"


def _load_daily(asof: str) -> Dict[str, Any]:
    pool = load_pool()
    taxonomy = load_taxonomy()
    frames: Dict[str, pd.DataFrame] = {}
    for symbol in pool:
        path = DAILY_ROOT / f"{symbol}_1d.pkl"
        if path.exists():
            frames[symbol] = pd.read_pickle(path)
    return DailyCandidateBuilder(pool, taxonomy).build(frames, asof=asof)


def _subset_stats(rows: List[Dict[str, Any]]) -> Dict[str, Any]:
    values = [row.get("to_latest") for row in rows if row.get("to_latest") is not None]
    mfe = [row.get("post_mfe") for row in rows if row.get("post_mfe") is not None]
    mae = [row.get("post_mae") for row in rows if row.get("post_mae") is not None]
    return {
        "count": len(rows),
        "positive_to_latest": sum(value > 0 for value in values),
        "mean_to_latest": mean(values) if values else None,
        "median_to_latest": median(values) if values else None,
        "mean_post_mfe": mean(mfe) if mfe else None,
        "mean_post_mae": mean(mae) if mae else None,
    }


def _load_old_summary(trade_date: str) -> Dict[str, Any]:
    path = ROOT / "reports" / f"today_v6_layered_cycles_replay_{trade_date.replace('-', '')}.json"
    if not path.exists():
        return {}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        return payload.get("event_summary") or {}
    except (OSError, json.JSONDecodeError):
        return {}


def build_review(trade_date: str) -> Dict[str, Any]:
    day_root = ROOT / "data" / "live_signal" / trade_date
    observations: List[Dict[str, Any]] = []
    for record in _read_jsonl(day_root / "tick_samples.jsonl"):
        observation = dict(record.get("observation") or {})
        if not observation.get("symbol") or not observation.get("event_ts"):
            continue
        observation["_historical_market_sector"] = record.get("market_sector") or {}
        observations.append(observation)
    observations.sort(key=lambda row: (str(row.get("event_ts")), str(row.get("symbol"))))

    if not observations:
        raise RuntimeError(f"{trade_date}没有可重放Tick证据")
    prior_dates = []
    for path in DAILY_ROOT.glob("*_1d.pkl"):
        frame = pd.read_pickle(path)
        if "eob" not in frame:
            continue
        dates = pd.to_datetime(frame["eob"], errors="coerce").dropna()
        prior_dates.extend(dates[dates.dt.strftime("%Y-%m-%d") < trade_date].dt.strftime("%Y-%m-%d").tolist())
    daily_asof = max(prior_dates) if prior_dates else ""
    daily = _load_daily(daily_asof)
    candidates = {row["symbol"]: row for row in daily.get("candidates", [])}

    helper = LiveSignalService(
        notifier=FeishuNotifier(dry_run=True),
        advisor=DeepSeekAdvisor(key_file=ROOT / "data" / "missing_deepseek_key.txt"),
    )
    auction = AuctionPathAnalyzer()
    for record in _read_jsonl(day_root / "auction_snapshots.jsonl"):
        snapshot = record.get("snapshot") or {}
        if snapshot.get("rows"):
            auction.add_snapshot(snapshot)
    auction_analysis = helper._apply_auction_group_context(auction.latest_analysis) if auction.snapshots else {"rows": [], "by_symbol": {}}
    auction_map = auction_analysis.get("by_symbol") or {}

    taxonomy = load_taxonomy()
    sector = LiveSectorHealthEngine(taxonomy)
    sector.set_candidates(daily.get("candidates", []))
    continuation = TrendContinuationAnalyzer()
    multitimeframe = MultiTimeframeIndicatorEngine()
    minute_seed = multitimeframe.seed_from_directory(MINUTE_ROOT, candidates)
    capital = CapitalBehaviorEngine()
    intraday = IntradayEventEngine()
    virtual_positions: Dict[str, Dict[str, Any]] = {}
    events: List[Dict[str, Any]] = []
    transitions: Dict[str, List[Dict[str, Any]]] = {}

    for observation in observations:
        symbol = str(observation.get("symbol"))
        candidate = dict(candidates.get(symbol) or {})
        if not candidate:
            continue
        gate = auction_map.get(symbol, {})
        mtf = multitimeframe.update(observation)
        sector_context = sector.update(observation)
        market_sector = observation.get("_historical_market_sector") or {"status": "NO_HISTORICAL_SNAPSHOT"}
        continuation_sector = dict(sector_context)
        if market_sector.get("board_code"):
            continuation_sector.update({
                "market_board_pct": market_sector.get("board_pct"),
                "market_board_rank": market_sector.get("board_rank"),
            })
        continuation_context = continuation.update(observation, candidate, gate, continuation_sector)
        base_strength = LiveSignalService._live_strength(candidate, sector_context, gate)
        live_strength = (
            int(_safe_float(continuation_context.get("score"), base_strength))
            if candidate.get("daily_route") == "TREND_CONTINUATION" else base_strength
        )
        if mtf.get("periods"):
            live_strength = max(0, min(100, live_strength + int(round((_safe_float(mtf.get("score")) - 50.0) * 0.20))))
        capital_context = capital.update(observation, candidate, continuation_sector, mtf)
        if capital_context.get("status") == "READY":
            adjustment = max(-6, min(6, int(round((_safe_float(capital_context.get("score"), 50.0) - 50.0) * 0.12))))
            live_strength = max(0, min(100, live_strength + adjustment))

        history = transitions.setdefault(symbol, [])
        if not history or history[-1].get("regime") != capital_context.get("regime"):
            history.append({
                "event_ts": observation.get("event_ts"),
                "regime": capital_context.get("regime"),
                "regime_cn": capital_context.get("regime_cn"),
                "phase": capital_context.get("phase"),
                "phase_cn": capital_context.get("phase_cn"),
                "score": capital_context.get("score"),
                "confidence": capital_context.get("confidence"),
                "price": observation.get("price"),
                "signed_trade_ratio_180s": (capital_context.get("medium_180s") or {}).get("signed_trade_ratio"),
                "quote_ofi_180s": (capital_context.get("medium_180s") or {}).get("quote_ofi"),
                "above_vwap_ratio_180s": (capital_context.get("medium_180s") or {}).get("above_vwap_ratio"),
            })

        candidate.update({
            "live_sector": sector_context,
            "market_sector": market_sector,
            "continuation_context": continuation_context,
            "multitimeframe": mtf,
            "capital_behavior": capital_context,
            "live_signal_strength": live_strength,
        })
        sudden = LiveSignalService._sudden_trend_context(candidate, observation, mtf, sector_context, market_sector, gate)
        candidate["sudden_trend_context"] = sudden
        if candidate.get("daily_route") == "TREND_CONTINUATION":
            candidate["intraday_eligible"] = bool(continuation_context.get("confirmed"))
        elif (
            candidate.get("daily_route") == "TREND_PULLBACK"
            and live_strength >= 60
            and sector_context.get("state") in {"IGNITION", "EXPANSION", "HEALTHY_TREND"}
            and not gate.get("hard_veto")
        ):
            candidate["intraday_eligible"] = True
        if symbol in virtual_positions:
            candidate.update({
                "action": "MONITOR_EXIT",
                "monitor_sell": True,
                "position_entry_date": virtual_positions[symbol]["entry_date"],
                "position_entry_price": virtual_positions[symbol]["entry_price"],
            })
        event = intraday.on_tick(_raw_tick(observation), candidate, auction_gate=gate)
        if not event:
            continue
        event.update({
            "name": candidate.get("name", ""),
            "capital_behavior": capital_context,
            "daily_signal_strength": candidate.get("signal_strength"),
        })
        events.append(event)
        if event.get("event") == "BUY_EVENT_WATCH" and symbol not in virtual_positions:
            virtual_positions[symbol] = {
                "entry_date": str(event.get("event_ts"))[:10],
                "entry_price": event.get("price"),
            }

    reviewed = _post_event_stats(events, observations)
    actionable = [row for row in reviewed if row.get("event") in {"BUY_EVENT_WATCH", "OPPORTUNITY_EVENT_WATCH"} and row.get("executable") is not False]
    formal = [row for row in actionable if row.get("event") == "BUY_EVENT_WATCH"]
    opportunities = [row for row in actionable if row.get("event") == "OPPORTUNITY_EVENT_WATCH"]
    outflows = [row for row in reviewed if row.get("pattern") == "CAPITAL_OUTFLOW_CONFIRMED"]
    phase_counts = Counter((row.get("capital_behavior") or {}).get("phase") for row in reviewed)
    event_summary = {
        "count": len(reviewed),
        "type_counts": dict(Counter(row.get("event") for row in reviewed)),
        "pattern_counts": dict(Counter(row.get("pattern") for row in reviewed)),
        "capital_phase_counts": dict(phase_counts),
        "actionable": _subset_stats(actionable),
        "formal_entries": _subset_stats(formal),
        "opportunities": _subset_stats(opportunities),
        "capital_outflow_events": _subset_stats(outflows),
    }
    key_symbols = {"SZSE.300274", "SHSE.603259"}
    return {
        "generated_at": datetime.now().isoformat(),
        "trade_date": trade_date,
        "daily_asof": daily_asof,
        "data_asof": max(str(row.get("event_ts")) for row in observations),
        "tick_evidence_count": len(observations),
        "daily_coverage": daily.get("available_size"),
        "minute_seed": minute_seed,
        "rules_version": "daily_signal_v8 + intraday_mtf + capital_behavior_v1",
        "events": reviewed,
        "event_summary": event_summary,
        "old_v6_summary": _load_old_summary(trade_date),
        "key_capital_transitions": {symbol: transitions.get(symbol, []) for symbol in key_symbols},
        "final_capital_snapshot": capital.snapshot(),
        "path_stats": _path_stats(observations, candidates),
        "limitations": [
            "当日规则由当日问题推动形成，本重放是发现性检验，不是独立样本外证明",
            "落盘Tick约每5秒一条而非交易所完整逐笔，主动成交方向和五档OFI都是代理量",
            "资金意图是价格、成交、盘口、板块共同支持的可证伪假设，不是对主力真实想法的直接观测",
            "事件后的MFE/MAE和至收盘收益只用于评价，未参与任何事前信号计算",
            "未计手续费、滑点与真实可成交队列，不能把信号路径收益直接当作策略净收益",
        ],
    }


def render_markdown(result: Dict[str, Any]) -> str:
    summary = result["event_summary"]
    old = result.get("old_v6_summary") or {}
    lines = [
        "# V8多周期资金行为信号：当日顺序重放",
        "",
        f"交易日：{result['trade_date']}；事前日线截止：{result['daily_asof']}；Tick截止：{result['data_asof']}",
        f"证据：{result['tick_evidence_count']}条落盘Tick；日线覆盖{result['daily_coverage']}/29；一分钟预热{result['minute_seed'].get('ready_count')}/{result['minute_seed'].get('symbol_count')}。",
        "",
        "## 结论与V6对照",
        "",
        f"- V6：事件{old.get('count', '—')}个，正式新开仓{(old.get('formal_entries') or {}).get('count', '—')}个。",
        f"- V8：事件{summary['count']}个，正式新开仓{summary['formal_entries']['count']}个，机会观察{summary['opportunities']['count']}个。",
        f"- V8正式新开仓截至收盘为正：{summary['formal_entries']['positive_to_latest']}/{summary['formal_entries']['count']}；平均{_pct(summary['formal_entries']['mean_to_latest'])}；平均后续最大有利波动{_pct(summary['formal_entries']['mean_post_mfe'])}。",
        "- 这里比较的是信号质量和可执行性，不是策略账户收益；样本只有一个交易日，不据此下盈利结论。",
        "",
        "## V8事件明细",
        "",
        "| 时间 | 标的 | 动作层 | 触发 | 资金状态 | 大周期 | 分数 | 价格 | 至收盘 | 后续MFE/MAE |",
        "|---|---|---|---|---|---|---:|---:|---:|---:|",
    ]
    for row in result["events"]:
        capital = row.get("capital_behavior") or {}
        structure = capital.get("structure") or {}
        lines.append(
            f"| {_clock(row.get('event_ts'))} | {row.get('name')}（{str(row.get('symbol')).split('.')[-1]}） | "
            f"{row.get('event')} | {row.get('pattern')} | {capital.get('phase_cn', '—')} | "
            f"{structure.get('phase_cn', '—')} | {capital.get('score', '—')} | {_safe_float(row.get('price')):.3f} | "
            f"{_pct(row.get('to_latest'))} | {_pct(row.get('post_mfe'))}/{_pct(row.get('post_mae'))} |"
        )
    if not result["events"]:
        lines.append("| - | - | 当日无事件 | - | - | - | - | - | - | - |")

    lines.extend(["", "## 阳光电源与药明康德的资金状态演化", ""])
    for symbol, rows in result.get("key_capital_transitions", {}).items():
        name = next((event.get("name") for event in result["events"] if event.get("symbol") == symbol), symbol)
        lines.append(f"### {name}（{symbol.split('.')[-1]}）")
        lines.append("")
        for row in rows:
            if row.get("confidence") == "LOW" and len(rows) > 8:
                continue
            lines.append(
                f"- {_clock(row.get('event_ts'))}：{row.get('regime_cn')}（当前{row.get('phase_cn')}）{row.get('score')}/100（{row.get('confidence')}）；"
                f"180秒主动成交{_safe_float(row.get('signed_trade_ratio_180s')):+.2f}，OFI{_safe_float(row.get('quote_ofi_180s')):+.2f}，站上VWAP{_safe_float(row.get('above_vwap_ratio_180s')):.0%}。"
            )
        lines.append("")

    lines.extend([
        "## 信号如何使用",
        "",
        "- 大周期先验回答‘这段资金更像推进、再积累、平衡还是派发风险’，只改变入场优先级，不单独买卖。",
        "- 日内状态回答‘此刻是真流入、受控推进、卖压吸收、换手还是持续流出’，必须由至少60—150秒证据形成。",
        "- 新增资金延续买点：只有大周期支持、板块健康、5/15/30分钟共振、资金状态高置信且价格未远离VWAP时，才允许不等机械回踩直接升级新开仓。",
        "- 已持仓：受控推进/卖压吸收支持继续持有；确认流出需要主动成交、盘口和价格至少共同转弱，才触发减仓，避免单个盘口抖动误杀。",
        "",
        "## 限制",
        "",
    ])
    lines.extend([f"- {item}" for item in result["limitations"]])
    return "\n".join(lines) + "\n"


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--trade-date", default="2026-08-11")
    args = parser.parse_args()
    result = build_review(args.trade_date)
    suffix = args.trade_date.replace("-", "")
    json_path = ROOT / "reports" / f"today_v8_capital_behavior_replay_{suffix}.json"
    md_path = ROOT / "reports" / f"today_v8_capital_behavior_replay_{suffix}.md"
    json_path.write_text(json.dumps(result, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    md_path.write_text(render_markdown(result), encoding="utf-8")
    print(json.dumps({
        "json": str(json_path),
        "markdown": str(md_path),
        "event_summary": result["event_summary"],
    }, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
