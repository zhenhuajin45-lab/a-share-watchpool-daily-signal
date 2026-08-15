# coding: utf-8
"""用2026-08-10真实落盘证据回放V5强趋势延续逻辑。

本脚本不发送飞书、不写虚拟持仓、不接触正在运行的GoldMiner进程。股票池和当天数据
已经参与规则发现，因此输出是发现性复盘，不是独立样本外证明。
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


STATUS_CN = {
    "OBSERVING": "观察开盘承接",
    "TESTING_ACCEPTANCE": "承接条件未齐",
    "ACCEPTED": "承接确认",
    "REACCELERATING": "承接后再加速",
    "FAILED_ACCEPTANCE": "承接失败",
    "DEGRADED": "延续性降级",
    "LIMIT_LOCKED": "接近涨停/成交性不足",
    "DATA_CONFLICT": "竞价数据冲突",
    "NOT_APPLICABLE": "非趋势延续路线",
}

PATTERN_CN = {
    "TREND_ACCEPTANCE": "强趋势承接确认",
    "TREND_REACCELERATION": "承接后再加速",
    "PULLBACK_RECLAIM": "趋势回踩后收复",
    "SAME_DAY_CONTINUATION_INVALIDATION": "当日延续失效预警",
}


def _pct(value: Optional[float]) -> str:
    return "不可用" if value is None else f"{value:+.2%}"


def _clock(value: Any) -> str:
    return str(value or "")[11:19]


def _load_v4_review(trade_date: str) -> Dict[str, Any]:
    path = ROOT / "reports" / f"today_signal_strength_review_v4_{trade_date.replace('-', '')}.json"
    if not path.exists():
        return {}
    return json.loads(path.read_text(encoding="utf-8"))


def build_review(trade_date: str) -> Dict[str, Any]:
    day_root = ROOT / "data" / "live_signal" / trade_date
    tick_records = _read_jsonl(day_root / "tick_samples.jsonl")
    auction_records = _read_jsonl(day_root / "auction_snapshots.jsonl")
    old_daily = _latest_daily_payload(_read_jsonl(day_root / "daily_candidates.jsonl"))
    daily_asof = str(old_daily.get("asof") or "2026-08-07")
    daily = _load_v4_daily(daily_asof)
    candidates = {row["symbol"]: row for row in daily.get("candidates", [])}

    service = LiveSignalService(
        notifier=FeishuNotifier(dry_run=True),
        advisor=DeepSeekAdvisor(key_file=ROOT / "data" / "missing_deepseek_key.txt"),
    )
    auction_analyzer = AuctionPathAnalyzer()
    for record in auction_records:
        snapshot = record.get("snapshot") or {}
        if snapshot.get("rows"):
            auction_analyzer.add_snapshot(snapshot)
    auction_analysis = (
        service._apply_auction_group_context(auction_analyzer.latest_analysis)
        if auction_analyzer.snapshots else {"rows": [], "by_symbol": {}}
    )
    auction_map = auction_analysis.get("by_symbol", {})

    observations = [
        record.get("observation") or {} for record in tick_records
        if (record.get("observation") or {}).get("symbol")
        and (record.get("observation") or {}).get("event_ts")
    ]
    observations.sort(key=lambda row: (str(row.get("event_ts")), str(row.get("symbol"))))
    sector = LiveSectorHealthEngine(load_taxonomy())
    sector.set_candidates(daily.get("candidates", []))
    continuation = TrendContinuationAnalyzer()
    intraday = IntradayEventEngine()
    virtual_positions: Dict[str, Dict[str, Any]] = {}
    events: List[Dict[str, Any]] = []
    continuation_timeline: Dict[str, List[Dict[str, Any]]] = {}

    for observation in observations:
        symbol = str(observation.get("symbol"))
        candidate = dict(candidates.get(symbol) or {})
        if not candidate:
            continue
        sector_context = sector.update(observation)
        auction = auction_map.get(symbol, {})
        continuation_context = continuation.update(observation, candidate, auction, sector_context)
        base_strength = LiveSignalService._live_strength(candidate, sector_context, auction)
        live_strength = (
            int(_safe_float(continuation_context.get("score"), base_strength))
            if candidate.get("daily_route") == "TREND_CONTINUATION" else base_strength
        )
        candidate.update({
            "live_sector": sector_context,
            "continuation_context": continuation_context,
            "live_signal_strength": live_strength,
        })
        if candidate.get("daily_route") == "TREND_CONTINUATION":
            candidate["intraday_eligible"] = bool(continuation_context.get("confirmed"))
            continuation_timeline.setdefault(symbol, []).append({
                "event_ts": continuation_context.get("asof"),
                "status": continuation_context.get("status"),
                "score": continuation_context.get("score"),
                "missing": continuation_context.get("missing"),
            })
        elif (
            candidate.get("daily_route") == "TREND_PULLBACK"
            and live_strength >= 60
            and sector_context.get("state") in {"IGNITION", "EXPANSION", "HEALTHY_TREND"}
            and not auction.get("hard_veto")
        ):
            candidate["intraday_eligible"] = True
        if symbol in virtual_positions:
            candidate.update({
                "action": "MONITOR_EXIT",
                "monitor_sell": True,
                "position_entry_date": virtual_positions[symbol]["entry_date"],
                "position_entry_price": virtual_positions[symbol]["entry_price"],
            })
        event = intraday.on_tick(_raw_tick(observation), candidate, auction_gate=auction)
        if not event:
            continue
        event["name"] = candidate.get("name", "")
        events.append(event)
        if event.get("event") == "BUY_EVENT_WATCH":
            virtual_positions[symbol] = {
                "entry_date": str(event.get("event_ts"))[:10],
                "entry_price": event.get("price"),
            }

    buy_events = [row for row in events if row.get("event") == "BUY_EVENT_WATCH"]
    opportunity_events = [row for row in events if row.get("event") == "OPPORTUNITY_EVENT_WATCH"]
    risk_events = [row for row in events if row.get("event") == "RISK_EVENT_WATCH"]
    buy_review = _post_event_stats(buy_events, observations)
    opportunity_review = _post_event_stats(opportunity_events, observations)
    paths = _path_stats(observations, candidates)
    final_context = continuation.snapshot().get("by_symbol", {})

    for event in buy_review:
        symbol = str(event.get("symbol"))
        prior_opportunity = next(
            (
                row for row in opportunity_review
                if row.get("symbol") == symbol and str(row.get("event_ts")) < str(event.get("event_ts"))
            ),
            None,
        )
        event["upgraded_from_opportunity_at"] = prior_opportunity.get("event_ts") if prior_opportunity else None
        later_risk = next(
            (row for row in risk_events if row.get("symbol") == symbol and str(row.get("event_ts")) > str(event.get("event_ts"))),
            None,
        )
        event["risk_event"] = later_risk
        if later_risk:
            begin = datetime.fromisoformat(str(event["event_ts"]))
            end = datetime.fromisoformat(str(later_risk["event_ts"]))
            event["seconds_to_invalidation"] = (end - begin).total_seconds()

    values = [row.get("to_latest") for row in buy_review if row.get("to_latest") is not None]
    mfes = [row.get("post_mfe") for row in buy_review if row.get("post_mfe") is not None]
    maes = [row.get("post_mae") for row in buy_review if row.get("post_mae") is not None]
    v4 = _load_v4_review(trade_date)
    v4_events = v4.get("v4_event_review", [])
    v5_symbols = {row.get("symbol") for row in buy_review + opportunity_review}
    filtered_v4 = [row for row in v4_events if row.get("symbol") not in v5_symbols]

    trend_table = []
    for symbol, candidate in candidates.items():
        if candidate.get("daily_route") != "TREND_CONTINUATION":
            continue
        context = final_context.get(symbol, {})
        trend_table.append({
            "symbol": symbol,
            "name": candidate.get("name", ""),
            "daily_strength": candidate.get("signal_strength"),
            "continuation_score": context.get("score"),
            "status": context.get("status"),
            "auction_score": (context.get("auction") or {}).get("score"),
            "sector_score": (context.get("sector") or {}).get("score"),
            "acceptance_score": context.get("acceptance_score"),
            "missing": context.get("missing") or [],
            **paths.get(symbol, {}),
        })
    trend_table.sort(key=lambda row: (-int(_safe_float(row.get("continuation_score"))), row["symbol"]))

    return {
        "generated_at": datetime.now().isoformat(),
        "trade_date": trade_date,
        "daily_asof": daily_asof,
        "data_asof": max((str(row.get("event_ts")) for row in observations), default=""),
        "tick_evidence_count": len(observations),
        "auction_snapshot_count": len(auction_records),
        "auction_snapshot_times": [str((row.get("snapshot") or {}).get("snapshot_at")) for row in auction_records],
        "rules_version": "daily_v4 + trend_continuation_v1 + intraday_event_v5",
        "buy_events": buy_review,
        "opportunity_events": opportunity_review,
        "risk_events": risk_events,
        "event_summary": {
            "count": len(buy_review),
            "positive_to_latest": sum(value > 0 for value in values),
            "mean_to_latest": mean(values) if values else None,
            "median_to_latest": median(values) if values else None,
            "mean_post_mfe": mean(mfes) if mfes else None,
            "mean_post_mae": mean(maes) if maes else None,
            "pattern_counts": dict(Counter(row.get("pattern") for row in buy_review)),
            "same_day_invalidations": len(risk_events),
            "opportunity_count": len(opportunity_review),
        },
        "v4_summary": v4.get("v4_event_summary", {}),
        "filtered_v4_events": filtered_v4,
        "trend_table": trend_table,
        "continuation_timeline": continuation_timeline,
        "limitations": [
            "今天已经参与规则发现，不能作为独立样本外验证日",
            "Tick来自每5秒落盘证据，不是完整逐笔回放",
            "竞价只有4个第三方报价代理快照，不是交易所逐笔委托队列",
            "今天没有历史全市场板块雷达快照；单例主题不会被假装成已获板块确认",
            "当日买入后的真实T+1卖出结果需要下一交易日数据才能验收",
        ],
    }


def render_markdown(result: Dict[str, Any]) -> str:
    summary = result["event_summary"]
    v4 = result.get("v4_summary") or {}
    lines = [
        "# A股轮动V5：强趋势延续性目标拆解与今日事前回放",
        "",
        f"生成时间：{result['generated_at']}",
        f"交易日：{result['trade_date']}；日线截止：{result['daily_asof']}；盘中证据截止：{result['data_asof']}",
        "",
        "> 本报告严格按时间顺序重放当天已落盘证据，但规则由今天的问题推动形成，因此属于发现性研究，不是样本外盈利证明。",
        "",
        "## 一、目标模式",
        "",
        "目标不是预测每一只上涨股票，而是在精选池中输出少量、可解释、可复核的正期望事件：D-1形成候选，集合竞价判断资金是否续接，盘中等待真实承接或承接后再加速；信号失效时立即预警，实际交易继续服从A股T+1。",
        "",
        "验收按以下顺序进行：",
        "",
        "1. 事前性：任何信号只能使用事件时点及更早数据。",
        "2. 延续质量：触发后MFE、MAE、收盘/后续收益和相对板块表现优于旧逻辑。",
        "3. 机会覆盖：不能因高J、高开或短期涨幅机械漏掉健康趋势。",
        "4. 风险识别：竞价脆弱、板块不扩散、跌破VWAP或个股掉队要延后/取消信号。",
        "5. 可执行性：涨停难成交、远离VWAP和T+1约束必须显式展示。",
        "",
        "## 二、问题—解决程度台账",
        "",
        "| 问题 | 本轮处理 | 当前程度 | 现实边界 |",
        "|---|---|---|---|",
        "| 高J/高开被机械否决 | 改成条件式趋势路线，高位只提高承接要求 | 已实现 | 仍需独立样本验证收益 |",
        "| 强趋势是否还能延续 | 新增日线、竞价、板块、盘中承接四段评分 | 已实现 | 分数尚未校准成胜率 |",
        "| 高开后开盘直冲被误认承接 | 高开/脆弱竞价必须完成首次压力测试，并在至少45秒后取得第二次四段确认 | 已实现 | 今日属于规则发现样本 |",
        "| 集合竞价只看最终高开 | 增加9:20后虚拟价格变化、盘口保持和同板块同步 | 已实现 | 仅4次第三方代理快照 |",
        "| 板块上涨但梯队不健康 | 使用宽度、VWAP比例、前排/中军/跟随/掉队 | 已实现 | 单例主题需要全市场板块雷达确认 |",
        "| 买点触发后迅速失效 | 新增当日延续失效预警，T+1不可卖时也必须提示 | 已实现 | 下一日卖出质量尚未验证 |",
        "| 信号强度是否等于胜率 | 明确只作排序并保存分项 | 部分解决 | 需要滚动样本外校准 |",
        "| 赚钱能力是否成立 | 保存毛收益、MFE、MAE、基准超额和T+1路径 | 尚未完成 | 至少需要多个未参与调参的交易日 |",
        "",
        "## 三、V5延续性决策链",
        "",
        "- 日线质量：趋势路线、慢线方向、MACD 5/10/5、顶背离、5日涨幅/ATR透支程度。",
        "- 竞价续接：最终缺口、9:20后价格保持、买盘金额保持、盘口方向、同主题竞价同步。",
        "- 板块健康：上涨宽度、站上VWAP宽度、梯队角色、个股相对板块强弱和全市场板块背景。",
        "- 盘中承接：至少120秒事实窗口、近180秒站上VWAP比例、竞价强度保持、首次压力测试/收复、至少45秒后的第二次确认、60秒与180秒动量、盘口卖压和位置透支。",
        "- 事件输出：只在四段同时成立时产生“承接确认/再加速”；随后失效则产生风险预警，不伪装成可卖成交。",
        "",
        "## 四、今日真实数据口径",
        "",
        f"- Tick证据样本：{result['tick_evidence_count']:,}条，原服务约每5秒落盘一次。",
        f"- 竞价快照：{result['auction_snapshot_count']}次，时点为：{'、'.join(result['auction_snapshot_times'])}。",
        "- 集合竞价为第三方公开报价代理；只有多源价格冲突是硬否决，其余都允许被盘中真实承接修复。",
        "- 今日没有事前保存全市场板块雷达快照，因此单例主题不会在回放中获得虚构的板块确认。",
        "",
        "## 五、新逻辑下今天会产生的买入观察信号",
        "",
        f"达到T+1新开仓观察门槛共{summary['count']}个；截至收盘/证据截止上涨{summary['positive_to_latest']}/{summary['count']}；平均至截止{_pct(summary['mean_to_latest'])}；中位数{_pct(summary['median_to_latest'])}；平均事件后MFE {_pct(summary['mean_post_mfe'])}；平均事件后MAE {_pct(summary['mean_post_mae'])}。",
        "",
        "| 时刻 | 股票 | 事件 | 综合强度 | 日线/竞价/板块/承接 | 事件后MFE | 事件后MAE | 至收盘 | 失效预警 |",
        "|---|---|---|---:|---|---:|---:|---:|---|",
    ]
    for row in result["buy_events"]:
        continuation = row.get("continuation") or {}
        components = (
            f"{(continuation.get('daily') or {}).get('score','-')}/"
            f"{(continuation.get('auction') or {}).get('score','-')}/"
            f"{(continuation.get('sector') or {}).get('score','-')}/"
            f"{continuation.get('acceptance_score','-')}"
        ) if continuation else "非延续路线"
        risk = row.get("risk_event")
        risk_text = (
            f"{_clock(risk.get('event_ts'))}，{int(_safe_float(row.get('seconds_to_invalidation')))}秒后"
            if risk else "未触发"
        )
        event_name = PATTERN_CN.get(row.get('pattern'), row.get('pattern'))
        if row.get("upgraded_from_opportunity_at"):
            event_name += f"（{_clock(row.get('upgraded_from_opportunity_at'))}由机会观察升级）"
        lines.append(
            f"| {_clock(row.get('event_ts'))} | {row.get('name')}（{str(row.get('symbol')).split('.')[-1]}） | "
            f"{event_name} | {row.get('composite_signal_strength')} | {components} | "
            f"{_pct(row.get('post_mfe'))} | {_pct(row.get('post_mae'))} | {_pct(row.get('to_latest'))} | {risk_text} |"
        )
    if not result["buy_events"]:
        lines.append("| - | 今日无满足四段确认的事件 | - | - | - | - | - | - | - |")

    lines.extend([
        "",
        "### 趋势机会/已有持仓做T参考（不视为T+1新开仓）",
        "",
        "| 时刻 | 股票 | 事件 | 综合强度 | 日线/竞价/板块/承接 | 事件后MFE | 事件后MAE | 至收盘 |",
        "|---|---|---|---:|---|---:|---:|---:|",
    ])
    for row in result["opportunity_events"]:
        continuation = row.get("continuation") or {}
        components = (
            f"{(continuation.get('daily') or {}).get('score','-')}/"
            f"{(continuation.get('auction') or {}).get('score','-')}/"
            f"{(continuation.get('sector') or {}).get('score','-')}/"
            f"{continuation.get('acceptance_score','-')}"
        ) if continuation else "非延续路线"
        lines.append(
            f"| {_clock(row.get('event_ts'))} | {row.get('name')}（{str(row.get('symbol')).split('.')[-1]}） | "
            f"{PATTERN_CN.get(row.get('pattern'), row.get('pattern'))} | {row.get('composite_signal_strength')} | {components} | "
            f"{_pct(row.get('post_mfe'))} | {_pct(row.get('post_mae'))} | {_pct(row.get('to_latest'))} |"
        )
    if not result["opportunity_events"]:
        lines.append("| - | 今日无降级机会事件 | - | - | - | - | - | - |")

    lines.extend([
        "",
        "## 六、与V4回放对比",
        "",
        f"- V4事件：{v4.get('events', '不可用')}个；平均至收盘{_pct(v4.get('mean_to_latest'))}；平均事件后MFE {_pct(v4.get('mean_post_mfe'))}。",
        f"- V5正式T+1新开仓观察：{summary['count']}个；趋势机会/持仓做T参考：{summary['opportunity_count']}个。正式信号平均至收盘{_pct(summary['mean_to_latest'])}，平均事件后MFE {_pct(summary['mean_post_mfe'])}。",
        f"- V5当日延续失效预警：{summary['same_day_invalidations']}个。预警不是卖出成交，T+1约束不变。",
        "",
        "### V4有、V5取消的事件",
        "",
        "| 股票 | V4时刻 | V4形态 | V4后MFE | 至收盘 | V5取消的现实原因 |",
        "|---|---|---|---:|---:|---|",
    ])
    trend_map = {row["symbol"]: row for row in result["trend_table"]}
    for row in result["filtered_v4_events"]:
        context = trend_map.get(row.get("symbol"), {})
        reason = "、".join((context.get("missing") or ["未通过新的四段延续确认"])[:3])
        lines.append(
            f"| {row.get('name')}（{str(row.get('symbol')).split('.')[-1]}） | {_clock(row.get('event_ts'))} | "
            f"{row.get('pattern')} | {_pct(row.get('post_mfe'))} | {_pct(row.get('to_latest'))} | {reason} |"
        )

    lines.extend([
        "",
        "## 七、收盘时趋势延续状态",
        "",
        "| 股票 | 日线强度 | 延续分 | 状态 | 竞价/板块/承接 | 当日涨幅 | 当前主要缺口 |",
        "|---|---:|---:|---|---|---:|---|",
    ])
    for row in result["trend_table"]:
        lines.append(
            f"| {row['name']}（{row['symbol'].split('.')[-1]}） | {row.get('daily_strength')} | {row.get('continuation_score')} | "
            f"{STATUS_CN.get(row.get('status'), row.get('status'))} | {row.get('auction_score')}/{row.get('sector_score')}/{row.get('acceptance_score')} | "
            f"{_pct(row.get('latest_return'))} | {'、'.join((row.get('missing') or ['四段条件满足'])[:3])} |"
        )

    lines.extend([
        "",
        "## 八、现实判断",
        "",
        "- 本轮解决的是“信号形成过程”而不是直接证明赚钱：延续性已经从单一KDJ/MACD升级为可追溯的四段事实链。",
        "- 新逻辑会显著减少开盘两分钟的冲动确认，并能在当日买点失效时给出风险预警；但若当天实际买入，预警不能绕过T+1变成卖单。",
        "- 单例主题在没有全市场板块历史快照时被保守降级，这是数据诚实性，不是永久否决；正式实时服务可使用已接入的全市场板块雷达补充确认。",
        "- 今天已经用于设计规则，任何看起来变好的数字都只能说明逻辑更贴近今天，下一步必须用后续未参与设计的交易日做冻结参数检验。",
        "- 真正的下一验收点是：冻结V5后连续记录信号、失效、T+1退出和沪深300/板块超额，不再根据单日输赢随意改阈值。",
        "",
        "## 九、仍未解决",
        "",
    ])
    lines.extend(f"- {item}" for item in result["limitations"])
    return "\n".join(lines) + "\n"


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--date", default=datetime.now().strftime("%Y-%m-%d"))
    args = parser.parse_args()
    result = build_review(args.date)
    stamp = args.date.replace("-", "")
    json_path = ROOT / "reports" / f"today_v5_continuation_replay_{stamp}.json"
    md_path = ROOT / "reports" / f"today_v5_continuation_replay_{stamp}.md"
    json_path.write_text(json.dumps(result, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    md_path.write_text(render_markdown(result), encoding="utf-8")
    print(json.dumps({
        "json": str(json_path), "markdown": str(md_path),
        "events": result["event_summary"]["count"],
        "opportunities": result["event_summary"]["opportunity_count"],
        "invalidations": result["event_summary"]["same_day_invalidations"],
        "mean_to_latest": result["event_summary"]["mean_to_latest"],
    }, ensure_ascii=False))


if __name__ == "__main__":
    main()
