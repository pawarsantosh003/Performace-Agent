import tempfile
import unittest
from pathlib import Path

from perf_agent.cli import _exit_code_for_readiness, main
from perf_agent.models import ReadinessStatus


class CLITests(unittest.TestCase):
    def test_exit_code_for_readiness(self) -> None:
        self.assertEqual(_exit_code_for_readiness(ReadinessStatus.GREEN), 0)
        self.assertEqual(_exit_code_for_readiness(ReadinessStatus.AMBER), 1)
        self.assertEqual(_exit_code_for_readiness(ReadinessStatus.RED), 2)
        self.assertEqual(_exit_code_for_readiness(ReadinessStatus.BLOCKED), 2)

    def test_release_gate_mode_returns_valid_code(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            rc = main([
                "run",
                "--config",
                "examples/perf_agent_config.json",
                "--out",
                tmp,
                "--approve-risky",
                "--release-gate",
            ])
            self.assertIn(rc, {0, 1, 2})
            self.assertTrue(Path(tmp).exists())


if __name__ == "__main__":
    unittest.main()
