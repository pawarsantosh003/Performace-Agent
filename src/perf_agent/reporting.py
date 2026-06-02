from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .models import AgentRun, Finding, Severity
from .serialization import to_json


class ReportWriter:
    def write_all(self, run: AgentRun, output_dir: Path) -> dict[str, str]:
        output_dir.mkdir(parents=True, exist_ok=True)
        artifacts = {
            "report": str(output_dir / "performance_report.md"),
            "baseline": str(output_dir / "baseline.json"),
            "backlog": str(output_dir / "optimization_backlog.json"),
            "readiness": str(output_dir / "release_readiness.json"),
            "raw_results": str(output_dir / "raw_results.json"),
            "manifest": str(output_dir / "manifest.json"),
        }
        Path(artifacts["report"]).write_text(self._markdown(run), encoding="utf-8")
        Path(artifacts["baseline"]).write_text(json.dumps(_baseline(run), indent=2), encoding="utf-8")
        Path(artifacts["backlog"]).write_text(json.dumps([to_json(f) for f in run.findings], indent=2), encoding="utf-8")
        Path(artifacts["readiness"]).write_text(json.dumps(to_json(run.readiness), indent=2), encoding="utf-8")
        Path(artifacts["raw_results"]).write_text(json.dumps(to_json(run.scenario_results), indent=2), encoding="utf-8")
        Path(artifacts["manifest"]).write_text(json.dumps(_manifest(run), indent=2), encoding="utf-8")
        return artifacts

    def _markdown(self, run: AgentRun) -> str:
        readiness = run.readiness
        assert readiness is not None
        lines = [
            f"# Performance Test Report: {run.config.application_name}",
            "",
            f"- Run ID: `{run.run_id}`",
            f"- Release: `{run.config.release_id}`",
            f"- Environment: `{run.config.environment.name}`",
            f"- Generated: {datetime.now(UTC).isoformat()}",
            f"- Release readiness: **{readiness.status.value.upper()}** ({readiness.score}/100)",
            "",
            "## Executive Summary",
            "",
            self._executive_summary(run),
            "",
            "## Release Readiness Scorecard",
            "",
            "| Dimension | Score |",
            "| --- | ---: |",
        ]
        for name, score in readiness.dimensions.items():
            lines.append(f"| {name.replace('_', ' ').title()} | {score:.1f} |")
        if readiness.blockers:
            lines.extend(["", "### Blockers", ""])
            lines.extend([f"- {item}" for item in readiness.blockers])

        lines.extend(["", "## Scenario Results", ""])
        for result in run.scenario_results:
            lines.extend([f"### {result.scenario_name} ({result.test_type.value})", ""])
            if result.endpoint_results:
                lines.extend(["| Endpoint | p95 ms | p99 ms | RPS | Error % | Samples |", "| --- | ---: | ---: | ---: | ---: | ---: |"])
                for endpoint in result.endpoint_results:
                    lines.append(
                        f"| {endpoint.name} | {endpoint.p95_ms} | {endpoint.p99_ms} | "
                        f"{endpoint.throughput_rps} | {endpoint.error_rate_pct} | {endpoint.sample_count} |"
                    )
                lines.append("")
            if result.web_vitals_results:
                lines.extend(["| Page | LCP p75 ms | INP p75 ms | CLS p75 | FCP p75 ms | TTFB p75 ms |", "| --- | ---: | ---: | ---: | ---: | ---: |"])
                for page in result.web_vitals_results:
                    lines.append(
                        f"| {page.page_name} | {page.lcp_p75_ms} | {page.inp_p75_ms} | "
                        f"{page.cls_p75} | {page.fcp_p75_ms} | {page.ttfb_p75_ms} |"
                    )
                lines.append("")
            if result.infra_metrics:
                infra = result.infra_metrics
                lines.extend(
                    [
                        f"- CPU: {infra.cpu_pct}%",
                        f"- Memory: {infra.memory_pct}%",
                        f"- Disk I/O: {infra.disk_io_pct}%",
                        f"- Network: {infra.network_pct}%",
                        "",
                    ]
                )

        lines.extend(["## Prioritized Optimization Backlog", ""])
        if not run.findings:
            lines.append("No performance findings exceeded configured thresholds.")
        else:
            lines.extend(["| Priority | Severity | Category | Finding | Score |", "| --- | --- | --- | --- | ---: |"])
            for index, finding in enumerate(run.findings, start=1):
                lines.append(f"| P{index} | {finding.severity.value} | {finding.category} | {finding.title} | {finding.score} |")
            lines.append("")
            for index, finding in enumerate(run.findings, start=1):
                lines.extend(
                    [
                        f"### P{index}: {finding.title}",
                        "",
                        f"- Severity: {finding.severity.value}",
                        f"- Category: {finding.category}",
                        f"- Score: {finding.score}",
                        f"- Recommendation: {finding.recommendation}",
                        f"- Validation: {finding.validation_plan}",
                        f"- Likely root cause: {finding.likely_cause or 'More telemetry is needed to isolate the exact root cause.'}",
                        "- Solution steps:",
                    ]
                )
                lines.extend([f"  - {item}" for item in finding.solution_steps] or ["  - Review the supporting evidence and collect deeper telemetry for this path."])
                lines.extend(
                    [
                        "- Owner actions:",
                    ]
                )
                lines.extend([f"  - {item}" for item in finding.owner_actions] or ["  - Assign an engineering owner to validate and remediate this finding."])
                if finding.documentation_links:
                    lines.append("- Reference links:")
                    lines.extend([f"  - {item}" for item in finding.documentation_links])
                lines.extend(
                    [
                        "- Evidence:",
                    ]
                )
                lines.extend([f"  - {item}" for item in finding.evidence])
                lines.append("")

        lines.extend(["## Generated Artifacts", ""])
        for name, path in run.artifacts.items():
            lines.append(f"- {name}: `{path}`")
        lines.append("")
        return "\n".join(lines)

    def _executive_summary(self, run: AgentRun) -> str:
        critical = _count(run.findings, Severity.CRITICAL)
        high = _count(run.findings, Severity.HIGH)
        medium = _count(run.findings, Severity.MEDIUM)
        if critical:
            return f"The release is blocked by {critical} critical performance finding(s). Address blockers or formally accept risk before release."
        if high:
            return f"The release has {high} high-severity performance risk(s). Proceed only with mitigation ownership and release approval."
        if medium:
            return f"The release is broadly viable with {medium} medium-severity improvement item(s) to track."
        return "The tested scenarios meet configured performance thresholds. Continue monitoring against the generated baseline after launch."


def _count(findings: list[Finding], severity: Severity) -> int:
    return sum(1 for finding in findings if finding.severity == severity)


def _baseline(run: AgentRun) -> dict[str, Any]:
    return {
        "application_name": run.config.application_name,
        "release_id": run.config.release_id,
        "environment": run.config.environment.name,
        "run_id": run.run_id,
        "created_at": datetime.now(UTC).isoformat(),
        "readiness": to_json(run.readiness),
        "scenarios": to_json(run.scenario_results),
    }


def _manifest(run: AgentRun) -> dict[str, Any]:
    first_scenario = run.config.scenarios[0] if run.config.scenarios else None
    first_page = first_scenario.pages[0] if first_scenario and first_scenario.pages else None
    first_endpoint = first_scenario.endpoints[0] if first_scenario and first_scenario.endpoints else None
    return {
        "application_name": run.config.application_name,
        "release_id": run.config.release_id,
        "environment": run.config.environment.name,
        "base_url": run.config.environment.base_url,
        "primary_url": first_page.url if first_page else first_endpoint.url if first_endpoint else run.config.environment.base_url,
        "test_type": first_scenario.test_type.value if first_scenario else "",
        "concurrent_users": first_scenario.workload.concurrent_users if first_scenario else 0,
        "duration_seconds": first_scenario.workload.duration_seconds if first_scenario else 0,
        "run_id": run.run_id,
        "created_at": datetime.now(UTC).isoformat(),
        "readiness": to_json(run.readiness),
        "finding_count": len(run.findings),
        "critical_count": _count(run.findings, Severity.CRITICAL),
        "high_count": _count(run.findings, Severity.HIGH),
        "medium_count": _count(run.findings, Severity.MEDIUM),
        "low_count": _count(run.findings, Severity.LOW),
    }
