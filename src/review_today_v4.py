# coding: utf-8
"""用当天已落盘事实生成 V3 问题汇总与 V4 影子回放。

限制：Tick 输入来自实时服务每5秒保存的证据样本，不是完整逐笔；本脚本只做
事后复盘，不回写盘中候选，不发送飞书，不修改正在运行的进程。
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from datetime import datetime
from pathlib import Path
from statistics import mean, median
from typing import Any, Dict, Iterable, List, Optional

import pandas as pd

from auction_path import AuctionPathAnalyzer
from intraday_engine import IntradayEventEngine
from live_signal_service import (
    DailyCandidateBuilder,
    DeepSeekAdvisor,
    FeishuNotifier,
    LiveSignalService,
    load_pool,
    load_taxonomy,
)
from sector_health import LiveSectorHealthEngine


ROOT = Path(r"D:\codex\a_share_rotation")


def _safe_float(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _read_jsonl(path: Path) -> List[Dict[str, Any]]:
    rows = []
    if not path.exists():
        return rows
    with path.open("r", encoding="utf-8-sig", errors="ignore") as handle:
        for line in handle:
            if not line.strip():
                continue
            try:
                value = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(value, dict):
                rows.append(value)
    return rows


def _latest_daily_payload(rows: Iterable[Dict[str, Any]]) -> Dict[str, Any]:
    payloads = [row.get("payload") for row in rows if (row.get("payload") or {}).get("candidates")]
    return payloads[-1] if payloads else {}


def _load_v4_daily(asof: str) -> Dict[str, Any]:
    pool = load_pool()
    taxonomy = load_taxonomy()
    cache = ROOT / "data" / "goldminer" / "daily_adjust_prev_20210101_20260807"
    frames = {}
    for symbol in pool:
        path = cache / f"{symbol}_1d.pkl"
        if path.exists():
            frames[symbol] = pd.read_pickle(path)
    return DailyCandidateBuilder(pool, taxonomy).build(frames, asof=asof)


def _raw_tick(observation: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "symbol": observation.get("symbol"),
        "created_at": observation.get("event_ts"),
        "price": observation.get("price"),
        "cum_volume": observation.get("cum_volume"),
        "cum_amount": observation.get("cum_amount"),
        "quotes": observation.get("quotes") or [],
    }


def _percent(value: Optional[float]) -> str:
    return "不可用" if value is None else f"{value:+.2%}"


def _replay(
    tick_records: List[Dict[str, Any]],
    candidates: List[Dict[str, Any]],
    auction_analysis: Dict[str, Any],
    taxonomy: Dict[str, Any],
) -> Dict[str, Any]:
    candidate_map = {row["symbol"]: row for row in candidates}
    auction_map = auction_analysis.get("by_symbol", {})
    sector = LiveSectorHealthEngine(taxonomy)
    sector.set_candidates(candidates)
    engine = IntradayEventEngine()
    ordered = []
    for record in tick_records:
        observation = record.get("observation") or {}
        if observation.get("symbol") and observation.get("event_ts"):
            ordered.append(observation)
    ordered.sort(key=lambda row: (str(row.get("event_ts")), str(row.get("symbol"))))
    events = []
    for observation in ordered:
        symbol = str(observation.get("symbol"))
        candidate = dict(candidate_map.get(symbol) or {})
        if not candidate:
            continue
        sector_context = sector.update(observation)
        auction = auction_map.get(symbol, {})
        strength = LiveSignalService._live_strength(candidate, sector_context, auction)
        candidate["live_sector"] = sector_context
        candidate["live_signal_strength"] = strength
        if (
            candidate.get("daily_route") in {"TREND_CONTINUATION", "TREND_PULLBACK"}
            and strength >= 60
            and sector_context.get("state") in {"IGNITION", "EXPANSION", "HEALTHY_TREND"}
            and not auction.get("hard_veto")
        ):
            candidate["intraday_eligible"] = True
        event = engine.on_tick(_raw_tick(observation), candidate, auction_gate=auction)
        if event:
            event["name"] = candidate.get("name", "")
            events.append(event)
    return {
        "events": events,
        "sector_snapshot": sector.snapshot(),
        "intraday_snapshot": engine.snapshot(),
        "ordered_observations": ordered,
    }


def _path_stats(observations: List[Dict[str, Any]], candidates: Dict[str, Dict[str, Any]]) -> Dict[str, Any]:
    paths: Dict[str, Dict[str, Any]] = {}
    for observation in observations:
        symbol = str(observation.get("symbol"))
        close = _safe_float((candidates.get(symbol) or {}).get("close"))
        price = _safe_float(observation.get("price"))
        if close <= 0 or price <= 0:
            continue
        row = paths.setdefault(symbol, {"prices": [], "timestamps": [], "close": close})
        row["prices"].append(price)
        row["timestamps"].append(str(observation.get("event_ts")))
    result = {}
    for symbol, row in paths.items():
        result[symbol] = {
            "first_ts": row["timestamps"][0],
            "last_ts": row["timestamps"][-1],
            "latest_return": row["prices"][-1] / row["close"] - 1.0,
            "mfe_return": max(row["prices"]) / row["close"] - 1.0,
            "mae_return": min(row["prices"]) / row["close"] - 1.0,
            "last_price": row["prices"][-1],
        }
    return result


def _post_event_stats(events: List[Dict[str, Any]], observations: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    by_symbol: Dict[str, List[Dict[str, Any]]] = {}
    for observation in observations:
        by_symbol.setdefault(str(observation.get("symbol")), []).append(observation)
    result = []
    for event in events:
        symbol = str(event.get("symbol"))
        event_ts = str(event.get("event_ts") or "")
        event_price = _safe_float(event.get("price"))
        later = [row for row in by_symbol.get(symbol, []) if str(row.get("event_ts")) >= event_ts]
        prices = [_safe_float(row.get("price")) for row in later if _safe_float(row.get("price")) > 0]
        result.append({
            **event,
            "post_mfe": max(prices) / event_price - 1.0 if prices and event_price > 0 else None,
            "post_mae": min(prices) / event_price - 1.0 if prices and event_price > 0 else None,
            "to_latest": prices[-1] / event_price - 1.0 if prices and event_price > 0 else None,
        })
    return result


def build_review(trade_date: str) -> Dict[str, Any]:
    day_root = ROOT / "data" / "live_signal" / trade_date
    daily_log = _read_jsonl(day_root / "daily_candidates.jsonl")
    tick_log = _read_jsonl(day_root / "tick_samples.jsonl")
    old_events = _read_jsonl(day_root / "tick_events.jsonl")
    auction_log = _read_jsonl(day_root / "auction_snapshots.jsonl")
    old_daily = _latest_daily_payload(daily_log)
    asof = str(old_daily.get("asof") or "2026-08-07")
    v4_daily = _load_v4_daily(asof)
    taxonomy = load_taxonomy()

    service = LiveSignalService(
        notifier=FeishuNotifier(dry_run=True),
        advisor=DeepSeekAdvisor(key_file=ROOT / "data" / "missing_deepseek_key.txt"),
    )
    service.latest_daily = v4_daily
    analyzer = AuctionPathAnalyzer()
    for record in auction_log:
        snapshot = record.get("snapshot")
        if snapshot and snapshot.get("rows"):
            analyzer.add_snapshot(snapshot)
    service.auction_analyzer = analyzer
    auction_analysis = service._apply_auction_group_context(analyzer.latest_analysis) if analyzer.snapshots else {"rows": [], "by_symbol": {}}

    replay = _replay(tick_log, v4_daily.get("candidates", []), auction_analysis, taxonomy)
    candidates = {row["symbol"]: row for row in v4_daily.get("candidates", [])}
    paths = _path_stats(replay["ordered_observations"], candidates)

    old_auction_analysis = {}
    for record in auction_log:
        if (record.get("analysis") or {}).get("rows"):
            old_auction_analysis = record["analysis"]
    old_veto_rows = [row for row in old_auction_analysis.get("rows", []) if row.get("gate") == "VETO"]
    old_veto_review = []
    for row in old_veto_rows:
        symbol = str(row.get("symbol"))
        old_veto_review.append({
            "symbol": symbol,
            "name": candidates.get(symbol, {}).get("name", row.get("name", "")),
            "old_label": row.get("label"),
            "auction_gap": row.get("final_gap"),
            **paths.get(symbol, {}),
        })

    old_sell_review = _post_event_stats(
        [row for row in old_events if row.get("event") == "SELL_EVENT_WATCH"],
        replay["ordered_observations"],
    )
    v4_event_review = _post_event_stats(replay["events"], replay["ordered_observations"])
    v4_to_latest = [row["to_latest"] for row in v4_event_review if row.get("to_latest") is not None]
    v4_post_mfe = [row["post_mfe"] for row in v4_event_review if row.get("post_mfe") is not None]
    calibration = {}
    for label, low, high in (("80-100", 80, 101), ("65-79", 65, 80), ("0-64", 0, 65)):
        bucket = [
            row for row in v4_event_review
            if low <= int(_safe_float(row.get("composite_signal_strength"), _safe_float(row.get("live_signal_strength")))) < high
        ]
        values = [row["to_latest"] for row in bucket if row.get("to_latest") is not None]
        calibration[label] = {
            "events": len(bucket),
            "positive": sum(value > 0 for value in values),
            "mean_to_latest": mean(values) if values else None,
            "median_to_latest": median(values) if values else None,
        }

    intraday_map = replay["intraday_snapshot"].get("by_symbol", {})
    sector_by_symbol = replay["sector_snapshot"].get("by_symbol", {})
    auction_map = auction_analysis.get("by_symbol", {})
    signal_table = []
    for symbol, candidate in candidates.items():
        if symbol not in paths:
            continue
        sector_context = sector_by_symbol.get(symbol, {})
        strength = LiveSignalService._live_strength(candidate, sector_context, auction_map.get(symbol, {}))
        signal_table.append({
            "symbol": symbol,
            "name": candidate.get("name", ""),
            "daily_action": candidate.get("action"),
            "daily_route": candidate.get("daily_route"),
            "daily_strength": candidate.get("signal_strength"),
            "live_strength": strength,
            "slow_j": candidate.get("slow_j"),
            "sector": sector_context.get("theme"),
            "sector_state": sector_context.get("state"),
            "role": sector_context.get("role"),
            "phase": (intraday_map.get(symbol) or {}).get("phase"),
            **paths[symbol],
        })
    signal_table.sort(key=lambda row: (-row["live_strength"], -row["latest_return"], row["symbol"]))

    latest_ts = max((str(row.get("event_ts")) for row in replay["ordered_observations"]), default="")
    return {
        "generated_at": datetime.now().isoformat(),
        "trade_date": trade_date,
        "data_asof": latest_ts,
        "daily_asof": asof,
        "data_scope": "stored_5_second_tick_evidence_not_full_tick",
        "v3_action_counts": dict(Counter(row.get("action") for row in old_daily.get("candidates", []))),
        "v4_action_counts": dict(Counter(row.get("action") for row in v4_daily.get("candidates", []))),
        "v4_route_counts": dict(Counter(row.get("daily_route") for row in v4_daily.get("candidates", []))),
        "sector_groups": [row for row in replay["sector_snapshot"].get("groups", []) if row.get("observed_count", 0) >= 2],
        "signal_table": signal_table,
        "old_veto_review": old_veto_review,
        "old_sell_review": old_sell_review,
        "v4_event_review": v4_event_review,
        "v4_event_summary": {
            "events": len(v4_event_review),
            "positive_to_latest": sum(value > 0 for value in v4_to_latest),
            "mean_to_latest": mean(v4_to_latest) if v4_to_latest else None,
            "median_to_latest": median(v4_to_latest) if v4_to_latest else None,
            "mean_post_mfe": mean(v4_post_mfe) if v4_post_mfe else None,
            "pattern_counts": dict(Counter(row.get("pattern") for row in v4_event_review)),
            "strength_calibration": calibration,
        },
    }


def render_markdown(result: Dict[str, Any]) -> str:
    veto = result["old_veto_review"]
    veto_latest = [row.get("latest_return") for row in veto if row.get("latest_return") is not None]
    veto_mfe = [row.get("mfe_return") for row in veto if row.get("mfe_return") is not None]
    lines = [
        "# A股轮动：今日问题汇总与V4信号强度复盘",
        "",
        f"生成时间：{result['generated_at']}",
        f"复盘交易日：{result['trade_date']}；日线事前截面：{result['daily_asof']}；Tick证据截止：{result['data_asof']}",
        "",
        "> 口径：使用盘中实时服务每5秒落盘的证据样本近似回放，不是完整逐笔Tick；所有V4触发只使用对应时点及更早数据。",
        "",
        "## 一、今天确认的问题与修正状态",
        "",
        "| 问题 | 今日证据 | V4处理 |",
        "|---|---|---|",
        "| 信号过度恐高 | 高J、月线高位、5日涨幅和高开被多层硬否决 | 高位改为保护/趋势路线；只有组合转弱才退出 |",
        "| 高开3%永久VETO | 多只同板块共振上涨仍被GAP_TOO_HIGH挡住 | 高开降为CAUTION；同板块共振可升级SUPPORT |",
        "| 日内只允许极少数A_PRIORITY | 盘前BUY为0时全天结构性无法产生买点 | TREND_CONTINUATION等路线可经实时板块扩散升级 |",
        "| 所有买点强迫深回踩 | 趋势路线历史回放明显优于反转，但等待回踩损失价格 | 新增TREND_EXPANSION承接事件 |",
        "| J≥60直接卖出 | 多个卖点后继续出现可观MFE | J只提高保护级别；卖点需要价格+动量/背离组合 |",
        "| 盘中总结难读 | 代码、内部状态、长小数堆叠，无板块梯队 | 名称优先、四段式总结、强度/板块/角色/变化 |",
        "| 日内涨幅参考错位 | 旧代码用candidate.pre_close，可能实际对应D-2 | 改用candidate.close/真实昨收，VWAP偏离替代固定5%恐高线 |",
        "",
        "## 二、V3与V4盘前截面对比",
        "",
        f"- V3动作：{result['v3_action_counts']}",
        f"- V4动作：{result['v4_action_counts']}",
        f"- V4路线：{result['v4_route_counts']}",
        "- V4的BUY仍然只是允许进入Tick验证的盘前候选，不是订单，也不代表开盘立即买入。",
        "",
        "## 三、板块健康与梯队",
        "",
        "| 板块 | 状态 | 强度 | 上涨/观察 | 站上VWAP | 中位涨幅 | 梯队代表 |",
        "|---|---|---:|---:|---:|---:|---|",
    ]
    state_cn = {"EXPANSION": "扩散强势", "HEALTHY_TREND": "健康趋势", "IGNITION": "点火", "CONCENTRATED": "集中/脆弱", "DIVERGING": "分歧", "DECAY": "退潮", "NEUTRAL": "中性"}
    role_cn = {"LEADER": "龙头", "FRONT": "前排", "CORE": "中军", "FOLLOWER": "跟随", "LAGGARD": "掉队"}
    for group in result["sector_groups"][:10]:
        reps = "、".join(f"{row.get('name')}({role_cn.get(row.get('role'), row.get('role'))})" for row in group.get("members", [])[:5])
        lines.append(
            f"| {group.get('theme')} | {state_cn.get(group.get('state'), group.get('state'))} | {group.get('score')} | "
            f"{group.get('up_count')}/{group.get('observed_count')} | {group.get('above_vwap_count')}/{group.get('observed_count')} | "
            f"{_percent(group.get('median_return'))} | {reps} |"
        )
    lines.extend([
        "",
        "## 四、当前信号强度排行",
        "",
        "| 排名 | 股票 | V4路线 | 日线强度 | 实时强度 | 当日涨幅 | 板块/角色 | 慢J |",
        "|---:|---|---|---:|---:|---:|---|---:|",
    ])
    for index, row in enumerate(result["signal_table"][:15], 1):
        lines.append(
            f"| {index} | {row['name']}（{row['symbol'].split('.')[-1]}） | {row.get('daily_route')} | "
            f"{row.get('daily_strength')} | {row.get('live_strength')} | {_percent(row.get('latest_return'))} | "
            f"{row.get('sector') or '样本不足'}/{role_cn.get(row.get('role'), row.get('role') or '待分类')} | {_safe_float(row.get('slow_j')):.1f} |"
        )
    lines.extend([
        "",
        "## 五、旧竞价VETO的机会成本",
        "",
        f"- 旧VETO共{len(veto)}只；截至证据截止时上涨{sum(value > 0 for value in veto_latest)}/{len(veto_latest)}只。",
        f"- 旧VETO平均当前涨幅：{_percent(mean(veto_latest) if veto_latest else None)}；平均盘中MFE：{_percent(mean(veto_mfe) if veto_mfe else None)}。",
        "",
        "| 股票 | 旧否决原因 | 竞价涨幅 | 截止时涨幅 | 盘中MFE | 盘中MAE |",
        "|---|---|---:|---:|---:|---:|",
    ])
    for row in sorted(veto, key=lambda item: -_safe_float(item.get("latest_return"))):
        lines.append(
            f"| {row.get('name')}（{row.get('symbol','').split('.')[-1]}） | {row.get('old_label')} | {_percent(row.get('auction_gap'))} | "
            f"{_percent(row.get('latest_return'))} | {_percent(row.get('mfe_return'))} | {_percent(row.get('mae_return'))} |"
        )
    lines.extend([
        "",
        "## 六、旧卖点后的继续上涨空间",
        "",
        "| 股票 | 旧卖点时刻 | 卖点价格 | 卖后MFE | 卖后MAE | 至截止时 |",
        "|---|---|---:|---:|---:|---:|",
    ])
    for row in result["old_sell_review"]:
        lines.append(
            f"| {row.get('name') or row.get('symbol')} | {str(row.get('event_ts'))[11:19]} | {_safe_float(row.get('price')):.3f} | "
            f"{_percent(row.get('post_mfe'))} | {_percent(row.get('post_mae'))} | {_percent(row.get('to_latest'))} |"
        )
    lines.extend([
        "",
        "## 七、V4影子事件回放",
        "",
        f"- 事件数：{result['v4_event_summary']['events']}；截至收盘/证据截止时上涨：{result['v4_event_summary']['positive_to_latest']}/{result['v4_event_summary']['events']}。",
        f"- 平均至截止收益：{_percent(result['v4_event_summary']['mean_to_latest'])}；中位数：{_percent(result['v4_event_summary']['median_to_latest'])}；平均事件后MFE：{_percent(result['v4_event_summary']['mean_post_mfe'])}。",
        f"- 形态分布：{result['v4_event_summary']['pattern_counts']}。这些是当日零成本影子结果，不是T+1完整交易收益。",
        f"- 强度分层校准：{result['v4_event_summary']['strength_calibration']}。单日只用于诊断排序是否失真，不能据此调参后宣称有效。",
        "",
        "| 股票 | 事件时刻 | 类型 | 价格 | 强度 | 板块状态/角色 | 事件后MFE | 至截止时 |",
        "|---|---|---|---:|---:|---|---:|---:|",
    ])
    for row in result["v4_event_review"]:
        sector = row.get("live_sector") or {}
        lines.append(
            f"| {row.get('name')}（{row.get('symbol','').split('.')[-1]}） | {str(row.get('event_ts'))[11:19]} | {row.get('pattern')} | "
            f"{_safe_float(row.get('price')):.3f} | {row.get('composite_signal_strength', row.get('live_signal_strength'))} "
            f"(位置{row.get('entry_quality','NA')}) | "
            f"{state_cn.get(sector.get('state'), sector.get('state'))}/{role_cn.get(sector.get('role'), sector.get('role'))} | "
            f"{_percent(row.get('post_mfe'))} | {_percent(row.get('to_latest'))} |"
        )
    lines.extend([
        "",
        "## 八、判断边界",
        "",
        "- 本报告验证的是V4是否修复结构性漏信号和过早卖出，不是最终盈利证明。",
        "- 今日已被用于发现问题，不能再作为V4的独立样本外验收日。",
        "- V4事件数量增加不等于质量提高；收盘后必须继续计算T+1路径、MAE、MFE、板块状态变化和相对沪深300超额。",
        "- 本报告的历史盘中梯队仍是精选池代理；V4实时服务已接入打板策略同口径的全市场板块涨幅、排名和主力净流入背景。二者会在飞书中分别标注，绝不把精选池梯队代理伪装成全市场健康度。",
    ])
    return "\n".join(lines) + "\n"


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--date", default=datetime.now().strftime("%Y-%m-%d"))
    args = parser.parse_args()
    result = build_review(args.date)
    stamp = args.date.replace("-", "")
    json_path = ROOT / "reports" / f"today_signal_strength_review_v4_{stamp}.json"
    md_path = ROOT / "reports" / f"today_signal_strength_review_v4_{stamp}.md"
    json_path.write_text(json.dumps(result, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    md_path.write_text(render_markdown(result), encoding="utf-8")
    print(json.dumps({
        "json": str(json_path), "markdown": str(md_path),
        "data_asof": result["data_asof"],
        "v4_events": len(result["v4_event_review"]),
        "old_veto": len(result["old_veto_review"]),
    }, ensure_ascii=False))


if __name__ == "__main__":
    main()
