import tempfile
import unittest
from pathlib import Path

from perf_agent.cli import _exit_code_for_readiness, _gate_decision_for_readiness, main
from perf_agent.models import ReadinessStatus


class CLITests(unittest.TestCase):
    def test_exit_code_for_readiness(self) -> None:
        self.assertEqual(_exit_code_for_readiness(ReadinessStatus.GREEN), 0)
        self.assertEqual(_exit_code_for_readiness(ReadinessStatus.AMBER), 1)
        self.assertEqual(_exit_code_for_readiness(ReadinessStatus.RED), 2)
        self.assertEqual(_exit_code_for_readiness(ReadinessStatus.BLOCKED), 2)
        self.assertEqual(_gate_decision_for_readiness(ReadinessStatus.GREEN), "pass")
        self.assertEqual(_gate_decision_for_readiness(ReadinessStatus.AMBER), "warn")
        self.assertEqual(_gate_decision_for_readiness(ReadinessStatus.RED), "block")
        self.assertEqual(_gate_decision_for_readiness(ReadinessStatus.BLOCKED), "block")

    def test_release_gate_mode_returns_valid_code(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            rc = main([
                "run",
                "--config",
                "examples/ci_release_gate_config.json",
                "--out",
                tmp,
                "--release-gate",
            ])
            self.assertEqual(rc, 0)
            self.assertTrue(Path(tmp).exists())

    def test_legacy_risky_bypass_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            rc = main([
                "run",
                "--config",
                "examples/perf_agent_config.json",
                "--out",
                tmp,
                "--approve-risky",
            ])
            self.assertEqual(rc, 2)

    def test_ci_sample_passes_and_writes_gate_artifact(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            rc = main([
                "run",
                "--config",
                "examples/ci_release_gate_config.json",
                "--out",
                tmp,
                "--release-gate",
            ])

            gate_files = list(Path(tmp).glob("*/release_gate.json"))
            self.assertEqual(rc, 0)
            self.assertEqual(len(gate_files), 1)
            self.assertIn('"decision": "pass"', gate_files[0].read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
