"""Evidence-first author long/short ratio ledger for Windows migration."""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import math
from pathlib import Path
from typing import Any


VERIFIED_STATES = {"ARTICLE_TEXT_VERIFIED", "ARTICLE_IMAGE_VERIFIED", "CROSS_SOURCE_VERIFIED", "USER_CONFIRMED"}
ATTEMPT_STATES = {"NOT_FOUND", "ARTICLE_FOUND_RATIO_MISSING", "OCR_AMBIGUOUS", "AUTHOR_DID_NOT_PUBLISH", "NON_TRADING_DAY", "SOURCE_UNAVAILABLE"}


def load(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return value if isinstance(value, dict) else {}


def write(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temporary.replace(path)


def finite(value: Any) -> float | None:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return None
    return parsed if math.isfinite(parsed) and parsed >= 0 else None


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--ledger", type=Path, required=True)
    parser.add_argument("--trade-date", required=True)
    parser.add_argument("--ratio")
    parser.add_argument("--verification", default="ARTICLE_IMAGE_VERIFIED")
    parser.add_argument("--attempt-status")
    parser.add_argument("--source-url", default="")
    parser.add_argument("--article-title", default="")
    parser.add_argument("--evidence-text", default="")
    parser.add_argument("--evidence-path", default="")
    parser.add_argument("--allow-correction", action="store_true")
    args = parser.parse_args()

    trade_date = args.trade_date.replace("-", "")
    if len(trade_date) != 8 or not trade_date.isdigit():
        raise SystemExit("trade date must be YYYYMMDD")
    ratio = finite(args.ratio)
    if ratio is None and args.attempt_status not in ATTEMPT_STATES:
        raise SystemExit("provide --ratio or a valid --attempt-status")
    if ratio is not None and args.verification not in VERIFIED_STATES:
        raise SystemExit("invalid verification state")
    if ratio is not None and args.verification.startswith("ARTICLE_") and not args.source_url:
        raise SystemExit("article verification requires --source-url")

    ledger = load(args.ledger)
    observations = ledger.get("observations") if isinstance(ledger.get("observations"), list) else []
    attempts = ledger.get("attempts") if isinstance(ledger.get("attempts"), list) else []
    corrections = ledger.get("corrections") if isinstance(ledger.get("corrections"), list) else []
    by_date = {str(item.get("trade_date")): item for item in observations if isinstance(item, dict)}
    action = args.attempt_status or "VERIFIED_INSERTED"
    if ratio is not None:
        existing = by_date.get(trade_date)
        if existing and abs(float(existing.get("ratio")) - ratio) > 0.005 and not args.allow_correction:
            action = "CONFLICT_QUARANTINED"
        else:
            if existing and abs(float(existing.get("ratio")) - ratio) > 0.005:
                action = "VERIFIED_CORRECTION"
                corrections.append({
                    "trade_date": trade_date,
                    "old_ratio": existing.get("ratio"),
                    "new_ratio": round(ratio, 4),
                    "corrected_at": dt.datetime.now().astimezone().isoformat(),
                    "source_url": args.source_url or None,
                    "evidence_path": args.evidence_path or None,
                })
            row = {
                "trade_date": trade_date,
                "ratio": round(ratio, 4),
                "verification": args.verification,
                "source_url": args.source_url,
                "article_title": args.article_title or None,
                "evidence_path": args.evidence_path or None,
                "captured_at": dt.datetime.now().astimezone().isoformat(),
                "evidence_hash": hashlib.sha256(f"{trade_date}|{ratio}|{args.source_url}|{args.evidence_text}".encode("utf-8")).hexdigest(),
            }
            if action != "CONFLICT_QUARANTINED":
                by_date[trade_date] = row
                observations = [by_date[key] for key in sorted(by_date)]
    attempts.append({
        "trade_date": trade_date,
        "attempted_at": dt.datetime.now().astimezone().isoformat(),
        "status": action,
        "ratio": ratio,
        "source_url": args.source_url or None,
        "article_title": args.article_title or None,
        "evidence_path": args.evidence_path or None,
        "evidence_hash": hashlib.sha256(args.evidence_text.encode("utf-8")).hexdigest() if args.evidence_text else None,
    })
    ledger = {
        "schema_version": "author_long_short_ledger_v1",
        "updated_at": dt.datetime.now().astimezone().isoformat(),
        "observations": observations,
        "attempts": attempts[-120:],
        "corrections": corrections[-120:],
        "policy": "只有有证据的核验值进入序列；断更、缺失和OCR不清不填0、不猜曲线。",
    }
    write(args.ledger, ledger)
    print(json.dumps({"action": action, "trade_date": trade_date, "ratio": ratio, "ledger": str(args.ledger)}, ensure_ascii=False))
    return 2 if action == "CONFLICT_QUARANTINED" else 0


if __name__ == "__main__":
    raise SystemExit(main())
