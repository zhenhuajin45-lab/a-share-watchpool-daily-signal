from __future__ import annotations

import hashlib
import importlib.util
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("evaluate_premarket_release_gate", ROOT / "tools" / "evaluate_premarket_release_gate.py")
assert SPEC and SPEC.loader
gate = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(gate)


class PremarketReleaseGateTests(unittest.TestCase):
    def valid_record(self, root: Path, stage: str, date: str = "20260817") -> dict:
        artifact = root / f"{stage}.json"
        artifact.write_bytes(b"auditable-artifact")
        contracts = gate.STAGE_CONTRACTS[stage]
        return {
            "schema_version": contracts["schema"],
            "execution_trade_date": date,
            "status": "PASS",
            "checks": {name: True for name in contracts["checks"]},
            "real_orders_sent": False,
            "evidence_files": [{"path": str(artifact), "sha256": hashlib.sha256(artifact.read_bytes()).hexdigest()}],
            "_evidence_path": str(root / f"{stage}_evidence.json"),
        }

    def test_empty_shell_json_does_not_count(self) -> None:
        result = gate.evaluate([{"execution_trade_date": "20260817", "_evidence_path": "shell.json"}], [], [])
        self.assertEqual(result["counts"]["replay_days"], 0)
        self.assertEqual(result["evidence_quality"]["invalid_files"], 1)
        self.assertEqual(result["release_gate"], "NOT_MET")

    def test_valid_evidence_with_matching_hash_counts(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            record = self.valid_record(root, "replay")
            result = gate.evaluate([record], [], [])
        self.assertEqual(result["counts"]["replay_days"], 1)
        self.assertEqual(result["evidence_quality"]["invalid_files"], 0)

    def test_shadow_requires_explicit_no_real_orders(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            record = self.valid_record(Path(directory), "shadow")
            record.pop("real_orders_sent")
            result = gate.evaluate([], [record], [])
        self.assertEqual(result["counts"]["shadow_days"], 0)
        self.assertIn("real_orders_sent", result["evidence_quality"]["invalid_evidence"][0]["reasons"])


if __name__ == "__main__":
    unittest.main()
