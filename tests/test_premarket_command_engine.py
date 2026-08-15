from __future__ import annotations

import json
import sys
import unittest
import tempfile
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from premarket_command.engine import build_premarket_command, index_technical_from_bars
from premarket_command.review import apply_restrictive_review
from premarket_command.opening_review import apply_opening_tighten_only
from premarket_command.publisher import publish_contract
from premarket_command.integration import plan_sector_alignment
from premarket_command.integration import load_published_command


class PremarketEngineTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.sample = json.loads((ROOT / "examples" / "premarket_input.sample.json").read_text(encoding="utf-8"))

    def published_baseline(self) -> dict:
        command = build_premarket_command(self.sample)
        command["status"] = "READY_FOR_DEEPSEEK_REVIEW"
        command["source_health"] = {"publishable": True, "blockers": []}
        command["operational_acceptance"] = {
            "release_gate": "MET",
            "counts": {"replay_days": 20, "shadow_days": 5, "simulation_days": 5},
            "checks": {"replay_20_days": True, "shadow_5_days": True, "simulation_5_days": True, "no_real_order_contract": True},
        }
        return apply_restrictive_review(command, {"available": True, "verdict": "CONFIRM", "recommended_position_cap_pct": 100, "sector_downgrades": []})

    def test_output_has_no_stock_pool(self) -> None:
        command = build_premarket_command(self.sample)
        self.assertFalse(command["policy"]["contains_stock_pool"])
        self.assertNotIn("stock_plan", command)
        self.assertNotIn("watchlist", command)

    def test_position_cap_uses_independent_ceiling_minimum(self) -> None:
        command = build_premarket_command(self.sample)
        self.assertLessEqual(command["position_command"]["base_cap_pct"], 65)
        self.assertGreaterEqual(command["position_command"]["base_cap_pct"], 0)

    def test_deepseek_cannot_raise_cap(self) -> None:
        command = build_premarket_command(self.sample)
        base = command["position_command"]["base_cap_pct"]
        reviewed = apply_restrictive_review(command, {"available": True, "verdict": "CONFIRM", "recommended_position_cap_pct": 100, "sector_downgrades": []})
        self.assertEqual(reviewed["position_command"]["base_cap_pct"], base)

    def test_deepseek_can_downgrade_but_not_add_sector(self) -> None:
        command = build_premarket_command(self.sample)
        names = [item["sector_name"] for item in command["sector_rotation"]["primary_attack_sectors"]]
        reviewed = apply_restrictive_review(command, {"available": True, "verdict": "CONFIRM_WITH_RESTRICTIONS", "recommended_position_cap_pct": 30, "sector_downgrades": names[:1]})
        after = reviewed["sector_rotation"]["primary_attack_sectors"]
        self.assertEqual([item["sector_name"] for item in after], names)
        if after:
            self.assertEqual(after[0]["permission"], "RECONFIRM_ONLY")

    def test_partial_or_undated_evidence_cannot_publish(self) -> None:
        command = build_premarket_command(self.sample)
        reviewed = apply_restrictive_review(command, {
            "available": True,
            "verdict": "CONFIRM",
            "recommended_position_cap_pct": 100,
            "sector_downgrades": [],
        })
        self.assertEqual(reviewed["release_status"], "REVIEW_PENDING")
        self.assertFalse(reviewed["publication_gate"]["source_publishable"])

    def test_external_freshness_must_match_execution_date(self) -> None:
        payload = json.loads(json.dumps(self.sample))
        payload["external_market"].update({
            "trade_date": "20260815",
            "fresh_for_execution": True,
        })
        command = build_premarket_command(payload)
        self.assertFalse(command["external_resonance"]["fresh_for_execution"])
        self.assertNotIn("external_market", command["source_health"]["missing"])
        self.assertIn("external_market_date", command["source_health"]["stale_or_undated"])

    def test_external_freshness_accepts_execution_day_capture(self) -> None:
        payload = json.loads(json.dumps(self.sample))
        payload["external_market"].update({
            "trade_date": payload["execution_trade_date"],
            "fresh_for_execution": True,
        })
        command = build_premarket_command(payload)
        self.assertTrue(command["external_resonance"]["fresh_for_execution"])
        self.assertTrue(command["source_health"]["freshness"]["external_market_date"])

    def test_ambiguous_author_ocr_never_enters_ratio_sequence(self) -> None:
        payload = json.loads(json.dumps(self.sample))
        payload["author_ratio"]["observations"] = [{"trade_date": "20260814", "ratio": 9.99, "verification": "OCR_AMBIGUOUS"}]
        command = build_premarket_command(payload)
        self.assertFalse(command["author_long_short_ratio"]["available"])
        self.assertIn("author_ratio", command["source_health"]["missing"])

    def test_reviewed_lower_cap_cannot_be_bypassed_by_expansion(self) -> None:
        command = build_premarket_command(self.sample)
        command["status"] = "READY_FOR_DEEPSEEK_REVIEW"
        command["source_health"] = {"publishable": True, "blockers": []}
        command["operational_acceptance"] = {
            "release_gate": "MET",
            "counts": {"replay_days": 20, "shadow_days": 5, "simulation_days": 5},
            "checks": {"replay_20_days": True, "shadow_5_days": True, "simulation_5_days": True, "no_real_order_contract": True},
        }
        command["position_command"].update({"base_cap_pct": 50, "conditional_expansion_cap_pct": 60})
        reviewed = apply_restrictive_review(command, {
            "available": True,
            "verdict": "CONFIRM_WITH_RESTRICTIONS",
            "recommended_position_cap_pct": 30,
            "recommended_expansion_cap_pct": 50,
            "sector_downgrades": [],
        })
        self.assertEqual(reviewed["release_status"], "PUBLISHED")
        self.assertEqual(reviewed["position_command"]["base_cap_pct"], 30)
        self.assertLessEqual(reviewed["position_command"]["conditional_expansion_cap_pct"], 30)

    def test_opening_review_only_tightens_cap_and_sector_set(self) -> None:
        baseline = self.published_baseline()
        baseline["position_command"].update({"base_cap_pct": 50, "conditional_expansion_cap_pct": 50, "conditional_expansion_enabled": False})
        baseline["sector_rotation"]["primary_attack_sectors"] = [{"sector_name": "算力"}, {"sector_name": "通信"}]
        opening = build_premarket_command(self.sample)
        opening["position_command"]["base_cap_pct"] = 35
        opening["sector_rotation"]["primary_attack_sectors"] = [{"sector_name": "通信"}, {"sector_name": "新增板块"}]
        opening["source_health"] = {"publishable": True, "blockers": []}
        result = apply_opening_tighten_only(baseline, opening)
        self.assertEqual(result["position_command"]["base_cap_pct"], 35)
        self.assertEqual([row["sector_name"] for row in result["sector_rotation"]["primary_attack_sectors"]], ["通信"])

    def test_publisher_rejects_draft(self) -> None:
        command = build_premarket_command(self.sample)
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaises(ValueError):
                publish_contract(command, Path(directory))

    def test_publisher_rejects_fabricated_published_flag(self) -> None:
        command = build_premarket_command(self.sample)
        command["release_status"] = "PUBLISHED"
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaises(ValueError):
                publish_contract(command, Path(directory))

    def test_reader_rejects_manually_edited_published_flag(self) -> None:
        command = build_premarket_command(self.sample)
        command["release_status"] = "PUBLISHED"
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "latest.json"
            path.write_text(json.dumps(command), encoding="utf-8")
            value, status = load_published_command(path)
        self.assertIsNone(value)
        self.assertEqual(status, "NOT_PUBLISHED")

    def test_deepseek_confirmation_cannot_bypass_operational_gate(self) -> None:
        command = build_premarket_command(self.sample)
        command["status"] = "READY_FOR_DEEPSEEK_REVIEW"
        command["source_health"] = {"publishable": True, "blockers": []}
        command["operational_acceptance"] = {"release_gate": "MET"}
        reviewed = apply_restrictive_review(command, {
            "available": True,
            "verdict": "CONFIRM",
            "recommended_position_cap_pct": 20,
            "sector_downgrades": [],
        })
        self.assertEqual(reviewed["release_status"], "REVIEW_PENDING")
        self.assertIn("operational_release_gate_not_met", reviewed["publication_gate"]["blockers"])

    def test_plan_sector_alignment_never_adds_a_direction(self) -> None:
        command = build_premarket_command(self.sample)
        command["sector_rotation"]["primary_attack_sectors"] = [{"sector_name": "通信"}]
        alignment = plan_sector_alignment(command, [{"group_key": "通信"}, {"group_key": "芯片"}])
        self.assertEqual(alignment["aligned_plan_sectors"], ["通信"])
        self.assertEqual(alignment["non_whitelist_plan_sectors"], ["芯片"])

    def test_plan_sector_alignment_normalizes_concept_suffix(self) -> None:
        command = build_premarket_command(self.sample)
        command["sector_rotation"]["primary_attack_sectors"] = [{"sector_name": "机器人概念"}]
        alignment = plan_sector_alignment(command, [{"group_key": "机器人"}])
        self.assertEqual(alignment["aligned_plan_sectors"], ["机器人"])

    def test_index_bars_are_recomputed_without_future_data(self) -> None:
        bars = [{"date": f"202607{day:02d}", "close": 100 + day, "volume": 1000 + day} for day in range(1, 31)]
        result = index_technical_from_bars("测试指数", "TEST", bars)
        self.assertEqual(result["status"], "OK")
        self.assertEqual(result["data_date"], "20260730")
        self.assertEqual(result["trend"], "BULLISH_ALIGNMENT")


if __name__ == "__main__":
    unittest.main()
