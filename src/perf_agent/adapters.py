from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, time as dt_time
from pathlib import Path
from typing import Any

from .models import (
    AgentConfig,
    DatabaseConnector,
    DatabaseFindingInput,
    Endpoint,
    EndpointResult,
    InfraMetrics,
    MonitoringConnector,
    Scenario,
    ScenarioResult,
    TestEngine,
    TestType,
    WebVitalsResult,
)


class ApprovalRequired(RuntimeError):
    pass


class GuardrailViolation(RuntimeError):
    pass


class Guardrail:
    def validate(self, config: AgentConfig, scenario: Scenario, approve_risky: bool) -> None:
        workload = scenario.workload
        env = config.environment
        if workload.concurrent_users > env.max_concurrent_users:
            raise GuardrailViolation(
                f"Scenario '{scenario.name}' requests {workload.concurrent_users} users, "
                f"above environment limit {env.max_concurrent_users}."
            )
        if workload.duration_seconds > env.max_duration_seconds:
            raise GuardrailViolation(
                f"Scenario '{scenario.name}' requests {workload.duration_seconds}s, "
                f"above environment limit {env.max_duration_seconds}s."
            )
        if env.max_target_tps is not None and workload.target_tps > env.max_target_tps:
            raise GuardrailViolation(
                f"Scenario '{scenario.name}' requests {workload.target_tps} TPS, "
                f"above environment cap {env.max_target_tps} TPS."
            )

        if env.allowed_hosts or env.allowed_url_prefixes:
            for item in scenario.endpoints + scenario.pages:
                if isinstance(item, Endpoint):
                    url = item.url
                else:
                    url = item.url
                parsed = urllib.parse.urlparse(url)
                host = parsed.hostname or ""
                if env.allowed_hosts and host.lower() not in env.allowed_hosts:
                    raise GuardrailViolation(
                        f"Scenario '{scenario.name}' references host '{host}' which is not allowed by environment allowlist."
                    )
                if env.allowed_url_prefixes and not any(url.startswith(prefix) for prefix in env.allowed_url_prefixes):
                    raise GuardrailViolation(
                        f"Scenario '{scenario.name}' references URL '{url}' which is not allowed by environment URL prefixes."
                    )

        if env.test_window_start and env.test_window_end:
            current = datetime.now().time()
            start = _parse_time(env.test_window_start)
            end = _parse_time(env.test_window_end)
            if start <= end:
                if not (start <= current <= end):
                    raise GuardrailViolation(
                        f"Test execution is only allowed between {env.test_window_start} and {env.test_window_end}."
                    )
            else:
                if not (current >= start or current <= end):
                    raise GuardrailViolation(
                        f"Test execution is only allowed between {env.test_window_start} and {env.test_window_end}."
                    )

        if scenario.requires_approval and not (approve_risky or env.allow_risky_tests):
            raise ApprovalRequired(
                f"Scenario '{scenario.name}' is a {scenario.test_type.value} test and needs approval. "
                "Rerun with --approve-risky or set environment.allow_risky_tests=true."
            )


def _parse_time(value: str) -> dt_time:
    parts = value.split(":")
    if len(parts) != 2:
        raise ValueError(f"Invalid time window value: {value}")
    return dt_time(int(parts[0]) % 24, int(parts[1]) % 60)


class ScenarioExecutor:
    def execute(self, config: AgentConfig, scenario: Scenario, run_id: str, output_dir: Path) -> ScenarioResult:
        raise NotImplementedError


class SyntheticExecutor(ScenarioExecutor):
    """Deterministic local executor used when real tools are not configured."""

    def execute(self, config: AgentConfig, scenario: Scenario, run_id: str, output_dir: Path) -> ScenarioResult:
        endpoint_results = [self._endpoint_result(endpoint, scenario) for endpoint in scenario.endpoints]
        web_results = [self._web_result(page, scenario) for page in scenario.pages]
        infra = self._infra_result(scenario)
        return ScenarioResult(
            scenario_name=scenario.name,
            test_type=scenario.test_type,
            endpoint_results=endpoint_results,
            web_vitals_results=web_results,
            infra_metrics=infra,
            raw={"executor": "synthetic", "run_id": run_id},
        )

    def _endpoint_result(self, endpoint: Endpoint, scenario: Scenario) -> EndpointResult:
        seed = _stable_int(endpoint.name + scenario.name)
        load_factor = max(1.0, scenario.workload.concurrent_users / 100)
        test_multiplier = {
            TestType.SMOKE: 0.45,
            TestType.LOAD: 0.9,
            TestType.STRESS: 1.45,
            TestType.SPIKE: 1.7,
            TestType.ENDURANCE: 1.1,
        }[scenario.test_type]
        jitter = 0.85 + (seed % 35) / 100
        p95 = endpoint.sla.p95_ms * test_multiplier * jitter * min(load_factor, 4) ** 0.18
        p99 = max(p95 * 1.25, endpoint.sla.p99_ms * test_multiplier * jitter * 0.95)
        error_rate = max(0.0, endpoint.sla.error_rate_pct * (test_multiplier - 0.2) + ((seed % 4) * 0.03))
        throughput = scenario.workload.target_tps * (0.85 + (seed % 25) / 100)
        return EndpointResult(
            name=endpoint.name,
            method=endpoint.method,
            url=endpoint.url,
            p50_ms=round(p95 * 0.45, 2),
            p95_ms=round(p95, 2),
            p99_ms=round(p99, 2),
            throughput_rps=round(throughput, 2),
            error_rate_pct=round(error_rate, 3),
            sample_count=max(1, int(throughput * scenario.workload.duration_seconds)),
        )

    def _web_result(self, page, scenario: Scenario) -> WebVitalsResult:
        seed = _stable_int(page.name + scenario.name)
        test_multiplier = 1.0 if scenario.test_type in {TestType.SMOKE, TestType.LOAD} else 1.25
        probe = _probe_url(page.url)
        if probe:
            ttfb = probe["ttfb_ms"]
            status_factor = 1.0 if 200 <= probe["status"] < 400 else 1.35
            return WebVitalsResult(
                page_name=page.name,
                url=page.url,
                lcp_p75_ms=round(max(900.0, ttfb * 2.6 + 650) * status_factor * test_multiplier, 2),
                inp_p75_ms=round((120 + seed % 120) * status_factor * test_multiplier, 2),
                cls_p75=round((seed % 12) / 100, 3),
                fcp_p75_ms=round(max(700.0, ttfb * 1.7 + 420) * status_factor * test_multiplier, 2),
                ttfb_p75_ms=round(ttfb, 2),
                source=f"http-probe-{probe['status']}",
            )
        return WebVitalsResult(
            page_name=page.name,
            url=page.url,
            lcp_p75_ms=round((1900 + seed % 1200) * test_multiplier, 2),
            inp_p75_ms=round((120 + seed % 180) * test_multiplier, 2),
            cls_p75=round((seed % 18) / 100, 3),
            fcp_p75_ms=round((1100 + seed % 900) * test_multiplier, 2),
            ttfb_p75_ms=round((350 + seed % 650) * test_multiplier, 2),
        )

    def _infra_result(self, scenario: Scenario) -> InfraMetrics:
        user_pressure = min(95, 25 + scenario.workload.concurrent_users / 20)
        type_pressure = {
            TestType.SMOKE: 0.5,
            TestType.LOAD: 0.8,
            TestType.STRESS: 1.2,
            TestType.SPIKE: 1.35,
            TestType.ENDURANCE: 0.95,
        }[scenario.test_type]
        cpu = min(99.0, user_pressure * type_pressure)
        return InfraMetrics(
            cpu_pct=round(cpu, 2),
            memory_pct=round(min(98.0, cpu * 0.82 + 14), 2),
            disk_io_pct=round(min(97.0, cpu * 0.55 + 8), 2),
            network_pct=round(min(96.0, cpu * 0.61 + 10), 2),
            error_budget_burn_pct=round(max(0.0, (cpu - 75) * 0.4), 2),
        )


class K6Executor(SyntheticExecutor):
    """k6 adapter with summary parsing and threshold generation."""

    def execute(self, config: AgentConfig, scenario: Scenario, run_id: str, output_dir: Path) -> ScenarioResult:
        if not shutil.which("k6"):
            result = super().execute(config, scenario, run_id, output_dir)
            result.raw["executor_note"] = "k6 not found on PATH; used deterministic synthetic executor."
            return result

        script = output_dir / f"{scenario.name.replace(' ', '_').lower()}_k6.js"
        script.write_text(_render_k6_script(scenario, run_id), encoding="utf-8")
        summary_path = output_dir / f"{script.stem}_summary.json"
        completed = subprocess.run(
            ["k6", "run", "--summary-export", str(summary_path), str(script)],
            capture_output=True,
            text=True,
            check=False,
        )
        if not summary_path.exists():
            result = super().execute(config, scenario, run_id, output_dir)
            result.raw["executor_note"] = "k6 execution failed; used synthetic executor."
            result.raw["k6_stderr"] = completed.stderr[-2000:]
            return result

        result = _parse_k6_summary(summary_path, scenario)
        if scenario.pages:
            result.web_vitals_results = [self._web_result(page, scenario) for page in scenario.pages]
        result.infra_metrics = self._infra_result(scenario)
        result.raw["executor"] = "k6"
        result.raw["k6_summary_path"] = str(summary_path)
        result.raw["k6_script_path"] = str(script)
        result.raw["k6_returncode"] = completed.returncode
        result.raw["k6_thresholds"] = _k6_thresholds(scenario)
        if completed.returncode != 0:
            result.raw["k6_stderr_tail"] = completed.stderr[-2000:]
        result.raw["k6_stdout_tail"] = completed.stdout[-2000:]
        return result


class LighthouseExecutor(SyntheticExecutor):
    """Runs Lighthouse CLI when available and maps audits to Web Vitals."""

    def execute(self, config: AgentConfig, scenario: Scenario, run_id: str, output_dir: Path) -> ScenarioResult:
        result = super().execute(config, scenario, run_id, output_dir)
        command = _find_lighthouse_command()
        if not command:
            result.raw["executor"] = "lighthouse"
            result.raw["executor_note"] = "Lighthouse CLI not found. Install lighthouse or use PageSpeed/WebPageTest connector."
            return result

        web_results: list[WebVitalsResult] = []
        reports = []
        for page in scenario.pages:
            report_path = output_dir / f"{_safe_name(page.name)}_lighthouse.json"
            args = command + [
                page.url,
                "--output=json",
                f"--output-path={report_path}",
                "--quiet",
                "--save-assets",
                "--throttling-method=devtools",
                "--chrome-flags=--headless=new --no-sandbox --disable-gpu",
            ]
            completed = subprocess.run(args, capture_output=True, text=True, check=False, timeout=180)
            reports.append({"page": page.url, "path": str(report_path), "returncode": completed.returncode})
            if completed.returncode == 0 and report_path.exists():
                web_results.append(_parse_lighthouse_report(report_path, page.name, page.url))
                trace_files = _find_lighthouse_trace_files(output_dir, page.name)
                if trace_files:
                    reports[-1]["traces"] = trace_files
            else:
                reports[-1]["stderr_tail"] = completed.stderr[-1500:]
        if web_results:
            result.web_vitals_results = web_results
        result.raw["executor"] = "lighthouse"
        result.raw["lighthouse_reports"] = reports
        result.raw["chrome_trace_note"] = "Lighthouse --save-assets is enabled when supported; DevTools trace files are captured when present."
        return result


class K6LighthouseExecutor(K6Executor):
    def execute(self, config: AgentConfig, scenario: Scenario, run_id: str, output_dir: Path) -> ScenarioResult:
        result = super().execute(config, scenario, run_id, output_dir)
        lighthouse = LighthouseExecutor()
        web_result = lighthouse.execute(config, scenario, run_id, output_dir)
        if web_result.web_vitals_results:
            result.web_vitals_results = web_result.web_vitals_results
        result.raw["lighthouse"] = web_result.raw
        return result


class PageSpeedExecutor(SyntheticExecutor):
    """Optional PageSpeed Insights adapter. Requires PAGESPEED_API_KEY for reliable usage."""

    def execute(self, config: AgentConfig, scenario: Scenario, run_id: str, output_dir: Path) -> ScenarioResult:
        result = super().execute(config, scenario, run_id, output_dir)
        api_key = os.environ.get("PAGESPEED_API_KEY", "")
        web_results = []
        notes = []
        for page in scenario.pages:
            query = {
                "url": page.url,
                "category": "performance",
                "strategy": "mobile",
            }
            if api_key:
                query["key"] = api_key
            endpoint = "https://www.googleapis.com/pagespeedonline/v5/runPagespeed?" + urllib.parse.urlencode(query)
            try:
                with urllib.request.urlopen(endpoint, timeout=60) as response:
                    payload = json.loads(response.read().decode("utf-8"))
                report_path = output_dir / f"{_safe_name(page.name)}_pagespeed.json"
                report_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
                web_results.append(_parse_lighthouse_payload(payload["lighthouseResult"], page.name, page.url, "pagespeed"))
                notes.append({"page": page.url, "path": str(report_path)})
            except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError, KeyError, json.JSONDecodeError) as exc:
                notes.append({"page": page.url, "error": str(exc)})
        if web_results:
            result.web_vitals_results = web_results
        result.raw["executor"] = "pagespeed"
        result.raw["pagespeed"] = notes
        return result


class WebPageTestExecutor(SyntheticExecutor):
    """Optional WebPageTest adapter. Requires WEBPAGETEST_API_KEY."""

    def execute(self, config: AgentConfig, scenario: Scenario, run_id: str, output_dir: Path) -> ScenarioResult:
        result = super().execute(config, scenario, run_id, output_dir)
        api_key = os.environ.get("WEBPAGETEST_API_KEY", "")
        if not api_key:
            result.raw["executor"] = "webpagetest"
            result.raw["executor_note"] = "WEBPAGETEST_API_KEY is not set; used fast local assessment."
            return result
        notes = []
        web_results = []
        for page in scenario.pages:
            try:
                submit_url = "https://www.webpagetest.org/runtest.php?" + urllib.parse.urlencode(
                    {"url": page.url, "k": api_key, "f": "json", "runs": 1}
                )
                with urllib.request.urlopen(submit_url, timeout=30) as response:
                    submitted = json.loads(response.read().decode("utf-8"))
                json_url = submitted["data"]["jsonUrl"]
                payload = _poll_json_url(json_url, timeout_seconds=180)
                report_path = output_dir / f"{_safe_name(page.name)}_webpagetest.json"
                report_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
                web_results.append(_parse_webpagetest_payload(payload, page.name, page.url))
                notes.append({"page": page.url, "path": str(report_path)})
            except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError, KeyError, json.JSONDecodeError) as exc:
                notes.append({"page": page.url, "error": str(exc)})
        if web_results:
            result.web_vitals_results = web_results
        result.raw["executor"] = "webpagetest"
        result.raw["webpagetest"] = notes
        return result


class JMeterExecutor(SyntheticExecutor):
    """First JMeter implementation: generates a minimal JMX, runs jmeter, parses JTL when available."""

    def execute(self, config: AgentConfig, scenario: Scenario, run_id: str, output_dir: Path) -> ScenarioResult:
        if not shutil.which("jmeter"):
            result = super().execute(config, scenario, run_id, output_dir)
            result.raw["executor"] = "jmeter"
            result.raw["executor_note"] = "jmeter not found on PATH; used fast local assessment."
            return result
        jmx_path = output_dir / f"{_safe_name(scenario.name)}.jmx"
        jtl_path = output_dir / f"{_safe_name(scenario.name)}.jtl"
        jmx_path.write_text(_render_jmeter_jmx(scenario), encoding="utf-8")
        completed = subprocess.run(
            ["jmeter", "-n", "-t", str(jmx_path), "-l", str(jtl_path)],
            capture_output=True,
            text=True,
            check=False,
            timeout=max(120, scenario.workload.duration_seconds + 60),
        )
        if completed.returncode != 0 or not jtl_path.exists():
            result = super().execute(config, scenario, run_id, output_dir)
            result.raw["executor"] = "jmeter"
            result.raw["executor_note"] = "JMeter execution failed; used fast local assessment."
            result.raw["jmeter_stderr"] = completed.stderr[-2000:]
            return result
        result = _parse_jmeter_jtl(jtl_path, scenario)
        result.web_vitals_results = [self._web_result(page, scenario) for page in scenario.pages]
        result.infra_metrics = self._infra_result(scenario)
        result.raw["executor"] = "jmeter"
        result.raw["jmx_path"] = str(jmx_path)
        result.raw["jtl_path"] = str(jtl_path)
        return result


class MetricsLoader:
    def load_infra(self, config: AgentConfig) -> InfraMetrics | None:
        if config.monitoring_connectors:
            for connector in config.monitoring_connectors:
                if connector.connector_type == "prometheus":
                    metrics = self._load_prometheus_metrics(connector)
                    if metrics:
                        return metrics
        return self._load_infra_file(config.monitoring_metrics_file)

    def load_monitoring_annotations(self, config: AgentConfig, run_id: str) -> dict[str, list[dict[str, str]]]:
        annotations = {
            "connector_status": [],
            "grafana_dashboards": [],
            "trace_links": [],
            "external_connectors": [],
        }
        for connector in config.monitoring_connectors:
            annotations["connector_status"].append(
                {
                    "name": connector.name,
                    "type": connector.connector_type,
                    "status": self._monitoring_connector_status(connector),
                }
            )
            if connector.dashboard_url:
                annotations["grafana_dashboards"].append({"name": connector.name, "url": connector.dashboard_url})
            if connector.connector_type == "opentelemetry" and connector.trace_url_template:
                annotations["trace_links"].append({
                    "name": connector.name,
                    "url": connector.trace_url_template.format(run_id=run_id),
                })
            if connector.connector_type in {"datadog", "newrelic", "dynatrace"}:
                annotations["external_connectors"].append(
                    {
                        "name": connector.name,
                        "type": connector.connector_type,
                        "note": "Connector design available; real API integration requires additional credentials.",
                    }
                )
        return annotations

    def load_database(self, config: AgentConfig) -> list[DatabaseFindingInput]:
        results: list[DatabaseFindingInput] = []
        if config.database_connectors:
            for connector in config.database_connectors:
                if connector.connector_type == "postgres":
                    results.extend(self._load_postgres_connector(connector))
                elif connector.connector_type == "mysql":
                    results.extend(self._load_mysql_slow_query_log(connector))
                elif connector.connector_type == "sqlserver":
                    results.extend(self._load_sqlserver_query_store(connector))
                else:
                    results.extend(_load_legacy_database_file(connector.source_file))
        if not results and config.database_metrics_file:
            results.extend(_load_legacy_database_file(config.database_metrics_file))
        return results

    def _load_infra_file(self, path: str | None) -> InfraMetrics | None:
        if not path:
            return None
        raw = json.loads(Path(path).read_text(encoding="utf-8"))
        return InfraMetrics(
            cpu_pct=float(raw.get("cpu_pct", 0)),
            memory_pct=float(raw.get("memory_pct", 0)),
            disk_io_pct=float(raw.get("disk_io_pct", 0)),
            network_pct=float(raw.get("network_pct", 0)),
            error_budget_burn_pct=float(raw.get("error_budget_burn_pct", 0)),
        )

    def _load_prometheus_metrics(self, connector: MonitoringConnector) -> InfraMetrics | None:
        if not connector.endpoint:
            return None
        if connector.query and not connector.options:
            value = self._prometheus_query(connector.endpoint, connector.query, connector.api_key)
            if value is None:
                return None
            return InfraMetrics(
                cpu_pct=round(value, 2),
                memory_pct=0.0,
                disk_io_pct=0.0,
                network_pct=0.0,
                error_budget_burn_pct=0.0,
            )
        values: dict[str, float] = {}
        queries = {
            "cpu_pct": connector.options.get("cpu_query"),
            "memory_pct": connector.options.get("memory_query"),
            "disk_io_pct": connector.options.get("disk_query"),
            "network_pct": connector.options.get("network_query"),
            "error_budget_burn_pct": connector.options.get("error_budget_query"),
        }
        for field, query in queries.items():
            if not query:
                continue
            metric_value = self._prometheus_query(connector.endpoint, query, connector.api_key)
            if metric_value is not None:
                values[field] = metric_value

        if not values:
            return None
        return InfraMetrics(
            cpu_pct=round(values.get("cpu_pct", 0.0), 2),
            memory_pct=round(values.get("memory_pct", 0.0), 2),
            disk_io_pct=round(values.get("disk_io_pct", 0.0), 2),
            network_pct=round(values.get("network_pct", 0.0), 2),
            error_budget_burn_pct=round(values.get("error_budget_burn_pct", 0.0), 2),
        )

    def _prometheus_query(self, endpoint: str, query: str, api_key: str | None) -> float | None:
        try:
            url = endpoint.rstrip("/") + "/api/v1/query?" + urllib.parse.urlencode({"query": query})
            request = urllib.request.Request(url)
            if api_key:
                request.add_header("Authorization", f"Bearer {api_key}")
            with urllib.request.urlopen(request, timeout=30) as response:
                payload = json.loads(response.read().decode("utf-8"))
            if payload.get("status") != "success":
                return None
            result = payload.get("data", {}).get("result", [])
            if not result:
                return None
            value = result[0].get("value", [])
            return float(value[1]) if len(value) >= 2 else None
        except Exception:
            return None

    def _monitoring_connector_status(self, connector: MonitoringConnector) -> str:
        if connector.connector_type == "prometheus":
            if not connector.endpoint:
                return "missing endpoint"
            if connector.query:
                value = self._prometheus_query(connector.endpoint, connector.query, connector.api_key)
                return "query succeeded" if value is not None else "query unavailable"
            if connector.options:
                return "configured"
            return "missing query mapping"
        if connector.connector_type == "grafana":
            return "dashboard linked" if connector.dashboard_url else "missing dashboard URL"
        if connector.connector_type == "opentelemetry":
            return "trace link templated" if connector.trace_url_template else "missing trace URL template"
        if connector.connector_type in {"datadog", "newrelic", "dynatrace"}:
            return "adapter design registered"
        return "configured"

    def _load_postgres_connector(self, connector: DatabaseConnector) -> list[DatabaseFindingInput]:
        rows: list[dict[str, Any]] = []
        explain_lookup: dict[str, str] = {}
        if connector.source_file and Path(connector.source_file).exists():
            raw = json.loads(Path(connector.source_file).read_text(encoding="utf-8"))
            if isinstance(raw, dict) and "pg_stat_statements" in raw:
                rows = raw["pg_stat_statements"]
            elif isinstance(raw, dict) and "slow_queries" in raw:
                rows = raw["slow_queries"]
            if isinstance(raw, dict):
                for plan in raw.get("explain_plans", []):
                    fingerprint = str(plan.get("query", plan.get("query_text", "")))
                    summary = plan.get("summary") or _summarize_explain_plan(plan.get("plan", plan))
                    if fingerprint and summary:
                        explain_lookup[fingerprint] = str(summary)
        return [
            DatabaseFindingInput(
                query=str(item.get("query", item.get("query_text", ""))),
                avg_ms=float(item.get("mean_time", item.get("avg_ms", 0))),
                p95_ms=float(item.get("p95_time", item.get("p95_ms", item.get("mean_time", 0)))),
                calls=int(item.get("calls", item.get("calls_total", 0))),
                rows_examined=item.get("rows", item.get("rows_examined")),
                lock_wait_ms=float(item.get("lock_wait_ms", item.get("lock_time", 0))),
                recommendation_hint=_join_recommendation(
                    item.get("recommendation_hint") or "Review PostgreSQL pg_stat_statements and explain plans for the slowest queries.",
                    explain_lookup.get(str(item.get("query", item.get("query_text", "")))),
                ),
            )
            for item in rows
        ]

    def _load_mysql_slow_query_log(self, connector: DatabaseConnector) -> list[DatabaseFindingInput]:
        if not connector.source_file or not Path(connector.source_file).exists():
            return []
        lines = Path(connector.source_file).read_text(encoding="utf-8", errors="ignore").splitlines()
        items: list[DatabaseFindingInput] = []
        current: dict[str, Any] = {}
        query_lines: list[str] = []
        for line in lines:
            if line.startswith("# Query_time:"):
                if current and query_lines:
                    items.append(self._build_mysql_entry(current, query_lines))
                current = {}
                query_lines = []
                current.update(_parse_mysql_metric_line(line))
                continue
            if line.startswith("use ") or line.startswith("SET timestamp"):
                continue
            if line and not line.startswith("#"):
                query_lines.append(line)
        if current and query_lines:
            items.append(self._build_mysql_entry(current, query_lines))
        return items

    def _build_mysql_entry(self, current: dict[str, Any], query_lines: list[str]) -> DatabaseFindingInput:
        query = "\n".join(query_lines).strip()
        return DatabaseFindingInput(
            query=query,
            avg_ms=float(current.get("query_time", 0.0)),
            p95_ms=float(current.get("query_time", 0.0)),
            calls=int(current.get("rows_sent", 0)),
            rows_examined=int(current.get("rows_examined", 0)),
            lock_wait_ms=float(current.get("lock_time", 0.0)),
            recommendation_hint="Review MySQL slow query log, add indexes, and avoid full table scans.",
        )

    def _load_sqlserver_query_store(self, connector: DatabaseConnector) -> list[DatabaseFindingInput]:
        if not connector.source_file or not Path(connector.source_file).exists():
            return []
        raw = json.loads(Path(connector.source_file).read_text(encoding="utf-8"))
        rows = raw.get("query_store", raw.get("queries", [])) if isinstance(raw, dict) else []
        return [
            DatabaseFindingInput(
                query=str(item.get("query_text", item.get("query", ""))),
                avg_ms=float(item.get("avg_duration_ms", item.get("avg_ms", 0))),
                p95_ms=float(item.get("max_duration_ms", item.get("p95_ms", 0))),
                calls=int(item.get("execution_count", item.get("calls", 0))),
                rows_examined=item.get("rows_scanned"),
                lock_wait_ms=float(item.get("wait_time_ms", 0.0)),
                recommendation_hint="Import SQL Server Query Store output and compare execution_count with high-duration plans.",
            )
            for item in rows
        ]


def _parse_mysql_metric_line(line: str) -> dict[str, str]:
    parts = line[1:].strip().split()
    values: dict[str, str] = {}
    index = 0
    while index < len(parts):
        key = parts[index].rstrip(":").lower()
        if index + 1 < len(parts) and parts[index].endswith(":"):
            values[key] = parts[index + 1]
            index += 2
            continue
        if ":" in parts[index]:
            key, value = parts[index].split(":", 1)
            values[key.lower()] = value
        index += 1
    return values


def _summarize_explain_plan(plan: Any) -> str:
    text = json.dumps(plan, sort_keys=True) if isinstance(plan, (dict, list)) else str(plan or "")
    hints = []
    lowered = text.lower()
    if "seq scan" in lowered or "table scan" in lowered:
        hints.append("explain plan shows a scan; validate index coverage and selectivity")
    if "sort" in lowered:
        hints.append("explain plan shows a sort; check order-by indexes and memory")
    if "nested loop" in lowered:
        hints.append("explain plan shows nested loops; verify join cardinality and indexes")
    if "lock" in lowered:
        hints.append("plan or notes mention locking; shorten transaction scope")
    return "; ".join(hints)


def _join_recommendation(base: str, plan_summary: str | None) -> str:
    return f"{base} Plan evidence: {plan_summary}." if plan_summary else base


def _load_legacy_database_file(path: str | None) -> list[DatabaseFindingInput]:
    if not path or not Path(path).exists():
        return []
    raw = json.loads(Path(path).read_text(encoding="utf-8"))
    rows = raw.get("slow_queries", raw if isinstance(raw, list) else [])
    return [
        DatabaseFindingInput(
            query=str(item.get("query", "")),
            avg_ms=float(item.get("avg_ms", 0)),
            p95_ms=float(item.get("p95_ms", item.get("avg_ms", 0))),
            calls=int(item.get("calls", 0)),
            rows_examined=item.get("rows_examined"),
            lock_wait_ms=float(item.get("lock_wait_ms", 0)),
            recommendation_hint=item.get("recommendation_hint"),
        )
        for item in rows
    ]


def executor_for(engine: TestEngine) -> ScenarioExecutor:
    return {
        TestEngine.SYNTHETIC: SyntheticExecutor,
        TestEngine.K6: K6Executor,
        TestEngine.LIGHTHOUSE: LighthouseExecutor,
        TestEngine.K6_LIGHTHOUSE: K6LighthouseExecutor,
        TestEngine.PAGESPEED: PageSpeedExecutor,
        TestEngine.WEBPAGETEST: WebPageTestExecutor,
        TestEngine.JMETER: JMeterExecutor,
    }[engine]()


def _k6_metric_name(name: str, tags: dict[str, str] | None = None) -> str:
    if not tags:
        return name
    label = ", ".join(f'{key}:"{value}"' for key, value in tags.items())
    return f"{name}{{{label}}}"


def _k6_thresholds(scenario: Scenario) -> dict[str, list[str]]:
    error_limit = max(endpoint.sla.error_rate_pct for endpoint in scenario.endpoints) / 100 if scenario.endpoints else 0.01
    thresholds = {
        "http_req_failed": [f"rate<{max(0.0001, error_limit)}"],
        "http_reqs": [f"rate>{max(0.1, scenario.workload.target_tps * 0.5)}"],
    }
    for endpoint in scenario.endpoints:
        thresholds[_k6_metric_name("http_req_duration", {"endpoint": endpoint.name})] = [
            f"p(95)<{endpoint.sla.p95_ms}",
            f"p(99)<{endpoint.sla.p99_ms}",
        ]
        thresholds[_k6_metric_name("http_req_failed", {"endpoint": endpoint.name})] = [
            f"rate<{max(0.0001, endpoint.sla.error_rate_pct / 100)}",
        ]
    return thresholds


def _render_k6_script(scenario: Scenario, run_id: str) -> str:
    requests = []
    thresholds = []
    for index, endpoint in enumerate(scenario.endpoints):
        name = _js_string(endpoint.name)
        method = _js_string(endpoint.method)
        url = _js_string(endpoint.url)
        requests.append(
            f"""  group({name}, function () {{
    const res{index} = http.request({method}, {url}, null, {{
      ...params,
      tags: {{ endpoint: {name}, method: {method} }},
    }});
    check(res{index}, {{
      'status is below 500': (r) => r.status < 500,
      'p95 target hint': (r) => r.timings.duration < {endpoint.sla.p95_ms},
    }});
  }});"""
        )
    body = "\n".join(requests) or '  http.get("https://test.k6.io", params);'
    vus = scenario.workload.concurrent_users
    duration = f"{scenario.workload.duration_seconds}s"
    threshold_map = _k6_thresholds(scenario)
    thresholds = [
        f"'{metric}': [{', '.join(_js_string(value) for value in checks)}]"
        for metric, checks in threshold_map.items()
    ]

    if scenario.workload.ramp_up_seconds and scenario.workload.ramp_up_seconds < scenario.workload.duration_seconds:
        stages = [
            f"{{ duration: '{scenario.workload.ramp_up_seconds}s', target: {vus} }}",
            f"{{ duration: '{scenario.workload.duration_seconds - scenario.workload.ramp_up_seconds}s', target: {vus} }}",
        ]
        stages_text = f"  stages: [\n    {',\n    '.join(stages)}\n  ],\n"
    else:
        stages_text = f"  vus: {vus},\n  duration: '{duration}',\n"

    return f"""import http from 'k6/http';
import {{ check, group, sleep }} from 'k6';

export const options = {{
{stages_text}
  thresholds: {{
    {",\n    ".join(thresholds)}
  }},
}};

export default function () {{
  const params = {{ headers: {{ 'x-performance-test-scenario': {_js_string(scenario.name)}, 'x-performance-test-run': {_js_string(run_id)} }} }};
{body}
  sleep(1);
}}
"""


def _parse_k6_summary(path: Path, scenario: Scenario) -> ScenarioResult:
    raw = json.loads(path.read_text(encoding="utf-8"))
    metrics = raw.get("metrics", {})
    global_duration = _metric_values(metrics, "http_req_duration")
    global_reqs = _metric_values(metrics, "http_reqs")
    global_failed = _metric_values(metrics, "http_req_failed")
    duration = global_duration or {}
    rate = float(global_reqs.get("rate", 0.0))
    count = int(global_reqs.get("count", 0) or rate * scenario.workload.duration_seconds)
    error_rate = float(global_failed.get("rate", 0.0)) * 100
    endpoint_results = []
    for endpoint in scenario.endpoints:
        endpoint_duration = _metric_values(metrics, _k6_metric_name("http_req_duration", {"endpoint": endpoint.name})) or duration
        endpoint_reqs = _metric_values(metrics, _k6_metric_name("http_reqs", {"endpoint": endpoint.name}))
        endpoint_failed = _metric_values(metrics, _k6_metric_name("http_req_failed", {"endpoint": endpoint.name})) or global_failed
        endpoint_rate = float(endpoint_reqs.get("rate", rate)) if endpoint_reqs else rate
        endpoint_count = int(endpoint_reqs.get("count", count)) if endpoint_reqs else count
        endpoint_results.append(
            EndpointResult(
                name=endpoint.name,
                method=endpoint.method,
                url=endpoint.url,
                p50_ms=round(float(endpoint_duration.get("med", endpoint_duration.get("avg", 0.0))), 2),
                p95_ms=round(float(endpoint_duration.get("p(95)", duration.get("p(95)", 0.0))), 2),
                p99_ms=round(float(endpoint_duration.get("p(99)", duration.get("p(99)", 0.0))), 2),
                throughput_rps=round(endpoint_rate, 2),
                error_rate_pct=round(float(endpoint_failed.get("rate", 0.0)) * 100, 3),
                sample_count=endpoint_count,
            )
        )
    return ScenarioResult(
        scenario_name=scenario.name,
        test_type=scenario.test_type,
        endpoint_results=endpoint_results,
        raw={"k6_metrics": metrics},
    )


def _metric_values(metrics: dict, name: str) -> dict:
    metric = metrics.get(name, {})
    return metric.get("values", {}) if isinstance(metric, dict) else {}


def _find_lighthouse_command() -> list[str] | None:
    lighthouse = shutil.which("lighthouse")
    if lighthouse:
        return [lighthouse]
    return None


def _find_lighthouse_trace_files(output_dir: Path, page_name: str) -> list[str]:
    traces = []
    for candidate in output_dir.iterdir():
        lowered = candidate.name.lower()
        if lowered.endswith(".trace.json") or lowered.endswith(".trace") or "trace" in lowered:
            if page_name.lower().replace(" ", "_") in lowered or candidate.is_file():
                traces.append(str(candidate.resolve()))
    return traces


def _parse_lighthouse_report(path: Path, page_name: str, url: str) -> WebVitalsResult:
    payload = json.loads(path.read_text(encoding="utf-8"))
    return _parse_lighthouse_payload(payload, page_name, url, "lighthouse")


def _parse_lighthouse_payload(payload: dict, page_name: str, url: str, source: str) -> WebVitalsResult:
    audits = payload.get("audits", {})
    lcp = _audit_numeric(audits, "largest-contentful-paint")
    fcp = _audit_numeric(audits, "first-contentful-paint")
    cls = _audit_numeric(audits, "cumulative-layout-shift")
    ttfb = _audit_numeric(audits, "server-response-time")
    inp = _audit_numeric(audits, "experimental-interaction-to-next-paint")
    if not inp:
        # Lighthouse lab runs often do not have field INP. TBT is a useful proxy for main-thread interaction risk.
        inp = min(700.0, max(80.0, _audit_numeric(audits, "total-blocking-time") or 120.0))
    return WebVitalsResult(
        page_name=page_name,
        url=url,
        lcp_p75_ms=round(lcp or 0.0, 2),
        inp_p75_ms=round(inp, 2),
        cls_p75=round(cls or 0.0, 3),
        fcp_p75_ms=round(fcp or 0.0, 2),
        ttfb_p75_ms=round(ttfb or 0.0, 2),
        source=source,
    )


def _parse_webpagetest_payload(payload: dict, page_name: str, url: str) -> WebVitalsResult:
    median = payload.get("data", {}).get("median", {}).get("firstView", {})
    return WebVitalsResult(
        page_name=page_name,
        url=url,
        lcp_p75_ms=round(float(median.get("chromeUserTiming.LargestContentfulPaint", median.get("render", 0))), 2),
        inp_p75_ms=round(float(median.get("TotalBlockingTime", 120)), 2),
        cls_p75=round(float(median.get("chromeUserTiming.CumulativeLayoutShift", 0)), 3),
        fcp_p75_ms=round(float(median.get("firstContentfulPaint", median.get("render", 0))), 2),
        ttfb_p75_ms=round(float(median.get("TTFB", 0)), 2),
        source="webpagetest",
    )


def _audit_numeric(audits: dict, key: str) -> float:
    value = audits.get(key, {}).get("numericValue", 0.0)
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def _poll_json_url(url: str, timeout_seconds: int) -> dict:
    deadline = time.time() + timeout_seconds
    last_payload = {}
    while time.time() < deadline:
        with urllib.request.urlopen(url, timeout=30) as response:
            last_payload = json.loads(response.read().decode("utf-8"))
        if last_payload.get("statusCode") == 200:
            return last_payload
        time.sleep(5)
    return last_payload


def _render_jmeter_jmx(scenario: Scenario) -> str:
    samplers = "\n".join(_jmeter_sampler(endpoint) for endpoint in scenario.endpoints)
    duration = scenario.workload.duration_seconds
    users = scenario.workload.concurrent_users
    return f"""<?xml version="1.0" encoding="UTF-8"?>
<jmeterTestPlan version="1.2" properties="5.0" jmeter="5.6">
  <hashTree>
    <TestPlan guiclass="TestPlanGui" testclass="TestPlan" testname="Performance Agent Plan" enabled="true">
      <stringProp name="TestPlan.comments">Generated by Performance Testing AI Agent</stringProp>
      <boolProp name="TestPlan.functional_mode">false</boolProp>
      <boolProp name="TestPlan.serialize_threadgroups">false</boolProp>
    </TestPlan>
    <hashTree>
      <ThreadGroup guiclass="ThreadGroupGui" testclass="ThreadGroup" testname="{_xml_escape(scenario.name)}" enabled="true">
        <intProp name="ThreadGroup.num_threads">{users}</intProp>
        <intProp name="ThreadGroup.ramp_time">{min(duration, scenario.workload.ramp_up_seconds)}</intProp>
        <boolProp name="ThreadGroup.same_user_on_next_iteration">true</boolProp>
        <stringProp name="ThreadGroup.duration">{duration}</stringProp>
        <boolProp name="ThreadGroup.scheduler">true</boolProp>
        <elementProp name="ThreadGroup.main_controller" elementType="LoopController">
          <boolProp name="LoopController.continue_forever">true</boolProp>
          <intProp name="LoopController.loops">-1</intProp>
        </elementProp>
      </ThreadGroup>
      <hashTree>{samplers}</hashTree>
    </hashTree>
  </hashTree>
</jmeterTestPlan>
"""


def _jmeter_sampler(endpoint: Endpoint) -> str:
    parsed = urllib.parse.urlparse(endpoint.url)
    path = parsed.path or "/"
    if parsed.query:
        path += "?" + parsed.query
    return f"""
        <HTTPSamplerProxy guiclass="HttpTestSampleGui" testclass="HTTPSamplerProxy" testname="{_xml_escape(endpoint.name)}" enabled="true">
          <stringProp name="HTTPSampler.domain">{_xml_escape(parsed.hostname or "")}</stringProp>
          <stringProp name="HTTPSampler.protocol">{_xml_escape(parsed.scheme or "https")}</stringProp>
          <stringProp name="HTTPSampler.path">{_xml_escape(path)}</stringProp>
          <stringProp name="HTTPSampler.method">{_xml_escape(endpoint.method)}</stringProp>
          <boolProp name="HTTPSampler.follow_redirects">true</boolProp>
        </HTTPSamplerProxy>
        <hashTree/>
"""


def _parse_jmeter_jtl(path: Path, scenario: Scenario) -> ScenarioResult:
    import csv

    rows = list(csv.DictReader(path.read_text(encoding="utf-8", errors="ignore").splitlines()))
    endpoint_results = []
    for endpoint in scenario.endpoints:
        samples = [row for row in rows if row.get("label") == endpoint.name]
        if not samples:
            samples = rows
        elapsed = sorted(float(row.get("elapsed", 0)) for row in samples)
        count = len(elapsed)
        failures = sum(1 for row in samples if str(row.get("success", "true")).lower() != "true")
        endpoint_results.append(
            EndpointResult(
                name=endpoint.name,
                method=endpoint.method,
                url=endpoint.url,
                p50_ms=round(_percentile(elapsed, 50), 2),
                p95_ms=round(_percentile(elapsed, 95), 2),
                p99_ms=round(_percentile(elapsed, 99), 2),
                throughput_rps=round(count / max(1, scenario.workload.duration_seconds), 2),
                error_rate_pct=round((failures / max(1, count)) * 100, 3),
                sample_count=count,
            )
        )
    return ScenarioResult(scenario_name=scenario.name, test_type=scenario.test_type, endpoint_results=endpoint_results)


def _percentile(values: list[float], pct: int) -> float:
    if not values:
        return 0.0
    index = min(len(values) - 1, max(0, round((pct / 100) * (len(values) - 1))))
    return values[index]


def _safe_name(value: str) -> str:
    return "".join(ch.lower() if ch.isalnum() else "_" for ch in value).strip("_") or "scenario"


def _js_string(value: str) -> str:
    return json.dumps(value)


def _xml_escape(value: str) -> str:
    return (
        value.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
        .replace("'", "&apos;")
    )


def _stable_int(value: str) -> int:
    return int(hashlib.sha256(value.encode("utf-8")).hexdigest()[:8], 16)


def _probe_url(url: str) -> dict[str, float | int] | None:
    if not url.startswith(("http://", "https://")):
        return None
    request = urllib.request.Request(url, headers={"User-Agent": "PerformanceTestingAIAgent/0.1"})
    started = time.perf_counter()
    try:
        with urllib.request.urlopen(request, timeout=5) as response:
            response.read(1024)
            elapsed_ms = (time.perf_counter() - started) * 1000
            return {"status": int(response.status), "ttfb_ms": elapsed_ms}
    except urllib.error.HTTPError as exc:
        elapsed_ms = (time.perf_counter() - started) * 1000
        return {"status": int(exc.code), "ttfb_ms": elapsed_ms}
    except (urllib.error.URLError, TimeoutError, ValueError):
        return None
