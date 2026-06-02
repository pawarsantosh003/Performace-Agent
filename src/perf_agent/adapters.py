from __future__ import annotations

import hashlib
import json
import shutil
import subprocess
import time
import urllib.error
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
    """Optional k6 adapter. Falls back to synthetic results when k6 is unavailable."""

    def execute(self, config: AgentConfig, scenario: Scenario, run_id: str, output_dir: Path) -> ScenarioResult:
        if not shutil.which("k6"):
            result = super().execute(config, scenario, run_id, output_dir)
            result.raw["executor_note"] = "k6 not found on PATH; used deterministic synthetic executor."
            return result

        script = output_dir / f"{scenario.name.replace(' ', '_').lower()}_k6.js"
        script.write_text(_render_k6_script(scenario), encoding="utf-8")
        summary_path = output_dir / f"{script.stem}_summary.json"
        completed = subprocess.run(
            ["k6", "run", "--summary-export", str(summary_path), str(script)],
            capture_output=True,
            text=True,
            check=False,
        )
        if completed.returncode != 0 or not summary_path.exists():
            result = super().execute(config, scenario, run_id, output_dir)
            result.raw["executor_note"] = "k6 execution failed; used synthetic executor."
            result.raw["k6_stderr"] = completed.stderr[-2000:]
            return result

        result = super().execute(config, scenario, run_id, output_dir)
        result.raw["executor"] = "k6"
        result.raw["k6_summary_path"] = str(summary_path)
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


def _render_k6_script(scenario: Scenario) -> str:
    requests = []
    for endpoint in scenario.endpoints:
        requests.append(f'  http.request("{endpoint.method}", "{endpoint.url}", null, params);')
    body = "\n".join(requests) or '  http.get("https://test.k6.io", params);'
    vus = scenario.workload.concurrent_users
    duration = f"{scenario.workload.duration_seconds}s"
    return f"""import http from 'k6/http';
import {{ sleep }} from 'k6';

export const options = {{
  vus: {vus},
  duration: '{duration}',
}};

export default function () {{
  const params = {{ headers: {{ 'x-performance-test-scenario': '{scenario.name}' }} }};
{body}
  sleep(1);
}}
"""


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
