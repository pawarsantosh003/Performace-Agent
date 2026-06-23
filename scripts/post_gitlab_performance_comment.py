from __future__ import annotations

import json
import os
import urllib.request
from pathlib import Path


def main() -> int:
    merge_request = os.environ.get("CI_MERGE_REQUEST_IID")
    token = os.environ.get("GITLAB_TOKEN")
    if not merge_request or not token:
        print("GitLab merge request comment skipped: CI_MERGE_REQUEST_IID or GITLAB_TOKEN is not set.")
        return 0

    summaries = sorted(Path("runs").glob("*/readiness_summary.md"))
    body = "# Performance Readiness Gate\n\n"
    body += summaries[-1].read_text(encoding="utf-8") if summaries else "No readiness summary was generated."
    body += f"\n\nGate exit code: `{os.environ.get('GATE_CODE', 'unknown')}`"

    api_url = os.environ["CI_API_V4_URL"].rstrip("/")
    project_id = os.environ["CI_PROJECT_ID"]
    url = f"{api_url}/projects/{project_id}/merge_requests/{merge_request}/notes"
    request = urllib.request.Request(
        url,
        data=json.dumps({"body": body}).encode("utf-8"),
        headers={
            "PRIVATE-TOKEN": token,
            "Content-Type": "application/json",
        },
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=30) as response:
        print(response.read().decode("utf-8"))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
