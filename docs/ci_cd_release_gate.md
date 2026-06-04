# CI/CD and Release Gate Automation

This repository now includes cross-platform CI/CD templates and a CLI release gate mode for automated release readiness checks.

## Release Gate Policy

The agent computes a release readiness score and status from performance findings.

- `GREEN` (score >= 90): pass
- `AMBER` (75 <= score < 90): warn, review before merge/release
- `RED` (60 <= score < 75): fail release gate
- `BLOCKED` (critical findings or score < 60): fail release gate

Use the CLI with `--release-gate` to automatically return:

- `0` for GREEN
- `1` for AMBER
- `2` for RED or BLOCKED

## CLI Gate Usage

Run the agent in CI with release gate mode:

```powershell
python -m perf_agent run --config examples/perf_agent_config.json --out runs --approve-risky --release-gate
```

The agent writes artifacts under `runs/<run-id>/`, including:

- `performance_report.md`
- `release_readiness.json`
- `readiness_summary.md`
- `manifest.json`
- `optimization_backlog.json`
- `raw_results.json`

## GitHub Actions

Use `.github/workflows/release-readiness-gate.yml` to invoke the CLI, upload artifacts, and post a PR comment with the summary.

Key steps:

1. Checkout source.
2. Install the package in editable mode.
3. Run `python -m perf_agent run ... --release-gate`.
4. Upload generated `runs/**` artifacts.
5. Post or update a PR comment with `readiness_summary.md`.

### What this workflow does

- Blocks the job on `RED` or `BLOCKED`.
- Marks a warning state with exit code `1` on `AMBER`.
- Uses `peter-evans/create-or-update-comment@v4` to attach a readable summary to pull requests.

## GitLab CI/CD

Use `.gitlab-ci.yml` to run the release gate and archive `runs/` as artifacts.

The template includes an optional merge request note step when `CI_MERGE_REQUEST_IID` and `GITLAB_TOKEN` are present.

## Jenkins

Use the `Jenkinsfile` in the repository for simple scripted pipeline execution.

The sample pipeline:

- checks out code
- installs Python dependencies
- runs the performance agent in gate mode
- archives `runs/**`

## Azure DevOps

Use `azure-pipelines.yml` to run the gate on Azure-hosted agents and publish `runs/` as build artifacts.

## Report Attachments

All CI templates upload the `runs/` directory so the performance report, readiness JSON, and summary markdown are available to reviewers.

## Sample Pipeline Reports

The repository includes `examples/sample_pipeline_report.md` as a sample readiness summary that can be used as a starting point for PR comment bodies or release dashboards.
