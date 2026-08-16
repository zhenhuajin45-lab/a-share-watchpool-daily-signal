#!/usr/bin/env python3
"""Parse a calibrated Kaipanla UIA snapshot without OCR guessing."""

from __future__ import annotations

import argparse
import datetime as dt
import json
import re
from pathlib import Path
from typing import Any


def set_path(target: dict[str, Any], dotted: str, value: Any) -> None:
    cursor = target
    parts = dotted.split(".")
    for part in parts[:-1]:
        cursor = cursor.setdefault(part, {})
    cursor[parts[-1]] = value


def convert(value: str, kind: str) -> Any:
    cleaned = value.replace(",", "").replace("%", "").strip()
    if kind == "int":
        return int(float(cleaned))
    if kind == "float":
        return float(cleaned)
    return value.strip()


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--raw", type=Path, required=True)
    parser.add_argument("--calibration", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    raw = json.loads(args.raw.read_text(encoding="utf-8"))
    calibration = json.loads(args.calibration.read_text(encoding="utf-8"))
    page = str(raw.get("page_label") or "")
    page_map = (calibration.get("pages") or {}).get(page) or {}
    payload: dict[str, Any] = {
        "schema_version": "kaipanla_normalized_page_v1",
        "page_label": page,
        "trade_date": calibration.get("trade_date"),
        "normalized_at": dt.datetime.now().astimezone().isoformat(),
        "raw_evidence_path": str(args.raw),
        "screenshot_path": raw.get("screenshot_path"),
        "status": "CALIBRATION_REQUIRED",
        "values": {},
        "matches": [],
        "errors": [],
    }
    if raw.get("status") != "OK":
        payload["errors"].append(f"raw_capture_status:{raw.get('status') or 'UNAVAILABLE'}")
    elif page_map.get("calibrated") is not True:
        payload["errors"].append(f"page_not_calibrated:{page}")
    else:
        text = "\n".join(str(item.get("text") or "") for item in raw.get("elements") or [] if isinstance(item, dict))
        for field in page_map.get("fields") or []:
            match = re.search(str(field.get("pattern") or ""), text, flags=re.MULTILINE)
            if not match:
                if field.get("required", True):
                    payload["errors"].append(f"required_field_not_matched:{field.get('path')}")
                continue
            try:
                value = convert(match.group(int(field.get("group", 1))), str(field.get("type") or "text"))
                set_path(payload["values"], str(field["path"]), value)
                payload["matches"].append({"path": field["path"], "matched_text": match.group(0)})
            except (KeyError, IndexError, TypeError, ValueError) as exc:
                payload["errors"].append(f"parse_error:{field.get('path')}:{type(exc).__name__}")
        payload["status"] = "OK" if not payload["errors"] else "PARSE_FAILED"
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"status": payload["status"], "page_label": page, "errors": payload["errors"], "output": str(args.output)}, ensure_ascii=False))
    return 0 if payload["status"] == "OK" else 2


if __name__ == "__main__":
    raise SystemExit(main())
