from __future__ import annotations

from .models import (
    AgentConfig,
    Endpoint,
    EndpointResult,
    Finding,
    ReadinessScore,
    ReadinessStatus,
    ScenarioResult,
    Severity,
)


class PerformanceAnalyzer:
    def analyze(self, config: AgentConfig, results: list[ScenarioResult]) -> list[Finding]:
        findings: list[Finding] = []
        endpoint_map = {
            endpoint.name: endpoint
            for scenario in config.scenarios
            for endpoint in scenario.endpoints
        }
        for result in results:
            for endpoint_result in result.endpoint_results:
                endpoint = endpoint_map.get(endpoint_result.name)
                if endpoint:
                    findings.extend(self._analyze_endpoint(endpoint, endpoint_result, result))
            for web_result in result.web_vitals_results:
                findings.extend(self._analyze_web_vitals(config, web_result, result))
            if result.infra_metrics:
                findings.extend(self._analyze_infra(result))
            for db_input in result.database_inputs:
                findings.extend(self._analyze_database(db_input))

        for finding in findings:
            finding.score = round(_score_finding(finding), 2)
        return sorted(findings, key=lambda item: item.score, reverse=True)

    def readiness(self, config: AgentConfig, results: list[ScenarioResult], findings: list[Finding]) -> ReadinessScore:
        dimensions = {
            "critical_journey_sla": _dimension_score([f for f in findings if f.category == "api"]),
            "error_rate_availability": _dimension_score([f for f in findings if "error rate" in f.title.lower()]),
            "core_web_vitals": _dimension_score([f for f in findings if f.category == "web"]),
            "database_health": _dimension_score([f for f in findings if f.category == "database"]),
            "infrastructure_headroom": _dimension_score([f for f in findings if f.category == "infrastructure"]),
            "endurance_stability": _dimension_score([f for f in findings if "endurance" in f.title.lower()]),
            "baseline_regression": 100.0,
        }
        weights = {
            "critical_journey_sla": 0.25,
            "error_rate_availability": 0.15,
            "core_web_vitals": 0.15,
            "database_health": 0.15,
            "infrastructure_headroom": 0.10,
            "endurance_stability": 0.10,
            "baseline_regression": 0.10,
        }
        score = round(sum(dimensions[key] * weight for key, weight in weights.items()), 2)
        blockers = [f.title for f in findings if f.severity == Severity.CRITICAL]
        status = _status(score, blockers)
        return ReadinessScore(score=score, status=status, dimensions=dimensions, blockers=blockers)

    def _analyze_endpoint(self, endpoint: Endpoint, result: EndpointResult, scenario: ScenarioResult) -> list[Finding]:
        findings: list[Finding] = []
        if result.p95_ms > endpoint.sla.p95_ms:
            findings.append(
                Finding(
                    title=f"{endpoint.name} p95 latency exceeds SLA",
                    severity=_severity_ratio(result.p95_ms / endpoint.sla.p95_ms),
                    category="api",
                    evidence=[
                        f"Scenario: {scenario.scenario_name}",
                        f"Observed p95: {result.p95_ms} ms",
                        f"SLA p95: {endpoint.sla.p95_ms} ms",
                    ],
                    business_impact=endpoint.business_criticality,
                    user_experience_impact=4,
                    technical_severity=min(5, int(result.p95_ms / endpoint.sla.p95_ms * 2)),
                    frequency=4,
                    fix_confidence=4,
                    implementation_effort=3,
                    recommendation="Profile the endpoint trace, review downstream dependency timings, add caching or query optimization where evidence points to repeated expensive work.",
                    validation_plan="Rerun the same scenario and confirm p95 latency is below the configured SLA with no increase in error rate.",
                    likely_cause="The request path is doing too much work under load, often because of slow backend processing, uncached repeated reads, large payloads, slow third-party calls, or database queries on the critical path.",
                    solution_steps=[
                        "Inspect traces for this endpoint and sort spans by duration.",
                        "Check whether database, cache, or external service spans dominate the request time.",
                        "Add caching for repeated read-heavy data where correctness allows it.",
                        "Reduce payload size and avoid returning unused fields.",
                        "Move noncritical work such as notifications, audit enrichment, or analytics to asynchronous processing.",
                        "Tune connection pools and timeouts if queueing is visible in traces.",
                    ],
                    owner_actions=[
                        "Backend engineer: profile the endpoint and identify the slowest dependency.",
                        "Database owner: review query plans for the endpoint's critical queries.",
                        "SRE/DevOps: check service CPU, memory, thread pool, and connection pool saturation during the run.",
                    ],
                    documentation_links=[
                        "https://web.dev/articles/optimize-ttfb",
                        "https://opentelemetry.io/docs/concepts/signals/traces/",
                    ],
                )
            )
        if result.p99_ms > endpoint.sla.p99_ms:
            findings.append(
                Finding(
                    title=f"{endpoint.name} p99 tail latency exceeds SLA",
                    severity=_severity_ratio(result.p99_ms / endpoint.sla.p99_ms),
                    category="api",
                    evidence=[
                        f"Scenario: {scenario.scenario_name}",
                        f"Observed p99: {result.p99_ms} ms",
                        f"SLA p99: {endpoint.sla.p99_ms} ms",
                    ],
                    business_impact=endpoint.business_criticality,
                    user_experience_impact=4,
                    technical_severity=4,
                    frequency=3,
                    fix_confidence=3,
                    implementation_effort=3,
                    recommendation="Investigate tail latency using distributed traces, dependency timeouts, connection pool saturation, and queueing delays.",
                    validation_plan="Compare p99 before and after remediation using identical load and data conditions.",
                    likely_cause="A small subset of requests is much slower than the rest, usually from dependency timeouts, uneven cache hits, lock contention, garbage collection pauses, cold starts, or saturated worker pools.",
                    solution_steps=[
                        "Compare fast and slow traces for the same endpoint.",
                        "Look for retries, timeout waits, lock waits, or queueing spans in p99 requests.",
                        "Add timeout budgets and circuit breakers for unreliable dependencies.",
                        "Warm critical caches before peak traffic where appropriate.",
                        "Increase worker or connection pool capacity only after confirming saturation.",
                    ],
                    owner_actions=[
                        "Backend engineer: group p99 traces by slow dependency.",
                        "SRE/DevOps: check autoscaling lag, pod restarts, and runtime GC pauses.",
                    ],
                    documentation_links=[
                        "https://opentelemetry.io/docs/concepts/signals/traces/",
                    ],
                )
            )
        if result.error_rate_pct > endpoint.sla.error_rate_pct:
            findings.append(
                Finding(
                    title=f"{endpoint.name} error rate exceeds threshold",
                    severity=Severity.CRITICAL if result.error_rate_pct > endpoint.sla.error_rate_pct * 3 else Severity.HIGH,
                    category="api",
                    evidence=[
                        f"Scenario: {scenario.scenario_name}",
                        f"Observed error rate: {result.error_rate_pct}%",
                        f"Allowed error rate: {endpoint.sla.error_rate_pct}%",
                    ],
                    business_impact=endpoint.business_criticality,
                    user_experience_impact=5,
                    technical_severity=5,
                    frequency=4,
                    fix_confidence=4,
                    implementation_effort=3,
                    recommendation="Inspect failed request traces and logs, group by status code and exception, and fix the highest-volume failure mode before release.",
                    validation_plan="Rerun load test and confirm error rate remains below threshold for the full duration.",
                    likely_cause="The application is returning failures under load, often from timeouts, unhandled exceptions, rate limits, exhausted connection pools, or overloaded dependencies.",
                    solution_steps=[
                        "Group failures by HTTP status code and exception type.",
                        "Inspect logs for the top failing request path during the exact test window.",
                        "Check dependency timeout, retry, and rate-limit behavior.",
                        "Add graceful fallback or backpressure where downstream systems are saturated.",
                        "Fix the highest-volume error first and rerun the same scenario.",
                    ],
                    owner_actions=[
                        "Backend engineer: fix the top exception or failed status path.",
                        "SRE/DevOps: confirm no WAF, CDN, gateway, or rate-limit rule is rejecting test traffic.",
                    ],
                    documentation_links=[
                        "https://opentelemetry.io/docs/concepts/signals/logs/",
                    ],
                )
            )
        return findings

    def _analyze_web_vitals(self, config: AgentConfig, result, scenario: ScenarioResult) -> list[Finding]:
        thresholds = config.web_vitals
        checks = [
            ("LCP", result.lcp_p75_ms, thresholds.lcp_p75_ms, "ms", _web_guidance("LCP")),
            ("INP", result.inp_p75_ms, thresholds.inp_p75_ms, "ms", _web_guidance("INP")),
            ("CLS", result.cls_p75, thresholds.cls_p75, "", _web_guidance("CLS")),
            ("FCP", result.fcp_p75_ms, thresholds.fcp_p75_ms, "ms", _web_guidance("FCP")),
            ("TTFB", result.ttfb_p75_ms, thresholds.ttfb_p75_ms, "ms", _web_guidance("TTFB")),
        ]
        findings: list[Finding] = []
        for name, observed, threshold, unit, guidance in checks:
            if observed > threshold:
                ratio = observed / threshold
                findings.append(
                    Finding(
                        title=f"{result.page_name} {name} exceeds target",
                        severity=_severity_ratio(ratio),
                        category="web",
                        evidence=[
                            f"Scenario: {scenario.scenario_name}",
                            f"Observed {name}: {observed}{unit}",
                            f"Target {name}: {threshold}{unit}",
                            f"Source: {result.source}",
                        ],
                        business_impact=4,
                        user_experience_impact=5 if name in {"LCP", "INP", "CLS"} else 3,
                        technical_severity=min(5, int(ratio * 2)),
                        frequency=4,
                        fix_confidence=4,
                        implementation_effort=3,
                        recommendation=guidance["recommendation"],
                        validation_plan=f"Rerun browser audit and verify p75 {name} is within target on the same device and network profile.",
                        likely_cause=guidance["likely_cause"],
                        solution_steps=guidance["solution_steps"],
                        owner_actions=guidance["owner_actions"],
                        documentation_links=guidance["documentation_links"],
                    )
                )
        return findings

    def _analyze_infra(self, result: ScenarioResult) -> list[Finding]:
        metrics = result.infra_metrics
        assert metrics is not None
        checks = [
            ("CPU", metrics.cpu_pct),
            ("memory", metrics.memory_pct),
            ("disk I/O", metrics.disk_io_pct),
            ("network", metrics.network_pct),
        ]
        findings: list[Finding] = []
        for name, value in checks:
            if value >= 80:
                findings.append(
                    Finding(
                        title=f"Infrastructure {name} utilization is near saturation",
                        severity=Severity.CRITICAL if value >= 92 else Severity.HIGH,
                        category="infrastructure",
                        evidence=[f"Scenario: {result.scenario_name}", f"Observed {name}: {value}%"],
                        business_impact=4,
                        user_experience_impact=4,
                        technical_severity=5 if value >= 92 else 4,
                        frequency=4,
                        fix_confidence=4,
                        implementation_effort=3,
                        recommendation="Review service capacity, autoscaling signals, resource requests/limits, and workload distribution before release.",
                        validation_plan="Rerun target load and confirm sustained utilization remains below the saturation threshold with acceptable latency.",
                        likely_cause=f"{name} is close to saturation during the scenario, which can create queueing delays and unstable latency under additional load.",
                        solution_steps=[
                            "Correlate utilization with latency and error spikes during the same time window.",
                            "Check whether autoscaling triggered early enough and whether new capacity became healthy in time.",
                            "Review container or VM CPU/memory requests, limits, and throttling.",
                            "Tune scaling signals or add baseline capacity for predictable launch traffic.",
                            "Reduce hot spots by balancing traffic or optimizing the highest-cost code path.",
                        ],
                        owner_actions=[
                            "SRE/DevOps: inspect infrastructure dashboards for saturation and autoscaling behavior.",
                            "Backend engineer: reduce resource-heavy operations on hot paths.",
                        ],
                        documentation_links=[
                            "https://prometheus.io/docs/introduction/overview/",
                            "https://grafana.com/docs/grafana/latest/alerting/",
                        ],
                    )
                )
        return findings

    def _analyze_database(self, item) -> list[Finding]:
        findings: list[Finding] = []
        if item.p95_ms >= 500 or item.lock_wait_ms >= 100:
            severity = Severity.CRITICAL if item.p95_ms >= 1500 or item.lock_wait_ms >= 500 else Severity.HIGH
            hint = item.recommendation_hint or "Review the execution plan, index coverage, lock waits, rows scanned, and query shape."
            findings.append(
                Finding(
                    title="Database bottleneck detected on slow query",
                    severity=severity,
                    category="database",
                    evidence=[
                        f"Query: {item.query[:240]}",
                        f"p95: {item.p95_ms} ms",
                        f"avg: {item.avg_ms} ms",
                        f"calls: {item.calls}",
                        f"lock wait: {item.lock_wait_ms} ms",
                    ],
                    business_impact=4,
                    user_experience_impact=4,
                    technical_severity=5 if severity == Severity.CRITICAL else 4,
                    frequency=5 if item.calls > 1000 else 3,
                    fix_confidence=4,
                    implementation_effort=3,
                    recommendation=hint,
                    validation_plan="Capture the query plan before and after remediation, then rerun the load scenario and compare p95 and total DB time.",
                    likely_cause="A database operation is consuming significant time or waiting on locks, which can slow dependent APIs and create cascading latency.",
                    solution_steps=[
                        "Run EXPLAIN or the platform equivalent for the query fingerprint.",
                        "Check index coverage, rows scanned, sort operations, temp files, and lock waits.",
                        "Add or adjust indexes only after validating write overhead and cardinality.",
                        "Shorten transactions and avoid holding locks while doing external work.",
                        "Verify connection pool sizing so application threads do not queue behind DB connections.",
                    ],
                    owner_actions=[
                        "Database owner: review the query plan and index strategy.",
                        "Backend engineer: reduce query frequency, projection size, and transaction scope.",
                    ],
                    documentation_links=[
                        "https://www.postgresql.org/docs/current/using-explain.html",
                        "https://dev.mysql.com/doc/refman/8.0/en/using-explain.html",
                        "https://learn.microsoft.com/en-us/sql/relational-databases/performance/display-an-actual-execution-plan",
                    ],
                )
            )
        return findings


def _score_finding(finding: Finding) -> float:
    effort_inverse = 6 - max(1, min(5, finding.implementation_effort))
    return (
        finding.business_impact * 20 * 0.30
        + finding.user_experience_impact * 20 * 0.25
        + finding.technical_severity * 20 * 0.25
        + finding.frequency * 20 * 0.10
        + finding.fix_confidence * 20 * 0.05
        + effort_inverse * 20 * 0.05
    )


def _dimension_score(findings: list[Finding]) -> float:
    if not findings:
        return 100.0
    penalty = 0.0
    for finding in findings:
        penalty += {
            Severity.CRITICAL: 45,
            Severity.HIGH: 28,
            Severity.MEDIUM: 15,
            Severity.LOW: 7,
            Severity.INFO: 2,
        }[finding.severity]
    return max(0.0, 100.0 - penalty)


def _severity_ratio(ratio: float) -> Severity:
    if ratio >= 2.0:
        return Severity.CRITICAL
    if ratio >= 1.35:
        return Severity.HIGH
    if ratio >= 1.1:
        return Severity.MEDIUM
    return Severity.LOW


def _status(score: float, blockers: list[str]) -> ReadinessStatus:
    if blockers or score < 60:
        return ReadinessStatus.BLOCKED if blockers else ReadinessStatus.RED
    if score < 75:
        return ReadinessStatus.RED
    if score < 90:
        return ReadinessStatus.AMBER
    return ReadinessStatus.GREEN


def _web_guidance(metric: str) -> dict[str, list[str] | str]:
    guidance = {
        "LCP": {
            "recommendation": "Improve the largest visible content render by optimizing hero media, server response, critical CSS, and render-blocking JavaScript.",
            "likely_cause": "The largest above-the-fold element is arriving or rendering too late. Common causes are large hero images, slow TTFB, render-blocking CSS/JS, unoptimized fonts, or lazy-loading the LCP image.",
            "solution_steps": [
                "Identify the LCP element in Lighthouse or Chrome DevTools Performance panel.",
                "If it is an image, compress it, serve WebP/AVIF, size it correctly, and preload it.",
                "Do not lazy-load the hero/LCP image.",
                "Inline or prioritize critical CSS needed for above-the-fold content.",
                "Defer or split noncritical JavaScript that blocks initial rendering.",
                "Use CDN caching and verify cache headers for static assets.",
                "Reduce TTFB if backend response time is a major contributor.",
            ],
            "owner_actions": [
                "Frontend engineer: optimize the LCP element and render-blocking resources.",
                "Backend/SRE: verify TTFB, CDN, and cache behavior.",
                "QA: rerun the same URL with Lighthouse after changes.",
            ],
            "documentation_links": [
                "https://web.dev/articles/optimize-lcp",
                "https://web.dev/articles/lcp",
            ],
        },
        "INP": {
            "recommendation": "Reduce interaction latency by breaking up long JavaScript tasks, optimizing event handlers, and deferring noncritical work.",
            "likely_cause": "The main thread is busy when users interact. Common causes are large JavaScript bundles, expensive click/input handlers, hydration work, layout thrashing, or third-party scripts.",
            "solution_steps": [
                "Record an interaction in Chrome DevTools and find long tasks above 50 ms.",
                "Split large JavaScript tasks with scheduler/yielding or smaller chunks.",
                "Debounce expensive input handlers and avoid synchronous heavy work on click/change events.",
                "Reduce unused JavaScript and defer third-party scripts.",
                "Avoid forced synchronous layouts by batching DOM reads and writes.",
                "Move CPU-heavy work to a Web Worker where feasible.",
            ],
            "owner_actions": [
                "Frontend engineer: profile long tasks and optimize event handlers.",
                "Product/analytics owner: review third-party scripts loaded on the page.",
            ],
            "documentation_links": [
                "https://web.dev/articles/inp",
                "https://web.dev/articles/optimize-inp",
            ],
        },
        "CLS": {
            "recommendation": "Stabilize layout by reserving space for images/ads/embeds, avoiding late content insertion, and controlling font swaps.",
            "likely_cause": "Elements are moving after initial render because dimensions are missing, dynamic content is inserted above existing content, or fonts swap without stable sizing.",
            "solution_steps": [
                "Add width and height or aspect-ratio to images, videos, and embeds.",
                "Reserve fixed space for ads, banners, and dynamic widgets.",
                "Avoid injecting content above existing visible content after load.",
                "Use font-display and fallback font metrics to reduce font shift.",
                "Animate with transform/opacity instead of layout-changing properties.",
            ],
            "owner_actions": [
                "Frontend engineer: reserve dimensions for shifting elements.",
                "Marketing/ads owner: ensure dynamic placements have fixed slots.",
            ],
            "documentation_links": [
                "https://web.dev/articles/cls",
                "https://web.dev/articles/optimize-cls",
            ],
        },
        "FCP": {
            "recommendation": "Improve first paint by reducing TTFB, prioritizing critical CSS, and removing render-blocking resources.",
            "likely_cause": "The browser cannot paint early because the document, critical CSS, fonts, or render path is delayed.",
            "solution_steps": [
                "Reduce server response time and redirects.",
                "Inline critical CSS for above-the-fold content.",
                "Defer noncritical CSS and JavaScript.",
                "Preconnect to required origins for fonts or critical assets.",
                "Reduce initial HTML and CSS payload size.",
            ],
            "owner_actions": [
                "Frontend engineer: optimize critical render path.",
                "SRE/Backend: reduce TTFB and redirect chains.",
            ],
            "documentation_links": [
                "https://web.dev/articles/fcp",
                "https://web.dev/articles/critical-rendering-path/render-blocking-css",
            ],
        },
        "TTFB": {
            "recommendation": "Reduce server response delay with caching, backend profiling, CDN optimization, and fewer redirects.",
            "likely_cause": "The server or edge layer is slow to return the first byte. Common causes are uncached dynamic rendering, slow database calls, cold starts, redirects, or distant origin routing.",
            "solution_steps": [
                "Check redirect chains and remove unnecessary redirects.",
                "Verify CDN/page cache hit ratio and cache-control headers.",
                "Profile backend request handling and database calls.",
                "Use server-side caching for expensive dynamic content.",
                "Move static assets to CDN and enable compression.",
                "Review origin region and edge routing for the target users.",
            ],
            "owner_actions": [
                "Backend engineer: profile server-side processing.",
                "SRE/CDN owner: validate edge cache and routing behavior.",
            ],
            "documentation_links": [
                "https://web.dev/articles/ttfb",
                "https://web.dev/articles/optimize-ttfb",
            ],
        },
    }
    return guidance[metric]
