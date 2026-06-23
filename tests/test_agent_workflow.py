import tempfile
import unittest
from dataclasses import replace
from pathlib import Path

from perf_agent.config import load_config
from perf_agent.governance import ApprovalManager, User, UserRole, UserStore
from perf_agent.models import TestEngine
from perf_agent.workflow import PerformanceAgent


class AgentWorkflowTests(unittest.TestCase):
    def test_sample_workflow_generates_artifacts(self) -> None:
        config = load_config("examples/perf_agent_config.json")
        config = replace(config, scenarios=[config.scenarios[0]])
        agent = PerformanceAgent()

        with tempfile.TemporaryDirectory() as tmp:
            run = agent.run(config, output_root=Path(tmp))

            self.assertIsNotNone(run.readiness)
            self.assertTrue(run.findings)
            for artifact in run.artifacts.values():
                self.assertTrue(Path(artifact).exists())

    def test_risky_scenario_requires_approval(self) -> None:
        config = load_config("examples/perf_agent_config.json")
        agent = PerformanceAgent()

        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaisesRegex(RuntimeError, "[Aa]pprov"):
                agent.run(config, output_root=Path(tmp))

    def test_risky_scenario_runs_with_config_bound_approval(self) -> None:
        config = load_config("examples/perf_agent_config.json")
        tester = User("tester", UserStore.hash_password("StrongPassword1"), UserRole.TESTER)
        approver = User("approver", UserStore.hash_password("StrongPassword2"), UserRole.APPROVER)

        with tempfile.TemporaryDirectory() as tmp:
            manager = ApprovalManager(Path(tmp) / "approvals.json")
            approval = manager.request(config, tester)
            manager.approve(approval.approval_id, approver)

            run = PerformanceAgent().run(config, output_root=Path(tmp), approval=approval)

            self.assertIsNotNone(run.readiness)

    def test_sample_config_declares_test_engine(self) -> None:
        config = load_config("examples/perf_agent_config.json")

        self.assertEqual(config.test_engine, TestEngine.SYNTHETIC)


if __name__ == "__main__":
    unittest.main()
