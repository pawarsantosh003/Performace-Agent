from __future__ import annotations

import argparse
import json
import mimetypes
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlparse

from .adapters import ApprovalRequired, GuardrailViolation
from .config import ConfigError, parse_config
from .serialization import to_json
from .workflow import PerformanceAgent


PROJECT_ROOT = Path(__file__).resolve().parents[2]
WEB_ROOT = Path(__file__).resolve().parent / "web_static"
EXAMPLES_ROOT = PROJECT_ROOT / "examples"
RUNS_ROOT = PROJECT_ROOT / "runs"


class AgentWebHandler(BaseHTTPRequestHandler):
    server_version = "PerfAgentWeb/0.1"

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        path = parsed.path
        if path == "/":
            self._serve_file(WEB_ROOT / "index.html")
            return
        if path.startswith("/static/"):
            static_path = WEB_ROOT / path.removeprefix("/static/")
            self._serve_file(static_path)
            return
        if path == "/api/sample-config":
            self._send_json(_read_json(EXAMPLES_ROOT / "perf_agent_config.json"))
            return
        if path == "/api/runs":
            query = parse_qs(parsed.query)
            self._send_json(_list_runs(search=query.get("q", [""])[0]))
            return
        if path.startswith("/api/run-details/"):
            self._send_run_details(path)
            return
        if path.startswith("/api/runs/"):
            self._send_run_artifact(path)
            return
        self._send_error(HTTPStatus.NOT_FOUND, "Route not found")

    def do_POST(self) -> None:
        parsed = urlparse(self.path)
        if parsed.path != "/api/run":
            self._send_error(HTTPStatus.NOT_FOUND, "Route not found")
            return

        try:
            payload = self._read_json_body()
            config = parse_config(payload["config"], base_dir=EXAMPLES_ROOT)
            approve_risky = bool(payload.get("approve_risky", False))
            use_k6 = bool(payload.get("use_k6", False))
            run = PerformanceAgent(use_k6=use_k6).run(config, output_root=RUNS_ROOT, approve_risky=approve_risky)
            self._send_json(
                {
                    "run_id": run.run_id,
                    "readiness": to_json(run.readiness),
                    "findings": to_json(run.findings),
                    "scenario_results": to_json(run.scenario_results),
                    "artifacts": _web_artifact_links(run.run_id),
                    "manifest": _read_json(Path(run.artifacts["manifest"])),
                }
            )
        except KeyError as exc:
            self._send_error(HTTPStatus.BAD_REQUEST, f"Missing field: {exc}")
        except (ConfigError, ApprovalRequired, GuardrailViolation) as exc:
            self._send_error(HTTPStatus.BAD_REQUEST, str(exc))
        except json.JSONDecodeError as exc:
            self._send_error(HTTPStatus.BAD_REQUEST, f"Invalid JSON: {exc}")

    def log_message(self, format: str, *args) -> None:
        print(f"{self.address_string()} - {format % args}")

    def _read_json_body(self) -> dict:
        length = int(self.headers.get("Content-Length", "0"))
        raw = self.rfile.read(length).decode("utf-8")
        return json.loads(raw)

    def _send_run_artifact(self, path: str) -> None:
        parts = [unquote(part) for part in path.split("/") if part]
        if len(parts) != 4 or parts[0] != "api" or parts[1] != "runs":
            self._send_error(HTTPStatus.NOT_FOUND, "Artifact not found")
            return
        run_id = parts[2]
        artifact = parts[3]
        allowed = {
            "report": "performance_report.md",
            "baseline": "baseline.json",
            "backlog": "optimization_backlog.json",
            "readiness": "release_readiness.json",
            "raw": "raw_results.json",
            "manifest": "manifest.json",
        }
        if artifact not in allowed:
            self._send_error(HTTPStatus.NOT_FOUND, "Artifact not found")
            return
        artifact_path = RUNS_ROOT / run_id / allowed[artifact]
        self._serve_file(artifact_path, download_name=allowed[artifact])

    def _send_run_details(self, path: str) -> None:
        parts = [unquote(part) for part in path.split("/") if part]
        if len(parts) != 3 or parts[0] != "api" or parts[1] != "run-details":
            self._send_error(HTTPStatus.NOT_FOUND, "Run not found")
            return
        run_id = parts[2]
        run_dir = RUNS_ROOT / run_id
        if not run_dir.exists() or not run_dir.is_dir():
            self._send_error(HTTPStatus.NOT_FOUND, "Run not found")
            return
        try:
            manifest_path = run_dir / "manifest.json"
            readiness = _read_json(run_dir / "release_readiness.json")
            self._send_json(
                {
                    "run_id": run_id,
                    "manifest": _read_json(manifest_path) if manifest_path.exists() else _legacy_manifest(run_id, readiness),
                    "readiness": readiness,
                    "findings": _read_json(run_dir / "optimization_backlog.json"),
                    "scenario_results": _read_json(run_dir / "raw_results.json"),
                    "report_text": (run_dir / "performance_report.md").read_text(encoding="utf-8"),
                    "artifacts": _web_artifact_links(run_id),
                }
            )
        except FileNotFoundError:
            self._send_error(HTTPStatus.NOT_FOUND, "Run artifacts are incomplete")

    def _serve_file(self, path: Path, download_name: str | None = None) -> None:
        resolved = path.resolve()
        allowed_roots = [WEB_ROOT.resolve(), RUNS_ROOT.resolve()]
        if not any(str(resolved).startswith(str(root)) for root in allowed_roots):
            self._send_error(HTTPStatus.FORBIDDEN, "Forbidden")
            return
        if not resolved.exists() or not resolved.is_file():
            self._send_error(HTTPStatus.NOT_FOUND, "File not found")
            return
        content_type = mimetypes.guess_type(str(resolved))[0] or "application/octet-stream"
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", f"{content_type}; charset=utf-8" if content_type.startswith("text/") else content_type)
        if download_name:
            self.send_header("Content-Disposition", f'inline; filename="{download_name}"')
        self.end_headers()
        self.wfile.write(resolved.read_bytes())

    def _send_json(self, payload: object, status: HTTPStatus = HTTPStatus.OK) -> None:
        raw = json.dumps(payload, indent=2).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def _send_error(self, status: HTTPStatus, message: str) -> None:
        self._send_json({"error": message}, status=status)


def serve(host: str = "127.0.0.1", port: int = 8765) -> None:
    RUNS_ROOT.mkdir(parents=True, exist_ok=True)
    server = ThreadingHTTPServer((host, port), AgentWebHandler)
    print(f"Performance Testing AI Agent UI: http://{host}:{port}")
    server.serve_forever()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Start the Performance Testing AI Agent web UI.")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8765)
    args = parser.parse_args(argv)
    serve(host=args.host, port=args.port)
    return 0


def _read_json(path: Path) -> object:
    return json.loads(path.read_text(encoding="utf-8"))


def _list_runs(search: str = "") -> list[dict[str, str]]:
    if not RUNS_ROOT.exists():
        return []
    runs = []
    needle = search.strip().lower()
    for path in sorted(RUNS_ROOT.iterdir(), key=lambda item: item.stat().st_mtime, reverse=True):
        if not path.is_dir():
            continue
        manifest_path = path / "manifest.json"
        readiness_path = path / "release_readiness.json"
        if manifest_path.exists():
            item = _read_json(manifest_path)
        else:
            readiness = _read_json(readiness_path) if readiness_path.exists() else {}
            item = {
                "run_id": path.name,
                "application_name": path.name,
                "release_id": "",
                "environment": "",
                "primary_url": "",
                "test_type": "",
                "created_at": "",
                "readiness": readiness,
                "finding_count": "n/a",
            }
        item["report_url"] = f"/api/runs/{path.name}/report"
        item["details_url"] = f"/api/run-details/{path.name}"
        haystack = " ".join(
            str(item.get(key, ""))
            for key in ["run_id", "application_name", "release_id", "environment", "primary_url", "test_type"]
        ).lower()
        if needle and needle not in haystack:
            continue
        runs.append(item)
    return runs


def _legacy_manifest(run_id: str, readiness: dict) -> dict[str, object]:
    return {
        "run_id": run_id,
        "application_name": run_id,
        "release_id": "",
        "environment": "",
        "base_url": "",
        "primary_url": "",
        "test_type": "",
        "created_at": "",
        "readiness": readiness,
        "finding_count": "n/a",
        "critical_count": "n/a",
        "high_count": "n/a",
        "medium_count": "n/a",
        "low_count": "n/a",
    }


def _web_artifact_links(run_id: str) -> dict[str, str]:
    return {
        "report": f"/api/runs/{run_id}/report",
        "baseline": f"/api/runs/{run_id}/baseline",
        "backlog": f"/api/runs/{run_id}/backlog",
        "readiness": f"/api/runs/{run_id}/readiness",
        "raw": f"/api/runs/{run_id}/raw",
        "manifest": f"/api/runs/{run_id}/manifest",
    }


if __name__ == "__main__":
    raise SystemExit(main())
