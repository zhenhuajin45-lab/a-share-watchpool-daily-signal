# coding: utf-8
"""按真实顺序Tick重放V16结构择时影子层，不发送飞书、不修改实盘状态。"""

from __future__ import annotations

import argparse
import json
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Iterable, Optional

import pandas as pd

from live_signal_service import LIVE_ROOT, MINUTE_LIVE_CACHE_ROOT
from multitimeframe_engine import MultiTimeframeIndicatorEngine
from signal_rules import compute_features
from structured_timing import StructuredTimingEngine, build_daily_timing_context


ROOT = Path(r"D:\codex\a_share_rotation")


def _json_default(value: Any) -> Any:
    if isinstance(value, (datetime, pd.Timestamp)):
        return value.isoformat()
    raise TypeError(type(value).__name__)


def _daily_from_minute(frame: pd.DataFrame, trade_date: str) -> pd.DataFrame:
    data = frame.copy()
    data["eob"] = pd.to_datetime(data["eob"], errors="coerce")
    if getattr(data["eob"].dt, "tz", None) is not None:
        data["eob"] = data["eob"].dt.tz_convert("Asia/Shanghai").dt.tz_localize(None)
    data = data[data["eob"].dt.strftime("%Y-%m-%d") < trade_date].copy()
    data["trade_date"] = data["eob"].dt.strftime("%Y-%m-%d")
    return data.groupby("trade_date", as_index=False).agg(
        symbol=("symbol", "last"), eob=("eob", "last"), open=("open", "first"),
        high=("high", "max"), low=("low", "min"), close=("close", "last"),
        volume=("volume", "sum"), amount=("amount", "sum"),
    )


def _last_json(path: Path) -> Dict[str, Any]:
    last = ""
    with path.open("r", encoding="utf-8") as handle:
        for line in handle:
            if line.strip():
                last = line
    return json.loads(last) if last else {}


def _safe_float(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _iter_jsonl(path: Path) -> Iterable[Dict[str, Any]]:
    with path.open("r", encoding="utf-8") as handle:
        for line_number, line in enumerate(handle, 1):
            try:
                yield json.loads(line)
            except Exception:
                continue


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--date", default="2026-08-13")
    args = parser.parse_args()
    trade_date = args.date
    day_root = LIVE_ROOT / trade_date
    tick_file = day_root / "tick_samples.jsonl"
    daily_file = day_root / "daily_candidates.jsonl"
    if not tick_file.exists() or not daily_file.exists():
        raise SystemExit(f"缺少重放证据：{tick_file} / {daily_file}")
    latest_daily = (_last_json(daily_file).get("payload") or {})
    candidate_map = {
        str(row.get("symbol")): row for row in latest_daily.get("candidates", []) if row.get("symbol")
    }
    mtf = MultiTimeframeIndicatorEngine()
    timing = StructuredTimingEngine()
    static_by_symbol: Dict[str, Dict[str, Any]] = {}
    for symbol, candidate in candidate_map.items():
        path = MINUTE_LIVE_CACHE_ROOT / f"{symbol}_1m.pkl"
        if not path.exists():
            continue
        frame = pd.read_pickle(path)
        frame["eob"] = pd.to_datetime(frame["eob"], errors="coerce")
        if getattr(frame["eob"].dt, "tz", None) is not None:
            frame["eob"] = frame["eob"].dt.tz_convert("Asia/Shanghai").dt.tz_localize(None)
        seed = frame[frame["eob"].dt.strftime("%Y-%m-%d") < trade_date].copy()
        mtf.seed(symbol, seed)
        timing.seed(symbol, seed)
        features = compute_features(_daily_from_minute(seed, trade_date), minimum=20)
        static = build_daily_timing_context(features)
        static_by_symbol[symbol] = static
        candidate["timing_static_context"] = static

    slots = ["10:00:00", "11:45:00", "14:00:00", "14:55:00", "15:00:00"]
    snapshots: Dict[str, Dict[str, Any]] = {}
    changes = []
    last_state: Dict[str, tuple] = {}
    final_prices: Dict[str, float] = {}
    observation_count = 0
    for payload in _iter_jsonl(tick_file):
        observation = payload.get("observation") or {}
        symbol = str(observation.get("symbol") or payload.get("symbol") or "")
        if symbol not in candidate_map:
            continue
        try:
            ts = pd.Timestamp(observation.get("event_ts")).to_pydatetime()
        except Exception:
            continue
        if ts.strftime("%Y-%m-%d") != trade_date:
            continue
        observation = dict(observation)
        observation["event_ts"] = ts
        observation_count += 1
        final_prices[symbol] = _safe_float(observation.get("price"))
        mtf_context = mtf.update(observation)
        candidate = dict(candidate_map[symbol])
        candidate["timing_static_context"] = static_by_symbol.get(symbol, {"status": "UNAVAILABLE"})
        sector = payload.get("live_sector") or payload.get("market_sector") or {}
        market_sector = payload.get("market_sector") or {}
        if market_sector:
            sector = dict(sector)
            sector.update({
                "market_board_percentile": market_sector.get("health_percentile"),
                "market_board_entry_support": market_sector.get("entry_support"),
                "market_board_rotation_caution": market_sector.get("rotation_caution"),
                "market_board_rank": market_sector.get("board_rank"),
            })
        capital = payload.get("capital_behavior") or {}
        continuation = payload.get("continuation") or {}
        context = timing.update(
            observation, candidate, mtf_context, sector=sector, capital=capital,
            continuation=continuation,
            market_permission={"new_entry_permission": "SELECTIVE"},
        )
        state_key = (
            context.get("path"), context.get("route_alignment_status"),
            context.get("shadow_entry_ready"), context.get("divergence_30_60"),
        )
        if last_state.get(symbol) != state_key:
            changes.append({
                "event_ts": ts.isoformat(), "symbol": symbol, "name": candidate.get("name"),
                "price": observation.get("price"), "path": context.get("path"),
                "route_alignment_status": context.get("route_alignment_status"),
                "shadow_entry_ready": context.get("shadow_entry_ready"),
                "room_atr": (context.get("location") or {}).get("room_atr"),
                "location": (context.get("location") or {}).get("state"),
                "setup_15m": (context.get("setup_15m") or {}).get("state"),
                "execution": (context.get("execution") or {}).get("state"),
                "divergence_30_60": context.get("divergence_30_60"),
            })
            last_state[symbol] = state_key
        current_clock = ts.strftime("%H:%M:%S")
        for slot in slots:
            if slot not in snapshots and current_clock >= slot:
                rows = [row for row in timing.snapshot().get("rows", []) if row.get("status") == "READY"]
                snapshots[slot] = {
                    "captured_at": ts.isoformat(),
                    "path_counts": dict(Counter(str(row.get("path")) for row in rows)),
                    "route_status_counts": dict(Counter(str(row.get("route_alignment_status")) for row in rows)),
                    "gap_hold_fast_track": [
                        {"symbol": row.get("symbol"), "name": candidate_map.get(str(row.get("symbol")), {}).get("name"), "score": row.get("shadow_score")}
                        for row in rows if row.get("shadow_entry_ready")
                    ],
                    "top_shadow": [
                        {
                            "symbol": row.get("symbol"),
                            "name": candidate_map.get(str(row.get("symbol")), {}).get("name"),
                            "score": row.get("shadow_score"), "path": row.get("path"),
                            "route_status": row.get("route_alignment_status"),
                            "room_atr": (row.get("location") or {}).get("room_atr"),
                            "divergence": row.get("divergence_30_60"),
                        }
                        for row in sorted(rows, key=lambda item: -int(_safe_float(item.get("shadow_score"))))[:6]
                    ],
                }

    for row in changes:
        close = final_prices.get(str(row.get("symbol")))
        price = _safe_float(row.get("price"))
        row["to_close"] = close / price - 1.0 if close and price > 0 else None
    fast_track = [row for row in changes if row.get("shadow_entry_ready")]
    failure_transitions = [
        row for row in changes if row.get("path") in StructuredTimingEngine.FAILURE_PATHS
    ]
    result = {
        "generated_at": datetime.now().isoformat(),
        "trade_date": trade_date,
        "daily_asof": latest_daily.get("asof"),
        "pool_count": len(candidate_map),
        "observation_count": observation_count,
        "rules_version": "structured_timing_v16_shadow_20260813",
        "strategy_effect": "NONE_SHADOW_ZERO_WEIGHT",
        "snapshots": snapshots,
        "state_change_count": len(changes),
        "gap_hold_fast_track_changes": fast_track,
        "failure_path_changes": failure_transitions,
        "changes": changes,
        "no_lookahead": True,
        "limitations": [
            "重放只复现V16结构择时影子层，不重发原正式信号，也不发送飞书。",
            "使用旧进程每5秒证据日志，不等同于交易所全逐笔；盘口字段沿用当时可见快照。",
            "D-1结构由前复权一分钟缓存聚合，范围约2026-05-11起，长周期压力带可能不完整。",
        ],
    }
    output_root = ROOT / "reports" / f"v16_structured_timing_replay_{trade_date.replace('-', '')}"
    output_root.mkdir(parents=True, exist_ok=True)
    output_json = output_root / "replay.json"
    output_json.write_text(json.dumps(result, ensure_ascii=False, indent=2, default=_json_default), encoding="utf-8")
    lines = [
        f"# V16结构择时影子顺序重放｜{trade_date}", "",
        f"顺序观察：{observation_count}；状态变化：{len(changes)}；GAP_HOLD快速研究候选变化：{len(fast_track)}；失败Path变化：{len(failure_transitions)}。",
        "",
    ]
    for slot, snapshot in snapshots.items():
        lines.extend([
            f"## {slot} 快照", "",
            f"Path：{snapshot['path_counts']}", "",
            f"路线状态：{snapshot['route_status_counts']}", "",
            "GAP_HOLD快速研究候选：" + (
                "、".join(f"{row['name']}({row['score']})" for row in snapshot["gap_hold_fast_track"])
                or "无"
            ), "",
        ])
    lines.extend(["## 重要状态变化", ""])
    for row in changes:
        if row.get("shadow_entry_ready") or row.get("path") in StructuredTimingEngine.FAILURE_PATHS:
            lines.append(
                f"- {row['event_ts'][11:19]} {row.get('name')}｜{row.get('path')}｜{row.get('route_alignment_status')}｜"
                f"Room {row.get('room_atr')}｜30/60 {row.get('divergence_30_60')}｜到收盘{_safe_float(row.get('to_close')):+.2%}"
            )
    lines.extend(["", "边界：全部为零权重影子重放，不代表正式买卖信号。", ""])
    output_md = output_root / "timeline.md"
    output_md.write_text("\n".join(lines), encoding="utf-8")
    print(json.dumps({
        "output_json": str(output_json), "output_md": str(output_md),
        "observation_count": observation_count, "state_change_count": len(changes),
        "fast_track_count": len(fast_track), "failure_transition_count": len(failure_transitions),
    }, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
