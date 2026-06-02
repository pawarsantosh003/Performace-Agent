from __future__ import annotations

import argparse
import sys
from pathlib import Path

from .adapters import ApprovalRequired, GuardrailViolation
from .config import ConfigError, load_config
from .workflow import PerformanceAgent


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run the Performance Testing AI Agent MVP.")
    parser.add_argument("run", nargs="?", help="Command to execute. Currently only 'run' is supported.")
    parser.add_argument("--config", required=True, help="Path to agent JSON config.")
    parser.add_argument("--out", default="runs", help="Output directory for reports and baselines.")
    parser.add_argument("--approve-risky", action="store_true", help="Approve stress, spike, and endurance scenarios.")
    parser.add_argument("--use-k6", action="store_true", help="Use k6 when available; falls back to synthetic results.")
    args = parser.parse_args(argv)

    if args.run != "run":
        parser.error("Only the 'run' command is supported.")

    try:
        config = load_config(args.config)
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
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

