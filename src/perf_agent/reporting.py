from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .models import AgentRun, Endpoint, EndpointResult, Finding, ScenarioResult, Severity
from .serialization import to_json


class ReportWriter:
    def write_all(self, run: AgentRun, output_dir: Path) -> dict[str, str]:
        output_dir.mkdir(parents=True, exist_ok=True)
        artifacts = {
            "report": str(output_dir / "performance_report.md"),
            "baseline": str(output_dir / "baseline.json"),
            "backlog": str(output_dir / "optimization_backlog.json"),
            "readiness": str(output_dir / "release_readiness.json"),
            "gate": str(output_dir / "release_gate.json"),
            "readiness_summary": str(output_dir / "readiness_summary.md"),
            "raw_results": str(output_dir / "raw_results.json"),
            "connectors": str(output_dir / "connector_annotations.json"),
            "manifest": str(output_dir / "manifest.json"),
        }
        run.artifacts = artifacts
        Path(artifacts["report"]).write_text(self._markdown(run), encoding="utf-8")
        Path(artifacts["baseline"]).write_text(json.dumps(_baseline(run), indent=2), encoding="utf-8")
        Path(artifacts["backlog"]).write_text(json.dumps([to_json(f) for f in run.findings], indent=2), encoding="utf-8")
        Path(artifacts["readiness"]).write_text(json.dumps(to_json(run.readiness), indent=2), encoding="utf-8")
        Path(artifacts["gate"]).write_text(json.dumps(_gate_result(run), indent=2), encoding="utf-8")
        Path(artifacts["raw_results"]).write_text(json.dumps(to_json(run.scenario_results), indent=2), encoding="utf-8")
        Path(artifacts["connectors"]).write_text(json.dumps(to_json(run.connector_annotations), indent=2), encoding="utf-8")
        Path(artifacts["manifest"]).write_text(json.dumps(_manifest(run), indent=2), encoding="utf-8")
        Path(artifacts["readiness_summary"]).write_text(self._readiness_summary(run), encoding="utf-8")
        return artifacts

    def _readiness_summary(self, run: AgentRun) -> str:
        readiness = run.readiness
        assert readiness is not None
        critical_count = _count(run.findings, Severity.CRITICAL)
        high_count = _count(run.findings, Severity.HIGH)
        medium_count = _count(run.findings, Severity.MEDIUM)
        low_count = _count(run.findings, Severity.LOW)

        lines = [
            f"# Release Readiness Summary: {run.config.application_name}",
            "",
            f"- Run ID: `{run.run_id}`",
            f"- Release: `{run.config.release_id}`",
            f"- Environment: `{run.config.environment.name}`",
            f"- Release readiness: **{readiness.status.value.upper()}** ({readiness.score}/100)",
            "",
            "## Severity Breakdown",
            f"- Critical findings: {critical_count}",
            f"- High findings: {high_count}",
            f"- Medium findings: {medium_count}",
            f"- Low findings: {low_count}",
            "",
        ]
        if readiness.blockers:
            lines.extend(["## Blockers", ""])
            lines.extend([f"- {item}" for item in readiness.blockers])
            lines.append("")

        lines.extend(["## Artifacts", "", f"- report: `{run.artifacts['report']}`", f"- readiness: `{run.artifacts['readiness']}`", f"- readiness_summary: `{run.artifacts['readiness_summary']}`", ""])
        return "\n".join(lines)

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
            ai_summary = [f for f in run.findings if f.ai_rca_summary or f.ai_confidence_pct]
            if ai_summary:
                lines.extend(["### Evidence-Based AI RCA & Recommendations", ""])
                for finding in ai_summary:
                    lines.extend([
                        f"- **{finding.title}** ({finding.category}, {finding.severity.value})",
                        f"  - Prompt template: {finding.ai_prompt_template or 'not recorded'}",
                        f"  - AI confidence: {finding.ai_confidence_pct:.1f}%",
                        f"  - RCA summary: {finding.ai_rca_summary or 'No AI RCA available.'}",
                        f"  - Recommendation: {finding.ai_recommendation or finding.recommendation}",
                        f"  - Validation plan: {finding.ai_validation_plan or finding.validation_plan}",
                    ])
                    if finding.ai_evidence_citations:
                        lines.append("  - Evidence citations:")
                        lines.extend([f"    - {item}" for item in finding.ai_evidence_citations])
                    if finding.ai_guardrail_failures:
                        lines.append("  - Guardrail notes:")
                        lines.extend([f"    - {item}" for item in finding.ai_guardrail_failures])
                    lines.append("")
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
                if finding.ai_rca_summary or finding.ai_confidence_pct:
                    lines.extend([
                        "",
                        "- AI RCA Summary:",
                        f"- AI Prompt Template: {finding.ai_prompt_template or 'not recorded'}",
                        f"  - {finding.ai_rca_summary or 'No AI RCA available.'}",
                        f"- AI Confidence: {finding.ai_confidence_pct:.1f}%",
                        f"- AI Recommendation: {finding.ai_recommendation or finding.recommendation}",
                        f"- AI Validation Plan: {finding.ai_validation_plan or finding.validation_plan}",
                    ])
                    if finding.ai_evidence_citations:
                        lines.append("- AI Evidence citations:")
                        lines.extend([f"  - {item}" for item in finding.ai_evidence_citations])
                    if finding.ai_guardrail_failures:
                        lines.append("- AI Guardrail notes:")
                        lines.extend([f"  - {item}" for item in finding.ai_guardrail_failures])
                lines.append("")

        lines.extend(["## Failing Endpoints", ""])
        failing = _failing_endpoints(run)
        if not failing:
            lines.append("No endpoint SLA or error-rate failures were detected.")
            lines.append("")
        else:
            lines.extend(["| Scenario | Endpoint | Failure | Observed | Target | Suggested solution |", "| --- | --- | --- | ---: | ---: | --- |"])
            for item in failing:
                lines.append(
                    f"| {item['scenario']} | {item['endpoint']} | {item['failure']} | "
                    f"{item['observed']} | {item['target']} | {item['solution']} |"
                )
            lines.append("")

        lines.extend(["## Database Bottleneck Evidence", ""])
        database_findings = [finding for finding in run.findings if finding.category == "database"]
        if not database_findings:
            lines.append("No database bottleneck findings were detected from imported diagnostics.")
            lines.append("")
        else:
            for finding in database_findings:
                lines.extend([f"### {finding.title}", ""])
                lines.extend([f"- {item}" for item in finding.evidence])
                lines.append(f"- Recommendation: {finding.recommendation}")
                lines.append("")

        lines.extend(["## Observability and Database Connectors", ""])
        if run.connector_annotations:
            if run.connector_annotations.get("connector_status"):
                lines.append("### Connector Status")
                for item in run.connector_annotations["connector_status"]:
                    lines.append(f"- {item['name']} ({item['type']}): {item['status']}")
                lines.append("")
            if run.connector_annotations.get("grafana_dashboards"):
                lines.append("### Grafana Dashboards")
                for item in run.connector_annotations["grafana_dashboards"]:
                    lines.append(f"- {item['name']}: {item['url']}")
                lines.append("")
            if run.connector_annotations.get("trace_links"):
                lines.append("### Trace Correlation")
                for item in run.connector_annotations["trace_links"]:
                    lines.append(f"- {item['name']}: {item['url']}")
                lines.append("")
            if run.connector_annotations.get("external_connectors"):
                lines.append("### External Connectors")
                for item in run.connector_annotations["external_connectors"]:
                    lines.append(f"- {item['name']} ({item['type']}): {item['note']}")
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
        "connector_annotations": to_json(run.connector_annotations),
        "failing_endpoints": _failing_endpoints(run),
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
        "test_engine": run.config.test_engine.value,
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
        "connector_annotation_count": sum(len(v) for v in run.connector_annotations.values()) if run.connector_annotations else 0,
        "failing_endpoint_count": len(_failing_endpoints(run)),
        "database_finding_count": len([finding for finding in run.findings if finding.category == "database"]),
        "gate": _gate_result(run),
    }


def _gate_result(run: AgentRun) -> dict[str, Any]:
    readiness = run.readiness
    assert readiness is not None
    decision = {
        "green": "pass",
        "amber": "warn",
        "red": "block",
        "blocked": "block",
    }[readiness.status.value]
    exit_code = {"pass": 0, "warn": 1, "block": 2}[decision]
    return {
        "run_id": run.run_id,
        "application_name": run.config.application_name,
        "release_id": run.config.release_id,
        "environment": run.config.environment.name,
        "score": readiness.score,
        "status": readiness.status.value,
        "decision": decision,
        "exit_code": exit_code,
        "blockers": readiness.blockers,
        "policy": {
            "green": "score >= 90 and no critical blockers",
            "amber": "75 <= score < 90 and no critical blockers",
            "red": "60 <= score < 75 and no critical blockers",
            "blocked": "critical blocker present or score < 60",
        },
    }


def _failing_endpoints(run: AgentRun) -> list[dict[str, str]]:
    endpoint_targets = {
        endpoint.name: endpoint
        for scenario in run.config.scenarios
        for endpoint in scenario.endpoints
    }
    failures: list[dict[str, str]] = []
    for result in run.scenario_results:
        for endpoint_result in result.endpoint_results:
            endpoint = endpoint_targets.get(endpoint_result.name)
            if not endpoint:
                continue
            failures.extend(_endpoint_failures(result, endpoint, endpoint_result))
    return failures


def _endpoint_failures(result: ScenarioResult, endpoint: Endpoint, observed: EndpointResult) -> list[dict[str, str]]:
    checks = [
        ("p95 latency", observed.p95_ms, endpoint.sla.p95_ms, "Profile traces, optimize slow dependencies, add caching, and tune database queries on the endpoint path."),
        ("p99 tail latency", observed.p99_ms, endpoint.sla.p99_ms, "Compare fast versus slow traces, reduce retries/timeouts, and remove queueing or lock contention."),
        ("error rate", observed.error_rate_pct, endpoint.sla.error_rate_pct, "Group failed requests by status/exception, fix the top failure mode, and validate gateway/rate-limit rules."),
    ]
    failures = []
    for failure, value, target, solution in checks:
        if value > target:
            failures.append(
                {
                    "scenario": result.scenario_name,
                    "endpoint": endpoint.name,
                    "method": endpoint.method,
                    "url": endpoint.url,
                    "failure": failure,
                    "observed": str(round(value, 3)),
                    "target": str(round(target, 3)),
                    "solution": solution,
                }
            )
    return failures
