"""Read-only Windows UI Automation capture template for Kaipanla.

This bridge intentionally only exports visible UI text. The Windows Codex must
calibrate window selectors against the installed Kaipanla version and then map
the raw snapshot into the normalized contracts described in docs/02.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
from pathlib import Path
from typing import Any


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--screenshot", type=Path)
    parser.add_argument("--page-label", required=True, choices=["market_emotion", "sector_rank", "interval_stats", "tomorrow_topics"])
    parser.add_argument("--title-regex", default=".*开盘啦.*")
    parser.add_argument("--max-elements", type=int, default=5000)
    args = parser.parse_args()

    payload: dict[str, Any] = {
        "schema_version": "kaipanla_windows_uia_raw_v1",
        "captured_at": dt.datetime.now().astimezone().isoformat(),
        "status": "UNAVAILABLE",
        "window_title": None,
        "page_label": args.page_label,
        "elements": [],
        "screenshot_path": str(args.screenshot) if args.screenshot else None,
        "error": None,
    }
    try:
        from pywinauto import Desktop  # type: ignore

        window = Desktop(backend="uia").window(title_re=args.title_regex)
        window.wait("visible", timeout=20)
        payload["window_title"] = window.window_text()
        if args.screenshot:
            args.screenshot.parent.mkdir(parents=True, exist_ok=True)
            window.capture_as_image().save(args.screenshot)
        for element in window.descendants()[: args.max_elements]:
            try:
                text = str(element.window_text() or "").strip()
                info = element.element_info
                if not text:
                    continue
                payload["elements"].append({
                    "text": text,
                    "control_type": str(getattr(info, "control_type", "")),
                    "automation_id": str(getattr(info, "automation_id", "")),
                    "class_name": str(getattr(info, "class_name", "")),
                    "rectangle": [
                        int(getattr(info.rectangle, "left", 0)),
                        int(getattr(info.rectangle, "top", 0)),
                        int(getattr(info.rectangle, "right", 0)),
                        int(getattr(info.rectangle, "bottom", 0)),
                    ],
                })
            except Exception:
                continue
        payload["status"] = "OK" if payload["elements"] else ("SCREENSHOT_ONLY" if args.screenshot and args.screenshot.exists() else "EMPTY")
    except Exception as exc:
        payload["error"] = str(exc)[:500]

    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"status": payload["status"], "elements": len(payload["elements"]), "output": str(args.output)}, ensure_ascii=False))
    return 0 if payload["status"] == "OK" else 2


if __name__ == "__main__":
    raise SystemExit(main())
