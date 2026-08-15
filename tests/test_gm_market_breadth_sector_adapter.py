from __future__ import annotations

import importlib.util
import unittest
from pathlib import Path

import pandas as pd


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("gm_market_breadth_sector_adapter", ROOT / "adapters" / "gm_market_breadth_sector_adapter.py")
assert SPEC and SPEC.loader
gm_market = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(gm_market)
OPENING_SPEC = importlib.util.spec_from_file_location("gm_opening_auction_adapter", ROOT / "adapters" / "gm_opening_auction_adapter.py")
assert OPENING_SPEC and OPENING_SPEC.loader
gm_opening = importlib.util.module_from_spec(OPENING_SPEC)
OPENING_SPEC.loader.exec_module(gm_opening)


class GmMarketBreadthSectorAdapterTests(unittest.TestCase):
    def test_limit_detection_uses_half_tick_tolerance(self) -> None:
        self.assertTrue(gm_market.at_limit(9.999, 10.0, 0.01))
        self.assertFalse(gm_market.at_limit(9.98, 10.0, 0.01))
        self.assertTrue(gm_market.at_lower_limit(8.001, 8.0, 0.01))

    def test_non_thematic_performance_concepts_are_excluded(self) -> None:
        features = pd.DataFrame([
            {"symbol": f"SHSE.{index:06d}", "current_return_pct": 2, "interval_return_pct": 3, "amount": 1e8, "amount_ratio_5d": 1.2, "current_main_net": 1e7, "interval_main_net": 2e7, "net_inflow_days": 2}
            for index in range(1, 10)
        ])
        memberships = pd.DataFrame([
            {"symbol": f"SHSE.{index:06d}", "sector_code": "X", "sector_name": "昨日涨停_含一字"}
            for index in range(1, 10)
        ])
        self.assertEqual(gm_market.sector_rows(features, memberships, "CONCEPT", 8, 1500), [])

    def test_popularity_and_yesterday_labels_are_excluded(self) -> None:
        for name in ("东方财富热股", "昨日高振幅", "昨日涨停_含一字"):
            self.assertIsNotNone(gm_market.NON_ATTACK_CONCEPT.search(name))

    def test_opening_snapshot_requires_execution_date_coverage(self) -> None:
        ticks = pd.DataFrame([
            {"symbol": "SHSE.600001", "snapshot_date": "20260817", "snapshot_time": "092000", "gap_pct": 1.0, "cum_amount": 100},
            {"symbol": "SHSE.600002", "snapshot_date": "20260816", "snapshot_time": "092000", "gap_pct": -1.0, "cum_amount": 100},
        ])
        memberships = pd.DataFrame(columns=["symbol", "sector_code", "sector_name", "group_type"])
        snapshot = gm_opening.build_snapshot(ticks, memberships, "20260817", 2)
        self.assertEqual(snapshot["status"], "UNAVAILABLE")
        self.assertEqual(snapshot["breadth"]["coverage"], 0.5)

    def test_opening_snapshot_rejects_post_close_tick(self) -> None:
        ticks = pd.DataFrame([{"symbol": "SHSE.600001", "snapshot_date": "20260817", "snapshot_time": "150000", "gap_pct": 1.0, "cum_amount": 100}])
        memberships = pd.DataFrame(columns=["symbol", "sector_code", "sector_name", "group_type"])
        snapshot = gm_opening.build_snapshot(ticks, memberships, "20260817", 1)
        self.assertEqual(snapshot["status"], "UNAVAILABLE")


if __name__ == "__main__":
    unittest.main()
