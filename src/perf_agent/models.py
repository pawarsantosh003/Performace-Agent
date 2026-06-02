from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any


class TestType(str, Enum):
    SMOKE = "smoke"
    LOAD = "load"
    STRESS = "stress"
    SPIKE = "spike"
    ENDURANCE = "endurance"


class Severity(str, Enum):
    CRITICAL = "critical"
    HIGH = "high"
    MEDIUM = "medium"
    LOW = "low"
    INFO = "info"


class ReadinessStatus(str, Enum):
    GREEN = "green"
    AMBER = "amber"
    RED = "red"
    BLOCKED = "blocked"


@dataclass(frozen=True)
class WebVitalThresholds:
    lcp_p75_ms: int = 2500
    inp_p75_ms: int = 200
    cls_p75: float = 0.1
    fcp_p75_ms: int = 1800
    ttfb_p75_ms: int = 800


@dataclass(frozen=True)
class ApiSla:
    p95_ms: int
    p99_ms: int
    error_rate_pct: float
    throughput_rps: float


@dataclass(frozen=True)
class Workload:
    concurrent_users: int
    duration_seconds: int
    ramp_up_seconds: int
    target_tps: float


@dataclass(frozen=True)
class Endpoint:
    name: str
    method: str
    url: str
    sla: ApiSla
    business_criticality: int = 3


@dataclass(frozen=True)
class PageTarget:
    name: str
    url: str
    business_criticality: int = 3


@dataclass(frozen=True)
class Scenario:
    name: str
    test_type: TestType
    workload: Workload
    endpoints: list[Endpoint] = field(default_factory=list)
    pages: list[PageTarget] = field(default_factory=list)
    requires_approval: bool = False


@dataclass(frozen=True)
class Environment:
    name: str
    base_url: str
    allow_risky_tests: bool = False
    max_concurrent_users: int = 1000
    max_duration_seconds: int = 7200


@dataclass(frozen=True)
class AgentConfig:
    application_name: str
    release_id: str
    environment: Environment
    scenarios: list[Scenario]
    web_vitals: WebVitalThresholds = field(default_factory=WebVitalThresholds)
    monitoring_metrics_file: str | None = None
    database_metrics_file: str | None = None
    previous_baseline_file: str | None = None


@dataclass
class EndpointResult:
    name: str
    method: str
    url: str
    p50_ms: float
    p95_ms: float
    p99_ms: float
    throughput_rps: float
    error_rate_pct: float
    sample_count: int


@dataclass
class WebVitalsResult:
    page_name: str
    url: str
    lcp_p75_ms: float
    inp_p75_ms: float
    cls_p75: float
    fcp_p75_ms: float
    ttfb_p75_ms: float
    source: str = "synthetic"


@dataclass
class InfraMetrics:
    cpu_pct: float
    memory_pct: float
    disk_io_pct: float
    network_pct: float
    error_budget_burn_pct: float = 0.0


@dataclass
class DatabaseFindingInput:
    query: str
    avg_ms: float
    p95_ms: float
    calls: int
    rows_examined: int | None = None
    lock_wait_ms: float = 0.0
    recommendation_hint: str | None = None


@dataclass
class ScenarioResult:
    scenario_name: str
    test_type: TestType
    endpoint_results: list[EndpointResult] = field(default_factory=list)
    web_vitals_results: list[WebVitalsResult] = field(default_factory=list)
    infra_metrics: InfraMetrics | None = None
    database_inputs: list[DatabaseFindingInput] = field(default_factory=list)
    raw: dict[str, Any] = field(default_factory=dict)


@dataclass
class Finding:
    title: str
    severity: Severity
    category: str
    evidence: list[str]
    business_impact: int
    user_experience_impact: int
    technical_severity: int
    frequency: int
    fix_confidence: int
    implementation_effort: int
    recommendation: str
    validation_plan: str
    likely_cause: str = ""
    solution_steps: list[str] = field(default_factory=list)
    owner_actions: list[str] = field(default_factory=list)
    documentation_links: list[str] = field(default_factory=list)
    score: float = 0.0


@dataclass
class ReadinessScore:
    score: float
    status: ReadinessStatus
    dimensions: dict[str, float]
    blockers: list[str]


@dataclass
class AgentRun:
    run_id: str
    config: AgentConfig
    scenario_results: list[ScenarioResult] = field(default_factory=list)
    findings: list[Finding] = field(default_factory=list)
    readiness: ReadinessScore | None = None
    artifacts: dict[str, str] = field(default_factory=dict)
