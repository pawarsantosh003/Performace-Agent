# Governance, Security, and Approval Controls

## Roles

| Role | Capabilities |
| --- | --- |
| Viewer | Sign in, view saved runs, reports, findings, and approved artifacts. |
| Tester | Viewer access plus smoke/load execution and risky-test approval requests. |
| Approver | Tester access plus approve or reject pending risky-test requests. Approvers cannot approve their own request. |
| Admin | Full access, including approval decisions and audit-trail review. |

## User Configuration

Configure users with `PERF_AGENT_USERS_FILE` or `PERF_AGENT_USERS_JSON`.

Use [users.example.json](../examples/users.example.json) as the template. Passwords in user configuration must be either:

- a PBKDF2 `password_hash`, or
- a secret reference such as `$secret:env:PERF_TESTER_PASSWORD`

Plaintext passwords in user files are rejected.

For local startup, `start_agent_ui.bat` requests a temporary administrator password using a hidden prompt when `PERF_AGENT_ADMIN_PASSWORD` is not already set.

Self-service signup is disabled by default. To enable it for one organization domain:

```powershell
$env:PERF_AGENT_ALLOWED_EMAIL_DOMAIN = "example.com"
```

New self-service accounts receive the Viewer role.

## Approval Workflow

Stress, spike, and endurance scenarios require approval.

1. A Tester, Approver, or Admin configures the exact test.
2. The user submits an approval request with the purpose and expected impact.
3. An Approver or Admin reviews the request.
4. Non-admin requesters cannot approve their own request.
5. The requester selects the approved record and starts the test.
6. The approval is consumed after a successful run.

Approvals are:

- bound to a SHA-256 fingerprint of the complete test configuration
- invalidated if URLs, workload, duration, release, engine, limits, or scenarios change
- usable only by the requester
- one-time use

The CLI accepts `--approval-id <id>` for risky scenarios. The legacy `--approve-risky` bypass is rejected.

## Secret References

Supported reference formats:

```text
$secret:azure:<secret-name>
$secret:aws:<secret-id>
$secret:vault:<path>
$secret:env:<environment-variable>
```

Supported providers:

- Azure Key Vault
- AWS Secrets Manager
- HashiCorp Vault
- environment variables for local development and CI

Credential-bearing fields such as `api_key`, `connection_string`, passwords, tokens, and nested sensitive connector options reject plaintext values.

## Redaction

The agent recursively redacts sensitive keys and secret references from:

- audit events
- JSON artifacts
- reports and manifests
- serialized connector configuration

Audit logs are written to `runs/audit.log`.

## Environment Guardrails

Environment configuration supports:

- `allowed_hosts`
- `allowed_url_prefixes`
- `max_concurrent_users`
- `max_duration_seconds`
- `max_target_tps`
- `test_window_start`
- `test_window_end`

Approval does not override these guardrails.

## Web Security

- Session cookies are `HttpOnly`, `SameSite=Strict`, time limited, and marked `Secure` behind HTTPS.
- API run history, artifacts, approvals, and audit endpoints require authentication.
- Cross-origin state-changing requests are rejected.
- Only Admin users can read the audit trail.
