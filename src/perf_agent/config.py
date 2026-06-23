from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from .governance import require_secret_reference, resolve_secret_references
from .models import (
    AgentConfig,
    ApiSla,
    DatabaseConnector,
    Endpoint,
    Environment,
    MonitoringConnector,
    PageTarget,
    Scenario,
    TestEngine,
    TestType,
    WebVitalThresholds,
    Workload,
)


class ConfigError(ValueError):
    pass


def load_config(path: str | Path) -> AgentConfig:
    config_path = Path(path)
    try:
        raw = json.loads(config_path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise ConfigError(f"Config file not found: {config_path}") from exc
    except json.JSONDecodeError as exc:
        raise ConfigError(f"Invalid JSON config: {config_path}: {exc}") from exc

    return parse_config(raw, base_dir=config_path.parent)


def parse_config(raw: dict[str, Any], base_dir: Path | None = None) -> AgentConfig:
    required = ["application_name", "release_id", "environment", "scenarios"]
    missing = [key for key in required if key not in raw]
    if missing:
        raise ConfigError(f"Missing config keys: {', '.join(missing)}")

    env_raw = raw["environment"]
    environment = Environment(
        name=_required(env_raw, "name"),
        base_url=_required(env_raw, "base_url"),
        allow_risky_tests=bool(env_raw.get("allow_risky_tests", False)),
        max_concurrent_users=int(env_raw.get("max_concurrent_users", 1000)),
        max_duration_seconds=int(env_raw.get("max_duration_seconds", 7200)),
        max_target_tps=_optional_float(env_raw.get("max_target_tps")),
        allowed_hosts=[str(item).lower() for item in env_raw.get("allowed_hosts", []) if item],
        allowed_url_prefixes=[str(item) for item in env_raw.get("allowed_url_prefixes", []) if item],
        test_window_start=_optional_str(env_raw.get("test_window_start")),
        test_window_end=_optional_str(env_raw.get("test_window_end")),
    )

    web_raw = raw.get("web_vitals", {})
    web_vitals = WebVitalThresholds(
        lcp_p75_ms=int(web_raw.get("lcp_p75_ms", 2500)),
        inp_p75_ms=int(web_raw.get("inp_p75_ms", 200)),
        cls_p75=float(web_raw.get("cls_p75", 0.1)),
        fcp_p75_ms=int(web_raw.get("fcp_p75_ms", 1800)),
        ttfb_p75_ms=int(web_raw.get("ttfb_p75_ms", 800)),
    )

    scenarios = [_parse_scenario(item) for item in raw["scenarios"]]
    return AgentConfig(
        application_name=str(raw["application_name"]),
        release_id=str(raw["release_id"]),
        environment=environment,
        scenarios=scenarios,
        web_vitals=web_vitals,
        monitoring_metrics_file=_optional_path(raw.get("monitoring_metrics_file"), base_dir),
        database_metrics_file=_optional_path(raw.get("database_metrics_file"), base_dir),
        monitoring_connectors=[_parse_monitoring_connector(item) for item in raw.get("monitoring_connectors", [])],
        database_connectors=[_parse_database_connector(item, base_dir) for item in raw.get("database_connectors", [])],
        previous_baseline_file=_optional_path(raw.get("previous_baseline_file"), base_dir),
        test_engine=TestEngine(str(raw.get("test_engine", "synthetic")).lower()),
    )


def _parse_scenario(raw: dict[str, Any]) -> Scenario:
    workload_raw = _required(raw, "workload")
    workload = Workload(
        concurrent_users=int(_required(workload_raw, "concurrent_users")),
        duration_seconds=int(_required(workload_raw, "duration_seconds")),
        ramp_up_seconds=int(workload_raw.get("ramp_up_seconds", 60)),
        target_tps=float(_required(workload_raw, "target_tps")),
    )
    test_type = TestType(str(_required(raw, "test_type")).lower())
    requires_approval = bool(raw.get("requires_approval", test_type in {TestType.STRESS, TestType.SPIKE, TestType.ENDURANCE}))

    return Scenario(
        name=str(_required(raw, "name")),
        test_type=test_type,
        workload=workload,
        endpoints=[_parse_endpoint(item) for item in raw.get("endpoints", [])],
        pages=[_parse_page(item) for item in raw.get("pages", [])],
        requires_approval=requires_approval,
    )


def _parse_endpoint(raw: dict[str, Any]) -> Endpoint:
    sla_raw = _required(raw, "sla")
    return Endpoint(
        name=str(_required(raw, "name")),
        method=str(raw.get("method", "GET")).upper(),
        url=str(_required(raw, "url")),
        sla=ApiSla(
            p95_ms=int(_required(sla_raw, "p95_ms")),
            p99_ms=int(_required(sla_raw, "p99_ms")),
            error_rate_pct=float(_required(sla_raw, "error_rate_pct")),
            throughput_rps=float(_required(sla_raw, "throughput_rps")),
        ),
        business_criticality=int(raw.get("business_criticality", 3)),
    )


def _parse_page(raw: dict[str, Any]) -> PageTarget:
    return PageTarget(
        name=str(_required(raw, "name")),
        url=str(_required(raw, "url")),
        business_criticality=int(raw.get("business_criticality", 3)),
    )


def _required(raw: dict[str, Any], key: str) -> Any:
    if key not in raw:
        raise ConfigError(f"Missing required key: {key}")
    return raw[key]


def _optional_path(value: str | None, base_dir: Path | None) -> str | None:
    if not value:
        return None
    path = Path(value)
    if path.is_absolute() or base_dir is None:
        return str(path)
    return str((base_dir / path).resolve())


def _optional_float(value: Any) -> float | None:
    if value is None or value == "":
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        raise ConfigError(f"Invalid numeric value: {value}")


def _optional_str(value: Any) -> str | None:
    if value is None:
        return None
    return str(value)


def _parse_monitoring_connector(raw: dict[str, Any]) -> MonitoringConnector:
    try:
        return MonitoringConnector(
            name=str(_required(raw, "name")),
            connector_type=str(_required(raw, "type")).lower(),
            endpoint=raw.get("endpoint"),
            api_key=require_secret_reference(raw.get("api_key"), "monitoring connector api_key"),
            query=raw.get("query"),
            dashboard_url=raw.get("dashboard_url"),
            trace_url_template=raw.get("trace_url_template"),
            options=resolve_secret_references(dict(raw.get("options", {}))) if raw.get("options") else {},
        )
    except (ValueError, RuntimeError) as exc:
        raise ConfigError(str(exc)) from exc


def _parse_database_connector(raw: dict[str, Any], base_dir: Path | None) -> DatabaseConnector:
    try:
        return DatabaseConnector(
            name=str(_required(raw, "name")),
            connector_type=str(_required(raw, "type")).lower(),
            source_file=_optional_path(raw.get("source_file"), base_dir),
            connection_string=require_secret_reference(
                raw.get("connection_string"),
                "database connector connection_string",
            ),
            options=resolve_secret_references(dict(raw.get("options", {}))) if raw.get("options") else {},
        )
    except (ValueError, RuntimeError) as exc:
        raise ConfigError(str(exc)) from exc
