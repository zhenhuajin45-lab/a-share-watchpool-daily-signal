#!/usr/bin/env python3
"""Capture a read-only 09:15-09:25 GM call-auction market snapshot."""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import math
import os
import sys
from pathlib import Path
from typing import Any, Iterable

import pandas as pd


INDEX_SYMBOLS = {"SHSE.000001": "上证指数", "SZSE.399001": "深证成指", "SZSE.399006": "创业板指", "SHSE.000688": "科创50"}


def chunks(values: list[str], size: int) -> Iterable[list[str]]:
    for index in range(0, len(values), size):
        yield values[index : index + size]


def compact_date(value: Any) -> str:
    text = "".join(character for character in str(value or "") if character.isdigit())
    return text[:8]


def compact_time(value: Any) -> str:
    text = "".join(character for character in str(value or "") if character.isdigit())
    return text[8:14] if len(text) >= 14 else ""


def finite(value: Any) -> float | None:
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    return result if math.isfinite(result) else None


def load_json(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"expected JSON object: {path}")
    return value


def read_evidence_frame(bundle: dict[str, Any], key: str) -> pd.DataFrame:
    row = (bundle.get("evidence") or {}).get(key) or {}
    path = Path(str(row.get("path") or ""))
    if not path.is_file():
        raise FileNotFoundError(f"GM bundle evidence missing: {key}:{path}")
    if row.get("sha256") and hashlib.sha256(path.read_bytes()).hexdigest() != row["sha256"]:
        raise ValueError(f"GM bundle evidence hash mismatch: {key}")
    return pd.read_csv(path, compression="gzip")


def normalize_ticks(rows: list[dict[str, Any]], previous_close: pd.DataFrame) -> pd.DataFrame:
    ticks = pd.DataFrame(rows)
    if ticks.empty or "symbol" not in ticks:
        return pd.DataFrame()
    for column in ("price", "cum_amount"):
        ticks[column] = pd.to_numeric(ticks.get(column), errors="coerce")
    ticks["snapshot_date"] = ticks.get("created_at", pd.Series(dtype=str)).map(compact_date)
    ticks["snapshot_time"] = ticks.get("created_at", pd.Series(dtype=str)).map(compact_time)
    ticks = ticks.merge(previous_close[["symbol", "previous_close"]], on="symbol", how="left")
    ticks["gap_pct"] = (ticks["price"] / ticks["previous_close"] - 1.0) * 100.0
    return ticks


def build_snapshot(
    ticks: pd.DataFrame,
    memberships: pd.DataFrame,
    execution_date: str,
    universe_count: int,
) -> dict[str, Any]:
    valid = ticks[
        (ticks["snapshot_date"] == execution_date)
        & ticks["snapshot_time"].between("091500", "092500", inclusive="both")
        & ticks["gap_pct"].notna()
    ].copy() if not ticks.empty else pd.DataFrame()
    coverage = len(valid) / universe_count if universe_count else 0.0
    rise = int((valid["gap_pct"] > 0.001).sum()) if not valid.empty else 0
    fall = int((valid["gap_pct"] < -0.001).sum()) if not valid.empty else 0
    flat = int(len(valid) - rise - fall)
    sectors: list[dict[str, Any]] = []
    if not valid.empty and not memberships.empty:
        merged = memberships[["symbol", "sector_code", "sector_name", "group_type"]].drop_duplicates().merge(valid[["symbol", "gap_pct", "cum_amount"]], on="symbol", how="inner")
        for (code, name, group_type), part in merged.groupby(["sector_code", "sector_name", "group_type"], sort=False):
            members = int(part["symbol"].nunique())
            if members < (8 if group_type == "CONCEPT" else 20):
                continue
            sectors.append({
                "sector_code": str(code),
                "sector_name": str(name),
                "group_type": str(group_type),
                "observed_count": members,
                "auction_return_pct": round(float(part["gap_pct"].median()), 4),
                "auction_up_ratio": round(float((part["gap_pct"] > 0).mean()), 4),
                "auction_cum_amount_yi": round(float(part["cum_amount"].fillna(0).sum() / 100_000_000), 2),
            })
        sectors.sort(key=lambda row: (-float(row["auction_return_pct"]), -float(row["auction_up_ratio"]), str(row["sector_name"])))
        for rank, row in enumerate(sectors, 1):
            row["auction_rank"] = rank
    return {
        "schema_version": "gm_opening_auction_snapshot_v1",
        "status": "READY" if coverage >= 0.85 else "UNAVAILABLE",
        "source": "GM_CURRENT_INCLUDE_CALL_AUCTION",
        "execution_trade_date": execution_date,
        "breadth": {"rise_count": rise, "fall_count": fall, "flat_count": flat, "observed_count": len(valid), "universe_count": universe_count, "coverage": round(coverage, 4)},
        "sectors": sectors[:160],
    }


def write_json_atomic(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2, default=str) + "\n", encoding="utf-8")
    temporary.replace(path)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--execution-date", required=True)
    parser.add_argument("--previous-bundle", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--raw-output", type=Path, required=True)
    parser.add_argument("--batch-size", type=int, default=300)
    args = parser.parse_args()
    execution_date = compact_date(args.execution_date)
    payload: dict[str, Any] = {
        "schema_version": "gm_opening_auction_bundle_v1",
        "captured_at": dt.datetime.now().astimezone().isoformat(),
        "execution_trade_date": execution_date,
        "status": "UNAVAILABLE",
        "source_health": {"token_present": bool(os.environ.get("GM_TOKEN")), "errors": []},
    }
    raw_ticks = pd.DataFrame()
    try:
        from gm.api import current, set_token  # type: ignore

        token = os.environ.get("GM_TOKEN", "").strip()
        if not token:
            raise RuntimeError("GM_TOKEN missing")
        set_token(token)
        bundle = load_json(args.previous_bundle)
        if bundle.get("status") != "READY":
            raise ValueError("previous GM market bundle is not READY")
        bars = read_evidence_frame(bundle, "bars")
        industries = read_evidence_frame(bundle, "industries")
        concepts = read_evidence_frame(bundle, "concepts") if (bundle.get("evidence") or {}).get("concepts") else pd.DataFrame()
        bars["trade_date"] = bars["eob"].map(compact_date)
        previous_date = str(bundle.get("trade_date") or "")
        previous = bars[bars["trade_date"] == previous_date].copy()
        previous["previous_close"] = pd.to_numeric(previous["close"], errors="coerce")
        previous = previous.dropna(subset=["previous_close"]).drop_duplicates("symbol")
        symbols = sorted(previous["symbol"].astype(str).unique())
        frames: list[pd.DataFrame] = []
        for batch_index, batch in enumerate(chunks(symbols + list(INDEX_SYMBOLS), args.batch_size)):
            try:
                frames.append(pd.DataFrame(current(batch, fields="symbol,created_at,price,cum_amount,quotes", include_call_auction=True)))
            except Exception as exc:
                payload["source_health"]["errors"].append(f"batch:{batch_index}:{type(exc).__name__}:{str(exc)[:160]}")
        raw_ticks = pd.concat([frame for frame in frames if not frame.empty], ignore_index=True) if any(not frame.empty for frame in frames) else pd.DataFrame()
        ticks = normalize_ticks(raw_ticks, previous[["symbol", "previous_close"]])
        memberships = []
        if not industries.empty:
            memberships.append(industries.assign(group_type="SW2021_L1"))
        if not concepts.empty:
            memberships.append(concepts.assign(group_type="CONCEPT"))
        membership_frame = pd.concat(memberships, ignore_index=True) if memberships else pd.DataFrame()
        snapshot = build_snapshot(ticks[ticks["symbol"].isin(symbols)], membership_frame, execution_date, len(symbols))
        index_ticks = raw_ticks[raw_ticks.get("symbol", pd.Series(dtype=str)).isin(INDEX_SYMBOLS)].copy() if not raw_ticks.empty else pd.DataFrame()
        index_rows = []
        for row in index_ticks.to_dict("records"):
            index_rows.append({"symbol": row.get("symbol"), "name": INDEX_SYMBOLS.get(str(row.get("symbol"))), "created_at": row.get("created_at"), "price": row.get("price")})
        payload.update({"status": snapshot["status"], "market_snapshot": snapshot, "major_indices": index_rows})
        payload["source_health"].update({
            "ready": snapshot["status"] == "READY" and not payload["source_health"]["errors"],
            "include_call_auction": True,
            "previous_trade_date": previous_date,
            "tick_rows": len(raw_ticks),
            "snapshot_date_exact": snapshot["status"] == "READY",
        })
    except Exception as exc:
        payload["source_health"]["errors"].append(f"fatal:{type(exc).__name__}:{str(exc)[:400]}")
        payload["source_health"]["ready"] = False
    args.raw_output.parent.mkdir(parents=True, exist_ok=True)
    raw_ticks.to_csv(args.raw_output, index=False, encoding="utf-8-sig", compression="gzip")
    payload["evidence"] = {"raw_ticks": {"path": str(args.raw_output), "rows": len(raw_ticks), "sha256": hashlib.sha256(args.raw_output.read_bytes()).hexdigest()}}
    write_json_atomic(args.output, payload)
    print(json.dumps({"status": payload["status"], "ready": payload["source_health"].get("ready"), "errors": payload["source_health"].get("errors", [])[:3], "output": str(args.output)}, ensure_ascii=False))
    sys.stdout.flush()
    sys.stderr.flush()
    os._exit(0 if payload["source_health"].get("ready") else 2)


if __name__ == "__main__":
    main()
