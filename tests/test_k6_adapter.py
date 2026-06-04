import json
import tempfile
import unittest
from pathlib import Path

from perf_agent.adapters import _k6_metric_name, _k6_thresholds, _parse_k6_summary, _render_k6_script
from perf_agent.models import ApiSla, Endpoint, Scenario, TestType, Workload


class K6AdapterTests(unittest.TestCase):
    def test_k6_metric_name_quotes_tags(self) -> None:
        name = _k6_metric_name("http_req_duration", {"endpoint": "search query", "method": "GET"})
        self.assertEqual(name, 'http_req_duration{endpoint:"search query", method:"GET"}')

    def test_k6_thresholds_include_endpoint_specific_values(self) -> None:
        scenario = Scenario(
            name="search",
            test_type=TestType.LOAD,
            workload=Workload(concurrent_users=20, duration_seconds=30, ramp_up_seconds=5, target_tps=10.0),
            endpoints=[
                Endpoint(
                    name="search query",
                    method="GET",
                    url="https://example.com/search?q=test",
                    sla=ApiSla(p95_ms=250, p99_ms=400, error_rate_pct=0.5, throughput_rps=10.0),
                )
            ],
            pages=[],
        )
        thresholds = _k6_thresholds(scenario)
        self.assertIn('http_req_failed', thresholds)
        self.assertIn('http_reqs', thresholds)
        self.assertIn('http_req_duration{endpoint:"search query"}', thresholds)
        self.assertIn('http_req_failed{endpoint:"search query"}', thresholds)
        self.assertEqual(thresholds['http_req_duration{endpoint:"search query"}'][0], 'p(95)<250')

    def test_render_k6_script_includes_expected_sections(self) -> None:
        scenario = Scenario(
            name="search",
            test_type=TestType.LOAD,
            workload=Workload(concurrent_users=5, duration_seconds=20, ramp_up_seconds=5, target_tps=5.0),
            endpoints=[
                Endpoint(
                    name="search query",
                    method="GET",
                    url="https://example.com/search?q=test",
                    sla=ApiSla(p95_ms=250, p99_ms=400, error_rate_pct=0.5, throughput_rps=5.0),
                )
            ],
            pages=[],
        )
        script = _render_k6_script(scenario, "run-123")
        self.assertIn("stages:", script)
        self.assertIn("thresholds:", script)
        self.assertIn("http_req_duration{endpoint:\"search query\"}", script)
        self.assertIn("http_req_failed{endpoint:\"search query\"}", script)

    def test_parse_k6_summary_maps_endpoint_metrics(self) -> None:
        scenario = Scenario(
            name="search",
            test_type=TestType.LOAD,
            workload=Workload(concurrent_users=20, duration_seconds=60, ramp_up_seconds=10, target_tps=10.0),
            endpoints=[
                Endpoint(
                    name="search query",
                    method="GET",
                    url="https://example.com/search?q=test",
                    sla=ApiSla(p95_ms=250, p99_ms=400, error_rate_pct=0.5, throughput_rps=10.0),
                )
            ],
            pages=[],
        )
        summary = {
            "metrics": {
                "http_req_duration": {"values": {"med": 170.0, "p(95)": 220.0, "p(99)": 330.0, "avg": 175.0}},
                "http_reqs": {"values": {"rate": 12.5, "count": 750}},
                "http_req_failed": {"values": {"rate": 0.004}},
                'http_req_duration{endpoint:"search query"}': {"values": {"med": 180.0, "p(95)": 230.0, "p(99)": 320.0, "avg": 190.0}},
                'http_req_failed{endpoint:"search query"}': {"values": {"rate": 0.002}},
            }
        }
        with tempfile.TemporaryDirectory() as tmp:
            summary_path = Path(tmp) / "k6_summary.json"
            summary_path.write_text(json.dumps(summary), encoding="utf-8")
            result = _parse_k6_summary(summary_path, scenario)

        self.assertEqual(len(result.endpoint_results), 1)
        endpoint_result = result.endpoint_results[0]
        self.assertEqual(endpoint_result.p95_ms, 230.0)
        self.assertEqual(endpoint_result.p99_ms, 320.0)
        self.assertEqual(endpoint_result.throughput_rps, 12.5)
        self.assertEqual(endpoint_result.error_rate_pct, 0.2)


if __name__ == "__main__":
    unittest.main()
