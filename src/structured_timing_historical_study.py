# coding: utf-8
"""V16结构化择时影子层的事前历史切片验证。

输入沿用既有ARMED事件，特征只读取事件时点以前的一分钟数据和D-1日线；
D1/D3等结果仅作为事后标签。该脚本不修改实盘配置、不发送飞书。
"""

from __future__ import annotations

import json
import math
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Iterable, List

import numpy as np
import pandas as pd

from multitimeframe_engine import aggregate_completed_minutes, classify_period
from signal_rules import compute_features
from structured_timing import StructuredTimingEngine, build_daily_timing_context


ROOT = Path(r"D:\codex\a_share_rotation")
SOURCE = ROOT / "reports" / "historical_pullback_reclaim_study_20260511_20260807.json"
CACHE = ROOT / "data" / "goldminer" / "1m_20260511_20260807"
OUTPUT_JSON = ROOT / "reports" / "structured_timing_v16_historical_study_20260813.json"
OUTPUT_MD = ROOT / "reports" / "structured_timing_v16_historical_study_20260813.md"


def _safe_float(value: Any, default: float = float("nan")) -> float:
    try:
        result = float(value)
        return result if math.isfinite(result) else default
    except (TypeError, ValueError):
        return default


def _summary(rows: Iterable[Dict[str, Any]]) -> Dict[str, Any]:
    rows = list(rows)
    d1 = np.asarray([_safe_float(row.get("baseline_d1")) for row in rows], dtype=float)
    d3 = np.asarray([_safe_float(row.get("baseline_d3")) for row in rows], dtype=float)
    mae = np.asarray([_safe_float(row.get("baseline_mae_1d")) for row in rows], dtype=float)
    d1 = d1[np.isfinite(d1)]
    d3 = d3[np.isfinite(d3)]
    mae = mae[np.isfinite(mae)]
    gains = d1[d1 > 0].sum() if len(d1) else 0.0
    losses = -d1[d1 < 0].sum() if len(d1) else 0.0
    return {
        "count": len(rows),
        "d1_avg": float(d1.mean()) if len(d1) else None,
        "d1_win_rate": float((d1 > 0).mean()) if len(d1) else None,
        "d1_profit_factor": float(gains / losses) if losses > 0 else None,
        "d3_avg": float(d3.mean()) if len(d3) else None,
        "mae_1d_avg": float(mae.mean()) if len(mae) else None,
    }


def _period_context(prefix: pd.DataFrame) -> Dict[str, Any]:
    periods: Dict[str, Dict[str, Any]] = {}
    for minutes in (5, 15, 30, 60, 120):
        bars = aggregate_completed_minutes(prefix, minutes).tail(400)
        periods[str(minutes)] = classify_period(bars, minutes)
    active_bearish = [
        key for key in ("30", "60")
        if periods[key].get("macd_divergence") == "BEARISH"
        and periods[key].get("divergence_lifecycle") == "CONFIRMED_ACTIVE"
    ]
    active_bullish = [
        key for key in ("30", "60")
        if periods[key].get("macd_divergence") == "BULLISH"
        and periods[key].get("divergence_lifecycle") == "CONFIRMED_ACTIVE"
    ]
    if len(active_bearish) == 2:
        divergence = "BOTH_BEARISH"
    elif active_bearish:
        divergence = f"BEARISH_{active_bearish[0]}M"
    elif len(active_bullish) == 2:
        divergence = "BOTH_BULLISH"
    elif active_bullish:
        divergence = f"BULLISH_{active_bullish[0]}M"
    else:
        divergence = "NONE"
    one_twenty = periods["120"]
    return {
        "periods": periods,
        "divergence_30_60": divergence,
        "higher_timeframe_risk_shadow": bool(active_bearish),
        "one_twenty_minute_structure_shadow": {
            "status": "READY" if one_twenty.get("state") != "UNAVAILABLE" else "UNAVAILABLE",
            "state": one_twenty.get("state"),
            "score": one_twenty.get("score"),
            "ma225": one_twenty.get("ma225"),
            "ma225_ready": one_twenty.get("ma225") is not None,
            "warmup_bar_count": one_twenty.get("completed_bar_count", 0),
            "role": "LONG_STRUCTURE_SHADOW_NO_MA225_HARD_BLOCK",
        },
    }


def _load(path: Path) -> pd.DataFrame:
    frame = pd.read_pickle(path).copy()
    frame["eob"] = pd.to_datetime(frame["eob"], errors="coerce")
    if getattr(frame["eob"].dt, "tz", None) is not None:
        frame["eob"] = frame["eob"].dt.tz_convert("Asia/Shanghai").dt.tz_localize(None)
    return frame.dropna(subset=["eob"]).sort_values("eob").reset_index(drop=True)


def _fmt_pct(value: Any) -> str:
    number = _safe_float(value)
    return "—" if not math.isfinite(number) else f"{number:+.2%}"


def main() -> None:
    source = json.loads(SOURCE.read_text(encoding="utf-8"))
    frame_cache: Dict[str, Dict[str, pd.DataFrame]] = {}
    output_rows: List[Dict[str, Any]] = []
    for event in source.get("events", []):
        symbol = str(event.get("symbol") or "")
        if not symbol:
            continue
        if symbol not in frame_cache:
            minute_path = CACHE / f"{symbol}_1m.pkl"
            daily_path = CACHE / f"{symbol}_1d.pkl"
            if not minute_path.exists() or not daily_path.exists():
                frame_cache[symbol] = {}
            else:
                frame_cache[symbol] = {"minute": _load(minute_path), "daily": _load(daily_path)}
        frames = frame_cache[symbol]
        if not frames:
            continue
        event_ts = pd.Timestamp(event.get("armed_signal_ts"))
        daily_asof = pd.Timestamp(event.get("daily_asof"))
        daily = frames["daily"][frames["daily"]["eob"].dt.normalize() <= daily_asof.normalize()].copy()
        minute = frames["minute"]
        prefix = minute[minute["eob"] <= event_ts].copy()
        day_prefix = prefix[prefix["eob"].dt.normalize() == event_ts.normalize()].copy()
        if len(daily) < 20 or day_prefix.empty:
            continue
        features = compute_features(daily, minimum=20)
        if features is None or features.empty:
            continue
        static = build_daily_timing_context(features)
        mtf = _period_context(prefix)
        engine = StructuredTimingEngine()
        prior_minute = minute[minute["eob"].dt.normalize() < event_ts.normalize()]
        engine.seed(symbol, prior_minute)
        volume = pd.to_numeric(day_prefix.get("volume", 0), errors="coerce").fillna(0).sum()
        amount = pd.to_numeric(day_prefix.get("amount", 0), errors="coerce").fillna(0).sum()
        vwap = amount / volume if volume > 0 else _safe_float(event.get("armed_signal_price"))
        if vwap > _safe_float(event.get("armed_signal_price")) * 10:
            vwap /= 100.0
        observation = {
            "symbol": symbol,
            "event_ts": event_ts.to_pydatetime(),
            "price": _safe_float(event.get("armed_signal_price")),
            "vwap": vwap,
            "cum_volume": volume,
            "amount_imbalance": 0.0,
            "session_open_hint": _safe_float(day_prefix.iloc[0].get("open")),
            "completed_bar_high": _safe_float(day_prefix["high"].max()),
            "completed_bar_low": _safe_float(day_prefix["low"].min()),
        }
        candidate = {
            "symbol": symbol,
            "daily_route": "TREND_CONTINUATION",
            "close": static.get("reference_close"),
            "timing_static_context": static,
        }
        context = engine.update(observation, candidate, mtf)
        row = dict(event)
        row.update({
            "path_v16": context.get("path"),
            "room_atr_v16": (context.get("location") or {}).get("room_atr"),
            "location_v16": (context.get("location") or {}).get("state"),
            "setup_15m_v16": (context.get("setup_15m") or {}).get("state"),
            "execution_v16": (context.get("execution") or {}).get("state"),
            "clv_v16": (context.get("facts") or {}).get("clv"),
            "high_to_now_atr_v16": (context.get("facts") or {}).get("high_to_now_atr"),
            "divergence_30_60_v16": context.get("divergence_30_60"),
            "shadow_entry_ready_v16": context.get("shadow_entry_ready"),
            "shadow_score_v16": context.get("shadow_score"),
            "strategy_effect": context.get("strategy_effect"),
        })
        output_rows.append(row)

    healthy_paths = StructuredTimingEngine.POSITIVE_PATHS
    failure_paths = StructuredTimingEngine.FAILURE_PATHS
    groups = {
        "all": _summary(output_rows),
        "room_gt_2_atr": _summary(row for row in output_rows if _safe_float(row.get("room_atr_v16"), -99) > 2),
        "room_le_2_or_unknown": _summary(row for row in output_rows if _safe_float(row.get("room_atr_v16"), -99) <= 2),
        "healthy_path": _summary(row for row in output_rows if row.get("path_v16") in healthy_paths),
        "failure_path": _summary(row for row in output_rows if row.get("path_v16") in failure_paths),
        "high_clv_room_healthy": _summary(
            row for row in output_rows
            if _safe_float(row.get("room_atr_v16"), -99) > 2
            and row.get("path_v16") in healthy_paths
            and _safe_float(row.get("clv_v16"), -99) > 0.8
        ),
        "shadow_entry_ready": _summary(row for row in output_rows if row.get("shadow_entry_ready_v16")),
        "bearish_divergence_30_60": _summary(
            row for row in output_rows if str(row.get("divergence_30_60_v16") or "").startswith(("BEARISH", "BOTH_BEARISH"))
        ),
    }
    result = {
        "generated_at": datetime.now().isoformat(),
        "rules_version": "structured_timing_v16_shadow_20260813",
        "source_event_count": len(source.get("events", [])),
        "usable_event_count": len(output_rows),
        "feature_boundary": "D_MINUS_1_DAILY_PLUS_MINUTE_PREFIX_AT_OR_BEFORE_ARMED_EVENT",
        "strategy_effect": "NONE_SHADOW_ZERO_WEIGHT",
        "groups": groups,
        "rows": output_rows,
        "limitations": [
            "人工池在研究区间结束后确定，存在选池/幸存者偏差。",
            "该区间参与过规则发现，不是独立样本外。",
            "历史一分钟数据没有逐笔五档盘口，amount_imbalance固定为中性。",
            "当前验证不含手续费、滑点、成交可得性和组合资金曲线。",
            "shadow_entry_ready要求板块证据；历史切片缺少全市场板块时间序列，因此不会据此决定是否正式生产。",
        ],
    }
    OUTPUT_JSON.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")

    lines = [
        "# V16结构化择时影子层历史切片验证",
        "",
        f"可用事件：{len(output_rows)}/{len(source.get('events', []))}；特征边界：D-1日线 + 事件时点及以前的一分钟前缀。",
        "",
        "| 分组 | 样本 | D1平均 | D1胜率 | PF | D3平均 | 1日MAE |",
        "|---|---:|---:|---:|---:|---:|---:|",
    ]
    labels = {
        "all": "全部",
        "room_gt_2_atr": "Room>2ATR",
        "room_le_2_or_unknown": "Room<=2/未知",
        "healthy_path": "健康Path",
        "failure_path": "失败Path",
        "high_clv_room_healthy": "Room>2+健康Path+CLV>0.8",
        "shadow_entry_ready": "影子条件齐备",
        "bearish_divergence_30_60": "30/60分钟确认顶背离",
    }
    for key, value in groups.items():
        profit_factor_text = "—" if value["d1_profit_factor"] is None else f"{value['d1_profit_factor']:.2f}"
        lines.append(
            f"| {labels[key]} | {value['count']} | {_fmt_pct(value['d1_avg'])} | "
            f"{_fmt_pct(value['d1_win_rate'])} | "
            f"{profit_factor_text} | "
            f"{_fmt_pct(value['d3_avg'])} | {_fmt_pct(value['mae_1d_avg'])} |"
        )
    lines.extend([
        "",
        "## 结论边界",
        "",
        "本结果只用于判断V16实现是否值得继续影子运行；不自动把任何条件升级为正式买点。",
        "所有新增状态在实盘中的strategy_effect仍为NONE_SHADOW_ZERO_WEIGHT。",
        "",
        "## 限制",
        "",
        *[f"- {item}" for item in result["limitations"]],
    ])
    OUTPUT_MD.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(json.dumps({"json": str(OUTPUT_JSON), "markdown": str(OUTPUT_MD), "groups": groups}, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
