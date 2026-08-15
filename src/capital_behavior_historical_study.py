# coding: utf-8
"""V8大周期资金结构的事前历史分层研究。

复用V7入场位置样本，只增加D-1可计算的资金结构分层；不使用历史五档，因此只验证
大周期先验有没有筛选价值，不把结果伪装成完整资金行为策略回测。
"""

from __future__ import annotations

import json
from collections import defaultdict
from pathlib import Path
from typing import Any, Dict, List

from historical_pullback_reclaim_study import _pct, _summary, build_study


ROOT = Path(r"D:\codex\a_share_rotation")
REPORT_JSON = ROOT / "reports" / "historical_capital_behavior_study_20260511_20260807.json"
REPORT_MD = ROOT / "reports" / "historical_capital_behavior_study_20260511_20260807.md"


def build_capital_study() -> Dict[str, Any]:
    base = build_study()
    events = base.get("events") or []
    reclaim_events = [row for row in events if row.get("reclaim_found") and row.get("reclaim_entry_price")]

    def supported(row: Dict[str, Any]) -> bool:
        capital = row.get("capital_structure") or {}
        return bool(
            float(capital.get("score") or 0) >= 60
            and capital.get("phase") in {"MARKUP", "REACCUMULATION", "ACCUMULATION"}
        )

    supported_armed = [row for row in events if supported(row)]
    supported_reclaim = [row for row in reclaim_events if supported(row)]
    phase_rows: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
    for row in events:
        phase_rows[str((row.get("capital_structure") or {}).get("phase") or "UNKNOWN")].append(row)

    base.update({
        "study_version": "v8_structural_capital_mechanical_high_score_audit",
        "capital_supported_armed_count": len(supported_armed),
        "capital_supported_reclaim_count": len(supported_reclaim),
        "capital_supported_baseline_summary": _summary(supported_armed, "baseline"),
        "capital_supported_reclaim_summary": _summary(supported_reclaim, "reclaim"),
        "capital_phase_summary": {
            phase: {
                "count": len(rows),
                "baseline": _summary(rows, "baseline"),
                "reclaim": _summary(
                    [row for row in rows if row.get("reclaim_found") and row.get("reclaim_entry_price")],
                    "reclaim",
                ),
            }
            for phase, rows in sorted(phase_rows.items())
        },
    })
    base["limitations"] = list(base.get("limitations") or []) + [
        "本研究只验证D-1日线/周线资金结构先验；历史分钟数据没有五档，不能复原实盘Tick资金行为分。",
        "资金结构阈值由当前样本提出，尚未经过独立样本外冻结验证。",
    ]
    return base


def render(result: Dict[str, Any]) -> str:
    raw = result["baseline_summary"]
    supported = result["capital_supported_baseline_summary"]
    raw_reclaim = result["reclaim_summary"]
    supported_reclaim = result["capital_supported_reclaim_summary"]

    def row(label: str, summary: Dict[str, Any], horizon: str) -> str:
        item = summary.get(horizon) or {}
        pf = item.get("profit_factor")
        return (
            f"| {label} | {item.get('n', 0)} | {_pct(item.get('mean'))} | {_pct(item.get('win_rate'))} | "
            f"{'NA' if pf is None else f'{pf:.2f}'} | {_pct(item.get('mean_excess_vs_csi300_open_proxy'))} |"
        )

    lines = [
        "# V8大周期资金结构历史分层研究：禁止把结构高分等同于买点",
        "",
        f"区间：{result['period']['start']} 至 {result['period']['end']}；高位武装事件{result['armed_event_count']}个，"
        f"其中机械高分组{result['capital_supported_armed_count']}个。",
        "",
        "## 1. 高位武装后直接处理",
        "",
        "| 分组 | D1样本 | D1平均 | D1胜率 | PF | 沪深300近似超额 |",
        "|---|---:|---:|---:|---:|---:|",
        row("全部", raw, "d1"),
        row("机械高分（已否决为直接加分）", supported, "d1"),
        "",
        "## 2. 完成回踩—收复后处理",
        "",
        "| 分组 | D1样本 | D1平均 | D1胜率 | PF | 沪深300近似超额 |",
        "|---|---:|---:|---:|---:|---:|",
        row("全部", raw_reclaim, "d1"),
        row("机械高分（仅分层观察）", supported_reclaim, "d1"),
        "",
        "## 3. 各资金阶段D1结果",
        "",
    ]
    for phase, payload in result["capital_phase_summary"].items():
        item = (payload.get("baseline") or {}).get("d1") or {}
        lines.append(
            f"- {phase}：事件{payload.get('count', 0)}，D1样本{item.get('n', 0)}，"
            f"平均{_pct(item.get('mean'))}，胜率{_pct(item.get('win_rate'))}。"
        )
    lines.extend([
        "",
        "## 4. 解释边界",
        "",
        "- 结论：大周期结构适合解释所处场景和持仓职责，不具备被本样本支持的独立买入加分价值。实盘候选排序中的资金结构加分已固定为0。",
        "- MARKUP更像持仓延续语义，新开仓容易买在伸展段；低位吸收/平衡仍必须等待盘中资金、板块、分钟周期和位置共同确认。",
        *[f"- {item}" for item in result.get("limitations") or []],
        "",
    ])
    return "\n".join(lines)


def main() -> None:
    result = build_capital_study()
    REPORT_JSON.write_text(json.dumps(result, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    REPORT_MD.write_text(render(result), encoding="utf-8")
    print(json.dumps({
        "json": str(REPORT_JSON), "markdown": str(REPORT_MD),
        "armed": result["armed_event_count"],
        "capital_supported_armed": result["capital_supported_armed_count"],
        "reclaim": result["reclaim_event_count"],
        "capital_supported_reclaim": result["capital_supported_reclaim_count"],
        "baseline_d1": result["baseline_summary"]["d1"],
        "capital_supported_baseline_d1": result["capital_supported_baseline_summary"]["d1"],
        "reclaim_d1": result["reclaim_summary"]["d1"],
        "capital_supported_reclaim_d1": result["capital_supported_reclaim_summary"]["d1"],
    }, ensure_ascii=False, indent=2, default=str))


if __name__ == "__main__":
    main()
