from __future__ import annotations

import importlib.util
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("external_market_adapter", ROOT / "adapters" / "external_market_adapter.py")
assert SPEC and SPEC.loader
external = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(external)


class PremarketSourceAdapterTests(unittest.TestCase):
    def test_large_korea_move_is_not_circuit_breaker_evidence(self) -> None:
        flags = external.derive_risk_flags(
            {"korea_tech_pct": -8.0, "korea_broad_pct": -7.0, "korea_market_stress_pct": -9.0},
            korea_circuit_breaker=False,
        )
        self.assertFalse(flags["korea_circuit_breaker"])
        self.assertFalse(flags["asia_tech_circuit_breaker"])
        self.assertTrue(flags["quant_panic"])

    def test_missing_external_fields_do_not_default_pass(self) -> None:
        result = external.classify_external_tech_shock({"missing_gate_fields": ["sox_pct"]})
        self.assertEqual(result["level"], "UNKNOWN")
        self.assertEqual(result["entry_policy"], "review_required")

    def test_kospi_and_kosdaq_are_distinct_symbols(self) -> None:
        symbols = {item["key"]: item["symbol"] for item in external.YAHOO_SYMBOLS}
        self.assertEqual(symbols["kospi"], "^KS11")
        self.assertEqual(symbols["kosdaq"], "^KQ11")
        self.assertNotEqual(symbols["kospi"], symbols["kosdaq"])


if __name__ == "__main__":
    unittest.main()
