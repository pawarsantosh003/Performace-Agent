import os
import json
import unittest
from unittest.mock import patch

from perf_agent.ai import AIReasoningEngine
from perf_agent.config import load_config
from perf_agent.models import AgentRun, Finding, Severity


class AIRCATests(unittest.TestCase):
    @patch.dict(os.environ, {"OPENAI_API_KEY": ""}, clear=False)
    def test_fallback_ai_rca_generates_confidence_and_validation(self) -> None:
        config = load_config("examples/perf_agent_config.json")
        finding = Finding(
            title="Test API latency exceeds SLA",
            severity=Severity.HIGH,
            category="api",
            evidence=["Observed p95: 320 ms", "SLA p95: 200 ms"],
            business_impact=4,
            user_experience_impact=4,
            technical_severity=4,
            frequency=3,
            fix_confidence=4,
            implementation_effort=3,
            recommendation="Investigate backend latency.",
            validation_plan="Re-run the same scenario and verify p95 is below SLA.",
        )
        run = AgentRun(run_id="test-run", config=config, findings=[finding])
        engine = AIReasoningEngine()
        engine.enrich_findings(run, {"previous_run": {"readiness": 88}})

        self.assertGreater(run.findings[0].ai_confidence_pct, 0.0)
        self.assertTrue(run.findings[0].ai_rca_summary)
        self.assertTrue(run.findings[0].ai_recommendation)
        self.assertTrue(run.findings[0].ai_validation_plan)
        self.assertEqual(run.findings[0].ai_validation_plan, finding.validation_plan)
        self.assertTrue(run.findings[0].ai_evidence_citations)
        self.assertEqual(run.findings[0].ai_prompt_template, "api_latency_rca")

    @patch.dict(os.environ, {"OPENAI_API_KEY": "test-key", "OPENAI_MODEL": "gpt-test"}, clear=False)
    def test_openai_payload_uses_structured_output_schema(self) -> None:
        config = load_config("examples/perf_agent_config.json")
        finding = Finding(
            title="Home page LCP exceeds target",
            severity=Severity.HIGH,
            category="web",
            evidence=["Observed LCP: 3200ms", "Target LCP: 2500ms"],
            business_impact=4,
            user_experience_impact=5,
            technical_severity=4,
            frequency=4,
            fix_confidence=4,
            implementation_effort=3,
            recommendation="Optimize the LCP element.",
            validation_plan="Rerun Lighthouse and verify LCP is below target.",
        )
        run = AgentRun(run_id="test-run", config=config, findings=[finding])
        engine = AIReasoningEngine()

        payload = engine._openai_payload(run.findings, {"previous_readiness": {"score": 80}})

        self.assertEqual(payload["model"], "gpt-test")
        self.assertEqual(payload["text"]["format"]["type"], "json_schema")
        self.assertTrue(payload["text"]["format"]["strict"])
        self.assertIn("findings", payload["text"]["format"]["schema"]["required"])
        self.assertIn("web_vitals_rca", payload["input"][1]["content"])

    def test_guardrails_remove_invalid_citations_and_require_validation(self) -> None:
        config = load_config("examples/perf_agent_config.json")
        finding = Finding(
            title="Database bottleneck detected on slow query",
            severity=Severity.CRITICAL,
            category="database",
            evidence=["p95: 910 ms", "lock wait: 640 ms"],
            business_impact=4,
            user_experience_impact=4,
            technical_severity=5,
            frequency=4,
            fix_confidence=4,
            implementation_effort=3,
            recommendation="Review indexes and lock contention.",
            validation_plan="Rerun load test and compare DB p95.",
        )
        run = AgentRun(run_id="test-run", config=config, findings=[finding])
        engine = AIReasoningEngine()
        insight = {
            "title": finding.title,
            "category": "database",
            "severity": "critical",
            "rca_summary": "The query is slow and has lock contention.",
            "confidence_pct": 92,
            "ai_recommendation": "Add the missing index and shorten transactions.",
            "validation_plan": "",
            "evidence_citations": ["p95: 910 ms", "invented cache metric"],
            "critical_claims": ["The query is slow."],
            "next_telemetry": [],
        }

        guarded = engine._apply_guardrails(finding, insight, None)

        self.assertEqual(guarded["evidence_citations"], ["p95: 910 ms"])
        self.assertEqual(guarded["validation_plan"], finding.validation_plan)
        self.assertLessEqual(guarded["confidence_pct"], 55.0)
        self.assertTrue(guarded["guardrail_failures"])

    def test_parse_responses_api_output_text(self) -> None:
        engine = AIReasoningEngine()
        response = {
            "output": [
                {
                    "content": [
                        {
                            "type": "output_text",
                            "text": json.dumps(
                                {
                                    "findings": [
                                        {
                                            "title": "API p95 exceeds SLA",
                                            "category": "api",
                                            "severity": "high",
                                            "rca_summary": "p95 exceeds SLA.",
                                            "confidence_pct": 80,
                                            "ai_recommendation": "Profile traces.",
                                            "validation_plan": "Rerun same load scenario.",
                                            "evidence_citations": ["Observed p95: 320 ms"],
                                            "critical_claims": ["p95 exceeds SLA."],
                                            "next_telemetry": ["trace waterfall"],
                                        }
                                    ]
                                }
                            ),
                        }
                    ]
                }
            ]
        }

        insights = engine._parse_openai_response(json.dumps(response))

        self.assertEqual(insights[0]["category"], "api")
        self.assertEqual(insights[0]["confidence_pct"], 80)


if __name__ == "__main__":
    unittest.main()
