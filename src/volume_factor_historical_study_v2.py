# coding: utf-8
"""量能V2机制历史研究：只评估因子，不把当前精选池伪装成历史可选股票池。"""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

import numpy as np
import pandas as pd

from live_signal_service import load_pool_entries
from volume_soft_factor import evaluate_volume_soft_factor


ROOT = Path(r"D:\codex\a_share_rotation")
DAILY_ROOT = ROOT / "data" / "goldminer" / "daily_adjust_prev_current"
OUTPUT_ROOT = ROOT / "research" / "volume_factor_v2_20260811"


def _category(factor: dict) -> str:
    if factor.get("top_persistent_contraction") and factor.get("top_volume_cliff"):
        return "TOP_CONTRACTION_AND_CLIFF"
    if factor.get("top_persistent_contraction"):
        return "TOP_PERSISTENT_CONTRACTION"
    if factor.get("top_volume_cliff"):
        return "TOP_VOLUME_CLIFF"
    if factor.get("top_context"):
        return "TOP_CONTEXT_NO_RETREAT"
    if factor.get("offensive_active") and factor.get("bottom_startup"):
        return "BOTTOM_STARTUP_OFFENSE"
    if factor.get("offensive_active"):
        return "BOTTOM_PATTERN_NO_STARTUP"
    if factor.get("bottom_context"):
        return "BOTTOM_NO_OFFENSE"
    if int(factor.get("raw_offensive_bonus", 0)) > 0:
        return "NON_BOTTOM_RAW_DISABLED"
    return "OTHER"


def main() -> None:
    records = []
    symbols = list(load_pool_entries())
    for symbol in symbols:
        path = DAILY_ROOT / f"{symbol}_1d.pkl"
        if not path.exists():
            continue
        frame = pd.read_pickle(path).copy()
        frame["eob"] = pd.to_datetime(frame["eob"], errors="coerce")
        frame = frame.dropna(subset=["eob", "open", "high", "low", "close", "volume"]).sort_values("eob").reset_index(drop=True)
        for index in range(59, len(frame) - 5):
            asof = frame.iloc[index]["eob"]
            if asof.date() < pd.Timestamp("2025-01-01").date():
                continue
            factor = evaluate_volume_soft_factor(frame.iloc[:index + 1])
            if factor.get("status") == "UNAVAILABLE":
                continue
            close = float(frame.iloc[index]["close"])
            future_close = {h: float(frame.iloc[index + h]["close"]) / close - 1.0 for h in (1, 3, 5)}
            future_low = float(frame.iloc[index + 1:index + 4]["low"].min()) / close - 1.0
            records.append({
                "symbol": symbol,
                "asof": asof.strftime("%Y-%m-%d"),
                "category": _category(factor),
                "status": factor.get("status"),
                "risk_level": factor.get("risk_level"),
                "bottom_startup": bool(factor.get("bottom_startup")),
                "top_persistent_contraction": bool(factor.get("top_persistent_contraction")),
                "top_volume_cliff": bool(factor.get("top_volume_cliff")),
                "range_position_60d": (factor.get("audit") or {}).get("range_position_60d"),
                "return_1d": future_close[1], "return_3d": future_close[3], "return_5d": future_close[5],
                "next_day_big_negative": future_close[1] <= -0.03,
                "max_adverse_low_3d": future_low,
            })
    detail = pd.DataFrame(records)
    summaries = []
    for category, group in detail.groupby("category"):
        summaries.append({
            "category": category,
            "samples": int(len(group)),
            "win_rate_1d": float((group["return_1d"] > 0).mean()),
            "win_rate_3d": float((group["return_3d"] > 0).mean()),
            "win_rate_5d": float((group["return_5d"] > 0).mean()),
            "mean_return_1d": float(group["return_1d"].mean()),
            "mean_return_3d": float(group["return_3d"].mean()),
            "mean_return_5d": float(group["return_5d"].mean()),
            "median_return_5d": float(group["return_5d"].median()),
            "next_day_big_negative_rate": float(group["next_day_big_negative"].mean()),
            "mean_max_adverse_low_3d": float(group["max_adverse_low_3d"].mean()),
        })
    summary = pd.DataFrame(summaries).sort_values("category")
    OUTPUT_ROOT.mkdir(parents=True, exist_ok=True)
    detail.to_csv(OUTPUT_ROOT / "event_detail.csv", index=False, encoding="utf-8-sig")
    summary.to_csv(OUTPUT_ROOT / "category_summary.csv", index=False, encoding="utf-8-sig")
    metadata = {
        "generated_at": datetime.now().isoformat(),
        "study_window": "2025-01-01 through available 2026-08-11 data",
        "price_adjustment": "ADJUST_PREV/front-adjusted",
        "symbols": len(symbols),
        "rows": len(detail),
        "no_lookahead": "factor only sees bars through asof; future bars are labels only",
        "important_limit": "current known combined pool creates survivorship/selection bias; this is factor-mechanism evidence, not a historical tradable portfolio return",
    }
    (OUTPUT_ROOT / "metadata.json").write_text(json.dumps(metadata, ensure_ascii=False, indent=2), encoding="utf-8")
    print(summary.to_json(orient="records", force_ascii=True))


if __name__ == "__main__":
    main()
