from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("author_ratio_nightly_task", ROOT / "tools" / "author_ratio_nightly_task.py")
assert SPEC and SPEC.loader
nightly = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(nightly)


class AuthorRatioNightlyTaskTests(unittest.TestCase):
    def args(self) -> argparse.Namespace:
        return argparse.Namespace(trade_date="20260814", evidence=Path("evidence.json"), ledger=Path("ledger.json"))

    def test_one_article_can_record_current_and_previous_trade_day(self) -> None:
        evidence = {
            "status": "ARTICLE_TEXT_VERIFIED",
            "source_url": "https://mp.weixin.qq.com/s/example",
            "article_title": "test",
            "observations": [
                {"trade_date": "20260814", "ratio": 1.69},
                {"trade_date": "20260813", "ratio": 2.88},
            ],
        }
        commands = nightly.build_commands(self.args(), evidence)
        self.assertEqual(len(commands), 2)
        self.assertIn("20260814", commands[0])
        self.assertIn("20260813", commands[1])

    def test_article_cannot_backfill_an_unbounded_old_date(self) -> None:
        evidence = {
            "status": "ARTICLE_IMAGE_VERIFIED",
            "source_url": "https://mp.weixin.qq.com/s/example",
            "observations": [{"trade_date": "20260701", "ratio": 1.0}],
        }
        with self.assertRaises(ValueError):
            nightly.build_commands(self.args(), evidence)

    def test_article_date_must_match_requested_trade_date(self) -> None:
        evidence = {
            "status": "ARTICLE_TEXT_VERIFIED",
            "article_date": "20260813",
            "source_url": "https://mp.weixin.qq.com/s/example",
            "observations": [{"trade_date": "20260814", "ratio": 1.69}],
        }
        with self.assertRaises(ValueError):
            nightly.build_commands(self.args(), evidence)

    def test_image_observations_require_original_evidence_files(self) -> None:
        evidence = {
            "status": "ARTICLE_IMAGE_VERIFIED",
            "source_url": "https://mp.weixin.qq.com/s/example",
            "observations": [{"trade_date": "20260814", "ratio": 1.69}],
        }
        with self.assertRaises(ValueError):
            nightly.build_commands(self.args(), evidence)

    def test_all_ledger_conflicts_are_rejected_before_write(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            ledger = Path(directory) / "ledger.json"
            ledger.write_text(json.dumps({
                "observations": [{"trade_date": "20260813", "ratio": 9.99}],
            }), encoding="utf-8")
            args = self.args()
            args.ledger = ledger
            evidence = {
                "status": "ARTICLE_TEXT_VERIFIED",
                "source_url": "https://mp.weixin.qq.com/s/example",
                "observations": [
                    {"trade_date": "20260814", "ratio": 1.69},
                    {"trade_date": "20260813", "ratio": 2.88},
                ],
            }
            with self.assertRaises(ValueError):
                nightly.build_commands(args, evidence)

    def test_evidence_file_hash_mismatch_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "image.png"
            path.write_bytes(b"image-evidence")
            evidence = {"evidence_files": [{"path": str(path), "sha256": "0" * 64}]}
            with self.assertRaises(ValueError):
                nightly.verify_evidence_files(evidence)
            evidence["evidence_files"][0]["sha256"] = hashlib.sha256(path.read_bytes()).hexdigest()
            nightly.verify_evidence_files(evidence)


if __name__ == "__main__":
    unittest.main()
