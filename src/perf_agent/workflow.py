from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .adapters import Guardrail, MetricsLoader, executor_for
from .analysis import PerformanceAnalyzer
from .ai import AIReasoningEngine
from .models import AgentConfig, AgentRun, TestEngine
from .reporting import ReportWriter


class PerformanceAgent:
    def __init__(self, use_k6: bool = False) -> None:
        self.guardrail = Guardrail()
        self.engine_override = TestEngine.K6 if use_k6 else None
        self.metrics_loader = MetricsLoader()
        self.analyzer = PerformanceAnalyzer()
        self.ai_engine = AIReasoningEngine()
        self.report_writer = ReportWriter()

    def run(self, config: AgentConfig, output_root: str | Path, approve_risky: bool = False) -> AgentRun:
        run_id = _run_id(config)
        output_dir = Path(output_root) / run_id
        output_dir.mkdir(parents=True, exist_ok=True)

        agent_run = AgentRun(run_id=run_id, config=config)
        shared_infra = self.metrics_loader.load_infra(config)
        database_inputs = self.metrics_loader.load_database(config)
        agent_run.connector_annotations = self.metrics_loader.load_monitoring_annotations(config, run_id)

        for index, scenario in enumerate(config.scenarios):
            self.guardrail.validate(config, scenario, approve_risky=approve_risky)
            engine = self.engine_override or config.test_engine
            result = executor_for(engine).execute(config, scenario, run_id=run_id, output_dir=output_dir)
            if shared_infra:
                result.infra_metrics = shared_infra
            if index == 0:
                result.database_inputs.extend(database_inputs)
            agent_run.scenario_results.append(result)

        agent_run.historical_context = self._load_historical_context(output_root, config)
        agent_run.findings = self.analyzer.analyze(config, agent_run.scenario_results)
        self.ai_engine.enrich_findings(agent_run, agent_run.historical_context)
        agent_run.readiness = self.analyzer.readiness(config, agent_run.scenario_results, agent_run.findings)
        agent_run.artifacts = self.report_writer.write_all(agent_run, output_dir)
        return agent_run

    def _load_historical_context(self, output_root: str | Path, config: AgentConfig) -> dict[str, Any] | None:
        root = Path(output_root)
        if config.previous_baseline_file:
            baseline_path = Path(config.previous_baseline_file)
            if baseline_path.exists():
                try:
                    return json.loads(baseline_path.read_text(encoding="utf-8"))
                except Exception:
                    return None
        if not root.exists():
            return None
        baseline_files = sorted(
            root.rglob("baseline.json"),
            key=lambda path: path.stat().st_mtime,
            reverse=True,
        )
        if baseline_files:
            try:
                return json.loads(baseline_files[0].read_text(encoding="utf-8"))
            except Exception:
                return None
        return None


def _run_id(config: AgentConfig) -> str:
    timestamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    safe_app = "".join(ch.lower() if ch.isalnum() else "-" for ch in config.application_name).strip("-")
    safe_release = "".join(ch.lower() if ch.isalnum() else "-" for ch in config.release_id).strip("-")
    return f"{safe_app}-{safe_release}-{timestamp}"
