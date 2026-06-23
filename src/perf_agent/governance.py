from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import secrets
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import asdict, dataclass, field, is_dataclass
from datetime import UTC, datetime
from enum import Enum
from http.cookies import SimpleCookie
from pathlib import Path
from typing import Any

from .models import AgentConfig

REPO_ROOT = Path(__file__).resolve().parents[2]
RUNS_ROOT = REPO_ROOT / "runs"
RUNS_ROOT.mkdir(parents=True, exist_ok=True)
AUDIT_LOG_PATH = RUNS_ROOT / "audit.log"
APPROVALS_PATH = RUNS_ROOT / "approvals.json"
USERS_DB_PATH = RUNS_ROOT / "users.json"
SESSION_COOKIE_NAME = "perf_agent_session"
SESSION_TIMEOUT_SECONDS = 8 * 60 * 60
SECRET_REF_PREFIX = "$secret:"
PBKDF2_ITERATIONS = 310_000

SENSITIVE_KEYS = {
    "api_key",
    "authorization",
    "client_secret",
    "connection_string",
    "cookie",
    "password",
    "password_confirm",
    "secret",
    "secret_string",
    "token",
    "vault_token",
}


class UserRole(str, Enum):
    VIEWER = "viewer"
    TESTER = "tester"
    APPROVER = "approver"
    ADMIN = "admin"


@dataclass(frozen=True)
class User:
    username: str
    password_hash: str
    role: UserRole
    display_name: str | None = None


@dataclass
class AuditEvent:
    timestamp: str
    event_type: str
    username: str | None
    role: str | None
    details: dict[str, Any] = field(default_factory=dict)


@dataclass
class ApprovalRecord:
    approval_id: str
    config_hash: str
    application_name: str
    release_id: str
    environment: str
    scenario_names: list[str]
    requested_by: str
    requested_at: str
    status: str = "pending"
    approved_by: str | None = None
    approved_at: str | None = None
    rejected_by: str | None = None
    rejected_at: str | None = None
    consumed_at: str | None = None
    run_id: str | None = None
    comment: str | None = None


class SessionManager:
    def __init__(self) -> None:
        self.sessions: dict[str, tuple[User, float]] = {}

    def create_session(self, user: User) -> str:
        session_id = secrets.token_urlsafe(32)
        self.sessions[session_id] = (user, time.time())
        return session_id

    def get_user(self, cookie_header: str | None) -> User | None:
        if not cookie_header:
            return None
        cookie = SimpleCookie()
        cookie.load(cookie_header)
        morsel = cookie.get(SESSION_COOKIE_NAME)
        if not morsel:
            return None
        entry = self.sessions.get(morsel.value)
        if not entry:
            return None
        user, last_seen = entry
        if time.time() - last_seen > SESSION_TIMEOUT_SECONDS:
            self.sessions.pop(morsel.value, None)
            return None
        self.sessions[morsel.value] = (user, time.time())
        return user

    def destroy_session(self, cookie_header: str | None) -> None:
        if not cookie_header:
            return
        cookie = SimpleCookie()
        cookie.load(cookie_header)
        morsel = cookie.get(SESSION_COOKIE_NAME)
        if morsel:
            self.sessions.pop(morsel.value, None)


class AuditLogger:
    def __init__(self, path: Path = AUDIT_LOG_PATH) -> None:
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def log(self, event_type: str, user: User | None, details: dict[str, Any] | None = None) -> None:
        event = AuditEvent(
            timestamp=datetime.now(UTC).isoformat(),
            event_type=event_type,
            username=user.username if user else None,
            role=user.role.value if user else None,
            details=redact_sensitive(details or {}),
        )
        with self.path.open("a", encoding="utf-8") as writer:
            writer.write(json.dumps(asdict(event), sort_keys=True) + "\n")

    def tail(self, limit: int = 100) -> list[AuditEvent]:
        if not self.path.exists():
            return []
        lines = self.path.read_text(encoding="utf-8").strip().splitlines()
        events: list[AuditEvent] = []
        for line in lines[-limit:]:
            try:
                events.append(AuditEvent(**json.loads(line)))
            except Exception:
                continue
        return events


class ApprovalManager:
    def __init__(self, path: Path = APPROVALS_PATH) -> None:
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._approvals: list[ApprovalRecord] | None = None

    def request(self, config: AgentConfig, user: User, comment: str | None = None) -> ApprovalRecord:
        risky = risky_scenario_names(config)
        if not risky:
            raise ValueError("Approval is only required for stress, spike, or endurance scenarios.")
        record = ApprovalRecord(
            approval_id=f"apr_{secrets.token_urlsafe(12)}",
            config_hash=config_fingerprint(config),
            application_name=config.application_name,
            release_id=config.release_id,
            environment=config.environment.name,
            scenario_names=risky,
            requested_by=user.username,
            requested_at=datetime.now(UTC).isoformat(),
            comment=comment,
        )
        approvals = self._load()
        approvals.append(record)
        self._save()
        return record

    def approve(self, approval_id: str, user: User, comment: str | None = None) -> ApprovalRecord:
        record = self._find(approval_id)
        if record.status != "pending":
            raise ValueError(f"Approval request is already {record.status}.")
        if record.requested_by == user.username and user.role != UserRole.ADMIN:
            raise ValueError("Requesters cannot approve their own risky test.")
        record.status = "approved"
        record.approved_by = user.username
        record.approved_at = datetime.now(UTC).isoformat()
        if comment:
            record.comment = comment
        self._save()
        return record

    def reject(self, approval_id: str, user: User, comment: str | None = None) -> ApprovalRecord:
        record = self._find(approval_id)
        if record.status != "pending":
            raise ValueError(f"Approval request is already {record.status}.")
        record.status = "rejected"
        record.rejected_by = user.username
        record.rejected_at = datetime.now(UTC).isoformat()
        if comment:
            record.comment = comment
        self._save()
        return record

    def validate(
        self,
        approval_id: str,
        config: AgentConfig,
        executor_username: str | None = None,
    ) -> ApprovalRecord:
        record = self._find(approval_id)
        if record.status != "approved":
            raise ValueError("Risky test approval is not approved.")
        if record.consumed_at:
            raise ValueError("Risky test approval has already been used.")
        if not hmac.compare_digest(record.config_hash, config_fingerprint(config)):
            raise ValueError("Risky test configuration changed after approval. Request a new approval.")
        if executor_username and record.requested_by != executor_username:
            raise ValueError("Risky test approval may only be used by its requester.")
        return record

    def consume(self, approval_id: str, run_id: str) -> ApprovalRecord:
        record = self._find(approval_id)
        if record.status != "approved" or record.consumed_at:
            raise ValueError("Only an unused approved request can be consumed.")
        record.status = "consumed"
        record.consumed_at = datetime.now(UTC).isoformat()
        record.run_id = run_id
        self._save()
        return record

    def list_for_user(self, user: User) -> list[ApprovalRecord]:
        approvals = self._load()
        if user.role in {UserRole.APPROVER, UserRole.ADMIN}:
            return list(reversed(approvals))
        return [item for item in reversed(approvals) if item.requested_by == user.username]

    def _find(self, approval_id: str) -> ApprovalRecord:
        for record in self._load():
            if record.approval_id == approval_id:
                return record
        raise ValueError("Approval request not found.")

    def _load(self) -> list[ApprovalRecord]:
        if self._approvals is not None:
            return self._approvals
        if not self.path.exists():
            self._approvals = []
            return self._approvals
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
            self._approvals = [
                ApprovalRecord(**item)
                for item in raw
                if isinstance(item, dict) and item.get("approval_id")
            ]
        except Exception:
            self._approvals = []
        return self._approvals

    def _save(self) -> None:
        assert self._approvals is not None
        self.path.write_text(
            json.dumps([asdict(item) for item in self._approvals], indent=2),
            encoding="utf-8",
        )


class SecretManager:
    @classmethod
    def get_secret(cls, ref: str) -> str:
        if not isinstance(ref, str) or not ref.startswith(SECRET_REF_PREFIX):
            raise ValueError("Secret reference must start with $secret:")
        parts = ref[len(SECRET_REF_PREFIX) :].split(":", 1)
        if len(parts) != 2 or not all(parts):
            raise ValueError("Secret reference must be $secret:<azure|aws|vault|env>:<name>")
        provider, name = parts[0].lower(), parts[1]
        if provider == "azure":
            return AzureKeyVaultSecretManager().get_secret(name)
        if provider == "aws":
            return AwsSecretsManager().get_secret(name)
        if provider == "vault":
            return VaultSecretManager().get_secret(name)
        if provider == "env":
            value = os.environ.get(name)
            if value is None:
                raise RuntimeError(f"Environment secret is not set: {name}")
            return value
        raise ValueError(f"Unsupported secret provider: {provider}")


class AzureKeyVaultSecretManager:
    def get_secret(self, name: str) -> str:
        vault_name = os.environ.get("AZURE_KEY_VAULT_NAME")
        tenant = os.environ.get("AZURE_TENANT_ID")
        client_id = os.environ.get("AZURE_CLIENT_ID")
        client_secret = os.environ.get("AZURE_CLIENT_SECRET")
        if not all([vault_name, tenant, client_id, client_secret]):
            raise RuntimeError(
                "Azure Key Vault requires AZURE_KEY_VAULT_NAME, AZURE_TENANT_ID, "
                "AZURE_CLIENT_ID, and AZURE_CLIENT_SECRET."
            )
        token = self._acquire_token(tenant, client_id, client_secret)
        url = f"https://{vault_name}.vault.azure.net/secrets/{urllib.parse.quote(name)}?api-version=7.3"
        return _request_json_secret(url, {"Authorization": f"Bearer {token}"}, ("value",))

    def _acquire_token(self, tenant: str, client_id: str, client_secret: str) -> str:
        data = urllib.parse.urlencode(
            {
                "client_id": client_id,
                "client_secret": client_secret,
                "grant_type": "client_credentials",
                "scope": "https://vault.azure.net/.default",
            }
        ).encode("utf-8")
        url = f"https://login.microsoftonline.com/{urllib.parse.quote(tenant)}/oauth2/v2.0/token"
        request = urllib.request.Request(url, data=data, method="POST")
        request.add_header("Content-Type", "application/x-www-form-urlencoded")
        with urllib.request.urlopen(request, timeout=30) as response:
            return str(json.loads(response.read().decode("utf-8"))["access_token"])


class AwsSecretsManager:
    def get_secret(self, name: str) -> str:
        try:
            import boto3
        except ImportError as exc:
            raise RuntimeError("AWS Secrets Manager support requires boto3") from exc
        secret = boto3.session.Session().client("secretsmanager").get_secret_value(SecretId=name)
        if "SecretString" in secret:
            return str(secret["SecretString"])
        if "SecretBinary" in secret:
            return base64.b64decode(secret["SecretBinary"]).decode("utf-8")
        raise RuntimeError("AWS secret contained no retrievable value")


class VaultSecretManager:
    def get_secret(self, path: str) -> str:
        addr = os.environ.get("VAULT_ADDR")
        token = os.environ.get("VAULT_TOKEN")
        if not addr or not token:
            raise RuntimeError("HashiCorp Vault support requires VAULT_ADDR and VAULT_TOKEN.")
        url = f"{addr.rstrip('/')}/v1/{path.lstrip('/')}"
        request = urllib.request.Request(url, method="GET")
        request.add_header("X-Vault-Token", token)
        with urllib.request.urlopen(request, timeout=30) as response:
            payload = json.loads(response.read().decode("utf-8"))
        data = payload.get("data", {})
        if isinstance(data, dict) and isinstance(data.get("data"), dict):
            data = data["data"]
        if isinstance(data, dict):
            value = data.get("value")
            if value is not None:
                return str(value)
        raise RuntimeError("Vault secret must contain a 'value' field.")


class UserStore:
    def __init__(self) -> None:
        self.users = {user.username: user for user in self._load_users()}

    def authenticate(self, username: str, password: str) -> User | None:
        user = self.users.get(username)
        if user and self.verify_password(password, user.password_hash):
            return user
        return None

    def register(self, email: str, password: str) -> User:
        allowed_domain = os.environ.get("PERF_AGENT_ALLOWED_EMAIL_DOMAIN", "").strip().lower()
        if not allowed_domain:
            raise ValueError("Self-service signup is disabled.")
        email = email.strip().lower()
        if email.count("@") != 1 or not email.endswith(f"@{allowed_domain}"):
            raise ValueError(f"Only @{allowed_domain} email addresses are allowed.")
        username = email.split("@", 1)[0]
        if username in self.users:
            raise ValueError("User already exists.")
        _validate_password(password)
        user = User(username, self.hash_password(password), UserRole.VIEWER, email)
        self.users[username] = user
        self._save_users()
        return user

    def _load_users(self) -> list[User]:
        raw_users = os.environ.get("PERF_AGENT_USERS_JSON")
        if raw_users:
            return self._parse_users(json.loads(raw_users))
        users_file = os.environ.get("PERF_AGENT_USERS_FILE")
        if users_file and Path(users_file).exists():
            return self._parse_users(json.loads(Path(users_file).read_text(encoding="utf-8")))
        legacy_error: RuntimeError | None = None
        if USERS_DB_PATH.exists():
            try:
                return self._parse_users(json.loads(USERS_DB_PATH.read_text(encoding="utf-8")))
            except RuntimeError as exc:
                legacy_error = exc
        admin_user = os.environ.get("PERF_AGENT_ADMIN_USER")
        admin_password = os.environ.get("PERF_AGENT_ADMIN_PASSWORD")
        if admin_user and admin_password:
            return [
                User(
                    username=admin_user,
                    password_hash=self.hash_password(admin_password),
                    role=UserRole.ADMIN,
                    display_name="Administrator",
                )
            ]
        if legacy_error:
            raise RuntimeError(
                "The persisted user database uses an insecure legacy password format. "
                "Configure a temporary admin account and recreate users."
            ) from legacy_error
        raise RuntimeError(
            "No users configured. Set PERF_AGENT_USERS_JSON, PERF_AGENT_USERS_FILE, "
            "or PERF_AGENT_ADMIN_USER/PERF_AGENT_ADMIN_PASSWORD."
        )

    def _parse_users(self, raw: Any) -> list[User]:
        if not isinstance(raw, list):
            raise RuntimeError("User configuration must be a JSON array.")
        users: list[User] = []
        for item in raw:
            username = str(item.get("username", "")).strip()
            role = UserRole(str(item.get("role", UserRole.VIEWER.value)).lower())
            password_hash = item.get("password_hash")
            password = item.get("password")
            if not username or (not password_hash and not password):
                raise RuntimeError("Each user requires username and password or password_hash.")
            if password_hash:
                encoded = str(password_hash)
            else:
                if not isinstance(password, str) or not password.startswith(SECRET_REF_PREFIX):
                    raise RuntimeError(
                        "User passwords in configuration must use password_hash or a $secret: reference."
                    )
                resolved = resolve_secret(password)
                encoded = self.hash_password(str(resolved))
            users.append(User(username, encoded, role, item.get("display_name")))
        return users

    def _save_users(self) -> None:
        USERS_DB_PATH.write_text(
            json.dumps(
                [
                    {
                        "username": user.username,
                        "password_hash": user.password_hash,
                        "role": user.role.value,
                        "display_name": user.display_name,
                    }
                    for user in self.users.values()
                ],
                indent=2,
            ),
            encoding="utf-8",
        )

    @staticmethod
    def hash_password(password: str) -> str:
        salt = secrets.token_bytes(16)
        digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, PBKDF2_ITERATIONS)
        return f"pbkdf2_sha256${PBKDF2_ITERATIONS}${salt.hex()}${digest.hex()}"

    @staticmethod
    def verify_password(password: str, password_hash: str) -> bool:
        try:
            algorithm, iterations, salt_hex, digest_hex = password_hash.split("$", 3)
            if algorithm != "pbkdf2_sha256":
                return False
            actual = hashlib.pbkdf2_hmac(
                "sha256",
                password.encode("utf-8"),
                bytes.fromhex(salt_hex),
                int(iterations),
            )
            return hmac.compare_digest(actual.hex(), digest_hex)
        except (ValueError, TypeError):
            return False


def resolve_secret(value: Any) -> Any:
    if isinstance(value, str) and value.startswith(SECRET_REF_PREFIX):
        return SecretManager.get_secret(value)
    return value


def resolve_secret_references(value: Any) -> Any:
    if isinstance(value, dict):
        resolved: dict[str, Any] = {}
        for key, item in value.items():
            if _is_sensitive_key(str(key)) and item is not None and item != "":
                if not isinstance(item, str) or not item.startswith(SECRET_REF_PREFIX):
                    raise ValueError(
                        f"Sensitive option '{key}' must use a $secret: reference."
                    )
            resolved[str(key)] = resolve_secret_references(item)
        return resolved
    if isinstance(value, list):
        return [resolve_secret_references(item) for item in value]
    return resolve_secret(value)


def require_secret_reference(value: Any, field_name: str) -> str | None:
    if value in {None, ""}:
        return None
    if not isinstance(value, str) or not value.startswith(SECRET_REF_PREFIX):
        raise ValueError(f"{field_name} must use a secret reference such as $secret:env:SECRET_NAME.")
    return str(resolve_secret(value))


def risky_scenario_names(config: AgentConfig) -> list[str]:
    return [scenario.name for scenario in config.scenarios if scenario.requires_approval]


def config_fingerprint(config: AgentConfig) -> str:
    canonical = json.dumps(redact_sensitive(asdict(config)), sort_keys=True, separators=(",", ":"), default=_json_default)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def redact_sensitive(value: Any) -> Any:
    if is_dataclass(value):
        value = asdict(value)
    if isinstance(value, dict):
        return {
            str(key): "<redacted>" if _is_sensitive_key(str(key)) else redact_sensitive(item)
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [redact_sensitive(item) for item in value]
    if isinstance(value, tuple):
        return [redact_sensitive(item) for item in value]
    if isinstance(value, str) and value.startswith(SECRET_REF_PREFIX):
        return "<secret-reference>"
    if isinstance(value, Enum):
        return value.value
    return value


def _is_sensitive_key(key: str) -> bool:
    lowered = key.lower()
    return lowered in SENSITIVE_KEYS or any(token in lowered for token in ("password", "secret", "token", "api_key"))


def _json_default(value: Any) -> Any:
    if isinstance(value, Enum):
        return value.value
    raise TypeError(f"Unsupported value: {type(value)!r}")


def _validate_password(password: str) -> None:
    if len(password) < 12:
        raise ValueError("Password must be at least 12 characters.")
    if not any(char.isupper() for char in password):
        raise ValueError("Password must include an uppercase letter.")
    if not any(char.islower() for char in password):
        raise ValueError("Password must include a lowercase letter.")
    if not any(char.isdigit() for char in password):
        raise ValueError("Password must include a number.")


def _request_json_secret(url: str, headers: dict[str, str], path: tuple[str, ...]) -> str:
    request = urllib.request.Request(url, method="GET", headers=headers)
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            payload: Any = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        raise RuntimeError(f"Secret manager request failed: {exc.code} {exc.reason}") from exc
    for key in path:
        payload = payload[key]
    return str(payload)
