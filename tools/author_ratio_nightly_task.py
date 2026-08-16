#!/usr/bin/env python3
"""Validate a nightly evidence handoff before appending the author-ratio ledger."""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import math
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
VERIFIED_STATES = {"ARTICLE_TEXT_VERIFIED", "ARTICLE_IMAGE_VERIFIED", "CROSS_SOURCE_VERIFIED", "USER_CONFIRMED"}
ATTEMPT_STATES = {"NOT_FOUND", "ARTICLE_FOUND_RATIO_MISSING", "OCR_AMBIGUOUS", "AUTHOR_DID_NOT_PUBLISH", "NON_TRADING_DAY", "SOURCE_UNAVAILABLE"}


def compact_date(value: object) -> str:
    return str(value or "").replace("-", "")


def verify_evidence_files(evidence: dict[str, object]) -> None:
    rows = evidence.get("evidence_files")
    if rows is None:
        return
    if not isinstance(rows, list) or not rows:
        raise ValueError("evidence_files must be a non-empty list")
    for row in rows:
        if not isinstance(row, dict):
            raise ValueError("each evidence file must be an object")
        path = Path(str(row.get("path") or ""))
        resolved = path if path.is_absolute() else ROOT / path
        if not resolved.is_file():
            raise ValueError(f"evidence file missing: {path}")
        expected_hash = str(row.get("sha256") or "").upper()
        actual_hash = hashlib.sha256(resolved.read_bytes()).hexdigest().upper()
        if len(expected_hash) != 64 or actual_hash != expected_hash:
            raise ValueError(f"evidence file hash mismatch: {path}")


def verify_ledger_conflicts(ledger_path: Path, candidates: list[tuple[str, float]]) -> None:
    try:
        ledger = json.loads(ledger_path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"existing ledger is unreadable: {ledger_path}") from exc
    rows = ledger.get("observations") if isinstance(ledger, dict) else None
    if rows is None:
        return
    if not isinstance(rows, list):
        raise ValueError("existing ledger observations must be a list")
    existing_by_date = {
        compact_date(row.get("trade_date")): row.get("ratio")
        for row in rows
        if isinstance(row, dict)
    }
    for trade_date, ratio in candidates:
        if trade_date not in existing_by_date:
            continue
        try:
            existing = float(existing_by_date[trade_date])
        except (TypeError, ValueError) as exc:
            raise ValueError(f"existing ledger ratio is invalid: {trade_date}") from exc
        if not math.isfinite(existing) or abs(existing - ratio) > 0.005:
            raise ValueError(f"existing ledger ratio conflicts with evidence: {trade_date}")


def build_commands(args: argparse.Namespace, evidence: dict[str, object]) -> list[list[str]]:
    primary_date = compact_date(args.trade_date)
    try:
        primary_day = dt.datetime.strptime(primary_date, "%Y%m%d").date()
    except ValueError as exc:
        raise ValueError("trade date must be YYYYMMDD") from exc
    evidence_text = json.dumps(evidence, ensure_ascii=False)
    verify_evidence_files(evidence)
    article_date = compact_date(evidence.get("article_date"))
    if article_date and article_date != primary_date:
        raise ValueError("article_date must match the requested trade date")
    shared_status = str(evidence.get("status") or "SOURCE_UNAVAILABLE")
    if shared_status == "ARTICLE_IMAGE_VERIFIED" and evidence.get("evidence_files") is None:
        raise ValueError("ARTICLE_IMAGE_VERIFIED requires evidence_files")
    source_url = str(evidence.get("source_url") or "")
    article_title = str(evidence.get("article_title") or "")
    raw_observations = evidence.get("observations")
    if raw_observations is not None and (not isinstance(raw_observations, list) or not raw_observations):
        raise ValueError("observations must be a non-empty list")
    observations = raw_observations if isinstance(raw_observations, list) else None
    if observations:
        commands: list[list[str]] = []
        candidates: list[tuple[str, float]] = []
        seen_dates: set[str] = set()
        for raw in observations:
            if not isinstance(raw, dict):
                raise ValueError("each observation must be an object")
            trade_date = compact_date(raw.get("trade_date"))
            try:
                observed_day = dt.datetime.strptime(trade_date, "%Y%m%d").date()
            except ValueError as exc:
                raise ValueError(f"invalid observation trade_date: {trade_date}") from exc
            if observed_day > primary_day or (primary_day - observed_day).days > 7:
                raise ValueError(f"observation date is outside article evidence window: {trade_date}")
            if trade_date in seen_dates:
                raise ValueError(f"duplicate observation trade_date: {trade_date}")
            seen_dates.add(trade_date)
            status = str(raw.get("verification") or shared_status)
            if status == "ARTICLE_IMAGE_VERIFIED" and evidence.get("evidence_files") is None:
                raise ValueError(f"ARTICLE_IMAGE_VERIFIED requires evidence_files: {trade_date}")
            ratio = raw.get("ratio")
            try:
                ratio_value = float(ratio)
            except (TypeError, ValueError):
                ratio_value = math.nan
            if status not in VERIFIED_STATES or not math.isfinite(ratio_value) or ratio_value < 0 or not source_url:
                raise ValueError(f"verified observation is incomplete: {trade_date}")
            candidates.append((trade_date, ratio_value))
            commands.append([
                sys.executable, str(ROOT / "adapters" / "author_ratio_ledger.py"),
                "--ledger", str(args.ledger), "--trade-date", trade_date,
                "--ratio", str(ratio_value), "--verification", status,
                "--source-url", source_url, "--article-title", article_title,
                "--evidence-path", str(args.evidence), "--evidence-text", evidence_text,
            ])
        if primary_date not in seen_dates:
            raise ValueError("observations must include the requested trade date")
        verify_ledger_conflicts(args.ledger, candidates)
        return commands

    command = [sys.executable, str(ROOT / "adapters" / "author_ratio_ledger.py"), "--ledger", str(args.ledger), "--trade-date", primary_date]
    if shared_status in VERIFIED_STATES and evidence.get("ratio") is not None:
        command += ["--ratio", str(evidence["ratio"]), "--verification", shared_status, "--source-url", source_url, "--article-title", article_title, "--evidence-path", str(args.evidence), "--evidence-text", evidence_text]
    else:
        attempt = shared_status if shared_status in ATTEMPT_STATES else "SOURCE_UNAVAILABLE"
        command += ["--attempt-status", attempt, "--source-url", source_url, "--article-title", article_title, "--evidence-path", str(args.evidence), "--evidence-text", evidence_text]
    return [command]


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--trade-date", required=True)
    parser.add_argument("--evidence", type=Path, required=True, help="Browser/OCR evidence JSON; never a guessed ratio")
    parser.add_argument("--ledger", type=Path, required=True)
    args = parser.parse_args()
    evidence = {}
    try:
        evidence = json.loads(args.evidence.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        evidence = {"status": "SOURCE_UNAVAILABLE"}
    try:
        commands = build_commands(args, evidence)
    except ValueError as exc:
        print(json.dumps({"status": "EVIDENCE_REJECTED", "error": str(exc)}, ensure_ascii=False), file=sys.stderr)
        return 2
    return_codes = [subprocess.run(command, check=False).returncode for command in commands]
    return max(return_codes, default=2)


if __name__ == "__main__":
    raise SystemExit(main())
