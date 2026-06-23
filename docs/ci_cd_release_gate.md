# CI/CD and Release Gate Automation

This repository includes cross-platform CI/CD templates and a CLI release gate mode for automated performance release decisions.

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

Machine-readable gate output is written to `release_gate.json`:

```json
{
  "score": 92.5,
  "status": "green",
  "decision": "pass",
  "exit_code": 0,
  "blockers": []
}
```

CI systems should handle the exit codes as follows:

- `0`: pass the job.
- `1`: publish a warning/unstable result but do not block automatically.
- `2`: fail the job and block release.

## CLI Gate Usage

Run the agent in CI with release gate mode:

```powershell
python -m perf_agent run --config examples/ci_release_gate_config.json --out runs --release-gate
```

The agent writes artifacts under `runs/<run-id>/`, including:

- `performance_report.md`
- `release_readiness.json`
- `release_gate.json`
- `readiness_summary.md`
- `manifest.json`
- `optimization_backlog.json`
- `raw_results.json`

## GitHub Actions

Use `.github/workflows/release-readiness-gate.yml` to invoke the CLI, upload artifacts, and post or update a PR comment with the summary.

Key steps:

1. Checkout source.
2. Install the package in editable mode.
3. Run the CLI while preserving its exit code.
4. Upload generated `runs/**` artifacts with `if: always()`.
5. Post or update a PR comment with `readiness_summary.md`.
6. Pass, warn, or block according to the captured exit code.

### What this workflow does

- Blocks the job on `RED` or `BLOCKED`.
- Emits a GitHub workflow warning on `AMBER`.
- Uses `actions/github-script` and the `pull-requests: write` permission to maintain one PR comment.
- Uses `actions/upload-artifact` to retain the complete `runs/` directory.

## GitLab CI/CD

Use `.gitlab-ci.yml` to run the release gate and archive `runs/` as artifacts.

The template:

- treats exit code `1` as an allowed warning using `allow_failure.exit_codes`
- blocks on exit code `2`
- uploads artifacts with `when: always`
- optionally posts a merge-request note when `CI_MERGE_REQUEST_IID` and `GITLAB_TOKEN` are present

## Jenkins

Use the `Jenkinsfile` in the repository for simple scripted pipeline execution.

The sample pipeline:

- checks out code
- installs Python dependencies
- marks the build unstable for AMBER
- fails the build for RED/BLOCKED
- archives `runs/**` even when the gate blocks

## Azure DevOps

Use `azure-pipelines.yml` to run the gate on Azure-hosted agents and publish `runs/` as build artifacts. AMBER emits an Azure warning and continues; RED/BLOCKED fails the gate step. Artifact publishing runs with `condition: always()`.

## Configuration

All templates default to:

```text
examples/ci_release_gate_config.json
```

Replace `PERF_AGENT_CONFIG` with the application-specific pre-production configuration. Keep credentials and API keys in the CI platform's secret store, not in the JSON file.

GitHub supports a repository variable named `PERF_AGENT_CONFIG`. Jenkins, GitLab, and Azure define the same environment/variable name in their templates.

## Pull Request and Merge Request Comments

GitHub Actions posts a sticky-style PR comment using a hidden marker and updates the existing bot comment on subsequent runs.

GitLab can post a merge-request note when `GITLAB_TOKEN` is configured with permission to create notes.

Jenkins and Azure publish the summary as build artifacts by default. Teams can connect their existing SCM status/comment plugins without changing the release-gate contract.

## Report Attachments

All CI templates upload the `runs/` directory so the performance report, gate JSON, readiness JSON, summary Markdown, backlog, baseline, raw results, and connector metadata are available to reviewers.

## Branch Protection

For automatic blocking:

1. Add the performance gate job as a required check on the protected branch.
2. Keep RED/BLOCKED mapped to a failing job.
3. Decide whether AMBER requires human approval, a label, or only reviewer acknowledgement.
4. Use `--approval-id` for any approved stress, spike, or endurance pipeline.

## Sample Pipeline Reports

The repository includes `examples/sample_pipeline_report.md` as a sample readiness summary that can be used as a starting point for PR comment bodies or release dashboards.
