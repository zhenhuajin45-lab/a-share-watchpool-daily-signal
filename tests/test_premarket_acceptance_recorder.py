from __future__ import annotations

import importlib.util
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("record_premarket_acceptance", ROOT / "tools" / "record_premarket_acceptance.py")
assert SPEC and SPEC.loader
recorder = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(recorder)


class PremarketAcceptanceRecorderTests(unittest.TestCase):
    def test_missing_stage_checks_are_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            evidence = root / "artifact.json"
            evidence.write_text("{}", encoding="utf-8")
            with self.assertRaises(ValueError):
                recorder.build_record("replay", "20260817", ["completed"], [evidence], root / "record.json")

    def test_shadow_record_hashes_artifact_and_confirms_no_real_orders(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            evidence = root / "artifact.json"
            evidence.write_text("{}", encoding="utf-8")
            checks = list(recorder.STAGE_CONTRACTS["shadow"]["checks"])
            record = recorder.build_record(
                "shadow",
                "20260817",
                checks,
                [evidence],
                root / "record.json",
                confirm_no_real_orders=True,
            )
        self.assertIs(record["real_orders_sent"], False)
        self.assertEqual(len(record["evidence_files"][0]["sha256"]), 64)


if __name__ == "__main__":
    unittest.main()
