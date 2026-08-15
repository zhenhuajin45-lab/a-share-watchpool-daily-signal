#!/usr/bin/env python3
"""Call an OpenAI-compatible DeepSeek endpoint and apply tighten-only review."""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import urllib.request
import datetime as dt
from pathlib import Path
from typing import Any


PACKAGE_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PACKAGE_ROOT / "src"))

from premarket_command.review import apply_restrictive_review, build_deepseek_prompt  # noqa: E402


def extract_json(text: str) -> dict[str, Any]:
    cleaned = re.sub(r"^```(?:json)?\s*|\s*```$", "", text.strip(), flags=re.I | re.S)
    decoder = json.JSONDecoder()
    for index, char in enumerate(cleaned):
        if char != "{":
            continue
        try:
            value, _ = decoder.raw_decode(cleaned[index:])
        except json.JSONDecodeError:
            continue
        return value if isinstance(value, dict) else {}
    return {}


def validate_review(value: dict[str, Any]) -> None:
    allowed_verdicts = {"CONFIRM", "CONFIRM_WITH_RESTRICTIONS", "OBSERVE_ONLY", "REVIEW_PENDING"}
    if value.get("schema_version") != "premarket_deepseek_review_v1":
        raise ValueError("DeepSeek schema_version mismatch")
    if str(value.get("verdict") or "").upper() not in allowed_verdicts:
        raise ValueError("DeepSeek verdict invalid")
    try:
        cap = float(value.get("recommended_position_cap_pct"))
    except (TypeError, ValueError) as exc:
        raise ValueError("DeepSeek recommended_position_cap_pct invalid") from exc
    if not 0 <= cap <= 100:
        raise ValueError("DeepSeek recommended_position_cap_pct out of range")
    if not isinstance(value.get("sector_downgrades", []), list):
        raise ValueError("DeepSeek sector_downgrades must be a list")


def call_api(prompt: str, api_key: str, model: str, api_url: str, timeout: int) -> tuple[dict[str, Any], dict[str, Any]]:
    body = json.dumps({
        "model": model,
        "messages": [{"role": "user", "content": prompt}],
        "temperature": 0.1,
        "response_format": {"type": "json_object"},
    }).encode("utf-8")
    request = urllib.request.Request(api_url, data=body, method="POST", headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"})
    with urllib.request.urlopen(request, timeout=timeout) as response:
        payload = json.loads(response.read().decode("utf-8"))
    content = payload["choices"][0]["message"]["content"]
    result = extract_json(str(content))
    if not result:
        raise ValueError("DeepSeek response was not valid JSON")
    validate_review(result)
    return {**result, "available": True, "model": model}, payload


def write_atomic(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(text, encoding="utf-8")
    temporary.replace(path)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--command", type=Path, required=True)
    parser.add_argument("--review-output", type=Path, required=True)
    parser.add_argument("--final-output", type=Path, required=True)
    parser.add_argument("--prompt-output", type=Path)
    parser.add_argument("--raw-output", type=Path)
    parser.add_argument("--model", default=os.environ.get("DEEPSEEK_PREMARKET_MODEL", "deepseek-v4-pro"))
    parser.add_argument("--api-url", default=os.environ.get("DEEPSEEK_API_URL", "https://api.deepseek.com/chat/completions"))
    parser.add_argument("--timeout", type=int, default=240)
    args = parser.parse_args()
    command = json.loads(args.command.read_text(encoding="utf-8"))
    prompt = build_deepseek_prompt(command)
    prompt_output = args.prompt_output or args.review_output.with_suffix(".prompt.txt")
    raw_output = args.raw_output or args.review_output.with_suffix(".raw.json")
    write_atomic(prompt_output, prompt + "\n")
    key = os.environ.get("DEEPSEEK_API_KEY", "").strip()
    raw_response: dict[str, Any] = {
        "schema_version": "premarket_deepseek_raw_v1",
        "captured_at": dt.datetime.now().astimezone().isoformat(),
        "api_url": args.api_url,
        "model": args.model,
        "credential_logged": False,
    }
    if not key:
        review = {
            "schema_version": "premarket_deepseek_review_v1",
            "available": False,
            "verdict": "REVIEW_PENDING",
            "conclusion": "DEEPSEEK_API_KEY missing; deterministic command remains a draft and cannot be marked published.",
            "recommended_position_cap_pct": (command.get("position_command") or {}).get("base_cap_pct", 0),
            "disagreements": ["DEEPSEEK_API_KEY_missing"],
            "sector_downgrades": [],
        }
        raw_response.update({"available": False, "error": "DEEPSEEK_API_KEY_missing"})
    else:
        try:
            review, api_payload = call_api(prompt, key, args.model, args.api_url, args.timeout)
            raw_response.update({"available": True, "response": api_payload})
        except Exception as exc:
            review = {
                "schema_version": "premarket_deepseek_review_v1",
                "available": False,
                "verdict": "REVIEW_PENDING",
                "conclusion": f"DeepSeek unavailable: {str(exc)[:300]}",
                "recommended_position_cap_pct": (command.get("position_command") or {}).get("base_cap_pct", 0),
                "disagreements": [str(exc)[:300]],
                "sector_downgrades": [],
            }
            raw_response.update({"available": False, "error": str(exc)[:500]})
    final = apply_restrictive_review(command, review)
    for path, value in ((raw_output, raw_response), (args.review_output, review), (args.final_output, final)):
        write_atomic(path, json.dumps(value, ensure_ascii=False, indent=2) + "\n")
    print(json.dumps({"available": review.get("available"), "verdict": review.get("verdict"), "release_status": final.get("release_status"), "final_cap": (final.get("position_command") or {}).get("base_cap_pct")}, ensure_ascii=False))
    return 0 if final.get("release_status") == "PUBLISHED" else 2


if __name__ == "__main__":
    raise SystemExit(main())
