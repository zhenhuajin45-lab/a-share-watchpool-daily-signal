# coding: utf-8
"""把截至指定日期的精选池前复权日线写入D盘独立研究缓存。"""

from __future__ import annotations

import argparse
import json
import os
from datetime import datetime
from pathlib import Path

import pandas as pd
from gm.api import ADJUST_PREV, history, set_token

from live_signal_service import load_pool


ROOT = Path(r"D:\codex\a_share_rotation")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--end", default=datetime.now().strftime("%Y-%m-%d"))
    parser.add_argument("--output", default=str(ROOT / "data" / "goldminer" / "daily_adjust_prev_current"))
    args = parser.parse_args()
    token = os.getenv("GOLDMINER_TOKEN", "").strip()
    if not token:
        raise SystemExit("缺少GOLDMINER_TOKEN")
    set_token(token)
    symbols = load_pool()
    frame = history(
        ",".join(symbols), "1d", "2021-01-01", f"{args.end} 16:00:00",
        fields="symbol,eob,open,high,low,close,volume,amount,pre_close",
        skip_suspended=True, adjust=ADJUST_PREV, df=True,
    )
    if frame is None or frame.empty:
        raise RuntimeError("GoldMiner未返回日线")
    frame["eob"] = pd.to_datetime(frame["eob"], errors="coerce")
    frame = frame.dropna(subset=["symbol", "eob", "high", "low", "close"]).sort_values(["symbol", "eob"])
    output = Path(args.output)
    output.mkdir(parents=True, exist_ok=True)
    coverage = {}
    for symbol in symbols:
        part = frame[frame["symbol"] == symbol].reset_index(drop=True)
        part.to_pickle(output / f"{symbol}_1d.pkl")
        coverage[symbol] = {
            "rows": len(part),
            "end": part["eob"].max().isoformat() if len(part) else None,
        }
    metadata = {
        "generated_at": datetime.now().isoformat(), "source": "GoldMiner history",
        "frequency": "1d", "adjustment": "ADJUST_PREV/front-adjusted",
        "requested_end": args.end, "pool_size": len(symbols),
        "returned_symbols": int(frame["symbol"].nunique()), "coverage": coverage,
    }
    (output / "metadata.json").write_text(json.dumps(metadata, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({
        "output": str(output), "requested_end": args.end, "pool_size": len(symbols),
        "returned_symbols": int(frame["symbol"].nunique()), "rows": len(frame),
    }, ensure_ascii=True))


if __name__ == "__main__":
    main()
