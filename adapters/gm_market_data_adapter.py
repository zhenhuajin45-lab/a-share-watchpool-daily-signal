"""Windows GM SDK adapter template for premarket market facts.

Run this file only in the Windows environment where the GM terminal and SDK are
installed. It writes normalized index bars and a lightweight SDK health block;
it never sends orders.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import sys
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path
from typing import Any


INDEX_SPECS = {
    "SHSE.000001": "上证指数",
    "SZSE.399001": "深证成指",
    "SZSE.399006": "创业板指",
    "SHSE.000688": "科创50",
}


def records(value: Any) -> list[dict[str, Any]]:
    if value is None:
        return []
    if hasattr(value, "to_dict"):
        return list(value.to_dict("records"))
    return [dict(item) if not isinstance(item, dict) else item for item in value]


def normalize_date(value: Any) -> str:
    text = str(value or "")
    return text[:10].replace("-", "")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--trade-date", required=True, help="YYYYMMDD")
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--count", type=int, default=80)
    args = parser.parse_args()

    payload: dict[str, Any] = {
        "schema_version": "gm_premarket_market_facts_v1",
        "trade_date": args.trade_date,
        "captured_at": dt.datetime.now().astimezone().isoformat(),
        "sdk_health": {"gm_import_ok": False, "token_present": bool(os.environ.get("GM_TOKEN"))},
        "major_indices": [],
    }
    try:
        from gm.api import ADJUST_NONE, history_n, set_token  # type: ignore

        payload["sdk_health"]["gm_import_ok"] = True
        try:
            payload["sdk_health"]["gm_version"] = version("gm")
        except PackageNotFoundError:
            payload["sdk_health"]["gm_version"] = "unknown"
        payload["sdk_health"]["history_n_contract"] = {
            "frequency": "1d",
            "count": args.count,
            "end_time": "source trade date 15:30:00",
            "skip_suspended": True,
            "adjust": "ADJUST_NONE",
        }
        token = os.environ.get("GM_TOKEN", "").strip()
        if not token:
            raise RuntimeError("GM_TOKEN missing")
        set_token(token)
        end_time = f"{args.trade_date[:4]}-{args.trade_date[4:6]}-{args.trade_date[6:]} 15:30:00"
        for symbol, name in INDEX_SPECS.items():
            try:
                raw = history_n(
                    symbol=symbol,
                    frequency="1d",
                    count=args.count,
                    end_time=end_time,
                    fields="symbol,eob,close,volume,amount",
                    skip_suspended=True,
                    adjust=ADJUST_NONE,
                    df=False,
                )
                bars = []
                for item in records(raw):
                    bars.append({
                        "date": normalize_date(item.get("eob") or item.get("bob")),
                        "close": item.get("close"),
                        "volume": item.get("volume"),
                        "amount": item.get("amount"),
                    })
                last_bar_date = bars[-1]["date"] if bars else None
                status = "OK" if len(bars) >= min(args.count, 20) and last_bar_date == args.trade_date else "STALE_OR_INCOMPLETE"
                payload["major_indices"].append({
                    "symbol": symbol,
                    "name": name,
                    "bars": bars,
                    "bar_count": len(bars),
                    "last_bar_date": last_bar_date,
                    "expected_last_bar_date": args.trade_date,
                    "status": status,
                })
            except Exception as exc:  # Preserve partial coverage.
                payload["major_indices"].append({"symbol": symbol, "name": name, "status": "UNAVAILABLE", "error": str(exc)[:240]})
    except Exception as exc:
        payload["sdk_health"]["error"] = str(exc)[:400]

    ok_indices = [item for item in payload["major_indices"] if item.get("status") == "OK"]
    payload["sdk_health"].update({
        "terminal_data_reachable": bool(ok_indices),
        "ok_index_count": len(ok_indices),
        "required_index_count": len(INDEX_SPECS),
        "ready": len(ok_indices) == len(INDEX_SPECS),
    })

    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(payload, ensure_ascii=False, indent=2, default=str) + "\n", encoding="utf-8")
    print(json.dumps({"output": str(args.output), "indices": len(payload["major_indices"]), "sdk_health": payload["sdk_health"]}, ensure_ascii=False))
    return 0 if payload["sdk_health"].get("ready") else 2


if __name__ == "__main__":
    exit_code = main()
    # gm 3.0.183 may normalize SystemExit during SDK shutdown on Windows.
    # Flush the evidence first, then preserve the health-check exit contract.
    sys.stdout.flush()
    sys.stderr.flush()
    os._exit(exit_code)
