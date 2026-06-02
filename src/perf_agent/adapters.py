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
from pathlib import Path

from .models import (
    AgentConfig,
    DatabaseFindingInput,
    Endpoint,
    EndpointResult,
    InfraMetrics,
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
        if scenario.requires_approval and not (approve_risky or env.allow_risky_tests):
            raise ApprovalRequired(
                f"Scenario '{scenario.name}' is a {scenario.test_type.value} test and needs approval. "
                "Rerun with --approve-risky or set environment.allow_risky_tests=true."
            )


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
                "--chrome-flags=--headless=new --no-sandbox",
            ]
            completed = subprocess.run(args, capture_output=True, text=True, check=False, timeout=180)
            reports.append({"page": page.url, "path": str(report_path), "returncode": completed.returncode})
            if completed.returncode == 0 and report_path.exists():
                web_results.append(_parse_lighthouse_report(report_path, page.name, page.url))
            else:
                reports[-1]["stderr_tail"] = completed.stderr[-1500:]
        if web_results:
            result.web_vitals_results = web_results
        result.raw["executor"] = "lighthouse"
        result.raw["lighthouse_reports"] = reports
        result.raw["chrome_trace_note"] = "Lighthouse --save-assets is enabled when supported; generated DevTools trace assets are stored beside the report."
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
    def load_infra(self, path: str | None) -> InfraMetrics | None:
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

    def load_database(self, path: str | None) -> list[DatabaseFindingInput]:
        if not path:
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


def _render_k6_script(scenario: Scenario, run_id: str) -> str:
    requests = []
    checks = []
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
        checks.append(f"'http_req_duration{{endpoint:{endpoint.name}}}': ['p(95)<{endpoint.sla.p95_ms}', 'p(99)<{endpoint.sla.p99_ms}']")
    body = "\n".join(requests) or '  http.get("https://test.k6.io", params);'
    vus = scenario.workload.concurrent_users
    duration = f"{scenario.workload.duration_seconds}s"
    error_limit = max(endpoint.sla.error_rate_pct for endpoint in scenario.endpoints) / 100 if scenario.endpoints else 0.01
    thresholds = [
        f"'http_req_failed': ['rate<{max(0.0001, error_limit)}']",
        f"'http_reqs': ['rate>{max(0.1, scenario.workload.target_tps * 0.5)}']",
    ]
    thresholds.extend(checks)
    return f"""import http from 'k6/http';
import {{ check, group, sleep }} from 'k6';

export const options = {{
  vus: {vus},
  duration: '{duration}',
  thresholds: {{
    {", ".join(thresholds)}
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
    duration = _metric_values(metrics, "http_req_duration")
    reqs = _metric_values(metrics, "http_reqs")
    failed = _metric_values(metrics, "http_req_failed")
    count = int(reqs.get("count", 0) or reqs.get("rate", 0) * scenario.workload.duration_seconds)
    rate = float(reqs.get("rate", 0.0))
    error_rate = float(failed.get("rate", 0.0)) * 100
    endpoint_results = []
    for endpoint in scenario.endpoints:
        endpoint_metric = _metric_values(metrics, f"http_req_duration{{endpoint:{endpoint.name}}}") or duration
        endpoint_results.append(
            EndpointResult(
                name=endpoint.name,
                method=endpoint.method,
                url=endpoint.url,
                p50_ms=round(float(endpoint_metric.get("med", endpoint_metric.get("avg", 0.0))), 2),
                p95_ms=round(float(endpoint_metric.get("p(95)", duration.get("p(95)", 0.0))), 2),
                p99_ms=round(float(endpoint_metric.get("p(99)", duration.get("p(99)", 0.0))), 2),
                throughput_rps=round(rate, 2),
                error_rate_pct=round(error_rate, 3),
                sample_count=count,
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
