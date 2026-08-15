# coding: utf-8
"""下载精选池前复权日线到D盘；凭证只从环境变量读取。"""

from __future__ import annotations

import json
import os
import re
from datetime import datetime
from pathlib import Path

import pandas as pd
from gm.api import ADJUST_PREV, history, set_token


ROOT = Path(r"D:\codex\a_share_rotation")
POOL_FILE = ROOT / "universe" / "selected_pool_20260809.txt"
OUTPUT = ROOT / "data" / "goldminer" / "daily_adjust_prev_20210101_20260807"


def load_pool() -> list[str]:
    pattern = re.compile(r"\|\s*((?:SHSE|SZSE)\.\d{6})\s*\|")
    symbols = []
    for line in POOL_FILE.read_text(encoding="utf-8").splitlines():
        match = pattern.search(line)
        if match and match.group(1) not in symbols:
            symbols.append(match.group(1))
    return symbols


def main() -> None:
    token = os.getenv("GOLDMINER_TOKEN", "").strip()
    if not token:
        raise SystemExit("缺少 GOLDMINER_TOKEN")
    set_token(token)
    symbols = load_pool()
    frame = history(
        ",".join(symbols),
        "1d",
        "2021-01-01",
        "2026-08-07 16:00:00",
        fields="symbol,eob,open,high,low,close,volume,amount,pre_close",
        adjust=ADJUST_PREV,
        df=True,
    )
    if frame is None or frame.empty:
        raise RuntimeError("GoldMiner未返回日线")
    frame["eob"] = pd.to_datetime(frame["eob"], errors="coerce")
    frame = frame.dropna(subset=["symbol", "eob", "high", "low", "close"]).sort_values(["symbol", "eob"])
    OUTPUT.mkdir(parents=True, exist_ok=True)
    coverage = {}
    for symbol in symbols:
        part = frame[frame["symbol"] == symbol].reset_index(drop=True)
        part.to_pickle(OUTPUT / f"{symbol}_1d.pkl")
        coverage[symbol] = {
            "rows": len(part),
            "start": part["eob"].min().isoformat() if len(part) else None,
            "end": part["eob"].max().isoformat() if len(part) else None,
        }
    metadata = {
        "generated_at": datetime.now().isoformat(),
        "source": "GoldMiner history",
        "frequency": "1d",
        "adjustment": "ADJUST_PREV/front-adjusted",
        "requested_start": "2021-01-01",
        "requested_end": "2026-08-07",
        "pool_size": len(symbols),
        "returned_symbols": int(frame["symbol"].nunique()),
        "coverage": coverage,
    }
    (OUTPUT / "metadata.json").write_text(json.dumps(metadata, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({"output": str(OUTPUT), "pool_size": len(symbols), "returned_symbols": frame["symbol"].nunique(), "rows": len(frame)}, ensure_ascii=False))


if __name__ == "__main__":
    main()
