# Performance Testing AI Agent

This repository contains a working MVP of the Performance Testing AI Agent described in [Performance_Testing_AI_Agent_Blueprint.md](./Performance_Testing_AI_Agent_Blueprint.md).

The agent is designed to run before UAT or production release and produce:

- Load, spike, stress, endurance, and smoke-test assessments.
- Core Web Vitals and frontend performance findings.
- API latency, throughput, and error-rate findings.
- Database bottleneck findings from supplied diagnostics.
- Infrastructure saturation findings.
- Prioritized optimization backlog.
- Release readiness score.
- Pre-launch baseline artifacts.

## Current MVP Capabilities

- JSON-based application and scenario configuration.
- Guardrails for risky stress, spike, and endurance tests.
- Deterministic synthetic executor that works without external tools.
- Optional k6 adapter that runs k6 when it is installed on PATH.
- Monitoring and database metric loaders from JSON files.
- Performance analysis and weighted backlog scoring.
- Markdown and JSON report generation.
- Baseline artifact generation.

## Quick Start

Use the bundled Python runtime in Codex, or any Python 3.11+ installation.

```powershell
$py = "C:\Users\SantoshLaxmanPawar\.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe"
$env:PYTHONPATH = "$PWD\src"
& $py -m perf_agent run --config .\examples\perf_agent_config.json --out .\runs --approve-risky
```

The `--approve-risky` flag is required for the sample spike scenario. Without it, the guardrail stops the run.

Generated artifacts are written under `runs/<run-id>/`:

- `performance_report.md`
- `baseline.json`
- `optimization_backlog.json`
- `release_readiness.json`
- `raw_results.json`

## Web UI

For most users on Windows, double-click:

```text
start_agent_ui.bat
```

Keep that terminal window open, then open:

```text
http://127.0.0.1:8765
```

Start the browser UI with:

```powershell
$py = "C:\Users\SantoshLaxmanPawar\.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe"
$env:PYTHONPATH = "$PWD\src"
& $py -m perf_agent.web --host 127.0.0.1 --port 8765
```

Then open the same URL:

```text
http://127.0.0.1:8765
```

The UI lets a user:

- Run a fast Quick Web Check by pasting a website URL.
- Edit common fields such as application, release, environment, users, duration, and test type.
- Build a scenario from form controls without editing JSON.
- Search and reopen previous local runs.
- View the detailed report inside the UI.
- Download report, baseline, backlog, readiness JSON, and raw results.
- See clearer run states: Passed, Amber, Failed, Blocked, and Running.
- Keep k6 disabled for fast UI results, or enable it intentionally for a real duration-based load run.
- Approve risky spike/stress/endurance tests only when needed.
- Switch to Full Release mode for advanced JSON scenarios.
- Review release readiness, findings, scenario metrics, and generated artifacts.

## Test Engines

The UI and CLI now support multiple engines:

| Engine | Requirement | Notes |
| --- | --- | --- |
| Fast Assessment | None | Uses local HTTP probe plus deterministic estimates; fastest fallback. |
| k6 Load Test | `k6` installed on PATH | Generates a k6 script, runs it, parses `summary-export` metrics, and maps thresholds. |
| Lighthouse Audit | `lighthouse` installed on PATH, or local `npx --no-install lighthouse` | Parses Lighthouse JSON audits into LCP, FCP, CLS, TTFB, and INP/TBT proxy. |
| k6 + Lighthouse | k6 and Lighthouse installed | Uses real load metrics plus real Lighthouse web metrics. |
| PageSpeed Insights | Optional `PAGESPEED_API_KEY` environment variable | Calls PageSpeed Insights and parses the Lighthouse result. |
| WebPageTest | `WEBPAGETEST_API_KEY` environment variable | Submits a WebPageTest run, polls for completion, and parses first-view metrics. |
| JMeter | `jmeter` installed on PATH | Generates a minimal JMX, runs JMeter non-GUI, and parses JTL results. |

CLI example:

```powershell
& $py -m perf_agent run --config .\examples\perf_agent_config.json --out .\runs --approve-risky --engine lighthouse
```

## Running With k6

If k6 is installed and available on PATH, run:

```powershell
& $py -m perf_agent run --config .\examples\perf_agent_config.json --out .\runs --approve-risky --use-k6
```

If k6 is not installed or fails, the agent falls back to deterministic synthetic results and records that in raw output.

## Configuration

The agent reads a JSON config with:

- `application_name`
- `release_id`
- `environment`
- `web_vitals`
- `monitoring_metrics_file`
- `database_metrics_file`
- `scenarios`

Each scenario includes:

- `name`
- `test_type`: `smoke`, `load`, `stress`, `spike`, or `endurance`
- `workload`
- `endpoints`
- `pages`
- `requires_approval`

## Production Integration Points

Replace or extend these classes as the implementation matures:

- `SyntheticExecutor` in `src/perf_agent/adapters.py` for JMeter, Gatling, Locust, or cloud load providers.
- `K6Executor` in `src/perf_agent/adapters.py` for full k6 summary parsing and threshold mapping.
- `MetricsLoader` in `src/perf_agent/adapters.py` for Prometheus, Grafana, Datadog, New Relic, Dynatrace, Elastic, or OpenTelemetry backends.
- `PerformanceAnalyzer` in `src/perf_agent/analysis.py` for deeper RCA, baseline regression analysis, and model-assisted recommendations.
- `ReportWriter` in `src/perf_agent/reporting.py` for Power BI, Confluence, SharePoint, Jira, Azure Boards, or ServiceNow publishing.

## Recommended Next Build Steps

1. Add real k6 summary parsing and threshold mapping.
2. Add Prometheus/Grafana query adapters.
3. Add database-specific connectors for PostgreSQL, MySQL, and SQL Server.
4. Add an LLM recommendation layer with schema-validated outputs.
5. Add CI templates for GitHub Actions, Jenkins, GitLab CI/CD, and Azure DevOps.
6. Add a simple web dashboard for run history and approval gates.
