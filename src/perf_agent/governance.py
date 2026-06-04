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
from dataclasses import asdict, dataclass, field
from datetime import datetime, time as dt_time
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
ALLOWED_EMAIL_DOMAIN = "ishir"


class UserRole(str):
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
    run_id: str
    scenario_name: str
    approved_by: str
    role: str
    approved_at: str
    comment: str | None = None


class SessionManager:
    def __init__(self) -> None:
        self.sessions: dict[str, tuple[User, float]] = {}

    def create_session(self, user: User) -> str:
        session_id = secrets.token_urlsafe(24)
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
        session_id = morsel.value
        entry = self.sessions.get(session_id)
        if not entry:
            return None
        user, created_at = entry
        if time.time() - created_at > SESSION_TIMEOUT_SECONDS:
            self.sessions.pop(session_id, None)
            return None
        self.sessions[session_id] = (user, time.time())
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
            timestamp=datetime.now().isoformat(),
            event_type=event_type,
            username=user.username if user else None,
            role=str(user.role) if user else None,
            details=details or {},
        )
        with self.path.open("a", encoding="utf-8") as writer:
            writer.write(json.dumps(asdict(event)) + "\n")

    def tail(self, limit: int = 100) -> list[AuditEvent]:
        if not self.path.exists():
            return []
        lines = self.path.read_text(encoding="utf-8").strip().splitlines()
        events: list[AuditEvent] = []
        for line in lines[-limit:]:
            try:
                raw = json.loads(line)
                events.append(AuditEvent(**raw))
            except Exception:
                continue
        return events


class ApprovalManager:
    def __init__(self, path: Path = APPROVALS_PATH) -> None:
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._approvals: list[ApprovalRecord] | None = None

    def _load(self) -> list[ApprovalRecord]:
        if self._approvals is not None:
            return self._approvals
        if not self.path.exists():
            self._approvals = []
            return self._approvals
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
            self._approvals = [ApprovalRecord(**item) for item in raw]
        except Exception:
            self._approvals = []
        return self._approvals

    def _save(self) -> None:
        assert self._approvals is not None
        self.path.write_text(json.dumps([asdict(item) for item in self._approvals], indent=2), encoding="utf-8")

    def approve(self, run_id: str, scenario_name: str, user: User, comment: str | None = None) -> ApprovalRecord:
        approvals = self._load()
        record = ApprovalRecord(
            run_id=run_id,
            scenario_name=scenario_name,
            approved_by=user.username,
            role=user.role.value,
            approved_at=datetime.now().isoformat(),
            comment=comment,
        )
        approvals.append(record)
        self._save()
        return record

    def list(self, run_id: str | None = None) -> list[ApprovalRecord]:
        approvals = self._load()
        if run_id is None:
            return approvals
        return [item for item in approvals if item.run_id == run_id]


class SecretManager:
    @classmethod
    def get_secret(cls, ref: str) -> str:
        if not isinstance(ref, str) or not ref.startswith(SECRET_REF_PREFIX):
            raise ValueError("Secret reference must start with $secret:")
        parts = ref[len(SECRET_REF_PREFIX) :].split(":", 1)
        if len(parts) != 2:
            raise ValueError("Secret reference must be in format $secret:<provider>:<name>")
        provider, name = parts
        provider = provider.lower()
        if provider == "azure":
            return AzureKeyVaultSecretManager().get_secret(name)
        if provider == "aws":
            return AwsSecretsManager().get_secret(name)
        if provider == "vault":
            return VaultSecretManager().get_secret(name)
        raise ValueError(f"Unsupported secret provider: {provider}")


class AzureKeyVaultSecretManager:
    def get_secret(self, name: str) -> str:
        vault_name = os.environ.get("AZURE_KEY_VAULT_NAME")
        tenant = os.environ.get("AZURE_TENANT_ID")
        client_id = os.environ.get("AZURE_CLIENT_ID")
        client_secret = os.environ.get("AZURE_CLIENT_SECRET")
        if not all([vault_name, tenant, client_id, client_secret]):
            raise RuntimeError("Azure Key Vault requires AZURE_KEY_VAULT_NAME, AZURE_TENANT_ID, AZURE_CLIENT_ID, and AZURE_CLIENT_SECRET.")
        token = self._acquire_token(tenant, client_id, client_secret)
        url = f"https://{vault_name}.vault.azure.net/secrets/{urllib.parse.quote(name)}?api-version=7.3"
        request = urllib.request.Request(url, method="GET")
        request.add_header("Authorization", f"Bearer {token}")
        request.add_header("Content-Type", "application/json")
        try:
            with urllib.request.urlopen(request, timeout=30) as response:
                payload = json.loads(response.read().decode("utf-8"))
                return payload.get("value", "")
        except urllib.error.HTTPError as exc:
            raise RuntimeError(f"Azure Key Vault request failed: {exc.code} {exc.reason}") from exc

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
        try:
            with urllib.request.urlopen(request, timeout=30) as response:
                payload = json.loads(response.read().decode("utf-8"))
                return payload["access_token"]
        except urllib.error.HTTPError as exc:
            raise RuntimeError(f"Azure token request failed: {exc.code} {exc.reason}") from exc


class AwsSecretsManager:
    def get_secret(self, name: str) -> str:
        try:
            import boto3
        except ImportError as exc:
            raise RuntimeError("AWS Secrets Manager support requires boto3") from exc
        session = boto3.session.Session()
        client = session.client("secretsmanager")
        secret = client.get_secret_value(SecretId=name)
        if "SecretString" in secret:
            return secret["SecretString"]
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
        request.add_header("Content-Type", "application/json")
        try:
            with urllib.request.urlopen(request, timeout=30) as response:
                payload = json.loads(response.read().decode("utf-8"))
                if "data" in payload and isinstance(payload["data"], dict):
                    data = payload["data"]
                    if "data" in data:
                        return str(data["data"].get("value", ""))
                    return str(data.get("value", ""))
                return str(payload.get("data", ""))
        except urllib.error.HTTPError as exc:
            raise RuntimeError(f"Vault request failed: {exc.code} {exc.reason}") from exc


class UserStore:
    def __init__(self) -> None:
        self.users = {user.username: user for user in self._load_users()}

    def _load_users(self) -> list[User]:
        # First, try to load from persistent storage
        if USERS_DB_PATH.exists():
            try:
                raw = json.loads(USERS_DB_PATH.read_text(encoding="utf-8"))
                if isinstance(raw, list):
                    users = self._parse_users(raw)
                    if users:
                        return users
            except Exception:
                pass
        
        # Fall back to environment variables
        raw_users = os.environ.get("PERF_AGENT_USERS_JSON")
        if raw_users:
            return self._parse_users(json.loads(raw_users))
        users_file = os.environ.get("PERF_AGENT_USERS_FILE")
        if users_file and Path(users_file).exists():
            return self._parse_users(json.loads(Path(users_file).read_text(encoding="utf-8")))
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
        raise RuntimeError("No Perf Agent users configured. Set PERF_AGENT_USERS_JSON, PERF_AGENT_USERS_FILE, or PERF_AGENT_ADMIN_USER/PERF_AGENT_ADMIN_PASSWORD.")

    def _parse_users(self, raw: Any) -> list[User]:
        if not isinstance(raw, list):
            raise RuntimeError("PERF_AGENT_USERS_JSON must be a JSON array of users")
        users: list[User] = []
        for item in raw:
            username = str(item.get("username", "")).strip()
            password = item.get("password")
            role = str(item.get("role", UserRole.VIEWER)).lower()
            if not username or not password:
                raise RuntimeError("Each user must include username and password")
            
            # Check if password is already a hash (SHA256 = 64 hex chars) or a secret ref
            if isinstance(password, str):
                if password.startswith(SECRET_REF_PREFIX):
                    password = SecretManager.get_secret(password)
                    password_hash = self.hash_password(str(password))
                elif len(password) == 64 and all(c in "0123456789abcdef" for c in password.lower()):
                    # Already a SHA256 hash from saved database
                    password_hash = password
                else:
                    # Plain password, needs hashing
                    password_hash = self.hash_password(str(password))
            else:
                password_hash = self.hash_password(str(password))
            
            users.append(
                User(
                    username=username,
                    password_hash=password_hash,
                    role=UserRole(role),
                    display_name=str(item.get("display_name")) if item.get("display_name") else None,
                )
            )
        return users

    @staticmethod
    def hash_password(password: str) -> str:
        digest = hashlib.sha256(password.encode("utf-8")).hexdigest()
        return digest

    @staticmethod
    def verify_password(password: str, password_hash: str) -> bool:
        return hmac.compare_digest(
            hashlib.sha256(password.encode("utf-8")).hexdigest(),
            password_hash,
        )

    def authenticate(self, username: str, password: str) -> User | None:
        user = self.users.get(username)
        if not user:
            return None
        if self.verify_password(password, user.password_hash):
            return user
        return None

    def register(self, email: str, password: str) -> User:
        """Register a new user with email domain validation."""
        email = email.strip().lower()
        
        # Validate email format and domain
        if not self._is_valid_email(email):
            raise ValueError("Invalid email format.")
        
        if not email.endswith(f"@{ALLOWED_EMAIL_DOMAIN}"):
            raise ValueError(f"Only @{ALLOWED_EMAIL_DOMAIN} domain emails are allowed.")
        
        # Extract username from email
        username = email.split("@")[0]
        
        # Check if user already exists
        if username in self.users:
            raise ValueError("User already exists.")
        
        # Validate password
        if not password or len(password) < 6:
            raise ValueError("Password must be at least 6 characters long.")
        
        # Create new user
        new_user = User(
            username=username,
            password_hash=self.hash_password(password),
            role=UserRole.VIEWER,
            display_name=email,
        )
        
        # Save to persistent storage
        self._save_user(new_user)
        
        # Add to in-memory store
        self.users[username] = new_user
        
        return new_user

    def _is_valid_email(self, email: str) -> bool:
        """Basic email validation."""
        if not email or "@" not in email:
            return False
        parts = email.split("@")
        if len(parts) != 2:
            return False
        local, domain = parts
        if not local or not domain:
            return False
        if not domain:
            return False
        return True

    def _save_user(self, user: User) -> None:
        """Save user to persistent storage."""
        users_data = []
        
        # Load existing users
        if USERS_DB_PATH.exists():
            try:
                users_data = json.loads(USERS_DB_PATH.read_text(encoding="utf-8"))
            except Exception:
                users_data = []
        
        # Add new user
        users_data.append({
            "username": user.username,
            "password": user.password_hash,  # Store hash
            "role": str(user.role),
            "display_name": user.display_name,
        })
        
        # Save to file
        USERS_DB_PATH.write_text(json.dumps(users_data, indent=2), encoding="utf-8")


def resolve_secret(value: Any) -> Any:
    if isinstance(value, str) and value.startswith(SECRET_REF_PREFIX):
        return SecretManager.get_secret(value)
    return value
