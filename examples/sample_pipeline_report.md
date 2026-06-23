# Sample Performance Pipeline Report

- Run ID: `sample-app-20260602T120000Z`
- Release: `2026.05.29-rc1`
- Environment: `pre-prod`
- Status: **AMBER**
- Score: **78 / 100**
- Gate decision: **WARN**
- Gate exit code: `1`

## Findings

- `Checkout submit` p95 latency exceeds SLA
- `Infrastructure CPU utilization is near saturation`

## Artifacts

- `performance_report.md`
- `release_readiness.json`
- `release_gate.json`
- `readiness_summary.md`
- `optimization_backlog.json`
- `raw_results.json`

## Pipeline behavior

- `GREEN` = pass
- `AMBER` = warn
- `RED` / `BLOCKED` = fail

## Reviewer action

Review the high-priority findings, confirm mitigation ownership, and approve or reject the release according to the team gate policy.
