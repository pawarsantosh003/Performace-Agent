from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

from .adapters import Guardrail, K6Executor, MetricsLoader, SyntheticExecutor
from .analysis import PerformanceAnalyzer
from .models import AgentConfig, AgentRun
from .reporting import ReportWriter


class PerformanceAgent:
    def __init__(self, use_k6: bool = False) -> None:
        self.guardrail = Guardrail()
        self.executor = K6Executor() if use_k6 else SyntheticExecutor()
        self.metrics_loader = MetricsLoader()
        self.analyzer = PerformanceAnalyzer()
        self.report_writer = ReportWriter()

    def run(self, config: AgentConfig, output_root: str | Path, approve_risky: bool = False) -> AgentRun:
        run_id = _run_id(config)
        output_dir = Path(output_root) / run_id
        output_dir.mkdir(parents=True, exist_ok=True)

        agent_run = AgentRun(run_id=run_id, config=config)
        shared_infra = self.metrics_loader.load_infra(config.monitoring_metrics_file)
        database_inputs = self.metrics_loader.load_database(config.database_metrics_file)

        for index, scenario in enumerate(config.scenarios):
            self.guardrail.validate(config, scenario, approve_risky=approve_risky)
            result = self.executor.execute(config, scenario, run_id=run_id, output_dir=output_dir)
            if shared_infra:
                result.infra_metrics = shared_infra
            if index == 0:
                result.database_inputs.extend(database_inputs)
            agent_run.scenario_results.append(result)

        agent_run.findings = self.analyzer.analyze(config, agent_run.scenario_results)
        agent_run.readiness = self.analyzer.readiness(config, agent_run.scenario_results, agent_run.findings)
        agent_run.artifacts = self.report_writer.write_all(agent_run, output_dir)
        return agent_run


def _run_id(config: AgentConfig) -> str:
    timestamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    safe_app = "".join(ch.lower() if ch.isalnum() else "-" for ch in config.application_name).strip("-")
    safe_release = "".join(ch.lower() if ch.isalnum() else "-" for ch in config.release_id).strip("-")
    return f"{safe_app}-{safe_release}-{timestamp}"
