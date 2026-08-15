# coding: utf-8
"""按V6分层周期与突发趋势通道，顺序重放2026-08-10真实落盘证据。

只读取D盘历史一分钟、D-1日线、集合竞价快照和每5秒Tick证据；不发送飞书、
不写虚拟持仓、不接触正在运行的GoldMiner进程。规则由当日问题推动形成，因此本报告
属于发现性研究，不是样本外收益证明。
"""

from __future__ import annotations

import argparse
import json
from collections import Counter
from datetime import datetime
from pathlib import Path
from statistics import mean, median
from typing import Any, Dict, List, Optional

from auction_path import AuctionPathAnalyzer
from continuation_engine import TrendContinuationAnalyzer
from intraday_engine import IntradayEventEngine
from live_signal_service import DeepSeekAdvisor, FeishuNotifier, LiveSignalService, load_taxonomy
from multitimeframe_engine import MultiTimeframeIndicatorEngine
from review_today_v4 import (
    ROOT,
    _latest_daily_payload,
    _load_v4_daily,
    _path_stats,
    _post_event_stats,
    _raw_tick,
    _read_jsonl,
    _safe_float,
)
from sector_health import LiveSectorHealthEngine


MINUTE_ROOT = ROOT / "data" / "goldminer" / "1m_20260511_20260807"


def _clock(value: Any) -> str:
    return str(value or "")[11:19]


def _pct(value: Optional[float]) -> str:
    return "不可用" if value is None else f"{value:+.2%}"


def _event_layer(event: Dict[str, Any]) -> str:
    return {
        "DISCOVERY_EVENT_WATCH": "发现层（不可成交）",
        "OPPORTUNITY_EVENT_WATCH": "机会/做T参考",
        "BUY_EVENT_WATCH": "T+1新开仓观察",
        "RISK_EVENT_WATCH": "T+1当日失效预警",
        "SELL_EVENT_WATCH": "卖出观察",
    }.get(str(event.get("event")), str(event.get("event")))


def _mtf_text(context: Dict[str, Any]) -> str:
    periods = context.get("periods") or {}
    states = "/".join(str((periods.get(key) or {}).get("state", "NA")) for key in ("5", "15", "30"))
    return f"{states}；{context.get('alignment')} {context.get('score', 0)}/100"


def build_review(trade_date: str) -> Dict[str, Any]:
    day_root = ROOT / "data" / "live_signal" / trade_date
    tick_records = _read_jsonl(day_root / "tick_samples.jsonl")
    auction_records = _read_jsonl(day_root / "auction_snapshots.jsonl")
    old_daily = _latest_daily_payload(_read_jsonl(day_root / "daily_candidates.jsonl"))
    daily_asof = str(old_daily.get("asof") or "2026-08-07")
    daily = _load_v4_daily(daily_asof)
    # 当日实盘已经在开盘前落盘了严格D-1候选；若研究缓存尚未覆盖该日期，
    # 直接使用这份事前快照，绝不因事后重算或缓存缺口把候选池变成空集。
    if not daily.get("candidates") and old_daily.get("candidates"):
        daily = old_daily
    candidates = {row["symbol"]: row for row in daily.get("candidates", [])}

    helper = LiveSignalService(
        notifier=FeishuNotifier(dry_run=True),
        advisor=DeepSeekAdvisor(key_file=ROOT / "data" / "missing_deepseek_key.txt"),
    )
    auction_analyzer = AuctionPathAnalyzer()
    for record in auction_records:
        snapshot = record.get("snapshot") or {}
        if snapshot.get("rows"):
            auction_analyzer.add_snapshot(snapshot)
    auction_analysis = (
        helper._apply_auction_group_context(auction_analyzer.latest_analysis)
        if auction_analyzer.snapshots else {"rows": [], "by_symbol": {}}
    )
    auction_map = auction_analysis.get("by_symbol", {})

    observations = [
        record.get("observation") or {} for record in tick_records
        if (record.get("observation") or {}).get("symbol")
        and (record.get("observation") or {}).get("event_ts")
    ]
    observations.sort(key=lambda row: (str(row.get("event_ts")), str(row.get("symbol"))))

    taxonomy = load_taxonomy()
    sector = LiveSectorHealthEngine(taxonomy)
    sector.set_candidates(daily.get("candidates", []))
    continuation = TrendContinuationAnalyzer()
    multitimeframe = MultiTimeframeIndicatorEngine()
    seed_status = multitimeframe.seed_from_directory(MINUTE_ROOT, candidates)
    intraday = IntradayEventEngine()
    events: List[Dict[str, Any]] = []
    virtual_positions: Dict[str, Dict[str, Any]] = {}
    first_mtf_confirm: Dict[str, Dict[str, Any]] = {}
    first_sudden_discovery: Dict[str, Dict[str, Any]] = {}

    for observation in observations:
        symbol = str(observation.get("symbol"))
        candidate = dict(candidates.get(symbol) or {})
        if not candidate:
            continue
        auction = auction_map.get(symbol, {})
        mtf = multitimeframe.update(observation)
        sector_context = sector.update(observation)
        market_sector = {"status": "NO_HISTORICAL_SNAPSHOT"}
        continuation_context = continuation.update(observation, candidate, auction, sector_context)
        base_strength = LiveSignalService._live_strength(candidate, sector_context, auction)
        live_strength = (
            int(_safe_float(continuation_context.get("score"), base_strength))
            if candidate.get("daily_route") == "TREND_CONTINUATION" else base_strength
        )
        if mtf.get("periods"):
            live_strength = max(0, min(100, live_strength + int(round((_safe_float(mtf.get("score")) - 50) * 0.20))))
        candidate.update({
            "live_sector": sector_context,
            "continuation_context": continuation_context,
            "multitimeframe": mtf,
            "live_signal_strength": live_strength,
            "market_sector": market_sector,
        })
        sudden = LiveSignalService._sudden_trend_context(
            candidate, observation, mtf, sector_context, market_sector, auction,
        )
        candidate["sudden_trend_context"] = sudden
        if mtf.get("trigger_confirmed") and symbol not in first_mtf_confirm:
            first_mtf_confirm[symbol] = {
                "symbol": symbol, "name": candidate.get("name"), "event_ts": observation.get("event_ts"),
                "context": mtf, "intraday_return": sudden.get("intraday_return"),
            }
        if sudden.get("discovered") and symbol not in first_sudden_discovery:
            first_sudden_discovery[symbol] = {
                "symbol": symbol, "name": candidate.get("name"), "event_ts": observation.get("event_ts"),
                "sudden": sudden, "context": mtf, "sector": sector_context,
            }

        if candidate.get("daily_route") == "TREND_CONTINUATION":
            candidate["intraday_eligible"] = bool(continuation_context.get("confirmed"))
        elif (
            candidate.get("daily_route") == "TREND_PULLBACK"
            and live_strength >= 60
            and sector_context.get("state") in {"IGNITION", "EXPANSION", "HEALTHY_TREND"}
            and not auction.get("hard_veto")
        ):
            candidate["intraday_eligible"] = True
        if symbol in virtual_positions:
            candidate.update({
                "action": "MONITOR_EXIT", "monitor_sell": True,
                "position_entry_date": virtual_positions[symbol]["entry_date"],
                "position_entry_price": virtual_positions[symbol]["entry_price"],
            })
        event = intraday.on_tick(_raw_tick(observation), candidate, auction_gate=auction)
        if not event:
            continue
        event["name"] = candidate.get("name", "")
        event["daily_signal_strength"] = candidate.get("signal_strength")
        events.append(event)
        if event.get("event") == "BUY_EVENT_WATCH":
            virtual_positions[symbol] = {
                "entry_date": str(event.get("event_ts"))[:10],
                "entry_price": event.get("price"),
            }

    reviewed_events = _post_event_stats(events, observations)
    paths = _path_stats(observations, candidates)
    daily_rows = []
    for candidate in candidates.values():
        daily_rows.append({
            "symbol": candidate.get("symbol"), "name": candidate.get("name"),
            "action": candidate.get("action"), "daily_route": candidate.get("daily_route"),
            "signal_strength": candidate.get("signal_strength"), "daily_j_9202": candidate.get("slow_j"),
            "monthly_j_9202": candidate.get("monthly_slow_j"), "monthly_j_822": candidate.get("monthly_fast_j"),
            "monthly_alignment": candidate.get("monthly_dual_alignment"),
            "daily_fast_822_role": candidate.get("daily_fast_822_role"),
            **paths.get(str(candidate.get("symbol")), {}),
        })
    daily_rows.sort(key=lambda row: (-int(_safe_float(row.get("signal_strength"))), str(row.get("symbol"))))

    values = [row.get("to_latest") for row in reviewed_events if row.get("to_latest") is not None]
    actionable = [
        row for row in reviewed_events
        if row.get("event") in {"BUY_EVENT_WATCH", "OPPORTUNITY_EVENT_WATCH"}
        and row.get("executable") is not False
    ]
    formal_entries = [row for row in actionable if row.get("event") == "BUY_EVENT_WATCH"]
    opportunities = [row for row in actionable if row.get("event") == "OPPORTUNITY_EVENT_WATCH"]
    def subset_stats(rows: List[Dict[str, Any]]) -> Dict[str, Any]:
        subset = [row.get("to_latest") for row in rows if row.get("to_latest") is not None]
        return {
            "count": len(rows), "positive_to_latest": sum(value > 0 for value in subset),
            "mean_to_latest": mean(subset) if subset else None,
            "median_to_latest": median(subset) if subset else None,
        }
    return {
        "generated_at": datetime.now().isoformat(),
        "trade_date": trade_date,
        "daily_asof": daily_asof,
        "data_asof": max((str(row.get("event_ts")) for row in observations), default=""),
        "tick_evidence_count": len(observations),
        "auction_snapshot_count": len(auction_records),
        "minute_seed": seed_status,
        "rules_version": "daily_monthly_v6_layered + intraday_mtf_v1 + sudden_trend_upgrade_v1",
        "daily_rows": daily_rows,
        "events": reviewed_events,
        "event_summary": {
            "count": len(reviewed_events),
            "type_counts": dict(Counter(row.get("event") for row in reviewed_events)),
            "pattern_counts": dict(Counter(row.get("pattern") for row in reviewed_events)),
            "positive_to_latest": sum(value > 0 for value in values),
            "mean_to_latest": mean(values) if values else None,
            "median_to_latest": median(values) if values else None,
            "actionable": subset_stats(actionable),
            "formal_entries": subset_stats(formal_entries),
            "opportunities": subset_stats(opportunities),
            "unexecutable_discoveries": sum(row.get("event") == "DISCOVERY_EVENT_WATCH" for row in reviewed_events),
        },
        "first_mtf_confirm": list(first_mtf_confirm.values()),
        "first_sudden_discovery": list(first_sudden_discovery.values()),
        "final_multitimeframe": multitimeframe.snapshot(),
        "final_sector": sector.snapshot(),
        "limitations": [
            "规则由2026-08-10现象推动形成，当天回放属于发现性研究，不是独立样本外检验",
            "Tick为约5秒一次落盘证据，不是交易所完整逐笔；一分钟OHLC足够近似，但盘口瞬时变化可能漏采",
            "没有保存当天历史全市场板块雷达快照；回放只使用精选池实时板块代理，可能低估单例题材",
            "分钟指标只在完整K线结束后生效，当前未完成K线被明确排除",
            "涨停封死的发现事件不等于存在可成交买点",
        ],
    }


def render_markdown(result: Dict[str, Any]) -> str:
    summary = result["event_summary"]
    lines = [
        "# A股轮动V6：分层周期、突发趋势与8月10日顺序重放",
        "",
        f"生成时间：{result['generated_at']}",
        f"交易日：{result['trade_date']}；D-1日线截止：{result['daily_asof']}；Tick截止：{result['data_asof']}",
        f"证据：{result['tick_evidence_count']}条约5秒Tick；一分钟预热 {result['minute_seed']['ready_count']}/{result['minute_seed']['symbol_count']}只。",
        "",
        "> 这是严格按时间顺序、只读已完成K线的发现性回放。由于8月10日参与了规则形成，不能把结果当成样本外盈利证明。",
        "",
        "## 1. 新周期职责",
        "",
        "- 日线：KDJ(9,20,2)决定结构与30-40价值区；日线KDJ(8,2,2)仅保留审计值，不再参与评分或买卖。",
        "- 月线：KDJ(9,20,2)定中期结构，KDJ(8,2,2)判断当前月修复/共振/降温。",
        "- 5/15/30分钟：统一使用KDJ(8,2,2)+MACD(5,10,5)，只读完整K线；5分钟负责触发，15/30分钟至少一档负责确认。",
        "- MACD顶/底背离继续使用已确认价格拐点；不拿未完成右端极值作弊。",
        "",
        "## 2. 当天事件结果",
        "",
        f"事件共{summary['count']}个；分层={summary['type_counts']}。其中不可成交发现{summary['unexecutable_discoveries']}个，不纳入可执行收益。",
        f"可执行信号{summary['actionable']['count']}个：截至收盘为正 {summary['actionable']['positive_to_latest']}/{summary['actionable']['count']}；"
        f"平均至收盘 {_pct(summary['actionable']['mean_to_latest'])}，中位 {_pct(summary['actionable']['median_to_latest'])}。",
        f"正式T+1观察{summary['formal_entries']['count']}个：截至收盘 {_pct(summary['formal_entries']['mean_to_latest'])}；"
        f"机会/做T参考{summary['opportunities']['count']}个：平均至收盘 {_pct(summary['opportunities']['mean_to_latest'])}。",
        "",
        "| 时间 | 标的 | 分层 | 触发 | 价格 | 当时涨幅 | 多周期 | 可成交 | 至收盘 |",
        "|---|---|---|---|---:|---:|---|---|---:|",
    ]
    for row in result["events"]:
        context = row.get("multitimeframe") or {}
        lines.append(
            f"| {_clock(row.get('event_ts'))} | {row.get('name')}（{str(row.get('symbol')).split('.')[-1]}） | "
            f"{_event_layer(row)} | {row.get('pattern')} | {_safe_float(row.get('price')):.3f} | "
            f"{_pct(row.get('intraday_return'))} | {_mtf_text(context)} | "
            f"{'是' if row.get('executable', row.get('event') != 'DISCOVERY_EVENT_WATCH') else '否'} | {_pct(row.get('to_latest'))} |"
        )
    if not result["events"]:
        lines.append("| - | - | 当天无事件 | - | - | - | - | - | - |")

    lines.extend(["", "## 3. 突发趋势捕捉", ""])
    for row in result["first_sudden_discovery"]:
        sudden, context = row["sudden"], row["context"]
        lines.extend([
            f"### {row['name']}（{str(row['symbol']).split('.')[-1]}） {_clock(row['event_ts'])}",
            "",
            f"- 当时涨幅：{_pct(sudden.get('intraday_return'))}；发现强度：{sudden.get('score')}/100；多周期：{_mtf_text(context)}。",
            f"- 成交性：{'可成交' if sudden.get('executable') else '不可成交'}；涨停封死：{'是' if sudden.get('limit_locked') else '否'}。",
            f"- 依据：{'；'.join(sudden.get('reasons') or [])}。",
            "",
        ])
    if not result["first_sudden_discovery"]:
        lines.append("当天没有标的同时满足突发涨幅、多周期确认、VWAP与盘口条件。\n")

    lines.extend(["## 4. 实际飞书信号会长什么样", ""])
    discovery = next((row for row in result["events"] if row.get("event") == "DISCOVERY_EVENT_WATCH"), None)
    opportunity = next((row for row in result["events"] if row.get("event") == "OPPORTUNITY_EVENT_WATCH"), None)
    if discovery:
        lines.extend([
            "```text",
            "【A股轮动｜突发趋势已发现｜当前不可成交】",
            f"{discovery.get('name')}（{str(discovery.get('symbol')).split('.')[-1]}）｜发现强度 {discovery.get('composite_signal_strength')}/100（不是胜率）",
            f"现价 {discovery.get('price')}｜相对昨收 {_pct(discovery.get('intraday_return'))}｜{_mtf_text(discovery.get('multitimeframe') or {})}",
            "成交性：卖一为空/价格封在涨停附近，只记录捕捉到，不伪装成可以买到的买点。",
            "后续：实时监控开板承接、5分钟再次转强及15/30分钟共振。",
            "```",
            "",
        ])
    if opportunity:
        lines.extend([
            "```text",
            "【A股轮动｜趋势机会/持仓做T参考】",
            f"{opportunity.get('name')}（{str(opportunity.get('symbol')).split('.')[-1]}）｜综合强度 {opportunity.get('composite_signal_strength')}/100",
            f"现价 {opportunity.get('price')}｜相对VWAP {_pct(opportunity.get('vwap_gap'))}",
            f"多周期：{_mtf_text(opportunity.get('multitimeframe') or {})}",
            "分级：只确认趋势机会；未达到T+1新开仓门槛时不写入虚拟新仓。",
            "```",
            "",
        ])

    lines.extend(["## 5. 验收与未解决项", ""])
    lines.extend([
        "- 已解决：日线/月线/分钟线参数不再混用；分钟触发无未完成K线；百花医药式突发趋势拥有独立升级通道；封板不可成交不会误报买点。",
        "- 仍需验证：用6-7月更多交易日做样本外式滚动重放，比较V5/V6的事件数、未来1-5日胜率、MFE/MAE及相对沪深300超额。",
        "- 仍需补强：保存全市场板块雷达历史快照，否则回放时只能使用精选池板块代理，无法完整复原当时全市场资金偏好。",
        "",
        "限制：",
    ])
    lines.extend([f"- {item}" for item in result["limitations"]])
    return "\n".join(lines) + "\n"


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--trade-date", default="2026-08-10")
    args = parser.parse_args()
    result = build_review(args.trade_date)
    suffix = args.trade_date.replace("-", "")
    json_path = ROOT / "reports" / f"today_v6_layered_cycles_replay_{suffix}.json"
    md_path = ROOT / "reports" / f"today_v6_layered_cycles_replay_{suffix}.md"
    json_path.write_text(json.dumps(result, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    md_path.write_text(render_markdown(result), encoding="utf-8")
    print(json.dumps({
        "json": str(json_path), "markdown": str(md_path),
        "event_summary": result["event_summary"],
        "sudden_discoveries": [row["name"] for row in result["first_sudden_discovery"]],
    }, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
