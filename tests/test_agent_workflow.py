import tempfile
import unittest
from pathlib import Path

from perf_agent.config import load_config
from perf_agent.workflow import PerformanceAgent


class AgentWorkflowTests(unittest.TestCase):
    def test_sample_workflow_generates_artifacts(self) -> None:
        config = load_config("examples/perf_agent_config.json")
        agent = PerformanceAgent()

        with tempfile.TemporaryDirectory() as tmp:
            run = agent.run(config, output_root=Path(tmp), approve_risky=True)

            self.assertIsNotNone(run.readiness)
            self.assertTrue(run.findings)
            for artifact in run.artifacts.values():
                self.assertTrue(Path(artifact).exists())

    def test_risky_scenario_requires_approval(self) -> None:
        config = load_config("examples/perf_agent_config.json")
        agent = PerformanceAgent()

        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaisesRegex(RuntimeError, "needs approval"):
                agent.run(config, output_root=Path(tmp), approve_risky=False)


if __name__ == "__main__":
    unittest.main()
