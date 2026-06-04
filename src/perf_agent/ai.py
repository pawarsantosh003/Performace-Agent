from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from typing import Any

from .models import AgentRun, Finding, Severity

DEFAULT_OPENAI_MODEL = "gpt-4o-mini"
OPENAI_TIMEOUT_SECONDS = 30

PROMPT_TEMPLATES: dict[str, dict[str, str]] = {
    "web": {
        "name": "web_vitals_rca",
        "instruction": (
            "Analyze Core Web Vitals evidence. Focus on LCP, INP, CLS, FCP, TTFB, render-blocking resources, "
            "server response, image/font delivery, and interaction long tasks."
        ),
    },
    "api": {
        "name": "api_latency_rca",
        "instruction": (
            "Analyze API latency, throughput, p95/p99 tail behavior, error-rate evidence, dependency timing, "
            "connection pools, retries, rate limits, payload size, and trace correlation."
        ),
    },
    "database": {
        "name": "database_bottleneck_rca",
        "instruction": (
            "Analyze database bottleneck evidence. Focus on slow query shape, rows scanned, missing indexes, "
            "lock waits, explain-plan signals, hot rows, transaction scope, and query frequency."
        ),
    },
    "infrastructure": {
        "name": "infrastructure_saturation_rca",
        "instruction": (
            "Analyze infrastructure saturation evidence. Focus on CPU, memory, disk I/O, network, autoscaling, "
            "resource limits, queueing, throttling, and error-budget burn."
        ),
    },
}

SYSTEM_INSTRUCTIONS = (
    "You are a senior performance engineering RCA assistant. Return only structured JSON that matches the schema. "
    "Use only supplied evidence and supplied historical context. Every critical claim must be supported by evidence_citations. "
    "Do not invent tools, metrics, system names, timings, queries, or owners. If evidence is insufficient, say so, lower confidence, "
    "and request the next telemetry needed. Every recommendation must include a validation step."
)

RCA_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": ["findings"],
    "properties": {
        "findings": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": [
                    "title",
                    "category",
                    "severity",
                    "rca_summary",
                    "confidence_pct",
                    "ai_recommendation",
                    "validation_plan",
                    "evidence_citations",
                    "critical_claims",
                    "next_telemetry",
                ],
                "properties": {
                    "title": {"type": "string"},
                    "category": {"type": "string"},
                    "severity": {"type": "string"},
                    "rca_summary": {"type": "string"},
                    "confidence_pct": {"type": "number", "minimum": 0, "maximum": 100},
                    "ai_recommendation": {"type": "string"},
                    "validation_plan": {"type": "string"},
                    "evidence_citations": {"type": "array", "items": {"type": "string"}},
                    "critical_claims": {"type": "array", "items": {"type": "string"}},
                    "next_telemetry": {"type": "array", "items": {"type": "string"}},
                },
            },
        }
    },
}


class AIReasoningEngine:
    def enrich_findings(self, run: AgentRun, historical_context: dict[str, Any] | None = None) -> None:
        candidates = [
            finding
            for finding in run.findings
            if finding.severity in {Severity.CRITICAL, Severity.HIGH}
        ]
        if not candidates:
            return

        historical_summary = self._summarize_historical_context(historical_context)
        insights = self._generate_insights(candidates, historical_summary)
        for finding, insight in zip(candidates, insights):
            guarded = self._apply_guardrails(finding, insight, historical_summary)
            finding.ai_rca_summary = guarded["rca_summary"]
            finding.ai_recommendation = guarded["ai_recommendation"]
            finding.ai_validation_plan = guarded["validation_plan"]
            finding.ai_confidence_pct = guarded["confidence_pct"]
            finding.ai_evidence_citations = guarded["evidence_citations"]
            finding.ai_prompt_template = guarded["prompt_template"]
            finding.ai_guardrail_failures = guarded["guardrail_failures"]
            finding.ai_structured_output = guarded["structured_output"]

    def _generate_insights(self, findings: list[Finding], historical_context: dict[str, Any] | None) -> list[dict[str, Any]]:
        if self._api_key():
            try:
                payload = self._openai_payload(findings, historical_context)
                response_text = self._openai_request(payload)
                return self._parse_openai_response(response_text)
            except Exception:
                return self._fallback_insights(findings, historical_context)
        return self._fallback_insights(findings, historical_context)

    def _openai_payload(self, findings: list[Finding], historical_context: dict[str, Any] | None) -> dict[str, Any]:
        return {
            "model": self._model(),
            "input": [
                {"role": "system", "content": SYSTEM_INSTRUCTIONS},
                {"role": "user", "content": self._build_prompt(findings, historical_context)},
            ],
            "text": {
                "format": {
                    "type": "json_schema",
                    "name": "performance_rca_response",
                    "strict": True,
                    "schema": RCA_SCHEMA,
                }
            },
            "temperature": 0.1,
            "max_output_tokens": 2200,
        }

    def _openai_request(self, payload: dict[str, Any]) -> str:
        url = self._api_base().rstrip("/") + "/v1/responses"
        data = json.dumps(payload).encode("utf-8")
        request = urllib.request.Request(url, data=data, method="POST")
        request.add_header("Content-Type", "application/json")
        request.add_header("Authorization", f"Bearer {self._api_key()}")
        request.add_header("User-Agent", "PerformanceTestingAIAgent/0.2")
        try:
            with urllib.request.urlopen(request, timeout=OPENAI_TIMEOUT_SECONDS) as response:
                return response.read().decode("utf-8")
        except urllib.error.HTTPError as exc:
            raise RuntimeError(f"OpenAI structured RCA request failed: {exc.code} {exc.reason}") from exc

    def _build_prompt(self, findings: list[Finding], historical_context: dict[str, Any] | None) -> str:
        prompt: list[str] = [
            "Generate evidence-grounded RCA for the following high-priority performance findings.",
            "Rules:",
            "- Cite exact evidence strings from evidence or historical_context in evidence_citations.",
            "- Keep confidence below 60 when evidence does not isolate a root cause.",
            "- Include one validation_plan per recommendation.",
            "- Put any telemetry still needed in next_telemetry.",
            "",
        ]
        if historical_context:
            prompt.extend(["historical_context:", json.dumps(historical_context, indent=2), ""])
        for index, finding in enumerate(findings, start=1):
            template = PROMPT_TEMPLATES.get(finding.category, _default_template())
            prompt.extend(
                [
                    f"finding_{index}:",
                    f"title: {finding.title}",
                    f"category: {finding.category}",
                    f"severity: {finding.severity.value}",
                    f"prompt_template: {template['name']}",
                    f"template_instruction: {template['instruction']}",
                    "evidence:",
                    *[f"- {line}" for line in finding.evidence],
                    f"existing_likely_cause: {finding.likely_cause or 'not provided'}",
                    f"existing_recommendation: {finding.recommendation}",
                    f"existing_validation_plan: {finding.validation_plan}",
                    "",
                ]
            )
        return "\n".join(prompt)

    def _parse_openai_response(self, response_text: str) -> list[dict[str, Any]]:
        parsed_response = json.loads(response_text)
        output_text = parsed_response.get("output_text")
        if not output_text:
            output_text = self._extract_response_text(parsed_response)
        parsed = json.loads(self._extract_json(output_text))
        if isinstance(parsed, dict):
            return list(parsed.get("findings", []))
        return list(parsed)

    def _extract_response_text(self, parsed_response: dict[str, Any]) -> str:
        chunks: list[str] = []
        for output in parsed_response.get("output", []):
            for item in output.get("content", []):
                if item.get("type") == "output_text":
                    chunks.append(str(item.get("text", "")))
                if item.get("type") == "refusal":
                    raise RuntimeError(f"OpenAI refused RCA generation: {item.get('refusal', '')}")
        if not chunks:
            raise ValueError("OpenAI response did not include output_text")
        return "\n".join(chunks)

    def _extract_json(self, text: str) -> str:
        text = text.strip()
        if text.startswith("{") or text.startswith("["):
            return text
        start = text.find("{")
        end = text.rfind("}")
        if start >= 0 and end >= 0:
            return text[start : end + 1]
        start = text.find("[")
        end = text.rfind("]")
        if start >= 0 and end >= 0:
            return text[start : end + 1]
        raise ValueError("Unable to extract JSON from structured RCA response")

    def _fallback_insights(self, findings: list[Finding], historical_context: dict[str, Any] | None = None) -> list[dict[str, Any]]:
        return [self._fallback_insight(finding, historical_context) for finding in findings]

    def _fallback_insight(self, finding: Finding, historical_context: dict[str, Any] | None) -> dict[str, Any]:
        template = PROMPT_TEMPLATES.get(finding.category, _default_template())
        citations = [line for line in finding.evidence if line]
        historical_note = _historical_note(historical_context)
        confidence = _fallback_confidence(finding, citations, historical_context)
        return {
            "title": finding.title,
            "category": finding.category,
            "severity": finding.severity.value,
            "rca_summary": self._fallback_rca(finding, historical_note),
            "confidence_pct": confidence,
            "ai_recommendation": finding.recommendation,
            "validation_plan": finding.validation_plan,
            "evidence_citations": citations,
            "critical_claims": [finding.likely_cause] if finding.likely_cause else [],
            "next_telemetry": _next_telemetry(finding),
            "prompt_template": template["name"],
        }

    def _fallback_rca(self, finding: Finding, historical_note: str) -> str:
        suffix = f" {historical_note}" if historical_note else ""
        if finding.category == "web":
            return "Core Web Vitals are outside target based on the supplied page metric evidence; the exact render-path cause needs browser trace confirmation." + suffix
        if finding.category == "api":
            return "API SLA evidence shows latency or errors above threshold; traces are needed to isolate whether backend, database, dependency, or queueing time dominates." + suffix
        if finding.category == "database":
            return "Database evidence shows slow execution or lock waits that can cascade into API latency under load." + suffix
        if finding.category == "infrastructure":
            return "Infrastructure utilization evidence indicates saturation risk that can drive queueing, latency, and instability." + suffix
        return "The issue is supported by collected performance evidence, but deeper telemetry is needed to isolate root cause." + suffix

    def _apply_guardrails(self, finding: Finding, insight: dict[str, Any], historical_context: dict[str, Any] | None) -> dict[str, Any]:
        template = PROMPT_TEMPLATES.get(finding.category, _default_template())
        allowed_citations = _allowed_citations(finding, historical_context)
        citations = [str(item) for item in insight.get("evidence_citations", []) if str(item) in allowed_citations]
        failures: list[str] = []
        if insight.get("evidence_citations") and len(citations) != len(insight.get("evidence_citations", [])):
            failures.append("Removed citation(s) not present in supplied evidence or historical context.")
        if not citations:
            failures.append("No valid evidence citation supplied; fallback citations were attached and confidence was reduced.")
            citations = list(finding.evidence)

        validation = str(insight.get("validation_plan") or "").strip() or finding.validation_plan
        if not validation:
            failures.append("Recommendation had no validation step; generated validation fallback.")
            validation = "Rerun the same scenario and compare the affected metric against the configured threshold."

        recommendation = str(insight.get("ai_recommendation") or "").strip() or finding.recommendation
        if recommendation and not validation:
            failures.append("Recommendation blocked because validation step is missing.")

        rca_summary = str(insight.get("rca_summary") or "").strip() or self._fallback_rca(finding, _historical_note(historical_context))
        confidence = _bounded_confidence(insight.get("confidence_pct", 0.0))
        if failures:
            confidence = min(confidence or 55.0, 55.0)
        if finding.severity == Severity.CRITICAL and not citations:
            confidence = min(confidence, 40.0)

        structured_output = {
            "title": insight.get("title", finding.title),
            "category": insight.get("category", finding.category),
            "severity": insight.get("severity", finding.severity.value),
            "rca_summary": rca_summary,
            "confidence_pct": confidence,
            "ai_recommendation": recommendation,
            "validation_plan": validation,
            "evidence_citations": citations,
            "critical_claims": list(insight.get("critical_claims", [])),
            "next_telemetry": list(insight.get("next_telemetry", _next_telemetry(finding))),
            "prompt_template": str(insight.get("prompt_template") or template["name"]),
        }
        return {
            **structured_output,
            "prompt_template": structured_output["prompt_template"],
            "guardrail_failures": failures,
            "structured_output": structured_output,
        }

    def _summarize_historical_context(self, historical_context: dict[str, Any] | None) -> dict[str, Any] | None:
        if not historical_context:
            return None
        summary: dict[str, Any] = {}
        for key in ["application_name", "release_id", "environment", "run_id", "created_at"]:
            if key in historical_context:
                summary[key] = historical_context[key]
        readiness = historical_context.get("readiness")
        if readiness:
            summary["previous_readiness"] = readiness
        failing = historical_context.get("failing_endpoints")
        if failing:
            summary["previous_failing_endpoints"] = failing[:10]
        scenarios = historical_context.get("scenarios") or []
        summary["previous_metric_snapshot"] = _summarize_scenarios(scenarios)
        return summary

    def _api_key(self) -> str:
        return os.environ.get("OPENAI_API_KEY", "")

    def _model(self) -> str:
        return os.environ.get("OPENAI_MODEL", DEFAULT_OPENAI_MODEL)

    def _api_base(self) -> str:
        return os.environ.get("OPENAI_API_BASE", "https://api.openai.com")


def _default_template() -> dict[str, str]:
    return {
        "name": "generic_performance_rca",
        "instruction": "Analyze supplied performance evidence and generate evidence-backed RCA and validation guidance.",
    }


def _allowed_citations(finding: Finding, historical_context: dict[str, Any] | None) -> set[str]:
    allowed = {str(item) for item in finding.evidence}
    if historical_context:
        allowed.update(_flatten_strings(historical_context))
    return allowed


def _flatten_strings(value: Any) -> set[str]:
    if isinstance(value, str):
        return {value}
    if isinstance(value, dict):
        values: set[str] = set()
        for item in value.values():
            values.update(_flatten_strings(item))
        return values
    if isinstance(value, list):
        values: set[str] = set()
        for item in value:
            values.update(_flatten_strings(item))
        return values
    if isinstance(value, (int, float, bool)):
        return {str(value)}
    return set()


def _bounded_confidence(value: Any) -> float:
    try:
        return round(max(0.0, min(100.0, float(value))), 1)
    except (TypeError, ValueError):
        return 0.0


def _fallback_confidence(finding: Finding, citations: list[str], historical_context: dict[str, Any] | None) -> float:
    base = 70.0
    if finding.severity == Severity.CRITICAL:
        base += 8
    if len(citations) >= 3:
        base += 7
    if historical_context:
        base += 5
    return min(90.0, base)


def _historical_note(historical_context: dict[str, Any] | None) -> str:
    if not historical_context:
        return ""
    readiness = historical_context.get("previous_readiness") or historical_context.get("readiness")
    if isinstance(readiness, dict) and readiness.get("score") is not None:
        return f"Previous baseline readiness was {readiness.get('status', 'unknown')} at {readiness.get('score')}/100."
    return "Historical baseline context was supplied and should be compared during validation."


def _next_telemetry(finding: Finding) -> list[str]:
    if finding.category == "web":
        return ["Lighthouse trace", "Chrome DevTools performance trace", "LCP element details"]
    if finding.category == "api":
        return ["OpenTelemetry traces for p95 and p99 requests", "error logs grouped by status and exception"]
    if finding.category == "database":
        return ["EXPLAIN or actual execution plan", "index usage statistics", "lock wait samples"]
    if finding.category == "infrastructure":
        return ["Prometheus CPU/memory/disk/network series for the test window", "autoscaling and throttling events"]
    return ["Additional telemetry linked to the affected scenario"]


def _summarize_scenarios(scenarios: list[dict[str, Any]]) -> list[dict[str, Any]]:
    summary: list[dict[str, Any]] = []
    for scenario in scenarios[:5]:
        item: dict[str, Any] = {
            "scenario_name": scenario.get("scenario_name"),
            "test_type": scenario.get("test_type"),
        }
        endpoints = scenario.get("endpoint_results") or []
        if endpoints:
            item["endpoints"] = [
                {
                    "name": endpoint.get("name"),
                    "p95_ms": endpoint.get("p95_ms"),
                    "p99_ms": endpoint.get("p99_ms"),
                    "error_rate_pct": endpoint.get("error_rate_pct"),
                }
                for endpoint in endpoints[:5]
            ]
        pages = scenario.get("web_vitals_results") or []
        if pages:
            item["pages"] = [
                {
                    "page_name": page.get("page_name"),
                    "lcp_p75_ms": page.get("lcp_p75_ms"),
                    "inp_p75_ms": page.get("inp_p75_ms"),
                    "cls_p75": page.get("cls_p75"),
                    "source": page.get("source"),
                }
                for page in pages[:5]
            ]
        if item.get("endpoints") or item.get("pages"):
            summary.append(item)
    return summary
