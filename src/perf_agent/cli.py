from __future__ import annotations

import argparse
import sys
from pathlib import Path

from .adapters import ApprovalRequired, GuardrailViolation
from .config import ConfigError, load_config
from .models import AgentConfig, ReadinessStatus, TestEngine
from .workflow import PerformanceAgent


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run the Performance Testing AI Agent MVP.")
    parser.add_argument("run", nargs="?", help="Command to execute. Currently only 'run' is supported.")
    parser.add_argument("--config", required=True, help="Path to agent JSON config.")
    parser.add_argument("--out", default="runs", help="Output directory for reports and baselines.")
    parser.add_argument("--approve-risky", action="store_true", help="Approve stress, spike, and endurance scenarios.")
    parser.add_argument("--use-k6", action="store_true", help="Use k6 when available; falls back to synthetic results.")
    parser.add_argument(
        "--engine",
        choices=[item.value for item in TestEngine],
        help="Test engine to use: synthetic, k6, lighthouse, k6_lighthouse, pagespeed, webpagetest, or jmeter.",
    )
    parser.add_argument(
        "--release-gate",
        action="store_true",
        help="Enable CI release gate exit codes: 0=green, 1=amber, 2=red/blocked.",
    )
    args = parser.parse_args(argv)

    if args.run != "run":
        parser.error("Only the 'run' command is supported.")

    try:
        config = load_config(args.config)
        if args.engine:
            config = _with_engine(config, TestEngine(args.engine))
        agent = PerformanceAgent(use_k6=args.use_k6)
        run = agent.run(config, output_root=Path(args.out), approve_risky=args.approve_risky)
    except (ConfigError, ApprovalRequired, GuardrailViolation) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2

    print(f"Run ID: {run.run_id}")
    print(f"Readiness: {run.readiness.status.value.upper()} ({run.readiness.score}/100)")
    print("Artifacts:")
    for name, path in run.artifacts.items():
        print(f"  {name}: {path}")
    if args.release_gate:
        exit_code = _exit_code_for_readiness(run.readiness.status)
        print(f"Release gate status: {run.readiness.status.value.upper()} -> exit code {exit_code}")
        return exit_code
    return 0


def _with_engine(config: AgentConfig, engine: TestEngine) -> AgentConfig:
    return AgentConfig(
        application_name=config.application_name,
        release_id=config.release_id,
        environment=config.environment,
        scenarios=config.scenarios,
        web_vitals=config.web_vitals,
        monitoring_metrics_file=config.monitoring_metrics_file,
        database_metrics_file=config.database_metrics_file,
        previous_baseline_file=config.previous_baseline_file,
        test_engine=engine,
    )


def _exit_code_for_readiness(status: ReadinessStatus) -> int:
    if status == ReadinessStatus.GREEN:
        return 0
    if status == ReadinessStatus.AMBER:
        return 1
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
