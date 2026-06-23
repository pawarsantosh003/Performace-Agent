import tempfile
import unittest
from pathlib import Path

from perf_agent.adapters import MetricsLoader
from perf_agent.config import load_config
from perf_agent.workflow import PerformanceAgent


class Phase3ObservabilityTests(unittest.TestCase):
    def test_phase3_config_loads_database_and_connector_annotations(self) -> None:
        config = load_config("examples/phase3_observability_config.json")
        loader = MetricsLoader()

        database_inputs = loader.load_database(config)
        annotations = loader.load_monitoring_annotations(config, "run-123")

        self.assertGreaterEqual(len(database_inputs), 6)
        self.assertTrue(any("Plan evidence" in (item.recommendation_hint or "") for item in database_inputs))
        self.assertTrue(annotations["grafana_dashboards"])
        self.assertTrue(annotations["trace_links"])
        self.assertTrue(annotations["connector_status"])

    def test_phase3_run_persists_evidence_artifacts(self) -> None:
        config = load_config("examples/phase3_observability_config.json")
        agent = PerformanceAgent()

        with tempfile.TemporaryDirectory() as tmp:
            run = agent.run(config, output_root=Path(tmp))

            self.assertTrue(run.connector_annotations["trace_links"])
            self.assertTrue(any(finding.category == "database" for finding in run.findings))
            self.assertIn("connectors", run.artifacts)
            self.assertTrue(Path(run.artifacts["connectors"]).exists())
            report_text = Path(run.artifacts["report"]).read_text(encoding="utf-8")
            self.assertIn("Failing Endpoints", report_text)
            self.assertIn("Database Bottleneck Evidence", report_text)


if __name__ == "__main__":
    unittest.main()
