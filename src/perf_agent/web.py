from __future__ import annotations

import argparse
import json
import mimetypes
import os
from dataclasses import asdict
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, unquote, urlparse

from .adapters import ApprovalRequired, GuardrailViolation
from .config import ConfigError, parse_config
from .governance import (
    ApprovalManager,
    AuditLogger,
    SessionManager,
    UserStore,
    UserRole,
    SESSION_COOKIE_NAME,
    SESSION_TIMEOUT_SECONDS,
    risky_scenario_names,
)
from .serialization import to_json
from .workflow import PerformanceAgent


PROJECT_ROOT = Path(__file__).resolve().parents[2]
WEB_ROOT = Path(__file__).resolve().parent / "web_static"
EXAMPLES_ROOT = PROJECT_ROOT / "examples"
RUNS_ROOT = PROJECT_ROOT / "runs"
SESSION_MANAGER = SessionManager()
AUDIT_LOGGER = AuditLogger()
APPROVAL_MANAGER = ApprovalManager()
USER_STORE = UserStore()


class AgentWebHandler(BaseHTTPRequestHandler):
    server_version = "PerfAgentWeb/0.1"

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        path = parsed.path.rstrip("/")
        if path == "":
            self._serve_file(WEB_ROOT / "index.html")
            return
        if path.startswith("/static/"):
            static_path = WEB_ROOT / path.removeprefix("/static/")
            self._serve_file(static_path)
            return
        if path == "/api/session":
            self._send_json(self._session_payload())
            return
        if path.startswith("/api/") and not self._current_user():
            self._send_error(HTTPStatus.UNAUTHORIZED, "Authentication required.")
            return
        if path == "/api/sample-config":
            self._send_json(_read_json(EXAMPLES_ROOT / "perf_agent_config.json"))
            return
        if path == "/api/audit":
            self._send_audit()
            return
        if path == "/api/approvals":
            self._send_approvals()
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
        if not self._origin_allowed():
            self._send_error(HTTPStatus.FORBIDDEN, "Cross-origin request rejected.")
            return
        parsed = urlparse(self.path)
        if parsed.path == "/api/login":
            self._handle_login()
            return
        if parsed.path == "/api/signup":
            self._handle_signup()
            return
        if parsed.path == "/api/logout":
            self._handle_logout()
            return
        if parsed.path == "/api/approval-request":
            self._handle_approval_request()
            return
        if parsed.path == "/api/approve":
            self._handle_approval_decision("approve")
            return
        if parsed.path == "/api/reject":
            self._handle_approval_decision("reject")
            return
        if parsed.path != "/api/run":
            self._send_error(HTTPStatus.NOT_FOUND, "Route not found")
            return

        try:
            user = self._current_user()
            if not user:
                self._send_error(HTTPStatus.UNAUTHORIZED, "Authentication required to start runs.")
                return
            if user.role == UserRole.VIEWER:
                self._send_error(HTTPStatus.FORBIDDEN, "Viewer role cannot start performance tests.")
                return
            payload = self._read_json_body()
            config = parse_config(payload["config"], base_dir=EXAMPLES_ROOT)
            use_k6 = bool(payload.get("use_k6", False))
            risky_scenarios = risky_scenario_names(config)
            approval_id = str(payload.get("approval_id", "")).strip()
            approval = None
            if risky_scenarios:
                if not approval_id:
                    raise ApprovalRequired("Stress, spike, and endurance tests require an approved request.")
                try:
                    approval = APPROVAL_MANAGER.validate(
                        approval_id,
                        config,
                        executor_username=user.username,
                    )
                except ValueError as exc:
                    raise ApprovalRequired(str(exc)) from exc
            AUDIT_LOGGER.log(
                "run_requested",
                user,
                {
                    "release_id": config.release_id,
                    "environment": config.environment.name,
                    "risky_scenarios": risky_scenarios,
                    "approval_id": approval_id or None,
                },
            )
            run = PerformanceAgent(use_k6=use_k6).run(
                config,
                output_root=RUNS_ROOT,
                approval=approval,
            )
            if approval:
                APPROVAL_MANAGER.consume(approval.approval_id, run.run_id)
                AUDIT_LOGGER.log(
                    "approval_consumed",
                    user,
                    {"approval_id": approval.approval_id, "run_id": run.run_id},
                )
            AUDIT_LOGGER.log("run_completed", user, {"run_id": run.run_id, "status": run.readiness.status.value})
            self._send_json(
                {
                    "run_id": run.run_id,
                    "readiness": to_json(run.readiness),
                    "findings": to_json(run.findings),
                    "scenario_results": to_json(run.scenario_results),
                    "connector_annotations": to_json(run.connector_annotations),
                    "artifacts": _web_artifact_links(run.run_id),
                    "manifest": _read_json(Path(run.artifacts["manifest"])),
                }
            )
        except KeyError as exc:
            self._send_error(HTTPStatus.BAD_REQUEST, f"Missing field: {exc}")
        except (ConfigError, ApprovalRequired, GuardrailViolation) as exc:
            AUDIT_LOGGER.log("run_denied", self._current_user(), {"reason": str(exc)})
            self._send_error(HTTPStatus.BAD_REQUEST, str(exc))
        except json.JSONDecodeError as exc:
            self._send_error(HTTPStatus.BAD_REQUEST, f"Invalid JSON: {exc}")

    def log_message(self, format: str, *args) -> None:
        print(f"{self.address_string()} - {format % args}")

    def _current_user(self) -> Any:
        return SESSION_MANAGER.get_user(self.headers.get("Cookie"))

    def _session_payload(self) -> dict[str, Any]:
        user = self._current_user()
        if not user:
            return {
                "authenticated": False,
                "signup_enabled": bool(os.environ.get("PERF_AGENT_ALLOWED_EMAIL_DOMAIN")),
                "signup_domain": os.environ.get("PERF_AGENT_ALLOWED_EMAIL_DOMAIN", ""),
            }
        return {
            "authenticated": True,
            "username": user.username,
            "role": user.role.value,
            "display_name": user.display_name,
            "signup_enabled": bool(os.environ.get("PERF_AGENT_ALLOWED_EMAIL_DOMAIN")),
            "signup_domain": os.environ.get("PERF_AGENT_ALLOWED_EMAIL_DOMAIN", ""),
        }

    def _handle_login(self) -> None:
        try:
            payload = self._read_json_body()
            username = str(payload.get("username", "")).strip()
            password = str(payload.get("password", ""))
            user = USER_STORE.authenticate(username, password)
            if not user:
                AUDIT_LOGGER.log("login_failed", None, {"username": username})
                self._send_error(HTTPStatus.UNAUTHORIZED, "Invalid username or password.")
                return
            session_id = SESSION_MANAGER.create_session(user)
            AUDIT_LOGGER.log("login_success", user, {})
            self.send_response(HTTPStatus.OK)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Set-Cookie", self._session_cookie(session_id))
            self.end_headers()
            self.wfile.write(
                json.dumps(
                    {"authenticated": True, "username": user.username, "role": user.role.value}
                ).encode("utf-8")
            )
        except json.JSONDecodeError as exc:
            self._send_error(HTTPStatus.BAD_REQUEST, f"Invalid JSON: {exc}")

    def _handle_signup(self) -> None:
        try:
            payload = self._read_json_body()
            email = str(payload.get("email", "")).strip()
            password = str(payload.get("password", ""))
            password_confirm = str(payload.get("password_confirm", ""))
            
            if not email or not password:
                self._send_error(HTTPStatus.BAD_REQUEST, "Email and password are required.")
                return
            
            if password != password_confirm:
                self._send_error(HTTPStatus.BAD_REQUEST, "Passwords do not match.")
                return
            
            try:
                user = USER_STORE.register(email, password)
                AUDIT_LOGGER.log("user_registered", None, {"email": email, "username": user.username})
                session_id = SESSION_MANAGER.create_session(user)
                AUDIT_LOGGER.log("login_success", user, {"method": "signup"})
                self.send_response(HTTPStatus.OK)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.send_header("Set-Cookie", self._session_cookie(session_id))
                self.end_headers()
                self.wfile.write(
                    json.dumps(
                        {"authenticated": True, "username": user.username, "role": user.role.value}
                    ).encode("utf-8")
                )
            except ValueError as exc:
                AUDIT_LOGGER.log("signup_failed", None, {"email": email, "reason": str(exc)})
                self._send_error(HTTPStatus.BAD_REQUEST, str(exc))
        except json.JSONDecodeError as exc:
            self._send_error(HTTPStatus.BAD_REQUEST, f"Invalid JSON: {exc}")

    def _handle_logout(self) -> None:
        user = self._current_user()
        if user:
            AUDIT_LOGGER.log("logout", user, {})
        SESSION_MANAGER.destroy_session(self.headers.get("Cookie"))
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header(
            "Set-Cookie",
            f"{SESSION_COOKIE_NAME}=deleted; HttpOnly; Path=/; Max-Age=0; SameSite=Strict",
        )
        self.end_headers()
        self.wfile.write(json.dumps({"authenticated": False}).encode("utf-8"))

    def _handle_approval_request(self) -> None:
        user = self._current_user()
        if not user:
            self._send_error(HTTPStatus.UNAUTHORIZED, "Authentication required to request approval.")
            return
        if user.role == UserRole.VIEWER:
            self._send_error(HTTPStatus.FORBIDDEN, "Viewer role cannot request performance test approval.")
            return
        try:
            payload = self._read_json_body()
            config = parse_config(payload["config"], base_dir=EXAMPLES_ROOT)
            record = APPROVAL_MANAGER.request(config, user, payload.get("comment"))
            AUDIT_LOGGER.log("approval_requested", user, asdict(record))
            self._send_json(asdict(record), status=HTTPStatus.CREATED)
        except (KeyError, ConfigError, ValueError, json.JSONDecodeError) as exc:
            AUDIT_LOGGER.log("approval_request_denied", user, {"reason": str(exc)})
            self._send_error(HTTPStatus.BAD_REQUEST, str(exc))

    def _handle_approval_decision(self, decision: str) -> None:
        user = self._current_user()
        if not user:
            self._send_error(HTTPStatus.UNAUTHORIZED, "Authentication required to approve.")
            return
        if user.role not in {UserRole.APPROVER, UserRole.ADMIN}:
            self._send_error(HTTPStatus.FORBIDDEN, "Only Approver and Admin roles can approve risky test runs.")
            return
        try:
            payload = self._read_json_body()
            approval_id = str(payload.get("approval_id", "")).strip()
            comment = payload.get("comment")
            if not approval_id:
                self._send_error(HTTPStatus.BAD_REQUEST, "approval_id is required.")
                return
            if decision == "approve":
                record = APPROVAL_MANAGER.approve(approval_id, user, comment)
                event = "approval_approved"
            else:
                record = APPROVAL_MANAGER.reject(approval_id, user, comment)
                event = "approval_rejected"
            AUDIT_LOGGER.log(event, user, asdict(record))
            self._send_json(asdict(record))
        except json.JSONDecodeError as exc:
            AUDIT_LOGGER.log("approval_decision_denied", user, {"decision": decision, "reason": str(exc)})
            self._send_error(HTTPStatus.BAD_REQUEST, f"Invalid JSON: {exc}")
        except ValueError as exc:
            AUDIT_LOGGER.log("approval_decision_denied", user, {"decision": decision, "reason": str(exc)})
            self._send_error(HTTPStatus.BAD_REQUEST, str(exc))

    def _send_audit(self) -> None:
        user = self._current_user()
        if not user or user.role != UserRole.ADMIN:
            self._send_error(HTTPStatus.FORBIDDEN, "Audit log access requires Admin role.")
            return
        entries = [asdict(entry) for entry in AUDIT_LOGGER.tail(200)]
        self._send_json(entries)

    def _send_approvals(self) -> None:
        user = self._current_user()
        if not user:
            self._send_error(HTTPStatus.UNAUTHORIZED, "Authentication required.")
            return
        approvals = APPROVAL_MANAGER.list_for_user(user)
        self._send_json([asdict(item) for item in approvals])

    def _session_cookie(self, session_id: str) -> str:
        secure = self.headers.get("X-Forwarded-Proto", "").lower() == "https"
        secure_part = "; Secure" if secure else ""
        return (
            f"{SESSION_COOKIE_NAME}={session_id}; HttpOnly; Path=/; "
            f"SameSite=Strict; Max-Age={SESSION_TIMEOUT_SECONDS}{secure_part}"
        )

    def _read_json_body(self) -> dict:
        length = int(self.headers.get("Content-Length", "0"))
        raw = self.rfile.read(length).decode("utf-8")
        return json.loads(raw)

    def _origin_allowed(self) -> bool:
        origin = self.headers.get("Origin")
        if not origin:
            return True
        parsed = urlparse(origin)
        return parsed.netloc.lower() == self.headers.get("Host", "").lower()

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
            "gate": "release_gate.json",
            "raw": "raw_results.json",
            "connectors": "connector_annotations.json",
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
        run_dir = _safe_run_dir(run_id)
        if not run_dir:
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
                    "connector_annotations": _read_optional_json(run_dir / "connector_annotations.json", {}),
                    "report_text": (run_dir / "performance_report.md").read_text(encoding="utf-8"),
                    "artifacts": _web_artifact_links(run_id),
                }
            )
        except FileNotFoundError:
            self._send_error(HTTPStatus.NOT_FOUND, "Run artifacts are incomplete")

    def _serve_file(self, path: Path, download_name: str | None = None) -> None:
        resolved = path.resolve()
        allowed_roots = [WEB_ROOT.resolve(), RUNS_ROOT.resolve()]
        if not any(resolved == root or resolved.is_relative_to(root) for root in allowed_roots):
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


def _read_optional_json(path: Path, default: object) -> object:
    if not path.exists():
        return default
    return _read_json(path)


def _safe_run_dir(run_id: str) -> Path | None:
    candidate = (RUNS_ROOT / run_id).resolve()
    root = RUNS_ROOT.resolve()
    if candidate == root or not candidate.is_relative_to(root):
        return None
    if not candidate.exists() or not candidate.is_dir():
        return None
    return candidate


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
        "gate": f"/api/runs/{run_id}/gate",
        "raw": f"/api/runs/{run_id}/raw",
        "connectors": f"/api/runs/{run_id}/connectors",
        "manifest": f"/api/runs/{run_id}/manifest",
    }


if __name__ == "__main__":
    raise SystemExit(main())
